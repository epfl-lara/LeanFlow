"""Exercise fixed request ceilings and scratch-only session authority."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import agent_session as session
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
        context={},
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
    assert ledger == {"limit": 3, "used": 3}


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
