"""Bind resumed native conversations and token baselines to their owning backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import HarnessResult
from leanflow_cli.workflows.prover.agent_session import run_session
from tests.leanflow.test_prover_runtime import project


@pytest.mark.parametrize("previous_backend", [None, "codex", "claude", "lemex"])
@pytest.mark.parametrize("backend", ["codex", "claude", "lemex"])
def test_native_resume_identity_requires_matching_backend_without_resetting_spending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_backend: str | None,
    backend: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project(root)
    workspace = root / ".leanflow/jobs/job"
    workspace.mkdir(parents=True)
    ledger = workspace.parent / ".runtime/job/request-count.json"
    ledger.parent.mkdir(parents=True)
    prior = {
        "used": 5,
        "research_policy": "online-v1",
        "session_id": "old-session",
        "usage_total": {"input_tokens": 100, "output_tokens": 20},
    }
    if previous_backend is not None:
        prior["backend"] = previous_backend
    ledger.write_text(json.dumps(prior))
    same_backend = previous_backend == backend

    def harness(request: Any, on_event: Any = None) -> HarnessResult:
        assert request.backend == backend
        assert request.session_id == ("old-session" if same_backend else "")
        assert request.previous_usage == (prior["usage_total"] if same_backend else None)
        request.on_admitting()
        assert json.loads(ledger.read_text())["used"] == 6
        request.on_started()
        on_event({"type": "thread.started", "thread_id": "current-session"})
        checkpoint = json.loads(ledger.read_text())
        assert checkpoint["backend"] == backend
        assert checkpoint["session_id"] == "current-session"
        assert checkpoint["used"] == 6
        if not same_backend:
            assert "usage_total" not in checkpoint
        return HarnessResult(
            "completed",
            "{}",
            session_id="current-session",
            returncode=0,
            usage={"input_tokens": 2, "output_tokens": 1},
            usage_total={
                "input_tokens": 102 if same_backend else 2,
                "output_tokens": 21 if same_backend else 1,
            },
        )

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    monkeypatch.setattr("leanflow_cli.runtime.lemex_install.resolve_lemex_executable", lambda: None)
    monkeypatch.setattr(
        "leanflow_cli.runtime.lemex_install.managed_distribution_root", lambda: None
    )
    result = run_session(
        role="prover",
        prompt="prove",
        project_root=root,
        workspace=workspace,
        config={"provider": backend, "model": "explicit-native-model", "reasoning_effort": "high"},
        api_budget=10,
        log_path=workspace / "events.jsonl",
        context={},
    )
    saved = json.loads(ledger.read_text())
    assert result["agent_calls"] == saved["used"] == 6
    assert result["input_tokens"] == 2 and result["output_tokens"] == 1
    assert saved["backend"] == backend and saved["session_id"] == "current-session"


def test_native_backend_binding_survives_crash_after_first_identity_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    project(root)
    workspace = root / ".leanflow/jobs/job"
    workspace.mkdir(parents=True)
    ledger = workspace.parent / ".runtime/job/request-count.json"

    def crash(request: Any, on_event: Any = None) -> HarnessResult:
        request.on_admitting()
        request.on_started()
        on_event({"type": "system", "session_id": "claude-session"})
        raise RuntimeError("simulated controller crash")

    monkeypatch.setattr("agent.runtime.harness.run_harness", crash)
    with pytest.raises(RuntimeError, match="simulated controller crash"):
        run_session(
            role="prover",
            prompt="prove",
            project_root=root,
            workspace=workspace,
            config={"provider": "claude"},
            api_budget=1,
            log_path=workspace / "events.jsonl",
            context={},
        )
    saved = json.loads(ledger.read_text())
    assert saved["backend"] == "claude"
    assert saved["session_id"] == "claude-session" and saved["used"] == 1
