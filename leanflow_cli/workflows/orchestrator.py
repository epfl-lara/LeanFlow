"""Deterministic orchestrator floor for the /prove redesign (Phase 4, specs §4.1).

Pure leaf: a frozen :class:`RouteContext` snapshot plus an ordered route
table that turns what used to be terminal stops (stall, budget breakpoint,
retry exhaustion) into routing decisions. No I/O beyond the state handed in;
the exec layer and the runner own every side effect.

The floor consumes the EXISTING deterministic classifier output
(``route_workflow_step`` → ``live_state["route_decision"]``) — it extends
those outputs, it does not build a new classifier. The LLM routing layer is
spec'd separately (§4.4) and stays disabled until Phase 6; on easy runs the
floor's first row is a byte-identical no-op passthrough (``direct-prove``)
and no extra model call ever happens.

Route vocabulary note: the Part III function-level enum is the seven values
below minus ``ask-human``; ``ask-human`` is the roadmap-v3 addition wired in
a later Phase-4 sub-step. It is included in :data:`ROUTES` now so the
vocabulary does not churn, but no deterministic row emits it yet.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from leanflow_cli.workflows.plan_state import Blueprint, node_id_for
from leanflow_cli.workflows.queue_manager import TheoremKey, TheoremQueueManager

ROUTES = (
    "direct-prove",
    "decompose",
    "plan",
    "negate",
    "park",
    "re-state",
    "escalate",
    "ask-human",  # roadmap-v3 addition; emitted by a later sub-step, never by this table
)
TRIGGERS = ("scope-entry", "stall", "budget-breakpoint", "retry-exhausted", "event")

#: Mirrors native_runner.MANAGER_HARD_RETRY_LIMIT (layering forbids importing
#: the runner; the equality is pinned by tests, struggle_signals-style).
HARD_RETRY_LIMIT = 2

#: Outcome statuses that count as unresolved work for routing purposes.
UNRESOLVED_OUTCOME_STATUSES = frozenset({"blocked", "reverted-to-sorry", "skipped", "unknown"})

#: Negation statuses that mean "a probe has not conclusively run yet".
NEGATION_UNATTEMPTED = frozenset({"", "none", "not-attempted", "probe-proposed"})


def orchestrator_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_ORCHESTRATOR_ENABLED", "") or "").strip().lower()
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
    """Everything one orchestrator invocation knows (specs §4.1 RouteContext)."""

    trigger: str = "scope-entry"
    workflow_kind: str = "prove"
    route_decision: Mapping[str, Any] = field(default_factory=dict)
    active_file: str = ""
    target_symbol: str = ""
    declaration_queue_total: int = 0
    sorry_count: int = 0
    project_sorry_count: int = 0
    diagnostics: str = ""
    blocker_summary: str = ""
    stable_cycles: int = 0
    blocked_runs: int = 0
    attempt_count: int = 0
    hard_retries: int = 0
    warning_retries: int = 0
    pending_count: int = 0
    unresolved_outcomes: int = 0
    search_exhausted: bool = False
    graph_frontier: tuple[str, ...] = ()  # node names
    graph_blocked: tuple[str, ...] = ()
    target_node_status: str = ""
    target_node_found: bool = False  # the graph positively knows this node
    target_is_sublemma: bool = False
    negation_status: str = ""  # summary probe verdict, packet status as fallback
    negation_proved: bool = False  # promoted-quality scratch verdict exists
    plan_md_exists: bool = False
    decision_packet: Mapping[str, Any] = field(default_factory=dict)
    routes_used_this_scope: int = 0
    research_mode: bool = False

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
    target_symbol = str(
        assignment.get("target_symbol", "") or current.get("target_symbol", "") or ""
    ).strip()
    active_file = str(
        assignment.get("active_file", "")
        or current.get("active_file", "")
        or current.get("active_file_label", "")
        or ""
    ).strip()

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

    target_node_status = ""
    target_node_found = False
    target_is_sublemma = False
    frontier: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    if blueprint is not None:
        try:
            frontier = tuple(node.name for node in blueprint.frontier())
            blocked = tuple(node.name for node in blueprint.nodes if node.status == "blocked")
            if target_symbol and active_file:
                node = blueprint.node_by_id(node_id_for(target_symbol, active_file))
                if node is not None:
                    target_node_found = True
                    target_node_status = node.status
                    target_is_sublemma = any(
                        edge.kind == "split_of" and edge.source == node.id
                        for edge in blueprint.edges
                    )
        except Exception:
            pass

    # Negation evidence: the summary probe verdict is authoritative over the
    # (possibly stale) packet status — a probe that already ran must never be
    # re-routed just because the packet still says probe-proposed.
    negation_proved = False
    negation_status = str(packet.get("negation_status", "") or "")
    if summary is not None and target_symbol and active_file:
        try:
            storage_key = TheoremKey.make(target_symbol, active_file).storage_key()
            for entry in summary.get("negation_probes") or []:
                if not isinstance(entry, Mapping):
                    continue
                if str(entry.get("key", "") or "") != storage_key:
                    continue
                negation = dict(entry.get("negation") or {})
                verdict = str(negation.get("verdict", "") or "")
                if verdict:
                    negation_status = verdict
                if verdict == "negation_proved" and negation.get("axioms_ok"):
                    negation_proved = True
                    break
        except Exception:
            pass

    return RouteContext(
        trigger=trigger if trigger in TRIGGERS else "event",
        workflow_kind=str(current.get("workflow_kind", "") or "prove"),
        route_decision=dict(current.get("route_decision") or {}),
        active_file=active_file,
        target_symbol=target_symbol,
        declaration_queue_total=_as_int(current.get("declaration_queue_total", 0) or 0),
        sorry_count=_as_int(current.get("sorry_count", 0) or 0),
        project_sorry_count=_as_int(current.get("project_sorry_count", 0) or 0),
        diagnostics=_truncate(str(current.get("diagnostics", "") or ""), 2000),
        blocker_summary=str(current.get("blocker_summary", "") or ""),
        stable_cycles=_as_int(autonomy.get("continuation_stable_cycles", 0) or 0),
        blocked_runs=_as_int(autonomy.get("continuation_blocked_runs", 0) or 0),
        attempt_count=attempt_count,
        hard_retries=hard_retries,
        warning_retries=warning_retries,
        pending_count=pending_count,
        unresolved_outcomes=unresolved,
        search_exhausted=bool(current.get("search_exhausted")),
        graph_frontier=frontier,
        graph_blocked=blocked,
        target_node_status=target_node_status,
        target_node_found=target_node_found,
        target_is_sublemma=target_is_sublemma,
        negation_status=negation_status,
        negation_proved=negation_proved,
        plan_md_exists=plan_md_exists,
        decision_packet=packet,
        routes_used_this_scope=_as_int(autonomy.get("orchestrator_routes_used", 0) or 0),
        research_mode=research_mode,
    )


def orchestrator_route(ctx: RouteContext, *, max_routes: int | None = None) -> OrchestratorRoute:
    """The Phase-4 deterministic route table (specs §4.1, eight ordered rows).

    Pure: reads the context, never mutates state; the runner owns the
    ``orchestrator_routes_used`` counter and every route's execution. The
    happy path (row 1) is a no-op passthrough so easy runs stay
    byte-identical.
    """
    limit = orchestrator_max_routes() if max_routes is None else max(1, max_routes)
    breakpoint_trigger = ctx.trigger in {"budget-breakpoint", "retry-exhausted"}
    genuine_failures = max(ctx.attempt_count, ctx.hard_retries)

    # Falsity evidence outranks everything, including the happy path — a
    # false statement must never keep direct-proving.
    # Row 5 — kernel-quality negation of the MAIN goal. Escalation is
    # irreversible (the scope resolves as disproved), so it demands POSITIVE
    # graph evidence: the node is known and has no split_of parent. A
    # missing graph must never turn a sub-lemma refutation into a main-goal
    # disproof.
    if (
        ctx.negation_proved
        and ctx.target_node_found
        and not ctx.target_is_sublemma
        and ctx.has_queue_item()
    ):
        return OrchestratorRoute(
            route="escalate",
            reason="negation kernel-proved on the main statement; scope resolves as disproved",
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # Row 4 — falsity landed on a SUB-lemma: the decomposition was wrong;
    # re-state via non-destructive OR-route backtracking.
    if ctx.target_is_sublemma and (ctx.target_node_status == "false" or ctx.negation_proved):
        return OrchestratorRoute(
            route="re-state",
            reason="sub-lemma is false; invalidate the split and re-decompose",
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # Negation proved but the graph cannot confirm the node's scope: park
    # loudly for review instead of proving a refuted statement or escalating
    # on unconfirmed evidence.
    if ctx.negation_proved and not ctx.target_node_found:
        return OrchestratorRoute(
            route="park",
            reason=(
                "negation kernel-proved but the dependency graph cannot confirm whether "
                "this is the main goal; parked for review"
            ),
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
        )

    # Row 1 — happy path: live queue item, few attempts, no breakpoint.
    if ctx.has_queue_item() and ctx.attempt_count < HARD_RETRY_LIMIT and not breakpoint_trigger:
        return OrchestratorRoute(
            route="direct-prove",
            reason="queue item active with attempts below the hard-retry limit; passthrough",
        )

    # Row 8 — route budget or exhaustion streak spent: park, never silently.
    consecutive = _as_int(ctx.decision_packet.get("consecutive_exhausted", 0) or 0)
    if ctx.routes_used_this_scope >= limit or (
        breakpoint_trigger and str(ctx.decision_packet.get("scope", "")) == "queue"
    ):
        return OrchestratorRoute(
            route="park",
            reason=(
                f"route budget spent ({ctx.routes_used_this_scope}/{limit})"
                if ctx.routes_used_this_scope >= limit
                else f"queue-level breakpoint after {consecutive} consecutive exhaustions"
            ),
            target={"target_symbol": ctx.target_symbol, "active_file": ctx.active_file},
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
