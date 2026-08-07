"""Tests for the extracted lean_parsing pure parsers + native_runner re-export (Phase 2)."""

from leanflow_cli.lean import lean_parsing
from leanflow_cli.native import native_runner


def test_native_runner_reexports_are_identical():
    for name in lean_parsing.__all__:
        assert getattr(native_runner, name) is getattr(lean_parsing, name), name


def test_strip_lean_comments_and_strings_removes_comments_and_string_literals():
    src = (
        "theorem keep : True := by\n"
        "  -- sorry hidden in a line comment\n"
        "  /- block comment with sorry -/\n"
        '  let s := "string with sorry inside"\n'
        "  trivial\n"
    )
    stripped = lean_parsing._strip_lean_comments_and_strings(src)
    # The real `sorry`-free declaration survives; the three decoy "sorry"s are gone.
    assert "sorry" not in stripped
    assert "theorem keep" in stripped
    assert "trivial" in stripped
    # Newlines from a block comment are preserved so line geometry is not corrupted.
    assert stripped.count("\n") == src.count("\n")
    assert lean_parsing._text_has_sorry(src) is False


def test_declaration_line_index_from_text_indexes_kind_name_and_sorry():
    src = (
        "theorem alpha : True := by trivial\n\nlemma beta : True := by\n  sorry\n\ndef gamma := 1\n"
    )
    entries = lean_parsing._declaration_line_index_from_text(src)
    by_name = {e["name"]: e for e in entries}
    assert set(by_name) == {"alpha", "beta", "gamma"}
    assert by_name["beta"]["kind"] == "lemma"
    assert by_name["beta"]["has_sorry"] is True
    assert by_name["alpha"]["has_sorry"] is False
    assert by_name["alpha"]["line"] == 1
    # The names-only helper agrees with the full index.
    assert lean_parsing._declaration_names_from_text(src) == {"alpha", "beta", "gamma"}
    # Only alpha (and gamma is a def) is a completed theorem/lemma/example.
    assert lean_parsing._text_has_theorem_or_lemma_without_sorry(src) is True


def test_declaration_index_excludes_explicit_universe_suffix_separator():
    """Keep the declaration name separate from ``.{...}`` universe binders."""
    src = "theorem Space.example.{u_2, u_1} {A : Type u_1} : True := by trivial\n"

    entries = lean_parsing._declaration_line_index_from_text(src)

    assert [entry["name"] for entry in entries] == ["Space.example"]


def test_declaration_region_excludes_next_declaration_docs_and_attributes():
    src = "\n".join(
        [
            "theorem first : True := by",
            "  trivial",
            "",
            "/-- documentation for second -/",
            "@[category research open, AMS 11,",
            'formal_proof using lean4 at "https://example.test/proof"]',
            "theorem second : True := by",
            "  sorry",
        ]
    )

    entries = lean_parsing._declaration_line_index_from_text(src)

    assert entries[0]["end_line"] == 2
    assert entries[0]["text"] == "theorem first : True := by\n  trivial"
    assert "documentation for second" not in entries[0]["text"]
    assert "@[category research open" not in entries[0]["text"]
    assert "formal_proof using lean4" not in entries[0]["text"]


def test_find_assignment_marker_skips_comments_and_strings():
    # `:=` tokens inside a block comment and inside a string literal must be skipped; the
    # function returns the first *real* (top-level, uncommented, unquoted) `:=`.
    text = "theorem t /- := decoy -/ : True := by trivial -- := trailing"
    idx = lean_parsing._find_assignment_marker_for_statement(text)
    assert idx != -1
    assert text[idx : idx + 2] == ":="
    # It is the real proof-body marker, not the one hidden in the block comment / line comment.
    assert text[idx:].startswith(":= by trivial")

    # A `:=` buried entirely inside a string literal is not a marker.
    assert lean_parsing._find_assignment_marker_for_statement('let s := "a := b"') == 6
    assert lean_parsing._find_assignment_marker_for_statement('"only := inside a string"') == -1

    dependent = "theorem d : (let x := True; x) := by trivial"
    dependent_idx = lean_parsing._find_assignment_marker_for_statement(dependent)
    assert dependent[dependent_idx:].startswith(":= by trivial")


def test_statement_signature_text_excludes_proof_body():
    text = "theorem demo (n : Nat) : n = n := by\n  -- proof changes often\n  rfl"

    assert lean_parsing._statement_signature_text(text) == "theorem demo (n : Nat) : n = n"


def test_declaration_statement_text_keeps_dependent_lets_before_term_proof():
    """A term proof cannot make the statement parser select a type-level assignment."""
    signature = """private lemma dependent_term_proof (t : Nat) :
    let n := 840 * t + 361
    let x := 210 * t + 91
    n < x"""
    declaration = f"{signature} := dependentTermProof"

    assert lean_parsing.declaration_statement_text(declaration) == signature


def test_declaration_statement_text_does_not_count_escaped_assignment_keywords():
    """Escaped identifiers named let/have are types, not assignment forms."""
    for escaped_identifier in ("«let»", "«have»"):
        signature = f"theorem escaped_keyword : {escaped_identifier}"
        for proof in ("by exact escapedProof", "escapedProof"):
            declaration = f"{signature} := {proof}"
            assert lean_parsing.declaration_statement_text(declaration) == signature


def test_declaration_statement_text_keeps_top_level_have_before_proof():
    """A result-type have assignment is retained before by and term proofs."""
    signature = """theorem dependent_have :
    have h : True := True.intro
    True"""
    for proof in ("by trivial", "dependentHaveProof"):
        declaration = f"{signature} := {proof}"
        assert lean_parsing.declaration_statement_text(declaration) == signature


def test_extract_target_symbol_prefers_theorem_then_lemma_then_def():
    assert lean_parsing._extract_target_symbol("lemma foo : True") == "foo"
    assert lean_parsing._extract_target_symbol("def d := 1\ntheorem bar : True") == "bar"
    assert lean_parsing._extract_target_symbol("no declarations here") == ""


def test_suggestion_tactic_inside_parenthesized_term_is_detected():
    source = """theorem demo : True := by
  exact helper (hstep := by
    intro n hn h
    exact?) n hn
"""

    assert lean_parsing._lean_suggestion_tactic_markers(source) == ("exact?",)


def test_suggestion_tactics_are_detected_after_tactic_combinators():
    source = """theorem demo : True := by
  first
  | exact?
  | all_goals simp?
"""

    assert lean_parsing._lean_suggestion_tactic_markers(source) == ("exact?", "simp?")


def test_suggestion_like_text_in_identifiers_comments_and_strings_is_ignored():
    source = """theorem demo : True := by
  let myexact := "apply?"
  -- exact?
  trivial
"""

    assert lean_parsing._lean_suggestion_tactic_markers(source) == ()
