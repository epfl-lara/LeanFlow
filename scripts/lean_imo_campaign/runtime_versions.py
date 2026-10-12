"""Resolve immutable per-cell runtime versions without changing active experiments."""

from pathlib import Path
from typing import Any


def runtime_directory(directory: Path, cell: dict[str, Any]) -> Path:
    """Return a cell's pinned snapshot, defaulting to the original campaign runtime."""
    return Path(cell.get("runtime_directory", directory)).resolve()
