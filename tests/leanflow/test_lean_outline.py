"""Tests for the declaration-outline helpers (lean_declarations.declaration_outline/region).

Build a small .lean fixture and assert the token-cheap outline rows and the single-declaration
source-region lookup, plus the JSON tool wrapper's outline-string / region shape.
"""

from __future__ import annotations

import json

import tools.implementations.lean_tool as lean_tool
from leanflow_cli.lean import lean_declarations as ld

_FIXTURE = "\n".join(
    [
        "import Mathlib",
        "",
        "@[simp]",
        "def qNum : Nat := 3",
        "",
        "theorem qThm : True := by",
        "  trivial",
        "",
        "lemma qLem (n : Nat) : n = n := by",
        "  rfl",
    ]
)


def _write_fixture(tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text(_FIXTURE + "\n", encoding="utf-8")
    return target


def test_declaration_outline_lists_each_declaration(tmp_path):
    target = _write_fixture(tmp_path)

    outline = ld.declaration_outline(target)

    assert [(row["kind"], row["name"]) for row in outline] == [
        ("def", "qNum"),
        ("theorem", "qThm"),
        ("lemma", "qLem"),
    ]
    # Outline rows carry line ranges but not the heavy source text.
    assert outline[1]["line"] == 6
    assert outline[1]["end_line"] == 7
    assert "text" not in outline[0]


def test_declaration_region_returns_source_text(tmp_path):
    target = _write_fixture(tmp_path)

    region = ld.declaration_region(target, "qThm")

    assert region is not None
    assert region["kind"] == "theorem"
    assert region["name"] == "qThm"
    assert region["line"] == 6
    assert "trivial" in region["text"]


def test_declaration_region_missing_symbol_returns_none(tmp_path):
    target = _write_fixture(tmp_path)

    assert ld.declaration_region(target, "nope") is None


def test_lean_outline_tool_returns_outline_lines(tmp_path):
    target = _write_fixture(tmp_path)

    payload = json.loads(lean_tool.lean_outline_tool(str(target)))

    assert payload["success"] is True
    assert payload["count"] == 3
    assert payload["outline"][0] == "def qNum L4-4"
    assert payload["outline"][1] == "theorem qThm L6-7"
    assert payload["outline"][2] == "lemma qLem L9-10"
    assert "declarations" not in payload


def test_lean_outline_tool_symbol_returns_region(tmp_path):
    target = _write_fixture(tmp_path)

    payload = json.loads(lean_tool.lean_outline_tool(str(target), symbol="qLem"))

    assert payload["success"] is True
    assert payload["symbol"] == "qLem"
    assert payload["declaration"]["name"] == "qLem"
    assert "rfl" in payload["declaration"]["text"]


def test_lean_outline_tool_missing_symbol_reports_error(tmp_path):
    target = _write_fixture(tmp_path)

    payload = json.loads(lean_tool.lean_outline_tool(str(target), symbol="ghost"))

    assert payload["success"] is False
    assert payload["declaration"] is None
    assert "ghost" in payload["error"]
