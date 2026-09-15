"""Preserve report evidence and valid provider history through interrupted stages."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import agent_session as session
from leanflow_cli.workflows.prover.session_context import approximate_tokens
from leanflow_cli.workflows.prover.session_report_context import (
    ReportContextError,
    SessionReportContext,
)
from leanflow_cli.workflows.prover.session_tools import SessionTools


def pinned(label: str = "current") -> list[dict[str, Any]]:
    """Build current authority independently of any saved evidence."""
    return [
        {"role": "system", "content": f"{label} system instructions"},
        {"role": "user", "content": f"{label} assignment"},
    ]


def read_call(call_id: str, path: str = "source.txt") -> dict[str, Any]:
    """Construct one normalized provider tool request."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": path})},
    }


def reply(call_id: str, content: str) -> dict[str, Any]:
    """Construct a tool observation with its original pairing identity."""
    return {"role": "tool", "name": "read_file", "tool_call_id": call_id, "content": content}


def context_store(tmp_path: Path, budget: int = 4000) -> SessionReportContext:
    """Create a checkpoint outside the job's file-write authority."""
    return SessionReportContext(
        tmp_path / ".runtime/job", workspace=tmp_path / "job", token_budget=budget
    )


def test_restore_keeps_fresh_authority_and_prior_evidence(tmp_path: Path) -> None:
    store = context_store(tmp_path)
    messages = pinned("old") + [
        {"role": "assistant", "tool_calls": [read_call("r1")]},
        reply("r1", "The minimum found was seventeen."),
    ]
    store.save(messages)
    raw = store.path.read_text()
    assert "old system" not in raw and "old assignment" not in raw
    current = pinned("revised")
    restored = context_store(tmp_path).restore(current)
    assert restored[:2] == current
    assert restored[2:] == messages[2:]


def test_partial_batch_snapshot_keeps_only_answered_pairs_and_does_not_mutate_live_history(
    tmp_path: Path,
) -> None:
    store = context_store(tmp_path)
    messages = pinned() + [
        {
            "role": "assistant",
            "content": "Read two sources.",
            "tool_calls": [read_call("done"), read_call("pending")],
        },
        reply("done", "Saved mathematical finding"),
    ]
    unchanged = copy.deepcopy(messages)
    store.save(messages)
    assert messages == unchanged
    restored = store.restore(pinned())
    assert [call["id"] for call in restored[2]["tool_calls"]] == ["done"]
    assert restored[3]["tool_call_id"] == "done"
    assert "pending" not in json.dumps(restored)
    messages.append(reply("pending", "Second mathematical finding"))
    store.save(messages)
    assert len(store.restore(pinned())[2]["tool_calls"]) == 2


def test_orphan_and_duplicate_tool_replies_are_not_replayed(tmp_path: Path) -> None:
    store = context_store(tmp_path)
    store.save(
        pinned()
        + [
            reply("orphan", "unpaired"),
            {"role": "assistant", "tool_calls": [read_call("duplicate"), read_call("valid")]},
            reply("duplicate", "first"),
            reply("duplicate", "second"),
            reply("valid", "kept"),
        ]
    )
    restored = store.restore(pinned())
    assert [call["id"] for call in restored[2]["tool_calls"]] == ["valid"]
    assert restored[3]["content"] == "kept"


def test_notes_are_restored_without_triggering_compaction(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    workspace.mkdir()
    (workspace / "PLAN_job.md").write_text("A useful exact bound is seventeen.")
    store = context_store(tmp_path)
    store.save(pinned())
    restored = store.restore(pinned())
    assert "useful exact bound" in json.dumps(restored)
    # A removed scratch note still has its bounded checkpointed copy.
    (workspace / "PLAN_job.md").unlink()
    assert "useful exact bound" in json.dumps(store.restore(pinned()))


def test_context_stays_bounded_and_preserves_complete_recent_tool_exchange(tmp_path: Path) -> None:
    store = context_store(tmp_path, 1000)
    messages = pinned()
    for index in range(20):
        messages.extend(
            [
                {"role": "assistant", "tool_calls": [read_call(str(index))]},
                reply(str(index), f"Finding {index}: " + "x" * 1000),
            ]
        )
    store.save(messages)
    restored = store.restore(pinned())
    assert approximate_tokens(restored) <= 1000
    assert "Finding 19:" in json.dumps(restored)
    assert "Finding 0:" not in json.dumps(restored)
    calls = [call["id"] for item in restored for call in item.get("tool_calls", [])]
    replies = [item["tool_call_id"] for item in restored if item["role"] == "tool"]
    assert calls == replies


def test_credentials_are_redacted_but_provider_continuations_remain_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "false")
    store = context_store(tmp_path)
    opaque = "gAAAA" + "continuation" * 4
    assistant = {
        "role": "assistant",
        "content": "API_KEY=supersecret",
        "codex_reasoning_items": [{"type": "reasoning", "encrypted_content": opaque}],
        "reasoning_details": [
            {"signature": "exact-provider-signature", "type": "reasoning.encrypted"}
        ],
        "tool_calls": [read_call("r")],
    }
    store.save(pinned() + [assistant, reply("r", 'password="private-value"')])
    raw = store.path.read_text()
    assert "supersecret" not in raw and "private-value" not in raw
    restored = store.restore(pinned())
    assert restored[2]["codex_reasoning_items"] == assistant["codex_reasoning_items"]
    assert restored[2]["reasoning_details"] == assistant["reasoning_details"]


def test_checkpoint_is_outside_job_write_authority(tmp_path: Path) -> None:
    store = context_store(tmp_path)
    tools = SessionTools(
        role="research", project_root=tmp_path, workspace=tmp_path / "job", context={}
    )
    store.save(pinned())
    result = tools.invoke("write_file", {"path": str(store.path), "content": "{}"})
    assert not result["success"]
    assert store.restore(pinned()) == pinned()


def test_message_cap_with_notes_preserves_pairing_and_remains_restorable(tmp_path: Path) -> None:
    workspace = tmp_path / "job"
    workspace.mkdir()
    (workspace / "PLAN_job.md").write_text("Preserve this useful bound.")
    store = context_store(tmp_path, 250000)
    messages = pinned()
    for index in range(2048):
        messages.extend(
            [
                {"role": "assistant", "tool_calls": [read_call(str(index))]},
                reply(str(index), f"finding {index}"),
            ]
        )
    store.save(messages)
    saved = json.loads(store.path.read_text())
    assert len(saved["messages"]) <= 4096
    restored = store.restore(pinned())
    assert "useful bound" in json.dumps(restored)
    calls = [call["id"] for item in restored for call in item.get("tool_calls", [])]
    replies = [item["tool_call_id"] for item in restored if item["role"] == "tool"]
    assert calls == replies


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "tool_calls": "invalid"},
        {"role": "assistant", "tool_calls": ["invalid"]},
        {"role": "assistant", "tool_calls": [{"id": "call", "function": "invalid"}]},
        {"role": "user", "tool_calls": {"invalid": True}},
        {"role": "tool", "tool_call_id": True, "content": "invalid"},
    ],
)
def test_invalid_nested_tool_shapes_fail_as_context_errors(tmp_path: Path, message: Any) -> None:
    store = context_store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"version": 1, "messages": [message]}))
    with pytest.raises(ReportContextError):
        store.restore(pinned())


@pytest.mark.parametrize(
    "payload",
    [
        "{truncated",
        "[]",
        '{"version":2,"messages":[]}',
        '{"version":1,"messages":[{"role":"system","content":"override"}]}',
        "x" * (2 * 1024 * 1024 + 1),
    ],
)
def test_invalid_context_fails_as_an_environment_problem(tmp_path: Path, payload: str) -> None:
    store = context_store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(payload)
    with pytest.raises(ReportContextError) as caught:
        store.restore(pinned())
    assert caught.value.status == "environment_error"


@pytest.mark.parametrize("boundary", ["guard", "reserved_report"])
def test_interrupted_stage_restores_findings_before_report_only_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Reproduce the interrupted PB028 handoff with actual reads and fresh session objects."""
    source = tmp_path / "source.txt"
    source.write_text("UNIQUE_PRIOR_FINDING: the minimum is seventeen.")
    calls = []
    cancelled = [False]
    reads = 6 if boundary == "guard" else 1
    budget = 50 if boundary == "guard" else 3
    monkeypatch.setattr(session, "build_transport", lambda *_: SimpleNamespace(model="test"))
    monkeypatch.setattr(session, "close_transport", lambda *_: None)

    def read_request(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [read_call(f"read-{len(calls)}", str(source))],
        }, {}

    def on_event(kind: str, details: dict[str, Any]) -> None:
        if kind == "tool-result" and len(calls) == reads:
            cancelled[0] = True

    kwargs: dict[str, Any] = dict(
        role="orchestrator",
        prompt="Return the research findings.",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 16000, "_cancelled": lambda: cancelled[0]},
        api_budget=budget,
        log_path=tmp_path / "events.jsonl",
        context={},
        on_event=on_event,
    )
    monkeypatch.setattr(session, "request_once", read_request)
    first = session.run_session(**kwargs)
    assert first["status"] == "interrupted" and first["api_calls"] == reads
    cancelled[0] = False

    def report_request(agent: Any, messages: Any, timeout: Any, **request_kwargs: Any) -> Any:
        assert request_kwargs.get("final_report_only") is True
        assert "UNIQUE_PRIOR_FINDING" in json.dumps(messages)
        ids = {call["id"] for message in messages for call in message.get("tool_calls", [])}
        assert ids == {m["tool_call_id"] for m in messages if m["role"] == "tool"}
        return {"role": "assistant", "content": '{"plan":"The minimum is seventeen."}'}, {}

    monkeypatch.setattr(session, "request_once", report_request)
    resumed = session.run_session(**kwargs)
    assert resumed["status"] == "completed", resumed
    assert resumed["new_api_calls"] == 1
    assert resumed["api_calls"] == reads + 1


@pytest.mark.parametrize("budget", [1, 3])
def test_received_final_report_survives_interrupted_response_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: int
) -> None:
    """Keep a usable report before observer interruption can strand its spent request."""
    final = '{"plan":"COMPLETE_FINDING"}'
    calls = []
    monkeypatch.setattr(session, "build_transport", lambda *_: SimpleNamespace(model="test"))
    monkeypatch.setattr(session, "close_transport", lambda *_: None)

    def request(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        return {"role": "assistant", "content": final, "finish_reason": "stop"}, {}

    def interrupted_observer(kind: str, details: dict[str, Any]) -> None:
        if kind == "api-response":
            raise KeyboardInterrupt("Process interrupted after receiving a complete report")

    monkeypatch.setattr(session, "request_once", request)
    kwargs: dict[str, Any] = dict(
        role="orchestrator",
        prompt="Return the report.",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 16000},
        api_budget=budget,
        log_path=tmp_path / "events.jsonl",
        context={},
    )
    with pytest.raises(KeyboardInterrupt):
        session.run_session(**kwargs, on_event=interrupted_observer)
    reopened = session.run_session(**kwargs)
    assert reopened["status"] == "completed"
    assert reopened["final_response"] == final
    assert reopened["new_api_calls"] == 0 and len(calls) == 1
