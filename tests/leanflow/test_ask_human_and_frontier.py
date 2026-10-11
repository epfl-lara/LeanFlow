"""Phase 4 (5/6) tests: ask-human route, re-state ACK, graph-frontier selection."""

from __future__ import annotations

import pytest

from leanflow_cli.workflows.orchestrator import RouteContext, orchestrator_route
from leanflow_cli.workflows.queue_models import QueueItem, select_next_item


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_HUMAN_REVIEW_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))


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
