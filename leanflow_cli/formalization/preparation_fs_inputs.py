"""Fingerprint and archive setup filesystem entries without sharing mathematical source inodes."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from core.file_copy import copy_file
from leanflow_cli.formalization.evidence import identity, normalized_relative, sync_directory
from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings

CONFIG = {"lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json"}
MANAGED = CONFIG | {".lake", ".leanflow/project.yaml"}


def _entries(path: Path) -> Iterator[Path]:
    """Yield deterministic directory entries without retaining the full dependency tree."""
    yield path
    if path.is_dir() and not path.is_symlink():
        for child in sorted(path.iterdir()):
            yield from _entries(child)


def entry_identity(
    path: Path,
    *,
    check: Callable[[], None] | None = None,
    link_root: Path | None = None,
) -> str | None:
    """Stream complete entry bytes/modes and reject changes or escaping links while hashing."""
    if not path.exists() and not path.is_symlink():
        return None
    result = hashlib.sha256()
    base = (
        link_root.resolve()
        if link_root is not None
        else path.resolve() if path.is_dir() and not path.is_symlink() else path.parent.resolve()
    )
    if link_root is not None and not path.absolute().is_relative_to(link_root.absolute()):
        raise ValueError("Preparation entry is outside its explicit link boundary")
    for entry in _entries(path):
        if check:
            check()
        before = entry.lstat()
        relative = "" if entry == path else entry.relative_to(path).as_posix()
        mode = stat.S_IMODE(before.st_mode)
        if stat.S_ISLNK(before.st_mode):
            if not entry.resolve().is_relative_to(base):
                raise ValueError(f"Preparation input has an escaping link: {entry}")
            content = ["link", os.readlink(entry)]
        elif stat.S_ISDIR(before.st_mode):
            content = ["directory"]
        elif stat.S_ISREG(before.st_mode):
            digest = hashlib.sha256()
            descriptor = os.open(entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError("Preparation entry changed before hashing")
                while block := stream.read(1024 * 1024):
                    if check:
                        check()
                    digest.update(block)
            content = ["file", digest.hexdigest()]
        else:
            raise ValueError(f"Unsupported preparation entry: {entry}")
        after = entry.lstat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("Preparation entry changed while hashing")
        result.update(bytes.fromhex(identity([relative, mode, content])))
    return result.hexdigest()


def protected_inputs(root: Path, *, source_paths: Iterable[Path | str] = ()) -> dict[str, str]:
    """Bind mathematical inputs and declared source artifacts, excluding incidental build output."""
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs if name not in {".git", ".lake", ".leanflow", ".venv"}
        )
        for name in files:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if relative not in CONFIG and path.suffix in {
                ".lean",
                ".tex",
                ".ltx",
                ".bib",
                ".sty",
                ".cls",
            }:
                result[relative] = str(entry_identity(path))
    for supplied in source_paths:
        path = Path(supplied)
        path = path if path.is_absolute() else root / path
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        normalized_relative(relative)
        checksum = entry_identity(path)
        if checksum is None or not path.is_file():
            raise ValueError(
                "Declared preparation source artifact is missing or not an ordinary file"
            )
        result[relative] = checksum
    return result


def managed_path(relative: str, before: str | None) -> bool:
    """Allow disjoint dependency/build entries while preserving existing dependency Git metadata."""
    normalized_relative(relative)
    if relative in MANAGED or import_root_path(relative, before):
        return True
    return relative.startswith(".lake/") and ".git" not in relative.split("/")


def import_root_path(relative: str, before: str | None) -> bool:
    """Permit only originally absent ordinary Lean root paths beyond known infrastructure."""
    normalized_relative(relative)
    return (
        before is None
        and relative.endswith(".lean")
        and not any(part.startswith(".") for part in relative.split("/"))
    )


def validate_import_root(path: Path) -> None:
    """Reject declarations, commands, proof holes, and source rewriting in a setup-created root."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 64000:
        raise ValueError("Preparation roots must be bounded ordinary import-only Lean files")
    text = _strip_lean_comments_and_strings(path.read_text())
    if not re.fullmatch(r"\s*(?:import\s+[A-Za-z_][\w'.]*(?:[ \t]+[A-Za-z_][\w'.]*)*\s*)*", text):
        raise ValueError(
            "Preparation cannot create mathematical declarations; root files must contain imports only"
        )


def copy_entry(source: Path, target: Path, *, check: Callable[[], None] | None = None) -> str:
    """Copy and flush distinct inodes, requiring an unchanged complete source identity."""
    expected = entry_identity(source, check=check)
    if expected is None or source.is_symlink():
        raise ValueError("Preparation copies require existing ordinary entries")
    target.parent.mkdir(parents=True, exist_ok=True)

    def copy(current: Path, destination: Path) -> None:
        if check:
            check()
        if current.is_symlink():
            destination.symlink_to(os.readlink(current))
        elif current.is_dir():
            destination.mkdir(
                mode=stat.S_IMODE(current.stat().st_mode) | stat.S_IWUSR | stat.S_IXUSR
            )
            for child in sorted(current.iterdir()):
                copy(child, destination / child.name)
            destination.chmod(stat.S_IMODE(current.stat().st_mode))
        else:
            copy_file(current, destination, check=check)
        if check:
            check()

    copy(source, target)
    if (
        entry_identity(target, check=check) != expected
        or entry_identity(source, check=check) != expected
    ):
        raise ValueError("Preparation entry changed while copying its private version")

    def flush(entry: Path) -> None:
        if check:
            check()
        if entry.is_symlink():
            return
        if entry.is_dir():
            for child in sorted(entry.iterdir()):
                flush(child)
        descriptor = os.open(entry, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    flush(target)
    sync_directory(target.parent)
    return expected
