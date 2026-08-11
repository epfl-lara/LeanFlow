"""Pin the unified `decide()` and `apply_decision()` verdict policy.

`decide(DecisionContext)` is pure, while `apply_decision()` commits retry side
effects. Each row covers a source-tagged verdict branch and matches the
queue-boundary characterization tests.
"""

from __future__ import annotations

import pytest

from leanflow_cli.workflows.queue_manager import (
    Classification,
    DecisionContext,
    DecisionSource,
    ManagerCheck,
    QueueItem,
    TheoremQueueManager,
)

HARD_CHECK = ManagerCheck(has_assigned_error=True)
SORRY_CHECK = ManagerCheck(has_assigned_sorry=True)
WARNING_CHECK = ManagerCheck(has_assigned_warning=True)
CLEAN_CHECK = ManagerCheck()
FUTURE_CHECK = ManagerCheck(verification_failed=True, has_future_evidence=True)


def _manager(tmp_path) -> TheoremQueueManager:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo", reasons=("contains sorry",)), active_file=str(active))
    return mgr


def test_decide_is_pure(tmp_path):
    """Verify that `decide()` performs no I/O or mutation."""
    mgr = _manager(tmp_path)
    ctx = DecisionContext(source=DecisionSource.POST_EDIT, check=HARD_CHECK, signature="sig-1")

    first = mgr.decide(ctx)
    second = mgr.decide(ctx)

    assert first == second
    assert mgr.hard_retries_for_current() == 0
    assert mgr.warning_retries_for_current() == 0


@pytest.mark.parametrize(
    ("source", "check", "expected"),
    [
        # (action, feedback_kind, consume_retry, retry_limit,
        #  record_failed_attempt, restore_baseline)
        # D2: hard-retry limits are source-dependent data.
        (
            DecisionSource.FINAL_REPORT,
            HARD_CHECK,
            ("continue_same_theorem", "error", "hard", 2, False, False),
        ),
        (
            DecisionSource.POST_EDIT,
            HARD_CHECK,
            ("continue_same_theorem", "error", "hard", 8, True, False),
        ),
        (
            DecisionSource.VERIFICATION_RESULT,
            HARD_CHECK,
            ("continue_same_theorem", "error", "", 0, True, False),
        ),
        (
            DecisionSource.LIVE_STATE,
            HARD_CHECK,
            ("continue_same_theorem", "error", "", 0, False, False),
        ),
        (
            DecisionSource.BUDGET_EXHAUSTION,
            HARD_CHECK,
            ("restore_baseline", "error", "", 0, True, True),
        ),
        # Sorry evidence renders the legacy "sorry" kind.
        (
            DecisionSource.POST_EDIT,
            SORRY_CHECK,
            ("continue_same_theorem", "sorry", "hard", 8, True, False),
        ),
        # Warning-once grants the single cleanup turn.
        (
            DecisionSource.FINAL_REPORT,
            WARNING_CHECK,
            ("continue_same_theorem", "warning", "warning", 1, False, False),
        ),
        (
            DecisionSource.POST_EDIT,
            WARNING_CHECK,
            ("continue_same_theorem", "warning", "warning", 1, False, False),
        ),
        # The legacy warning branch has NO trigger-tool gate: verification
        # results consume the cleanup window too (only HARD retries are
        # post-edit-gated, drift D2).
        (
            DecisionSource.VERIFICATION_RESULT,
            WARNING_CHECK,
            ("continue_same_theorem", "warning", "warning", 1, False, False),
        ),
        # Predicate-only sources: warning evidence means "not blocked" —
        # never a continue, never a restore, never a consumption.
        (
            DecisionSource.LIVE_STATE,
            WARNING_CHECK,
            ("advance_queue", "warning", "", 0, False, False),
        ),
        (
            DecisionSource.BUDGET_EXHAUSTION,
            WARNING_CHECK,
            ("advance_queue", "warning", "", 0, False, False),
        ),
        # Clean / future-only advance with the legacy "" kind (D7 adapter rule).
        (DecisionSource.FINAL_REPORT, CLEAN_CHECK, ("advance_queue", "", "", 0, False, False)),
        (DecisionSource.FINAL_REPORT, FUTURE_CHECK, ("advance_queue", "", "", 0, False, False)),
        (DecisionSource.LIVE_STATE, CLEAN_CHECK, ("advance_queue", "", "", 0, False, False)),
    ],
)
def test_decide_golden_grid(tmp_path, source, check, expected):
    mgr = _manager(tmp_path)
    decision = mgr.decide(DecisionContext(source=source, check=check))

    actual = (
        decision.action,
        decision.feedback_kind,
        decision.consume_retry,
        decision.retry_limit,
        decision.record_failed_attempt,
        decision.restore_baseline,
    )
    assert actual == expected


def test_hard_exhaustion_uses_pre_consumption_count(tmp_path):
    """Restore fires only when the count has already reached the limit."""
    mgr = _manager(tmp_path)
    ctx = DecisionContext(source=DecisionSource.FINAL_REPORT, check=HARD_CHECK)

    below = mgr.apply_decision(ctx, mgr.decide(ctx))
    assert below.action == "continue_same_theorem"
    assert below.retry_count == 1

    at_edge = mgr.apply_decision(ctx, mgr.decide(ctx))
    assert at_edge.action == "continue_same_theorem"
    assert at_edge.retry_count == 2

    exhausted = mgr.decide(ctx)
    assert exhausted.action == "restore_baseline"
    assert exhausted.restore_baseline is True
    assert exhausted.retry_count == 2
    assert exhausted.consume_retry == ""


def test_post_edit_signature_idempotency(tmp_path):
    """An identical check (same signature) never double-consumes — D3/boundary pin."""
    mgr = _manager(tmp_path)
    ctx = DecisionContext(source=DecisionSource.POST_EDIT, check=HARD_CHECK, signature="same")

    first = mgr.apply_decision(ctx, mgr.decide(ctx))
    assert first.retry_count == 1

    # decide() predicts the post-consumption count without mutating: a spent
    # signature means the count stays.
    predicted = mgr.decide(ctx)
    assert predicted.retry_count == 1
    second = mgr.apply_decision(ctx, predicted)
    assert second.retry_count == 1
    assert mgr.hard_retries_for_current() == 1

    fresh = DecisionContext(source=DecisionSource.POST_EDIT, check=HARD_CHECK, signature="new")
    third = mgr.apply_decision(fresh, mgr.decide(fresh))
    assert third.retry_count == 2


def test_verification_result_never_consumes(tmp_path):
    """D2: verification-tool triggers consume nothing at any count."""
    mgr = _manager(tmp_path)
    ctx = DecisionContext(source=DecisionSource.VERIFICATION_RESULT, check=HARD_CHECK)

    for _ in range(10):
        decision = mgr.apply_decision(ctx, mgr.decide(ctx))
        assert decision.action == "continue_same_theorem"
        assert decision.record_failed_attempt is True
    assert mgr.hard_retries_for_current() == 0


def test_axiom_blockers_veto_acceptance(tmp_path):
    """D6: a clean kernel check with forbidden axioms is still a hard blocker."""
    mgr = _manager(tmp_path)
    ctx = DecisionContext(
        source=DecisionSource.FINAL_REPORT,
        check=CLEAN_CHECK,
        axiom_blockers=("sorryAx",),
    )

    decision = mgr.decide(ctx)
    assert decision.action == "continue_same_theorem"
    assert decision.classification is Classification.HARD_BLOCKER
    assert decision.feedback_kind == "error"

    # The veto forces classification, not the kind: sorry evidence still
    # renders the legacy "sorry" string.
    with_sorry = mgr.decide(
        DecisionContext(
            source=DecisionSource.FINAL_REPORT, check=SORRY_CHECK, axiom_blockers=("sorryAx",)
        )
    )
    assert with_sorry.classification is Classification.HARD_BLOCKER
    assert with_sorry.feedback_kind == "sorry"


def test_cleanup_reason_folds_into_classification(tmp_path):
    """The legacy local_cleanup_reason routing applies inside decide()."""
    mgr = _manager(tmp_path)

    warned = mgr.decide(
        DecisionContext(
            source=DecisionSource.POST_EDIT,
            check=CLEAN_CHECK,
            cleanup_reason="unused variable `h`",
        )
    )
    assert warned.action == "continue_same_theorem"
    assert warned.classification is Classification.WARNING_ONCE
    assert warned.feedback_kind == "warning"

    sorried = mgr.decide(
        DecisionContext(
            source=DecisionSource.POST_EDIT,
            check=CLEAN_CHECK,
            cleanup_reason="contains sorry",
        )
    )
    assert sorried.classification is Classification.HARD_BLOCKER
    assert sorried.feedback_kind == "sorry"

    errored = mgr.decide(
        DecisionContext(
            source=DecisionSource.POST_EDIT,
            check=CLEAN_CHECK,
            cleanup_reason="error: unsolved goals",
        )
    )
    assert errored.classification is Classification.HARD_BLOCKER
    assert errored.feedback_kind == "error"


def test_warning_acceptance_clears_bookkeeping_on_apply(tmp_path):
    mgr = _manager(tmp_path)
    ctx = DecisionContext(source=DecisionSource.POST_EDIT, check=WARNING_CHECK, signature="w1")

    granted = mgr.apply_decision(ctx, mgr.decide(ctx))
    assert granted.action == "continue_same_theorem"
    assert mgr.warning_retries_for_current() == 1

    accepted = mgr.decide(ctx)
    assert accepted.action == "advance_queue"
    assert accepted.accepted_after_warning_limit is True
    assert accepted.classification is Classification.ACCEPT

    mgr.apply_decision(ctx, accepted)
    assert mgr.warning_retries_for_current() == 0
    assert mgr.hard_retries_for_current() == 0


def test_decide_without_assignment_stays_sane(tmp_path):
    mgr = TheoremQueueManager()
    ctx = DecisionContext(source=DecisionSource.POST_EDIT, check=HARD_CHECK)

    decision = mgr.decide(ctx)
    assert decision.action == "continue_same_theorem"
    assert decision.retry_count == 0
    # apply_decision without a current assignment is a no-op.
    assert mgr.apply_decision(ctx, decision) == decision
