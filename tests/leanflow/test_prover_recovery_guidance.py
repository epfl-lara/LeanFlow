"""Check actionable recovery decisions and the exact next prover request."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import recovery
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
