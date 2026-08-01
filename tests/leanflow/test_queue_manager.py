from __future__ import annotations

import json

from leanflow_cli.workflows.queue_manager import (
    Classification,
    DecisionContext,
    DecisionSource,
    ManagerCheck,
    PrepareState,
    QueueInvariantError,
    QueueItem,
    TheoremKey,
    TheoremQueueManager,
    VerificationRecord,
    VerificationScope,
    classify_check,
    select_next_item,
)


def test_select_next_item_uses_only_diagnostic_or_sorry_items() -> None:
    queue = [
        QueueItem(label="clean", reasons=()),
        QueueItem(label="broken", reasons=("diagnostic near line 12",)),
        QueueItem(label="later", reasons=("contains sorry",)),
    ]

    selected = select_next_item(queue, is_present_in_file=lambda label: label != "missing")

    assert selected is not None
    assert selected.label == "broken"
    assert select_next_item([queue[0]], is_present_in_file=lambda _label: True) is None


def test_classify_check_uses_one_explicit_priority_order() -> None:
    assert classify_check(ManagerCheck(has_assigned_sorry=True)) is Classification.HARD_BLOCKER
    assert (
        classify_check(ManagerCheck(has_assigned_error=True, has_assigned_warning=True))
        is Classification.HARD_BLOCKER
    )
    assert classify_check(ManagerCheck(has_assigned_warning=True)) is Classification.WARNING_ONCE
    assert (
        classify_check(ManagerCheck(verification_failed=True, has_future_evidence=True))
        is Classification.FUTURE_ONLY
    )
    assert classify_check(ManagerCheck(verification_failed=True)) is Classification.HARD_BLOCKER
    assert classify_check(ManagerCheck()) is Classification.ACCEPT


def test_warning_cleanup_is_consumed_once_per_assignment(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager(warning_retry_limit=1)
    mgr.assign(QueueItem(label="demo", reasons=("contains sorry",)), active_file=str(active))

    first_ctx = DecisionContext(
        source=DecisionSource.FINAL_REPORT, check=ManagerCheck(has_assigned_warning=True)
    )
    first = mgr.apply_decision(first_ctx, mgr.decide(first_ctx))
    second_ctx = DecisionContext(
        source=DecisionSource.FINAL_REPORT, check=ManagerCheck(has_assigned_warning=True)
    )
    second = mgr.decide(second_ctx)

    assert first.action == "continue_same_theorem"
    assert first.classification is Classification.WARNING_ONCE
    assert first.feedback_kind == "warning"
    assert first.retry_count == 1
    assert second.action == "advance_queue"
    assert second.classification is Classification.ACCEPT
    assert second.accepted_after_warning_limit is True
    assert mgr.warning_retries_for_current() == 1

    # Committing the accept clears the retry bookkeeping (legacy runner behavior).
    mgr.apply_decision(second_ctx, second)
    assert mgr.warning_retries_for_current() == 0


def test_hard_feedback_window_completion_requests_a_new_route(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager(hard_retry_limit=1)
    mgr.assign(QueueItem(label="demo"), active_file=str(active))
    ctx = DecisionContext(
        source=DecisionSource.FINAL_REPORT,
        check=ManagerCheck(has_assigned_sorry=True),
        signature="first-rejection",
    )
    mgr.apply_decision(ctx, mgr.decide(ctx))

    decision = mgr.decide(ctx)

    assert decision.action == "restore_baseline"
    assert decision.restore_baseline is True
    assert decision.reason == (
        "local feedback window complete; restore baseline sorry and continue on a new route"
    )


def test_retry_signatures_are_idempotent_and_serialized(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))

    first = mgr.consume_retry_once(kind="warning", signature="same-warning")
    second = mgr.consume_retry_once(kind="warning", signature="same-warning")
    third = mgr.consume_retry_once(kind="warning", signature="new-warning")

    assert (first, second, third) == (1, 1, 2)
    restored = TheoremQueueManager.from_autonomy_state(mgr.to_autonomy_state())
    assert restored.warning_retries_for_current() == 2
    assert restored.consume_retry_once(kind="warning", signature="same-warning") == 2


def test_assign_transition_clears_retry_counters_atomically(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem first : True := by",
                "  sorry",
                "",
                "theorem second : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="first"), active_file=str(active))
    mgr.consume_warning_retry()
    mgr.consume_hard_retry()
    mgr.record_verification(
        VerificationRecord(
            scope=VerificationScope.TARGET,
            ok=True,
            tool="lean_incremental_check",
            target="first",
            summary="target first passed",
        )
    )
    mgr.record_attempt(cycle=1, proof_shape="exact ?x", reason="unsolved goals")

    transition = mgr.assign(QueueItem(label="second"), active_file=str(active))

    assert transition.is_new_theorem()
    assert mgr.current is not None
    assert mgr.current.key == TheoremKey.make("second", str(active))
    assert mgr.warning_retries_for_current() == 0
    assert mgr.hard_retries_for_current() == 0
    assert mgr.last_verification is None
    mgr.check_invariants()


def test_check_invariants_rejects_stale_retry_counters(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))
    mgr._warning_retries[TheoremKey.make("other", str(active))] = 1

    try:
        mgr.check_invariants()
    except QueueInvariantError as exc:
        assert "survived a transition" in str(exc)
    else:
        raise AssertionError("expected stale retry counter invariant failure")


def test_record_attempts_are_scoped_and_pruned_per_theorem(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager(failed_attempt_history=2)
    mgr.assign(QueueItem(label="demo"), active_file=str(active))

    for cycle in range(1, 5):
        mgr.record_attempt(cycle=cycle, proof_shape=f"attempt {cycle}", reason="type mismatch")

    attempts = mgr.attempts_for_current()
    assert attempts == 2
    assert [attempt.cycle for attempt in mgr.attempts_for(mgr.current.key)] == [3, 4]


def test_record_attempt_deduplicates_gate_presentations_within_one_turn(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))

    first = mgr.record_attempt(
        cycle=2,
        proof_shape="- sorry + exact candidate",
        reason="warning: declaration uses 'sorry'",
        declaration_hash="ABC123",
        gate_verdict=" Warning:  declaration uses 'SORRY' ",
        turn_key="run-1:cycle-2",
    )
    duplicate = mgr.record_attempt(
        cycle=2,
        proof_shape="theorem demo : True := by exact candidate",
        reason="warning: declaration uses 'sorry'",
        declaration_hash="abc123",
        gate_verdict="warning: declaration uses 'sorry'",
        turn_key="run-1:cycle-2",
    )

    assert first is not None
    assert duplicate is None
    assert mgr.attempt_count_for(mgr.current.key) == 1
    assert mgr.attempts_for(mgr.current.key)[0].proof_shape.startswith("- sorry")


def test_record_attempt_identity_allows_real_changes_and_new_turns(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))

    common = {
        "cycle": 2,
        "proof_shape": "shape",
        "reason": "warning: declaration uses 'sorry'",
        "declaration_hash": "decl-a",
        "gate_verdict": "sorry",
        "turn_key": "run-1:cycle-2",
    }
    assert mgr.record_attempt(**common) is not None
    assert mgr.record_attempt(**{**common, "declaration_hash": "decl-b"}) is not None
    assert mgr.record_attempt(**{**common, "gate_verdict": "type mismatch"}) is not None
    assert mgr.record_attempt(**{**common, "turn_key": "run-1:cycle-3"}) is not None
    assert mgr.attempt_count_for(mgr.current.key) == 4


def test_failed_attempt_dedupe_identity_survives_checkpoint_resume(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))
    kwargs = {
        "cycle": 7,
        "proof_shape": "first presentation",
        "reason": "unsolved goals",
        "declaration_hash": "declaration-sha256",
        "gate_verdict": "Unsolved   goals",
        "turn_key": "run-resumed:cycle-7",
    }
    assert mgr.record_attempt(**kwargs) is not None

    state = mgr.to_autonomy_state()
    restored = TheoremQueueManager.from_autonomy_state(state)
    duplicate = restored.record_attempt(
        **{**kwargs, "proof_shape": "full declaration presentation"}
    )

    assert duplicate is None
    attempts = restored.attempts_for(TheoremKey.make("demo", str(active)))
    assert len(attempts) == 1
    assert state["failed_attempts"][0]["declaration_hash"] == "declaration-sha256"
    assert state["failed_attempts"][0]["gate_verdict"] == "unsolved goals"
    assert state["failed_attempts"][0]["turn_key"] == "run-resumed:cycle-7"


def test_successful_tool_results_are_not_failed_attempt_evidence(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))

    successful = mgr.record_attempt(
        cycle=1,
        proof_shape="prepare_file",
        reason=json.dumps({"success": True, "ok": True, "status": "prepared"}),
    )
    operational_failure = mgr.record_attempt(
        cycle=2,
        proof_shape="check_target",
        reason=json.dumps({"success": True, "ok": False, "error": "kernel rejected"}),
    )

    assert successful is None
    assert operational_failure is not None
    assert mgr.attempts_for_current() == 1


def test_truncated_success_and_content_payloads_are_not_failed_attempt_evidence(
    tmp_path,
) -> None:
    active = tmp_path / "Main.lean"
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))

    truncated_success = mgr.record_attempt(
        cycle=1,
        proof_shape="research",
        reason='{"success": true, "status": "answered", "response": "truncated...',
    )
    content_only = mgr.record_attempt(
        cycle=2,
        proof_shape="read_file",
        reason=json.dumps({"content": "theorem demo : True := by sorry"}),
    )

    assert truncated_success is None
    assert content_only is None
    assert mgr.attempts_for_current() == 0


def test_resume_discards_legacy_successful_tool_result_attempt(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    state = {
        "failed_attempts": [
            {
                "target_symbol": "demo",
                "active_file": str(active),
                "attempt": 1,
                "cycle": 1,
                "proof_shape": "prepare_file",
                "reason": json.dumps({"success": True, "ok": True}),
            },
            {
                "target_symbol": "demo",
                "active_file": str(active),
                "attempt": 2,
                "cycle": 2,
                "proof_shape": "exact True.intro",
                "reason": (
                    "target:demo passed | tool: lean_incremental_check | "
                    "errors: 0, warnings: 0, sorry: 0"
                ),
            },
            {
                "target_symbol": "demo",
                "active_file": str(active),
                "attempt": 3,
                "cycle": 3,
                "proof_shape": "exact missing",
                "reason": "unknown identifier 'missing'",
            },
        ]
    }

    restored = TheoremQueueManager.from_autonomy_state(state)
    attempts = restored.attempts_for(TheoremKey.make("demo", str(active)))

    assert len(attempts) == 1
    assert attempts[0].attempt == 1
    assert attempts[0].proof_shape == "exact missing"


def test_resume_preserves_full_history_ring_ordinals(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    state = {
        "failed_attempts": [
            {
                "target_symbol": "demo",
                "active_file": str(active),
                "attempt": number,
                "cycle": number,
                "proof_shape": f"attempt {number}",
                "reason": "kernel rejected",
            }
            for number in range(11, 21)
        ]
    }

    restored = TheoremQueueManager.from_autonomy_state(state)
    attempts = restored.attempts_for(TheoremKey.make("demo", str(active)))

    assert [attempt.attempt for attempt in attempts] == list(range(11, 21))


def test_verification_and_disabled_tools_are_typed_run_state(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))
    record = VerificationRecord(
        scope=VerificationScope.TARGET,
        ok=True,
        tool="lean_incremental_check",
        target="demo",
        cache="warm",
        elapsed_s=0.42,
        errors=0,
        sorry_count=0,
        summary="target demo passed",
    )

    mgr.record_verification(record)
    mgr.disable_tool("lean_auto_try")

    assert mgr.last_verification == record
    assert mgr.disabled_tools == frozenset({"lean_auto_try"})
    mgr.invalidate_verification()
    assert mgr.last_verification is None


def test_legacy_autonomy_state_round_trips_with_normalized_keys(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(
        QueueItem(label="demo"),
        active_file=str(active),
        slice_text="theorem demo : True := by\n  sorry",
        prepare=PrepareState(success=True, ok=True, elapsed_s=0.1),
    )
    mgr.consume_warning_retry()
    mgr.record_verification(
        VerificationRecord(
            scope=VerificationScope.TARGET,
            ok=True,
            tool="lean_incremental_check",
            target="demo",
            summary="target demo passed",
        )
    )
    mgr.disable_tool("lean_auto_try")
    mgr.record_attempt(cycle=2, proof_shape="- sorry\n+ exact True.intro", reason="unsolved goals")
    mgr.record_outcome(status="blocked", note="retry limit")

    restored = TheoremQueueManager.from_autonomy_state(mgr.to_autonomy_state())

    assert restored.current is not None
    assert restored.current.key == TheoremKey.make("demo", str(active))
    assert restored.current.prepare.is_warm()
    assert restored.warning_retries_for_current() == 1
    assert restored.attempts_for_current() == 1
    assert restored.outcome_for(restored.current.key).status == "blocked"
    assert restored.last_verification is not None
    assert restored.last_verification.tool == "lean_incremental_check"
    assert restored.disabled_tools == frozenset({"lean_auto_try"})


def test_outcome_verification_round_trips(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))
    record = VerificationRecord(
        scope=VerificationScope.TARGET,
        ok=True,
        tool="lean_incremental_check",
        target="demo",
        summary="target demo passed",
        source_revision_sha256="a" * 64,
    )

    mgr.record_outcome(status="solved", note="done", verification=record)

    state = mgr.to_autonomy_state()
    outcome = next(iter(state["theorem_outcomes"].values()))
    assert outcome["last_verification"]["scope"] == "target:demo"
    assert outcome["last_verification"]["source_revision_sha256"] == "a" * 64
    restored = TheoremQueueManager.from_autonomy_state(state)
    assert restored.outcome_for(restored.current.key).verification.tool == "lean_incremental_check"
    assert (
        restored.outcome_for(restored.current.key).verification.source_revision_sha256 == "a" * 64
    )


def test_blocked_outcomes_reopen_idempotently_at_strategy_refresh(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="blocked_demo"), active_file=str(active))
    blocked_key = mgr.current.key
    mgr.record_outcome(status="blocked", note="direct route exhausted")
    mgr.assign(QueueItem(label="solved_demo"), active_file=str(active))
    solved_key = mgr.current.key
    mgr.record_outcome(status="solved", note="kernel accepted")

    reopened = mgr.reopen_blocked_outcomes(trigger="campaign epoch refresh")

    assert [outcome.key for outcome in reopened] == [blocked_key]
    assert mgr.outcome_for(blocked_key).status == "unresolved"
    assert "prior blocker: direct route exhausted" in mgr.outcome_for(blocked_key).note
    assert mgr.outcome_for(solved_key).status == "solved"
    assert mgr.reopen_blocked_outcomes(trigger="same refresh replay") == ()


def test_deferred_route_outcomes_reopen_without_losing_attempt_evidence(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="hard_demo"), active_file=str(active))
    deferred_key = mgr.current.key
    mgr.record_attempt(
        cycle=7,
        proof_shape="exact attempted_shape",
        reason="route-specific blocker",
    )
    mgr.record_outcome(status="deferred", note="direct route exhausted")

    reopened = mgr.reopen_blocked_outcomes(trigger="campaign epoch refresh")

    assert [outcome.key for outcome in reopened] == [deferred_key]
    assert mgr.outcome_for(deferred_key).status == "unresolved"
    assert "prior blocker: direct route exhausted" in mgr.outcome_for(deferred_key).note
    assert mgr.attempt_entries_for(deferred_key)[0]["proof_shape"] == "exact attempted_shape"


def test_retire_theorem_state_removes_deleted_helper_scheduler_knowledge(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    mgr = TheoremQueueManager()
    mgr.replace_queue([QueueItem(label="bad_helper"), QueueItem(label="parent_goal")])
    mgr.assign(QueueItem(label="bad_helper"), active_file=str(active))
    helper_key = mgr.current.key
    mgr.record_attempt(cycle=3, proof_shape="exact impossible", reason="false")
    mgr.record_outcome(status="disproved", note="authoritative negation")

    assert mgr.retire_theorem_state(helper_key) is True
    assert mgr.current is None
    assert mgr.outcome_for(helper_key) is None
    assert mgr.attempt_entries_for(helper_key) == ()
    assert [item.label for item in mgr.queue] == ["parent_goal"]
    assert "bad_helper" not in json.dumps(mgr.to_checkpoint_state())
    assert mgr.retire_theorem_state(helper_key) is False


def test_checkpoint_state_preserves_knowledge_but_resets_process_local_state(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    mgr = TheoremQueueManager()
    mgr.assign(
        QueueItem(label="demo"),
        active_file=str(active),
        prepare=PrepareState(success=True, ok=True, elapsed_s=1.0),
    )
    mgr.record_attempt(cycle=3, proof_shape="exact missing", reason="unknown identifier")
    mgr.record_verification(
        VerificationRecord(
            scope=VerificationScope.TARGET,
            ok=False,
            tool="lean_incremental_check",
            target="demo",
            summary="target failed",
        )
    )
    mgr.disable_tool("lean_auto_search", "provider unavailable")

    state = mgr.to_checkpoint_state()

    assert state["failed_attempts"][0]["proof_shape"] == "exact missing"
    assert state["current_queue_assignment"]["incremental_prepare"]["success"] is False
    assert "last_verification" not in state
    assert "disabled_tools_this_run" not in state


def test_pending_count_excludes_current_item(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    mgr = TheoremQueueManager()
    mgr.replace_queue(
        [
            QueueItem(label="current", reasons=("contains sorry",)),
            QueueItem(label="future", reasons=("contains sorry",)),
        ]
    )
    mgr.assign(QueueItem(label="current"), active_file=str(active))

    assert mgr.pending_count == 1


def test_peek_assignment_does_not_mutate_current_assignment(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    mgr = TheoremQueueManager()
    mgr.replace_queue([QueueItem(label="first"), QueueItem(label="second")])
    mgr.assign(QueueItem(label="first"), active_file=str(active))

    view = mgr.peek_assignment(QueueItem(label="second"), active_file=str(active))

    assert mgr.current.key.target_symbol == "first"
    assert view.current.key.target_symbol == "second"
