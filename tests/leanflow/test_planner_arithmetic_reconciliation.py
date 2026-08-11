"""Regression tests for versioned planner arithmetic state quarantine."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from core.utils import atomic_json_write
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.plan_state import Blueprint, GraphEdge, GraphNode
from leanflow_cli.workflows.planner_arithmetic_reconciliation import (
    PLANNER_ARITHMETIC_RECONCILIATION_KEY,
    PLANNER_ARITHMETIC_RECONCILIATION_VERSION,
)

FALSE_RATIONAL_IDENTITY = """private lemma erdos_242_residual_mod_seven_eq_one_rational_identity (q : ℕ) :
    (4 : ℚ) / ((168 * q + 25 : ℕ) : ℚ) =
      (1 : ℚ) / ((42 * q + 8 : ℕ) : ℚ) +
      (1 : ℚ) / (((168 * q + 25 : ℕ) * (7 * q + 4 : ℕ) : ℕ) : ℚ) +
      (1 : ℚ) / (((168 * q + 25 : ℕ) * (42 * q + 8 : ℕ) : ℕ) : ℚ) := by
  sorry"""

VALID_DENOMINATORS = """private lemma erdos_242_residual_mod_seven_eq_one_denominators_pos (q : ℕ) :
    (0 : ℚ) < ((42 * q + 8 : ℕ) : ℚ) ∧
    (0 : ℚ) < (((168 * q + 25 : ℕ) * (7 * q + 4 : ℕ) : ℕ) : ℚ) ∧
    (0 : ℚ) < (((168 * q + 25 : ℕ) * (42 * q + 8 : ℕ) : ℕ) : ℚ) := by
  sorry"""


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    return state_dir


def _seed(bp: Blueprint, summary: dict[str, object]) -> None:
    paths = plan_state.plan_state_paths()
    atomic_json_write(paths.blueprint_json, bp.to_mapping(), sort_keys=True)
    atomic_json_write(paths.summary_json, summary, sort_keys=True)
    paths.plan_md.write_text(
        "# Proving Plan\n\n## Strategy\n\n- stale rational identity route\n",
        encoding="utf-8",
    )


def test_resume_quarantines_exact_live_false_identity_without_cascading(enabled, tmp_path) -> None:
    source = tmp_path / "Erdos242.lean"
    original_source = "theorem untouched : True := by trivial\n"
    source.write_text(original_source, encoding="utf-8")
    false_name = "erdos_242_residual_mod_seven_eq_one_rational_identity"
    valid_name = "erdos_242_residual_mod_seven_eq_one_denominators_pos"
    bp = Blueprint(
        goal="prove the residual family",
        revision=7,
        nodes=(
            GraphNode(
                id="n-false",
                name=false_name,
                file=str(source),
                statement=FALSE_RATIONAL_IDENTITY,
                status="conjectured",
                generated_by="planner",
            ),
            GraphNode(
                id="n-valid",
                name=valid_name,
                file=str(source),
                statement=VALID_DENOMINATORS,
                status="conjectured",
                generated_by="planner",
            ),
            GraphNode(
                id="n-kernel",
                name="kernel_backed_identity",
                file=str(source),
                statement=FALSE_RATIONAL_IDENTITY,
                status="proved",
                generated_by="planner",
            ),
            GraphNode(
                id="n-nonlinear",
                name="unsupported_nonlinear",
                file=str(source),
                notes="Use x*x = n after deriving a square witness.",
                status="conjectured",
                generated_by="planner",
            ),
        ),
        edges=(
            GraphEdge(source="n-false", target="n-parent", kind="split_of"),
            GraphEdge(source="n-valid", target="n-parent", kind="split_of"),
        ),
    )
    _seed(
        bp,
        {
            "grounding_findings": [
                "Let n = 168*q+25. Then n+7 = 24*(7*q+4).",
                "Let n = 168*q+25. Then n+7 = 168*q+32.",
            ],
            "strategy_notes": [
                f"State helper {false_name} and prove it with field_simp.",
                f"Keep valid helper {valid_name} for denominator positivity.",
            ],
            "strategy_notes_scope": {
                "target_symbol": "target",
                "active_file": str(source),
            },
        },
    )

    reconciled = plan_state.load_blueprint()
    summary = plan_state.load_summary()

    assert reconciled.revision == 8
    assert reconciled.node_by_id("n-false") is None
    assert reconciled.node_by_id("n-valid") is not None
    assert reconciled.node_by_id("n-kernel") is not None
    assert reconciled.node_by_id("n-nonlinear") is not None
    assert GraphEdge(source="n-valid", target="n-parent", kind="split_of") in reconciled.edges
    assert all(edge.source != "n-false" and edge.target != "n-false" for edge in reconciled.edges)
    assert summary["grounding_findings"] == [
        "Let n = 168*q+25. Then n+7 = 24*(7*q+4).",
        "Let n = 168*q+25. Then n+7 = 168*q+32.",
    ]
    assert summary["strategy_notes"] == [
        f"Keep valid helper {valid_name} for denominator positivity."
    ]
    migration = summary[PLANNER_ARITHMETIC_RECONCILIATION_KEY]
    assert migration["policy_version"] == PLANNER_ARITHMETIC_RECONCILIATION_VERSION
    node_retirement = next(
        item for item in migration["retirements"] if item.get("node_id") == "n-false"
    )
    assert node_retirement["evidence"][0]["issues"] == [
        {
            "kind": "ground-rational-identity",
            "claim": false_name,
            "evidence": "exact counterexample at q=0: 4/25 != 7/50",
        }
    ]
    prompt_view = plan_state.read_generated_plan_prompt_view()
    assert false_name not in prompt_view
    assert valid_name in prompt_view
    assert source.read_text(encoding="utf-8") == original_source
    assert plan_state.load_blueprint().revision == 8
    events = [
        json.loads(line)
        for line in plan_state.plan_state_paths()
        .journal_jsonl.read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["event"] == "planner-arithmetic-state-reconciled"
    assert events[-1]["retired_node_ids"] == ["n-false"]


def test_resume_keeps_valid_affine_planner_node_and_stabilizes_version(enabled) -> None:
    valid = GraphNode(
        id="n-valid-affine",
        name="valid_affine",
        file="Demo.lean",
        notes="Let n = 168*q+25. Then n+7 = 168*q+32.",
        status="conjectured",
        generated_by="planner",
    )
    _seed(
        Blueprint(goal="valid", revision=3, nodes=(valid,)),
        {
            "grounding_findings": ["Let n = 168*q+25. Then n+7 = 168*q+32."],
            "strategy_notes": ["Keep the valid affine normalization."],
        },
    )

    first = plan_state.load_blueprint()
    summary = plan_state.load_summary()
    second = plan_state.load_blueprint()

    assert first == second
    assert first.revision == 3
    assert first.node_by_id("n-valid-affine") == valid
    assert summary["grounding_findings"] == ["Let n = 168*q+25. Then n+7 = 168*q+32."]
    assert summary[PLANNER_ARITHMETIC_RECONCILIATION_KEY]["retirements"] == []


def test_resume_preserves_existential_nodes_notes_and_empirical_prose(enabled) -> None:
    """Regression: arbitrary legacy prose is not an asserted universal identity."""
    existential = GraphNode(
        id="n-existential",
        name="factor_pair_witnesses",
        file="Demo.lean",
        statement="""private lemma factor_pair_witnesses (s : ℕ) :
    ∃ p₁ p₂ : ℕ, p₁ * p₂ = (210 * s + 44) * (210 * s + 44) ∧
      7 ∣ p₁ ∧ 7 ∣ p₂ := by
  sorry""",
        status="conjectured",
        generated_by="planner",
    )
    affine_notes = GraphNode(
        id="n-notes",
        name="residue_case",
        file="Demo.lean",
        notes=("k=455s+106: 24k+1=840s+2545=5*(168s+509), " "d=5 nonresidual mod 24"),
        status="conjectured",
        generated_by="planner",
    )
    divisibility_notes = GraphNode(
        id="n-divisibility-notes",
        name="M_divisibility",
        file="Demo.lean",
        notes=(
            "M = (24k+1)(6k+2), M ≡ 4 (mod 7) so 7|(M+10); "
            "70|M(M+10) from 2|M and 5|M or 5|(M+10)."
        ),
        status="conjectured",
        generated_by="planner",
    )
    empirical = "Tested s=0, n=121; s=1, n=961; s=3, n=2641, and all exact checks passed."
    residue_inventory = "Branches cover mod17=2, mod11=1, mod53=3, and mod41=4."
    _seed(
        Blueprint(
            goal="preserve legacy evidence",
            revision=11,
            nodes=(existential, affine_notes, divisibility_notes),
        ),
        {
            "grounding_findings": [empirical, residue_inventory],
            "strategy_notes": ["Keep the factor-pair and residue portfolios active."],
        },
    )

    reconciled = plan_state.load_blueprint()
    summary = plan_state.load_summary()

    assert reconciled.revision == 11
    assert {node.id for node in reconciled.nodes} == {
        "n-existential",
        "n-notes",
        "n-divisibility-notes",
    }
    assert summary["grounding_findings"] == [empirical, residue_inventory]
    assert summary["strategy_notes"] == ["Keep the factor-pair and residue portfolios active."]
    assert summary[PLANNER_ARITHMETIC_RECONCILIATION_KEY]["retirements"] == []


def test_resume_retires_or_demotes_explicitly_uncertain_advisory_nodes(enabled) -> None:
    """Persisted unchecked ideas cannot remain actionable after a restart."""
    planner_uncertain = GraphNode(
        id="n-planner-uncertain",
        name="unvalidated_witness",
        file="Demo.lean",
        statement="lemma unvalidated_witness (n : Nat) : n % 3 = 0 := by sorry",
        notes="If B+1 is not always divisible by 3, the witness x must be changed.",
        status="stated",
        generated_by="planner",
    )
    decomposer_uncertain = GraphNode(
        id="n-decomposer-uncertain",
        name="placeholder_branch",
        file="Demo.lean",
        statement="lemma placeholder_branch : True := by sorry",
        notes="The edge case t = 0 needs separate verification.",
        status="stated",
        generated_by="decomposer",
    )
    kernel_backed = GraphNode(
        id="n-kernel-backed",
        name="kernel_backed",
        file="Demo.lean",
        notes="Historical note: this placeholder was later checked.",
        status="proved",
        generated_by="planner",
    )
    certain = GraphNode(
        id="n-certain",
        name="certain_helper",
        file="Demo.lean",
        notes="Checked by exact computation on the complete finite range.",
        status="stated",
        generated_by="planner",
    )
    _seed(
        Blueprint(
            goal="resume",
            revision=4,
            nodes=(planner_uncertain, decomposer_uncertain, kernel_backed, certain),
            edges=(
                GraphEdge(source="n-planner-uncertain", target="n-parent", kind="split_of"),
                GraphEdge(source="n-decomposer-uncertain", target="n-parent", kind="split_of"),
                GraphEdge(source="n-certain", target="n-parent", kind="split_of"),
            ),
        ),
        {
            "grounding_findings": [],
            "strategy_notes": [],
            "counters": {"stated": 4},
        },
    )

    reconciled = plan_state.load_blueprint()
    summary = plan_state.load_summary()

    planner = reconciled.node_by_id("n-planner-uncertain")
    assert planner is not None and planner.status == "conjectured"
    decomposer = reconciled.node_by_id("n-decomposer-uncertain")
    assert decomposer is not None and decomposer.status == "conjectured"
    assert reconciled.node_by_id("n-kernel-backed") == kernel_backed
    assert reconciled.node_by_id("n-certain") == certain
    # Keep dependency edges attached to demoted hypotheses: dropping an edge
    # would accidentally unblock a downstream node merely because its premise
    # was found to be unchecked.
    assert GraphEdge(source="n-planner-uncertain", target="n-parent", kind="split_of") in (
        reconciled.edges
    )
    assert GraphEdge(source="n-decomposer-uncertain", target="n-parent", kind="split_of") in (
        reconciled.edges
    )
    assert GraphEdge(source="n-certain", target="n-parent", kind="split_of") in reconciled.edges
    retirements = summary[PLANNER_ARITHMETIC_RECONCILIATION_KEY]["retirements"]
    by_id = {item.get("node_id"): item for item in retirements}
    assert by_id["n-planner-uncertain"]["action"] == "demoted"
    assert by_id["n-decomposer-uncertain"]["action"] == "demoted"
    assert by_id["n-planner-uncertain"]["evidence"][0]["kind"] == "conditional-revision"
    assert by_id["n-decomposer-uncertain"]["evidence"][0]["kind"] == "needs-checking"
    assert summary["counters"] == {"conjectured": 2, "proved": 1, "stated": 1}


def test_uncertain_advisory_reconciliation_is_idempotent_across_live_counters(
    enabled,
) -> None:
    """Attempt accounting cannot retrigger demotion or migration reporting."""
    uncertain = GraphNode(
        id="n-uncertain",
        name="unchecked_helper",
        file="Demo.lean",
        statement="lemma unchecked_helper : True := by sorry",
        notes="This candidate needs separate verification.",
        status="stated",
        generated_by="planner",
    )
    _seed(
        Blueprint(goal="idempotence", revision=2, nodes=(uncertain,)),
        {
            "grounding_findings": [],
            "strategy_notes": [],
            "counters": {"stated": 1},
        },
    )

    first = plan_state.load_blueprint()
    first_events = (
        plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8").splitlines()
    )
    node = first.node_by_id("n-uncertain")
    assert node is not None and node.status == "conjectured"
    assert plan_state.load_summary()["counters"] == {"conjectured": 1}

    persisted = plan_state.save_blueprint(
        first.replace_node(
            replace(
                node,
                attempts=node.attempts + 3,
                api_steps=node.api_steps + 11,
                owner="foreground-prover",
                source_sha256="new-live-source-revision",
                decision_packets=("packet-live",),
            )
        )
    )
    second = plan_state.load_blueprint()
    second_events = (
        plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8").splitlines()
    )

    assert second.revision == persisted.revision
    assert second.node_by_id("n-uncertain").status == "conjectured"
    assert second_events == first_events
    reconciliation_events = [
        json.loads(line)
        for line in second_events
        if json.loads(line).get("event") == "planner-arithmetic-state-reconciled"
    ]
    assert len(reconciliation_events) == 1
    assert reconciliation_events[0]["demoted_node_ids"] == ["n-uncertain"]
