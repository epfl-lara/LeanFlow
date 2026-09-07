"""Retain and label rejected graph proposals across fresh planning and review contexts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import planning_controller
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime


@pytest.mark.parametrize("gate", ["proposal", "review", "skeleton"])
def test_retry_receives_rejected_proposal_and_exact_gate_feedback(tmp_path, monkeypatch, gate):
    source = tmp_path / "Main.lean"
    source.write_text("theorem goal : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    proposals = []
    first = {"plan": "Retain this concrete argument.", "nodes": []}
    reviews = 0
    materializations = 0

    def session(**kwargs: Any) -> dict[str, Any]:
        nonlocal reviews
        if kwargs["role"] == "review":
            reviews += 1
            response = {
                "accepted": gate != "review" or reviews > 1,
                "critique": "The proposed split leaves the same hard obligation.",
            }
        elif (
            "previous_proposal" in kwargs["context"]
            or "Return JSON" in kwargs["prompt"]
            and "nodes" in kwargs["prompt"]
        ):
            proposals.append(kwargs["context"])
            response = first if len(proposals) == 1 else {"plan": "Repaired proposal.", "nodes": []}
        else:
            response = {"plan": "Initial outline."}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(response)}

    def materialize(*args: Any) -> None:
        nonlocal materializations
        materializations += 1
        if gate == "skeleton" and materializations == 1:
            raise ValueError("unknown identifier in the proposed helper")

    monkeypatch.setattr(planning_controller, "materialize", materialize)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[source],
        config=ProverConfig(mode="research"),
        session=session,
    )
    if gate == "proposal":
        first["nodes"] = [{"id": runtime.dag.roots[0], "statement": "theorem goal : False"}]
    assert runtime._research_plan("Construct a useful graph.")
    assert len(proposals) == 2
    assert proposals[1]["previous_proposal"] == first
    expected = {
        "proposal": "existing statements are immutable",
        "review": "same hard obligation",
        "skeleton": "unknown identifier",
    }[gate]
    assert expected in proposals[1]["planning_critique"]
    assert "previous_proposal" not in proposals[0]
    assert source.read_text() == "theorem goal : True := by sorry\n"


def test_exhausted_refinement_budget_labels_the_dropped_direction_change_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer-accepted direction change the budget cannot admit must not stay "proposed"."""
    source = tmp_path / "Main.lean"
    source.write_text("theorem goal : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')

    def session(**kwargs: Any) -> dict[str, Any]:
        repairing = "Repair only" in kwargs["prompt"]
        if kwargs["role"] == "review":
            response = {"accepted": True, "change_kind": "direction", "critique": "Sound."}
        else:
            response = {
                "plan": "Switch to induction." if repairing else "Argue directly.",
                "nodes": [],
                "change_kind": "direction",
            }
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(response)}

    # The budget exit fires before skeleton materialization; skip the Lean gate entirely.
    monkeypatch.setattr(planning_controller, "materialize", lambda *args: None)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[source],
        config=ProverConfig(mode="research", plan_refinements=0),
        session=session,
    )
    assert runtime._research_plan("Construct a useful graph.")
    assert runtime.state["proposal_status"] == "accepted"
    assert runtime.state["plan_markdown"] == "Argue directly."
    assert not runtime._research_plan(
        "Repair only the branch for goal.", affected={runtime.dag.roots[0]}
    )
    persisted = json.loads((runtime.store.directory / "state.json").read_text())
    for state in (runtime.state, persisted):
        assert state["proposal_status"] == "rejected"
        assert state["proposal_critique"].startswith("Plan refinement budget exhausted")
        assert state["phase"] == "proving"
        assert state["plan_markdown"] == "Argue directly."
        assert state["proposed_plan"] == "Switch to induction."
        assert "planning_request" not in state
    assert runtime.state["metrics"]["plan_refinements"] == 0
    events = [
        json.loads(line)["event"]
        for line in (runtime.store.directory / "events.jsonl").read_text().splitlines()
    ]
    assert events.count("plan_refinement_budget_exhausted") == 1
    assert "plan_rejected" not in events
