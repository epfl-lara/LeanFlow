"""Detect exact checked-helper signatures already present in current source."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _declaration_line_index_from_text
from leanflow_cli.workflows.planner_graph_identity import declaration_signature

_DECLARATION_NAME_RE = re.compile(
    r"\b(?P<kind>lemma|theorem)\s+(?:«[^»]+»|[^\s:({]+)",
    flags=re.DOTALL,
)
_SIMPLE_BINDER_NAME_RE = re.compile(r"(?:[^\W\d]|_)[\w']*", flags=re.UNICODE)


@dataclass(frozen=True)
class ExactSourceDuplicate:
    """Describe one proof-insensitive exact declaration duplicate."""

    existing_symbol: str
    reason: str = "exact_current_declaration_signature"


def source_name_collision(
    declaration: str,
    *,
    target_symbol: str,
    active_file: str,
) -> ExactSourceDuplicate | None:
    """Return a same-name declaration already present before the target.

    Checked helpers are insertion candidates, not replacement declarations. A
    same-name source declaration therefore makes insertion invalid regardless
    of whether its statement or proof matches the candidate.
    """
    try:
        source = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return None
    entries = tuple(dict(entry) for entry in _declaration_line_index_from_text(source))
    target_entry = _entry_for_symbol(entries, target_symbol)
    candidate_entries = tuple(
        dict(entry) for entry in _declaration_line_index_from_text(str(declaration or ""))
    )
    if target_entry is None or len(candidate_entries) != 1:
        return None
    candidate_name = str(candidate_entries[0].get("name", "") or "").strip()
    target_line = int(target_entry.get("line", 0) or 0)
    if not candidate_name or target_line <= 0:
        return None
    candidate_short = candidate_name.rsplit(".", 1)[-1]
    for entry in entries:
        entry_line = int(entry.get("line", 0) or 0)
        entry_name = str(entry.get("name", "") or "").strip()
        if entry_line <= 0 or entry_line >= target_line or not entry_name:
            continue
        if entry_name == candidate_name or entry_name.rsplit(".", 1)[-1] == candidate_short:
            return ExactSourceDuplicate(
                existing_symbol=entry_name,
                reason="same_name_current_source",
            )
    return None


def _entry_for_symbol(
    entries: Sequence[Mapping[str, Any]],
    symbol: str,
) -> Mapping[str, Any] | None:
    """Return one unambiguous declaration entry for an exact-or-short symbol."""
    wanted = str(symbol or "").strip()
    aliases = {wanted, wanted.rsplit(".", 1)[-1]}
    matches = [entry for entry in entries if str(entry.get("name", "") or "") in aliases]
    return matches[0] if len(matches) == 1 else None


def _name_independent_signature(declaration: str) -> str:
    """Return an exact declaration signature with only its name erased."""
    signature = declaration_signature(declaration)
    match = _DECLARATION_NAME_RE.search(signature)
    if match is None:
        return ""
    return (
        signature[: match.start()]
        + f"{match.group('kind')} $declaration"
        + signature[match.end() :]
    )


def _balanced_group_end(text: str, start: int) -> int | None:
    """Return the matching binder delimiter index, or ``None``."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    opening = text[start : start + 1]
    closing = pairs.get(opening)
    if not closing:
        return None
    stack = [closing]
    for index in range(start + 1, len(text)):
        char = text[index]
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                return index
    return None


def _top_level_colon(text: str) -> int | None:
    """Return the first colon outside nested delimiter groups."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack: list[str] = []
    for index, char in enumerate(text):
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == ":" and not stack:
            return index
    return None


def _alpha_normalized_signature(declaration: str) -> str:
    """Return a name-independent signature modulo leading binder names.

    Hypothesis names are not part of a Lean proposition. Research workers
    routinely rediscover an existing helper with names such as ``hθpos``
    instead of ``hθ0``; normalize those leading declaration binders before the
    source-coverage comparison while leaving the result expression intact.
    """
    signature = _name_independent_signature(declaration)
    if not signature:
        return ""
    name_match = re.search(r"\$(?:declaration)\b", signature)
    if name_match is None:
        return signature
    mapping: dict[str, str] = {}
    cursor = name_match.end()
    while cursor < len(signature):
        while cursor < len(signature) and signature[cursor].isspace():
            cursor += 1
        if cursor >= len(signature) or signature[cursor] not in "({[":
            break
        end = _balanced_group_end(signature, cursor)
        if end is None:
            break
        inner = signature[cursor + 1 : end]
        colon = _top_level_colon(inner)
        if colon is None:
            break
        names = inner[:colon].strip().split()
        if not names or any(_SIMPLE_BINDER_NAME_RE.fullmatch(name) is None for name in names):
            break
        for name in names:
            if name != "_" and name not in mapping:
                mapping[name] = f"_leanflow_b{len(mapping)}"
        cursor = end + 1
    normalized = signature
    for name in sorted(mapping, key=len, reverse=True):
        normalized = re.sub(
            rf"(?<![\w']){re.escape(name)}(?![\w'])",
            mapping[name],
            normalized,
        )
    return normalized


def exact_source_duplicate(
    declaration: str,
    *,
    target_symbol: str,
    active_file: str,
) -> ExactSourceDuplicate | None:
    """Return an exact same-signature declaration already before the target.

    This is deliberately a source deduplication rule, not kernel evidence.
    A proved graph status cannot authorize semantic or eventual-family
    subsumption because graph reconciliation may preserve that status after an
    import or earlier declaration changes. Any non-identical helper therefore
    fails open and remains eligible for the current parent recheck.
    """
    try:
        source = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return None
    entries = tuple(dict(entry) for entry in _declaration_line_index_from_text(source))
    target_entry = _entry_for_symbol(entries, target_symbol)
    candidate_entries = tuple(
        dict(entry) for entry in _declaration_line_index_from_text(str(declaration or ""))
    )
    if target_entry is None or len(candidate_entries) != 1:
        return None
    candidate_text = str(candidate_entries[0].get("text", "") or "").strip()
    candidate_name = str(candidate_entries[0].get("name", "") or "").strip()
    candidate_signature = _alpha_normalized_signature(candidate_text)
    target_line = int(target_entry.get("line", 0) or 0)
    if (
        candidate_text != str(declaration or "").strip()
        or not candidate_name
        or not candidate_signature
        or target_line <= 0
    ):
        return None
    for entry in entries:
        entry_line = int(entry.get("line", 0) or 0)
        entry_name = str(entry.get("name", "") or "").strip()
        entry_text = str(entry.get("text", "") or "").strip()
        if entry_line <= 0 or entry_line >= target_line or not entry_name or not entry_text:
            continue
        # An exact same-name declaration means insertion already happened and
        # the parent still owes its source/axiom verification and recovery
        # bookkeeping. Only a distinct declaration can make this candidate a
        # pure duplicate before that gate.
        if (
            entry_name == candidate_name
            or entry_name.rsplit(".", 1)[-1] == candidate_name.rsplit(".", 1)[-1]
        ):
            continue
        if _alpha_normalized_signature(entry_text) == candidate_signature:
            return ExactSourceDuplicate(existing_symbol=entry_name)
    return None
