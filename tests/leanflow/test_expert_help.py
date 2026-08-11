"""Process-lifecycle tests for command-backed expert advisors."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from leanflow_cli.cli import expert_help
from tools.utilities.interrupt import set_interrupt


class _AdvisorAbort(BaseException):
    """Interrupt an advisor call through the unexpected BaseException path."""


def _pid_is_live(process_id: int) -> bool:
    """Return whether a PID exists in a non-zombie state."""
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    completed = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(process_id)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    state = completed.stdout.strip()
    return bool(state) and not state.startswith("Z")


def _wait_for_pid_pair(path, *, timeout_s: float = 5.0) -> tuple[int, int]:
    """Return a leader/child PID pair after an advisor publishes it."""
    deadline = time.monotonic() + timeout_s
    last_content = ""
    while time.monotonic() < deadline:
        try:
            last_content = path.read_text(encoding="utf-8")
            leader, child = last_content.split(":", maxsplit=1)
            return int(leader), int(child)
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.01)
    raise AssertionError(f"advisor did not publish a complete PID pair: {last_content!r}")


def test_sandbox_codex_expert_falls_back_to_model_adapter_when_cli_missing(monkeypatch):
    monkeypatch.setenv("LEANFLOW_SANDBOX", "1")
    monkeypatch.setattr(expert_help.shutil, "which", lambda _command: None)

    assert expert_help.is_command_expert_provider("codex") is False
    assert expert_help.is_command_expert_provider("codex-cli") is False


def test_regular_codex_expert_remains_command_backed(monkeypatch):
    monkeypatch.delenv("LEANFLOW_SANDBOX", raising=False)
    monkeypatch.setattr(expert_help.shutil, "which", lambda _command: None)

    assert expert_help.is_command_expert_provider("codex") is True


def test_codex_expert_inherits_workflow_model_and_reasoning(monkeypatch, tmp_path):
    """Bind regular command advisors to the explicit all-lanes runtime."""
    captured = {}
    monkeypatch.setenv("AUXILIARY_LEAN_DECOMPOSE_HELPERS_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("AUXILIARY_LEAN_DECOMPOSE_HELPERS_REASONING_EFFORT", "xhigh")
    monkeypatch.setattr(expert_help, "record_expert_help_activity", lambda *_a, **_k: None)

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="advisor result", stderr="", timed_out=False)

    monkeypatch.setattr(expert_help, "_run_isolated_expert_command", run)

    result = expert_help.run_command_expert_help(
        provider="codex",
        task="lean_decompose_helpers",
        prompt="split the proof",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    command = captured["command"]
    assert command[:2] == ["codex", "exec"]
    assert command[2:6] == [
        "--model",
        "gpt-5.6-luna",
        "--config",
        'model_reasoning_effort="xhigh"',
    ]
    assert result.response == "advisor result"


def test_codex_expert_preserves_explicit_template_runtime(monkeypatch):
    """Do not duplicate model overrides already owned by a custom template."""
    monkeypatch.setenv("AUXILIARY_LEAN_REASONING_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("AUXILIARY_LEAN_REASONING_REASONING_EFFORT", "xhigh")

    command = expert_help._apply_command_runtime_overrides(
        [
            "codex",
            "exec",
            "--model",
            "custom-model",
            "--config",
            'model_reasoning_effort="high"',
            "-",
        ],
        provider="codex",
        task="lean_reasoning",
    )

    assert command.count("--model") == 1
    assert command.count("--config") == 1
    assert "custom-model" in command
    assert 'model_reasoning_effort="high"' in command


def test_advisor_interrupt_before_launch_does_not_spawn(monkeypatch, tmp_path):
    spawned: list[bool] = []
    set_interrupt(True)
    monkeypatch.setattr(
        expert_help.subprocess,
        "Popen",
        lambda *_args, **_kwargs: spawned.append(True),
    )
    try:
        with pytest.raises(InterruptedError, match="before launch"):
            expert_help._run_isolated_expert_command(
                [sys.executable, "-c", "pass"],
                input="advisor prompt",
                cwd=str(tmp_path),
                timeout=30,
            )
    finally:
        set_interrupt(False)

    assert spawned == []


def test_shutdown_generation_crossing_popen_cancels_new_registration(monkeypatch, tmp_path):
    real_generation = expert_help._expert_shutdown_generation

    def cross_shutdown_boundary() -> int:
        generation = real_generation()
        assert expert_help.shutdown_active_expert_commands(timeout_s=0) == ()
        return generation

    set_interrupt(False)
    monkeypatch.setattr(
        expert_help,
        "_expert_shutdown_generation",
        cross_shutdown_boundary,
    )

    started_at = time.monotonic()
    with pytest.raises(InterruptedError):
        expert_help._run_isolated_expert_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            input="advisor prompt",
            cwd=str(tmp_path),
            timeout=30,
        )

    assert time.monotonic() - started_at < 3
    with expert_help._ACTIVE_EXPERT_COMMANDS_LOCK:
        assert expert_help._ACTIVE_EXPERT_COMMANDS == {}


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX process groups")
def test_advisor_global_interrupt_reaps_detached_tree_and_unregisters(tmp_path):
    pid_path = tmp_path / "interrupt-pids.txt"
    script = "\n".join(
        [
            "import os, subprocess, sys, time",
            "from pathlib import Path",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)",
            "Path(sys.argv[1]).write_text(f'{os.getpid()}:{child.pid}', encoding='utf-8')",
            "time.sleep(60)",
        ]
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            expert_help._run_isolated_expert_command(
                [sys.executable, "-c", script, str(pid_path)],
                input="advisor prompt",
                cwd=str(tmp_path),
                timeout=30,
            )
        except BaseException as exc:
            errors.append(exc)

    set_interrupt(False)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    leader_pid = child_pid = 0
    try:
        leader_pid, child_pid = _wait_for_pid_pair(pid_path)
        set_interrupt(True)
        worker.join(timeout=3)

        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], InterruptedError)
        assert not _pid_is_live(leader_pid)
        assert not _pid_is_live(child_pid)
        with expert_help._ACTIVE_EXPERT_COMMANDS_LOCK:
            assert expert_help._ACTIVE_EXPERT_COMMANDS == {}
    finally:
        set_interrupt(False)
        for process_id in (leader_pid, child_pid):
            if process_id and _pid_is_live(process_id):
                os.kill(process_id, signal.SIGKILL)
        worker.join(timeout=2)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX process groups")
def test_process_owner_shutdown_cancels_active_advisor(tmp_path):
    pid_path = tmp_path / "shutdown-pids.txt"
    script = "\n".join(
        [
            "import os, subprocess, sys, time",
            "from pathlib import Path",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)",
            "Path(sys.argv[1]).write_text(f'{os.getpid()}:{child.pid}', encoding='utf-8')",
            "time.sleep(60)",
        ]
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            expert_help._run_isolated_expert_command(
                [sys.executable, "-c", script, str(pid_path)],
                input="advisor prompt",
                cwd=str(tmp_path),
                timeout=30,
            )
        except BaseException as exc:
            errors.append(exc)

    set_interrupt(False)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    leader_pid = child_pid = 0
    try:
        leader_pid, child_pid = _wait_for_pid_pair(pid_path)
        residual = expert_help.shutdown_active_expert_commands(timeout_s=3)
        worker.join(timeout=1)

        assert residual == ()
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], InterruptedError)
        assert not _pid_is_live(leader_pid)
        assert not _pid_is_live(child_pid)
    finally:
        set_interrupt(False)
        for process_id in (leader_pid, child_pid):
            if process_id and _pid_is_live(process_id):
                os.kill(process_id, signal.SIGKILL)
        worker.join(timeout=2)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX process groups")
def test_process_owner_shutdown_uses_known_group_when_process_inventory_is_unavailable(
    monkeypatch,
    tmp_path,
):
    """The owned Popen group remains cancellable when restricted hosts hide `ps e`."""
    errors: list[BaseException] = []

    def run() -> None:
        try:
            expert_help._run_isolated_expert_command(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                input="advisor prompt",
                cwd=str(tmp_path),
                timeout=30,
            )
        except BaseException as exc:
            errors.append(exc)

    set_interrupt(False)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with expert_help._ACTIVE_EXPERT_COMMANDS_LOCK:
            if expert_help._ACTIVE_EXPERT_COMMANDS:
                break
        time.sleep(0.01)
    monkeypatch.setattr(expert_help, "_snapshot_tagged_expert_processes", lambda _token: [])

    try:
        residual = expert_help.shutdown_active_expert_commands(timeout_s=3)
        worker.join(timeout=1)

        assert residual == ()
        assert not worker.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], InterruptedError)
    finally:
        set_interrupt(False)
        expert_help.shutdown_active_expert_commands(timeout_s=3)
        worker.join(timeout=2)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX process groups")
def test_advisor_timeout_terminates_spawned_grandchild(tmp_path):
    child_pid_path = tmp_path / "grandchild.pid"
    script = "\n".join(
        [
            "import subprocess, sys, time",
            "from pathlib import Path",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)",
            "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')",
            "print(child.pid, flush=True)",
            "time.sleep(60)",
        ]
    )

    result = expert_help._run_isolated_expert_command(
        [sys.executable, "-c", script, str(child_pid_path)],
        input="advisor prompt",
        cwd=str(tmp_path),
        timeout=1,
    )

    assert result.timed_out is True
    assert result.returncode is None
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while _pid_is_live(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert not _pid_is_live(child_pid)
    finally:
        if _pid_is_live(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_long_advisor_refreshes_parent_workflow_heartbeat(monkeypatch, tmp_path):
    heartbeats = []
    activities = []
    monkeypatch.setattr(expert_help, "_EXPERT_HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(
        expert_help,
        "touch_workflow_runtime_heartbeat",
        lambda: heartbeats.append(time.monotonic()) or True,
    )
    monkeypatch.setattr(
        expert_help,
        "record_expert_help_activity",
        lambda *args, **kwargs: activities.append((args, kwargs)),
    )

    result = expert_help._run_isolated_expert_command(
        [sys.executable, "-c", "import time; time.sleep(0.25); print('done')"],
        input="advisor prompt",
        cwd=str(tmp_path),
        timeout=2,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "done"
    assert heartbeats
    assert activities
    assert activities[0][0] == (
        "expert-help-heartbeat",
        "Expert help command remains active",
    )
    assert activities[0][1]["mode"] == "command"
    assert activities[0][1]["partial_response_available"] is False


def test_expert_heartbeat_bypasses_suppressed_stdout_and_updates_primary_log(monkeypatch):
    activities = []
    run_log = []

    class _Stream:
        def __init__(self):
            self.writes = []

        def write(self, value):
            self.writes.append(value)

        def flush(self):
            return None

    stream = _Stream()
    monkeypatch.setattr(
        expert_help, "append_workflow_activity", lambda *a, **kw: activities.append((a, kw))
    )
    monkeypatch.setattr(expert_help, "append_workflow_run_log", run_log.append)
    monkeypatch.setattr(expert_help.sys, "__stdout__", stream)

    expert_help.record_expert_help_activity(
        "expert-help-heartbeat",
        "Expert help command remains active",
        elapsed_s=45,
        timeout_s=600,
        mode="command",
    )

    assert activities
    assert run_log == stream.writes
    assert "45s elapsed" in run_log[0]
    assert "600s timeout" in run_log[0]


@pytest.mark.parametrize("detached", [False, True])
@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX process groups")
def test_advisor_timeout_finds_grandchild_after_leader_exits(tmp_path, detached):
    child_pid_path = tmp_path / f"reparented-{detached}.pid"
    script = "\n".join(
        [
            "import subprocess, sys",
            "from pathlib import Path",
            (
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)'], "
                f"start_new_session={detached!r})"
            ),
            "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')",
            "print(child.pid, flush=True)",
        ]
    )

    started_at = time.monotonic()
    result = expert_help._run_isolated_expert_command(
        [sys.executable, "-c", script, str(child_pid_path)],
        input="advisor prompt",
        cwd=str(tmp_path),
        timeout=1,
    )

    assert result.timed_out is True
    assert time.monotonic() - started_at < 5
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while _pid_is_live(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert not _pid_is_live(child_pid)
    finally:
        if _pid_is_live(child_pid):
            os.kill(child_pid, signal.SIGKILL)


@pytest.mark.skipif(
    not hasattr(signal, "SIGUSR1") or not hasattr(os, "killpg"),
    reason="requires POSIX signal handling",
)
def test_advisor_base_exception_cleans_detached_grandchild(tmp_path):
    child_pid_path = tmp_path / "base-exception-grandchild.pid"
    script = "\n".join(
        [
            "import os, signal, subprocess, sys, time",
            "from pathlib import Path",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)",
            "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')",
            "time.sleep(0.2)",
            "os.kill(os.getppid(), signal.SIGUSR1)",
            "time.sleep(60)",
        ]
    )

    previous_handler = signal.getsignal(signal.SIGUSR1)

    def abort_advisor(_signal_number, _frame):
        raise _AdvisorAbort()

    signal.signal(signal.SIGUSR1, abort_advisor)
    try:
        with pytest.raises(_AdvisorAbort):
            expert_help._run_isolated_expert_command(
                [sys.executable, "-c", script, str(child_pid_path)],
                input="advisor prompt",
                cwd=str(tmp_path),
                timeout=30,
            )
    finally:
        signal.signal(signal.SIGUSR1, previous_handler)

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while _pid_is_live(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert not _pid_is_live(child_pid)
    finally:
        if _pid_is_live(child_pid):
            os.kill(child_pid, signal.SIGKILL)
