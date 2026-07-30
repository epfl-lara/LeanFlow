"""Bound live research actors across threads and worker processes.

Research workflows can host foreground proving, process-isolated research
workers, and short-lived planner delegates at the same time.  This module
provides one shared actor-lifetime gate: a dispatch worker acquires before
building its agent, and a planner delegate acquires before constructing its
conversation. Nested provider and auxiliary calls retain the actor's lease;
foreground prover/control-plane calls do not consume background capacity.
File locks coordinate processes, a local semaphore coordinates threads, and
OS cleanup releases a slot automatically if its process dies.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from core.filesystem import ensure_directory
from core.home import leanflow_home

BACKGROUND_PROVIDER_CAPACITY_ENV = "LEANFLOW_BACKGROUND_PROVIDER_CAPACITY"
BACKGROUND_PROVIDER_NAMESPACE_ENV = "LEANFLOW_BACKGROUND_PROVIDER_NAMESPACE"

_RESEARCH_MODE_ENV = "LEANFLOW_RESEARCH_MODE"
_RESEARCH_WORKERS_ENV = "LEANFLOW_RESEARCH_WORKERS"
_DISPATCH_WORKER_ENV = "LEANFLOW_DISPATCH_WORKER"
_POLL_INTERVAL_S = 0.1

_SEMAPHORE_LOCK = threading.Lock()
_SEMAPHORES: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_CURRENT_LEASE: ContextVar[BackgroundProviderLease | None] = ContextVar(
    "leanflow_background_capacity_lease",
    default=None,
)


class BackgroundCapacityUnavailable(RuntimeError):
    """Raised when a bounded actor acquisition cannot obtain a slot in time."""


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def background_provider_gate_configured() -> bool:
    """Return whether this process belongs to an actor-capacity-managed run."""
    return (
        BACKGROUND_PROVIDER_CAPACITY_ENV in os.environ
        or _truthy_env(_RESEARCH_MODE_ENV)
        or _truthy_env(_DISPATCH_WORKER_ENV)
    )


def background_provider_capacity(default: int = 2) -> int:
    """Return the live background-actor cap, with one sequential route slot.

    ``--no-parallel`` records zero background workers, but its synchronous
    planner lanes still need one sequential actor slot. Therefore the actor
    gate always has at least one slot when it is active.
    """
    raw = os.getenv(BACKGROUND_PROVIDER_CAPACITY_ENV)
    if raw is None:
        raw = os.getenv(_RESEARCH_WORKERS_ENV, str(default))
    try:
        return max(1, int(str(raw or default).strip()))
    except ValueError:
        return max(1, int(default))


def background_actor_context_active() -> bool:
    """Return whether the current execution context already owns an actor slot."""
    lease = _CURRENT_LEASE.get()
    return isinstance(lease, BackgroundProviderLease) and not lease._released


def _capacity_namespace() -> str:
    """Return a stable lock namespace shared by one workflow and its children."""
    explicit = str(os.getenv(BACKGROUND_PROVIDER_NAMESPACE_ENV, "") or "").strip()
    if explicit:
        source = explicit
    else:
        source = "\0".join(
            (
                str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd()),
                str(os.getenv("LEANFLOW_WORKFLOW_RUN_ID", "") or "project-run"),
            )
        )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def _slot_root(namespace: str) -> Path:
    root = leanflow_home() / "runtime" / "provider-capacity" / namespace
    return ensure_directory(root)


def _local_semaphore(namespace: str, capacity: int) -> threading.BoundedSemaphore:
    key = (namespace, capacity)
    with _SEMAPHORE_LOCK:
        semaphore = _SEMAPHORES.get(key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(capacity)
            _SEMAPHORES[key] = semaphore
        return semaphore


@dataclass
class BackgroundProviderLease:
    """Own one cross-process background-actor slot until explicitly released."""

    slot: int
    _file: IO[bytes]
    _semaphore: threading.BoundedSemaphore
    _released: bool = False
    _references: int = 1
    _reference_lock: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._reference_lock = threading.Lock()

    def retain(self) -> BackgroundProviderLease:
        """Borrow this context's actor slot for a nested provider helper."""
        with self._reference_lock:
            if self._released:
                raise RuntimeError("cannot retain a released background provider lease")
            self._references += 1
        return self

    def release(self) -> None:
        """Release one reference, closing the actor slot after the final owner."""
        with self._reference_lock:
            if self._released:
                return
            self._references -= 1
            if self._references > 0:
                return
            self._released = True
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._semaphore.release()
            if _CURRENT_LEASE.get() is self:
                _CURRENT_LEASE.set(None)


def acquire_background_provider_lease(
    *,
    enabled: bool = True,
    cancelled: Callable[[], bool] | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
    timeout_s: float | None = None,
) -> BackgroundProviderLease | None:
    """Wait cooperatively for one background actor slot.

    Return ``None`` when the process is not capacity-managed. A nested helper
    in the same copied context retains the existing lease. A cancellation
    callback turns a queued actor into ``InterruptedError`` so shutdown never
    waits behind another research conversation.
    """
    if not enabled or not background_provider_gate_configured():
        return None
    if cancelled is not None and cancelled():
        raise InterruptedError("Background actor cancelled while waiting for capacity")
    existing = _CURRENT_LEASE.get()
    if isinstance(existing, BackgroundProviderLease) and not existing._released:
        return existing.retain()
    if existing is not None:
        _CURRENT_LEASE.set(None)
    capacity = background_provider_capacity()
    namespace = _capacity_namespace()
    semaphore = _local_semaphore(namespace, capacity)
    interval = max(0.01, float(poll_interval_s))
    deadline = None if timeout_s is None else time.monotonic() + max(0.0, float(timeout_s))

    def remaining_wait() -> float:
        if deadline is None:
            return interval
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackgroundCapacityUnavailable(
                f"background actor capacity busy ({capacity}/{capacity})"
            )
        return min(interval, remaining)

    while True:
        if cancelled is not None and cancelled():
            raise InterruptedError("Background actor cancelled while waiting for capacity")
        if semaphore.acquire(timeout=remaining_wait()):
            break

    try:
        root = _slot_root(namespace)
        while True:
            if cancelled is not None and cancelled():
                raise InterruptedError("Background actor cancelled while waiting for capacity")
            remaining_wait()
            for slot in range(capacity):
                slot_file = (root / f"slot-{slot}.lock").open("a+b")
                try:
                    fcntl.flock(slot_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    slot_file.close()
                    continue
                lease = BackgroundProviderLease(
                    slot=slot,
                    _file=slot_file,
                    _semaphore=semaphore,
                )
                _CURRENT_LEASE.set(lease)
                return lease
            time.sleep(remaining_wait())
    except BaseException:
        semaphore.release()
        raise


@contextmanager
def background_provider_lease(
    *,
    enabled: bool = True,
    cancelled: Callable[[], bool] | None = None,
    timeout_s: float | None = None,
) -> Iterator[BackgroundProviderLease | None]:
    """Hold or retain one actor slot for a conversation or nested request."""
    lease = acquire_background_provider_lease(
        enabled=enabled,
        cancelled=cancelled,
        timeout_s=timeout_s,
    )
    try:
        yield lease
    finally:
        if lease is not None:
            lease.release()


# Actor-lifetime leases and request-lifetime leases deliberately use the same
# slots.  Provider helpers invoked inside an actor inherit the ContextVar and
# retain its lease instead of deadlocking on a second acquisition.
background_actor_lease = background_provider_lease
