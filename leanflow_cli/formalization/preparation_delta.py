"""Select disjoint changed infrastructure leaves instead of replacing healthy dependency trees."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from leanflow_cli.formalization.preparation_fs_inputs import CONFIG, entry_identity


def _git_metadata(relative: str) -> bool:
    return ".git" in relative.split("/")


def infrastructure_baseline(
    root: Path, *, check: Callable[[], None] | None = None
) -> dict[str, str | None]:
    """Capture existing config and dependency leaves before private preparation begins."""
    result = {name: entry_identity(root / name, check=check) for name in sorted(CONFIG)}
    result[".leanflow/project.yaml"] = entry_identity(root / ".leanflow/project.yaml", check=check)
    lake = root / ".lake"
    if lake.is_symlink():
        raise ValueError("Preparation dependency root cannot be aliased")
    if not lake.exists():
        result[".lake"] = None
        return result
    for current, directories, files in os.walk(lake, followlinks=False):
        directories[:] = sorted(name for name in directories if name != ".git")
        linked = [name for name in directories if (Path(current) / name).is_symlink()]
        directories[:] = [name for name in directories if name not in linked]
        for name in sorted([*files, *linked]):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            result[relative] = entry_identity(path, check=check, link_root=lake)
    return result


def managed_delta(
    root: Path,
    staged: Path,
    baseline: Mapping[str, str | None],
    *,
    check: Callable[[], None] | None = None,
) -> dict[str, str | None]:
    """Return changed leaves and absent subtrees, preserving canonical package Git configuration.

    Every existing leaf uses its pre-preparation hash, so an editor's intervening
    save cannot be adopted. Missing staged entries are left untouched. The legacy
    whole-tree baseline is supported with one independent baseline check.
    """
    original = dict(baseline)
    if original.get(".lake") is not None:
        if entry_identity(root / ".lake", check=check) != original[".lake"]:
            raise ValueError("Infrastructure changed during preparation")
        original = {**original, **infrastructure_baseline(root, check=check)}
        original.pop(".lake", None)
    result = {
        name: original.get(name)
        for name in sorted(CONFIG | {".leanflow/project.yaml"})
        if (staged / name).exists()
        and entry_identity(staged / name, check=check) != original.get(name)
    }

    def visit(path: Path) -> None:
        if check:
            check()
        relative = path.relative_to(staged).as_posix()
        if _git_metadata(relative):
            return
        canonical = root / relative
        if path.is_symlink():
            if (
                relative in original
                and entry_identity(path, check=check, link_root=staged / ".lake")
                == original[relative]
            ):
                return
            raise ValueError("Preparation delta cannot publish aliased infrastructure")
        if path.is_dir():
            existed = any(key == relative or key.startswith(relative + "/") for key in original)
            if not existed and not canonical.exists() and not canonical.is_symlink():
                result[relative] = None
                return
            for child in sorted(path.iterdir()):
                visit(child)
        elif entry_identity(path, check=check) != original.get(relative):
            result[relative] = original.get(relative)

    if (staged / ".lake").exists():
        visit(staged / ".lake")
    return result
