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


@dataclass(frozen=True)
class NegationContext:
    """One declaration's file split around it, plus the closed proposition it states."""

    prefix: str
    suffix: str
    goal: NegationGoal


# A line consisting only of attributes and/or modifiers (nothing else).
_ATTR_OR_MODIFIER_TOKEN = (
    r"(?:@\[[^\]]*\]|@[A-Za-z0-9_.]+"
    r"|private|protected|noncomputable|unsafe|partial|nonrec|scoped|local)"
)
_ATTR_OR_MODIFIER_LINE_RE = re.compile(
    rf"{_ATTR_OR_MODIFIER_TOKEN}(?:\s+{_ATTR_OR_MODIFIER_TOKEN})*"
)
# A `<scoped-command> ... in` wrapper on a single line (open/set_option/... in).
_WRAPPER_LINE_RE = re.compile(
    r"(?:set_option|variable|include|omit|attribute|open(?:\s+scoped)?)\b.*\bin"
)


def _block_comment_open(text: str) -> int | None:
    """Index of the opener that matches a trailing ``-/`` in ``text``, nesting-aware.

    Scans right-to-left counting ``-/``/``/-`` so a doc comment whose body
    contains a nested block comment is matched as one unit. Returns None on an
    unbalanced tail (leave the prefix untouched rather than guess).
    """
    depth = 0
    index = len(text)
    while index >= 2:
        pair = text[index - 2 : index]
        if pair == "-/":
            depth += 1
            index -= 2
        elif pair == "/-":
            depth -= 1
            index -= 2
            if depth == 0:
                return index
        else:
            index -= 1
    return None


def _strip_dangling_declaration_docs(prefix: str) -> str:
    """Drop the doc comments and attributes that belonged to the replaced declaration.

    ``declaration_region`` starts a declaration at its ``theorem``/``lemma``
    keyword, so its leading doc comment, attributes and modifiers stay in
    ``prefix``. Left there, they end up in front of the replacement's own
    ``set_option autoImplicit false in`` wrapper, which Lean rejects outright
    ("unexpected token 'set_option'") -- the scratch fails to parse for a purely
    syntactic reason and the refutation is misread as inconclusive.

    Walk the trailing preamble right-to-left, deleting only what cannot precede
    a ``set_option ... in`` command -- ``/-- -/`` doc comments and
    attribute/modifier lines -- while scanning *past* (and keeping) the tokens
    that legally can: ``... in`` command wrappers (they may also carry notation
    scope the verbatim proposition needs), plain/`/-!` comments, and whitespace.
    Stop at the first token that is none of these (real code, a namespace, a
    standalone command). The negated proposition is fixed independently by
    ``build_negation_goal``, so an over-eager strip can only make the scratch
    fail to elaborate (inconclusive) -- never certify a different statement.
    """
    delete: list[tuple[int, int]] = []
    end = len(prefix)
    while True:
        # `right` is this token's right edge including the whitespace that
        # follows it, so dropping a token also removes the blank line it leaves.
        right = end
        while end > 0 and prefix[end - 1].isspace():
            end -= 1
        if end == 0:
            break
        if prefix[end - 2 : end] == "-/":
            opener = _block_comment_open(prefix[:end])
            if opener is None:
                break
            if prefix.startswith("/--", opener):
                delete.append((opener, right))  # a declaration doc comment: drop it
            end = opener  # plain and /-! comments are kept, but scan on past them
            continue
        line_start = prefix.rfind("\n", 0, end) + 1
        line = prefix[line_start:end].strip()
        if line.startswith("--"):
            end = line_start  # a line comment is legal here; keep it, scan past
            continue
        if _ATTR_OR_MODIFIER_LINE_RE.fullmatch(line):
            delete.append((line_start, right))
            end = line_start
            continue
        if _WRAPPER_LINE_RE.fullmatch(line):
            end = line_start  # a `... in` wrapper is legal here; keep it, scan past
            continue
        break
    result = prefix
    for start, stop in sorted(delete, reverse=True):
        result = result[:start] + result[stop:]
    return result


def negation_context(root: Path, node: Node) -> NegationContext | None:
    """Locate a declaration and its closed proposition, declining ambiguous contexts.

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
    # A doc comment / attributes belong to the declaration they precede. When
    # that declaration is replaced they dangle in front of the replacement's own
    # "set_option ... in" wrapper, which Lean rejects; drop them with it.
    prefix = _strip_dangling_declaration_docs(source[:start])
    if re.search(r"\b(?:variable|variables|include|omit)\b", lean_code_mask(prefix)):
        return None
    goal = build_negation_goal(str(path), node.name, cwd=str(root))
    if not isinstance(goal, NegationGoal):
        return None
    return NegationContext(prefix=prefix, suffix=source[end:], goal=goal)


def prepare_negation(root: Path, node: Node) -> NegationTask | None:
    """Negate an explicitly bound proposition, declining ambiguous ambient contexts.

    Quantify the original binders *inside* the negation. Keeping hypotheses as
    outer binders could certify only a vacuous conditional counterexample.
    """
    context = negation_context(root, node)
    if context is None:
        return None
    name = "leanflow_negation_" + digest(node.id + node.statement)[:16]
    statement = f"theorem {name} : ¬ ({context.goal.prop})"
    declaration = "set_option autoImplicit false in\n" + statement + " := by\n  sorry\n"
    scratch = context.prefix + declaration + context.suffix
    hole = len(sorry_spans(context.prefix))
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
