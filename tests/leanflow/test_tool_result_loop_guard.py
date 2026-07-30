"""Tests for assignment-local repeated Lean tool-result boundaries."""

from __future__ import annotations

import json

from leanflow_cli.workflows import tool_result_loop_guard


def _failed_screen(
    line: int = 102,
    column: int = 85,
    *,
    status: str = "screened_no_verified_candidate",
    message: str = "No goals to be solved",
) -> str:
    return json.dumps(
        {
            "success": False,
            "status": status,
            "backend_tool": "mcp_lean_lsp_lean_multi_attempt",
            "items": [
                {
                    "snippet": "candidate text that may change",
                    "diagnostics": [
                        {
                            "severity": "error",
                            "message": message,
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


def test_changed_source_or_screening_location_resets_the_streak():
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
    changed_common = {
        **common,
        "args": {"attempts": ["simp", "omega"], "line": 140},
    }
    changed_location = tool_result_loop_guard.observe(
        state,
        result_text=_failed_screen(line=140),
        source_revision_sha256="source-b",
        **changed_common,
    )

    assert (first.streak, second.streak) == (1, 2)
    assert changed_source.streak == 1
    assert changed_location.streak == 1


def test_multi_attempt_tracks_same_location_across_varying_failure_shapes():
    state: dict = {}
    decisions = []
    for index in range(tool_result_loop_guard.HARD_LIMIT):
        result = _failed_screen(
            status=("screened_no_verified_candidate" if index % 2 == 0 else "invalid_candidates"),
            message=f"candidate family {index} cannot prove the unchanged goal",
        )
        decisions.append(
            tool_result_loop_guard.observe(
                state,
                function_name="lean_multi_attempt",
                args={
                    "file_path": "/tmp/Main.lean",
                    "line": 102,
                    "attempts": [f"candidate {index}", "other"],
                },
                result_text=result,
                target_symbol="demo",
                active_file="/tmp/Main.lean",
                source_revision_sha256="same-source",
            )
        )

    assert decisions[tool_result_loop_guard.NUDGE_LIMIT - 1].nudge is True
    assert decisions[-1].close_turn is True
    assert decisions[-1].streak == tool_result_loop_guard.HARD_LIMIT


def test_helper_check_tracks_same_statement_across_varying_proof_failures():
    state: dict = {}
    decisions = []
    for index in range(tool_result_loop_guard.HARD_LIMIT):
        decisions.append(
            tool_result_loop_guard.observe(
                state,
                function_name="lean_incremental_check",
                args={
                    "action": "check_helper",
                    "replacement": (
                        "private lemma helper_false {n : ℕ} (h : n ≤ 1) : n = 1 := by\n"
                        f"  have attempt_{index} : n ≤ 1 := h\n"
                        "  omega\n"
                    ),
                },
                result_text=json.dumps(
                    {
                        "success": True,
                        "ok": False,
                        "action": "check_helper",
                        "messages": [
                            {
                                "severity": "error",
                                "message": f"proof variant {index} failed",
                                "file_start": {"line": 12 + index, "column": 3},
                            }
                        ],
                    }
                ),
                target_symbol="demo",
                active_file="/tmp/Main.lean",
                source_revision_sha256="same-source",
            )
        )

    assert decisions[tool_result_loop_guard.NUDGE_LIMIT - 1].nudge is True
    assert decisions[-1].close_turn is True
    assert decisions[-1].streak == tool_result_loop_guard.HARD_LIMIT


def test_helper_check_changed_statement_resets_the_streak():
    state: dict = {}
    common = {
        "function_name": "lean_incremental_check",
        "result_text": json.dumps(
            {
                "success": True,
                "ok": False,
                "action": "check_helper",
                "messages": [{"severity": "error", "message": "failed"}],
            }
        ),
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "source_revision_sha256": "same-source",
    }
    first = tool_result_loop_guard.observe(
        state,
        args={
            "action": "check_helper",
            "replacement": "private lemma helper_a : False := by\n  omega\n",
        },
        **common,
    )
    second = tool_result_loop_guard.observe(
        state,
        args={
            "action": "check_helper",
            "replacement": "private lemma helper_a : False := by\n  simp\n",
        },
        **common,
    )
    changed = tool_result_loop_guard.observe(
        state,
        args={
            "action": "check_helper",
            "replacement": "private lemma helper_a : True := by\n  simp\n",
        },
        **common,
    )

    assert (first.streak, second.streak) == (1, 2)
    assert changed.streak == 1


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


def test_outline_budget_counts_different_symbols_until_source_changes():
    state: dict = {}
    decisions = [
        tool_result_loop_guard.observe(
            state,
            function_name="lean_outline",
            args={"file_path": "/tmp/Main.lean", "symbol": f"helper_{index}"},
            result_text=json.dumps(
                {
                    "success": True,
                    "symbol": f"helper_{index}",
                    "declaration": {"name": f"helper_{index}"},
                }
            ),
            target_symbol="demo",
            active_file="/tmp/Main.lean",
            source_revision_sha256="same-source",
        )
        for index in range(tool_result_loop_guard.OUTLINE_HARD_LIMIT)
    ]

    assert decisions[tool_result_loop_guard.OUTLINE_NUDGE_LIMIT - 1].nudge is True
    assert decisions[-1].close_turn is True
    assert decisions[-1].streak == tool_result_loop_guard.OUTLINE_HARD_LIMIT

    changed = tool_result_loop_guard.observe(
        state,
        function_name="lean_outline",
        args={"file_path": "/tmp/Main.lean", "symbol": "after_edit"},
        result_text=json.dumps(
            {
                "success": True,
                "symbol": "after_edit",
                "declaration": {"name": "after_edit"},
            }
        ),
        target_symbol="demo",
        active_file="/tmp/Main.lean",
        source_revision_sha256="changed-source",
    )

    assert changed.streak == 1
    assert changed.close_turn is False


def test_alternating_advisor_failures_share_one_bounded_family():
    state: dict = {}
    common = {
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "source_revision_sha256": "same-source",
    }
    first = tool_result_loop_guard.observe(
        state,
        function_name="lean_reasoning_help",
        args={"theorem_id": "demo"},
        result_text=json.dumps({"success": False, "status": "timeout"}),
        **common,
    )
    second = tool_result_loop_guard.observe(
        state,
        function_name="lean_decompose_helpers",
        args={"theorem_id": "demo"},
        result_text=json.dumps({"success": False, "status": "unavailable"}),
        **common,
    )

    assert first.tool_key == second.tool_key == "lean_advisor"
    assert first.streak == 1
    assert second.streak == tool_result_loop_guard.ADVISOR_NUDGE_LIMIT == 2
    assert second.nudge is True
    assert tool_result_loop_guard.advisor_preflight_blocked(
        state,
        function_name="lean_reasoning_help",
        **common,
    )

    blocked = tool_result_loop_guard.observe(
        state,
        function_name="lean_reasoning_help",
        args={"theorem_id": "demo"},
        result_text=json.dumps(
            {
                "success": False,
                "status": "advisor_retry_exhausted",
                "provider_called": False,
            }
        ),
        **common,
    )
    assert blocked.close_turn is True
    assert blocked.streak == tool_result_loop_guard.ADVISOR_HARD_LIMIT == 3


def test_successful_advisor_answer_clears_failure_family():
    state: dict = {}
    common = {
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "source_revision_sha256": "same-source",
    }
    tool_result_loop_guard.observe(
        state,
        function_name="lean_reasoning_help",
        args={},
        result_text=json.dumps({"success": False, "status": "timeout"}),
        **common,
    )
    decision = tool_result_loop_guard.observe(
        state,
        function_name="lean_decompose_helpers",
        args={},
        result_text=json.dumps({"success": True, "status": "completed"}),
        **common,
    )

    assert decision.tool_key == "lean_advisor"
    assert decision.streak == 0
    assert tool_result_loop_guard.STATE_KEY not in state
