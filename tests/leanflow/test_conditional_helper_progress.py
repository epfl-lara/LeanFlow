"""Conditional-helper campaign progress classification tests."""

from __future__ import annotations

from pathlib import Path

from leanflow_cli.workflows import conditional_helper_progress, plan_state


def _node(name: str, file: Path, *, statement: str, status: str) -> plan_state.GraphNode:
    return plan_state.GraphNode(
        id=plan_state.node_id_for(name, str(file)),
        kind="lemma",
        name=name,
        file=str(file),
        statement=statement,
        status=status,
    )


def _structural_blueprint(
    file: Path,
    *,
    helper_name: str,
    helper_statement: str,
    extra_nodes: tuple[plan_state.GraphNode, ...] = (),
    extra_edges: tuple[plan_state.GraphEdge, ...] = (),
) -> plan_state.Blueprint:
    parent = _node(
        "residual",
        file,
        statement="(k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : Witness k",
        status="proving",
    )
    helper = _node(helper_name, file, statement=helper_statement, status="proved")
    return plan_state.Blueprint(
        nodes=(parent, helper, *extra_nodes),
        edges=(
            plan_state.GraphEdge(source=helper.id, target=parent.id, kind="split_of"),
            plan_state.GraphEdge(source=parent.id, target=helper.id, kind="depends_on"),
            *extra_edges,
        ),
    )


def test_exceptional_family_bridge_is_deferred_but_residue_helper_is_not(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "lemma conditional\n"
        "    (h_one : ∀ s : ℕ, ∃ x : ℕ, x = 840 * s + 1)\n"
        "    (h_169 : ∀ s : ℕ, ∃ x : ℕ, x = 840 * s + 169)\n"
        "    (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : Witness k := by\n"
        "  exact buildWitness h_one h_169 k hk hmod\n\n"
        "lemma mod_eleven (t : ℕ) (ht : 1 ≤ t) (hmod : t % 11 = 7) : "
        "Witness t := by\n"
        "  exact residueCertificate t ht hmod\n\n"
        "theorem residual (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : "
        "Witness k := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    parent = _node(
        "residual",
        active,
        statement="(k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : Witness k",
        status="proving",
    )
    conditional = _node("conditional", active, statement="Witness bridge", status="proved")
    residue = _node("mod_eleven", active, statement="Witness residue", status="proved")
    blueprint = plan_state.Blueprint(
        nodes=(parent, conditional, residue),
        edges=(
            plan_state.GraphEdge(conditional.id, parent.id, "split_of"),
            plan_state.GraphEdge(parent.id, conditional.id, "depends_on"),
            plan_state.GraphEdge(residue.id, parent.id, "split_of"),
            plan_state.GraphEdge(parent.id, residue.id, "depends_on"),
        ),
    )

    assessments = conditional_helper_progress.assess_conditional_helpers(blueprint)

    assert set(assessments) == {conditional.id}
    assessment = assessments[conditional.id]
    assert assessment.node_name == "conditional"
    assert len(assessment.unresolved_obligation_types) == 2
    assert all(value.startswith("∀ s : ℕ") for value in assessment.obligation_types)


def test_reverse_implication_with_the_target_as_premise_is_deferred(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "lemma reverse\n"
        "    (h : ∀ k : ℕ, 1 ≤ k → k % 7 = 0 → Witness k) :\n"
        "    ∀ s : ℕ, Exceptional s := by\n"
        "  exact deriveExceptional h\n\n"
        "theorem residual (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : "
        "Witness k := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    blueprint = _structural_blueprint(
        active,
        helper_name="reverse",
        helper_statement="(…) : ∀ s, Exceptional s",
    )

    assessments = conditional_helper_progress.assess_conditional_helpers(blueprint)

    helper_id = plan_state.node_id_for("reverse", str(active))
    assert assessments[helper_id].unresolved_obligation_types == (
        "∀ k : ℕ, 1 ≤ k → k % 7 = 0 → Witness k",
    )


def test_opaque_target_result_as_helper_premise_is_deferred(tmp_path):
    """An opaque target proposition is circular even without visible Prop syntax."""
    active = tmp_path / "Main.lean"
    active.write_text(
        "lemma reverse_opaque (h : Goldbach) : ExceptionalFamilies := by\n"
        "  exact deriveExceptional h\n\n"
        "theorem residual : Goldbach := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    blueprint = _structural_blueprint(
        active,
        helper_name="reverse_opaque",
        helper_statement="Goldbach → ExceptionalFamilies",
    )

    assessments = conditional_helper_progress.assess_conditional_helpers(blueprint)

    helper_id = plan_state.node_id_for("reverse_opaque", str(active))
    assert assessments[helper_id].unresolved_obligation_types == ("Goldbach",)


def test_exact_target_use_releases_conditional_bridge(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "lemma conditional (h : ∀ s : ℕ, Exceptional s) (k : ℕ) : Witness k := by\n"
        "  exact buildWitness h k\n\n"
        "theorem residual (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : "
        "Witness k := by\n"
        "  exact conditional establishedExceptional k\n",
        encoding="utf-8",
    )
    blueprint = _structural_blueprint(
        active,
        helper_name="conditional",
        helper_statement="(…) : Witness k",
    )

    assert conditional_helper_progress.assess_conditional_helpers(blueprint) == {}


def test_explicit_graph_obligation_releases_conditional_bridge(tmp_path):
    active = tmp_path / "Main.lean"
    obligation = "∀ s : ℕ, ∃ x : ℕ, x = 840 * s + 169"
    active.write_text(
        f"lemma conditional (h : {obligation}) (k : ℕ) : Witness k := by\n"
        "  exact buildWitness h k\n\n"
        "lemma exceptional : ∀ s : ℕ, ∃ x : ℕ, x = 840 * s + 169 := by\n"
        "  sorry\n\n"
        "theorem residual (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : "
        "Witness k := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    helper_id = plan_state.node_id_for("conditional", str(active))
    obligation_node = _node("exceptional", active, statement=obligation, status="stated")
    blueprint = _structural_blueprint(
        active,
        helper_name="conditional",
        helper_statement="(…) : Witness k",
        extra_nodes=(obligation_node,),
        extra_edges=(
            plan_state.GraphEdge(helper_id, obligation_node.id, "depends_on"),
            plan_state.GraphEdge(obligation_node.id, helper_id, "split_of"),
        ),
    )

    assert conditional_helper_progress.assess_conditional_helpers(blueprint) == {}


def test_function_valued_data_premise_is_not_treated_as_proof_obligation(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "lemma map_helper (f : ℕ → ℕ) (k : ℕ) : Witness k := by\n"
        "  exact buildMappedWitness f k\n\n"
        "theorem residual (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : "
        "Witness k := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    blueprint = _structural_blueprint(
        active,
        helper_name="map_helper",
        helper_statement="(…) : Witness k",
    )

    assert conditional_helper_progress.assess_conditional_helpers(blueprint) == {}
