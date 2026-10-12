"""Test bounded decomposition prompts and source-backed route guards."""

from __future__ import annotations

import json

from tools.utilities import decomposer_prompt, decomposer_source_guard


def _write_constrained_target(tmp_path):
    """Write a target with sorry-free consistency and negation facts."""
    target = tmp_path / "Demo.lean"
    target.write_text(
        """private lemma demo_terminal_conditions_consistent :
    ∃ n : Nat, n = 0 := by
  exact ⟨0, rfl⟩

private lemma demo_terminal_conditions_do_not_imply_false :
    ¬ (∀ n : Nat, n = 0 → False) := by
  intro h
  exact h 0 rfl

private lemma demo_counterexample_to_false_placeholder :
    ∃ n : Nat, n = 0 := by
  sorry

theorem demo : True := by
  sorry
""",
        encoding="utf-8",
    )
    return target


def test_source_guard_finds_sorry_free_constraints_and_rejects_terminal_false(tmp_path):
    target = _write_constrained_target(tmp_path)

    context = decomposer_source_guard.load_decomposer_source_context(
        theorem_id="demo",
        file_path=str(target),
        cwd=str(tmp_path),
    )

    assert context.status == "loaded"
    assert [constraint.name for constraint in context.constraints] == [
        "demo_terminal_conditions_consistent",
        "demo_terminal_conditions_do_not_imply_false",
    ]
    rejected = decomposer_source_guard.helper_source_conflict_reason(
        {
            "name": "demo_terminal_residue_cover",
            "purpose": "Close the exhausted terminal residue branch by contradiction.",
            "lean_skeleton": "private lemma demo_terminal_residue_cover : False := by\n  sorry",
        },
        theorem_id="demo",
        constraints=context.constraints,
    )
    allowed = decomposer_source_guard.helper_source_conflict_reason(
        {
            "name": "demo_terminal_residue_witness",
            "purpose": "Produce a surviving witness.",
            "lean_skeleton": (
                "private lemma demo_terminal_residue_witness : " "∃ n : Nat, n = 0 := by\n  sorry"
            ),
        },
        theorem_id="demo",
        constraints=context.constraints,
    )

    assert "demo_terminal_conditions_consistent" in rejected
    assert "non-False coverage or witness-producing helper" in rejected
    assert allowed == ""


def test_source_guard_allows_trivial_false_hypothesis_but_rejects_matching_route(tmp_path):
    target = _write_constrained_target(tmp_path)
    context = decomposer_source_guard.load_decomposer_source_context(
        theorem_id="demo",
        file_path=str(target),
        cwd=str(tmp_path),
    )

    trivial = decomposer_source_guard.helper_source_conflict_reason(
        {
            "name": "demo_terminal_residue_cover",
            "purpose": "Close the terminal residue route.",
            "lean_skeleton": (
                "private lemma demo_terminal_residue_cover (h : False) : False := by\n" "  sorry"
            ),
        },
        theorem_id="demo",
        constraints=context.constraints,
    )
    matching = decomposer_source_guard.helper_source_conflict_reason(
        {
            "name": "demo_terminal_residue_cover",
            "purpose": "Close the terminal residue route.",
            "lean_skeleton": (
                "private lemma demo_terminal_residue_cover (n : Nat) (h : n = 0) : "
                "False := by\n  sorry"
            ),
        },
        theorem_id="demo",
        constraints=context.constraints,
    )

    assert trivial == ""
    assert "demo_terminal_conditions_do_not_imply_false" in matching


def test_observed_erdos242_prefix_contradiction_route_is_rejected():
    constraint = decomposer_source_guard.SourceConstraint(
        name=(
            "Erdos242.erdos_242_residual_mod_seven_eq_zero_"
            "terminal_conditions_do_not_imply_false"
        ),
        statement=(
            "private lemma erdos_242_residual_mod_seven_eq_zero_"
            "terminal_conditions_do_not_imply_false : "
            "¬ (∀ t : ℕ, 1 ≤ t → t ≠ 15 → t % 5 ≠ 2 → t % 5 ≠ 3 → "
            "t % 13 ≠ 1 → t % 19 ≠ 13 → t % 11 ≠ 6 → False)"
        ),
        declaration_sha256="observed-transcript-fixture",
        kind="negated_false_implication",
    )
    helper = {
        "name": "erdos_242_residual_mod_seven_eq_zero_residue_cover",
        "purpose": "Package the terminal exhaustive contradiction after residue branches fail.",
        "lean_skeleton": (
            "private lemma erdos_242_residual_mod_seven_eq_zero_residue_cover "
            "(t : ℕ) (ht : 1 ≤ t) (h15 : t ≠ 15) "
            "(h5two : t % 5 ≠ 2) (h5 : t % 5 ≠ 3) "
            "(h13 : t % 13 ≠ 1) (h19 : t % 19 ≠ 13) "
            "(h11 : t % 11 ≠ 6) : False := by\n  sorry"
        ),
        "proof_hints": ["Move the existing terminal branch contradiction into this helper."],
    }

    reason = decomposer_source_guard.helper_source_conflict_reason(
        helper,
        theorem_id="erdos_242_residual_mod_seven_eq_zero",
        constraints=(constraint,),
    )

    assert "terminal_conditions_do_not_imply_false" in reason
    assert "not an exhaustive contradiction" in reason


def test_source_guard_resolves_qualified_targets_and_fails_closed_on_ambiguity(tmp_path):
    target = tmp_path / "Namespaced.lean"
    target.write_text(
        """namespace A
private lemma demo_terminal_consistent : ∃ n : Nat, n = 0 := by
  exact ⟨0, rfl⟩
theorem demo : True := by
  sorry
end A

namespace B
@[simp]
private lemma demo_terminal_consistent : ∃ n : Nat, n = 1 := by
  -- The word sorry in a comment is not a placeholder.
  exact ⟨1, rfl⟩
def unrelated : Nat := by
  sorry
theorem demo : True := by
  sorry
end B
""",
        encoding="utf-8",
    )

    ambiguous = decomposer_source_guard.load_decomposer_source_context(
        theorem_id="demo", file_path=str(target), cwd=str(tmp_path)
    )
    qualified = decomposer_source_guard.load_decomposer_source_context(
        theorem_id="B.demo", file_path=str(target), cwd=str(tmp_path)
    )

    assert ambiguous.status == "target_ambiguous"
    assert ambiguous.constraints == ()
    assert qualified.status == "loaded"
    assert [constraint.name for constraint in qualified.constraints] == [
        "B.demo_terminal_consistent"
    ]


def test_source_guard_resolves_relative_dotted_target_inside_namespace(tmp_path):
    """Resolve the source-declared dotted name beneath its enclosing namespace."""
    target = tmp_path / "Erdos242.lean"
    target.write_text(
        """namespace Erdos242
private lemma preceding_helper : True := by
  trivial

/-- A decorated research target following generated helpers. -/
@[category research open, AMS 11]
theorem erdos_242.variants.schinzel_generalization
    (a : Nat) (ha : 0 < a) : True := by
  sorry

end Erdos242
""",
        encoding="utf-8",
    )

    context = decomposer_source_guard.load_decomposer_source_context(
        theorem_id="erdos_242.variants.schinzel_generalization",
        file_path=str(target),
        cwd=str(tmp_path),
    )

    assert context.status == "loaded"
    assert context.target_start_line == 7
    assert context.target_end_line == 9
    assert context.target_statement.startswith("theorem erdos_242.variants.schinzel_generalization")


def test_source_guard_fails_closed_on_ambiguous_relative_dotted_target(tmp_path):
    """Reject a relative dotted name when multiple outer namespaces contain it."""
    target = tmp_path / "AmbiguousDotted.lean"
    target.write_text(
        """namespace A
theorem demo.variants.target : True := by
  sorry
end A

namespace B
theorem demo.variants.target : True := by
  sorry
end B

namespace Outer
theorem B.demo.variants.target : True := by
  sorry
end Outer
""",
        encoding="utf-8",
    )

    ambiguous = decomposer_source_guard.load_decomposer_source_context(
        theorem_id="demo.variants.target",
        file_path=str(target),
        cwd=str(tmp_path),
    )
    exact = decomposer_source_guard.load_decomposer_source_context(
        theorem_id="B.demo.variants.target",
        file_path=str(target),
        cwd=str(tmp_path),
    )

    assert ambiguous.status == "target_ambiguous"
    assert ambiguous.target_statement == ""
    # The exact full name wins over the additional relative suffix match in
    # `Outer.B.demo.variants.target`.
    assert exact.status == "loaded"
    assert exact.target_start_line == 7


def test_prompt_statement_shaping_removes_term_style_proof():
    context = decomposer_prompt.shape_decomposer_prompt_context(
        theorem_id="demo",
        theorem_statement="theorem demo : True := trivial\n" + "proof noise\n" * 20_000,
        current_diagnostics="",
        current_goals="",
        current_attempt="",
        recent_failed_attempts="",
        source_context=decomposer_source_guard.DecomposerSourceContext(status="unavailable"),
    )

    assert context.theorem_statement == "theorem demo : True"


def test_prompt_source_index_overrides_stale_helper_absence_claim(tmp_path):
    """Surface a current helper declaration beside contradictory narrative history."""
    target = tmp_path / "Demo.lean"
    target.write_text(
        "private lemma durable_helper : True := by trivial\n\n" "theorem demo : True := by sorry\n",
        encoding="utf-8",
    )
    source_context = decomposer_source_guard.load_decomposer_source_context(
        theorem_id="demo",
        file_path=str(target),
        cwd=str(tmp_path),
    )

    context = decomposer_prompt.shape_decomposer_prompt_context(
        theorem_id="demo",
        theorem_statement=source_context.target_statement,
        current_diagnostics="",
        current_goals="",
        current_attempt="Use durable_helper.",
        recent_failed_attempts=(
            "The checked durable_helper candidate is not yet banked and should be inserted."
        ),
        source_context=source_context,
    )
    prompt, stats = decomposer_prompt.compose_decomposer_user_prompt(
        context=context,
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        max_helper_count=2,
        question="Continue the proof.",
        json_contract='{"helpers":[]}',
    )

    assert "Current source declaration index" in prompt
    assert "`durable_helper`: present without placeholders" in prompt
    assert "overrides stale narrative absence or integration claims" in prompt
    assert stats["stale_source_status_claim_count"] == 1


def test_prompt_shaping_keeps_target_evidence_and_bounds_large_context(tmp_path):
    target = _write_constrained_target(tmp_path)
    source_context = decomposer_source_guard.load_decomposer_source_context(
        theorem_id="demo",
        file_path=str(target),
        cwd=str(tmp_path),
    )
    diagnostics = {
        "success": True,
        "errors": 1,
        "warnings": 82,
        "messages": [
            *[
                {
                    "severity": "warning",
                    "line": index + 100,
                    "message": "Missing AMS attribute for unrelated declaration",
                }
                for index in range(80)
            ],
            {
                "severity": "error",
                "line": 15,
                "message": "demo has an unsolved goal: ⊢ True",
            },
            {
                "severity": "warning",
                "line": 15,
                "message": "declaration uses `sorry`",
            },
        ],
    }
    attempts = "\n".join(
        f"- attempt: route-{index}\n  result: "
        + ("negative evidence: consistent terminal witness" if index == 2 else "ordinary failure")
        for index in range(10)
    )

    context = decomposer_prompt.shape_decomposer_prompt_context(
        theorem_id="demo",
        theorem_statement="theorem demo : True := by\n" + "  exact trivial\n" * 10_000,
        current_diagnostics=json.dumps(diagnostics),
        current_goals="⊢ True\n" + "goal detail\n" * 10_000,
        current_attempt="current tactic\n" * 10_000,
        recent_failed_attempts=attempts,
        source_context=source_context,
    )
    prompt, stats = decomposer_prompt.compose_decomposer_user_prompt(
        context=context,
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        max_helper_count=3,
        question="Find a useful helper.",
        json_contract='{"helpers":[]}',
    )

    assert "demo has an unsolved goal" in prompt
    assert "declaration uses `sorry`" in prompt
    assert "Missing AMS attribute" not in prompt
    assert "demo_terminal_conditions_consistent" in prompt
    assert "route-2" in prompt
    assert "route-9" in prompt
    assert "route-0" not in prompt
    assert len(prompt) <= decomposer_prompt.DECOMPOSER_USER_PROMPT_MAX_CHARS
    assert stats["omitted_diagnostic_count"] == 80
    assert stats["omitted_failed_attempt_count"] == 5
    assert stats["section_chars"]["current_goals"] <= decomposer_prompt.GOALS_MAX_CHARS
