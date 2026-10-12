"""Recover dependency bytes from explicitly registered abandoned agent views."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from core.utils import atomic_json_write
from leanflow_cli.runtime.storage.entries import managed_entry_size, remove_owned_directory
from leanflow_cli.runtime.storage.leases import (
    StorageSafetyError,
    _lease_active,
    _process_start,
    _state_directory,
    storage_home,
    storage_lock,
)
from leanflow_cli.runtime.storage.process_views import ProcessViews, process_views

_OWNER = ".leanflow-cache-owner"
_TOKEN = re.compile(r"^[0-9a-f]{32}$")


class WorkspaceStatus(TypedDict):
    """Report dependency bytes and reclamation restricted to registered views."""

    bytes: int
    pinned_bytes: int
    removed: list[str]
    preserved: list[str]


def _safe_workspace(path: Path) -> bool:
    """Require a real private view and real cache parents before removing any bytes."""
    selected = path.absolute()
    return not any(part.is_symlink() for part in (selected, *selected.parents)) and not any(
        (selected / part).is_symlink() for part in (".lake", ".lake/packages", _OWNER)
    )


def _registry(home: Path) -> Path:
    """Create the trusted workspace registry without following external links."""
    path = _state_directory(home) / "workspaces"
    if path.is_symlink():
        raise StorageSafetyError(f"linked workspace registry: {path}")
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _record(path: Path, home: Path) -> Path:
    """Name a workspace record by its canonical registration path."""
    digest = hashlib.sha256(str(path).encode()).hexdigest()
    return _registry(home) / f"{digest}.json"


def _read(path: Path) -> dict[str, object] | None:
    """Read one regular trusted record, retaining invalid records conservatively."""
    if path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _owns_workspace(owner: dict[str, object] | None, payload: dict[str, object]) -> bool:
    """Require the exact versioned random owner marker before any reclamation."""
    token = payload.get("owner_token")
    return bool(
        isinstance(token, str)
        and _TOKEN.fullmatch(token)
        and owner is not None
        and owner.get("schema_version") == 1
        and owner.get("owner_token") == token
    )


def register_workspace(snapshot: Path, *, dependency_bytes: int | None = None) -> None:
    """Pin this private dependency view to the current process, without retaining proofs."""
    home = storage_home()
    path = snapshot.expanduser().absolute()
    if not _safe_workspace(path) or not path.is_dir():
        raise StorageSafetyError(f"linked or missing private workspace: {path}")
    start = _process_start(os.getpid())
    if start is None:
        raise StorageSafetyError("cannot verify workspace owner process identity")
    with storage_lock(home):
        record = _record(path, home)
        existing = _read(record) if record.exists() else None
        if existing is not None and existing.get("pid") != os.getpid():
            if _lease_active(existing):
                raise StorageSafetyError(f"private workspace is owned by another process: {path}")
        token = uuid.uuid4().hex
        size = dependency_bytes
        if size is None:
            size = managed_entry_size(path / ".lake/packages")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("workspace dependency bytes must be nonnegative")
        atomic_json_write(path / _OWNER, {"schema_version": 1, "owner_token": token})
        atomic_json_write(
            record,
            {
                "schema_version": 1,
                "path": str(path),
                "owner_token": token,
                "pid": os.getpid(),
                "process_start": start,
                "bytes": size,
            },
        )


def forget_workspace(snapshot: Path) -> None:
    """Drop only the current process's registry record after dependency release."""
    home = storage_home()
    path = snapshot.expanduser().absolute()
    with storage_lock(home):
        record = _record(path, home)
        payload = _read(record)
        owner = _read(path / _OWNER)
        if (
            payload is None
            or payload.get("pid") != os.getpid()
            or payload.get("process_start") != _process_start(os.getpid())
            or not _owns_workspace(owner, payload)
        ):
            return
        record.unlink()
        (path / _OWNER).unlink()


def workspace_status(
    home: Path, *, clean: bool = False, inspect: Callable[[], ProcessViews] | None = None
) -> WorkspaceStatus:
    """Reclaim dead registered views' dependency copies while retaining every other file."""
    registry = home / "storage/workspaces"
    result: WorkspaceStatus = {"bytes": 0, "pinned_bytes": 0, "removed": [], "preserved": []}
    if not registry.exists():
        return result
    if registry.is_symlink():
        result["preserved"] = [str(registry)]
        return result
    removed: list[str] = []
    preserved: list[str] = []
    owners: dict[tuple[object, object], bool] = {}
    live_views: ProcessViews | None = None
    total = pinned = 0
    for record in registry.iterdir():
        payload = _read(record)
        raw = (payload or {}).get("path")
        if (
            payload is None
            or payload.get("schema_version") != 1
            or not isinstance(raw, str)
            or not Path(raw).is_absolute()
        ):
            preserved.append(str(record))
            continue
        path = Path(raw)
        if record.name != _record(path, home).name or not _safe_workspace(path):
            preserved.append(str(path))
            continue
        if not path.exists():
            if clean:
                record.unlink()
            continue
        owner = _read(path / _OWNER)
        if not _owns_workspace(owner, payload):
            preserved.append(str(path))
            continue
        packages = path / ".lake/packages"
        size = payload.get("bytes", 0) if packages.is_dir() else 0
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            preserved.append(str(path))
            continue
        total += size
        owner_key = (payload.get("pid"), payload.get("process_start"))
        if not all(isinstance(value, (int, str, type(None))) for value in owner_key):
            active = True
        else:
            if owner_key not in owners:
                owners[owner_key] = _lease_active(payload)
            active = owners[owner_key]
        if active:
            pinned += size
            continue
        if live_views is None:
            live_views = (inspect or process_views)()
        if live_views.uses(path):
            pinned += size
            preserved.append(str(path))
            continue
        if not clean:
            continue
        try:
            # This marker makes a interrupted prune resumable; proof files and
            # the project's own build directory are deliberately outside it.
            atomic_json_write(
                path / ".leanflow-dependencies-released", {"restore_pinned_dependencies": True}
            )
            if packages.is_dir():
                remove_owned_directory(packages)
            record.unlink()
            (path / _OWNER).unlink()
        except (OSError, StorageSafetyError):
            preserved.append(str(path))
            continue
        total -= size
        removed.append(str(packages))
    return {"bytes": total, "pinned_bytes": pinned, "removed": removed, "preserved": preserved}
