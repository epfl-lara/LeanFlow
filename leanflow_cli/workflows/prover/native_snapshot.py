"""Build independent, filtered project copies for native agent workspaces."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from core.file_copy import copy_file
from core.utils import atomic_json_write
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
_RELEASED = ".leanflow-dependencies-released"


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
    if target.exists():
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=target.parent) as temporary:
        staged = Path(temporary) / "project"
        staged.mkdir()
        _copy_filtered(source, staged, target)
        staged.rename(target)
    return target


def _copy_filtered(source: Path, target: Path, destination: Path) -> None:
    """Clone permitted source and dependency bytes while excluding private controller paths."""
    for directory, names, files in os.walk(source, followlinks=False):
        current = Path(directory)
        relative = current.relative_to(source)
        names[:] = [
            name
            for name in names
            if name not in _EXCLUDED
            and not (current / name).is_symlink()
            and not (current / name).resolve().is_relative_to(destination)
            and not target.is_relative_to((current / name).resolve())
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
            copy_file(item, output / name)


def prepare_snapshot(project_root: Path, destination: Path) -> Path:
    """Create a private project or restore released dependencies before a resumed session."""
    if destination.is_symlink() or (destination / ".lake").is_symlink():
        raise ValueError("A private snapshot cannot link its project or cache to another directory")
    if not destination.exists():
        return create_snapshot(project_root, destination)
    marker = destination / _RELEASED
    if not marker.exists():
        return destination
    canonical = json.loads((project_root / "lake-manifest.json").read_text())
    saved = json.loads((destination / "lake-manifest.json").read_text())
    available = {item["name"]: item for item in canonical.get("packages", [])}
    if any(available.get(item["name"]) != item for item in saved.get("packages", [])):
        raise ValueError(
            "Saved dependency revisions changed; restore them before resuming this job"
        )
    source = project_root / ".lake/packages"
    target = destination / ".lake/packages"
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Canonical dependency cache is missing; restore it before resuming")
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dependencies-", dir=target.parent) as temporary:
        staged = Path(temporary) / "packages"
        staged.mkdir()
        _copy_filtered(source, staged, target)
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        staged.rename(target)
    marker.unlink()
    return destination


def release_snapshot_dependencies(snapshot: Path) -> None:
    """Drop rebuildable library copies after a session while retaining proof and scratch files."""
    if snapshot.is_symlink():
        raise ValueError("Cannot release dependencies through a linked snapshot")
    cache = snapshot / ".lake"
    if cache.is_symlink():
        cache.unlink()
        return
    packages = cache / "packages"
    if not packages.exists() and not packages.is_symlink():
        return
    # The marker also recovers interrupted cleanup: a partial cache is never reused.
    atomic_json_write(snapshot / _RELEASED, {"restore_pinned_dependencies": True})
    if packages.is_symlink():
        packages.unlink()
    else:
        shutil.rmtree(packages)
