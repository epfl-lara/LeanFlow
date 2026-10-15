"""Keep literal publication control names distinct from ordinary case-folded user libraries."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path

from .evidence import identifier, normalized_relative
from .publication_io import PublicationConflict

_CONTROL_FILES = {"current.json", "handoff.json", "proof-current.json", "publication.json"}
_CONTROL_DIRECTORIES = {"revisions", "receipts", "proof-revisions", "receipt-history"}
_REVISION_FILE = re.compile(r"(?:rev|proof-rev)-[a-f0-9]{64}\.json\Z")
_REVISION_ID = re.compile(r"(?:rev|proof-rev)-[a-f0-9]{64}\Z")
_CONTROL_ID = re.compile(r"(?:rev|proof-rev|receipt)-[a-f0-9]{64}(?:\.json)?\Z")
_MAX_CASE_PROBES = 262144


def exact_control_path(root: Path, relative: str) -> bool:
    """Require every existing component's literal spelling; permit prospective absent suffixes."""
    normalized_relative(relative)
    cursor = root.resolve()
    probed = 0
    for component in relative.split("/"):
        target = cursor / component
        if not target.exists() and not target.is_symlink():
            return True
        with os.scandir(cursor) as children:
            found = False
            for child in children:
                probed += 1
                if probed > _MAX_CASE_PROBES:
                    raise PublicationConflict(
                        "Publication control case check exceeds its entry bound"
                    )
                if child.name == component:
                    found = True
                    break
            if not found:
                return False
        cursor = target
    return True


def assert_exact_control_path(root: Path, relative: str) -> None:
    """Refuse case aliases before reading or creating controller-owned publication metadata."""
    if not exact_control_path(root, relative):
        raise PublicationConflict(
            "Publication control path has a case alias: "
            + relative
            + "; preserve the existing library/history and restore the literal controller spelling before publication"
        )


def exact_receipt_control_path(directory: Path, relative: str) -> bool:
    """Check document ancestry without adopting similarly named ordinary user libraries."""
    if directory.parent.name == "formalization":
        root = directory.parents[1]
    elif directory.parent.name == "formalizations" and directory.parents[1].name == ".leanflow":
        root = directory.parents[2]
    else:
        root = directory
    target = directory / relative
    return exact_control_path(root, target.relative_to(root).as_posix())


def assert_receipt_control_path(directory: Path, relative: str) -> None:
    """Refuse an aliased receipt path including its reserved document ancestors."""
    if not exact_receipt_control_path(directory, relative):
        raise PublicationConflict("Publication receipt control path has a case alias: " + relative)


def _children(path: Path, probe: Callable[[], None]) -> list[Path]:
    """Enumerate one regular directory under the caller's shared probe bound."""
    if path.is_symlink() or not path.is_dir():
        return []
    result = []
    with os.scandir(path) as children:
        for child in children:
            probe()
            result.append(path / child.name)
    return result


def _controller_document(
    children: list[Path], probe: Callable[[], None], read: Callable[[Path], bytes | str | None]
) -> bool:
    """Recognize actual revision/receipt shapes instead of adopting generic library JSON files."""
    for child in children:
        lowered = child.name.lower()
        if lowered in _CONTROL_DIRECTORIES:
            if any(_REVISION_FILE.fullmatch(path.name) for path in _children(child, probe)):
                return True
        elif (
            lowered in _CONTROL_FILES
            and not child.is_symlink()
            and child.is_file()
            and child.stat().st_size <= 8192
        ):
            data = read(child)
            if not isinstance(data, bytes):
                continue
            try:
                value = json.loads(data)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(value, dict):
                continue
            if _REVISION_ID.fullmatch(str(value.get("revision_id", ""))) or re.fullmatch(
                r"handoff-[a-f0-9]{64}", str(value.get("handoff_id", ""))
            ):
                return True
    return False


def aliased_document_controls(
    root: Path,
    probe: Callable[[], None],
    read: Callable[[Path], bytes | str | None],
) -> Iterator[Path]:
    """Retain genuine case-renamed public controls without claiming approval from their bytes."""
    for namespace in _children(root, probe):
        if namespace.name.lower() != "formalization":
            continue
        for document in _children(namespace, probe):
            try:
                identifier(document.name)
            except ValueError:
                continue
            children = _children(document, probe)
            records = {
                child: _children(child, probe)
                for child in children
                if child.name.lower() in _CONTROL_DIRECTORIES
            }
            history = [
                receipt
                for child, entries in records.items()
                if child.name.lower() == "receipt-history"
                for entry in entries
                for receipt in _children(entry, probe)
            ]
            aliases = (
                namespace.name != "formalization"
                or any(
                    child.name.lower() in _CONTROL_FILES | _CONTROL_DIRECTORIES
                    and child.name != child.name.lower()
                    for child in children
                )
                or any(
                    entry.name != entry.name.lower() and _CONTROL_ID.fullmatch(entry.name.lower())
                    for entry in [
                        *(entry for entries in records.values() for entry in entries),
                        *history,
                    ]
                )
            )
            if not aliases or not _controller_document(children, probe, read):
                continue
            for child in children:
                lowered = child.name.lower()
                if lowered in _CONTROL_FILES:
                    yield child
                elif lowered in _CONTROL_DIRECTORIES:
                    for entry in records[child]:
                        if entry.suffix.lower() == ".json":
                            yield entry
                        elif lowered == "receipt-history":
                            for receipt in _children(entry, probe):
                                if receipt.suffix == ".json":
                                    yield receipt
