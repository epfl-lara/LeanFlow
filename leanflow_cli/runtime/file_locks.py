"""Cross-agent file reservations for autonomous LeanFlow workflows."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from core.home import leanflow_home
from core.utils import atomic_json_write

PROJECT_STATE_DIRNAME = ".leanflow"
REGISTRY_VERSION = 1
FILE_LOCK_KIND = "file"
NAMESPACE_LOCK_KIND = "namespace"

logger = logging.getLogger(__name__)

try:  # POSIX advisory locking; strict terminal acquisition requires it.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

_LOCK_FILE_MUTEX = threading.RLock()
_LOCK_FILE_LOCAL = threading.local()


class FileLockRegistryError(RuntimeError):
    """Report lock-registry state that cannot safely authorize a strict lease."""


@dataclass
class _HeldRegistryLock:
    """Track one thread's re-entrant sidecar lease."""

    process_id: int
    depth: int
    handle: TextIO
    cross_process: bool


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _leanflow_home() -> Path:
    return leanflow_home()


def _project_lock_root() -> Path | None:
    explicit = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(Path.cwd())
    for base in candidates:
        try:
            resolved = base.resolve()
        except Exception:
            continue
        for candidate in (resolved, *resolved.parents):
            if (candidate / PROJECT_STATE_DIRNAME / "project.yaml").is_file():
                return candidate / PROJECT_STATE_DIRNAME / "workflow-state"
    return None


def _lock_root() -> Path:
    return _project_lock_root() or (_leanflow_home() / "workflow-state")


def _lock_file() -> Path:
    return _lock_root() / "file_locks.json"


def _lock_sidecar() -> Path:
    path = _lock_file()
    return path.with_suffix(path.suffix + ".lock")


def _held_registry_locks() -> dict[str, _HeldRegistryLock]:
    """Return re-entrant registry leases held by the current thread."""
    entries = getattr(_LOCK_FILE_LOCAL, "entries", None)
    if not isinstance(entries, dict):
        entries = {}
        _LOCK_FILE_LOCAL.entries = entries
    return entries


@contextlib.contextmanager
def _file_lock_transaction(*, strict: bool = False) -> Iterator[None]:
    """Serialize one registry transaction across threads and processes.

    Keep the sidecar flock held across the complete read-clean-check-write
    sequence. Re-entrant calls on the same thread reuse the original file
    description so an inner release cannot drop the outer process lock.
    """
    lock_path = _lock_sidecar()
    key = str(lock_path.absolute())
    process_id = os.getpid()
    with _LOCK_FILE_MUTEX:
        entries = _held_registry_locks()
        existing = entries.get(key)
        if existing is not None and existing.process_id == process_id:
            if strict and not existing.cross_process:
                raise FileLockRegistryError(
                    f"strict lock-registry transaction requires POSIX flock at {lock_path}"
                )
            existing.depth += 1
            try:
                yield
            finally:
                existing.depth -= 1
            return
        if existing is not None:
            # A fork can inherit thread-local Python state. Never treat the
            # parent's file description as a re-entrant child lease.
            entries.pop(key, None)
            with contextlib.suppress(OSError):
                existing.handle.close()

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as handle:
            cross_process = False
            if fcntl is None:
                if strict:
                    raise FileLockRegistryError(
                        f"strict lock-registry transaction requires POSIX flock at {lock_path}"
                    )
            else:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    cross_process = True
                except OSError as exc:
                    if strict:
                        raise FileLockRegistryError(
                            f"could not acquire strict lock-registry flock at {lock_path}: {exc}"
                        ) from exc
                    logger.debug(
                        "flock unavailable for %s; registry update is process-local only",
                        lock_path,
                        exc_info=True,
                    )
            entries[key] = _HeldRegistryLock(
                process_id=process_id,
                depth=1,
                handle=handle,
                cross_process=cross_process,
            )
            try:
                yield
            finally:
                entries.pop(key, None)
                if cross_process and fcntl is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _resolve_path(path: str) -> str:
    raw = (path or "").strip()
    if not raw:
        return ""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return str(candidate)


def _empty_payload() -> dict[str, Any]:
    """Return a new empty registry payload."""
    return {"version": REGISTRY_VERSION, "locks": {}}


def _strict_registry_error(path: Path, detail: str) -> FileLockRegistryError:
    """Build a stable fail-closed registry error."""
    return FileLockRegistryError(
        f"invalid lock registry at {path}: {detail}; refusing strict acquisition"
    )


def _validate_strict_payload(payload: dict[str, Any], *, path: Path) -> None:
    """Reject registry shapes a terminal lease cannot interpret safely."""
    version = payload.get("version")
    if type(version) is not int or version != REGISTRY_VERSION:
        raise _strict_registry_error(path, f"unsupported version {version!r}")
    locks = payload.get("locks")
    if not isinstance(locks, dict):
        raise _strict_registry_error(path, "'locks' must be an object")
    for file_path, entry in locks.items():
        if not isinstance(file_path, str) or not file_path or not Path(file_path).is_absolute():
            raise _strict_registry_error(path, f"invalid lock path {file_path!r}")
        if str(Path(file_path).resolve()) != file_path:
            raise _strict_registry_error(path, f"non-canonical lock path {file_path!r}")
        if not isinstance(entry, dict):
            raise _strict_registry_error(path, f"entry for {file_path!r} must be an object")
        owner_id = entry.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise _strict_registry_error(path, f"entry for {file_path!r} has no owner_id")
        kind = entry.get("kind", FILE_LOCK_KIND)
        if not isinstance(kind, str) or kind not in {FILE_LOCK_KIND, NAMESPACE_LOCK_KIND}:
            raise _strict_registry_error(
                path, f"entry for {file_path!r} has unsupported kind {kind!r}"
            )
        process_id = entry.get("process_id")
        if process_id is not None and (type(process_id) is not int or process_id <= 0):
            raise _strict_registry_error(path, f"entry for {file_path!r} has invalid process_id")
        expires_at = entry.get("expires_at")
        if expires_at is not None and not isinstance(expires_at, str):
            raise _strict_registry_error(path, f"entry for {file_path!r} has invalid expires_at")
        if isinstance(expires_at, str) and expires_at.strip():
            try:
                datetime.fromisoformat(expires_at)
            except ValueError as exc:
                raise _strict_registry_error(
                    path, f"entry for {file_path!r} has invalid expires_at"
                ) from exc


def _read_payload(*, strict: bool = False) -> dict[str, Any]:
    """Read the registry, optionally rejecting every unrecognized shape."""
    # Deliberately tolerant read: locks are advisory and TTL-bounded, so a reset
    # on corruption self-heals (a brief double-work window, never lost results).
    # Strict terminal acquisition opts out: unknown state cannot authorize a
    # mathematical outcome and must remain untouched for operator inspection.
    path = _lock_file()
    if not os.path.lexists(path):
        return _empty_payload()
    if strict and path.is_symlink():
        raise _strict_registry_error(path, "registry path must not be a symlink")
    if not path.is_file():
        if strict:
            raise _strict_registry_error(path, "registry path is not a regular file")
        return _empty_payload()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if strict:
            raise _strict_registry_error(path, f"unreadable JSON ({exc})") from exc
        return _empty_payload()
    if not isinstance(payload, dict):
        if strict:
            raise _strict_registry_error(path, f"expected an object, got {type(payload).__name__}")
        return _empty_payload()
    if strict:
        _validate_strict_payload(payload, path=path)
        return payload
    version = payload.get("version")
    if version not in {None, REGISTRY_VERSION}:
        return _empty_payload()
    locks = payload.get("locks")
    if not isinstance(locks, dict):
        payload["locks"] = {}
    payload.setdefault("version", REGISTRY_VERSION)
    return payload


def _write_payload(payload: dict[str, Any]) -> None:
    # Crash-atomic so a crash mid-write never truncates the shared lock registry.
    atomic_json_write(_lock_file(), payload, sort_keys=True)


def _process_seems_alive(process_id: int) -> bool:
    """Return whether a lock-owning process still exists."""
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _locks_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the typed lock mapping from a normalized payload."""
    locks = payload.get("locks")
    return locks if isinstance(locks, dict) else {}


def _cleanup_expired(payload: dict[str, Any]) -> dict[str, Any]:
    now = _utc_now()
    cleaned: dict[str, Any] = {}
    locks = _locks_from_payload(payload)
    for file_path, entry in locks.items():
        if not isinstance(entry, dict):
            continue
        try:
            owner_process_id = int(entry.get("process_id", 0) or 0)
        except (TypeError, ValueError):
            owner_process_id = 0
        if owner_process_id > 0:
            if not _process_seems_alive(owner_process_id):
                continue
            # A process-backed lease remains authoritative for the full
            # lifetime of its owner. TTL is only a fallback for legacy leases
            # that cannot prove process liveness.
            cleaned[str(file_path)] = entry
            continue
        expires_at = str(entry.get("expires_at", "") or "").strip()
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
            except ValueError:
                expiry = now
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry <= now:
                continue
        cleaned[str(file_path)] = entry
    payload["locks"] = cleaned
    return payload


def _entry_kind(entry: dict[str, Any]) -> str:
    """Return the supported kind of an entry, treating legacy entries as files."""
    kind = str(entry.get("kind", FILE_LOCK_KIND) or FILE_LOCK_KIND)
    if kind == NAMESPACE_LOCK_KIND:
        return NAMESPACE_LOCK_KIND
    return FILE_LOCK_KIND


def _path_is_within(path: str, namespace: str) -> bool:
    """Return whether ``path`` is the namespace itself or one of its descendants."""
    return Path(path).is_relative_to(Path(namespace))


def _conflicting_locks(
    locks: dict[str, Any],
    *,
    normalized: str,
    owner_id: str,
    kind: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Return other-owner leases that overlap a requested file or namespace."""
    conflicts: list[tuple[str, dict[str, Any]]] = []
    for locked_path, entry in sorted(locks.items()):
        if not isinstance(entry, dict):
            continue
        current_owner = str(entry.get("owner_id", "") or "").strip()
        if not current_owner or current_owner == owner_id:
            continue
        existing_kind = _entry_kind(entry)
        exact_match = locked_path == normalized
        below_existing_namespace = existing_kind == NAMESPACE_LOCK_KIND and _path_is_within(
            normalized, locked_path
        )
        below_requested_namespace = kind == NAMESPACE_LOCK_KIND and _path_is_within(
            locked_path, normalized
        )
        if exact_match or below_existing_namespace or below_requested_namespace:
            conflicts.append((locked_path, entry))
    return conflicts


def list_file_locks() -> list[dict[str, Any]]:
    """Return active file and namespace leases after stale-entry cleanup."""
    with _file_lock_transaction():
        payload = _cleanup_expired(_read_payload())
        _write_payload(payload)
    locks = _locks_from_payload(payload)
    result: list[dict[str, Any]] = []
    for file_path, entry in sorted(locks.items()):
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        item["path"] = file_path
        item["kind"] = _entry_kind(entry)
        result.append(item)
    return result


def describe_lock(path: str) -> dict[str, Any]:
    """Return the exact active lease at ``path``, if any."""
    normalized = _resolve_path(path)
    if not normalized:
        return {}
    with _file_lock_transaction():
        payload = _cleanup_expired(_read_payload())
        _write_payload(payload)
    locks = _locks_from_payload(payload)
    entry = locks.get(normalized)
    if not isinstance(entry, dict):
        return {}
    item = dict(entry)
    item["path"] = normalized
    item["kind"] = _entry_kind(entry)
    return item


def _acquire_lock(
    path: str,
    *,
    owner_id: str,
    purpose: str,
    ttl_seconds: int,
    force: bool,
    kind: str,
    strict: bool,
) -> dict[str, Any]:
    """Acquire one file or namespace lease in a single registry transaction."""
    normalized = _resolve_path(path)
    if not normalized:
        return {"success": False, "error": "path required"}
    owner = (owner_id or "").strip()
    if not owner:
        return {"success": False, "error": "owner_id required"}
    ttl = max(60, int(ttl_seconds or 1800))
    now = _utc_now()
    expires_at = (now + timedelta(seconds=ttl)).isoformat()

    try:
        with _file_lock_transaction(strict=strict):
            payload = _cleanup_expired(_read_payload(strict=strict))
            locks = payload.setdefault("locks", {})
            current = locks.get(normalized)
            conflicts = _conflicting_locks(
                locks,
                normalized=normalized,
                owner_id=owner,
                kind=kind,
            )
            forceable_conflicts = [
                (conflict_path, conflict)
                for conflict_path, conflict in conflicts
                if kind == FILE_LOCK_KIND
                and conflict_path == normalized
                and _entry_kind(conflict) == FILE_LOCK_KIND
            ]
            if conflicts and (not force or len(forceable_conflicts) != len(conflicts)):
                conflict_path, conflict = conflicts[0]
                current_owner = str(conflict.get("owner_id", "") or "")
                noun = "Namespace" if kind == NAMESPACE_LOCK_KIND else "File"
                return {
                    "success": False,
                    "error": f"{noun} is locked by {current_owner}",
                    "lock": {
                        **conflict,
                        "path": conflict_path,
                        "kind": _entry_kind(conflict),
                    },
                }
            evicted = [conflict_path for conflict_path, _entry in forceable_conflicts]
            if force:
                for conflict_path in evicted:
                    locks.pop(conflict_path, None)
            current_owner = (
                str(current.get("owner_id", "") or "") if isinstance(current, dict) else ""
            )
            current_kind = _entry_kind(current) if isinstance(current, dict) else FILE_LOCK_KIND
            stored_kind = kind
            if current_owner == owner and current_kind == NAMESPACE_LOCK_KIND:
                # An incidental same-owner file reacquire must not silently
                # downgrade a stronger namespace reservation.
                stored_kind = NAMESPACE_LOCK_KIND
            locks[normalized] = {
                "owner_id": owner,
                "process_id": os.getpid(),
                "kind": stored_kind,
                "purpose": purpose.strip(),
                "created_at": str((current or {}).get("created_at", "") or now.isoformat()),
                "updated_at": now.isoformat(),
                "expires_at": expires_at,
            }
            _write_payload(payload)
    except (FileLockRegistryError, OSError) as exc:
        return {
            "success": False,
            "error": str(exc),
            "path": normalized,
            "registry_error": True,
        }
    return {
        "success": True,
        "path": normalized,
        "owner_id": owner,
        "kind": stored_kind,
        "purpose": purpose.strip(),
        "expires_at": expires_at,
        "evicted": evicted if force else [],
    }


def acquire_file_lock(
    path: str,
    *,
    owner_id: str,
    purpose: str = "",
    ttl_seconds: int = 1800,
    force: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Acquire an exclusive file lease for autonomous workflow coordination.

    A foreign ancestor namespace also blocks the acquisition. ``strict=True``
    fails closed if cross-process flock or the persisted registry is unusable.
    """
    return _acquire_lock(
        path,
        owner_id=owner_id,
        purpose=purpose,
        ttl_seconds=ttl_seconds,
        force=force,
        kind=FILE_LOCK_KIND,
        strict=strict,
    )


def acquire_namespace_lock(
    path: str,
    *,
    owner_id: str,
    purpose: str = "",
    ttl_seconds: int = 1800,
    force: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Acquire a namespace lease covering ``path`` and every descendant.

    Foreign descendant locks and foreign ancestor namespace locks block the
    acquisition atomically. Same-owner descendants remain valid and allow a
    terminal authority to strengthen its already-held file reservations.
    """
    return _acquire_lock(
        path,
        owner_id=owner_id,
        purpose=purpose,
        ttl_seconds=ttl_seconds,
        force=force,
        kind=NAMESPACE_LOCK_KIND,
        strict=strict,
    )


def _release_lock(
    path: str,
    *,
    owner_id: str,
    force: bool,
    strict: bool,
    expected_kind: str | None = None,
) -> dict[str, Any]:
    """Release one exact lease, optionally requiring its namespace kind."""
    normalized = _resolve_path(path)
    if not normalized:
        return {"success": False, "error": "path required"}
    owner = (owner_id or "").strip()
    if not owner and not force:
        return {"success": False, "error": "owner_id required"}
    try:
        with _file_lock_transaction(strict=strict):
            payload = _cleanup_expired(_read_payload(strict=strict))
            locks = payload.setdefault("locks", {})
            current = locks.get(normalized)
            if not isinstance(current, dict):
                if strict:
                    return {
                        "success": False,
                        "error": f"Strict lease is missing at {normalized}",
                        "path": normalized,
                        "registry_error": True,
                    }
                return {"success": True, "released": False, "path": normalized}
            current_kind = _entry_kind(current)
            if expected_kind is not None and current_kind != expected_kind:
                return {
                    "success": False,
                    "error": f"Path holds a {current_kind} lock, not a {expected_kind} lock",
                    "path": normalized,
                }
            current_owner = str(current.get("owner_id", "") or "")
            if current_owner and current_owner != owner and not force:
                return {
                    "success": False,
                    "error": f"File is locked by {current_owner}",
                    "path": normalized,
                }
            locks.pop(normalized, None)
            _write_payload(payload)
    except (FileLockRegistryError, OSError) as exc:
        return {
            "success": False,
            "error": str(exc),
            "path": normalized,
            "registry_error": True,
        }
    return {"success": True, "released": True, "path": normalized}


def release_file_lock(
    path: str,
    *,
    owner_id: str,
    force: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Release the exact file or legacy lease at ``path``."""
    return _release_lock(
        path,
        owner_id=owner_id,
        force=force,
        strict=strict,
        expected_kind=FILE_LOCK_KIND,
    )


def release_namespace_lock(
    path: str,
    *,
    owner_id: str,
    force: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Release the exact namespace lease at ``path``."""
    return _release_lock(
        path,
        owner_id=owner_id,
        force=force,
        strict=strict,
        expected_kind=NAMESPACE_LOCK_KIND,
    )


def release_all_file_locks(*, owner_id: str, strict: bool = False) -> dict[str, Any]:
    """Release every file and namespace lease owned by ``owner_id``."""
    owner = (owner_id or "").strip()
    if not owner:
        return {"success": False, "error": "owner_id required"}
    released: list[str] = []
    try:
        with _file_lock_transaction(strict=strict):
            payload = _cleanup_expired(_read_payload(strict=strict))
            locks = payload.setdefault("locks", {})
            for file_path, entry in list(locks.items()):
                if isinstance(entry, dict) and str(entry.get("owner_id", "") or "") == owner:
                    locks.pop(file_path, None)
                    released.append(file_path)
            _write_payload(payload)
    except (FileLockRegistryError, OSError) as exc:
        return {
            "success": False,
            "error": str(exc),
            "registry_error": True,
        }
    return {"success": True, "released": released, "count": len(released)}


def release_stale_file_locks(*, dead_owner_ids: Iterable[str]) -> dict[str, Any]:
    """Release legacy leases whose recorded workflow owners are terminal.

    New leases carry a process id and self-clean in ``_cleanup_expired``.
    This explicit owner reconciliation handles older persisted leases without
    guessing that an unknown owner is dead.
    """
    dead = {str(owner or "").strip() for owner in dead_owner_ids if str(owner or "").strip()}
    released: list[str] = []
    if not dead:
        return {"success": True, "released": released, "count": 0}
    with _file_lock_transaction():
        payload = _cleanup_expired(_read_payload())
        locks = payload.setdefault("locks", {})
        for file_path, entry in list(locks.items()):
            if isinstance(entry, dict) and str(entry.get("owner_id", "") or "") in dead:
                locks.pop(file_path, None)
                released.append(file_path)
        _write_payload(payload)
    return {"success": True, "released": released, "count": len(released)}


def ensure_file_lock(
    path: str,
    *,
    owner_id: str,
    purpose: str = "",
    strict: bool = False,
) -> dict[str, Any]:
    """Acquire or refresh the owner's file lease without a check/acquire race."""
    return acquire_file_lock(
        path,
        owner_id=owner_id,
        purpose=purpose or "active edit",
        strict=strict,
    )
