"""Exercise public setup limits and detached pipe writers without an execution policy."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from io import StringIO
from pathlib import Path

import pytest

from leanflow_cli.workflows.project_setup import public_process
from leanflow_cli.workflows.project_setup.process import SetupFailure, run_command


def _run(tmp_path: Path, code: str, cancel: threading.Event | None = None) -> tuple[int, str]:
    return run_command(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        log=StringIO(),
        cancel=cancel,
        progress=None,
        phase="test",
    )


def _record(root: Path) -> dict:
    paths = list(public_process.invocation_directory(root).glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


def test_silent_public_command_has_deadline_and_verified_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_SETUP_TIMEOUT_S", "0.2")
    started = time.monotonic()
    with pytest.raises(SetupFailure, match="deadline"):
        _run(tmp_path, "import time; time.sleep(30)")
    assert time.monotonic() - started < 6
    assert _record(tmp_path)["cleanup_verified"] is True


def test_public_output_overflow_is_bounded_and_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(public_process, "MAX_OUTPUT_BYTES", 8192)
    with pytest.raises(SetupFailure, match="output budget"):
        _run(tmp_path, "import os; os.write(1, b'x'*1000000)")
    record = _record(tmp_path)
    assert record["cleanup_verified"] is True
    assert 8192 < record["output_bytes"] <= 8192 + 4096


@pytest.mark.parametrize("explicit_cancel", [False, True])
def test_unobserved_separate_session_pipe_holder_returns_boundedly_and_retains_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_cancel: bool
) -> None:
    """Independent hidden writer cleanup remains a fixture responsibility, never inferred EOF."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(public_process, "EOF_TIMEOUT_S", 0.2)
    # Characterize the explicitly documented unobserved escape boundary. The
    # real separately started writer retains the pipe after its leader exits.
    monkeypatch.setattr(public_process.ProcessTree, "refresh", lambda self: None)
    marker = tmp_path / "writer.json"
    writer = f"import os,pathlib,time; pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(30)"
    code = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{writer!r}],start_new_session=True)"
    cancel = threading.Event() if explicit_cancel else None

    def stop() -> None:
        while not marker.exists():
            time.sleep(0.01)
        assert cancel is not None
        cancel.set()

    stopper = threading.Thread(target=stop) if explicit_cancel else None
    if stopper:
        stopper.start()
    started = time.monotonic()
    try:
        with pytest.raises(SetupFailure) as failure:
            _run(tmp_path, code, cancel)
        assert failure.value.code == "setup_cleanup_unverified"
        assert time.monotonic() - started < 6
        assert _record(tmp_path)["cleanup_verified"] is False
        assert marker.exists()
        pid = int(marker.read_text())
        os.kill(pid, 0)
    finally:
        if marker.exists():
            pid = int(marker.read_text())
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = subprocess.run(
                    ["/bin/ps", "-o", "stat=", "-p", str(pid)],
                    capture_output=True,
                    text=True,
                    timeout=2,
                ).stdout.strip()
                if not state or state.startswith("Z"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("owned fixture pipe holder did not close")
        if stopper:
            stopper.join(2)
            assert not stopper.is_alive()
    public_process.retry_pending_invocations(tmp_path)
    assert _record(tmp_path)["cleanup_verified"] is True


def test_unavailable_process_inspection_never_claims_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    def unavailable(self: object) -> None:
        raise OSError("process table unavailable")

    monkeypatch.setattr(public_process.ProcessTree, "refresh", unavailable)
    with pytest.raises(SetupFailure) as failure:
        _run(tmp_path, "import time; time.sleep(30)")
    assert failure.value.code == "setup_cleanup_unverified"
    assert _record(tmp_path)["cleanup_verified"] is False


def test_unconsumed_protocol_input_does_not_block_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unread pipe input cannot hold Stop or deadline outside supervision."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_SETUP_TIMEOUT_S", "0.2")
    started = time.monotonic()
    with pytest.raises(SetupFailure, match="deadline"):
        run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            log=StringIO(),
            cancel=None,
            progress=None,
            phase="test",
            input_text="x" * 64000,
        )
    assert time.monotonic() - started < 6
    assert _record(tmp_path)["cleanup_verified"] is True


def test_public_memory_limit_uses_owned_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.project_setup import execution

    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(execution, "setup_memory_limit_bytes", lambda: 1)
    with pytest.raises(SetupFailure, match="memory limit"):
        _run(tmp_path, "import time; time.sleep(30)")
    assert _record(tmp_path)["peak_owned_rss_bytes"] > 1
    assert _record(tmp_path)["cleanup_verified"] is True


def test_normal_public_result_keeps_original_pid_reserved_until_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not reap a successful/failed leader before final owned-group escalation."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    original = public_process.cleanup_group
    seen = []

    def cleanup(child, tree):
        seen.append(child.returncode)
        assert child.returncode is None
        original(child, tree)

    monkeypatch.setattr(public_process, "cleanup_group", cleanup)
    code, _ = _run(tmp_path, "import sys; sys.exit(7)")
    assert code == 7 and seen == [None]
    record = _record(tmp_path)
    assert record["physical_eof_verified"] and record["cleanup_verified"]
