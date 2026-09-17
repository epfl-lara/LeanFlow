"""Recover stalled attempts through real controller journals and bounded mock sessions."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import negation_job, recovery
from leanflow_cli.workflows.prover.runtime import BudgetExhausted, ProverRuntime
from leanflow_cli.workflows.prover.session_response_guard import ProverResponseGuard
from tests.leanflow.test_prover_response_guard import Verifier, make_runtime, stalled_result


def resume(runtime: ProverRuntime, **kwargs: Any) -> ProverRuntime:
    """Reopen an existing persisted campaign without invoking a model during restore."""
    return ProverRuntime(
        root=runtime.root,
        targets=runtime.targets,
        config=runtime.config,
        run_id=runtime.run_id,
        resume=True,
        verifier=Verifier(),
        session=kwargs.pop("session", runtime.session),
        **kwargs,
    )


def decision_result(action: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": "complete",
        "api_calls": 1,
        "final_response": json.dumps({"action": action, "rationale": "Focused recovery", **extra}),
    }


@pytest.mark.parametrize("action", ["retry", "continue"])
def test_recovery_creates_new_attempt_preserving_spend_guard_and_partial_work(
    tmp_path: Path, action: str
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    scratch = Path(job["scratch_path"])
    scratch.write_text(
        scratch.read_text().replace("sorry", "\n  have h : True := by trivial\n  sorry", 1)
    )
    plan = Path(job["workspace"]) / "PLAN_job.md"
    plan.write_text("Keep h; discharge the remaining goal with exact h.")
    guard = ProverResponseGuard(plan.parent.parent / ".runtime" / job["id"], role="prover")
    for used in (1, 2):
        guard.observe(
            {"content": "", "finish_reason": "length"}, candidate_ready=False, used=used, limit=5
        )
    saved_guard = guard.path.read_bytes()
    seen = []

    def session(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return decision_result(action, instructions="Use exact h for the remaining goal.")

    runtime.session = session
    runtime._handle_result(job, stalled_result())
    assert node.status == "retry"
    assert len(seen) == 1 and seen[0]["role"] == "orchestrator"
    prompt = seen[0]["prompt"]
    assert "response_stalled" in prompt and job["id"] in prompt
    assert plan.read_text() in prompt
    assert "scope" in prompt and "Saved proof artifacts" in prompt
    assert job["response_recovery_enqueued"] is True
    assert runtime.consumed == 3
    assert runtime.state["metrics"]["decompositions"] == 1
    assert runtime.state["recovery_decisions"][0]["source_job_id"] == job["id"]
    assert (
        runtime.state["prover_recovery_guidance"][node.id]["instructions"]
        == "Use exact h for the remaining goal."
    )

    reopened = resume(runtime)
    assert not reopened.state.get("recovery_in_flight")
    assert node.id not in reopened.resume_jobs
    next_job, context = reopened._new_job("prover", node=reopened.dag.by_id()[node.id])
    assert next_job["id"] != job["id"]
    assert next_job["api_calls"] == 0
    assert next_job["api_budget"] == runtime.config.job_api_calls
    assert "have h : True" in Path(next_job["scratch_path"]).read_text()
    assert (Path(next_job["workspace"]) / "PLAN_job.md").read_text() == plan.read_text()
    assert context["previous_job"]["id"] == job["id"]
    assert guard.path.read_bytes() == saved_guard
    assert ProverResponseGuard(guard.path.parent, role="prover").stopped
    assert reopened.consumed == 3


def test_old_terminal_blocks_migrate_once_even_without_result_file(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    runtime._finish_job(job, stalled_result())
    job["result_processed"] = True
    Path(job["result_path"]).unlink()
    node.status = "blocked"
    runtime._persist()

    first = resume(runtime)
    journal = first.state["recovery_in_flight"][node.id]
    assert journal["stage"] == "decide"
    assert journal["report"]["job_id"] == job["id"]
    assert journal["report"]["stop_reason"]["code"] == "response_stalled"
    assert first.consumed == 2 and first.state["metrics"]["decompositions"] == 0
    again = resume(first)
    assert again.state["recovery_in_flight"][node.id] == journal
    again._recover(again.dag.by_id()[node.id])
    assert again.state["recovery_decisions"][0]["action"] == "retry"
    stopped = resume(again)
    assert stopped.dag.by_id()[node.id].status == "retry"
    assert not stopped.state.get("recovery_in_flight")
    assert len(stopped.state["recovery_decisions"]) == 1
    assert stopped.consumed == 3


def test_legacy_migration_preserves_candidate_and_newer_revision(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    node, revised = runtime.dag.nodes
    for item in (node, revised):
        job, _ = runtime._new_job("prover", node=item)
        runtime._finish_job(job, stalled_result())
        job["result_processed"] = True
        item.status = "blocked"
    node.candidate = ["trivial"]
    revised.revision += 1
    revised.status = "pending"
    runtime._persist()
    reopened = resume(runtime)
    assert reopened.dag.by_id()[node.id].candidate == ["trivial"]
    assert reopened.dag.by_id()[node.id].status == "candidate"
    assert reopened.dag.by_id()[revised.id].status == "pending"
    assert not reopened.state.get("recovery_in_flight")


def test_decision_replay_after_crash_is_once_without_recharge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    runtime._finish_job(job, stalled_result())
    node.status = "blocked"
    runtime._persist()
    reopened = resume(
        runtime,
        session=lambda **kw: decision_result("continue", instructions="Try exact True.intro."),
    )
    original = recovery.recovery_decision

    def decide_then_crash(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise SystemExit("after reply persisted")

    monkeypatch.setattr(recovery, "recovery_decision", decide_then_crash)
    with pytest.raises(SystemExit):
        reopened._recover(reopened.dag.by_id()[node.id])
    monkeypatch.setattr(recovery, "recovery_decision", original)
    second = resume(reopened, session=lambda **kw: pytest.fail("Persisted decision must replay"))
    second._recover(second.dag.by_id()[node.id])
    assert second.dag.by_id()[node.id].status == "retry"
    assert second.state["metrics"]["decompositions"] == 1
    assert len(second.state["recovery_decisions"]) == 1
    assert second.consumed == 3
    third = resume(second)
    assert not third.state.get("recovery_in_flight")
    assert (
        third.state["prover_recovery_guidance"][node.id]["instructions"] == "Try exact True.intro."
    )


def test_stalls_obey_campaign_recovery_budget_instead_of_private_node_limit(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.config = replace(runtime.config, max_decompositions=3)
    runtime.state["config"] = asdict(runtime.config)
    runtime.session = lambda **kw: decision_result("retry")
    node = runtime.dag.nodes[0]
    for _ in range(4):
        job, _ = runtime._new_job("prover", node=node)
        runtime._handle_result(job, stalled_result())
    assert len(runtime.state["recovery_decisions"]) == 3
    assert runtime.state["metrics"]["decompositions"] == 3
    assert node.status == "blocked"
    assert runtime.consumed == 11
    assert "Campaign recovery budget exhausted (3/3)" in node.notes
    assert resume(runtime).state["recovery_in_flight"][node.id]["stage"] == "budget_exhausted"


def test_stall_never_spends_calls_beyond_remaining_campaign_allocation(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.config = replace(runtime.config, total_api_calls=2, job_api_calls=2)
    runtime.state["config"] = asdict(runtime.config)
    runtime.session = lambda **kw: pytest.fail("No campaign calls remain")
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    with pytest.raises(BudgetExhausted):
        runtime._handle_result(job, stalled_result())
    assert runtime.consumed == 2
    assert len(runtime.state["jobs"]) == 1
    assert runtime.state["recovery_in_flight"][node.id]["stage"] == "decide"
    reopened = resume(runtime)
    with pytest.raises(BudgetExhausted):
        reopened._recover(reopened.dag.by_id()[node.id])
    assert reopened.consumed == 2
    assert reopened.state["metrics"]["decompositions"] == 1


def test_decompose_decision_uses_existing_planning_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = make_runtime(tmp_path)
    runtime.session = lambda **kw: decision_result("decompose")
    node = runtime.dag.nodes[0]
    planned = []

    def plan(reason: str, **kwargs: Any) -> bool:
        planned.append((reason, kwargs))
        runtime.state.pop("planning_request", None)
        runtime.state["recovery_in_flight"][node.id]["applied"] = True
        return True

    monkeypatch.setattr(runtime, "_research_plan", plan)
    job, _ = runtime._new_job("prover", node=node)
    runtime._handle_result(job, stalled_result())
    assert len(planned) == 1 and "Repair only the branch" in planned[0][0]
    assert planned[0][1]["affected"] == runtime.dag.affected(node.id)
    assert node.status == "retry"
    assert runtime.state["metrics"]["decompositions"] == 1
    assert not runtime.state.get("recovery_in_flight")
    assert node.id not in runtime.state.get("prover_recovery_guidance", {})


def test_stalled_negation_redecides_once_with_exact_failure_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = make_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    actions = iter(["negate", "continue"])
    prompts = []

    def decide(**kwargs: Any) -> dict[str, Any]:
        prompts.append(kwargs["prompt"])
        return decision_result(
            next(actions), instructions="Return to the positive claim; use constructor."
        )

    def negate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        job, _ = runtime._new_job("negation", node=node)
        runtime._finish_job(job, stalled_result())
        return {"certified": False, "notes": "No usable action"}

    runtime.session = decide
    monkeypatch.setattr(recovery, "empirical_screen", lambda *a, **kw: {})
    monkeypatch.setattr(negation_job, "attempt_negation", negate)
    job, _ = runtime._new_job("prover", node=node)
    runtime._handle_result(job, stalled_result())
    assert len(prompts) == 2
    negation = next(j for j in runtime.state["jobs"] if j["role"] == "negation")
    assert negation["id"] in prompts[1] and '"role": "negation"' in prompts[1]
    assert runtime.state["recovery_decisions"][1]["source_job_id"] == negation["id"]
    assert node.status == "retry" and runtime.consumed == 6
    assert not resume(runtime).state.get("recovery_in_flight")


def test_standard_mode_stall_remains_blocked_without_orchestrator(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.config = replace(runtime.config, mode="standard")
    runtime.state["config"] = asdict(runtime.config)
    runtime.session = lambda **kw: pytest.fail("Standard mode has no orchestrator")
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    scratch = Path(job["scratch_path"])
    scratch.write_text(
        scratch.read_text().replace("sorry", "\n  have h : True := by trivial\n  sorry", 1)
    )
    runtime._handle_result(job, stalled_result())
    assert node.status == "blocked"
    assert not runtime.state.get("recovery_in_flight")
    assert runtime.consumed == 2
    assert not resume(runtime).state.get("recovery_in_flight")
