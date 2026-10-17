"""Roll back incomplete infrastructure writes while preserving concurrent directory/file saves."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.formalization.preparation_journal import (
    TERMINAL,
    PreparationConflict,
    preparation_directory,
    save_journal,
    settle,
)
from leanflow_cli.formalization.preparation_progress import save_entry_progress
from leanflow_cli.formalization.publication_io import entry_path, rename_exclusive


def recover_locked(
    root: Path,
    registry: dict[str, Any],
    journal: dict[str, Any],
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Recover registered setup with exclusive project ownership; never adopt an editor's inode."""
    if journal["state"] in TERMINAL:
        settle(root, registry, journal)
        return journal
    if journal["state"] == "CONFLICTED":
        raise PreparationConflict("Resolve preserved preparation versions before retrying")
    if journal["state"] in {"PREPARING", "PREPARED"}:
        journal["state"] = "ROLLED_BACK"
        save_journal(root, journal, checkpoint)
        settle(root, registry, journal)
        return journal

    def save(relative: str | None = None) -> None:
        if relative is None:
            save_journal(root, journal, checkpoint)
        else:
            save_entry_progress(preparation_directory(root), journal, relative, checkpoint)

    def point(name: str) -> None:
        if checkpoint:
            checkpoint(name)

    def conflict(message: str) -> None:
        if message not in journal["conflicts"]:
            journal["conflicts"].append(message)
        save()

    def restore_unknown(relative: str, slot: Path, destination: Path) -> None:
        conflict(f"User version retained: {relative} ({slot.relative_to(root)})")
        if entry_identity(destination) is None:
            try:
                point("unknown_restore_pending:" + relative)
                rename_exclusive(slot, destination)
                point("unknown_restored:" + relative)
            except FileExistsError:
                conflict(
                    f"Concurrent destination prevents restoring retained user version: {relative}"
                )

    journal["state"] = "ROLLING_BACK"
    save()
    for relative in reversed(journal["order"]):
        row = journal["writes"][relative]
        destination, captured, incoming = (
            entry_path(root, value) for value in (relative, row["captured"], row["incoming"])
        )
        unknown = False
        for slot in row["rollback_slots"]:
            location = entry_path(root, slot["path"])
            actual = entry_identity(location)
            if actual is not None and actual != slot["expected"]:
                restore_unknown(relative, location, destination)
                unknown = True
        if unknown:
            continue
        current, captured_hash = entry_identity(destination), entry_identity(captured)
        installed = entry_identity(incoming) is None and row["stage"] not in {
            "ready",
            "capture_pending",
            "captured",
        }
        if current is not None and current != row["before"]:
            metadata = destination.lstat()
            if not installed or row.get("incoming_inode") != [metadata.st_dev, metadata.st_ino]:
                conflict(f"Independent user destination preserved: {relative}")
                continue
        if current is None and captured_hash is None and row["before"] is None and not installed:
            continue
        if (
            current is None
            and installed
            and row["stage"] not in {"restored", "restore_pending"}
            and not any(
                entry_identity(entry_path(root, slot["path"])) == row["after"]
                for slot in row["rollback_slots"]
            )
        ):
            conflict(f"Installed destination deleted without recovery capture: {relative}")
            continue
        if current not in {None, row["before"], row["after"]}:
            conflict(f"Unknown user destination preserved: {relative}")
            continue
        if current == row["before"] and current is not None:
            if captured_hash not in {None, row["before"]}:
                conflict(f"Retained user edit survives beside unchanged destination: {relative}")
            continue
        if current is not None:
            slot = {
                "path": str(Path(row["captured"]).parent / (uuid.uuid4().hex + ".rollback")),
                "expected": row["after"],
            }
            row["rollback_slots"].append(slot)
            row["stage"] = "rollback_remove_pending"
            save(relative)
            location = entry_path(root, slot["path"])
            point("recovery_move_pending:" + relative)
            try:
                rename_exclusive(destination, location)
            except FileNotFoundError:
                conflict(f"Destination deleted during preparation recovery: {relative}")
                continue
            point("recovery_moved:" + relative)
            if entry_identity(location) != row["after"]:
                restore_unknown(relative, location, destination)
                continue
            row["stage"] = "rollback_removed"
            save(relative)
        captured_hash = entry_identity(captured)
        if captured_hash is not None and captured_hash != row["before"]:
            restore_unknown(relative, captured, destination)
            continue
        if row["before"] is not None:
            if captured_hash is None:
                conflict(f"Original preparation destination is missing: {relative}")
                continue
            row["stage"] = "restore_pending"
            save(relative)
            point("restore_pending:" + relative)
            try:
                rename_exclusive(captured, destination)
            except FileExistsError:
                conflict(f"Concurrent destination prevents preparation restoration: {relative}")
                continue
            point("restored:" + relative)
        elif entry_identity(destination) is not None:
            conflict(f"User created an originally absent preparation destination: {relative}")
            continue
        row["stage"] = "restored"
        save(relative)
    journal["state"] = "CONFLICTED" if journal["conflicts"] else "ROLLED_BACK"
    save()
    settle(root, registry, journal)
    if journal["state"] == "CONFLICTED":
        raise PreparationConflict(
            "Concurrent preparation edits were preserved; explicit resolution required"
        )
    return journal
