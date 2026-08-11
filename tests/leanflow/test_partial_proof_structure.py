"""Tests for deterministic monolithic partial-proof decomposition guidance."""

from leanflow_cli.workflows import partial_proof_structure


def test_complex_unresolved_proof_requests_graph_visible_helpers():
    declaration = """
theorem result : True := by
  have h₁ : True := by trivial
  have h₂ : True := by trivial
  have h₃ : True := by trivial
  sorry
"""

    structure = partial_proof_structure.assess(declaration)
    guidance = partial_proof_structure.feedback_lines("result", declaration)

    assert structure.needs_decomposition
    assert structure.milestone_count == 3
    assert "top-level helper declarations" in "\n".join(guidance)
    assert "lean_decompose_helpers" in "\n".join(guidance)


def test_small_or_completed_proof_does_not_request_decomposition():
    small = "theorem result : True := by\n  have h : True := by trivial\n  sorry\n"
    complete = """
theorem result : True := by
  have h₁ : True := by trivial
  have h₂ : True := by trivial
  have h₃ : True := by trivial
  exact h₃
"""

    assert not partial_proof_structure.assess(small).needs_decomposition
    assert not partial_proof_structure.assess(complete).needs_decomposition
    assert partial_proof_structure.feedback_lines("result", small) == ()
