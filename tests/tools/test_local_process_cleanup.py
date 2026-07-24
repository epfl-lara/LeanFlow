"""Regression tests for local terminal descendant cleanup."""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from tools.environments.local import LocalEnvironment
from tools.utilities import process_tree
from tools.utilities.interrupt import set_interrupt
from tools.utilities.process_tree import ProcessRecord, _owned_processes


def _process_alive(pid: int) -> bool:
    """Return whether a POSIX process still accepts signal-zero probes."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_independent_python_child(marker: str) -> int:
    """Return the test Python child after it has entered its own process group."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in completed.stdout.splitlines():
            if marker not in line or "bash -lic" in line:
                continue
            fields = line.strip().split(None, 3)
            if len(fields) >= 3 and int(fields[0]) == int(fields[2]):
                return int(fields[0])
        time.sleep(0.05)
    return 0


def _independent_group_command(marker: str) -> str:
    """Build a uniquely discoverable detached-child command for one test."""
    script = (
        "import json, os, time; "
        "os.setpgid(0, 0); "
        f"print(json.dumps({{'pid': os.getpid(), 'pgid': os.getpgrp(), "
        f"'marker': {marker!r}}}), flush=True); "
        "time.sleep(60)"
    )
    return f"python3 -c {shlex.quote(script)}"


def test_owned_process_selection_keeps_unrelated_sessions_out():
    """Selection follows descendants/session identity, never cwd or command text."""
    snapshot = {
        100: ProcessRecord(100, 10, 100, 100),
        101: ProcessRecord(101, 100, 101, 100),
        # Reparented member of the same command session.
        102: ProcessRecord(102, 1, 102, 100),
        # An unrelated process can use the same cwd/command but has another SID.
        200: ProcessRecord(200, 1, 200, 200),
    }

    selected = _owned_processes(100, expected_session_id=100, snapshot=snapshot)

    assert set(selected) == {100, 101, 102}


def test_owned_process_selection_rejects_a_reused_root_identity():
    """A recycled root PID cannot pull its unrelated descendants into cleanup."""
    snapshot = {
        # PID 100 has been reused in another session after the owned root died.
        100: ProcessRecord(100, 10, 100, 200),
        101: ProcessRecord(101, 100, 100, 200),
        # The original reparented session member remains ours.
        102: ProcessRecord(102, 1, 102, 100),
    }

    selected = _owned_processes(100, expected_session_id=100, snapshot=snapshot)

    assert set(selected) == {102}


def test_snapshot_failure_escalates_only_revalidated_identities(monkeypatch):
    """A missing refresh still kills a matching root without trusting reused PIDs."""
    initial = {
        100: ProcessRecord(100, 10, 100, 100),
        101: ProcessRecord(101, 100, 101, 100),
    }
    snapshots: list[dict[int, ProcessRecord] | None] = [initial, None]
    signals: list[tuple[int, set[int]]] = []

    monkeypatch.setattr(process_tree, "_process_snapshot", lambda: snapshots.pop(0))
    monkeypatch.setattr(
        process_tree,
        "_any_process_alive",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        process_tree,
        "_validated_live_records",
        lambda records, *, expected_session_id: {100: records[100]},
    )
    monkeypatch.setattr(
        process_tree,
        "_signal_owned_processes",
        lambda records, signum, **kwargs: signals.append((signum, set(records))),
    )

    selected = process_tree.terminate_process_tree(
        100,
        expected_session_id=100,
        term_grace_s=0,
        kill_grace_s=0,
    )

    assert signals == [
        (signal.SIGTERM, {100, 101}),
        (signal.SIGKILL, {100}),
    ]
    assert selected == (100, 101)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_interrupt_reaps_child_that_opens_an_independent_process_group():
    """Interrupt must reap a child outside the login shell's process group."""
    env = LocalEnvironment(cwd="/tmp", timeout=30)
    result_holder: dict[str, dict] = {}
    child_pid = 0
    marker = f"interrupt-{os.getpid()}-{time.monotonic_ns()}"
    command = _independent_group_command(marker)
    set_interrupt(False)

    def run() -> None:
        result_holder["result"] = env.execute(command, timeout=30)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        child_pid = _wait_for_independent_python_child(marker)
        assert child_pid > 1
        set_interrupt(True)
        worker.join(timeout=6)

        assert not worker.is_alive()
        assert result_holder["result"]["returncode"] == 130
        assert not _process_alive(child_pid)
    finally:
        set_interrupt(False)
        env.cleanup()
        if child_pid and _process_alive(child_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(child_pid, signal.SIGKILL)
        worker.join(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_timeout_reaps_child_that_opens_an_independent_process_group():
    """The terminal timeout is an ownership boundary for the complete tree."""
    env = LocalEnvironment(cwd="/tmp", timeout=1)
    child_pid = 0
    marker = f"timeout-{os.getpid()}-{time.monotonic_ns()}"
    command = _independent_group_command(marker)
    set_interrupt(False)
    result_holder: dict[str, dict] = {}

    def run() -> None:
        result_holder["result"] = env.execute(command, timeout=1)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        child_pid = _wait_for_independent_python_child(marker)
        assert child_pid > 1
        worker.join(timeout=6)

        assert not worker.is_alive()
        assert result_holder["result"]["returncode"] == 124
        assert not _process_alive(child_pid)
    finally:
        env.cleanup()
        if child_pid and _process_alive(child_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(child_pid, signal.SIGKILL)
        worker.join(timeout=2)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_native_shutdown_sweeps_active_task_environment():
    """Runner cleanup reaps an in-flight command before its daemon thread is abandoned."""
    from leanflow_cli.native.runtime_cleanup import _close_agent_terminal_resources
    from tools.implementations import terminal_tool

    task_id = "native-process-cleanup-regression"
    env = LocalEnvironment(cwd="/tmp", timeout=30)
    child_pid = 0
    marker = f"shutdown-{os.getpid()}-{time.monotonic_ns()}"
    command = _independent_group_command(marker)
    result_holder: dict[str, dict] = {}
    set_interrupt(False)

    with terminal_tool._env_lock:
        terminal_tool._active_environments[task_id] = env
        terminal_tool._last_activity[task_id] = time.time()

    def run() -> None:
        result_holder["result"] = env.execute(command, timeout=30)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        child_pid = _wait_for_independent_python_child(marker)
        assert child_pid > 1

        _close_agent_terminal_resources(SimpleNamespace(_managed_tool_task_id=task_id))
        worker.join(timeout=6)

        assert not worker.is_alive()
        assert not _process_alive(child_pid)
        assert task_id not in terminal_tool._active_environments
    finally:
        terminal_tool.cleanup_vm(task_id)
        if child_pid and _process_alive(child_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(child_pid, signal.SIGKILL)
        worker.join(timeout=2)
