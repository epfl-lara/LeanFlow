"""Cross-agent file reservations for autonomous LeanFlow workflows."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from core.utils import atomic_json_write

PROJECT_STATE_DIRNAME = ".leanflow"


_LOCK_FILE_MUTEX = threading.Lock()


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


def _read_payload() -> dict[str, Any]:
    # Deliberately tolerant read: locks are advisory and TTL-bounded, so a reset
    # on corruption self-heals (a brief double-work window, never lost results).
    # Writes below are crash-atomic, so corruption here means external tampering.
    path = _lock_file()
    if not path.is_file():
        return {"version": 1, "locks": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "locks": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "locks": {}}
    locks = payload.get("locks")
    if not isinstance(locks, dict):
        payload["locks"] = {}
    payload.setdefault("version", 1)
    return payload


def _write_payload(payload: dict[str, Any]) -> None:
    # Crash-atomic so a crash mid-write never truncates the shared lock registry.
    atomic_json_write(_lock_file(), payload, sort_keys=True)


def _cleanup_expired(payload: dict[str, Any]) -> dict[str, Any]:
    now = _utc_now()
    cleaned: dict[str, Any] = {}
    locks = payload.get("locks") if isinstance(payload.get("locks"), dict) else {}
    for file_path, entry in locks.items():
        if not isinstance(entry, dict):
            continue
        expires_at = str(entry.get("expires_at", "") or "").strip()
        if expires_at:
            try:
                expiry = datetime.fromisoformat(expires_at)
            except ValueError:
                expiry = now
            if expiry <= now:
                continue
        cleaned[str(file_path)] = entry
    payload["locks"] = cleaned
    return payload


def list_file_locks() -> list[dict[str, Any]]:
    with _LOCK_FILE_MUTEX:
        payload = _cleanup_expired(_read_payload())
        _write_payload(payload)
    locks = payload.get("locks") if isinstance(payload.get("locks"), dict) else {}
    result = []
    for file_path, entry in sorted(locks.items()):
        item = dict(entry)
        item["path"] = file_path
        result.append(item)
    return result


def describe_lock(path: str) -> dict[str, Any]:
    normalized = _resolve_path(path)
    if not normalized:
        return {}
    with _LOCK_FILE_MUTEX:
        payload = _cleanup_expired(_read_payload())
        _write_payload(payload)
    locks = payload.get("locks") if isinstance(payload.get("locks"), dict) else {}
    entry = locks.get(normalized)
    if not isinstance(entry, dict):
        return {}
    item = dict(entry)
    item["path"] = normalized
    return item


def acquire_file_lock(
    path: str,
    *,
    owner_id: str,
    purpose: str = "",
    ttl_seconds: int = 1800,
    force: bool = False,
) -> dict[str, Any]:
    """Acquire an exclusive file lock for autonomous workflow coordination. Reserves the file under `owner_id` with a TTL (default 30m); fails if another owner holds the lock unless `force=True`. Returns success dict with expiry timestamp."""
    normalized = _resolve_path(path)
    if not normalized:
        return {"success": False, "error": "path required"}
    owner = (owner_id or "").strip()
    if not owner:
        return {"success": False, "error": "owner_id required"}
    ttl = max(60, int(ttl_seconds or 1800))
    now = _utc_now()
    expires_at = (now + timedelta(seconds=ttl)).isoformat()

    with _LOCK_FILE_MUTEX:
        payload = _cleanup_expired(_read_payload())
        locks = payload.setdefault("locks", {})
        current = locks.get(normalized)
        if isinstance(current, dict):
            current_owner = str(current.get("owner_id", "") or "")
            if current_owner and current_owner != owner and not force:
                return {
                    "success": False,
                    "error": f"File is locked by {current_owner}",
                    "lock": {"path": normalized, **current},
                }
        locks[normalized] = {
            "owner_id": owner,
            "purpose": purpose.strip(),
            "created_at": str((current or {}).get("created_at", "") or now.isoformat()),
            "updated_at": now.isoformat(),
            "expires_at": expires_at,
        }
        _write_payload(payload)
    return {
        "success": True,
        "path": normalized,
        "owner_id": owner,
        "purpose": purpose.strip(),
        "expires_at": expires_at,
    }


def release_file_lock(path: str, *, owner_id: str, force: bool = False) -> dict[str, Any]:
    normalized = _resolve_path(path)
    if not normalized:
        return {"success": False, "error": "path required"}
    owner = (owner_id or "").strip()
    if not owner and not force:
        return {"success": False, "error": "owner_id required"}
    with _LOCK_FILE_MUTEX:
        payload = _cleanup_expired(_read_payload())
        locks = payload.setdefault("locks", {})
        current = locks.get(normalized)
        if not isinstance(current, dict):
            return {"success": True, "released": False, "path": normalized}
        current_owner = str(current.get("owner_id", "") or "")
        if current_owner and current_owner != owner and not force:
            return {
                "success": False,
                "error": f"File is locked by {current_owner}",
                "path": normalized,
            }
        locks.pop(normalized, None)
        _write_payload(payload)
    return {"success": True, "released": True, "path": normalized}


def release_all_file_locks(*, owner_id: str) -> dict[str, Any]:
    owner = (owner_id or "").strip()
    if not owner:
        return {"success": False, "error": "owner_id required"}
    released: list[str] = []
    with _LOCK_FILE_MUTEX:
        payload = _cleanup_expired(_read_payload())
        locks = payload.setdefault("locks", {})
        for file_path, entry in list(locks.items()):
            if isinstance(entry, dict) and str(entry.get("owner_id", "") or "") == owner:
                locks.pop(file_path, None)
                released.append(file_path)
        _write_payload(payload)
    return {"success": True, "released": released, "count": len(released)}


def ensure_file_lock(path: str, *, owner_id: str, purpose: str = "") -> dict[str, Any]:
    current = describe_lock(path)
    if current and str(current.get("owner_id", "") or "") not in {"", owner_id}:
        return {
            "success": False,
            "error": f"File is locked by {current.get('owner_id', '')}",
            "lock": current,
        }
    return acquire_file_lock(path, owner_id=owner_id, purpose=purpose or "active edit")
