from __future__ import annotations

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
    )

    mgr.record_outcome(status="solved", note="done", verification=record)

    state = mgr.to_autonomy_state()
    outcome = next(iter(state["theorem_outcomes"].values()))
    assert outcome["last_verification"]["scope"] == "target:demo"
    restored = TheoremQueueManager.from_autonomy_state(state)
    assert restored.outcome_for(restored.current.key).verification.tool == "lean_incremental_check"


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
