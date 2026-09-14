"""Keep exact proof obligations inline while moving bulky evidence into job artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.workflows.prover.session_context import approximate_tokens


def assignment_message(prompt: str, context: dict[str, Any]) -> dict[str, Any]:
    """Render a complete assignment without mutating its controller snapshot."""
    return {
        "role": "user",
        "content": prompt + "\n\nAssignment context:\n" + json.dumps(context, ensure_ascii=False),
    }


def compact_assignment(
    prompt: str, context: dict[str, Any], *, workspace: Path, token_budget: int
) -> tuple[dict[str, Any], list[str]]:
    """Externalize proof bodies and long notes, never claims, edges, status, or plans.

    Graph snapshots include every accumulated proof body, often twice in a review.
    Those bodies are evidence rather than the pinned mathematical contract. Keep
    complete readable artifacts and explicit pointers; if the contract itself is
    too large, admission must still fail without a provider call.
    """
    original = assignment_message(prompt, context)
    if approximate_tokens([original]) <= token_budget:
        return original, []
    reduced = copy.deepcopy(context)
    artifacts: list[str] = []

    def project(value: Any) -> None:
        """Replace only bulky node evidence with exact content-addressed artifacts."""
        if isinstance(value, list):
            for item in value:
                project(item)
        elif isinstance(value, dict):
            for key, item in list(value.items()):
                if (
                    key in {"candidate", "notes"}
                    and len(json.dumps(item, ensure_ascii=False)) > 2000
                ):
                    digest = hashlib.sha256(
                        json.dumps(item, ensure_ascii=False).encode()
                    ).hexdigest()
                    path = workspace / "assignment-evidence" / f"{digest}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_json_write(path, {key: item})
                    artifacts.append(str(path))
                    # Preserve the field's original shape and make the omission explicit.
                    value[key] = [] if isinstance(item, list) else str(item)[:1000]
                    value[key + "_artifact"] = {
                        "path": str(path),
                        "note": "Full evidence retained here; read_file supports pagination. An omitted candidate is not proof acceptance; use the supplied node status.",
                    }
                else:
                    project(item)

    project(reduced)
    current = reduced.get("dag")
    proposed = reduced.get("proposed_dag")
    if isinstance(current, dict) and isinstance(proposed, dict):
        by_id = {
            node.get("id"): node for node in current.get("nodes", []) if isinstance(node, dict)
        }
        referenced = []
        for node in proposed.get("nodes", []):
            if isinstance(node, dict) and node == by_id.get(node.get("id")):
                referenced.append({"id": node["id"], "same_as_current_dag_node": node["id"]})
            else:
                referenced.append(node)
        proposed["nodes"] = referenced
        proposed["node_reference_note"] = (
            "A same_as_current_dag_node entry inherits the COMPLETE unchanged node with that ID "
            "from dag.nodes above, including its exact statement, dependencies and proof status. "
            "All changed and new nodes are supplied in full."
        )
    compacted = assignment_message(prompt, reduced)
    return (
        (compacted, sorted(set(artifacts)))
        if approximate_tokens([compacted]) < approximate_tokens([original])
        else (original, [])
    )
