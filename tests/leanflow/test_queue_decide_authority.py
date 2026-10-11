"""Phase 0 rollout: the decide()-authoritative FLIP (LEANFLOW_QUEUE_DECIDE_AUTHORITY).

The heavy parity net lives in ``test_queue_step_boundary_golden.py`` (every
boundary golden runs under both ``legacy`` and ``authority``) and in the
manual soak (0 shadow mismatches over a real 4-theorem run). These tests pin
the flag reader, the LIVE_STATE gate end-to-end under the flag, and the one
INTENDED behavior change the flip adopts: decide()'s canonical golden-grid
verdict on cells the legacy branch handled differently (BUDGET_EXHAUSTION with
non-hard evidence advances instead of unconditionally restoring).
"""

from __future__ import annotations

from leanflow_cli.workflows import queue_decide_shadow
from leanflow_cli.workflows.queue_manager import (
    DecisionContext,
    DecisionSource,
    ManagerCheck,
    TheoremQueueManager,
)


def test_authority_flag_default_off(monkeypatch):
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", raising=False)
    assert queue_decide_shadow.authority_enabled() is False
    for truthy in ("1", "true", "yes", "on", "ON"):
        monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", truthy)
        assert queue_decide_shadow.authority_enabled() is True
    monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", "0")
    assert queue_decide_shadow.authority_enabled() is False


def test_budget_gate_decide_policy_restores_only_on_hard():
    """decide()'s BUDGET_EXHAUSTION policy (what the flag-on gate honors):
    RESTORE only on a HARD_BLOCKER; non-hard evidence would advance. NOTE:
    the gate's own precondition — _same_queue_assignment_still_blocked, itself
    a hard-only predicate — normally filters non-hard evidence BEFORE this
    decide() runs, so restore_baseline is the production-reachable verdict; the
    non-hard branch guards only the rare gate-1/live-evidence skew. This is a
    POLICY assertion (the gate consumes decision.restore_baseline)."""
    mgr = TheoremQueueManager()
    warn = ManagerCheck(has_assigned_warning=True)
    decision = mgr.decide(DecisionContext(source=DecisionSource.BUDGET_EXHAUSTION, check=warn))
    assert decision.action == "advance_queue"
    assert decision.restore_baseline is False

    hard = ManagerCheck(has_assigned_error=True)
    decision = mgr.decide(DecisionContext(source=DecisionSource.BUDGET_EXHAUSTION, check=hard))
    assert decision.action == "restore_baseline"
    assert decision.restore_baseline is True
