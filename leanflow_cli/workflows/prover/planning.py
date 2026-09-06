"""Validate model planning proposals before changing the controller-owned DAG."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    declaration_statement_text,
)
from leanflow_cli.workflows.prover.models import Dag, Node, safe_relative_file
from leanflow_cli.workflows.prover.source import lean_code_mask, sorry_spans


def json_report(text: str) -> dict[str, Any]:
    """Read one report object without interpreting prose as a successful operation."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        report = json.loads(text)
    except ValueError:
        return {}
    return report if isinstance(report, dict) else {}


def apply_proposal(
    dag: Dag, proposal: dict[str, Any], *, max_nodes: int, affected: set[str] | None = None
) -> tuple[Dag, dict[str, str]]:
    """Apply a bounded proposal while retaining original statements and unrelated progress."""
    updated = copy.deepcopy(dag)
    index = updated.by_id()
    skeletons: dict[str, str] = {}
    raw_nodes = proposal.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise ValueError("planning nodes must be a list")
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise ValueError("each planned node must be an object")
        node_id = str(raw.get("id", ""))
        existing = index.get(node_id)
        if existing is not None:
            if affected is not None and node_id not in affected:
                continue
            statement = str(raw.get("statement", existing.statement))
            if statement not in {existing.statement, existing.statement + " := by sorry"}:
                raise ValueError("existing statements are immutable; add a different helper ID")
            dependencies = raw.get("dependencies", existing.dependencies)
            if not isinstance(dependencies, list) or not all(
                isinstance(item, str) for item in dependencies
            ):
                raise ValueError("dependencies must be node IDs")
            if existing.dependencies != dependencies:
                if existing.status == "proved":
                    raise ValueError("cannot rewrite dependencies of an independently proved node")
                existing.dependencies = list(dependencies)
                existing.revision += 1
                existing.status = "pending"
            existing.informal_justification = str(
                raw.get("informal_justification", existing.informal_justification)
            )
            continue
        skeleton = str(raw.get("statement", ""))
        entries = _declaration_line_index_from_text(lean_code_mask(skeleton))
        if len(entries) != 1 or len(sorry_spans(skeleton)) != 1:
            raise ValueError(
                "new nodes require one complete declaration with exactly one literal sorry"
            )
        entry = entries[0]
        mask = lean_code_mask(skeleton)
        hole_end = sorry_spans(skeleton)[0][1]
        declaration_offset = sum(
            len(line) for line in mask.splitlines(keepends=True)[: entry["line"] - 1]
        )
        if mask[:declaration_offset].strip() or mask[hole_end:].strip():
            raise ValueError(
                "helper skeleton must end at its single sorry and contain no other commands"
            )
        if entry["kind"] not in {"theorem", "lemma", "def", "abbrev"}:
            raise ValueError("new nodes may be theorem, lemma, def, or abbrev declarations")
        if str(entry["name"]) != str(raw.get("name", "")):
            raise ValueError("planned name does not match its declaration")
        if any(node.name == str(entry["name"]) for node in updated.nodes):
            raise ValueError("a helper may not shadow an existing graph declaration")
        if re.search(
            r"(?m)^\s*(?:axiom|constant|namespace|section|end|import|set_option|run_cmd|#)\b", mask
        ):
            raise ValueError("a planned skeleton may contain only its declaration")
        relative = safe_relative_file(str(raw.get("file", f"LeanFlowProofs/{node_id}.lean")))
        if not relative.startswith("LeanFlowProofs/"):
            raise ValueError("new helper modules must be under LeanFlowProofs/")
        if any(node.file == relative for node in updated.nodes):
            raise ValueError("each new helper requires its own module")
        dependencies = raw.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise ValueError("dependencies must be node IDs")
        node = Node(
            id=node_id,
            name=str(raw["name"]),
            statement=declaration_statement_text(skeleton),
            file=relative,
            module=relative[:-5].replace("/", "."),
            informal_justification=str(raw.get("informal_justification", "")),
            dependencies=list(dependencies),
            kind=str(entry["kind"]),
            holes=[0],
        )
        updated.nodes.append(node)
        index[node_id] = node
        skeletons[node_id] = skeleton
    # The user's roots cannot disappear when a planning direction changes.
    updated.revision += 1
    updated.validate(max_nodes)
    reachable: set[str] = set()
    pending = list(updated.roots)
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(index[node_id].dependencies)
    if set(skeletons) - reachable:
        raise ValueError("new helper nodes must contribute to an original root")
    for helper_id in skeletons:
        helper = index[helper_id]
        for ancestor_id in updated.affected(helper_id) - {helper_id}:
            ancestor = index[ancestor_id]
            if _normalized_claim(helper) == _normalized_claim(ancestor):
                raise ValueError(
                    f"helper {helper_id} restates ancestor {ancestor_id}; split a smaller obligation"
                )
    updated.nodes = [
        node
        for node in updated.nodes
        if node.original or node.status == "proved" or node.id in reachable
    ]
    updated.validate(max_nodes)
    return updated, skeletons


def _normalized_claim(node: Node) -> str:
    """Ignore declaration names and formatting while preserving literals in a claim."""
    signature = re.sub(r"^\s*(?:theorem|lemma|def|abbrev)\s+", "", node.statement)
    if signature.startswith(node.name):
        signature = signature[len(node.name) :]
    return "".join(re.findall(r'"(?:\\.|[^"\\])*"|«[^»]*»|\S', signature))


def planning_prompt(*, reason: str, review: bool = False) -> str:
    """Specify the report protocol and preserve the orchestrator's non-proving role."""
    common = (
        "You are the LeanFlow research orchestrator. Do not prove theorem bodies. "
        "Use the supplied current PLAN and DAG to make one concrete, bounded advance. "
        "Preserve user statements and all verified progress. Research source mathematics first; "
        "provers own Lean lemma search. Reuse local resources before repeat web searches. "
        "No invented axioms. Download sources into your workspace with provenance. "
        "Prefer useful small helpers over broad advice or endless search. "
        f"Current request: {reason}\n"
    )
    if review:
        return common + (
            "Review the proposed graph for mathematical meaning, correct dependency direction, manageable "
            'subproblems and file organization. Return JSON {"accepted": true|false, "critique": "...", "change_kind":"direction"|"decomposition"}. '
            "Acceptance is a semantic planning review; Lean independently checks syntax later."
        )
    return common + (
        'Return JSON {"plan":"complete updated informal proof outline, findings, failed directions and '
        'resource paths", "change_kind":"direction"|"decomposition", "nodes":[...], "research_jobs":[{"question":"..."}], '
        '"libraries":[{"name":"packageName", "git":"https://public-host/repository", "rev":"full immutable Git commit hash"}]}. '
        "Existing nodes use their existing id and statement; dependencies may be updated. New nodes "
        "require id, name, statement (a COMPLETE Lean declaration ending := by sorry), "
        "informal_justification, file (LeanFlowProofs/Name.lean), dependencies (node IDs). "
        "Use a unique module per helper and fully qualified declaration names when needed. "
        "New helper modules can import original imports and other generated helper modules, but cannot "
        "import original goal modules: that would create a circular Lean import. If required definitions "
        "are local to the goal file, use local have statements inside the existing sorry instead. "
        "The original roots must remain. Do not rewrite an existing helper statement; propose a new helper ID. "
        "Do not add an unnecessary helper. research_jobs are optional bounded computation/web tasks, not advice."
        " A changed mathematical proof strategy is direction; splitting the same strategy into smaller obligations is decomposition."
    )
