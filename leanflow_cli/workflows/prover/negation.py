"""Construct conservative, closed negation assignments for independent Lean checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from leanflow_cli.lean.lean_declarations import declaration_region
from leanflow_cli.lean.negation_probe import NegationGoal, build_negation_goal
from leanflow_cli.workflows.prover.models import Node, digest
from leanflow_cli.workflows.prover.source import lean_code_mask, read_source, sorry_spans


@dataclass(frozen=True)
class NegationTask:
    """Carry an alternate scratch declaration; never authorize a canonical source edit."""

    node: Node
    source: str


def prepare_negation(root: Path, node: Node) -> NegationTask | None:
    """Negate an explicitly bound proposition, declining ambiguous ambient contexts.

    Quantify the original binders *inside* the negation. Keeping hypotheses as
    outer binders could certify only a vacuous conditional counterexample.
    Section variables and include/omit directives require elaborated context
    capture, which this narrow adapter deliberately refuses to approximate.
    """
    if node.kind not in {"theorem", "lemma"}:
        return None
    path = root / node.file
    region = declaration_region(path, node.name)
    if region is None:
        return None
    source = read_source(path)
    lines = source.splitlines(keepends=True)
    start = sum(map(len, lines[: int(region["line"]) - 1]))
    end = sum(map(len, lines[: int(region["end_line"])]))
    prefix = source[:start]
    # A doc comment belongs to the declaration it precedes. Replacing that
    # declaration leaves the comment dangling in front of "set_option ... in",
    # which Lean rejects outright ("unexpected token 'set_option'"), so the
    # negation would fail to parse for a purely syntactic reason and be
    # misread as an inconclusive refutation. Drop it with its declaration.
    stripped = prefix.rstrip()
    if stripped.endswith("-/"):
        opener = stripped.rfind("/--")
        if opener != -1 and "-/" not in stripped[opener + 3 : -2]:
            prefix = prefix[:opener]
    if re.search(r"\b(?:variable|variables|include|omit)\b", lean_code_mask(prefix)):
        return None
    goal = build_negation_goal(str(path), node.name, cwd=str(root))
    if not isinstance(goal, NegationGoal):
        return None
    name = "leanflow_negation_" + digest(node.id + node.statement)[:16]
    statement = f"theorem {name} : ¬ ({goal.prop})"
    declaration = "set_option autoImplicit false in\n" + statement + " := by\n  sorry\n"
    scratch = prefix + declaration + source[end:]
    hole = len(sorry_spans(prefix))
    alternate = Node(
        id="negation_" + node.id,
        name=name,
        statement=statement,
        file=node.file,
        module=node.module,
        informal_justification="Prove the negation of the entire universally quantified original claim.",
        dependencies=list(node.dependencies),
        holes=[hole],
    )
    return NegationTask(node=alternate, source=scratch)
