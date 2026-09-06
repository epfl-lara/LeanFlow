"""Compact prover rows must say what happened and stay joinable to full records."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import agent_session as session
from leanflow_cli.workflows.prover import observer as observer_module
from leanflow_cli.workflows.prover.event_preview import (
    MESSAGE_CHARS,
    PREVIEW_CHARS,
    event_message,
    preview_details,
)
from leanflow_cli.workflows.prover.store import RunStore


def _assistant(content: str = "", **extra: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": content, **extra}


def test_api_response_preview_bounds_text_and_summarizes_tool_calls() -> None:
    long = "Consider the floating-variable argument. " * 40
    details = {
        "api_calls": 3,
        "assistant": _assistant(
            long,
            reasoning="**Checking the invariant**",
            finish_reason="tool_calls",
            tool_calls=[
                {
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "PLAN_job.md", "content": "y" * 900}),
                    },
                },
                {
                    "type": "function",
                    "function": {"name": "lean_search", "arguments": '{"query":"Finset.sum_le"}'},
                },
            ],
        ),
    }
    preview = preview_details("api-response", details)
    assert len(preview["content_preview"]) <= PREVIEW_CHARS
    assert preview["content_preview"].endswith("…")
    assert preview["content_chars"] == len(long)
    assert preview["reasoning_preview"] == "**Checking the invariant**"
    assert preview["finish_reason"] == "tool_calls"
    assert preview["tool_call_count"] == 2
    assert [call["name"] for call in preview["tool_calls"]] == ["write_file", "lean_search"]
    assert len(preview["tool_calls"][0]["arguments_preview"]) <= 300
    message = event_message("api-response", details, preview)
    assert len(message) <= MESSAGE_CHARS
    assert message.startswith("Consider the floating-variable argument.")
    assert "2 tool calls: write_file, lean_search" in message


def test_tool_only_response_and_reasoning_only_response_have_messages() -> None:
    tools = {
        "assistant": _assistant(
            tool_calls=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.md"}'},
                }
            ]
        )
    }
    assert (
        event_message("api-response", tools, preview_details("api-response", tools))
        == 'read_file({"path":"a.md"})'
    )
    thinking = {"assistant": _assistant(reasoning="**Mapping helper dependencies**")}
    assert (
        event_message("api-response", thinking, preview_details("api-response", thinking))
        == "Reasoning: **Mapping helper dependencies**"
    )
    assert event_message("api-response", {"assistant": _assistant()}, {}) == "Empty response"


def test_tool_result_preview_reports_outcome_and_bounded_payload() -> None:
    ok = {
        "tool": "read_file",
        "arguments": '{"path":"x.lean","limit":40}',
        "result": {"success": True, "content": "z" * 5000},
    }
    preview = preview_details("tool-result", ok)
    assert preview["success"] is True and preview["result_chars"] > 5000
    assert len(preview["result_preview"]) <= PREVIEW_CHARS
    assert (
        event_message("tool-result", ok, preview) == 'read_file({"path":"x.lean","limit":40}) → ok'
    )
    failed = {
        "tool": "lean_check",
        "arguments": "{}",
        "result": {"success": False, "error": "unknown identifier 'foo'"},
    }
    preview = preview_details("tool-result", failed)
    assert preview["error"] == "unknown identifier 'foo'"
    assert (
        event_message("tool-result", failed, preview)
        == "lean_check({}) → failed: unknown identifier 'foo'"
    )


def test_previews_redact_credentials() -> None:
    details = {
        "tool": "terminal",
        "arguments": "{}",
        "result": {
            "success": True,
            "content": "export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789",
        },
    }
    preview = preview_details("tool-result", details)
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in json.dumps(preview)


@pytest.mark.parametrize(
    "kind,details,expected",
    [
        (
            "api-request",
            {"api_calls": 10, "api_budget": 50, "model": "gpt-6"},
            "Request 10/50 · gpt-6",
        ),
        (
            "api-request",
            {"api_calls": 50, "api_budget": 50, "final_report_only": True},
            "Request 50/50 (final report, tools disabled)",
        ),
        (
            "job-session-start",
            {"role": "prover", "api_budget": 300, "api_calls": 0},
            "prover session started · budget 300 calls",
        ),
        (
            "job-session-end",
            {"status": "completed", "api_calls": 21, "final_response": '{"plan": "Use induction"}'},
            'Session completed after 21 calls · {"plan": "Use induction"}',
        ),
        (
            "job_finished",
            {"job_id": "prover_00004", "status": "budget_exhausted", "api_calls": 300},
            "prover_00004 budget_exhausted · 300 calls",
        ),
        ("api-error", {"api_calls": 4, "error": "timed out"}, "Provider error: timed out"),
        (
            "context-compacted",
            {"method": "deterministic", "api_calls": 12},
            "Context compacted at call 12",
        ),
        (
            "submission-feedback",
            {"accepted": True, "conditional": False},
            "Candidate accepted by the independent check",
        ),
        (
            "submission-feedback",
            {"accepted": False, "error": "sorry remains"},
            "Candidate rejected: sorry remains",
        ),
        ("candidate_checked", {"node_id": "n_1", "accepted": False}, "Candidate rejected for n_1"),
        (
            "submission_checked",
            {"node_id": "n_2", "accepted": True, "conditional": True},
            "Submission accepted for n_2 (conditional)",
        ),
        (
            "plan_rejected",
            {"reason": "cycle between lemma A and B"},
            "Plan rejected: cycle between lemma A and B",
        ),
        ("user-guidance-received", {"message_id": "m1"}, "User guidance delivered to the job"),
        ("user_message", {"message": "Try compactness."}, "User message: Try compactness."),
        ("libraries_installed", {"accepted": True}, "Planned libraries installed"),
        ("job_cleanup_error", {"error": "disk full"}, "disk full"),
        ("something_new", {}, "something_new"),
    ],
)
def test_messages_describe_the_event(kind: str, details: dict, expected: str) -> None:
    assert event_message(kind, details, preview_details(kind, details)) == expected


def test_observer_projects_readable_rows_with_previews_and_evidence_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # RunObserver writes these process globals directly; register them for teardown
    # so later status tests do not append to this test's project directory.
    for name in (
        "LEANFLOW_PROJECT_ROOT",
        "LEANFLOW_WORKFLOW_RUN_ID",
        "LEANFLOW_NATIVE_WORKFLOW_KIND",
    ):
        monkeypatch.setenv(name, "")
    rows: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        observer_module,
        "append_workflow_activity",
        lambda kind, message, **details: rows.append((kind, message, details)),
    )
    monkeypatch.setattr(observer_module, "append_workflow_run_log", lambda *_: None)
    observer = observer_module.RunObserver(tmp_path, "run-1")
    observer.event(
        "api-response",
        {
            "job_id": "prover_00001",
            "node_id": "n1",
            "evidence_id": "abc123def456",
            "api_calls": 2,
            "input_tokens": 100,
            "output_tokens": 20,
            "assistant": _assistant(
                "The base case follows by simp; now the step.",
                tool_calls=[
                    {
                        "type": "function",
                        "function": {"name": "lean_check", "arguments": '{"code":"simp"}'},
                    }
                ],
            ),
        },
    )
    ((kind, message, details),) = rows
    assert kind == "api-response"
    assert message == 'The base case follows by simp; now the step. · lean_check({"code":"simp"})'
    assert details["agent_session_id"] == "prover_00001"
    assert details["evidence_id"] == "abc123def456"
    assert details["content_preview"].startswith("The base case")
    assert details["tool_calls"] == [{"name": "lean_check", "arguments_preview": '{"code":"simp"}'}]
    assert "assistant" not in details, "full transcripts stay out of the shared stream"


def test_store_mints_and_preserves_evidence_ids(tmp_path: Path) -> None:
    (tmp_path / ".leanflow").mkdir()
    store = RunStore(tmp_path, "run-1")
    seen: list[tuple[str, dict[str, Any]]] = []
    store.on_event = lambda kind, details: seen.append((kind, details))
    store.event("candidate_checked", {"node_id": "n1", "accepted": True})
    store.event(
        "api-response",
        {"job_id": "j1", "evidence_id": "shared0001ab", "assistant": _assistant("hi")},
    )
    records = [
        json.loads(line) for line in (store.directory / "events.jsonl").read_text().splitlines()
    ]
    minted = records[0]["evidence_id"]
    assert len(minted) == 12 and seen[0][1]["evidence_id"] == minted
    assert records[1]["evidence_id"] == seen[1][1]["evidence_id"] == "shared0001ab"


def test_session_log_and_projected_events_share_one_evidence_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, "build_transport", lambda *_: SimpleNamespace(model="test"))
    monkeypatch.setattr(session, "close_transport", lambda *_: None)
    monkeypatch.setattr(
        session,
        "request_once",
        lambda *_a, **_k: (
            _assistant("I cannot finish this yet."),
            {"input_tokens": 3, "output_tokens": 2},
        ),
    )
    projected: list[tuple[str, dict[str, Any]]] = []
    session.run_session(
        role="prover",
        prompt="Prove it",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 16000},
        api_budget=1,
        log_path=tmp_path / "job.jsonl",
        context={"job_id": "prover_00001", "run_id": "run-1"},
        on_event=lambda kind, details: projected.append((kind, details)),
    )
    logged = [json.loads(line) for line in (tmp_path / "job.jsonl").read_text().splitlines()]
    assert [entry["type"] for entry in logged] == [kind for kind, _ in projected]
    for entry, (_, details) in zip(logged, projected, strict=True):
        assert entry["evidence_id"] == details["evidence_id"]
        assert "evidence_id" not in entry["details"]
    response = next(entry for entry in logged if entry["type"] == "api-response")
    assert response["details"]["assistant"]["content"] == "I cannot finish this yet."
