"""Select deterministic depth-first jobs without duplicating shared prerequisites."""

from __future__ import annotations

from leanflow_cli.workflows.prover.models import Dag, Node


def ready_nodes(dag: Dag, *, order: str, active: set[str], limit: int) -> list[Node]:
    """Return runnable nodes, keeping definitions and trusted acceptance bottom-up."""
    if limit <= 0:
        return []
    dag.validate(max(128, len(dag.nodes)))
    index = dag.by_id()
    seen: set[str] = set()
    ordered: list[Node] = []

    def walk(node_id: str) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        node = index[node_id]
        if order == "top-down":
            ordered.append(node)
        for dependency in node.dependencies:
            walk(dependency)
        if order != "top-down":
            ordered.append(node)

    for root in dag.roots:
        walk(root)
    result: list[Node] = []
    for node in ordered:
        if node.id in active or node.status not in {"pending", "retry"}:
            continue
        dependencies = [index[dep] for dep in node.dependencies]
        if any(dep.status in {"false", "failed", "blocked"} for dep in dependencies):
            continue
        requires_closed = order != "top-down" or node.kind not in {"theorem", "lemma", "example"}
        if requires_closed and any(dep.status != "proved" for dep in dependencies):
            continue
        result.append(node)
        if len(result) >= limit:
            break
    return result
