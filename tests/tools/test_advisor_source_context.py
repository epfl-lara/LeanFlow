"""Characterize authoritative source grounding for Lean proof advisors."""

from tools.utilities.advisor_source_context import (
    advisor_source_conflicts,
    load_advisor_source_context,
)


def test_advisor_source_context_loads_exact_target_and_referenced_definitions(tmp_path):
    target = tmp_path / "Main.lean"
    target.write_text(
        """\
namespace Demo

noncomputable def finalValue (p : Nat → Nat) : Nat :=
  ∏ i in Finset.range 3, p i

private lemma finalValue_pos (p : Nat → Nat) : 0 < finalValue p := by
  sorry

theorem result (p : Nat → Nat) : finalValue p = finalValue p := by
  sorry

end Demo
""",
        encoding="utf-8",
    )

    context = load_advisor_source_context(
        theorem_id="result",
        file_path=str(target),
        evidence="Use finalValue and finalValue_pos, but do not guess their definitions.",
    )

    assert context.status == "loaded"
    assert context.target_statement == (
        "theorem result (p : Nat → Nat) : finalValue p = finalValue p"
    )
    assert context.referenced_names == ("finalValue", "finalValue_pos")
    rendered = context.render()
    assert "noncomputable def finalValue" in rendered
    assert "∏ i in Finset.range 3, p i" in rendered
    assert "private lemma finalValue_pos" in rendered
    assert "sorry" not in rendered


def test_advisor_source_conflict_rejects_hypothetical_redefinition(tmp_path):
    target = tmp_path / "Main.lean"
    target.write_text(
        """\
def finalValue (n : Nat) : Nat := n + 1
theorem result (n : Nat) : 0 < finalValue n := by
  sorry
""",
        encoding="utf-8",
    )
    context = load_advisor_source_context(
        theorem_id="result",
        file_path=str(target),
        evidence="Reason about finalValue.",
    )

    assert advisor_source_conflicts(
        "Assuming `finalValue` is the product of all entries, unfold it first.",
        context,
    ) == ("finalValue",)
    assert (
        advisor_source_conflicts(
            "Use the exact supplied `finalValue` declaration and prove its result is positive.",
            context,
        )
        == ()
    )


def test_advisor_allows_evidence_based_revision_of_provisional_determine_answer(tmp_path):
    target = tmp_path / "Main.lean"
    target.write_text(
        """\
/-- The answer to be determined. -/
def answer : Set Nat := Set.univ
theorem result : {n : Nat | P n} = answer := by
  sorry
""",
        encoding="utf-8",
    )
    context = load_advisor_source_context(
        theorem_id="result",
        file_path=str(target),
        evidence="Determine whether answer should be revised.",
    )

    assert context.provisional_names == ("answer",)
    assert "current bodies are conjectures" in context.render()
    assert (
        advisor_source_conflicts(
            "The definition of `answer` should instead be the singleton {2}.",
            context,
        )
        == ()
    )
