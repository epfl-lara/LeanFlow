"""Tests for the extracted lean_parsing pure parsers + native_runner re-export (Phase 2)."""

from epflemma_cli import lean_parsing, native_runner


def test_native_runner_reexports_are_identical():
    for name in lean_parsing.__all__:
        assert getattr(native_runner, name) is getattr(lean_parsing, name), name


def test_strip_lean_comments_and_strings_removes_comments_and_string_literals():
    src = (
        'theorem keep : True := by\n'
        '  -- sorry hidden in a line comment\n'
        '  /- block comment with sorry -/\n'
        '  let s := "string with sorry inside"\n'
        '  trivial\n'
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
        "theorem alpha : True := by trivial\n"
        "\n"
        "lemma beta : True := by\n"
        "  sorry\n"
        "\n"
        "def gamma := 1\n"
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


def test_find_assignment_marker_skips_comments_and_strings():
    # `:=` tokens inside a block comment and inside a string literal must be skipped; the
    # function returns the first *real* (top-level, uncommented, unquoted) `:=`.
    text = 'theorem t /- := decoy -/ : True := by trivial -- := trailing'
    idx = lean_parsing._find_assignment_marker_for_statement(text)
    assert idx != -1
    assert text[idx : idx + 2] == ":="
    # It is the real proof-body marker, not the one hidden in the block comment / line comment.
    assert text[idx:].startswith(":= by trivial")

    # A `:=` buried entirely inside a string literal is not a marker.
    assert lean_parsing._find_assignment_marker_for_statement('let s := "a := b"') == 6
    assert lean_parsing._find_assignment_marker_for_statement('"only := inside a string"') == -1


def test_extract_target_symbol_prefers_theorem_then_lemma_then_def():
    assert lean_parsing._extract_target_symbol("lemma foo : True") == "foo"
    assert lean_parsing._extract_target_symbol("def d := 1\ntheorem bar : True") == "bar"
    assert lean_parsing._extract_target_symbol("no declarations here") == ""
