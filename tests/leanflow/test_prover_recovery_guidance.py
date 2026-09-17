"""Check actionable recovery decisions and the exact next prover request."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.workflows.prover import agent_session, recovery
from leanflow_cli.workflows.prover.runtime import InfrastructureFailure
from tests.leanflow.test_prover_recovery_decisions import research_runtime


@pytest.mark.parametrize("action", ["retry", "continue"])
def test_decision_preserves_explicit_instructions(action: str) -> None:
    instructions = "Use the saved h lemma; check one rewrite before expanding the proof."
    result = recovery.parse_decision(json.dumps({"action": action, "instructions": instructions}))
    assert result["action"] == action
    assert result["instructions"] == instructions
    assert not result["fallback"]


@pytest.mark.parametrize("instructions", [None, [], {}, 12, "", "   ", "x" * 12001])
def test_continue_requires_bounded_text_instructions(instructions: Any) -> None:
    result = recovery.parse_decision(
        json.dumps({"action": "continue", "instructions": instructions})
    )
    assert result["action"] == "invalid"
    assert result["fallback"]


def test_recovery_prompt_has_failure_evidence_and_all_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = research_runtime(tmp_path)
    prompts: list[str] = []

    def decide(role: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        prompts.append(prompt)
        return {"final_response": '{"action":"stop","rationale":"No useful next action"}'}

    monkeypatch.setattr(runtime, "_run_role", decide)
    recovery.recovery_decision(
        runtime,
        runtime.dag.nodes[0],
        {
            "status": "response_stalled",
            "job_id": "prover_00037",
            "stop_reason": {"response_kind": "truncated", "unusable_responses": 2},
            "response_failure": {"plan_tail": "Saved partial proof reduces goal to h."},
        },
    )
    prompt = prompts[0]
    for action in recovery.ACTIONS:
        assert "- " + action + ":" in prompt
    for evidence in (
        "prover_00037",
        "truncated",
        "unusable_responses",
        "Saved partial proof reduces goal to h.",
    ):
        assert evidence in prompt


def test_context_failure_is_not_cached_as_a_completed_decision(tmp_path: Path) -> None:
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    runtime._enqueue_recovery(node, {"status": "response_stalled"})
    runtime.session = lambda **kwargs: {
        "status": "context_limit",
        "api_calls": 0,
        "final_response": "",
    }
    with pytest.raises(InfrastructureFailure):
        recovery.recovery_decision(runtime, node, {})
    assert "decision_result" not in runtime.state["recovery_in_flight"][node.id]


def test_decomposition_instructions_reach_the_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    instructions = "Split the conjunction into two independently checkable helpers."
    runtime.session = lambda **kwargs: {
        "status": "completed",
        "api_calls": 1,
        "final_response": json.dumps({"action": "decompose", "instructions": instructions}),
    }
    plans: list[str] = []
    monkeypatch.setattr(
        runtime, "_research_plan", lambda reason, **kwargs: plans.append(reason) or True
    )
    runtime._recover(node, {"status": "response_stalled"})
    assert len(plans) == 1 and instructions in plans[0]


@pytest.mark.parametrize(("role", "action"), [("prover", "continue"), ("negation", "negate")])
def test_instruction_snapshot_is_role_and_revision_scoped(
    tmp_path: Path, role: str, action: str
) -> None:
    from leanflow_cli.workflows.prover.response_recovery import record_recovery_guidance

    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    instructions = "Check the concrete saved witness first."
    record_recovery_guidance(runtime, node, {"action": action, "instructions": instructions}, {})
    contexts: list[dict[str, Any]] = []
    runtime.session = lambda **kwargs: contexts.append(kwargs["context"]) or {
        "status": "completed",
        "api_calls": 0,
    }
    job, context = runtime._new_job(role, node=node)
    runtime._invoke(job, context, "assignment")
    assert contexts[-1]["recovery_guidance"]["instructions"] == instructions
    runtime.state["prover_recovery_guidance"][node.id]["instructions"] = "Changed later"
    runtime._invoke(job, context, "assignment")
    assert contexts[-1]["recovery_guidance"]["instructions"] == instructions
    stale_job = {**job, "node_revision": node.revision + 1}
    runtime._invoke(stale_job, context, "assignment")
    assert "recovery_guidance" not in contexts[-1]


def test_stall_decision_instructions_reach_actual_provider_with_saved_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = research_runtime(tmp_path, total_api_calls=100, orchestrator_api_calls=2)
    runtime.state["design_plan_complete"] = True
    runtime._persist()
    statement = runtime.dag.nodes[0].statement
    instructions = "Continue the saved h proof. Use exact True.intro for h, then exact h."
    provider_requests: list[list[dict[str, Any]]] = []
    orchestrator_requests: list[str] = []
    monkeypatch.setattr(
        agent_session, "build_transport", lambda *args, **kwargs: SimpleNamespace(model="test")
    )
    monkeypatch.setattr(agent_session, "close_transport", lambda *args: None)

    def request(
        agent: Any, messages: list[dict[str, Any]], timeout: float
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        provider_requests.append(json.loads(json.dumps(messages)))
        context = json.loads(messages[1]["content"].split("Assignment context:\n", 1)[1])
        assert context["assignment"]["statement"] == statement
        guidance = context.get("recovery_guidance")
        if guidance is None:
            return {
                "role": "assistant",
                "content": "",
                "reasoning": "repeated derivation",
                "finish_reason": "length",
            }, {"output_tokens": 65536}
        assert guidance["action"] == "continue"
        assert guidance["instructions"] == instructions
        assert guidance["source_job_id"] == "prover_00001"
        assert "have h : True" in Path(context["scratch_file"]).read_text()
        assert "Finish the concrete h subgoal" in Path(context["plan_path"]).read_text()
        return {"role": "assistant", "content": '{"proof":"trivial"}', "finish_reason": "stop"}, {
            "output_tokens": 10
        }

    monkeypatch.setattr(agent_session, "request_once", request)

    def session(**kwargs: Any) -> dict[str, Any]:
        if kwargs["role"] == "orchestrator":
            orchestrator_requests.append(kwargs["prompt"])
            assert "response_stalled" in kwargs["prompt"]
            assert "truncated" in kwargs["prompt"]
            assert "Finish the concrete h subgoal" in kwargs["prompt"]
            return {
                "status": "completed",
                "api_calls": 1,
                "final_response": json.dumps(
                    {
                        "action": "continue",
                        "instructions": instructions,
                        "rationale": "A small saved subgoal remains.",
                    }
                ),
            }
        if not kwargs["context"].get("recovery_guidance"):
            scratch = Path(kwargs["context"]["scratch_file"])
            scratch.write_text(
                scratch.read_text().replace("sorry", "\n  have h : True := by sorry\n  exact h")
            )
            (Path(kwargs["workspace"]) / "PLAN_job.md").write_text("Finish the concrete h subgoal.")
        return agent_session.run_session(**kwargs)

    runtime.session = session
    result = runtime.run()
    assert result["status"] == "completed"
    assert len(orchestrator_requests) == 1
    assert len(provider_requests) == 3
    assert result["metrics"]["api_calls"] == 4
    assert result["metrics"]["decompositions"] == 1
    jobs = result["jobs"]
    assert [job["role"] for job in jobs] == ["prover", "orchestrator", "prover"]
    assert jobs[0]["status"] == "response_stalled" and jobs[0]["api_calls"] == 2
    assert jobs[2]["recovery_guidance"]["instructions"] == instructions
    guard = Path(jobs[0]["workspace"]).parent / ".runtime" / jobs[0]["id"] / "response-guard.json"
    assert json.loads(guard.read_text())["stopped"] is True


def test_guidance_survives_assignment_and_history_compaction(tmp_path: Path) -> None:
    from leanflow_cli.workflows.prover.session_assignment import compact_assignment
    from leanflow_cli.workflows.prover.session_context import compact_history

    guidance = {
        "action": "continue",
        "instructions": "Use the proved transport lemma, then one rewrite.",
    }
    statement = "theorem exact_helper : True := by sorry"
    assignment, _ = compact_assignment(
        "Prove this helper",
        {
            "assignment": {"statement": statement, "notes": "old notes " * 10000},
            "recovery_guidance": guidance,
        },
        workspace=tmp_path,
        token_budget=2000,
    )
    messages = [{"role": "system", "content": "contract"}, assignment]
    result, changed = compact_history(
        messages + [{"role": "assistant", "content": "old history " * 10000}],
        context_tokens=3000,
        target_tokens=1000,
        hard_limit=4000,
        workspace=tmp_path,
    )
    assert changed
    assert result[1] == assignment
    assert statement in result[1]["content"]
    assert guidance["instructions"] in result[1]["content"]
