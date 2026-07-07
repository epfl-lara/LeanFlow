"""Phase 4 §4.1 tests: the pure deterministic orchestrator floor."""

from __future__ import annotations

import pytest

from leanflow_cli.workflows.orchestrator import (
    HARD_RETRY_LIMIT,
    ROUTES,
    OrchestratorRoute,
    RouteContext,
    build_route_context,
    orchestrator_enabled,
    orchestrator_max_routes,
    orchestrator_route,
)
from leanflow_cli.workflows.plan_state import Blueprint, GraphEdge, GraphNode, node_id_for
from leanflow_cli.workflows.queue_manager import QueueItem, TheoremQueueManager


def _ctx(**overrides) -> RouteContext:
    base = dict(
        trigger="scope-entry",
        target_symbol="demo",
        active_file="Demo/Main.lean",
        declaration_queue_total=2,
        attempt_count=0,
    )
    base.update(overrides)
    return RouteContext(**base)


def test_hard_retry_limit_mirrors_native_runner_constant():
    from leanflow_cli.native import native_runner as runner

    assert HARD_RETRY_LIMIT == runner.MANAGER_HARD_RETRY_LIMIT


def test_flags_default_off_and_bounded():
    assert orchestrator_enabled() is False
    assert orchestrator_max_routes() == 4


def test_row1_happy_path_is_passthrough():
    """Property: any live queue item below the hard-retry limit at a
    non-breakpoint trigger routes direct-prove."""
    for trigger in ("scope-entry", "event"):
        for attempts in range(HARD_RETRY_LIMIT):
            route = orchestrator_route(_ctx(trigger=trigger, attempt_count=attempts))
            assert route.route == "direct-prove"
            assert route.source == "deterministic"


def test_row2_breakpoint_with_search_exhausted_decomposes():
    route = orchestrator_route(
        _ctx(trigger="budget-breakpoint", attempt_count=3, search_exhausted=True)
    )
    assert route.route == "decompose"
    assert route.target["target_symbol"] == "demo"


def test_row3_breakpoint_without_probe_verdict_negates():
    route = orchestrator_route(
        _ctx(
            trigger="budget-breakpoint",
            attempt_count=3,
            search_exhausted=False,
            negation_status="not-attempted",
        )
    )
    assert route.route == "negate"

    # A conclusive probe verdict removes the negate row.
    after_probe = orchestrator_route(
        _ctx(
            trigger="budget-breakpoint",
            attempt_count=3,
            search_exhausted=True,
            negation_status="inconclusive",
        )
    )
    assert after_probe.route == "decompose"


def test_row4_false_sublemma_restates():
    route = orchestrator_route(
        _ctx(trigger="event", target_node_status="false", target_is_sublemma=True)
    )
    assert route.route == "re-state"

    scratch_only = orchestrator_route(
        _ctx(trigger="event", negation_proved=True, target_is_sublemma=True)
    )
    assert scratch_only.route == "re-state"


def test_row5_main_goal_negation_escalates_as_disproof():
    route = orchestrator_route(
        _ctx(
            trigger="event",
            negation_proved=True,
            target_node_found=True,
            target_is_sublemma=False,
        )
    )
    assert route.route == "escalate"
    assert "disproved" in route.reason


def test_negation_without_graph_confirmation_parks_never_escalates():
    """Escalation is irreversible: a missing graph must never promote a
    (possibly sub-lemma) refutation into a main-goal disproof."""
    route = orchestrator_route(_ctx(trigger="event", negation_proved=True, target_node_found=False))
    assert route.route == "park"
    assert "cannot confirm" in route.reason


def test_summary_probe_verdict_overrides_stale_packet_status(tmp_path):
    """An inconclusive probe already on record must not be re-routed to
    negate just because the packet still says probe-proposed."""
    from leanflow_cli.workflows.queue_manager import TheoremKey

    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    storage_key = TheoremKey.make("demo", str(active)).storage_key()

    ctx = build_route_context(
        trigger="budget-breakpoint",
        autonomy_state={
            "current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}
        },
        summary={
            "negation_probes": [{"key": storage_key, "negation": {"verdict": "inconclusive"}}]
        },
        decision_packet={"negation_status": "probe-proposed"},
    )

    assert ctx.negation_status == "inconclusive"
    assert ctx.negation_proved is False


def test_builder_tolerates_garbage_persisted_ints():
    ctx = build_route_context(
        trigger="stall",
        live_state={"declaration_queue_total": "x", "sorry_count": None},
        autonomy_state={
            "orchestrator_routes_used": "many",
            "continuation_stable_cycles": "x",
        },
    )
    assert ctx.declaration_queue_total == 0
    assert ctx.routes_used_this_scope == 0
    garbage_packet = RouteContext(
        trigger="budget-breakpoint",
        target_symbol="demo",
        active_file="Demo/Main.lean",
        attempt_count=4,
        decision_packet={"scope": "queue", "consecutive_exhausted": "x"},
    )
    assert orchestrator_route(garbage_packet).route == "park"


def test_row6_scope_entry_without_queue_or_plan_plans():
    route = orchestrator_route(
        RouteContext(
            trigger="scope-entry",
            declaration_queue_total=0,
            project_sorry_count=7,
            plan_md_exists=False,
        )
    )
    assert route.route == "plan"

    with_plan = orchestrator_route(
        RouteContext(
            trigger="scope-entry",
            declaration_queue_total=0,
            project_sorry_count=7,
            plan_md_exists=True,
        )
    )
    assert with_plan.route == "direct-prove"  # passthrough fallback


def test_row7_stall_decomposes_active_item_and_plans_in_research_mode():
    active = orchestrator_route(_ctx(trigger="stall", attempt_count=2))
    assert active.route == "decompose"

    research = orchestrator_route(_ctx(trigger="stall", attempt_count=2, research_mode=True))
    assert research.route == "plan"

    no_item = orchestrator_route(
        RouteContext(trigger="stall", declaration_queue_total=0, attempt_count=0)
    )
    assert no_item.route == "plan"


def test_row8_route_budget_and_queue_breakpoint_park():
    spent = orchestrator_route(_ctx(trigger="stall", attempt_count=4, routes_used_this_scope=4))
    assert spent.route == "park"

    queue_scope = orchestrator_route(
        _ctx(
            trigger="budget-breakpoint",
            attempt_count=4,
            decision_packet={"scope": "queue", "consecutive_exhausted": 3},
        )
    )
    assert queue_scope.route == "park"
    assert "consecutive" in queue_scope.reason

    custom_limit = orchestrator_route(
        _ctx(trigger="stall", attempt_count=4, routes_used_this_scope=2), max_routes=2
    )
    assert custom_limit.route == "park"


def test_breakpoint_fallthrough_changes_strategy():
    low_attempts = orchestrator_route(
        _ctx(trigger="retry-exhausted", attempt_count=1, search_exhausted=False)
    )
    assert low_attempts.route == "decompose"

    no_assignment = orchestrator_route(
        RouteContext(trigger="budget-breakpoint", declaration_queue_total=0)
    )
    assert no_assignment.route == "plan"


def test_every_emitted_route_is_in_the_vocabulary():
    contexts = [
        _ctx(),
        _ctx(trigger="stall", attempt_count=3),
        _ctx(trigger="budget-breakpoint", attempt_count=3, search_exhausted=True),
        _ctx(trigger="budget-breakpoint", attempt_count=3),
        _ctx(trigger="event", negation_proved=True),
        RouteContext(trigger="scope-entry"),
    ]
    for ctx in contexts:
        assert orchestrator_route(ctx).route in ROUTES


def test_build_route_context_is_total_on_empty_inputs():
    ctx = build_route_context(trigger="scope-entry")
    assert ctx.trigger == "scope-entry"
    assert ctx.has_queue_item() is False
    assert orchestrator_route(ctx).route in ROUTES

    weird = build_route_context(trigger="not-a-trigger")
    assert weird.trigger == "event"


def test_build_route_context_reads_queue_graph_and_negation(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo", reasons=("contains sorry",)), active_file=str(active))
    mgr.record_attempt(cycle=1, proof_shape="direct", reason="type mismatch")
    mgr.record_attempt(cycle=2, proof_shape="direct2", reason="type mismatch")

    node_id = node_id_for("demo", str(active))
    blueprint = Blueprint(
        nodes=(
            GraphNode(id=node_id, name="demo", file=str(active), status="proving"),
            GraphNode(id="n-parent", name="main", file=str(active), status="stated"),
        ),
        edges=(GraphEdge(source=node_id, target="n-parent", kind="split_of"),),
    )
    from leanflow_cli.workflows.queue_manager import TheoremKey

    storage_key = TheoremKey.make("demo", str(active)).storage_key()
    summary = {
        "negation_probes": [
            {
                "key": storage_key,
                "negation": {"verdict": "negation_proved", "axioms_ok": True},
            }
        ]
    }

    ctx = build_route_context(
        trigger="event",
        live_state={"active_file": str(active), "search_exhausted": True},
        autonomy_state={
            "current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)},
            "orchestrator_routes_used": 1,
            "continuation_stable_cycles": 2,
        },
        mgr=mgr,
        blueprint=blueprint,
        summary=summary,
        decision_packet={"negation_status": "probe-proposed"},
    )

    assert ctx.attempt_count == 2
    assert ctx.target_is_sublemma is True
    assert ctx.negation_proved is True
    assert ctx.search_exhausted is True
    assert ctx.routes_used_this_scope == 1
    assert "main" in ctx.graph_frontier
    # Sub-lemma falsity evidence wins: re-state, not escalate.
    assert orchestrator_route(ctx).route == "re-state"


@pytest.mark.parametrize("route", ROUTES)
def test_route_dataclass_accepts_vocabulary(route):
    decision = OrchestratorRoute(route=route, reason="r")
    assert decision.route == route
