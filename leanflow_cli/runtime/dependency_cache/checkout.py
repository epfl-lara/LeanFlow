"""Validate pinned package sources and clone immutable cache bytes privately."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from core.file_copy import copy_file
from leanflow_cli.runtime.storage import ensure_storage_capacity, storage_policy
from tools.utilities.repository_research_policy import clean_room_path_block_reason

_EXCLUDED = {
    ".leanflow",
    ".codex",
    ".claude",
    ".lemex",
    "node_modules",
    ".venv",
    "__pycache__",
    ".mcp.json",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _git(checkout: Path, *arguments: str) -> str:
    """Read checkout metadata without network access or inherited Git overrides."""
    result = subprocess.run(
        [
            "git",
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(checkout),
            *arguments,
        ],
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"Cannot validate installed dependency {checkout.name}")
    return result.stdout.strip()


def _validate_packages(root: Path, identity: dict[str, Any]) -> bool:
    """Require exact clean checkouts and existing compiled artifacts before publication."""
    packages = root / ".lake/packages"
    if not packages.is_dir():
        return False
    if {path.name for path in packages.iterdir()} != {
        item["name"] for item in identity["packages"]
    }:
        raise ValueError("Installed dependency packages must exactly match the locked closure")
    compiled = False
    for package in identity["packages"]:
        checkout = packages / package["name"]
        if checkout.is_symlink() or not checkout.is_dir():
            raise ValueError(f"Missing private checkout for dependency {package['name']}")
        git = checkout / ".git"
        if not git.is_dir() or git.is_symlink():
            raise ValueError("Git worktree pointers cannot be published as shared dependencies")
        if (git / "objects/info/alternates").exists():
            raise ValueError("Shared dependencies cannot use external Git object stores")
        lines = _git(checkout, "rev-parse", "--show-toplevel", "HEAD").splitlines()
        if (
            len(lines) != 2
            or Path(lines[0]).resolve() != checkout.resolve()
            or lines[1] != package["rev"]
        ):
            raise ValueError(f"Dependency {package['name']} differs from its locked revision")
        if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError(
                f"Dependency {package['name']} has modified tracked or untracked source files"
            )
        for directory, names, files in os.walk(checkout, followlinks=False):
            current = Path(directory)
            for name in (*names, *files):
                path = current / name
                if path.is_symlink():
                    _relative_link(path, checkout)
            if (
                ".lake" in current.relative_to(checkout).parts
                and "build" in current.relative_to(checkout).parts
            ):
                compiled = compiled or any(name.endswith(".olean") for name in files)
                compiled = compiled or (current.name == "bin" and bool(files))
    return compiled


def _relative_link(path: Path, checkout: Path) -> str:
    """Accept portable relative links only within the same locked checkout."""
    target = os.readlink(path)
    try:
        if Path(target).is_absolute() or not path.resolve(strict=True).is_relative_to(
            checkout.resolve()
        ):
            raise ValueError("Shared dependencies cannot contain symlink escapes")
    except (OSError, RuntimeError) as exc:
        raise ValueError("Shared dependencies contain an unresolved symlink") from exc
    return target


def _copy_packages(
    source: Path,
    destination: Path,
    *,
    include_git: bool,
    policy_root: Path | None = None,
    filter_sources: bool = False,
) -> None:
    """Clone independent inodes while preserving reserve and native source filtering."""
    destination.mkdir()
    filtered = filter_sources or not include_git
    minimum = storage_policy().min_free_bytes
    for directory, names, files in os.walk(source, followlinks=False):
        current = Path(directory)
        relative = current.relative_to(source)
        checkout = source / relative.parts[0] if relative.parts else source
        output = destination / relative
        output.mkdir(parents=True, exist_ok=True)
        permitted = []
        for name in names:
            path = current / name
            if (filtered and name in _EXCLUDED) or (not include_git and name == ".git"):
                continue
            logical = (
                policy_root / ".lake/packages" / path.relative_to(source) if policy_root else path
            )
            if filtered and (
                path.is_symlink() or clean_room_path_block_reason(logical, cwd=policy_root)
            ):
                continue
            if path.is_symlink():
                if not relative.parts:
                    raise ValueError("Shared dependency package roots cannot be symlinks")
                (output / name).symlink_to(_relative_link(path, checkout), target_is_directory=True)
            else:
                permitted.append(name)
        names[:] = permitted
        for name in files:
            item = current / name
            if (filtered and name in _EXCLUDED) or (not include_git and name == ".git"):
                continue
            logical = (
                policy_root / ".lake/packages" / item.relative_to(source) if policy_root else item
            )
            if filtered and (
                item.is_symlink() or clean_room_path_block_reason(logical, cwd=policy_root)
            ):
                continue
            if include_git and name == ".git":
                raise ValueError(
                    "Private dependencies cannot retain external Git worktree pointers"
                )
            copied = output / name
            if item.is_symlink():
                copied.symlink_to(_relative_link(item, checkout))
                continue
            if not item.is_file():
                raise ValueError("Shared dependencies contain an unsupported special file")
            # CoW normally costs metadata only. Check each source file's logical
            # size so an unsupported-filesystem fallback still preserves reserve.
            if shutil.disk_usage(output).free < minimum + item.stat().st_size:
                ensure_storage_capacity(output, required_bytes=item.stat().st_size)
            copy_file(item, copied)
            copied.chmod(copied.stat().st_mode | stat.S_IWUSR)


def _seal(packages: Path) -> None:
    """Remove writable permission bits from cached dependency bytes and directories."""
    for directory, _, files in os.walk(packages, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(path.stat().st_mode & ~0o222)
        path = Path(directory)
        path.chmod(path.stat().st_mode & ~0o222)


def _remove_stage(stage: Path) -> None:
    """Remove owned staging bytes even after an interrupted sealing operation."""
    for directory, names, _ in os.walk(stage, followlinks=False):
        current = Path(directory)
        current.chmod(current.stat().st_mode | stat.S_IWUSR)
        for name in names:
            child = current / name
            if not child.is_symlink():
                child.chmod(child.stat().st_mode | stat.S_IWUSR)
    shutil.rmtree(stage)
