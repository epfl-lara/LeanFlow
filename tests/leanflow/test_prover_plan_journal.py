"""Carry planning findings across the fresh contexts that would otherwise forget them."""

from __future__ import annotations

import json
from typing import Any

import pytest

from leanflow_cli.workflows.prover import plan_journal, planning_controller
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.runtime import ProverRuntime


def test_exact_repeats_collapse_into_one_counted_entry() -> None:
    """A constraint violated twice must read as twice, not fill the record."""
    state: dict[str, Any] = {}
    plan_journal.record(state, "rejected plan draft", "name does not match", at="t1")
    plan_journal.record(state, "rejected plan draft", "name does not match", at="t2")
    (entry,) = state["plan_journal"]
    assert (entry["count"], entry["first_at"], entry["last_at"]) == (2, "t1", "t2")


def test_a_repeatedly_violated_constraint_survives_eviction() -> None:
    """Eviction is least-recently-seen, so the recurring complaint is the one kept."""
    state: dict[str, Any] = {}
    plan_journal.record(state, "rejected plan draft", "the recurring one", at="t0")
    for index in range(plan_journal.MAX_ENTRIES * 2):
        plan_journal.record(state, "rejected plan draft", f"one-off {index}", at=f"t{index + 1}")
        # Seen again, so it keeps refreshing its position ahead of the noise.
        plan_journal.record(state, "rejected plan draft", "the recurring one", at=f"t{index + 1}z")
    journal = state["plan_journal"]
    assert len(journal) == plan_journal.MAX_ENTRIES
    assert any(entry["detail"] == "the recurring one" for entry in journal)


def test_details_are_normalized_bounded_and_never_empty() -> None:
    state: dict[str, Any] = {}
    assert plan_journal.record(state, "note", "   \n  ", at="t1") is None
    assert plan_journal.record(state, "note", "a\n  b   c", at="t1")["detail"] == "a b c"
    long = plan_journal.record(state, "note", "x" * (plan_journal.MAX_DETAIL + 500), at="t2")
    assert len(long["detail"]) == plan_journal.MAX_DETAIL
    assert state["plan_journal"] and all(e["detail"] for e in state["plan_journal"])


def test_render_is_empty_until_there_is_something_to_say() -> None:
    assert plan_journal.render([]) == ""
    assert plan_journal.render(None) == ""
    text = plan_journal.render(
        [{"kind": "rejected plan draft", "detail": "immutable statements", "count": 3}]
    )
    assert "## Planning journal" in text
    assert "(seen 3x)" in text and "immutable statements" in text


def _planning_runtime(tmp_path, monkeypatch, *, reject_reviews: int):
    """A runtime whose reviewer rejects the first `reject_reviews` proposals."""
    source = tmp_path / "Main.lean"
    source.write_text("theorem goal : True := by sorry\n")
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n')
    proposals: list[dict[str, Any]] = []
    reviews = 0

    def session(**kwargs: Any) -> dict[str, Any]:
        nonlocal reviews
        if kwargs["role"] == "review":
            reviews += 1
            response = {
                "accepted": reviews > reject_reviews,
                "critique": f"Rejection {reviews}: the split leaves the same hard obligation.",
            }
        elif "previous_proposal" in kwargs["context"] or "nodes" in kwargs["prompt"]:
            proposals.append(kwargs["context"])
            response = {"plan": f"Proposal {len(proposals)}.", "nodes": []}
        else:
            response = {"plan": "Initial outline."}
        return {"status": "completed", "api_calls": 1, "final_response": json.dumps(response)}

    monkeypatch.setattr(planning_controller, "materialize", lambda *args: None)
    runtime = ProverRuntime(
        root=tmp_path,
        targets=[source],
        config=ProverConfig(mode="research"),
        session=session,
    )
    return runtime, proposals


def test_the_next_proposal_context_carries_the_earlier_rejection(tmp_path, monkeypatch) -> None:
    """The loop fix: attempt N+1 sees what attempt N was rejected for.

    Each proposal runs in a fresh model context holding only the previous draft
    and the newest critique, so without the journal a planner satisfies the new
    complaint while reintroducing an older one.
    """
    runtime, proposals = _planning_runtime(tmp_path, monkeypatch, reject_reviews=2)
    assert runtime._research_plan("Construct a useful graph.")
    assert len(proposals) == 3

    assert proposals[0].get("planning_journal") == []
    second = proposals[1]["planning_journal"]
    assert [entry["detail"] for entry in second] == [
        "Rejection 1: the split leaves the same hard obligation."
    ]
    # By the third attempt BOTH earlier rejections are present together --
    # exactly the pairing a single `planning_critique` can never deliver.
    third = [entry["detail"] for entry in proposals[2]["planning_journal"]]
    assert third == [
        "Rejection 1: the split leaves the same hard obligation.",
        "Rejection 2: the split leaves the same hard obligation.",
    ]


def test_an_accepted_plan_does_not_take_the_journal_with_it(tmp_path, monkeypatch) -> None:
    """plan_markdown is replaced wholesale on acceptance; the journal must not be."""
    runtime, _ = _planning_runtime(tmp_path, monkeypatch, reject_reviews=1)
    assert runtime._research_plan("Construct a useful graph.")
    assert runtime.state["plan_markdown"] == "Proposal 2."
    assert [entry["detail"] for entry in runtime.state["plan_journal"]] == [
        "Rejection 1: the split leaves the same hard obligation."
    ]


def test_plan_md_shows_the_plan_and_the_journal_together(tmp_path, monkeypatch) -> None:
    runtime, _ = _planning_runtime(tmp_path, monkeypatch, reject_reviews=1)
    assert runtime._research_plan("Construct a useful graph.")
    plan = (runtime.store.directory / "PLAN.md").read_text()
    assert "Proposal 2." in plan
    assert "## Planning journal" in plan
    assert "Rejection 1: the split leaves the same hard obligation." in plan


@pytest.mark.parametrize(
    ("kind", "detail"),
    [("recovery decision for goal: negate", "the obligation is false as written")],
)
def test_record_finding_writes_through_the_runtime(tmp_path, monkeypatch, kind, detail) -> None:
    """Recovery and negation outcomes use the same durable record as planning."""
    runtime, _ = _planning_runtime(tmp_path, monkeypatch, reject_reviews=0)
    runtime.record_finding(kind, detail)
    (entry,) = runtime.state["plan_journal"]
    assert (entry["kind"], entry["detail"]) == (kind, detail)
    assert runtime._context()["planning_journal"] == runtime.state["plan_journal"]
