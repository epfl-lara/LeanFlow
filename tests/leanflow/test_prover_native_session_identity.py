"""Bind resumed native conversations and token baselines to their owning backend."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import HarnessResult
from agent.runtime.harness.models import NativeCleanupUnverified, NativeLaunchFailure
from leanflow_cli.workflows.prover import agent_session
from leanflow_cli.workflows.prover.agent_session import run_session
from tests.leanflow.test_prover_runtime import project


@pytest.fixture(autouse=True)
def native_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep native database preparation inside disposable test homes."""
    for backend in ("codex", "lemex", "claude"):
        key = "CLAUDE_CONFIG_DIR" if backend == "claude" else f"{backend.upper()}_HOME"
        monkeypatch.setenv(key, str(tmp_path / f"native-{backend}"))
    monkeypatch.setattr(
        "leanflow_cli.workflows.prover.check_process.preflight_check",
        lambda **_: {"success": True, "ok": True},
    )


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
        prior["database_home"] = str(tmp_path / f"native-{previous_backend}/leanflow-sqlite")
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
            cleanup_verified=True,
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


def _existing_native_job(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    """Retain prior spending and native token provenance across the tested launch boundary."""
    root = tmp_path / "project"
    root.mkdir()
    project(root)
    workspace = root / ".leanflow/jobs/job"
    workspace.mkdir(parents=True)
    ledger = workspace.parent / ".runtime/job/request-count.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "used": 3,
                "backend": "codex",
                "research_policy": "online-v1",
                "database_home": str(tmp_path / "native-codex/leanflow-sqlite"),
                "session_id": "old-session",
                "usage_total": {"input_tokens": 100, "output_tokens": 20},
            }
        )
    )
    return {
        "role": "prover",
        "prompt": "prove",
        "project_root": root,
        "workspace": workspace,
        "config": {"provider": "codex"},
        "api_budget": 10,
        "log_path": workspace / "events.jsonl",
        "context": {},
    }, ledger


@pytest.mark.parametrize(
    "launch_status,session_status",
    [
        ("startup_error", "provider_error"),
        ("cancelled", "interrupted"),
        ("deadline_exceeded", "timeout"),
    ],
)
def test_verified_prelaunch_failure_refunds_only_new_admission_and_allows_safe_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_status: str, session_status: str
) -> None:
    kwargs, ledger = _existing_native_job(tmp_path)
    root, workspace = kwargs["project_root"], kwargs["workspace"]
    original = (root / "Main.lean").read_bytes()
    calls = []
    released = []
    monkeypatch.setattr(agent_session, "release_snapshot_dependencies", released.append)

    def harness(request: Any, on_event: Any = None) -> HarnessResult:
        calls.append(request)
        assert request.session_id == "old-session"
        assert request.previous_usage == {"input_tokens": 100, "output_tokens": 20}
        assert json.loads(ledger.read_text())["native_cleanup_unverified"] is True
        request.on_admitting()
        assert json.loads(ledger.read_text())["used"] == 4
        if len(calls) == 1:
            raise NativeLaunchFailure(
                "Fixture helper closed before author launch",
                status=launch_status,
                session_id="old-session",
            )
        request.on_started()
        return HarnessResult(
            "completed",
            "{}",
            session_id="old-session",
            usage={"input_tokens": 2, "output_tokens": 1},
            usage_total={"input_tokens": 102, "output_tokens": 21},
            cleanup_verified=True,
        )

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    first = run_session(**kwargs)
    saved = json.loads(ledger.read_text())
    assert first["status"] == session_status and first["cleanup_verified"] is True
    assert first["agent_calls"] == saved["used"] == 3
    assert first["usage_known"] is False and saved["usage"] == {}
    assert saved["usage_total"] == {"input_tokens": 100, "output_tokens": 20}
    assert saved["native_cleanup_unverified"] is False and released == [workspace / "project"]
    second = run_session(**kwargs)
    saved = json.loads(ledger.read_text())
    assert second["status"] == "completed" and second["agent_calls"] == saved["used"] == 4
    assert second["usage_known"] and saved["usage"] == {"input_tokens": 2, "output_tokens": 1}
    assert saved["usage_total"] == {"input_tokens": 102, "output_tokens": 21}
    assert not saved["native_cleanup_unverified"] and len(calls) == len(released) == 2
    assert (root / "Main.lean").read_bytes() == original


@pytest.mark.parametrize("observed_usage", [False, True, None])
def test_cleanup_exception_preserves_observed_usage_and_blocks_reuse_of_private_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed_usage: bool | None
) -> None:
    kwargs, ledger = _existing_native_job(tmp_path)
    root, workspace = kwargs["project_root"], kwargs["workspace"]
    original = (root / "Main.lean").read_bytes()
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail("Unverified native cleanup cannot release or reuse private invocation inputs")

    monkeypatch.setattr(agent_session, "release_snapshot_dependencies", forbidden)

    def harness(request: Any, on_event: Any = None) -> HarnessResult:
        calls.append(request)
        assert json.loads(ledger.read_text())["native_cleanup_unverified"] is True
        request.on_admitting()
        request.on_started()
        on_event({"type": "thread.started", "thread_id": "observed-session"})
        scratch = request.cwd / ".leanflow-job"
        (scratch / "candidate.txt").write_text("trivial")
        (scratch / "Scratch.lean").write_text("theorem privateCandidate : True := by trivial\n")
        observed = (
            HarnessResult(
                "completed",
                '{"proof":"trivial"}',
                session_id="observed-session",
                usage={"input_tokens": 7, "output_tokens": 5} if observed_usage else None,
                usage_total={"input_tokens": 107, "output_tokens": 25} if observed_usage else None,
            )
            if observed_usage is not False
            else None
        )
        raise NativeCleanupUnverified("Fixture native descendant remains live", result=observed)

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    with pytest.raises(NativeCleanupUnverified, match="descendant remains live"):
        run_session(**kwargs)
    saved = json.loads(ledger.read_text())
    assert saved["native_cleanup_unverified"] is True and saved["used"] == 4
    assert saved["session_id"] == "observed-session"
    assert saved["usage"] == ({"input_tokens": 7, "output_tokens": 5} if observed_usage else None)
    assert saved["usage_total"] == (
        {"input_tokens": 107, "output_tokens": 25}
        if observed_usage
        else {"input_tokens": 100, "output_tokens": 20}
    )
    assert not (workspace / "candidate.txt").exists() and not (workspace / "Scratch.lean").exists()
    assert (workspace / "project/.leanflow-job/candidate.txt").read_text() == "trivial"
    before = {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    ledger_before = ledger.read_bytes()
    monkeypatch.setattr("agent.runtime.harness.run_harness", forbidden)
    monkeypatch.setattr(agent_session, "prepare_snapshot", forbidden)
    monkeypatch.setattr(agent_session, "NativeBridge", forbidden)
    for _ in range(2):
        with pytest.raises(
            ValueError, match="closure is unverified.*retain this job's private inputs"
        ):
            run_session(**kwargs)
        assert ledger.read_bytes() == ledger_before
        assert {
            path.relative_to(workspace).as_posix(): path.read_bytes()
            for path in workspace.rglob("*")
            if path.is_file()
        } == before
    assert len(calls) == 1 and (root / "Main.lean").read_bytes() == original
