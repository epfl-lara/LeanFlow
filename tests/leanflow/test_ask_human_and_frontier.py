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
    monkeypatch.setenv("LEANFLOW_HUMAN_REVIEW_ENABLED", "1")
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


def test_fidelity_suspect_main_goal_routes_ask_human(monkeypatch):
    monkeypatch.setenv("LEANFLOW_HUMAN_REVIEW_ENABLED", "1")
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


def test_fidelity_suspect_main_goal_continues_without_human_review(monkeypatch):
    monkeypatch.delenv("LEANFLOW_HUMAN_REVIEW_ENABLED", raising=False)
    route = orchestrator_route(
        RouteContext(
            trigger="scope-entry",
            target_symbol="demo",
            active_file="Demo.lean",
            declaration_queue_total=2,
            target_node_found=True,
            fidelity_suspect=True,
        )
    )

    assert route.route == "direct-prove"


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


def test_apply_ask_human_continues_as_plan_without_opt_in(enabled, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HUMAN_REVIEW_ENABLED", "0")
    events = _events(monkeypatch)
    node_id = _seed_node("demo", "Demo.lean")
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}
    }
    history: list[dict[str, Any]] = []

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="ask-human", reason="fidelity suspect"),
        history,
        autonomy_state,
        {},
    )

    assert action == "continue"
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"
    assert not plan_state.load_summary().get("human_questions")
    assert not any(args[0] == "ask-human" for args, _k in events)
    assert history and "human review is disabled" in history[-1]["content"]


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


def test_selector_never_retries_an_absolutely_excluded_false_item():
    excluded = [QueueItem(label="false_helper", reasons=("contains sorry",))]

    assert (
        select_next_item(
            excluded,
            is_present_in_file=lambda _label: True,
            precedence=lambda _label: 3,
        )
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
    assert precedence("parked_one") == 3
    assert precedence("ready_one") == 1  # no frontier ORDERING without the flag

    selected = select_next_item(
        _queue(), is_present_in_file=lambda label: True, precedence=precedence
    )
    assert selected.label == "ready_one"

    # Orchestrator ownership does not disable graph safety.
    monkeypatch.delenv("LEANFLOW_ORCHESTRATOR_ENABLED", raising=False)
    precedence = runner._graph_frontier_precedence()
    assert precedence is not None
    assert precedence("parked_one") == 3
    assert precedence("ready_one") == 1


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

    # With rich frontier ordering off, dependency safety remains active and
    # ready nodes retain stable source-order rank 1.
    monkeypatch.delenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", raising=False)
    monkeypatch.delenv("LEANFLOW_ORCHESTRATOR_ENABLED", raising=False)
    precedence = runner._graph_frontier_precedence()
    assert precedence is not None
    assert precedence("ready_one") == 1
    assert precedence("parked_one") == 3
    assert precedence("dep") == 2
    assert precedence("waiting_one") == 2
    assert precedence("SomeFile.lean") == 1

    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    precedence = runner._graph_frontier_precedence()
    assert precedence is not None
    assert precedence("ready_one") == 0
    assert precedence("parked_one") == 3
    assert precedence("waiting_one") == 2  # dependency blocked -> avoid
    assert precedence("SomeFile.lean") == 1  # project-scope labels: unknown


def test_runner_precedence_prefers_current_assignment_split_dependency(
    enabled, monkeypatch, tmp_path
):
    """Keep a newly placed split ahead of unrelated historical frontier work."""
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Demo.lean")
    target_id = plan_state.node_id_for("current_target", active_file)
    helper_id = plan_state.node_id_for("current_target_helper", active_file)
    unrelated_id = plan_state.node_id_for("short_unrelated", active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=unrelated_id,
                    name="short_unrelated",
                    file=active_file,
                    status="stated",
                ),
                plan_state.GraphNode(
                    id=target_id,
                    name="current_target",
                    file=active_file,
                    status="proving",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    name="current_target_helper",
                    file=active_file,
                    status="stated",
                ),
            ),
            edges=(
                plan_state.GraphEdge(
                    source=helper_id,
                    target=target_id,
                    kind="split_of",
                ),
                plan_state.GraphEdge(
                    source=target_id,
                    target=helper_id,
                    kind="depends_on",
                ),
            ),
        )
    )
    precedence = runner._graph_frontier_precedence(
        {
            "current_queue_assignment": {
                "target_symbol": "current_target",
                "active_file": active_file,
            }
        }
    )

    assert precedence is not None
    assert precedence("current_target_helper") == -1
    assert precedence("short_unrelated") == 0
    assert precedence("current_target") == 1
    selected = select_next_item(
        (
            QueueItem(label="short_unrelated", reasons=("contains sorry",)),
            QueueItem(label="current_target_helper", reasons=("contains sorry",)),
            QueueItem(label="current_target", reasons=("contains sorry",)),
        ),
        is_present_in_file=lambda _label: True,
        precedence=precedence,
        # Curriculum may prefer the short unrelated lemma only within a rank.
        order_key=lambda label: 0 if label == "short_unrelated" else 100,
    )
    assert selected is not None
    assert selected.label == "current_target_helper"


def test_runner_precedence_keeps_blocked_current_assignment_ahead_of_downstream_sorry(
    enabled, monkeypatch, tmp_path
):
    """A stalled route cannot hand a downstream theorem an upstream sorry."""
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Demo.lean")
    current_id = plan_state.node_id_for("hard_parent", active_file)
    downstream_id = plan_state.node_id_for("result", active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=current_id,
                    name="hard_parent",
                    file=active_file,
                    status="blocked",
                ),
                plan_state.GraphNode(
                    id=downstream_id,
                    name="result",
                    file=active_file,
                    status="stated",
                ),
            )
        )
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "hard_parent",
            "active_file": active_file,
        },
        "theorem_outcomes": {
            f"{active_file}::hard_parent": {
                "target_symbol": "hard_parent",
                "active_file": active_file,
                "status": "deferred",
                "note": "current route exhausted",
            }
        },
    }
    queue = (
        QueueItem(label="hard_parent", reasons=("contains sorry",)),
        QueueItem(label="result", reasons=("contains sorry",)),
    )

    precedence = runner._graph_frontier_precedence(
        autonomy_state,
        active_file=active_file,
        queue_labels=("hard_parent", "result"),
    )

    assert precedence is not None
    assert precedence("hard_parent") == -2
    assert precedence("result") == 0
    selected = select_next_item(
        queue,
        is_present_in_file=lambda _label: True,
        precedence=precedence,
    )
    assert selected is not None
    assert selected.label == "hard_parent"


def test_runner_precedence_selects_unresolved_answer_before_result_consumer(
    enabled, monkeypatch, tmp_path
):
    """A known graph node cannot jump over an earlier pending source dependency."""
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active = tmp_path / "P4.lean"
    active.write_text(
        "def answer : Set Nat := sorry\n\n"
        "theorem result : {n : Nat | n > 0} = answer := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    result_id = plan_state.node_id_for("result", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=result_id,
                    name="result",
                    file=str(active),
                    status="stated",
                ),
            )
        )
    )
    queue = (
        QueueItem(label="answer", reasons=("contains sorry",)),
        QueueItem(label="result", reasons=("contains sorry",)),
    )

    precedence = runner._graph_frontier_precedence(
        {},
        active_file=str(active),
        queue_labels=("answer", "result"),
    )

    assert precedence is not None
    assert precedence("answer") == 1
    assert precedence("result") == 2
    selected = select_next_item(
        queue,
        is_present_in_file=lambda _label: True,
        precedence=precedence,
    )
    assert selected is not None
    assert selected.label == "answer"


def test_runner_precedence_keeps_split_family_focused_through_parent_handback(
    enabled, monkeypatch, tmp_path
):
    """Stick to the assigned helper, then its sibling and parent before unrelated work."""
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Demo.lean")
    parent_id = plan_state.node_id_for("parent", active_file)
    helper_id = plan_state.node_id_for("helper", active_file)
    sibling_id = plan_state.node_id_for("sibling", active_file)
    unrelated_id = plan_state.node_id_for("short_unrelated", active_file)

    def save(*, helper_status: str, sibling_status: str) -> None:
        plan_state.save_blueprint(
            plan_state.Blueprint(
                nodes=(
                    plan_state.GraphNode(
                        id=unrelated_id,
                        name="short_unrelated",
                        file=active_file,
                        status="stated",
                    ),
                    plan_state.GraphNode(
                        id=parent_id,
                        name="parent",
                        file=active_file,
                        status="proving",
                    ),
                    plan_state.GraphNode(
                        id=helper_id,
                        name="helper",
                        file=active_file,
                        status=helper_status,
                    ),
                    plan_state.GraphNode(
                        id=sibling_id,
                        name="sibling",
                        file=active_file,
                        status=sibling_status,
                    ),
                ),
                edges=(
                    plan_state.GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
                    plan_state.GraphEdge(source=sibling_id, target=parent_id, kind="split_of"),
                    plan_state.GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
                    plan_state.GraphEdge(source=parent_id, target=sibling_id, kind="depends_on"),
                ),
                revision=plan_state.load_blueprint().revision,
            )
        )

    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "helper",
            "active_file": active_file,
        }
    }
    save(helper_status="stated", sibling_status="stated")
    precedence = runner._graph_frontier_precedence(autonomy_state, active_file=active_file)
    assert precedence is not None
    assert precedence("helper") == -2
    assert precedence("sibling") == 0
    assert precedence("short_unrelated") == 0
    queue = tuple(
        QueueItem(label=label, reasons=("contains sorry",))
        for label in ("short_unrelated", "sibling", "helper", "parent")
    )
    selected = select_next_item(
        queue,
        is_present_in_file=lambda _label: True,
        precedence=precedence,
        order_key=lambda label: 0 if label == "short_unrelated" else 100,
    )
    assert selected is not None and selected.label == "helper"

    # Source truth can lose the helper one refresh before graph reconciliation
    # marks it proved. Its absence from the unresolved queue still enables the
    # one-level split handback without opening older ancestor branches.
    precedence = runner._graph_frontier_precedence(
        autonomy_state,
        active_file=active_file,
        queue_labels=("short_unrelated", "sibling", "parent"),
    )
    assert precedence is not None and precedence("sibling") == -1
    assert precedence("parent") == 1

    save(helper_status="proved", sibling_status="stated")
    precedence = runner._graph_frontier_precedence(autonomy_state, active_file=active_file)
    selected = select_next_item(
        tuple(item for item in queue if item.label != "helper"),
        is_present_in_file=lambda _label: True,
        precedence=precedence,
        order_key=lambda label: 0 if label == "short_unrelated" else 100,
    )
    assert selected is not None and selected.label == "sibling"

    save(helper_status="proved", sibling_status="proved")
    precedence = runner._graph_frontier_precedence(autonomy_state, active_file=active_file)
    selected = select_next_item(
        tuple(item for item in queue if item.label not in {"helper", "sibling"}),
        is_present_in_file=lambda _label: True,
        precedence=precedence,
        order_key=lambda label: 0 if label == "short_unrelated" else 100,
    )
    assert selected is not None and selected.label == "parent"


def test_runner_precedence_assigns_source_dependency_before_downstream_parent(
    enabled, monkeypatch, tmp_path
):
    """Do not keep a parent assigned while its generated source helper has sorry."""
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Demo.lean")
    parent_id = plan_state.node_id_for("parent", active_file)
    helper_id = plan_state.node_id_for("source_helper", active_file)
    planned_id = plan_state.node_id_for("planned_subhelper", active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=parent_id,
                    name="parent",
                    file=active_file,
                    status="proving",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    name="source_helper",
                    file=active_file,
                    status="stated",
                    generated_by="decomposer",
                ),
                plan_state.GraphNode(
                    id=planned_id,
                    name="planned_subhelper",
                    file=active_file,
                    status="conjectured",
                    generated_by="decomposer",
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
                plan_state.GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
                plan_state.GraphEdge(
                    source=helper_id,
                    target=planned_id,
                    kind="depends_on",
                ),
            ),
        )
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "parent",
            "active_file": active_file,
        }
    }
    queue = tuple(
        QueueItem(label=label, reasons=("contains sorry",)) for label in ("source_helper", "parent")
    )

    precedence = runner._graph_frontier_precedence(
        autonomy_state,
        active_file=active_file,
        queue_labels=("source_helper", "parent"),
    )

    assert precedence is not None
    assert precedence("source_helper") == -1
    assert precedence("parent") == 1
    selected = select_next_item(
        queue,
        is_present_in_file=lambda _label: True,
        precedence=precedence,
        order_key=lambda label: 0 if label == "parent" else 100,
    )
    assert selected is not None and selected.label == "source_helper"


def test_runner_precedence_keeps_source_helper_with_graph_only_child(
    enabled, monkeypatch, tmp_path
):
    """A planning-only child cannot oscillate its source helper with the parent."""
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Demo.lean")
    parent_id = plan_state.node_id_for("parent", active_file)
    helper_id = plan_state.node_id_for("source_helper", active_file)
    planned_id = plan_state.node_id_for("planned_subhelper", active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=parent_id,
                    name="parent",
                    file=active_file,
                    status="proving",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    name="source_helper",
                    file=active_file,
                    status="blocked",
                    generated_by="decomposer",
                ),
                plan_state.GraphNode(
                    id=planned_id,
                    name="planned_subhelper",
                    file=active_file,
                    status="conjectured",
                    generated_by="decomposer",
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
                plan_state.GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
                plan_state.GraphEdge(
                    source=helper_id,
                    target=planned_id,
                    kind="depends_on",
                ),
            ),
        )
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "source_helper",
            "active_file": active_file,
        },
        "theorem_outcomes": {
            f"{active_file}::source_helper": {
                "target_symbol": "source_helper",
                "active_file": active_file,
                "status": "deferred",
                "note": "current route exhausted",
            }
        },
    }
    queue = tuple(
        QueueItem(label=label, reasons=("contains sorry",)) for label in ("parent", "source_helper")
    )

    precedence = runner._graph_frontier_precedence(
        autonomy_state,
        active_file=active_file,
        queue_labels=("parent", "source_helper"),
    )

    assert precedence is not None
    assert precedence("source_helper") == -2
    assert precedence("parent") == 2
    selected = select_next_item(
        queue,
        is_present_in_file=lambda _label: True,
        precedence=precedence,
        order_key=lambda label: 0 if label == "parent" else 100,
    )
    assert selected is not None and selected.label == "source_helper"


def test_runner_precedence_hands_verified_source_absent_child_to_direct_parent(
    enabled, monkeypatch, tmp_path
):
    """Prefer the exact parent when source truth gets ahead of graph reconciliation."""
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Erdos242.lean")
    child = "erdos_242_residual_mod_seven_eq_one_normalized_of_mod_five_zero"
    parent = "erdos_242_residual_mod_seven_eq_one_normalized"
    unrelated = "erdos_242_family_one_ordering"
    ancestor = "erdos_242_residual_mod_seven_eq_one"
    old_sibling = "erdos_242_residual_mod_seven_eq_one_of_mod_five_one"
    child_id = plan_state.node_id_for(child, active_file)
    parent_id = plan_state.node_id_for(parent, active_file)
    unrelated_id = plan_state.node_id_for(unrelated, active_file)
    ancestor_id = plan_state.node_id_for(ancestor, active_file)
    old_sibling_id = plan_state.node_id_for(old_sibling, active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=unrelated_id,
                    name=unrelated,
                    file=active_file,
                    status="stated",
                ),
                plan_state.GraphNode(
                    id=parent_id,
                    name=parent,
                    file=active_file,
                    status="audited",
                ),
                # This is the live stale state: kernel/source verification has
                # removed the child from the queue, while the graph still says
                # that the child is being proved.
                plan_state.GraphNode(
                    id=child_id,
                    name=child,
                    file=active_file,
                    status="proving",
                ),
                plan_state.GraphNode(
                    id=ancestor_id,
                    name=ancestor,
                    file=active_file,
                    status="stated",
                ),
                plan_state.GraphNode(
                    id=old_sibling_id,
                    name=old_sibling,
                    file=active_file,
                    status="stated",
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=child_id, target=parent_id, kind="split_of"),
                plan_state.GraphEdge(source=parent_id, target=child_id, kind="depends_on"),
                # Preserve the older family edges present in the live graph;
                # handback must not climb or revive either historical branch.
                plan_state.GraphEdge(source=parent_id, target=ancestor_id, kind="split_of"),
                plan_state.GraphEdge(source=ancestor_id, target=parent_id, kind="depends_on"),
                plan_state.GraphEdge(source=old_sibling_id, target=parent_id, kind="split_of"),
            ),
        )
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": child,
            "active_file": active_file,
        }
    }
    queue = (
        QueueItem(label=unrelated, reasons=("contains sorry",)),
        QueueItem(label=parent, reasons=("contains sorry",)),
    )

    precedence = runner._graph_frontier_precedence(
        autonomy_state,
        active_file=active_file,
        queue_labels=tuple(item.label for item in queue),
    )

    assert precedence is not None
    assert precedence(parent) == -1
    assert precedence(unrelated) == 0
    assert precedence(ancestor) == 1
    selected = select_next_item(
        queue,
        is_present_in_file=lambda _label: True,
        precedence=precedence,
        order_key=lambda label: 0 if label == unrelated else 100,
    )
    assert selected is not None
    assert selected.label == parent


def test_runner_precedence_revives_fresh_split_dependency_from_stale_deferred_outcome(
    enabled, monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Demo.lean")
    parent_id = plan_state.node_id_for("parent", active_file)
    helper_id = plan_state.node_id_for("helper", active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=parent_id, name="parent", file=active_file, status="proving"
                ),
                plan_state.GraphNode(
                    id=helper_id, name="helper", file=active_file, status="stated"
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
                plan_state.GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
            ),
        )
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "parent",
            "active_file": active_file,
        },
        "theorem_outcomes": {
            f"{active_file}::helper": {
                "target_symbol": "helper",
                "active_file": active_file,
                "status": "deferred",
            }
        },
    }

    precedence = runner._graph_frontier_precedence(autonomy_state, active_file=active_file)

    assert precedence is not None
    assert precedence("helper") == -1


def test_runner_precedence_is_file_scoped_for_duplicate_declaration_names(
    enabled, monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    first_file = str(tmp_path / "A.lean")
    second_file = str(tmp_path / "B.lean")
    parent_id = plan_state.node_id_for("parent", first_file)
    first_helper_id = plan_state.node_id_for("helper", first_file)
    second_helper_id = plan_state.node_id_for("helper", second_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=parent_id, name="parent", file=first_file, status="proving"
                ),
                plan_state.GraphNode(
                    id=first_helper_id, name="helper", file=first_file, status="stated"
                ),
                plan_state.GraphNode(
                    id=second_helper_id, name="helper", file=second_file, status="false"
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=first_helper_id, target=parent_id, kind="split_of"),
                plan_state.GraphEdge(source=parent_id, target=first_helper_id, kind="depends_on"),
            ),
        )
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "parent",
            "active_file": first_file,
        }
    }

    first = runner._graph_frontier_precedence(autonomy_state, active_file=first_file)
    second = runner._graph_frontier_precedence({}, active_file=second_file)

    assert first is not None and first("helper") == -1
    assert second is not None and second("helper") == 3


def test_runner_precedence_propagates_invalid_dependencies_and_avoids_cycles(
    enabled, monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Demo.lean")
    ids = {
        name: plan_state.node_id_for(name, active_file)
        for name in ("parent", "middle", "false_leaf", "cycle_a", "cycle_b")
    }
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=tuple(
                plan_state.GraphNode(
                    id=ids[name],
                    name=name,
                    file=active_file,
                    status="false" if name == "false_leaf" else "stated",
                )
                for name in ids
            ),
            edges=(
                plan_state.GraphEdge(source=ids["parent"], target=ids["middle"], kind="depends_on"),
                plan_state.GraphEdge(
                    source=ids["middle"], target=ids["false_leaf"], kind="depends_on"
                ),
                plan_state.GraphEdge(
                    source=ids["cycle_a"], target=ids["cycle_b"], kind="depends_on"
                ),
                plan_state.GraphEdge(
                    source=ids["cycle_b"], target=ids["cycle_a"], kind="depends_on"
                ),
            ),
        )
    )

    precedence = runner._graph_frontier_precedence({}, active_file=active_file)

    assert precedence is not None
    assert precedence("parent") == 3
    assert precedence("middle") == 3
    assert precedence("cycle_a") == 2
    assert precedence("cycle_b") == 2


def test_false_current_helper_excludes_its_split_family_before_replan(
    enabled, monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Demo.lean")
    parent_id = plan_state.node_id_for("parent", active_file)
    helper_id = plan_state.node_id_for("false_helper", active_file)
    sibling_id = plan_state.node_id_for("sibling", active_file)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=parent_id, name="parent", file=active_file, status="conjectured"
                ),
                plan_state.GraphNode(
                    id=helper_id, name="false_helper", file=active_file, status="false"
                ),
                plan_state.GraphNode(
                    id=sibling_id, name="sibling", file=active_file, status="stated"
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=helper_id, target=parent_id, kind="split_of"),
                plan_state.GraphEdge(source=sibling_id, target=parent_id, kind="split_of"),
                plan_state.GraphEdge(source=parent_id, target=helper_id, kind="depends_on"),
                plan_state.GraphEdge(source=parent_id, target=sibling_id, kind="depends_on"),
            ),
        )
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "false_helper",
            "active_file": active_file,
        }
    }

    precedence = runner._graph_frontier_precedence(
        autonomy_state,
        active_file=active_file,
        queue_labels=("sibling", "parent"),
    )

    assert precedence is not None
    assert precedence("sibling") == 3
    assert precedence("parent") == 3
    assert (
        select_next_item(
            (
                QueueItem(label="sibling", reasons=("contains sorry",)),
                QueueItem(label="parent", reasons=("contains sorry",)),
            ),
            is_present_in_file=lambda _label: True,
            precedence=precedence,
        )
        is None
    )


def test_ambiguous_multiple_split_parents_fail_closed(enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active_file = str(tmp_path / "Demo.lean")
    names = ("helper", "parent_a", "parent_b", "sibling_a", "sibling_b", "unrelated")
    ids = {name: plan_state.node_id_for(name, active_file) for name in names}
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=tuple(
                plan_state.GraphNode(
                    id=ids[name],
                    name=name,
                    file=active_file,
                    status="proved" if name == "helper" else "stated",
                )
                for name in names
            ),
            edges=(
                plan_state.GraphEdge(source=ids["helper"], target=ids["parent_a"], kind="split_of"),
                plan_state.GraphEdge(source=ids["helper"], target=ids["parent_b"], kind="split_of"),
                plan_state.GraphEdge(
                    source=ids["parent_a"], target=ids["helper"], kind="depends_on"
                ),
                plan_state.GraphEdge(
                    source=ids["parent_a"], target=ids["sibling_a"], kind="depends_on"
                ),
                plan_state.GraphEdge(
                    source=ids["parent_b"], target=ids["helper"], kind="depends_on"
                ),
                plan_state.GraphEdge(
                    source=ids["parent_b"], target=ids["sibling_b"], kind="depends_on"
                ),
            ),
        )
    )
    state = {
        "current_queue_assignment": {
            "target_symbol": "helper",
            "active_file": active_file,
        }
    }

    precedence = runner._graph_frontier_precedence(
        state,
        active_file=active_file,
        queue_labels=("parent_a", "parent_b", "sibling_a", "sibling_b", "unrelated"),
    )

    assert precedence is not None
    assert precedence("sibling_a") == 3
    assert precedence("sibling_b") == 3
    assert precedence("unrelated") == 0


def test_exhausted_graph_frontier_routes_to_plan():
    ctx = RouteContext(
        trigger="scope-entry",
        active_file="Demo.lean",
        declaration_queue_total=1,
        sorry_count=1,
        queue_frontier_exhausted=True,
    )

    route = orchestrator_route(ctx)

    assert route.route == "plan"
    assert "no assignable graph frontier" in route.reason
