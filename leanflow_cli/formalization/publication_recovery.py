"""Roll back journaled writes while retaining and restoring concurrent user entries."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, read_blob, sync_directory
from leanflow_cli.formalization.publication_io import (
    PublicationConflict,
    entry_hash,
    entry_path,
    rename_exclusive,
)
from leanflow_cli.formalization.publication_journal import (
    audit_retained,
    load_journal,
    save_journal,
    settle_terminal,
)


def recover_locked(
    root: Path,
    journal_path: Path,
    registry: dict[str, Any],
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Recover one owned transaction; the caller must hold project publication ownership."""
    journal = load_journal(journal_path)
    directory = journal_path.parent
    if journal["state"] in {"COMMITTED", "ROLLED_BACK"}:
        audit_retained(root, registry)
        settle_terminal(root, registry, journal)
        return journal
    if journal["state"] == "PREPARING":
        # No project destination can move before the complete PREPARED journal.
        journal["state"] = "ROLLED_BACK"
        save_journal(journal_path, journal, checkpoint)
        settle_terminal(root, registry, journal)
        return journal

    def save() -> None:
        save_journal(journal_path, journal, checkpoint)

    def point(name: str) -> None:
        if checkpoint:
            checkpoint(name)

    def conflict(relative: str, detail: str) -> None:
        issue = f"{relative}: {detail}"
        if issue not in journal["conflicts"]:
            journal["conflicts"].append(issue)
            save()

    # Complete all evidence/path checks before moving the first destination.
    for relative, entry in journal["writes"].items():
        entry_path(root, relative)
        entry_path(directory, entry["captured"])
        for key in (entry["before"], entry["after"]):
            if key is not None:
                read_blob(contained(directory, "blobs"), key)
        for slot in entry["rollback_slots"]:
            entry_path(directory, slot["path"])
    journal["state"] = "ROLLING_BACK"
    save()

    def restore_unknown(relative: str, slot: Path, target: Path) -> bool:
        """Put unknown user bytes back only when their original location remains empty."""
        conflict(relative, f"user bytes preserved in {slot.relative_to(directory)}")
        if entry_hash(target) is not None:
            return False
        point(f"conflict_restore_pending:{relative}")
        try:
            rename_exclusive(slot, target)
        except FileExistsError:
            conflict(relative, "a concurrent destination prevents restoring displaced user bytes")
            return False
        point(f"conflict_restored:{relative}")
        return True

    for relative in reversed(journal["order"]):
        entry = journal["writes"][relative]
        target = entry_path(root, relative)
        captured = entry_path(directory, entry["captured"])
        unknown_slot = False
        for slot in entry["rollback_slots"]:
            location = entry_path(directory, slot["path"])
            actual = entry_hash(location)
            if actual is not None and actual != slot["expected"]:
                restore_unknown(relative, location, target)
                unknown_slot = True
        if unknown_slot:
            continue
        current = entry_hash(target)
        captured_hash = entry_hash(captured)
        staged = entry_path(directory, entry["staged"])
        # A matching hash alone never establishes that we installed this inode.
        # A failed exclusive rename can leave an independently created equal file.
        installed = not staged.exists() and entry["stage"] not in {
            "staged",
            "capture_pending",
            "captured",
        }
        if current is not None and current != entry["before"]:
            info = target.lstat()
            if not installed or entry.get("staged_inode") != [info.st_dev, info.st_ino]:
                conflict(relative, "independent user destination preserved")
                continue
        if current is None and captured_hash is None and entry["before"] is None and not installed:
            continue
        if (
            current is None
            and installed
            and not any(
                entry_hash(entry_path(directory, slot["path"])) == entry["after"]
                for slot in entry["rollback_slots"]
            )
            and entry["stage"] not in {"restored", "restore_pending"}
        ):
            conflict(relative, "installed destination was deleted without a recovery capture")
            continue
        if current not in {None, entry["before"], entry["after"]}:
            conflict(relative, "unknown user destination preserved")
            continue
        if current == entry["before"] and current is not None:
            if captured_hash not in {None, entry["before"]}:
                conflict(relative, "displaced user edit retained beside the unchanged destination")
            continue
        if current is not None:
            slot = {"path": f"displaced/{uuid.uuid4().hex}.rollback", "expected": entry["after"]}
            entry["rollback_slots"].append(slot)
            entry["stage"] = "rollback_remove_pending"
            save()
            point(f"recovery_move_pending:{relative}")
            location = entry_path(directory, slot["path"])
            try:
                rename_exclusive(target, location)
            except FileNotFoundError:
                conflict(relative, "destination was deleted during recovery")
                continue
            point(f"recovery_moved:{relative}")
            if entry_hash(location) != entry["after"]:
                restore_unknown(relative, location, target)
                continue
            entry["stage"] = "rollback_removed"
            save()
        captured_hash = entry_hash(captured)
        if captured_hash is not None and captured_hash != entry["before"]:
            restore_unknown(relative, captured, target)
            continue
        if entry["before"] is not None:
            if captured_hash is None:
                conflict(relative, "original destination is missing without its captured inode")
                continue
            entry["stage"] = "restore_pending"
            save()
            point(f"restore_pending:{relative}")
            try:
                rename_exclusive(captured, target)
            except FileExistsError:
                conflict(relative, "concurrent user destination prevents restoring the original")
                continue
            point(f"restored:{relative}")
        elif entry_hash(target) is not None:
            conflict(relative, "user created an originally absent destination during recovery")
            continue
        entry["stage"] = "restored"
        save()
    for created in reversed(journal["created_dirs"]):
        path = entry_path(root, created["path"])
        try:
            info = path.lstat()
            if (
                created.get("inode") == [info.st_dev, info.st_ino]
                and path.is_dir()
                and not path.is_symlink()
            ):
                os.rmdir(path)
                sync_directory(path.parent)
        except OSError:
            # Nonempty or replaced directories belong to the user and stay in place.
            pass
    journal["state"] = "CONFLICTED" if journal["conflicts"] else "ROLLED_BACK"
    save()
    settle_terminal(root, registry, journal)
    if journal["conflicts"]:
        raise PublicationConflict(
            f"Publication conflict: {journal['conflicts'][0]}; inspect {journal_path}"
        )
    return journal
