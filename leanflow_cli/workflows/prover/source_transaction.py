"""Recover interrupted proof installations without adopting untracked source edits."""

from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.utils import atomic_json_write
from leanflow_cli.workflows.prover.models import Dag, Node, digest
from leanflow_cli.workflows.prover.source import (
    SourceDocument,
    project_path,
    read_source,
)

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
    if journal.get("kind") == "materialization":
        recover_materialization(runtime, journal)
        return
    document = SourceDocument(**journal["before_document"])
    source = project_path(runtime.root, document.path)
    current = digest(read_source(source))
    if current not in {journal["before_sha256"], journal["after_sha256"]}:
        raise ValueError(
            f"protected source changed during interrupted proof installation: {document.path}; restore the recorded before or after bytes before resuming"
        )
    if runtime.state.get("source_transaction") == journal["id"]:
        if (
            current != journal["after_sha256"]
            or digest(runtime.documents[document.path].render()) != current
        ):
            raise ValueError("committed proof source differs from its transaction checkpoint")
        path.unlink()
        return
    replace_source(source, document.render().encode("utf-8"))
    runtime.documents[document.path] = document
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
    runtime._persist()
    path.unlink()


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
    atomic_json_write(path, journal)
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
            raise ValueError(f"protected source changed during planning transaction: {relative}")
        entry = {"before": encode(current), "allowed": [encode(current)]}
        journal["writes"][relative] = entry
    elif encode(current) != entry["after"]:
        raise ValueError(f"protected source changed during planning transaction: {relative}")
    entry["after"] = encode(after)
    if entry["after"] not in entry["allowed"]:
        entry["allowed"].append(entry["after"])
    if len(json.dumps(journal)) > 64 * 1024 * 1024:
        raise ValueError("Planning transaction exceeds its 64 MiB recovery limit")
    atomic_json_write(runtime.store.directory / "source-transaction.json", journal)
    # Validate again after journaling so concurrent edits are never adopted as a rollback base.
    if (path.read_bytes() if path.exists() else None) != current:
        raise ValueError(f"protected source changed during planning transaction: {relative}")
    if after is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_source(path, after)


def recover_materialization(runtime: ProverRuntime, journal: dict[str, Any]) -> None:
    """Recover only journaled bytes, validating every file before changing any of them."""
    entries = journal["writes"]
    committed = runtime.state.get("source_transaction") == journal["id"]
    observed: dict[str, bytes | None] = {}
    for relative, entry in entries.items():
        path = project_path(runtime.root, relative)
        current = path.read_bytes() if path.exists() else None
        encoded = base64.b64encode(current).decode() if current is not None else None
        if encoded not in ([entry["after"]] if committed else entry["allowed"]):
            raise ValueError(
                f"protected source changed during interrupted planning transaction: {relative}"
            )
        observed[relative] = current
    if not committed:
        for relative, entry in reversed(list(entries.items())):
            path = project_path(runtime.root, relative)
            if (path.read_bytes() if path.exists() else None) != observed[relative]:
                raise ValueError(
                    f"protected source changed during interrupted planning transaction: {relative}"
                )
            if entry["before"] is None:
                path.unlink(missing_ok=True)
                if relative.startswith("LeanFlowProofs/") and path.suffix == ".lean":
                    artifact = project_path(
                        runtime.root,
                        str(Path(".lake/build/lib/lean") / Path(relative).with_suffix(".olean")),
                    )
                    artifact.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                replace_source(path, base64.b64decode(entry["before"]))
        runtime.documents = {
            key: SourceDocument(**value) for key, value in journal["before_documents"].items()
        }
        runtime.dag = Dag.from_dict(journal["before_dag"])
        runtime.state["changes"] = journal["before_changes"]
        runtime.state["retired_nodes"] = journal["before_retired"]
        runtime._persist()
    (runtime.store.directory / "source-transaction.json").unlink(missing_ok=True)


def commit_materialization(runtime: ProverRuntime) -> None:
    """Publish one coherent graph/source checkpoint before removing its write journal."""
    path = runtime.store.directory / "source-transaction.json"
    if not path.exists():
        return
    journal = json.loads(path.read_text())
    if journal.get("kind") != "materialization":
        raise ValueError("Unexpected source transaction during planning commit")
    runtime.state["source_transaction"] = journal["id"]
    runtime._persist()
    path.unlink()


def replace_source(path: Path, content: bytes) -> None:
    """Atomically replace source using an exclusively created sibling temporary file."""
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
