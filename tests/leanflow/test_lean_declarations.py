"""Tests for the extracted lean_declarations helpers + lean_services re-export (Phase 5).

These cover the pure, path-based declaration indexing / lookup cluster moved out of lean_services.
The re-export-identity test pins every moved name to the same object on ``lean_services`` so the
callers still living there (lean_inspect, lean_sorries, _local_proof_context_payload,
_local_incremental_auto_probe, lean_proof_context) resolve the extracted helpers without a
back-import. The behavior tests exercise: declaration boundary detection over attribute / modifier
preambles, name lookup (full + short namespaced name), the surrounding-declaration window, the
statement/proof split, and location-based text slicing.
"""

from __future__ import annotations

from leanflow_cli.lean import lean_declarations as ld
from leanflow_cli.lean import lean_services


def test_lean_services_reexports_are_identical():
    # Every re-exported name in lean_services must be the SAME object as in lean_declarations, so
    # the lean_services callers resolve the extracted helpers without a back-import.
    for name in ld.__all__:
        assert getattr(lean_services, name) is getattr(ld, name), name


def test_declaration_index_recognizes_preamble_and_boundaries(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "\n".join(
            [
                "import Mathlib",
                "",
                "@[simp]",
                "noncomputable def qNum : Nat := by",
                "  sorry",
                "",
                "/-- doc -/",
                "theorem qThm : True := by",
                "  trivial",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    entries = ld._declaration_index(target)

    assert [e["name"] for e in entries] == ["qNum", "qThm"]
    # The decorated `noncomputable def` is recognized through its attribute/modifier preamble.
    assert entries[0]["kind"] == "def"
    assert entries[0]["line"] == 4
    # The first declaration's region excludes separator whitespace and the next declaration's docs.
    assert entries[0]["end_line"] == 5
    assert "sorry" in entries[0]["text"]
    assert entries[1]["kind"] == "theorem"
    assert entries[1]["line"] == 8


def test_declaration_index_target_range_ends_on_last_proof_line(tmp_path):
    """Keep exact declaration ranges off the blank line after a tactic proof."""
    target = tmp_path / "Demo.lean"
    target.write_text(
        "\n".join(
            [
                "theorem target : True := by",
                "  sorry",
                "",
                "/-- documentation for the next declaration -/",
                "@[category research open, AMS 11,",
                'formal_proof using lean4 at "https://example.test/proof"]',
                "theorem next_target : True := by",
                "  trivial",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    entries = ld._declaration_index(target)

    assert entries[0]["end_line"] == 2
    assert entries[0]["text"] == "theorem target : True := by\n  sorry"


def test_declaration_index_excludes_next_scoped_command_preamble(tmp_path):
    """Keep a following scoped command and its docs out of the prior declaration."""
    target = tmp_path / "Demo.lean"
    target.write_text(
        "\n".join(
            [
                "def first : Nat := 1",
                "",
                "variable (P : Type) in",
                "/-- A scoped declaration. -/",
                "abbrev Scoped := P",
                "",
                "open scoped Classical in",
                "/-- Another scoped declaration. -/",
                "def second : Nat := 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    entries = ld._declaration_index(target)

    assert [entry["name"] for entry in entries] == ["first", "Scoped", "second"]
    assert entries[0]["end_line"] == 1
    assert entries[0]["text"] == "def first : Nat := 1"
    assert entries[1]["end_line"] == 5
    assert entries[1]["text"] == "abbrev Scoped := P"


def test_declaration_index_recognizes_inline_open_scoped_wrapper(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "open scoped Classical in def wrapped : Nat := 1\n",
        encoding="utf-8",
    )

    entries = ld._declaration_index(target)

    assert [(entry["kind"], entry["name"]) for entry in entries] == [("def", "wrapped")]


def test_declaration_index_missing_file_returns_empty(tmp_path):
    assert ld._declaration_index(tmp_path / "nope.lean") == []


def test_find_symbol_line_and_declaration_entry_short_name(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "\n".join(
            [
                "theorem Foo.bar : True := by",
                "  trivial",
                "lemma baz : True := by",
                "  trivial",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # _find_symbol_line matches the stored declaration name exactly.
    assert ld._find_symbol_line(target, "Foo.bar") == 1
    assert ld._find_symbol_line(target, "missing") is None
    assert ld._find_symbol_line(target, "") is None

    # _find_declaration_entry matches the stored short name `baz` against a namespaced query's
    # last segment, so a fully-qualified `Some.baz` still resolves the locally-declared `baz`.
    entry = ld._find_declaration_entry(target, "Some.baz")
    assert entry is not None
    assert entry["name"] == "baz"
    assert entry["kind"] == "lemma"
    # An exact full-name query resolves too.
    assert ld._find_declaration_entry(target, "Foo.bar")["name"] == "Foo.bar"
    assert ld._find_declaration_entry(target, "") is None


def test_surrounding_declarations_window(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "\n".join(f"theorem t{i} : True := trivial" for i in range(7)) + "\n",
        encoding="utf-8",
    )

    # Proof context may expose only preceding declarations; later neighbours
    # are not in scope while Lean elaborates the target.
    assert ld._surrounding_declarations(target, "t3", window=1) == ["t2"]
    assert ld._surrounding_declarations(target, "absent") == []


def test_surrounding_declarations_keeps_referenced_helper_outside_window(tmp_path):
    target = tmp_path / "Demo.lean"
    declarations = ["lemma banked : True := by trivial"]
    declarations.extend(f"lemma filler{i} : True := by trivial" for i in range(20))
    declarations.append("theorem result : True := by exact banked")
    target.write_text("\n\n".join(declarations) + "\n", encoding="utf-8")

    in_scope = ld._surrounding_declarations(target, "result", window=3)

    assert in_scope == ["banked", "filler17", "filler18", "filler19"]


def test_split_declaration_statement_and_proof():
    statement, proof = ld._split_declaration_statement_and_proof(
        "theorem demo : True := by\n  trivial"
    )
    assert statement == "theorem demo : True"
    assert proof == "trivial"

    # Without a `:= by` marker it falls back to first-line statement / remainder proof.
    statement, proof = ld._split_declaration_statement_and_proof("def x : Nat\n  := 0")
    assert statement == "def x : Nat"
    assert proof == ":= 0"

    assert ld._split_declaration_statement_and_proof("   ") == ("", "")


def test_declaration_text_from_location(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "\n".join(["line1", "theorem demo : True := by", "  trivial", "line4"]) + "\n",
        encoding="utf-8",
    )

    # decl_start/proof_end are 1-indexed inclusive line bounds; the slice is stripped.
    text = ld._declaration_text_from_location(
        target, {"decl_start": 2, "proof_end": 3, "decl_end": 2}
    )
    assert text == "theorem demo : True := by\n  trivial"

    # An empty / inverted range yields "".
    assert ld._declaration_text_from_location(target, {"decl_start": 0}) == ""
    assert ld._declaration_text_from_location(target, {"decl_start": 5, "decl_end": 4}) == ""
