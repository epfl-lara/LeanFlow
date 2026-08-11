"""Run blocking foreground work while the process owner performs maintenance."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_ACTIVE_WORKERS_LOCK = threading.Lock()
_ACTIVE_WORKERS: set[threading.Thread] = set()
_ACTIVE_NAMED_WORKERS: dict[str, threading.Thread] = {}


def _track_worker(worker: threading.Thread) -> None:
    """Register one daemon action until its execution boundary is proven closed."""
    with _ACTIVE_WORKERS_LOCK:
        _ACTIVE_WORKERS.add(worker)


def _forget_worker(worker: threading.Thread) -> None:
    """Retire one completed daemon action from the process-owner registry."""
    with _ACTIVE_WORKERS_LOCK:
        _ACTIVE_WORKERS.discard(worker)
        for name, active in tuple(_ACTIVE_NAMED_WORKERS.items()):
            if active is worker:
                _ACTIVE_NAMED_WORKERS.pop(name, None)


def start_parent_maintained_action(
    action: Callable[[], None],
    *,
    name: str,
    settle_s: float = 0.0,
) -> bool:
    """Start one named auxiliary action without blocking the process owner.

    Coalesce repeated submissions while the same named action is live. The
    worker is registered with native finalization, and an auxiliary failure is
    logged instead of escaping into foreground proof control flow.
    """
    worker_name = str(name or "leanflow-parent-maintenance").strip()

    def target() -> None:
        try:
            action()
        except Exception:
            logger.debug("parent-maintained auxiliary action failed", exc_info=True)
        finally:
            _forget_worker(threading.current_thread())

    with _ACTIVE_WORKERS_LOCK:
        current = _ACTIVE_NAMED_WORKERS.get(worker_name)
        if current is not None and current.is_alive():
            return False
        worker = threading.Thread(target=target, name=worker_name, daemon=True)
        _ACTIVE_WORKERS.add(worker)
        _ACTIVE_NAMED_WORKERS[worker_name] = worker
        worker.start()
    # Let trivial reconciliations retain their historical immediate
    # visibility without ever waiting materially on a real portfolio refresh.
    worker.join(timeout=max(0.0, float(settle_s)))
    return True


def quiesce_parent_maintained_actions(
    *,
    cancel: Callable[[], None] | None = None,
    timeout_s: float = 2.0,
) -> tuple[str, ...]:
    """Cancel and boundedly join every still-live parent-maintained action."""
    if cancel is not None:
        cancel()
    with _ACTIVE_WORKERS_LOCK:
        workers = tuple(_ACTIVE_WORKERS)
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    for worker in workers:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if not worker.is_alive():
            _forget_worker(worker)
    return tuple(worker.name for worker in workers if worker.is_alive())


def run_with_parent_maintenance(
    action: Callable[[], T],
    *,
    maintenance: Callable[[], None] | None,
    cancel: Callable[[], None] | None = None,
    interval_s: float = 1.0,
    cancellation_join_timeout_s: float = 2.0,
) -> T:
    """Run ``action`` while the calling thread periodically owns maintenance.

    The blocking action runs in one daemon thread. The caller remains the
    process-owning thread and can therefore reap child processes and refresh
    runtime heartbeats without moving those duties into the worker. When no
    maintenance callback exists, execute synchronously with no thread cost.
    """
    if maintenance is None:
        return action()

    results: list[T] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            results.append(action())
        except BaseException as exc:  # preserve the caller's existing exception semantics
            errors.append(exc)

    worker = threading.Thread(
        target=target,
        name="leanflow-parent-maintained-action",
        daemon=True,
    )
    _track_worker(worker)
    worker.start()
    cadence = max(0.01, float(interval_s))
    next_poll = time.monotonic() + cadence
    try:
        while worker.is_alive():
            worker.join(timeout=min(0.1, cadence))
            if worker.is_alive() and time.monotonic() >= next_poll:
                try:
                    maintenance()
                except Exception:
                    # Maintenance is resumable auxiliary work. Match the managed
                    # conversation supervisor: never fail foreground mathematics.
                    logger.debug("parent maintenance callback failed", exc_info=True)
                finally:
                    next_poll = time.monotonic() + cadence
    except BaseException:
        # SIGHUP/SIGTERM are translated into BaseException so Python cleanup
        # runs. Do not let that escape with a daemon writer still active.
        if cancel is not None:
            try:
                cancel()
            except Exception:
                logger.debug("parent-maintained action cancellation failed", exc_info=True)
        worker.join(timeout=max(0.0, float(cancellation_join_timeout_s)))
        raise
    finally:
        if not worker.is_alive():
            _forget_worker(worker)

    if errors:
        raise errors[0]
    if not results:  # pragma: no cover - defensive against impossible worker loss
        raise RuntimeError("parent-maintained action exited without a result")
    return results[0]
