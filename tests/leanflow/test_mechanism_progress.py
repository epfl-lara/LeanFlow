"""Parent-scoped proof-mechanism provenance tests."""

from __future__ import annotations

from leanflow_cli.workflows import finite_branch_progress, mechanism_progress, plan_state


def _node(name: str, file: str, *, status: str = "proved") -> plan_state.GraphNode:
    return plan_state.GraphNode(
        id=plan_state.node_id_for(name, file),
        kind="lemma",
        name=name,
        file=file,
        statement="True",
        status=status,
    )


def _link(child: plan_state.GraphNode, parent: plan_state.GraphNode):
    return (
        plan_state.GraphEdge(source=child.id, target=parent.id, kind="split_of"),
        plan_state.GraphEdge(source=parent.id, target=child.id, kind="depends_on"),
    )


def _singleton_source(*, include_zero: bool, include_one: bool, include_bridge: bool) -> str:
    """Return a tiny universal target with selected finite-instance helpers."""
    parts: list[str] = []
    if include_zero:
        parts.append("lemma demo_at_zero : (0 : Nat) = 0 := by\n  rfl\n")
    if include_one:
        parts.append("lemma demo_at_one : (1 : Nat) = 1 := by\n  rfl\n")
    if include_bridge:
        parts.append("lemma demo_step (s : Nat) : True := by\n  trivial\n")
    parts.append("theorem demo (s : Nat) : True := by\n  sorry\n")
    return "\n".join(parts)


def _grouped_finite_case_helper() -> str:
    """Return the exact finite-range proof shape observed on Erdős 242."""
    return (
        "private lemma demo_at_two_through_five (s : ℕ)\n"
        "    (hs : s = 2 ∨ s = 3 ∨ s = 4 ∨ s = 5) : True := by\n"
        "  rcases hs with rfl | rfl | rfl | rfl\n"
        "  all_goals trivial\n"
    )


def test_closed_singleton_classifier_rejects_nested_uniform_nat_helper():
    declaration = (
        "lemma demo_at_mod_two : ∀ s : ℕ, s % 2 = 0 → True := by\n" "  intro s hs\n" "  trivial\n"
    )

    branch = finite_branch_progress.branch_from_declaration(
        declaration,
        target_symbol="demo",
    )

    assert branch is None


def _audit_declaration(name: str, literal: int) -> str:
    """Return one live-shaped closed Erdős audit declaration."""
    return (
        f"private lemma {name} :\n"
        "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
        f"      (4 / ((24 * {literal} + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by\n"
        "  exact audit_certificate\n"
    )


def test_checked_declaration_plural_extraction_returns_both_audit_branches():
    first = _audit_declaration("erdos_242_audit_k50008", 50008)
    second = _audit_declaration("erdos_242_audit_k50009", 50009)

    branches = finite_branch_progress.branches_from_checked_declarations(
        (first, second),
        target_symbol="erdos_242_residual_mod_seven_eq_one",
    )

    assert {branch.value for branch in branches} == {50008, 50009}
    assert (
        finite_branch_progress.branch_from_checked_declarations(
            (first, second),
            target_symbol="erdos_242_residual_mod_seven_eq_one",
        )
        is None
    )
    assert (
        finite_branch_progress.branch_from_checked_declarations(
            (first,),
            target_symbol="erdos_242_residual_mod_seven_eq_one",
        )
        == branches[0]
    )


def test_closed_audit_classifier_fails_open_for_untrusted_lookalikes():
    mismatched = _audit_declaration("erdos_242_audit_k358", 359)
    parametric = (
        "private lemma erdos_242_audit_k358 (k : ℕ) :\n"
        "    ∃ x : ℕ, x = (24 * 358 + 1 : ℕ) := by\n"
        "  exact audit_parametric k\n"
    )
    scalar = (
        "private lemma erdos_242_audit_k358 : "
        "Nat.Prime (24 * 358 + 1 : ℕ) := by\n"
        "  exact audit_prime\n"
    )

    for declaration in (mismatched, parametric, scalar):
        assert (
            finite_branch_progress.branch_from_declaration(
                declaration,
                target_symbol="erdos_242_residual_mod_seven_eq_one",
            )
            is None
        )


def test_exact_closed_target_case_is_immediate_evidence():
    declaration = _audit_declaration(
        "erdos_242_residual_mod_seven_eq_one_case_k_eq_1",
        1,
    )

    branch = finite_branch_progress.branch_from_declaration(
        declaration,
        target_symbol="erdos_242_residual_mod_seven_eq_one",
    )

    assert branch is not None
    assert branch.value == 1
    assert finite_branch_progress.immediate_evidence_branch(branch) is True
    assert (
        finite_branch_progress.branch_from_declaration(
            declaration,
            target_symbol="erdos_242_residual_mod_seven_eq_two",
        )
        is None
    )


def test_closed_target_case_is_contained_before_family_saturation(tmp_path):
    active = tmp_path / "Demo.lean"
    target = "demo_residual"
    helper = f"{target}_case_k_eq_1"
    active.write_text(
        _audit_declaration(helper, 1)
        + f"\nprivate lemma {target} (k : ℕ) : ∃ x : ℕ, x = k := by\n  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _node(target, file, status="proving")
    child = _node(helper, file)
    blueprint = plan_state.Blueprint(
        nodes=(parent, child),
        edges=_link(child, parent),
    )

    assessments = finite_branch_progress.assess_saturated_finite_branch_helpers(
        blueprint,
        {child.id},
        previously_proved_node_ids=set(),
    )

    assert assessments[child.id].prior_branch_count == 0
    assert assessments[child.id].branch.kind == "closed_target_case"


def test_first_unintegrated_singleton_remains_available_as_base_evidence(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=False, include_one=False, include_bridge=False)
    after = _singleton_source(include_zero=True, include_one=False, include_bridge=False)
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_zero",),
        evidence_helper_names=("demo_at_zero",),
    )

    assert assessment is None


def test_existing_singleton_repair_is_not_counted_as_a_new_candidate(tmp_path):
    active = tmp_path / "Demo.lean"
    before = (
        "lemma demo_at_zero : (0 : Nat) = 0 := by\n"
        "  sorry\n\n"
        "theorem demo (s : Nat) : True := by\n"
        "  sorry\n"
    )
    after = before.replace(
        "lemma demo_at_zero : (0 : Nat) = 0 := by\n  sorry",
        "lemma demo_at_zero : (0 : Nat) = 0 := by\n  rfl",
    )
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_zero",),
        evidence_helper_names=("demo_at_zero",),
    )

    assert assessment is None


def test_incomplete_source_singleton_does_not_block_a_first_checked_candidate(tmp_path):
    active = tmp_path / "Demo.lean"
    before = (
        "lemma demo_at_zero : (0 : Nat) = 0 := by\n"
        "  sorry\n\n"
        "theorem demo (s : Nat) : True := by\n"
        "  sorry\n"
    )
    after = before.replace(
        "theorem demo",
        "lemma demo_at_one : (1 : Nat) = 1 := by\n  rfl\n\ntheorem demo",
    )
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_one",),
        evidence_helper_names=("demo_at_one",),
    )

    assert assessment is None


def test_newly_closed_target_is_sent_to_the_target_gate_without_rollback(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=True, include_one=False, include_bridge=False)
    after = _singleton_source(include_zero=True, include_one=True, include_bridge=False).replace(
        "theorem demo (s : Nat) : True := by\n  sorry",
        "theorem demo (s : Nat) : True := by\n  trivial",
    )
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_one",),
        evidence_helper_names=("demo_at_one",),
    )

    assert assessment is None


def test_second_unintegrated_singleton_is_rejected_without_uniform_bridge(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=True, include_one=False, include_bridge=False)
    after = _singleton_source(include_zero=True, include_one=True, include_bridge=False)
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_one",),
        evidence_helper_names=("demo_at_one",),
    )

    assert assessment is not None
    assert assessment.candidate_names == ("demo_at_one",)
    assert assessment.prior_names == ("demo_at_zero",)


def test_grouped_erdos_finite_cases_are_rejected_after_existing_base(tmp_path):
    """The live two-through-five helper cannot bypass singleton containment."""
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=True, include_one=True, include_bridge=False)
    after = _grouped_finite_case_helper() + "\n" + before
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_two_through_five",),
        evidence_helper_names=("demo_at_two_through_five",),
    )

    assert assessment is not None
    assert assessment.candidate_names == ("demo_at_two_through_five",)
    assert assessment.prior_names == ("demo_at_one", "demo_at_zero")
    assert assessment.candidate_branches == ("finite-cases:s={2,3,4,5}",)


def test_first_grouped_finite_case_helper_remains_base_evidence(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=False, include_one=False, include_bridge=False)
    after = _grouped_finite_case_helper() + "\n" + before
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_two_through_five",),
        evidence_helper_names=("demo_at_two_through_five",),
    )

    assert assessment is None


def test_later_grouped_finite_case_family_is_rejected(tmp_path):
    active = tmp_path / "Demo.lean"
    earlier = _grouped_finite_case_helper()
    before = (
        earlier
        + "\n"
        + _singleton_source(
            include_zero=False,
            include_one=False,
            include_bridge=False,
        )
    )
    later = (
        "private lemma demo_at_six_or_seven (s : ℕ)\n"
        "    (hs : s = 6 ∨ s = 7) : True := by\n"
        "  rcases hs with rfl | rfl\n"
        "  all_goals trivial\n\n"
    )
    after = later + before
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_six_or_seven",),
        evidence_helper_names=("demo_at_six_or_seven",),
    )

    assert assessment is not None
    assert assessment.candidate_names == ("demo_at_six_or_seven",)
    assert assessment.prior_names == ("demo_at_two_through_five",)
    assert assessment.candidate_branches == ("finite-cases:s={6,7}",)


def test_grouped_finite_cases_are_allowed_with_structural_bridge(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=True, include_one=False, include_bridge=True)
    after = _grouped_finite_case_helper() + "\n" + before
    active.write_text(after, encoding="utf-8")
    file = str(active)
    target = _node("demo", file, status="proving")
    bridge = _node("demo_step", file, status="stated")
    blueprint = plan_state.Blueprint(nodes=(target, bridge), edges=_link(bridge, target))

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        blueprint,
        target_symbol="demo",
        active_file=file,
        before_text=before,
        after_text=after,
        helper_names=("demo_at_two_through_five",),
        evidence_helper_names=("demo_at_two_through_five",),
    )

    assert assessment is None


def test_existing_grouped_finite_case_repair_is_not_a_new_candidate(tmp_path):
    active = tmp_path / "Demo.lean"
    incomplete = _grouped_finite_case_helper().replace("all_goals trivial", "sorry")
    target = _singleton_source(include_zero=True, include_one=False, include_bridge=False)
    before = incomplete + "\n" + target
    after = _grouped_finite_case_helper() + "\n" + target
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_two_through_five",),
        evidence_helper_names=("demo_at_two_through_five",),
    )

    assert assessment is None


def test_grouped_finite_case_does_not_rollback_newly_closed_target(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=True, include_one=False, include_bridge=False)
    after = (_grouped_finite_case_helper() + "\n" + before).replace(
        "theorem demo (s : Nat) : True := by\n  sorry",
        "theorem demo (s : Nat) : True := by\n  trivial",
    )
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_two_through_five",),
        evidence_helper_names=("demo_at_two_through_five",),
    )

    assert assessment is None


def test_uniform_congruence_helper_is_not_finite_case_accumulation(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=True, include_one=False, include_bridge=False)
    helper = "lemma demo_at_residue_two (s : Nat) (hs : s % 5 = 2) : True := by\n" "  trivial\n\n"
    after = helper + before
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_residue_two",),
        evidence_helper_names=("demo_at_residue_two",),
    )

    assert assessment is None


def test_residue_witness_case_split_remains_a_structural_helper(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=True, include_one=False, include_bridge=False)
    helper = (
        "lemma demo_at_residue_cases (s : Nat) (r : Nat)\n"
        "    (hr : r = 0 ∨ r = 1) (hmod : s % 2 = r) : True := by\n"
        "  trivial\n\n"
    )
    after = helper + before
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_residue_cases",),
        evidence_helper_names=("demo_at_residue_cases",),
    )

    assert assessment is None


def test_structural_uniform_bridge_allows_multiple_induction_bases(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=True, include_one=False, include_bridge=True)
    after = _singleton_source(include_zero=True, include_one=True, include_bridge=True)
    active.write_text(after, encoding="utf-8")
    file = str(active)
    target = _node("demo", file, status="proving")
    bridge = _node("demo_step", file, status="stated")
    blueprint = plan_state.Blueprint(
        nodes=(target, bridge),
        edges=_link(bridge, target),
    )

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        blueprint,
        target_symbol="demo",
        active_file=file,
        before_text=before,
        after_text=after,
        helper_names=("demo_at_one",),
        evidence_helper_names=("demo_at_one",),
    )

    assert assessment is None


def test_same_edit_target_integration_is_not_singleton_evidence(tmp_path):
    active = tmp_path / "Demo.lean"
    before = _singleton_source(include_zero=True, include_one=False, include_bridge=False)
    after = _singleton_source(include_zero=True, include_one=True, include_bridge=False)
    active.write_text(after, encoding="utf-8")

    assessment = finite_branch_progress.assess_repeated_unintegrated_singleton_edit(
        plan_state.Blueprint(),
        target_symbol="demo",
        active_file=str(active),
        before_text=before,
        after_text=after,
        helper_names=("demo_at_one",),
        evidence_helper_names=(),
    )

    assert assessment is None


def test_exact_local_dependency_defines_residue_agnostic_mechanism(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma factor_pair (n : Nat) : True := by\n"
        "  trivial\n\n"
        "lemma residue_eleven (n : Nat) (h : n % 17 = 11) : True := by\n"
        "  exact factor_pair n\n\n"
        "lemma residue_nine (n : Nat) (h : n % 17 = 9) : True := by\n"
        "  simpa using factor_pair n\n\n"
        "lemma alternate_certificate (n : Nat) : True := by\n"
        "  trivial\n\n"
        "lemma residue_other_route (n : Nat) (h : n % 17 = 4) : True := by\n"
        "  exact alternate_certificate n\n\n"
        "theorem parent : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _node("parent", file, status="proving")
    eleven = _node("residue_eleven", file)
    nine = _node("residue_nine", file)
    other = _node("residue_other_route", file)
    blueprint = plan_state.Blueprint(
        nodes=(parent, eleven, nine, other),
        edges=(*_link(eleven, parent), *_link(nine, parent), *_link(other, parent)),
    )

    records = {
        record.node_name: record
        for record in mechanism_progress.derive_parent_scoped_mechanisms(
            blueprint, {eleven.id, nine.id, other.id}
        )
    }

    assert (
        records["residue_eleven"].mechanism_signature == records["residue_nine"].mechanism_signature
    )
    assert records["residue_eleven"].local_dependencies == ("factor_pair",)
    assert records["residue_nine"].local_dependencies == ("factor_pair",)
    assert (
        records["residue_other_route"].mechanism_signature
        != records["residue_eleven"].mechanism_signature
    )
    assert records["residue_other_route"].local_dependencies == ("alternate_certificate",)


def test_direct_certificate_body_fallback_does_not_collapse_unrelated_routes(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma direct_one : True := by\n"
        "  have h : (1 : Nat) = 1 := by norm_num\n"
        "  exact True.intro\n\n"
        "lemma direct_nine : True := by\n"
        "  have witness : (9 : Nat) = 9 := by norm_num\n"
        "  exact True.intro\n\n"
        "lemma direct_other : True := by\n"
        "  omega\n\n"
        "theorem parent : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _node("parent", file, status="proving")
    one = _node("direct_one", file)
    nine = _node("direct_nine", file)
    other = _node("direct_other", file)
    blueprint = plan_state.Blueprint(
        nodes=(parent, one, nine, other),
        edges=(*_link(one, parent), *_link(nine, parent), *_link(other, parent)),
    )

    records = {
        record.node_name: record
        for record in mechanism_progress.derive_parent_scoped_mechanisms(
            blueprint, {one.id, nine.id, other.id}
        )
    }

    assert records["direct_one"].local_dependencies == ()
    assert records["direct_nine"].local_dependencies == ()
    assert records["direct_one"].mechanism_signature == records["direct_nine"].mechanism_signature
    assert records["direct_other"].mechanism_signature != records["direct_one"].mechanism_signature


def test_mechanism_scope_changes_with_explicit_parent(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma factor_pair (n : Nat) : True := by\n"
        "  trivial\n\n"
        "lemma child_one (n : Nat) : True := by\n"
        "  exact factor_pair n\n\n"
        "lemma child_two (n : Nat) : True := by\n"
        "  exact factor_pair n\n\n"
        "theorem parent_one : True := by\n"
        "  sorry\n\n"
        "theorem parent_two : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent_one = _node("parent_one", file, status="proving")
    parent_two = _node("parent_two", file, status="proving")
    child_one = _node("child_one", file)
    child_two = _node("child_two", file)
    blueprint = plan_state.Blueprint(
        nodes=(parent_one, parent_two, child_one, child_two),
        edges=(*_link(child_one, parent_one), *_link(child_two, parent_two)),
    )

    records = {
        record.node_name: record
        for record in mechanism_progress.derive_parent_scoped_mechanisms(
            blueprint, {child_one.id, child_two.id}
        )
    }

    assert records["child_one"].mechanism_signature == records["child_two"].mechanism_signature
    assert records["child_one"].parent_id != records["child_two"].parent_id


def test_evidence_only_verified_node_is_not_forced_mechanism_progress(tmp_path):
    """Negation evidence remains a proved fact without becoming proof progress."""
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma terminal_conditions_consistent : True := by\n"
        "  trivial\n\n"
        "theorem parent : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _node("parent", file, status="proving")
    before_evidence = _node("terminal_conditions_consistent", file, status="proving")
    proved_evidence = _node("terminal_conditions_consistent", file)
    evidence_edge = plan_state.GraphEdge(
        source=proved_evidence.id,
        target=parent.id,
        kind="evidence",
    )
    before = plan_state.Blueprint(
        nodes=(parent, before_evidence),
        edges=(evidence_edge,),
    )
    after = plan_state.Blueprint(
        nodes=(parent, proved_evidence),
        edges=(evidence_edge,),
    )

    assert mechanism_progress.evidence_only_node_ids(after, {proved_evidence.id}) == {
        proved_evidence.id
    }
    batch = mechanism_progress.build_mechanism_batch(
        before,
        after,
        previously_proved_node_ids=(),
        newly_verified_node_ids=(proved_evidence.id,),
        eligible_node_ids=(proved_evidence.id,),
    )

    assert batch.candidate_records == ()
    assert batch.forced_node_ids == ()


def test_saturated_graph_family_does_not_defer_strictly_broader_congruence(tmp_path):
    """Graph/queue policy lets a class containing prior coverage reach normal accounting."""
    active = tmp_path / "Demo.lean"
    prior_specs = ((10, 3), (7, 1), (11, 2), (13, 4))
    active.write_text(
        "".join(
            f"lemma residue_{modulus}_{residue} (t : Nat) "
            f"(h : t % {modulus} = {residue}) : True := by\n  trivial\n\n"
            for modulus, residue in prior_specs
        )
        + "lemma broader (t : Nat) (h : t % 5 = 3) : True := by\n  omega\n\n"
        + "theorem parent : True := by\n  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _node("parent", file, status="proving")
    prior = tuple(_node(f"residue_{modulus}_{residue}", file) for modulus, residue in prior_specs)
    broader = _node("broader", file)
    blueprint = plan_state.Blueprint(
        nodes=(parent, *prior, broader),
        edges=tuple(edge for helper in (*prior, broader) for edge in _link(helper, parent)),
    )

    assessments = finite_branch_progress.assess_saturated_finite_branch_helpers(
        blueprint,
        {broader.id},
        previously_proved_node_ids={node.id for node in prior},
    )

    assert assessments == {}
