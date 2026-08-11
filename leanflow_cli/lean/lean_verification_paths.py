"""Resolve the Lean project that owns an explicit verification target."""

from __future__ import annotations

from pathlib import Path

from leanflow_cli.workflows.project import find_lean_project_root


def verification_project_root(
    target_path: Path | None,
    discovered_root: Path | None,
) -> Path | None:
    """Return the target-owning project root for a canonical verification.

    Tool calls can carry a shell ``cwd`` outside the active nested Lean project.
    An explicit Lean target is stronger evidence for file/module verification;
    otherwise ``file_exact`` silently degrades to a project build in the wrong
    directory.
    """
    if target_path is None:
        return discovered_root
    target_root = find_lean_project_root(target_path.parent)
    if target_root is None:
        return discovered_root
    if discovered_root is None:
        return target_root
    try:
        target_path.relative_to(discovered_root)
    except ValueError:
        return target_root
    return discovered_root
