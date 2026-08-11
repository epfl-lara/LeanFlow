"""Release process-wide services owned by the native workflow runner."""

from __future__ import annotations

import contextlib
import logging
import math
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, ContextManager, NoReturn

logger = logging.getLogger(__name__)

_FOREGROUND_DRAIN_TIMEOUT_ENV = "LEANFLOW_NATIVE_FOREGROUND_DRAIN_TIMEOUT_S"
_FOREGROUND_DRAIN_DEFAULT_TIMEOUT_S = 10.0
_FOREGROUND_DRAIN_MAX_TIMEOUT_S = 30.0
_FOREGROUND_DRAIN_JOIN_SLICE_S = 0.1
_INCREMENTAL_CLOSE_TIMEOUT_ENV = "LEANFLOW_NATIVE_INCREMENTAL_CLOSE_TIMEOUT_S"
_INCREMENTAL_CLOSE_DEFAULT_TIMEOUT_S = 5.0
_INCREMENTAL_CLOSE_MAX_TIMEOUT_S = 30.0


def native_exit_status_fields(exit_code: int, reason: str) -> dict[str, Any]:
    """Return normalized fields for one terminal live-status snapshot."""
    return {
        "exit_code": int(exit_code),
        "reason": str(reason or ""),
    }


class NativeTerminationSignal(BaseException):
    """Carry a catchable SIGHUP/SIGTERM request through native cleanup."""

    def __init__(self, signum: int):
        self.signum = int(signum)
        try:
            signal_name = signal.Signals(self.signum).name
        except ValueError:
            signal_name = str(self.signum)
        super().__init__(f"native process received {signal_name}")


def _native_foreground_drain_timeout_s(timeout_s: float | None = None) -> float:
    """Return a finite bounded grace period for foreground-thread drainage."""
    raw: str | float = (
        os.getenv(
            _FOREGROUND_DRAIN_TIMEOUT_ENV,
            str(_FOREGROUND_DRAIN_DEFAULT_TIMEOUT_S),
        )
        if timeout_s is None
        else timeout_s
    )
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = _FOREGROUND_DRAIN_DEFAULT_TIMEOUT_S
    if math.isnan(parsed):
        parsed = _FOREGROUND_DRAIN_DEFAULT_TIMEOUT_S
    return max(0.0, min(_FOREGROUND_DRAIN_MAX_TIMEOUT_S, parsed))


def _incremental_close_timeout_s() -> float:
    """Return the bounded LeanProbe close wait used during process exit."""
    raw = str(
        os.getenv(
            _INCREMENTAL_CLOSE_TIMEOUT_ENV,
            str(_INCREMENTAL_CLOSE_DEFAULT_TIMEOUT_S),
        )
        or ""
    )
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = _INCREMENTAL_CLOSE_DEFAULT_TIMEOUT_S
    if not math.isfinite(parsed):
        parsed = _INCREMENTAL_CLOSE_DEFAULT_TIMEOUT_S
    return max(0.01, min(_INCREMENTAL_CLOSE_MAX_TIMEOUT_S, parsed))


def _close_incremental_sessions_bounded(close: Callable[[], object]) -> bool:
    """Close LeanProbe without waiting forever on an abandoned tool lock.

    An interrupted foreground check can still own LeanProbe's internal lock
    after its Lean child is gone. The native process exits through ``os._exit``
    after cleanup, so a timed-out daemon closer may be abandoned safely while
    the caller records a truthful runtime-cleanup failure.
    """
    result: list[object] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            result.append(close())
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(
        target=target,
        name="leanflow-incremental-close",
        daemon=True,
    )
    worker.start()
    worker.join(timeout=_incremental_close_timeout_s())
    if worker.is_alive():
        return False
    if errors:
        raise errors[0]
    return bool(result) and result[0] is not False


def drain_managed_foreground_worker(
    agent: Any,
    *,
    timeout_s: float | None = None,
    reason: str = "native runner foreground drain",
) -> None:
    """Cooperatively drain the exact foreground worker captured at entry.

    Reissue the agent interrupt, then join only that thread in short slices up
    to the configured deadline.  A replacement registered while the captured
    worker unwinds remains attached to the agent.  A live captured worker is a
    hard quiescence failure because callers must not checkpoint around it.
    """
    if agent is None:
        return
    worker = getattr(agent, "_managed_foreground_worker", None)
    if not isinstance(worker, threading.Thread):
        return
    if worker is threading.current_thread():
        raise RuntimeError("cannot drain the current thread as a foreground worker")

    if worker.is_alive():
        interrupt = getattr(agent, "interrupt", None)
        if callable(interrupt):
            try:
                interrupt(reason)
            except Exception:
                logger.debug("Foreground drain interrupt failed", exc_info=True)

        timeout = _native_foreground_drain_timeout_s(timeout_s)
        deadline = time.monotonic() + timeout
        while worker.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            worker.join(timeout=min(_FOREGROUND_DRAIN_JOIN_SLICE_S, remaining))

    if worker.is_alive():
        raise RuntimeError(f"foreground worker {worker.name!r} is still live after bounded drain")
    if getattr(agent, "_managed_foreground_worker", None) is worker:
        delattr(agent, "_managed_foreground_worker")


class NativeRunFinalizer:
    """Run the native runner's ordered exit sequence at most once.

    Exit requests can arrive through an inner control loop, the outer signal
    handler, and the enclosing ``finally`` block.  The first request owns the
    truthful process outcome; later requests return that same code without
    repeating persistence or activity events.  Each cleanup step is isolated
    so one failing subsystem cannot prevent the remaining exit record.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._finalized = False
        self._exit_code: int | None = None

    @property
    def finalized(self) -> bool:
        """Return whether an exit sequence has already been claimed."""
        with self._lock:
            return self._finalized

    def finalize(
        self,
        exit_code: int,
        *,
        stop_owned_work: Callable[[], None],
        outcome_authority: Callable[[], ContextManager[None]] | None = None,
        select_outcome: Callable[[int], int] | None = None,
        failure_exit_code: int | None = None,
        handle_finalization_failure: Callable[[int, str], int] | None = None,
        persist_finalization_failure: Callable[[], None] | None = None,
        persist_checkpoint: Callable[[], None] | None = None,
        release_locks: Callable[[], None],
        persist_exited: Callable[[], None],
        emit_runner_exit: Callable[[], None],
        record_outcome: Callable[[], None],
    ) -> int:
        """Run every native exit step once and return the first selected code."""
        selected_code = int(exit_code)
        signal_exit_requested = selected_code == 130
        fallback_code = int(selected_code if failure_exit_code is None else failure_exit_code)
        with self._lock:
            if self._finalized:
                return int(self._exit_code if self._exit_code is not None else selected_code)
            # Claim finalization before callbacks run. A re-entrant signal must
            # not duplicate the durable outcome or runner-exit activity event.
            # Cache a fail-closed provisional result until authority, selection,
            # and terminal persistence all finish.
            self._finalized = True
            self._exit_code = fallback_code

        try:
            stop_owned_work()
        except BaseException as exc:
            logger.warning("Failed to finalize native runner owned work: %s", exc)
            if isinstance(exc, (NativeTerminationSignal, KeyboardInterrupt)):
                # A first termination request can arrive while quiescing owned
                # work.  It must become sticky before any mathematical outcome
                # authority is entered, otherwise a cached 0/3 could commit.
                signal_exit_requested = True
                selected_code = 130
        steps: list[tuple[str, Callable[[], None]]] = []
        if persist_checkpoint is not None:
            # Quiesce subprocess writers before taking the final source snapshot,
            # and retain every authority lease through terminal persistence.
            steps.append(("workflow checkpoint", persist_checkpoint))
        steps.extend(
            [
                ("live status", persist_exited),
                ("runner-exit activity", emit_runner_exit),
                ("campaign outcome", record_outcome),
            ]
        )

        def run_steps() -> tuple[BaseException, ...]:
            nonlocal selected_code, signal_exit_requested
            failures: list[BaseException] = []
            for label, finish in steps:
                try:
                    finish()
                # Finalization is the process boundary: even a second termination
                # signal must not skip the durable status/activity/outcome steps.
                except BaseException as exc:
                    logger.warning("Failed to finalize native runner %s: %s", label, exc)
                    if isinstance(exc, (NativeTerminationSignal, KeyboardInterrupt)):
                        signal_exit_requested = True
                        selected_code = 130
                    failures.append(exc)
            return tuple(failures)

        def apply_failure(exc: BaseException, detail: str) -> None:
            nonlocal selected_code, signal_exit_requested
            if isinstance(exc, (NativeTerminationSignal, KeyboardInterrupt)):
                signal_exit_requested = True
            selected_code = 130 if signal_exit_requested else fallback_code
            if handle_finalization_failure is None:
                return
            try:
                selected_code = int(handle_finalization_failure(selected_code, detail))
            except BaseException as failure_exc:
                logger.warning(
                    "Failed to persist native finalization failure state: %s",
                    failure_exc,
                )
                if isinstance(
                    failure_exc,
                    (NativeTerminationSignal, KeyboardInterrupt),
                ):
                    signal_exit_requested = True
                selected_code = 130 if signal_exit_requested else fallback_code
            else:
                # A cleanup hook cannot translate a previously observed user
                # termination into an infrastructure pause.
                if signal_exit_requested:
                    selected_code = 130

        def overwrite_failed_terminal_records() -> None:
            nonlocal selected_code, signal_exit_requested
            if persist_finalization_failure is None:
                return
            try:
                persist_finalization_failure()
            except BaseException as failure_exc:
                logger.warning(
                    "Failed to overwrite terminal records after finalization failure: %s",
                    failure_exc,
                )
                if isinstance(
                    failure_exc,
                    (NativeTerminationSignal, KeyboardInterrupt),
                ):
                    signal_exit_requested = True
                    selected_code = 130

        steps_completed = False
        try:
            # Interrupted exits make no mathematical claim and therefore do
            # not enter the terminal source/graph authority transaction.
            authority = (
                outcome_authority()
                if outcome_authority is not None and not signal_exit_requested
                else contextlib.nullcontext()
            )
            with authority:
                if select_outcome is not None:
                    try:
                        selected_code = int(select_outcome(selected_code))
                    except BaseException as exc:
                        logger.warning("Failed to select native runner final outcome: %s", exc)
                        # The outer failure transaction must update both the
                        # selected code and caller-owned status state before
                        # any durable callback runs.
                        raise
                    else:
                        if signal_exit_requested:
                            selected_code = 130
                step_failures = run_steps()
                steps_completed = True
                if step_failures and failure_exit_code is not None:
                    raise step_failures[0]
        except BaseException as exc:
            logger.warning("Native runner terminal finalization failed: %s", exc)
            apply_failure(exc, f"{type(exc).__name__}: {str(exc)[:240]}")
            if not steps_completed:
                run_steps()
            else:
                overwrite_failed_terminal_records()

        release_error: BaseException | None = None
        try:
            release_locks()
        except BaseException as exc:
            release_error = exc
            logger.warning("Failed to finalize native runner file locks: %s", exc)
        if release_error is not None and failure_exit_code is not None:
            apply_failure(
                release_error,
                "file-lock release failed: "
                f"{type(release_error).__name__}: {str(release_error)[:200]}",
            )
            overwrite_failed_terminal_records()
        with self._lock:
            self._exit_code = selected_code
        return selected_code


def _native_termination_signals() -> tuple[int, ...]:
    """Return graceful process-termination signals available on this platform."""
    signals: list[int] = []
    for name in ("SIGHUP", "SIGTERM"):
        value = getattr(signal, name, None)
        if isinstance(value, int) and value not in signals:
            signals.append(value)
    return tuple(signals)


def install_native_termination_handlers(
    on_termination: Callable[[int], None] | None = None,
) -> dict[int, Any]:
    """Translate SIGHUP/SIGTERM into an exception so Python ``finally`` blocks run.

    Dispatch workers and the foreground native runner deliberately own detached
    subprocess trees.  The default POSIX action for either signal terminates
    Python immediately and bypasses their cleanup.  The first handled signal
    masks both termination signals while shutdown proceeds, then raises a
    dedicated ``BaseException`` that cannot be mistaken for a model/backend
    failure or swallowed by ordinary ``except Exception`` handlers.
    """
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, Any] = {}

    def handle(signum: int, _frame: Any) -> NoReturn:
        for tracked_signal in previous:
            try:
                signal.signal(tracked_signal, signal.SIG_IGN)
            except (OSError, RuntimeError, ValueError):
                logger.debug(
                    "Failed to defer termination signal %s during native cleanup",
                    tracked_signal,
                    exc_info=True,
                )
        if on_termination is not None:
            try:
                on_termination(signum)
            except Exception:
                logger.debug("Native termination callback failed", exc_info=True)
        raise NativeTerminationSignal(signum)

    for signum in _native_termination_signals():
        try:
            previous_handler = signal.getsignal(signum)
            signal.signal(signum, handle)
        except (OSError, RuntimeError, ValueError):
            logger.debug(
                "Failed to install native termination handler for signal %s",
                signum,
                exc_info=True,
            )
            continue
        previous[signum] = previous_handler
    return previous


def restore_native_termination_handlers(handlers: dict[int, Any]) -> None:
    """Restore SIGHUP/SIGTERM handlers replaced for native cleanup."""
    if threading.current_thread() is not threading.main_thread():
        return
    for signum, handler in handlers.items():
        try:
            signal.signal(signum, handler)
        except (OSError, RuntimeError, ValueError):
            logger.debug(
                "Failed to restore native termination handler for signal %s",
                signum,
                exc_info=True,
            )


def defer_repeated_sigint() -> Any:
    """Ignore additional SIGINT delivery while an interrupted runner shuts down."""
    if threading.current_thread() is not threading.main_thread():
        return None
    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (OSError, RuntimeError, ValueError):
        return None
    return previous


def restore_sigint(handler: Any) -> None:
    """Restore the pre-cleanup SIGINT handler after shutdown completes."""
    if handler is None or threading.current_thread() is not threading.main_thread():
        return
    try:
        signal.signal(signal.SIGINT, handler)
    except (OSError, RuntimeError, ValueError):
        logger.debug("Failed to restore SIGINT handler after native cleanup", exc_info=True)


def _finalize_multiprocessing_semaphores() -> None:
    """Release named semaphore handles before the runner bypasses finalization.

    Some optional progress/reporting dependencies create a process-local
    ``multiprocessing`` lock lazily.  ``os._exit`` is intentional for native
    runners because abandoned tool threads can otherwise hang interpreter
    shutdown, but it also skips the lock's registered ``SemLock._cleanup``
    finalizer.  Run only those non-blocking semaphore finalizers here; running
    the complete multiprocessing exit registry could join unrelated children
    and recreate the shutdown hang this module exists to avoid.
    """
    try:
        import multiprocessing.util as multiprocessing_util
    except (ImportError, RuntimeError):
        return

    registry = getattr(multiprocessing_util, "_finalizer_registry", None)
    if not isinstance(registry, dict):
        return
    for finalizer in tuple(registry.values()):
        callback = getattr(finalizer, "_callback", None)
        if (
            getattr(callback, "__module__", "") != "multiprocessing.synchronize"
            or getattr(callback, "__qualname__", "") != "SemLock._cleanup"
        ):
            continue
        try:
            finalizer()
        except Exception:
            logger.debug(
                "Failed to finalize multiprocessing semaphore before native exit",
                exc_info=True,
            )


def _close_agent_provider_clients(agent: Any) -> None:
    """Close the foreground agent's shared provider clients when initialized."""
    if agent is None:
        return

    anthropic_client = getattr(agent, "_anthropic_client", None)
    anthropic_close = getattr(anthropic_client, "close", None)
    if callable(anthropic_close):
        anthropic_close()
        agent._anthropic_client = None

    openai_client = getattr(agent, "client", None)
    if openai_client is None:
        return
    managed_close = getattr(agent, "_close_openai_client", None)
    if callable(managed_close):
        managed_close(openai_client, reason="native_runner_exit", shared=True)
    else:
        fallback_close = getattr(openai_client, "close", None)
        if callable(fallback_close):
            fallback_close()
    agent.client = None


def _close_agent_terminal_resources(agent: Any) -> None:
    """Terminate foreground and background terminal work owned by one agent."""
    if agent is None:
        return
    task_id = str(
        getattr(agent, "_managed_tool_task_id", "") or getattr(agent, "session_id", "") or ""
    ).strip()
    if not task_id:
        return

    from tools.implementations.terminal_tool import cleanup_vm, clear_task_env_overrides
    from tools.utilities.process_registry import process_registry

    failures: list[BaseException] = []
    termination: BaseException | None = None
    for close in (
        lambda: process_registry.kill_task_processes(task_id),
        lambda: cleanup_vm(task_id),
        lambda: clear_task_env_overrides(task_id),
    ):
        try:
            close()
        except (NativeTerminationSignal, KeyboardInterrupt) as exc:
            if termination is None:
                termination = exc
        except Exception as exc:
            failures.append(exc)
    if termination is not None:
        raise termination
    if failures:
        raise RuntimeError(f"{len(failures)} terminal cleanup operation(s) failed") from failures[0]


def shutdown_native_runtime_services(agent: Any = None) -> tuple[str, ...]:
    """Close subprocess, provider, incremental-Lean, and MCP services without masking exit status.

    Every cleanup step is attempted even if an earlier subsystem raises.  The
    native runner is the process owner for these long-lived services, so leaving
    one open can keep an otherwise checkpointed workflow alive indefinitely.
    """
    from leanflow_cli.cli.expert_help import shutdown_active_expert_commands
    from leanflow_cli.lean.lean_incremental import close_incremental_sessions
    from tools.mcp.mcp_tool import shutdown_mcp_servers

    def close_expert_commands() -> None:
        """Fail cleanup truthfully while an advisor owner thread remains live."""
        residual = shutdown_active_expert_commands()
        if residual:
            raise RuntimeError(
                "expert command cleanup failed for PIDs: "
                + ", ".join(str(process_id) for process_id in residual)
            )

    def close_incremental_lean_sessions() -> None:
        """Fail cleanup truthfully when the owned LeanProbe refuses to close."""
        if not _close_incremental_sessions_bounded(close_incremental_sessions):
            raise RuntimeError("incremental Lean session close failed")

    def close_mcp_servers() -> None:
        """Fail cleanup truthfully while any MCP server remains owned."""
        failed = shutdown_mcp_servers()
        if failed:
            raise RuntimeError("MCP server cleanup failed for: " + ", ".join(sorted(failed)))

    steps: tuple[tuple[str, Callable[[], object]], ...] = (
        ("terminal processes", lambda: _close_agent_terminal_resources(agent)),
        ("expert commands", close_expert_commands),
        ("provider clients", lambda: _close_agent_provider_clients(agent)),
        ("incremental Lean sessions", close_incremental_lean_sessions),
        ("MCP servers", close_mcp_servers),
    )
    failures: list[str] = []
    termination: BaseException | None = None
    for label, close in steps:
        try:
            close()
        except (NativeTerminationSignal, KeyboardInterrupt) as exc:
            if termination is None:
                termination = exc
            logger.warning(
                "Termination requested while closing %s during native runner exit: %s",
                label,
                exc,
            )
        except Exception as exc:
            failures.append(label)
            logger.warning("Failed to close %s during native runner exit: %s", label, exc)
    if termination is not None:
        raise termination
    return tuple(failures)


def exit_native_process(exit_code: int) -> NoReturn:
    """Exit after explicit cleanup without waiting on abandoned tool threads.

    The native runner owns a subprocess. Interrupted concurrent tool calls can
    leave ``ThreadPoolExecutor`` workers blocked inside provider libraries even
    after their MCP children are reaped. Normal Python finalization joins those
    workers forever, so flush user-visible output and use the process boundary
    as the final cancellation mechanism.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            logger.debug("Failed to flush native runner stream", exc_info=True)
    _finalize_multiprocessing_semaphores()
    os._exit(int(exit_code))
