"""Check that counterexample assignments negate the complete original claim."""

from pathlib import Path

import pytest

from leanflow_cli.workflows.prover.negation import (
    _strip_dangling_declaration_docs,
    prepare_negation,
)
from leanflow_cli.workflows.prover.source import discover


def test_negation_quantifies_explicit_hypotheses_inside_not(tmp_path: Path) -> None:
    path = tmp_path / "Main.lean"
    path.write_text("theorem goal (n : Nat) (h : n = 0) : n = 1 := by sorry\n")
    dag, _ = discover(tmp_path, [path], fill_definitions=False)
    task = prepare_negation(tmp_path, dag.nodes[0])
    assert task is not None
    assert "¬ (∀ (n : Nat) (h : n = 0), n = 1)" in task.node.statement
    assert "autoImplicit false" in task.source
    assert task.node.holes == [0]
    assert path.read_text().startswith("theorem goal")


@pytest.mark.parametrize("prefix", ["variable (x : Empty)\n", "variable {n : Nat}\ninclude n\n"])
def test_negation_declines_ambient_variables(tmp_path: Path, prefix: str) -> None:
    path = tmp_path / "Main.lean"
    path.write_text(prefix + "theorem goal : True := by sorry\n")
    dag, _ = discover(tmp_path, [path], fill_definitions=False)
    assert prepare_negation(tmp_path, dag.nodes[0]) is None


def test_negation_ignores_comments_and_tracks_prior_holes(tmp_path: Path) -> None:
    path = tmp_path / "Main.lean"
    path.write_text(
        "-- variable x\ntheorem prior : True := by sorry\ntheorem goal : False := by sorry\n"
    )
    dag, _ = discover(tmp_path, [path], fill_definitions=False)
    task = prepare_negation(tmp_path, dag.nodes[1])
    assert task is not None
    assert task.node.holes == [1]
    assert "theorem prior" in task.source


def test_definition_has_no_negation_assignment(tmp_path: Path) -> None:
    path = tmp_path / "Main.lean"
    path.write_text("def value : Nat := sorry\n")
    dag, _ = discover(tmp_path, [path], fill_definitions=True)
    assert prepare_negation(tmp_path, dag.nodes[0]) is None


# --- dangling declaration preamble ------------------------------------------
#
# declaration_region starts a declaration at its `theorem` keyword, so its
# leading doc comment / attributes stay in the prefix. Replacing the
# declaration would leave them in front of the negation's own
# `set_option autoImplicit false in` wrapper, which Lean rejects with a parse
# error -- misread as an inconclusive refutation. They must be dropped, while
# `... in` wrappers (which legally precede set_option and may carry notation
# scope) are kept.


def test_strip_drops_a_doc_comment_with_a_nested_block_comment() -> None:
    # The old rfind("/--") + `"-/" not in ...` heuristic refused to strip a doc
    # comment whose body contained a nested `-/`; the nesting-aware scan does.
    prefix = "import X\n\n/-- Uses `/- inner -/` in prose. -/\n"
    assert _strip_dangling_declaration_docs(prefix) == "import X\n\n"


def test_strip_drops_a_doc_comment_sitting_before_a_wrapper() -> None:
    prefix = "/-- Doc. -/\nset_option maxHeartbeats 400000 in\n"
    out = _strip_dangling_declaration_docs(prefix)
    assert "Doc." not in out  # the doc comment is gone
    assert "set_option maxHeartbeats 400000 in" in out  # the wrapper is kept


def test_strip_drops_attribute_and_modifier_lines_but_keeps_real_code() -> None:
    prefix = "theorem prior : True := by trivial\n\n@[simp]\nprivate\n"
    out = _strip_dangling_declaration_docs(prefix)
    assert "@[simp]" not in out and "private" not in out
    assert "theorem prior : True := by trivial" in out  # stops at real code


def test_strip_keeps_a_module_doc_and_a_bare_wrapper() -> None:
    # `/-!` module docs and `... in` wrappers legally precede set_option.
    for prefix in ("/-! Module. -/\n", "open scoped Classical in\n"):
        assert _strip_dangling_declaration_docs(prefix) == prefix


def test_negation_drops_a_dangling_doc_comment_from_the_scratch(tmp_path: Path) -> None:
    path = tmp_path / "Main.lean"
    path.write_text("/-- The main claim. -/\ntheorem goal : True := by sorry\n")
    dag, _ = discover(tmp_path, [path], fill_definitions=False)
    task = prepare_negation(tmp_path, dag.nodes[0])
    assert task is not None
    # The doc comment no longer precedes the injected `set_option ... in`.
    assert "The main claim." not in task.source
    assert "set_option autoImplicit false in" in task.source
