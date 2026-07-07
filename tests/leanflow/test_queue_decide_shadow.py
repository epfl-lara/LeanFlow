"""Shadow-compare tests (Phase 0 P0.4): decide() vs the legacy verdict gates.

With LEANFLOW_QUEUE_DECIDE_SHADOW=1 the boundary and live-state gates also
evaluate the pure decide() policy and log a queue-decide-shadow-mismatch
activity event on divergence. The golden grid must produce ZERO mismatches;
a synthetically-wrong legacy reification must produce exactly one.
"""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner


class _StubAgent:
    quiet_mode = True

    def __init__(self, autonomy_state: dict[str, Any]):
        self._managed_autonomy_state = autonomy_state
        self._session_messages = [{"role": "assistant", "content": "partial"}]
        self._managed_pending_theorem_feedback: dict[str, str] | None = {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
        }
        self.interrupt_messages: list[str | None] = []

    def is_interrupted(self) -> bool:
        return False

    def interrupt(self, message: str | None = None) -> None:
        self.interrupt_messages.append(message)

    def set_tool_result_appendix(self, text: str) -> None:
        self._post_tool_result_appendix = text

    def clear_tool_result_appendix(self) -> None:
        if hasattr(self, "_post_tool_result_appendix"):
            del self._post_tool_result_appendix


def _autonomy_state() -> dict[str, Any]:
    return {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
            "slice": "theorem demo : True := by\n  sorry",
        },
        "current_cycle": 2,
    }


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


def _clean_same_assignment_live_state() -> dict[str, Any]:
    return {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": ["diagnostic near line 3"]},
        "diagnostics": "",
        "goals": "no goals",
        "build_status": "ok",
    }


def _wire(monkeypatch, live_state, *, cleanup_reason="", has_sorry=False):
    monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_SHADOW", "1")
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: dict(live_state),
    )
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    monkeypatch.setattr(
        runner, "_declaration_diagnostic_feedback_reason", lambda *args, **kwargs: cleanup_reason
    )
    monkeypatch.setattr(
        runner, "_find_declaration_entry", lambda file, label: {"has_sorry": has_sorry}
    )
    return events


def _mismatches(events):
    return [(a, k) for a, k in events if a[0] == "queue-decide-shadow-mismatch"]


def _run_boundary(agent, *, tool="patch+lean_incremental_check", check=None):
    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool=tool,
        manager_verification=check,
    )


HARD_CHECK = {
    "ok": False,
    "mode": "incremental_target",
    "command": "lean_interact check_target",
    "target": "demo",
    "output": "error: unsolved goals",
}


@pytest.mark.parametrize("tool", ["patch+lean_incremental_check", "lean_verify"])
def test_hard_error_continue_produces_no_mismatch(monkeypatch, tool):
    autonomy_state = _autonomy_state()
    events = _wire(monkeypatch, _blocked_live_state())
    _run_boundary(_StubAgent(autonomy_state), tool=tool, check=dict(HARD_CHECK))
    assert _mismatches(events) == []


def test_hard_exhaustion_restore_produces_no_mismatch(monkeypatch):
    autonomy_state = _autonomy_state()
    events = _wire(monkeypatch, _blocked_live_state())
    monkeypatch.setattr(
        runner,
        "_restore_queue_assignment_to_baseline_sorry",
        lambda *args, **kwargs: {"restored": True, "reason": "restored"},
    )
    for _ in range(runner.MANAGER_POST_EDIT_HARD_RETRY_LIMIT):
        runner._increment_manager_feedback_retry(
            autonomy_state,
            target_symbol="demo",
            active_file="Demo/Main.lean",
            kind="error",
        )
    _run_boundary(_StubAgent(autonomy_state), check=dict(HARD_CHECK))
    assert _mismatches(events) == []


def test_warning_cleanup_both_passes_produce_no_mismatch(monkeypatch):
    autonomy_state = _autonomy_state()
    live_state = _blocked_live_state()
    live_state.update(
        {"diagnostics": "warning: unused variable `h`", "goals": "no goals", "build_status": "ok"}
    )
    events = _wire(monkeypatch, live_state, cleanup_reason="unused variable `h`")
    warning_check = {
        "ok": True,
        "mode": "incremental_target",
        "command": "lean_interact check_target",
        "target": "demo",
        "output": "warning: unused variable `h`",
    }
    agent = _StubAgent(autonomy_state)
    for _ in range(2):
        agent._managed_pending_theorem_feedback = {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
        }
        _run_boundary(agent, check=dict(warning_check))
    assert _mismatches(events) == []


def test_clean_same_assignment_advance_produces_no_mismatch(monkeypatch):
    autonomy_state = _autonomy_state()
    events = _wire(monkeypatch, _clean_same_assignment_live_state())
    _run_boundary(
        _StubAgent(autonomy_state),
        check={
            "ok": True,
            "mode": "incremental_target",
            "command": "lean_interact check_target",
            "target": "demo",
            "output": "",
        },
    )
    assert _mismatches(events) == []


def test_live_state_probe_produces_no_mismatch(monkeypatch):
    events = _wire(monkeypatch, {})
    blocked = runner._same_queue_assignment_still_blocked(
        {"current_queue_assignment": {"target_symbol": "demo", "active_file": "Demo/Main.lean"}},
        _blocked_live_state(),
    )
    clean = runner._same_queue_assignment_still_blocked(
        {"current_queue_assignment": {"target_symbol": "demo", "active_file": "Demo/Main.lean"}},
        _clean_same_assignment_live_state(),
    )
    assert blocked is True
    assert clean is False
    assert _mismatches(events) == []


def test_synthetic_divergence_emits_exactly_one_mismatch_event(monkeypatch):
    autonomy_state = _autonomy_state()
    events = _wire(monkeypatch, _blocked_live_state())
    # Corrupt the legacy reification: claim the gate advanced when it blocked.
    monkeypatch.setattr(
        runner,
        "_shadow_legacy_outcome",
        lambda **kwargs: {**kwargs, "action": "advance_queue"},
    )
    _run_boundary(_StubAgent(autonomy_state), check=dict(HARD_CHECK))

    # The corrupted reifier also affects the boundary's internal live-state
    # probe; assert on the boundary-gate event specifically.
    boundary_mismatches = [(a, k) for a, k in _mismatches(events) if k.get("source") == "post_edit"]
    assert len(boundary_mismatches) == 1
    _args, kwargs = boundary_mismatches[0]
    assert kwargs["legacy"]["action"] == "advance_queue"
    assert kwargs["decided"]["action"] == "continue_same_theorem"
    assert "action" in kwargs["diverged_fields"]


def _wire_final_report(monkeypatch, *, incremental_ok, output=""):
    monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_SHADOW", "1")
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


def test_final_report_accept_produces_no_mismatch(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    events = _wire_final_report(monkeypatch, incremental_ok=True)
    autonomy_state = {
        "current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}
    }

    updated = runner._review_agent_final_report(_final_report_result(), autonomy_state)

    assert updated["manager_final_report_review"]["ok"] is True
    assert _mismatches(events) == []


def test_final_report_reject_produces_no_mismatch(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    events = _wire_final_report(
        monkeypatch, incremental_ok=False, output="error: declaration uses sorry"
    )
    autonomy_state = {
        "current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}
    }

    updated = runner._review_agent_final_report(_final_report_result(), autonomy_state)

    assert updated["manager_final_report_review"]["ok"] is False
    assert _mismatches(events) == []


def test_final_report_retry_exhausted_produces_no_mismatch(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    events = _wire_final_report(
        monkeypatch, incremental_ok=False, output="error: declaration uses sorry"
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "theorem demo : True := by\n  sorry",
        }
    }
    for index in range(runner.MANAGER_HARD_RETRY_LIMIT):
        runner._increment_manager_feedback_retry(
            autonomy_state,
            target_symbol="demo",
            active_file=str(active),
            kind="sorry",
            signature=f"seed-{index}",
        )

    updated = runner._review_agent_final_report(_final_report_result(), autonomy_state)

    assert updated["exit_reason"] == "manager_retry_exhausted"
    assert _mismatches(events) == []


def test_budget_exhaustion_produces_no_mismatch(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  exact False.elim ?bad\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_SHADOW", "1")
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner, "_manager_verify_queue_file", lambda path: {"ok": True, "command": "lake"}
    )
    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    live_state = {
        "target_symbol": "demo",
        "active_file": str(active),
        "active_file_label": "Demo.lean",
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "diagnostics": "error: unsolved goals",
        "goals": "⊢ False",
        "build_status": "error",
        "current_blocker": "error: unsolved goals",
    }
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: dict(live_state),
    )
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda state: state)
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "Assigned declaration slice (1-2):\ntheorem demo : True := by\n  sorry",
        }
    }

    _history, _updated, handled = runner._handle_api_step_budget_exhaustion(
        _StubAgent(autonomy_state),
        {"completed": False, "exit_reason": "max_iterations", "api_calls": 180},
        [{"role": "assistant", "content": "failed attempt"}],
        autonomy_state,
        live_state,
        cycle=3,
        phase="autonomous",
    )

    assert handled is True
    assert _mismatches(events) == []


def test_shadow_off_by_default(monkeypatch):
    autonomy_state = _autonomy_state()
    events = _wire(monkeypatch, _blocked_live_state())
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_SHADOW", raising=False)
    compare_calls: list[Any] = []
    monkeypatch.setattr(runner, "_shadow_compare", lambda **kwargs: compare_calls.append(kwargs))
    _run_boundary(_StubAgent(autonomy_state), check=dict(HARD_CHECK))
    assert compare_calls == []
    assert _mismatches(events) == []
