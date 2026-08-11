"""P1.4 tests: the mechanical budget breakpoint (flag-gated, additive)."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.queue_manager import QueueItem, TheoremKey, TheoremQueueManager


@pytest.fixture()
def breakpoint_enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_BUDGET_BREAKPOINT", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    return state_dir


def _events(monkeypatch) -> list[tuple[tuple, dict]]:
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    return events


def _autonomy_state(active_file: str) -> dict[str, Any]:
    return {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": active_file,
            "slice": "theorem demo : True := by\n  sorry",
        }
    }


def test_api_steps_accumulate_and_round_trip(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo"), active_file=str(active))
    key = TheoremKey.make("demo", str(active))

    assert mgr.add_api_steps_for(key, 40) == 40
    assert mgr.add_api_steps_for(key, 25) == 65
    assert mgr.add_api_steps_for(key, 0) == 65

    restored = TheoremQueueManager.from_autonomy_state(mgr.to_autonomy_state())
    assert restored.api_steps_for(key) == 65


def test_flag_off_records_effort_without_breakpoint_semantics(monkeypatch, tmp_path):
    monkeypatch.delenv("LEANFLOW_BUDGET_BREAKPOINT", raising=False)
    events = _events(monkeypatch)
    autonomy_state = _autonomy_state(str(tmp_path / "Main.lean"))

    tripped = runner._maybe_trigger_budget_breakpoint(
        {"api_calls": 10_000, "exit_reason": "max_iterations"},
        autonomy_state,
        {},
        phase="autonomous",
        exhausted=True,
    )

    assert tripped is False
    assert list(autonomy_state["theorem_api_steps"].values()) == [10_000]
    assert "budget_breakpoint" not in autonomy_state
    assert "consecutive_exhausted_assignments" not in autonomy_state
    assert events == []
    assert (
        runner._autonomous_stop_reason([], {}, {"budget_breakpoint": {"scope": "theorem"}})
        != "budget-breakpoint"
    )


def test_theorem_budget_trips_breakpoint_with_packet_and_report(
    breakpoint_enabled, monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_THEOREM_BUDGET_STEPS", "100")
    events = _events(monkeypatch)
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))

    first = runner._maybe_trigger_budget_breakpoint(
        {"api_calls": 60}, autonomy_state, {}, phase="autonomous"
    )
    assert first is False
    assert "budget_breakpoint" not in autonomy_state

    second = runner._maybe_trigger_budget_breakpoint(
        {"api_calls": 60}, autonomy_state, {}, cycle=4, phase="autonomous"
    )
    assert second is True
    armed = autonomy_state["budget_breakpoint"]
    assert armed["scope"] == "theorem"

    # Stop reason becomes first-priority budget-breakpoint.
    assert runner._autonomous_stop_reason([], {}, autonomy_state) == "budget-breakpoint"

    # N1 artifact chain: packet persisted, node blocked, documented report.
    summary = plan_state.load_summary()
    packet = summary["decision_packets"][0]
    assert packet["packet_id"] == armed["packet_id"]
    assert packet["scope"] == "theorem"
    assert packet["api_steps_used"] == 120
    assert packet["budget"] == 100
    assert packet["options"] == ["split", "plan", "negate", "park", "re-state", "abort"]
    node = plan_state.load_blueprint().node_by_id(packet["node_id"])
    assert node.status == "blocked"
    assert summary["final_report"]["status"] == "documented"
    assert any(args[0] == "budget-breakpoint" for args, _kwargs in events)

    # Idempotent once armed: no duplicate packets.
    runner._maybe_trigger_budget_breakpoint(
        {"api_calls": 60}, autonomy_state, {}, phase="autonomous"
    )
    assert len(plan_state.load_summary()["decision_packets"]) == 1


def test_zero_budget_disables_per_theorem_cap(breakpoint_enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_THEOREM_BUDGET_STEPS", "0")
    _events(monkeypatch)
    autonomy_state = _autonomy_state(str(tmp_path / "Main.lean"))

    tripped = runner._maybe_trigger_budget_breakpoint(
        {"api_calls": 10_000}, autonomy_state, {}, phase="autonomous"
    )

    assert tripped is False


def test_consecutive_exhaustions_trip_queue_breakpoint(breakpoint_enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_THEOREM_BUDGET_STEPS", "0")
    monkeypatch.setenv("LEANFLOW_QUEUE_BREAKPOINT_CONSECUTIVE", "2")
    _events(monkeypatch)
    autonomy_state = _autonomy_state(str(tmp_path / "Main.lean"))

    assert (
        runner._maybe_trigger_budget_breakpoint(
            {"api_calls": 5, "exit_reason": "max_iterations"},
            autonomy_state,
            {},
            phase="autonomous",
            exhausted=True,
        )
        is False
    )
    assert (
        runner._maybe_trigger_budget_breakpoint(
            {"api_calls": 5, "exit_reason": "manager_retry_exhausted"},
            autonomy_state,
            {},
            phase="autonomous",
        )
        is True
    )
    assert autonomy_state["budget_breakpoint"]["scope"] == "queue"


def test_gate_accept_resets_exhaustion_streak(breakpoint_enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_THEOREM_BUDGET_STEPS", "0")
    monkeypatch.setenv("LEANFLOW_QUEUE_BREAKPOINT_CONSECUTIVE", "2")
    _events(monkeypatch)
    autonomy_state = _autonomy_state(str(tmp_path / "Main.lean"))

    runner._maybe_trigger_budget_breakpoint(
        {"api_calls": 5}, autonomy_state, {}, phase="autonomous", exhausted=True
    )
    runner._maybe_trigger_budget_breakpoint(
        {"api_calls": 5, "manager_final_report_review": {"ok": True}},
        autonomy_state,
        {},
        phase="autonomous",
    )
    assert autonomy_state["consecutive_exhausted_assignments"] == 0

    tripped = runner._maybe_trigger_budget_breakpoint(
        {"api_calls": 5}, autonomy_state, {}, phase="autonomous", exhausted=True
    )
    assert tripped is False
