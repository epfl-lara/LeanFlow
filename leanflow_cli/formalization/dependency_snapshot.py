"""Freeze exact admitted dependency bytes without substituting a compatibility cache."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.file_copy import copy_file
from leanflow_cli.formalization.dependency_files import resolve_dependency_file
from leanflow_cli.formalization.dependency_inputs import dependency_inputs
from leanflow_cli.formalization.dependency_modules import output_paths
from leanflow_cli.formalization.environment import environment_identity
from leanflow_cli.formalization.evidence import contained
from leanflow_cli.formalization.lean_layout import inspect_layout
from leanflow_cli.runtime.storage import (
    ensure_storage_capacity,
    register_workspace,
    storage_policy,
)
from leanflow_cli.workflows.prover.native_snapshot import _EXCLUDED, _validate_destination
from tools.utilities.repository_research_policy import clean_room_path_block_reason


def _package_roots(root: Path, inventory: dict[str, Any]) -> dict[Path, Path]:
    """Locate exactly the already-admitted closure without rereading mutable metadata."""
    result: dict[Path, Path] = {}
    for package in inventory["packages"].values():
        path = contained(root, package["directory"])
        relative = path.relative_to(root)
        if any(part in _EXCLUDED for part in relative.parts):
            raise ValueError(f"Admitted dependency overlaps private project state: {relative}")
        if path in result or any(path in old.parents or old in path.parents for old in result):
            raise ValueError("Admitted dependency directories must not overlap")
        result[path] = relative
    return result


def _copy_file(source: Path, target: Path, root: Path, minimum: int) -> None:
    """Copy isolated bytes or fail when policy would remove an admitted resource."""
    reason = clean_room_path_block_reason(source, cwd=root)
    if reason:
        raise ValueError(f"Formalization snapshot cannot omit an input: {reason}")
    if shutil.disk_usage(target.parent).free < minimum + source.stat().st_size:
        ensure_storage_capacity(target.parent, required_bytes=source.stat().st_size)
    copy_file(source, target)


def _copy_package(source: Path, target: Path, root: Path, minimum: int) -> None:
    """Copy every admitted package resource except untrusted Git bookkeeping."""
    target.mkdir(parents=True)
    for directory, names, files in os.walk(source, followlinks=False):
        current = Path(directory)
        names[:] = sorted(name for name in names if current != source or name != ".git")
        output = target / current.relative_to(source)
        for name in [*names, *sorted(files)]:
            if current == source and name == ".git":
                continue
            path = current / name
            mode = path.lstat().st_mode
            reason = clean_room_path_block_reason(path, cwd=root)
            if reason:
                raise ValueError(f"Formalization snapshot cannot omit an admitted input: {reason}")
            if stat.S_ISDIR(mode):
                (output / name).mkdir()
            else:
                resolved = resolve_dependency_file(path, source)
                if not stat.S_ISREG(resolved.lstat().st_mode):
                    raise ValueError(f"Admitted dependency input is aliased or nonregular: {path}")
                _copy_file(resolved, output / name, root, minimum)


def _copy_project(
    source: Path, target: Path, destination: Path, packages: dict[Path, Path], build: Path
) -> None:
    """Keep ordinary project isolation while copying admitted packages without filtering."""
    minimum = storage_policy().min_free_bytes
    for directory, names, files in os.walk(source, followlinks=False):
        current = Path(directory)
        relative = current.relative_to(source)
        output = target / relative
        output.mkdir(parents=True, exist_ok=True)
        descend = []
        for name in sorted(names):
            path = current / name
            if path in packages:
                _copy_package(path, target / packages[path], source, minimum)
                continue
            excluded = (
                name in _EXCLUDED
                or path.is_symlink()
                or path.resolve().is_relative_to(destination)
                or target.is_relative_to(path.resolve())
                or (relative == Path(".lake") and name == "build")
                or path == build
            )
            if excluded:
                if any(path == package or path in package.parents for package in packages):
                    raise ValueError(
                        f"Admitted dependency overlaps an excluded snapshot path: {path}"
                    )
                continue
            reason = clean_room_path_block_reason(path, cwd=source)
            if reason:
                raise ValueError(f"Formalization snapshot cannot omit an input: {reason}")
            descend.append(name)
        names[:] = descend
        for name in sorted(files):
            path = current / name
            if name in _EXCLUDED or path.is_symlink() or not path.is_file():
                continue
            _copy_file(path, output / name, source, minimum)


def create_formalization_snapshot(
    project_root: Path,
    destination: Path,
    *,
    environment: Callable[[Path], str] | None = None,
    expected_environment: str | None = None,
) -> Path:
    """Publish a private copy only after canonical and frozen input identities agree.

    Compiled dependencies are evidence inputs, not interchangeable build-cache hits.
    The supplied environment callback is the same authority used by the calling
    controller, including its compiler and policy fingerprints.
    """
    _validate_destination(destination)
    source = project_root.resolve(strict=True)
    target = destination.resolve()
    if target == source:
        raise ValueError("A formalization snapshot must differ from the canonical project")
    if target.exists():
        raise FileExistsError(str(target))
    identity = environment or environment_identity
    dependencies = dependency_inputs(source)
    packages = _package_roots(source, dependencies)
    layout = inspect_layout(source)
    if not layout.get("accepted"):
        raise ValueError(layout.get("error", "Unsupported frozen build layout"))
    build, _ = output_paths(source, layout, list(packages))
    if any(target.is_relative_to(package) for package in packages):
        raise ValueError("A formalization snapshot cannot be created inside an admitted dependency")
    canonical_environment = identity(source)
    if expected_environment is not None and canonical_environment != expected_environment:
        raise ValueError("Canonical formalization compiler/dependency environment changed")
    target.parent.mkdir(parents=True, exist_ok=True)
    ensure_storage_capacity(target.parent)
    with tempfile.TemporaryDirectory(prefix=".formalization-snapshot-", dir=target.parent) as temp:
        staged = Path(temp) / "project"
        staged.mkdir()
        _copy_project(source, staged, target, packages, build)
        if dependency_inputs(staged) != dependencies or dependency_inputs(source) != dependencies:
            raise ValueError("Formalization dependency inputs changed while freezing evidence")
        if identity(staged) != canonical_environment or identity(source) != canonical_environment:
            raise ValueError("Formalization compiler/dependency environment changed while freezing")
        if target.exists() or target.is_symlink():
            raise FileExistsError(str(target))
        staged.rename(target)
    register_workspace(target)
    return target
