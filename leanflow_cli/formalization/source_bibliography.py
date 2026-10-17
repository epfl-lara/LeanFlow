"""Discover literal bibliography inputs and supplied root-jobname output without executing TeX."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ResolveSupport = Callable[[Path, str, tuple[str, ...]], Path | None]
BIBLIOGRAPHY_MAX_BYTES = 4 * 1024 * 1024


class BibliographySizeLimitError(ValueError):
    """Distinguish an observed support-size limit from unsafe or changing files."""

    def __init__(self, path: Path, size_bytes: int, limit_bytes: int) -> None:
        """Retain the bounded metadata needed to explain unavailable support."""
        super().__init__(f"Expected a bounded regular file: {path}")
        self.path = path
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


def check_bibliography_size(path: Path, limit_bytes: int = BIBLIOGRAPHY_MAX_BYTES) -> None:
    """Classify oversized regular support without reading or accepting an alias."""
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Expected a bounded regular file: {path}")
    if metadata.st_size > limit_bytes:
        raise BibliographySizeLimitError(path, metadata.st_size, limit_bytes)


@dataclass(frozen=True)
class OversizedBibliographyFile:
    """Describe an unread support candidate without treating it as frozen context."""

    path: Path
    target: str
    role: str
    size_bytes: int
    limit_bytes: int


@dataclass(frozen=True)
class BibliographySupport:
    """Keep rendered bibliography discovery separate from absent database source."""

    files: tuple[Path, ...]
    missing_targets: tuple[str, ...]
    missing_databases: tuple[str, ...]
    ambiguous_jobnames: tuple[Path, ...]
    case_aliases: tuple[Path, ...]
    missing_rendered: bool
    rendered_available: bool
    oversized_files: tuple[OversizedBibliographyFile, ...]


def _local_path(root: Path, path: Path) -> bool:
    """Reject lexical escapes before inspecting a candidate or its parent."""
    return path.is_relative_to(root) and ".." not in path.relative_to(root).parts


def _regular_local_directory(root: Path, path: Path) -> bool:
    """Require contained real directories before enumerating a jobname entry."""
    if not _local_path(root, path):
        return False
    cursor = path
    try:
        while True:
            if not stat.S_ISDIR(cursor.lstat().st_mode):
                return False
            if cursor == root:
                return True
            cursor = cursor.parent
    except FileNotFoundError:
        return False


def _regular_local_file(root: Path, path: Path) -> bool:
    """Refuse outside, nonregular, or aliased candidates before any content read."""
    if not _regular_local_directory(root, path.parent):
        return False
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _jobname_files(root: Path, selected: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Distinguish exact directory entries from case-folded filesystem lookups."""
    exact: dict[Path, None] = {}
    aliases: dict[Path, None] = {}
    for candidate in dict.fromkeys((selected.with_suffix(".bbl"), root / (selected.stem + ".bbl"))):
        if not _regular_local_directory(root, candidate.parent):
            continue
        with os.scandir(candidate.parent) as entries:
            matches = sorted(
                entry.name
                for entry in entries
                if entry.name.casefold() == candidate.name.casefold()
            )
        for name in matches:
            path = candidate.parent / name
            if name != candidate.name:
                aliases[path] = None
            elif _regular_local_file(root, path):
                exact[path] = None
    return tuple(exact), tuple(aliases)


def discover_bibliography_support(
    root: Path,
    selected: Path,
    parent: Path,
    value: str,
    resolve: ResolveSupport,
) -> BibliographySupport:
    """Freeze supplied support candidates while retaining missing and ambiguous inputs.

    Traditional LaTeX reads the root job name's .bbl, not the bibliography
    database argument's stem. A supplied .bbl supports the frozen rendering;
    it does not establish that the BibTeX databases can be regenerated. All
    named .bib inputs are independently complete source context without a
    generated .bbl; a differently named literal .bbl is only extra context.
    """
    if not _local_path(root, selected) or not _local_path(root, parent):
        raise ValueError("Bibliography source must remain inside its original bundle")
    jobnames, aliases = _jobname_files(root, selected)
    job_context = tuple(dict.fromkeys((*jobnames, *aliases)))
    files: dict[Path, None] = {}
    oversized: dict[tuple[Path, str], OversizedBibliographyFile] = {}

    def retain(candidate: Path, role: str, target: str) -> bool:
        """Exclude known oversize inputs while preserving every other read failure."""
        try:
            check_bibliography_size(candidate)
        except BibliographySizeLimitError as error:
            files.pop(candidate, None)
            oversized[candidate, role] = OversizedBibliographyFile(
                candidate, target, role, error.size_bytes, error.limit_bytes
            )
            return False
        files[candidate] = None
        return True

    usable_jobnames = tuple(
        candidate
        for candidate in jobnames
        if retain(candidate, "rendered_bibliography", candidate.relative_to(root).as_posix())
    )
    for candidate in aliases:
        if _regular_local_file(root, candidate):
            retain(candidate, "bibliography_context", candidate.relative_to(root).as_posix())
    missing_targets: list[str] = []
    missing_databases: list[str] = []
    unavailable_database = False
    for token in dict.fromkeys(token.strip() for token in value.split(",")):
        if not token or any(character in token for character in "\\#{}"):
            missing_targets.append(token)
            continue
        database = token if token.endswith(".bib") else token + ".bib"
        database_path = resolve(parent, database, ())
        literal = resolve(parent, token, (".bib", ".bbl"))
        if literal is not None:
            # Preserve legacy literal .bbl context, but never call it the
            # rendered jobname or record an APFS case alias under a fake name.
            matches = [
                candidate
                for candidate in job_context
                if candidate.parent == literal.parent
                and candidate.name.casefold() == literal.name.casefold()
            ]
            if not matches and literal != database_path:
                retain(
                    literal,
                    "database" if literal.suffix.lower() == ".bib" else "bibliography_context",
                    token,
                )
        if database_path is None:
            missing_databases.append(token)
            unavailable_database = True
        else:
            unavailable_database |= not retain(database_path, "database", token)
    rendered_available = len(jobnames) == len(usable_jobnames) == 1 and not aliases
    return BibliographySupport(
        tuple(files),
        tuple(missing_targets),
        tuple(missing_databases) if rendered_available else (),
        job_context if len(jobnames) > 1 or aliases else (),
        aliases,
        not usable_jobnames and unavailable_database,
        rendered_available,
        tuple(oversized.values()),
    )
