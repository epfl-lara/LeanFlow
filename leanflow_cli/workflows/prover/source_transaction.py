"""Recover interrupted proof installations without adopting untracked source edits."""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.formalization.evidence import digest as bytes_digest
from leanflow_cli.workflows.prover import source_mutation
from leanflow_cli.workflows.prover.materialization_transaction import (
    begin_materialization as begin_materialization,
)
from leanflow_cli.workflows.prover.materialization_transaction import (
    commit_materialization as commit_materialization,
)
from leanflow_cli.workflows.prover.materialization_transaction import (
    materialized_write as materialized_write,
)
from leanflow_cli.workflows.prover.materialization_transaction import (
    record_compilation as record_compilation,
)
from leanflow_cli.workflows.prover.materialization_transaction import (
    recover_materialization as recover_materialization,
)
from leanflow_cli.workflows.prover.models import Node, digest
from leanflow_cli.workflows.prover.source import (
    SourceConflictError,
    SourceDocument,
    project_path,
    read_source,
)
from leanflow_cli.workflows.prover.source_journal import load_journal, save_journal
from leanflow_cli.workflows.prover.source_mutation_recovery import rollback as rollback_mutation

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def begin_proof(
    runtime: ProverRuntime, node: Node, candidate: list[str], before: SourceDocument, after: str
) -> dict[str, Any]:
    """Durably record both permitted source states before replacing canonical bytes."""
    if (
        max(len(before.render().encode("utf-8")), len(after.encode("utf-8")))
        > source_mutation.FILE_LIMIT
    ):
        raise ValueError("Proof source exceeds its 8 MiB recovery limit")
    journal = {
        "id": uuid.uuid4().hex,
        "kind": "proof",
        "path": node.file,
        "node_id": node.id,
        "candidate": candidate,
        "before_document": before.to_dict(),
        "before_change": copy.deepcopy(
            next(
                (
                    change
                    for change in runtime.state["changes"]
                    if change["path"] == str(runtime.root / node.file)
                ),
                None,
            )
        ),
        "before_sha256": digest(before.render()),
        "after_sha256": digest(after),
        "scoped_cache": getattr(runtime.verifier, "scoped_modules", None) is not None,
    }
    if (runtime.store.directory / "source-transaction.json").exists():
        raise ValueError(
            "An earlier source transaction must be recovered before proof installation"
        )
    save_journal(runtime, journal)
    return journal


def install_proof(runtime: ProverRuntime, journal: dict[str, Any], content: bytes) -> None:
    """Journal a capture before installing only into an exclusively emptied source name."""
    document = SourceDocument(**journal["before_document"])
    identifier = source_mutation.create(
        runtime.root, document.path, document.render().encode("utf-8"), content
    )
    journal["mutation"] = identifier
    save_journal(runtime, journal)
    source_mutation.install(runtime.root, identifier)


def finish_proof(runtime: ProverRuntime, journal: dict[str, Any]) -> None:
    """Check retained originals before checkpointing a proof as integrated."""
    identifier = journal["mutation"]
    source_mutation.commit(runtime.root, identifier)
    runtime.state.setdefault("retained_source_mutations", []).append(identifier)


def audit_retained_sources(runtime: ProverRuntime) -> None:
    """Block future proof authority when an old descriptor changes retained source bytes."""
    for identifier in runtime.state.get("retained_source_mutations", []):
        source_mutation.audit_retained(runtime.root, identifier)


def recover_proof(runtime: ProverRuntime) -> None:
    """Roll back an uncommitted install and retain its candidate for fresh verification."""
    from leanflow_cli.formalization.publication import publication_guard

    with publication_guard(runtime.root):
        _recover_proof_locked(runtime)


def _recover_proof_locked(runtime: ProverRuntime) -> None:
    """Recover source bytes only while formalization cannot publish another generation."""
    path = runtime.store.directory / "source-transaction.json"
    journal = load_journal(runtime)
    if journal is None:
        return
    if journal.get("kind") == "materialization":
        recover_materialization(runtime, journal)
        return
    document = SourceDocument(**journal["before_document"])
    source = project_path(runtime.root, document.path)
    if journal.get("mutation"):
        mutation = source_mutation.load(runtime.root, journal["mutation"])
        if (
            mutation["path"] != document.path
            or (mutation["before"]["sha256"] if mutation["before"] else None)
            != journal["before_sha256"]
            or (mutation["after"]["sha256"] if mutation["after"] else None)
            != journal["after_sha256"]
        ):
            raise SourceConflictError("Prover source capture differs from its run transaction")
    if journal.get("legacy_rollback_mutation"):
        legacy_identifier = journal.pop("legacy_rollback_mutation")
        rollback_mutation(runtime.root, legacy_identifier)
        retained = runtime.state.setdefault("retained_source_mutations", [])
        if legacy_identifier not in retained:
            retained.append(legacy_identifier)
        save_journal(runtime, journal)
    current_bytes = read_source(source).encode("utf-8") if source.exists() else None
    current = bytes_digest(current_bytes) if current_bytes is not None else None
    if not journal.get("mutation") and current not in {
        journal["before_sha256"],
        journal["after_sha256"],
    }:
        raise SourceConflictError(
            f"protected source changed during interrupted proof installation: {document.path}; restore the recorded before or after bytes before resuming"
        )
    if runtime.state.get("source_transaction") == journal["id"]:
        if (
            current != journal["after_sha256"]
            or digest(runtime.documents[document.path].render()) != current
        ):
            raise SourceConflictError(
                "committed proof source differs from its transaction checkpoint"
            )
        if journal.get("mutation"):
            source_mutation.validate_installed(runtime.root, journal["mutation"])
            retained = runtime.state.setdefault("retained_source_mutations", [])
            if journal["mutation"] not in retained:
                retained.append(journal["mutation"])
                runtime._persist()
        path.unlink()
        return
    scoped = (
        journal.get("scoped_cache") is True
        or getattr(runtime.verifier, "scoped_modules", None) is not None
    )
    if scoped:
        from leanflow_cli.workflows.prover.scoped_cache import mark_cache_refresh_needed

        mark_cache_refresh_needed(runtime.root, "Proof source rolled back: " + document.path)
    if journal.get("mutation"):
        rollback_mutation(runtime.root, journal["mutation"])
        runtime.state.setdefault("retained_source_mutations", []).append(journal["mutation"])
    elif current != journal["before_sha256"]:
        # Old run journals lack captures: preserve the exact pre-barrier observation.
        identifier = source_mutation.create(
            runtime.root,
            document.path,
            current_bytes,
            document.render().encode("utf-8"),
        )
        journal["legacy_rollback_mutation"] = identifier
        save_journal(runtime, journal)
        source_mutation.install(runtime.root, identifier)
        source_mutation.commit(runtime.root, identifier)
        runtime.state.setdefault("retained_source_mutations", []).append(identifier)
    runtime.documents[document.path] = document
    if not scoped:
        artifact = project_path(
            runtime.root,
            str(Path(".lake/build/lib/lean") / Path(document.path).with_suffix(".olean")),
        )
        artifact.unlink(missing_ok=True)
    node = runtime.dag.by_id()[journal["node_id"]]
    node.candidate = list(journal["candidate"])
    node.status = "candidate"
    node.conditional_dependencies = [
        dep for dep in node.dependencies if runtime.dag.by_id()[dep].status != "proved"
    ]
    node.notes += "\nRecovered an interrupted proof installation; recheck the retained candidate before promotion."
    current_change = next(
        (change for change in runtime.state["changes"] if change["path"] == str(source)), None
    )
    previous_change = journal.get("before_change")
    if current_change is not None:
        failed_agent = current_change.get("agent_id", "")
        if previous_change is not None:
            current_change.clear()
            current_change.update(previous_change)
            current_change["last_attempt"] = {"status": "rolled_back", "agent_id": failed_agent}
        else:
            current_change.update(status="rolled_back", pending=False)
    runtime._persist()
    path.unlink()
