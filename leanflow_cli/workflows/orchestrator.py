"""Select deterministic routes for managed proof workflows.

This pure leaf applies an ordered route table to a frozen ``RouteContext``.
Stalls, budget breakpoints, and retry exhaustion become route decisions; the
execution layer and runner own every side effect.

The floor consumes the existing classifier output from
``route_workflow_step`` and extends it without duplicating classification.
The optional LLM layer may refine eligible routes separately. Easy runs retain
the no-op ``direct-prove`` path.

``ask-human`` is available only when the workflow explicitly enables human
review. Otherwise the floor records uncertainty through autonomous planning
routes and leaves the source statement unchanged.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from leanflow_cli.lean import negation_probe
from leanflow_cli.lean.lean_parsing import _statement_signature_text
from leanflow_cli.workflows import (
    campaign_epoch,
    negation_promotion,
    research_semantic_identity,
)
from leanflow_cli.workflows.orchestrator_coverage import statement_shape_compatibility
from leanflow_cli.workflows.plan_state import Blueprint, node_id_for
from leanflow_cli.workflows.queue_manager import TheoremKey, TheoremQueueManager
from leanflow_cli.workflows.research_findings import (
    canonical_checked_helpers,
    relevant_findings,
)

ROUTES = (
    "direct-prove",
    "decompose",
    "plan",
    "negate",
    "park",
    "re-state",
    "escalate",
    "ask-human",  # Human review for ambiguous scope or statement-fidelity concerns.
)
TRIGGERS = ("scope-entry", "stall", "budget-breakpoint", "retry-exhausted", "event")

#: Mirrors native_runner.MANAGER_HARD_RETRY_LIMIT (layering forbids importing
#: the runner; the equality is pinned by tests, struggle_signals-style).
HARD_RETRY_LIMIT = 2

#: Outcome statuses that count as unresolved work for routing purposes.
UNRESOLVED_OUTCOME_STATUSES = frozenset(
    {"blocked", "deferred", "reverted-to-sorry", "skipped", "unknown"}
)

#: Negation statuses that mean "a probe has not conclusively run yet".
NEGATION_UNATTEMPTED = frozenset({"", "none", "not-attempted", "probe-proposed"})

#: Routes a prover may explicitly request when reporting an unresolved turn.
#: These are all non-terminal strategy changes; verdict and parking authority
#: remain with the deterministic manager and kernel gates.
PROVER_REQUESTED_ROUTES = frozenset({"decompose", "plan", "negate"})
PROVER_ROUTE_REASON_MAX_CHARS = 1600
SEMANTIC_REFRESH_ROUTE = "refresh-portfolio"
_PROVER_ROUTE_MARKER_RE = re.compile(
    r"^\s*(?:[-+*]\s+)?"
    r"(?:(?:blocked|stalled)\s*(?:[:\-\u2013\u2014]\s*))?"
    r"(?:requested\s+(?:(?:next|continuation|continuing)\s+)?route|route\s+requested)"
    r"\s*(?::|=|is\b|[-\u2013\u2014])\s*"
    r"[`*_]{0,2}(?P<route>decompose|plan|negate)\b[`*_]{0,2}"
    r"(?P<suffix>.*)$",
    flags=re.IGNORECASE,
)
_PROVER_ROUTE_REASON_LINE_RE = re.compile(
    r"^\s*(?:[-+*]\s+)?reason\s*:\s*(?P<reason>\S.*)$",
    flags=re.IGNORECASE,
)
_PROVER_ROUTE_FENCE_RE = re.compile(r"^\s{0,3}(?P<fence>`{3,}|~{3,})")
_PROVER_ROUTE_DENIAL_RE = re.compile(
    r"\b(?:no|not|never|cannot|can't|invalid)\b"
    r"|\b(?:example|sample|template|hypothetical|illustrative|illustration)\b"
    r"|\b(?:quote|quoted|quoting|reject|rejected|rejecting)\b"
    r"|\bprior\s+report\b",
    flags=re.IGNORECASE,
)
_PROVER_ROUTE_REASON_CONTRADICTION_RE = re.compile(
    r"\b(?:reject(?:ed|ing)?|invalid)\b"
    r"|\bnot\s+valid\b"
    r"|\b(?:do\s+not|don't|cannot|can't|never)\s+"
    r"(?:use|take|follow|select|honou?r|request)\b",
    flags=re.IGNORECASE,
)
_PROVER_ROUTE_TOKEN_RE = re.compile(r"\b(?:decompose|plan|negate)\b", flags=re.IGNORECASE)
_COUNTEREXAMPLE_DELIVERABLE_KEYS = frozenset(
    {
        "counterexample",
        "counterexample_evidence",
        "countermodel",
        "refutation",
        "refutation_witness",
    }
)
_COUNTEREXAMPLE_NAME_RE = re.compile(
    r"(?:^|[._'])(?:counterexample|countermodel|false|is_false|negation|refutation|refutes?)(?:$|[._'])",
    flags=re.IGNORECASE,
)
_EVIDENCE_SUPPORTED_NEGATE_REQUEST_RE = re.compile(
    r"\brequested\s+(?:next\s+)?route(?:\s*(?::|=|-)|\s+is\b)?" r"\s*[`*_]*negate\b",
    flags=re.IGNORECASE,
)
_EVIDENCE_SUPPORTED_NEGATE_RESOLUTION_RE = re.compile(
    r"\brequired\s+resolution\s*:[^\n]{0,500}\broute\s+"
    r"(?:it|this|the\s+(?:statement|goal|declaration|candidate))\s+as\s+"
    r"(?:an?\s+)?negated\s+obstruction\b",
    flags=re.IGNORECASE,
)
_EVIDENCE_SUPPORTED_NEGATE_DENIAL_RE = re.compile(
    r"(?:"
    r"\b(?:do\s+not|don't|cannot|can't|never|must\s+not|should\s+not|not)\s+"
    r"(?:to\s+)?"
    r"(?:request(?:ed|ing)?|route|routing|use|choose|select)\b"
    r"[^\n.!?]{0,160}\bnegat(?:e|ed|ion)\b"
    r"|\bnon[-\s]+negated\s+obstruction\b"
    r"|\broute\s+(?:it|this|the\s+(?:statement|goal|declaration|candidate))\s+as\s+"
    r"(?:an?\s+)?negated\s+obstruction\b[^\n.!?]{0,80}"
    r"\b(?:is|would\s+be)\s+(?:not\s+valid|invalid|unsound|incorrect|wrong)\b"
    r")",
    flags=re.IGNORECASE,
)


def orchestrator_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_ORCHESTRATOR_ENABLED", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def human_review_enabled() -> bool:
    """Return whether this workflow explicitly permits human-review routes."""
    raw = str(os.getenv("LEANFLOW_HUMAN_REVIEW_ENABLED", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def orchestrator_max_routes() -> int:
    raw = str(os.getenv("LEANFLOW_ORCHESTRATOR_MAX_ROUTES", "") or "").strip()
    try:
        value = int(raw) if raw else 4
    except ValueError:
        value = 4
    return max(1, value)


@dataclass(frozen=True)
class RouteContext:
    """Capture all inputs available to one orchestrator invocation."""

    trigger: str = "scope-entry"
    workflow_kind: str = "prove"
    route_decision: Mapping[str, Any] = field(default_factory=dict)
    active_file: str = ""
    target_symbol: str = ""
    target_statement: str = ""
    declaration_queue_total: int = 0
    sorry_count: int = 0
    project_sorry_count: int = 0
    diagnostics: str = ""
    blocker_summary: str = ""
    queue_frontier_exhausted: bool = False
    deferred_exact_verification: bool = False
    requested_route: str = ""
    requested_route_reason: str = ""
    stable_cycles: int = 0
    blocked_runs: int = 0
    attempt_count: int = 0
    hard_retries: int = 0
    warning_retries: int = 0
    pending_count: int = 0
    unresolved_outcomes: int = 0
    search_exhausted: bool = False
    graph_frontier: tuple[str, ...] = ()  # target-scoped dependency node names
    graph_unrelated_frontier: tuple[str, ...] = ()  # campaign-global scheduling inventory
    graph_blocked: tuple[str, ...] = ()
    target_node_status: str = ""
    target_node_found: bool = False  # the graph positively knows this node
    target_is_sublemma: bool = False
    target_generated_by: str = ""
    fidelity_suspect: bool = False  # statement-fidelity audit said BLOCK
    negation_status: str = ""  # summary probe verdict, packet status as fallback
    negation_proved: bool = False  # this run revalidated the requested-root disproof
    negation_probe_budget_remaining: int | None = None
    negation_refresh_evidence_key: str = ""
    negation_refresh_retry_consumed: bool = False
    plan_md_exists: bool = False
    decision_packet: Mapping[str, Any] = field(default_factory=dict)
    research_findings: tuple[Mapping[str, Any], ...] = ()
    verified_graph_facts: tuple[Mapping[str, Any], ...] = ()
    verified_counterexample_evidence: tuple[Mapping[str, Any], ...] = ()
    failed_route_signatures: tuple[str, ...] = ()
    # Compatibility name for the campaign's durable no-progress route streak.
    routes_used_this_scope: int = 0
    research_mode: bool = False
    epoch_refresh_required: bool = False
    semantic_refresh_work_due: bool = False
    previous_epoch_routes: tuple[str, ...] = ()
    current_epoch_routes: tuple[str, ...] = ()
    semantic_route_history: tuple[Mapping[str, Any], ...] = ()

    def has_queue_item(self) -> bool:
        return bool(self.target_symbol and self.active_file)


@dataclass(frozen=True)
class OrchestratorRoute:
    """One routing decision: the action plus why (source is 'deterministic'
    for the floor; the Phase-6 LLM layer emits 'llm')."""

    route: str
    reason: str
    target: Mapping[str, Any] = field(default_factory=dict)
    source: str = "deterministic"


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit]


def _as_int(value: Any, default: int = 0) -> int:
    """Tolerant coercion for persisted values — totality over garbage state."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _negation_refresh_evidence_key(
    *,
    storage_key: str,
    target_statement: str,
    probe_entry: Mapping[str, Any] | None,
) -> str:
    """Return a stable identity for one target's current negation evidence."""
    probe = dict(probe_entry or {})
    evidence = dict(probe.get("promotion_evidence") or {})
    statement_digest = hashlib.sha256(str(target_statement or "").encode("utf-8")).hexdigest()
    declaration_identity = str(
        evidence.get("declaration_signature_sha256", "") or statement_digest
    ).strip()
    source_identity = str(evidence.get("source_revision_sha256", "") or statement_digest).strip()
    material = "\0".join((storage_key, declaration_identity, source_identity, statement_digest))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _RequestedRouteMarker:
    """Hold one dedicated marker and its optional adjacent evidence line."""

    route: str
    line_index: int
    marker_line: str
    reason_line: str = ""

    @property
    def evidence(self) -> str:
        """Return only authority-bearing report lines."""
        return "\n".join(part for part in (self.marker_line, self.reason_line) if part)


def _route_report_lines(text: str) -> tuple[tuple[int, str], ...]:
    """Return non-quoted report lines outside Markdown fences."""
    eligible: list[tuple[int, str]] = []
    fence_character = ""
    fence_length = 0
    for index, raw_line in enumerate(str(text or "").splitlines()):
        stripped = raw_line.strip()
        if raw_line.lstrip().startswith(">"):
            continue
        fence_match = _PROVER_ROUTE_FENCE_RE.match(raw_line)
        if fence_character:
            if (
                fence_match is not None
                and fence_match.group("fence")[0] == fence_character
                and len(fence_match.group("fence")) >= fence_length
            ):
                fence_character = ""
                fence_length = 0
            continue
        if fence_match is not None:
            fence = fence_match.group("fence")
            fence_character = fence[0]
            fence_length = len(fence)
            continue
        if stripped:
            eligible.append((index, stripped))
    return tuple(eligible)


def _route_marker_suffix_is_explicit(suffix: str) -> bool:
    """Return whether trailing marker text is punctuation or explicit evidence."""
    trailing = str(suffix or "").strip()
    if not trailing or re.fullmatch(r"[.!?]+", trailing):
        return True
    if trailing.startswith("/"):
        detail = trailing[1:].strip()
    else:
        detail_match = re.match(
            r"^(?:[,;:]|[-\u2013\u2014])\s*(?P<detail>\S.*)$",
            trailing,
        )
        if detail_match is None:
            return False
        detail = str(detail_match.group("detail") or "").strip()
    return bool(detail and not _PROVER_ROUTE_TOKEN_RE.search(detail))


def _route_reason_line(
    indexed_lines: Mapping[int, str],
    marker: _RequestedRouteMarker,
) -> str | None:
    """Return an adjacent reason, or ``None`` when it contradicts the marker."""
    candidate = str(indexed_lines.get(marker.line_index + 1, "") or "").strip()
    if not candidate:
        return ""
    match_text = candidate.replace("**", "").replace("__", "")
    match = _PROVER_ROUTE_REASON_LINE_RE.fullmatch(match_text)
    if match is None:
        return ""
    reason = str(match.group("reason") or "").strip()
    if (
        not reason
        or _PROVER_ROUTE_REASON_CONTRADICTION_RE.search(reason)
        or _PROVER_ROUTE_MARKER_RE.fullmatch(reason.replace("**", "").replace("__", ""))
    ):
        return None
    return candidate


def _requested_route_marker(text: str) -> tuple[str, str]:
    """Parse one fail-closed route marker and its bounded evidence source."""
    lines = _route_report_lines(text)
    indexed_lines = dict(lines)
    markers: list[_RequestedRouteMarker] = []
    for index, line in lines:
        match_text = line.replace("**", "").replace("__", "")
        match = _PROVER_ROUTE_MARKER_RE.fullmatch(match_text)
        if match is None or _PROVER_ROUTE_DENIAL_RE.search(match_text):
            continue
        suffix = str(match.group("suffix") or "")
        if not _route_marker_suffix_is_explicit(suffix):
            continue
        route = str(match.group("route") or "").strip().lower()
        if route not in PROVER_REQUESTED_ROUTES:
            continue
        marker = _RequestedRouteMarker(
            route=route,
            line_index=index,
            marker_line=line,
        )
        reason_line = _route_reason_line(indexed_lines, marker)
        if reason_line is None:
            continue
        markers.append(
            _RequestedRouteMarker(
                route=route,
                line_index=index,
                marker_line=line,
                reason_line=reason_line,
            )
        )
    routes = {marker.route for marker in markers}
    if len(routes) != 1:
        return "", ""
    marker = markers[0]
    return marker.route, marker.evidence


def requested_route_from_text(text: str) -> str:
    """Return one unambiguous route from a dedicated prover-report marker."""
    route, _reason = _requested_route_marker(str(text or ""))
    return route


def bounded_requested_route_reason(text: str, route: str = "") -> str:
    """Return only a matched route line and its adjacent strict reason line."""
    parsed_route, evidence = _requested_route_marker(str(text or ""))
    requested = str(route or parsed_route or "").strip().lower()
    if requested not in PROVER_REQUESTED_ROUTES or parsed_route != requested:
        return ""
    return _truncate(evidence, PROVER_ROUTE_REASON_MAX_CHARS)


def _same_assignment_file(left: str, right: str) -> bool:
    """Return whether two paths identify the same normalized queue file."""
    if not left or not right:
        return left == right
    try:
        return (
            TheoremKey.make("__leanflow_scope__", left).active_file
            == TheoremKey.make("__leanflow_scope__", right).active_file
        )
    except Exception:
        return False


def _structured_counterexample_marker(finding: Mapping[str, Any]) -> bool:
    """Return whether a finding carries counterexample data rather than prose."""
    raw_deliverable = finding.get("deliverable")
    if not isinstance(raw_deliverable, Mapping):
        return False
    for key in _COUNTEREXAMPLE_DELIVERABLE_KEYS:
        if key not in raw_deliverable:
            continue
        value = raw_deliverable.get(key)
        if isinstance(value, Mapping):
            return bool(value)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return bool(value)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return True
    return False


def _declaration_result_type(statement: str) -> str:
    """Return the top-level result type from one Lean declaration slice."""
    signature = _statement_signature_text(str(statement or ""))
    depth = 0
    for index, character in enumerate(signature):
        if character in "([{":
            depth += 1
            continue
        if character in ")]}":
            depth = max(0, depth - 1)
            continue
        if character != ":" or depth:
            continue
        previous = signature[index - 1] if index else ""
        following = signature[index + 1] if index + 1 < len(signature) else ""
        if previous == ":" or following == ":":
            continue
        return signature[index + 1 :].strip()
    return ""


def _strip_result_type_outer_parens(result_type: str) -> str:
    """Remove parentheses only when they enclose the complete result type."""
    current = str(result_type or "").strip()
    while current.startswith("(") and current.endswith(")"):
        depth = 0
        encloses_all = True
        for index, character in enumerate(current):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(current) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        current = current[1:-1].strip()
    return current


def _result_type_has_top_level_conditional(result_type: str) -> bool:
    """Return whether the result only characterizes a negative proposition."""
    current = _strip_result_type_outer_parens(result_type)
    depth = 0
    for index, character in enumerate(current):
        if character in "([{":
            depth += 1
            continue
        if character in ")]}":
            depth = max(0, depth - 1)
            continue
        if depth:
            continue
        if character in {"→", "↔"}:
            return True
        if current.startswith("->", index) or current.startswith("<->", index):
            return True
    return False


def _declaration_is_counterexample_evidence(statement: str) -> bool:
    """Return whether a proved declaration has an explicit negative result."""
    result_type = _declaration_result_type(statement)
    if not result_type or _result_type_has_top_level_conditional(result_type):
        return False
    if "¬" in result_type or "≠" in result_type:
        return True
    if re.search(r"\bNot\b", result_type):
        return True
    if re.search(r"(?:=\s*False\b|\bFalse\s*=)", result_type):
        return True
    return bool(re.match(r"^False\b", result_type))


def _canonical_proposition_text(value: str) -> str:
    """Return a whitespace-insensitive proposition identity for exact matching."""
    return re.sub(r"\s+", "", _strip_result_type_outer_parens(value))


def _declaration_directly_negates_target(statement: str, target_statement: str) -> bool:
    """Return whether a declaration concludes the exact target proposition's negation."""
    result_type = _strip_result_type_outer_parens(_declaration_result_type(statement))
    target_type = _declaration_result_type(target_statement)
    if not result_type or not target_type:
        return False
    operand = ""
    if result_type.startswith("¬"):
        operand = result_type[1:].strip()
    else:
        match = re.match(r"^Not\b(.*)$", result_type, flags=re.DOTALL)
        if match is not None:
            operand = match.group(1).strip()
    return bool(
        operand and _canonical_proposition_text(operand) == _canonical_proposition_text(target_type)
    )


def _checked_counterexample_declarations(
    findings: Sequence[Mapping[str, Any]],
    *,
    target_symbol: str,
    active_file: str,
) -> dict[str, set[str]]:
    """Index exact-target checked helper declarations labeled as counterexamples."""
    indexed: dict[str, set[str]] = {}
    for finding in findings:
        if (
            str(finding.get("target_symbol", "") or "").strip() != target_symbol
            or not _same_assignment_file(
                str(finding.get("active_file", "") or ""),
                active_file,
            )
            or not _structured_counterexample_marker(finding)
        ):
            continue
        for helper in canonical_checked_helpers(finding):
            if str(
                helper.get("anchor_target_symbol", "") or ""
            ).strip() != target_symbol or not _same_assignment_file(
                str(helper.get("active_file", "") or ""),
                active_file,
            ):
                continue
            declaration = str(helper.get("declaration", "") or "")
            declaration_hash = hashlib.sha256(declaration.strip().encode("utf-8")).hexdigest()
            worker_check = dict(helper.get("worker_check") or {})
            names = {
                str(name or "").strip()
                for name in (worker_check.get("replacement_declarations") or ())
                if str(name or "").strip()
            }
            if declaration.strip() and names:
                indexed.setdefault(declaration_hash, set()).update(names)
    return indexed


def verified_counterexample_evidence(
    blueprint: Blueprint | None,
    findings: Sequence[Mapping[str, Any]],
    *,
    target_symbol: str,
    active_file: str,
) -> tuple[Mapping[str, Any], ...]:
    """Return proved counterexample helpers attached to the exact target.

    A graph ``proved`` status is parent-kernel authority, while the evidence
    edge supplies exact target scope. A helper must additionally expose a
    negative result or match a schema-valid structured counterexample finding;
    blocker prose and unrelated proved helpers cannot enter this set.
    """
    if blueprint is None or not target_symbol or not active_file:
        return ()
    target_id = node_id_for(target_symbol, active_file)
    target_node = blueprint.node_by_id(target_id)
    if (
        target_node is None
        or target_node.name != target_symbol
        or not _same_assignment_file(target_node.file, active_file)
    ):
        return ()
    checked = _checked_counterexample_declarations(
        findings,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    nodes_by_id = {node.id: node for node in blueprint.nodes}
    evidence: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for edge in blueprint.edges:
        if edge.kind != "evidence" or edge.target != target_id or edge.source in seen:
            continue
        node = nodes_by_id.get(edge.source)
        if (
            node is None
            or node.status != "proved"
            or not node.name
            or not node.statement.strip()
            or not _same_assignment_file(node.file, active_file)
        ):
            continue
        declaration_hash = hashlib.sha256(node.statement.strip().encode("utf-8")).hexdigest()
        matched_names = checked.get(declaration_hash, set())
        matched_finding = node.name in matched_names or node.name.split(".")[-1] in {
            name.split(".")[-1] for name in matched_names
        }
        explicit_negative = _declaration_is_counterexample_evidence(node.statement)
        direct_target_negation = _declaration_directly_negates_target(
            node.statement,
            target_node.statement,
        )
        named_counterexample = bool(_COUNTEREXAMPLE_NAME_RE.search(node.name))
        # A negative conclusion on an evidence edge is commonly an ordinary
        # proof premise (for example, an arithmetic non-equality). It earns a
        # negation route only when it directly negates the target, carries an
        # explicit counterexample name, or is authenticated by a structured
        # checked-counterexample finding.
        if not (
            direct_target_negation
            or (named_counterexample and explicit_negative)
            or matched_finding
        ):
            continue
        seen.add(node.id)
        evidence.append(
            {
                "node_id": node.id,
                "name": node.name,
                "statement": _truncate(node.statement, 1800),
                "basis": (
                    "proved-direct-target-negation"
                    if direct_target_negation
                    else (
                        "proved-named-negative-target-evidence"
                        if named_counterexample and explicit_negative
                        else "proved-target-evidence-matched-structured-finding"
                    )
                ),
            }
        )
    evidence.sort(key=lambda item: (str(item.get("name", "")), str(item.get("node_id", ""))))
    return tuple(evidence[:8])


def evidence_supported_negate_request_from_text(
    text: str,
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    """Return ``negate`` for the loose prover spelling only with verified evidence."""
    if not evidence:
        return ""
    rendered = str(text or "")
    if _EVIDENCE_SUPPORTED_NEGATE_DENIAL_RE.search(rendered):
        return ""
    affirmative = _EVIDENCE_SUPPORTED_NEGATE_REQUEST_RE.search(
        rendered
    ) or _EVIDENCE_SUPPORTED_NEGATE_RESOLUTION_RE.search(rendered)
    return "negate" if affirmative else ""


def verified_counterexample_route_reason(evidence: Sequence[Mapping[str, Any]]) -> str:
    """Render bounded deterministic provenance for an evidence-backed negate route."""
    names = [str(item.get("name", "") or "").strip() for item in evidence]
    rendered = ", ".join(name for name in names if name) or "[unnamed helper]"
    return _truncate(
        "Requested route: negate.\n"
        "Reason: parent-kernel-verified counterexample evidence for the exact current target: "
        f"{rendered}",
        PROVER_ROUTE_REASON_MAX_CHARS,
    )


def _verified_counterexample_route_target(ctx: RouteContext) -> dict[str, Any]:
    """Build exact evidence metadata for a deterministic negation route."""
    evidence_ids = [
        str(item.get("node_id", "") or "")
        for item in ctx.verified_counterexample_evidence
        if str(item.get("node_id", "") or "")
    ]
    return {
        "target_symbol": ctx.target_symbol,
        "active_file": ctx.active_file,
        "verified_counterexample_evidence": evidence_ids,
        "counterexample_evidence_reason": verified_counterexample_route_reason(
            ctx.verified_counterexample_evidence
        ),
        # The executor must still run its earlier authoritative source-helper
        # promotion scan. It skips only the unavailable scratch phase when no
        # exact helper promotes.
        "source_negation_recovery_only": not _negation_probe_has_budget(ctx),
    }


def build_route_context(
    *,
    trigger: str,
    live_state: Mapping[str, Any] | None = None,
    autonomy_state: Mapping[str, Any] | None = None,
    mgr: TheoremQueueManager | None = None,
    blueprint: Blueprint | None = None,
    summary: Mapping[str, Any] | None = None,
    decision_packet: Mapping[str, Any] | None = None,
    plan_md_exists: bool = False,
    research_mode: bool = False,
) -> RouteContext:
    """Total snapshot builder — never raises; absent inputs yield defaults."""
    current = dict(live_state or {})
    autonomy = dict(autonomy_state or {})
    packet = dict(decision_packet or {})
    assignment = dict(autonomy.get("current_queue_assignment") or {})
    target_statement = str(assignment.get("slice", "") or "")
    target_symbol = str(
        assignment.get("target_symbol", "") or current.get("target_symbol", "") or ""
    ).strip()
    active_file = str(
        assignment.get("active_file", "")
        or current.get("active_file", "")
        or current.get("active_file_label", "")
        or ""
    ).strip()
    route_request = dict(autonomy.get("prover_requested_route") or {})
    requested_route = str(route_request.get("route", "") or "").strip().lower()
    request_target = str(route_request.get("target_symbol", "") or "").strip()
    request_file = str(route_request.get("active_file", "") or "").strip()
    requested_route_reason = bounded_requested_route_reason(
        str(route_request.get("reason", "") or ""),
        requested_route,
    )
    if (
        requested_route not in PROVER_REQUESTED_ROUTES
        or request_target != target_symbol
        or request_file != active_file
    ):
        requested_route = ""
        requested_route_reason = ""

    attempt_count = hard_retries = warning_retries = pending_count = 0
    unresolved = 0
    if mgr is not None and target_symbol and active_file:
        try:
            key = TheoremKey.make(target_symbol, active_file)
            attempt_count = mgr.attempt_count_for(key)
            hard_retries = mgr.hard_retries_for(key)
            warning_retries = mgr.warning_retries_for(key)
        except Exception:
            pass
    if mgr is not None:
        try:
            pending_count = mgr.pending_count
            unresolved = sum(
                1
                for outcome in mgr.outcomes.values()
                if outcome.status in UNRESOLVED_OUTCOME_STATUSES
            )
        except Exception:
            pass

    scoped_findings: tuple[Mapping[str, Any], ...] = ()
    try:
        scoped_findings = relevant_findings(
            summary,
            target_symbol=target_symbol,
            active_file=active_file,
            blueprint=blueprint,
        )
    except Exception:
        pass

    target_node_status = ""
    target_node_found = False
    target_is_sublemma = False
    target_generated_by = ""
    fidelity_suspect = False
    frontier: tuple[str, ...] = ()
    unrelated_frontier: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    verified_graph_facts: tuple[Mapping[str, Any], ...] = ()
    verified_counterexamples: tuple[Mapping[str, Any], ...] = ()
    if blueprint is not None:
        try:
            blocked = tuple(node.name for node in blueprint.nodes if node.status == "blocked")
            target_node_id = node_id_for(target_symbol, active_file)
            direct_dependency_ids = {
                edge.target
                for edge in blueprint.edges
                if edge.kind == "depends_on" and edge.source == target_node_id
            } | {
                edge.source
                for edge in blueprint.edges
                if edge.kind == "split_of" and edge.target == target_node_id
            }
            ready_nodes = blueprint.frontier()
            if target_symbol and active_file:
                frontier = tuple(
                    node.name for node in ready_nodes if node.id in direct_dependency_ids
                )
                unrelated_frontier = tuple(
                    node.name
                    for node in ready_nodes
                    if node.id not in direct_dependency_ids and node.id != target_node_id
                )
            else:
                # Without an exact assignment, the global frontier is valid
                # scheduling inventory; no dependency claim is being made.
                frontier = tuple(node.name for node in ready_nodes)
            normalized_file = TheoremKey.make(target_symbol, active_file).active_file
            proved_nodes = [
                node
                for node in blueprint.nodes
                if node.status == "proved"
                and node.name
                and node.id != target_node_id
                and TheoremKey.make(node.name, node.file).active_file == normalized_file
            ]
            proved_nodes.sort(key=lambda node: (node.id not in direct_dependency_ids, node.name))
            verified_graph_facts = tuple(
                {
                    "node_id": node.id,
                    "name": node.name,
                    "statement": _truncate(
                        node.statement, 2400 if node.id in direct_dependency_ids else 900
                    ),
                    "notes": _truncate(node.notes, 500),
                    "relationship": (
                        "direct-dependency"
                        if node.id in direct_dependency_ids
                        else "same-file-proved-unrelated"
                    ),
                    "route_compatibility": statement_shape_compatibility(
                        str(assignment.get("slice", "") or ""), node.statement
                    ),
                }
                for node in proved_nodes[:16]
            )
            if target_symbol and active_file:
                node = blueprint.node_by_id(node_id_for(target_symbol, active_file))
                if node is not None:
                    target_node_found = True
                    target_node_status = node.status
                    target_generated_by = node.generated_by.strip().lower()
                    fidelity_suspect = "fidelity: suspect" in str(node.notes or "")
                    target_is_sublemma = any(
                        edge.kind == "split_of" and edge.source == node.id
                        for edge in blueprint.edges
                    )
        except Exception:
            pass
    try:
        verified_counterexamples = verified_counterexample_evidence(
            blueprint,
            scoped_findings,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    except Exception:
        pass

    failed_route_signatures: list[str] = []
    if mgr is not None and target_symbol and active_file:
        try:
            key = TheoremKey.make(target_symbol, active_file)
            for entry in mgr.attempt_entries_for(key)[-6:]:
                proof_shape = _truncate(str(entry.get("proof_shape", "") or ""), 700)
                reason = _truncate(str(entry.get("reason", "") or ""), 700)
                signature = " | ".join(part for part in (proof_shape, reason) if part)
                if signature:
                    failed_route_signatures.append(signature)
            for bucket, signatures in mgr.retry_signatures_for(key).items():
                failed_route_signatures.extend(
                    f"{bucket}: {_truncate(str(signature), 700)}"
                    for signature in signatures[-4:]
                    if str(signature).strip()
                )
        except Exception:
            pass
    for state_key in ("failed_route_signatures", "verified_route_signatures"):
        raw_signatures = autonomy.get(state_key) or ()
        if isinstance(raw_signatures, (list, tuple)):
            failed_route_signatures.extend(
                _truncate(str(signature), 700)
                for signature in raw_signatures[-8:]
                if str(signature).strip()
            )

    # Scratch probes and raw promotion rows only update feasibility/audit
    # state. Terminal falsity requires the exact promotion payload that this
    # native run revalidated without reconciliation ambiguity.
    negation_proved = False
    negation_status = str(packet.get("negation_status", "") or "")
    negation_probe_budget_remaining: int | None = None
    negation_refresh_evidence_key = ""
    negation_refresh_retry_consumed = False
    if summary is not None and target_symbol and active_file:
        try:
            storage_key = TheoremKey.make(target_symbol, active_file).storage_key()
            negation_probe_budget_remaining = negation_probe.remaining_probe_budget(
                summary.get("negation_probes"),
                storage_key,
            )
            matching_probe: Mapping[str, Any] | None = None
            for entry in summary.get("negation_probes") or []:
                if not isinstance(entry, Mapping):
                    continue
                if str(entry.get("key", "") or "") != storage_key:
                    continue
                matching_probe = entry
                negation = dict(entry.get("negation") or {})
                verdict = str(negation.get("verdict", "") or "")
                if verdict:
                    negation_status = verdict
            if negation_status == "inconclusive":
                negation_refresh_evidence_key = _negation_refresh_evidence_key(
                    storage_key=storage_key,
                    target_statement=target_statement,
                    probe_entry=matching_probe,
                )
                negation_refresh_retry_consumed = campaign_epoch.negation_refresh_retry_consumed(
                    autonomy,
                    evidence_key=negation_refresh_evidence_key,
                )
            promotion = negation_promotion.authoritative_runtime_main_promotion(
                autonomy,
                summary=summary,
            )
            if promotion is not None and str(promotion.get("key", "") or "") == storage_key:
                negation_proved = True
                negation_status = "negation_promoted"
        except Exception:
            pass

    epoch_refresh = dict(autonomy.get("campaign_epoch_route_refresh") or {})
    previous_epoch_routes = tuple(
        str(route) for route in (epoch_refresh.get("previous_routes") or []) if str(route).strip()
    )
    current_epoch_routes = tuple(
        str(entry.get("route", "") or "")
        for entry in (autonomy.get("campaign_epoch_routes") or [])
        if isinstance(entry, Mapping) and str(entry.get("route", "") or "").strip()
    )
    campaign = dict(summary.get("campaign") or {}) if isinstance(summary, Mapping) else {}
    raw_semantic_history = autonomy.get(campaign_epoch.SEMANTIC_ROUTE_HISTORY_STATE_KEY)
    if not isinstance(raw_semantic_history, (list, tuple)):
        raw_semantic_history = campaign.get(campaign_epoch.SEMANTIC_ROUTE_HISTORY_FIELD)
    semantic_route_history = tuple(
        dict(entry) for entry in (raw_semantic_history or []) if isinstance(entry, Mapping)
    )
    if not semantic_route_history:
        legacy_records = [
            dict(entry)
            for entry in (epoch_refresh.get("previous_route_portfolio") or [])
            if isinstance(entry, Mapping)
        ]
        legacy_records.extend(
            dict(entry)
            for entry in (autonomy.get("campaign_epoch_routes") or [])
            if isinstance(entry, Mapping)
        )
        semantic_route_history = tuple(legacy_records)[
            -campaign_epoch.SEMANTIC_ROUTE_LEGACY_BACKFILL_CAP :
        ]

    return RouteContext(
        trigger=trigger if trigger in TRIGGERS else "event",
        workflow_kind=str(current.get("workflow_kind", "") or "prove"),
        route_decision=dict(current.get("route_decision") or {}),
        active_file=active_file,
        target_symbol=target_symbol,
        target_statement=_truncate(target_statement, 4000),
        declaration_queue_total=_as_int(current.get("declaration_queue_total", 0) or 0),
        sorry_count=_as_int(current.get("sorry_count", 0) or 0),
        project_sorry_count=_as_int(current.get("project_sorry_count", 0) or 0),
        diagnostics=_truncate(str(current.get("diagnostics", "") or ""), 2000),
        blocker_summary=str(current.get("blocker_summary", "") or ""),
        queue_frontier_exhausted=bool(current.get("queue_frontier_exhausted")),
        deferred_exact_verification=bool(
            str(current.get("proof_state_authority", "") or "").strip() == "source_only_unverified"
            and current.get("defer_incremental_warmup")
            and _as_int(current.get("sorry_count", 0) or 0) == 0
        ),
        requested_route=requested_route,
        requested_route_reason=requested_route_reason,
        stable_cycles=_as_int(autonomy.get("continuation_stable_cycles", 0) or 0),
        blocked_runs=_as_int(autonomy.get("continuation_blocked_runs", 0) or 0),
        attempt_count=attempt_count,
        hard_retries=hard_retries,
        warning_retries=warning_retries,
        pending_count=pending_count,
        unresolved_outcomes=unresolved,
        search_exhausted=bool(current.get("search_exhausted")),
        graph_frontier=frontier,
        graph_unrelated_frontier=unrelated_frontier,
        graph_blocked=blocked,
        target_node_status=target_node_status,
        target_node_found=target_node_found,
        target_is_sublemma=target_is_sublemma,
        target_generated_by=target_generated_by,
        fidelity_suspect=fidelity_suspect,
        negation_status=negation_status,
        negation_proved=negation_proved,
        negation_probe_budget_remaining=negation_probe_budget_remaining,
        negation_refresh_evidence_key=negation_refresh_evidence_key,
        negation_refresh_retry_consumed=negation_refresh_retry_consumed,
        plan_md_exists=plan_md_exists,
        decision_packet=packet,
        research_findings=scoped_findings,
        verified_graph_facts=verified_graph_facts,
        verified_counterexample_evidence=verified_counterexamples,
        failed_route_signatures=tuple(dict.fromkeys(failed_route_signatures))[-16:],
        routes_used_this_scope=_as_int(autonomy.get("orchestrator_routes_used", 0) or 0),
        research_mode=research_mode,
        epoch_refresh_required=bool(epoch_refresh.get("required")),
        semantic_refresh_work_due=bool(epoch_refresh.get("required"))
        and str(epoch_refresh.get("reason", "") or "")
        == campaign_epoch.SEMANTIC_PORTFOLIO_ROLLOVER_REASON,
        previous_epoch_routes=previous_epoch_routes,
        current_epoch_routes=current_epoch_routes,
        semantic_route_history=semantic_route_history,
    )


def strategy_directive(route: OrchestratorRoute, ctx: RouteContext) -> str:
    """Render fallback guidance when a route does not place work mechanically.

    Routes handled entirely by the runner return an empty string.
    """
    if route.route == "decompose":
        return "\n".join(
            [
                "[LEANFLOW ORCHESTRATOR ROUTE: decompose]",
                f"- reason: {route.reason}",
                f"- directive: stop direct attempts on `{ctx.target_symbol}`. Call "
                "`lean_decompose_helpers` now, insert the ready helpers, prove each, "
                "then assemble the target from them.",
                "- a helper's `sorry` is normal work-in-progress during the turn.",
            ]
        )
    if route.route == "plan":
        return "\n".join(
            [
                "[LEANFLOW ORCHESTRATOR ROUTE: plan]",
                f"- reason: {route.reason}",
                "- directive: before more proof attempts, consult the read-only generated "
                "plan view and current queue/kernel state, inventory the remaining `sorry` "
                "declarations, identify the hardest missing helper lemmas, then execute the "
                "first concrete step in that attack order. Structured planner state is "
                "persisted by the workflow manager; do not edit managed plan.md.",
            ]
        )
    if route.route == "re-state":
        return "\n".join(
            [
                "[LEANFLOW ORCHESTRATOR ROUTE: re-state]",
                f"- reason: {route.reason}",
                f"- directive: `{ctx.target_symbol}` is refuted as stated — the "
                "decomposition that produced it was wrong. Do NOT keep proving it. "
                "Re-examine the parent statement and propose a corrected split; a "
                "kernel-verified counterexample outranks any proof attempt.",
            ]
        )
    return ""


def generated_helper_negation_preflight_due(ctx: RouteContext) -> bool:
    """Return whether a generated helper needs its one bounded falsity probe."""
    return bool(
        ctx.research_mode
        and ctx.trigger == "scope-entry"
        and ctx.target_generated_by in {"decomposer", "planner"}
        and ctx.negation_status in NEGATION_UNATTEMPTED
        and _negation_probe_has_budget(ctx)
        and ctx.has_queue_item()
    )


def orchestrator_route(ctx: RouteContext, *, max_routes: int | None = None) -> OrchestratorRoute:
    """Apply the deterministic ordered route policy without mutating state.

    Pure: reads the context, never mutates state; the campaign epoch layer owns
    the durable ``orchestrator_routes_used`` streak and the runner owns every route's execution. The
    happy path (row 1) is a no-op passthrough so easy runs stay
    byte-identical.
    """
    limit = orchestrator_max_routes() if max_routes is None else max(1, max_routes)
    breakpoint_trigger = ctx.trigger in {"budget-breakpoint", "retry-exhausted"}
    genuine_failures = max(ctx.attempt_count, ctx.hard_retries)

    # Falsity evidence outranks everything, including the happy path — a
    # false statement must never keep direct-proving.
    # Row 5 — this process already revalidated a registered requested root.
    # Immutable scope-entry authority outranks later mutable split topology;
    # the route only reflects that terminal state and never derives it from
    # the graph or raw promotion history.
    if ctx.negation_proved and ctx.has_queue_item():
        return OrchestratorRoute(
            route="escalate",
            reason="negation kernel-proved on the main statement; scope resolves as disproved",
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # Row 4 — falsity landed on a SUB-lemma: the decomposition was wrong;
    # re-state via non-destructive OR-route backtracking.
    if ctx.target_is_sublemma and ctx.target_node_status == "false":
        return OrchestratorRoute(
            route="re-state",
            reason="sub-lemma is false; invalidate the split and re-decompose",
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # Negation proved but the graph cannot confirm the node's scope: request
    # human review instead of using the difficulty-only park route.
    if ctx.negation_proved and not ctx.target_node_found:
        if not human_review_enabled():
            return OrchestratorRoute(
                route="plan",
                reason=(
                    "negation kernel-proved but graph scope is unknown; repair the "
                    "dependency map while preserving the source statement"
                ),
                target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
            )
        return OrchestratorRoute(
            route="ask-human",
            reason=(
                "negation kernel-proved but the dependency graph cannot confirm whether "
                "this is the main goal; human review required"
            ),
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # The statement-fidelity audit marked the main goal suspect. Spending
    # budget on a possibly wrong statement is the
    # one failure the kernel cannot catch. Non-blocking: park and continue.
    if (
        ctx.fidelity_suspect
        and human_review_enabled()
        and ctx.target_node_found
        and not ctx.target_is_sublemma
        and ctx.trigger in {"scope-entry", "event"}
        and ctx.has_queue_item()
    ):
        return OrchestratorRoute(
            route="ask-human",
            reason="statement fidelity is suspect on the main goal; human review requested",
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # Generated helper statements are model-authored conjectures, not source
    # obligations. In research mode, give each one a single bounded
    # feasibility preflight before any ordinary persistence route. This must
    # outrank stale prover requests and campaign rollovers: either can survive
    # an interrupted weaker-model turn and otherwise postpone falsity checking
    # indefinitely. A conclusive counterexample activates the existing
    # false-subtree cleanup; an inconclusive result is persisted and the next
    # scope entry falls through without repeating the probe.
    if generated_helper_negation_preflight_due(ctx):
        return OrchestratorRoute(
            route="negate",
            reason=(
                "new generated helper requires one feasibility preflight before direct proving"
            ),
            target={
                "target_symbol": ctx.target_symbol,
                "active_file": ctx.active_file,
                "generated_by": ctx.target_generated_by,
            },
        )

    # A sorry-free source candidate that exceeded bounded exact verification
    # is newer work than the attempt/route history restored for its theorem.
    # Give it directly to the foreground for profiling, optimization, and a
    # bounded LeanProbe check. Replaying persistence routes here can starve the
    # only action capable of turning the candidate into kernel authority.
    timeout_decomposition_requested = bool(
        ctx.requested_route == "decompose"
        and "repeated verification timeouts" in ctx.requested_route_reason.lower()
    )
    if (
        ctx.deferred_exact_verification
        and ctx.has_queue_item()
        and not timeout_decomposition_requested
    ):
        return OrchestratorRoute(
            route="direct-prove",
            reason=(
                "sorry-free proof candidate awaits deferred exact verification; "
                "optimize and verify it before replaying older persistence routes"
            ),
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # The runtime raises this one-shot request only after the same sorry-free
    # declaration has failed bounded exact verification more than once. It is
    # newer evidence than the semantic/route ledger and must reach the
    # mechanical splitter before another epoch refresh can consume the scope.
    if timeout_decomposition_requested and ctx.has_queue_item():
        return OrchestratorRoute(
            route="decompose",
            reason=(
                "repeated verification timeouts require structural decomposition; "
                f"{ctx.requested_route_reason}"
            ),
            target={
                "target_symbol": ctx.target_symbol,
                "active_file": ctx.active_file,
                "prover_requested_route": "decompose",
                "prover_request_reason": ctx.requested_route_reason,
                "timeout_decomposition_recovery": True,
            },
            source="deterministic-timeout-recovery",
        )

    # Row 8 — the campaign's no-progress route streak is spent. This guard
    # intentionally precedes both explicit prover requests and the happy-path
    # passthrough: neither branch may evade a due fresh-context epoch. Kernel
    # falsity and statement-fidelity evidence above still outrank the budget.
    consecutive = _as_int(ctx.decision_packet.get("consecutive_exhausted", 0) or 0)
    if ctx.routes_used_this_scope >= limit or (
        breakpoint_trigger and str(ctx.decision_packet.get("scope", "")) == "queue"
    ):
        if ctx.workflow_kind in {"prove", "autoprove"}:
            return OrchestratorRoute(
                route=SEMANTIC_REFRESH_ROUTE,
                reason=(
                    f"route budget spent ({ctx.routes_used_this_scope}/{limit})"
                    if ctx.routes_used_this_scope >= limit
                    else f"queue-level breakpoint after {consecutive} consecutive exhaustions"
                ),
                target={
                    "target_symbol": ctx.target_symbol,
                    "active_file": ctx.active_file,
                    "campaign_rollover_reason": (campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON),
                },
            )
        return OrchestratorRoute(
            route="park",
            reason=(
                f"route budget spent ({ctx.routes_used_this_scope}/{limit})"
                if ctx.routes_used_this_scope >= limit
                else f"queue-level breakpoint after {consecutive} consecutive exhaustions"
            ),
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # A fresh campaign epoch must execute a strategy change before returning
    # to ordinary proving. Persisted route history makes the obligation
    # restart-safe: a process crash cannot turn a rollover into another
    # direct attempt with the same proof shape. An exact current prover
    # handoff is itself the newer strategy change, so let the one-shot request
    # run below before reconsidering the older epoch portfolio on a later tick.
    if (
        ctx.epoch_refresh_required
        and ctx.requested_route not in PROVER_REQUESTED_ROUTES
        and ctx.trigger in {"scope-entry", "event"}
        and ctx.has_queue_item()
    ):
        route = epoch_refresh_route(ctx)
        return OrchestratorRoute(
            route=route,
            reason=(
                "fresh epoch requires a distinct strategy before direct proving; "
                f"previous routes: {', '.join(ctx.previous_epoch_routes) or '[none]'}"
            ),
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # An unresolved source queue with no assignable graph item is not a final
    # sweep and must not fall through to the previous theorem. Refresh the
    # decomposition/plan until the graph exposes a valid frontier again.
    if ctx.queue_frontier_exhausted:
        return OrchestratorRoute(
            route="plan",
            reason="unresolved source queue has no assignable graph frontier; re-plan",
        )

    # An unresolved prover final may carry an explicit route request. Treat it
    # as a one-shot strategy-change event even when the same turn also made
    # kernel-verified progress; progress must not erase the reported blocker.
    if ctx.requested_route in PROVER_REQUESTED_ROUTES and ctx.has_queue_item():
        requested_route = ctx.requested_route
        reason = f"prover reported a blocker and requested route {requested_route}"
        if ctx.requested_route_reason:
            reason += f"; {ctx.requested_route_reason}"
        if (
            requested_route == "negate"
            and not _negation_probe_has_budget(ctx)
            and not ctx.verified_counterexample_evidence
        ):
            requested_route = persistence_route(
                ctx,
                previous_routes=ctx.current_epoch_routes,
            )
            reason = (
                "prover requested negate, but the exact-target scratch-probe budget "
                f"is exhausted; selected persistence route {requested_route} instead"
            )
        target: dict[str, Any] = {
            "target_symbol": ctx.target_symbol,
            "active_file": ctx.active_file,
        }
        if requested_route == ctx.requested_route:
            target["prover_requested_route"] = ctx.requested_route
            if ctx.requested_route_reason:
                target["prover_request_reason"] = ctx.requested_route_reason
            if requested_route == "negate" and ctx.verified_counterexample_evidence:
                target.update(_verified_counterexample_route_target(ctx))
        return OrchestratorRoute(
            route=requested_route,
            reason=reason,
            target=target,
        )

    # A parent-kernel-verified negative helper on an exact target evidence
    # edge is a feasibility event, even before ordinary retry thresholds are
    # reached. Formalize it through the negation gate instead of spending the
    # next prover turn rediscovering the same obstruction. This route is not a
    # disproof: only authoritative negation promotion above can terminate.
    if ctx.verified_counterexample_evidence and ctx.has_queue_item():
        recovery_only = not _negation_probe_has_budget(ctx)
        return OrchestratorRoute(
            route="negate",
            reason=(
                "parent-kernel-verified counterexample evidence on the exact current target "
                + (
                    "requires authoritative source-negation recovery; scratch budget is spent"
                    if recovery_only
                    else "requires authoritative negation checking"
                )
            ),
            target=_verified_counterexample_route_target(ctx),
        )

    # Repeated kernel-rejected attempts are themselves a scope-entry/event
    # strategy boundary. Falling through to the default direct route here was
    # the source of epoch-to-epoch repetition on the Erdős 242 campaign.
    if (
        ctx.has_queue_item()
        and ctx.attempt_count >= HARD_RETRY_LIMIT
        and ctx.trigger in {"scope-entry", "event"}
    ):
        route = persistence_route(ctx, previous_routes=ctx.current_epoch_routes)
        return OrchestratorRoute(
            route=route,
            reason=(
                "repeated rejected attempts require a new persistence route; "
                f"selected {route} from the current epoch portfolio"
            ),
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # Row 1 — happy path: live queue item, few attempts, and neither a
    # breakpoint nor a stall (a stall consult exists precisely to reroute).
    if (
        ctx.has_queue_item()
        and ctx.attempt_count < HARD_RETRY_LIMIT
        and not breakpoint_trigger
        and ctx.trigger != "stall"
    ):
        return OrchestratorRoute(
            route="direct-prove",
            reason="queue item active with attempts below the hard-retry limit; passthrough",
        )

    # Row 2 — breakpoint/exhaustion with search exhausted: split the theorem
    # (spec order: decompose is considered before the negation row).
    if breakpoint_trigger and ctx.attempt_count >= HARD_RETRY_LIMIT and ctx.search_exhausted:
        return OrchestratorRoute(
            route="decompose",
            reason="attempts and search exhausted; decomposition is the expected next move",
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # Row 3 — breakpoint with repeated genuine failures and no conclusive
    # probe yet: check feasibility before pouring more budget in.
    if (
        ctx.trigger == "budget-breakpoint"
        and genuine_failures >= 2
        and ctx.negation_status in NEGATION_UNATTEMPTED
        and _negation_probe_has_budget(ctx)
        and ctx.has_queue_item()
    ):
        return OrchestratorRoute(
            route="negate",
            reason="repeated failures with no feasibility verdict; run the negation probe",
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # Row 6 — scope entry on an empty queue with project sorries and no plan.
    if (
        ctx.trigger == "scope-entry"
        and ctx.declaration_queue_total == 0
        and ctx.project_sorry_count > 0
        and not ctx.plan_md_exists
    ):
        return OrchestratorRoute(
            route="plan",
            reason="no queue and no plan while project sorries remain; plan before proving",
        )

    # Row 7 — stall: research runs plan; an active queue item decomposes.
    if ctx.trigger == "stall":
        if ctx.has_queue_item() and not ctx.research_mode:
            return OrchestratorRoute(
                route="decompose",
                reason="stalled on an active item; force the decomposition route",
                target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
            )
        return OrchestratorRoute(
            route="plan",
            reason="stalled without a clear next item; re-plan from the graph frontier",
        )

    # Breakpoint fallthrough (attempts below the row-2/3 bars): decompose if
    # something is assigned, else plan — a breakpoint must change strategy.
    if breakpoint_trigger:
        if ctx.has_queue_item():
            return OrchestratorRoute(
                route="decompose",
                reason="breakpoint on an active item; change strategy via decomposition",
                target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
            )
        return OrchestratorRoute(route="plan", reason="breakpoint without an assignment; re-plan")

    # Default passthrough: nothing to reroute.
    return OrchestratorRoute(
        route="direct-prove",
        reason="no route-table row matched; passthrough",
    )


def _negation_probe_has_budget(ctx: RouteContext) -> bool:
    """Return whether the exact target is not known to have spent its probe budget."""
    remaining = ctx.negation_probe_budget_remaining
    return remaining is None or remaining > 0


def _persistence_route_candidates(ctx: RouteContext) -> list[str]:
    """Return safe non-terminal persistence routes for the current evidence."""
    candidates = ["decompose", "negate", "plan"]
    if ctx.negation_status not in NEGATION_UNATTEMPTED or not _negation_probe_has_budget(ctx):
        candidates.remove("negate")
    return candidates


def persistence_route(ctx: RouteContext, *, previous_routes: tuple[str, ...]) -> str:
    """Return a non-direct route not yet used in the supplied portfolio."""
    candidates = _persistence_route_candidates(ctx)
    previous = [route for route in previous_routes if route in candidates]
    previous_set = set(previous)
    unseen = [route for route in candidates if route not in previous_set]
    if unseen:
        return unseen[0]
    if previous:
        different = [route for route in candidates if route != previous[-1]]
        if different:
            return different[0]
    return candidates[0]


def _semantic_route_target(
    ctx: RouteContext,
    route: OrchestratorRoute,
) -> dict[str, Any]:
    """Attach current mathematical hypotheses to a persistence route target."""
    target = dict(route.target or {})
    focus = dict(target.get("semantic_route_focus") or {})
    if ctx.graph_frontier:
        focus["graph_frontier"] = list(ctx.graph_frontier[:8])
    if route.route == "negate" and ctx.negation_refresh_evidence_key:
        focus["negation_refresh_evidence_key"] = ctx.negation_refresh_evidence_key
    research_shapes: set[str] = set()
    for finding in ctx.research_findings:
        identities = research_semantic_identity.proof_shape_identities(finding)
        research_shapes.update(identities.values)
    if research_shapes:
        focus["research_proof_shapes"] = sorted(research_shapes)[:16]
    if focus:
        target["semantic_route_focus"] = focus
    return target


def _semantic_history_keys(ctx: RouteContext) -> set[str]:
    """Return exact-assignment semantic route keys since verified progress."""
    normalized_file = os.path.realpath(ctx.active_file) if ctx.active_file else ""
    keys: set[str] = set()
    for record in ctx.semantic_route_history:
        record_target = str(record.get("target_symbol", "") or "").strip()
        record_file = str(record.get("active_file", "") or "").strip()
        if record_target != ctx.target_symbol:
            continue
        if (os.path.realpath(record_file) if record_file else "") != normalized_file:
            continue
        keys.add(research_semantic_identity.route_record_semantic_identity(record).key)
    return keys


def admit_semantically_distinct_route(
    ctx: RouteContext,
    proposed: OrchestratorRoute,
) -> OrchestratorRoute:
    """Admit a new persistence intent or rotate/refresh deterministically.

    The no-progress ledger is kernel-reset only. Reworded reasons, new job
    ids, epoch counters, and route hashes therefore cannot buy another model
    turn for the same route family and mathematical target hypothesis.
    """
    candidates = _persistence_route_candidates(ctx)
    if (
        proposed.route == "decompose"
        and ctx.requested_route == "decompose"
        and "repeated verification timeouts" in ctx.requested_route_reason.lower()
        and dict(proposed.target or {}).get("timeout_decomposition_recovery") is True
    ):
        # This authenticated one-shot request responds to new kernel/resource
        # evidence. Rotating it through the older semantic ledger would roll
        # epochs forever without giving the splitter a construction boundary.
        return proposed
    if proposed.route not in candidates or not ctx.has_queue_item():
        return proposed
    if ctx.semantic_refresh_work_due:
        # The preceding internal refresh already checkpointed the exhausted
        # semantic ledger and rolled the worker portfolio. Permit exactly one
        # real fresh-epoch route to consume that durable refresh obligation.
        # Its token-bound epoch selection is replayed without another charge;
        # after observable work clears the obligation, unchanged semantics are
        # rejected normally again.
        refreshed_target = _semantic_route_target(ctx, proposed)
        refreshed_target["semantic_refresh_work_due"] = True
        return OrchestratorRoute(
            route=proposed.route,
            reason=proposed.reason,
            target=refreshed_target,
            source=proposed.source,
        )
    used_keys = _semantic_history_keys(ctx)
    proposed_target = _semantic_route_target(ctx, proposed)
    proposed_identity = research_semantic_identity.route_semantic_identity(
        route=proposed.route,
        target_symbol=ctx.target_symbol,
        active_file=ctx.active_file,
        reason=proposed.reason,
        target=proposed_target,
    )
    if proposed_identity.key not in used_keys:
        return OrchestratorRoute(
            route=proposed.route,
            reason=proposed.reason,
            target=proposed_target,
            source=proposed.source,
        )

    for candidate in candidates:
        if candidate == proposed.route:
            continue
        candidate_target = _semantic_route_target(
            ctx,
            OrchestratorRoute(
                route=candidate,
                reason="semantic route rotation",
                target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
            ),
        )
        identity = research_semantic_identity.route_semantic_identity(
            route=candidate,
            target_symbol=ctx.target_symbol,
            active_file=ctx.active_file,
            reason="semantic route rotation",
            target=candidate_target,
        )
        if identity.key in used_keys:
            continue
        return OrchestratorRoute(
            route=candidate,
            reason=(
                f"semantic {proposed.route} intent was already attempted without verified "
                f"graph progress; rotate to distinct route family {candidate}"
            ),
            target=candidate_target,
            source="deterministic-semantic-admission",
        )

    return OrchestratorRoute(
        route=SEMANTIC_REFRESH_ROUTE,
        reason=(
            "semantic route portfolio exhausted without verified graph progress; "
            "refresh worker findings and target a new hypothesis or proof shape"
        ),
        target={
            "target_symbol": ctx.target_symbol,
            "active_file": ctx.active_file,
            "next_candidate_route": "portfolio-refresh",
            "repeated_semantic_route_key": proposed_identity.key,
            "campaign_rollover_reason": (campaign_epoch.SEMANTIC_PORTFOLIO_ROLLOVER_REASON),
        },
        source="deterministic-semantic-admission",
    )


def epoch_refresh_route(ctx: RouteContext) -> str:
    """Return a non-direct route distinct from the prior epoch when possible."""
    unseen = _epoch_refresh_unseen_routes(ctx)
    if unseen:
        return unseen[0]
    return persistence_route(ctx, previous_routes=ctx.previous_epoch_routes)


def _epoch_refresh_unseen_routes(ctx: RouteContext) -> list[str]:
    """Return viable route kinds absent from the prior epoch portfolio.

    An inconclusive negation probe is spent during an ordinary persistence
    rotation, but it remains a viable fresh-epoch experiment when the exact
    theorem key retains probe budget and both decomposition and planning were
    already tried. Prefer either ordinary unseen route first; only then reopen
    negation rather than falsely calling a previously used route kind a
    distinct fresh-epoch strategy.
    """
    candidates = _persistence_route_candidates(ctx)
    previous = set(ctx.previous_epoch_routes)
    unseen = [route for route in candidates if route not in previous]
    if unseen:
        return unseen
    if (
        ctx.negation_status == "inconclusive"
        and ctx.negation_probe_budget_remaining is not None
        and ctx.negation_probe_budget_remaining > 0
        and ctx.negation_refresh_evidence_key
        and not ctx.negation_refresh_retry_consumed
        and "negate" not in previous
    ):
        return ["negate"]
    return []


def epoch_refresh_negation_retry_evidence_key(ctx: RouteContext, route: str) -> str:
    """Return the evidence marker consumed by a special fresh-epoch negation retry."""
    if (
        str(route or "").strip().lower() != "negate"
        or ctx.negation_status != "inconclusive"
        or ctx.negation_probe_budget_remaining is None
        or ctx.negation_probe_budget_remaining <= 0
        or not ctx.negation_refresh_evidence_key
        or ctx.negation_refresh_retry_consumed
    ):
        return ""
    candidates = _persistence_route_candidates(ctx)
    previous = set(ctx.previous_epoch_routes)
    if any(candidate not in previous for candidate in candidates):
        return ""
    return ctx.negation_refresh_evidence_key


def persistence_route_is_distinct(
    ctx: RouteContext,
    route: str,
    *,
    previous_routes: tuple[str, ...],
) -> bool:
    """Return whether an advisory route advances a persistence portfolio."""
    normalized = str(route or "").strip().lower()
    candidates = _persistence_route_candidates(ctx)
    if normalized not in candidates:
        return False
    previous = [value for value in previous_routes if value in candidates]
    unseen = [candidate for candidate in candidates if candidate not in set(previous)]
    if unseen:
        return normalized in unseen
    return not previous or normalized != previous[-1]


def epoch_refresh_route_is_distinct(ctx: RouteContext, route: str) -> bool:
    """Return whether an advisory route satisfies a pending epoch refresh."""
    if not ctx.epoch_refresh_required:
        return True
    unseen = _epoch_refresh_unseen_routes(ctx)
    if unseen:
        return str(route or "").strip().lower() in unseen
    return persistence_route_is_distinct(
        ctx,
        route,
        previous_routes=ctx.previous_epoch_routes,
    )
