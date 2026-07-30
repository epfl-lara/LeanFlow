"""Tests for assignment-local repeated Lean tool-result boundaries."""

from __future__ import annotations

import json

from leanflow_cli.workflows import tool_result_loop_guard


def _failed_screen(line: int = 102, column: int = 85) -> str:
    return json.dumps(
        {
            "success": False,
            "status": "screened_no_verified_candidate",
            "backend_tool": "mcp_lean_lsp_lean_multi_attempt",
            "items": [
                {
                    "snippet": "candidate text that may change",
                    "diagnostics": [
                        {
                            "severity": "error",
                            "message": "No goals to be solved",
                            "line": line,
                            "column": column,
                        }
                    ],
                }
            ],
        }
    )


def test_repeated_result_nudges_then_closes_the_turn():
    state: dict = {}
    decisions = [
        tool_result_loop_guard.observe(
            state,
            function_name="lean_multi_attempt",
            args={"attempts": [f"candidate {index}", "other"]},
            result_text=_failed_screen(),
            target_symbol="demo",
            active_file="/tmp/Main.lean",
            source_revision_sha256="same-source",
        )
        for index in range(tool_result_loop_guard.HARD_LIMIT)
    ]

    assert decisions[tool_result_loop_guard.NUDGE_LIMIT - 1].nudge is True
    assert decisions[-1].close_turn is True
    assert decisions[-1].streak == tool_result_loop_guard.HARD_LIMIT


def test_changed_source_or_diagnostic_resets_the_streak():
    state: dict = {}
    common = {
        "function_name": "lean_multi_attempt",
        "args": {"attempts": ["simp", "omega"]},
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
    }
    first = tool_result_loop_guard.observe(
        state,
        result_text=_failed_screen(),
        source_revision_sha256="source-a",
        **common,
    )
    second = tool_result_loop_guard.observe(
        state,
        result_text=_failed_screen(),
        source_revision_sha256="source-a",
        **common,
    )
    changed_source = tool_result_loop_guard.observe(
        state,
        result_text=_failed_screen(),
        source_revision_sha256="source-b",
        **common,
    )
    changed_diagnostic = tool_result_loop_guard.observe(
        state,
        result_text=_failed_screen(line=140),
        source_revision_sha256="source-b",
        **common,
    )

    assert (first.streak, second.streak) == (1, 2)
    assert changed_source.streak == 1
    assert changed_diagnostic.streak == 1


def test_verified_result_clears_prior_loop_state():
    state: dict = {}
    common = {
        "function_name": "lean_multi_attempt",
        "args": {"attempts": ["simp", "omega"]},
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "source_revision_sha256": "same-source",
    }
    tool_result_loop_guard.observe(state, result_text=_failed_screen(), **common)
    decision = tool_result_loop_guard.observe(
        state,
        result_text=json.dumps(
            {
                "success": True,
                "target_verified": True,
                "verified_attempts": ["simp"],
            }
        ),
        **common,
    )

    assert decision.close_turn is False
    assert tool_result_loop_guard.STATE_KEY not in state


def test_exact_check_is_not_tracked_but_feedback_is():
    assert (
        tool_result_loop_guard.tool_key("lean_incremental_check", {"action": "check_target"}) == ""
    )
    assert (
        tool_result_loop_guard.tool_key("lean_incremental_check", {"action": "feedback"})
        == "lean_incremental_check:feedback"
    )
