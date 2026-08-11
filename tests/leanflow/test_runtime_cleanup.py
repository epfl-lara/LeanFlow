"""Tests for native runner process-service cleanup."""

from __future__ import annotations

import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from leanflow_cli.native import runtime_cleanup


class _ControlledThread(threading.Thread):
    """Model a foreground thread with deterministic join-time progression."""

    def __init__(self, clock: list[float], *, exit_after_joins: int | None = None):
        super().__init__(name="controlled-foreground")
        self._clock = clock
        self._exit_after_joins = exit_after_joins
        self._alive = True
        self.join_timeouts: list[float] = []

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout: float | None = None) -> None:
        assert timeout is not None
        self.join_timeouts.append(timeout)
        self._clock[0] += timeout
        if self._exit_after_joins is not None and len(self.join_timeouts) >= self._exit_after_joins:
            self._alive = False


def test_foreground_drain_reinterrupts_and_clears_exact_dead_worker(monkeypatch):
    clock = [10.0]
    worker = _ControlledThread(clock, exit_after_joins=2)
    interrupts: list[str] = []
    agent = SimpleNamespace(
        _managed_foreground_worker=worker,
        interrupt=interrupts.append,
    )
    monkeypatch.setattr(runtime_cleanup.time, "monotonic", lambda: clock[0])

    runtime_cleanup.drain_managed_foreground_worker(
        agent,
        timeout_s=0.25,
        reason="signal finalization retry",
    )

    assert interrupts == ["signal finalization retry"]
    assert worker.join_timeouts == pytest.approx([0.1, 0.1])
    assert not hasattr(agent, "_managed_foreground_worker")


def test_foreground_drain_preserves_newer_worker_registration(monkeypatch):
    clock = [20.0]
    captured = _ControlledThread(clock, exit_after_joins=1)
    replacement = threading.Thread(name="replacement-foreground")
    agent = SimpleNamespace(_managed_foreground_worker=captured)

    def replace_worker(_reason: str) -> None:
        agent._managed_foreground_worker = replacement

    agent.interrupt = replace_worker
    monkeypatch.setattr(runtime_cleanup.time, "monotonic", lambda: clock[0])

    runtime_cleanup.drain_managed_foreground_worker(agent, timeout_s=0.2)

    assert captured.is_alive() is False
    assert agent._managed_foreground_worker is replacement


def test_foreground_drain_raises_at_bounded_deadline_in_short_slices(monkeypatch):
    clock = [30.0]
    worker = _ControlledThread(clock)
    interrupts: list[str] = []
    agent = SimpleNamespace(
        _managed_foreground_worker=worker,
        interrupt=interrupts.append,
    )
    monkeypatch.setattr(runtime_cleanup.time, "monotonic", lambda: clock[0])

    with pytest.raises(RuntimeError, match="still live after bounded drain"):
        runtime_cleanup.drain_managed_foreground_worker(agent, timeout_s=0.25)

    assert interrupts == ["native runner foreground drain"]
    assert sum(worker.join_timeouts) == pytest.approx(0.25)
    assert worker.join_timeouts
    assert all(0.0 < timeout <= 0.1 for timeout in worker.join_timeouts)
    assert agent._managed_foreground_worker is worker


@pytest.mark.parametrize(
    "configured, expected",
    [
        ("999", 30.0),
        ("-1", 0.0),
        ("nan", 10.0),
        ("invalid", 10.0),
        ("inf", 30.0),
    ],
)
def test_foreground_drain_env_timeout_is_finite_and_capped(
    monkeypatch,
    configured,
    expected,
):
    monkeypatch.setenv("LEANFLOW_NATIVE_FOREGROUND_DRAIN_TIMEOUT_S", configured)

    assert runtime_cleanup._native_foreground_drain_timeout_s() == expected


def test_foreground_drain_rejects_self_join():
    agent = SimpleNamespace(_managed_foreground_worker=threading.current_thread())

    with pytest.raises(RuntimeError, match="cannot drain the current thread"):
        runtime_cleanup.drain_managed_foreground_worker(agent, timeout_s=0.1)


def test_native_run_finalizer_runs_ordered_exit_sequence_once():
    calls: list[str] = []
    finalizer = runtime_cleanup.NativeRunFinalizer()

    first = finalizer.finalize(
        130,
        stop_owned_work=lambda: calls.append("stop"),
        persist_checkpoint=lambda: calls.append("checkpoint"),
        release_locks=lambda: calls.append("locks"),
        persist_exited=lambda: calls.append("status"),
        emit_runner_exit=lambda: calls.append("activity"),
        record_outcome=lambda: calls.append("outcome"),
    )
    second = finalizer.finalize(
        0,
        stop_owned_work=lambda: calls.append("duplicate-stop"),
        release_locks=lambda: calls.append("duplicate-locks"),
        persist_exited=lambda: calls.append("duplicate-status"),
        emit_runner_exit=lambda: calls.append("duplicate-activity"),
        record_outcome=lambda: calls.append("duplicate-outcome"),
    )

    assert first == 130
    assert second == 130
    assert finalizer.finalized is True
    assert calls == ["stop", "checkpoint", "status", "activity", "outcome", "locks"]


def test_native_run_finalizer_attempts_durable_records_after_cleanup_failure():
    calls: list[str] = []
    finalizer = runtime_cleanup.NativeRunFinalizer()

    def fail_owned_work():
        calls.append("stop")
        raise runtime_cleanup.NativeTerminationSignal(15)

    result = finalizer.finalize(
        2,
        stop_owned_work=fail_owned_work,
        release_locks=lambda: calls.append("locks"),
        persist_exited=lambda: calls.append("status"),
        emit_runner_exit=lambda: calls.append("activity"),
        record_outcome=lambda: calls.append("outcome"),
    )

    assert result == 130
    assert calls == ["stop", "status", "activity", "outcome", "locks"]


def test_native_run_finalizer_stop_phase_signal_skips_math_authority():
    calls: list[str] = []

    class _Authority:
        def __enter__(self):
            pytest.fail("signal exit entered mathematical outcome authority")

        def __exit__(self, *_args):
            return None

    def stop():
        calls.append("stop")
        raise runtime_cleanup.NativeTerminationSignal(15)

    result = runtime_cleanup.NativeRunFinalizer().finalize(
        0,
        stop_owned_work=stop,
        outcome_authority=_Authority,
        select_outcome=lambda code: calls.append(f"select:{code}") or code,
        failure_exit_code=2,
        release_locks=lambda: calls.append("locks"),
        persist_exited=lambda: calls.append("status"),
        emit_runner_exit=lambda: calls.append("activity"),
        record_outcome=lambda: calls.append("outcome"),
    )

    assert result == 130
    assert calls == ["stop", "select:130", "status", "activity", "outcome", "locks"]


@pytest.mark.parametrize("failure_stage", ["enter", "exit"])
@pytest.mark.parametrize("failure_kind, expected_code", [("runtime", 2), ("signal", 130)])
def test_native_run_finalizer_authority_failure_caches_pause_and_releases_locks(
    failure_stage,
    failure_kind,
    expected_code,
):
    calls: list[str] = []
    finalizer = runtime_cleanup.NativeRunFinalizer()

    class _FailingAuthority:
        @staticmethod
        def fail():
            if failure_kind == "signal":
                raise runtime_cleanup.NativeTerminationSignal(15)
            raise RuntimeError("authority broke")

        def __enter__(self):
            calls.append("authority-enter")
            if failure_stage == "enter":
                self.fail()

        def __exit__(self, *_args):
            calls.append("authority-exit")
            if failure_stage == "exit":
                self.fail()

    first = finalizer.finalize(
        0,
        stop_owned_work=lambda: calls.append("stop"),
        outcome_authority=_FailingAuthority,
        select_outcome=lambda code: code,
        failure_exit_code=2,
        handle_finalization_failure=lambda code, detail: calls.append("downgrade") or code,
        persist_finalization_failure=lambda: calls.append("failure-status"),
        release_locks=lambda: calls.append("locks"),
        persist_exited=lambda: calls.append("status"),
        emit_runner_exit=lambda: calls.append("activity"),
        record_outcome=lambda: calls.append("outcome"),
    )
    repeated = finalizer.finalize(
        3,
        stop_owned_work=lambda: pytest.fail("repeated finalization ran"),
        release_locks=lambda: pytest.fail("repeated finalization released"),
        persist_exited=lambda: pytest.fail("repeated finalization persisted"),
        emit_runner_exit=lambda: pytest.fail("repeated finalization emitted"),
        record_outcome=lambda: pytest.fail("repeated finalization recorded"),
    )

    assert first == expected_code
    assert repeated == expected_code
    assert calls[-1] == "locks"
    assert "downgrade" in calls
    if failure_stage == "enter":
        assert calls == [
            "stop",
            "authority-enter",
            "downgrade",
            "status",
            "activity",
            "outcome",
            "locks",
        ]
    else:
        assert calls == [
            "stop",
            "authority-enter",
            "status",
            "activity",
            "outcome",
            "authority-exit",
            "downgrade",
            "failure-status",
            "locks",
        ]


@pytest.mark.parametrize("failing_step", ["checkpoint", "status", "activity", "outcome"])
def test_native_run_finalizer_required_persistence_failure_downgrades_math_exit(
    failing_step,
):
    calls: list[str] = []
    finalizer = runtime_cleanup.NativeRunFinalizer()

    def step(name):
        def run():
            calls.append(name)
            if name == failing_step:
                raise RuntimeError(f"{name} failed")

        return run

    result = finalizer.finalize(
        3,
        stop_owned_work=step("stop"),
        select_outcome=lambda code: code,
        failure_exit_code=2,
        handle_finalization_failure=lambda code, detail: calls.append("downgrade") or code,
        persist_finalization_failure=step("corrective-status"),
        persist_checkpoint=step("checkpoint"),
        release_locks=step("locks"),
        persist_exited=step("status"),
        emit_runner_exit=step("activity"),
        record_outcome=step("outcome"),
    )

    assert result == 2
    assert (
        finalizer.finalize(
            0,
            stop_owned_work=lambda: None,
            release_locks=lambda: None,
            persist_exited=lambda: None,
            emit_runner_exit=lambda: None,
            record_outcome=lambda: None,
        )
        == 2
    )
    assert calls[-1] == "locks"
    assert "downgrade" in calls
    assert "corrective-status" in calls


def test_native_run_finalizer_lock_release_failure_downgrades_math_exit():
    calls: list[str] = []
    finalizer = runtime_cleanup.NativeRunFinalizer()

    def fail_release():
        calls.append("locks")
        raise RuntimeError("lock registry unavailable")

    result = finalizer.finalize(
        0,
        stop_owned_work=lambda: calls.append("stop"),
        select_outcome=lambda code: code,
        failure_exit_code=2,
        handle_finalization_failure=lambda code, detail: calls.append("downgrade") or code,
        persist_finalization_failure=lambda: calls.append("corrective-status"),
        release_locks=fail_release,
        persist_exited=lambda: calls.append("status"),
        emit_runner_exit=lambda: calls.append("activity"),
        record_outcome=lambda: calls.append("outcome"),
    )

    assert result == 2
    assert calls[-2:] == ["downgrade", "corrective-status"]


@pytest.mark.parametrize("failing_step", ["checkpoint", "status", "locks"])
def test_native_run_finalizer_preserves_signal_130_on_cleanup_runtime_failure(failing_step):
    calls: list[str] = []
    finalizer = runtime_cleanup.NativeRunFinalizer()

    def step(name):
        def run():
            calls.append(name)
            if name == failing_step:
                raise RuntimeError(f"{name} failed")

        return run

    result = finalizer.finalize(
        130,
        stop_owned_work=step("stop"),
        select_outcome=lambda code: code,
        failure_exit_code=2,
        handle_finalization_failure=lambda code, detail: calls.append("correct") or code,
        persist_finalization_failure=step("corrective-status"),
        persist_checkpoint=step("checkpoint"),
        release_locks=step("locks"),
        persist_exited=step("status"),
        emit_runner_exit=step("activity"),
        record_outcome=step("outcome"),
    )

    assert result == 130
    assert (
        finalizer.finalize(
            0,
            stop_owned_work=lambda: None,
            release_locks=lambda: None,
            persist_exited=lambda: None,
            emit_runner_exit=lambda: None,
            record_outcome=lambda: None,
        )
        == 130
    )
    assert "correct" in calls


def test_native_run_finalizer_preserves_initial_signal_when_selector_raises():
    calls: list[str] = []

    result = runtime_cleanup.NativeRunFinalizer().finalize(
        130,
        stop_owned_work=lambda: calls.append("stop"),
        select_outcome=lambda _code: (_ for _ in ()).throw(RuntimeError("selector failed")),
        failure_exit_code=2,
        release_locks=lambda: calls.append("locks"),
        persist_exited=lambda: calls.append("status"),
        emit_runner_exit=lambda: calls.append("activity"),
        record_outcome=lambda: calls.append("outcome"),
    )

    assert result == 130
    assert calls == ["stop", "status", "activity", "outcome", "locks"]


def test_native_run_finalizer_preserves_signal_when_failure_handler_raises():
    calls: list[str] = []

    def fail_status():
        calls.append("status")
        raise RuntimeError("status failed")

    def fail_handler(_code, _detail):
        calls.append("handler")
        raise RuntimeError("handler failed")

    result = runtime_cleanup.NativeRunFinalizer().finalize(
        130,
        stop_owned_work=lambda: calls.append("stop"),
        select_outcome=lambda code: code,
        failure_exit_code=2,
        handle_finalization_failure=fail_handler,
        persist_exited=fail_status,
        emit_runner_exit=lambda: calls.append("activity"),
        record_outcome=lambda: calls.append("outcome"),
        release_locks=lambda: calls.append("locks"),
    )

    assert result == 130
    assert calls == ["stop", "status", "activity", "outcome", "handler", "locks"]


def test_native_run_finalizer_keeps_signal_sticky_after_later_release_failure():
    calls: list[str] = []

    def signal_status():
        calls.append("status")
        raise runtime_cleanup.NativeTerminationSignal(15)

    def fail_release():
        calls.append("locks")
        raise RuntimeError("release failed")

    result = runtime_cleanup.NativeRunFinalizer().finalize(
        0,
        stop_owned_work=lambda: calls.append("stop"),
        select_outcome=lambda code: code,
        failure_exit_code=2,
        handle_finalization_failure=lambda code, _detail: calls.append(f"correct:{code}") or code,
        persist_finalization_failure=lambda: calls.append("corrective-status"),
        persist_exited=signal_status,
        emit_runner_exit=lambda: calls.append("activity"),
        record_outcome=lambda: calls.append("outcome"),
        release_locks=fail_release,
    )

    assert result == 130
    assert "correct:130" in calls


def test_shutdown_attempts_every_service_after_failure(monkeypatch):
    calls: list[str] = []

    class _Client:
        def close(self):
            calls.append("anthropic")
            raise RuntimeError("provider close failed")

    agent = SimpleNamespace(_anthropic_client=_Client(), client=None)

    import leanflow_cli.lean.lean_incremental as lean_incremental
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setattr(
        lean_incremental,
        "close_incremental_sessions",
        lambda: calls.append("incremental"),
    )
    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda: calls.append("mcp"))

    failures = runtime_cleanup.shutdown_native_runtime_services(agent)

    assert failures == ("provider clients",)
    assert calls == ["anthropic", "incremental", "mcp"]


def test_shutdown_bounds_incremental_close_while_foreground_probe_lock_is_held(
    monkeypatch,
):
    """Let interrupted native processes exit despite an abandoned probe lock."""
    release = threading.Event()

    import leanflow_cli.lean.lean_incremental as lean_incremental
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setenv("LEANFLOW_NATIVE_INCREMENTAL_CLOSE_TIMEOUT_S", "0.02")
    monkeypatch.setattr(
        lean_incremental,
        "close_incremental_sessions",
        lambda: release.wait(timeout=1.0) or True,
    )
    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda: ())

    try:
        failures = runtime_cleanup.shutdown_native_runtime_services(None)
    finally:
        release.set()

    assert failures == ("incremental Lean sessions",)


def test_shutdown_sweeps_expert_commands_and_attempts_later_services(monkeypatch):
    calls: list[str] = []

    import leanflow_cli.lean.lean_incremental as lean_incremental
    import tools.mcp.mcp_tool as mcp_tool
    from leanflow_cli.cli import expert_help

    monkeypatch.setattr(
        runtime_cleanup,
        "_close_agent_terminal_resources",
        lambda _agent: calls.append("terminal"),
    )
    monkeypatch.setattr(
        expert_help,
        "shutdown_active_expert_commands",
        lambda: calls.append("expert") or (4242,),
    )
    monkeypatch.setattr(
        runtime_cleanup,
        "_close_agent_provider_clients",
        lambda _agent: calls.append("provider"),
    )
    monkeypatch.setattr(
        lean_incremental,
        "close_incremental_sessions",
        lambda: calls.append("incremental"),
    )
    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda: calls.append("mcp"))

    failures = runtime_cleanup.shutdown_native_runtime_services(object())

    assert failures == ("expert commands",)
    assert calls == ["terminal", "expert", "provider", "incremental", "mcp"]


def test_shutdown_reports_retained_mcp_server_as_finalizer_failure(monkeypatch):
    """Do not claim native runtime cleanup while an MCP process remains owned."""
    import leanflow_cli.lean.lean_incremental as lean_incremental
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setattr(lean_incremental, "close_incremental_sessions", lambda: None)
    monkeypatch.setattr(
        mcp_tool,
        "shutdown_mcp_servers",
        lambda: ("lean-lsp",),
    )

    assert runtime_cleanup.shutdown_native_runtime_services(None) == ("MCP servers",)


def test_shutdown_preserves_termination_after_attempting_every_service(monkeypatch):
    calls: list[str] = []

    import leanflow_cli.lean.lean_incremental as lean_incremental
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setattr(
        runtime_cleanup,
        "_close_agent_terminal_resources",
        lambda _agent: calls.append("terminal")
        or (_ for _ in ()).throw(runtime_cleanup.NativeTerminationSignal(15)),
    )
    monkeypatch.setattr(
        runtime_cleanup,
        "_close_agent_provider_clients",
        lambda _agent: calls.append("provider"),
    )
    monkeypatch.setattr(
        lean_incremental,
        "close_incremental_sessions",
        lambda: calls.append("incremental"),
    )
    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda: calls.append("mcp"))

    with pytest.raises(runtime_cleanup.NativeTerminationSignal):
        runtime_cleanup.shutdown_native_runtime_services(object())

    assert calls == ["terminal", "provider", "incremental", "mcp"]


def test_termination_handlers_turn_hup_into_catchable_cleanup_signal(monkeypatch):
    original_hup = object()
    original_term = object()
    originals = {1: original_hup, 15: original_term}
    installed: dict[int, object] = {}
    callbacks: list[int] = []

    monkeypatch.setattr(runtime_cleanup, "_native_termination_signals", lambda: (1, 15))
    monkeypatch.setattr(
        runtime_cleanup.signal,
        "getsignal",
        lambda signum: originals[signum],
    )
    monkeypatch.setattr(
        runtime_cleanup.signal,
        "signal",
        lambda signum, handler: installed.__setitem__(signum, handler),
    )

    previous = runtime_cleanup.install_native_termination_handlers(callbacks.append)
    hup_handler = installed[1]

    with pytest.raises(runtime_cleanup.NativeTerminationSignal) as caught:
        assert callable(hup_handler)
        hup_handler(1, None)

    assert caught.value.signum == 1
    assert callbacks == [1]
    assert installed == {1: runtime_cleanup.signal.SIG_IGN, 15: runtime_cleanup.signal.SIG_IGN}

    runtime_cleanup.restore_native_termination_handlers(previous)

    assert installed == {1: original_hup, 15: original_term}


def test_shutdown_closes_both_provider_client_shapes(monkeypatch):
    calls: list[tuple[str, object]] = []
    anthropic = SimpleNamespace(close=lambda: calls.append(("anthropic", None)))
    openai = object()

    class _Agent:
        _anthropic_client = anthropic
        client = openai

        def _close_openai_client(self, client, *, reason, shared):
            calls.append((reason, (client, shared)))

    import leanflow_cli.lean.lean_incremental as lean_incremental
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setattr(lean_incremental, "close_incremental_sessions", lambda: None)
    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda: None)
    agent = _Agent()

    assert runtime_cleanup.shutdown_native_runtime_services(agent) == ()
    assert calls == [
        ("anthropic", None),
        ("native_runner_exit", (openai, True)),
    ]
    assert agent._anthropic_client is None
    assert agent.client is None


def test_shutdown_reaps_only_the_managed_agents_terminal_task(monkeypatch):
    calls: list[tuple[str, str]] = []
    agent = SimpleNamespace(
        _managed_tool_task_id="leanflow-native-owned",
        session_id="provider-session",
        _anthropic_client=None,
        client=None,
    )

    import leanflow_cli.lean.lean_incremental as lean_incremental
    import tools.implementations.terminal_tool as terminal_tool
    import tools.mcp.mcp_tool as mcp_tool
    from tools.utilities.process_registry import process_registry

    monkeypatch.setattr(
        process_registry,
        "kill_task_processes",
        lambda task_id: calls.append(("processes", task_id)) or (),
    )
    monkeypatch.setattr(
        terminal_tool,
        "cleanup_vm",
        lambda task_id: calls.append(("environment", task_id)),
    )
    monkeypatch.setattr(
        terminal_tool,
        "clear_task_env_overrides",
        lambda task_id: calls.append(("overrides", task_id)),
    )
    monkeypatch.setattr(lean_incremental, "close_incremental_sessions", lambda: None)
    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda: None)

    assert runtime_cleanup.shutdown_native_runtime_services(agent) == ()
    assert calls == [
        ("processes", "leanflow-native-owned"),
        ("environment", "leanflow-native-owned"),
        ("overrides", "leanflow-native-owned"),
    ]


def test_terminal_cleanup_attempts_environment_after_process_registry_failure(monkeypatch):
    calls: list[str] = []
    agent = SimpleNamespace(_managed_tool_task_id="owned-task")

    import tools.implementations.terminal_tool as terminal_tool
    from tools.utilities.process_registry import process_registry

    def fail_processes(_task_id):
        calls.append("processes")
        raise RuntimeError("registry failed")

    monkeypatch.setattr(process_registry, "kill_task_processes", fail_processes)
    monkeypatch.setattr(
        terminal_tool,
        "cleanup_vm",
        lambda _task_id: calls.append("environment"),
    )
    monkeypatch.setattr(
        terminal_tool,
        "clear_task_env_overrides",
        lambda _task_id: calls.append("overrides"),
    )

    with pytest.raises(RuntimeError, match="1 terminal cleanup operation"):
        runtime_cleanup._close_agent_terminal_resources(agent)

    assert calls == ["processes", "environment", "overrides"]


def test_shutdown_attempts_later_services_after_keyboard_interrupt(monkeypatch):
    calls: list[str] = []

    class _Client:
        def close(self):
            calls.append("provider")
            raise KeyboardInterrupt

    import leanflow_cli.lean.lean_incremental as lean_incremental
    import tools.mcp.mcp_tool as mcp_tool

    monkeypatch.setattr(
        lean_incremental,
        "close_incremental_sessions",
        lambda: calls.append("incremental"),
    )
    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda: calls.append("mcp"))

    with pytest.raises(KeyboardInterrupt):
        runtime_cleanup.shutdown_native_runtime_services(
            SimpleNamespace(_anthropic_client=_Client(), client=None)
        )

    assert calls == ["provider", "incremental", "mcp"]


def test_native_process_exit_flushes_then_skips_thread_finalization(monkeypatch):
    calls: list[object] = []

    class _Stream:
        def flush(self):
            calls.append("flush")

    class _Exited(RuntimeError):
        pass

    monkeypatch.setattr(runtime_cleanup.sys, "stdout", _Stream())
    monkeypatch.setattr(runtime_cleanup.sys, "stderr", _Stream())
    monkeypatch.setattr(
        runtime_cleanup,
        "_finalize_multiprocessing_semaphores",
        lambda: calls.append("semaphores"),
    )

    def fake_exit(code):
        calls.append(code)
        raise _Exited

    monkeypatch.setattr(runtime_cleanup.os, "_exit", fake_exit)

    with pytest.raises(_Exited):
        runtime_cleanup.exit_native_process(2)

    assert calls == ["flush", "flush", "semaphores", 2]


def test_native_process_exit_finalizes_named_semaphores_without_warning():
    script = """
import multiprocessing as mp
from leanflow_cli.native.runtime_cleanup import exit_native_process

semaphore = mp.get_context("spawn").Semaphore(1)
exit_native_process(0)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "leaked semaphore" not in completed.stderr


def test_semaphore_finalization_does_not_run_unrelated_finalizers(monkeypatch):
    calls: list[str] = []

    def semaphore_cleanup():
        calls.append("semaphore")

    semaphore_cleanup.__module__ = "multiprocessing.synchronize"
    semaphore_cleanup.__qualname__ = "SemLock._cleanup"

    def unrelated_cleanup():
        calls.append("unrelated")

    class _Finalizer:
        def __init__(self, callback):
            self._callback = callback

        def __call__(self):
            self._callback()

    import multiprocessing.util as multiprocessing_util

    monkeypatch.setattr(
        multiprocessing_util,
        "_finalizer_registry",
        {
            (0, 0): _Finalizer(semaphore_cleanup),
            (0, 1): _Finalizer(unrelated_cleanup),
        },
    )

    runtime_cleanup._finalize_multiprocessing_semaphores()

    assert calls == ["semaphore"]
