"""Journal planning source writes and restore the previous graph without clobbering edits."""

from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover import source_mutation
from leanflow_cli.workflows.prover.models import Dag, safe_relative_file
from leanflow_cli.workflows.prover.source import SourceConflictError, SourceDocument, project_path
from leanflow_cli.workflows.prover.source_journal import load_journal, save_journal
from leanflow_cli.workflows.prover.source_mutation_recovery import preflight as preflight_mutations
from leanflow_cli.workflows.prover.source_mutation_recovery import rollback as rollback_mutation

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def begin_materialization(runtime: ProverRuntime) -> dict[str, Any]:
    """Journal the previous graph and source generation before any planning writes."""
    path = runtime.store.directory / "source-transaction.json"
    if path.exists():
        raise ValueError("An earlier source transaction must be recovered before materialization")
    journal: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "kind": "materialization",
        "writes": {},
        "before_dag": runtime.dag.to_dict(),
        "before_documents": {key: doc.to_dict() for key, doc in runtime.documents.items()},
        "before_changes": copy.deepcopy(runtime.state["changes"]),
        "before_retired": copy.deepcopy(runtime.state.get("retired_nodes", [])),
    }
    save_journal(runtime, journal)
    return journal


def materialized_write(
    runtime: ProverRuntime,
    journal: dict[str, Any],
    path: Path,
    after: bytes | None,
    *,
    expected_before: bytes | None = None,
    require_absent: bool = False,
) -> None:
    """Record exact before/after bytes durably, then perform one bounded source mutation."""
    relative = str(path.relative_to(runtime.root))
    path = project_path(runtime.root, relative)
    current = path.read_bytes() if path.exists() else None
    if any(value is not None and len(value) > 8 * 1024 * 1024 for value in (current, after)):
        raise ValueError("Planning transaction file exceeds its 8 MiB recovery limit")
    encode = lambda value: base64.b64encode(value).decode() if value is not None else None
    entry = journal["writes"].get(relative)
    if entry is None:
        recorded = journal["before_documents"].get(relative)
        if recorded is not None:
            expected_before = SourceDocument(**recorded).render().encode("utf-8")
        if (require_absent and current is not None) or (
            expected_before is not None and current != expected_before
        ):
            raise SourceConflictError(
                f"protected source changed during planning transaction: {relative}"
            )
        entry = {"before": encode(current), "allowed": [encode(current)]}
        journal["writes"][relative] = entry
    elif encode(current) != entry["after"]:
        raise SourceConflictError(
            f"protected source changed during planning transaction: {relative}"
        )
    entry["after"] = encode(after)
    if entry["after"] not in entry["allowed"]:
        entry["allowed"].append(entry["after"])
    if len(json.dumps(journal)) > 64 * 1024 * 1024:
        raise ValueError("Planning transaction exceeds its 64 MiB recovery limit")
    save_journal(runtime, journal)
    identifier = source_mutation.create(runtime.root, relative, current, after)
    journal.setdefault("mutations", []).append(identifier)
    save_journal(runtime, journal)
    source_mutation.install(runtime.root, identifier)


def recover_materialization(runtime: ProverRuntime, journal: dict[str, Any]) -> None:
    """Recover only journaled bytes, validating every file before changing any of them."""
    from leanflow_cli.formalization.publication import publication_guard

    with publication_guard(runtime.root):
        _recover_materialization_locked(runtime, journal)


def _recover_materialization_locked(runtime: ProverRuntime, journal: dict[str, Any]) -> None:
    """Retain the publication lock across every rollback write and validation."""
    entries = journal["writes"]
    committed = runtime.state.get("source_transaction") == journal["id"]
    if not committed and journal.get("legacy_recovery_mutations"):
        for identifier in reversed(journal.pop("legacy_recovery_mutations")):
            rollback_mutation(runtime.root, identifier)
        save_journal(runtime, journal)
    compiled_artifacts: list[tuple[str, bytes | None]] = []
    for relative in journal.get("compiled_modules", []):
        if (
            not isinstance(relative, str)
            or not relative.startswith("LeanFlowProofs/")
            or Path(relative).suffix != ".lean"
        ):
            raise ValueError("Invalid helper compilation journal entry")
        safe_relative_file(relative)
        artifact_relative = str(Path(".lake/build/lib/lean") / Path(relative).with_suffix(".olean"))
        project_path(runtime.root, artifact_relative)
        encoded_before = journal.get("compiled_artifacts", {}).get(relative)
        before = (
            base64.b64decode(encoded_before, validate=True) if encoded_before is not None else None
        )
        compiled_artifacts.append((artifact_relative, before))
    observed: dict[str, bytes | None] = {}
    for relative, entry in entries.items():
        path = project_path(runtime.root, relative)
        current = path.read_bytes() if path.exists() else None
        encoded = base64.b64encode(current).decode() if current is not None else None
        if (committed or not journal.get("mutations")) and encoded not in (
            [entry["after"]] if committed else entry["allowed"]
        ):
            raise SourceConflictError(
                f"protected source changed during interrupted planning transaction: {relative}"
            )
        observed[relative] = current
    if not committed:
        recovered = journal.setdefault("recovered_mutations", [])
        preflight_mutations(runtime.root, journal.get("mutations", []), recovered)
        for identifier in reversed(journal.get("mutations", [])):
            if identifier not in recovered:
                rollback_mutation(runtime.root, identifier)
                recovered.append(identifier)
                save_journal(runtime, journal)
            else:
                source_mutation.audit_retained(runtime.root, identifier)
            retained = runtime.state.setdefault("retained_source_mutations", [])
            if identifier not in retained:
                retained.append(identifier)
        for relative, entry in reversed(list(entries.items())):
            if journal.get("mutations"):
                continue
            path = project_path(runtime.root, relative)
            if (path.read_bytes() if path.exists() else None) != observed[relative]:
                raise SourceConflictError(
                    f"protected source changed during interrupted planning transaction: {relative}"
                )
            before = base64.b64decode(entry["before"]) if entry["before"] is not None else None
            identifier = source_mutation.create(runtime.root, relative, observed[relative], before)
            journal.setdefault("legacy_recovery_mutations", []).append(identifier)
            save_journal(runtime, journal)
            source_mutation.install(runtime.root, identifier)
            source_mutation.commit(runtime.root, identifier)
            runtime.state.setdefault("retained_source_mutations", []).append(identifier)
            if before is None and relative.startswith("LeanFlowProofs/") and path.suffix == ".lean":
                artifact = project_path(
                    runtime.root,
                    str(Path(".lake/build/lib/lean") / Path(relative).with_suffix(".olean")),
                )
                artifact.unlink(missing_ok=True)
        # Restore usable caches for the previous graph even if planning stops here.
        # A failed replan must neither retain rejected imports nor strand existing jobs.
        for relative, before in compiled_artifacts:
            artifact = project_path(runtime.root, relative)
            if before is None:
                artifact.unlink(missing_ok=True)
            else:
                replace_artifact(artifact, before)
        runtime.documents = {
            key: SourceDocument(**value) for key, value in journal["before_documents"].items()
        }
        runtime.dag = Dag.from_dict(journal["before_dag"])
        runtime.state["changes"] = journal["before_changes"]
        runtime.state["retired_nodes"] = journal["before_retired"]
        runtime._persist()
    if committed:
        for identifier in journal.get("mutations", []):
            source_mutation.audit_retained(runtime.root, identifier)
            retained = runtime.state.setdefault("retained_source_mutations", [])
            if identifier not in retained:
                retained.append(identifier)
        runtime._persist()
    (runtime.store.directory / "source-transaction.json").unlink(missing_ok=True)


def record_compilation(runtime: ProverRuntime, journal: dict[str, Any], relative: str) -> None:
    """Snapshot a generated module's existing artifact before compilation can replace it."""
    artifact = project_path(
        runtime.root, str(Path(".lake/build/lib/lean") / Path(relative).with_suffix(".olean"))
    )
    before = artifact.read_bytes() if artifact.is_file() else None
    if before is not None and len(before) > 8 * 1024 * 1024:
        raise ValueError("Planning transaction artifact exceeds its 8 MiB recovery limit")
    journal.setdefault("compiled_modules", []).append(relative)
    journal.setdefault("compiled_artifacts", {})[relative] = (
        base64.b64encode(before).decode() if before is not None else None
    )
    if len(json.dumps(journal)) > 64 * 1024 * 1024:
        raise ValueError("Planning transaction exceeds its 64 MiB recovery limit")
    save_journal(runtime, journal)


def commit_materialization(runtime: ProverRuntime) -> None:
    """Publish one coherent graph/source checkpoint before removing its write journal."""
    path = runtime.store.directory / "source-transaction.json"
    journal = load_journal(runtime)
    if journal is None:
        return
    if journal.get("kind") != "materialization":
        raise ValueError("Unexpected source transaction during planning commit")
    for identifier in journal.get("mutations", []):
        # Earlier writes to the same source can have been superseded by a later mutation.
        source_mutation.audit_retained(runtime.root, identifier)
    last_by_path = {
        source_mutation.load(runtime.root, identifier)["path"]: identifier
        for identifier in journal.get("mutations", [])
    }
    for identifier in last_by_path.values():
        source_mutation.commit(runtime.root, identifier)
    runtime.state.setdefault("retained_source_mutations", []).extend(journal.get("mutations", []))
    runtime.state["source_transaction"] = journal["id"]
    runtime._persist()
    path.unlink()


def replace_artifact(path: Path, content: bytes) -> None:
    """Install controller-owned compiled cache bytes using an exclusive sibling temporary."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
