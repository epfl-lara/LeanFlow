"""Journal public baseline configuration changes without replacing competing editor versions."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable
from itertools import islice
from pathlib import Path
from typing import Any

from core.regular_files import read_regular_bytes
from leanflow_cli.formalization.evidence import durable_json, sync_directory
from leanflow_cli.formalization.publication_io import mkdir_durable, rename_exclusive
from leanflow_cli.workflows.project_setup import invocation_evidence
from leanflow_cli.workflows.prover import source_mutation
from leanflow_cli.workflows.prover.source import SourceConflictError
from leanflow_cli.workflows.prover.source_mutation_recovery import preflight, rollback

BASE = ".leanflow/baseline-setup"
LIMIT = 64 * 1024
MAX_HISTORY = 256
Boundary = Callable[[str], None] | None


def _history(directory: Path, *, limit: int = MAX_HISTORY) -> list[Path]:
    """Stop discovery at its declared bound before sorting or loading any excess history."""
    paths = list(islice(directory.glob("*.json"), limit + 1))
    if len(paths) > limit:
        raise ValueError(f"Baseline history exceeds its bounded audit: {directory}")
    return sorted(paths)


def _base(root: Path) -> Path:
    """Locate the project-owned journal without traversing an infrastructure alias."""
    for path in (root / ".leanflow", root / BASE):
        if path.is_symlink():
            raise SourceConflictError(f"Baseline setup journal is aliased: {path}")
    return root / BASE


def _load(path: Path) -> dict[str, Any]:
    """Read a bounded baseline journal before interpreting any mutation reference."""
    record = json.loads(read_regular_bytes(path, limit=LIMIT))
    root = path.parents[3] if path.parent.name == "archive" else path.parents[2]
    if (
        not isinstance(record, dict)
        or record.get("schema") != 1
        or not re.fullmatch(r"[0-9a-f]{32}", str(record.get("id", "")))
        or record.get("state")
        not in {"preparing", "promoting", "promoted", "committed", "aborted", "acknowledged"}
        or not isinstance(record.get("mutations"), list)
        or len(record["mutations"]) > 16
        or any(not re.fullmatch(r"[0-9a-f]{32}", str(value)) for value in record["mutations"])
        or record.get("root") != str(root)
        or not isinstance(record.get("invocations"), list)
        or len(record["invocations"]) > invocation_evidence.MAX_INVOCATIONS
        or any(not re.fullmatch(r"[0-9a-f]{32}", str(value)) for value in record["invocations"])
        or len(record["invocations"]) != len(set(record["invocations"]))
        or record.get("stage") != str(root / BASE / "stages" / str(record.get("id")))
        or path.name not in {"pending.json", f"{record.get('id')}.json"}
        or not isinstance(record.get("adopted_editor_versions", {}), dict)
        or len(record.get("adopted_editor_versions", {})) > 64
        or any(
            key in record and not re.fullmatch(r"[0-9a-f]{64}", str(record[key]))
            for key in ("recovery_observation_id", "recovery_parent_observation_id")
        )
    ):
        raise SourceConflictError(f"Invalid baseline setup journal: {path}")
    return record


def _save(root: Path, record: dict[str, Any]) -> None:
    """Persist intent before each canonical filesystem boundary."""
    path = _base(root) / "pending.json"
    if path.exists() or path.is_symlink():
        previous = _load(path)
        if previous["id"] != record["id"]:
            raise SourceConflictError("Another baseline journal owns this setup")
        record["invocations"] = list(
            dict.fromkeys([*previous["invocations"], *record["invocations"]])
        )
    if len(json.dumps(record).encode()) > LIMIT:
        raise ValueError("Baseline setup journal exceeds its recovery bound")
    durable_json(path, record)


def register_invocation(root: Path, identifier: str) -> None:
    """Bind a new public invocation into the active transaction before spawning it."""
    path = _base(root) / "pending.json"
    if path.exists() or path.is_symlink():
        record = _load(path)
        record["invocations"].append(identifier)
        _save(root, record)


def _retained_issue(root: Path, identifier: str, adopted: dict[str, list[Any]]) -> str | None:
    """Inspect retained inodes without mutating history during a status request."""
    record = source_mutation.load(root, identifier)
    if record["state"] == "conflicted":
        return str(record.get("conflict", "unresolved retained baseline versions"))
    location = source_mutation.directory(root, identifier)
    expected = [(location / "candidate", record["after"])]
    if record.get("original_captured"):
        expected.append((location / "displaced", record["before"]))
    expected.extend((location / slot, record["after"]) for slot in record["rollback_slots"])
    canonical = source_mutation.observe(root / record["path"])
    for path, identity in expected:
        actual = source_mutation.observe(path)
        key = f"{identifier}:{path.name}"
        if actual in adopted.get(key, []) or (
            actual is not None and canonical is not None and actual["inode"] == canonical["inode"]
        ):
            # A current editor save is a new setup input, not a late detached
            # writer. Its changed fingerprint still revokes the old receipt.
            continue
        if actual != identity:
            return f"Retained baseline editor version changed: {path}"
    return None


def pending_issue(root: Path) -> str | None:
    """Report interrupted or conflicting setup before a receipt can claim readiness."""
    try:
        if not invocation_evidence.closed(root):
            return "Baseline setup has uncertain or missing subprocess closure evidence"
        base = _base(root)
        if not base.exists():
            return None
        records = all_records(root)
        from leanflow_cli.workflows.project_setup import recovery_barrier

        binding = recovery_barrier.current(
            root, required=any(record.get("recovery_observation_id") for _, record in records)
        )
        if binding is not None and binding["acknowledgment_complete"] is not True:
            return "Baseline recovery acknowledgment is incomplete; retain all current versions"
        required = list(
            dict.fromkeys(
                identifier for _, record in records for identifier in record["invocations"]
            )
        )
        for offset in range(0, len(required), invocation_evidence.MAX_INVOCATIONS):
            if not invocation_evidence.closed(
                root, required=required[offset : offset + invocation_evidence.MAX_INVOCATIONS]
            ):
                return "Baseline setup has uncertain or missing archived subprocess evidence"
        adopted: dict[str, list[Any]] = {}
        for _, record in records:
            if record["state"] == "committed":
                for key, identity in record.get("adopted_editor_versions", {}).items():
                    adopted.setdefault(key, []).append(identity)
        for path, record in records:
            if not _closed(record, invocations_verified=True):
                return (
                    f"Baseline setup has uncertain or missing subprocess closure evidence: {path}"
                )
            if path.name == "pending.json":
                return f"Baseline setup is incomplete; retry setup to recover {path}"
            from leanflow_cli.workflows.project_setup.baseline_recovery import (
                acknowledged_issue,
                has_acknowledgment,
            )

            if has_acknowledgment(root, record):
                issue = acknowledged_issue(root, record)
                if issue:
                    return issue
                continue
            for identifier in record["mutations"]:
                issue = _retained_issue(root, identifier, adopted)
                if issue:
                    return issue
        from leanflow_cli.workflows.project_setup.baseline_recovery import canonical_issue

        return canonical_issue(root)
    except (OSError, ValueError, SourceConflictError) as error:
        return f"Baseline setup recovery evidence is unavailable: {error}"


def _closed(record: dict[str, Any], *, invocations_verified: bool = False) -> bool:
    """Require every persisted public subprocess invocation to prove physical closure."""
    root = Path(record["root"])
    pending = _base(root) / "pending.json"
    current = _load(pending) if pending.exists() else None
    if current is None or current["id"] != record["id"]:
        active = _base(root) / f"{record['id']}.json"
        current = _load(active if active.exists() else _base(root) / "archive" / active.name)
    record["invocations"] = list(dict.fromkeys([*current["invocations"], *record["invocations"]]))
    return (
        invocations_verified or invocation_evidence.closed(root, required=record["invocations"])
    ) and invocation_evidence.closed(Path(record["stage"]))


def recover_pending(root: Path, *, boundary: Boundary = None) -> None:
    """Recover only quiescent baseline work while existing project ownership is held."""
    path = _base(root) / "pending.json"
    from leanflow_cli.workflows.project_setup import recovery_barrier
    from leanflow_cli.workflows.project_setup.public_process import (
        retry_pending_invocations,
    )

    try:
        binding = recovery_barrier.current(root)
    except (OSError, ValueError) as error:
        raise SourceConflictError(f"Baseline recovery barrier is unavailable: {error}") from error
    if binding is not None and binding["acknowledgment_complete"] is not True:
        raise SourceConflictError(
            "Baseline recovery acknowledgment is incomplete; retain all versions"
        )

    retry_pending_invocations(root)
    if not invocation_evidence.closed(root):
        raise SourceConflictError(
            "Baseline setup has uncertain or missing subprocess closure evidence"
        )
    if not path.exists():
        issue = pending_issue(root)
        if issue:
            raise SourceConflictError(issue)
        return
    record = _load(path)
    retry_pending_invocations(Path(record["stage"]))
    if not _closed(record):
        raise SourceConflictError(
            f"Baseline setup has uncertain subprocess closure; retain its inputs and inspect {path}"
        )
    if record["state"] == "acknowledged" or record.get("recovery_observation_id"):
        # An external keep-current acknowledgment is terminal, never permission
        # for automatic source recovery based on acknowledgement-only closure.
        record["state"] = "acknowledged"
    elif record["state"] == "promoting":
        preflight(root, record["mutations"], [])
        for identifier in reversed(record["mutations"]):
            rollback(root, identifier, boundary=boundary)
    elif record["state"] in {"promoted", "committed"}:
        for identifier in record["mutations"]:
            source_mutation.validate_installed(root, identifier)
    if record["state"] not in {"acknowledged", "committed"}:
        record["state"] = "aborted"
    _save(root, record)
    rename_exclusive(path, _base(root) / f"{record['id']}.json")
    issue = pending_issue(root)
    if issue:
        raise SourceConflictError(issue)


def begin(root: Path) -> dict[str, Any]:
    """Register a same-filesystem private configuration stage before any mutation."""
    recover_pending(root)
    compact_history(root)
    identifier = uuid.uuid4().hex
    base = _base(root)
    mkdir_durable(base)
    stage = base / "stages" / identifier
    mkdir_durable(stage)
    previous_records = all_records(root)
    previously_bound = {
        value for _, previous in previous_records for value in previous["invocations"]
    }
    initial_invocations = [
        value
        for value in invocation_evidence.expected(root)
        if value not in previously_bound
        and invocation_evidence.load_record(root, value)[0].get("cleanup_verified") is True
    ]
    record: dict[str, Any] = {
        "schema": 1,
        "id": identifier,
        "stage": str(stage),
        "root": str(root),
        "state": "preparing",
        "mutations": [],
        "invocations": initial_invocations,
        "adopted_editor_versions": {},
    }
    from leanflow_cli.workflows.project_setup import recovery_barrier

    binding = recovery_barrier.current(root)
    if binding is not None:
        record["recovery_parent_observation_id"] = binding["observation_id"]
    for path, previous in previous_records:
        for mutation in previous["mutations"]:
            item = source_mutation.load(root, mutation)
            current = source_mutation.observe(root / item["path"])
            if current is None:
                continue
            location = source_mutation.directory(root, mutation)
            for leaf in ("candidate", "displaced", *item["rollback_slots"]):
                retained = source_mutation.observe(location / leaf)
                if retained is not None and retained["inode"] == current["inode"]:
                    record["adopted_editor_versions"][f"{mutation}:{leaf}"] = retained
    _save(root, record)
    return record


def record_inputs(root: Path, record: dict[str, Any], snapshots: dict[str, bytes | None]) -> None:
    """Keep exact original bytes and original absence before effectful resolution."""
    originals = Path(record["stage"]) / ".baseline-originals"
    for relative, content in snapshots.items():
        source_mutation.entry_path(root, relative)
        if content is not None:
            target = originals / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                with target.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                sync_directory(target.parent)
            elif read_regular_bytes(target, limit=source_mutation.FILE_LIMIT) != content:
                raise SourceConflictError(f"Retained original baseline bytes changed: {target}")
    record["original_inputs"] = {
        relative: source_mutation.digest(content) if content is not None else None
        for relative, content in snapshots.items()
    }
    _save(root, record)


def promote(
    root: Path,
    record: dict[str, Any],
    snapshots: dict[str, bytes | None],
    *,
    check: Callable[[], None],
    boundary: Boundary = None,
) -> None:
    """Capture actual config entries and install only into exclusively emptied names."""
    stage = Path(record["stage"])
    record_inputs(root, record, snapshots)
    record["after_inputs"] = {}
    for relative, before in snapshots.items():
        check()
        target = stage / relative
        after = (
            read_regular_bytes(target, limit=source_mutation.FILE_LIMIT)
            if target.exists()
            else None
        )
        record["after_inputs"][relative] = (
            source_mutation.digest(after) if after is not None else None
        )
        if before is not None or after is not None:
            identifier = source_mutation.create(root, relative, before, after)
            record["mutations"].append(identifier)
            _save(root, record)
    record["state"] = "promoting"
    _save(root, record)
    if boundary:
        boundary("prepared")
    packages = stage / ".lake/packages"
    if packages.is_dir():
        destination = root / ".lake/packages"
        mkdir_durable(destination)
        for package in sorted(packages.iterdir()):
            check()
            if package.is_symlink() or not package.is_dir():
                raise SourceConflictError(f"Unsupported staged dependency entry: {package}")
            target = destination / package.name
            if not target.exists() and not target.is_symlink():
                rename_exclusive(package, target)
    for identifier in record["mutations"]:
        check()
        source_mutation.install(root, identifier, boundary=boundary)
    record["state"] = "promoted"
    _save(root, record)


def finish(root: Path, record: dict[str, Any]) -> dict[str, bytes | None]:
    """Commit readiness only after commands and all installed/retained versions verify."""
    if not _closed(record):
        raise SourceConflictError("Baseline subprocess closure remains unverified")
    configuration: dict[str, bytes | None] = {}
    for relative, expected in record["after_inputs"].items():
        path = Path(record["stage"]) / relative
        content = (
            read_regular_bytes(path, limit=source_mutation.FILE_LIMIT) if path.exists() else None
        )
        actual = source_mutation.digest(content) if content is not None else None
        canonical = source_mutation.observe(root / relative)
        if (
            actual != expected
            or (canonical["sha256"] if canonical is not None else None) != expected
        ):
            raise SourceConflictError(f"Validated baseline configuration changed: {relative}")
        configuration[relative] = content
    for identifier in record["mutations"]:
        source_mutation.commit(root, identifier)
    for identifier in record["mutations"]:
        source_mutation.validate_installed(root, identifier)
    record["state"] = "committed"
    _save(root, record)
    rename_exclusive(_base(root) / "pending.json", _base(root) / f"{record['id']}.json")
    from leanflow_cli.workflows.project_setup.baseline_recovery import consume_canonical_binding

    consume_canonical_binding(root, record["id"])
    issue = pending_issue(root)
    if issue:
        raise SourceConflictError(issue)
    return configuration


def all_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Read positively indexed active and archived journals without losing retained audits."""
    from leanflow_cli.workflows.project_setup.baseline_history import all_records as read_history

    return read_history(root)


def compact_history(root: Path) -> None:
    """Compact physically closed history before allocating the next active journal."""
    from leanflow_cli.workflows.project_setup.baseline_history import compact_history as compact

    compact(root, threshold=MAX_HISTORY - 1)
