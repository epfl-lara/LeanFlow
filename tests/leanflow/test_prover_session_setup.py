"""Require isolated Lean readiness before admitting native prover model work."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.harness import HarnessResult
from leanflow_cli.workflows.prover import check_process
from leanflow_cli.workflows.prover.agent_session import run_session
from tests.leanflow.test_prover_runtime import project


@pytest.mark.parametrize("role", ["prover", "negation"])
@pytest.mark.parametrize("previous_calls", [0, 3])
def test_private_setup_failure_stops_before_model_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, previous_calls: int
) -> None:
    project(tmp_path)
    workspace = tmp_path / ".leanflow/jobs/job"
    workspace.mkdir(parents=True)
    (workspace / "Scratch.lean").write_text(
        "import Mathlib.Data.List.Basic\ntheorem target : True := by sorry\n"
    )
    ledger = workspace.parent / ".runtime/job/request-count.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"used": previous_calls}))
    attempts = []

    def fail(**kwargs: Any) -> dict[str, Any]:
        attempts.append(kwargs)
        assert kwargs["project_root"] == workspace / "project"
        assert kwargs["workspace"] == workspace / "project/.leanflow-job"
        assert kwargs["source_file"] == workspace / "project/.leanflow-job/Scratch.lean"
        return {
            "success": False,
            "ok": False,
            "error_code": "backend_error",
            "error": "Lake tried to replace repl",
        }

    monkeypatch.setattr(check_process, "preflight_check", fail)
    monkeypatch.setattr(
        "agent.runtime.harness.run_harness",
        lambda *a, **k: pytest.fail("model launched before Lean was ready"),
    )
    events = []
    result = run_session(
        role=role,
        prompt="prove",
        project_root=tmp_path,
        workspace=workspace,
        config={"provider": "codex"},
        api_budget=10,
        log_path=workspace / "events.jsonl",
        context={},
        on_event=lambda kind, detail: events.append((kind, detail)),
    )
    assert len(attempts) == 1
    assert result["status"] == "environment_error"
    assert "Lake tried to replace repl" in result["error"]
    assert result["agent_calls"] == result["api_calls"] == previous_calls
    assert json.loads(ledger.read_text())["used"] == previous_calls
    evidence = json.loads((ledger.parent / "lean-setup.json").read_text())
    assert evidence["accepted"] is False
    assert evidence["project_root"] == str(workspace / "project")
    assert evidence["error_code"] == "backend_error"
    assert [kind for kind, _ in events] == [
        "job-setup-start",
        "job-setup-result",
        "job-session-end",
    ]


@pytest.mark.parametrize(
    "verdict,status",
    [
        ({"success": True, "ok": False}, "environment_error"),
        ({"success": False, "ok": False, "error_code": "check_cancelled"}, "interrupted"),
        ({"success": False, "ok": False, "timed_out": True}, "timeout"),
    ],
)
def test_setup_requires_lean_acceptance_and_preserves_stop_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: dict[str, Any], status: str
) -> None:
    project(tmp_path)
    workspace = tmp_path / "jobs/prover"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(check_process, "preflight_check", lambda **_: verdict)
    monkeypatch.setattr(
        "agent.runtime.harness.run_harness", lambda *a, **k: pytest.fail("model launch")
    )
    result = run_session(
        role="prover",
        prompt="prove",
        project_root=tmp_path,
        workspace=workspace,
        config={"provider": "codex"},
        api_budget=1,
        log_path=workspace / "events.jsonl",
        context={},
    )
    assert result["status"] == status
    assert result["agent_calls"] == 0


@pytest.mark.parametrize(
    "build_result,status",
    [
        (
            {"success": False, "returncode": 1, "stderr": "local module is invalid"},
            "environment_error",
        ),
        ({"success": False, "error_code": "check_cancelled"}, "interrupted"),
        ({"success": False, "timed_out": True}, "timeout"),
    ],
)
def test_local_prerequisite_failure_stops_before_leanprobe_and_model_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_result: dict[str, Any],
    status: str,
) -> None:
    project(tmp_path)
    workspace = tmp_path / "jobs/prover"
    workspace.mkdir(parents=True)
    (workspace / "Scratch.lean").write_text("import Main\ntheorem target : True := by sorry\n")
    monkeypatch.setattr(check_process, "isolated_command", lambda **_: build_result)
    monkeypatch.setattr(
        check_process, "preflight_check", lambda **_: pytest.fail("LeanProbe launch")
    )
    monkeypatch.setattr(
        "agent.runtime.harness.run_harness", lambda *a, **k: pytest.fail("model launch")
    )
    result = run_session(
        role="prover",
        prompt="prove",
        project_root=tmp_path,
        workspace=workspace,
        config={"provider": "codex"},
        api_budget=1,
        log_path=workspace / "events.jsonl",
        context={},
    )
    assert result["status"] == status
    assert result["agent_calls"] == result["api_calls"] == 0
    evidence = json.loads((workspace.parent / ".runtime/prover/lean-setup.json").read_text())
    assert evidence["accepted"] is False
    assert evidence["prerequisite_build"]["modules"] == ["Main"]


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("LEANFLOW_TEST_LEAN_PROJECT"), reason="Requires a prebuilt Lean project"
)
def test_real_private_workspace_setup_and_warm_leanprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise snapshot creation, sandbox startup, assigned imports, and warm feedback."""
    # Cloning a fresh Mathlib cache exceeds the suite's 30-second unit-test limit.
    signal.alarm(300)
    canonical = Path(os.environ["LEANFLOW_TEST_LEAN_PROJECT"]).resolve()
    local_import = os.environ.get("LEANFLOW_TEST_LEAN_IMPORT", "")
    workspace = tmp_path / "jobs/prover_00001"
    workspace.mkdir(parents=True)
    (workspace / "Scratch.lean").write_text(
        (f"import {local_import}\n" if local_import else "") + "import Mathlib.Data.List.Basic\n"
        "theorem privateProbe (xs : List Nat) : xs ++ [] = xs := by simp\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    checks = []

    def harness(request: Any, **kwargs: Any) -> HarnessResult:
        assert request.cwd == workspace / "project"
        scratch = request.cwd / ".leanflow-job"
        evidence = json.loads(
            (workspace.parent / ".runtime/prover_00001/lean-setup.json").read_text()
        )
        assert evidence["accepted"] is True, evidence
        assert evidence["cold_start"] is True
        if local_import:
            assert local_import in evidence["prerequisite_build"]["modules"]
            assert (
                request.cwd
                / ".lake/build/lib/lean"
                / Path(*local_import.split(".")).with_suffix(".olean")
            ).is_file()
        private_repl = request.cwd / ".lake/packages/repl"
        assert (private_repl / ".git").is_dir()
        assert (private_repl / ".lake/build/bin/repl").is_file()
        worker = check_process._WORKERS[(request.cwd.resolve(), scratch.resolve())]
        verdict = check_process.check_scratch(
            project_root=request.cwd,
            workspace=scratch,
            file=scratch / "Scratch.lean",
            declaration="privateProbe",
            include_axiom_profile=True,
            timeout_s=120,
        )
        assert verdict.get("ok") is True, verdict
        assert verdict["cold_start"] is False
        assert check_process._WORKERS[(request.cwd.resolve(), scratch.resolve())] is worker
        checks.append(verdict)
        request.on_admitting()
        request.on_started()
        return HarnessResult("completed", "{}", cleanup_verified=True)

    monkeypatch.setattr("agent.runtime.harness.run_harness", harness)
    result = run_session(
        role="prover",
        prompt="Deterministic sandbox smoke test",
        project_root=canonical,
        workspace=workspace,
        config={"provider": "codex", "wall_time_s": 180},
        api_budget=1,
        log_path=workspace / "events.jsonl",
        context={},
    )
    assert result["status"] == "completed", result
    assert len(checks) == 1
    assert not (workspace / "project/.lake/packages").exists()
    assert not any(
        key[1] == (workspace / "project/.leanflow-job").resolve() for key in check_process._WORKERS
    )


def test_native_setup_caches_only_newly_created_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capture freshness before preparation; an existing workspace stays ineligible on retry."""
    from leanflow_cli.workflows.prover import session_setup

    project(tmp_path)
    workspace = tmp_path / "jobs/prover"
    workspace.mkdir(parents=True)
    freshness: list[bool] = []

    def setup(*args: Any, cache_fresh_snapshot: bool = False) -> dict[str, Any]:
        freshness.append(cache_fresh_snapshot)
        return {"accepted": False, "error_code": "backend_error", "error": "setup failed"}

    monkeypatch.setattr(session_setup, "check_session_setup", setup)
    monkeypatch.setattr(
        "agent.runtime.harness.run_harness", lambda *args, **kwargs: pytest.fail("model launch")
    )
    for _ in range(2):
        result = run_session(
            role="prover",
            prompt="prove",
            project_root=tmp_path,
            workspace=workspace,
            config={"provider": "codex"},
            api_budget=1,
            log_path=workspace / "events.jsonl",
            context={},
        )
        assert result["status"] == "environment_error"
    assert freshness == [True, False]
