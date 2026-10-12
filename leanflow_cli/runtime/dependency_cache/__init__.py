"""Share pinned Lean dependencies through sealed caches and writable private copies."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from core.home import leanflow_home
from leanflow_cli.runtime.dependency_cache.checkout import (
    _copy_packages,
    _remove_stage,
    _seal,
    _validate_packages,
)
from leanflow_cli.runtime.dependency_cache.identity import (
    _canonical_root,
    _identity,
    _key,
)
from leanflow_cli.runtime.storage import (
    ensure_storage_capacity,
    lease_entry,
    register_entry,
    storage_lock,
    touch_entry,
)
from leanflow_cli.runtime.storage.entries import read_entry


def dependency_cache_key(project_root: Path) -> str | None:
    """Return the versioned compatibility key without fetching or building anything."""
    identity = _identity(_canonical_root(project_root))
    return _key(identity) if identity is not None else None


def _cache_root() -> Path:
    """Resolve the one managed cache location and reject linked ownership boundaries."""
    root = leanflow_home().expanduser().resolve() / "cache/dependencies/v1"
    for path in (root, root.parent, root.parent.parent):
        if path.is_symlink():
            raise ValueError("Shared dependency cache directories cannot be symlinks")
    return root


def _valid_entry(entry: Path, identity: dict[str, Any]) -> bool:
    """Check publication identity before exposing managed cached bytes."""
    if entry.is_symlink():
        raise ValueError("Shared dependency entries cannot be symlinks")
    if not entry.exists():
        return False
    record = read_entry(entry)
    if (
        record is None
        or record.get("kind") != "dependency"
        or record.get("identity") != identity
        or record.get("key") != entry.name
    ):
        raise ValueError("Shared dependency cache identity is inconsistent")
    if (entry / "packages").is_symlink() or not (entry / "packages").is_dir():
        raise ValueError("Shared dependency cache packages are invalid")
    return True


def ensure_dependency_entry(project_root: Path) -> Path | None:
    """Publish ready canonical dependencies atomically, or reuse an exact existing entry.

    Never fetch libraries or publish a private agent workspace. Source-only projects
    return None; callers publish after their normal Lean protocol readiness check.
    """
    root = _canonical_root(project_root)
    identity = _identity(root)
    if identity is None:
        return None
    entry = _cache_root() / _key(identity)
    ensure_storage_capacity(root)
    with storage_lock():
        if _valid_entry(entry, identity):
            # An existing entry must not hide local edits in a canonical project.
            # Missing/source-only installations can reuse the pinned ready entry.
            if (root / ".lake/packages").is_dir() and any((root / ".lake/packages").iterdir()):
                _validate_packages(root, identity)
            touch_entry(entry)
            return entry
        if not _validate_packages(root, identity):
            return None
        entry.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=entry.parent))
        register_entry(stage, kind="staging")
        try:
            with lease_entry(stage):
                _copy_packages(root / ".lake/packages", stage / "packages", include_git=True)
                # A checkout edited while copying must not become a trusted cache.
                if _identity(root) != identity or not _validate_packages(root, identity):
                    raise ValueError("Canonical dependency inputs changed during publication")
                _seal(stage / "packages")
                stage.rename(entry)
                try:
                    register_entry(entry, kind="dependency", identity=identity)
                except BaseException:
                    _remove_stage(entry)
                    raise
        finally:
            if stage.exists():
                _remove_stage(stage)
        return entry


def materialize_dependencies(
    project_root: Path,
    destination_packages: Path,
    *,
    include_git: bool = True,
    seed: bool = True,
    filter_sources: bool = False,
) -> bool:
    """Publish a writable private dependency view without downloads or shared inodes.

    Set seed=False during project setup to reuse only an already verified cache.
    Snapshot callers filter private sources but retain Git metadata so Lake can
    recognize the installed package URLs and revisions without replacing them.
    An existing destination is never replaced implicitly.
    """
    root = _canonical_root(project_root)
    identity = _identity(root)
    if identity is None:
        return False
    if destination_packages.exists() or destination_packages.is_symlink():
        raise FileExistsError(str(destination_packages))
    if any(parent.is_symlink() for parent in (destination_packages, *destination_packages.parents)):
        raise ValueError("Private dependency destinations cannot traverse linked directories")
    entry = _cache_root() / _key(identity)
    with storage_lock():
        if seed:
            if ensure_dependency_entry(root) is None:
                return False
        elif not _valid_entry(entry, identity):
            return False
        with lease_entry(entry):
            touch_entry(entry)
            destination_packages.parent.mkdir(parents=True, exist_ok=True)
            ensure_storage_capacity(destination_packages.parent)
            with tempfile.TemporaryDirectory(
                prefix=".dependencies-", dir=destination_packages.parent
            ) as temporary:
                staged = Path(temporary) / "packages"
                _copy_packages(
                    entry / "packages",
                    staged,
                    include_git=include_git,
                    policy_root=root,
                    filter_sources=filter_sources,
                )
                staged.rename(destination_packages)
    return True


def validate_dependency_source(project_root: Path) -> bool:
    """Recognize only disposable, exact pinned source checkouts without build outputs.

    Unexpected packages, source edits, and untracked user files prevent replacement.
    Missing packages are eligible for lookup restoration without any download.
    """
    root = _canonical_root(project_root)
    identity = _identity(root)
    if identity is None:
        return False
    packages = root / ".lake/packages"
    if not packages.exists():
        return True
    if not packages.is_dir():
        return False
    children = list(packages.iterdir())
    if not children:
        return True
    if {item.name for item in children} != {item["name"] for item in identity["packages"]}:
        return False
    try:
        if _validate_packages(root, identity):
            return False
        for package in children:
            build = package / ".lake/build"
            if build.exists() and any(item.is_file() for item in build.rglob("*")):
                return False
    except (ValueError, OSError, subprocess.SubprocessError):
        return False
    return True


def restore_project_dependencies(project_root: Path) -> bool:
    """Restore an exact cached build only into absent or disposable source checkouts.

    The caller holds its project setup lock. Copy first, recheck source admission,
    and retain the old directory until the complete replacement is published.
    """
    root = _canonical_root(project_root)
    if not validate_dependency_source(root):
        return False
    packages = root / ".lake/packages"
    packages.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=packages.parent) as temporary:
        staging = Path(temporary)
        copied = staging / "packages"
        if not materialize_dependencies(root, copied, seed=False):
            return False
        if not validate_dependency_source(root):
            return False
        previous = staging / "previous"
        if packages.exists():
            packages.rename(previous)
        try:
            copied.rename(packages)
        except BaseException:
            if previous.exists():
                previous.rename(packages)
            raise
    return True
