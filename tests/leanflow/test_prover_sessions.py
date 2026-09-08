"""Exercise fixed request ceilings and scratch-only session authority."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import agent_session as session
from leanflow_cli.workflows.prover.runtime import BudgetExhausted
from leanflow_cli.workflows.prover.session_context import compact_history
from leanflow_cli.workflows.prover.session_tools import SessionTools


def run_fake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: Any, **overrides: Any
) -> dict[str, Any]:
    """Run the real session loop against an injected provider response."""
    monkeypatch.setattr(session, "build_transport", lambda *_: SimpleNamespace(model="test"))
    monkeypatch.setattr(session, "close_transport", lambda *_: None)
    monkeypatch.setattr(session, "request_once", request)
    return session.run_session(
        role="prover",
        prompt="Prove the assigned statement",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test", "context_tokens": 16000, **overrides.pop("config", {})},
        api_budget=overrides.pop("api_budget", 3),
        log_path=tmp_path / "job.jsonl",
        context=overrides.pop("context", {}),
        **overrides,
    )


def test_early_give_up_continues_without_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def request(
        _agent: Any, messages: Any, _timeout: float
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        calls.append(messages)
        return {"role": "assistant", "content": "I cannot solve this"}, {
            "input_tokens": 10,
            "output_tokens": 5,
        }

    result = run_fake(tmp_path, monkeypatch, request)
    assert result["status"] == "budget_exhausted"
    assert result["api_calls"] == len(calls) == 3
    assert result["input_tokens"] == 30
    assert result["cost_usd"] is None
    resumed = run_fake(tmp_path, monkeypatch, request)
    assert resumed["api_calls"] == 3 and resumed["new_api_calls"] == 0
    assert len(calls) == 3


def test_provider_failure_consumes_admission_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def request(*_args: Any) -> Any:
        ledger = json.loads((tmp_path / ".runtime/job/request-count.json").read_text())
        assert ledger["used"] == 1
        raise TimeoutError("provider timed out")

    result = run_fake(tmp_path, monkeypatch, request)
    assert result["status"] == "provider_error"
    assert result["api_calls"] == 1
    assert (tmp_path / "job/report.json").is_file()


def test_completed_candidate_ends_without_report_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = run_fake(
        tmp_path,
        monkeypatch,
        lambda *_: ({"role": "assistant", "content": '{"proof":"by trivial"}'}, {}),
    )
    assert result["status"] == "completed"
    assert result["api_calls"] == 1


def test_same_job_cannot_expand_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = lambda *_: ({"role": "assistant", "content": '{"proof":"by trivial"}'}, {})
    run_fake(tmp_path, monkeypatch, request)
    with pytest.raises(ValueError, match="cannot change"):
        run_fake(tmp_path, monkeypatch, request, api_budget=4)


def test_compaction_preserves_pinned_context_and_complete_tool_pairs(tmp_path: Path) -> None:
    messages = [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "assignment DAG PLAN"},
    ]
    messages += [
        {"role": "assistant", "content": "old" * 3000},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "new evidence"},
    ]
    result, changed = compact_history(messages, context_tokens=150, workspace=tmp_path)
    assert changed and result[:2] == messages[:2]
    assert result[-2:] == messages[-2:]


def test_tools_enforce_role_paths_and_symlinks(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    canonical = tmp_path / "Main.lean"
    canonical.write_text("theorem goal : True := by sorry")
    (job / "escape.lean").symlink_to(canonical)
    prover = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})
    for path in (
        str(canonical),
        "../Main.lean",
        "escape.lean",
        "PLAN.md",
        "request-count.json",
        "../.runtime/job/request-count.json",
    ):
        assert not prover.invoke("write_file", {"path": path, "content": "changed"})["success"]
    assert prover.invoke("write_file", {"path": "PLAN_job.md", "content": "Try trivial"})["success"]
    assert canonical.read_text().endswith("by sorry")
    assert not prover.invoke("lean_reasoning_help", {})["success"]
    orchestrator = SessionTools(
        role="orchestrator", project_root=tmp_path, workspace=job, context={}
    )
    assert not orchestrator.invoke("lean_check", {})["success"]
    assert not orchestrator.invoke("lean_search", {"query": "trivial"})["success"]


def test_read_file_previews_a_non_utf8_document(tmp_path: Path) -> None:
    """A latin-1 .tex/.bib doc (now reachable via search_project) must preview.

    Decoding with replacement means a non-UTF-8 byte yields the replacement
    character instead of failing the whole read, and byte offsets keep working.
    """
    doc = tmp_path / "notes.tex"
    doc.write_bytes(b"Cauchy-Schwarz r\xe9sum\xe9\n")  # 0xe9 is invalid UTF-8
    job = tmp_path / "job"
    job.mkdir()
    prover = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})

    result = prover.invoke("read_file", {"path": str(doc)})

    assert result["success"] is True
    assert "Cauchy" in result["content"] and "Schwarz" in result["content"]
    assert "�" in result["content"]  # the non-UTF-8 bytes became U+FFFD


def test_read_file_pagination_never_skips_source_bytes(tmp_path: Path) -> None:
    """next_offset counts SOURCE bytes, not re-encoded replacement-char bytes.

    A malformed byte decodes to a 3-byte U+FFFD; counting the re-encoded length
    would advance the offset past unread bytes and drop them. Reading one char
    at a time must visit every byte exactly once.
    """
    doc = tmp_path / "notes.tex"
    doc.write_bytes(b"\xe9ABC")  # bad lead byte then three ASCII bytes
    job = tmp_path / "job"
    job.mkdir()
    prover = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})

    seen = []
    offset = 0
    for _ in range(10):
        out = prover.invoke("read_file", {"path": str(doc), "offset": offset, "limit": 1})
        assert out["success"] is True
        if out["next_offset"] == offset:  # end of file
            break
        seen.append(out["content"])
        offset = out["next_offset"]

    assert "".join(seen) == "�ABC"  # the 'A' and 'B' are not skipped
    assert offset == 4  # every one of the four source bytes was consumed


def test_read_file_malformed_multibyte_respects_the_character_limit(tmp_path: Path) -> None:
    """A malformed multibyte sequence must not overflow the requested char limit.

    Bytes like F4 90 80 80 are continuation-shaped but above U+10FFFF, so Python
    decodes them to SEVERAL U+FFFD; a request for one character must still return
    one, not the whole run.
    """
    from leanflow_cli.workflows.prover.session_tools import _utf8_window

    for raw in (b"\xf4\x90\x80\x80\x41", b"\xed\xa0\x80A", b"\xc0\x80B", b"\xf5\xf5A"):
        content, consumed = _utf8_window(raw, 1)
        assert len(content) == 1  # never more than the limit
        assert 1 <= consumed <= len(raw)  # forward progress, within the window

    doc = tmp_path / "bad.tex"
    doc.write_bytes(b"\xf4\x90\x80\x80done")
    job = tmp_path / "job"
    job.mkdir()
    prover = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})
    out = prover.invoke("read_file", {"path": str(doc), "limit": 1})
    assert out["success"] is True and len(out["content"]) == 1


def test_tool_request_uses_same_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = 0

    def request(*_args: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal count
        count += 1
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": str(count),
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps(
                            {"path": "PLAN_job.md", "content": f"Attempt {count}"}
                        ),
                    },
                }
            ],
        }, {}

    result = run_fake(tmp_path, monkeypatch, request)
    assert result["api_calls"] == count == 3
    assert (tmp_path / "job/PLAN_job.md").read_text() == "Attempt 3"


def test_rejected_submission_keeps_same_job_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = []

    def feedback(response):
        checks.append(response)
        return {"accepted": len(checks) == 3, "error": "unsolved goals"}

    result = run_fake(
        tmp_path,
        monkeypatch,
        lambda *_: ({"role": "assistant", "content": '{"proof":"by trivial"}'}, {}),
        config={"_candidate_feedback": feedback},
    )
    assert result["status"] == "completed"
    assert result["api_calls"] == 3 == len(checks)
    ledger = json.loads((tmp_path / ".runtime/job/request-count.json").read_text())
    assert (ledger["limit"], ledger["used"]) == (3, 3)
    assert ledger["usage"]["api_calls"] == 3


def test_cancellation_prevents_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    cancelled = threading.Event()
    calls = []

    def request(*_args):
        calls.append(1)
        cancelled.set()
        return {"role": "assistant", "content": "Need another attempt"}, {}

    result = run_fake(tmp_path, monkeypatch, request, config={"_cancelled": cancelled.is_set})
    assert result["status"] == "interrupted"
    assert len(calls) == result["api_calls"] == 1


def test_candidate_file_submission_needs_no_extra_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = lambda *_: (
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "submit",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "candidate.txt", "content": "rfl"}),
                    },
                }
            ],
        },
        {},
    )
    result = run_fake(
        tmp_path, monkeypatch, request, config={"_candidate_feedback": lambda _: {"accepted": True}}
    )
    assert result["status"] == "completed" and result["api_calls"] == 1


def test_context_admission_counts_tool_schemas_before_charging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, "skill_guidance", lambda *_: "")
    monkeypatch.setattr(
        SessionTools, "schemas", lambda _: [{"description": "large schema " * 2000}]
    )
    calls = []
    result = run_fake(
        tmp_path,
        monkeypatch,
        lambda *_: (calls.append(1) or {"role": "assistant", "content": '{"proof":"trivial"}'}, {}),
        config={"context_tokens": 2000},
    )
    assert result["status"] == "context_limit"
    assert result["api_calls"] == 0 and calls == []


def test_context_admission_counts_per_request_budget_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, "skill_guidance", lambda *_: "")
    monkeypatch.setattr(SessionTools, "schemas", lambda _: [])
    captured = []

    def request(_agent, messages, _timeout):
        captured.append(messages)
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    run_fake(tmp_path / "reference", monkeypatch, request)
    tokens = session.approximate_tokens(captured[0])
    result = run_fake(
        tmp_path / "bounded",
        monkeypatch,
        request,
        config={"context_tokens": tokens, "max_output_tokens": 1, "compression": False},
    )
    assert result["status"] == "context_limit"
    assert result["api_calls"] == 0 and len(captured) == 1


def test_noop_scratch_write_does_not_reset_repeated_search_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover import session_search

    monkeypatch.setattr(
        session_search, "search_sources", lambda *_: {"success": True, "matches": []}
    )
    job = tmp_path / "job"
    job.mkdir()
    tools = SessionTools(role="prover", project_root=tmp_path, workspace=job, context={})
    for _ in range(3):
        tools.invoke("write_file", {"path": "PLAN_job.md", "content": "Same idea"})
        result = tools.invoke("search_project", {"query": "same lemma"})
    assert result["success"] is False
    assert "already returned" in result["error"]


def test_large_tool_result_remains_valid_json_with_full_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"success": True, "matches": [{"text": "evidence" * 4000}]}
    monkeypatch.setattr(SessionTools, "invoke", lambda *_: payload)
    calls = []

    def request(_agent, messages, _timeout):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "evidence",
                        "function": {"name": "search_project", "arguments": '{"query":"lemma"}'},
                    }
                ],
            }, {}
        content = next(message["content"] for message in messages if message["role"] == "tool")
        result = json.loads(content)
        assert result["success"] is True and result["truncated"] is True
        assert len(content) <= 16000
        assert json.loads(Path(result["artifact_path"]).read_text()) == payload
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    result = run_fake(tmp_path, monkeypatch, request, config={"context_tokens": 64000})
    assert result["status"] == "completed", result
    assert result["api_calls"] == 2
    assert any("tool-results" in path for path in result["artifacts"])


def test_addressed_guidance_is_pinned_before_request_and_after_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / ".leanflow/workflow-state/prover/run/inbox.jsonl"
    inbox.parent.mkdir(parents=True)
    inbox.write_text(
        json.dumps({"id": "m", "agent_id": "job", "message": "Use the new polynomial identity."})
        + "\n"
    )
    context = {"run_id": "run", "job_id": "job", "inbox_offset": 0}

    def interrupted(_agent, messages, _timeout):
        assert "Use the new polynomial identity." in messages[1]["content"]
        raise RuntimeError("provider outage")

    first = run_fake(tmp_path, monkeypatch, interrupted, context=context)
    assert first["status"] == "provider_error"

    def resumed(_agent, messages, _timeout):
        assert messages[1]["content"].count("Use the new polynomial identity.") == 1
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {}

    second = run_fake(tmp_path, monkeypatch, resumed, context=context)
    assert second["status"] == "completed" and second["api_calls"] == 2


def test_global_deadline_during_submission_keeps_campaign_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def feedback(response: str) -> dict[str, Any]:
        raise BudgetExhausted("campaign wall-clock budget exhausted", code="campaign_wall_time")

    result = run_fake(
        tmp_path,
        monkeypatch,
        lambda *args: ({"role": "assistant", "content": '{"proof":"trivial"}'}, {}),
        config={"_candidate_feedback": feedback},
    )
    assert result["status"] == "timeout"
    assert result["api_calls"] == 1
    assert result["stop_reason"]["scope"] == "campaign"
    assert result["stop_reason"]["code"] == "campaign_wall_time"
