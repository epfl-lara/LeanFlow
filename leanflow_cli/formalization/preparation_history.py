"""Discover bounded preparation ownership without reconstructing approval from surviving records."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import identifier, identity
from leanflow_cli.formalization.preparation_errors import PreparationConflict

MAX_CONTROL_BYTES = 8 * 1024**2
MAX_TOTAL_CONTROL_BYTES = 64 * 1024**2
MAX_HISTORY_ENTRIES = 524288
MAX_HISTORY_BYTES = 256 * 1024**3
MAX_NAMES = 16384


def _version(value: os.stat_result) -> tuple[int, ...]:
    """Bind an observed inode and every mutation-sensitive metadata field."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _limit() -> PreparationConflict:
    """Keep over-limit history unavailable without authorizing a truncated acknowledgment."""
    return PreparationConflict(
        "Preparation registry history exceeds bounded automatic recovery; preserve all history "
        "and use supported bounded recovery assistance; no truncated acknowledgment is allowed"
    )


def observed_entry(path: Path, budget: list[int]) -> str | None:
    """Stream a bounded stable version, recording links and special entries without opening them."""
    try:
        first = path.lstat()
    except FileNotFoundError:
        return None
    checksum = hashlib.sha256()
    pending = [path]
    while pending:
        entry = pending.pop()
        before = entry.lstat()
        budget[0] += 1
        if budget[0] > MAX_HISTORY_ENTRIES:
            raise _limit()
        relative = entry.relative_to(path).as_posix() if entry != path else ""
        mode = before.st_mode
        if stat.S_ISDIR(mode):
            with os.scandir(entry) as children:
                names = []
                for child in children:
                    names.append(child.name)
                    if len(names) + budget[0] > MAX_HISTORY_ENTRIES:
                        raise _limit()
            pending.extend(entry / name for name in sorted(names, reverse=True))
            content: Any = ["directory", sorted(names)]
        elif stat.S_ISLNK(mode):
            content = ["link", os.readlink(entry)]
        elif stat.S_ISREG(mode):
            budget[1] += before.st_size
            if budget[1] > MAX_HISTORY_BYTES:
                raise _limit()
            fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if _version(os.fstat(fd)) != _version(before):
                    raise PreparationConflict("Preparation history changed before observation")
                remaining = before.st_size
                data_hash = hashlib.sha256()
                while remaining:
                    block = os.read(fd, min(1024**2, remaining))
                    if not block:
                        raise PreparationConflict("Preparation history changed during observation")
                    remaining -= len(block)
                    data_hash.update(block)
                if os.read(fd, 1) or _version(os.fstat(fd)) != _version(before):
                    raise PreparationConflict("Preparation history changed during observation")
                content = ["file", data_hash.hexdigest()]
            finally:
                os.close(fd)
        else:
            content = ["nonregular", before.st_dev, before.st_ino, mode]
        if _version(entry.lstat()) != _version(before):
            raise PreparationConflict("Preparation history changed during observation")
        checksum.update(bytes.fromhex(identity([relative, stat.S_IMODE(mode), content])))
    if _version(path.lstat()) != _version(first):
        raise PreparationConflict("Preparation history changed during observation")
    return checksum.hexdigest()


def _children(path: Path, probes: list[int]) -> list[Path]:
    """Enumerate a literal directory without following authority aliases."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(mode):
        raise PreparationConflict("Preparation registry history contains a non-directory owner")
    result = []
    with os.scandir(path) as children:
        for child in children:
            probes[0] += 1
            if probes[0] > MAX_NAMES:
                raise _limit()
            result.append(path / child.name)
    return sorted(result)


def _owned(name: str) -> bool:
    """Recognize only controller transaction identifiers, not incidental filesystem names."""
    try:
        identifier(name)
        return True
    except ValueError:
        return False


def discover_history(root: Path, directory: Path) -> dict[str, Any]:
    """Find full journals, progress owners, receipt and project slots under finite namespace bounds."""
    if (root / ".leanflow").is_symlink() or directory.parent.is_symlink():
        raise PreparationConflict("Preparation registry history has an aliased namespace")
    evidence: dict[str, str | None] = {}
    transactions: set[str] = set()
    slots: dict[str, str] = {}
    archives: dict[str, str | None] = {}
    unavailable_controls: list[str] = []
    budget = [0]
    probes = [0]

    def control(path: Path) -> None:
        """Bind control bytes while avoiding special-file reads and unbounded allocation."""
        relative = path.relative_to(directory).as_posix()
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISREG(mode):
            evidence["home:" + relative] = observed_entry(path, [0, 0])
            if relative == "receipt.json" or relative.startswith("index-history/"):
                unavailable_controls.append(relative)
            return
        size = path.lstat().st_size
        budget[0] += size
        if size > MAX_CONTROL_BYTES or budget[0] > MAX_TOTAL_CONTROL_BYTES:
            raise _limit()
        data = read_regular_bytes(path, MAX_CONTROL_BYTES)
        evidence["home:" + relative] = hashlib.sha256(data).hexdigest()

    home_names = (
        {path.name: path for path in _children(directory, probes)} if directory.exists() else {}
    )
    for reserved in (
        "registry.json",
        "receipt.json",
        "transactions",
        "progress",
        "archive-intents",
        "index-history",
    ):
        if any(name.casefold() == reserved.casefold() and name != reserved for name in home_names):
            raise PreparationConflict("Preparation registry history has a case-aliased control")
    if "receipt.json" in home_names:
        control(home_names["receipt.json"])
    for name in ("transactions", "progress", "archive-intents"):
        parent = directory / name
        for path in _children(parent, probes):
            key = path.stem if name == "transactions" else path.name
            if not _owned(key) or (name == "transactions" and path.suffix != ".json"):
                continue
            transactions.add(key)
            if name == "transactions":
                control(path)
            else:
                evidence["home:" + path.relative_to(directory).as_posix()] = "owned-history"
    for path in _children(directory / "index-history", probes):
        if path.suffix == ".json" and _owned(path.stem):
            control(path)
            relative = path.relative_to(directory).as_posix()
            archives[relative] = evidence["home:" + relative]
    project_parent = root / ".leanflow/preparation-transactions"
    for path in _children(project_parent, probes):
        if not _owned(path.name):
            continue
        if stat.S_ISDIR(path.lstat().st_mode) and not _children(path, probes):
            continue
        transactions.add(path.name)
        slots[path.name] = path.relative_to(root).as_posix()
        evidence["project:" + slots[path.name]] = "owned-retained-slots"
    payload = {
        "schema_version": 1,
        "root": str(root.resolve()),
        "transactions": sorted(transactions),
        "evidence": evidence,
        "slot_roots": slots,
        "index_history": archives,
        "unavailable_controls": unavailable_controls,
    }
    return {**payload, "discovery_id": identity(payload)}
