"""Phase 4 (2/6) tests: orchestrator runner wiring — consult, apply, resume."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.orchestrator import OrchestratorRoute
from leanflow_cli.workflows.queue_manager import TheoremKey, TheoremQueueManager
from leanflow_cli.workflows.workflow_json_io import update_json_file


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_ENABLED", "1")
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


def _complete_mechanical_selection(
    route: OrchestratorRoute,
    state: dict[str, Any],
    active_file: str,
) -> bool:
    """Record exact durable route evidence and consume its fresh token."""
    if route.route == runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE:
        return (
            runner._apply_orchestrator_route_with_completion(route, [], state, {}) == "continue"
            and runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY not in state
            and bool(state.get("campaign_epoch_requested"))
        )
    evidence_kind = {
        "decompose": "decomposition-fallback",
        "negate": "negation-probe",
        "plan": "planner",
    }[route.route]
    runner._record_orchestrator_route_execution(
        state,
        runner.route_execution.RouteExecution.recorded(
            route=route.route,
            target_symbol="demo",
            active_file=active_file,
            outcome="test evidence persisted",
            evidence_kind=evidence_kind,
        ),
    )
    return runner._complete_epoch_route_after_observable_work(route, state)


def test_consult_noops_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("LEANFLOW_ORCHESTRATOR_ENABLED", raising=False)
    events = _events(monkeypatch)

    route = runner._orchestrator_consult("stall", _autonomy_state(str(tmp_path)), {})

    assert route is None
    assert events == []


def test_deferred_proof_candidate_supersedes_stale_epoch_negate(enabled, monkeypatch, tmp_path):
    """Do not replay feasibility work ahead of a newer sorry-free proof candidate."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "deferred-proof-stale-negate")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "_restored_assignment_verification_timeout_reason",
        lambda *_args, **_kwargs: "LeanProbe timed out after 300 seconds",
    )
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    plan_state.save_queue_manager_state(state)
    runner.campaign_epoch.roll_epoch(
        state,
        reason=runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        cycle=4,
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.record_route_decision(
        state,
        route="negate",
        target_symbol="demo",
        active_file=str(active),
        trigger="scope-entry",
        route_reason="stale feasibility branch",
    )
    live_state = {
        "active_file": str(active),
        "target_symbol": "demo",
        "proof_state_authority": "source_only_unverified",
        "defer_incremental_warmup": True,
        "sorry_count": 0,
    }

    selected = runner._orchestrator_consult("scope-entry", state, live_state)

    assert selected is not None and selected.route == "direct-prove"
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in state
    assert runner.campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY not in state
    assert any(
        event[0] == "campaign-epoch-stale-negate-backpressured" for event, _details in events
    )
    assert not any(event[0] == "campaign-epoch-route-resumed" for event, _details in events)
    refresh = runner.campaign_epoch.campaign_snapshot()["epoch_route_refresh"]
    assert refresh["required"] is False
    assert refresh["superseded_route"] == "negate"


def test_repeated_current_revision_timeouts_request_decomposition(enabled, monkeypatch, tmp_path):
    """Route a repeatedly timed-out sorry-free declaration to structural recovery."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "repeated-timeout-decompose")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "_restored_assignment_verification_timeout_reason",
        lambda *_args, **_kwargs: "LeanProbe timed out after 300 seconds",
    )
    monkeypatch.setattr(
        runner,
        "_assignment_verification_timeout_count",
        lambda *_args, **_kwargs: 2,
    )
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    live_state = {
        "active_file": str(active),
        "target_symbol": "demo",
        "proof_state_authority": "source_only_unverified",
        "defer_incremental_warmup": True,
        "sorry_count": 0,
    }

    selected = runner._orchestrator_consult("scope-entry", state, live_state)

    assert selected is not None and selected.route == "decompose"
    assert "top-level helpers" in selected.target["prover_request_reason"]
    assert any(
        event[0] == "verification-timeout-decomposition-requested" and details["timeout_count"] == 2
        for event, details in events
    )


def test_repeated_timeout_decomposition_survives_spent_route_ledger(enabled, monkeypatch, tmp_path):
    """A spent persistence ledger cannot rotate new timeout recovery into refresh."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "spent-timeout-decompose")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "_restored_assignment_verification_timeout_reason",
        lambda *_args, **_kwargs: "LeanProbe timed out after 300 seconds",
    )
    monkeypatch.setattr(
        runner,
        "_assignment_verification_timeout_count",
        lambda *_args, **_kwargs: 3,
    )
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    state["orchestrator_routes_used"] = 4
    state[runner.campaign_epoch.SEMANTIC_ROUTE_HISTORY_STATE_KEY] = [
        {
            "route": route,
            "target_symbol": "demo",
            "active_file": str(active),
        }
        for route in ("decompose", "negate", "plan")
    ]
    live_state = {
        "active_file": str(active),
        "target_symbol": "demo",
        "proof_state_authority": "lean_inspect",
        "deferred_exact_verification": True,
        "sorry_count": 0,
    }

    selected = runner._orchestrator_consult("scope-entry", state, live_state)

    assert selected is not None and selected.route == "decompose"
    assert selected.source == "deterministic-timeout-recovery"
    assert selected.target["timeout_decomposition_recovery"] is True


def test_repeated_live_state_timeouts_request_decomposition(enabled, monkeypatch, tmp_path):
    """Apply timeout structural recovery during a live campaign, not only startup."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "live-timeout-decompose")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "_restored_assignment_verification_timeout_reason",
        lambda *_args, **_kwargs: "lake env lean timed out after 600 seconds",
    )
    monkeypatch.setattr(
        runner,
        "_assignment_verification_timeout_count",
        lambda *_args, **_kwargs: 3,
    )
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    live_state = {
        "active_file": str(active),
        "target_symbol": "demo",
        "proof_state_authority": "lean_inspect",
        "deferred_exact_verification": True,
        "diagnostics": "no errors found",
        "sorry_count": 0,
    }

    selected = runner._orchestrator_consult("scope-entry", state, live_state)

    assert selected is not None and selected.route == "decompose"
    assert "top-level helpers" in selected.target["prover_request_reason"]


def test_deferred_proof_candidate_drops_interrupted_mechanical_route(
    enabled, monkeypatch, tmp_path
):
    """Crash-durable persistence cannot outrank a newer proof candidate."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "deferred-proof-stale-inflight")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "_restored_assignment_verification_timeout_reason",
        lambda *_args, **_kwargs: "LeanProbe timed out after 300 seconds",
    )
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    plan_state.save_queue_manager_state(state)
    runner.campaign_epoch.record_route_decision(
        state,
        route="decompose",
        target_symbol="demo",
        active_file=str(active),
        trigger="event",
        route_reason="interrupted old decomposition",
        reserve_inflight=True,
    )
    assert state[runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY]["route"] == "decompose"
    live_state = {
        "active_file": str(active),
        "target_symbol": "demo",
        "proof_state_authority": "source_only_unverified",
        "defer_incremental_warmup": True,
        "sorry_count": 0,
    }

    selected = runner._orchestrator_consult("scope-entry", state, live_state)

    assert selected is not None and selected.route == "direct-prove"
    current = runner.campaign_epoch.campaign_snapshot()["inflight_route"]
    assert current["route"] == "direct-prove"
    assert any(
        event[0] == "campaign-stale-persistence-backpressured"
        and details["routes"] == ["decompose"]
        for event, details in events
    )
    assert not any(event[0] == "campaign-inflight-route-resumed" for event, _details in events)


def test_deferred_proof_candidate_drops_stale_fidelity_human_review(enabled, monkeypatch, tmp_path):
    """A corrected policy audit must retire its unfinished false human-review route."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "deferred-proof-stale-fidelity-review")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "_restored_assignment_verification_timeout_reason",
        lambda *_args, **_kwargs: "LeanProbe timed out after 300 seconds",
    )
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    plan_state.save_queue_manager_state(state)
    runner.campaign_epoch.record_route_decision(
        state,
        route="ask-human",
        target_symbol="demo",
        active_file=str(active),
        trigger="scope-entry",
        route_reason="statement fidelity is suspect on the main goal; human review requested",
        reserve_inflight=True,
    )
    live_state = {
        "active_file": str(active),
        "target_symbol": "demo",
        "proof_state_authority": "source_only_unverified",
        "defer_incremental_warmup": True,
        "sorry_count": 0,
    }

    selected = runner._orchestrator_consult("scope-entry", state, live_state)

    assert selected is not None and selected.route == "direct-prove"
    assert any(
        event[0] == "campaign-stale-route-backpressured" and details["routes"] == ["ask-human"]
        for event, details in events
    )
    assert not any(event[0] == "campaign-inflight-route-resumed" for event, _details in events)


def test_consult_refreshes_plan_render_without_a_graph_mutation(enabled, monkeypatch, tmp_path):
    events = _events(monkeypatch)
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    plan_state.save_queue_manager_state(state)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=plan_state.node_id_for("demo", str(active)),
                    name="demo",
                    file=str(active),
                    status="proving",
                ),
            )
        )
    )
    runner.campaign_epoch.record_route_decision(
        state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    plan_state.append_journal_event(
        {
            "event": "orchestrator-route",
            "trigger": "scope-entry",
            "route": "direct-prove",
            "reason": "old route reason",
            "name": "demo",
            "file": str(active),
        }
    )
    plan_state.save_plan_md(plan_state.load_blueprint(), plan_state.load_summary())
    prior = plan_state.plan_state_paths().plan_md.read_text(encoding="utf-8")
    assert "current orchestrator route: `direct-prove` for `demo`" in prior
    assert "old route reason" not in prior
    assert "route rationales are omitted" in prior
    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: OrchestratorRoute(route="plan", reason="fresh route reason"),
    )

    selected = runner._orchestrator_consult("event", state, {})

    assert selected is not None and selected.route == "plan"
    rendered = plan_state.plan_state_paths().plan_md.read_text(encoding="utf-8")
    strategy = rendered.split("## Strategy", 1)[1].split("## Frontier", 1)[0]
    assert "current orchestrator route: `plan` for `demo`" in strategy
    assert "fresh route reason" not in strategy
    assert "old route reason" not in strategy
    assert "route rationales are omitted" in strategy


def test_failed_research_scope_consult_reopens_scope(monkeypatch):
    monkeypatch.setattr(runner.research_mode, "research_mode_enabled", lambda: True)
    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_enabled", lambda: True)
    monkeypatch.setattr(runner, "_maybe_sync_plan_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_statement_fidelity_audit", lambda *args, **kwargs: "pass")
    monkeypatch.setattr(runner, "_migrate_research_findings_for_assignment", lambda *args: None)
    monkeypatch.setattr(runner, "_maintain_research_portfolio", lambda *args: None)
    monkeypatch.setattr(runner, "_take_research_findings_prompt", lambda *args: "")
    monkeypatch.setattr(runner, "_orchestrator_consult", lambda *args, **kwargs: None)
    autonomy_state: dict[str, Any] = {"orchestrator_scope_entered": True}

    assert runner._research_scope_entry_setup("start", autonomy_state, {}) == "start"
    assert "orchestrator_scope_entered" not in autonomy_state


def test_epoch_route_selection_waits_for_observable_mechanical_completion(
    enabled, monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "route-selection-token")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        autonomy_state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        autonomy_state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )

    route = runner._orchestrator_consult("scope-entry", autonomy_state, {})

    assert route is not None and route.route == "decompose"
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY in autonomy_state
    assert runner.campaign_epoch.campaign_snapshot()["epoch_route_refresh"]["required"] is True
    # An unrelated provider return cannot consume a mechanical selection.
    assert runner._complete_epoch_route_after_managed_turn(autonomy_state) is False
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY in autonomy_state
    runner._record_orchestrator_route_execution(
        autonomy_state,
        runner.route_execution.RouteExecution.recorded(
            route="decompose",
            target_symbol="demo",
            active_file=str(active),
            outcome="no insertable helper; guarded fallback recorded",
            evidence_kind="decomposition-fallback",
        ),
    )
    assert runner._complete_epoch_route_after_observable_work(route, autonomy_state) is True
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in autonomy_state
    assert runner.campaign_epoch.campaign_snapshot()["epoch_route_refresh"]["required"] is False


def test_fresh_plan_selection_completes_only_after_successful_planner(
    enabled, monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "fresh-plan-success")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(runner.planner_phase, "planner_enabled", lambda: True)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: OrchestratorRoute(route="plan", reason="fresh planner route"),
    )
    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: runner.planner_phase.PlannerOutcome(
            ok=True,
            reason="planner artifacts persisted",
            nodes_added=1,
            synthesis_status="ok",
        ),
    )

    selected = runner._orchestrator_consult("scope-entry", state, {})
    assert selected is not None and selected.route == "plan"
    assert runner._apply_orchestrator_route_with_completion(selected, [], state, {}) == "continue"
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in state
    assert runner.campaign_epoch.campaign_snapshot()["epoch_route_refresh"]["required"] is False


def test_capacity_deferred_plan_keeps_fresh_selection_for_exact_replay(
    enabled, monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "fresh-plan-capacity")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(runner.planner_phase, "planner_enabled", lambda: True)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: OrchestratorRoute(route="plan", reason="fresh planner route"),
    )
    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: runner.planner_phase.PlannerOutcome(
            ok=False,
            reason="all background slots busy",
            synthesis_status="capacity-deferred",
        ),
    )

    selected = runner._orchestrator_consult("scope-entry", state, {})
    assert selected is not None and selected.route == "plan"
    assert runner._apply_orchestrator_route_with_completion(selected, [], state, {}) == "continue"
    selection = state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY]
    assert selection["route"] == "plan"
    assert runner.campaign_epoch.campaign_snapshot()["epoch_route_refresh"]["required"] is True
    assert runner._orchestrator_event_due(state, 1) == "event"

    campaign_before_replay = runner.campaign_epoch.campaign_snapshot()

    def forbidden_floor(_ctx):
        raise AssertionError("event replay must reuse the durable fresh selection")

    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_route", forbidden_floor)
    replay = runner._orchestrator_consult("event", state, {})

    assert replay is not None
    assert replay.route == selected.route
    assert replay.reason == selected.reason
    assert replay.source == selected.source
    assert replay.target == selected.target
    assert state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY] == selection
    campaign_after_replay = runner.campaign_epoch.campaign_snapshot()
    assert (
        campaign_after_replay["no_progress_route_streak"]
        == campaign_before_replay["no_progress_route_streak"]
    )
    assert campaign_after_replay["epoch_routes"] == campaign_before_replay["epoch_routes"]
    assert not campaign_after_replay.get("inflight_route")

    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: runner.planner_phase.PlannerOutcome(
            ok=True,
            reason="planner artifacts persisted on retry",
            nodes_added=1,
            synthesis_status="ok",
        ),
    )
    assert runner._apply_orchestrator_route_with_completion(replay, [], state, {}) == "continue"
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in state
    completed_campaign = runner.campaign_epoch.campaign_snapshot()
    assert completed_campaign["epoch_route_refresh"]["required"] is False
    assert not completed_campaign.get("inflight_route")


def test_evidence_interrupted_plan_forces_construction_before_replanning(
    enabled, monkeypatch, tmp_path
):
    """Preserved planner evidence must not trigger unchanged-source fanout again."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "fresh-plan-evidence-interrupted")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(runner.planner_phase, "planner_enabled", lambda: True)
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: OrchestratorRoute(route="plan", reason="fresh planner route"),
    )
    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: runner.planner_phase.PlannerOutcome(
            ok=False,
            reason="lane evidence preserved before synthesis boundary",
            synthesis_status=runner.planner_phase.PLANNER_EVIDENCE_INTERRUPTED_STATUS,
            lanes=({"lane": "mathlib", "status": "completed"},),
        ),
    )

    selected = runner._orchestrator_consult("scope-entry", state, {})
    assert selected is not None and selected.route == "plan"
    history: list[dict[str, str]] = []
    assert (
        runner._apply_orchestrator_route_with_completion(selected, history, state, {}) == "continue"
    )

    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in state
    execution = runner._current_orchestrator_route_execution(state)
    assert execution is not None and execution.completed
    assert execution.evidence_kind == "plan-route-obstacle"
    marker = state[runner.route_execution.PLANNER_TERMINAL_OBSTACLE_STATE_KEY]
    assert marker["outcome"] == runner.planner_phase.PLANNER_EVIDENCE_INTERRUPTED_STATUS
    assert "preserved" in history[-1]["content"].lower()
    assert "construct" in history[-1]["content"]

    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: pytest.fail(
            "unchanged source must attempt preserved evidence before replanning"
        ),
    )
    assert (
        runner._orchestrator_apply_route(
            OrchestratorRoute(route="plan", reason="repeated planner request"),
            history,
            state,
            {},
            agent=None,
        )
        == "continue"
    )
    assert any(event[0] == "planner-route-suppressed" for event, _details in events)


def test_timed_out_plan_retires_fresh_selection_for_new_route(enabled, monkeypatch, tmp_path):
    """A bounded synthesis timeout must not replay the same planner forever."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "fresh-plan-timeout")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(runner.planner_phase, "planner_enabled", lambda: True)
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: OrchestratorRoute(route="plan", reason="fresh planner route"),
    )
    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: runner.planner_phase.PlannerOutcome(
            ok=False,
            reason="synthesizer unavailable (timeout)",
            synthesis_status="timeout",
        ),
    )

    selected = runner._orchestrator_consult("scope-entry", state, {})
    assert selected is not None and selected.route == "plan"
    assert runner._apply_orchestrator_route_with_completion(selected, [], state, {}) == "continue"

    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in state
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["epoch_route_refresh"]["required"] is False
    execution = runner._current_orchestrator_route_execution(state)
    assert execution is not None and execution.completed
    assert execution.evidence_kind == "plan-route-obstacle"
    target_signature = runner.research_helper_candidate_priority.target_signature_sha256(
        str(active),
        "demo",
    )
    assert runner.route_execution.planner_terminal_obstacle_blocks_request(
        state,
        target_symbol="demo",
        active_file=str(active),
        target_signature_sha256=target_signature,
    )
    resumed_state: dict[str, Any] = {}
    runner.campaign_epoch.rehydrate_campaign(resumed_state)
    assert runner.route_execution.planner_terminal_obstacle_blocks_request(
        resumed_state,
        target_symbol="demo",
        active_file=str(active),
        target_signature_sha256=target_signature,
    )
    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: pytest.fail(
            "an unchanged target must not launch the cooled-down planner"
        ),
    )
    assert (
        runner._orchestrator_apply_route(
            OrchestratorRoute(route="plan", reason="replayed planner request"),
            [],
            state,
            {},
            agent=None,
        )
        == "continue"
    )
    assert any(event[0] == "planner-route-suppressed" for event, _details in events)
    active.write_text(
        "private lemma helper : True := by trivial\n\n" "theorem demo : True := by\n  sorry\n",
        encoding="utf-8",
    )
    assert (
        runner.research_helper_candidate_priority.target_signature_sha256(
            str(active),
            "demo",
        )
        == target_signature
    )
    assert runner.route_execution.planner_terminal_obstacle_blocks_request(
        state,
        target_symbol="demo",
        active_file=str(active),
        target_signature_sha256=target_signature,
    )
    runner._record_orchestrator_route_execution(
        state,
        runner.route_execution.RouteExecution.recorded(
            route="decompose",
            target_symbol="demo",
            active_file=str(active),
            outcome="helper integrated",
            evidence_kind="decomposition-helper",
        ),
    )
    assert runner.route_execution.PLANNER_TERMINAL_OBSTACLE_STATE_KEY not in state
    assert (
        runner.campaign_epoch.PLANNER_TERMINAL_OBSTACLE_FIELD
        not in runner.campaign_epoch.campaign_snapshot()
    )
    assert any(event[0] == "planner-route-obstacle" for event, _details in events)


def test_completed_plan_requires_target_edit_before_replanning(enabled, monkeypatch, tmp_path):
    """Keep concrete planner advice active across helper-only and route-only progress."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "completed-plan-advice")
    monkeypatch.setattr(runner.planner_phase, "planner_enabled", lambda: True)
    monkeypatch.setattr(runner, "_record_activity", lambda *_args, **_kwargs: None)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    state["_orchestrator_last_ctx"] = {
        "target_symbol": "demo",
        "active_file": str(active),
    }
    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: runner.planner_phase.PlannerOutcome(
            ok=True,
            reason="use the checked tangent route",
            synthesis_status="completed",
        ),
    )

    assert (
        runner._orchestrator_apply_route(
            OrchestratorRoute(route="plan", reason="request exact advice"),
            [],
            state,
            {},
            agent=None,
        )
        == "continue"
    )
    marker = dict(state[runner.route_execution.PLANNER_TERMINAL_OBSTACLE_STATE_KEY])
    assert marker["outcome"] == "planner-completed"
    assert marker["target_declaration_sha256"] == runner._target_declaration_sha256(
        str(active),
        "demo",
    )
    resumed_state: dict[str, Any] = {}
    runner.campaign_epoch.rehydrate_campaign(resumed_state)
    assert resumed_state[runner.route_execution.PLANNER_TERMINAL_OBSTACLE_STATE_KEY] == marker

    active.write_text(
        "private lemma helper : True := by trivial\n\n" "theorem demo : True := by\n  sorry\n",
        encoding="utf-8",
    )
    signature = runner.research_helper_candidate_priority.target_signature_sha256(
        str(active),
        "demo",
    )
    declaration = runner._target_declaration_sha256(str(active), "demo")
    assert runner.route_execution.planner_terminal_obstacle_blocks_request(
        state,
        target_symbol="demo",
        active_file=str(active),
        target_signature_sha256=signature,
        target_declaration_sha256=declaration,
    )
    runner._record_orchestrator_route_execution(
        state,
        runner.route_execution.RouteExecution.recorded(
            route="decompose",
            target_symbol="demo",
            active_file=str(active),
            outcome="helper integrated",
            evidence_kind="decomposition-helper",
        ),
    )
    assert state[runner.route_execution.PLANNER_TERMINAL_OBSTACLE_STATE_KEY] == marker

    active.write_text(
        "private lemma helper : True := by trivial\n\n"
        "theorem demo : True := by\n  have h : True := trivial\n  sorry\n",
        encoding="utf-8",
    )
    assert runner.route_execution.clear_planner_terminal_obstacle_after_target_change(
        state,
        target_symbol="demo",
        active_file=str(active),
        target_signature_sha256=signature,
        target_declaration_sha256=runner._target_declaration_sha256(str(active), "demo"),
    )


def test_hydrated_fresh_selection_is_event_due_without_volatile_replay_token(
    enabled, monkeypatch, tmp_path
):
    """A fresh process must replay durable selection authority on an event tick."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "fresh-plan-hydrated-event")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(runner.planner_phase, "planner_enabled", lambda: True)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    first_state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        first_state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        first_state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: OrchestratorRoute(route="plan", reason="durable planner route"),
    )
    monkeypatch.setattr(
        runner,
        "_run_planner_phase_with_parent_maintenance",
        lambda *_args, **_kwargs: runner.planner_phase.PlannerOutcome(
            ok=False,
            reason="all background slots busy",
            synthesis_status="capacity-deferred",
        ),
    )
    selected = runner._orchestrator_consult("scope-entry", first_state, {})
    assert selected is not None and selected.route == "plan"
    assert (
        runner._apply_orchestrator_route_with_completion(selected, [], first_state, {})
        == "continue"
    )
    selection = dict(first_state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY])

    resumed = _autonomy_state(str(active))
    runner.campaign_epoch.ensure_campaign(resumed)
    assert runner._INFLIGHT_ROUTE_REPLAY_TOKEN_KEY not in resumed
    assert resumed[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY] == selection
    assert runner._orchestrator_event_due(resumed, 2) == "event"

    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: pytest.fail("hydrated selection must not be recomputed"),
    )
    replay = runner._orchestrator_consult("event", resumed, {})

    assert replay is not None
    assert replay.route == selected.route
    assert replay.reason == selected.reason
    assert replay.source == selected.source
    assert replay.target == selected.target
    assert resumed[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY] == selection


def test_observable_mechanical_completion_rejects_assignment_change(enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "fresh-route-scope-change")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    selected = runner._orchestrator_consult("scope-entry", state, {})
    assert selected is not None and selected.route == "decompose"
    runner._record_orchestrator_route_execution(
        state,
        runner.route_execution.RouteExecution.recorded(
            route="decompose",
            target_symbol="demo",
            active_file=str(active),
            outcome="helper inserted",
            evidence_kind="decomposition-helper",
        ),
    )
    state["current_queue_assignment"] = {
        "target_symbol": "new_helper",
        "active_file": str(active),
    }

    assert runner._complete_epoch_route_after_observable_work(selected, state) is False
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY in state


def test_legacy_parent_decomposition_consumes_while_inserted_child_is_active(
    enabled, monkeypatch, tmp_path
):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "legacy-parent-decomposition")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    selected = runner._orchestrator_consult("scope-entry", state, {})
    assert selected is not None and selected.route == "decompose"
    selection = dict(state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY])
    state["current_queue_assignment"] = {
        "target_symbol": "factors_lt",
        "active_file": str(active),
    }
    monkeypatch.setattr(
        runner,
        "read_workflow_activity",
        lambda **_kwargs: [
            {
                "event_id": "placed-parent-helper",
                "timestamp": (
                    datetime.fromisoformat(selection["selected_at"]) + timedelta(seconds=1)
                ).isoformat(),
                "type": "decomposer",
                "details": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "ok": True,
                    "placed": ["factors_lt"],
                },
            }
        ],
    )

    execution = runner._reconcile_legacy_epoch_route_completion(state)

    assert execution is not None and execution.evidence_kind == "decomposition-helper"
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in state
    assert runner._reconcile_legacy_epoch_route_completion(state) is None


def test_legacy_ambiguous_decomposition_keeps_selection_pending(enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "legacy-ambiguous-decomposition")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    runner._orchestrator_consult("scope-entry", state, {})
    selection = dict(state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY])
    monkeypatch.setattr(
        runner,
        "read_workflow_activity",
        lambda **_kwargs: [
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "type": "decomposer",
                "details": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "ok": False,
                    "placed": [],
                },
            }
        ],
    )

    assert runner._reconcile_legacy_epoch_route_completion(state) is None
    assert state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY] == selection


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

    assert runner.route_execution.legacy_completion_from_activity(selection, [event]) is None


def test_provider_failure_does_not_consume_epoch_route_selection(monkeypatch):
    selection = {"token": "epoch-token", "epoch": 2, "route": "decompose"}
    autonomy_state = {
        runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY: dict(selection),
    }
    monkeypatch.setattr(
        runner,
        "_complete_epoch_route_after_managed_turn",
        lambda _state: pytest.fail("failed provider turn must not complete the route token"),
    )

    assert (
        runner._complete_epoch_route_for_managed_result(
            {"failed": True, "error": "provider unavailable"},
            autonomy_state,
        )
        is False
    )
    assert autonomy_state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY] == selection


def test_infrastructure_resume_reuses_durable_epoch_route_without_recharging(
    enabled, monkeypatch, tmp_path
):
    """A fresh process must reuse, not reselect, an unstarted refresh route."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "route-selection-resume")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    first_state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        first_state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        first_state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )

    real_floor = runner.orchestrator_floor.orchestrator_route
    floor_calls: list[str] = []

    def tracked_floor(ctx):
        floor_calls.append(ctx.trigger)
        return real_floor(ctx)

    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_route", tracked_floor)
    first_route = runner._orchestrator_consult("scope-entry", first_state, {})
    assert first_route is not None
    assert runner.campaign_epoch.record_managed_cycle(first_state) == 1
    assert (
        runner._complete_epoch_route_for_managed_result(
            {"failed": True, "error": "provider unavailable"},
            first_state,
        )
        is False
    )

    # Model an actual process restart: no in-memory route-selection token is
    # copied. Campaign hydration must reconstruct it from summary.json.
    resumed_state = _autonomy_state(str(active))
    runner.campaign_epoch.ensure_campaign(resumed_state)
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY in resumed_state

    resumed_route = runner._orchestrator_consult("scope-entry", resumed_state, {})
    assert resumed_route is not None
    assert resumed_route.route == first_route.route
    assert resumed_route.reason == first_route.reason
    assert resumed_route.source == first_route.source
    assert resumed_route.target == first_route.target
    assert runner.campaign_epoch.record_managed_cycle(resumed_state) == 2

    campaign = runner.campaign_epoch.campaign_snapshot()
    assert floor_calls == ["scope-entry"]
    assert campaign["epoch_cycles"] == 2
    assert campaign["no_progress_route_streak"] == 1
    assert [entry["route"] for entry in campaign["epoch_routes"]] == [first_route.route]
    assert len([event for event, _details in events if event[0] == "orchestrator-route"]) == 1
    assert any(event[0] == "campaign-epoch-route-resumed" for event, _details in events)


def test_interrupted_inflight_route_replays_once_without_recharging(enabled, monkeypatch, tmp_path):
    """A crash inside route application must not lose or double-charge the route."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "inflight-route-resume")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    first_state = _autonomy_state(str(active))
    first_state["prover_requested_route"] = {
        "route": "decompose",
        "target_symbol": "demo",
        "active_file": str(active),
    }

    selected = runner._orchestrator_consult("event", first_state, {})

    assert selected is not None and selected.route == "decompose"
    assert "prover_requested_route" not in first_state
    marker = dict(first_state.get(runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY) or {})
    assert marker["route"] == "decompose"
    assert runner.campaign_epoch.campaign_snapshot()["no_progress_route_streak"] == 1

    monkeypatch.setattr(
        runner,
        "_orchestrator_apply_route",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        runner._apply_orchestrator_route_with_completion(
            selected,
            [],
            first_state,
            {},
        )
    assert runner.campaign_epoch.campaign_snapshot()["inflight_route"]["token"] == marker["token"]

    resumed_state = _autonomy_state(str(active))
    # Model a checkpoint that landed after the atomic campaign write but
    # before the process-local one-shot request was popped.
    resumed_state["prover_requested_route"] = {
        "route": "decompose",
        "target_symbol": "demo",
        "active_file": str(active),
    }
    runner.campaign_epoch.ensure_campaign(resumed_state)
    floor_calls = 0

    def forbidden_floor(_ctx):
        nonlocal floor_calls
        floor_calls += 1
        raise AssertionError("unfinished route must resume before a fresh selection")

    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_route", forbidden_floor)
    replay = runner._orchestrator_consult("scope-entry", resumed_state, {})

    assert replay is not None and replay.route == "decompose"
    assert "prover_requested_route" not in resumed_state
    assert floor_calls == 0
    assert runner.campaign_epoch.campaign_snapshot()["no_progress_route_streak"] == 1

    def completed_decomposition(_route, _history, state, _live, **_kwargs):
        runner._record_orchestrator_route_execution(
            state,
            runner.route_execution.RouteExecution.recorded(
                route="decompose",
                target_symbol="demo",
                active_file=str(active),
                outcome="helper inserted",
                evidence_kind="decomposition-helper",
            ),
        )
        return "continue"

    monkeypatch.setattr(runner, "_orchestrator_apply_route", completed_decomposition)
    assert (
        runner._apply_orchestrator_route_with_completion(replay, [], resumed_state, {})
        == "continue"
    )
    assert "inflight_route" not in runner.campaign_epoch.campaign_snapshot()

    after_completion = _autonomy_state(str(active))
    runner.campaign_epoch.ensure_campaign(after_completion)
    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: OrchestratorRoute(
            route="direct-prove",
            reason="completed route may now be followed by a fresh decision",
        ),
    )
    fresh = runner._orchestrator_consult("scope-entry", after_completion, {})

    assert fresh is not None and fresh.route == "direct-prove"
    assert runner.campaign_epoch.campaign_snapshot()["no_progress_route_streak"] == 2
    assert len([event for event, _details in events if event[0] == "orchestrator-route"]) == 2
    assert (
        len([event for event, _details in events if event[0] == "campaign-inflight-route-resumed"])
        == 1
    )


def test_generated_helper_preflight_drops_nonnegation_inflight_replay(
    enabled, monkeypatch, tmp_path
):
    """A resumed strategy route must not bypass generated-helper falsity screening."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "generated-helper-inflight-preflight")
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("private lemma demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    plan_state.save_queue_manager_state(state)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=plan_state.node_id_for("demo", str(active)),
                    name="demo",
                    file=str(active),
                    status="proving",
                    generated_by="decomposer",
                ),
            )
        )
    )
    runner.campaign_epoch.record_route_decision(
        state,
        route="plan",
        target_symbol="demo",
        active_file=str(active),
        trigger="event",
        route_reason="interrupted weaker-model request",
        reserve_inflight=True,
    )
    stale = dict(state[runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY])
    state["prover_requested_route"] = {
        "route": "plan",
        "target_symbol": "demo",
        "active_file": str(active),
        "reason": "interrupted weaker-model request",
    }

    selected = runner._orchestrator_consult("scope-entry", state, {})

    assert selected is not None and selected.route == "negate"
    current = runner.campaign_epoch.campaign_snapshot()["inflight_route"]
    assert current["route"] == "negate"
    assert current["token"] != stale["token"]
    assert "prover_requested_route" not in state
    assert any(
        event[0] == "generated-helper-negation-preflight-superseded-replay"
        for event, _details in events
    )


def test_generated_helper_preflight_supersedes_nonnegation_epoch_selection(
    enabled, monkeypatch, tmp_path
):
    """A pending fresh-epoch route must yield to generated-helper falsity screening."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "generated-helper-epoch-preflight")
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("private lemma demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    plan_state.save_queue_manager_state(state)
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=plan_state.node_id_for("demo", str(active)),
                    name="demo",
                    file=str(active),
                    status="proving",
                    generated_by="planner",
                ),
            )
        )
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.record_route_decision(
        state,
        route="plan",
        target_symbol="demo",
        active_file=str(active),
        trigger="scope-entry",
        route_reason="stale fresh-epoch selection",
    )
    stale = dict(state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY])

    selected = runner._orchestrator_consult("scope-entry", state, {})

    assert selected is not None and selected.route == "negate"
    current = state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY]
    assert current["route"] == "negate"
    assert current["reason"] != stale["reason"]
    assert current["target"]["generated_by"] == "planner"
    assert any(
        event[0] == "generated-helper-negation-preflight-superseded-replay"
        for event, _details in events
    )


def test_resumed_inflight_route_drops_when_assignment_changed(enabled, monkeypatch, tmp_path):
    """A pending route from an old theorem must not run against the new queue item."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "inflight-route-stale-scope")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem demo : True := by\n  sorry\n\ntheorem next_demo : True := by\n  sorry\n",
        encoding="utf-8",
    )
    first_state = _autonomy_state(str(active))
    first_state["prover_requested_route"] = {
        "route": "decompose",
        "target_symbol": "demo",
        "active_file": str(active),
    }
    selected = runner._orchestrator_consult("event", first_state, {})
    assert selected is not None and selected.route == "decompose"

    resumed_state = {
        "current_queue_assignment": {
            "target_symbol": "next_demo",
            "active_file": str(active),
            "slice": "theorem next_demo : True := by\n  sorry",
        }
    }
    runner.campaign_epoch.ensure_campaign(resumed_state)
    assert (
        runner.campaign_epoch.reusable_inflight_route(
            resumed_state,
            target_symbol="next_demo",
            active_file=str(active),
        )
        == {}
    )
    assert "inflight_route" not in runner.campaign_epoch.campaign_snapshot()

    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: OrchestratorRoute(
            route="direct-prove",
            reason="new assignment receives a fresh route",
        ),
    )
    fresh = runner._orchestrator_consult("scope-entry", resumed_state, {})

    assert fresh is not None and fresh.route == "direct-prove"
    current = runner.campaign_epoch.campaign_snapshot()["inflight_route"]
    assert current["target_symbol"] == "next_demo"
    assert current["route"] == "direct-prove"


def test_direct_prove_marker_waits_for_managed_turn(enabled, monkeypatch, tmp_path):
    """A prompt-level direct route is incomplete until its prover turn returns."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "inflight-direct-prove")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))

    route = runner._orchestrator_consult("scope-entry", state, {})

    assert route is not None and route.route == "direct-prove"
    assert runner._apply_orchestrator_route_with_completion(route, [], state, {}) == "noop"
    assert runner.campaign_epoch.campaign_snapshot()["inflight_route"]["route"] == "direct-prove"
    assert runner._complete_epoch_route_for_managed_result({"messages": []}, state) is True
    assert "inflight_route" not in runner.campaign_epoch.campaign_snapshot()


def test_resumed_epoch_route_uses_unused_negation_kind_after_inconclusive_probe(
    enabled, monkeypatch, tmp_path
):
    """The live epoch-22 portfolio reserves and resumes its unused route kind."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "route-selection-viable-resume")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_BUDGET", "2")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    for route in ("plan", "decompose", "plan", "plan"):
        runner.campaign_epoch.record_route_decision(
            state,
            route=route,
            target_symbol="demo",
            active_file=str(active),
            limit=99,
        )
    runner.campaign_epoch.roll_epoch(
        state,
        reason=runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        cycle=4,
        target_symbol="demo",
        active_file=str(active),
    )
    storage_key = TheoremKey.make("demo", str(active)).storage_key()
    monkeypatch.setattr(
        runner.plan_state,
        "load_summary",
        lambda: {
            "negation_probes": [{"key": storage_key, "negation": {"verdict": "inconclusive"}}]
        },
    )

    selected = runner._orchestrator_consult("scope-entry", state, {})
    assert selected is not None and selected.route == "negate"
    assert (
        runner._complete_epoch_route_for_managed_result(
            {"failed": True, "error": "provider unavailable"},
            state,
        )
        is False
    )

    resumed = _autonomy_state(str(active))
    runner.campaign_epoch.ensure_campaign(resumed)
    replay = runner._orchestrator_consult("scope-entry", resumed, {})
    assert replay is not None and replay.route == "negate"
    assert _complete_mechanical_selection(replay, resumed, str(active)) is True

    after_completion = _autonomy_state(str(active))
    campaign = runner.campaign_epoch.ensure_campaign(after_completion)
    assert campaign["epoch_route_refresh"]["required"] is False
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in after_completion
    resumed_events = [
        event for event, _details in events if event[0] == "campaign-epoch-route-resumed"
    ]
    assert len(resumed_events) == 1


def test_fresh_epoch_selected_negate_forces_probe_before_scoped_attempt_threshold(
    enabled, monkeypatch, tmp_path
):
    """Execute an exact selected negate route even with zero helper-local failures."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "fresh-epoch-negate-force")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_BUDGET", "2")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    for route_name in ("plan", "decompose", "plan", "plan"):
        runner.campaign_epoch.record_route_decision(
            state,
            route=route_name,
            target_symbol="demo",
            active_file=str(active),
            limit=99,
        )
    runner.campaign_epoch.roll_epoch(
        state,
        reason=runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        cycle=4,
        target_symbol="demo",
        active_file=str(active),
    )
    storage_key = TheoremKey.make("demo", str(active)).storage_key()
    monkeypatch.setattr(
        runner.plan_state,
        "load_summary",
        lambda: {
            "negation_probes": [{"key": storage_key, "negation": {"verdict": "inconclusive"}}]
        },
    )

    selected = runner._orchestrator_consult("scope-entry", state, {})

    assert selected is not None and selected.route == "negate"
    selected_at = state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY]["selected_at"]
    calls: list[dict[str, Any]] = []

    def probe(_autonomy_state, *, target_symbol, active_file, **kwargs):
        calls.append(kwargs)
        return runner.route_execution.RouteExecution.recorded(
            route="negate",
            target_symbol=target_symbol,
            active_file=active_file,
            outcome="inconclusive",
            evidence_kind="negation-probe",
        )

    monkeypatch.setattr(runner, "_maybe_negation_probe", probe)

    assert runner._apply_orchestrator_route_with_completion(selected, [], state, {}) == "continue"
    assert calls == [
        {
            "force": True,
            "source_recovery_only": False,
            "trigger": "orchestrator-route",
            "route_reason": "",
            "selected_at": selected_at,
        }
    ]
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in state
    assert runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY not in state


def test_inconclusive_negation_refresh_is_once_per_durable_evidence(enabled, monkeypatch, tmp_path):
    """E22 negate must not recur in E24 until its probe evidence changes."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "negation-refresh-once")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_BUDGET", "2")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    declaration = "theorem demo : True := by\n  sorry\n"
    active.write_text(declaration, encoding="utf-8")
    state = _autonomy_state(str(active))
    runner.campaign_epoch.ensure_campaign(state)
    storage_key = TheoremKey.make("demo", str(active)).storage_key()

    def seed_probe(source_revision: str) -> None:
        def mutate(summary):
            summary["negation_probes"] = [
                {
                    "key": storage_key,
                    "negation": {"verdict": "inconclusive"},
                    "promotion_evidence": {
                        "declaration_signature_sha256": hashlib.sha256(
                            b"theorem demo : True"
                        ).hexdigest(),
                        "source_revision_sha256": source_revision,
                    },
                }
            ]

        update_json_file(runner.plan_state.plan_state_paths().summary_json, mutate)

    def spend_epoch(routes: tuple[str, ...]) -> None:
        for route in routes:
            runner.campaign_epoch.record_route_decision(
                state,
                route=route,
                target_symbol="demo",
                active_file=str(active),
                limit=99,
            )
        runner.campaign_epoch.roll_epoch(
            state,
            reason=runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
            cycle=4,
            target_symbol="demo",
            active_file=str(active),
        )

    seed_probe("revision-a")
    spend_epoch(("decompose", "plan", "plan", "plan"))
    first = runner._orchestrator_consult("scope-entry", state, {})
    assert first is not None and first.route == "negate"
    assert _complete_mechanical_selection(first, state, str(active)) is True
    state = _autonomy_state(str(active))
    runner.campaign_epoch.ensure_campaign(state)
    assert len(state[runner.campaign_epoch.EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY]) == 1

    # Two later epochs carry the same inconclusive evidence. Neither may
    # reopen negate merely because its immediately prior portfolio omitted it.
    spend_epoch(("decompose", "plan", "plan"))
    second = runner._orchestrator_consult("scope-entry", state, {})
    assert second is not None and second.route != "negate"
    assert _complete_mechanical_selection(second, state, str(active)) is True
    spend_epoch(("plan", "decompose", "plan"))
    third = runner._orchestrator_consult("scope-entry", state, {})
    assert third is not None and third.route != "negate"

    # A new probe/source identity gets exactly one fresh retry allowance.
    assert _complete_mechanical_selection(third, state, str(active)) is True
    seed_probe("revision-b")
    spend_epoch(("plan", "decompose", "plan"))
    refreshed = runner._orchestrator_consult("scope-entry", state, {})
    assert refreshed is not None and refreshed.route == "negate"


def test_semantic_refresh_under_fresh_epoch_uses_immediate_inflight_completion(
    enabled,
    monkeypatch,
    tmp_path,
):
    """Internal refresh never becomes an unstartable fresh-route selection."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "semantic-refresh-fresh-selection")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    for route_name in ("decompose", "negate", "plan"):
        runner.campaign_epoch.record_route_decision(
            state,
            route=route_name,
            target_symbol="demo",
            active_file=str(active),
            limit=99,
        )
    runner.campaign_epoch.roll_epoch(
        state,
        reason=runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        cycle=3,
        target_symbol="demo",
        active_file=str(active),
    )

    selected = runner._orchestrator_consult("scope-entry", state, {})

    assert selected is not None
    assert selected.route == runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    assert runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY not in state
    inflight = state[runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY]
    assert inflight["route"] == runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    assert runner._apply_orchestrator_route_with_completion(selected, [], state, {}) == "continue"
    assert runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY not in state
    assert state["campaign_epoch_requested"] == "semantic-route-portfolio-exhausted"
    assert "inflight_route" not in runner.campaign_epoch.campaign_snapshot()


def test_semantic_refresh_inflight_replays_once_and_completes_after_restart(
    enabled,
    monkeypatch,
    tmp_path,
):
    """A crash before internal refresh application replays and retires the exact action."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "semantic-refresh-inflight-resume")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    first_state = _autonomy_state(str(active))
    for route_name in ("decompose", "negate", "plan"):
        runner.campaign_epoch.record_route_decision(
            first_state,
            route=route_name,
            target_symbol="demo",
            active_file=str(active),
            limit=99,
        )
    runner.campaign_epoch.roll_epoch(
        first_state,
        reason=runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        cycle=3,
        target_symbol="demo",
        active_file=str(active),
    )
    selected = runner._orchestrator_consult("scope-entry", first_state, {})
    assert selected is not None
    assert selected.route == runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    charged_routes = first_state["orchestrator_routes_used"]
    assert runner.campaign_epoch.campaign_snapshot()["inflight_route"]["route"] == (
        runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    )

    resumed = _autonomy_state(str(active))
    runner.campaign_epoch.ensure_campaign(resumed)
    assert resumed[runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY]["route"] == (
        runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    )
    replay = runner._orchestrator_consult("scope-entry", resumed, {})

    assert replay is not None
    assert replay.route == runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    assert replay.source == "deterministic-semantic-admission"
    assert any(args and args[0] == "campaign-inflight-route-resumed" for args, _kwargs in events)
    assert resumed["orchestrator_routes_used"] == charged_routes
    assert runner._apply_orchestrator_route_with_completion(replay, [], resumed, {}) == "continue"
    assert runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY not in resumed
    assert resumed["campaign_epoch_requested"] == "semantic-route-portfolio-exhausted"


def test_semantic_refresh_next_epoch_runs_real_route_before_another_refresh(
    enabled,
    monkeypatch,
    tmp_path,
):
    """A refreshed unchanged ledger cannot create a zero-work rollover loop."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "semantic-refresh-work-opportunity")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    for route_name in ("decompose", "negate", "plan"):
        runner.campaign_epoch.record_route_decision(
            state,
            route=route_name,
            target_symbol="demo",
            active_file=str(active),
            limit=99,
        )
    runner.campaign_epoch.roll_epoch(
        state,
        reason=runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        cycle=3,
        target_symbol="demo",
        active_file=str(active),
    )
    refresh = runner._orchestrator_consult("scope-entry", state, {})
    assert refresh is not None
    assert refresh.route == runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    assert runner._apply_orchestrator_route_with_completion(refresh, [], state, {}) == "continue"

    runner.campaign_epoch.roll_epoch(
        state,
        reason=runner.campaign_epoch.consume_rollover_request(state),
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )
    real_route = runner._orchestrator_consult("scope-entry", state, {})

    assert real_route is not None
    assert real_route.route in runner.campaign_epoch.EPOCH_REFRESH_ALLOWED_ROUTES
    assert real_route.route != runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    assert real_route.target["semantic_refresh_work_due"] is True
    assert "campaign_epoch_requested" not in state
    selection = dict(state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY])
    assert selection["route"] == real_route.route
    charged_routes = state["orchestrator_routes_used"]

    # A crash before observable route work replays the exact token without
    # charging the campaign or falling back into another internal refresh.
    resumed = _autonomy_state(str(active))
    runner.campaign_epoch.ensure_campaign(resumed)
    replay = runner._orchestrator_consult("scope-entry", resumed, {})
    assert replay is not None
    assert replay.route == real_route.route
    assert resumed["orchestrator_routes_used"] == charged_routes
    assert resumed[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY]["token"] == (
        selection["token"]
    )


def test_spent_negation_budget_selects_and_applies_non_negate_refresh(
    enabled, monkeypatch, tmp_path
):
    """A default-budget probe row must not spend a fresh epoch on a no-op retry."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "spent-negation-refresh")
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_BUDGET", "1")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(runner.planner_phase, "planner_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "_maybe_negation_probe",
        lambda *_args, **_kwargs: pytest.fail("spent negate route must not reach the executor"),
    )
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    storage_key = TheoremKey.make("demo", str(active)).storage_key()

    def seed_probe(summary):
        summary["negation_probes"] = [
            {
                "key": storage_key,
                "negation": {"verdict": "inconclusive"},
            }
        ]

    update_json_file(runner.plan_state.plan_state_paths().summary_json, seed_probe)
    for route in ("decompose", "plan", "plan", "decompose"):
        runner.campaign_epoch.record_route_decision(
            state,
            route=route,
            target_symbol="demo",
            active_file=str(active),
            limit=99,
        )
    runner.campaign_epoch.roll_epoch(
        state,
        reason=runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        cycle=4,
        target_symbol="demo",
        active_file=str(active),
    )

    selected = runner._orchestrator_consult("scope-entry", state, {})

    assert selected is not None
    assert selected.route == runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    history: list[dict[str, Any]] = []
    assert runner._apply_orchestrator_route_with_completion(selected, history, state, {}) == (
        "continue"
    )
    assert any("[LEANFLOW SEMANTIC PORTFOLIO REFRESH]" in item["content"] for item in history)
    assert runner.campaign_epoch.INFLIGHT_ROUTE_STATE_KEY not in state
    assert state["campaign_epoch_requested"] == "semantic-route-portfolio-exhausted"


def test_completed_epoch_route_rotates_same_semantic_fresh_event(enabled, monkeypatch, tmp_path):
    """Completing a route does not make the same no-progress intent novel."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "route-selection-completed")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    runner.campaign_epoch.record_route_decision(
        state,
        route="direct-prove",
        target_symbol="demo",
        active_file=str(active),
    )
    runner.campaign_epoch.roll_epoch(
        state,
        reason="context-pressure",
        cycle=1,
        target_symbol="demo",
        active_file=str(active),
    )

    selected = runner._orchestrator_consult("scope-entry", state, {})
    assert selected is not None
    assert _complete_mechanical_selection(selected, state, str(active)) is True
    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: OrchestratorRoute(
            route=selected.route,
            reason="fresh event independently chose the same route",
        ),
    )

    repeated = runner._orchestrator_consult("event", state, {})

    assert repeated is not None
    assert repeated.route != selected.route
    assert repeated.source == "deterministic-semantic-admission"
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 2
    assert [entry["route"] for entry in campaign["epoch_routes"]] == [
        selected.route,
        repeated.route,
    ]


def test_consult_records_activity_and_charges_route_budget(enabled, monkeypatch, tmp_path):
    events = _events(monkeypatch)
    journal_events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        plan_state,
        "append_journal_event",
        lambda event: journal_events.append(dict(event)),
    )
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))

    route = runner._orchestrator_consult("stall", autonomy_state, {})

    assert route is not None
    assert route.route == "decompose"  # stall with an active item
    assert autonomy_state["orchestrator_routes_used"] == 1
    consults = [(a, k) for a, k in events if a[0] == "orchestrator-route"]
    assert len(consults) == 1
    assert consults[0][1]["route"] == "decompose"
    assert journal_events[0]["name"] == "demo"
    assert journal_events[0]["file"] == str(active)

    # The mechanical marker retires only when exact durable work is observed.
    def completed_decomposition(_route, _history, state, _live, **_kwargs):
        runner._record_orchestrator_route_execution(
            state,
            runner.route_execution.RouteExecution.recorded(
                route="decompose",
                target_symbol="demo",
                active_file=str(active),
                outcome="fallback recorded",
                evidence_kind="decomposition-fallback",
            ),
        )
        return "continue"

    monkeypatch.setattr(runner, "_orchestrator_apply_route", completed_decomposition)
    assert (
        runner._apply_orchestrator_route_with_completion(route, [], autonomy_state, {})
        == "continue"
    )
    passthrough = runner._orchestrator_consult("scope-entry", autonomy_state, {})
    assert passthrough.route == "direct-prove"
    assert autonomy_state["orchestrator_routes_used"] == 2


def test_explicit_prover_route_triggers_once_and_skips_llm(enabled, monkeypatch, tmp_path):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))
    autonomy_state["prover_requested_route"] = {
        "route": "plan",
        "target_symbol": "demo",
        "active_file": str(active),
    }
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: True)
    monkeypatch.setattr(
        runner.orchestrator_llm,
        "llm_route",
        lambda *args, **kwargs: pytest.fail("explicit route must remain deterministic"),
    )

    assert runner._orchestrator_event_due(autonomy_state, 1) == "event"
    route = runner._orchestrator_consult("event", autonomy_state, {})

    assert route is not None
    assert route.route == "plan"
    assert "prover_requested_route" not in autonomy_state
    assert runner._orchestrator_event_due(autonomy_state, 1) == ""
    consults = [(a, k) for a, k in events if a[0] == "orchestrator-route"]
    assert len(consults) == 1
    assert consults[0][1]["route"] == "plan"
    assert autonomy_state["orchestrator_routes_used"] == 1


@pytest.mark.parametrize("requested_route", ["decompose", "negate"])
def test_explicit_non_plan_route_triggers_once_and_skips_llm(
    enabled, monkeypatch, tmp_path, requested_route: str
):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))
    autonomy_state["prover_requested_route"] = {
        "route": requested_route,
        "target_symbol": "demo",
        "active_file": str(active),
        "reason": (
            "requested route: negate; s = 3 refutes this helper"
            if requested_route == "negate"
            else "requested route: decompose; split the residual cases"
        ),
    }
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: True)
    monkeypatch.setattr(
        runner.orchestrator_llm,
        "llm_route",
        lambda *args, **kwargs: pytest.fail("explicit route must remain deterministic"),
    )

    assert runner._orchestrator_event_due(autonomy_state, 1) == "event"
    route = runner._orchestrator_consult("event", autonomy_state, {})

    assert route is not None
    assert route.route == requested_route
    assert route.target["prover_requested_route"] == requested_route
    assert "requested route" in route.target["prover_request_reason"]
    assert "prover_requested_route" not in autonomy_state
    assert runner._orchestrator_event_due(autonomy_state, 1) == ""
    consults = [(a, k) for a, k in events if a[0] == "orchestrator-route"]
    assert len(consults) == 1
    assert consults[0][1]["route"] == requested_route
    assert autonomy_state["orchestrator_routes_used"] == 1


def test_explicit_decompose_outranks_stale_epoch_negate_in_same_consult(
    enabled, monkeypatch, tmp_path
):
    """Do not log a fresh epoch route after claiming its replay was superseded."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    state["prover_requested_route"] = {
        "route": "decompose",
        "target_symbol": "demo",
        "active_file": str(active),
        "reason": "requested route: decompose; split the remaining residue classes",
    }
    state[runner.campaign_epoch.EPOCH_ROUTE_REFRESH_STATE_KEY] = {
        "required": True,
        "previous_routes": ["decompose"],
    }
    stale_negate = {
        "route": "negate",
        "target_symbol": "demo",
        "active_file": str(active),
        "token": "epoch-negate",
        "epoch": 42,
    }
    monkeypatch.setattr(runner, "plan_state_enabled", lambda: False)
    monkeypatch.setattr(runner.campaign_epoch, "ensure_campaign", lambda _state: {})
    monkeypatch.setattr(
        runner.campaign_epoch,
        "reusable_inflight_route",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        runner.campaign_epoch,
        "reusable_epoch_route_selection",
        lambda *_args, **_kwargs: dict(stale_negate),
    )
    monkeypatch.setattr(
        runner.campaign_epoch,
        "record_route_decision",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: False)
    monkeypatch.setattr(runner, "_queue_manager_from_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner.plan_state, "append_journal_event", lambda *_args: None)

    selected = runner._orchestrator_consult("event", state, {})

    assert selected is not None and selected.route == "decompose"
    assert "prover_requested_route" not in state
    assert state[runner.campaign_epoch.EPOCH_ROUTE_SELECTION_STATE_KEY] == stale_negate
    assert [args[0] for args, _kwargs in events] == [
        "prover-route-request-superseded-replay",
        "orchestrator-route",
    ]
    assert events[-1][1]["route"] == "decompose"


@pytest.mark.parametrize("requested_route", ["decompose", "negate"])
@pytest.mark.parametrize("scope_mismatch", ["target", "file"])
def test_stale_non_plan_route_request_is_dropped_once(
    enabled, monkeypatch, tmp_path, requested_route: str, scope_mismatch: str
):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))
    autonomy_state["prover_requested_route"] = {
        "route": requested_route,
        "target_symbol": "previous_demo" if scope_mismatch == "target" else "demo",
        "active_file": (
            str(tmp_path / "Previous.lean") if scope_mismatch == "file" else str(active)
        ),
    }

    assert runner._orchestrator_event_due(autonomy_state, 1) == ""
    assert "prover_requested_route" not in autonomy_state
    assert runner._orchestrator_event_due(autonomy_state, 1) == ""
    dropped = [
        (args, kwargs) for args, kwargs in events if args[0] == "prover-route-request-dropped"
    ]
    assert len(dropped) == 1
    assert dropped[0][1]["route"] == requested_route
    assert dropped[0][1]["reason"] == "assignment-mismatch"


def test_explicit_spent_negate_request_falls_back_once(enabled, monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    state["prover_requested_route"] = {
        "route": "negate",
        "target_symbol": "demo",
        "active_file": str(active),
    }
    storage_key = TheoremKey.make("demo", str(active)).storage_key()

    def seed_probe(summary):
        summary["negation_probes"] = [{"key": storage_key, "negation": {"verdict": "inconclusive"}}]

    update_json_file(runner.plan_state.plan_state_paths().summary_json, seed_probe)
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_BUDGET", "1")
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: True)
    monkeypatch.setattr(
        runner.orchestrator_llm,
        "llm_route",
        lambda *args, **kwargs: pytest.fail("explicit route must remain deterministic"),
    )

    selected = runner._orchestrator_consult("event", state, {})

    assert selected is not None and selected.route == "decompose"
    assert "budget is exhausted" in selected.reason
    assert "prover_requested_route" not in state
    assert runner._orchestrator_event_due(state, 1) == ""


def test_failed_explicit_route_consult_retains_request_for_retry(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))
    autonomy_state["prover_requested_route"] = {
        "route": "decompose",
        "target_symbol": "demo",
        "active_file": str(active),
    }
    real_route = runner.orchestrator_floor.orchestrator_route
    attempts = 0

    def flaky_route(ctx):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient route construction failure")
        return real_route(ctx)

    monkeypatch.setattr(runner.orchestrator_floor, "orchestrator_route", flaky_route)

    assert runner._orchestrator_consult("event", autonomy_state, {}) is None
    assert autonomy_state["prover_requested_route"]["route"] == "decompose"
    assert runner._orchestrator_event_due(autonomy_state, 1) == "event"

    retried = runner._orchestrator_consult("event", autonomy_state, {})
    assert retried is not None and retried.route == "decompose"
    assert "prover_requested_route" not in autonomy_state
    assert runner._orchestrator_event_due(autonomy_state, 1) == ""


@pytest.mark.parametrize("requested_route", ["decompose", "negate"])
def test_failed_route_record_retains_explicit_request_for_retry(
    enabled, monkeypatch, tmp_path, requested_route: str
):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))
    autonomy_state["prover_requested_route"] = {
        "route": requested_route,
        "target_symbol": "demo",
        "active_file": str(active),
    }
    real_record = runner.campaign_epoch.record_route_decision
    attempts = 0

    def flaky_record(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient route record failure")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(runner.campaign_epoch, "record_route_decision", flaky_record)

    assert runner._orchestrator_consult("event", autonomy_state, {}) is None
    assert autonomy_state["prover_requested_route"]["route"] == requested_route
    assert runner._orchestrator_event_due(autonomy_state, 1) == "event"

    retried = runner._orchestrator_consult("event", autonomy_state, {})
    assert retried is not None and retried.route == requested_route
    assert "prover_requested_route" not in autonomy_state
    assert autonomy_state["orchestrator_routes_used"] == 1


def test_requested_route_fourth_decision_requests_campaign_rollover(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))
    autonomy_state["orchestrator_routes_used"] = 3
    autonomy_state["prover_requested_route"] = {
        "route": "plan",
        "target_symbol": "demo",
        "active_file": str(active),
    }

    route = runner._orchestrator_consult("event", autonomy_state, {})

    assert route is not None and route.route == "plan"
    assert autonomy_state["orchestrator_routes_used"] == 4
    assert (
        autonomy_state["campaign_epoch_requested"]
        == runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON
    )


def test_spent_prove_route_budget_refresh_is_llm_immutable(enabled, monkeypatch, tmp_path):
    """An advisory model cannot execute a fifth route before the due rollover."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    for route_name in ("direct-prove", "decompose", "negate", "plan"):
        runner.campaign_epoch.record_route_decision(
            state,
            route=route_name,
            target_symbol="demo",
            active_file=str(active),
            limit=99,
        )
    monkeypatch.setattr(runner.orchestrator_llm, "orchestrator_llm_enabled", lambda: True)
    monkeypatch.setattr(
        runner.orchestrator_llm,
        "llm_route",
        lambda *args, **kwargs: pytest.fail("portfolio refresh must remain deterministic"),
    )

    selected = runner._orchestrator_consult("event", state, {})

    assert selected is not None
    assert selected.route == runner.orchestrator_floor.SEMANTIC_REFRESH_ROUTE
    assert selected.target["campaign_rollover_reason"] == (
        runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON
    )


def test_search_boundary_plan_preserves_and_forwards_exact_target_scope(
    enabled, monkeypatch, tmp_path
):
    """Consuming the search boundary must not erase the planner's assignment evidence."""
    _events(monkeypatch)
    active = tmp_path / "FormalConjectures" / "ErdosProblems" / "242.lean"
    active.parent.mkdir(parents=True)
    declaration = (
        "private lemma erdos_242_residual_mod_seven_eq_five (k : ℕ) "
        "(hk : k % 7 = 5) : ∃ x y z, (4 : ℚ) / (24 * k + 1) = "
        "1 / x + 1 / y + 1 / z := by\n  sorry"
    )
    active.write_text(declaration + "\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "erdos_242_residual_mod_seven_eq_five",
            "active_file": str(active),
            "slice": declaration,
        },
        "prover_requested_route": {
            "route": "plan",
            "target_symbol": "erdos_242_residual_mod_seven_eq_five",
            "active_file": str(active),
        },
        "search_progress": {
            "target_symbol": "erdos_242_residual_mod_seven_eq_five",
            "active_file": str(active),
            "search_count": 12,
            "same_query_streak": 1,
            "unique_queries": ["erdos straus n = 168 t + 121"],
            "used_tools": {"lean_search": 7, "web_search": 5},
            "last_query": "erdos straus n = 168 t + 121",
            "hard_route_requested": True,
            "synthesis_grace_pending": True,
        },
        "failed_attempts": [
            {
                "target_symbol": "erdos_242_residual_mod_seven_eq_five",
                "active_file": str(active),
                "attempt": 1,
                "proof_shape": "search-only route",
                "reason": "no checked construction found",
            }
        ],
    }
    live_state = {
        "target_symbol": "erdos_242_residual_mod_seven_eq_five",
        "active_file": str(active),
        "goals": ("k : ℕ\nhk : k % 7 = 5\n" "⊢ ∃ x y z, (4 : ℚ) / (168 * (k / 7) + 121) = _"),
    }

    route = runner._orchestrator_consult("event", autonomy_state, live_state)
    assert route is not None and route.route == "plan"
    assert autonomy_state["search_progress"]["search_count"] == 12
    assert autonomy_state["search_progress"]["synthesis_grace_pending"] is True

    planner_calls: list[dict[str, Any]] = []

    def fake_planner(**kwargs):
        planner_calls.append(kwargs)
        return runner.planner_phase.PlannerOutcome(ok=True, reason="scoped", synthesis_status="ok")

    monkeypatch.setattr(runner.planner_phase, "planner_enabled", lambda: True)
    monkeypatch.setattr(runner.planner_phase, "run_planner_phase", fake_planner)

    action = runner._orchestrator_apply_route(
        route,
        [],
        autonomy_state,
        live_state,
        agent=object(),
    )

    assert action == "continue"
    call = planner_calls[0]
    assert call["target_symbol"] == "erdos_242_residual_mod_seven_eq_five"
    assert call["active_file"] == str(active)
    assert "24 * k + 1" in call["declaration_slice"]
    assert "168 * (k / 7) + 121" in call["lean_goal"]
    assert call["requested_route"] == "plan"
    assert "search-only route" in call["failed_route_signature"]
    assert '"search_count":12' in call["search_signature"]


def test_scope_consult_marks_included_research_findings_seen(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    summary = {
        "dispatch_ledger": [
            {
                "spec": {
                    "job_id": "campaign.ds-001",
                    "inputs": {"target_symbol": "demo", "active_file": str(active)},
                }
            }
        ],
        "research_findings": [
            {
                "job_id": "campaign.ds-001",
                "deliverable": {"summary": "try invariant h"},
            }
        ],
    }
    monkeypatch.setattr(runner.plan_state, "load_summary", lambda: summary)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", runner.plan_state.Blueprint)
    autonomy_state = _autonomy_state(str(active))

    route = runner._orchestrator_consult("scope-entry", autonomy_state, {})

    assert route is not None
    assert autonomy_state["orchestrator_jobs_seen"] == [
        runner.research_findings.delivery_key("campaign.ds-001", "demo")
    ]
    assert runner._orchestrator_event_due(autonomy_state, 1) == ""


def test_apply_strategy_route_appends_directive_and_resumes(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))
    # Simulate an armed breakpoint with a persisted packet + spent tranche.
    mgr = TheoremQueueManager.from_autonomy_state(autonomy_state)
    key = TheoremKey.make("demo", str(active))
    mgr.add_api_steps_for(key, 640)
    runner._flush_queue_manager(autonomy_state, mgr)
    plan_state.record_decision_packet(
        {"packet_id": "bp-9", "scope": "theorem", "node_id": "", "target_symbol": "demo"}
    )
    autonomy_state["budget_breakpoint"] = {"packet_id": "bp-9", "scope": "theorem"}
    autonomy_state["consecutive_exhausted_assignments"] = 2
    autonomy_state["_orchestrator_last_ctx"] = {
        "target_symbol": "demo",
        "active_file": str(active),
    }
    history: list[dict[str, Any]] = []

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="decompose", reason="test"),
        history,
        autonomy_state,
        {},
    )

    assert action == "continue"
    assert "[LEANFLOW ORCHESTRATOR ROUTE: decompose]" in history[-1]["content"]
    # Breakpoint disarmed, streak reset, fresh tranche granted.
    assert "budget_breakpoint" not in autonomy_state
    assert autonomy_state["consecutive_exhausted_assignments"] == 0
    refreshed = TheoremQueueManager.from_autonomy_state(autonomy_state)
    assert refreshed.api_steps_for(key) == 0
    # Packet decided as split by the floor.
    packets = plan_state.load_summary()["decision_packets"]
    assert packets[0]["decision"] == "split"
    assert packets[0]["decided_by"] == "orchestrator-floor"


def test_apply_park_writes_documented_report_and_stops(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}
    }

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="park", reason="route budget spent"),
        [],
        autonomy_state,
        {},
    )

    assert action == "stop:parked"
    assert plan_state.load_summary()["final_report"]["status"] == "documented"


def test_apply_route_exhaustion_park_never_stops_prove(enabled, monkeypatch, tmp_path):
    """A stale or resumed difficulty park remains a prove-campaign rollover."""
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}
    }
    history: list[dict[str, Any]] = []

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="park", reason="route budget spent"),
        history,
        autonomy_state,
        {},
    )

    assert action == "continue"
    assert autonomy_state["campaign_epoch_requested"] == (
        runner.campaign_epoch.ROUTE_PORTFOLIO_ROLLOVER_REASON
    )
    assert "do not treat route exhaustion as a proof outcome" in history[-1]["content"]


def test_apply_escalate_rejects_route_without_revalidated_runtime_payload(
    enabled, monkeypatch, tmp_path
):
    events = _events(monkeypatch)
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}
    }

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="escalate", reason="negation proved"),
        [],
        autonomy_state,
        {},
    )

    assert action == "continue"
    assert autonomy_state.get("terminal_outcome") is None
    assert "final_report" not in plan_state.load_summary()
    assert any(args[0] == "orchestrator-escalation-rejected" for args, _kwargs in events)


def test_apply_negate_route_runs_probe_and_resumes(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    probes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner,
        "_maybe_negation_probe",
        lambda autonomy_state, *, target_symbol, active_file, **_kwargs: (
            probes.append((target_symbol, active_file))
            or runner.route_execution.RouteExecution.recorded(
                route="negate",
                target_symbol=target_symbol,
                active_file=active_file,
                outcome="inconclusive",
                evidence_kind="negation-probe",
            )
        ),
    )
    autonomy_state: dict[str, Any] = {
        "budget_breakpoint": {"packet_id": "bp-1", "scope": "theorem"},
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"},
    }

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="negate", reason="no feasibility verdict"),
        [],
        autonomy_state,
        {},
    )

    assert action == "continue"
    assert probes == [("demo", "Demo.lean")]
    assert "budget_breakpoint" not in autonomy_state


def test_verified_counterexample_route_forces_probe_before_retry_threshold(
    enabled, monkeypatch, tmp_path
):
    """Exact parent-verified evidence must not defer to the low-attempt gate."""
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : ∀ n : Nat, n < 5 := by\n  sorry\n", encoding="utf-8")
    target_id = plan_state.node_id_for("demo", str(active))
    helper_id = plan_state.node_id_for("demo_counterexample", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=target_id,
                    name="demo",
                    file=str(active),
                    status="proving",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    name="demo_counterexample",
                    file=str(active),
                    statement=(
                        "private lemma demo_counterexample : ¬ ((5 : Nat) < 5) := by\n" "  omega"
                    ),
                    status="proved",
                ),
            ),
            edges=(
                plan_state.GraphEdge(
                    source=helper_id,
                    target=target_id,
                    kind="evidence",
                ),
            ),
        )
    )
    state = _autonomy_state(str(active))
    context = runner.orchestrator_floor.build_route_context(
        trigger="event",
        autonomy_state=state,
        blueprint=plan_state.load_blueprint(),
        summary=plan_state.load_summary(),
    )
    route = runner.orchestrator_floor.orchestrator_route(context)
    state["_orchestrator_last_ctx"] = {
        "target_symbol": "demo",
        "active_file": str(active),
    }
    calls: list[dict[str, Any]] = []

    def probe(_autonomy_state, *, target_symbol, active_file, **kwargs):
        calls.append(kwargs)
        return runner.route_execution.RouteExecution.recorded(
            route="negate",
            target_symbol=target_symbol,
            active_file=active_file,
            outcome="inconclusive",
            evidence_kind="negation-probe",
        )

    monkeypatch.setattr(runner, "_maybe_negation_probe", probe)
    history: list[dict[str, Any]] = []

    assert route.route == "negate"
    assert runner._orchestrator_apply_route(route, history, state, {}) == "continue"
    assert calls[0]["force"] is True
    assert calls[0]["trigger"] == "orchestrator-verified-counterexample"
    assert "false sublemma" in str(history[-1]["content"])


def test_spent_scratch_budget_still_recovers_verified_source_negation(
    enabled, monkeypatch, tmp_path
):
    """Scratch capacity cannot hide an authoritative source-helper promotion."""
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_BUDGET", "1")
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma not_demo : ¬ True := by simp\n" "theorem demo : True := by\n  sorry\n",
        encoding="utf-8",
    )
    target_id = plan_state.node_id_for("demo", str(active))
    helper_id = plan_state.node_id_for("not_demo", str(active))
    parent_id = plan_state.node_id_for("parent_demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=target_id,
                    name="demo",
                    file=str(active),
                    statement="theorem demo : True := by\n  sorry",
                    status="proving",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    name="not_demo",
                    file=str(active),
                    statement="lemma not_demo : ¬ True := by simp",
                    status="proved",
                ),
                plan_state.GraphNode(
                    id=parent_id,
                    name="parent_demo",
                    file=str(active),
                    status="proving",
                ),
            ),
            edges=(
                plan_state.GraphEdge(source=helper_id, target=target_id, kind="evidence"),
                plan_state.GraphEdge(source=target_id, target=parent_id, kind="split_of"),
            ),
        )
    )
    storage_key = TheoremKey.make("demo", str(active)).storage_key()

    def spend_probe(summary):
        summary["negation_probes"] = [{"key": storage_key, "negation": {"verdict": "inconclusive"}}]

    update_json_file(plan_state.plan_state_paths().summary_json, spend_probe)
    state = _autonomy_state(str(active))
    state["theorem_outcomes"] = {
        TheoremKey.make("not_demo", str(active)).storage_key(): {
            "target_symbol": "not_demo",
            "active_file": str(active),
            "status": "solved",
        }
    }
    context = runner.orchestrator_floor.build_route_context(
        trigger="event",
        autonomy_state=state,
        blueprint=plan_state.load_blueprint(),
        summary=plan_state.load_summary(),
    )
    route = runner.orchestrator_floor.orchestrator_route(context)
    state["_orchestrator_last_ctx"] = {
        "target_symbol": "demo",
        "active_file": str(active),
    }
    promotions: list[dict[str, Any]] = []
    monkeypatch.setattr(runner.negation_probe, "negation_probe_enabled", lambda: True)
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        runner.negation_promotion,
        "promote_source_negation",
        lambda **kwargs: promotions.append(dict(kwargs))
        or runner.negation_promotion.PromotionResult(
            True,
            "promoted",
            node_id=target_id,
            is_main_goal=False,
            evidence={"proof_declaration": "not_demo"},
        ),
    )
    monkeypatch.setattr(runner, "_negation_reconciliation_barrier", lambda _state: False)
    monkeypatch.setattr(runner, "_reconcile_false_decomposition_queue_state", lambda _state: ())
    monkeypatch.setattr(
        runner.negation_probe,
        "run_negation_probe",
        lambda *args, **kwargs: pytest.fail("spent scratch path must recover source first"),
    )
    monkeypatch.setattr(
        runner.campaign_epoch,
        "record_status",
        lambda *args, **kwargs: pytest.fail("a false sublemma cannot terminate the campaign"),
    )

    assert context.negation_probe_budget_remaining == 0
    assert route.route == "negate"
    assert route.target["source_negation_recovery_only"] is True
    assert runner._orchestrator_apply_route(route, [], state, {}) == "continue"
    assert promotions[0]["proof_declaration"] == "not_demo"
    assert state["negation_promotion"]["ok"] is True
    assert "terminal_outcome" not in state


def test_spent_scratch_budget_without_source_promotion_retires_route_nonterminally(
    enabled, monkeypatch, tmp_path
):
    """An exhausted evidence route yields to a fresh strategy without terminating."""
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_BUDGET", "1")
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    target_id = plan_state.node_id_for("demo", str(active))
    helper_id = plan_state.node_id_for("demo_counterexample", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=target_id,
                    name="demo",
                    file=str(active),
                    status="proving",
                ),
                plan_state.GraphNode(
                    id=helper_id,
                    name="demo_counterexample",
                    file=str(active),
                    statement="lemma demo_counterexample : ¬ True := by simp",
                    status="proved",
                ),
            ),
            edges=(plan_state.GraphEdge(source=helper_id, target=target_id, kind="evidence"),),
        )
    )
    storage_key = TheoremKey.make("demo", str(active)).storage_key()

    def spend_probe(summary):
        summary["negation_probes"] = [{"key": storage_key, "negation": {"verdict": "inconclusive"}}]

    update_json_file(plan_state.plan_state_paths().summary_json, spend_probe)
    state = _autonomy_state(str(active))
    context = runner.orchestrator_floor.build_route_context(
        trigger="event",
        autonomy_state=state,
        blueprint=plan_state.load_blueprint(),
        summary=plan_state.load_summary(),
    )
    route = runner.orchestrator_floor.orchestrator_route(context)
    state["_orchestrator_last_ctx"] = {
        "target_symbol": "demo",
        "active_file": str(active),
    }
    monkeypatch.setattr(runner.negation_probe, "negation_probe_enabled", lambda: True)
    monkeypatch.setattr(
        runner.negation_probe,
        "run_negation_probe",
        lambda *args, **kwargs: pytest.fail("source-recovery-only route cannot spend scratch"),
    )

    assert context.negation_probe_budget_remaining == 0
    assert route.route == "negate"
    assert runner._orchestrator_apply_route(route, [], state, {}) == "continue"
    execution = state["_negation_route_execution"]
    assert execution["status"] == "completed"
    assert execution["outcome"] == "budget_exhausted"
    assert execution["evidence_kind"] == "negate-route-obstacle"
    assert "terminal_outcome" not in state


def test_deferred_negate_route_keeps_inflight_marker_until_probe_is_recorded(
    enabled, monkeypatch, tmp_path
):
    """A selected negate route cannot complete or roll epochs on a probe no-op."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "deferred-negate-route")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _autonomy_state(str(active))
    target = {
        "target_symbol": "demo",
        "active_file": str(active),
        "prover_requested_route": "negate",
        "prover_request_reason": "requested route: negate; s = 3 is a counterexample",
    }
    runner.campaign_epoch.record_route_decision(
        state,
        route="negate",
        target_symbol="demo",
        active_file=str(active),
        trigger="event",
        route_reason="explicit negate request",
        route_target=target,
        reserve_inflight=True,
    )
    state["_orchestrator_last_ctx"] = {
        "target_symbol": "demo",
        "active_file": str(active),
    }
    route = OrchestratorRoute(route="negate", reason="explicit negate request", target=target)
    monkeypatch.setattr(
        runner,
        "_maybe_negation_probe",
        lambda *_args, **_kwargs: runner.route_execution.RouteExecution.deferred(
            route="negate",
            target_symbol="demo",
            active_file=str(active),
            reason="probe backend unavailable",
            explicit_request=True,
        ),
    )

    assert runner._apply_orchestrator_route_with_completion(route, [], state, {}) == "deferred"
    pending = runner.campaign_epoch.campaign_snapshot()["inflight_route"]
    assert pending["route"] == "negate"
    assert runner._orchestrator_event_due(state, 1) == "event"
    runner.campaign_epoch.request_rollover(
        state,
        runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON,
    )
    assert runner._consume_ready_campaign_rollover(state, {}) == ""

    monkeypatch.setattr(
        runner,
        "_maybe_negation_probe",
        lambda *_args, **_kwargs: runner.route_execution.RouteExecution.recorded(
            route="negate",
            target_symbol="demo",
            active_file=str(active),
            outcome="inconclusive",
            evidence_kind="negation-probe",
            explicit_request=True,
        ),
    )
    history: list[dict[str, Any]] = []

    assert runner._apply_orchestrator_route_with_completion(route, history, state, {}) == "continue"
    assert "inflight_route" not in runner.campaign_epoch.campaign_snapshot()
    assert runner._consume_ready_campaign_rollover(state, {}) == (
        runner.campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON
    )
    assert "s = 3 is a counterexample" in history[-1]["content"]


def test_apply_negate_stops_immediately_after_main_disproof(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)

    def promote_main(autonomy_state, *, target_symbol, active_file, **_kwargs):
        autonomy_state["terminal_outcome"] = "disproved"
        return runner.route_execution.RouteExecution.recorded(
            route="negate",
            target_symbol=target_symbol,
            active_file=active_file,
            outcome="negation_proved",
            evidence_kind="negation-promotion",
        )

    monkeypatch.setattr(runner, "_maybe_negation_probe", promote_main)
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}
    }

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="negate", reason="test feasibility"),
        [],
        autonomy_state,
        {},
    )

    assert action == "stop:disproved"


def test_event_triggers_fire_once_per_evidence(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    bp = plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(id="n-a", name="helper", file="D.lean", status="proved"),
            plan_state.GraphNode(id="n-b", name="main", file="D.lean", status="stated"),
        ),
        edges=(plan_state.GraphEdge(source="n-b", target="n-a", kind="depends_on"),),
    )
    plan_state.save_blueprint(bp)
    autonomy_state: dict[str, Any] = {}

    assert runner._orchestrator_event_due(autonomy_state, 3) == "event"
    # Same evidence: no second fire.
    assert runner._orchestrator_event_due(autonomy_state, 4) == ""


def test_failed_consult_releases_event_capture_for_next_boundary(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))
    scope = runner._orchestrator_event_scope(autonomy_state)
    runner.orchestrator_event_watermark.publish_once(
        autonomy_state,
        scope=scope,
        source="research-finding:campaign.ds-001::demo",
        reason="completed deep search",
    )

    assert runner._orchestrator_event_due(autonomy_state, 1) == "event"
    monkeypatch.setattr(
        runner.orchestrator_floor,
        "orchestrator_route",
        lambda _ctx: (_ for _ in ()).throw(RuntimeError("temporary route failure")),
    )

    assert runner._orchestrator_consult("event", autonomy_state, {}) is None
    # No source is re-published, but the unacknowledged prefix is claimable.
    assert runner._orchestrator_event_due(autonomy_state, 2) == "event"


def test_assignment_transition_preserves_campaign_route_streak(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    monkeypatch.setattr(
        runner, "_manager_prepare_incremental_queue_item", lambda file, label: {"success": False}
    )
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem demo : True := by\n  sorry\n\ntheorem next_demo : True := by\n  sorry\n",
        encoding="utf-8",
    )
    autonomy_state = _autonomy_state(str(active))
    autonomy_state["orchestrator_routes_used"] = 4
    autonomy_state["orchestrator_scope_entered"] = True

    runner._prepare_queue_assignment_state(
        autonomy_state,
        {
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "active_file": str(active),
            "current_queue_item_slice": "theorem next_demo : True := by\n  sorry",
        },
    )

    # The theorem gets a fresh scope-entry consult, but an assignment change
    # without verified graph progress cannot erase the campaign streak.
    assert autonomy_state["orchestrator_routes_used"] == 4
    assert "orchestrator_scope_entered" not in autonomy_state


def test_breakpoint_defers_terminal_report_to_the_route(enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_BUDGET_BREAKPOINT", "1")
    monkeypatch.setenv("LEANFLOW_THEOREM_BUDGET_STEPS", "50")
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = _autonomy_state(str(active))

    tripped = runner._maybe_trigger_budget_breakpoint(
        {"api_calls": 60}, autonomy_state, {}, phase="autonomous"
    )

    assert tripped is True
    summary = plan_state.load_summary()
    # Packet persisted, but no terminal 'documented' report while the
    # orchestrator may still resume the scope.
    assert summary["decision_packets"]
    assert "final_report" not in summary


def test_consumed_finding_fires_once_without_refiring_siblings(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)

    from leanflow_cli.workflows.workflow_json_io import update_json_file

    def _seed(entries, findings):
        def mutate(payload):
            payload["dispatch_ledger"] = entries
            payload["research_findings"] = findings

        update_json_file(plan_state.plan_state_paths().summary_json, mutate)

    job = lambda jid: {  # noqa: E731
        "spec": {
            "job_id": jid,
            "inputs": {"target_symbol": "demo", "active_file": "Demo.lean"},
        },
        "state": "done",
        "consumed": True,
    }
    finding = lambda jid: {"job_id": jid, "deliverable": {"summary": jid}}  # noqa: E731
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "Demo.lean",
        }
    }
    _seed(
        [job("run.o.np-001"), job("run.o.ds-002")],
        [finding("run.o.np-001"), finding("run.o.ds-002")],
    )
    assert runner._orchestrator_event_due(autonomy_state, 2) == "event"
    assert runner._orchestrator_consult("event", autonomy_state, {}) is not None

    # Persisted consumed findings do not re-fire after the first consult.
    assert runner._orchestrator_event_due(autonomy_state, 3) == ""

    # A genuinely new finding fires once.
    _seed(
        [job("run.o.np-001"), job("run.o.ds-002"), job("run.o.em-003")],
        [
            finding("run.o.np-001"),
            finding("run.o.ds-002"),
            finding("run.o.em-003"),
        ],
    )
    assert runner._orchestrator_event_due(autonomy_state, 4) == "event"
    assert runner._orchestrator_consult("event", autonomy_state, {}) is not None
    assert runner._orchestrator_event_due(autonomy_state, 5) == ""


def test_research_cadence_fires_on_schedule(enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_CADENCE_CYCLES", "4")
    _events(monkeypatch)
    autonomy_state: dict[str, Any] = {}

    assert runner._orchestrator_event_due(autonomy_state, 3) == ""
    assert runner._orchestrator_event_due(autonomy_state, 4) == "event"
    assert runner._orchestrator_event_due(autonomy_state, 4) == ""
    assert runner._orchestrator_consult("event", autonomy_state, {}) is not None
    assert runner._orchestrator_event_due(autonomy_state, 8) == "event"


def test_research_cadence_repeats_in_fresh_campaign_epoch(enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_CADENCE_CYCLES", "4")
    _events(monkeypatch)
    autonomy_state: dict[str, Any] = {"campaign_epoch": 1}

    assert runner._orchestrator_event_due(autonomy_state, 4) == "event"
    assert runner._orchestrator_consult("event", autonomy_state, {}) is not None
    autonomy_state["campaign_epoch"] = 2
    autonomy_state.pop("orchestrator_cadence_cycle", None)

    assert runner._orchestrator_event_due(autonomy_state, 4) == "event"
