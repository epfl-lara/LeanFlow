"""Resolve safe source positions for Lean multi-attempt tactic screening."""

from __future__ import annotations

import re
from pathlib import Path

from leanflow_cli.lean.lean_declarations import _declaration_index
from leanflow_cli.lean.lean_parsing import (
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
)

__all__ = [
    "_multi_attempt_replacement_candidate",
    "_resolve_multi_attempt_location",
]

_STANDALONE_PLACEHOLDER_RE = re.compile(
    r"^(?P<prefix>\s*(?:(?:·|-|\+)\s*)?)(?:sorry|admit)\b(?:\s*--.*)?\s*$"
)


def _resolve_tactic_line_after_blank(path: Path, requested_line: int) -> int:
    """Resolve an immediate post-proof blank to the preceding tactic line.

    Model-facing declaration ranges historically included separator whitespace. A caller may
    therefore submit that stale range end to ``lean_multi_attempt``. Only adjust one blank line
    directly after a multiline ``:= by`` declaration; every other location remains unchanged.
    """
    line = int(requested_line)
    if line <= 1:
        return line
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return line
    if line > len(lines) or lines[line - 1].strip():
        return line
    previous_line = line - 1
    for entry in _declaration_index(path):
        start = int(entry.get("line", 0) or 0)
        end = int(entry.get("end_line", 0) or 0)
        text = str(entry.get("text", "") or "")
        marker = _find_assignment_marker_for_statement(text)
        proof = _strip_lean_comments_and_strings(text[marker + 2 :]).lstrip() if marker >= 0 else ""
        if end == previous_line and end > start and re.match(r"by\b", proof):
            return previous_line
    return line


def _skip_lean_trivia(text: str, start: int) -> int:
    """Return the next source index after whitespace and Lean comments."""
    index = max(int(start), 0)
    block_depth = 0
    while index < len(text):
        if block_depth:
            if text.startswith("/-", index):
                block_depth += 1
                index += 2
            elif text.startswith("-/", index):
                block_depth -= 1
                index += 2
            else:
                index += 1
            continue
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            if newline < 0:
                return len(text)
            index = newline + 1
            continue
        if text.startswith("/-", index):
            block_depth = 1
            index += 2
            continue
        break
    return index


def _resolve_inline_tactic_column(path: Path, requested_line: int) -> int | None:
    """Return the 1-indexed tactic-body column for an inline ``:= by`` proof.

    A columnless multi-attempt normally targets the first non-whitespace character on its line.
    When the declaration header and tactic body share a line, that character begins the
    declaration rather than the proof. Resolve only when both ``by`` and its first tactic token
    occur on the requested line; ordinary multiline tactic lines keep their faster line-only path.
    """
    line = int(requested_line)
    if line <= 0:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    if line > len(lines):
        return None
    entry = next(
        (
            candidate
            for candidate in _declaration_index(path)
            if int(candidate.get("line", 0) or 0) <= line <= int(candidate.get("end_line", 0) or 0)
        ),
        None,
    )
    if entry is None:
        return None
    start_line = int(entry.get("line", 0) or 0)
    end_line = int(entry.get("end_line", 0) or 0)
    declaration = "\n".join(lines[start_line - 1 : end_line])
    marker = _find_assignment_marker_for_statement(declaration)
    if marker < 0:
        return None
    by_start = _skip_lean_trivia(declaration, marker + 2)
    if not re.match(r"by\b", declaration[by_start:]):
        return None
    tactic_start = _skip_lean_trivia(declaration, by_start + 2)
    if tactic_start >= len(declaration):
        return None
    by_line = start_line + declaration.count("\n", 0, by_start)
    tactic_line = start_line + declaration.count("\n", 0, tactic_start)
    if by_line != line or tactic_line != line:
        return None
    line_start = declaration.rfind("\n", 0, tactic_start) + 1
    return tactic_start - line_start + 1


def _first_tactic_body_line(path: Path, requested_line: int) -> int | None:
    """Return the first tactic line for the declaration containing a source line."""
    line = int(requested_line)
    if line <= 0:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    entry = next(
        (
            candidate
            for candidate in _declaration_index(path)
            if int(candidate.get("line", 0) or 0) <= line <= int(candidate.get("end_line", 0) or 0)
        ),
        None,
    )
    if entry is None:
        return None
    start_line = int(entry.get("line", 0) or 0)
    end_line = int(entry.get("end_line", 0) or 0)
    declaration = "\n".join(lines[start_line - 1 : end_line])
    marker = _find_assignment_marker_for_statement(declaration)
    if marker < 0:
        return None
    by_start = _skip_lean_trivia(declaration, marker + 2)
    if not re.match(r"by\b", declaration[by_start:]):
        return None
    tactic_start = _skip_lean_trivia(declaration, by_start + 2)
    if tactic_start >= len(declaration):
        return None
    return start_line + declaration.count("\n", 0, tactic_start)


def _line_closes_prior_syntax(path: Path, requested_line: int) -> bool:
    """Return whether replacing a whole tactic line would drop prior delimiters."""
    try:
        source_line = path.read_text(encoding="utf-8").splitlines()[requested_line - 1]
    except (IndexError, OSError, UnicodeError):
        return False
    sanitized = _strip_lean_comments_and_strings(source_line)
    return any(
        sanitized.count(closing) > sanitized.count(opening)
        for opening, closing in (("(", ")"), ("[", "]"), ("{", "}"))
    )


def _resolve_trailing_placeholder(
    path: Path,
    requested_line: int,
) -> tuple[int, int] | None:
    """Return a nearby standalone trailing placeholder inside the same declaration."""
    line = int(requested_line)
    if line <= 0:
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    entries = [
        entry
        for entry in _declaration_index(path)
        if int(entry.get("line", 0) or 0) <= line <= int(entry.get("end_line", 0) or 0) + 1
    ]
    if not entries:
        return None
    entry = entries[0]
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or 0)
    declaration = str(entry.get("text", "") or "")
    marker = _find_assignment_marker_for_statement(declaration)
    proof = (
        _strip_lean_comments_and_strings(declaration[marker + 2 :]).lstrip() if marker >= 0 else ""
    )
    if not re.match(r"by\b", proof):
        return None
    placeholders: list[tuple[int, int]] = []
    for candidate_line in range(start, min(end, len(lines)) + 1):
        match = _STANDALONE_PLACEHOLDER_RE.match(lines[candidate_line - 1])
        if match is not None:
            placeholders.append((candidate_line, len(match.group("prefix")) + 1))
    # A line request is a forward source anchor. Prefer the first hole at or
    # after it, even when earlier branches still contain intentional holes.
    at_or_after = [placeholder for placeholder in placeholders if placeholder[0] >= line]
    if at_or_after:
        return min(at_or_after, key=lambda placeholder: placeholder[0])
    # A stale anchor beyond the declaration may move backward only when the
    # declaration has one unambiguous standalone placeholder.
    if len(placeholders) == 1:
        return placeholders[0]
    return None


def _has_ambiguous_backward_placeholders(path: Path, requested_line: int) -> bool:
    """Return whether a line-only request could only jump to multiple earlier holes."""
    line = int(requested_line)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    entry = next(
        (
            candidate
            for candidate in _declaration_index(path)
            if int(candidate.get("line", 0) or 0)
            <= line
            <= int(candidate.get("end_line", 0) or 0) + 1
        ),
        None,
    )
    if entry is None:
        return False
    start = int(entry.get("line", 0) or 0)
    end = min(int(entry.get("end_line", 0) or 0), len(lines))
    placeholders = [
        candidate_line
        for candidate_line in range(start, end + 1)
        if _STANDALONE_PLACEHOLDER_RE.match(lines[candidate_line - 1])
    ]
    return len(placeholders) > 1 and all(candidate_line < line for candidate_line in placeholders)


def _multi_attempt_replacement_candidate(
    path: Path,
    line: int,
    column: int | None,
    snippet: str,
) -> tuple[str, str] | None:
    """Build a complete declaration replacing the selected placeholder with one tactic."""
    if column is None:
        return None
    entry = next(
        (
            candidate
            for candidate in _declaration_index(path)
            if int(candidate.get("line", 0) or 0)
            <= int(line)
            <= int(candidate.get("end_line", 0) or 0)
        ),
        None,
    )
    if entry is None:
        return None
    start = int(entry.get("line", 0) or 0)
    relative_line = int(line) - start
    declaration_lines = str(entry.get("text", "") or "").splitlines()
    if relative_line < 0 or relative_line >= len(declaration_lines):
        return None
    source_line = declaration_lines[relative_line]
    start_column = max(0, int(column) - 1)
    placeholder = re.match(r"(?:sorry|admit)\b", source_line[start_column:])
    if placeholder is None:
        return None
    declaration_lines[relative_line] = (
        source_line[:start_column]
        + str(snippet).strip()
        + source_line[start_column + placeholder.end() :]
    )
    name = str(entry.get("name", "") or "").strip()
    if not name:
        return None
    return name, "\n".join(declaration_lines)


def _resolve_multi_attempt_location(
    path: Path,
    requested_line: int,
    requested_column: int | None,
) -> tuple[int, int | None, str | None]:
    """Return a safe line, column, and adjustment for multi-attempt screening.

    Preserve valid explicit columns. Correct an out-of-range explicit column to a unique trailing
    placeholder in the same declaration, or reject it before any backend call. For line-only
    requests, correct either a stale blank declaration end or an inline tactic body. The latter
    deliberately supplies a column so the upstream MCP uses its exact-position LSP path instead
    of reconstructing incomplete context in the REPL.
    """
    line = int(requested_line)
    resolved_line = _resolve_tactic_line_after_blank(path, line)
    if resolved_line != line:
        placeholder = _resolve_trailing_placeholder(path, resolved_line)
        if placeholder is not None:
            return placeholder[0], placeholder[1], "trailing_placeholder"
        if _has_ambiguous_backward_placeholders(path, resolved_line):
            return resolved_line, None, "ambiguous_backward_placeholders"
        return resolved_line, None, "previous_tactic_line_after_blank"
    if requested_column is not None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
        column = int(requested_column)
        column_valid = 1 <= line <= len(lines) and 1 <= column <= len(lines[line - 1]) + 1
        if not column_valid:
            placeholder = _resolve_trailing_placeholder(path, line)
            if placeholder is not None:
                return (
                    placeholder[0],
                    placeholder[1],
                    "invalid_column_to_trailing_placeholder",
                )
            return line, None, "invalid_column"
        return line, requested_column, None
    placeholder = _resolve_trailing_placeholder(path, line)
    if placeholder is not None:
        return placeholder[0], placeholder[1], "trailing_placeholder"
    if _has_ambiguous_backward_placeholders(path, line):
        return line, None, "ambiguous_backward_placeholders"
    inline_column = _resolve_inline_tactic_column(path, line)
    if inline_column is not None:
        return line, inline_column, "inline_tactic_body"
    tactic_line = _first_tactic_body_line(path, line)
    if tactic_line is None:
        return line, None, "non_tactic_source_line"
    if line < tactic_line:
        return tactic_line, None, "first_tactic_line"
    if _line_closes_prior_syntax(path, line):
        return line, None, "cross_line_structural_suffix"
    return line, None, None
