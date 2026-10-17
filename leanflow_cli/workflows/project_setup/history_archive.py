"""Archive closed setup evidence with bounded positive membership and exact-ID reads."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import digest, durable_json, identity, sync_directory

PAGE_SIZE = 128
MAX_PAGES = 128
LIMIT = 64 * 1024
_ID = re.compile(r"[0-9a-f]{32}")


def _anchor(directory: Path) -> Path:
    """Keep positive expectations outside the removable archive and active JSON census."""
    return directory.with_name(f"{directory.name}.anchor")


def _binding(raw_head: bytes, membership: dict[str, str]) -> dict[str, Any]:
    """Bind the complete monotonic member set, including changes within its last page."""
    return {
        "schema": 1,
        "records": len(membership),
        "head_sha256": digest(raw_head),
        "membership_sha256": identity(membership),
    }


def _contents(directory: Path) -> tuple[bytes, dict[str, str]]:
    """Read bounded header pages without deriving authority from their mere presence."""
    head = directory / "head.json"
    raw_head = read_regular_bytes(head, limit=LIMIT)
    value = json.loads(raw_head)
    count = value.get("pages") if isinstance(value, dict) else None
    if type(count) is not int or not 0 <= count <= MAX_PAGES or value.get("schema") != 1:
        raise ValueError(f"Invalid setup archive header: {head}")
    result: dict[str, str] = {}
    for page in range(count):
        path = directory / f"index-{page:04d}.json"
        record = json.loads(read_regular_bytes(path, limit=LIMIT))
        entries = record.get("records") if isinstance(record, dict) else None
        if (
            not isinstance(entries, dict)
            or record.get("schema") != 1
            or not 1 <= len(entries) <= PAGE_SIZE
        ):
            raise ValueError(f"Invalid setup archive page: {path}")
        for identifier, checksum in entries.items():
            if (
                not _ID.fullmatch(identifier)
                or not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or identifier in result
            ):
                raise ValueError(f"Invalid setup archive membership: {path}")
            result[identifier] = checksum
    return raw_head, result


def members(directory: Path) -> dict[str, str]:
    """Require the externally anchored complete membership before trusting archived history."""
    if directory.is_symlink():
        raise ValueError("Setup archive directory is aliased")
    head = directory / "head.json"
    anchor = _anchor(directory)
    if not head.exists() and not head.is_symlink():
        if anchor.exists() or anchor.is_symlink():
            raise ValueError(f"Expected setup archive is missing: {directory}")
        if directory.exists() and next(directory.iterdir(), None) is not None:
            raise ValueError(f"Setup archive header is missing: {head}")
        return {}
    raw_head, result = _contents(directory)
    if not anchor.exists() and not anchor.is_symlink():
        raise ValueError(f"Setup archive anchor is missing; retain its evidence: {anchor}")
    value = json.loads(read_regular_bytes(anchor, limit=LIMIT))
    if value != _binding(raw_head, result):
        raise ValueError(f"Setup archive anchor does not match preserved history: {anchor}")
    return result


def read(directory: Path, identifier: str, membership: dict[str, str] | None = None) -> bytes:
    """Require one positively indexed, byte-identical preserved record."""
    if not _ID.fullmatch(identifier):
        raise ValueError("Invalid setup archive identity")
    known = members(directory) if membership is None else membership
    if identifier not in known:
        raise ValueError(f"Setup record is not in the archive: {identifier}")
    raw = read_regular_bytes(directory / f"{identifier}.json", limit=LIMIT)
    if digest(raw) != known[identifier]:
        raise ValueError(f"Preserved setup archive record changed: {identifier}")
    return raw


def preserve(directory: Path, identifier: str, raw: bytes) -> None:
    """Durably anchor exact closed evidence before any active name may be retired."""
    if not _ID.fullmatch(identifier) or len(raw) > LIMIT:
        raise ValueError("Invalid bounded setup archive record")
    known = members(directory)
    checksum = digest(raw)
    if identifier in known:
        if read(directory, identifier, known) != raw:
            raise ValueError("Setup archive identity already contains different evidence")
        return
    if len(known) >= PAGE_SIZE * MAX_PAGES:
        raise ValueError("Setup archive is full; retain its evidence and choose a fresh state home")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("Setup archive directory is aliased")
    if not (directory / "head.json").exists():
        durable_json(directory / "head.json", {"schema": 1, "pages": 0})
        raw_head, empty = _contents(directory)
        durable_json(_anchor(directory), _binding(raw_head, empty))
    target = directory / f"{identifier}.json"
    try:
        with target.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(directory)
    except FileExistsError:
        if read_regular_bytes(target, limit=LIMIT) != raw:
            raise ValueError("Unindexed setup archive version conflicts with closed evidence")
    page = len(known) // PAGE_SIZE
    entries = dict(list(known.items())[page * PAGE_SIZE :])
    entries[identifier] = checksum
    durable_json(directory / f"index-{page:04d}.json", {"schema": 1, "records": entries})
    durable_json(directory / "head.json", {"schema": 1, "pages": page + 1})
    raw_head, complete = _contents(directory)
    if complete != {**known, identifier: checksum}:
        raise ValueError("Setup archive membership changed before its anchor was durable")
    durable_json(_anchor(directory), _binding(raw_head, complete))
    # Both callers unlink active evidence only after this returns. A crash before
    # the anchor commit leaves active originals and makes archive reads fail closed.
    if members(directory) != complete:
        raise ValueError("Setup archive anchor changed before retiring active evidence")
