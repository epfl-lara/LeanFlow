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
