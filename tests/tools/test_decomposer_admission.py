"""Characterize closed-instantiation admission for helper decomposition."""

from __future__ import annotations

import json
from types import SimpleNamespace

from leanflow_cli.workflows import decomposer
from tools.implementations import lean_experts
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


def test_advisor_rejects_dropped_disjointness_before_lean_check(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        lean_experts,
        "lean_incremental_check",
        lambda **kwargs: calls.append(kwargs),
    )

    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": "pieces_of_ends",
                "lean_skeleton": PHYSICAL_PIECES_UNSAFE_BRIDGE,
            }
        ],
        theorem_statement=PHYSICAL_PIECES_PARENT,
        file_path="Demo.lean",
        theorem_id="exists_realization",
        cwd="",
        timeout_s=30,
    )

    assert calls == []
    assert helpers[0]["check_status"] == "rejected_admission"
    assert helpers[0]["ready_for_managed_placement"] is False
    assert helpers[0]["lean_skeleton"] == ""
    assert helpers[0]["admission_reason_code"] == "dropped_required_disjointness"
    assert validation["lean_check_count"] == 0


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


def test_advisor_validation_rejects_singleton_before_lean_check(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        lean_experts,
        "lean_incremental_check",
        lambda **kwargs: calls.append(kwargs),
    )

    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": "erdos_242_residual_mod_seven_eq_one_case_k_eq_1",
                "lean_skeleton": ERDOS_CLOSED_INSTANCE,
            }
        ],
        theorem_statement=ERDOS_PARENT,
        file_path="Erdos242.lean",
        theorem_id="erdos_242_residual_mod_seven_eq_one",
        cwd="",
        timeout_s=30,
    )

    assert calls == []
    assert helpers[0]["check_status"] == "rejected_instantiated_parent"
    assert helpers[0]["ready_for_managed_placement"] is False
    assert helpers[0]["lean_skeleton"] == ""
    assert helpers[0]["admission_guard"]["reason_code"] == ("closed_literal_parent_instantiation")
    assert validation["instantiated_parent_rejected_count"] == 1
    assert validation["lean_check_count"] == 0


def test_mechanical_decomposer_rechecks_precontract_singleton_and_journals(
    monkeypatch,
    tmp_path,
):
    target = tmp_path / "Erdos242.lean"
    target.write_text("import Mathlib\n\n" + ERDOS_PARENT + "\n", encoding="utf-8")
    events: list[dict[str, object]] = []

    monkeypatch.setattr(
        lean_experts,
        "lean_decompose_helpers_tool",
        lambda *args, **kwargs: json.dumps(
            {
                "success": True,
                "helpers": [
                    {
                        "name": "erdos_242_residual_mod_seven_eq_one_case_k_eq_1",
                        "lean_skeleton": ERDOS_CLOSED_INSTANCE,
                        "ready_for_managed_placement": True,
                        "validation_order": 1,
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(decomposer.plan_state, "append_journal_event", events.append)
    monkeypatch.setattr(
        "leanflow_cli.lean.lean_incremental.lean_incremental_check",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("rejected singleton must not start Lean placement validation")
        ),
    )

    outcome = decomposer.run_decomposer(
        target_symbol="erdos_242_residual_mod_seven_eq_one",
        active_file=str(target),
        statement=ERDOS_PARENT,
        cwd=str(tmp_path),
    )

    assert outcome.ok is False
    assert outcome.skipped == ("erdos_242_residual_mod_seven_eq_one_case_k_eq_1",)
    assert target.read_text(encoding="utf-8") == "import Mathlib\n\n" + ERDOS_PARENT + "\n"
    assert [event["event"] for event in events] == ["decomposer-instantiated-parent-rejected"]
    assert events[0]["reason_code"] == "closed_literal_parent_instantiation"
    assert events[0]["instantiated_parameters"] == [{"name": "k", "literal": "1"}]


def test_mechanical_decomposer_journals_normal_advisor_rejection(monkeypatch, tmp_path):
    target = tmp_path / "Erdos242.lean"
    target.write_text("import Mathlib\n\n" + ERDOS_PARENT + "\n", encoding="utf-8")
    events: list[dict[str, object]] = []
    admission = decomposer_admission.assess_helper_admission(
        ERDOS_PARENT,
        ERDOS_CLOSED_INSTANCE,
    )

    monkeypatch.setattr(
        lean_experts,
        "lean_decompose_helpers_tool",
        lambda *args, **kwargs: json.dumps(
            {
                "success": True,
                "helpers": [
                    {
                        "name": "erdos_242_residual_mod_seven_eq_one_case_k_eq_1",
                        "lean_skeleton": "",
                        "check_status": "rejected_instantiated_parent",
                        "ready_for_managed_placement": False,
                        "admission_guard": admission.journal_fields(),
                        "validation_order": 1,
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(decomposer.plan_state, "append_journal_event", events.append)

    outcome = decomposer.run_decomposer(
        target_symbol="erdos_242_residual_mod_seven_eq_one",
        active_file=str(target),
        statement=ERDOS_PARENT,
        cwd=str(tmp_path),
    )

    assert outcome.ok is False
    assert outcome.skipped == ("erdos_242_residual_mod_seven_eq_one_case_k_eq_1",)
    assert events[0]["event"] == "decomposer-instantiated-parent-rejected"
    assert events[0]["reason_code"] == "closed_literal_parent_instantiation"
    assert events[0]["conclusion_similarity"] == 1.0


def test_decomposer_prompt_forbids_singleton_instantiation(monkeypatch, tmp_path):
    target = tmp_path / "Erdos242.lean"
    target.write_text("import Mathlib\n\n" + ERDOS_PARENT + "\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            model="test-model",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "helpers": [
                                    {
                                        "name": ("erdos_242_residual_mod_seven_eq_one_case_k_eq_1"),
                                        "lean_skeleton": ERDOS_CLOSED_INSTANCE,
                                    }
                                ]
                            }
                        )
                    )
                )
            ],
        )

    monkeypatch.setattr(lean_experts, "resolve_expert_provider", lambda _task: "test-model")
    monkeypatch.setattr(lean_experts, "is_command_expert_provider", lambda _provider: False)
    monkeypatch.setattr(lean_experts, "call_llm", fake_call_llm)

    payload = json.loads(
        lean_experts.lean_decompose_helpers_tool(
            "erdos_242_residual_mod_seven_eq_one",
            str(target),
            theorem_statement=ERDOS_PARENT,
            cwd=str(tmp_path),
        )
    )

    messages = captured["messages"]
    assert isinstance(messages, list)
    system_prompt = str(messages[0]["content"])
    assert "must not be a closed literal instance of the parent conclusion" in system_prompt
    assert (
        "A finite base case is useful only when it states a distinct structural fact"
        in system_prompt
    )
    assert "same-premise existential certificate" in system_prompt
    assert "isolate the missing divisor, witness, or coverage fact" in system_prompt
    assert "audit every parent hypothesis that the helper omits" in system_prompt
    assert "try the smallest boundary counterexample" in system_prompt
    assert "Reachability, positivity, nonemptiness, and invariant helpers" in system_prompt
    assert "initial-condition hypotheses" in system_prompt
    assert payload["success"] is True
    assert payload["helpers"][0]["check_status"] == "rejected_instantiated_parent"
    assert payload["helpers"][0]["lean_skeleton"] == ""
    assert payload["decomposition_admission_guard"] == {
        "applied": True,
        "rejected_helper_count": 1,
        "reason_code": "closed_literal_parent_instantiation",
    }
    assert "Preserve the parent parameter" in payload["recommended_split"]
    assert payload["next_step"].startswith(
        "Discard every helper marked rejected_instantiated_parent"
    )
