"""Reject command injection through a literal sorry while retaining ordinary proofs."""

import pytest

from leanflow_cli.workflows.prover.source import SourceDocument


@pytest.mark.parametrize(
    "replacement",
    [
        "trivial\ntheorem injected : True := by trivial",
        "trivial\nattribute [local simp] Nat.add_comm",
        "trivial\naxiom illicit : False",
        "trivial\nnamespace Changed",
        "trivial\nend Hidden",
        'trivial\nmacro "changed" : tactic => `(tactic|trivial)',
        "trivial\n#eval IO.println 0",
        "trivial\nset_option autoImplicit true",
    ],
)
def test_literal_hole_cannot_introduce_a_command(replacement: str) -> None:
    document = SourceDocument("Main.lean", "theorem goal : True := by sorry\n")
    with pytest.raises(ValueError, match="command"):
        document.render({0: replacement})


@pytest.mark.parametrize(
    "replacement",
    [
        "\n  have h : True := by trivial\n  exact h",
        "\n  let xs := #[1, 2]\n  exact True.intro",
        "\n  set_option maxRecDepth 1000 in\n  trivial",
        'exact (by have label := "theorem injected"; trivial)',
        "exact Foo.«theorem» -- attribute ignored\n",
        "trivial /- axiom ignored /- theorem also ignored -/ -/",
    ],
)
def test_literal_hole_retains_proof_expressions(replacement: str) -> None:
    document = SourceDocument("Main.lean", "theorem goal : True := by sorry\n")
    assert document.render({0: replacement}).endswith(replacement + "\n")
