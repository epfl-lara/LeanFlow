from __future__ import annotations

from leanflow_cli.workflows import helper_integration_pending as pending
from leanflow_cli.workflows import plan_state


def test_pending_helper_integration_is_bounded_and_durable(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))
    active = str(tmp_path / "Main.lean")
    state: dict = {}

    remembered = pending.remember(
        state,
        target_symbol="demo",
        active_file=active,
        helper_names=[f"helper_{index}" for index in range(pending.MAX_HELPERS + 5)],
    )

    assert remembered is not None
    assert len(remembered.helper_names) == pending.MAX_HELPERS
    assert remembered.gate_attempts == 0
    state.clear()
    hydrated = pending.load(state)
    assert hydrated == remembered

    for expected_attempt in range(1, pending.MAX_GATE_ATTEMPTS + 1):
        attempted = pending.note_gate_attempt(
            state,
            target_symbol="demo",
            active_file=active,
        )
        assert attempted is not None
        assert attempted.gate_attempts == expected_attempt
    assert attempted.exhausted is True
    assert attempted.matches("demo", active)
    assert not attempted.matches("other", active)

    refreshed = pending.remember(
        state,
        target_symbol="demo",
        active_file=active,
        helper_names=["new_helper"],
    )
    assert refreshed is not None
    assert refreshed.gate_attempts == 0
    assert "new_helper" in refreshed.helper_names

    assert pending.retire(state) == refreshed
    assert pending.STATE_KEY not in state
    assert plan_state.load_summary()[pending.SUMMARY_KEY] == {}


def test_pending_helper_integration_rejects_malformed_state(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "0")
    state = {
        pending.STATE_KEY: {
            "target_symbol": "demo",
            "active_file": str(tmp_path / "Main.lean"),
            "helper_names": "not-a-list",
            "gate_attempts": "invalid",
        }
    }

    assert pending.load(state) is None
    assert pending.STATE_KEY not in state
