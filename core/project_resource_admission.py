"""Coordinate memory-heavy Lean work across one project process tree.

The provider/research worker capacity limits conversations, not the large Lean
subprocesses those conversations can start.  This module supplies the separate
project-scoped admission slot used at the tool and verifier boundaries.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import math
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from core.filesystem import ensure_directory
from core.runtime_modes import dispatch_worker_enabled

try:  # ``flock`` is the cross-process authority on the supported POSIX hosts.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts.
    fcntl = None  # type: ignore[assignment]


_PROCESS_GATES_GUARD = threading.Lock()
_PROCESS_GATES: dict[str, threading.Lock] = {}
_STICKY_GATES: dict[str, _HeldGate] = {}
_THREAD_STATE = threading.local()
_RECLAIM_ENV = "LEANFLOW_PROJECT_LEAN_ADMISSION"
_FOREGROUND_GRACE_ENV = "LEANFLOW_PROJECT_LEAN_FOREGROUND_GRACE_S"
_STICKY_RECHECK_INTERVAL_S = 0.05
_PRIORITY_RECHECK_INTERVAL_S = 0.01
_FOREGROUND_GRACE_DEFAULT_S = 1.0
_FOREGROUND_GRACE_MAX_S = 5.0
MAX_FOREGROUND_HANDOFF_LEASE_S = 120.0
_ADMISSION_OBSERVER: contextvars.ContextVar[Callable[[str, Mapping[str, object]], None] | None] = (
    contextvars.ContextVar("leanflow_project_lean_admission_observer", default=None)
)


class ProjectLeanAdmissionRetained(RuntimeError):
    """Refuse new local Lean work after a resident service failed to close."""

    def __init__(self, project_root: str, reason: str):
        self.project_root = project_root
        self.reason = str(reason or "resident Lean service close failed")
        super().__init__(f"Lean resource admission retained for {project_root}: {self.reason}")


@dataclass
class _AdmissionState:
    """Share mutable retention and handoff requests across nested scopes."""

    retained: bool = False
    reason: str = ""
    handoff_grace_s: float = 0.0
    handoff_reason: str = ""


@dataclass(frozen=True)
class ProjectLeanAdmission:
    """Describe one Lean-heavy admission attempt for workflow observability."""

    project_root: str
    lock_path: str
    waited_s: float
    contended: bool
    nested: bool
    enforced: bool
    _state: _AdmissionState = field(
        default_factory=_AdmissionState,
        repr=False,
        compare=False,
    )

    def retain_until_process_exit(self, reason: str) -> None:
        """Keep the slot after scope exit when resident Lean state did not close."""
        self._state.retained = True
        self._state.reason = str(reason or "resident Lean service close failed")[:300]

    def reserve_foreground_handoff(self, seconds: float, *, reason: str = "") -> float:
        """Request a bounded unlocked priority lease after this scope releases.

        The lease is only an expiring waiter-file deadline. It never retains the
        project process lock or main ``flock`` across the handoff.
        """
        try:
            requested = float(seconds)
        except (TypeError, ValueError):
            requested = 0.0
        if not math.isfinite(requested):
            requested = 0.0
        bounded = max(0.0, min(MAX_FOREGROUND_HANDOFF_LEASE_S, requested))
        if bounded > self._state.handoff_grace_s:
            self._state.handoff_grace_s = bounded
            self._state.handoff_reason = str(reason or "foreground handoff")[:300]
        return self._state.handoff_grace_s

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe view without exposing process-local handles."""
        return {
            "project_root": self.project_root,
            "lock_path": self.lock_path,
            "waited_s": round(self.waited_s, 3),
            "contended": self.contended,
            "nested": self.nested,
            "enforced": self.enforced,
            "retained_until_process_exit": self._state.retained,
            "retention_reason": self._state.reason,
            "foreground_handoff_grace_s": round(self._state.handoff_grace_s, 3),
            "foreground_handoff_reason": self._state.handoff_reason,
        }


@dataclass
class _HeldGate:
    """Retain the process and OS locks until the outer scope exits."""

    depth: int
    gate: threading.Lock
    handle: TextIO | None
    admission: ProjectLeanAdmission


@dataclass
class _ForegroundWaiter:
    """Own one crash-released foreground-priority marker."""

    path: Path
    handle: TextIO


@dataclass
class ProjectForegroundPriorityLease:
    """Own one cancellable, crash-bounded foreground priority marker.

    The marker does not hold the main Lean slot. Dispatch workers only defer
    their next admission until this lease is consumed or expires, so provider
    inference and other non-Lean background work remain concurrent.
    """

    project_root: str
    marker_path: str
    expires_at: float
    reason: str = ""
    _released: bool = field(default=False, repr=False, compare=False)
    _guard: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
        compare=False,
    )

    def release(self) -> bool:
        """Consume this exact marker without touching another runner's lease."""
        with self._guard:
            if self._released:
                return False
            self._released = True
            root = Path(self.project_root)
            marker = Path(self.marker_path)
            try:
                with _priority_state_lock(root):
                    existed = marker.exists()
                    marker.unlink(missing_ok=True)
                return existed
            except OSError:
                # The deadline remains the crash-safe release authority when
                # an observational cleanup cannot remove the marker now.
                return False

    def to_dict(self) -> dict[str, object]:
        """Return bounded lease metadata for workflow observability."""
        return {
            "project_root": self.project_root,
            "marker_path": self.marker_path,
            "expires_at": self.expires_at,
            "reason": self.reason,
            "released": self._released,
        }


@contextlib.contextmanager
def project_lean_admission_observer(
    observer: Callable[[str, Mapping[str, object]], None],
) -> Iterator[None]:
    """Observe actual admission phases within the current execution context."""
    token = _ADMISSION_OBSERVER.set(observer)
    try:
        yield
    finally:
        _ADMISSION_OBSERVER.reset(token)


def _notify_admission_observer(phase: str, details: Mapping[str, object]) -> None:
    """Report an admission phase without letting observability affect authority."""
    observer = _ADMISSION_OBSERVER.get()
    if observer is None:
        return
    try:
        observer(phase, dict(details))
    except Exception:
        # Admission and Lean verification remain authoritative when activity
        # persistence is unavailable or an observer itself is malformed.
        return


def project_lean_service_reclaim_enabled() -> bool:
    """Return whether this worker must release resident Lean services after each call.

    Foreground proving keeps its incremental session warm. Dispatch workers
    reclaim theirs before releasing the shared project slot so background
    research cannot accumulate one resident Lean process per worker.
    """
    configured = str(os.getenv(_RECLAIM_ENV, "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return configured and dispatch_worker_enabled()


def canonical_lean_project_root(project_root: str | os.PathLike[str]) -> Path:
    """Return the nearest actual Lean project root for a file or nested cwd."""
    candidate = Path(project_root).expanduser().resolve()
    if candidate.is_file() or candidate.suffix == ".lean":
        candidate = candidate.parent
    markers = ("lakefile.lean", "lakefile.toml", "lean-toolchain")
    for directory in (candidate, *candidate.parents):
        if any((directory / marker).is_file() for marker in markers):
            return directory
    return candidate


def _lock_path(root: Path) -> Path:
    """Return the durable project-local lock path without polluting Lean sources."""
    return root / ".leanflow" / "resource-gates" / "lean-heavy.lock"


def _priority_state_lock_path(root: Path) -> Path:
    """Return the mutex serializing foreground waiter registration and scans."""
    return root / ".leanflow" / "resource-gates" / "lean-heavy-priority.lock"


def _priority_waiter_root(root: Path) -> Path:
    """Return the directory containing crash-released foreground markers."""
    return root / ".leanflow" / "resource-gates" / "lean-heavy-foreground-waiters"


def _foreground_handoff_grace_s() -> float:
    """Return the bounded parent handoff window after foreground Lean work."""
    raw = str(os.getenv(_FOREGROUND_GRACE_ENV, _FOREGROUND_GRACE_DEFAULT_S) or "")
    try:
        configured = float(raw)
    except (TypeError, ValueError):
        configured = _FOREGROUND_GRACE_DEFAULT_S
    return max(0.0, min(_FOREGROUND_GRACE_MAX_S, configured))


def _foreground_grace_deadline(handle: TextIO) -> float:
    """Return a plausible wall-clock deadline stored in an unlocked marker."""
    try:
        handle.seek(0)
        raw = handle.read(80).strip()
        deadline = float(raw.removeprefix("grace-until="))
    except (OSError, TypeError, ValueError):
        return 0.0
    now = time.time()
    if deadline <= now or deadline > now + MAX_FOREGROUND_HANDOFF_LEASE_S + 1.0:
        return 0.0
    return deadline


def _arm_foreground_handoff(
    waiter: _ForegroundWaiter | None,
    *,
    requested_grace_s: float = 0.0,
) -> bool:
    """Publish a bounded unlocked grace marker before releasing the main slot."""
    if waiter is None or waiter.handle.closed:
        return False
    grace_s = (
        max(0.0, min(MAX_FOREGROUND_HANDOFF_LEASE_S, requested_grace_s))
        if requested_grace_s > 0.0
        else _foreground_handoff_grace_s()
    )
    if grace_s <= 0:
        return False
    try:
        waiter.handle.seek(0)
        waiter.handle.truncate()
        waiter.handle.write(f"grace-until={time.time() + grace_s:.9f}\n")
        waiter.handle.flush()
    except OSError:
        return False
    return True


def _try_exclusive_flock(handle: TextIO) -> bool:
    """Try to own one file lock without waiting, propagating real I/O errors."""
    if fcntl is None:
        return True
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


@contextlib.contextmanager
def _priority_state_lock(root: Path) -> Iterator[None]:
    """Serialize marker creation, stale cleanup, and foreground checks."""
    path = _priority_state_lock_path(root)
    ensure_directory(path.parent)
    handle = path.open("a+", encoding="utf-8")
    acquired = False
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            acquired = True
        yield
    finally:
        if acquired and fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _register_foreground_waiter(root: Path) -> _ForegroundWaiter | None:
    """Publish one locked waiter marker before blocking on project capacity."""
    if fcntl is None:
        return None
    waiter_root = _priority_waiter_root(root)
    ensure_directory(waiter_root)
    # A project used only by the parent may never run a background scan. Keep
    # its bounded handoff markers from accumulating across many Lean calls.
    _foreground_waiter_exists(root)
    marker = waiter_root / (f"waiter-{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}.lock")
    handle: TextIO | None = None
    try:
        with _priority_state_lock(root):
            handle = marker.open("x+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return _ForegroundWaiter(path=marker, handle=handle)
    except Exception:
        if handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        with contextlib.suppress(OSError):
            marker.unlink(missing_ok=True)
        raise


def _clear_foreground_waiter(
    root: Path,
    waiter: _ForegroundWaiter | None,
    *,
    preserve_grace: bool = False,
) -> None:
    """Release one waiter marker, optionally retaining its bounded grace file."""
    if waiter is None:
        return
    try:
        with _priority_state_lock(root):
            if not waiter.handle.closed and fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(waiter.handle.fileno(), fcntl.LOCK_UN)
            if not waiter.handle.closed:
                waiter.handle.close()
            if not preserve_grace:
                with contextlib.suppress(OSError):
                    waiter.path.unlink(missing_ok=True)
    finally:
        if not waiter.handle.closed:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(waiter.handle.fileno(), fcntl.LOCK_UN)
            waiter.handle.close()
        if not preserve_grace:
            with contextlib.suppress(OSError):
                waiter.path.unlink(missing_ok=True)


def reserve_project_foreground_priority_lease(
    project_root: str | os.PathLike[str],
    seconds: float,
    *,
    reason: str = "",
) -> ProjectForegroundPriorityLease | None:
    """Publish a cancellable unlocked priority lease for upcoming foreground Lean.

    This is the pre-admission counterpart to
    :meth:`ProjectLeanAdmission.reserve_foreground_handoff`: callers can arm it
    before a provider turn or another non-Lean phase starts. The exact marker
    remains bounded by the core maximum and is automatically ignored after its
    deadline if the owner crashes before consuming it.
    """
    try:
        requested = float(seconds)
    except (TypeError, ValueError):
        requested = 0.0
    if not math.isfinite(requested):
        requested = 0.0
    bounded = max(0.0, min(MAX_FOREGROUND_HANDOFF_LEASE_S, requested))
    if bounded <= 0.0 or fcntl is None:
        return None

    root = canonical_lean_project_root(project_root)
    waiter = _register_foreground_waiter(root)
    if waiter is None:
        return None
    expires_at = time.time() + bounded
    try:
        if not _arm_foreground_handoff(waiter, requested_grace_s=bounded):
            _clear_foreground_waiter(root, waiter)
            return None
        _clear_foreground_waiter(root, waiter, preserve_grace=True)
    except Exception:
        _clear_foreground_waiter(root, waiter)
        raise
    return ProjectForegroundPriorityLease(
        project_root=str(root),
        marker_path=str(waiter.path),
        expires_at=expires_at,
        reason=str(reason or "upcoming foreground Lean")[:300],
    )


def _foreground_waiter_exists(root: Path) -> bool:
    """Return whether any locked waiter or bounded handoff marker is active.

    Waiter locks are OS-owned. A dead waiter leaves an unlocked marker with no
    valid deadline; bounded handoff files also become inactive when their
    deadline expires. This scan removes both kinds of stale state under the
    registration mutex before admitting background work.
    """
    if fcntl is None:
        return False
    waiter_root = _priority_waiter_root(root)
    if not waiter_root.is_dir():
        return False
    active = False
    with _priority_state_lock(root):
        for marker in sorted(waiter_root.glob("waiter-*.lock")):
            try:
                handle = marker.open("r+", encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError:
                active = True
                continue
            acquired = False
            try:
                acquired = _try_exclusive_flock(handle)
                if not acquired:
                    active = True
                    continue
                if _foreground_grace_deadline(handle) > 0:
                    active = True
                    continue
                marker.unlink(missing_ok=True)
            finally:
                if acquired:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
    return active


def reclaim_process_foreground_waiters(
    project_root: str | os.PathLike[str],
    *,
    process_id: int | None = None,
) -> tuple[str, ...]:
    """Remove unlocked foreground markers owned by one exiting process.

    Finalization calls this only after process-owned Lean work has quiesced. An
    actively locked marker is never unlinked; its path is returned so the
    caller can fail cleanup truthfully. Markers for every other PID remain
    untouched, including valid bounded handoff leases from concurrent runners.
    """
    if fcntl is None:
        return ()
    root = canonical_lean_project_root(project_root)
    waiter_root = _priority_waiter_root(root)
    if not waiter_root.is_dir():
        return ()
    owner_pid = os.getpid() if process_id is None else int(process_id)
    if owner_pid <= 0:
        return ()

    residual: list[str] = []
    with _priority_state_lock(root):
        for marker in sorted(waiter_root.glob(f"waiter-{owner_pid}-*.lock")):
            try:
                handle = marker.open("r+", encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError:
                residual.append(str(marker))
                continue
            acquired = False
            try:
                acquired = _try_exclusive_flock(handle)
                if not acquired:
                    residual.append(str(marker))
                    continue
                marker.unlink(missing_ok=True)
            except OSError:
                residual.append(str(marker))
            finally:
                if acquired:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
    return tuple(residual)


def _acquire_background_file_gate(handle: TextIO, root: Path) -> None:
    """Acquire the main slot only while no foreground waiter is published."""
    if fcntl is None:
        return
    while True:
        if _foreground_waiter_exists(root):
            time.sleep(_PRIORITY_RECHECK_INTERVAL_S)
            continue
        if not _try_exclusive_flock(handle):
            time.sleep(_PRIORITY_RECHECK_INTERVAL_S)
            continue
        try:
            # Close the check/acquire race: a foreground process can publish
            # after the first scan but before this process obtains the slot.
            # It then owns priority, so release without starting any Lean work.
            if _foreground_waiter_exists(root):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                time.sleep(_PRIORITY_RECHECK_INTERVAL_S)
                continue
            return
        except Exception:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            raise


def _process_gate(path: Path) -> threading.Lock:
    """Return the in-process companion lock for one project gate path."""
    key = str(path)
    with _PROCESS_GATES_GUARD:
        gate = _PROCESS_GATES.get(key)
        if gate is None:
            gate = threading.Lock()
            _PROCESS_GATES[key] = gate
        return gate


def _held_gates() -> dict[str, _HeldGate]:
    """Return this thread's re-entrant admission ownership map."""
    held = getattr(_THREAD_STATE, "held_gates", None)
    if held is None:
        held = {}
        _THREAD_STATE.held_gates = held
    return held


def _acquire_process_gate(
    gate: threading.Lock,
    *,
    key: str,
    project_root: str,
) -> None:
    """Acquire one process gate or promptly refuse a sticky retained lease.

    A retained gate deliberately remains locked until process exit. Polling is
    required because another thread can make the gate sticky after an initial
    state check but before this caller reaches ``Lock.acquire``.
    """
    while True:
        with _PROCESS_GATES_GUARD:
            sticky = _STICKY_GATES.get(key)
        if sticky is not None:
            raise ProjectLeanAdmissionRetained(
                project_root,
                sticky.admission._state.reason,
            )
        if gate.acquire(timeout=_STICKY_RECHECK_INTERVAL_S):
            return


@contextlib.contextmanager
def project_lean_heavy_admission(
    project_root: str | os.PathLike[str],
) -> Iterator[ProjectLeanAdmission]:
    """Acquire the project-wide slot for an operation that can start Lean.

    Nested same-thread calls share the original file descriptor.  This lets an
    agent-level tool gate and a verifier-level subprocess gate compose without
    self-deadlocking, while the sidecar ``flock`` blocks other campaign and
    dispatch-worker processes until the actual Lean-heavy operation finishes.
    """
    root = canonical_lean_project_root(project_root)
    path = _lock_path(root)
    key = str(path)
    held = _held_gates()
    existing = held.get(key)
    if existing is not None:
        existing.depth += 1
        nested = ProjectLeanAdmission(
            project_root=str(root),
            lock_path=key,
            waited_s=existing.admission.waited_s,
            contended=existing.admission.contended,
            nested=True,
            enforced=existing.admission.enforced,
            _state=existing.admission._state,
        )
        try:
            yield nested
        finally:
            existing.depth -= 1
        return

    ensure_directory(path.parent)
    gate = _process_gate(path)
    started = time.monotonic()
    background = dispatch_worker_enabled()
    foreground_waiter = None if background else _register_foreground_waiter(root)
    admission_request_id = uuid.uuid4().hex
    admission_role = "background" if background else "foreground"
    observer_details: dict[str, object] = {
        "project_root": str(root),
        "lock_path": key,
        "admission_request_id": admission_request_id,
        "admission_role": admission_role,
    }
    _notify_admission_observer("waiting", observer_details)
    process_gate_acquired = False
    handle: TextIO | None = None
    enforced = False
    retained = False
    preserve_foreground_grace = False
    admission: ProjectLeanAdmission | None = None
    try:
        _acquire_process_gate(gate, key=key, project_root=str(root))
        process_gate_acquired = True
        handle = path.open("a+", encoding="utf-8")
        if fcntl is not None:
            if background:
                _acquire_background_file_gate(handle, root)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            enforced = True
        waited_s = max(0.0, time.monotonic() - started)
        admission = ProjectLeanAdmission(
            project_root=str(root),
            lock_path=key,
            waited_s=waited_s,
            contended=waited_s >= 0.01,
            nested=False,
            enforced=enforced,
        )
        _notify_admission_observer(
            "admitted",
            {**observer_details, **admission.to_dict()},
        )
        held[key] = _HeldGate(depth=1, gate=gate, handle=handle, admission=admission)
        try:
            yield admission
        finally:
            current = held.pop(key, None)
            if current is not None and current.handle is not None:
                retained = current.admission._state.retained
                if retained:
                    # Keep both the process lock and flock live. Same-process
                    # calls fail closed above; other processes wait until this
                    # process exits and releases the OS-owned slot.
                    with _PROCESS_GATES_GUARD:
                        _STICKY_GATES[key] = current
                else:
                    # Arm the unlocked grace marker while both the marker and
                    # main slot are still held. Background contenders then see
                    # a continuous priority handoff instead of winning the
                    # exact-check -> queue-finalization release/acquire race.
                    if not background:
                        preserve_foreground_grace = _arm_foreground_handoff(
                            foreground_waiter,
                            requested_grace_s=current.admission._state.handoff_grace_s,
                        )
                    if fcntl is not None:
                        with contextlib.suppress(OSError):
                            fcntl.flock(current.handle.fileno(), fcntl.LOCK_UN)
                    current.handle.close()
    finally:
        _clear_foreground_waiter(
            root,
            foreground_waiter,
            preserve_grace=preserve_foreground_grace,
        )
        # A flock/open failure occurs before ownership enters ``held``. Close
        # that partial descriptor explicitly rather than leaking it.
        if handle is not None and key not in _STICKY_GATES and not handle.closed:
            handle.close()
        if process_gate_acquired and not retained:
            gate.release()
        if admission is not None:
            _notify_admission_observer(
                "retained" if retained else "released",
                {**observer_details, **admission.to_dict()},
            )


@contextlib.contextmanager
def project_lean_verification_transaction(
    project_root: str | os.PathLike[str],
) -> Iterator[ProjectLeanAdmission]:
    """Retain foreground admission across sequential verification stages.

    Exact declaration elaboration and transitive axiom inspection each enter
    :func:`project_lean_heavy_admission` internally.  Holding this outer scope
    makes those entries re-entrant and prevents a queued background worker
    from acquiring the project slot between the two authoritative stages.
    """
    with project_lean_heavy_admission(project_root) as admission:
        yield admission
