"""Build independent, filtered project copies for native agent workspaces."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from core.file_copy import copy_file
from core.regular_files import read_regular_bytes
from core.utils import atomic_json_write
from leanflow_cli.runtime.dependency_cache import materialize_dependencies
from leanflow_cli.runtime.dependency_cache.checkout import _copy_packages
from leanflow_cli.runtime.storage import (
    ensure_storage_capacity,
    forget_workspace,
    register_workspace,
    storage_lock,
    storage_policy,
)
from leanflow_cli.runtime.storage.process_views import process_views
from tools.utilities.repository_research_policy import clean_room_path_block_reason

from .native_snapshot_publication import (
    PUBLICATION_EXCLUSION,
    exclude_publication_file,
    publication_documents,
)

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


class SnapshotProvenanceError(ValueError):
    """Require a fresh job workspace while preserving a view with unverifiable provenance."""


def _exclusion_record(destination: Path) -> Path:
    """Keep snapshot provenance outside the native agent's writable project."""
    return destination.parent / (".leanflow-snapshot-" + destination.name + "-build-exclusion.json")


def _exclusion_identity(
    destination: Path, build_dir: str, *, staged: Path | None = None
) -> dict[str, object]:
    """Bind evaluated cache exclusion to the controller-created directory inode."""
    observed = (staged or destination).stat()
    return {
        "schema_version": 2,
        "project": str(destination.resolve()),
        "excluded_build_dir": build_dir,
        "device": observed.st_dev,
        "inode": observed.st_ino,
        "excluded_publication_authority": PUBLICATION_EXCLUSION,
    }


def _assert_exclusion(destination: Path, build_dir: str) -> None:
    """Refuse an old view whose own-cache exclusion was never recorded."""
    record = _exclusion_record(destination)
    try:
        if record.is_symlink():
            raise ValueError("Snapshot exclusion provenance is aliased")
        value = json.loads(read_regular_bytes(record, 64 * 1024))
        if value != _exclusion_identity(destination, build_dir):
            raise ValueError(
                "Snapshot exclusion provenance differs from the evaluated build directory"
            )
    except (OSError, ValueError) as error:
        raise SnapshotProvenanceError(
            "Existing native snapshot lacks exact build-exclusion provenance or publication-exclusion provenance; preserve it and start a fresh proof job"
        ) from error


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


def create_snapshot(
    project_root: Path,
    destination: Path,
    *,
    excluded_build_dir: str | None = None,
    cancelled: Callable[[], bool] | None = None,
    deadline: float | None = None,
) -> Path:
    """Copy permitted files without links to canonical sources or controller state.

    Dependencies retain their compiled artifacts; the project's own build products
    are excluded so previously compiled target proofs cannot bypass source filtering.
    """
    from leanflow_cli.formalization.publication import publication_guard

    _validate_destination(destination)
    source = project_root.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("A native snapshot requires an existing canonical project directory")
    # Canonical publication ownership precedes global storage ownership in every
    # entry point; the model copy itself never receives that approval authority.
    with publication_guard(source, cancelled=cancelled, deadline=deadline):
        if excluded_build_dir is not None:
            with storage_lock():
                return _create_snapshot(
                    project_root, destination, excluded_build_dir=excluded_build_dir
                )
        return _create_snapshot(project_root, destination)


def _publish_exclusion(destination: Path, build_dir: str, staged: Path) -> None:
    """Flush exact staged-inode provenance before the project directory becomes visible."""
    from leanflow_cli.formalization.evidence import sync_directory

    record = _exclusion_record(destination)
    value = _exclusion_identity(destination, build_dir, staged=staged)
    with tempfile.NamedTemporaryFile(dir=record.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write((json.dumps(value, sort_keys=True) + "\n").encode())
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, record, follow_symlinks=False)
        sync_directory(record.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _preserve_unpublished_exclusion(destination: Path) -> None:
    """Retain orphan provenance before retrying a copy whose destination never appeared."""
    from leanflow_cli.formalization.evidence import sync_directory
    from leanflow_cli.formalization.publication_io import rename_exclusive

    record = _exclusion_record(destination)
    if not record.exists() and not record.is_symlink():
        return
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Existing native view must be preserved")
    retained = record.with_name(record.name + ".unpublished-" + uuid.uuid4().hex)
    rename_exclusive(record, retained)
    sync_directory(record.parent)


def _create_snapshot(
    project_root: Path, destination: Path, *, excluded_build_dir: str | None = None
) -> Path:
    """Copy one owned view, preserving any competing directory or exclusion record."""
    _validate_destination(destination)
    source = project_root.resolve(strict=True)
    if excluded_build_dir is not None:
        from leanflow_cli.formalization.evidence import contained

        build = contained(source, excluded_build_dir)
        packages = source / ".lake/packages"
        if (
            build == source
            or build == packages
            or build in packages.parents
            or packages in build.parents
        ):
            raise ValueError("Private snapshot build exclusion overlaps dependency inputs")
    target = destination.resolve()
    if target == source:
        raise ValueError("A native agent snapshot must differ from the canonical project")
    if target.exists():
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    if excluded_build_dir is not None:
        _preserve_unpublished_exclusion(target)
    ensure_storage_capacity(target.parent)
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=target.parent) as temporary:
        staged = Path(temporary) / "project"
        staged.mkdir()
        shared = _shared_dependencies(source, staged / ".lake/packages")
        _copy_filtered(
            source, staged, target, skip_packages=shared, excluded_build_dir=excluded_build_dir
        )
        if excluded_build_dir is not None:
            from leanflow_cli.formalization.evidence import sync_directory
            from leanflow_cli.formalization.publication_io import rename_exclusive

            _publish_exclusion(target, excluded_build_dir, staged)
            rename_exclusive(staged, target)
            sync_directory(target.parent)
        else:
            staged.rename(target)
    register_workspace(target)
    return target


def _copy_filtered(
    source: Path,
    target: Path,
    destination: Path,
    *,
    skip_packages: bool = False,
    excluded_build_dir: str | None = None,
) -> None:
    """Clone permitted source and dependency bytes while excluding private controller paths."""
    minimum = storage_policy().min_free_bytes
    documents = publication_documents(source)
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
            and (relative / name).as_posix() != excluded_build_dir
            and not (skip_packages and relative == Path(".lake") and name == "packages")
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
                or exclude_publication_file(relative / name, documents)
            ):
                continue
            if shutil.disk_usage(output).free < minimum + item.stat().st_size:
                ensure_storage_capacity(output, required_bytes=item.stat().st_size)
            copy_file(item, output / name)


def prepare_snapshot(
    project_root: Path,
    destination: Path,
    *,
    excluded_build_dir: str | None = None,
    cancelled: Callable[[], bool] | None = None,
    deadline: float | None = None,
) -> Path:
    """Create a private project or restore released dependencies before a resumed session."""
    _validate_destination(destination)
    from leanflow_cli.formalization.publication import publication_guard

    source = project_root.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("A native snapshot requires an existing canonical project directory")
    # Acquire P before storage even when an existing view resumes. Nested create
    # uses same-thread reentrant ownership rather than reversing these locks.
    with publication_guard(source, cancelled=cancelled, deadline=deadline):
        with storage_lock():
            return _prepare_snapshot(
                project_root, destination, excluded_build_dir=excluded_build_dir
            )


def _prepare_snapshot(
    project_root: Path, destination: Path, *, excluded_build_dir: str | None = None
) -> Path:
    """Prepare a view while holding the storage ownership lock."""
    if destination.is_symlink() or (destination / ".lake").is_symlink():
        raise ValueError("A private snapshot cannot link its project or cache to another directory")
    if not destination.exists():
        return create_snapshot(project_root, destination, excluded_build_dir=excluded_build_dir)
    if excluded_build_dir is not None:
        _assert_exclusion(destination, excluded_build_dir)
    register_workspace(destination)
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
    ensure_storage_capacity(target.parent)
    with tempfile.TemporaryDirectory(prefix=".dependencies-", dir=target.parent) as temporary:
        staged = Path(temporary) / "packages"
        if not _shared_dependencies(project_root, staged):
            staged.mkdir()
            _copy_filtered(source, staged, target)
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        staged.rename(target)
    marker.unlink()
    register_workspace(destination)
    return destination


def _validate_destination(destination: Path) -> None:
    """Reject linked workspace ownership boundaries before resolving or creating paths."""
    if any(path.is_symlink() for path in (destination, *destination.absolute().parents)):
        raise ValueError("A private snapshot cannot traverse a linked destination")


def _shared_dependencies(project_root: Path, destination: Path) -> bool:
    """Reuse compatible sealed dependencies while keeping edited installations private."""
    try:
        if materialize_dependencies(project_root, destination, filter_sources=True):
            return True
    except ValueError:
        # Canonical dependency edits and unsupported layouts retain the established
        # filtered private-copy path; they must never be silently replaced by a cache.
        pass
    source = project_root / ".lake/packages"
    if not source.is_dir() or source.is_symlink():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    _copy_packages(
        source, destination, include_git=True, filter_sources=True, policy_root=project_root
    )
    return True


def release_snapshot_dependencies(snapshot: Path) -> None:
    """Drop rebuildable library copies after a session while retaining proof and scratch files."""
    with storage_lock():
        _release_snapshot_dependencies(snapshot)


def _release_snapshot_dependencies(snapshot: Path) -> None:
    """Release under ownership lock, deferring views still used by background children."""
    if snapshot.is_symlink():
        raise ValueError("Cannot release dependencies through a linked snapshot")
    cache = snapshot / ".lake"
    if cache.is_symlink():
        cache.unlink()
        return
    packages = cache / "packages"
    if not packages.exists() and not packages.is_symlink():
        forget_workspace(snapshot)
        return
    if process_views().uses(snapshot):
        # Native shell tools may leave a child behind after the CLI exits.
        # Its registered view becomes collectible only after that child exits.
        return
    # The marker also recovers interrupted cleanup: a partial cache is never reused.
    atomic_json_write(snapshot / _RELEASED, {"restore_pinned_dependencies": True})
    if packages.is_symlink():
        packages.unlink()
    else:
        shutil.rmtree(packages)
    forget_workspace(snapshot)
