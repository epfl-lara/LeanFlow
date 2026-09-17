"""Prevent silent research termination through actual controller recovery and dispatch."""

from __future__ import annotations

import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import recovery
from tests.leanflow.test_prover_recovery_decisions import research_runtime
from tests.leanflow.test_prover_response_guard import make_runtime, stalled_result
from tests.leanflow.test_prover_response_recovery import decision_result, resume


@pytest.mark.parametrize("bad_reply", ['{"action":"stop"}', "not JSON", '{"action":"continue"}'])
def test_invalid_decision_returns_correction_then_dispatches_actionable_prover(
    tmp_path: Path, bad_reply: str
) -> None:
    runtime = research_runtime(tmp_path)
    runtime.state["design_plan_complete"] = True
    requests: list[dict[str, Any]] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        requests.append(kwargs)
        if len(requests) == 1:
            assert kwargs["role"] == "prover"
            return stalled_result()
        if len(requests) == 2:
            assert kwargs["role"] == "orchestrator"
            return {"status": "complete", "api_calls": 1, "final_response": bad_reply}
        if len(requests) == 3:
            assert kwargs["role"] == "orchestrator"
            assert "invalid_recovery_decision" in kwargs["prompt"]
            assert (
                '"allowed_actions": ["retry", "continue", "negate", "decompose"]'
                in kwargs["prompt"]
            )
            return decision_result("continue", instructions="Try exact True.intro.")
        assert len(requests) == 4 and kwargs["role"] == "prover"
        assert kwargs["context"]["recovery_guidance"]["instructions"] == "Try exact True.intro."
        return {"status": "complete", "api_calls": 1, "final_response": '{"proof":"trivial"}'}

    runtime.session = session
    state = runtime.run()
    assert state["status"] == "completed"
    assert [item["action"] for item in state["recovery_decisions"]] == ["invalid", "continue"]
    assert state["metrics"]["decompositions"] == 2
    assert state["metrics"]["api_calls"] == 5
    assert not state.get("recovery_in_flight")


def test_idle_stranded_obligation_calls_orchestrator_before_prover(tmp_path: Path) -> None:
    runtime = research_runtime(tmp_path)
    runtime.state["design_plan_complete"] = True
    runtime.dag.nodes[0].status = "blocked"
    roles: list[str] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        roles.append(kwargs["role"])
        if kwargs["role"] == "orchestrator":
            assert "scheduler_recovery" in kwargs["prompt"]
            return decision_result("retry")
        return {"status": "complete", "api_calls": 1, "final_response": '{"proof":"trivial"}'}

    runtime.session = session
    state = runtime.run()
    assert roles == ["orchestrator", "prover"]
    assert state["status"] == "completed"


def test_repeated_stop_uses_finite_recovery_budget_and_reports_exact_limit(tmp_path: Path) -> None:
    runtime = research_runtime(tmp_path, max_decompositions=3)
    runtime.state["design_plan_complete"] = True
    runtime.dag.nodes[0].status = "blocked"
    roles: list[str] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        roles.append(kwargs["role"])
        return decision_result("stop")

    runtime.session = session
    state = runtime.run()
    assert roles == ["orchestrator"] * 3
    assert state["status"] == "budget_exhausted"
    assert state["stop_reason"]["code"] == "campaign_recoveries"
    assert state["stop_reason"]["used"] == state["stop_reason"]["limit"] == 3
    assert state["stop_reason"]["remaining_api_calls"] > 0
    assert state["metrics"]["api_calls"] == 3
    reopened = resume(runtime, session=lambda **kw: pytest.fail("Spent capacity must not renew"))
    assert reopened.run()["status"] == "budget_exhausted"
    assert reopened.consumed == 3


@pytest.mark.parametrize("crash_point", ["cached_reply", "rejected_reply"])
def test_invalid_decision_crash_replay_does_not_repeat_or_refund_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_point: str
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    runtime._finish_job(job, stalled_result())
    runtime.session = lambda **kw: decision_result("stop")
    original_decide = recovery.recovery_decision
    original_persist = runtime._persist

    def decide_then_crash(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original_decide(*args, **kwargs)
        raise SystemExit("reply is cached")

    def persist_then_crash() -> None:
        original_persist()
        journal = runtime.state.get("recovery_in_flight", {}).get(node.id, {})
        if journal.get("report", {}).get("recovery_correction"):
            raise SystemExit("rejection is durable")

    if crash_point == "cached_reply":
        monkeypatch.setattr(recovery, "recovery_decision", decide_then_crash)
    else:
        monkeypatch.setattr(runtime, "_persist", persist_then_crash)
    with pytest.raises(SystemExit):
        runtime._recover(node, {"job_id": job["id"]})
    monkeypatch.setattr(recovery, "recovery_decision", original_decide)
    monkeypatch.setattr(runtime, "_persist", original_persist)
    assert runtime.consumed == 3
    calls: list[str] = []

    def corrected(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["role"])
        assert "invalid_recovery_decision" in kwargs["prompt"]
        return decision_result("retry")

    reopened = resume(runtime, session=corrected)
    reopened._recover(reopened.dag.by_id()[node.id])
    assert calls == ["orchestrator"]
    assert reopened.consumed == 4
    assert reopened.state["metrics"]["decompositions"] == 2
    assert [item["action"] for item in reopened.state["recovery_decisions"]] == ["invalid", "retry"]
    assert not resume(reopened).state.get("recovery_in_flight")


@pytest.mark.parametrize("journal_left", [False, True])
def test_resume_reopens_legacy_stop_despite_response_admission_marker(
    tmp_path: Path, journal_left: bool
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    runtime._finish_job(job, stalled_result())
    job.update(result_processed=True, response_recovery_enqueued=True)
    node.status = "blocked"
    old = {
        "node_id": node.id,
        "node_revision": node.revision,
        "source_job_id": job["id"],
        "action": "stop",
        "rationale": "Old policy",
    }
    runtime.state["recovery_decisions"] = [old]
    runtime.state["metrics"]["decompositions"] = 1
    if journal_left:
        runtime.state["recovery_in_flight"] = {
            node.id: {"stage": "done", "decision": old, "charged": True, "report": {}}
        }
    runtime._persist()
    reopened = resume(runtime)
    journal = reopened.state["recovery_in_flight"][node.id]
    assert journal["stage"] == "decide" and not journal["charged"]
    assert journal["report"]["recovery_correction"]["code"] == "retired_stop_decision"
    assert reopened.consumed == 2
    reopened._recover(reopened.dag.by_id()[node.id])
    assert reopened.dag.by_id()[node.id].status == "retry"
    assert reopened.state["metrics"]["decompositions"] == 2
    assert not resume(reopened).state.get("recovery_in_flight")


@pytest.mark.parametrize("status", ["proved", "false", "candidate", "retry", "stale"])
def test_legacy_stop_migration_preserves_checked_and_superseded_outcomes(
    tmp_path: Path, status: str
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    runtime._finish_job(job, stalled_result())
    job.update(result_processed=True, response_recovery_enqueued=True)
    runtime.state["recovery_decisions"] = [
        {"node_id": node.id, "node_revision": node.revision, "action": "stop"}
    ]
    node.status = status
    if status == "candidate":
        node.candidate = ["trivial"]
    if status == "stale":
        node.status = "blocked"
        node.revision += 1
    runtime._persist()
    assert not resume(runtime).state.get("recovery_in_flight")


def test_recovery_exhaustion_does_not_cancel_independent_active_prover(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.config = replace(runtime.config, max_decompositions=1)
    runtime.state["config"] = asdict(runtime.config)
    healthy_started = threading.Event()
    recovery_finished = threading.Event()
    original_handle = runtime._handle_result

    def handle(job: dict[str, Any], result: dict[str, Any]) -> None:
        original_handle(job, result)
        if result["status"] == "response_stalled":
            recovery_finished.set()

    def session(**kwargs: Any) -> dict[str, Any]:
        if kwargs["role"] == "orchestrator":
            return decision_result("stop")
        if kwargs["context"]["assignment"]["name"] == "stalled":
            assert healthy_started.wait(5)
            return stalled_result()
        healthy_started.set()
        assert recovery_finished.wait(5)
        assert not runtime.cancelled.is_set()
        return {"status": "complete", "api_calls": 1, "final_response": '{"proof":"trivial"}'}

    runtime.session = session
    runtime._handle_result = handle  # type: ignore[method-assign]
    state = runtime.run()
    assert state["status"] == "budget_exhausted"
    assert state["stop_reason"]["code"] == "campaign_recoveries"
    assert {node.name: node.status for node in runtime.dag.nodes} == {
        "stalled": "blocked",
        "healthy": "proved",
    }


def test_funded_exhausted_journal_reopens_once_without_resetting_spend(tmp_path: Path) -> None:
    runtime = research_runtime(tmp_path, max_decompositions=3)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    runtime.state["metrics"]["decompositions"] = 1
    runtime.state["design_plan_complete"] = True
    runtime.state["recovery_in_flight"] = {
        node.id: {"stage": "budget_exhausted", "charged": False, "report": {}}
    }
    roles: list[str] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        roles.append(kwargs["role"])
        if kwargs["role"] == "orchestrator":
            return decision_result("retry")
        return {"status": "complete", "api_calls": 1, "final_response": '{"proof":"trivial"}'}

    runtime.session = session
    state = runtime.run()
    assert roles == ["orchestrator", "prover"]
    assert state["status"] == "completed"
    assert state["metrics"]["decompositions"] == 2
    assert state["metrics"]["api_calls"] == 2


def test_unknown_journal_stage_is_an_explicit_state_error_without_model_calls(
    tmp_path: Path,
) -> None:
    runtime = research_runtime(tmp_path)
    runtime.state["design_plan_complete"] = True
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    runtime.state["recovery_in_flight"] = {node.id: {"stage": "unrecognized", "report": {}}}
    runtime.session = lambda **kw: pytest.fail("Corrupt state cannot authorize model work")
    state = runtime.run()
    assert state["status"] == "state_error"
    assert "Unknown recovery journal stage" in state["error"]
    assert state["metrics"]["api_calls"] == 0


def test_rejected_refinement_keeps_orchestrator_ownership_of_refuted_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from leanflow_cli.workflows.prover.models import Node

    runtime = research_runtime(tmp_path)
    target = runtime.dag.nodes[0]
    helper = Node(
        "h", "bad_helper", "theorem bad_helper : False", "Main.lean", "Main", status="false"
    )
    runtime.dag.nodes.append(helper)
    target.dependencies = [helper.id]
    target.candidate = ["trivial"]
    target.status = "candidate"
    runtime.state["recovery_in_flight"] = {
        helper.id: {
            "stage": "plan",
            "plan_started": False,
            "refinement": True,
            "reason": "Repair the refuted helper's branch",
            "affected": [helper.id, target.id],
            "charged": True,
            "decision": {"action": "negate"},
            "report": {"certified_negation": {"certified": True}},
        }
    }
    plans: list[bool] = []
    prompts: list[str] = []

    def plan(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        plans.append(refinement)
        runtime.state.pop("planning_request", None)
        if len(plans) == 1:
            runtime.state["proposal_critique"] = "The replacement still requires the refuted helper"
            return False
        assert helper.status == "false"
        assert target.candidate == ["trivial"]
        target.dependencies = []
        runtime.dag.nodes.remove(helper)
        return True

    def decide(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["role"] == "orchestrator"
        prompts.append(kwargs["prompt"])
        assert "last_plan_rejection" in kwargs["prompt"]
        assert "certified_negation" in kwargs["prompt"]
        if len(prompts) == 1:
            return decision_result("retry")
        assert '"allowed_actions": ["decompose"]' in kwargs["prompt"]
        return decision_result("decompose", instructions="Remove the refuted generated helper.")

    runtime.session = decide
    monkeypatch.setattr(runtime, "_research_plan", plan)
    runtime._recover(helper)
    assert plans == [True, True]
    assert len(prompts) == 2
    assert helper.status == "false"
    assert target.candidate == ["trivial"]
    assert not runtime.state.get("recovery_in_flight")
    assert [item["action"] for item in runtime.state["recovery_decisions"]] == [
        "invalid",
        "decompose",
    ]
    runtime._promote_candidates()
    assert target.status == "proved"
