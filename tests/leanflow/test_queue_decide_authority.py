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

from typing import Any

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import queue_decide_shadow
from leanflow_cli.workflows.queue_manager import (
    DecisionContext,
    DecisionSource,
    ManagerCheck,
    TheoremQueueManager,
)


def _blocked_live_state() -> dict[str, Any]:
    return {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "current_queue_item_slice": "theorem demo : True := by\n  sorry",
        "diagnostics": "error: unsolved goals",
        "goals": "⊢ False",
        "build_status": "unknown",
        "blocker_summary": "error: unsolved goals",
    }


def _clean_advanced_live_state() -> dict[str, Any]:
    return {
        "target_symbol": "next_demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
        "diagnostics": "warning: declaration uses sorry",
        "goals": "no goals",
        "build_status": "ok",
    }


def test_authority_flag_default_off(monkeypatch):
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", raising=False)
    assert queue_decide_shadow.authority_enabled() is False
    for truthy in ("1", "true", "yes", "on", "ON"):
        monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", truthy)
        assert queue_decide_shadow.authority_enabled() is True
    monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", "0")
    assert queue_decide_shadow.authority_enabled() is False


def test_live_state_gate_blocks_under_authority(monkeypatch):
    """A still-sorry assignment stays blocked whether decide() or the legacy
    predicate answers — same_assignment domain, HARD_BLOCKER classification."""
    monkeypatch.setattr(runner, "_find_declaration_entry", lambda file, label: {"has_sorry": True})
    autonomy = {
        "current_queue_assignment": {"target_symbol": "demo", "active_file": "Demo/Main.lean"}
    }
    live = _blocked_live_state()

    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", raising=False)
    legacy = runner._same_queue_assignment_still_blocked(autonomy, live)
    monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", "1")
    authority = runner._same_queue_assignment_still_blocked(autonomy, live)
    assert legacy is True and authority is True


def test_live_state_gate_advances_clean_under_authority(monkeypatch):
    """A clean assignment (no sorry, closed goals) advances under both."""
    monkeypatch.setattr(runner, "_find_declaration_entry", lambda file, label: {"has_sorry": False})
    autonomy = {
        "current_queue_assignment": {"target_symbol": "demo", "active_file": "Demo/Main.lean"}
    }
    live = {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": []},
        "goals": "no goals",
        "build_status": "ok",
    }
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", raising=False)
    legacy = runner._same_queue_assignment_still_blocked(autonomy, live)
    monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", "1")
    authority = runner._same_queue_assignment_still_blocked(autonomy, live)
    assert legacy is False and authority is False


def test_live_state_gate_advances_on_changed_assignment_under_authority(monkeypatch):
    """The same-assignment guard stays runner-owned: a fresh (advanced) item
    is never blocked, even with the flag on — decide() is only consulted on
    the same-assignment domain."""
    monkeypatch.setattr(runner, "_find_declaration_entry", lambda file, label: {"has_sorry": True})
    autonomy = {
        "current_queue_assignment": {"target_symbol": "demo", "active_file": "Demo/Main.lean"}
    }
    monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", "1")
    assert (
        runner._same_queue_assignment_still_blocked(autonomy, _clean_advanced_live_state()) is False
    )


def _wire_final_report(monkeypatch, *, incremental_ok, output=""):
    """Final-report seams with the SHADOW flag OFF (authority must run alone)."""
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_SHADOW", raising=False)
    monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", "1")
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "file")
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda active_file, target: {
            "ok": incremental_ok,
            "mode": "incremental_target",
            "command": "lean_interact check_target",
            "target": target,
            "output": output,
            "incremental": {
                "success": True,
                "ok": incremental_ok,
                "valid_without_sorry": incremental_ok,
                "has_errors": not incremental_ok,
                "has_sorry": not incremental_ok,
            },
        },
    )
    monkeypatch.setattr(
        runner, "_query_live_diagnostics", lambda active_file, target_symbol="": "no errors found"
    )
    return events


def _final_report_result():
    return {
        "completed": True,
        "interrupted": False,
        "final_response": "`demo` solved.",
        "messages": [{"role": "assistant", "content": "`demo` solved."}],
    }


def _spy_on_decide(monkeypatch):
    """Record the DecisionSource of every decide() call so a test can prove the
    authority path (not the legacy engine) actually ran."""
    sources: list[DecisionSource] = []
    original = TheoremQueueManager.decide

    def spy(self, ctx):
        sources.append(ctx.source)
        return original(self, ctx)

    monkeypatch.setattr(TheoremQueueManager, "decide", spy)
    return sources


def test_final_report_gate_runs_under_authority_without_shadow(monkeypatch, tmp_path):
    """Regression for the shadow-coupling bug: the FINAL_REPORT flip builds its
    own decide() evidence and drives the verdict even when the shadow flag is
    OFF. Proven by spying on decide() (a clean/sorry check would accept/reject
    under EITHER path, so behavior alone is not enough)."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    _wire_final_report(monkeypatch, incremental_ok=True)
    sources = _spy_on_decide(monkeypatch)
    autonomy = {"current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}}
    updated = runner._review_agent_final_report(_final_report_result(), autonomy)
    assert updated["manager_final_report_review"]["ok"] is True
    assert DecisionSource.FINAL_REPORT in sources  # the flip consulted decide()

    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    _wire_final_report(monkeypatch, incremental_ok=False, output="error: declaration uses sorry")
    sources = _spy_on_decide(monkeypatch)
    autonomy = {"current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}}
    updated = runner._review_agent_final_report(_final_report_result(), autonomy)
    assert updated["manager_final_report_review"]["ok"] is False
    assert DecisionSource.FINAL_REPORT in sources


def test_final_report_legacy_does_not_consult_decide(monkeypatch, tmp_path):
    """Counterpart: with the flag OFF, decide() is never consulted at the gate
    (the legacy engine owns the verdict) — proving the spy above is meaningful."""
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", raising=False)
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_SHADOW", raising=False)
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    # _wire_final_report sets authority=1; undo that for the legacy check.
    _wire_final_report(monkeypatch, incremental_ok=True)
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", raising=False)
    sources = _spy_on_decide(monkeypatch)
    autonomy = {"current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}}
    runner._review_agent_final_report(_final_report_result(), autonomy)
    assert DecisionSource.FINAL_REPORT not in sources


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
