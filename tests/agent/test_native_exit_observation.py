"""Keep an exited child PID reserved until owned group cleanup completes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from agent.runtime.harness import processes
from leanflow_cli.workflows.prover import check_process, native_command

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Exit observation fixtures use POSIX groups"
)


def test_exit_observation_does_not_reap_original_popen() -> None:
    """Observe both a live and exited child without releasing its PID or group identity."""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time;time.sleep(.2)"], start_new_session=True
    )
    try:
        assert not processes.process_exited(process)
        deadline = time.monotonic() + 3
        while not processes.process_exited(process):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert process.returncode is None
        assert processes.process_exited(process)
        assert process.returncode is None
        assert process.wait(timeout=1) == 0
        assert processes.process_exited(process)
    finally:
        if process.returncode is None:
            process.kill()
        process.wait(timeout=2)


@pytest.mark.parametrize("runner", ["checker", "native_tool"])
def test_late_uncached_same_group_child_is_stopped_before_leader_reaping(
    tmp_path, monkeypatch, runner
) -> None:
    """A child missed between samples remains owned through the unreaped original group."""
    project, workspace = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    workspace.mkdir()
    marker = workspace / "fixture.json"
    child = "import time;time.sleep(60)"
    script = (
        "import json,pathlib,subprocess,sys\n"
        f"child=subprocess.Popen([sys.executable,'-I','-c',{child!r}])\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'pid':child.pid}}))\n"
        "print('complete',flush=True)\n"
    )
    argv = [sys.executable, "-I", "-c", script]
    # Deliberately miss this fixture's child in controller sampling. Its exact
    # independent identity is retained below for cleanup after any assertion failure.
    monkeypatch.setattr(processes.ProcessTree, "refresh", lambda self: None)
    pid = None
    try:
        if runner == "checker":
            monkeypatch.setattr(
                check_process, "_sandbox_command", lambda argv, **kwargs: list(argv)
            )
            result = check_process.isolated_command(
                project_root=project, workspace=workspace, argv=argv, timeout_s=5
            )
        else:
            monkeypatch.setattr(native_command, "sandbox_command", lambda argv, policy: list(argv))
            result = native_command.run_command(
                argv, snapshot=project, directory=workspace, deadline=time.monotonic() + 5
            )
        pid = json.loads(marker.read_text())["pid"]
        assert result["success"] is True
        table = processes._cleanup_states(2)
        assert pid not in table or table[pid][1].startswith("Z")
    finally:
        if marker.exists():
            pid = json.loads(marker.read_text())["pid"]
            table = processes._processes()
            if pid in table:
                # This fresh fixture records the exact child before any cleanup retry.
                owner = processes.ProcessTree(800, {pid: table[pid][1]})
                owner.signal(signal.SIGKILL)
        check_process.close_check_workers(workspace)


def test_waitid_exit_observation_uses_wnowait_and_preserves_cached_state(monkeypatch) -> None:
    """Exercise the Linux observation branch on either host without reaping any real process."""
    from types import SimpleNamespace

    calls = []
    responses = [None, SimpleNamespace(si_pid=412)]

    def waitid(kind, pid, flags):
        calls.append((kind, pid, flags))
        return responses.pop(0)

    monkeypatch.setattr(processes.os, "waitid", waitid, raising=False)
    process = SimpleNamespace(pid=412, returncode=None)
    assert not processes.process_exited(process)
    assert processes.process_exited(process)
    assert process.returncode is None
    assert calls == [(os.P_PID, 412, os.WEXITED | os.WNOHANG | os.WNOWAIT)] * 2


def test_exit_observation_metadata_failure_cannot_claim_reaped_ownership(monkeypatch) -> None:
    """An externally lost child cannot be treated as exited while keeping an arbitrary group ID."""
    from types import SimpleNamespace

    def unavailable(*args):
        raise ChildProcessError("fixture child ownership unavailable")

    monkeypatch.setattr(processes.os, "waitid", unavailable, raising=False)
    process = SimpleNamespace(pid=412, returncode=None)
    with pytest.raises(processes.ProcessTrackingUnavailable):
        processes.process_exited(process)
    assert process.returncode is None


def test_term_ignoring_uncached_group_child_is_killed_before_leader_reaping(tmp_path, monkeypatch):
    """A first-stage exit cannot release the original group PID before KILL escalation."""
    marker = tmp_path / "child-ready"
    child_code = (
        "import os,pathlib,signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)"
    )
    script = (
        "import pathlib,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-I','-c',{child_code!r}])\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        "while not marker.exists():time.sleep(.01)\n"
        "time.sleep(60)\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-I", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    owner = None
    original = processes.signal_group
    observed = []

    def signal_group(process, sig):
        observed.append((sig, process.returncode))
        return original(process, sig)

    monkeypatch.setattr(processes, "signal_group", signal_group)
    tree = processes.ProcessTree(leader.pid, process=leader)
    monkeypatch.setattr(tree, "refresh", lambda: None)
    try:
        deadline = time.monotonic() + 3
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pid = int(marker.read_text())
        owner = processes.ProcessTree(800, {pid: processes._processes()[pid][1]})
        processes.cleanup_group(leader, tree)
        processes.require_output_eof(leader)
        assert observed == [(signal.SIGTERM, None), (signal.SIGKILL, None)]
        assert processes.verify_owned_stopped(owner, group=leader.pid, timeout_s=1)
    finally:
        if owner is not None:
            owner.signal(signal.SIGKILL)
        if leader.returncode is None:
            leader.kill()
        leader.wait(timeout=2)
        for stream in (leader.stdout, leader.stderr):
            if stream is not None:
                stream.close()
