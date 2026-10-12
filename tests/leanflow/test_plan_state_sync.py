"""P1.2 runner-side tests: per-cycle queue->graph sync + reconcile (dark)."""

from __future__ import annotations

import pytest

from leanflow_cli.workflows import resume_graph_reconciliation as _leaf_resume_graph_reconciliation


@pytest.fixture()
def plan_enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", "prove demo")
    return state_dir


def test_legacy_resume_reset_activity_correlation_is_exact():
    """Migration fallback requires the same node, run, and reset timestamp."""
    campaign = {
        "campaign_id": "campaign-one",
        "epoch": 7,
        "last_verified_graph_progress": {
            "node_ids": ["n-recovered"],
            "recorded_at": "2026-07-18T10:34:34+00:00",
        },
    }
    events = [
        {
            "type": "plan-graph-resume-gate-recovered",
            "timestamp": "2026-07-18T10:32:57+00:00",
            "run_id": "resume-run",
            "details": {"node_id": "n-recovered"},
        },
        {
            "type": "campaign-route-streak-reset",
            "timestamp": "2026-07-18T10:34:34+00:00",
            "run_id": "different-run",
            "details": {
                "campaign_id": "campaign-one",
                "epoch": 7,
                "node_ids": ["n-recovered"],
            },
        },
    ]

    assert _leaf_resume_graph_reconciliation.legacy_startup_reset_node_ids(campaign, events) == ()
    events[1]["run_id"] = "resume-run"
    assert _leaf_resume_graph_reconciliation.legacy_startup_reset_node_ids(campaign, events) == (
        "n-recovered",
    )


# ---------------------------------------------------------------------------
# Queue-drain outcome for the LAST theorem (the transition path never fires for
# it — nothing follows it — so its gate-backed 'solved' outcome is recorded on
# drain, then the ORDINARY gate-accept sync promotes it).
# ---------------------------------------------------------------------------
