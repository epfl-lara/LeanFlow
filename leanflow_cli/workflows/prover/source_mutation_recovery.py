"""Restore prover sources without overwriting concurrent or late-written editor versions."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import sync_directory
from leanflow_cli.formalization.publication_io import entry_path, rename_exclusive
from leanflow_cli.workflows.prover.source import SourceConflictError
from leanflow_cli.workflows.prover.source_mutation import (
    Boundary,
    audit_retained,
    conflict,
    directory,
    load,
    observe,
    point,
    save,
)


def restore_unknown(root: Path, record: dict[str, Any], retained: Path, target: Path) -> None:
    """Restore an unknown inode only into an empty name while retaining its identity."""
    if target.exists() or target.is_symlink():
        return
    try:
        os.link(retained, target, follow_symlinks=False)
    except FileExistsError:
        return
    except OSError as error:
        raise SourceConflictError(
            f"Cannot restore displaced source without replacing an editor version; "
            f"retained version: {retained}"
        ) from error
    sync_directory(target.parent)


def observe_captured(
    root: Path, record: dict[str, Any], retained: Path, target: Path
) -> dict[str, Any] | None:
    """Restore unreadable or oversized captured versions before reporting a bounded conflict."""
    try:
        return observe(retained)
    except (OSError, SourceConflictError) as error:
        restore_unknown(root, record, retained, target)
        conflict(root, record, f"captured editor version cannot be observed: {error}")
    return None


def preflight(root: Path, identifiers: list[str], recovered: list[str]) -> None:
    """Validate every retained version and current last write before a batch rollback starts."""
    latest: dict[str, dict[str, Any]] = {}
    for identifier in identifiers:
        record = load(root, identifier)
        captured = observe(directory(root, identifier) / "displaced")
        if captured is not None or record["state"] not in {"prepared", "capture_pending"}:
            audit_retained(root, identifier)
        if identifier not in recovered:
            latest[record["path"]] = record
    for record in latest.values():
        current = observe(entry_path(root, record["path"]))
        if current not in (None, record["before"], record["after"]):
            conflict(root, record, "independent editor destination preserved before batch rollback")


def rollback(root: Path, identifier: str, boundary: Boundary = None) -> None:
    """Recover crash boundaries idempotently, keeping every displaced inode reachable."""
    record = load(root, identifier)
    location = directory(root, identifier)
    target = entry_path(root, record["path"])
    original = location / "displaced"
    was_conflicted = record["state"] == "conflicted"
    for name in record["rollback_slots"]:
        retained = location / name
        actual = observe_captured(root, record, retained, target)
        if actual is not None and actual != record["after"]:
            restore_unknown(root, record, retained, target)
            conflict(root, record, f"unknown editor version retained in {retained}")
    current = observe(target)
    captured = observe_captured(root, record, original, target)
    incoming = observe(location / "incoming")
    # Before capture, only the observed original inode is a permitted no-op.
    if captured is None and current == record["before"]:
        if was_conflicted:
            conflict(root, record, str(record.get("conflict", "unresolved source versions")))
        record["original_captured"] = False
        record["state"] = "rolled_back"
        save(root, record)
        return
    if current is not None and current == record["before"]:
        if captured != record["before"]:
            conflict(root, record, "displaced original changed beside the restored destination")
    elif current is not None:
        # The same bytes in an independently created inode are still an editor's save.
        if current != record["after"] or incoming is not None:
            conflict(root, record, "independent editor destination preserved during rollback")
        if len(record["rollback_slots"]) >= 64:
            conflict(root, record, "rollback capture limit reached; preserve and inspect versions")
        slot = uuid.uuid4().hex + ".rollback"
        record["rollback_slots"].append(slot)
        record["state"] = "rollback_pending"
        save(root, record)
        point(boundary, "rollback_capture_pending")
        try:
            rename_exclusive(target, location / slot)
        except FileNotFoundError:
            conflict(root, record, "installed source disappeared during rollback capture")
        point(boundary, "rollback_captured")
        if observe_captured(root, record, location / slot, target) != record["after"]:
            restore_unknown(root, record, location / slot, target)
            conflict(root, record, "concurrent editor version captured during rollback")
    elif record["before"] is not None and captured is None:
        conflict(root, record, "original source missing without its displaced inode")
    elif (
        record["after"] is not None
        and incoming is None
        and not record["rollback_slots"]
        and record["state"] not in {"restore_pending", "rolled_back"}
    ):
        conflict(root, record, "installed destination disappeared without recovery evidence")

    if record["before"] is not None:
        if captured != record["before"]:
            if captured is not None:
                restore_unknown(root, record, original, target)
            conflict(root, record, "late editor version preserved in displaced original")
        if observe(target) is None:
            record["state"] = "restore_pending"
            save(root, record)
            point(boundary, "restore_pending")
            restore_unknown(root, record, original, target)
            point(boundary, "restored")
        if observe(target) != record["before"]:
            conflict(root, record, "concurrent editor save prevents restoring original source")
    elif observe(target) is not None:
        conflict(root, record, "independent destination appeared after rollback removal")
    if observe(original) != record["before"]:
        conflict(root, record, "late descriptor changed displaced original during restoration")
    for slot in record["rollback_slots"]:
        if observe(location / slot) not in (None, record["after"]):
            conflict(root, record, "late descriptor version retained after rollback capture")
    if record["after"] is not None and observe(location / "candidate") != record["after"]:
        conflict(root, record, "late editor version retained in the installed candidate inode")
    if was_conflicted:
        conflict(root, record, str(record.get("conflict", "unresolved source versions")))
    record["original_captured"] = captured is not None
    record["state"] = "rolled_back"
    save(root, record)
