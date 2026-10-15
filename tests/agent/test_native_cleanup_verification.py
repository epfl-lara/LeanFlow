"""Require independent owned-process and output-channel closure before reporting native cleanup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from agent.runtime.harness import (
    HarnessRequest,
    HarnessResult,
    IsolationPolicy,
    processes,
    run_harness,
    runner,
)
from agent.runtime.harness.models import NativeCleanupUnverified

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Native process groups require POSIX")


@pytest.fixture
def native_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], HarnessRequest]:
    """Run real fixture processes while leaving kernel sandbox coverage to the isolation suite."""
    monkeypatch.setattr(runner, "sandbox_command", lambda argv, policy, **kwargs: argv)

    def make(body: str) -> HarnessRequest:
        executable = tmp_path / "native-cli"
        executable.write_text(
            f"#!{sys.executable}\nimport sys\n"
            "if sys.argv[1:3] == ['mcp', 'list']:\n"
            "    print('[]')\n    sys.exit(0)\n" + body
        )
        executable.chmod(0o700)
        return HarnessRequest(
            "lemex",
            "a fixture task",
            tmp_path,
            executable=str(executable),
            isolation=IsolationPolicy(tmp_path),
        )

    return make


def _stop_fixture_child(path: Path) -> None:
    """Stop only the long-lived child created by this fixture, including on assertion failure."""
    if not path.exists():
        return
    pid = int(json.loads(path.read_text())["pid"])
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _child_body(path: Path, *, detached: bool, delay: float = 0.65) -> str:
    """Keep a descendant observable before its native parent exits with a terminal event."""
    child = (
        "import json,os,pathlib,time; "
        f"pathlib.Path({str(path)!r}).write_text(json.dumps({{'pid':os.getpid(),'group':os.getpgrp()}})); "
        "time.sleep(30)"
    )
    return (
        "import json,subprocess,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session={detached!r})\n"
        f"marker=Path({str(path)!r})\n"
        "until=time.monotonic()+3\n"
        "while not marker.exists():\n"
        "    assert time.monotonic()<until\n    time.sleep(0.01)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
        f"time.sleep({delay!r})\n"
    )


def test_cleanup_verification_is_an_explicit_result_capability() -> None:
    assert HarnessResult("completed").cleanup_verified is False
    assert HarnessResult("completed", cleanup_verified=True).cleanup_verified is True


def test_real_completed_native_session_reports_verified_cleanup(native_script) -> None:
    result = run_harness(native_script('print(\'{"type":"turn.completed"}\',flush=True)\n'))
    assert result.status == "completed" and result.cleanup_verified is True


@pytest.mark.parametrize("detached", [False, True])
def test_real_completed_native_session_reaps_background_and_tracked_session_escape(
    native_script, tmp_path: Path, detached: bool
) -> None:
    marker = tmp_path / "fixture-child.json"
    try:
        result = run_harness(native_script(_child_body(marker, detached=detached)))
        assert result.status == "completed" and result.cleanup_verified is True
        metadata = json.loads(marker.read_text())
        if detached:
            assert metadata["group"] == metadata["pid"]
        table = processes._cleanup_states(2)
        assert metadata["pid"] not in table or table[metadata["pid"]][1].startswith("Z")
    finally:
        _stop_fixture_child(marker)


def test_output_channels_left_open_after_group_cleanup_never_certify_closure(
    native_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "untracked-pipe-holder.json"
    monkeypatch.setattr(processes.ProcessTree, "refresh", lambda self: None)
    try:
        result = run_harness(native_script(_child_body(marker, detached=True, delay=0.1)))
        assert result.status == "failed" and "output channels" in result.error
        assert result.cleanup_verified is False
        child = json.loads(marker.read_text())["pid"]
        assert child in processes._cleanup_states(2)
    finally:
        _stop_fixture_child(marker)


@pytest.mark.parametrize("outcome", ["deadline", "failure"])
def test_noncompleted_native_result_can_certify_cleanup_only_after_real_shutdown(
    native_script, outcome: str
) -> None:
    request = native_script(
        'import time\nprint(\'{"type":"turn.completed"}\',flush=True)\n'
        + ("time.sleep(30)\n" if outcome == "deadline" else "sys.exit(3)\n")
    )
    if outcome == "deadline":
        request = replace(request, deadline=time.monotonic() + 1)
    result = run_harness(request)
    assert result.status == ("deadline_exceeded" if outcome == "deadline" else "failed")
    assert result.cleanup_verified is True


@pytest.mark.parametrize("metadata", ["", "malformed process row\n", "101 102 S\n"])
def test_empty_or_partial_process_metadata_cannot_certify_absence(
    monkeypatch: pytest.MonkeyPatch, metadata: str
) -> None:
    monkeypatch.setattr(
        processes.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=metadata)
    )
    try:
        verified = processes.verify_owned_stopped(None, group=101, timeout_s=0.05)
    except (OSError, ValueError, subprocess.SubprocessError):
        verified = False
    assert verified is False


@pytest.mark.parametrize(
    "state,identity,expected",
    [("S", "old-start", False), ("Z", "old-start", True), ("S", "new-start", True)],
)
def test_detached_tracked_identity_and_zombie_state_control_cleanup_evidence(
    monkeypatch: pytest.MonkeyPatch, state: str, identity: str, expected: bool
) -> None:
    tree = processes.ProcessTree(101, {202: "old-start"})
    monkeypatch.setattr(processes, "_cleanup_states", lambda timeout: {202: (303, state, identity)})
    assert processes.verify_owned_stopped(tree, group=101, timeout_s=0.02) is expected


@pytest.mark.parametrize("state,expected", [("S", False), ("Z", True)])
def test_remaining_original_group_member_prevents_cleanup_until_exited(
    monkeypatch: pytest.MonkeyPatch, state: str, expected: bool
) -> None:
    monkeypatch.setattr(processes, "_cleanup_states", lambda timeout: {202: (101, state, "start")})
    assert processes.verify_owned_stopped(None, group=101, timeout_s=0.02) is expected


def test_cleanup_group_raises_when_independent_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(30)"], start_new_session=True
    )
    monkeypatch.setattr(processes, "verify_owned_stopped", lambda *a, **kw: False)
    try:
        with pytest.raises(NativeCleanupUnverified, match="retain invocation inputs"):
            processes.cleanup_group(process, processes.ProcessTree(process.pid))
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def test_runner_cannot_return_success_when_independent_cleanup_metadata_is_unavailable(
    native_script, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(timeout: float) -> dict:
        raise ValueError("The cleanup process table is incomplete")

    monkeypatch.setattr(processes, "_cleanup_states", unavailable)
    with pytest.raises(NativeCleanupUnverified, match="retain invocation inputs"):
        run_harness(native_script('print(\'{"type":"turn.completed"}\',flush=True)\n'))


def test_final_leader_reaping_is_bounded_and_cannot_infer_cleanup_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits = []
    signals = []

    def wait(*, timeout=None):
        waits.append(timeout)
        assert timeout == 2, "Leader reaping must never wait without a finite deadline"
        raise subprocess.TimeoutExpired("fixture-native-leader", timeout)

    def forbidden(*args, **kwargs):
        pytest.fail("A leader still alive after SIGKILL cannot certify completed cleanup")

    leader = SimpleNamespace(pid=101, wait=wait)
    monkeypatch.setattr(processes, "signal_group", lambda process, sig: signals.append(sig))
    monkeypatch.setattr(processes, "verify_owned_stopped", forbidden)
    started = time.monotonic()
    with pytest.raises(
        NativeCleanupUnverified, match="leader did not exit.*retain invocation inputs"
    ):
        processes.cleanup_group(leader)
    assert waits == [2, 2] and signals == [signal.SIGTERM, signal.SIGKILL]
    assert time.monotonic() - started < 0.5
