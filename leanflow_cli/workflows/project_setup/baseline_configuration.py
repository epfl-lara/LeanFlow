"""Resolve baseline configuration privately before conflict-preserving canonical promotion."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.workflows.project_setup import baseline_transaction
from leanflow_cli.workflows.project_setup.process import SetupFailure
from leanflow_cli.workflows.project_setup.scaffold import (
    read_toolchain,
    scaffold,
    source_paths,
    validate_lakefile,
    validate_mutable_paths,
)
from leanflow_cli.workflows.project_setup.verification import configure_repl, manifest_revisions
from leanflow_cli.workflows.prover.source import lean_code_mask

CONFIG = ("lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json")


def _remote_git(value: Any) -> bool:
    """Accept explicit remote locations without interpreting relative repository paths."""
    return isinstance(value, str) and bool(
        re.match(r"(?:https?|ssh|git)://[^/]+/|[^/\s@:]+@[^/\s:]+:", value)
    )


def _validate_relocation(root: Path) -> None:
    """Reject local dependency inputs that the private baseline stage cannot faithfully bind."""
    unsupported = False
    config = validate_lakefile(root)
    if config is not None:
        dependencies = config.get("require", [])
        if not isinstance(dependencies, list):
            raise ValueError("Invalid Lake dependency declarations")
        unsupported = any(
            isinstance(row, dict)
            and ("path" in row or ("git" in row and not _remote_git(row["git"])))
            for row in dependencies
        )
    else:
        path = root / "lakefile.lean"
        text = read_regular_bytes(path, limit=8 * 1024**2).decode() if path.is_file() else ""
        mask = lean_code_mask(text)
        commands = list(
            re.finditer(r"(?m)^[ \t]*(?:require|import|open|package|lean_lib|lean_exe)\b", mask)
        )
        for index, command in enumerate(commands):
            if not re.match(r"\s*require\b", command.group()):
                continue
            end = commands[index + 1].start() if index + 1 < len(commands) else len(text)
            declaration = mask[command.end() : end]
            source = re.search(r"\bfrom\b", declaration)
            if source is not None:
                position = command.end() + source.end()
                remote = re.match(r'\s+git\s+("(?:[^"\\]|\\.)*")', text[position:end])
                unsupported |= remote is None or not _remote_git(json.loads(remote[1]))
    manifest = root / "lake-manifest.json"
    if manifest.is_file():
        value = json.loads(read_regular_bytes(manifest, limit=8 * 1024**2))
        packages = value.get("packages", []) if isinstance(value, dict) else []
        if not isinstance(packages, list):
            raise ValueError("Invalid Lake dependency manifest")
        unsupported |= any(
            isinstance(row, dict)
            and (
                row.get("type") == "path"
                or (row.get("type") == "git" and not _remote_git(row.get("url")))
            )
            for row in packages
            if isinstance(row, dict) and (row.get("type") in {"path", "git"})
        )
    if unsupported:
        raise SetupFailure(
            "local_dependency_setup_unsupported",
            "Private baseline setup cannot safely relocate this project's local path dependencies.",
            "Keep local dependencies and configuration unchanged. Set up this layout manually, or use explicitly pinned remote Git dependencies before retrying automatic setup.",
        )


def prepare_configuration(
    root: Path,
    *,
    name: str | None,
    preset: str,
    source_file: str | Path | None,
    changed: list[str],
    run: Callable[..., str],
    phase: Callable[[str, str], None],
    check: Callable[[], None],
    restore: Callable[[Path], bool],
) -> dict[str, Any]:
    """Keep effectful Lake updates off canonical pins and preserve original absence."""
    root.mkdir(parents=True, exist_ok=True)
    _validate_relocation(root)
    record = baseline_transaction.begin(root)
    stage = Path(record["stage"])
    snapshots: dict[str, bytes | None] = {}
    for relative in CONFIG:
        check()
        path = root / relative
        content = read_regular_bytes(path, limit=8 * 1024**2) if path.exists() else None
        snapshots[relative] = content
        if content is not None:
            (stage / relative).write_bytes(content)
    baseline_transaction.record_inputs(root, record, snapshots)
    for path in source_paths(root):
        check()
        target = stage / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read_regular_bytes(path, limit=8 * 1024**2))
    additions: list[str] = []
    phase("preparing", "Preparing project configuration in a private stage")
    scaffold(
        stage, name=name or root.name, preset=preset, source_file=source_file, changed=additions
    )
    for relative in additions:
        snapshots.setdefault(relative, None)
    baseline_transaction.record_inputs(root, record, snapshots)
    validate_lakefile(stage)
    if not read_toolchain(stage):
        raise SetupFailure(
            "toolchain_missing",
            "This project has no Lean toolchain version.",
            "Restore lean-toolchain, then retry setup.",
        )
    phase("toolchain", "Preparing the project's Lean toolchain")
    run(
        ["lake", "--version"],
        cwd=stage,
        code="toolchain_failed",
        remedy="Check the Lean toolchain and network access, then retry.",
    )

    def staged_run(command: list[str], **kwargs: Any) -> str:
        return run(command, cwd=stage, **kwargs)

    manifest = stage / "lake-manifest.json"
    previous = snapshots["lake-manifest.json"]
    pins = manifest_revisions(previous)
    toolchain = (stage / "lean-toolchain").read_bytes()
    phase("dependencies", "Resolving pinned dependencies in the private setup stage")
    added = configure_repl(stage, staged_run, additions)
    if added or not manifest.is_file() or "repl" not in manifest.read_text():
        restore(stage)
        staged_run(
            ["lake", "update", "repl"],
            code="dependency_download_failed",
            remedy="Check dependency access and retry. Canonical editor versions were retained.",
        )
        current = manifest_revisions(manifest.read_bytes() if manifest.is_file() else None)
        if any(current.get(key) != value for key, value in pins.items()):
            raise SetupFailure(
                "dependency_pin_conflict",
                "Adding REPL would change existing dependency pins; canonical pins were preserved.",
                "Choose a compatible REPL revision, then retry.",
            )
        if (stage / "lean-toolchain").read_bytes() != toolchain:
            raise SetupFailure(
                "toolchain_conflict",
                "Dependency setup changed the staged Lean toolchain; canonical toolchain was preserved.",
                "Choose compatible dependencies, then retry.",
            )
    validate_mutable_paths(stage)
    baseline_transaction.promote(root, record, snapshots, check=check)
    for relative in snapshots:
        path = stage / relative
        after = path.read_bytes() if path.is_file() else None
        if snapshots[relative] != after and relative not in changed:
            changed.append(relative)
    return record
