"""Check that counterexample assignments negate the complete original claim."""

from pathlib import Path

import pytest

from leanflow_cli.workflows.prover.negation import prepare_negation
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
