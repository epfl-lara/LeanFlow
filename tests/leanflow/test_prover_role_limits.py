"""Exercise native session admission across role limits, failures, and resume."""

from pathlib import Path

import pytest

from leanflow_cli.workflows.prover.runtime import (
    BudgetExhausted,
    InfrastructureFailure,
    ProverRuntime,
)
from tests.leanflow.test_prover_budget_repairs import make_runtime


def test_native_startup_failure_refunds_reserved_invocation(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    with pytest.raises(InfrastructureFailure):
        runtime._finish_job(job, {"status": "provider_error", "api_calls": 0})
    assert runtime.reserved == 0
    assert runtime.consumed == 0


def test_native_finish_is_idempotent(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    job, _ = runtime._new_job("research", prompt="question")
    result = {"status": "completed", "api_calls": 1}
    runtime._finish_job(job, result)
    runtime._finish_job(job, result)
    assert runtime.reserved == 0
    assert runtime.consumed == 1


def test_reservation_without_native_start_is_not_spent_on_resume(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime._new_job("prover", node=runtime.dag.nodes[0])
    resumed = ProverRuntime(
        root=tmp_path,
        targets=runtime.targets,
        config=runtime.config,
        verifier=runtime.verifier,
        run_id=runtime.run_id,
        resume=True,
    )
    assert resumed.reserved == 0
    assert resumed.consumed == 0
    assert resumed.resume_jobs


def test_parallel_admission_reserves_exactly_available_role_capacity(tmp_path: Path) -> None:
    """Concurrent jobs must not all observe the same free role slot."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(tmp_path, config=ProverConfig(prover_sessions=2, agent_calls=20))
    barrier = Barrier(8)

    def admit(index: int) -> bool:
        barrier.wait()
        try:
            runtime._new_job("prover", node=runtime.dag.nodes[0], prompt=str(index))
        except BudgetExhausted as error:
            assert error.code == "prover_sessions"
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(admit, range(8)))
    assert sum(results) == 2
    assert runtime.reserved == 2
    assert runtime.state["metrics"]["role_sessions"]["prover"] == {
        "used": 0,
        "reserved": 2,
        "limit": 2,
    }


def test_started_session_changes_reservation_to_spending_once(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(tmp_path, config=ProverConfig(prover_sessions=1))
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    runtime._session_event(job, "api-request", {"api_calls": 1})
    runtime._refresh_metrics()
    assert runtime.state["metrics"]["role_sessions"]["prover"] == {
        "used": 1,
        "reserved": 0,
        "limit": 1,
    }
    runtime._finish_job(job, {"status": "completed", "api_calls": 1})
    runtime._finish_job(job, {"status": "completed", "api_calls": 1})
    with pytest.raises(BudgetExhausted, match="1 started, 0 reserved, limit 1"):
        runtime._new_job("prover", node=runtime.dag.nodes[0])
    assert runtime.state["metrics"]["role_sessions"]["prover"]["used"] == 1


def test_failed_start_and_context_setup_refund_both_limits(tmp_path: Path, monkeypatch) -> None:
    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(tmp_path, config=ProverConfig(prover_sessions=1, agent_calls=1))
    context = runtime._context
    monkeypatch.setattr(
        runtime, "_context", lambda node: (_ for _ in ()).throw(ValueError("setup"))
    )
    with pytest.raises(ValueError, match="setup"):
        runtime._new_job("prover", node=runtime.dag.nodes[0])
    assert runtime.reserved == 0
    assert not runtime.state["jobs"]
    monkeypatch.setattr(runtime, "_context", context)
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    with pytest.raises(InfrastructureFailure):
        runtime._finish_job(job, {"status": "provider_error", "api_calls": 0})
    another, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    assert another["id"] != job["id"]
    assert runtime.state["metrics"]["role_sessions"]["prover"] == {
        "used": 0,
        "reserved": 1,
        "limit": 1,
    }


@pytest.mark.parametrize("role", ["orchestrator", "review", "research"])
def test_planning_roles_share_one_allowance_and_leave_prover_capacity(
    tmp_path: Path, role: str
) -> None:
    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(tmp_path, config=ProverConfig(orchestrator_sessions=1))
    job, _ = runtime._new_job(role, prompt="one")
    runtime._finish_job(job, {"status": "completed", "api_calls": 1})
    for other in ("orchestrator", "review", "research"):
        with pytest.raises(BudgetExhausted) as caught:
            runtime._new_job(other, prompt="two")
        assert caught.value.code == "orchestrator_sessions"
    runtime._new_job("prover", node=runtime.dag.nodes[0])
    runtime._new_job("negation", node=runtime.dag.nodes[0])
    assert not runtime.cancelled.is_set()


@pytest.mark.parametrize("role", ["prover", "orchestrator", "negation"])
def test_zero_blocks_only_its_role_and_unset_is_unlimited(tmp_path: Path, role: str) -> None:
    from leanflow_cli.workflows.prover.config import ProverConfig

    limits = {group + "_sessions": None for group in ("prover", "orchestrator", "negation")}
    limits[role + "_sessions"] = 0
    runtime = make_runtime(tmp_path, config=ProverConfig(**limits))
    with pytest.raises(BudgetExhausted) as caught:
        runtime._new_job(role, node=runtime.dag.nodes[0])
    assert caught.value.code == role + "_sessions"
    for other in {"prover", "orchestrator", "negation"} - {role}:
        for attempt in range(3):
            job, _ = runtime._new_job(other, node=runtime.dag.nodes[0], prompt=str(attempt))
            runtime._finish_job(job, {"status": "completed", "api_calls": 1})
        assert runtime.state["metrics"]["role_sessions"][other]["used"] == 3


def test_resume_preserves_spending_and_charges_continuation(tmp_path: Path, monkeypatch) -> None:
    import json

    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(tmp_path, config=ProverConfig(prover_sessions=2, agent_calls=3))
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    workspace = Path(job["workspace"])
    ledger = workspace.parent / ".runtime" / workspace.name / "request-count.json"
    ledger.write_text(json.dumps({"used": 1}))
    resumed = ProverRuntime(
        root=tmp_path,
        targets=runtime.targets,
        config=runtime.config,
        verifier=runtime.verifier,
        run_id=runtime.run_id,
        resume=True,
    )
    assert resumed.state["metrics"]["role_sessions"]["prover"] == {
        "used": 1,
        "reserved": 0,
        "limit": 2,
    }
    context = resumed._context
    monkeypatch.setattr(
        resumed, "_context", lambda node: (_ for _ in ()).throw(ValueError("setup"))
    )
    with pytest.raises(ValueError, match="setup"):
        resumed._new_job("prover", node=resumed.dag.nodes[0])
    assert resumed.reserved == 0
    assert resumed.resume_jobs[node.id]["status"] == "resume_pending"
    monkeypatch.setattr(resumed, "_context", context)
    continued, _ = resumed._new_job("prover", node=resumed.dag.nodes[0])
    assert continued["id"] == job["id"]
    assert resumed.state["metrics"]["role_sessions"]["prover"]["reserved"] == 1
    resumed._finish_job(continued, {"status": "completed", "api_calls": 2})
    with pytest.raises(BudgetExhausted) as caught:
        resumed._new_job("prover", node=resumed.dag.nodes[0])
    assert caught.value.code == "prover_sessions"
    assert resumed.consumed == 2
    assert resumed.reserved == 0


def test_scheduler_reports_role_limit_and_accepts_work_at_exact_limit(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.config import ProverConfig
    from tests.leanflow.test_prover_runtime import session

    runtime = make_runtime(
        tmp_path,
        config=ProverConfig(mode="standard", prover_sessions=0, agent_calls=10),
        session=session,
    )
    state = runtime.run()
    assert state["status"] == "budget_exhausted"
    assert state["stop_reason"]["code"] == "prover_sessions"
    assert state["stop_reason"]["scope"] == "role"
    assert state["stop_reason"]["used"] == state["stop_reason"]["limit"] == 0
    assert state["stop_reason"]["remaining_api_calls"] == 10
    second = tmp_path / "second"
    second.mkdir()
    runtime = make_runtime(
        second, config=ProverConfig(mode="standard", prover_sessions=1), session=session
    )
    assert runtime.run()["status"] == "completed"


def test_optional_research_exhaustion_keeps_parent_running(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.config import ProverConfig
    from leanflow_cli.workflows.prover.job_controller import research_job

    runtime = make_runtime(tmp_path, config=ProverConfig(orchestrator_sessions=0))
    parent, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    result = research_job(runtime, parent, "Count a finite example")
    assert result["status"] == "unavailable"
    assert result["stop_code"] == "orchestrator_sessions"
    assert "limit 0" in result["error"]
    assert not runtime.cancelled.is_set()
    assert not runtime.state.get("stop_code")


def test_role_wait_reconciles_reserved_work_before_admission(tmp_path: Path) -> None:
    from concurrent.futures import Future

    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(tmp_path, config=ProverConfig(prover_sessions=1))
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    completed = Future()
    completed.set_result({"status": "completed", "api_calls": 0})
    runtime.pending[completed] = job
    replacement, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    assert job["accounted"] is True
    assert replacement["id"] != job["id"]
    assert runtime.reserved == 1
    assert runtime.state["metrics"]["role_sessions"]["prover"]["used"] == 0
    assert completed in runtime.pending  # Controller still processes its saved result.


def test_started_failed_session_stays_charged(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(tmp_path, config=ProverConfig(prover_sessions=1))
    job, _ = runtime._new_job("prover", node=runtime.dag.nodes[0])
    with pytest.raises(InfrastructureFailure):
        runtime._finish_job(job, {"status": "provider_error", "api_calls": 1})
    with pytest.raises(BudgetExhausted) as caught:
        runtime._new_job("prover", node=runtime.dag.nodes[0])
    assert caught.value.code == "prover_sessions"
    assert runtime.state["metrics"]["role_sessions"]["prover"]["used"] == 1


def test_resumed_research_setup_failure_does_not_leak_allowance(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(tmp_path, config=ProverConfig(orchestrator_sessions=2))
    job, _ = runtime._new_job("research", prompt="unfinished question")
    workspace = Path(job["workspace"])
    ledger = workspace.parent / ".runtime" / workspace.name / "request-count.json"
    ledger.write_text(json.dumps({"used": 1}))
    resumed = ProverRuntime(
        root=tmp_path,
        targets=runtime.targets,
        config=runtime.config,
        verifier=runtime.verifier,
        run_id=runtime.run_id,
        resume=True,
    )
    context = resumed._context
    monkeypatch.setattr(
        resumed, "_context", lambda node: (_ for _ in ()).throw(ValueError("setup"))
    )
    with pytest.raises(ValueError, match="setup"):
        resumed._new_job("research", prompt="unfinished question")
    assert resumed.reserved == 0
    assert (
        resumed.resume_role_jobs[("research", "unfinished question")]["status"] == "resume_pending"
    )
    monkeypatch.setattr(resumed, "_context", context)
    continuation, _ = resumed._new_job("research", prompt="unfinished question")
    resumed._finish_job(continuation, {"status": "completed", "api_calls": 2})
    with pytest.raises(BudgetExhausted) as caught:
        resumed._new_job("review", prompt="one more")
    assert caught.value.code == "orchestrator_sessions"
    assert resumed.consumed == 2


def test_coincident_global_limit_preserves_recorded_role_stop(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(
        tmp_path, config=ProverConfig(mode="standard", agent_calls=0, prover_sessions=0)
    )
    state = runtime.run()
    assert state["stop_code"] == state["stop_reason"]["code"] == "prover_sessions"
    assert state["stop_reason"]["scope"] == "role"
    assert state["stop_reason"]["used"] == state["stop_reason"]["limit"] == 0


def test_assignment_and_recovery_guidance_show_remaining_role_capacity(tmp_path: Path) -> None:
    import json

    from leanflow_cli.workflows.prover.config import ProverConfig
    from leanflow_cli.workflows.prover.recovery import recovery_decision

    seen = []

    def decide(**kwargs):
        seen.append(kwargs)
        return {
            "status": "completed",
            "api_calls": 1,
            "final_response": json.dumps({"action": "retry", "rationale": "One near miss"}),
        }

    runtime = make_runtime(
        tmp_path,
        config=ProverConfig(prover_sessions=2, orchestrator_sessions=4, negation_sessions=0),
        session=decide,
    )
    node = runtime.dag.nodes[0]
    parent, assignment = runtime._new_job("prover", node=node)
    assert assignment["role_sessions"]["prover"]["remaining"] == 1
    assert assignment["role_sessions"]["prover"]["reserved"] == 1
    runtime._finish_job(parent, {"status": "completed", "api_calls": 1})
    decision = recovery_decision(runtime, node, {"notes": "One near miss"})
    assert decision["action"] == "retry"
    guidance = seen[0]["context"]
    assert guidance["role_sessions"]["orchestrator"]["remaining"] == 3
    assert guidance["recovery_actions"]["negate"]["can_start"] is False
    assert guidance["recovery_actions"]["retry"]["can_start"] is True
    assert guidance["recovery_actions"]["decompose"]["minimum_sessions_for_fresh_action"] == 3
    assert "negate is unavailable" in seen[0]["prompt"]


@pytest.mark.parametrize("role,limit", [("prover", 200), ("orchestrator", 50)])
def test_default_campaign_stops_at_the_native_invocation_limit(
    tmp_path: Path, role: str, limit: int
) -> None:
    from leanflow_cli.workflows.prover.config import ProverConfig

    runtime = make_runtime(tmp_path, config=ProverConfig())
    for attempt in range(limit):
        job, _ = runtime._new_job(role, node=runtime.dag.nodes[0], prompt=str(attempt))
        runtime._finish_job(job, {"status": "completed", "api_calls": 1})
    with pytest.raises(BudgetExhausted) as caught:
        runtime._new_job(role, node=runtime.dag.nodes[0], prompt="over budget")
    assert caught.value.code == role + "_sessions"
    assert runtime.state["metrics"]["role_sessions"][role]["used"] == limit
