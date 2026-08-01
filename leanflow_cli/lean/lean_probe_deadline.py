"""Bound LeanProbe calls independently of its internal REPL timeout."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Mapping
from typing import Any


class LeanProbeDeadlineExceeded(TimeoutError):
    """Report a LeanProbe call that outlived LeanFlow's wall-clock budget."""

    def __init__(
        self,
        timeout_s: float,
        *,
        worker_stopped: bool,
        sessions_terminated: bool,
    ) -> None:
        super().__init__(f"LeanProbe call exceeded its {timeout_s:g}s wall-clock deadline")
        self.timeout_s = timeout_s
        self.worker_stopped = worker_stopped
        self.sessions_terminated = sessions_terminated


def _owned_sessions(probe: Any) -> list[Any]:
    """Return a snapshot of sessions owned by one LeanProbe instance."""
    sessions: list[Any] = []
    for attribute in ("_sessions", "_code_sessions", "_scratch_sessions"):
        collection = getattr(probe, attribute, None)
        if isinstance(collection, Mapping):
            sessions.extend(list(collection.values()))
    return sessions


def _terminate_owned_sessions(probe: Any) -> bool:
    """Terminate owned REPL processes without acquiring LeanProbe's call lock.

    LeanInteract can race two timeout paths through ``kill``. One path may
    reap the process while the other raises while closing an already-closed
    stream. Treat observed process death as authoritative instead of retaining
    the project lease solely because that cleanup exception occurred.
    """
    terminated = True
    for session in _owned_sessions(probe):
        server = getattr(session, "server", None)
        kill = getattr(server, "kill", None)
        if not callable(kill):
            continue
        kill_failed = False
        try:
            kill()
        except Exception:
            kill_failed = True
        if kill_failed and not _server_is_stopped(server):
            terminated = False
    return terminated


def _server_is_stopped(server: Any) -> bool:
    """Return whether a server reports that its owned process has exited."""
    is_alive = getattr(server, "is_alive", None)
    if callable(is_alive):
        try:
            return not bool(is_alive())
        except Exception:
            return False
    process = getattr(server, "_proc", None)
    poll = getattr(process, "poll", None)
    if callable(poll):
        try:
            return poll() is not None
        except Exception:
            return False
    return False


def _native_probe_lock(probe: Any) -> Any | None:
    """Return LeanProbe's reentrant lock, excluding unrelated test doubles."""
    if not probe.__class__.__module__.startswith("lean_probe"):
        return None
    lock = getattr(probe, "_lock", None)
    return lock if callable(getattr(lock, "acquire", None)) else None


def call_lean_probe_with_deadline(
    probe: Any,
    method_name: str,
    *args: Any,
    deadline_s: float,
    shutdown_grace_s: float = 2.0,
    **kwargs: Any,
) -> Any:
    """Call one probe method and cancel owned work at a hard wall-clock deadline.

    LeanProbe's timeout applies to individual REPL commands, so a multi-command
    incremental replay can otherwise exceed the advertised tool budget. A
    poisoned probe lock can also block before any REPL command starts. The
    outer worker acquires the native reentrant lock with the same deadline and
    checks cancellation before entering the public method, preventing a timed-
    out waiter from launching delayed work after its caller has returned.
    """
    budget = max(0.001, float(deadline_s))
    deadline = time.monotonic() + budget
    cancelled = threading.Event()
    outcomes: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        lock = _native_probe_lock(probe)
        acquired = False
        try:
            if lock is not None:
                remaining = max(0.0, deadline - time.monotonic())
                acquired = bool(lock.acquire(timeout=remaining))
                if not acquired or cancelled.is_set():
                    return
            if cancelled.is_set():
                return
            method = getattr(probe, method_name)
            outcomes.put((True, method(*args, **kwargs)))
        except BaseException as exc:
            outcomes.put((False, exc))
        finally:
            if acquired and lock is not None:
                lock.release()

    worker = threading.Thread(
        target=run,
        daemon=True,
        name=f"leanflow-probe-{method_name}",
    )
    worker.start()
    try:
        worker.join(budget)
    except BaseException:
        cancelled.set()
        _terminate_owned_sessions(probe)
        worker.join(max(0.0, float(shutdown_grace_s)))
        raise
    if worker.is_alive():
        cancelled.set()
        sessions_terminated = _terminate_owned_sessions(probe)
        worker.join(max(0.0, float(shutdown_grace_s)))
        raise LeanProbeDeadlineExceeded(
            budget,
            worker_stopped=not worker.is_alive(),
            sessions_terminated=sessions_terminated,
        )

    try:
        succeeded, value = outcomes.get_nowait()
    except queue.Empty as exc:
        raise LeanProbeDeadlineExceeded(
            budget,
            worker_stopped=True,
            sessions_terminated=True,
        ) from exc
    if succeeded:
        return value
    if isinstance(value, BaseException):
        raise value
    raise RuntimeError(str(value))
