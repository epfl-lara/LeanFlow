"""Characterize safe source-position normalization for Lean multi-attempt."""

from __future__ import annotations

from leanflow_cli.lean import lean_attempt_location as location

ERDOS_242_DECLARATION = (
    "private lemma erdos_242_family_one_ordering (s : ℕ) (hs : 1 ≤ s) :\n"
    "    1 ≤ 210 * s + 1 ∧ 210 * s + 1 < 840 * s * (210 * s + 1) ∧\n"
    "      840 * s * (210 * s + 1) < 840 * s * (210 * s + 1) + "
    "(210 * s + 1) := by sorry\n"
)


def test_resolve_multi_attempt_location_targets_inline_body_at_line_1083(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "\n" * 1080 + ERDOS_242_DECLARATION + "private lemma next_target : True := by trivial\n",
        encoding="utf-8",
    )

    resolved = location._resolve_multi_attempt_location(target, 1083, None)
    target_line = target.read_text(encoding="utf-8").splitlines()[1082]

    assert target_line.index("sorry") + 1 == 79
    assert target_line[78:] == "sorry"
    assert resolved == (1083, 79, "inline_tactic_body")


def test_resolve_multi_attempt_location_preserves_explicit_column(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(ERDOS_242_DECLARATION, encoding="utf-8")

    assert location._resolve_multi_attempt_location(target, 3, 10) == (3, 10, None)


def test_resolve_multi_attempt_location_repairs_out_of_range_column(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")

    assert location._resolve_multi_attempt_location(target, 1, 90) == (
        2,
        3,
        "invalid_column_to_trailing_placeholder",
    )


def test_resolve_multi_attempt_location_rejects_out_of_range_column_without_hole(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem target : True := by trivial\n", encoding="utf-8")

    assert location._resolve_multi_attempt_location(target, 1, 90) == (
        1,
        None,
        "invalid_column",
    )


def test_resolve_multi_attempt_location_targets_multiline_trailing_sorry(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")

    assert location._resolve_multi_attempt_location(target, 2, None) == (
        2,
        3,
        "trailing_placeholder",
    )


def test_resolve_multi_attempt_location_advances_to_next_line_trailing_sorry(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem target : True := by\n  have h : True := trivial\n  sorry\n", encoding="utf-8"
    )

    assert location._resolve_multi_attempt_location(target, 2, None) == (
        3,
        3,
        "trailing_placeholder",
    )


def test_resolve_multi_attempt_location_finds_unique_moved_placeholder(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem target : True := by\n"
        "  have h1 : True := trivial\n"
        "  have h2 : True := trivial\n"
        "  have h3 : True := trivial\n"
        "  sorry\n",
        encoding="utf-8",
    )

    assert location._resolve_multi_attempt_location(target, 2, None) == (
        5,
        3,
        "trailing_placeholder",
    )


def test_resolve_multi_attempt_location_prefers_first_placeholder_at_or_after_request(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem target : True ∧ True := by\n" "  constructor\n" "  · sorry\n" "  · sorry\n",
        encoding="utf-8",
    )

    assert location._resolve_multi_attempt_location(target, 2, None) == (
        3,
        5,
        "trailing_placeholder",
    )


def test_resolve_multi_attempt_location_selects_later_branch_after_earlier_hole(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem target : True ∧ True := by\n"
        "  constructor\n"
        "  · sorry\n"
        "  · have marker : True := trivial\n"
        "    sorry\n",
        encoding="utf-8",
    )

    assert location._resolve_multi_attempt_location(target, 4, None) == (
        5,
        5,
        "trailing_placeholder",
    )


def test_resolve_multi_attempt_location_rejects_ambiguous_backward_jump(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem target : True ∧ True := by\n"
        "  constructor\n"
        "  · sorry\n"
        "  · sorry\n"
        "  have done : True := trivial\n",
        encoding="utf-8",
    )

    assert location._resolve_multi_attempt_location(target, 5, None) == (
        5,
        None,
        "ambiguous_backward_placeholders",
    )


def test_resolve_multi_attempt_location_corrects_blank_after_multiline_proof(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem target : True := by\n  sorry\n\n" "theorem next_target : True := by trivial\n",
        encoding="utf-8",
    )

    assert location._resolve_multi_attempt_location(target, 3, 11) == (
        2,
        3,
        "trailing_placeholder",
    )


def test_resolve_multi_attempt_location_ignores_comment_and_string_decoys(tmp_path):
    target = tmp_path / "Demo.lean"
    declaration = (
        'theorem target (label : String := ":= by sorry") : True '
        "/- := by sorry -/ := by /- tactic note -/ exact True.intro"
    )
    target.write_text(f"{declaration}\n", encoding="utf-8")

    assert location._resolve_multi_attempt_location(target, 1, None) == (
        1,
        declaration.index("exact") + 1,
        "inline_tactic_body",
    )


def test_resolve_multi_attempt_location_does_not_infer_term_proof_column(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem target : True := sorry\n", encoding="utf-8")

    assert location._resolve_multi_attempt_location(target, 1, None) == (
        1,
        None,
        "non_tactic_source_line",
    )


def test_resolve_multi_attempt_location_rejects_import_line(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "import Mathlib\n\ntheorem target : True := by\n  trivial\n", encoding="utf-8"
    )

    assert location._resolve_multi_attempt_location(target, 1, None) == (
        1,
        None,
        "non_tactic_source_line",
    )


def test_resolve_multi_attempt_location_advances_header_to_first_tactic(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem target : True := by\n  trivial\n", encoding="utf-8")

    assert location._resolve_multi_attempt_location(target, 1, None) == (
        2,
        None,
        "first_tactic_line",
    )


def test_resolve_multi_attempt_location_rejects_cross_line_closing_suffix(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem target : True := by\n" "  exact id (by\n" "    exact True.intro)\n",
        encoding="utf-8",
    )

    assert location._resolve_multi_attempt_location(target, 3, None) == (
        3,
        None,
        "cross_line_structural_suffix",
    )


def test_multi_attempt_replacement_candidate_builds_complete_declaration(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")

    assert location._multi_attempt_replacement_candidate(target, 2, 3, "exact True.intro") == (
        "target",
        "theorem target : True := by\n  exact True.intro",
    )
