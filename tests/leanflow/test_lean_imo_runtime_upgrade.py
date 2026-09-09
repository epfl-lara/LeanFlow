"""Protect active worker identities and per-cell runtime provenance during upgrades."""

import json
from pathlib import Path

import pytest

from scripts.lean_imo_campaign import adoption, runner
from scripts.lean_imo_campaign.runtime_versions import runtime_directory


def test_runtime_pin_survives_campaign_upgrade(tmp_path: Path) -> None:
    assert runtime_directory(tmp_path, {}) == tmp_path
    assert (
        runtime_directory(tmp_path, {"runtime_directory": str(tmp_path / "old")})
        == tmp_path / "old"
    )


def test_adoption_preserves_live_worker_and_uses_native_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cell = {"pid": 123, "process_identity": "birth command", "verified": False}
    monkeypatch.setattr(adoption, "process_identity", lambda _: "birth command")
    process = adoption.AdoptedProcess(cell)
    assert process.poll() is None
    monkeypatch.setattr(adoption, "process_identity", lambda _: "")
    assert process.poll() == 1
    cell["verified"] = True
    assert process.poll() == 0
    assert cell["returncode_source"] == "adopted_native_state"


def test_pid_reuse_is_never_adopted_or_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    cell = {"pid": 123, "process_identity": "original"}
    monkeypatch.setattr(adoption, "process_identity", lambda _: "replacement")
    with pytest.raises(ValueError, match="identity changed"):
        adoption.AdoptedProcess(cell)
    monkeypatch.setattr(adoption, "process_identity", lambda _: "original")
    process = adoption.AdoptedProcess(cell)
    monkeypatch.setattr(adoption, "process_identity", lambda _: "replacement")
    with pytest.raises(ValueError, match="reused"):
        process.poll()


def test_launch_pins_new_runtime_but_resume_keeps_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    for path, identity in ((old, "old-hash"), (new, "new-hash")):
        path.mkdir()
        (path / "provenance.json").write_text(json.dumps({"runtime_sha256": identity}))
    captured = []

    class Process:
        pid = 123

    def spawn(argv, **kwargs):
        captured.append(argv)
        return Process()

    monkeypatch.setattr(runner, "process_identity", lambda _: "birth command")
    monkeypatch.setattr(runner.subprocess, "Popen", spawn)
    monkeypatch.setattr(runner, "report", lambda *args: None)
    monkeypatch.setattr(runner, "prepare", lambda *args: tmp_path)
    monkeypatch.setattr(runner, "environment", lambda *args: {})
    campaign = {"default_runtime_directory": str(new)}
    fresh = {"id": "new-cell"}
    runner.launch(tmp_path, campaign, fresh)
    assert captured[-1][0] == str(new / "python-env/bin/python")
    assert str(new / "runtime") in captured[-1]
    assert fresh["runtime_sha256"] == "new-hash"
    resumed = {
        "id": "old-cell",
        "runtime_directory": str(old),
        "resume_run_id": "prior",
        "project": str(tmp_path),
        "recovery_attempts": 1,
        "metrics": {"elapsed_s": 55},
    }
    runner.launch(tmp_path, campaign, resumed)
    assert captured[-1][0] == str(old / "python-env/bin/python")
    assert resumed["runtime_sha256"] == "old-hash"
    assert resumed["metrics"]["elapsed_s"] == 55


def test_launch_resumes_the_run_with_the_most_consumed_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale resume_run_id would roll proved work back; the tip is what continues."""
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "provenance.json").write_text(json.dumps({"runtime_sha256": "h"}))
    project = tmp_path / "cell"
    runs = project / ".leanflow/workflow-state/prover"
    for name, calls in (("bench-x", 805), ("bench-x-r1", 2294), ("bench-x-r2", 805)):
        (runs / name).mkdir(parents=True)
        (runs / name / "state.json").write_text(json.dumps({"metrics": {"api_calls": calls}}))
    # A failed relaunch's clone is renamed aside; it must never win.
    (runs / "bench-x-r3.superseded").mkdir()
    (runs / "bench-x-r3.superseded" / "state.json").write_text(
        json.dumps({"metrics": {"api_calls": 9999}})
    )
    captured = []

    class Process:
        pid = 123

    monkeypatch.setattr(runner, "process_identity", lambda _: "birth command")
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda argv, **kw: captured.append(argv) or Process()
    )
    monkeypatch.setattr(runner, "report", lambda *args: None)
    monkeypatch.setattr(runner, "environment", lambda *args: {})
    campaign = {"default_runtime_directory": str(snapshot)}

    cell = {"id": "x", "resume_run_id": "bench-x", "project": str(project), "recovery_attempts": 3}
    runner.launch(tmp_path, campaign, cell)
    assert cell["resume_run_id"] == "bench-x-r1"
    assert cell["resume_source_corrected_from"] == "bench-x"
    assert cell["run_id"] == "bench-x-r3"

    already = {
        "id": "x",
        "resume_run_id": "bench-x-r1",
        "project": str(project),
        "recovery_attempts": 4,
    }
    runner.launch(tmp_path, campaign, already)
    assert already["resume_run_id"] == "bench-x-r1"
    assert "resume_source_corrected_from" not in already

    assert runner.resume_source(tmp_path / "nowhere") == ""


def test_adopted_terminal_run_is_scored_without_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "cell"
    state_dir = project / ".leanflow/workflow-state/prover/run"
    state_dir.mkdir(parents=True)
    metrics = {"api_calls": 77, "elapsed_s": 14000}
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "terminal": True,
                "verification": {"accepted": True},
                "metrics": metrics,
            }
        )
    )
    cell = {
        "id": "p-astra-bottom",
        "problem": {"id": "p"},
        "condition": "astra-bottom",
        "lane": 1,
        "status": "running",
        "project": str(project),
        "run_id": "run",
        "pid": 123,
        "process_identity": "birth command",
        "started_epoch": 1,
        "metrics": {},
        "verified": False,
    }
    (tmp_path / "campaign.json").write_text(
        json.dumps({"cells": [cell], "provenance": {"runtime_sha256": "original"}})
    )
    monkeypatch.setattr(adoption, "process_identity", lambda _: "")
    monkeypatch.setattr(runner, "next_cell", lambda *args: None)
    runner.run(tmp_path, adopt_active=True)
    result = json.loads((tmp_path / "campaign.json").read_text())
    assert result["status"] == "completed"
    assert result["cells"][0]["verified"]
    assert result["cells"][0]["metrics"] == metrics
    assert result["cells"][0]["returncode_source"] == "adopted_native_state"


def test_process_identity_is_untruncated_and_locale_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "birth very long command", "")

    monkeypatch.setattr(adoption.subprocess, "run", execute)
    assert adoption.process_identity(123) == "birth very long command"
    argv, kwargs = calls[0]
    assert argv[:2] == ["/bin/ps", "-ww"]
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_ps_failure_is_not_mistaken_for_worker_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(
        adoption.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "failure"),
    )
    monkeypatch.setattr(adoption.os, "kill", lambda *args: None)
    with pytest.raises(RuntimeError, match="Cannot inspect existing worker"):
        adoption.process_identity(123)

    def missing(*args):
        raise ProcessLookupError

    monkeypatch.setattr(adoption.os, "kill", missing)
    assert adoption.process_identity(123) == ""
