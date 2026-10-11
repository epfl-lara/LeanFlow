"""Build independent, filtered project copies for native agent workspaces."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from tools.utilities.repository_research_policy import clean_room_path_block_reason

_EXCLUDED = {
    ".git",
    ".leanflow",
    ".codex",
    ".lemex",
    ".claude",
    ".mcp.json",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def remap_project_text(
    text: str,
    source_root: Path,
    snapshot_root: Path,
    *,
    replacements: Mapping[Path, Path] | None = None,
) -> str:
    """Map original path prefixes once, preserving already-private paths and sibling roots."""
    paths = {
        str(source_root): str(snapshot_root),
        **{str(before): str(after) for before, after in (replacements or {}).items()},
        str(snapshot_root): str(snapshot_root),
    }
    prefixes = "|".join(re.escape(path) for path in sorted(paths, key=len, reverse=True))
    # A following name character denotes a different root, e.g. project2. The
    # replacement callback never revisits inserted paths nested below source_root.
    boundary = r"(?=$|[/\\\s\"'`<>()\[\]{},;:!?])"
    return re.sub(f"(?:{prefixes}){boundary}", lambda match: paths[match.group()], text)


def create_snapshot(project_root: Path, destination: Path) -> Path:
    """Copy permitted files without links to canonical sources or controller state.

    Dependencies retain their compiled artifacts; the project's own build products
    are excluded so previously compiled target proofs cannot bypass source filtering.
    """
    source = project_root.resolve(strict=True)
    target = destination.resolve()
    if target == source:
        raise ValueError("A native agent snapshot must differ from the canonical project")
    target.mkdir(parents=True, exist_ok=False)
    for directory, names, files in os.walk(source, followlinks=False):
        current = Path(directory)
        relative = current.relative_to(source)
        names[:] = [
            name
            for name in names
            if name not in _EXCLUDED
            and not (current / name).is_symlink()
            and not (current / name).resolve().is_relative_to(target)
            and not clean_room_path_block_reason(str(current / name), cwd=source)
            and not (relative == Path(".lake") and name == "build")
        ]
        output = target / relative
        output.mkdir(parents=True, exist_ok=True)
        for name in files:
            item = current / name
            if (
                name in _EXCLUDED
                or item.is_symlink()
                or not item.is_file()
                or clean_room_path_block_reason(str(item), cwd=source)
            ):
                continue
            # copy2 creates a distinct inode even when the input was a hardlink.
            shutil.copy2(item, output / name)
    return target
