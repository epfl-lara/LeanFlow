"""Require actual committed controller publication before granting a surviving receipt authority."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from .evidence import contained, identifier
from .publication_case_paths import assert_exact_control_path
from .publication_journal import load_journal, load_registry
from .publication_registry_inventory import PublicationAuthorityLoss


def _control_digest(path: Path) -> str:
    """Stream one stable regular control inode without imposing the recovery JSON size limit."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise PublicationAuthorityLoss("Published control evidence is nonregular: " + str(path))
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        opened = os.fstat(descriptor)

        def version(info: os.stat_result) -> tuple[int, ...]:
            """Bind inode and mutation identity while ignoring read access time."""
            return (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_nlink,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )

        if version(before) != version(opened):
            raise PublicationAuthorityLoss("Published control changed before hashing")
        checksum = hashlib.sha256()
        remaining = opened.st_size
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while remaining:
                data = handle.read(min(1024**2, remaining))
                if not data:
                    raise PublicationAuthorityLoss("Published control changed while hashing")
                checksum.update(data)
                remaining -= len(data)
            if handle.read(1):
                raise PublicationAuthorityLoss("Published control grew while hashing")
        if version(os.fstat(descriptor)) != version(opened) or version(path.lstat()) != version(
            before
        ):
            raise PublicationAuthorityLoss("Published control changed during hashing")
        return checksum.hexdigest()
    finally:
        os.close(descriptor)


def require_committed_publication(root: Path, document_id: str, revision_id: str) -> None:
    """Authenticate one document's controls against any live committed transaction, not global selection."""
    prefix = f"formalization/{identifier(document_id)}"
    revision_id = identifier(revision_id)
    controls = [
        f"{prefix}/current.json",
        f"{prefix}/handoff.json",
        f"{prefix}/revisions/{revision_id}.json",
        f"{prefix}/receipts/{revision_id}.json",
    ]
    expected = {}
    for relative in controls:
        assert_exact_control_path(root, relative)
        expected[relative] = _control_digest(contained(root, relative))
    registry = load_registry(root)
    for entry in registry["transactions"].values():
        if entry.get("loss"):
            continue
        assert_exact_control_path(root, entry["journal"])
        journal = load_journal(contained(root, entry["journal"]))
        if (
            journal["revision_id"] == revision_id
            and journal["state"] == "COMMITTED"
            and not journal.get("conflicts")
            and not journal.get("resolutions")
            and all(
                journal["writes"].get(relative, {}).get("after") == checksum
                for relative, checksum in expected.items()
            )
        ):
            return
    raise PublicationAuthorityLoss(
        "Published acceptance has no matching committed controller transaction; "
        "preserve retained evidence and prepare a fresh independently accepted publication"
    )
