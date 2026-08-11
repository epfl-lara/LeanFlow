"""Classify kernel-valid but non-reducing helpers for progress accounting.

Lean accepts a theorem whose assumptions contain the hard part of the active
goal.  Such a theorem remains a valid graph fact, but it is not proof progress
until those new higher-order obligations are explicit in the graph or the
assigned target actually uses the theorem.  This module performs that narrow,
source-backed classification without changing source acceptance or kernel
status.  The same gate recognizes one narrow surface pathology: a
same-premise existential helper whose visible witness and atomic burden is no
smaller than its parent's. Such transformed certificate wrappers remain valid
facts but cannot reset a research campaign merely for moving the hard
existential.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    LEAN_DECLARATION_PREAMBLE_RE,
    _declaration_line_index_from_text,
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
)
from leanflow_cli.workflows import mechanism_progress, plan_state
from tools.utilities import decomposer_admission

_OPENERS = {"(": ")", "{": "}", "[": "]"}
_IDENTIFIER_RE = re.compile(r"[A-Za-z_«][A-Za-z0-9_'.«»-]*")
_VISIBLE_PROP_RE = re.compile(
    r"(?:∃|∧|∨|¬|≠|≤|≥|∈|∉|(?<![:<>=!])=(?!=)|"
    r"(?<![-<])<(?![=-])|(?<![-=>])>(?![=])|\b(?:False|True|Prop)\b)"
)


@dataclass(frozen=True)
class ConditionalHelperAssessment:
    """Describe why one proved structural helper is not campaign progress."""

    node_id: str
    node_name: str
    parent_ids: tuple[str, ...]
    obligation_types: tuple[str, ...]
    represented_obligation_types: tuple[str, ...]
    unresolved_obligation_types: tuple[str, ...]
    target_integrated: bool = False
    obligation_reduction: decomposer_admission.ObligationReductionAssessment | None = None

    @property
    def reason_code(self) -> str:
        """Return the stable reason behind this deferred progress record."""
        reduction = self.obligation_reduction
        if reduction is not None and reduction.nonreducing_wrapper:
            return reduction.reason_code
        return "unrepresented_conditional_obligation"

    @property
    def deferred(self) -> bool:
        """Return whether campaign accounting must defer this helper."""
        reduction = self.obligation_reduction
        nonreducing = bool(reduction is not None and reduction.nonreducing_wrapper)
        return bool(
            not self.target_integrated and (self.unresolved_obligation_types or nonreducing)
        )


def _canonical_file(value: Any) -> str:
    """Return a stable source-file identity for graph/source comparisons."""
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(text))))


def _compact_type(value: str) -> str:
    """Return a comment-free whitespace-normalized Lean type."""
    text = " ".join(_strip_lean_comments_and_strings(str(value or "")).split()).strip()
    while text.startswith("(") and text.endswith(")"):
        end = _balanced_group_end(text, 0)
        if end != len(text):
            break
        text = text[1:-1].strip()
    return text.removeprefix(":").strip()


def _balanced_group_end(text: str, start: int) -> int | None:
    """Return the exclusive end of one balanced Lean delimiter group."""
    if start >= len(text) or text[start] not in _OPENERS:
        return None
    stack = [_OPENERS[text[start]]]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char in _OPENERS:
            stack.append(_OPENERS[char])
        elif char == stack[-1]:
            stack.pop()
            if not stack:
                return index + 1
        index += 1
    return None


def _top_level_colon(text: str) -> int:
    """Return the first binder-level colon, excluding nested type syntax."""
    stack: list[str] = []
    for index, char in enumerate(text):
        if char in _OPENERS:
            stack.append(_OPENERS[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == ":" and not stack:
            return index
    return -1


def _explicit_binder_types(declaration: str) -> tuple[str, ...]:
    """Return explicit declaration binder types in source order."""
    signature = str(declaration or "")
    marker = _find_assignment_marker_for_statement(signature)
    if marker >= 0:
        signature = signature[:marker]
    signature = _strip_lean_comments_and_strings(signature)
    match = re.match(LEAN_DECLARATION_PREAMBLE_RE, signature)
    if match is None:
        return ()
    index = match.end()
    result: list[str] = []
    while True:
        while index < len(signature) and signature[index].isspace():
            index += 1
        if index >= len(signature) or signature[index] not in _OPENERS:
            break
        end = _balanced_group_end(signature, index)
        if end is None:
            break
        binder = signature[index + 1 : end - 1].strip()
        colon = _top_level_colon(binder)
        if colon >= 0:
            binder_type = _compact_type(binder[colon + 1 :])
            if binder_type:
                result.append(binder_type)
        index = end
    return tuple(result)


def _declaration_result_type(declaration: str) -> str:
    """Return the explicit result type after declaration binders."""
    signature = str(declaration or "")
    marker = _find_assignment_marker_for_statement(signature)
    if marker >= 0:
        signature = signature[:marker]
    signature = _strip_lean_comments_and_strings(signature)
    match = re.match(LEAN_DECLARATION_PREAMBLE_RE, signature)
    if match is None:
        return ""
    index = match.end()
    while True:
        while index < len(signature) and signature[index].isspace():
            index += 1
        if index >= len(signature) or signature[index] not in _OPENERS:
            break
        end = _balanced_group_end(signature, index)
        if end is None:
            return ""
        index = end
    remainder = signature[index:].strip()
    return _compact_type(remainder) if remainder.startswith(":") else ""


def _top_level_arrow_parts(value: str) -> tuple[str, ...]:
    """Split a type at top-level Lean arrows."""
    text = str(value or "")
    stack: list[str] = []
    parts: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in _OPENERS:
            stack.append(_OPENERS[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == "→" and not stack:
            parts.append(text[start:index].strip())
            start = index + 1
        elif char == "-" and index + 1 < len(text) and text[index + 1] == ">" and not stack:
            parts.append(text[start:index].strip())
            start = index + 2
            index += 1
        index += 1
    if parts:
        parts.append(text[start:].strip())
    return tuple(parts)


def _is_higher_order_proof_premise(binder_type: str) -> bool:
    """Return whether syntax narrowly exposes a theorem-valued premise.

    Explicit ``∀`` types are higher-order obligations.  Arrow-valued data such
    as ``Nat → Nat`` is deliberately excluded unless the arrow chain visibly
    contains proposition syntax.  This is an accounting guard, not a Lean
    rejection gate; opaque predicate applications remain fail-open.
    """
    compact = _compact_type(binder_type)
    if compact.startswith("∀") or compact.startswith("forall "):
        return True
    arrow_parts = _top_level_arrow_parts(compact)
    return bool(arrow_parts and _VISIBLE_PROP_RE.search(compact))


def _contains_target_result(binder_type: str, target_result: str) -> bool:
    """Return whether a helper premise syntactically contains the target result."""
    candidate = _compact_type(binder_type)
    target = _compact_type(target_result)
    if not candidate or not target:
        return False
    if candidate == target:
        return True
    return bool(re.search(rf"(?<![A-Za-z0-9_'.]){re.escape(target)}(?![A-Za-z0-9_'.])", candidate))


def _target_assumption_obligations(
    helper_declaration: str,
    target_declaration: str,
) -> tuple[str, ...]:
    """Return helper premises that contain the assigned target's result."""
    target_result = _declaration_result_type(target_declaration)
    target_premises = {_compact_type(value) for value in _explicit_binder_types(target_declaration)}
    return tuple(
        binder_type
        for binder_type in _explicit_binder_types(helper_declaration)
        if _compact_type(binder_type) not in target_premises
        and _contains_target_result(binder_type, target_result)
    )


def checked_code_target_assumption_obligations(
    checked_code: Sequence[str],
    *,
    target_symbol: str,
    active_file: str,
) -> tuple[str, ...]:
    """Return target-containing premises from checked but not-yet-integrated code.

    The source target is parent-owned authority. Missing or ambiguous source
    identity fails open, preserving the checked helper as ordinary evidence.
    """
    try:
        source = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return ()
    target = str(target_symbol or "").strip()
    aliases = {target, target.rsplit(".", 1)[-1]}
    matches = [
        entry
        for entry in _declaration_line_index_from_text(source)
        if str(entry.get("name", "") or "") in aliases
    ]
    if len(matches) != 1:
        return ()
    target_declaration = str(matches[0].get("text", "") or "")
    obligations: list[str] = []
    for declaration in checked_code:
        obligations.extend(_target_assumption_obligations(declaration, target_declaration))
    return tuple(dict.fromkeys(value for value in obligations if value))


def _entry_for_name(entries: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    """Return one exact-or-short declaration entry, failing closed on ambiguity."""
    wanted = str(name or "").strip()
    aliases = {wanted, wanted.split(".")[-1]}
    matches = [entry for entry in entries if str(entry.get("name", "") or "") in aliases]
    return matches[0] if len(matches) == 1 else None


def _target_uses_helper(target_declaration: str, helper_name: str) -> bool:
    """Return whether the exact target proof body references one helper."""
    marker = _find_assignment_marker_for_statement(target_declaration)
    if marker < 0:
        return False
    identifiers = set(
        _IDENTIFIER_RE.findall(_strip_lean_comments_and_strings(target_declaration[marker + 2 :]))
    )
    helper = str(helper_name or "").strip()
    return bool(helper and ({helper, helper.split(".")[-1]} & identifiers))


def _represented_obligations(
    blueprint: plan_state.Blueprint,
    *,
    helper_id: str,
    parent_ids: Sequence[str],
    obligation_types: Sequence[str],
) -> set[str]:
    """Return exact higher-order obligations represented by another graph node.

    An exact proved fact is globally usable.  An unresolved node counts as a
    representation only when an explicit dependency/split edge connects it to
    the helper or its assigned parent; an unrelated same-shaped conjecture is
    not enough to legitimize the bridge.
    """
    wanted = {_compact_type(value) for value in obligation_types if _compact_type(value)}
    if not wanted:
        return set()
    related_ids = {helper_id, *parent_ids}
    connected: set[str] = set()
    for edge in blueprint.edges:
        if edge.source in related_ids:
            connected.add(edge.target)
        if edge.target in related_ids:
            connected.add(edge.source)
    represented: set[str] = set()
    for node in blueprint.nodes:
        if node.id in related_ids:
            continue
        statement = _compact_type(node.statement)
        if statement not in wanted:
            continue
        if node.status == "proved" or node.id in connected:
            represented.add(statement)
    return represented


def assess_conditional_helpers(
    blueprint: plan_state.Blueprint,
    node_ids: Iterable[str] | None = None,
) -> dict[str, ConditionalHelperAssessment]:
    """Return proved structural helpers whose unresolved burden defers progress.

    Source parsing fails open: missing or ambiguous declarations never suppress
    graph progress.  Only explicit structural children with a still-open graph
    parent are considered, so unrelated theorem-valued APIs are untouched.
    """
    candidates = (
        {str(node_id or "") for node_id in node_ids if str(node_id or "")}
        if node_ids is not None
        else {node.id for node in blueprint.nodes if node.status == "proved"}
    )
    source_cache: dict[str, tuple[dict[str, Any], ...]] = {}
    assessments: dict[str, ConditionalHelperAssessment] = {}
    for node_id in sorted(candidates):
        node = blueprint.node_by_id(node_id)
        if node is None or node.status != "proved" or not node.file or not node.name:
            continue
        parent_ids = mechanism_progress.parent_ids_for_node(blueprint, node_id)
        open_parent_ids = tuple(
            parent_id
            for parent_id in parent_ids
            if (parent := blueprint.node_by_id(parent_id)) is not None
            and parent.status in mechanism_progress.OPEN_PARENT_STATUSES
        )
        if not open_parent_ids:
            continue
        source_key = _canonical_file(node.file)
        if source_key not in source_cache:
            try:
                source = Path(node.file).read_text(encoding="utf-8")
            except OSError:
                source_cache[source_key] = ()
            else:
                source_cache[source_key] = tuple(
                    dict(entry) for entry in _declaration_line_index_from_text(source)
                )
        entries = source_cache[source_key]
        helper_entry = _entry_for_name(entries, node.name)
        if helper_entry is None:
            continue
        helper_types = _explicit_binder_types(str(helper_entry.get("text", "") or ""))
        parent_premise_types: set[str] = set()
        parent_result_types: set[str] = set()
        target_integrated = False
        obligation_reduction: decomposer_admission.ObligationReductionAssessment | None = None
        for parent_id in open_parent_ids:
            parent = blueprint.node_by_id(parent_id)
            if parent is None or _canonical_file(parent.file) != source_key:
                continue
            parent_entry = _entry_for_name(entries, parent.name)
            if parent_entry is None:
                continue
            parent_declaration = str(parent_entry.get("text", "") or "")
            parent_premise_types.update(_explicit_binder_types(parent_declaration))
            parent_result = _declaration_result_type(parent_declaration)
            if parent_result:
                parent_result_types.add(parent_result)
            target_integrated = target_integrated or _target_uses_helper(
                parent_declaration,
                node.name,
            )
            reduction = decomposer_admission.assess_obligation_reduction(
                parent_declaration,
                str(helper_entry.get("text", "") or ""),
            )
            if reduction.nonreducing_wrapper:
                obligation_reduction = reduction
        obligations = tuple(
            dict.fromkeys(
                binder_type
                for binder_type in helper_types
                if binder_type not in parent_premise_types
                and (
                    _is_higher_order_proof_premise(binder_type)
                    or any(
                        _contains_target_result(binder_type, target_result)
                        for target_result in parent_result_types
                    )
                )
            )
        )
        if not obligations and obligation_reduction is None:
            continue
        represented = _represented_obligations(
            blueprint,
            helper_id=node_id,
            parent_ids=open_parent_ids,
            obligation_types=obligations,
        )
        unresolved = tuple(
            value for value in obligations if _compact_type(value) not in represented
        )
        assessment = ConditionalHelperAssessment(
            node_id=node_id,
            node_name=node.name,
            parent_ids=open_parent_ids,
            obligation_types=obligations,
            represented_obligation_types=tuple(
                value for value in obligations if _compact_type(value) in represented
            ),
            unresolved_obligation_types=unresolved,
            target_integrated=target_integrated,
            obligation_reduction=obligation_reduction,
        )
        if assessment.deferred:
            assessments[node_id] = assessment
    return assessments


def deferred_helper_names(
    blueprint: plan_state.Blueprint,
    helper_names: Iterable[str],
) -> dict[str, ConditionalHelperAssessment]:
    """Return deferred assessments keyed by requested helper name."""
    requested = {str(name or "").strip() for name in helper_names if str(name or "").strip()}
    ids = {
        node.id
        for node in blueprint.nodes
        if node.name in requested or node.name.split(".")[-1] in requested
    }
    by_id = assess_conditional_helpers(blueprint, ids)
    return {
        assessment.node_name: assessment for assessment in by_id.values() if assessment.node_name
    }
