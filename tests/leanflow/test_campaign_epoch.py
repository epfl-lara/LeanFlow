"""Campaign epoch persistence and fresh-context handoff tests."""

from __future__ import annotations

import json

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import campaign_epoch, negation_promotion
from leanflow_cli.workflows.workflow_json_io import update_json_file


def test_campaign_epoch_rollover_persists_and_preserves_negative_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-demo")
    state: dict[str, object] = {
        "continuation_stable_cycles": 4,
        "continuation_blocked_runs": 3,
        "orchestrator_routes_used": 4,
        "manager_nudge_seen": ["demo::3"],
    }

    started = campaign_epoch.ensure_campaign(state)
    assert started["campaign_id"] == "run-demo"
    assert started["epoch"] == 1
    assert (
        started["resume_graph_progress_policy_version"]
        == campaign_epoch.RESUME_GRAPH_PROGRESS_POLICY_VERSION
    )

    handoff = campaign_epoch.roll_epoch(
        state,
        reason="cycle-ceiling",
        cycle=120,
        target_symbol="demo",
        active_file="Main.lean",
        live_message="unsolved goals",
        failed_attempts=[
            {"proof_shape": "simp", "reason": "same type mismatch"},
            {"proof_shape": "omega", "reason": "nonlinear goal"},
        ],
    )

    assert state["campaign_epoch"] == 2
    assert state["continuation_stable_cycles"] == 0
    assert state["continuation_blocked_runs"] == 0
    assert state["orchestrator_routes_used"] == 0
    assert "manager_nudge_seen" not in state
    assert "mathematical status: unresolved" in handoff
    assert "simp: same type mismatch" in handoff
    assert "fresh epoch: 2" in handoff

    summary = json.loads(
        (tmp_path / ".leanflow" / "workflow-state" / "summary.json").read_text(encoding="utf-8")
    )
    campaign = summary["campaign"]
    assert campaign["epoch"] == 2
    assert campaign["status"] == "running"
    assert campaign["epoch_history"][-1]["reason"] == "cycle-ceiling"


def test_semantic_route_history_survives_rollover_and_resets_only_on_progress(
    monkeypatch,
    tmp_path,
):
    """No-progress semantic intent crosses epochs and kernel progress clears it."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-semantic-routes")
    active_file = str(tmp_path / "Main.lean")
    state: dict[str, object] = {}

    campaign_epoch.record_route_decision(
        state,
        route="plan",
        target_symbol="demo",
        active_file=active_file,
        route_reason="first planning pass",
        route_target={"target_hypothesis": "residues modulo 41"},
    )
    before = state[campaign_epoch.SEMANTIC_ROUTE_HISTORY_STATE_KEY]
    assert isinstance(before, list)
    assert before[0]["semantic_route_family"] == "plan"
    assert before[0]["semantic_target_hypothesis"] != "assignment-root"

    campaign_epoch.roll_epoch(
        state,
        reason="semantic-route-portfolio-exhausted",
        cycle=1,
        target_symbol="demo",
        active_file=active_file,
    )
    resumed: dict[str, object] = {}
    campaign_epoch.ensure_campaign(resumed)
    assert resumed[campaign_epoch.SEMANTIC_ROUTE_HISTORY_STATE_KEY] == before

    campaign_epoch.record_verified_graph_progress(resumed, node_ids=["helper.demo"])
    assert resumed[campaign_epoch.SEMANTIC_ROUTE_HISTORY_STATE_KEY] == []
    assert campaign_epoch.campaign_snapshot()[campaign_epoch.SEMANTIC_ROUTE_HISTORY_FIELD] == []

    campaign_epoch.record_route_decision(
        resumed,
        route="plan",
        target_symbol="demo",
        active_file=active_file,
        route_target={"target_hypothesis": "residues modulo 41"},
    )
    restarted = resumed[campaign_epoch.SEMANTIC_ROUTE_HISTORY_STATE_KEY]
    assert isinstance(restarted, list)
    assert len(restarted) == 1


def test_epoch_selection_supersession_is_exact_and_restart_durable(monkeypatch, tmp_path):
    """A newer source candidate may retire only its exact pending route."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "supersede-epoch-route")
    active_file = str(tmp_path / "Main.lean")
    state: dict[str, object] = {}
    campaign_epoch.roll_epoch(
        state,
        reason=campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        cycle=4,
        target_symbol="demo",
        active_file=active_file,
    )
    campaign_epoch.record_route_decision(
        state,
        route="negate",
        target_symbol="demo",
        active_file=active_file,
        trigger="scope-entry",
        route_reason="stale feasibility branch",
    )
    selection = dict(state[campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY])

    assert not campaign_epoch.supersede_epoch_refresh_selection(
        state,
        refresh_token=str(selection["token"]),
        epoch=int(selection["epoch"]),
        target_symbol="other",
        active_file=active_file,
        reason="wrong scope",
    )
    assert campaign_epoch.supersede_epoch_refresh_selection(
        state,
        refresh_token=str(selection["token"]),
        epoch=int(selection["epoch"]),
        target_symbol="demo",
        active_file=active_file,
        reason="new sorry-free source candidate",
    )
    assert campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in state
    assert campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY not in state

    resumed: dict[str, object] = {}
    campaign_epoch.ensure_campaign(resumed)
    assert campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in resumed
    assert campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY not in resumed
    refresh = campaign_epoch.campaign_snapshot()["epoch_route_refresh"]
    assert refresh["required"] is False
    assert refresh["superseded_route"] == "negate"
    assert refresh["superseded_reason"] == "new sorry-free source candidate"


def test_rollover_request_is_idempotent_and_consumed():
    state: dict[str, object] = {}
    campaign_epoch.request_rollover(state, "route-portfolio-exhausted")
    campaign_epoch.request_rollover(state, "later-reason")

    assert campaign_epoch.consume_rollover_request(state) == "route-portfolio-exhausted"
    assert campaign_epoch.consume_rollover_request(state) == ""


def test_reset_compaction_state_marks_fresh_epoch():
    state = campaign_epoch.reset_compaction_state()
    assert state["reason"] == "epoch-rollover"
    assert state["snapshot_text"] == ""
    assert state["compacted"] is False


def test_signal_interruption_pauses_and_fresh_runner_resumes_campaign(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    first_state: dict = {}
    campaign_epoch.ensure_campaign(first_state)
    campaign_epoch.record_status(first_state, "paused", reason="signal interrupt")
    campaign_epoch.record_process_exit(
        first_state,
        130,
        verified=False,
        reason="signal interrupt",
    )
    interrupted = campaign_epoch.campaign_snapshot()

    assert interrupted["status"] == "paused"
    assert interrupted["last_exit_code"] == 130

    resumed_state: dict = {}
    resumed = campaign_epoch.ensure_campaign(resumed_state)

    assert resumed["status"] == "running"
    assert "status_reason" not in resumed
    assert resumed["last_exit_reason"] == "signal interrupt"
    assert resumed_state["campaign_id"] == first_state["campaign_id"]


def test_usage_limit_pause_blocks_resume_until_reset_then_clears_its_own_pause(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "usage-limit-resume")
    now = 1_700_000_000
    retry_after = {
        "kind": "usage_limit_reached",
        "retry_after_seconds": 601,
        "unavailable_until_epoch": now + 601,
        "resets_at_epoch": now + 600,
        "reported_resets_in_seconds": 600,
        "timing_consistent": True,
        "timing_clamped": False,
        "source": "exception.body",
    }
    first_state: dict = {}
    campaign_epoch.ensure_campaign(first_state)
    campaign_epoch.record_provider_usage_limit_pause(
        first_state,
        retry_after,
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        now_epoch=now,
    )

    monkeypatch.setattr(campaign_epoch.time, "time", lambda: now + 300)
    early_state: dict = {}
    early = campaign_epoch.ensure_campaign(early_state)

    assert early["status"] == "paused_infrastructure"
    assert early_state["operational_pause"] == "paused_infrastructure"
    assert early_state["provider_pause_owner"] == "provider_usage_limit"
    assert early_state["provider_retry_after"]["unavailable_until_epoch"] == now + 601

    monkeypatch.setattr(campaign_epoch.time, "time", lambda: now + 601)
    recovered_state: dict = {}
    recovered = campaign_epoch.ensure_campaign(recovered_state)

    assert recovered["status"] == "running"
    assert campaign_epoch.PROVIDER_USAGE_LIMIT_PAUSE_FIELD not in recovered
    assert "operational_pause" not in recovered_state
    assert "provider_retry_after" not in recovered_state


def test_successful_provider_probe_clears_matching_usage_pause_early(monkeypatch, tmp_path):
    """A fresh authenticated turn may recover before the reported reset epoch."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "usage-limit-probe-recovery")
    now = 1_700_000_000
    state: dict = {}
    campaign_epoch.ensure_campaign(state)
    campaign_epoch.record_provider_usage_limit_pause(
        state,
        {
            "kind": "usage_limit_reached",
            "retry_after_seconds": 3600,
            "unavailable_until_epoch": now + 3600,
        },
        provider="openai-codex",
        now_epoch=now,
    )

    assert not campaign_epoch.record_provider_availability_probe_success(
        state,
        provider="openrouter",
    )
    assert campaign_epoch.record_provider_availability_probe_success(
        state,
        provider="openai-codex",
    )

    snapshot = campaign_epoch.campaign_snapshot()
    assert snapshot["status"] == "running"
    assert campaign_epoch.PROVIDER_USAGE_LIMIT_PAUSE_FIELD not in snapshot
    assert "operational_pause" not in state
    assert "provider_retry_after" not in state
    assert not campaign_epoch.record_provider_availability_probe_success(
        state,
        provider="openai-codex",
    )


def test_usage_limit_pause_keeps_latest_deadline_across_worker_order(monkeypatch, tmp_path):
    """A stale worker result cannot shorten a later account reset."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "usage-limit-merge")
    now = 1_700_000_000
    state: dict = {}
    campaign_epoch.ensure_campaign(state)

    def metadata(delay: int) -> dict:
        return {
            "kind": "usage_limit_reached",
            "retry_after_seconds": delay,
            "unavailable_until_epoch": now + delay,
        }

    campaign_epoch.record_provider_usage_limit_pause(
        state,
        metadata(900),
        provider="openai-codex",
        now_epoch=now,
    )
    campaign_epoch.record_provider_usage_limit_pause(
        state,
        metadata(60),
        provider="openai-codex",
        now_epoch=now,
    )

    snapshot = campaign_epoch.campaign_snapshot()
    pause = snapshot[campaign_epoch.PROVIDER_USAGE_LIMIT_PAUSE_FIELD]
    assert pause["unavailable_until_epoch"] == now + 900
    assert state["provider_retry_after"]["unavailable_until_epoch"] == now + 900


def test_malformed_usage_limit_marker_hydrates_fail_closed_pause(monkeypatch, tmp_path):
    """Corrupt reset state cannot silently reopen provider admission."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "usage-limit-malformed")
    state: dict = {}
    campaign_epoch.ensure_campaign(state)

    def corrupt(summary):
        campaign = dict(summary["campaign"])
        campaign[campaign_epoch.PROVIDER_USAGE_LIMIT_PAUSE_FIELD] = {
            "kind": "usage_limit_reached",
            "unavailable_until_epoch": "not-an-epoch",
        }
        campaign["status"] = "paused_infrastructure"
        summary["campaign"] = campaign

    update_json_file(campaign_epoch._summary_path(), corrupt)
    resumed_state: dict = {}
    resumed = campaign_epoch.ensure_campaign(resumed_state)

    assert resumed["status"] == "paused_infrastructure"
    assert resumed_state["operational_pause"] == "paused_infrastructure"
    assert resumed_state["provider_pause_owner"] == "provider_usage_limit"
    assert resumed_state["provider_retry_after"] == {}


def test_usage_limit_expiry_restores_unrelated_infrastructure_pause(monkeypatch, tmp_path):
    """Reset expiry clears only the usage-limit-owned admission pause."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "usage-limit-prior-pause")
    now = 1_700_000_000
    state: dict = {}
    campaign_epoch.ensure_campaign(state)
    campaign_epoch.record_status(
        state,
        "paused_infrastructure",
        reason="local Lean service needs manual repair",
    )
    provider_state: dict = {}
    campaign_epoch.record_provider_usage_limit_pause(
        provider_state,
        {
            "kind": "usage_limit_reached",
            "retry_after_seconds": 60,
            "unavailable_until_epoch": now + 60,
        },
        provider="openai-codex",
        now_epoch=now,
    )

    monkeypatch.setattr(campaign_epoch.time, "time", lambda: now + 60)
    resumed_state: dict = {}
    resumed = campaign_epoch.ensure_campaign(resumed_state)

    assert resumed["status"] == "paused_infrastructure"
    assert resumed_state["operational_pause"] == "paused_infrastructure"
    assert resumed_state["infrastructure_pause_reason"] == (
        "local Lean service needs manual repair"
    )
    assert "provider_pause_owner" not in resumed_state


def test_usage_limit_extension_preserves_unrelated_pause_for_expiry(monkeypatch, tmp_path):
    """Extending a reset window must retain the pause that preceded it."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "usage-limit-prior-pause-extension")
    now = 1_700_000_000
    state: dict = {}
    campaign_epoch.ensure_campaign(state)
    campaign_epoch.record_status(
        state,
        "paused_infrastructure",
        reason="local Lean service needs manual repair",
    )
    campaign_epoch.record_provider_usage_limit_pause(
        state,
        {
            "kind": "usage_limit_reached",
            "retry_after_seconds": 60,
            "unavailable_until_epoch": now + 60,
        },
        provider="openai-codex",
        now_epoch=now,
    )
    campaign_epoch.record_provider_usage_limit_pause(
        state,
        {
            "kind": "usage_limit_reached",
            "retry_after_seconds": 120,
            "unavailable_until_epoch": now + 120,
        },
        provider="openai-codex",
        now_epoch=now,
    )

    monkeypatch.setattr(campaign_epoch.time, "time", lambda: now + 120)
    resumed_state: dict = {}
    resumed = campaign_epoch.ensure_campaign(resumed_state)

    assert resumed["status"] == "paused_infrastructure"
    assert resumed_state["operational_pause"] == "paused_infrastructure"
    assert resumed_state["infrastructure_pause_reason"] == (
        "local Lean service needs manual repair"
    )
    assert "provider_pause_owner" not in resumed_state


def test_planner_capacity_reservation_is_scope_bound_and_epoch_bound(monkeypatch, tmp_path):
    """A resumed marker survives a crash but not target or epoch transitions."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "planner-capacity-scope")
    first_state: dict = {}
    campaign_epoch.ensure_campaign(first_state)
    reservation = campaign_epoch.reserve_planner_capacity(
        first_state,
        target_symbol="demo",
        active_file="Main.lean",
        reason="capacity deferred",
    )

    checkpoint_restored_state: dict = {
        "campaign_id": first_state["campaign_id"],
        "campaign_epoch": 1,
        "campaign_status": "running",
    }
    campaign_epoch.ensure_campaign(checkpoint_restored_state)
    assert (
        checkpoint_restored_state[campaign_epoch.PLANNER_CAPACITY_RESERVATION_STATE_KEY]
        == reservation
    )

    resumed_state: dict = {}
    campaign_epoch.ensure_campaign(resumed_state)

    assert resumed_state[campaign_epoch.PLANNER_CAPACITY_RESERVATION_STATE_KEY] == reservation
    assert resumed_state["prover_requested_route"] == {
        "route": "plan",
        "target_symbol": "demo",
        "active_file": "Main.lean",
        "reason": "capacity deferred",
    }

    assert (
        campaign_epoch.reconcile_planner_capacity_reservation(
            resumed_state,
            target_symbol="next_demo",
            active_file="Main.lean",
        )
        == {}
    )
    assert campaign_epoch.PLANNER_CAPACITY_RESERVATION_FIELD not in (
        campaign_epoch.campaign_snapshot()
    )
    assert "prover_requested_route" not in resumed_state

    campaign_epoch.reserve_planner_capacity(
        resumed_state,
        target_symbol="next_demo",
        active_file="Main.lean",
        reason="capacity deferred again",
    )
    campaign_epoch.roll_epoch(
        resumed_state,
        reason="cycle-ceiling",
        cycle=120,
        target_symbol="next_demo",
        active_file="Main.lean",
    )

    assert campaign_epoch.PLANNER_CAPACITY_RESERVATION_FIELD not in (
        campaign_epoch.campaign_snapshot()
    )
    assert campaign_epoch.PLANNER_CAPACITY_RESERVATION_STATE_KEY not in resumed_state
    assert "prover_requested_route" not in resumed_state


def test_terminal_campaign_status_survives_startup_until_truth_revalidation(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "terminal-resume")
    first_state: dict = {}
    campaign_epoch.ensure_campaign(first_state)

    for terminal_status in ("disproved", "verified"):
        campaign_epoch.record_status(first_state, terminal_status, reason="kernel evidence")
        resumed_state: dict = {}
        resumed = campaign_epoch.ensure_campaign(resumed_state)

        assert resumed["status"] == terminal_status
        assert resumed_state["campaign_status"] == terminal_status

        campaign_epoch.record_status(first_state, "running", reason="test next status")


def test_route_streak_survives_same_campaign_resume(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-resume-routes")
    first_state: dict = {}

    for route in ("direct-prove", "plan", "decompose"):
        campaign_epoch.record_route_decision(first_state, route=route)

    resumed_state: dict = {}
    resumed = campaign_epoch.ensure_campaign(resumed_state)

    assert resumed["campaign_id"] == first_state["campaign_id"]
    assert resumed["no_progress_route_streak"] == 3
    assert resumed_state["orchestrator_routes_used"] == 3
    assert "campaign_epoch_requested" not in resumed_state


def test_resume_repairs_legacy_repeated_mechanism_route_reset(monkeypatch, tmp_path):
    """A stale same-mechanism reset cannot cancel an already-due epoch rollover."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "legacy-repeat-reset")
    initial_state: dict = {}
    campaign_epoch.ensure_campaign(initial_state)

    def seed_legacy(summary):
        campaign = dict(summary["campaign"])
        campaign.pop("mechanism_route_progress_policy_version", None)
        campaign["no_progress_route_streak"] = 0
        campaign["epoch_routes"] = [
            {"route": route, "decided_at": f"2026-07-17T09:0{index}:00+00:00"}
            for index, route in enumerate(("plan", "decompose", "negate", "plan"), start=1)
        ]
        campaign["last_verified_graph_progress"] = {
            "accounting": "parent-scoped-proof-mechanism",
            "node_ids": ["repeat-node"],
            "recorded_at": "2026-07-17T09:06:00+00:00",
        }
        campaign["verified_mechanisms"] = {
            "version": 1,
            "entries": {
                "parent:mechanism": {
                    "first_node_id": "first-node",
                    "seen_node_ids": ["first-node", "repeat-node"],
                }
            },
        }
        summary["campaign"] = campaign

    update_json_file(campaign_epoch._summary_path(), seed_legacy)

    resumed_state: dict = {}
    resumed = campaign_epoch.ensure_campaign(resumed_state)

    assert resumed["no_progress_route_streak"] == 4
    assert resumed_state["orchestrator_routes_used"] == 4
    assert resumed_state["campaign_epoch_requested"] == "route-no-graph-progress"
    reconciliation = resumed["mechanism_progress_policy_reconciliation"]
    assert reconciliation["reason"] == "legacy-repeated-mechanism-reset-ignored"
    assert reconciliation["previous_streak"] == 0
    assert reconciliation["repaired_streak"] == 4


def test_resume_reconciles_historical_conditional_helper_false_progress(monkeypatch, tmp_path):
    """A proved conditional fact stays proved but cannot retain a false reset."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "conditional-progress-resume")
    state: dict = {}
    campaign_epoch.ensure_campaign(state)

    def seed_historical_false_progress(summary):
        campaign = dict(summary["campaign"])
        campaign["no_progress_route_streak"] = 0
        campaign["no_progress_route_limit"] = 4
        campaign["epoch_routes"] = [
            {
                "route": route,
                "decided_at": f"2026-07-17T13:0{index}:00+00:00",
            }
            for index, route in enumerate(("plan", "decompose", "negate", "plan"), start=1)
        ]
        campaign["last_verified_graph_progress"] = {
            "accounting": "parent-scoped-proof-mechanism",
            "node_ids": ["conditional-node"],
            "recorded_at": "2026-07-17T13:05:00+00:00",
        }
        campaign["verified_mechanisms"] = {
            "version": 1,
            "entries": {
                "parent:bridge": {
                    "first_node_id": "conditional-node",
                    "last_node_id": "conditional-node",
                    "seen_node_ids": ["conditional-node"],
                    "seen_count": 1,
                }
            },
        }
        summary["campaign"] = campaign

    update_json_file(campaign_epoch._summary_path(), seed_historical_false_progress)

    reconciled = campaign_epoch.reconcile_conditional_helper_progress(
        state,
        deferred_node_ids=["conditional-node"],
    )

    assert reconciled.newly_deferred_node_ids == ("conditional-node",)
    assert reconciled.removed_ledger_node_ids == ("conditional-node",)
    assert reconciled.previous_streak == 0
    assert reconciled.repaired_streak == 4
    campaign = campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 4
    assert "last_verified_graph_progress" not in campaign
    assert campaign["verified_mechanisms"]["entries"] == {}
    assert campaign["conditional_helper_progress"]["deferred_node_ids"] == ["conditional-node"]
    assert state["orchestrator_routes_used"] == 4
    assert state["campaign_epoch_requested"] == "route-no-graph-progress"

    unchanged = campaign_epoch.reconcile_conditional_helper_progress(
        state,
        deferred_node_ids=["conditional-node"],
    )
    assert unchanged.newly_deferred_node_ids == ()
    assert unchanged.released_node_ids == ()

    released = campaign_epoch.reconcile_conditional_helper_progress(
        state,
        deferred_node_ids=[],
    )
    assert released.released_node_ids == ("conditional-node",)


def test_finite_branch_reconciliation_uses_latest_genuine_reset(monkeypatch, tmp_path):
    """Every historical false reset is ignored after the latest real progress."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "finite-branch-reset-history")
    state: dict = {}
    campaign_epoch.ensure_campaign(state)

    def seed_historical_false_progress(summary):
        campaign = dict(summary["campaign"])
        campaign.pop("finite_branch_progress_policy_version", None)
        campaign["no_progress_route_streak"] = 0
        campaign["no_progress_route_limit"] = 4
        campaign["epoch_routes"] = [
            {"route": "plan", "decided_at": "2026-07-17T13:01:00+00:00"},
            {"route": "decompose", "decided_at": "2026-07-17T13:03:00+00:00"},
            {"route": "plan", "decided_at": "2026-07-17T13:05:00+00:00"},
            {"route": "negate", "decided_at": "2026-07-17T13:07:00+00:00"},
        ]
        campaign["last_verified_graph_progress"] = {
            "accounting": "parent-scoped-proof-mechanism",
            "node_ids": ["finite-node-2"],
            "recorded_at": "2026-07-17T13:06:00+00:00",
        }
        summary["campaign"] = campaign

    update_json_file(campaign_epoch._summary_path(), seed_historical_false_progress)
    monkeypatch.setattr(
        campaign_epoch,
        "read_workflow_activity",
        lambda **_kwargs: [
            {
                "type": "campaign-route-streak-reset",
                "timestamp": "2026-07-17T13:02:00+00:00",
                "details": {
                    "campaign_id": "finite-branch-reset-history",
                    "epoch": 1,
                    "node_ids": ["genuine-node"],
                },
            },
            {
                "type": "campaign-route-streak-reset",
                "timestamp": "2026-07-17T13:04:00+00:00",
                "details": {
                    "campaign_id": "finite-branch-reset-history",
                    "epoch": 1,
                    "node_ids": ["finite-node-1"],
                },
            },
            {
                "type": "campaign-route-streak-reset",
                "timestamp": "2026-07-17T13:06:00+00:00",
                "details": {
                    "campaign_id": "finite-branch-reset-history",
                    "epoch": 1,
                    "node_ids": ["finite-node-2"],
                },
            },
        ],
    )

    reconciled = campaign_epoch.reconcile_finite_branch_progress(
        state,
        evidence_node_ids=["finite-node-1", "finite-node-2"],
    )

    assert reconciled.reconstructed_streak == 3
    assert reconciled.repaired_streak == 3
    assert reconciled.rollover_required is False
    assert campaign_epoch.campaign_snapshot()["no_progress_route_streak"] == 3
    assert "campaign_epoch_requested" not in state


def test_finite_branch_reconciliation_preserves_genuine_progress_anchor(monkeypatch, tmp_path):
    """Historical evidence cannot disturb a genuinely eligible latest anchor."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "finite-branch-genuine-anchor")
    state: dict = {}
    campaign_epoch.ensure_campaign(state)
    genuine_anchor = {
        "accounting": "parent-scoped-proof-mechanism",
        "node_ids": ["genuine-node"],
        "recorded_at": "2026-07-17T14:06:00+00:00",
        "extra": {"preserve": True},
    }

    def seed_genuine_progress(summary):
        campaign = dict(summary["campaign"])
        campaign.pop("finite_branch_progress_policy_version", None)
        campaign["no_progress_route_streak"] = 2
        campaign["epoch_routes"] = [
            {"route": "plan", "decided_at": "2026-07-17T14:07:00+00:00"},
            {"route": "decompose", "decided_at": "2026-07-17T14:08:00+00:00"},
        ]
        campaign["last_verified_graph_progress"] = genuine_anchor
        summary["campaign"] = campaign

    update_json_file(campaign_epoch._summary_path(), seed_genuine_progress)

    reconciled = campaign_epoch.reconcile_finite_branch_progress(
        state,
        evidence_node_ids=["finite-node-1", "finite-node-2"],
    )

    campaign = campaign_epoch.campaign_snapshot()
    assert reconciled.changed is False
    assert reconciled.repaired_streak == 2
    assert campaign["no_progress_route_streak"] == 2
    assert campaign["last_verified_graph_progress"] == genuine_anchor
    assert "campaign_epoch_requested" not in state


def test_resume_graph_progress_reconciliation_repairs_legacy_startup_reset(monkeypatch, tmp_path):
    """The live pre-policy resume reset is repaired on the next startup."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "legacy-resume-progress-reset")
    state: dict = {}
    campaign_epoch.ensure_campaign(state)
    recovered_node_id = "n-recovered-before-process-start"

    def seed_legacy_resume_reset(summary):
        campaign = dict(summary["campaign"])
        campaign.pop("resume_graph_progress_policy_version", None)
        campaign["no_progress_route_streak"] = 1
        campaign["no_progress_route_limit"] = 4
        campaign["epoch_routes"] = [
            {"route": route, "decided_at": decided_at}
            for route, decided_at in (
                ("decompose", "2026-07-18T10:02:00+00:00"),
                ("plan", "2026-07-18T10:03:00+00:00"),
                ("negate", "2026-07-18T10:04:00+00:00"),
                ("plan", "2026-07-18T10:05:00+00:00"),
                ("plan", "2026-07-18T10:35:00+00:00"),
            )
        ]
        campaign["finite_branch_progress_reconciliation"] = {
            "repaired_streak": 4,
            "reconciled_at": "2026-07-18T10:32:58+00:00",
        }
        campaign["last_verified_graph_progress"] = {
            "accounting": "parent-scoped-proof-mechanism",
            "node_ids": [recovered_node_id],
            "recorded_at": "2026-07-18T10:34:34+00:00",
        }
        summary["campaign"] = campaign

    update_json_file(campaign_epoch._summary_path(), seed_legacy_resume_reset)
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        campaign_epoch,
        "append_workflow_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(campaign_epoch, "read_workflow_activity", lambda **_kwargs: [])

    reconciliation = campaign_epoch.reconcile_resume_graph_progress(
        state,
        recovered_node_ids=[recovered_node_id],
    )

    assert reconciliation.changed is True
    assert reconciliation.previous_streak == 1
    assert reconciliation.reconstructed_streak == 5
    assert reconciliation.repaired_streak == 4
    campaign = campaign_epoch.campaign_snapshot()
    assert campaign["resume_graph_progress_policy_version"] == 1
    assert campaign["no_progress_route_streak"] == 4
    assert "last_verified_graph_progress" not in campaign
    assert state["orchestrator_routes_used"] == 4
    assert state["campaign_epoch_requested"] == "route-no-graph-progress"
    assert [args[0] for args, _kwargs in events] == ["campaign-resume-graph-progress-reconciled"]

    repeated = campaign_epoch.reconcile_resume_graph_progress(
        state,
        recovered_node_ids=[recovered_node_id],
    )
    assert repeated.changed is False
    assert repeated.repaired_streak == 4
    assert [args[0] for args, _kwargs in events] == ["campaign-resume-graph-progress-reconciled"]


def _seed_legacy_infrastructure_route_replay() -> None:
    """Seed the exact old-schema route replay observed in the live campaign."""

    def seed(summary):
        campaign = dict(summary["campaign"])
        campaign.pop("epoch_route_replay_policy_version", None)
        campaign["epoch"] = 13
        campaign["epoch_cycles"] = 3
        campaign["no_progress_route_streak"] = 4
        campaign["no_progress_route_limit"] = 4
        campaign["last_verified_graph_progress"] = {
            "node_ids": ["older-helper"],
            "recorded_at": "2026-07-17T09:36:16+00:00",
        }
        campaign["epoch_route_refresh"] = {
            "required": False,
            "token": "refresh-token",
            "previous_epoch": 12,
            "new_epoch": 13,
            "reason": "route-no-graph-progress",
            "previous_routes": ["plan", "plan", "negate", "decompose"],
            "requested_at": "2026-07-17T09:51:00+00:00",
            "selected_route": "negate",
            "started_at": "2026-07-17T10:21:29+00:00",
        }
        campaign["epoch_routes"] = [
            {
                "route": "negate",
                "target_symbol": "demo",
                "active_file": "/tmp/Main.lean",
                "decided_at": "2026-07-17T09:51:07+00:00",
            },
            {
                "route": "negate",
                "target_symbol": "demo",
                "active_file": "/tmp/Main.lean",
                "decided_at": "2026-07-17T10:18:02+00:00",
            },
            {
                "route": "decompose",
                "target_symbol": "demo",
                "active_file": "/tmp/Main.lean",
                "decided_at": "2026-07-17T10:21:36+00:00",
            },
            {
                "route": "negate",
                "target_symbol": "demo",
                "active_file": "/tmp/Main.lean",
                "decided_at": "2026-07-17T10:23:45+00:00",
            },
        ]
        campaign["last_route_decision"] = dict(campaign["epoch_routes"][-1])
        summary["campaign"] = campaign

    update_json_file(campaign_epoch._summary_path(), seed)


def _legacy_route_replay_events(*, second_trigger: str = "scope-entry", provider_pause=True):
    events = [
        {
            "type": "orchestrator-route",
            "timestamp": "2026-07-17T10:18:02+00:00",
            "run_id": "resume-run",
            "details": {
                "trigger": second_trigger,
                "route": "negate",
                "target_symbol": "demo",
                "active_file": "/tmp/Main.lean",
                "routes_used": 2,
            },
        }
    ]
    if provider_pause:
        events[0:0] = [
            {
                "type": "managed-conversation-failed",
                "timestamp": "2026-07-17T09:55:02+00:00",
                "run_id": "failed-run",
                "message": "Managed conversation failed: APIError: overloaded",
                "details": {},
            },
            {
                "type": "runner-exit",
                "timestamp": "2026-07-17T09:55:06+00:00",
                "run_id": "failed-run",
                "message": "Paused after provider/API failure",
                "details": {
                    "exit_code": 2,
                    "reason": "startup provider/API failure",
                },
            },
        ]
    return events


def test_resume_reconciles_legacy_scope_route_replayed_after_provider_pause(monkeypatch, tmp_path):
    """Remove only the duplicate pre-start refresh route from old state."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "legacy-route-replay")
    campaign_epoch.ensure_campaign({})
    _seed_legacy_infrastructure_route_replay()
    monkeypatch.setattr(
        campaign_epoch,
        "_route_replay_reconciliation_events",
        lambda: _legacy_route_replay_events(),
    )
    activities: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        campaign_epoch,
        "append_workflow_activity",
        lambda event, _message, **details: activities.append((event, details)),
    )

    resumed_state: dict = {}
    resumed = campaign_epoch.ensure_campaign(resumed_state)

    assert resumed["epoch_route_replay_policy_version"] == 1
    assert resumed["epoch_cycles"] == 3
    assert resumed["no_progress_route_streak"] == 3
    assert resumed_state["orchestrator_routes_used"] == 3
    assert [entry["route"] for entry in resumed["epoch_routes"]] == [
        "negate",
        "decompose",
        "negate",
    ]
    assert resumed["last_route_decision"]["decided_at"] == "2026-07-17T10:23:45+00:00"
    reconciliation = resumed["epoch_route_replay_reconciliation"]
    assert reconciliation["reason"] == "legacy-infrastructure-scope-entry-replay"
    assert reconciliation["previous_streak"] == 4
    assert reconciliation["repaired_streak"] == 3
    assert reconciliation["removed_decisions"] == ["2026-07-17T10:18:02+00:00"]
    assert [event for event, _details in activities] == ["campaign-route-replay-reconciled"]

    replayed = campaign_epoch.ensure_campaign({})
    assert replayed["no_progress_route_streak"] == 3
    assert len(replayed["epoch_routes"]) == 3
    assert [event for event, _details in activities] == ["campaign-route-replay-reconciled"]


@pytest.mark.parametrize(
    ("second_trigger", "provider_pause"),
    (("event", True), ("scope-entry", False)),
)
def test_resume_does_not_reconcile_legitimate_or_unproven_legacy_route_repetition(
    monkeypatch, tmp_path, second_trigger, provider_pause
):
    """A repeated route needs both scope-entry and provider-pause evidence."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "legacy-route-no-repair")
    campaign_epoch.ensure_campaign({})
    _seed_legacy_infrastructure_route_replay()
    monkeypatch.setattr(
        campaign_epoch,
        "_route_replay_reconciliation_events",
        lambda: _legacy_route_replay_events(
            second_trigger=second_trigger,
            provider_pause=provider_pause,
        ),
    )

    resumed = campaign_epoch.ensure_campaign({})

    assert resumed["epoch_route_replay_policy_version"] == 1
    assert resumed["no_progress_route_streak"] == 4
    assert len(resumed["epoch_routes"]) == 4
    assert "epoch_route_replay_reconciliation" not in resumed


def test_resume_does_not_reconcile_replay_after_route_streak_was_reset(monkeypatch, tmp_path):
    """Do not subtract old routes when later graph progress changed the streak."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "legacy-route-reset")
    campaign_epoch.ensure_campaign({})
    _seed_legacy_infrastructure_route_replay()

    def reset_streak(summary):
        campaign = dict(summary["campaign"])
        campaign["no_progress_route_streak"] = 2
        campaign["last_verified_graph_progress"] = {
            "node_ids": ["new-helper"],
            "recorded_at": "2026-07-17T10:20:00+00:00",
        }
        summary["campaign"] = campaign

    update_json_file(campaign_epoch._summary_path(), reset_streak)
    monkeypatch.setattr(
        campaign_epoch,
        "_route_replay_reconciliation_events",
        lambda: _legacy_route_replay_events(),
    )

    resumed = campaign_epoch.ensure_campaign({})

    assert resumed["no_progress_route_streak"] == 2
    assert len(resumed["epoch_routes"]) == 4
    assert "epoch_route_replay_reconciliation" not in resumed


def test_managed_cycle_count_survives_resume_and_resets_only_at_rollover(monkeypatch, tmp_path):
    """A fresh process must resume the same epoch's cumulative turn count."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-resume-cycles")
    first_state: dict = {}

    assert campaign_epoch.record_managed_cycle(first_state) == 1
    assert campaign_epoch.record_managed_cycle(first_state) == 2
    assert campaign_epoch.record_managed_cycle(first_state) == 3

    resumed_state: dict = {}
    resumed = campaign_epoch.ensure_campaign(resumed_state)

    assert resumed["campaign_id"] == first_state["campaign_id"]
    assert resumed["epoch_cycles"] == 3
    assert campaign_epoch.managed_cycle_count(resumed_state) == 3
    assert campaign_epoch.record_managed_cycle(resumed_state) == 4

    # Startup route rollovers historically supplied zero and erased the true
    # cross-process count from epoch history. Durable epoch cycles outrank that
    # process-local compatibility argument.
    campaign_epoch.roll_epoch(
        resumed_state,
        reason="route-no-graph-progress",
        cycle=0,
    )

    campaign = campaign_epoch.campaign_snapshot()
    assert campaign["epoch_history"][-1]["cycles"] == 4
    assert campaign["epoch_cycles"] == 0
    assert campaign_epoch.managed_cycle_count(resumed_state) == 0


def test_provider_turn_nonce_is_campaign_monotonic_across_resume_and_epoch_rollover(
    monkeypatch, tmp_path
):
    """A repeated local cycle number must still identify a new provider turn."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-provider-turns")
    first_state: dict = {}

    first = campaign_epoch.reserve_provider_turn(first_state)
    second = campaign_epoch.reserve_provider_turn(first_state)

    resumed_state: dict = {}
    campaign_epoch.ensure_campaign(resumed_state)
    third = campaign_epoch.reserve_provider_turn(resumed_state)
    campaign_epoch.roll_epoch(resumed_state, reason="cycle-ceiling", cycle=1)
    fourth = campaign_epoch.reserve_provider_turn(resumed_state)

    assert [first["nonce"], second["nonce"], third["nonce"], fourth["nonce"]] == [
        1,
        2,
        3,
        4,
    ]
    assert first["epoch"] == third["epoch"] == 1
    assert fourth["epoch"] == 2
    assert fourth["campaign_id"] == "run-provider-turns"


def test_fresh_authoritative_campaign_blocks_nonce_until_root_registry_is_sealed(
    monkeypatch, tmp_path
):
    """Campaign creation and its provider-blocking root marker are atomic."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "fresh-root-gate")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    state: dict = {}

    campaign = campaign_epoch.ensure_campaign(state)

    assert campaign[campaign_epoch.NEGATION_PROMOTION_ROOT_REGISTRATION_OPEN_FIELD] is True
    with pytest.raises(campaign_epoch.CampaignRootProviderBlocked, match="not registered"):
        campaign_epoch.reserve_provider_turn(state)
    assert campaign_epoch.campaign_snapshot()["provider_turn_nonce"] == 0

    registered = negation_promotion.record_requested_campaign_roots(
        [],
        campaign_id="fresh-root-gate",
        cwd=str(tmp_path),
    )
    assert registered.ok is True
    assert campaign_epoch.reserve_provider_turn(state)["nonce"] == 1


def test_ordinary_prove_without_authoritative_negation_never_opens_root_gate(monkeypatch, tmp_path):
    """Disabled plan/negation features cannot deadlock an ordinary prover turn."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "ordinary-prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)
    monkeypatch.delenv("LEANFLOW_NEGATION_PROBE", raising=False)
    state: dict = {}

    campaign = campaign_epoch.ensure_campaign(state)

    assert campaign_epoch.NEGATION_PROMOTION_ROOT_REGISTRATION_OPEN_FIELD not in campaign
    assert campaign_epoch.reserve_provider_turn(state)["nonce"] == 1


def test_marker_absent_legacy_campaign_is_not_retroactively_gated(monkeypatch, tmp_path):
    """Enabling research on resume cannot infer roots from a legacy current queue."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "legacy-rootless")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)
    monkeypatch.delenv("LEANFLOW_NEGATION_PROBE", raising=False)
    first: dict = {}
    campaign_epoch.ensure_campaign(first)
    campaign_epoch.reserve_provider_turn(first)

    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    resumed: dict = {}
    campaign = campaign_epoch.ensure_campaign(resumed)

    assert campaign_epoch.NEGATION_PROMOTION_ROOT_REGISTRATION_OPEN_FIELD not in campaign
    assert campaign_epoch.reserve_provider_turn(resumed)["nonce"] == 2


def test_resume_never_backfills_nonce_into_marker_bound_registry(monkeypatch, tmp_path):
    """Resume cannot synthesize the fresh-origin evidence used by root authority."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")
    roots: list[dict] = []

    def seed(summary):
        summary["campaign"] = {
            "campaign_id": "missing-origin-nonce",
            "epoch": 1,
            "status": "paused",
            campaign_epoch.NEGATION_PROMOTION_ROOT_REGISTRATION_OPEN_FIELD: False,
            negation_promotion._CAMPAIGN_ROOTS_FIELD: {
                "campaign_id": "missing-origin-nonce",
                "roots": roots,
                "registry_sha256": negation_promotion._campaign_root_registry_sha256(roots),
            },
        }

    update_json_file(campaign_epoch._summary_path(), seed)
    resumed: dict = {}

    campaign_epoch.ensure_campaign(resumed)
    snapshot = campaign_epoch.campaign_snapshot()

    assert "provider_turn_nonce" not in snapshot
    allowed, reason = negation_promotion.campaign_root_provider_gate(snapshot)
    assert allowed is False
    assert "provider" in reason
    with pytest.raises(campaign_epoch.CampaignRootProviderBlocked, match="provider"):
        campaign_epoch.reserve_provider_turn(resumed)
    assert "provider_turn_nonce" not in campaign_epoch.campaign_snapshot()


def test_marker_absent_legacy_campaign_gets_nonce_only_at_reservation(monkeypatch, tmp_path):
    """Legacy proving remains resumable without acquiring main-goal authority."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE", "1")

    def seed(summary):
        summary["campaign"] = {
            "campaign_id": "legacy-without-nonce",
            "epoch": 1,
            "status": "paused",
        }

    update_json_file(campaign_epoch._summary_path(), seed)
    resumed: dict = {}

    campaign_epoch.ensure_campaign(resumed)
    assert "provider_turn_nonce" not in campaign_epoch.campaign_snapshot()

    assert campaign_epoch.reserve_provider_turn(resumed)["nonce"] == 1
    snapshot = campaign_epoch.campaign_snapshot()
    assert snapshot["provider_turn_nonce"] == 1
    assert campaign_epoch.NEGATION_PROMOTION_ROOT_REGISTRATION_OPEN_FIELD not in snapshot
    assert negation_promotion._validate_campaign_root_registry(snapshot).ok is False


def test_resumed_runner_rolls_at_durable_cycle_ceiling_before_provider_call(monkeypatch, tmp_path):
    """Process restarts cannot grant another full local cycle allowance."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-resume-cycle-ceiling")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    first_state: dict = {}
    for _ in range(8):
        campaign_epoch.record_managed_cycle(first_state)

    resumed_state: dict = {}
    rolled = False
    roll_calls: list[tuple[str, int]] = []
    provider_calls: list[str] = []

    def live_state(*args, **kwargs):
        return {
            "active_file": "/tmp/Demo.lean",
            "target_symbol": "demo",
            "sorry_count": 0 if rolled else 1,
            "verification_ok": rolled,
        }

    def roll_epoch(
        agent,
        history,
        compaction_state,
        checkpoint_state,
        autonomy_state,
        live_state,
        *,
        reason,
        cycle,
    ):
        nonlocal rolled
        roll_calls.append((reason, cycle))
        campaign_epoch.roll_epoch(autonomy_state, reason=reason, cycle=cycle)
        rolled = True
        return history, compaction_state, checkpoint_state

    monkeypatch.setattr(runner, "_is_autonomous_workflow", lambda: True)
    monkeypatch.setattr(runner, "_autonomous_max_cycles", lambda: 8)
    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_build_live_proof_state_compat", live_state)
    monkeypatch.setattr(
        runner, "_promote_live_state_to_verified_compat", lambda state, autonomy_state: state
    )
    monkeypatch.setattr(
        runner, "_live_state_is_verified", lambda state: bool(state.get("verification_ok"))
    )
    monkeypatch.setattr(runner, "_roll_autonomous_campaign_epoch", roll_epoch)
    monkeypatch.setattr(
        runner, "_advance_project_prove_manager_if_needed", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        runner, "_rebuild_history_for_theorem_transition", lambda *args, **kwargs: (None, None)
    )
    monkeypatch.setattr(
        runner, "_maybe_run_document_formalization_review_agent", lambda *args: False
    )
    monkeypatch.setattr(runner, "_maybe_announce_final_file_sweep_state", lambda *args: None)
    monkeypatch.setattr(runner, "_maybe_sync_plan_state", lambda *args: None)
    monkeypatch.setattr(
        runner,
        "_run_managed_conversation_with_retries",
        lambda *args, **kwargs: provider_calls.append("called"),
    )

    runner._drive_autonomous_followups_inner(None, "", [], {}, {}, resumed_state)

    assert roll_calls == [("cycle-ceiling", 8)]
    assert provider_calls == []
    assert resumed_state["campaign_epoch"] == 2
    assert campaign_epoch.campaign_snapshot()["epoch_history"][-1]["cycles"] == 8


def test_resumed_startup_rolls_spent_epoch_before_its_provider_turn(monkeypatch, tmp_path):
    """The process startup turn is not a loophole around the durable ceiling."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-startup-cycle-ceiling")
    first_state: dict = {}
    for _ in range(8):
        campaign_epoch.record_managed_cycle(first_state)

    resumed_state: dict = {}
    roll_calls: list[tuple[str, int]] = []

    def roll_epoch(
        agent,
        history,
        compaction_state,
        checkpoint_state,
        autonomy_state,
        live_state,
        *,
        reason,
        cycle,
    ):
        roll_calls.append((reason, cycle))
        campaign_epoch.roll_epoch(autonomy_state, reason=reason, cycle=cycle)
        return [{"role": "user", "content": "fresh epoch"}], {}, {}

    monkeypatch.setattr(runner, "_autonomous_max_cycles", lambda: 8)
    monkeypatch.setattr(runner, "_roll_autonomous_campaign_epoch", roll_epoch)

    history, compaction, checkpoint, rolled = runner._roll_spent_startup_epoch_if_needed(
        None,
        [{"role": "assistant", "content": "stale context"}],
        {"compacted": False},
        {"current": "checkpoint"},
        resumed_state,
        {"target_symbol": "demo"},
    )

    assert rolled is True
    assert roll_calls == [("cycle-ceiling", 8)]
    assert history == [{"role": "user", "content": "fresh epoch"}]
    assert compaction == {}
    assert checkpoint == {}
    assert resumed_state["campaign_epoch"] == 2


def test_rollover_after_four_route_decisions_across_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-four-routes")
    first_state: dict = {}
    for route in ("direct-prove", "plan", "decompose"):
        campaign_epoch.record_route_decision(first_state, route=route)

    resumed_state: dict = {}
    campaign_epoch.ensure_campaign(resumed_state)
    streak = campaign_epoch.record_route_decision(resumed_state, route="negate")

    assert streak == 4
    assert resumed_state["orchestrator_routes_used"] == 4
    assert (
        resumed_state["campaign_epoch_requested"]
        == campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON
    )
    assert campaign_epoch.campaign_snapshot()["no_progress_route_streak"] == 4

    reason = campaign_epoch.consume_rollover_request(resumed_state)
    handoff = campaign_epoch.roll_epoch(resumed_state, reason=reason, cycle=7)

    assert "fresh epoch: 2" in handoff
    assert resumed_state["campaign_epoch"] == 2
    assert resumed_state["orchestrator_routes_used"] == 0
    assert campaign_epoch.campaign_snapshot()["no_progress_route_streak"] == 0


def test_route_rollover_keeps_spent_epoch_scope_after_assignment_switch(monkeypatch, tmp_path):
    """A resumed rollover attributes the spent epoch to its causal route."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-switched-rollover-scope")
    old_file = str(tmp_path / "Old.lean")
    new_file = str(tmp_path / "New.lean")
    state: dict = {}
    for route in ("plan", "plan", "negate", "decompose"):
        campaign_epoch.record_route_decision(
            state,
            route=route,
            target_symbol="old_target",
            active_file=old_file,
        )

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        campaign_epoch,
        "append_workflow_activity",
        lambda event, _message, **details: events.append((event, details)),
    )
    handoff = campaign_epoch.roll_epoch(
        state,
        reason=campaign_epoch.consume_rollover_request(state),
        cycle=0,
        target_symbol="new_target",
        active_file=new_file,
    )

    snapshot = campaign_epoch.campaign_snapshot()
    ended = snapshot["epoch_history"][-1]
    assert ended["target_symbol"] == "old_target"
    assert ended["active_file"] == old_file
    assert ended["route_portfolio"][-1]["target_symbol"] == "old_target"
    assert snapshot["epoch_worker_refresh"]["target_symbol"] == "new_target"
    assert snapshot["epoch_worker_refresh"]["active_file"] == new_file
    ended_event = next(details for event, details in events if event == "campaign-epoch-ended")
    started_event = next(details for event, details in events if event == "campaign-epoch-started")
    assert ended_event["target_symbol"] == "old_target"
    assert ended_event["active_file"] == old_file
    assert started_event["target_symbol"] == "new_target"
    assert started_event["active_file"] == new_file
    assert "active target: new_target" in handoff


def test_rollover_persists_distinct_route_obligation_until_strategy_starts(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-distinct-route-refresh")
    state: dict = {}
    for route in ("direct-prove", "direct-prove", "direct-prove", "direct-prove"):
        campaign_epoch.record_route_decision(
            state,
            route=route,
            target_symbol="hard_goal",
            active_file=str(tmp_path / "Main.lean"),
        )

    campaign_epoch.roll_epoch(
        state,
        reason=campaign_epoch.consume_rollover_request(state),
        cycle=4,
        target_symbol="hard_goal",
        active_file=str(tmp_path / "Main.lean"),
    )

    refresh = state[campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY]
    assert refresh["required"] is True
    assert refresh["previous_routes"] == ["direct-prove"] * 4
    assert "orchestrator_current_route" not in state
    assert (
        campaign_epoch.mark_epoch_refresh_started(
            state,
            route="direct-prove",
            refresh_token=str(refresh["token"]),
            epoch=int(refresh["new_epoch"]),
        )
        is False
    )

    resumed: dict = {}
    campaign_epoch.ensure_campaign(resumed)
    assert resumed[campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY]["required"] is True
    assert (
        campaign_epoch.mark_epoch_refresh_started(
            resumed,
            route="decompose",
            refresh_token="stale-token",
            epoch=int(refresh["new_epoch"]),
        )
        is False
    )
    assert (
        campaign_epoch.mark_epoch_refresh_started(
            resumed,
            route="decompose",
            refresh_token=str(refresh["token"]),
            epoch=int(refresh["new_epoch"]),
        )
        is True
    )
    assert campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY not in resumed
    assert campaign_epoch.campaign_snapshot()["epoch_route_refresh"] == {
        **refresh,
        "required": False,
        "selected_route": "decompose",
        "started_at": campaign_epoch.campaign_snapshot()["epoch_route_refresh"]["started_at"],
    }


def test_epoch_route_started_activity_includes_exact_refresh_token(monkeypatch, tmp_path):
    """Expose the consumed refresh token for live replay correlation."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-route-started-token")
    state: dict = {}
    campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="hard_goal",
        active_file=str(tmp_path / "Main.lean"),
    )
    refresh = dict(state[campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY])
    activities: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        campaign_epoch,
        "append_workflow_activity",
        lambda event, _message, **details: activities.append((event, details)),
    )

    assert campaign_epoch.mark_epoch_refresh_started(
        state,
        route="decompose",
        refresh_token=str(refresh["token"]),
        epoch=int(refresh["new_epoch"]),
    )

    event, details = activities[-1]
    assert event == "campaign-epoch-route-started"
    assert details["refresh_token"] == refresh["token"]


def test_stale_prior_route_cannot_consume_fresh_epoch_obligation(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-stale-route-token")
    state: dict = {"orchestrator_current_route": "plan"}
    campaign_epoch.record_route_decision(state, route="plan")

    campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="hard_goal",
        active_file=str(tmp_path / "Main.lean"),
    )

    refresh = dict(state[campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY])
    assert "orchestrator_current_route" not in state
    assert (
        campaign_epoch.mark_epoch_refresh_started(
            state,
            route="plan",
            refresh_token=str(refresh["token"]),
            epoch=int(refresh["new_epoch"]),
        )
        is False
    )
    assert campaign_epoch.campaign_snapshot()["epoch_route_refresh"]["required"] is True


def test_exact_persisted_viable_route_completes_when_static_unseen_route_is_unavailable(
    monkeypatch, tmp_path
):
    """A reserved viable route outranks the wider static route vocabulary."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-viable-route-refresh")
    active_file = str(tmp_path / "Main.lean")
    state: dict = {}
    for route in ("plan", "decompose", "plan", "plan"):
        campaign_epoch.record_route_decision(
            state,
            route=route,
            target_symbol="hard_goal",
            active_file=active_file,
            limit=99,
        )
    campaign_epoch.roll_epoch(
        state,
        reason=campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        cycle=4,
        target_symbol="hard_goal",
        active_file=active_file,
    )
    refresh = dict(state[campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY])
    assert campaign_epoch._refresh_route_is_distinct(refresh, "decompose") is False

    campaign_epoch.record_route_decision(
        state,
        route="decompose",
        target_symbol="hard_goal",
        active_file=active_file,
        trigger="scope-entry",
        route_reason="negation was already attempted, so decompose is the viable change",
    )
    selection = dict(state[campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY])

    assert (
        campaign_epoch.mark_epoch_refresh_started(
            state,
            route="plan",
            refresh_token=str(selection["token"]),
            epoch=int(selection["epoch"]),
            target_symbol="hard_goal",
            active_file=active_file,
        )
        is False
    )
    assert (
        campaign_epoch.mark_epoch_refresh_started(
            state,
            route="decompose",
            refresh_token=str(selection["token"]),
            epoch=int(selection["epoch"]),
            target_symbol="hard_goal",
            active_file=active_file,
        )
        is True
    )
    snapshot = campaign_epoch.campaign_snapshot()["epoch_route_refresh"]
    assert snapshot["required"] is False
    assert snapshot["selected_route"] == "decompose"


def test_invalid_persisted_epoch_route_selection_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-invalid-route-refresh")
    state: dict = {}
    campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="hard_goal",
        active_file=str(tmp_path / "Main.lean"),
    )
    refresh = dict(state[campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY])
    refresh["pending_selection"] = {
        "token": refresh["token"],
        "epoch": refresh["new_epoch"],
        "route": "park",
        "target_symbol": "hard_goal",
        "active_file": str(tmp_path / "Main.lean"),
    }
    state[campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY] = refresh

    assert (
        campaign_epoch.mark_epoch_refresh_started(
            state,
            route="park",
            refresh_token=str(refresh["token"]),
            epoch=int(refresh["new_epoch"]),
            target_symbol="hard_goal",
            active_file=str(tmp_path / "Main.lean"),
        )
        is False
    )
    assert campaign_epoch.campaign_snapshot()["epoch_route_refresh"]["required"] is True


def test_verified_graph_progress_resets_durable_route_streak(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state: dict = {}
    for route in ("direct-prove", "plan", "decompose"):
        campaign_epoch.record_route_decision(state, route=route)

    reset = campaign_epoch.record_verified_graph_progress(state, node_ids=["n-helper"])

    assert reset is True
    assert state["orchestrator_routes_used"] == 0
    resumed_state: dict = {}
    resumed = campaign_epoch.ensure_campaign(resumed_state)
    assert resumed["no_progress_route_streak"] == 0
    assert resumed_state["orchestrator_routes_used"] == 0


@pytest.mark.parametrize(
    "rollover_reason",
    [
        campaign_epoch.SEMANTIC_PORTFOLIO_ROLLOVER_REASON,
        campaign_epoch.ROUTE_PORTFOLIO_ROLLOVER_REASON,
    ],
)
def test_verified_graph_progress_cancels_every_no_progress_portfolio_rollover(
    monkeypatch,
    tmp_path,
    rollover_reason,
):
    """New kernel progress invalidates a not-yet-consumed difficulty rollover."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    state: dict = {}
    campaign_epoch.record_route_decision(state, route="plan")
    campaign_epoch.request_rollover(state, rollover_reason)

    campaign_epoch.record_verified_graph_progress(state, node_ids=["n-new-helper"])

    assert "campaign_epoch_requested" not in state
    assert state["orchestrator_routes_used"] == 0


def test_requested_plan_supersedes_stale_negate_replays_but_not_counterexample():
    """Rank the newest explicit route above strategy-only negate replay."""
    inflight = {"route": "negate", "token": "inflight-negate"}
    selection = {"route": "negate", "token": "epoch-negate"}

    assert campaign_epoch.replay_sources_superseded_by_requested_route(
        requested_route="plan",
        inflight_route=inflight,
        epoch_selection=selection,
    ) == (
        campaign_epoch.INFLIGHT_ROUTE_STATE_KEY,
        campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY,
    )
    assert (
        campaign_epoch.replay_sources_superseded_by_requested_route(
            requested_route="plan",
            inflight_route=inflight,
            epoch_selection=selection,
            authenticated_negate=True,
        )
        == ()
    )
    assert (
        campaign_epoch.replay_sources_superseded_by_requested_route(
            requested_route="negate",
            inflight_route=inflight,
            epoch_selection=selection,
        )
        == ()
    )
