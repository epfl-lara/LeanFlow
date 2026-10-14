"""Capture stopped compiler slots before deleting only their recorded inode and bytes."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import durable_json, identity, sync_directory
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.formalization.publication_io import rename_exclusive

_CAPTURE = re.compile(r"\.leanflow-install-[a-f0-9]{32}\.disposal-[a-f0-9]{32}")


def _present(path: Path) -> bool:
    """Recognize an unknown alias without following its destination."""
    return path.exists() or path.is_symlink()


def _version(path: Path) -> dict[str, Any] | None:
    """Expose a retained entry's inode and bytes without assuming disposal ownership."""
    if not _present(path):
        return None
    info = path.lstat()
    if path.is_symlink():
        return {"device": info.st_dev, "inode": info.st_ino, "symlink": os.readlink(path)}
    return {"device": info.st_dev, "inode": info.st_ino, "sha256": entry_identity(path)}


def disposal_asset_observation(
    target: Path, journal: dict[str, Any] | None = None, *, raw_loss: bool = False
) -> dict[str, Any]:
    """Scope readable attempts locally; observe unknown adjacent assets only for raw loss."""
    slot = None
    attempt = (journal or {}).get("attempt_id")
    if isinstance(attempt, str) and re.fullmatch(r"[a-f0-9]{32}", attempt):
        candidate = target.parent / (".leanflow-install-" + attempt)
        if (journal or {}).get("slot") == str(candidate):
            slot = candidate
    pattern = (
        ".leanflow-install-*.disposal-*"
        if raw_loss
        else (slot.name + ".disposal-*" if slot is not None else None)
    )
    paths = {
        path
        for path in (target.parent.glob(pattern) if pattern is not None else ())
        if _CAPTURE.fullmatch(path.name)
    }
    record = (journal or {}).get("slot_disposal")
    if isinstance(record, dict) and isinstance(record.get("capture"), str):
        path = Path(record["capture"])
        if path.parent == target.parent and _CAPTURE.fullmatch(path.name):
            paths.add(path)
    return {str(path): _version(path) for path in sorted(paths)}


def _record(journal: dict[str, Any], slot: Path) -> dict[str, Any] | None:
    """Require a self-bound adjacent capture before resuming an interrupted deletion."""
    value = journal.get("slot_disposal")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Compiler disposal intent is unsupported; its versions were preserved.")
    capture = value.get("capture")
    if (
        set(value)
        != {"schema_version", "slot", "capture", "slot_inode", "slot_sha256", "state", "record_id"}
        or value.get("schema_version") != 1
        or value.get("slot") != str(slot)
        or not isinstance(capture, str)
        or Path(capture).parent != slot.parent
        or not _CAPTURE.fullmatch(Path(capture).name)
        or not capture.startswith(str(slot) + ".disposal-")
        or value.get("slot_inode") != journal.get("slot_inode")
        or not isinstance(value.get("slot_sha256"), str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["slot_sha256"])
        or not isinstance(value.get("state"), str)
        or value["state"] not in {"CAPTURE_PENDING", "DELETE_PENDING", "DISPOSED", "PRESERVED"}
        or value.get("record_id")
        != identity({key: item for key, item in value.items() if key != "record_id"})
    ):
        raise ValueError("Compiler disposal intent changed; its versions were preserved.")
    return value


def _save(path: Path, journal: dict[str, Any], record: dict[str, Any], state: str) -> None:
    """Register a capture phase durably before the next filesystem mutation."""
    payload = {key: item for key, item in record.items() if key != "record_id"}
    payload["state"] = state
    journal["slot_disposal"] = {**payload, "record_id": identity(payload)}
    journal["record_id"] = identity(
        {key: item for key, item in journal.items() if key != "record_id"}
    )
    durable_json(path, journal)


def _exact(
    path: Path, record: dict[str, Any], journal: dict[str, Any], check: Callable[[], None]
) -> bool:
    """Require the recorded inode and complete bytes at the private captured entry."""
    if not _present(path) or path.is_symlink() or not path.is_dir():
        return False
    info = path.lstat()
    if (
        [info.st_dev, info.st_ino] != record["slot_inode"]
        or any(child.name != "compiler" for child in path.iterdir())
        or entry_identity(path, check=check) != record["slot_sha256"]
    ):
        return False
    actual = entry_identity(path / "compiler", check=check)
    return actual is None or actual == journal.get("after") or journal["state"] == "SLOT_READY"


def dispose_compiler_slot(
    path: Path,
    journal: dict[str, Any],
    slot: Path,
    *,
    check: Callable[[], None],
    checkpoint: Callable[[str], None] | None = None,
) -> None:
    """Move into a registered no-clobber capture and preserve unknown versions on every retry."""
    record = _record(journal, slot)

    def boundary(state: str) -> None:
        if checkpoint:
            checkpoint("compiler-disposal:" + state)

    def preserve(message: str, capture: Path | None = None) -> None:
        if record is not None:
            _save(path, journal, record, "PRESERVED")
        if capture is not None and _present(capture):
            try:
                rename_exclusive(capture, slot)
            except (FileExistsError, FileNotFoundError):
                pass
        raise ValueError(message)

    if record is None:
        if not _present(slot):
            return
        if slot.is_symlink() or not slot.is_dir():
            preserve("Compiler incoming slot is aliased or unsupported; its version was preserved.")
        info = slot.lstat()
        if journal.get("slot_inode") is None:
            preserve(
                "Compiler slot was created before its inode was journaled; preserve and acknowledge its exact observation."
            )
        if journal["slot_inode"] != [info.st_dev, info.st_ino] or any(
            child.name != "compiler" for child in slot.iterdir()
        ):
            preserve("Compiler incoming slot was replaced or contains an independent version.")
        actual = entry_identity(slot / "compiler", check=check)
        if (
            actual is not None
            and actual != journal.get("after")
            and journal["state"] != "SLOT_READY"
        ):
            preserve("Compiler incoming bytes changed; the version was preserved.")
        record = {
            "schema_version": 1,
            "slot": str(slot),
            "capture": str(slot) + ".disposal-" + uuid.uuid4().hex,
            "slot_inode": journal["slot_inode"],
            "slot_sha256": entry_identity(slot, check=check),
        }
        _save(path, journal, record, "CAPTURE_PENDING")
        record = journal["slot_disposal"]
        boundary("CAPTURE_PENDING")
    capture = Path(record["capture"])
    if record["state"] == "PRESERVED":
        preserve("Compiler disposal versions require exact observed preservation.")
    if record["state"] == "DISPOSED":
        if _present(slot) or _present(capture):
            preserve("Recreated compiler disposal versions were preserved.")
        return
    if _present(slot) and _present(capture):
        preserve("Independent compiler incoming and captured versions were preserved.")
    if _present(slot):
        if record["state"] != "CAPTURE_PENDING" or not _exact(slot, record, journal, check):
            preserve(
                "Compiler incoming changed before disposal capture; its version was preserved."
            )
        check()
        boundary("CAPTURE")
        try:
            rename_exclusive(slot, capture)
        except FileExistsError:
            preserve("Concurrent compiler disposal capture was preserved.")
        except FileNotFoundError:
            preserve("Compiler incoming disappeared during capture; inspect its versions.")
        boundary("CAPTURED")
    if _present(capture):
        if not _exact(capture, record, journal, check):
            preserve("Unknown captured compiler version was preserved.", capture)
        if _present(slot):
            preserve(
                "Independent compiler incoming appeared after capture; both versions were preserved."
            )
        _save(path, journal, record, "DELETE_PENDING")
        record = journal["slot_disposal"]
        boundary("DELETE_PENDING")
        check()
        if not _exact(capture, record, journal, check):
            preserve(
                "Compiler disposal capture changed before deletion; its version was preserved.",
                capture,
            )
        shutil.rmtree(capture)
        sync_directory(capture.parent)
        boundary("DELETED")
    if _present(slot) or _present(capture):
        preserve("Compiler disposal paths changed during deletion; inspect retained versions.")
    _save(path, journal, record, "DISPOSED")
    boundary("DISPOSED")
