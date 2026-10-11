"""Characterize closed-instantiation admission for helper decomposition."""

from __future__ import annotations

from tools.utilities import decomposer_admission

ERDOS_PARENT = """private lemma erdos_242_residual_mod_seven_eq_one (k : ℕ) (hk : 1 ≤ k)
    (hmod : k % 7 = 1) :
    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧
      (4 / ((24 * k + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by
  sorry"""

ERDOS_CLOSED_INSTANCE = """private lemma erdos_242_residual_mod_seven_eq_one_case_k_eq_1 :
    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧
      (4 / ((24 * 1 + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by
  sorry"""

ERDOS_PARAMETERIZED_RESIDUE = """private lemma erdos_242_residual_mod_thirty_five_eq_one
    (k : ℕ) (hk : 1 ≤ k) (hmod : k % 35 = 1) :
    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧
      (4 / ((24 * k + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by
  sorry"""

ERDOS_ALPHA_PARAMETERIZED_RESIDUE = """private lemma erdos_242_residual_mod_thirty_five_eq_one_alpha
    (n : Nat) (hn : 1 ≤ n) (hmod : n % 35 = 1) :
    ∃ x y z : Nat, 1 ≤ x ∧ x < y ∧ y < z ∧
      (4 / ((24 * n + 1 : Nat) : Rat)) = 1 / x + 1 / y + 1 / z := by
  sorry"""

ERDOS_STRUCTURAL_BASE = """private lemma erdos_242_k_eq_one_denominator :
    (24 * 1 + 1 : ℕ) = 5 * 5 := by
  sorry"""

ERDOS_MOD_FIVE_TWO_PARENT = """private lemma erdos_242_mod_five_two
    (q : ℕ) (hmod : q % 5 = 2) :
    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧
      (4 / ((168 * q + 25 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by
  sorry"""

ERDOS_ASSEMBLY_WRAPPER = """private lemma erdos_242_mod_five_two_exists_assembly_inputs
    (q : ℕ) (hmod : q % 5 = 2) :
    ∃ x p₁ p₂ : ℕ,
      let n := 168 * q + 25
      let Q := 4 * x - n
      let B := n * x
      1 ≤ x ∧ 0 < Q ∧ p₁ * p₂ = B ^ 2 ∧
        Q ∣ (B + p₁) ∧ Q ∣ (B + p₂) ∧
        x < (B + p₁) / Q ∧ (B + p₁) / Q < (B + p₂) / Q := by
  sorry"""

ERDOS_NARROW_DIVISOR_HELPER = """private lemma erdos_242_mod_five_two_divisor
    (q : ℕ) (hmod : q % 5 = 2) :
    ∃ d : ℕ, d ∣ (168 * q + 25) := by
  sorry"""

PHYSICAL_PIECES_PARENT = """private lemma exists_realization
    {n : ℕ} (s : Strategy n) (fine : List ℝ) :
    ∃ xiangPoints : Finset (Set.Ioo (0 : ℝ) 1),
      Disjoint s.points xiangPoints ∧
      physicalPieces s xiangPoints = fine := by
  sorry"""

PHYSICAL_PIECES_UNSAFE_BRIDGE = """private lemma pieces_of_ends
    {n : ℕ} (s : Strategy n)
    (xiangPoints : Finset (Set.Ioo (0 : ℝ) 1)) (fine : List ℝ)
    (hends : s.playEnds xiangPoints = fine) :
    physicalPieces s xiangPoints = fine := by
  sorry"""

PHYSICAL_PIECES_SAFE_BRIDGE = """private lemma pieces_of_ends
    {n : ℕ} (s : Strategy n)
    (xiangPoints : Finset (Set.Ioo (0 : ℝ) 1)) (fine : List ℝ)
    (hd : Disjoint s.points xiangPoints)
    (hends : s.playEnds xiangPoints = fine) :
    physicalPieces s xiangPoints = fine := by
  sorry"""


def test_exact_erdos_singleton_is_rejected_but_real_decompositions_survive():
    rejected = decomposer_admission.assess_helper_admission(
        ERDOS_PARENT,
        ERDOS_CLOSED_INSTANCE,
    )
    parameterized = decomposer_admission.assess_helper_admission(
        ERDOS_PARENT,
        ERDOS_PARAMETERIZED_RESIDUE,
    )
    alpha_parameterized = decomposer_admission.assess_helper_admission(
        ERDOS_PARENT,
        ERDOS_ALPHA_PARAMETERIZED_RESIDUE,
    )
    structural = decomposer_admission.assess_helper_admission(
        ERDOS_PARENT,
        ERDOS_STRUCTURAL_BASE,
    )

    assert rejected.accepted is False
    assert rejected.reason_code == "closed_literal_parent_instantiation"
    assert rejected.instantiated_parameters == (("k", "1"),)
    assert parameterized.accepted is True
    assert alpha_parameterized.accepted is True
    assert structural.accepted is True


def test_play_ends_bridge_must_preserve_required_disjointness():
    unsafe = decomposer_admission.assess_helper_admission(
        PHYSICAL_PIECES_PARENT,
        PHYSICAL_PIECES_UNSAFE_BRIDGE,
    )
    safe = decomposer_admission.assess_helper_admission(
        PHYSICAL_PIECES_PARENT,
        PHYSICAL_PIECES_SAFE_BRIDGE,
    )

    assert unsafe.accepted is False
    assert unsafe.reason_code == "dropped_required_disjointness"
    assert "overlapping cut points" in unsafe.reason
    assert safe.accepted is True


def test_near_identical_parenthesized_singleton_is_still_rejected():
    parenthesized = ERDOS_CLOSED_INSTANCE.replace(
        "    ∃ x y z : ℕ,",
        "    (∃ x y z : Nat,",
    ).replace(
        "= 1 / x + 1 / y + 1 / z := by",
        "= 1 / x + 1 / y + 1 / z) := by",
    )

    assessment = decomposer_admission.assess_helper_admission(
        ERDOS_PARENT,
        parenthesized,
    )

    assert assessment.accepted is False
    assert assessment.instantiated_parameters == (("k", "1"),)


def test_same_premise_assembly_wrapper_is_not_graph_progress():
    """The observed certificate rewrite keeps at least the whole existential burden."""
    wrapper = decomposer_admission.assess_obligation_reduction(
        ERDOS_MOD_FIVE_TWO_PARENT,
        ERDOS_ASSEMBLY_WRAPPER,
    )
    narrower = decomposer_admission.assess_obligation_reduction(
        ERDOS_MOD_FIVE_TWO_PARENT,
        ERDOS_NARROW_DIVISOR_HELPER,
    )

    assert wrapper.nonreducing_wrapper is True
    assert wrapper.reason_code == "nonreducing_existential_wrapper"
    assert wrapper.parent_profile.to_mapping() == {
        "existential_variables": 3,
        "logical_atoms": 4,
        "relation_atoms": 4,
    }
    assert wrapper.helper_profile.to_mapping() == {
        "existential_variables": 3,
        "logical_atoms": 7,
        "relation_atoms": 7,
    }
    assert narrower.reducing is True


def test_stronger_branch_premise_is_not_mislabeled_as_same_premise_wrapper():
    parent = """theorem eventual (q : ℕ) :
      ∃ x y : ℕ, x < y ∧ x + y = q := by sorry"""
    residue = """lemma residue (q : ℕ) (hmod : q % 5 = 2) :
      ∃ x y : ℕ, x < y ∧ x + y = q := by sorry"""

    assessment = decomposer_admission.assess_obligation_reduction(parent, residue)

    assert assessment.reducing is True
    assert assessment.reason_code == ""
