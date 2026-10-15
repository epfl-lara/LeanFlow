"""Distinguish verified prelaunch failures from surviving or unobservable native helpers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent.runtime.harness import HarnessRequest, IsolationPolicy, processes, run_harness, runner
from agent.runtime.harness.isolation import sandbox_command
from agent.runtime.harness.models import BackendError, NativeCleanupUnverified, NativeLaunchFailure

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Owned process groups require POSIX")


@pytest.fixture
def launch_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Run real subprocess fixtures with kernel sandbox behavior tested separately."""
    monkeypatch.setattr(runner, "sandbox_command", lambda argv, policy, **kwargs: argv)

    def make(discovery="print('[]')\n", author="", **kwargs):
        executable = tmp_path / "native-cli"
        executable.write_text(
            f"#!{sys.executable}\nimport sys\n"
            "if sys.argv[1:3] == ['mcp', 'list']:\n"
            + "".join("    " + line + "\n" for line in discovery.splitlines())
            + "    sys.exit(0)\n"
            + author
        )
        executable.chmod(0o700)
        return HarnessRequest(
            "lemex",
            "fixture task",
            tmp_path,
            executable=str(executable),
            isolation=IsolationPolicy(tmp_path),
            session_id="prior-session",
            **kwargs,
        )

    return make


def _stop_child(marker: Path) -> None:
    """Close only this fixture's actor if an assertion failed before controller cleanup."""
    if marker.is_file():
        try:
            os.kill(int(marker.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _discovery_child(marker: Path, *, detached: bool, response: str | None) -> str:
    """Keep a helper child visible before returning metadata or waiting for cancellation."""
    child = (
        "import os,pathlib,time;"
        f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(30)"
    )
    outputs = ",stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL" if response is not None else ""
    body = (
        "import pathlib,subprocess,time\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session={detached!r}{outputs})\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        "until=time.monotonic()+3\n"
        "while not marker.exists():\n"
        "    assert time.monotonic()<until\n    time.sleep(0.01)\n"
    )
    return body + (
        "time.sleep(30)\n" if response is None else f"time.sleep(0.4)\nprint({response!r})\n"
    )


@pytest.mark.parametrize(
    "kind", ["missing_policy", "wrong_root", "invalid_deadline", "bad_backend"]
)
def test_pure_prelaunch_rejection_has_explicit_cleanup_evidence(
    launch_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    request = launch_script()
    if kind == "missing_policy":
        request = replace(request, isolation=None)
    elif kind == "wrong_root":
        request = replace(request, isolation=IsolationPolicy(tmp_path / "other"))
    elif kind == "invalid_deadline":
        request = replace(request, deadline=float("nan"))
    else:
        request = replace(request, backend="unsupported")
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *a, **kw: pytest.fail("No actor may launch")
    )
    with pytest.raises(NativeLaunchFailure) as caught:
        run_harness(request)
    result = caught.value.result
    assert result.cleanup_verified and result.status == "startup_error"
    assert (
        result.session_id == "prior-session" and result.usage is None and result.returncode is None
    )


def test_snapshot_hardlink_rejection_is_verified_without_launch(
    launch_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = launch_script()
    original = tmp_path / "original"
    original.write_text("unchanged")
    os.link(original, tmp_path / "hardlink")
    monkeypatch.setattr(runner, "sandbox_command", sandbox_command)
    with pytest.raises(NativeLaunchFailure, match="hardlink") as caught:
        run_harness(request)
    assert caught.value.result.cleanup_verified and original.read_text() == "unchanged"


@pytest.mark.parametrize("kind", ["log_path", "admission"])
def test_local_setup_failure_after_discovery_never_launches_author(
    launch_script, tmp_path: Path, kind: str
) -> None:
    marker = tmp_path / "author-started"
    request = launch_script(author=f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
    if kind == "log_path":
        blocker = tmp_path / "plain-file"
        blocker.write_text("data")
        request = replace(request, log_path=blocker / "native.log")
    else:

        def reject():
            raise BackendError("No call was admitted")

        request = replace(request, on_admitting=reject)
    with pytest.raises(NativeLaunchFailure) as caught:
        run_harness(request)
    assert caught.value.result.cleanup_verified and not marker.exists()


@pytest.mark.parametrize("detached", [False, True])
def test_bad_discovery_certifies_no_launch_only_after_real_helper_closure(
    launch_script, tmp_path: Path, detached: bool
) -> None:
    child = tmp_path / "discovery-child"
    author = tmp_path / "author-started"
    request = launch_script(
        _discovery_child(child, detached=detached, response="not metadata"),
        f"from pathlib import Path\nPath({str(author)!r}).touch()\n",
    )
    try:
        with pytest.raises(NativeLaunchFailure, match="unsupported response") as caught:
            run_harness(request)
        assert caught.value.result.cleanup_verified and not author.exists()
        pid = int(child.read_text())
        table = processes._cleanup_states(2)
        assert pid not in table or table[pid][1].startswith("Z")
    finally:
        _stop_child(child)


@pytest.mark.parametrize("outcome", ["cancelled", "deadline_exceeded"])
def test_stop_during_real_discovery_returns_verified_no_launch_after_helper_shutdown(
    launch_script, tmp_path: Path, outcome: str
) -> None:
    child = tmp_path / "discovery-child"
    author = tmp_path / "author-started"
    request = launch_script(
        _discovery_child(child, detached=True, response=None),
        f"from pathlib import Path\nPath({str(author)!r}).touch()\n",
    )
    request = replace(
        request,
        cancelled=child.exists if outcome == "cancelled" else None,
        deadline=time.monotonic() + 0.5 if outcome == "deadline_exceeded" else None,
    )
    try:
        with pytest.raises(NativeLaunchFailure) as caught:
            run_harness(request)
        assert caught.value.result.status == outcome and caught.value.result.cleanup_verified
        assert not author.exists() and caught.value.result.usage is None
        pid = int(child.read_text())
        table = processes._cleanup_states(2)
        assert pid not in table or table[pid][1].startswith("Z")
    finally:
        _stop_child(child)


def test_unobserved_discovery_pipe_holder_never_certifies_no_launch(
    launch_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "unobserved-helper"
    request = launch_script(
        _discovery_child(child, detached=True, response=None), cancelled=child.exists
    )
    monkeypatch.setattr(processes.ProcessTree, "refresh", lambda self: None)
    try:
        with pytest.raises(NativeCleanupUnverified, match="output channels") as caught:
            run_harness(request)
        assert caught.value.result is None
        assert int(child.read_text()) in processes._cleanup_states(2)
    finally:
        _stop_child(child)


def test_uncertain_discovery_cleanup_is_never_relabelled_as_verified_prelaunch(
    launch_script, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processes, "verify_owned_stopped", lambda *a, **kw: False)
    with pytest.raises(NativeCleanupUnverified) as caught:
        run_harness(launch_script())
    assert caught.value.result is None


@pytest.mark.parametrize("status", ["completed", "failed", "cleanup_unverified"])
def test_no_launch_capability_rejects_postlaunch_statuses(status: str) -> None:
    with pytest.raises(ValueError, match="prelaunch failure status"):
        NativeLaunchFailure("fixture", status=status)


@pytest.mark.parametrize("kind", ["fork", "timeout", "parse"])
def test_one_failed_process_sample_does_not_kill_a_healthy_native_turn(
    launch_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    marker = tmp_path / "author-started"
    request = launch_script(
        author=(
            "import json,time\nfrom pathlib import Path\n"
            f"Path({str(marker)!r}).touch()\ntime.sleep(0.8)\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}}),flush=True)\n"
        )
    )
    request = replace(request, session_id="")
    original = processes._processes
    failed = False

    def interrupted():
        nonlocal failed
        if marker.exists() and not failed:
            failed = True
            if kind == "timeout":
                raise subprocess.TimeoutExpired("ps", 2)
            if kind == "parse":
                raise ValueError("truncated row")
            raise OSError("temporary fork failure")
        return original()

    monkeypatch.setattr(processes, "_processes", interrupted)
    result = run_harness(request)
    assert failed and result.status == "completed" and result.cleanup_verified
    assert result.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


def test_repeated_sampling_failure_stops_with_verified_cleanup_and_observed_usage(
    launch_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "author-started"
    request = replace(
        launch_script(
            author=(
                "import json,time\nfrom pathlib import Path\n"
                f"Path({str(marker)!r}).touch()\n"
                "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':4,'output_tokens':2}}),flush=True)\n"
                "time.sleep(30)\n"
            )
        ),
        session_id="",
    )
    original = processes._processes
    failures = 0

    def unavailable():
        nonlocal failures
        if marker.exists() and failures < 5:
            failures += 1
            raise ValueError("temporary process table loss")
        return original()

    monkeypatch.setattr(processes, "_processes", unavailable)
    started = time.monotonic()
    result = run_harness(request)
    assert failures == 5 and time.monotonic() - started < 5
    assert result.status == "failed" and "tracking metadata" in result.error
    assert result.cleanup_verified and result.usage == {
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
    }


@pytest.mark.parametrize("known_usage", [False, True])
def test_uncertain_postlaunch_cleanup_carries_observed_usage_without_inventing_it(
    launch_script, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, known_usage: bool
) -> None:
    marker = tmp_path / "author-started"
    event = {"type": "turn.completed"}
    if known_usage:
        event["usage"] = {"input_tokens": 11, "output_tokens": 7}
    request = replace(
        launch_script(
            author=(
                "import time\nfrom pathlib import Path\n"
                f"Path({str(marker)!r}).touch()\nprint({json.dumps(event)!r},flush=True)\ntime.sleep(0.3)\n"
            )
        ),
        session_id="",
    )
    original = processes.verify_owned_stopped
    monkeypatch.setattr(
        processes,
        "verify_owned_stopped",
        lambda *a, **kw: False if marker.exists() else original(*a, **kw),
    )
    with pytest.raises(NativeCleanupUnverified) as caught:
        run_harness(request)
    result = caught.value.result
    assert (
        result is not None and result.status == "cleanup_unverified" and not result.cleanup_verified
    )
    assert result.usage == (
        {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18} if known_usage else None
    )
