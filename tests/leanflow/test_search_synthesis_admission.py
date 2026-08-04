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
