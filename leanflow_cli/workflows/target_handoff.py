"""Render bounded exact-target graph, research, and negative-route knowledge.

The handoff is deterministic and parent-owned. It gives fresh prover and
decomposer contexts the local facts that a global frontier digest omits while
preserving the authority distinction between kernel-verified helpers,
parent-recheckable worker evidence, and unverified advisor route exclusions.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from leanflow_cli.workflows import (
    advisor_route_facts,
    dispatch_ledger_compaction,
    queued_helper_handoff,
)
from leanflow_cli.workflows.dispatch_models import ASSIGNMENT_REVISION_INPUT_KEY
from leanflow_cli.workflows.plan_state import Blueprint, load_blueprint, load_summary, node_id_for
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

DEFAULT_MAX_CHARS = 20_000
GRAPH_NEIGHBOR_CAP = 8
RESEARCH_FINDING_CAP = 6
RESEARCH_ITEM_CAP = 2_200
WORKER_FACT_EVIDENCE_CAP = 900
NESTED_METHOD_OBSTRUCTION_CAP = 4
NESTED_METHOD_OBSTRUCTION_VALUE_CAP = 360
QUEUED_HELPER_CANDIDATE_CAP = 2
_WORKER_FACT_TOP_LEVEL_KEYS = frozenset(
    {
        "audit_delta",
        "bounded_experiment",
        "checked_delta",
        "checked_helper_status",
        "checked_helpers",
        "concrete_evidence",
        "concrete_new_construction",
        "counterexample",
        "coverage",
        "exact_identity",
        "method_obstruction",
        "non_coverage",
        "obstruction",
        "status",
    }
)
_WORKER_FACT_CODE_KEY_PARTS = (
    "candidate_code",
    "declaration",
    "proof",
    "replacement",
    "statement",
)
_WORKER_FACT_ACTION_KEY_PARTS = (
    "action",
    "command",
    "focus",
    "instruction",
    "integration",
    "message",
    "next",
    "objective",
    "plan",
    "prompt",
    "recommend",
    "route",
    "strategy",
    "todo",
)
_WORKER_FACT_DIRECTIVE_RE = re.compile(
    r"(?:"
    r"\b(?:ignore|disregard|override)\b.{0,80}"
    r"\b(?:context|contract|instruction|parent|prompt|system)\b"
    r"|\b(?:agent|model|worker|you)\s+(?:must|need\s+to|should)\b"
    r"|(?:^|[.!?]\s+)(?:please\s+)?"
    r"(?:continue|edit|focus|implement|insert|invoke|launch|modify|patch|rerun|retry|"
    r"run|search|try|use|write)\b"
    r")",
    flags=re.IGNORECASE | re.DOTALL,
)
_WORKER_FACT_CODE_TEXT_RE = re.compile(
    r"(?:"
    r"```"
    r"|(?:^|\s)(?:(?:private|protected|noncomputable)\s+)*"
    r"(?:lemma|theorem|def)\s+[A-Za-z_][A-Za-z0-9_'.]*"
    r"(?:\s*\([^\n]*\))?\s*(?::|:=)"
    r"|:=\s*by\b"
    r"|(?:^|[;\n]\s*)(?:apply|exact|refine|rw|simpa)\b"
    r")",
    flags=re.IGNORECASE | re.DOTALL,
)
_SEMANTIC_NESTED_KEY_PRIORITY = (
    "s",
    "a",
    "n",
    "x",
    "y",
    "z",
    "witness",
    "instance",
    "q",
    "b",
    "p1",
    "p2",
    "denominator",
    "modulus",
    "residue",
    "bounds",
    "factor_route_consequence",
    "method_obstruction",
)
_SEMANTIC_NESTED_KEY_PRIORITY_INDEX = {
    key: index for index, key in enumerate(_SEMANTIC_NESTED_KEY_PRIORITY)
}
_CHECKED_HELPER_STATUS = "worker_checked_parent_recheck_required"
_CHECKED_HELPER_TOOL = "lean_incremental_check"
_CHECKED_HELPER_ACTION = "check_helper"
_SHA256_RE = re.compile(r"[0-9a-f]{64}", flags=re.IGNORECASE)
_DROP_WORKER_FACT = object()

_OBSTRUCTION_RE = re.compile(
    r"(?:obstruction|route\s+(?:cannot|can't|fails?)|method\s+(?:cannot|can't|fails?)|"
    r"no\s+(?:admissible|required|nonresidual|suitable)|factor\s+(?:failure|limitation)|"
    r"circular|excluded)",
    flags=re.IGNORECASE,
)
_NESTED_OBSTRUCTION_KEY_PARTS = (
    "blocker",
    "counterexample",
    "countermodel",
    "non_coverage",
    "noncoverage",
    "obstruction",
)
_NESTED_OBSTRUCTION_CONTEXT_KEYS = (
    "case",
    "choice",
    "condition",
    "scope",
    "status",
)
_FINITE_INSTANCE_RE = re.compile(
    r"\bs\s*=\s*(\d+)\b(?!\s*\.\.)",
    flags=re.IGNORECASE,
)


def _same_file(left: str, right: str) -> bool:
    """Return whether two persisted paths identify the same source file."""
    if not left or not right:
        return left == right
    project_root = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd())

    def canonical(value: str) -> str:
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded):
            expanded = os.path.join(project_root, expanded)
        return os.path.realpath(expanded)

    return canonical(left) == canonical(right)


def _bounded_line(value: Any, cap: int) -> str:
    """Collapse whitespace and return one bounded prompt line."""
    normalized = " ".join(str(value or "").split()).strip()
    if len(normalized) <= cap:
        return normalized
    return normalized[: max(0, cap - 1)].rstrip() + "…"


def _statement_signature(statement: str) -> str:
    """Return a bounded declaration-head projection, never a stored proof body."""
    text = str(statement or "").strip()
    assignment = text.find(":=")
    if assignment >= 0:
        text = text[:assignment]
    return _bounded_line(text, 380)


def _target_node_ids(
    blueprint: Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> set[str]:
    """Return graph node ids for one exact queue assignment."""
    return {
        node.id
        for node in blueprint.nodes
        if node.name == target_symbol and _same_file(node.file, active_file)
    }


def _assignment_revision(
    blueprint: Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> str:
    """Return the portfolio-compatible statement revision for one assignment."""
    node = blueprint.node_by_id(node_id_for(target_symbol, active_file))
    statement = str(node.statement or "") if node is not None else ""
    if not statement.strip():
        return ""
    return sha256(statement.encode("utf-8")).hexdigest()


def _proved_neighbor_lines(
    blueprint: Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> list[str]:
    """Render direct proved neighbors with edge-authority labels."""
    target_ids = _target_node_ids(
        blueprint,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    if not target_ids:
        return []
    nodes = {node.id: node for node in blueprint.nodes}
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for edge in blueprint.edges:
        neighbor_id = ""
        role = ""
        if edge.kind == "evidence" and edge.target in target_ids:
            neighbor_id = edge.source
            role = "evidence-only"
        elif edge.kind == "depends_on" and edge.source in target_ids:
            neighbor_id = edge.target
            role = "proof-support"
        elif edge.kind == "split_of" and edge.target in target_ids:
            neighbor_id = edge.source
            role = "proof-support"
        elif edge.kind == "alternative_of" and edge.target in target_ids:
            neighbor_id = edge.source
            role = "alternative-evidence"
        if not neighbor_id or neighbor_id in seen:
            continue
        node = nodes.get(neighbor_id)
        if node is None or node.status != "proved" or not _same_file(node.file, active_file):
            continue
        seen.add(neighbor_id)
        signature = _statement_signature(node.statement)
        suffix = f" — {signature}" if signature else ""
        candidates.append((node.name, f"- [{role}] `{node.name}`{suffix}"))
    candidates.sort(key=lambda item: item[0])
    return [line for _name, line in candidates[:GRAPH_NEIGHBOR_CAP]]


def _active_campaign_id(summary: Mapping[str, Any]) -> str:
    """Return the shared summary's current campaign id when present."""
    campaign = summary.get("campaign")
    if isinstance(campaign, Mapping):
        return str(campaign.get("campaign_id", "") or "").strip()
    return ""


def _consumed_target_findings(
    summary: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    assignment_revision: str,
) -> tuple[dict[str, Any], ...]:
    """Return latest exact-target findings in durable completion order.

    ``research_findings`` is only the active delivery materialization. Recover
    older consumed results from the lossless dispatch ledger so a method fact
    cannot vanish immediately before a later audit delta corrects it.
    """
    if not assignment_revision:
        return ()
    campaign_id = _active_campaign_id(summary)
    materialized_by_job: dict[str, dict[str, Any]] = {}
    for raw in summary.get("research_findings") or []:
        if not isinstance(raw, Mapping):
            continue
        finding = dict(raw)
        job_id = str(finding.get("job_id", "") or "").strip()
        finding_campaign = str(finding.get("campaign_id", "") or "").strip()
        if not job_id or not str(finding.get("consumed_at", "") or "").strip():
            continue
        if campaign_id and finding_campaign and campaign_id != finding_campaign:
            continue
        if str(finding.get("target_symbol", "") or "").strip() != target_symbol:
            continue
        if not _same_file(str(finding.get("active_file", "") or ""), active_file):
            continue
        materialized_by_job[job_id] = finding

    selected: list[tuple[str, int, dict[str, Any]]] = []
    ledger_job_ids: set[str] = set()
    hydrated_ledger = dispatch_ledger_compaction.hydrate_dispatch_ledger(
        summary.get("dispatch_ledger") or [],
        state_root=workflow_state_root(),
    )
    for index, raw in enumerate(hydrated_ledger):
        entry = dict(raw)
        spec = entry.get("spec")
        result = entry.get("result")
        if (
            not isinstance(spec, Mapping)
            or not isinstance(result, Mapping)
            or entry.get("consumed") is not True
            or str(entry.get("state", "") or "") != "done"
        ):
            continue
        inputs = spec.get("inputs")
        input_state = dict(inputs) if isinstance(inputs, Mapping) else {}
        job_id = str(spec.get("job_id", "") or "").strip()
        if not job_id:
            continue
        finding_campaign = str(input_state.get("campaign_id", "") or "").strip()
        if campaign_id and finding_campaign and campaign_id != finding_campaign:
            continue
        if str(input_state.get("target_symbol", "") or "").strip() != target_symbol:
            continue
        ledger_file = str(input_state.get("active_file", "") or "")
        if not _same_file(ledger_file, active_file):
            continue
        if (
            str(input_state.get(ASSIGNMENT_REVISION_INPUT_KEY, "") or "").strip()
            != assignment_revision
        ):
            continue
        materialized = materialized_by_job.get(job_id)
        if materialized is not None:
            finding = dict(materialized)
        else:
            deliverable = result.get("deliverable")
            finding = {
                "job_id": job_id,
                "campaign_id": finding_campaign,
                "archetype": str(spec.get("archetype", "") or ""),
                "objective": str(spec.get("objective", "") or ""),
                "target_symbol": target_symbol,
                "active_file": ledger_file,
                "deliverable": dict(deliverable) if isinstance(deliverable, Mapping) else {},
            }
        completed_at = str(
            entry.get("finished_at", "")
            or finding.get("consumed_at", "")
            or entry.get("updated_at", "")
            or ""
        ).strip()
        if not completed_at:
            continue
        finding["consumed_at"] = completed_at
        ledger_job_ids.add(job_id)
        selected.append((completed_at, index, finding))

    offset = len(selected)
    for index, (job_id, finding) in enumerate(materialized_by_job.items()):
        if job_id in ledger_job_ids:
            continue
        if str(finding.get(ASSIGNMENT_REVISION_INPUT_KEY, "") or "").strip() != assignment_revision:
            continue
        consumed_at = str(finding.get("consumed_at", "") or "").strip()
        selected.append((consumed_at, offset + index, finding))
    selected.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in selected)


def consumed_target_findings(
    *,
    target_symbol: str,
    active_file: str,
    summary: Mapping[str, Any],
    blueprint: Blueprint | None,
) -> tuple[dict[str, Any], ...]:
    """Return current-revision consumed findings from the lossless ledger.

    This public projection is for parent-owned recovery work that must survive
    foreground delivery-cache compaction. It deliberately retains delivery
    receipts and never rematerializes or restages the returned evidence.
    """
    if blueprint is None:
        return ()
    return _consumed_target_findings(
        summary,
        target_symbol=str(target_symbol or "").strip(),
        active_file=str(active_file or "").strip(),
        assignment_revision=_assignment_revision(
            blueprint,
            target_symbol=str(target_symbol or "").strip(),
            active_file=str(active_file or "").strip(),
        ),
    )


def _compact_value(value: Any, *, depth: int = 0) -> Any:
    """Project one deliverable value into bounded deterministic JSON data."""
    if depth >= 4:
        return _bounded_line(value, 500)
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        ordered_items = sorted(
            value.items(),
            key=lambda pair: (
                _SEMANTIC_NESTED_KEY_PRIORITY_INDEX.get(
                    str(pair[0]).strip().casefold(),
                    len(_SEMANTIC_NESTED_KEY_PRIORITY),
                ),
                str(pair[0]).casefold(),
                str(pair[0]),
            ),
        )
        for key, item in ordered_items[:16]:
            compact[str(key)] = _compact_value(item, depth=depth + 1)
        if len(value) > 16:
            compact["omitted_key_count"] = len(value) - 16
        return compact
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        compact_items = [_compact_value(item, depth=depth + 1) for item in items[:4]]
        if len(items) > 4:
            compact_items.append({"omitted_item_count": len(items) - 4})
        return compact_items
    if isinstance(value, str):
        return _bounded_line(value, 900)
    return value


def _checked_helper_projection(raw_helpers: Any) -> list[dict[str, Any]]:
    """Keep exact parent-recheckable helper evidence without route-context bulk."""
    if not isinstance(raw_helpers, Sequence) or isinstance(raw_helpers, (str, bytes, bytearray)):
        return []
    projected: list[dict[str, Any]] = []
    for raw in raw_helpers[:2]:
        if not isinstance(raw, Mapping):
            continue
        helper = dict(raw)
        projected.append(
            {
                key: _compact_value(helper[key])
                for key in (
                    "anchor_target_symbol",
                    "declaration",
                    "declaration_sha256",
                    "parent_recheck_required",
                    "worker_check",
                )
                if key in helper
            }
        )
    return projected


def _nested_method_obstructions(value: Any) -> list[dict[str, str]]:
    """Return bounded scalar obstructions nested below model-authored wrappers.

    Research reports commonly place useful route exclusions below containers
    such as ``tested_construction_analysis``. Promote only the explicit
    negative leaf plus a small observational case/scope label. Candidate code,
    proof bodies, directives, and every other sibling remain outside the
    worker-visible consumed-fact channel.
    """
    found: list[dict[str, str]] = []

    def safe_scalar(item: Any, *, cap: int) -> str:
        if not isinstance(item, (str, int)) or isinstance(item, bool):
            return ""
        projected = _code_free_worker_value(item)
        if projected is _DROP_WORKER_FACT:
            return ""
        return _bounded_line(projected, cap)

    def visit(item: Any, *, depth: int = 0) -> None:
        if len(found) >= NESTED_METHOD_OBSTRUCTION_CAP or depth >= 6:
            return
        if isinstance(item, Mapping):
            normalized = {
                str(raw_key).strip().casefold(): (str(raw_key), nested)
                for raw_key, nested in item.items()
            }
            for normalized_key, (raw_key, nested) in sorted(normalized.items())[:32]:
                if len(found) >= NESTED_METHOD_OBSTRUCTION_CAP:
                    return
                obstruction_key = any(
                    marker in normalized_key for marker in _NESTED_OBSTRUCTION_KEY_PARTS
                )
                if obstruction_key:
                    evidence = safe_scalar(
                        nested,
                        cap=NESTED_METHOD_OBSTRUCTION_VALUE_CAP,
                    )
                    if evidence:
                        record: dict[str, str] = {
                            "field": _bounded_line(raw_key, 80),
                            "evidence": evidence,
                        }
                        for context_key in _NESTED_OBSTRUCTION_CONTEXT_KEYS:
                            context = normalized.get(context_key)
                            if context is None:
                                continue
                            context_value = safe_scalar(context[1], cap=180)
                            if context_value:
                                record[context_key] = context_value
                        found.append(record)
                    # A structured negative field may contain a narrower
                    # explicit negative leaf; inspect it without exposing the
                    # surrounding mapping wholesale.
                    if isinstance(nested, (Mapping, list, tuple)):
                        visit(nested, depth=depth + 1)
                    continue
                if _worker_fact_key_is_observational(normalized_key):
                    visit(nested, depth=depth + 1)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in list(item)[:32]:
                visit(nested, depth=depth + 1)
                if len(found) >= NESTED_METHOD_OBSTRUCTION_CAP:
                    return

    visit(value)
    return found


def _finding_projection(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact evidence-first projection of one consumed finding."""
    raw_deliverable = finding.get("deliverable")
    deliverable = dict(raw_deliverable) if isinstance(raw_deliverable, Mapping) else {}
    projection: dict[str, Any] = {}
    preferred = (
        "audit_delta",
        "bounded_experiment",
        "checked_delta",
        "concrete_evidence",
        "concrete_new_construction",
        "conclusion",
        "implication",
        "method_obstruction",
        "obstruction",
        "fundamental_blocker",
        "counterexample",
        "non_coverage",
        "exact_identity",
        "coverage",
        "issues",
        "new_proof_shape",
        "new_route",
        "status",
    )
    for key in preferred:
        if key in deliverable:
            projection[key] = _compact_value(deliverable[key])
    if "method_obstruction" not in projection and "obstruction" not in projection:
        nested_obstructions = _nested_method_obstructions(deliverable)
        if nested_obstructions:
            projection["method_obstruction"] = nested_obstructions
    canonical_helpers = _canonical_parent_checked_helpers(finding)
    if canonical_helpers:
        projection["checked_helper_status"] = _compact_value(deliverable["checked_helper_status"])
    checked_helpers = _checked_helper_projection(canonical_helpers)
    if checked_helpers:
        projection["checked_helpers"] = checked_helpers
    if not projection:
        for key, value in deliverable.items():
            if key in {"parent_route_context", "files_modified"}:
                continue
            projection[str(key)] = _compact_value(value)
            if len(projection) >= 8:
                break
    return projection


def _canonical_parent_checked_helpers(finding: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return exact helpers captured by the parent-observed inline-check contract."""
    deliverable = finding.get("deliverable")
    if not isinstance(deliverable, Mapping):
        return ()
    if (
        str(deliverable.get("checked_helper_status", "") or "") != _CHECKED_HELPER_STATUS
        or deliverable.get("parent_recheck_required") is not True
    ):
        return ()
    raw_helpers = deliverable.get("checked_helpers")
    if not isinstance(raw_helpers, Sequence) or isinstance(raw_helpers, (str, bytes, bytearray)):
        return ()
    canonical: list[dict[str, Any]] = []
    for raw_helper in raw_helpers:
        if not isinstance(raw_helper, Mapping):
            continue
        helper = dict(raw_helper)
        declaration = str(helper.get("declaration", "") or "")
        declaration_sha256 = str(helper.get("declaration_sha256", "") or "").strip()
        worker_check = helper.get("worker_check")
        replacement_declarations = (
            worker_check.get("replacement_declarations")
            if isinstance(worker_check, Mapping)
            else None
        )
        if (
            not declaration.strip()
            or not _SHA256_RE.fullmatch(declaration_sha256)
            or sha256(declaration.encode("utf-8")).hexdigest() != declaration_sha256.casefold()
            or not str(helper.get("anchor_target_symbol", "") or "").strip()
            or not str(helper.get("active_file", "") or "").strip()
            or helper.get("parent_recheck_required") is not True
            or not isinstance(worker_check, Mapping)
            or str(worker_check.get("tool", "") or "") != _CHECKED_HELPER_TOOL
            or str(worker_check.get("action", "") or "") != _CHECKED_HELPER_ACTION
            or worker_check.get("valid_without_sorry") is not True
            or worker_check.get("has_errors") is not False
            or worker_check.get("has_sorry") is not False
            or str(worker_check.get("verification_scope", "") or "") != "helper_candidate"
            or worker_check.get("replacement_matches_target") is not False
            or not isinstance(replacement_declarations, Sequence)
            or isinstance(replacement_declarations, (str, bytes, bytearray))
            or not any(str(value or "").strip() for value in replacement_declarations)
        ):
            continue
        canonical.append(helper)
    return tuple(canonical)


def _queued_child_candidate_lines(
    summary: Mapping[str, Any],
    blueprint: Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> list[str]:
    """Render exact parent findings that match one unchanged queued child stub."""
    binding = queued_helper_handoff.resolve_queued_helper_binding(
        summary,
        blueprint,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    if binding is None:
        return []
    findings = _consumed_target_findings(
        summary,
        target_symbol=binding.parent_symbol,
        active_file=binding.active_file,
        assignment_revision=binding.parent_assignment_revision,
    )
    candidates: list[queued_helper_handoff.QueuedHelperCandidate] = []
    seen: set[str] = set()
    for finding in reversed(findings):
        for helper in reversed(_canonical_parent_checked_helpers(finding)):
            candidate = queued_helper_handoff.candidate_from_checked_helper(
                binding,
                helper,
                job_id=str(finding.get("job_id", "") or ""),
                consumed_at=str(finding.get("consumed_at", "") or ""),
            )
            if candidate is None or candidate.declaration_sha256 in seen:
                continue
            seen.add(candidate.declaration_sha256)
            candidates.append(candidate)
            if len(candidates) >= QUEUED_HELPER_CANDIDATE_CAP:
                break
        if len(candidates) >= QUEUED_HELPER_CANDIDATE_CAP:
            break
    return queued_helper_handoff.render_queued_helper_candidates(binding, candidates)


def _finding_role(finding: Mapping[str, Any], projection: Mapping[str, Any]) -> str:
    """Classify theorem-instance evidence separately from route obstruction."""
    rendered = json.dumps(projection, ensure_ascii=False, sort_keys=True)
    checked = bool(_canonical_parent_checked_helpers(finding))
    if checked and _finite_witness_triplet(projection):
        return "PARENT-RECHECKABLE FINITE INSTANCE WITNESS"
    if _OBSTRUCTION_RE.search(rendered):
        return "METHOD OBSTRUCTION ONLY"
    return "RESEARCH EVIDENCE"


def _finding_scope(role: str) -> str:
    """Return the exact authority boundary for one compact finding role."""
    if role == "PARENT-RECHECKABLE FINITE INSTANCE WITNESS":
        return (
            "parent must recheck the exact helper against current source; if accepted it settles "
            "only the listed finite instance and does not prove the parametric target"
        )
    if role == "METHOD OBSTRUCTION ONLY":
        return (
            "exclude only the named method or premise; this is not instance falsity, target "
            "disproof, or evidence that another proof shape cannot work"
        )
    return "research evidence only; current source and kernel checks remain authoritative"


def _worker_fact_key_is_observational(key: Any) -> bool:
    """Return whether one nested key is data rather than proof text or an action."""
    normalized = str(key).strip().casefold()
    if normalized.endswith("_sha256"):
        return True
    action_key = any(
        normalized == part or normalized.startswith(part + "_") or normalized.endswith("_" + part)
        for part in _WORKER_FACT_ACTION_KEY_PARTS
    )
    return not any(part in normalized for part in _WORKER_FACT_CODE_KEY_PARTS) and not action_key


def _code_free_worker_value(value: Any) -> Any:
    """Project observation-only facts while dropping code and directives recursively."""
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if not _worker_fact_key_is_observational(key):
                continue
            projected = _code_free_worker_value(item)
            if projected is not _DROP_WORKER_FACT:
                compact[str(key)] = projected
        return compact
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        projected_items = [_code_free_worker_value(item) for item in value]
        return [item for item in projected_items if item is not _DROP_WORKER_FACT]
    if isinstance(value, str):
        if _WORKER_FACT_DIRECTIVE_RE.search(value) or _WORKER_FACT_CODE_TEXT_RE.search(value):
            return _DROP_WORKER_FACT
    return value


def _worker_fact_projection(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Remove proof bodies while retaining exact checked-fact metadata."""
    worker_projection: dict[str, Any] = {}
    for key, value in projection.items():
        if str(key) == "new_proof_shape" and isinstance(value, Mapping):
            raw_checked_delta = value.get("checked_delta")
            raw_certificate = (
                raw_checked_delta.get("certificate")
                if isinstance(raw_checked_delta, Mapping)
                else None
            )
            if isinstance(raw_certificate, Mapping):
                certificate = _code_free_worker_value(raw_certificate)
                if certificate is not _DROP_WORKER_FACT and certificate:
                    worker_projection["checked_certificate"] = certificate
            continue
        if str(key) not in _WORKER_FACT_TOP_LEVEL_KEYS:
            continue
        projected = _code_free_worker_value(value)
        if projected is not _DROP_WORKER_FACT:
            worker_projection[str(key)] = projected
    return worker_projection


def _finite_witness_triplet(value: Any) -> str:
    """Return the first explicit x/y/z witness without inspecting Lean proof text."""
    if isinstance(value, Mapping):
        if all(key in value for key in ("x", "y", "z")):
            components = [value.get(key) for key in ("x", "y", "z")]
            if all(isinstance(item, (int, str)) for item in components):
                return ", ".join(
                    f"{key}={_bounded_line(item, 80)}"
                    for key, item in zip(("x", "y", "z"), components, strict=True)
                )
        for item in value.values():
            triplet = _finite_witness_triplet(item)
            if triplet:
                return triplet
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            triplet = _finite_witness_triplet(item)
            if triplet:
                return triplet
    return ""


def _finite_instance_labels(value: Any) -> tuple[str, ...]:
    """Return exact finite parameter labels while rejecting range endpoints."""
    labels: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            if all(key in item for key in ("x", "y", "z")):
                for parameter in ("a", "n"):
                    finite_value = item.get(parameter)
                    if isinstance(finite_value, int) and not isinstance(finite_value, bool):
                        labels.append(f"{parameter}={finite_value}")
                    elif isinstance(finite_value, str) and finite_value.strip().isdigit():
                        labels.append(f"{parameter}={int(finite_value)}")
            for key, nested in item.items():
                normalized_key = str(key).strip().casefold()
                if normalized_key == "s" and (
                    isinstance(nested, int)
                    or (isinstance(nested, str) and nested.strip().isdigit())
                ):
                    labels.append(f"s={int(nested)}")
                visit(nested)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                visit(nested)
        elif isinstance(item, str):
            labels.extend(f"s={match.group(1)}" for match in _FINITE_INSTANCE_RE.finditer(item))

    visit(value)
    return tuple(dict.fromkeys(labels))


def compact_consumed_finding_fact(finding: Mapping[str, Any]) -> dict[str, Any]:
    """Return one bounded worker-visible fact from a consumed finding.

    Progress-ineligible evidence remains mathematical deduplication context.
    The projection keeps exact finite values and method scope but replaces Lean
    declarations with their existing hashes and worker-check metadata.
    """
    projection = _finding_projection(finding)
    worker_projection = _worker_fact_projection(projection)
    role = _finding_role(finding, worker_projection)
    canonical = json.dumps(
        worker_projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    rendered = (
        json.dumps(
            worker_projection,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        if worker_projection
        else ""
    )
    instances = _finite_instance_labels(worker_projection)
    witness = _finite_witness_triplet(worker_projection)
    evidence_sha256 = sha256(canonical.encode("utf-8")).hexdigest()
    if role == "PARENT-RECHECKABLE FINITE INSTANCE WITNESS" and witness:
        # Existing workers may report the same certificate through projections
        # that differ only in whether an ``s`` label survives action-text
        # filtering. Preserve that coalescing, while retaining ``a``/``n`` as
        # essential scope for bounded rational instances such as Erdos 242.
        rational_parameters = tuple(label for label in instances if label.startswith(("a=", "n=")))
        semantic_identity = "\x1f".join((role, *rational_parameters, witness))
    elif role == "PARENT-RECHECKABLE FINITE INSTANCE WITNESS" and instances:
        semantic_identity = "\x1f".join((role, *instances))
    else:
        semantic_identity = "\x1f".join((role, evidence_sha256))
    return {
        "job_id": _bounded_line(finding.get("job_id", ""), 180),
        "consumed_at": _bounded_line(finding.get("consumed_at", ""), 80),
        "role": role,
        "scope": _finding_scope(role),
        "covered_instances": list(instances[:8]),
        "finite_witness": _bounded_line(witness, 280),
        "evidence_excerpt": _bounded_line(rendered, WORKER_FACT_EVIDENCE_CAP),
        "evidence_sha256": evidence_sha256,
        "semantic_key": sha256(semantic_identity.encode("utf-8")).hexdigest()[:24],
    }


def _prioritized_consumed_findings(
    findings: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Retain mandatory correction roles before filling the newest finding window."""
    records = [dict(finding) for finding in findings]
    if len(records) <= RESEARCH_FINDING_CAP:
        return tuple(records)
    roles = [
        _finding_role(finding, _worker_fact_projection(_finding_projection(finding)))
        for finding in records
    ]
    selected: set[int] = set()
    for mandatory_role in (
        "METHOD OBSTRUCTION ONLY",
        "PARENT-RECHECKABLE FINITE INSTANCE WITNESS",
    ):
        for index in range(len(records) - 1, -1, -1):
            if roles[index] == mandatory_role:
                selected.add(index)
                break
    for generic_only in (False, True):
        for index in range(len(records) - 1, -1, -1):
            if index in selected:
                continue
            if (roles[index] == "RESEARCH EVIDENCE") != generic_only:
                continue
            selected.add(index)
            if len(selected) >= RESEARCH_FINDING_CAP:
                break
        if len(selected) >= RESEARCH_FINDING_CAP:
            break
    return tuple(records[index] for index in sorted(selected))


def _finding_lines(
    findings: Sequence[Mapping[str, Any]],
    *,
    include_header: bool = True,
) -> list[str]:
    """Render findings oldest-to-newest so later deltas retain precedence."""
    if not findings:
        return []
    lines = (
        [
            "Consumed exact-target research findings (oldest to newest):",
            "- ordering policy: later checked evidence can refine or overturn earlier route-local "
            "interpretations; a method obstruction never decides theorem-instance status",
        ]
        if include_header
        else []
    )
    for finding in findings:
        projection = _finding_projection(finding)
        role = _finding_role(finding, _worker_fact_projection(projection))
        job_id = _bounded_line(finding.get("job_id", "?"), 180)
        consumed_at = _bounded_line(finding.get("consumed_at", "?"), 80)
        policy = _finding_scope(role)
        rendered = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        rendered = _bounded_line(rendered, RESEARCH_ITEM_CAP)
        lines.extend(
            [
                f"- [{role}] `{job_id}` consumed {consumed_at}",
                f"  scope: {policy}",
                f"  evidence: {rendered}",
            ]
        )
    return lines


def target_knowledge_block(
    *,
    target_symbol: str,
    active_file: str,
    summary: Mapping[str, Any] | None = None,
    blueprint: Blueprint | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Return a bounded authority-labeled handoff for one exact assignment."""
    target = str(target_symbol or "").strip()
    active = str(active_file or "").strip()
    if not target or not active:
        return ""
    state = dict(load_summary() if summary is None else summary)
    graph = load_blueprint() if blueprint is None else blueprint
    neighbor_lines = _proved_neighbor_lines(
        graph,
        target_symbol=target,
        active_file=active,
    )
    route_facts = advisor_route_facts.matching_route_facts(
        state,
        target_symbol=target,
        active_file=active,
    )
    findings = _prioritized_consumed_findings(
        _consumed_target_findings(
            state,
            target_symbol=target,
            active_file=active,
            assignment_revision=_assignment_revision(
                graph,
                target_symbol=target,
                active_file=active,
            ),
        )
    )
    queued_candidate_lines = _queued_child_candidate_lines(
        state,
        graph,
        target_symbol=target,
        active_file=active,
    )
    if not neighbor_lines and not route_facts and not findings and not queued_candidate_lines:
        return ""
    header_lines = [
        "[LEANFLOW TARGET KNOWLEDGE]",
        f"- exact assignment: `{_bounded_line(target, 180)}` ({_bounded_line(active, 260)})",
        "- authority: current Lean source/kernel diagnostics > kernel-verified graph neighbors > "
        "parent-recheckable research evidence > unverified advisor route facts",
    ]
    context_lines: list[str] = []
    if queued_candidate_lines:
        context_lines.extend(queued_candidate_lines)
    if neighbor_lines:
        context_lines.extend(
            [
                "Kernel-verified direct graph neighbors:",
                "- evidence-only neighbors are already banked route facts, not new target progress; "
                "do not recreate, rename, or relabel them as decomposition progress",
                *neighbor_lines,
            ]
        )
    if route_facts:
        context_lines.append("Durable advisory route exclusions:")
        for record in route_facts:
            context_lines.append(
                "- [advisory route exclusion; unverified; not a target disproof] "
                + _bounded_line(record.get("fact_text", ""), 760)
            )
    finding_lines = _finding_lines(findings)
    lines = [*header_lines, *context_lines, *finding_lines]
    rendered = "\n".join(lines)
    cap = max(2_000, int(max_chars or DEFAULT_MAX_CHARS))
    if len(rendered) <= cap:
        return rendered

    fixed = "\n".join(header_lines)
    if queued_candidate_lines:
        # Exact Lean source is correctness data.  Preserve the complete JSON
        # string even when it exceeds the nominal prompt cap; truncating it
        # could turn a valid proof hint into different, plausible-looking code.
        required = "\n".join([fixed, "", *queued_candidate_lines]).rstrip()
        if len(required) >= cap:
            return required
        optional_context = context_lines[len(queued_candidate_lines) :]
        optional_lines = [*optional_context, *_finding_lines(findings)]
        optional = "\n".join(optional_lines)
        remaining = max(0, cap - len(required) - 2)
        if not optional or not remaining:
            return required
        return f"{required}\n\n{optional[:remaining].rstrip()}".rstrip()
    if not findings:
        context = "\n".join(context_lines)
        return f"{fixed}\n\n{context}"[:cap].rstrip()

    # Reserve the tail for exactly the newest selected delta. Older findings
    # stay in the middle and therefore cannot duplicate or displace that delta
    # when a small prompt cap truncates the handoff.
    older_finding_lines = _finding_lines(findings[:-1])
    latest_finding_lines = _finding_lines(
        findings[-1:],
        include_header=not bool(findings[:-1]),
    )
    middle_raw = "\n".join([*context_lines, *older_finding_lines])
    tail_raw = "\n".join(latest_finding_lines)
    available_after_fixed = max(0, cap - len(fixed))
    tail_budget = max(0, available_after_fixed - 2)
    tail = tail_raw[:tail_budget].rstrip()
    remaining = max(0, available_after_fixed - len(tail) - 2)
    if middle_raw and remaining > 2:
        middle = middle_raw[: remaining - 2].rstrip()
        if middle:
            return f"{fixed}\n\n{middle}\n\n{tail}"[:cap].rstrip()
    return f"{fixed}\n\n{tail}"[:cap].rstrip()
