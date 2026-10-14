"""Dispose checked stopped private incoming slots while preserving every user/history version."""

from __future__ import annotations

import os
import shutil
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
from leanflow_cli.formalization.publication_io import entry_path, publication_lock
from leanflow_cli.workflows.project_setup.cleanup import process_states


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
                incoming = entry_path(root, row["incoming"])
                disposition = row.get("incoming_disposal")
                actual = entry_identity(incoming, check=check)
                if actual is None and disposition is None:
                    continue
                if actual is None and disposition["state"] == "DISPOSED":
                    continue
                if actual is not None:
                    info = incoming.lstat()
                    owned = (
                        journal.get("incoming_cleanup_verified") is True
                        and journal.get("incoming_cleanup_basis") is not None
                        and not incoming.is_symlink()
                        and row.get("incoming_inode") == [info.st_dev, info.st_ino]
                        and actual == row["after"]
                    )
                    if not owned or (disposition and disposition["state"] == "DISPOSED"):
                        issue = (
                            f"Changed or unowned private preparation incoming preserved: {relative}"
                        )
                        if issue not in journal["conflicts"]:
                            journal["conflicts"].append(issue)
                        changed = True
                        results.append({"path": row["incoming"], "state": "PRESERVED"})
                        continue
                if disposition is None:
                    payload = {
                        "path": row["incoming"],
                        "inode": row["incoming_inode"],
                        "expected": row["after"],
                        "state": "DELETE_PENDING",
                    }
                    row["incoming_disposal"] = {
                        **payload,
                        "disposition_id": identity(payload),
                    }
                    save_entry_progress(preparation_directory(root), journal, relative)
                if actual is not None:
                    if check:
                        check()
                    info = incoming.lstat()
                    if (
                        incoming.is_symlink()
                        or row["incoming_inode"] != [info.st_dev, info.st_ino]
                        or entry_identity(incoming, check=check) != row["after"]
                    ):
                        issue = f"Private preparation incoming changed before disposal: {relative}"
                        if issue not in journal["conflicts"]:
                            journal["conflicts"].append(issue)
                        changed = True
                        results.append({"path": row["incoming"], "state": "PRESERVED"})
                        continue
                    if incoming.is_dir():
                        shutil.rmtree(incoming)
                    else:
                        incoming.unlink()
                    sync_directory(incoming.parent)
                payload = {
                    **{
                        key: value
                        for key, value in row["incoming_disposal"].items()
                        if key != "disposition_id"
                    },
                    "state": "DISPOSED",
                }
                row["incoming_disposal"] = {**payload, "disposition_id": identity(payload)}
                save_entry_progress(preparation_directory(root), journal, relative)
                results.append({"path": row["incoming"], "state": "DISPOSED"})
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
