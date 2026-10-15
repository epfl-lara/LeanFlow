"""Capture actual source inodes before bounded, no-clobber prover mutations."""

from __future__ import annotations

import os
import re
import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from leanflow_cli.formalization.evidence import contained, digest, durable_json, sync_directory
from leanflow_cli.formalization.publication_io import (
    check_filesystem,
    entry_path,
    mkdir_durable,
    rename_exclusive,
)
from leanflow_cli.workflows.prover.source import SourceConflictError

FILE_LIMIT = 8 * 1024 * 1024
JOURNAL_LIMIT = 64 * 1024
BASE = ".leanflow/prover-source-transactions"
Boundary = Callable[[str], None] | None


def observe(path: Path) -> dict[str, Any] | None:
    """Identify bounded regular bytes and their inode without following substituted links."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    inode = [info.st_dev, info.st_ino]
    if not stat.S_ISREG(info.st_mode):
        return {"inode": inode, "sha256": "nonregular"}
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > FILE_LIMIT:
            raise SourceConflictError(
                f"Protected source exceeds its bounded recovery contract: {path}"
            )
        content = handle.read(FILE_LIMIT + 1)
        after = os.fstat(handle.fileno())
    if (
        len(content) > FILE_LIMIT
        or [before.st_dev, before.st_ino] != inode
        or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
    ):
        raise SourceConflictError(f"Protected source changed while observing its inode: {path}")
    return {"inode": inode, "sha256": digest(content)}


def directory(root: Path, identifier: str) -> Path:
    """Resolve one exact controller mutation directory without accepting caller paths."""
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("Invalid prover source mutation identity")
    return contained(root, f"{BASE}/{identifier}")


def load(root: Path, identifier: str) -> dict[str, Any]:
    """Read and validate bounded durable mutation evidence before any recovery move."""
    import json

    path = directory(root, identifier) / "journal.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > JOURNAL_LIMIT:
        raise SourceConflictError(f"Prover source mutation journal is unavailable: {path}")
    record = json.loads(path.read_text())
    if (
        not isinstance(record, dict)
        or record.get("id") != identifier
        or record.get("schema") != 1
        or record.get("state")
        not in {
            "prepared",
            "capture_pending",
            "captured",
            "install_pending",
            "installed",
            "rollback_pending",
            "restore_pending",
            "rolled_back",
            "committed",
            "conflicted",
        }
    ):
        raise ValueError("Invalid prover source mutation journal")
    entry_path(root, record["path"])
    for key in ("before", "after"):
        value = record[key]
        if value is not None and (
            not isinstance(value, dict)
            or not isinstance(value.get("inode"), list)
            or len(value["inode"]) != 2
            or any(type(number) is not int or number < 0 for number in value["inode"])
            or not re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))
        ):
            raise ValueError("Invalid prover source mutation observation")
    if type(record.get("original_captured")) is not bool:
        raise ValueError("Invalid prover source capture observation")
    if not isinstance(record.get("rollback_slots"), list) or len(record["rollback_slots"]) > 64:
        raise ValueError("Invalid prover source rollback slots")
    for slot in record["rollback_slots"]:
        if not isinstance(slot, str) or not re.fullmatch(r"[0-9a-f]{32}\.rollback", slot):
            raise ValueError("Invalid prover source rollback slot")
    return record


def save(root: Path, record: dict[str, Any]) -> None:
    """Flush the bounded mutation intent before changing a canonical directory entry."""
    import json

    if len(json.dumps(record).encode()) > JOURNAL_LIMIT:
        raise ValueError("Prover source mutation journal exceeds its recovery limit")
    durable_json(directory(root, record["id"]) / "journal.json", record)


def point(boundary: Boundary, name: str) -> None:
    """Expose filesystem boundaries for deterministic competing-process regressions."""
    if boundary is not None:
        boundary(name)


def conflict(root: Path, record: dict[str, Any], detail: str) -> None:
    """Retain every displaced inode and identify the evidence requiring user reconciliation."""
    location = directory(root, record["id"])
    record["state"] = "conflicted"
    record["conflict"] = detail
    record["conflict_paths"] = {
        "canonical": str(entry_path(root, record["path"])),
        "journal": str(location / "journal.json"),
        "original": str(location / "displaced"),
        "candidate": str(location / "candidate"),
        "rollback_directory": str(location),
        # Avoid duplicating a long project prefix for every bounded capture slot.
        "rollback_versions": list(record["rollback_slots"]),
    }
    save(root, record)
    raise SourceConflictError(
        f"protected source changed during interrupted prover source mutation: {record['path']}; "
        f"{detail}; canonical: {record['conflict_paths']['canonical']}; "
        f"retained versions: {location}; inspect {location / 'journal.json'} for exact version paths. "
        "This conflicted journal cannot be reconciled by restoring bytes for same-run resume."
    )


def create(root: Path, relative: str, before: bytes | None, after: bytes | None) -> str:
    """Prepare bounded bytes and record the expected source inode before installation."""
    if any(value is not None and len(value) > FILE_LIMIT for value in (before, after)):
        raise ValueError("Prover source mutation file exceeds its 8 MiB recovery limit")
    target = entry_path(root, relative)
    actual = observe(target)
    expected = digest(before) if before is not None else None
    if (actual["sha256"] if actual else None) != expected:
        raise SourceConflictError(
            f"protected source changed before prover source mutation: {relative}"
        )
    identifier = uuid.uuid4().hex
    location = directory(root, identifier)
    mkdir_durable(location)
    check_filesystem(root, location, [target])
    # Exercise the actual directory/filesystem API before displacing a source.
    probe, moved = location / "capability-probe", location / "capability-moved"
    try:
        probe.write_bytes(b"probe")
        rename_exclusive(probe, moved)
        probe.write_bytes(b"second")
        try:
            rename_exclusive(probe, moved)
        except FileExistsError:
            pass
        else:
            raise ValueError("Filesystem replaced an occupied no-replace probe")
    finally:
        probe.unlink(missing_ok=True)
        moved.unlink(missing_ok=True)
        sync_directory(location)
    incoming = location / "incoming"
    if after is not None:
        with incoming.open("xb") as handle:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        # Pin the installed inode too: a later atomic editor save can unlink its
        # canonical name while another descriptor still writes to this version.
        os.link(incoming, location / "candidate", follow_symlinks=False)
        sync_directory(location)
    record = {
        "schema": 1,
        "id": identifier,
        "path": relative,
        "before": actual,
        "after": observe(incoming) if after is not None else None,
        "state": "prepared",
        "original_captured": False,
        "rollback_slots": [],
    }
    save(root, record)
    return identifier


def install(root: Path, identifier: str, boundary: Boundary = None) -> None:
    """Capture the actual destination, verify it, and install only into an empty name."""
    record = load(root, identifier)
    if record["state"] != "prepared":
        raise ValueError("Prover source mutation is not prepared")
    location = directory(root, identifier)
    target = entry_path(root, record["path"])
    mkdir_durable(target.parent)
    record["state"] = "capture_pending"
    save(root, record)
    point(boundary, "capture_pending")
    if record["before"] is not None:
        try:
            rename_exclusive(target, location / "displaced")
        except FileNotFoundError:
            conflict(root, record, "source disappeared before its capture")
        point(boundary, "captured")
        from leanflow_cli.workflows.prover.source_mutation_recovery import (
            observe_captured,
            restore_unknown,
        )

        if observe_captured(root, record, location / "displaced", target) != record["before"]:
            restore_unknown(root, record, location / "displaced", target)
            conflict(root, record, "concurrent editor version captured before installation")
    elif observe(target) is not None:
        conflict(root, record, "independent destination appeared before installation")
    record["original_captured"] = record["before"] is not None
    record["state"] = "captured"
    save(root, record)
    record["state"] = "install_pending"
    save(root, record)
    point(boundary, "install_pending")
    if record["after"] is not None:
        try:
            rename_exclusive(location / "incoming", target)
        except FileExistsError:
            conflict(root, record, "independent editor destination prevents installation")
        point(boundary, "installed")
    record["state"] = "installed"
    save(root, record)
    validate_installed(root, identifier)


def validate_installed(root: Path, identifier: str) -> None:
    """Reject canonical or retained changes before reporting a successful integration."""
    record = load(root, identifier)
    if observe(entry_path(root, record["path"])) != record["after"]:
        conflict(root, record, "installed destination was independently changed")
    audit_retained(root, identifier)


def audit_retained(root: Path, identifier: str) -> None:
    """Detect late descriptor writes without discarding the inode that preserves them."""
    record = load(root, identifier)
    if record["before"] is not None:
        captured = directory(root, identifier) / "displaced"
        actual = observe(captured)
        if (
            not (
                actual is None
                and record["state"] == "rolled_back"
                and record.get("original_captured") is False
            )
            and actual != record["before"]
        ):
            conflict(root, record, "late edit retained in the displaced original inode")
    if record["after"] is not None:
        actual = observe(directory(root, identifier) / "candidate")
        if actual != record["after"]:
            conflict(root, record, "late editor version retained in the installed candidate inode")
    for slot in record["rollback_slots"]:
        actual = observe(directory(root, identifier) / slot)
        if actual is not None and actual != record["after"]:
            conflict(root, record, "late editor version retained in a rollback inode")
    if record["state"] == "conflicted":
        conflict(root, record, str(record.get("conflict", "unresolved source versions")))


def commit(root: Path, identifier: str) -> None:
    """Mark checked filesystem completion while retaining original inodes for late writers."""
    validate_installed(root, identifier)
    record = load(root, identifier)
    record["state"] = "committed"
    save(root, record)
