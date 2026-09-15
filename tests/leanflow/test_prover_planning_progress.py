"""Stop unchanged planning failures without losing revised plans or resume progress."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import planning_controller
from leanflow_cli.workflows.prover.planning import planning_prompt
from leanflow_cli.workflows.prover.planning_progress import REPEATED_FAILURE_LIMIT
from tests.leanflow.test_prover_recovery import runtime_at


@pytest.mark.parametrize(
    "gate", ["empty", "malformed", "proposal", "review", "review_report", "skeleton"]
)
def test_repeated_failure_stops_before_dispatch_and_plain_resume_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    """Persist a recoverable stop with the rejected draft and all unproved roots intact."""
    calls: list[str] = []
    materializations = 0

    def session(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["role"])
        assert kwargs["role"] != "prover"
        if kwargs["role"] == "review":
            report = (
                {"critique": f"missing decision {len(calls)}"}
                if gate == "review_report"
                else {"accepted": gate != "review", "critique": "The obligation did not change."}
            )
        elif '"nodes"' in kwargs["prompt"]:
            if gate in {"empty", "malformed"}:
                return {
                    "status": "completed",
                    "api_calls": 1,
                    "final_response": "" if gate == "empty" else f"{{malformed {len(calls)}",
                }
            report = {"plan": "Use the same plan.", "nodes": {} if gate == "proposal" else []}
        else:
            report = {"plan": "Outline."}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}

    def materialize(*args: Any) -> None:
        nonlocal materializations
        materializations += 1
        raise ValueError("Unknown identifier in the unchanged helper.")

    monkeypatch.setattr(planning_controller, "materialize", materialize)
    runtime = runtime_at(tmp_path, session=session)
    baseline = (tmp_path / "Main.lean").read_bytes()
    result = runtime.run()
    assert result["status"] == "planning_stalled"
    assert result["stop_reason"]["code"] == "planning_stalled"
    assert result["stop_reason"]["remaining_api_calls"] > 0
    progress = result["planning_request"]["progress"]
    assert progress["next_attempt"] == REPEATED_FAILURE_LIMIT
    assert progress["stalled"]["repeats"] == REPEATED_FAILURE_LIMIT
    assert result["metrics"]["plan_refinements"] == 0
    assert all(node.status == "pending" for node in runtime.dag.nodes)
    assert materializations == (REPEATED_FAILURE_LIMIT if gate == "skeleton" else 0)
    expected_calls = 1 + REPEATED_FAILURE_LIMIT * (
        2 if gate in {"review", "review_report", "skeleton"} else 1
    )
    assert len(calls) == result["metrics"]["api_calls"] == expected_calls
    assert (tmp_path / "Main.lean").read_bytes() == baseline

    resumed = runtime_at(tmp_path, session=session, run_id=runtime.run_id, resume=True)
    again = resumed.run()
    assert again["status"] == "planning_stalled"
    assert len(calls) == again["metrics"]["api_calls"] == expected_calls
    assert again["planning_request"]["progress"] == progress
    assert not again.get("design_plan_complete")


def test_revised_plans_keep_exploring_after_three_matching_critiques(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged critique must not erase an actual revision to the proposed argument."""
    proposals = 0

    def session(**kwargs: Any) -> dict[str, Any]:
        nonlocal proposals
        if kwargs["role"] == "review":
            report = {"accepted": proposals == 5, "critique": "Address the remaining obligation."}
        elif '"nodes"' in kwargs["prompt"]:
            proposals += 1
            report = {"plan": f"Investigate argument {proposals}.", "nodes": []}
        else:
            report = {"plan": "Outline."}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}

    monkeypatch.setattr(planning_controller, "materialize", lambda *args: None)
    runtime = runtime_at(tmp_path, session=session)
    assert runtime._research_plan("Explore useful revisions.")
    assert proposals == 5
    assert runtime.state["metrics"]["api_calls"] == 11
    assert runtime.state["proposal_status"] == "accepted"
    assert all(node.status == "pending" for node in runtime.dag.nodes)


@pytest.mark.parametrize("gate", ["proposal", "review", "skeleton"])
def test_resume_skips_checkpointed_rejections_without_repeating_gates_or_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: str
) -> None:
    """Replay a crash immediately after the rejection checkpoint became durable."""
    proposals = 0
    reviews = 0
    materializations = 0
    contexts: list[dict[str, Any]] = []

    def session(**kwargs: Any) -> dict[str, Any]:
        nonlocal proposals, reviews
        if kwargs["role"] == "review":
            reviews += 1
            report = {"accepted": gate != "review" or proposals > 1, "critique": "Fix the draft."}
        elif '"nodes"' in kwargs["prompt"]:
            proposals += 1
            contexts.append(kwargs["context"])
            report = {
                "plan": f"Plan {proposals}.",
                "nodes": {} if gate == "proposal" and proposals == 1 else [],
            }
        else:
            report = {"plan": "Outline."}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}

    def materialize(*args: Any) -> None:
        nonlocal materializations
        materializations += 1
        if gate == "skeleton" and materializations == 1:
            raise ValueError("Bad helper skeleton.")

    monkeypatch.setattr(planning_controller, "materialize", materialize)
    first = runtime_at(tmp_path, session=session)
    persist = first._persist

    def interrupt_after_rejection() -> None:
        persist()
        if first.state.get("planning_request", {}).get("progress", {}).get("next_attempt") == 1:
            raise SystemExit("crash after committed rejection")

    monkeypatch.setattr(first, "_persist", interrupt_after_rejection)
    with pytest.raises(SystemExit, match="committed rejection"):
        first._research_plan("Design a plan.")
    resumed = runtime_at(tmp_path, session=session, run_id=first.run_id, resume=True)
    assert resumed._research_plan("Resume planning.")
    assert proposals == 2
    assert reviews == (1 if gate == "proposal" else 2)
    assert materializations == (2 if gate == "skeleton" else 1)
    assert resumed.state["metrics"]["api_calls"] == 1 + proposals + reviews
    assert resumed.state["metrics"]["reserved_api_calls"] == 0
    assert sum(entry["count"] for entry in resumed.state["plan_journal"]) == 1
    assert contexts[1]["previous_proposal"]["plan"] == "Plan 1."
    assert contexts[1]["planning_critique"]
    assert "planning_request" not in resumed.state


def test_review_prompt_distinguishes_historical_critiques_from_verified_evidence() -> None:
    """An old reviewer mistake must not become a permanent mathematical constraint."""
    from leanflow_cli.workflows.prover.plan_journal import render

    prompt = planning_prompt(reason="Review the corrected draft.", review=True)
    assert "fallible assessments of earlier drafts" in prompt
    assert "do not repeat an obsolete complaint" in prompt
    assert "Independent Lean verification remains the authority" in prompt
    assert "fallible" in render(
        [{"kind": "reviewer rejected the graph", "detail": "Missing placeholder"}]
    )


@pytest.mark.parametrize("committed", [False, True])
def test_resume_finishes_materialized_plan_without_rejecting_its_multiline_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    """Tie acceptance to the durable source transaction, and charge refinement exactly once."""
    from leanflow_cli.workflows.prover import source_transaction

    (tmp_path / "Main.lean").write_text("theorem goal : True ∧ True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    calls: list[str] = []
    materializations = 0

    def session(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["role"])
        if kwargs["role"] == "review":
            report = {"accepted": True, "change_kind": "direction"}
        elif '"nodes"' in kwargs["prompt"]:
            report = {
                "plan": "Use a smaller helper for both conjuncts.",
                "nodes": [
                    {"id": first.dag.roots[0], "dependencies": ["h"]},
                    {
                        "id": "h",
                        "name": "h",
                        "file": "LeanFlowProofs/H.lean",
                        "statement": "theorem h : True := by\n  sorry",
                    },
                ],
            }
        else:
            report = {"plan": "Outline."}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}

    original_materialize = planning_controller.materialize
    original_commit = source_transaction.commit_materialization

    def materialize(*args: Any) -> None:
        nonlocal materializations
        materializations += 1
        original_materialize(*args)

    def interrupt_commit(runtime: Any) -> None:
        if committed:
            original_commit(runtime)
        raise SystemExit("interrupt at materialization commit")

    monkeypatch.setattr(planning_controller, "materialize", materialize)
    monkeypatch.setattr(source_transaction, "commit_materialization", interrupt_commit)
    first = runtime_at(tmp_path, session=session)
    with pytest.raises(SystemExit, match="materialization commit"):
        first._research_plan("Change direction.", refinement=True)
    monkeypatch.setattr(source_transaction, "commit_materialization", original_commit)
    resumed = runtime_at(tmp_path, session=session, run_id=first.run_id, resume=True)
    assert resumed._research_plan("Resume.")
    assert calls == ["orchestrator", "orchestrator", "review"]
    assert materializations == (1 if committed else 2)
    assert resumed.state["metrics"]["api_calls"] == 3
    assert resumed.state["metrics"]["plan_refinements"] == 1
    assert resumed.state["proposal_status"] == "accepted"
    assert resumed.state["plan_markdown"] == "Use a smaller helper for both conjuncts."
    assert not resumed.state.get("plan_journal")
    assert "planning_request" not in resumed.state
    assert len(resumed.dag.nodes) == 2
    assert all(node.status == "pending" for node in resumed.dag.nodes)
    assert "theorem h : True := by\n  sorry" in (tmp_path / "LeanFlowProofs/H.lean").read_text()


def test_legacy_checkpoint_skips_only_attempts_with_an_allocated_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migrate a real interrupted old-style checkpoint without replaying its rejected gate."""
    proposal_calls = 0
    materializations = 0

    def session(**kwargs: Any) -> dict[str, Any]:
        nonlocal proposal_calls
        if kwargs["role"] == "review":
            report = {"accepted": True}
        elif '"nodes"' in kwargs["prompt"]:
            proposal_calls += 1
            if proposal_calls == 2:
                first.state["planning_request"].pop("progress")
                first._persist()
                raise SystemExit("legacy interrupted second proposal")
            if proposal_calls == 3:
                assert kwargs["context"]["previous_proposal"]["plan"] == "Plan 1."
                assert "Bad helper" in kwargs["context"]["planning_critique"]
            report = {"plan": f"Plan {proposal_calls}.", "nodes": []}
        else:
            report = {"plan": "Outline."}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(report)}

    def materialize(*args: Any) -> None:
        nonlocal materializations
        materializations += 1
        if materializations == 1:
            raise ValueError("Bad helper skeleton.")

    monkeypatch.setattr(planning_controller, "materialize", materialize)
    first = runtime_at(tmp_path, session=session)
    with pytest.raises(SystemExit, match="legacy interrupted"):
        first._research_plan("Design.")
    resumed = runtime_at(tmp_path, session=session, run_id=first.run_id, resume=True)
    assert resumed._research_plan("Resume.")
    assert materializations == 2
    assert proposal_calls == 3
    assert resumed.state["metrics"]["api_calls"] == 5
    assert sum(entry["count"] for entry in resumed.state["plan_journal"]) == 1
