"""Small JSON read/write helpers for LeanFlow managed-workflow state.

These wrap ``json`` with the durability contract the workflow-state layer
relies on: writes are crash-atomic (temp file + fsync + rename via
``core.utils.atomic_json_write``), and reads tolerate missing or empty files
(returning ``{}``) but fail LOUD with :class:`WorkflowStateCorruptionError`
when a non-empty state file does not parse — corrupted workflow state must
halt-and-alert, never silently reset to an empty run. Re-exported by
``leanflow_cli.workflows.workflow_state``; many of its functions call these.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write

logger = logging.getLogger(__name__)

try:  # POSIX advisory locking; degrades to in-process-only elsewhere
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

_WRITE_LOCK = threading.Lock()


class WorkflowStateLockTimeout(TimeoutError):
    """Retain existing state when bounded ownership publication cannot obtain its lock."""


@contextlib.contextmanager
def _bounded_json_write_lock(path: Path, timeout_s: float) -> Iterator[None]:
    """Retry only nonblocking lock attempts, releasing the process lock between attempts."""
    if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s < 0:
        raise ValueError("Workflow state lock timeout must be finite and nonnegative")
    deadline = time.monotonic() + timeout_s
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as handle:
        while True:
            acquired = _WRITE_LOCK.acquire(blocking=False)
            if acquired:
                file_locked = False
                try:
                    if fcntl is not None:
                        try:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            file_locked = True
                        except BlockingIOError:
                            acquired = False
                    if acquired:
                        yield
                        return
                finally:
                    try:
                        if file_locked:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        _WRITE_LOCK.release()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkflowStateLockTimeout(
                    f"Workflow state lock remains busy at {lock_path}; existing ownership retained"
                )
            time.sleep(min(0.025, remaining))


@contextlib.contextmanager
def json_write_lock(path: Path, *, timeout_s: float | None = None) -> Iterator[None]:
    """Serialize read-modify-write updates of ``path`` across processes.

    One shared sidecar flock per JSON file: every cooperative writer of a
    multi-key state file (plan-state summary, nudge log, dispatch ledger)
    must update inside this lock or risk clobbering other writers' keys.
    A finite timeout uses nonblocking attempts without holding the process
    lock while waiting for another process. The default keeps legacy waits.
    """
    if timeout_s is not None:
        with _bounded_json_write_lock(path, timeout_s):
            yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _WRITE_LOCK, lock_path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                logger.debug(
                    "flock unavailable for %s; update not cross-process locked",
                    lock_path,
                    exc_info=True,
                )
        yield


def update_json_file(
    path: Path,
    mutate: Callable[[dict[str, Any]], Any],
    *,
    lock_timeout_s: float | None = None,
) -> Any:
    """Transactionally read, mutate (in place), and atomically write ``path``."""
    with json_write_lock(path, timeout_s=lock_timeout_s):
        payload = read_json_file(path)
        outcome = mutate(payload)
        atomic_json_write(path, payload, sort_keys=True)
    return outcome


def update_json_file_if_changed(
    path: Path,
    mutate: Callable[[dict[str, Any]], tuple[Any, bool]],
    *,
    lock_timeout_s: float | None = None,
) -> Any:
    """Transactionally update ``path`` only when ``mutate`` reports a change.

    The callback executes under the same cross-process lock as
    :func:`update_json_file` and returns ``(outcome, changed)``. This avoids a
    second full-size state snapshot merely to compare payloads while retaining
    the existing crash-atomic write contract whenever durable state changes.
    """
    with json_write_lock(path, timeout_s=lock_timeout_s):
        payload = read_json_file(path)
        outcome, changed = mutate(payload)
        if changed:
            atomic_json_write(path, payload, sort_keys=True)
    return outcome


class WorkflowStateCorruptionError(RuntimeError):
    """A workflow-state JSON file exists, is non-empty, and cannot be parsed.

    Raised instead of the historical silent ``{}`` fallback: a truncated or
    garbled state file usually means a crashed non-atomic write or external
    tampering, and losing a long run's state silently is strictly worse than
    stopping. Inspect or remove the named file to continue.
    """


def read_json_file(path: Path) -> dict[str, Any]:
    """Return the JSON object stored at ``path``.

    Missing files, empty files, and OS-level read failures return ``{}``
    (tolerant, as before). A non-empty file that is not valid UTF-8 JSON or
    whose top level is not an object raises WorkflowStateCorruptionError.
    """
    try:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowStateCorruptionError(
            f"Corrupted workflow-state JSON at {path}: not valid UTF-8 ({exc}). "
            "Refusing to silently reset workflow state; inspect or remove the file to continue."
        ) from exc
    except OSError:
        logger.debug("Failed to read workflow-state JSON from %s", path, exc_info=True)
        return {}
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkflowStateCorruptionError(
            f"Corrupted workflow-state JSON at {path}: {exc}. "
            "Refusing to silently reset workflow state; inspect or remove the file to continue."
        ) from exc
    if not isinstance(payload, dict):
        raise WorkflowStateCorruptionError(
            f"Corrupted workflow-state JSON at {path}: expected a JSON object, "
            f"got {type(payload).__name__}. "
            "Refusing to silently reset workflow state; inspect or remove the file to continue."
        )
    return payload


def write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    """Write ``payload`` crash-atomically so a crash mid-write never truncates state."""
    atomic_json_write(path, payload, sort_keys=True)
