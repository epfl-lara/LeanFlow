"""Dispose checked stopped private incoming slots while preserving every user/history version."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import identity, sync_directory
from leanflow_cli.formalization.preparation_fs_inputs import entry_identity
from leanflow_cli.formalization.preparation_journal import (
    PreparationConflict,
    load_journal,
    load_registry,
    precommit_cleanup,
    preparation_directory,
    save_journal,
    save_registry,
)
from leanflow_cli.formalization.preparation_progress import save_entry_progress
from leanflow_cli.formalization.publication_io import entry_path, publication_lock, rename_exclusive
from leanflow_cli.workflows.project_setup.cleanup import process_states


def _present(path: Path) -> bool:
    """Recognize unknown aliases without following or removing their destination."""
    return path.exists() or path.is_symlink()


def _exact_owned(
    path: Path, journal: dict[str, Any], row: dict[str, Any], check: Callable[[], None] | None
) -> bool:
    """Require the stopped owner's recorded inode and complete bytes at the captured entry."""
    if not _present(path) or path.is_symlink():
        return False
    info = path.lstat()
    return (
        journal.get("incoming_cleanup_verified") is True
        and journal.get("incoming_cleanup_basis") is not None
        and row.get("incoming_inode") == [info.st_dev, info.st_ino]
        and entry_identity(path, check=check) == row["after"]
    )


def _dispose_entry(
    root: Path, journal: dict[str, Any], relative: str, check: Callable[[], None] | None
) -> str | None:
    """Capture into a registered private slot before verifying and deleting stopped inputs."""
    row = journal["writes"][relative]
    incoming = entry_path(root, row["incoming"])
    disposition = row.get("incoming_disposal")
    slot = (
        entry_path(root, disposition["slot"]) if disposition and disposition.get("slot") else None
    )

    def preserve(message: str) -> str:
        """Retain uncertainty as an inspectable transaction conflict."""
        if message not in journal["conflicts"]:
            journal["conflicts"].append(message)
        return "PRESERVED"

    if disposition and disposition["state"] == "DISPOSED":
        if _present(incoming) or (slot is not None and _present(slot)):
            return preserve(f"Recreated private preparation disposal input preserved: {relative}")
        return None
    if not _present(incoming) and disposition is None:
        return None
    if _present(incoming) and not _exact_owned(incoming, journal, row, check):
        return preserve(f"Changed or unowned private preparation incoming preserved: {relative}")
    if slot is None:
        payload = {
            "path": row["incoming"],
            "inode": row["incoming_inode"],
            "expected": row["after"],
            "slot": row["incoming"] + ".disposal-" + uuid.uuid4().hex,
            "state": "DELETE_PENDING",
        }
        row["incoming_disposal"] = {**payload, "disposition_id": identity(payload)}
        save_entry_progress(preparation_directory(root), journal, relative)
        slot = entry_path(root, payload["slot"])
    if _present(incoming) and _present(slot):
        return preserve(
            f"Independent incoming and disposal slot versions preserved: {relative} ({slot.relative_to(root)})"
        )
    if _present(incoming):
        if check:
            check()
        try:
            rename_exclusive(incoming, slot)
        except FileExistsError:
            return preserve(
                f"Concurrent private disposal slot preserved: {relative} ({slot.relative_to(root)})"
            )
        except FileNotFoundError:
            if _present(incoming) or _present(slot):
                return preserve(f"Private incoming changed during disposal capture: {relative}")
    if _present(slot):
        if not _exact_owned(slot, journal, row, check):
            # Restore only into an absent original name. Both versions remain
            # observable when an independent save now occupies that name.
            try:
                rename_exclusive(slot, incoming)
            except FileExistsError:
                pass
            return preserve(
                f"Unknown captured private incoming preserved: {relative} ({slot.relative_to(root)})"
            )
        if _present(incoming):
            return preserve(f"Independent incoming appeared after disposal capture: {relative}")
        if check:
            check()
        if not _exact_owned(slot, journal, row, check):
            return preserve(f"Private disposal slot changed before deletion: {relative}")
        if slot.is_dir():
            shutil.rmtree(slot)
        else:
            slot.unlink()
        sync_directory(slot.parent)
    if _present(incoming):
        return preserve(f"Independent incoming appeared during disposal: {relative}")
    payload = {
        **{
            key: value for key, value in row["incoming_disposal"].items() if key != "disposition_id"
        },
        "state": "DISPOSED",
    }
    row["incoming_disposal"] = {**payload, "disposition_id": identity(payload)}
    save_entry_progress(preparation_directory(root), journal, relative)
    return "DISPOSED"


def dispose_preparation_incoming(
    root: Path,
    *,
    cleanup_verified: bool,
    check: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Remove exact owned rolled-back incoming entries after verified descendant shutdown.

    Captured, rollback and resolved versions remain permanent history. Unknown
    inodes or changed bytes become observable conflicts, including incomplete
    deletions interrupted after their durable disposal intent was registered.
    """
    if cleanup_verified is not True:
        raise PreparationConflict("Verify preparation descendant cleanup before disposing inputs")
    root = root.resolve()
    results = []
    with publication_lock(root):
        registry = load_registry(root)
        table = process_states()
        caller = {"pid": os.getpid(), "start": table[os.getpid()][2]}
        for transaction_id in registry["transactions"]:
            if transaction_id in registry.get("journal_losses", {}):
                continue
            journal = load_journal(root, transaction_id)
            if journal["state"] != "ROLLED_BACK":
                continue
            owner = journal.get("owner") or {}
            marker = precommit_cleanup(journal)
            live_owner = owner.get("pid") in table and table[owner["pid"]][2] == owner.get("start")
            current_owner = owner == caller and (
                registry["current"] == transaction_id or marker is not None
            )
            dead_closed_owner = marker is not None and not live_owner
            if current_owner or dead_closed_owner:
                payload = {
                    "kind": (
                        "current_controller_verified" if current_owner else "closed_dead_controller"
                    ),
                    "owner": owner,
                    "marker_id": marker["marker_id"] if marker else None,
                    "observed_start": table[owner["pid"]][2] if owner.get("pid") in table else None,
                }
                journal["incoming_cleanup_verified"] = True
                journal["incoming_cleanup_basis"] = {**payload, "basis_id": identity(payload)}
                save_journal(root, journal)
            changed = False
            for relative, row in journal["writes"].items():
                if check:
                    check()
                outcome = _dispose_entry(root, journal, relative, check)
                if outcome is not None:
                    results.append({"path": row["incoming"], "state": outcome})
                    changed = changed or outcome == "PRESERVED"
            if changed:
                journal["state"] = "CONFLICTED"
                save_journal(root, journal)
                queue = registry.setdefault("conflict_queue", [])
                if transaction_id not in queue:
                    queue.append(transaction_id)
                if registry["active"] is None:
                    registry["active"] = transaction_id
                save_registry(root, registry)
    return results
