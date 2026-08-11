"""Suppress finite-instance research dominated by an exact universal obstruction."""

from __future__ import annotations

import re
from dataclasses import dataclass

from leanflow_cli.workflows import orchestrator
from leanflow_cli.workflows.plan_state import Blueprint, node_id_for
from leanflow_cli.workflows.planner_graph_identity import declaration_signature

_DECLARATION_RE = re.compile(
    r"^(?:(?:@\[[^\]]*\]|@[A-Za-z0-9_.]+|private|protected|noncomputable|unsafe|"
    r"partial|local)\s+)*(?:theorem|lemma)\s+(?:«[^»]+»|[^\s:({]+)(?P<core>.*)$",
    re.DOTALL,
)
_FINITE_EMPIRICAL_ROUTE_KEYS = frozenset(
    {
        "small-case-invariant",
        "boundary-counterexample-probe",
        "evidence-to-helper",
    }
)
_FINITE_OBJECTIVE_RE = re.compile(
    r"\b(?:boundary cases?|counterexamples?|finite(?:-instance)?|instances?|small cases?|"
    r"witness(?:es)?)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class UniversalObstruction:
    """Identify one parent-verified exact pointwise or closed target negation."""

    node_id: str
    name: str
    statement: str


def _top_level_result_colon(core: str) -> int:
    """Return the declaration result colon outside delimiters and strings."""
    closers = {")": "(", "]": "[", "}": "{", "⦄": "⦃"}
    openers = set(closers.values())
    stack: list[str] = []
    in_string = False
    escaped = False
    for index, character in enumerate(core):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            continue
        if character in openers:
            stack.append(character)
            continue
        if character in closers:
            if stack and stack[-1] == closers[character]:
                stack.pop()
            continue
        if character == ":" and not stack and not core.startswith(":=", index):
            return index
    return -1


def _signature_parts(statement: str) -> tuple[str, str] | None:
    """Return normalized declaration binders and result type."""
    match = _DECLARATION_RE.fullmatch(declaration_signature(statement))
    if match is None:
        return None
    core = str(match.group("core") or "").strip()
    result_colon = _top_level_result_colon(core)
    if result_colon < 0:
        return None
    binders = " ".join(core[:result_colon].split())
    result = " ".join(core[result_colon + 1 :].split())
    if not result:
        return None
    return binders, result


def _matching_outer_parenthesis(text: str) -> bool:
    """Return whether one opening parenthesis encloses the complete expression."""
    if not text.startswith("(") or not text.endswith(")"):
        return False
    depth = 0
    in_string = False
    escaped = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return False
            if depth < 0:
                return False
    return depth == 0 and not in_string


def _strip_outer_parentheses(text: str) -> str:
    """Remove only balanced parentheses enclosing a complete expression."""
    value = " ".join(str(text or "").split())
    while _matching_outer_parenthesis(value):
        value = value[1:-1].strip()
    return value


def _negated_body(result: str) -> str:
    """Return the body of one explicit Lean negation, or an empty string."""
    normalized = _strip_outer_parentheses(result)
    if normalized.startswith("¬"):
        return _strip_outer_parentheses(normalized[1:].strip())
    match = re.match(r"^Not\b(?P<body>.+)$", normalized, flags=re.DOTALL)
    if match is None:
        return ""
    return _strip_outer_parentheses(str(match.group("body") or ""))


def is_exact_universal_obstruction(target_statement: str, helper_statement: str) -> bool:
    """Return whether ``helper_statement`` is the exact target's universal negation.

    This intentionally accepts only an exact syntax-preserving relation: either
    the helper repeats the target binders and proves the pointwise negation, or
    it proves the closed negation of the target's complete quantified
    proposition. Alpha-renamed or logically equivalent results fail open.
    """
    target = _signature_parts(target_statement)
    helper = _signature_parts(helper_statement)
    if target is None or helper is None:
        return False
    target_binders, target_result = target
    helper_binders, helper_result = helper
    negated = _negated_body(helper_result)
    if not negated:
        return False
    normalized_target_result = _strip_outer_parentheses(target_result)
    if helper_binders == target_binders and negated == normalized_target_result:
        return True
    if helper_binders or not target_binders:
        return False
    closed_target = _strip_outer_parentheses(f"∀ {target_binders}, {target_result}")
    return negated == closed_target


def exact_target_universal_obstruction(
    blueprint: Blueprint | None,
    *,
    target_symbol: str,
    active_file: str,
) -> UniversalObstruction | None:
    """Return parent-kernel evidence that universally refutes the exact target.

    The orchestrator's existing graph gate remains the authority: the helper
    must be a proved same-file node on an exact-target evidence edge. This
    policy adds only the stronger syntactic dominance test needed to retire
    finite-instance research; it grants no negation-promotion verdict.
    """
    if blueprint is None or not target_symbol or not active_file:
        return None
    target = blueprint.node_by_id(node_id_for(target_symbol, active_file))
    if target is None or not target.statement.strip():
        return None
    evidence = orchestrator.verified_counterexample_evidence(
        blueprint,
        (),
        target_symbol=target_symbol,
        active_file=active_file,
    )
    for item in evidence:
        node_id = str(item.get("node_id", "") or "")
        node = blueprint.node_by_id(node_id)
        if node is None or not is_exact_universal_obstruction(target.statement, node.statement):
            continue
        return UniversalObstruction(
            node_id=node.id,
            name=node.name,
            statement=node.statement,
        )
    return None


def dominated_finite_instance_objective(
    *,
    archetype: str,
    route_key: str,
    objective: str,
) -> bool:
    """Return whether a worker objective seeks evidence subsumed by universality."""
    if str(archetype or "").strip() != "empirical":
        return False
    normalized_route = str(route_key or "").strip()
    if normalized_route in _FINITE_EMPIRICAL_ROUTE_KEYS:
        return True
    return bool(_FINITE_OBJECTIVE_RE.search(str(objective or "")))
