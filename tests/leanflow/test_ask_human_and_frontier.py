"""Phase 4 (5/6) tests: ask-human route, re-state ACK, graph-frontier selection."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.orchestrator import OrchestratorRoute, RouteContext, orchestrator_route
from leanflow_cli.workflows.queue_models import QueueItem, select_next_item


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))


def _events(monkeypatch) -> list[tuple[tuple, dict]]:
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    return events


# ---------------------------------------------------------------------------
# Floor: the ask-human row
# ---------------------------------------------------------------------------


def test_fidelity_suspect_main_goal_routes_ask_human():
    ctx = RouteContext(
        trigger="scope-entry",
        target_symbol="demo",
        active_file="Demo.lean",
        declaration_queue_total=2,
        target_node_found=True,
        target_is_sublemma=False,
        fidelity_suspect=True,
    )
    route = orchestrator_route(ctx)
    assert route.route == "ask-human"

    # A suspect SUB-lemma keeps normal routing (re-state path owns it).
    sublemma = orchestrator_route(
        RouteContext(
            trigger="scope-entry",
            target_symbol="demo",
            active_file="Demo.lean",
            declaration_queue_total=2,
            target_node_found=True,
            target_is_sublemma=True,
            fidelity_suspect=True,
        )
    )
    assert sublemma.route == "direct-prove"


# ---------------------------------------------------------------------------
# Apply: ask-human parks non-blockingly; main-goal re-state converts
# ---------------------------------------------------------------------------


def _seed_node(name: str, file: str, status: str = "proving") -> str:
    node_id = plan_state.node_id_for(name, file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(plan_state.GraphNode(id=node_id, name=name, file=file, status=status),)
        )
    )
    return node_id


def test_apply_ask_human_parks_and_continues(enabled, monkeypatch):
    events = _events(monkeypatch)
    node_id = _seed_node("demo", "Demo.lean")
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}
    }

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="ask-human", reason="fidelity suspect"),
        [],
        autonomy_state,
        {},
    )

    assert action == "continue"  # NON-blocking: the run keeps going
    assert plan_state.load_blueprint().node_by_id(node_id).status == "parked"
    summary = plan_state.load_summary()
    assert summary["human_questions"][0]["target_symbol"] == "demo"
    assert any(args[0] == "ask-human" for args, _k in events)


def test_main_goal_restate_requires_ack_and_converts(enabled, monkeypatch):
    events = _events(monkeypatch)
    node_id = _seed_node("demo", "Demo.lean")  # no split_of parent = main goal
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}
    }
    history: list[dict[str, Any]] = []

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="re-state", reason="negation evidence"),
        history,
        autonomy_state,
        {},
    )

    assert action == "continue"
    assert history == []  # no re-state directive was issued for the main goal
    assert plan_state.load_blueprint().node_by_id(node_id).status == "parked"
    ask = [k for a, k in events if a[0] == "ask-human"]
    assert ask and "ACK" in ask[0]["reason"]


def test_sublemma_restate_still_issues_directive(enabled, monkeypatch):
    _events(monkeypatch)
    file = "Demo.lean"
    child_id = plan_state.node_id_for("child", file)
    parent_id = plan_state.node_id_for("parent", file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(id=child_id, name="child", file=file, status="proving"),
                plan_state.GraphNode(id=parent_id, name="parent", file=file, status="stated"),
            ),
            edges=(plan_state.GraphEdge(source=child_id, target=parent_id, kind="split_of"),),
        )
    )
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "child", "active_file": file}
    }
    history: list[dict[str, Any]] = []

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="re-state", reason="sub-lemma false"),
        history,
        autonomy_state,
        {},
    )

    assert action == "continue"
    assert history and "[LEANFLOW ORCHESTRATOR ROUTE: re-state]" in history[-1]["content"]


# ---------------------------------------------------------------------------
# Graph-frontier queue selection
# ---------------------------------------------------------------------------


def _queue() -> list[QueueItem]:
    return [
        QueueItem(label="parked_one", reasons=("contains sorry",)),
        QueueItem(label="ready_one", reasons=("contains sorry",)),
        QueueItem(label="unknown_one", reasons=("contains sorry",)),
    ]


def test_selector_without_precedence_is_legacy_file_order():
    selected = select_next_item(_queue(), is_present_in_file=lambda label: True)
    assert selected.label == "parked_one"


def test_selector_prefers_frontier_ready_and_avoids_parked():
    ranks = {"parked_one": 2, "ready_one": 0, "unknown_one": 1}
    selected = select_next_item(
        _queue(),
        is_present_in_file=lambda label: True,
        precedence=lambda label: ranks.get(label, 1),
    )
    assert selected.label == "ready_one"


def test_selector_falls_back_to_avoided_items_when_nothing_else(monkeypatch):
    # A queue of only avoided items still proves — never a false final sweep.
    only_avoided = [QueueItem(label="parked_one", reasons=("contains sorry",))]
    selected = select_next_item(
        only_avoided, is_present_in_file=lambda label: True, precedence=lambda label: 2
    )
    assert selected.label == "parked_one"

    # And the None => final-sweep contract survives with precedence set.
    clean = [QueueItem(label="clean", reasons=())]
    assert (
        select_next_item(clean, is_present_in_file=lambda label: True, precedence=lambda label: 0)
        is None
    )


def test_selector_diagnostic_bucket_still_outranks_frontier_rank():
    queue = [
        QueueItem(label="sorry_ready", reasons=("contains sorry",)),
        QueueItem(label="diag_unknown", reasons=("diagnostic near line 3",)),
    ]
    ranks = {"sorry_ready": 0, "diag_unknown": 1}
    selected = select_next_item(
        queue,
        is_present_in_file=lambda label: True,
        precedence=lambda label: ranks.get(label, 1),
    )
    # Diagnostics unblock compilation: the bucket rule stays authoritative.
    assert selected.label == "diag_unknown"


def test_avoided_diagnostic_still_outranks_ready_sorry():
    """Per-bucket exclusion: a rank-2 diagnostic must not be dropped in
    favor of a rank-0 sorry item — diagnostics unblock compilation."""
    queue = [
        QueueItem(label="diag_parked", reasons=("diagnostic near line 3",)),
        QueueItem(label="sorry_ready", reasons=("contains sorry",)),
    ]
    ranks = {"diag_parked": 2, "sorry_ready": 0}
    selected = select_next_item(
        queue,
        is_present_in_file=lambda label: True,
        precedence=lambda label: ranks.get(label, 1),
    )
    assert selected.label == "diag_parked"


def test_missing_graph_converts_restate_to_ask_human(enabled, monkeypatch):
    """Fail closed: without positive split_of confirmation, a re-state must
    never issue the autonomous directive."""
    events = _events(monkeypatch)
    # No blueprint saved at all.
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}
    }
    history: list[dict[str, Any]] = []

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="re-state", reason="negation evidence"),
        history,
        autonomy_state,
        {},
    )

    assert action == "continue"
    assert history == []
    assert any(args[0] == "ask-human" for args, _k in events)


def test_parked_skip_active_without_frontier_flag(enabled, monkeypatch):
    """ask-human's non-blocking contract: with the orchestrator on but the
    frontier flag OFF, parked nodes are still skipped (no ordering beyond
    that)."""
    monkeypatch.delenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", raising=False)
    file = "Demo.lean"
    parked_id = plan_state.node_id_for("parked_one", file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(id=parked_id, name="parked_one", file=file, status="parked"),
                plan_state.GraphNode(
                    id=plan_state.node_id_for("ready_one", file),
                    name="ready_one",
                    file=file,
                    status="stated",
                ),
            )
        )
    )

    precedence = runner._graph_frontier_precedence()
    assert precedence is not None
    assert precedence("parked_one") == 2
    assert precedence("ready_one") == 1  # no frontier ORDERING without the flag

    selected = select_next_item(
        _queue(), is_present_in_file=lambda label: True, precedence=precedence
    )
    assert selected.label == "ready_one"

    # Orchestrator off too: fully legacy (no precedence at all).
    monkeypatch.delenv("LEANFLOW_ORCHESTRATOR_ENABLED", raising=False)
    assert runner._graph_frontier_precedence() is None


def test_runner_precedence_builder(enabled, monkeypatch):
    file = "Demo.lean"
    ready_id = plan_state.node_id_for("ready_one", file)
    parked_id = plan_state.node_id_for("parked_one", file)
    dep_id = plan_state.node_id_for("dep", file)
    waiting_id = plan_state.node_id_for("waiting_one", file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(id=ready_id, name="ready_one", file=file, status="stated"),
                plan_state.GraphNode(id=parked_id, name="parked_one", file=file, status="parked"),
                plan_state.GraphNode(id=dep_id, name="dep", file=file, status="blocked"),
                plan_state.GraphNode(id=waiting_id, name="waiting_one", file=file, status="stated"),
            ),
            edges=(plan_state.GraphEdge(source=waiting_id, target=dep_id, kind="depends_on"),),
        )
    )

    # Frontier flag off (orchestrator on): parked-skip mode only — covered
    # by test_parked_skip_active_without_frontier_flag. Fully off => None.
    monkeypatch.delenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", raising=False)
    monkeypatch.delenv("LEANFLOW_ORCHESTRATOR_ENABLED", raising=False)
    assert runner._graph_frontier_precedence() is None

    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    precedence = runner._graph_frontier_precedence()
    assert precedence is not None
    assert precedence("ready_one") == 0
    assert precedence("parked_one") == 2
    assert precedence("waiting_one") == 2  # dependency blocked -> avoid
    assert precedence("SomeFile.lean") == 1  # project-scope labels: unknown
