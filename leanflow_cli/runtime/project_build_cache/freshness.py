"""Retain cheap mutation evidence for exact inputs already hashed during controller setup."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from pathlib import Path


def _stamp(status: os.stat_result) -> tuple[int, ...]:
    """Identify replacement, relinking, chmod and same-size or restored-mtime rewrites."""
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        status.st_mode,
        status.st_nlink,
    )


class InputGuard:
    """Detect input mutations without reading compiled dependencies a second time.

    The sandbox can write only private root build outputs. Metadata comparisons
    supplement that confinement against concurrent external edits to hashed inputs.
    """

    def __init__(self) -> None:
        self.files: dict[Path, tuple[int, ...]] = {}
        self.directories: dict[Path, tuple[tuple[int, int], tuple[str, ...], frozenset[str]]] = {}

    def record_file(self, path: Path, before: os.stat_result) -> None:
        """Retain a file stamp only when the bytes were hashed without observed mutation."""
        after = path.lstat()
        if _stamp(before) != _stamp(after):
            raise ValueError("Project cache input changed while its content was hashed")
        self.files[path] = _stamp(after)

    def record_directory(
        self, path: Path, names: Sequence[str], *, excluded: set[str] | None = None
    ) -> None:
        """Retain the effective layout so added and removed inputs invalidate publication."""
        status = path.lstat()
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError(
                "Project cache input directories must remain private regular directories"
            )
        omitted = frozenset(excluded or ())
        expected = tuple(sorted(name for name in names if name not in omitted))
        actual = tuple(sorted(child.name for child in path.iterdir() if child.name not in omitted))
        if expected != actual:
            raise ValueError("Project cache input directory changed while it was fingerprinted")
        self.directories[path] = ((status.st_dev, status.st_ino), expected, omitted)

    def unchanged(self) -> bool:
        """Check all captured files and effective directory layouts without content rehashes."""
        try:
            for path, expected in self.files.items():
                if _stamp(path.lstat()) != expected:
                    return False
            for path, (inode, expected_names, omitted) in self.directories.items():
                status = path.lstat()
                if not stat.S_ISDIR(status.st_mode) or (status.st_dev, status.st_ino) != inode:
                    return False
                actual = tuple(
                    sorted(child.name for child in path.iterdir() if child.name not in omitted)
                )
                if actual != expected_names:
                    return False
            return True
        except OSError:
            return False
