"""Tests for bounded search-to-construction admission state."""

from __future__ import annotations

from leanflow_cli.native import search_synthesis_admission


def test_search_file_fingerprint_ignores_presentation_options():
    common = {"path": "/tmp/Main.lean", "pattern": "top_sum_bound"}

    content = search_synthesis_admission.source_inspection_fingerprint(
        "search_files",
        {**common, "output_mode": "content", "context": 20},
    )
    files_only = search_synthesis_admission.source_inspection_fingerprint(
        "search_files",
        {**common, "output_mode": "files_only", "context": 0},
    )

    assert content == files_only


def test_source_inspection_observation_resets_per_cycle_and_bounds_repeats():
    tracker: dict = {}
    for _ in range(3):
        tracker, decision = search_synthesis_admission.observe_source_inspection(
            tracker,
            function_name="search_files",
            args={"path": "/tmp/Main.lean", "pattern": "top_sum_bound"},
            cycle=4,
            hard_limit=12,
            repeat_hard_limit=3,
        )

    assert decision.close_turn is True
    assert decision.same_request_streak == 3

    tracker, refreshed = search_synthesis_admission.observe_source_inspection(
        tracker,
        function_name="read_file",
        args={"path": "/tmp/Main.lean", "offset": 1, "limit": 40},
        cycle=5,
        hard_limit=12,
        repeat_hard_limit=3,
    )

    assert refreshed.close_turn is False
    assert refreshed.count == 1
    assert tracker["construction_source_inspection_cycle"] == 5


def test_construction_source_boundary_blocks_only_its_cycle():
    """Keep an exhausted source window closed until orchestration advances."""
    tracker = {
        "construction_source_inspection_cycle": 4,
        "construction_source_inspection_count": 12,
        "construction_source_inspection_boundary": True,
    }

    blocked = search_synthesis_admission.blocked_construction_source_result(
        function_name="read_file",
        tracker=tracker,
        target_symbol="demo",
        active_file="/tmp/Main.lean",
        current_cycle=4,
    )
    refreshed = search_synthesis_admission.blocked_construction_source_result(
        function_name="read_file",
        tracker=tracker,
        target_symbol="demo",
        active_file="/tmp/Main.lean",
        current_cycle=5,
    )

    assert blocked is not None
    assert blocked["status"] == "construction_synthesis_required"
    assert refreshed is None


def test_construction_route_handoff_opens_fresh_provider_window():
    """A forced route handoff must not poison the next provider conversation."""
    tracker = {
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "construction_source_inspection_cycle": 4,
        "construction_source_inspection_count": 12,
        "construction_source_inspection_boundary": True,
        "construction_synthesis_rejection_count": 2,
    }

    pending = search_synthesis_admission.schedule_fresh_construction_window(tracker)
    assert pending["construction_source_window_reset_pending"] is True

    refreshed = search_synthesis_admission.prepare_provider_turn(pending)
    assert "construction_source_window_reset_pending" not in refreshed
    assert "construction_source_inspection_cycle" not in refreshed
    assert "construction_source_inspection_count" not in refreshed
    assert "construction_source_inspection_boundary" not in refreshed
    assert "construction_synthesis_rejection_count" not in refreshed
    assert refreshed["target_symbol"] == "demo"


def test_construction_debt_accumulates_across_route_labels():
    """Route alternation must not erase unchanged no-construction turns."""
    tracker = None
    for route in ("decompose", "negate", "decompose"):
        tracker, decision = search_synthesis_admission.observe_unresolved_construction_turn(
            tracker,
            target_symbol="demo",
            active_file="/tmp/Main.lean",
            source_revision_sha256="source-a",
            construction_attempt_serial=0,
            requested_route=route,
            limit=3,
        )

    assert decision.count == 3
    assert decision.require_construction is True
    assert tracker["routes"] == ["decompose", "negate", "decompose"]
    assert search_synthesis_admission.construction_required_for_assignment(
        tracker,
        target_symbol="demo",
        active_file="/tmp/Main.lean",
        source_revision_sha256="source-a",
        construction_attempt_serial=0,
    )


def test_construction_attempt_or_source_change_clears_route_debt():
    """Reward a concrete candidate or material source change immediately."""
    tracker = {
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "source_revision_sha256": "source-a",
        "construction_attempt_serial": 2,
        "count": 4,
        "routes": ["decompose", "negate"],
        "require_construction": True,
    }

    after_attempt, attempt_decision = (
        search_synthesis_admission.observe_unresolved_construction_turn(
            tracker,
            target_symbol="demo",
            active_file="/tmp/Main.lean",
            source_revision_sha256="source-a",
            construction_attempt_serial=3,
            requested_route="plan",
            limit=3,
        )
    )
    after_edit, edit_decision = search_synthesis_admission.observe_unresolved_construction_turn(
        tracker,
        target_symbol="demo",
        active_file="/tmp/Main.lean",
        source_revision_sha256="source-b",
        construction_attempt_serial=2,
        requested_route="plan",
        limit=3,
    )

    assert attempt_decision.reset_reason == "construction-attempted"
    assert attempt_decision.count == 0
    assert after_attempt["require_construction"] is False
    assert edit_decision.reset_reason == "source-changed"
    assert edit_decision.count == 0
    assert after_edit["require_construction"] is False


def test_construction_attempt_classifier_excludes_inspection_and_exact_replay():
    """Only materially distinct Lean candidates discharge construction debt."""
    assert search_synthesis_admission.construction_attempt_request(
        "lean_incremental_check",
        {
            "action": "check_helper",
            "replacement": "private lemma useful : True := by\n  trivial",
        },
    )
    assert not search_synthesis_admission.construction_attempt_request(
        "lean_incremental_check",
        {
            "action": "check_helper",
            "replacement": "#check Nat.add_comm\nprivate lemma inspect : True := by trivial",
        },
    )
    assert not search_synthesis_admission.construction_attempt_request(
        "apply_verified_patch",
        {"patch": "candidate"},
        result_status="rejected_candidate_replay",
    )
    assert not search_synthesis_admission.construction_attempt_request(
        "apply_verified_patch",
        {"patch": "candidate"},
        result_status="isolated_suggestion_probe_required",
    )
