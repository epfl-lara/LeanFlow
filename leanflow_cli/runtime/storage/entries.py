"""Identify explicitly owned cache and runtime directories for safe reclamation."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.runtime.storage.leases import (
    StorageSafetyError,
    _lease_active,
    _process_start,
    confined_path,
    storage_home,
    storage_lock,
)

MARKER = ".leanflow-storage.json"
_KEY = re.compile(r"^[0-9a-f]{64}$")


def read_entry(entry: Path, home: Path | None = None) -> dict[str, Any] | None:
    """Accept only the versioned ownership schema in a confined managed directory."""
    try:
        selected = confined_path(entry, home)
        marker = selected / MARKER
        if marker.is_symlink() or not selected.is_dir():
            return None
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return None
        kind = payload.get("kind")
        root = storage_home(home)
        parts = selected.relative_to(root).parts
        valid = (
            len(parts) == 4
            and parts[:3] in {("cache", "dependencies", "v1"), ("cache", "project-builds", "v1")}
            and (
                kind == ("dependency" if parts[1] == "dependencies" else "project-build")
                and bool(_KEY.fullmatch(parts[-1]))
                or kind == "staging"
                and parts[-1].startswith(".staging-")
            )
            or len(parts) == 2
            and (
                parts[0] == "runtimes"
                and kind == "runtime"
                or parts[0] == "releases"
                and kind == "runtime-release"
            )
        )
        if not valid or payload.get("key") != selected.name:
            return None
        for name in ("created_at", "last_used"):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            if not math.isfinite(value) or value < 0 or value > time.time() + 60:
                return None
        return payload
    except (OSError, ValueError, StorageSafetyError):
        return None


def register_entry(
    entry: Path,
    *,
    kind: str = "dependency",
    key: str | None = None,
    bytes: int | None = None,
    identity: Mapping[str, object] | None = None,
    metadata: Mapping[str, object] | None = None,
    home: Path | None = None,
) -> None:
    """Record deliberate ownership; preserve readiness and optional build identity."""
    root = storage_home(home)
    with storage_lock(root):
        selected = confined_path(entry, root)
        selected.mkdir(parents=True, exist_ok=True)
        marker = selected / MARKER
        if marker.is_symlink():
            raise StorageSafetyError(f"linked ownership marker: {marker}")
        now = time.time()
        previous = read_entry(selected, root)
        payload = dict(metadata or {})
        payload.update(
            schema_version=1,
            kind=kind,
            key=key or selected.name,
            created_at=(previous or {}).get("created_at", now),
            last_used=now,
        )
        if bytes is not None:
            payload["bytes"] = bytes
        if identity is not None:
            payload["identity"] = dict(identity)
        installer = payload.get("installer_pid")
        if isinstance(installer, int) and not isinstance(installer, bool):
            payload["installer_process_start"] = _process_start(installer)
        atomic_json_write(marker, payload)
        if read_entry(selected, root) is None:
            marker.unlink()
            raise StorageSafetyError(f"invalid managed entry schema: {selected}")
        if bytes is None and payload.get("ready", True) is not False:
            payload["bytes"] = managed_entry_size(selected)
            atomic_json_write(marker, payload)


def touch_entry(entry: Path, home: Path | None = None) -> None:
    """Advance last-use metadata atomically without changing sealed dependency bytes."""
    with storage_lock(home):
        selected = confined_path(entry, home)
        payload = read_entry(selected, home)
        if payload is None:
            raise StorageSafetyError(f"invalid managed entry: {selected}")
        payload["last_used"] = time.time()
        atomic_json_write(selected / MARKER, payload)


def recorded_entry_size(entry: Path, payload: dict[str, Any]) -> int:
    """Reuse publication estimates; inspect older marked entries only once."""
    stored = payload.get("bytes")
    if isinstance(stored, int) and not isinstance(stored, bool) and stored >= 0:
        return stored
    size = managed_entry_size(entry)
    if payload.get("ready", True) is not False:
        payload["bytes"] = size
        atomic_json_write(entry / MARKER, payload)
    return size


def managed_entry_size(entry: Path) -> int:
    """Count allocated bytes without following links or double counting hardlinks."""
    total = 0
    seen: set[tuple[int, int]] = set()
    for directory, names, files in os.walk(entry, followlinks=False):
        current = Path(directory)
        names[:] = [name for name in names if not (current / name).is_symlink()]
        for path in (current, *(current / name for name in files)):
            status = path.lstat()
            inode = (status.st_dev, status.st_ino)
            if inode not in seen:
                seen.add(inode)
                total += getattr(status, "st_blocks", (status.st_size + 511) // 512) * 512
    return total


def managed_candidates(home: Path) -> list[Path]:
    """Enumerate only immediate entries under the explicit storage roots."""
    entries: list[Path] = []
    for relative in ("cache/dependencies/v1", "cache/project-builds/v1", "runtimes", "releases"):
        parent = home / relative
        cursor = parent
        linked = False
        while cursor != home:
            linked |= cursor.is_symlink()
            cursor = cursor.parent
        if linked or not parent.is_dir():
            continue
        entries.extend(parent.iterdir())
    return entries


def protected_install_paths(home: Path) -> list[Path] | None:
    """Protect the selected install and current process; uncertainty pins all runtimes."""
    paths = [Path(sys.prefix).resolve(), Path(__file__).resolve()]
    journal = home / "install-switch.json"
    if journal.exists() or journal.is_symlink():
        try:
            if journal.is_symlink():
                return None
            pending = json.loads(journal.read_text(encoding="utf-8"))
            if (
                not isinstance(pending, dict)
                or pending.get("schema_version") != 1
                or pending.get("protect_all") is not False
                or not isinstance(pending.get("paths"), list)
                or not pending["paths"]
            ):
                return None
            for raw in pending["paths"]:
                if not isinstance(raw, str) or not Path(raw).is_absolute():
                    return None
                paths.append(Path(raw).resolve())
        except (OSError, ValueError):
            return None
    marker = home / "install-root"
    if not marker.exists() and not marker.is_symlink():
        return paths
    if marker.is_symlink():
        return None
    try:
        fields = dict(
            line.split("=", 1)
            for line in marker.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        for key in ("repo_root", "venv_dir"):
            raw = fields.get(key)
            if not raw or not Path(raw).is_absolute():
                return None
            paths.append(Path(raw).resolve())
    except (OSError, ValueError):
        return None
    return paths


def unfinished_install_active(payload: Mapping[str, object]) -> bool:
    """Protect an unfinished installer unless its persisted process is confirmed dead."""
    if payload.get("ready", True) is not False:
        return False
    return _lease_active(
        {
            "pid": payload.get("installer_pid"),
            "process_start": payload.get("installer_process_start"),
        }
    )


def remove_owned_directory(entry: Path) -> None:
    """Unseal directory modes and unlink entries without traversing symbolic links."""
    if entry.is_symlink() or not shutil.rmtree.avoids_symlink_attacks:
        raise StorageSafetyError(f"cannot safely delete managed directory: {entry}")
    for _, _, _, directory_fd in os.fwalk(entry, topdown=True, follow_symlinks=False):
        os.fchmod(directory_fd, os.fstat(directory_fd).st_mode | 0o700)
    shutil.rmtree(entry)
