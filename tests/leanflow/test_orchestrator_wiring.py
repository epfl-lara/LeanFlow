"""Phase 4 (2/6) tests: orchestrator runner wiring — consult, apply, resume."""

from __future__ import annotations

import pytest

from leanflow_cli.native import route_execution as _leaf_route_execution


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    return state_dir


def test_legacy_same_timestamp_completion_fails_closed(tmp_path):
    """Second-granularity legacy evidence cannot prove post-selection order."""
    active = tmp_path / "Demo.lean"
    selected_at = "2026-07-18T08:00:00+00:00"
    selection = {
        "token": "legacy-token",
        "epoch": 4,
        "route": "decompose",
        "target_symbol": "demo",
        "active_file": str(active),
        "selected_at": selected_at,
    }
    event = {
        "timestamp": selected_at,
        "type": "decomposer",
        "details": {
            "target_symbol": "demo",
            "active_file": str(active),
            "ok": True,
            "placed": ["helper"],
        },
    }

    assert _leaf_route_execution.legacy_completion_from_activity(selection, [event]) is None
