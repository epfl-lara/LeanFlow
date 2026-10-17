"""Capture canonical source/configuration bytes for formalization evidence."""

from __future__ import annotations

import os
from pathlib import Path

_SKIP = {
    ".lake",
    ".git",
    ".leanflow",
    ".leanflow-runtime",
    ".leanflow-job",
    ".venv",
    "node_modules",
}
_CONFIG = {"lakefile.lean", "lakefile.toml", "lean-toolchain", "lake-manifest.json"}


def source_files(root: Path) -> dict[str, bytes]:
    """Capture project source/config bytes without following source aliases."""
    files: dict[str, bytes] = {}
    for directory, names, entries in os.walk(root, followlinks=False):
        parent = Path(directory)
        names[:] = [
            name for name in names if name not in _SKIP and not (parent / name).is_symlink()
        ]
        for name in entries:
            path = parent / name
            if not (path.suffix == ".lean" or path.name in _CONFIG or path.name == "Blueprint.md"):
                continue
            if path.is_symlink():
                raise ValueError(f"Source snapshots cannot contain file symlinks: {path}")
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files
