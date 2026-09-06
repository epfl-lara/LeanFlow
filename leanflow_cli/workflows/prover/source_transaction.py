"""Recover interrupted proof installations without adopting untracked source edits."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.utils import atomic_json_write
from leanflow_cli.workflows.prover.models import Node, digest
from leanflow_cli.workflows.prover.source import SourceDocument, read_source, write_source

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def begin_proof(
    runtime: ProverRuntime, node: Node, candidate: list[str], before: SourceDocument, after: str
) -> dict[str, Any]:
    """Durably record both permitted source states before replacing canonical bytes."""
    journal = {
        "id": uuid.uuid4().hex,
        "kind": "proof",
        "path": node.file,
        "node_id": node.id,
        "candidate": candidate,
        "before_document": before.to_dict(),
        "before_sha256": digest(before.render()),
        "after_sha256": digest(after),
    }
    atomic_json_write(runtime.store.directory / "source-transaction.json", journal)
    return journal


def recover_proof(runtime: ProverRuntime) -> None:
    """Roll back an uncommitted install and retain its candidate for fresh verification."""
    path = runtime.store.directory / "source-transaction.json"
    if not path.is_file():
        return
    journal = json.loads(path.read_text())
    document = SourceDocument(**journal["before_document"])
    source = runtime.root / document.path
    current = digest(read_source(source))
    if current not in {journal["before_sha256"], journal["after_sha256"]}:
        raise ValueError(
            f"source changed during interrupted proof installation: {document.path}; restore the recorded before or after bytes before resuming"
        )
    if runtime.state.get("source_transaction") == journal["id"]:
        if (
            current != journal["after_sha256"]
            or digest(runtime.documents[document.path].render()) != current
        ):
            raise ValueError("committed proof source differs from its transaction checkpoint")
        path.unlink()
        return
    write_source(source, document.render())
    runtime.documents[document.path] = document
    if document.generated:
        artifact = (
            runtime.root
            / ".lake"
            / "build"
            / "lib"
            / "lean"
            / Path(document.path).with_suffix(".olean")
        )
        artifact.unlink(missing_ok=True)
    node = runtime.dag.by_id()[journal["node_id"]]
    node.candidate = list(journal["candidate"])
    node.status = "candidate"
    node.conditional_dependencies = [
        dep for dep in node.dependencies if runtime.dag.by_id()[dep].status != "proved"
    ]
    node.notes += "\nRecovered an interrupted proof installation; recheck the retained candidate before promotion."
    runtime._persist()
    path.unlink()
