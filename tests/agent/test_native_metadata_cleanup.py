"""Bound native metadata I/O and retain uncertain process and physical pipe ownership."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from agent.runtime.harness import metadata, processes
from agent.runtime.harness.models import NativeCleanupUnverified
from agent.runtime.harness.processes import ProcessTree, verify_owned_stopped

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Metadata pipe fixtures require POSIX")


@pytest.mark.parametrize("leader_exits", [False, True])
def test_metadata_detached_writer_retains_nonblocking_handles_until_eof_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leader_exits: bool
) -> None:
    """A stopped nonblocking reader cannot replace physical EOF or authorize input disposal."""
    marker = tmp_path / "fixture-child.json"
    script = (
        "import json,os,pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-I','-c','import time;time.sleep(60)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'child_pid':child.pid}))\n"
        "print(json.dumps({'id':1,'result':'metadata'}),flush=True)\n"
        + ("sys.exit(0)\n" if leader_exits else "time.sleep(60)\n")
    )
    monkeypatch.setattr(ProcessTree, "refresh", lambda self: None)
    session = metadata.MetadataSession([sys.executable, "-I", "-u", "-c", script, str(marker)])
    independently_owned = None
    try:
        session.__enter__()
        assert session.receive() == {"id": 1, "result": "metadata"}
        data = json.loads(marker.read_text())
        child_pid = data["child_pid"]
        identity = processes._processes()[child_pid][1]
        independently_owned = ProcessTree(800, {child_pid: identity})
        assert os.getpgid(child_pid) == child_pid
        started = time.monotonic()
        with pytest.raises(NativeCleanupUnverified, match="output pipes remain open"):
            session.close()
        assert time.monotonic() - started < 4
        assert session in metadata._RETAINED and not session.closed
        assert session._processes_verified and not session._output_eof_verified
        assert session.reader is not None and not session.reader.is_alive()
        assert session.process is not None and session.process.returncode is not None
        assert verify_owned_stopped(session.tree, group=session.process.pid, timeout_s=1)
        assert session.process.stdout is not None and not session.process.stdout.closed
        with pytest.raises(BlockingIOError):
            os.read(session.process.stdout.fileno(), 1)
        independently_owned.signal(signal.SIGKILL)
        assert verify_owned_stopped(independently_owned, group=child_pid, timeout_s=2)
        with monkeypatch.context() as patch:
            patch.setattr(
                metadata,
                "cleanup_group",
                lambda *args: pytest.fail("A metadata EOF-only retry signaled a former group"),
            )
            session.close()
        assert session.closed and session not in metadata._RETAINED
        assert session._output_eof_verified and session.process.stdout.closed
    finally:
        if independently_owned is None and marker.exists():
            child_pid = json.loads(marker.read_text())["child_pid"]
            entry = processes._processes().get(child_pid)
            if entry is not None:
                independently_owned = ProcessTree(800, {child_pid: entry[1]})
        if independently_owned is not None:
            independently_owned.signal(signal.SIGKILL)
        if session.process is not None:
            session.close()


def test_metadata_send_backpressure_obeys_deadline_and_closes_process() -> None:
    """A peer that never reads stdin cannot block a metadata request or buffered stream close."""
    session = metadata.MetadataSession(
        [sys.executable, "-I", "-c", "import time;time.sleep(60)"], timeout=0.3
    )
    started = time.monotonic()
    with session:
        with pytest.raises(metadata.MetadataUnavailable, match="timed out"):
            session.send({"payload": "x" * (2 * 1024 * 1024)})
    assert session.closed and session.process is not None and session.process.returncode is not None
    assert session.reader is not None and not session.reader.is_alive()
    assert time.monotonic() - started < 3


def test_metadata_output_overflow_ends_reader_and_closes_owned_process() -> None:
    """Stop parsing an oversized unterminated record without keeping a blocked reader alive."""
    session = metadata.MetadataSession(
        [
            sys.executable,
            "-I",
            "-c",
            "import os,time;os.write(1,b'x'*(5*1024*1024));time.sleep(60)",
        ],
        timeout=3,
    )
    with session:
        with pytest.raises(metadata.MetadataUnavailable, match="closed model discovery"):
            session.receive()
    assert session.closed and session.process is not None and session.process.returncode is not None
    assert session.reader is not None and not session.reader.is_alive()


def test_metadata_eof_keeps_final_json_without_newline() -> None:
    """Retain the previous readline behavior for a peer's final un-terminated JSON record."""
    with metadata.MetadataSession(
        [sys.executable, "-I", "-c", 'import sys;sys.stdout.write(\'{"id":1,"result":{}}\')']
    ) as session:
        assert session.receive() == {"id": 1, "result": {}}
    assert session.closed


def test_metadata_sampling_failure_keeps_owner_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable process metadata cannot certify cleanup even after the leader was killed."""
    session = metadata.MetadataSession([sys.executable, "-I", "-c", "import time;time.sleep(60)"])
    try:
        with monkeypatch.context() as patch:

            def unavailable():
                raise OSError("process metadata unavailable")

            patch.setattr(processes, "_processes", unavailable)
            session.__enter__()
            with pytest.raises(NativeCleanupUnverified, match="descendant cleanup"):
                session.close()
            assert session in metadata._RETAINED and not session.closed
            assert not session._processes_verified
        session.close()
        assert session.closed and session not in metadata._RETAINED
    finally:
        if session.process is not None:
            session.close()


def test_slow_process_sample_cannot_prevent_metadata_group_termination(monkeypatch) -> None:
    """A bounded slow ps sample must not make close fail before it signals its helper."""
    import threading

    sampling = threading.Event()
    original = ProcessTree.refresh

    def slow_first(self):
        if not sampling.is_set():
            sampling.set()
            time.sleep(1.2)
        return original(self)

    monkeypatch.setattr(ProcessTree, "refresh", slow_first)
    session = metadata.MetadataSession([sys.executable, "-I", "-c", "import time;time.sleep(60)"])
    try:
        session.__enter__()
        assert sampling.wait(2)
        session.close()
        assert session.closed and session.process.returncode is not None
        assert not session.reader.is_alive()
    finally:
        if session.process is not None:
            # The fixture's exact Popen reserves its group until this independent wait.
            if session.process.returncode is None:
                os.killpg(session.process.pid, signal.SIGKILL)
                session.process.wait(timeout=2)
            session.close()


def test_metadata_process_sampling_has_quarter_second_throttle(monkeypatch) -> None:
    """Idle output polling must not trigger a process-table command every 100 milliseconds."""
    import threading

    sampled = threading.Event()
    calls = []
    original = metadata.ProcessSampler.sample

    def sample(self):
        calls.append(time.monotonic())
        sampled.set()
        return original(self)

    monkeypatch.setattr(metadata.ProcessSampler, "sample", sample)
    with metadata.MetadataSession([sys.executable, "-I", "-c", "import time;time.sleep(60)"]):
        assert sampled.wait(2)
        time.sleep(0.65)
    assert len(calls) <= 3
