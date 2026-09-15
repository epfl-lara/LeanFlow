"""Preserve submitted helper declarations across the planner-to-reviewer handoff."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import planning_controller
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime
from leanflow_cli.workflows.prover.session_assignment import compact_assignment


@pytest.mark.parametrize("compact", [False, True])
def test_review_and_materialization_receive_the_same_complete_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compact: bool
) -> None:
    """Keep PB028's missing-placeholder rejection out of the review handoff."""
    source = tmp_path / "Main.lean"
    baseline = "theorem goal : True ∧ True := by sorry\n"
    source.write_text(baseline)
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    declarations = {
        "helper": "/-- Preserve the submitted declaration. -/\ntheorem helper : True := by\n  sorry",
        "reflexive": "theorem reflexive (n : Nat) : n = n := by sorry",
    }
    reviews: list[dict[str, Any]] = []
    prompts: list[str] = []
    materialized: list[dict[str, str]] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        if kwargs["role"] == "review":
            context = kwargs["context"]
            if compact:
                assignment, artifacts = compact_assignment(
                    kwargs["prompt"], context, workspace=tmp_path, token_budget=1
                )
                assert artifacts
                context = json.loads(assignment["content"].split("Assignment context:\n", 1)[1])
            reviews.append(context)
            prompts.append(kwargs["prompt"])
            response = {"accepted": True, "critique": "Use the two helper obligations."}
        elif '"nodes"' in kwargs["prompt"]:
            response = {
                "plan": "Use the helpers to prove the original conjunction.",
                "nodes": [
                    {"id": runtime.dag.roots[0], "dependencies": list(declarations)},
                    *[
                        {
                            "id": name,
                            "name": name,
                            "statement": declaration,
                            "file": f"LeanFlowProofs/{name}.lean",
                            "dependencies": [],
                        }
                        for name, declaration in declarations.items()
                    ],
                ],
            }
        else:
            response = {"plan": "Split the original conjunction."}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(response)}

    def materialize(runtime: ProverRuntime, updated: Any, skeletons: dict[str, str]) -> None:
        materialized.append(dict(skeletons))

    monkeypatch.setattr(planning_controller, "materialize", materialize)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[source],
        config=ProverConfig(mode="research"),
        session=session,
    )
    if compact:
        runtime.dag.by_id()[runtime.dag.roots[0]].notes = "Prior evidence. " * 1000
    assert runtime._research_plan("Construct a useful graph.")
    assert len(reviews) == 1
    reviewed = {node["id"]: node for node in reviews[0]["proposed_dag"]["nodes"]}
    assert {name: reviewed[name]["statement"] for name in declarations} == declarations
    assert materialized == [declarations]
    assert set(reviews[0]["new_helper_ids"]) == set(declarations)
    assert "signature-only" in prompts[0]
    assert "exact submitted declarations" in prompts[0]
    # The review projection must not rewrite stored claims or mark obligations proved.
    for name in declarations:
        assert ":= by" not in runtime.dag.by_id()[name].statement
        assert runtime.dag.by_id()[name].status == "pending"
    assert runtime.state["phase"] == "proving"
    assert runtime.state["proposal_status"] == "accepted"
    assert source.read_text() == baseline
