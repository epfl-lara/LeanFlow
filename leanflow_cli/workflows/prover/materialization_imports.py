"""Rebuild changed helper imports in DAG order before independent signature checks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.models import Dag
from leanflow_cli.workflows.prover.source import SourceDocument, project_path
from leanflow_cli.workflows.prover.source_transaction import record_compilation

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def compile_changed_helpers(
    runtime: ProverRuntime,
    dag: Dag,
    documents: dict[str, SourceDocument],
    journal: dict[str, Any],
) -> None:
    """Recompile changed helpers and their dependents without trusting stale imported artifacts."""
    changed = {
        node.id
        for node in dag.nodes
        if not node.original
        and (
            node.file not in runtime.documents
            or documents[node.file].render() != runtime.documents[node.file].render()
            or not project_path(
                runtime.root,
                str(Path(".lake/build/lib/lean") / Path(node.file).with_suffix(".olean")),
            ).is_file()
        )
    }
    affected: set[str] = set().union(*(dag.affected(node_id) for node_id in changed))
    index = dag.by_id()
    pending = {node_id for node_id in affected if not index[node_id].original}
    while pending:
        ready = sorted(
            node_id for node_id in pending if not pending.intersection(index[node_id].dependencies)
        )
        if not ready:
            raise ValueError("helper module cycle")
        for node_id in ready:
            runtime._ensure_active()
            relative = index[node_id].file
            record_compilation(runtime, journal, relative)
            checked = runtime.verifier.compile_module(relative)
            if checked.get("accepted") is not True:
                raise RuntimeError(str(checked))
            pending.remove(node_id)
