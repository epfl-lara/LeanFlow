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


def test_exhausted_screening_site_is_blocked_before_another_result():
    state: dict = {}
    common = {
        "function_name": "lean_multi_attempt",
        "args": {
            "file_path": "/tmp/Main.lean",
            "line": 102,
            "attempts": ["simp", "omega"],
        },
        "result_text": _failed_screen(),
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "source_revision_sha256": "same-source",
    }
    for _ in range(tool_result_loop_guard.HARD_LIMIT):
        tool_result_loop_guard.observe(state, **common)

    # A later tracked tool may become the current streak, but it must not
    # erase the exhausted exact screening site.
    tool_result_loop_guard.observe(
        state,
        function_name="lean_outline",
        args={"file_path": "/tmp/Main.lean", "symbol": "helper"},
        result_text=json.dumps({"success": True, "symbol": "helper"}),
        target_symbol="demo",
        active_file="/tmp/Main.lean",
        source_revision_sha256="same-source",
    )

    blocked = tool_result_loop_guard.exhausted_preflight(
        state,
        function_name="lean_multi_attempt",
        args=common["args"],
        target_symbol="demo",
        active_file="/tmp/Main.lean",
        source_revision_sha256="same-source",
    )
    changed_site = tool_result_loop_guard.exhausted_preflight(
        state,
        function_name="lean_multi_attempt",
        args={**common["args"], "line": 103},
        target_symbol="demo",
        active_file="/tmp/Main.lean",
        source_revision_sha256="same-source",
    )
    changed_source = tool_result_loop_guard.exhausted_preflight(
        state,
        function_name="lean_multi_attempt",
        args=common["args"],
        target_symbol="demo",
        active_file="/tmp/Main.lean",
        source_revision_sha256="changed-source",
    )

    assert blocked is not None
    assert blocked["streak"] == tool_result_loop_guard.HARD_LIMIT
    assert changed_site is None
    assert changed_source is None


def test_exhausted_preflight_result_does_not_reopen_or_close_the_turn():
    state: dict = {}
    common = {
        "function_name": "lean_multi_attempt",
        "args": {"file_path": "/tmp/Main.lean", "line": 102, "attempts": ["simp", "omega"]},
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "source_revision_sha256": "same-source",
    }
    for _ in range(tool_result_loop_guard.HARD_LIMIT):
        tool_result_loop_guard.observe(state, result_text=_failed_screen(), **common)

    decision = tool_result_loop_guard.observe(
        state,
        result_text=json.dumps(
            {
                "success": False,
                "status": "tool_result_retry_exhausted",
                "lean_started": False,
                "signature": state[tool_result_loop_guard.STATE_KEY]["signature"],
                "streak": tool_result_loop_guard.HARD_LIMIT,
            }
        ),
        **common,
    )

    assert decision.streak == tool_result_loop_guard.HARD_LIMIT
    assert decision.close_turn is False
    assert tool_result_loop_guard.EXHAUSTED_STATE_KEY in state


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


def test_helper_check_application_mismatch_tracks_symbol_across_statement_variants():
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
                        f"private lemma candidate_{index} : True := by\n"
                        f"  have h := exact_dependency argument_{index}\n"
                        "  trivial\n"
                    ),
                },
                result_text=json.dumps(
                    {
                        "success": False,
                        "ok": False,
                        "error": "Application type mismatch: The argument",
                        "output": (
                            "Application type mismatch: The argument\n"
                            "  candidate\n"
                            "has type Nat but is expected to have type Bool\n"
                            "in the application\n"
                            f"  exact_dependency argument_{index}"
                        ),
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
    assert decisions[-1].required_symbol == "exact_dependency"


def test_helper_check_application_mismatch_resets_for_different_symbol():
    state: dict = {}

    def observe(symbol: str):
        return tool_result_loop_guard.observe(
            state,
            function_name="lean_incremental_check",
            args={
                "action": "check_helper",
                "replacement": f"private lemma candidate_{symbol} : True := by trivial",
            },
            result_text=json.dumps(
                {
                    "success": False,
                    "ok": False,
                    "error": "Application type mismatch",
                    "output": f"Application type mismatch\nin the application\n  {symbol} value",
                }
            ),
            target_symbol="demo",
            active_file="/tmp/Main.lean",
            source_revision_sha256="same-source",
        )

    first = observe("dependency_a")
    second = observe("dependency_a")
    changed = observe("dependency_b")

    assert (first.streak, second.streak, changed.streak) == (1, 2, 1)
    assert changed.required_symbol == "dependency_b"


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


def test_proof_context_is_owned_by_the_search_synthesis_fence():
    """Do not interrupt before a threshold proof-context batch can be synthesized."""
    assert tool_result_loop_guard.tool_key("lean_proof_context", {}) == ""


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
    assert blocked.close_turn is False
    assert blocked.streak == tool_result_loop_guard.ADVISOR_NUDGE_LIMIT == 2


def test_exhausted_advisor_preflight_preserves_failure_memory_without_closing_turn():
    """A local circuit rejection must not cancel concrete sibling tool calls."""
    state: dict = {}
    common = {
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "source_revision_sha256": "same-source",
    }
    tool_result_loop_guard.hydrate_advisor_failure_streak(state, **common)

    decision = tool_result_loop_guard.observe(
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

    assert decision.tool_key == "lean_advisor"
    assert decision.streak == tool_result_loop_guard.ADVISOR_NUDGE_LIMIT
    assert decision.close_turn is False
    assert state[tool_result_loop_guard.ADVISOR_STATE_KEY]["streak"] == (
        tool_result_loop_guard.ADVISOR_NUDGE_LIMIT
    )


def test_unrelated_tools_do_not_erase_advisor_failure_family():
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
    tool_result_loop_guard.observe(
        state,
        function_name="lean_inspect",
        args={"target": "demo"},
        result_text=json.dumps({"success": True, "sorry_count": 1}),
        **common,
    )
    second = tool_result_loop_guard.observe(
        state,
        function_name="lean_decompose_helpers",
        args={"theorem_id": "demo"},
        result_text=json.dumps({"success": False, "status": "unavailable"}),
        **common,
    )

    assert first.streak == 1
    assert second.streak == tool_result_loop_guard.ADVISOR_NUDGE_LIMIT
    assert tool_result_loop_guard.advisor_preflight_blocked(
        state,
        function_name="lean_reasoning_help",
        **common,
    )


def test_durable_advisor_streak_hydrates_process_local_boundary():
    state: dict = {}
    common = {
        "target_symbol": "demo",
        "active_file": "/tmp/Main.lean",
        "source_revision_sha256": "same-source",
    }
    tool_result_loop_guard.hydrate_advisor_failure_streak(state, **common)

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

    assert blocked.streak == tool_result_loop_guard.ADVISOR_NUDGE_LIMIT
    assert blocked.close_turn is False


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
    assert tool_result_loop_guard.ADVISOR_STATE_KEY not in state


def test_varying_terminal_policy_denials_share_one_family():
    state: dict = {}
    common = {
        "target_symbol": "result",
        "active_file": "Main.lean",
        "source_revision_sha256": "same-source",
    }

    decisions = [
        tool_result_loop_guard.observe(
            state,
            function_name="terminal",
            args={"command": command},
            result_text=json.dumps(
                {
                    "success": False,
                    "status": "clean_room_terminal_denied",
                    "error": "command is outside the read-only allowlist",
                }
            ),
            **common,
        )
        for command in (
            "python3 probe.py",
            "python3 -c 'print(1)'",
            "bash probe.sh",
            "node probe.js",
            "ruby probe.rb",
            "perl probe.pl",
        )
    ]

    assert decisions[tool_result_loop_guard.NUDGE_LIMIT - 1].nudge is True
    assert decisions[-1].close_turn is True
    assert decisions[-1].streak == tool_result_loop_guard.HARD_LIMIT


def test_successful_terminal_clears_policy_denial_streak():
    state: dict = {}
    common = {
        "target_symbol": "result",
        "active_file": "Main.lean",
        "source_revision_sha256": "same-source",
    }
    tool_result_loop_guard.observe(
        state,
        function_name="terminal",
        args={"command": "python3 probe.py"},
        result_text=json.dumps({"success": False, "status": "clean_room_terminal_denied"}),
        **common,
    )

    decision = tool_result_loop_guard.observe(
        state,
        function_name="terminal",
        args={"command": "lake env lean Main.lean"},
        result_text=json.dumps({"success": True, "exit_code": 0}),
        **common,
    )

    assert decision.streak == 0
    assert tool_result_loop_guard.STATE_KEY not in state
