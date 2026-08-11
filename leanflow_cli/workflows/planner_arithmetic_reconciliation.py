"""Quarantine refuted or explicitly unchecked advisory plan state on reuse.

Planner synthesis now preflights arithmetic before graph insertion, but older
campaigns may already contain advisory nodes and prose derived from a refuted
identity. Interrupted planners may also have persisted candidates whose own
metadata says they are unchecked. This versioned migration reruns the same
conservative preflight on planner-owned claims, quarantines explicitly uncertain
planner/decomposer nodes, scrubs prompt-facing prose that names quarantined state,
and regenerates ``plan.md``. Lean source is never edited.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.workflows import (
    orchestrator_arithmetic_preflight,
    plan_state,
    planner_candidate_admission,
)
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file

PLANNER_ARITHMETIC_RECONCILIATION_KEY = "planner_arithmetic_reconciliation"
PLANNER_ARITHMETIC_RECONCILIATION_VERSION = 3
_RETIREMENT_HISTORY_LIMIT = 80
_KERNEL_BACKED_STATUSES = frozenset({"proved", "false"})
_SUMMARY_SECTIONS = ("grounding_findings", "strategy_notes")

_LOCK = threading.RLock()
_STAMP_CACHE: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}


@dataclass(frozen=True)
class PlannerArithmeticReconciliation:
    """Describe one persisted-state arithmetic reconciliation pass."""

    checked: bool = False
    changed: bool = False
    retired_node_ids: tuple[str, ...] = ()
    demoted_node_ids: tuple[str, ...] = ()
    retired_summary_count: int = 0


def _now_iso() -> str:
    """Return one stable UTC timestamp for migration evidence."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _path_stamp(path: Path) -> tuple[int, int]:
    """Return a cheap process-local invalidation stamp for one artifact."""
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def _artifact_stamp() -> tuple[tuple[int, int], tuple[int, int]]:
    """Return the graph/summary stamp used to avoid repeated JSON scans."""
    paths = plan_state.plan_state_paths()
    return (_path_stamp(paths.blueprint_json), _path_stamp(paths.summary_json))


def _planner_candidates(bp: plan_state.Blueprint) -> tuple[plan_state.GraphNode, ...]:
    """Return advisory planner nodes that lack immutable kernel authority."""
    return tuple(
        node
        for node in bp.nodes
        if node.generated_by.strip().lower() == "planner"
        and node.status not in _KERNEL_BACKED_STATUSES
    )


def _advisory_candidates(bp: plan_state.Blueprint) -> tuple[plan_state.GraphNode, ...]:
    """Return planner/decomposer nodes whose status lacks kernel authority."""
    return tuple(
        node
        for node in bp.nodes
        if node.generated_by.strip().lower() in {"planner", "decomposer"}
        and node.status not in _KERNEL_BACKED_STATUSES
    )


def _normalized_summary_items(summary: Mapping[str, Any], section: str) -> tuple[str, ...]:
    """Return non-empty prompt-facing planner prose as stable strings."""
    raw = summary.get(section)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


def _state_payload(bp: plan_state.Blueprint, summary: Mapping[str, Any]) -> dict[str, Any]:
    """Build the policy-relevant state used for versioned idempotence."""
    return {
        "advisory_nodes": [
            {
                "id": node.id,
                "kind": node.kind,
                "name": node.name,
                "file": node.file,
                "statement": node.statement,
                "status": node.status,
                "notes": node.notes,
                "generated_by": node.generated_by,
            }
            for node in sorted(
                _advisory_candidates(bp),
                key=lambda candidate: (candidate.id, candidate.file, candidate.name),
            )
        ],
        "summary": {
            section: list(_normalized_summary_items(summary, section))
            for section in _SUMMARY_SECTIONS
        },
    }


def _state_sha256(bp: plan_state.Blueprint, summary: Mapping[str, Any]) -> str:
    """Hash only planner arithmetic surfaces, excluding unrelated live state."""
    encoded = json.dumps(
        _state_payload(bp, summary),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _report(text: str) -> orchestrator_arithmetic_preflight.ArithmeticPreflightReport:
    """Run the shared route/synthesis arithmetic authority on one assertion."""
    return orchestrator_arithmetic_preflight.preflight_route_decision({"reason": text})


def _ground_rational_issues(text: str) -> tuple[dict[str, str], ...]:
    """Return only complete-declaration exact rational counterexamples."""
    return tuple(
        dict(issue)
        for issue in _report(text).evidence()
        if str(issue.get("kind", "") or "") == "ground-rational-identity"
    )


def _node_rejections(
    bp: plan_state.Blueprint,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Return direct exact-rational evidence from complete node declarations."""
    rejected: dict[str, tuple[dict[str, Any], ...]] = {}
    for node in _planner_candidates(bp):
        assertion = str(node.statement or "").strip()
        issues = _ground_rational_issues(assertion) if assertion else ()
        if issues:
            rejected[node.id] = ({"field": "statement", "issues": list(issues)},)
    return rejected


def _node_uncertainty_rejections(
    bp: plan_state.Blueprint,
) -> dict[str, tuple[dict[str, Any], ...]]:
    """Return explicit self-disqualification from advisory node metadata."""
    rejected: dict[str, tuple[dict[str, Any], ...]] = {}
    for node in _advisory_candidates(bp):
        evidence = planner_candidate_admission.candidate_uncertainty_evidence(node.to_mapping())
        if evidence:
            rejected[node.id] = tuple(dict(item) for item in evidence)
    return rejected


def _mentions_symbol(text: str, symbol: str) -> bool:
    """Return whether prose names one exact Lean-style declaration symbol."""
    name = str(symbol or "").strip()
    if not name:
        return False
    pattern = rf"(?<![A-Za-z0-9_'.]){re.escape(name)}(?![A-Za-z0-9_'.])"
    return re.search(pattern, text) is not None


def _summary_rejection(
    text: str,
    quarantined_nodes: Mapping[str, plan_state.GraphNode],
    node_evidence: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """Return exact-declaration or quarantined-symbol evidence for one summary item."""
    direct_issues = _ground_rational_issues(text)
    referenced = tuple(
        node_id for node_id, node in quarantined_nodes.items() if _mentions_symbol(text, node.name)
    )
    if not direct_issues and not referenced:
        return None
    result: dict[str, Any] = {}
    if direct_issues:
        result["issues"] = list(direct_issues)
    if referenced:
        result["referenced_retired_node_ids"] = list(referenced)
        result["referenced_node_evidence"] = {
            node_id: [dict(item) for item in node_evidence.get(node_id, ())]
            for node_id in referenced
        }
    return result


def _retirement_fingerprint(record: Mapping[str, Any]) -> str:
    """Return a stable identity for a retirement independent of its timestamp."""
    payload = {key: value for key, value in dict(record).items() if key != "retired_at"}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _merge_retirements(
    existing: Sequence[Any],
    incoming: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Append deduplicated bounded quarantine evidence to migration history."""
    merged = [dict(item) for item in existing if isinstance(item, Mapping)]
    seen = {str(item.get("fingerprint", "") or _retirement_fingerprint(item)) for item in merged}
    for raw in incoming:
        item = dict(raw)
        fingerprint = _retirement_fingerprint(item)
        if fingerprint in seen:
            continue
        item["fingerprint"] = fingerprint
        merged.append(item)
        seen.add(fingerprint)
    return merged[-_RETIREMENT_HISTORY_LIMIT:]


def _migration_is_current(summary: Mapping[str, Any], state_sha256: str) -> bool:
    """Return whether this policy already checked the exact planner surfaces."""
    raw = summary.get(PLANNER_ARITHMETIC_RECONCILIATION_KEY)
    migration = dict(raw) if isinstance(raw, Mapping) else {}
    try:
        version = int(migration.get("policy_version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    return (
        version >= PLANNER_ARITHMETIC_RECONCILIATION_VERSION
        and str(migration.get("state_sha256", "") or "") == state_sha256
    )


def _summary_without_rejections(
    summary: Mapping[str, Any],
    *,
    quarantined_nodes: Mapping[str, plan_state.GraphNode],
    node_evidence: Mapping[str, Sequence[Mapping[str, Any]]],
    retired_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove exact refutations and references to retired graph nodes."""
    cleaned = dict(summary)
    retirements: list[dict[str, Any]] = []
    for section in _SUMMARY_SECTIONS:
        kept: list[str] = []
        for index, text in enumerate(_normalized_summary_items(summary, section)):
            evidence = _summary_rejection(text, quarantined_nodes, node_evidence)
            if evidence is None:
                kept.append(text)
                continue
            retirements.append(
                {
                    "kind": "summary",
                    "section": section,
                    "index": index,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "evidence": evidence,
                    "retired_at": retired_at,
                }
            )
        cleaned[section] = kept
    if not cleaned.get("strategy_notes"):
        cleaned.pop("strategy_notes_scope", None)
    return cleaned, retirements


def reconcile_persisted_planner_arithmetic() -> PlannerArithmeticReconciliation:
    """Apply the versioned persisted-state guard before graph or prompt reuse.

    The migration is replay-safe and process-coordinated through the blueprint
    writer lease. It does not cascade through dependencies: a valid sibling or
    helper survives unless its own stored assertion is deterministically
    refuted. ``proved`` and ``false`` nodes are never reconsidered here because
    their kernel/negation gates outrank advisory arithmetic heuristics.
    """
    if not plan_state.plan_state_enabled():
        return PlannerArithmeticReconciliation()
    paths = plan_state.plan_state_paths()
    cache_key = str(paths.blueprint_json.parent.resolve(strict=False))
    with _LOCK:
        stamp = _artifact_stamp()
        if _STAMP_CACHE.get(cache_key) == stamp:
            return PlannerArithmeticReconciliation()

        with plan_state.blueprint_commit_guard():
            raw_blueprint = read_json_file(paths.blueprint_json)
            raw_summary = read_json_file(paths.summary_json)
            bp = plan_state.Blueprint.from_mapping(raw_blueprint)
            summary = dict(raw_summary)
            candidate_state = _state_payload(bp, summary)
            if not candidate_state["advisory_nodes"] and not any(
                candidate_state["summary"].values()
            ):
                _STAMP_CACHE[cache_key] = _artifact_stamp()
                return PlannerArithmeticReconciliation(checked=True)

            before_sha256 = _state_sha256(bp, summary)
            if _migration_is_current(summary, before_sha256):
                _STAMP_CACHE[cache_key] = _artifact_stamp()
                return PlannerArithmeticReconciliation(checked=True)

            retired_at = _now_iso()
            arithmetic_evidence = _node_rejections(bp)
            uncertainty_evidence = _node_uncertainty_rejections(bp)
            node_evidence = dict(arithmetic_evidence)
            for node_id, evidence in uncertainty_evidence.items():
                node_evidence[node_id] = (*node_evidence.get(node_id, ()), *evidence)
            quarantined_nodes = {node.id: node for node in bp.nodes if node.id in node_evidence}
            # A deterministic counterexample retires a claim. Explicitly
            # unchecked metadata is weaker evidence: keep the idea and its
            # dependency edges as a non-actionable conjecture so downstream
            # nodes cannot become spuriously unblocked.
            retired_node_ids = frozenset(arithmetic_evidence)
            demotion_candidates = frozenset(uncertainty_evidence).difference(retired_node_ids)
            demoted_node_ids = frozenset(
                node.id
                for node in bp.nodes
                if node.id in demotion_candidates and node.status != "conjectured"
            )
            cleaned_bp = replace(
                bp,
                nodes=tuple(
                    (
                        replace(node, status="conjectured")
                        if node.id in demoted_node_ids and node.status != "conjectured"
                        else node
                    )
                    for node in bp.nodes
                    if node.id not in retired_node_ids
                ),
                edges=tuple(
                    edge
                    for edge in bp.edges
                    if edge.source not in retired_node_ids and edge.target not in retired_node_ids
                ),
            )
            cleaned_summary, summary_retirements = _summary_without_rejections(
                summary,
                quarantined_nodes=quarantined_nodes,
                node_evidence=node_evidence,
                retired_at=retired_at,
            )
            node_retirements = [
                {
                    "kind": "node",
                    "node_id": node_id,
                    "name": quarantined_nodes[node_id].name,
                    "file": quarantined_nodes[node_id].file,
                    "status": quarantined_nodes[node_id].status,
                    "action": ("retired" if node_id in retired_node_ids else "demoted"),
                    "evidence": [dict(item) for item in node_evidence[node_id]],
                    "retired_at": retired_at,
                }
                for node_id in sorted(retired_node_ids | demoted_node_ids)
            ]
            retirements = [*node_retirements, *summary_retirements]

            persisted_bp = bp
            if cleaned_bp != bp:
                persisted_bp = plan_state.save_blueprint(cleaned_bp)

            previous_migration = summary.get(PLANNER_ARITHMETIC_RECONCILIATION_KEY)
            previous = dict(previous_migration) if isinstance(previous_migration, Mapping) else {}
            history = _merge_retirements(previous.get("retirements") or [], retirements)
            migration = {
                "policy_version": PLANNER_ARITHMETIC_RECONCILIATION_VERSION,
                "state_sha256": _state_sha256(persisted_bp, cleaned_summary),
                "checked_at": retired_at,
                "retirements": history,
            }

            def mutate(current: dict[str, Any]) -> None:
                current["grounding_findings"] = list(
                    cleaned_summary.get("grounding_findings") or []
                )
                current["strategy_notes"] = list(cleaned_summary.get("strategy_notes") or [])
                if cleaned_summary.get("strategy_notes_scope"):
                    current["strategy_notes_scope"] = dict(cleaned_summary["strategy_notes_scope"])
                else:
                    current.pop("strategy_notes_scope", None)
                current[PLANNER_ARITHMETIC_RECONCILIATION_KEY] = migration
                current["counters"] = plan_state.status_counters(persisted_bp)
                current["version"] = 1
                current["updated_at"] = retired_at

            update_json_file(paths.summary_json, mutate)
            persisted_summary = read_json_file(paths.summary_json)
            changed = bool(retired_node_ids or demoted_node_ids or summary_retirements)
            if changed:
                plan_state.save_plan_md(persisted_bp, persisted_summary)
                plan_state.append_journal_event(
                    {
                        "event": "planner-arithmetic-state-reconciled",
                        "policy_version": PLANNER_ARITHMETIC_RECONCILIATION_VERSION,
                        "retired_node_ids": sorted(retired_node_ids),
                        "demoted_node_ids": sorted(demoted_node_ids),
                        "retired_summary_count": len(summary_retirements),
                        "retirements": retirements,
                    }
                )

            _STAMP_CACHE[cache_key] = _artifact_stamp()
            return PlannerArithmeticReconciliation(
                checked=True,
                changed=changed,
                retired_node_ids=tuple(sorted(retired_node_ids)),
                demoted_node_ids=tuple(sorted(demoted_node_ids)),
                retired_summary_count=len(summary_retirements),
            )
