"""Guard unlimited Lean command execution and controller cancellation semantics."""

import subprocess
import sys
import threading

from core.deadlines import deadline_after, operation_cancellation, remaining_seconds
from leanflow_cli.workflows.prover import check_process


def test_none_does_not_become_a_short_deadline():
    assert deadline_after(None) is None
    assert remaining_seconds(None) is None
    assert remaining_seconds(deadline_after(0)) == 0


def test_output_capture_waits_without_any_deadline():
    process = subprocess.Popen(
        [sys.executable, "-c", "print('complete')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, _, _ = check_process._capture_command(process, None)
        assert out.strip() == b"complete"
        assert process.returncode == 0
    finally:
        check_process._kill(process)


def test_cancelled_operation_stops_unlimited_command(tmp_path, monkeypatch):
    project, scratch = tmp_path / "project", tmp_path / "scratch"
    project.mkdir()
    scratch.mkdir()
    monkeypatch.setattr(check_process, "_sandbox_command", lambda argv, **_: list(argv))
    cancelled = threading.Event()
    cancelled.set()
    with operation_cancellation(cancelled.is_set):
        result = check_process.isolated_command(
            project_root=project,
            workspace=scratch,
            argv=[sys.executable, "-c", "import time; time.sleep(1000)"],
            timeout_s=None,
        )
    assert result["error_code"] == "check_cancelled"


def test_expired_global_deadline_never_launches_command(tmp_path, monkeypatch):
    monkeypatch.setattr(
        check_process.subprocess,
        "Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("launched after deadline")),
    )
    result = check_process.isolated_command(
        project_root=tmp_path, workspace=tmp_path / "scratch", argv=["lean"], timeout_s=0
    )
    assert result["timed_out"]
