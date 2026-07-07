"""Phase 4 (2/6) tests: orchestrator runner wiring — consult, apply, resume."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.orchestrator import OrchestratorRoute
from leanflow_cli.workflows.queue_manager import TheoremKey, TheoremQueueManager


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


def test_consult_noops_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("LEANFLOW_ORCHESTRATOR_ENABLED", raising=False)
    events = _events(monkeypatch)

    route = runner._orchestrator_consult("stall", _autonomy_state(str(tmp_path)), {})

    assert route is None
    assert events == []


def test_consult_records_activity_and_charges_route_budget(enabled, monkeypatch, tmp_path):
    events = _events(monkeypatch)
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

    # Passthrough consults never charge the budget.
    passthrough = runner._orchestrator_consult("scope-entry", autonomy_state, {})
    assert passthrough.route == "direct-prove"
    assert autonomy_state["orchestrator_routes_used"] == 1


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


def test_apply_escalate_writes_disproved_report(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}
    }

    action = runner._orchestrator_apply_route(
        OrchestratorRoute(route="escalate", reason="negation proved"),
        [],
        autonomy_state,
        {},
    )

    assert action == "stop:disproved"
    assert plan_state.load_summary()["final_report"]["status"] == "disproved"


def test_apply_negate_route_runs_probe_and_resumes(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    probes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner,
        "_maybe_negation_probe",
        lambda autonomy_state, *, target_symbol, active_file: probes.append(
            (target_symbol, active_file)
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


def test_assignment_transition_opens_a_fresh_orchestrator_scope(enabled, monkeypatch, tmp_path):
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

    # New theorem scope: route budget and scope-entry consult both reset.
    assert "orchestrator_routes_used" not in autonomy_state
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


def test_consumed_job_does_not_refire_sibling_events(enabled, monkeypatch, tmp_path):
    _events(monkeypatch)

    from leanflow_cli.workflows.workflow_json_io import update_json_file

    def _seed_ledger(entries):
        # save_summary strips dispatch_ledger as a foreign key (by design);
        # seed through the ledger's own transactional write path.
        def mutate(payload):
            payload["dispatch_ledger"] = entries

        update_json_file(plan_state.plan_state_paths().summary_json, mutate)

    job = lambda jid, consumed=False: {  # noqa: E731
        "spec": {"job_id": jid},
        "state": "done",
        "consumed": consumed,
    }
    autonomy_state: dict[str, Any] = {}
    _seed_ledger([job("run.o.np-001"), job("run.o.ds-002")])
    assert runner._orchestrator_event_due(autonomy_state, 2) == "event"

    # Consuming one job must not re-fire the other.
    _seed_ledger([job("run.o.np-001", consumed=True), job("run.o.ds-002")])
    assert runner._orchestrator_event_due(autonomy_state, 3) == ""

    # A genuinely new done job fires once.
    _seed_ledger([job("run.o.ds-002"), job("run.o.em-003")])
    assert runner._orchestrator_event_due(autonomy_state, 4) == "event"
    assert runner._orchestrator_event_due(autonomy_state, 5) == ""


def test_research_cadence_fires_on_schedule(enabled, monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_CADENCE_CYCLES", "4")
    _events(monkeypatch)
    autonomy_state: dict[str, Any] = {}

    assert runner._orchestrator_event_due(autonomy_state, 3) == ""
    assert runner._orchestrator_event_due(autonomy_state, 4) == "event"
    assert runner._orchestrator_event_due(autonomy_state, 4) == ""
    assert runner._orchestrator_event_due(autonomy_state, 8) == "event"
