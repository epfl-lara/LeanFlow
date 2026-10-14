"""Publish only a checked new compiler through an owned adjacent no-clobber slot."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from leanflow_cli.formalization.evidence import durable_json, identity, sync_directory
from leanflow_cli.formalization.preparation_fs_inputs import copy_entry, entry_identity
from leanflow_cli.formalization.publication_io import rename_exclusive
from leanflow_cli.workflows.project_setup.cleanup import process_states
from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy
from leanflow_cli.workflows.project_setup.process import SetupFailure


def _same_device(source: Path, parent: Path) -> bool:
    """Select an exclusive move only when the native publication parent shares the filesystem."""
    return os.stat(source).st_dev == os.stat(parent).st_dev


def publish_compiler(
    downloaded: Path,
    target: Path,
    *,
    toolchain: str,
    policy: SetupExecutionPolicy,
    cancel: threading.Event | None,
    checkpoint: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Stage one absent native compiler, retaining uncertain input/target versions on any failure.

    The installer has already exited with verified descendant cleanup. Register
    the adjacent slot before copying or moving it; a cross-filesystem copy uses
    distinct inodes and complete byte/mode verification. Never remove a native
    destination, including cancellation after its successful exclusive rename.
    """
    if not policy.usage.cleanup_verified:
        raise SetupFailure(
            "setup_cleanup_unverified",
            "Compiler acquisition cleanup has not been verified.",
            "Retain the private compiler and inspect its owned descendants before publication.",
        )
    if target.parent.is_symlink() or target.exists() or target.is_symlink():
        raise SetupFailure(
            "setup_toolchain_destination_changed",
            "The compiler publication destination already exists or is aliased.",
            "Inspect the existing compiler; acquisition will not replace it.",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    attempt_id = uuid.uuid4().hex
    slot = target.parent / (".leanflow-install-" + attempt_id)
    incoming = slot / "compiler"
    journal_path = leanflow_home() / "toolchain-installations" / (attempt_id + ".json")

    def check() -> None:
        policy.check(cancel)
        policy.observe_disk()

    check()
    expected = entry_identity(downloaded, check=check)
    if expected is None:
        raise SetupFailure(
            "setup_toolchain_incomplete",
            "The private compiler disappeared before publication.",
            "Retain acquisition evidence and retry with a fresh private stage.",
        )
    journal = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "toolchain": toolchain,
        "target": str(target),
        "incoming": str(incoming),
        "slot": str(slot),
        "slot_inode": None,
        "owner": {"pid": os.getpid(), "start": process_states()[os.getpid()][2]},
        "cleanup_verified": policy.usage.cleanup_verified,
        "private": str(downloaded),
        "before": None,
        "after": expected,
        "state": "REGISTERED",
    }

    def save(state: str) -> None:
        journal["state"] = state
        journal["record_id"] = identity({k: v for k, v in journal.items() if k != "record_id"})
        durable_json(journal_path, journal)
        if checkpoint:
            checkpoint("compiler:" + state)

    save("REGISTERED")
    slot.mkdir(mode=0o700)
    sync_directory(slot.parent)
    if checkpoint:
        checkpoint("compiler:SLOT_CREATED")
    journal["slot_inode"] = [slot.stat().st_dev, slot.stat().st_ino]
    save("SLOT_READY")
    if _same_device(downloaded, target.parent):
        rename_exclusive(downloaded, incoming)
    else:
        copy_entry(downloaded, incoming, check=check)
    if entry_identity(incoming, check=check) != expected:
        raise SetupFailure(
            "setup_toolchain_input_changed",
            "The checked compiler changed before publication.",
            "Inspect the retained adjacent/private versions before trying again.",
        )
    save("PREPARED")
    check()
    try:
        rename_exclusive(incoming, target)
    except FileExistsError as error:
        raise SetupFailure(
            "setup_toolchain_destination_changed",
            "Another installer created the requested compiler.",
            "Keep both versions and retry after checking the existing compiler.",
        ) from error
    sync_directory(target.parent)
    save("INSTALLED")
    slot.rmdir()
    sync_directory(slot.parent)
    check()
    return {"journal_path": str(journal_path), "sha256": expected, "attempt_id": attempt_id}
