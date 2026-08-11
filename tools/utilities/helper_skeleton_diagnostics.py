"""Classify batch-only helper-skeleton elaboration failures."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SAFE_LEAN_IDENTIFIER = r"(?:_root_\.)?[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*"
_UNKNOWN_IDENTIFIER_ERROR = re.compile(
    rf"\s*unknown identifier\s+[`'\"](?P<identifier>{_SAFE_LEAN_IDENTIFIER})[`'\"]\s*",
    flags=re.IGNORECASE,
)


def _diagnostic_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return structured diagnostics without trusting free-form tool output."""
    items: list[Mapping[str, Any]] = []
    for key in ("messages", "items", "diagnostics"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            nested = value.get("items") or value.get("messages")
            if isinstance(nested, list):
                items.extend(item for item in nested if isinstance(item, Mapping))
    return items


def _unknown_identifier_errors(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return identifiers only when every structured error is an unknown identifier."""
    identifiers: set[str] = set()
    error_count = 0
    for item in _diagnostic_items(payload):
        if str(item.get("severity", "") or "").strip().lower() != "error":
            continue
        error_count += 1
        message = str(item.get("message", "") or item.get("text", "") or "").strip()
        match = _UNKNOWN_IDENTIFIER_ERROR.fullmatch(message)
        if match is None:
            return ()
        identifiers.add(match.group("identifier"))
    return tuple(sorted(identifiers)) if error_count else ()


def _lean_code_only(source: str) -> str:
    """Mask comments and strings so incidental identifier text cannot prove a dependency."""
    result: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(source):
        current = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if block_depth:
            if current == "/" and following == "-":
                block_depth += 1
                result.extend("  ")
                index += 2
                continue
            if current == "-" and following == "/":
                block_depth -= 1
                result.extend("  ")
                index += 2
                continue
            result.append("\n" if current == "\n" else " ")
            index += 1
            continue
        if in_string:
            result.append("\n" if current == "\n" else " ")
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == '"':
                in_string = False
            index += 1
            continue
        if current == "-" and following == "-":
            line_end = source.find("\n", index + 2)
            if line_end < 0:
                result.extend(" " * (len(source) - index))
                break
            result.extend(" " * (line_end - index))
            index = line_end
            continue
        if current == "/" and following == "-":
            block_depth = 1
            result.extend("  ")
            index += 2
            continue
        if current == "'" and (
            index == 0 or not (source[index - 1].isalnum() or source[index - 1] in "_'")
        ):
            char_end = index + 1
            char_escaped = False
            while char_end < len(source) and source[char_end] != "\n":
                candidate = source[char_end]
                if char_escaped:
                    char_escaped = False
                elif candidate == "\\":
                    char_escaped = True
                elif candidate == "'":
                    break
                char_end += 1
            if char_end < len(source) and source[char_end] == "'":
                result.extend(" " * (char_end - index + 1))
                index = char_end + 1
                continue
        if current == '"':
            in_string = True
            result.append(" ")
            index += 1
            continue
        result.append(current)
        index += 1
    return "".join(result)


def _identifier_aliases(identifier: str) -> set[str]:
    """Return conservative qualified and unqualified aliases for one Lean name."""
    normalized = str(identifier or "").strip().removeprefix("_root_.")
    if not normalized:
        return set()
    return {normalized, normalized.rsplit(".", 1)[-1]}


def _references_identifier(source: str, identifier: str) -> bool:
    """Return whether Lean code directly references an exact identifier token."""
    normalized = str(identifier or "").strip()
    if not normalized:
        return False
    return (
        re.search(
            rf"(?<![\w.]){re.escape(normalized)}(?![\w.])",
            source,
        )
        is not None
    )


def _signature_prefix(source: str) -> str:
    """Return the declaration prefix before its top-level result colon."""
    signature = source.split(":=", 1)[0]
    depths = {"(": 0, "{": 0, "[": 0}
    closing = {")": "(", "}": "{", "]": "["}
    for index, char in enumerate(signature):
        if char in depths:
            depths[char] += 1
        elif char in closing:
            opener = closing[char]
            depths[opener] = max(0, depths[opener] - 1)
        elif char == ":" and not any(depths.values()):
            return signature[:index]
    return signature


def _possibly_binds_identifier(source: str, identifier: str) -> bool:
    """Conservatively recognize declaration and tactic-local identifier binders."""
    if _references_identifier(_signature_prefix(source), identifier):
        return True
    escaped = re.escape(identifier)
    binder_patterns = (
        rf"\b(?:intro|intros|rintro|rename_i)\b[^\n]*\b{escaped}\b",
        rf"(?:\bfun\b|\bforall\b|[∀λ])[^\n]*\b{escaped}\b",
        rf"\b(?:let|have|set)\s+{escaped}\b",
        rf"\b(?:obtain|rcases|cases)\b[^\n]*\b{escaped}\b",
        rf"\bwith\b[^\n]*\b{escaped}\b",
    )
    return any(re.search(pattern, source) is not None for pattern in binder_patterns)


def batch_unprovided_identifiers(
    payload: Mapping[str, Any],
    proposals: Sequence[tuple[str, str]],
) -> tuple[str, ...]:
    """Return external unknown identifiers that make every proposal unrecoverable.

    Classification is intentionally fail-closed. Structured Lean errors must all
    be unknown-identifier errors, no proposal may declare any reported name, and
    every proposal must directly reference at least one reported name without
    possibly binding it locally. Otherwise callers retain their diagnostic
    sequential fallback.
    """
    unknown_identifiers = _unknown_identifier_errors(payload)
    if not unknown_identifiers or not proposals:
        return ()

    declared_aliases: set[str] = set()
    for declared_name, _ in proposals:
        declared_aliases.update(_identifier_aliases(declared_name))
    if any(
        _identifier_aliases(identifier) & declared_aliases for identifier in unknown_identifiers
    ):
        return ()

    proposal_code = [_lean_code_only(skeleton) for _, skeleton in proposals]
    if not all(
        any(
            _references_identifier(code, identifier)
            and not _possibly_binds_identifier(code, identifier)
            for identifier in unknown_identifiers
        )
        for code in proposal_code
    ):
        return ()
    return unknown_identifiers
