"""P1.4 tests: the mechanical budget breakpoint (flag-gated, additive)."""

from __future__ import annotations

import pytest

from leanflow_cli.workflows.queue_manager import QueueItem, TheoremKey, TheoremQueueManager


@pytest.fixture()
def breakpoint_enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_BUDGET_BREAKPOINT", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    return state_dir


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
