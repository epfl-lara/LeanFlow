"""Stage effectful dependency resolution and journal no-clobber configuration promotion."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.file_copy import copy_file, copy_tree
from leanflow_cli.formalization.evidence import contained, digest, durable_json, sync_directory
from leanflow_cli.formalization.publication_io import mkdir_durable, rename_exclusive
from leanflow_cli.workflows.prover import source_mutation
from leanflow_cli.workflows.prover.library_checkout import require_checkout
from leanflow_cli.workflows.prover.source import SourceConflictError, project_sources
from leanflow_cli.workflows.prover.source_mutation_recovery import preflight, rollback

BASE = ".leanflow/prover-library-transactions"
LIMIT = 64 * 1024


def pending_path(root: Path) -> Path:
    """Locate the single serialized library configuration transaction."""
    return contained(root, f"{BASE}/pending.json")


def _save(root: Path, record: dict[str, Any]) -> None:
    """Persist bounded mutation references before any canonical promotion."""
    if len(json.dumps(record).encode()) > LIMIT:
        raise ValueError("Library promotion journal exceeds its recovery limit")
    durable_json(pending_path(root), record)


def recover_pending(root: Path) -> list[str]:
    """Roll back a partial configuration install while retaining added package caches.

    The caller holds publication ownership. No package directory is removed: a
    concurrent compiler or editor may already be using an added checkout.
    """
    path = pending_path(root)
    if not path.exists() and not path.is_symlink():
        return []
    if path.is_symlink() or not path.is_file() or path.stat().st_size > LIMIT:
        raise SourceConflictError(f"Library promotion journal is unavailable: {path}")
    record = json.loads(path.read_text())
    if (
        not isinstance(record, dict)
        or record.get("schema") != 1
        or not re.fullmatch(r"[0-9a-f]{32}", str(record.get("id", "")))
        or record.get("state") not in {"pending", "committed", "rolled_back"}
        or not isinstance(record.get("mutations"), list)
        or len(record["mutations"]) > 3
        or not isinstance(record.get("recovered"), list)
        or not set(record["recovered"]).issubset(record["mutations"])
    ):
        raise SourceConflictError(f"Invalid library promotion journal: {path}")
    identifiers = record["mutations"]
    if record["state"] == "pending":
        preflight(root, identifiers, record["recovered"])
        for identifier in reversed(identifiers):
            if identifier not in record["recovered"]:
                rollback(root, identifier)
                record["recovered"].append(identifier)
                _save(root, record)
            else:
                source_mutation.audit_retained(root, identifier)
        record["state"] = "rolled_back"
        _save(root, record)
    else:
        for identifier in identifiers:
            source_mutation.audit_retained(root, identifier)
    archive = contained(root, f"{BASE}/{record['id']}.json")
    rename_exclusive(path, archive)
    return identifiers


def copy_inputs(
    root: Path,
    stage: Path,
    packages: dict[str, dict[str, Any]],
    *,
    check: Callable[[], None],
) -> None:
    """Copy compiler inputs and dependencies with distinct inodes, excluding credentials."""
    dependencies = root / ".lake"
    if dependencies.is_dir():
        _regular_copy_inputs(dependencies, check=check)
    stage.mkdir()
    config = {"lakefile.toml", "lakefile.lean", "lake-manifest.json", "lean-toolchain"}
    for name in sorted(config):
        check()
        path = root / name
        if path.is_file():
            copy_file(path, stage / name, check=check)
    for source in project_sources(root):
        if source.name in config:
            continue
        check()
        target = stage / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_file(source, target, check=check)
    if dependencies.is_dir():

        def ignore(directory: str, names: list[str]) -> list[str]:
            denied = {".leanflow", ".codex", ".claude", ".lemex", ".mcp.json", ".env", ".netrc"}
            if Path(directory).name == ".git":
                denied |= {"config", "hooks"}
            return [name for name in names if name in denied]

        copy_tree(dependencies, stage / ".lake", ignore=ignore, check=check)
        validate_private_links(stage, check=check)
        _private_git_configuration(stage, packages, check=check)
    else:
        (stage / ".lake").mkdir()


def _regular_copy_inputs(root: Path, *, check: Callable[[], None]) -> None:
    """Reject nonregular cache inputs before a normal copy can wait on a named pipe."""
    for directory, names, files in os.walk(root, followlinks=False):
        for name in (*names, *files):
            check()
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                raise ValueError(f"Private library copy has a nonregular input: {path}")


def validate_private_links(stage: Path, *, check: Callable[[], None]) -> None:
    """Refuse copied links back to canonical inputs before rewriting metadata or running hooks."""
    for directory, names, files in os.walk(stage, followlinks=False):
        for name in (*names, *files):
            check()
            path = Path(directory) / name
            if path.is_symlink():
                try:
                    safe = not Path(os.readlink(path)).is_absolute() and path.resolve(
                        strict=True
                    ).is_relative_to(stage)
                except (OSError, RuntimeError):
                    safe = False
                if not safe:
                    raise ValueError(f"Private library link escapes its copied inputs: {path}")


def _private_git_configuration(
    stage: Path, packages: dict[str, dict[str, Any]], *, check: Callable[[], None]
) -> None:
    """Recreate only locked remote identity, avoiding both credentials and needless Lake reclones."""
    from leanflow_cli.workflows.prover.libraries import _entries

    for name, package in packages.items():
        check()
        if package.get("type") != "git":
            continue
        normalized = _entries(
            [{"name": name, "git": package.get("url"), "rev": package.get("rev")}],
            None,
            verify_remote=False,
        )[0]
        metadata = stage / ".lake/packages" / name / ".git"
        if not metadata.exists():
            continue
        if metadata.is_symlink() or not metadata.is_dir():
            raise ValueError("Existing dependencies require private standard Git metadata")
        if metadata.resolve() != metadata.absolute():
            raise ValueError("Private Git metadata cannot use an aliased ancestor")
        if (metadata / "objects/info/alternates").exists():
            raise ValueError("Existing dependencies cannot borrow external Git object stores")
        sha256 = len(normalized["rev"]) == 64
        content = (
            "[core]\n"
            f"\trepositoryformatversion = {1 if sha256 else 0}\n"
            "\tbare = false\n"
            "\tfilemode = true\n"
            '[remote "origin"]\n'
            f'\turl = {json.dumps(package["url"])}\n'
            "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
            + ("[extensions]\n\tobjectFormat = sha256\n" if sha256 else "")
        )
        (metadata / "config").write_text(content)


def promote(
    root: Path,
    stage: Path,
    snapshots: dict[Path, bytes | None],
    packages: dict[str, dict[str, Any]],
    existing_packages: dict[str, tuple[int, int]],
    *,
    check: Callable[[], None],
    deadline: float | None = None,
) -> list[str]:
    """Install absent package entries and validated config bytes without replacing editor saves."""
    from leanflow_cli.workflows.prover.libraries import _configuration_bytes

    recover_pending(root)
    record: dict[str, Any] = {
        "schema": 1,
        "id": uuid.uuid4().hex,
        "state": "pending",
        "mutations": [],
        "recovered": [],
        "promoted_packages": [],
    }
    mkdir_durable(pending_path(root).parent)
    _save(root, record)
    # Prepare every expected configuration capture before installing any file.
    for path, before in snapshots.items():
        check()
        after = _configuration_bytes(stage / path.name)
        if after != before:
            identifier = source_mutation.create(root, path.name, before, after)
            record["mutations"].append(identifier)
            _save(root, record)
        else:
            current = source_mutation.observe(path)
            if current is None or current["sha256"] != digest(before or b""):
                raise SourceConflictError(f"Protected library configuration changed: {path}")
    destination = root / ".lake/packages"
    for name, package in packages.items():
        check()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", name):
            raise ValueError("Resolved package has an unsupported directory name")
        if package.get("dir", name) != name:
            raise ValueError("Custom dependency directories require manual installation")
        incoming, target = stage / ".lake/packages" / name, destination / name
        if name in existing_packages:
            info = target.stat()
            if target.is_symlink() or (info.st_dev, info.st_ino) != existing_packages[name]:
                raise SourceConflictError(f"Existing dependency directory changed: {target}")
            if package.get("type") == "git":
                require_checkout(target, package.get("rev", ""), deadline=deadline)
            continue
        if not incoming.exists():
            raise ValueError(f"Resolved dependency has no staged checkout: {incoming}")
        if incoming.is_symlink() or not incoming.is_dir():
            raise ValueError("Resolved dependency checkout must be a regular directory")
        if package.get("type") == "git":
            require_checkout(incoming, package.get("rev", ""), deadline=deadline)
        mkdir_durable(destination)
        record["promoted_packages"].append(name)
        _save(root, record)
        rename_exclusive(incoming, target)
    for identifier in record["mutations"]:
        check()
        source_mutation.install(root, identifier)
    for identifier in record["mutations"]:
        source_mutation.commit(root, identifier)
    for name, package in packages.items():
        check()
        if package.get("type") == "git":
            require_checkout(destination / name, package.get("rev", ""), deadline=deadline)
    # An unchanged toolchain/config is still protected through the final boundary.
    for path, before in snapshots.items():
        if _configuration_bytes(stage / path.name) == before:
            current = source_mutation.observe(path)
            if current is None or current["sha256"] != digest(before or b""):
                raise SourceConflictError(f"Protected library configuration changed: {path}")
    record["state"] = "committed"
    _save(root, record)
    identifiers = list(record["mutations"])
    recover_pending(root)
    sync_directory(root)
    return identifiers
