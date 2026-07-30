"""Discover golf candidates and measure Lean declaration size.

The helpers enumerate declared, sorry-free theorems and lemmas without
modifying project files.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _declaration_line_index_from_text

GOLF_WORKFLOW_KINDS = frozenset({"golf", "refactor"})

#: The queue-item reason marking a golf candidate (its own selection bucket).
GOLF_REASON = "golf candidate"


def is_golf_workflow(kind: str) -> bool:
    return str(kind or "").strip().lower() in GOLF_WORKFLOW_KINDS


def _blank_block_comments(text: str) -> str:
    """Replace ``/- … -/`` block-comment content with spaces, char-for-char.

    Every newline and every other character keeps its exact position, so
    line and column numbers are unchanged — only the bytes INSIDE block
    comments become spaces. This must run BEFORE the declaration parser:
    a ``theorem`` keyword inside a block comment would otherwise be indexed
    as a phantom declaration, which not only pollutes the queue but SPLITS
    the enclosing real declaration's region (truncating it at the phantom's
    line) — hiding a later ``sorry`` and corrupting ``declaration_chars``.
    Nesting-aware; ``--`` line comments and string literals cannot open a
    block comment (a stray ``/-`` inside a string is not a comment).
    """
    out = list(text)
    depth = 0
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if depth > 0:
            if ch == "/" and nxt == "-":
                out[i] = out[i + 1] = " "
                depth += 1
                i += 2
                continue
            if ch == "-" and nxt == "/":
                out[i] = out[i + 1] = " "
                depth -= 1
                i += 2
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == "-" and nxt == "-":  # line comment: parser skips these itself
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "-":
            out[i] = out[i + 1] = " "
            depth = 1
            i += 2
            continue
        if ch == '"':
            in_string = True
        i += 1
    return "".join(out)


def golf_declaration_queue(active_file: str, *, project_root: str = "") -> list[dict[str, Any]]:
    """Every declared, sorry-free theorem/lemma in the file as a golf candidate.

    Block comments are blanked BEFORE parsing (positions preserved), so a
    ``theorem`` inside ``/- … -/`` neither enters the queue nor truncates
    the region of the real declaration around it. "Sorry-free" is then
    STRUCTURAL: the parser's ``has_sorry`` flag ignores comments and string
    literals, so a `-- sorry` note never masks a finished proof and a real
    `by sorry` is never missed. Open work stays with ``prove``.

    This structural scan does not elaborate, so callers must compile-check a
    candidate before accepting it. Never raises: an unreadable file is an
    empty queue.
    """
    try:
        root = Path(project_root or ".")
        path = root / active_file if not os.path.isabs(active_file) else Path(active_file)
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for entry in _declaration_line_index_from_text(_blank_block_comments(text)):
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("kind", "")) not in {"theorem", "lemma"}:
            continue
        name = str(entry.get("name", "") or "")
        if not name or name.startswith("["):  # skip parser anonymous placeholders
            continue
        if entry.get("has_sorry"):
            continue  # structural sorry check — prove owns open work
        items.append(
            {
                "label": name,
                "kind": str(entry.get("kind", "")),
                "line": int(entry.get("line", 1)),
                "end_line": int(entry.get("end_line", entry.get("line", 1))),
                "reasons": [GOLF_REASON],
            }
        )
    return items


def declaration_chars(active_file: str, target_symbol: str, *, project_root: str = "") -> int:
    """Current on-disk character count of one declaration (0 when absent).

    Parses over block-comment-blanked source (same as the queue) so the
    region boundary is not corrupted by a phantom declaration; blanking is
    length-preserving, so the count still reflects the real on-disk span.
    """
    try:
        root = Path(project_root or ".")
        path = root / active_file if not os.path.isabs(active_file) else Path(active_file)
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for entry in _declaration_line_index_from_text(_blank_block_comments(text)):
        if isinstance(entry, Mapping) and str(entry.get("name", "")) == target_symbol:
            return len(str(entry.get("text", "")))
    return 0
