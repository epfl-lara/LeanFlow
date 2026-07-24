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


def test_resolve_multi_attempt_location_keeps_multiline_tactic_line_only(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")

    assert location._resolve_multi_attempt_location(target, 2, None) == (2, None, None)


def test_resolve_multi_attempt_location_corrects_blank_after_multiline_proof(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem target : True := by\n  sorry\n\n" "theorem next_target : True := by trivial\n",
        encoding="utf-8",
    )

    assert location._resolve_multi_attempt_location(target, 3, 11) == (
        2,
        None,
        "previous_tactic_line_after_blank",
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

    assert location._resolve_multi_attempt_location(target, 1, None) == (1, None, None)
