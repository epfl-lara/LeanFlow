"""Reproduce campaign early exits and stale cost coverage from the paused benchmark."""

import json
from pathlib import Path

import pytest

from leanflow_cli.workflows.prover import planning_controller
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime


def make_runtime(tmp_path: Path, **kwargs) -> ProverRuntime:
    """Create one protected root with no provider or Lean subprocess."""
    from tests.leanflow.test_prover_runtime import Verifier

    source = tmp_path / "Main.lean"
    source.write_text("theorem goal : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    kwargs.setdefault("config", ProverConfig())
    return ProverRuntime(root=tmp_path, targets=[source], verifier=Verifier(), **kwargs)


def test_local_exhaustion_does_not_exhaust_campaign(tmp_path):
    runtime = make_runtime(
        tmp_path,
        config=ProverConfig(job_api_calls=2, total_api_calls=20),
        session=lambda **kw: {"status": "budget_exhausted", "api_calls": 2},
    )
    result = runtime.run()
    assert result["status"] == "blocked"
    assert result["stop_reason"]["scope"] == "scheduler"
    assert result["metrics"]["api_calls"] == 2


@pytest.mark.parametrize("attempts,expected", [(1, "retry"), (4, "blocked")])
def test_accepted_local_plan_repair_reopens_within_retry_limit(
    tmp_path, monkeypatch, attempts, expected
):
    from leanflow_cli.workflows.prover import negation_job

    runtime = make_runtime(tmp_path, config=ProverConfig(mode="research", max_restarts=3))
    node = runtime.dag.nodes[0]
    node.status, node.attempts = "blocked", attempts
    monkeypatch.setattr(negation_job, "attempt_negation", lambda *args: {"certified": False})
    monkeypatch.setattr(runtime, "_research_plan", lambda *args, **kwargs: True)
    runtime._recover(node)
    assert node.status == expected
    assert node.attempts == attempts


def test_rejected_reviews_continue_within_total_budget(tmp_path, monkeypatch):
    reviews = 0

    def session(**kwargs):
        nonlocal reviews
        if kwargs["role"] == "review":
            reviews += 1
            reply = {"accepted": reviews == 4, "critique": "Split the remaining obligation."}
        else:
            reply = {"plan": "Concrete outline", "nodes": [], "change_kind": "decomposition"}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(reply)}

    runtime = make_runtime(
        tmp_path, config=ProverConfig(mode="research", total_api_calls=12), session=session
    )
    monkeypatch.setattr(planning_controller, "materialize", lambda *args: None)
    assert runtime._research_plan("Repair the rejected graph", affected=set(runtime.dag.roots))
    assert reviews == 4
    assert runtime.state["metrics"]["api_calls"] == 9
    assert runtime.state["metrics"]["plan_refinements"] == 0


def test_resumed_cost_is_incomplete_after_new_unpriced_request(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.state["jobs"] = [
        {
            "api_calls": 4,
            "cost_usd": 1.0,
            "costed_api_calls": 3,
            "cost_source": "provider_reported",
            "status": "running",
        }
    ]
    runtime._refresh_metrics()
    assert runtime.state["metrics"]["cost_complete"] is False


def test_missing_resume_cost_retains_known_subtotal(tmp_path):
    runtime = make_runtime(tmp_path)
    job, _ = runtime._new_job("research", prompt="computation")
    job.update(
        previous_cost_usd=1.25,
        previous_costed_api_calls=2,
        previous_api_calls=2,
        previous_cost_source="provider_reported",
        api_calls=3,
        cost_usd=1.25,
    )
    runtime._finish_job(
        job,
        {
            "status": "completed",
            "api_calls": 3,
            "cost_usd": None,
            "costed_api_calls": 0,
            "cost_source": "unavailable",
        },
    )
    assert job["cost_usd"] == 1.25
    assert runtime.state["metrics"]["cost_complete"] is False


def test_only_running_jobs_reduce_unspent_reservations(tmp_path):
    runtime = make_runtime(tmp_path, config=ProverConfig(total_api_calls=1000))
    runtime.reserved = 200
    runtime.state["jobs"] = [
        {"api_calls": 40, "status": "interrupted", "accounted": False},
        {"api_calls": 10, "status": "running", "accounted": False},
    ]
    runtime._refresh_metrics()
    assert runtime.state["metrics"]["remaining_reserved_api_calls"] == 190
    assert runtime.state["metrics"]["available_api_calls"] == 760


def test_zero_admission_survives_crash_before_dispatch(tmp_path):
    runtime = make_runtime(tmp_path)
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    resumed = ProverRuntime(
        root=tmp_path,
        targets=runtime.targets,
        config=runtime.config,
        verifier=runtime.verifier,
        run_id=runtime.run_id,
        resume=True,
    )
    assert resumed.consumed == 0
    assert resumed.resume_jobs[runtime.dag.nodes[0].id]["id"] == job["id"]


def test_role_reservation_failure_retains_resume_entry(tmp_path):
    from leanflow_cli.workflows.prover.runtime import BudgetExhausted

    runtime = make_runtime(tmp_path, config=ProverConfig(total_api_calls=10))
    job, _ = runtime._new_job("research", prompt="saved")
    runtime.reserved = 9
    runtime.resume_role_jobs[("research", "saved")] = job
    with pytest.raises(BudgetExhausted):
        runtime._new_job("research", prompt="saved")
    assert runtime.resume_role_jobs[("research", "saved")] is job
    assert runtime.reserved == 9


def test_research_without_free_reservation_returns_control_to_prover(tmp_path):
    from leanflow_cli.workflows.prover.job_controller import research_job

    runtime = make_runtime(tmp_path, config=ProverConfig(job_api_calls=5, total_api_calls=5))
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    result = research_job(runtime, job, "Check a finite computation")
    assert result["status"] == "unavailable"
    assert not runtime.cancelled.is_set()
    assert runtime.reserved == 5
    assert len(runtime.state["jobs"]) == 1


def test_wall_clock_has_its_own_terminal_status(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(runtime, "_elapsed", lambda: runtime.config.wall_time_s + 1)
    state = runtime.run()
    assert state["status"] == "timeout"
    assert state["stop_reason"]["code"] == "campaign_wall_time"


def test_failed_direction_proposals_do_not_charge_refinements(tmp_path):
    from leanflow_cli.workflows.prover.runtime import BudgetExhausted

    def session(**kwargs):
        reply = (
            {"accepted": False, "critique": "Try another split"}
            if kwargs["role"] == "review"
            else {"plan": "New direction", "nodes": []}
        )
        return {"api_calls": 1, "status": "completed", "final_response": json.dumps(reply)}

    runtime = make_runtime(
        tmp_path, config=ProverConfig(mode="research", total_api_calls=5), session=session
    )
    with pytest.raises(BudgetExhausted):
        runtime._research_plan("Replace a refuted helper", refinement=True)
    assert runtime.state["metrics"]["plan_refinements"] == 0
    assert runtime.consumed == 5


def test_reviewed_repair_overrides_proposer_direction_label(tmp_path, monkeypatch):
    def session(**kwargs):
        reply = (
            {"accepted": True, "change_kind": "repair"}
            if kwargs["role"] == "review"
            else {"plan": "Fix rewrite orientation", "nodes": [], "change_kind": "direction"}
        )
        return {"api_calls": 1, "status": "completed", "final_response": json.dumps(reply)}

    runtime = make_runtime(
        tmp_path, config=ProverConfig(mode="research", total_api_calls=5), session=session
    )
    monkeypatch.setattr(planning_controller, "materialize", lambda *args: None)
    assert runtime._research_plan("Local rewrite fix", affected=set(runtime.dag.roots))
    assert runtime.state["metrics"]["plan_refinements"] == 0


def test_verification_deadline_is_not_an_environment_failure(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    expired = False

    def preflight(_workspace):
        nonlocal expired
        expired = True
        return {"accepted": False, "error_code": "check_timeout", "error": "deadline"}

    runtime.verifier.preflight = preflight
    monkeypatch.setattr(
        runtime, "_elapsed", lambda: runtime.config.wall_time_s + 1 if expired else 0
    )
    state = runtime.run()
    assert state["status"] == "timeout"
    assert state["stop_reason"]["code"] == "campaign_wall_time"


def test_controller_reclaims_finished_parallel_allocation_before_replanning(tmp_path):
    from concurrent.futures import Future

    runtime = make_runtime(tmp_path, config=ProverConfig(job_api_calls=5, total_api_calls=5))
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    future = Future()
    future.set_result({"status": "completed", "api_calls": 1})
    runtime.pending[future] = job
    repair, _ = runtime._new_job("orchestrator", prompt="Repair branch")
    assert repair["api_budget"] == 4
    assert runtime.consumed == 1
    assert runtime.reserved == 4
    assert job["accounted"] is True
    assert future in runtime.pending  # Its mathematical result is still queued.


def test_unadmitted_research_does_not_spend_request_slots(tmp_path):
    from leanflow_cli.workflows.prover.job_controller import research_job
    from leanflow_cli.workflows.prover.session_tools import SessionTools

    runtime = make_runtime(tmp_path, config=ProverConfig(job_api_calls=5, total_api_calls=5))
    job, context = runtime._new_job("prover", node=runtime.dag.nodes[0])
    workspace = Path(job["workspace"])
    tools = SessionTools(
        role="prover",
        project_root=tmp_path,
        workspace=workspace,
        context=context,
        research_job=lambda question: research_job(runtime, job, question),
    )
    for _ in range(3):
        assert (
            tools._invoke("research_job", {"question": "Compute a finite sum"})["status"]
            == "unavailable"
        )
    ledger = workspace.parent / ".runtime" / workspace.name / "research-count.json"
    assert json.loads(ledger.read_text())["used"] == 0
    assert len(runtime.state["jobs"]) == 1


def test_capacity_wait_releases_lock_for_a_running_worker(tmp_path):
    import threading
    from concurrent.futures import Future

    runtime = make_runtime(tmp_path, config=ProverConfig(job_api_calls=5, total_api_calls=5))
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    future = Future()
    runtime.pending[future] = job

    def finish():
        with runtime.lock:
            future.set_result({"status": "completed", "api_calls": 1})

    timer = threading.Timer(0.05, finish)
    timer.start()
    try:
        repair, _ = runtime._new_job("orchestrator", prompt="Repair branch")
        assert repair["api_budget"] == 4
    finally:
        timer.join(timeout=1)
    assert not timer.is_alive()


def test_reviewer_compares_against_original_accepted_plan_after_resume(tmp_path, monkeypatch):
    seen = []

    def session(**kwargs):
        if kwargs["role"] == "review":
            seen.append(kwargs["context"]["previous_accepted_plan"])
            reply = {"accepted": True, "change_kind": "repair"}
        else:
            reply = {"plan": "New outline for the local repair", "nodes": []}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(reply)}

    runtime = make_runtime(tmp_path, config=ProverConfig(mode="research"), session=session)
    runtime.state["plan_markdown"] = "Interim outline saved before interruption"
    runtime.state["planning_request"] = {
        "reason": "Repair",
        "affected": runtime.dag.roots,
        "refinement": False,
        "steps": {},
        "previous_plan": "Original accepted mathematical route",
    }
    monkeypatch.setattr(planning_controller, "materialize", lambda *args: None)
    assert runtime._research_plan("resume")
    assert seen == ["Original accepted mathematical route"]
