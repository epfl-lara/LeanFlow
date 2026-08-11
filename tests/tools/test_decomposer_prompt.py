"""Test bounded decomposition prompts and source-backed route guards."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.implementations import lean_experts
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


def test_helper_validation_batches_elaborating_templates(monkeypatch):
    replacements: list[str] = []

    def fake_check(**kwargs):
        replacements.append(kwargs["replacement"])
        return {
            "success": True,
            "ok": False,
            "errors": 0,
            "sorry": 1,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(Path("Demo.lean").resolve()),
            "target": "demo",
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "output": "warning: declaration uses `sorry`",
        }

    monkeypatch.setattr(lean_experts, "lean_incremental_check", fake_check)
    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": f"demo_helper_{index}",
                "lean_skeleton": f"private lemma demo_helper_{index} : True := by\n  sorry",
            }
            for index in range(3)
        ],
        theorem_statement="theorem demo : True := by",
        file_path="Demo.lean",
        theorem_id="demo",
        cwd="",
        timeout_s=30,
    )

    assert len(replacements) == 1
    assert all(f"demo_helper_{index}" in replacements[0] for index in range(3))
    assert all(helper["ready_to_prove"] is True for helper in helpers)
    assert all(helper["ready_to_insert"] is False for helper in helpers)
    assert all(helper["ready_for_managed_placement"] is True for helper in helpers)
    assert validation["validation_mode"] == "batch"
    assert validation["lean_check_count"] == 1
    assert validation["validated_count"] == 3


def test_helper_validation_short_circuits_shared_unprovided_identifiers(monkeypatch, tmp_path):
    """Avoid per-helper retries when every proposal needs the same absent witness API."""
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls: list[str] = []

    def unknown_witnesses(**kwargs):
        calls.append(kwargs["replacement"])
        return {
            "success": True,
            "ok": False,
            "errors": 3,
            "has_errors": True,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": "demo",
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "messages": [
                {
                    "severity": "error",
                    "message": f"Unknown identifier `{identifier}`",
                }
                for identifier in (
                    "exceptional169_x",
                    "exceptional169_y",
                    "exceptional169_z",
                )
            ],
        }

    monkeypatch.setattr(lean_experts, "lean_incremental_check", unknown_witnesses)
    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": "demo_witness_order",
                "lean_skeleton": (
                    "private lemma demo_witness_order (s : Nat) :\n"
                    "    exceptional169_x s < exceptional169_y s ∧\n"
                    "      exceptional169_y s < exceptional169_z s := by\n"
                    "  sorry"
                ),
            },
            {
                "name": "demo_rational_identity",
                "lean_skeleton": (
                    "private lemma demo_rational_identity (s : Nat) :\n"
                    "    (exceptional169_x s : Rat) < exceptional169_y s ∧\n"
                    "      (exceptional169_y s : Rat) < exceptional169_z s := by\n"
                    "  sorry"
                ),
                "dependencies": ["demo_witness_order"],
            },
            {
                "name": "demo_witness",
                "lean_skeleton": (
                    "private lemma demo_witness (s : Nat) :\n"
                    "    exceptional169_x s < exceptional169_y s ∧\n"
                    "      exceptional169_y s < exceptional169_z s := by\n"
                    "  sorry"
                ),
                "dependencies": ["demo_witness_order", "demo_rational_identity"],
            },
        ],
        theorem_statement="theorem demo : True := by",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    assert len(calls) == 1
    assert all(helper["check_status"] == "failed" for helper in helpers)
    assert validation["validation_mode"] == "batch_unprovided_identifiers"
    assert validation["lean_check_count"] == 1
    assert validation["unprovided_identifiers"] == [
        "exceptional169_x",
        "exceptional169_y",
        "exceptional169_z",
    ]


def test_helper_validation_keeps_fallback_when_only_one_helper_needs_unknown(monkeypatch, tmp_path):
    """Retain attribution checks so an independent helper can still be accepted."""
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls: list[str] = []

    def check_with_partial_failure(**kwargs):
        replacement = kwargs["replacement"]
        calls.append(replacement)
        payload = {
            "success": True,
            "ok": False,
            "errors": 0,
            "has_errors": False,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": "demo",
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "messages": [],
        }
        if "external_witness" in replacement:
            payload.update(
                {
                    "errors": 1,
                    "has_errors": True,
                    "messages": [
                        {
                            "severity": "error",
                            "message": "Unknown identifier `external_witness`",
                        }
                    ],
                }
            )
        return payload

    monkeypatch.setattr(lean_experts, "lean_incremental_check", check_with_partial_failure)
    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": "demo_external",
                "lean_skeleton": (
                    "private lemma demo_external : True := by\n" "  exact external_witness"
                ),
            },
            {
                "name": "demo_independent",
                "lean_skeleton": "private lemma demo_independent : True := by\n  sorry",
            },
        ],
        theorem_statement="theorem demo : True := by",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    assert len(calls) == 3
    assert helpers[0]["check_status"] == "failed"
    assert helpers[1]["check_status"] == "ok"
    assert validation["validation_mode"] == "sequential_fallback"
    assert validation["lean_check_count"] == 3


def test_helper_validation_does_not_treat_local_binder_as_external_reference(monkeypatch, tmp_path):
    """A same-named local binder cannot prove that an independent helper is doomed."""
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls: list[str] = []

    def check_with_shadowed_name(**kwargs):
        replacement = kwargs["replacement"]
        calls.append(replacement)
        first_helper_present = "private lemma demo_external" in replacement
        payload = {
            "success": True,
            "ok": False,
            "errors": int(first_helper_present),
            "has_errors": first_helper_present,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": "demo",
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "messages": (
                [
                    {
                        "severity": "error",
                        "message": "Unknown identifier `external_witness`",
                    }
                ]
                if first_helper_present
                else []
            ),
        }
        return payload

    monkeypatch.setattr(lean_experts, "lean_incremental_check", check_with_shadowed_name)
    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": "demo_external",
                "lean_skeleton": (
                    "private lemma demo_external : True := by\n" "  exact external_witness"
                ),
            },
            {
                "name": "demo_local",
                "lean_skeleton": (
                    "private lemma demo_local (external_witness : True) : True := by\n"
                    "  exact external_witness"
                ),
            },
        ],
        theorem_statement="theorem demo : True := by",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    assert len(calls) == 3
    assert helpers[0]["check_status"] == "failed"
    assert helpers[1]["check_status"] == "ok"
    assert validation["validation_mode"] == "sequential_fallback"


def test_helper_validation_does_not_treat_character_data_as_external_reference(
    monkeypatch, tmp_path
):
    """Identifier-like character data cannot make an independent helper look doomed."""
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls: list[str] = []

    def check_with_character_data(**kwargs):
        replacement = kwargs["replacement"]
        calls.append(replacement)
        first_helper_present = "private lemma demo_external" in replacement
        return {
            "success": True,
            "ok": False,
            "errors": int(first_helper_present),
            "has_errors": first_helper_present,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": "demo",
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "messages": (
                [{"severity": "error", "message": "Unknown identifier `x`"}]
                if first_helper_present
                else []
            ),
        }

    monkeypatch.setattr(lean_experts, "lean_incremental_check", check_with_character_data)
    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": "demo_external",
                "lean_skeleton": "private lemma demo_external : True := by\n  exact x",
            },
            {
                "name": "demo_character",
                "lean_skeleton": "private lemma demo_character : ('x' : Char) = 'x' := by\n  sorry",
            },
        ],
        theorem_statement="theorem demo : True := by",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    assert len(calls) == 3
    assert helpers[0]["check_status"] == "failed"
    assert helpers[1]["check_status"] == "ok"
    assert validation["validation_mode"] == "sequential_fallback"


def test_helper_validation_keeps_fallback_for_proposed_later_dependency(monkeypatch, tmp_path):
    """Do not classify a source-order error as an external missing declaration."""
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls: list[str] = []

    def check_with_late_dependency(**kwargs):
        replacement = kwargs["replacement"]
        calls.append(replacement)
        payload = {
            "success": True,
            "ok": False,
            "errors": 0,
            "has_errors": False,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": "demo",
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "messages": [],
        }
        uses_position = replacement.find("private lemma demo_uses_later")
        later_position = replacement.find("private lemma demo_later")
        if uses_position >= 0 and (later_position < 0 or uses_position < later_position):
            payload.update(
                {
                    "errors": 1,
                    "has_errors": True,
                    "messages": [
                        {
                            "severity": "error",
                            "message": "Unknown identifier `demo_later`",
                        }
                    ],
                }
            )
        return payload

    monkeypatch.setattr(lean_experts, "lean_incremental_check", check_with_late_dependency)
    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": "demo_uses_later",
                "lean_skeleton": ("private lemma demo_uses_later : True := by\n  exact demo_later"),
                "dependencies": ["demo_later"],
            },
            {
                "name": "demo_later",
                "lean_skeleton": "private lemma demo_later : True := by\n  sorry",
            },
        ],
        theorem_statement="theorem demo : True := by",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    assert len(calls) == 3
    assert helpers[0]["check_status"] == "failed"
    assert helpers[1]["check_status"] == "ok"
    assert validation["validation_mode"] == "sequential_fallback"


def test_helper_validation_keeps_fallback_for_mixed_batch_errors(monkeypatch, tmp_path):
    """Retain diagnostic retries when the batch is not purely missing identifiers."""
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls: list[str] = []

    def mixed_failure(**kwargs):
        calls.append(kwargs["replacement"])
        return {
            "success": True,
            "ok": False,
            "errors": 2,
            "has_errors": True,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": "demo",
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "messages": [
                {
                    "severity": "error",
                    "message": "Unknown identifier `external_witness`",
                },
                {"severity": "error", "message": "type mismatch"},
            ],
        }

    monkeypatch.setattr(lean_experts, "lean_incremental_check", mixed_failure)
    _, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": f"demo_external_{index}",
                "lean_skeleton": (
                    f"private lemma demo_external_{index} : True := by\n" "  exact external_witness"
                ),
            }
            for index in range(2)
        ],
        theorem_statement="theorem demo : True := by",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    assert len(calls) == 3
    assert validation["validation_mode"] == "sequential_fallback"
    assert validation["lean_check_count"] == 3


@pytest.mark.parametrize(
    "unsafe_update",
    [
        {"errors": 0, "messages": [{"severity": "error", "message": "hidden error"}]},
        {"has_errors": True},
        {"replacement_matches_target": False},
        {"verification_scope": "scratch_replacement"},
    ],
)
def test_helper_validation_rejects_fail_open_check_payloads(monkeypatch, tmp_path, unsafe_update):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    check = {
        "success": True,
        "ok": False,
        "errors": 0,
        "has_errors": False,
        "sorry": 1,
        "tool": "lean_probe",
        "action": "check_target",
        "file": str(target.resolve()),
        "target": "demo",
        "replacement_matches_target": True,
        "verification_scope": "target_candidate",
    }
    check.update(unsafe_update)
    monkeypatch.setattr(lean_experts, "lean_incremental_check", lambda **kwargs: check)

    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": "demo_helper",
                "lean_skeleton": "private lemma demo_helper : True := by\n  sorry",
            }
        ],
        theorem_statement="theorem demo : True := by",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    assert helpers[0]["check_status"] == "failed"
    assert helpers[0]["ready_to_prove"] is False
    assert helpers[0]["ready_for_managed_placement"] is False
    assert validation["ready_for_managed_placement_count"] == 0


def test_invalid_batch_identity_does_not_trigger_repeated_sequential_checks(monkeypatch, tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls: list[str] = []

    def invalid_identity(**kwargs):
        calls.append(kwargs["replacement"])
        return {
            "success": True,
            "errors": 0,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": "demo",
            "replacement_matches_target": False,
            "verification_scope": "scratch_replacement",
        }

    monkeypatch.setattr(lean_experts, "lean_incremental_check", invalid_identity)
    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": f"demo_helper_{index}",
                "lean_skeleton": f"private lemma demo_helper_{index} : True := by\n  sorry",
            }
            for index in range(3)
        ],
        theorem_statement="theorem demo : True := by",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    assert len(calls) == 1
    assert all(helper["check_status"] == "failed" for helper in helpers)
    assert validation["validation_mode"] == "batch_contract_failure"
    assert validation["lean_check_count"] == 1


def test_prior_environment_failure_uses_canonical_full_source_fallback(monkeypatch, tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    exact_sources: list[str] = []

    def broken_incremental(**kwargs):
        return {
            "success": False,
            "ok": False,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": "demo",
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "error_code": "prior_decl_failed",
            "error": (
                "failed to build env before target at Move.half: "
                "line 66:0 error: unexpected end of input"
            ),
        }

    def exact_source_check(source, **kwargs):
        exact_sources.append(source)
        return {
            "success": True,
            "ok": True,
            "timed_out": False,
            "retryable": False,
            "failure_kind": "",
            "output": "Demo.lean:2:0: warning: declaration uses `sorry`",
            "messages": [],
        }

    monkeypatch.setattr(lean_experts, "lean_incremental_check", broken_incremental)
    monkeypatch.setattr(lean_experts, "lean_ephemeral_source_check", exact_source_check)

    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[
            {
                "name": f"demo_helper_{index}",
                "lean_skeleton": f"private lemma demo_helper_{index} : True := by\n  sorry",
            }
            for index in range(2)
        ],
        theorem_statement="theorem demo : True := by",
        file_path=str(target),
        theorem_id="demo",
        cwd=str(tmp_path),
        timeout_s=30,
    )

    assert len(exact_sources) == 1
    assert exact_sources[0].index("demo_helper_0") < exact_sources[0].index("theorem demo")
    assert exact_sources[0].index("demo_helper_1") < exact_sources[0].index("theorem demo")
    assert all(helper["check_status"] == "ok" for helper in helpers)
    assert validation["validation_mode"] == "batch"
    assert validation["lean_check_count"] == 1
    assert validation["canonical_fallback_count"] == 1
    assert validation["ready_to_prove_count"] == 2


def test_decompose_tool_uses_resolved_source_statement_over_caller_text(monkeypatch, tmp_path):
    """Exact file identity makes the source signature authoritative end to end."""
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    captured: dict[str, object] = {}
    replacements: list[str] = []

    def fake_call_llm(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            model="test-model",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "obstacle_summary": "split the source target",
                                "recommended_split": "prove two small helpers",
                                "helpers": [
                                    {
                                        "name": f"demo_helper_{index}",
                                        "purpose": "source-backed helper",
                                        "lean_skeleton": (
                                            f"private lemma demo_helper_{index} : True := by\n"
                                            "  sorry"
                                        ),
                                        "dependencies": [],
                                        "proof_hints": ["trivial"],
                                    }
                                    for index in range(2)
                                ],
                            }
                        )
                    )
                )
            ],
        )

    def exact_source_check(**kwargs):
        replacement = str(kwargs["replacement"])
        replacements.append(replacement)
        assert kwargs["allow_placeholders_for_elaboration"] is True
        assert "theorem demo : True := by\n  sorry" in replacement
        assert "theorem demo : False" not in replacement
        return {
            "success": True,
            "errors": 0,
            "has_errors": False,
            "sorry": 3,
            "tool": "lean_probe",
            "action": "check_target",
            "file": str(target.resolve()),
            "target": "demo",
            "replacement_matches_target": True,
            "verification_scope": "target_candidate",
            "output": "warning: declaration uses 'sorry'",
        }

    monkeypatch.setattr(lean_experts, "call_llm", fake_call_llm)
    monkeypatch.setattr(lean_experts, "lean_incremental_check", exact_source_check)

    payload = json.loads(
        lean_experts.lean_decompose_helpers_tool(
            "demo",
            str(target),
            theorem_statement="theorem demo : False := by",
            cwd=str(tmp_path),
        )
    )

    user_prompt = str(captured["messages"][1]["content"])
    assert "Theorem statement/signature:\ntheorem demo : True" in user_prompt
    assert "theorem demo : False" not in user_prompt
    assert len(replacements) == 1
    assert payload["skeleton_validation"]["validation_mode"] == "batch"
    assert payload["skeleton_validation"]["ready_to_prove_count"] == 2
    assert payload["context_shaping"]["theorem_statement_source"] == "source"
    assert payload["context_shaping"]["caller_statement_overridden"] is True


@pytest.mark.parametrize(
    ("name", "skeleton"),
    [
        ("demo_helper", "private lemma different_name : True := by\n  sorry"),
        ("demo_helper", "open Nat\nprivate lemma demo_helper : True := by\n  sorry"),
        (
            "demo_helper",
            "private lemma demo_helper : True := by\n  sorry\n"
            "private lemma smuggled : True := by\n  sorry",
        ),
        ("demo_helper", "axiom demo_helper : True"),
        ("demo_helper", "private lemma demo_helper : True := by\n  sorry\nopen Nat"),
    ],
)
def test_helper_validation_rejects_shape_smuggling_without_lean_check(monkeypatch, name, skeleton):
    calls: list[object] = []
    monkeypatch.setattr(
        lean_experts,
        "lean_incremental_check",
        lambda **kwargs: calls.append(kwargs),
    )

    helpers, validation = lean_experts._validate_helper_skeletons(
        helpers=[{"name": name, "lean_skeleton": skeleton}],
        theorem_statement="theorem demo : True := by",
        file_path="Demo.lean",
        theorem_id="demo",
        cwd="",
        timeout_s=30,
    )

    assert calls == []
    assert helpers[0]["check_status"] == "rejected_shape"
    assert helpers[0]["ready_for_managed_placement"] is False
    assert validation["shape_rejected_count"] == 1
    assert validation["lean_check_count"] == 0


def test_decompose_tool_filters_context_and_rejects_source_conflict(monkeypatch, tmp_path):
    target = _write_constrained_target(tmp_path)
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
                                "obstacle_summary": "All residue routes fail.",
                                "recommended_split": "Derive the terminal contradiction.",
                                "helpers": [
                                    {
                                        "name": "demo_terminal_residue_cover",
                                        "purpose": (
                                            "Close the exhausted terminal residue branch "
                                            "by contradiction."
                                        ),
                                        "lean_skeleton": (
                                            "private lemma demo_terminal_residue_cover : "
                                            "False := by\n  sorry"
                                        ),
                                        "dependencies": [],
                                        "proof_hints": ["All terminal residues are impossible."],
                                    }
                                ],
                            }
                        )
                    )
                )
            ],
        )

    def unexpected_check(**kwargs):
        raise AssertionError("source-conflicted helpers must not start a Lean check")

    diagnostics = json.dumps(
        {
            "errors": 1,
            "messages": [
                *[
                    {
                        "severity": "warning",
                        "line": index + 100,
                        "message": "Missing problem category attribute",
                    }
                    for index in range(200)
                ],
                {
                    "severity": "error",
                    "line": 15,
                    "message": "demo: unsolved goal ⊢ True",
                },
            ],
        }
    )
    monkeypatch.setattr(lean_experts, "call_llm", fake_call_llm)
    monkeypatch.setattr(lean_experts, "lean_incremental_check", unexpected_check)

    payload = json.loads(
        lean_experts.lean_decompose_helpers_tool(
            "demo",
            str(target),
            theorem_statement="theorem demo : True := by\n  sorry",
            current_diagnostics=diagnostics,
            current_goals="⊢ True",
            current_attempt="exact?\n" * 20_000,
            cwd=str(tmp_path),
        )
    )

    user_prompt = captured["messages"][1]["content"]
    system_prompt = captured["messages"][0]["content"]
    helper = payload["helpers"][0]
    assert len(user_prompt) <= decomposer_prompt.DECOMPOSER_USER_PROMPT_MAX_CHARS
    assert "Missing problem category attribute" not in user_prompt
    assert "demo: unsolved goal" in user_prompt
    assert "demo_terminal_conditions_consistent" in user_prompt
    assert "never turn a source-verified consistent terminal branch" in system_prompt
    assert payload["status"] == "answered_with_source_guard"
    assert payload["source_constraint_guard"]["applied"] is True
    assert payload["skeleton_validation"]["source_conflict_count"] == 1
    assert payload["skeleton_validation"]["lean_check_count"] == 0
    assert helper["check_status"] == "rejected_source_conflict"
    assert helper["ready_to_prove"] is False
    assert helper["lean_skeleton"] == ""
    assert helper["rejected_lean_skeleton_chars"] > 0
    assert "Discard the source-conflicted terminal contradiction" in payload["recommended_split"]
    assert payload["next_step"].startswith("Discard every helper marked rejected_source_conflict")
    assert payload["context_shaping"]["user_prompt_chars"] == len(user_prompt)
