"""Fence cache admission with retained exact refresh pointers and observable interrupted captures."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, digest, identity, sync_directory
from leanflow_cli.formalization.publication_io import rename_exclusive

CacheVersion = tuple[int, int, int, int, str]
BASE = ".leanflow/scoped-import-cache"


def read_cache_file(path: Path) -> tuple[CacheVersion, bytes] | None:
    """Read a regular no-follow entry while binding its open inode and stable contents."""
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(observed.st_mode):
        raise ValueError(f"Local import artifact is not a regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        current = os.fstat(stream.fileno())
        token = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if token(current) != token(observed):
            raise ValueError(f"Local import artifact changed while opening: {path}")
        content = stream.read()
        after = os.fstat(stream.fileno())
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        ):
            raise ValueError(f"Local import artifact changed while reading: {path}")
    return (*token(observed), digest(content)), content


def exclusive_json(path: Path, value: dict[str, Any]) -> None:
    """Flush a new registered record without replacing an independently created version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write((json.dumps(value, sort_keys=True, indent=2) + "\n").encode())
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def _signed(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "record_id": identity(payload)}


def _asset(path: Path) -> Any:
    """Observe unknown retained metadata without following an unexpected alias."""
    if path.is_symlink():
        info = path.lstat()
        return {"inode": [info.st_dev, info.st_ino], "link": os.readlink(path)}
    observed = read_cache_file(path)
    return list(observed[0]) if observed else None


def _superseded(root: Path, key: str, assets: dict[str, Any]) -> bool:
    """Accept only an unchanged observation superseded by a completed fresh refresh."""
    path = contained(root, BASE + "/refresh-superseded/" + key + "-" + identity(assets) + ".json")
    acknowledged = read_cache_file(path)
    if acknowledged is None:
        return False
    value = json.loads(acknowledged[1])
    return (
        isinstance(value, dict)
        and value.get("root") == str(root.resolve())
        and value.get("assets") == assets
        and value.get("record_id")
        == identity({key: item for key, item in value.items() if key != "record_id"})
    )


def _pending(root: Path) -> dict[str, Any]:
    """Keep interrupted pointer actions blocking until their exact assets are superseded."""
    result: dict[str, Any] = {}
    parent = contained(root, BASE + "/refresh-actions")
    known = set()
    for path in sorted(parent.glob("*.json")):
        if len(path.stem) != 32 or any(char not in "0123456789abcdef" for char in path.stem):
            raise ValueError("Scoped cache refresh action name changed")
        known.add(path.stem)
        capture = contained(root, BASE + "/refresh-pending/" + path.name)
        history = contained(root, BASE + "/refresh-history/" + path.name)
        raw = read_cache_file(path)
        if raw is None:
            raise ValueError("Scoped cache refresh action disappeared")
        try:
            action = json.loads(raw[1])
            archived = read_cache_file(history)
            closed = (
                isinstance(action, dict)
                and action.get("root") == str(root.resolve())
                and action.get("record_id")
                == identity({key: item for key, item in action.items() if key != "record_id"})
                and archived is not None
                and list(archived[0]) == action.get("expected_version")
                and json.loads(archived[1]) == action.get("expected_marker")
                and not capture.exists()
                and not capture.is_symlink()
            )
        except ValueError:
            closed = False
        if closed:
            continue
        assets = {"action": list(raw[0]), "capture": _asset(capture), "history": _asset(history)}
        if _superseded(root, path.stem, assets):
            continue
        result[path.stem] = assets
    for capture in sorted(contained(root, BASE + "/refresh-pending").glob("*.json")):
        key = capture.stem
        if key in known:
            continue
        if len(key) != 32 or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("Scoped cache orphan capture name changed")
        history = contained(root, BASE + "/refresh-history/" + capture.name)
        assets = {"action": None, "capture": _asset(capture), "history": _asset(history)}
        if not _superseded(root, key, assets):
            result[key] = assets
    return result


def _snapshot(root: Path) -> tuple[CacheVersion, dict[str, Any]] | None:
    path = contained(root, BASE + "/needs-refresh.json")
    observed = read_cache_file(path)
    if observed is None:
        return None
    value = json.loads(observed[1])
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("root") != str(root.resolve())
        or not isinstance(value.get("generation"), str)
        or not isinstance(value.get("reason"), str)
        or value.get("marker_id")
        != identity({key: item for key, item in value.items() if key != "marker_id"})
    ):
        raise ValueError(f"Scoped cache refresh authority changed: {path}")
    return observed[0], value


def cache_refresh_needed(root: Path) -> dict[str, Any] | None:
    """Expose a current marker or interrupted capture without reconstructing write authority."""
    pending = _pending(root)
    current = _snapshot(root)
    if current is not None:
        return {**current[1], "pending_assets": pending}
    if pending:
        return {
            "kind": "interrupted_refresh_capture",
            "root": str(root.resolve()),
            "reason": "Incomplete scoped cache refresh pointer action",
            "affected_paths": None,
            "pending_assets": pending,
            "marker_id": identity(pending),
        }
    return None


def assert_cache_ready(root: Path) -> None:
    """Refuse admission until fresh compilation clears the exact observed refresh blocker."""
    marker = cache_refresh_needed(root)
    if marker is not None:
        raise ValueError("Scoped import cache needs refresh: " + marker["reason"])


def _capture(root: Path, observed: tuple[CacheVersion, dict[str, Any]]) -> dict[str, Any]:
    """Register an exclusive pointer capture before moving and verify its actual inode and bytes."""
    key = uuid.uuid4().hex
    action = _signed(
        {
            "schema_version": 1,
            "root": str(root.resolve()),
            "generation": key,
            "expected_version": list(observed[0]),
            "expected_marker": observed[1],
        }
    )
    exclusive_json(contained(root, BASE + "/refresh-actions/" + key + ".json"), action)
    source = contained(root, BASE + "/needs-refresh.json")
    capture = contained(root, BASE + "/refresh-pending/" + key + ".json")
    capture.parent.mkdir(parents=True, exist_ok=True)
    rename_exclusive(source, capture)
    try:
        actual = read_cache_file(capture)
        matches = (
            actual is not None and actual[0] == observed[0] and json.loads(actual[1]) == observed[1]
        )
    except ValueError:
        matches = False
    if not matches:
        try:
            rename_exclusive(capture, source)
        except (FileExistsError, FileNotFoundError):
            pass
        raise ValueError("Unknown scoped cache refresh pointer preserved")
    return action


def _archive(root: Path, action: dict[str, Any]) -> None:
    """Retain an exact captured pointer instead of deleting any metadata pathname."""
    key = action["generation"]
    capture = contained(root, BASE + "/refresh-pending/" + key + ".json")
    history = contained(root, BASE + "/refresh-history/" + key + ".json")
    actual = read_cache_file(capture)
    if (
        actual is None
        or list(actual[0]) != action["expected_version"]
        or json.loads(actual[1]) != action["expected_marker"]
    ):
        raise ValueError("Scoped cache refresh capture changed before retention")
    history.parent.mkdir(parents=True, exist_ok=True)
    rename_exclusive(capture, history)
    archived = read_cache_file(history)
    if (
        archived is None
        or list(archived[0]) != action["expected_version"]
        or json.loads(archived[1]) != action["expected_marker"]
    ):
        raise ValueError("Unknown archived scoped cache refresh pointer preserved")


def mark_cache_refresh_needed(
    root: Path, reason: str, *, affected_paths: list[str] | None = None
) -> str:
    """Register a barrier under caller publication ownership and preserve every prior pointer."""
    if not reason.strip():
        raise ValueError("Scoped cache refresh requires a reason")
    previous = cache_refresh_needed(root)
    observed = _snapshot(root)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "root": str(root.resolve()),
        "generation": uuid.uuid4().hex,
        "reason": reason,
        "previous_marker_id": previous["marker_id"] if previous else None,
        "covered_pending": _pending(root),
        "affected_paths": sorted(set(affected_paths)) if affected_paths is not None else None,
    }
    value = {**payload, "marker_id": identity(payload)}
    exclusive_json(contained(root, BASE + "/barriers/" + value["generation"] + ".json"), value)
    action = _capture(root, observed) if observed is not None else None
    exclusive_json(contained(root, BASE + "/needs-refresh.json"), value)
    if action is not None:
        _archive(root, action)
    return value["marker_id"]


def clear_cache_refresh(root: Path, marker_id: str) -> None:
    """Archive only the exact completed marker and supersede unchanged prior pending observations."""
    observed = _snapshot(root)
    if observed is None or observed[1]["marker_id"] != marker_id:
        raise ValueError("Scoped cache refresh marker changed during publication")
    pending = _pending(root)
    if pending != observed[1].get("covered_pending", {}):
        raise ValueError("Scoped cache pending refresh assets changed during publication")
    action = _capture(root, observed)
    _archive(root, action)
    for key, assets in pending.items():
        payload = {
            "schema_version": 1,
            "root": str(root.resolve()),
            "marker_id": marker_id,
            "assets": assets,
        }
        exclusive_json(
            contained(root, BASE + "/refresh-superseded/" + key + "-" + identity(assets) + ".json"),
            _signed(payload),
        )
