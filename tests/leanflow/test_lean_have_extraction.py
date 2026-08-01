"""Test local-have discovery for automatic helper extraction."""

from leanflow_cli.lean.lean_have_extraction import candidates, ranked_candidates, select_candidate

DECLARATION = """theorem demo (a b : Nat) (h : a = b) : a = b := by
  have short : a = b := by
    exact h
  have substantial : a + 1 = b + 1 := by
    have nested : a = b := h
    calc
      a + 1 = b + 1 := by omega
      _ = b + 1 := rfl
      _ = b + 1 := by rfl
      _ = b + 1 := by omega
      _ = b + 1 := rfl
  exact h
"""


def test_candidates_return_only_outer_named_have_blocks():
    """Ignore nested local facts while preserving exact outer source spans."""
    found = candidates(DECLARATION)

    assert [candidate.name for candidate in found] == ["short", "substantial"]
    assert "have nested" in found[1].source
    assert DECLARATION[found[1].start : found[1].end] == found[1].source


def test_select_candidate_chooses_largest_eligible_block():
    """Choose the largest existing proof when no local name is requested."""
    selected = select_candidate(DECLARATION, minimum_lines=4)

    assert selected is not None
    assert selected.name == "substantial"


def test_select_candidate_honors_explicit_name_and_rejects_placeholders():
    """Allow deterministic selection while excluding admitted proof blocks."""
    selected = select_candidate(DECLARATION, have_name="short", minimum_lines=20)
    admitted = select_candidate(
        """theorem bad : True := by
  have unfinished : True := by
    sorry
  trivial
""",
        have_name="unfinished",
    )

    assert selected is not None
    assert selected.name == "short"
    assert admitted is None


def test_candidates_ignore_nested_block_and_line_comments():
    """Historical proof branches in comments must never become split candidates."""
    declaration = """theorem demo : True := by
  /- old branch
  have commented : True := by
    /- have nestedComment : True := by trivial -/
    trivial
  -/
  -- have lineComment : True := by trivial
  have active : True := by
    trivial
    trivial
  exact active
"""

    found = candidates(declaration)

    assert [candidate.name for candidate in found] == ["active"]


def test_ranked_candidates_reports_largest_context_reduction_first():
    ranked = ranked_candidates(DECLARATION, minimum_lines=2)

    assert [candidate.name for candidate in ranked] == ["substantial", "short"]
