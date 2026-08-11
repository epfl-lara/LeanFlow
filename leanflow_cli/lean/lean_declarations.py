"""Index and locate top-level declarations in Lean source files.

Unlike the string-oriented helpers in ``lean_parsing``, these functions read a
filesystem path and return line-indexed declaration regions. They remain
re-exported from ``lean_services`` for compatibility.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "LEAN_DECLARATION_PREAMBLE_RE",
    "_declaration_index",
    "_find_symbol_line",
    "_find_declaration_entry",
    "_surrounding_declarations",
    "_split_declaration_statement_and_proof",
    "_declaration_text_from_location",
    "declaration_outline",
    "declaration_region",
]


# Single source of truth for the declaration-preamble pattern lives in lean_parsing; import (and
# re-export, via __all__) it here rather than duplicating the literal, to avoid future drift.
from leanflow_cli.lean.lean_parsing import (
    LEAN_DECLARATION_PREAMBLE_RE,
    _trim_declaration_region_end,
)


def _declaration_index(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    pattern = re.compile(LEAN_DECLARATION_PREAMBLE_RE)
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        match = pattern.match(line)
        if not match:
            continue
        name = (match.group(2) or "").strip()
        if not name:
            continue
        entries.append({"kind": match.group(1), "name": name, "line": line_number})
    for idx, entry in enumerate(entries):
        start = entry["line"]
        next_start = entries[idx + 1]["line"] if idx + 1 < len(entries) else None
        end = _trim_declaration_region_end(lines, start=start, next_start=next_start)
        entry["end_line"] = end
        entry["text"] = "\n".join(lines[start - 1 : end]).strip()
    return entries


def _find_symbol_line(path: Path, symbol: str | None) -> int | None:
    wanted = str(symbol or "").strip()
    if not wanted:
        return None
    for entry in _declaration_index(path):
        if entry["name"] == wanted:
            return int(entry["line"])
    return None


def _find_declaration_entry(path: Path, theorem_id: str) -> dict[str, Any] | None:
    wanted = str(theorem_id or "").strip()
    if not wanted:
        return None
    short_name = wanted.split(".")[-1]
    for entry in _declaration_index(path):
        name = str(entry.get("name", "") or "").strip()
        if name in {wanted, short_name}:
            return dict(entry)
    return None


def _surrounding_declarations(path: Path, theorem_id: str, *, window: int = 12) -> list[str]:
    """Return relevant declarations that precede the requested declaration.

    Later declarations in the same file are not in scope while Lean elaborates
    the requested declaration. Include every source-local declaration named by
    the target plus a bounded recent window, so inserted helper banks remain
    visible without dumping the entire file.
    """
    entries = _declaration_index(path)
    if not entries:
        return []
    wanted = str(theorem_id or "").strip()
    short_name = wanted.split(".")[-1]
    for idx, entry in enumerate(entries):
        name = str(entry.get("name", "") or "").strip()
        if name not in {wanted, short_name}:
            continue
        start = max(0, idx - max(0, int(window)))
        target_text = str(entry.get("text", "") or "")
        referenced = {
            str(item.get("name", "") or "").strip()
            for item in entries[:idx]
            if str(item.get("name", "") or "").strip()
            and re.search(
                rf"(?<![\w.]){re.escape(str(item.get('name', '') or '').strip())}(?![\w'])",
                target_text,
            )
        }
        return [
            str(item.get("name", "") or "").strip()
            for item_index, item in enumerate(entries[:idx])
            if str(item.get("name", "") or "").strip()
            and (item_index >= start or str(item.get("name", "") or "").strip() in referenced)
        ]
    return []


def _split_declaration_statement_and_proof(text: str) -> tuple[str, str]:
    snippet = str(text or "").strip()
    if not snippet:
        return "", ""
    match = re.search(r":=\s*by\b", snippet)
    if match:
        statement = snippet[: match.start()].rstrip()
        proof = snippet[match.end() :].lstrip()
        return statement, proof
    statement_line = snippet.splitlines()[0].strip()
    remainder = "\n".join(snippet.splitlines()[1:]).strip()
    return statement_line, remainder


def declaration_outline(path: Path) -> list[dict[str, Any]]:
    """Return one token-cheap row per top-level declaration: kind, name, line, end_line (no source)."""
    return [
        {
            "kind": str(entry.get("kind", "") or ""),
            "name": str(entry.get("name", "") or ""),
            "line": int(entry.get("line", 0) or 0),
            "end_line": int(entry.get("end_line", 0) or 0),
        }
        for entry in _declaration_index(path)
    ]


def declaration_region(path: Path, symbol: str) -> dict[str, Any] | None:
    """Return the named declaration's kind/name/line/end_line plus its full source ``text``, or None."""
    entry = _find_declaration_entry(path, symbol)
    if not entry:
        return None
    return {
        "kind": str(entry.get("kind", "") or ""),
        "name": str(entry.get("name", "") or ""),
        "line": int(entry.get("line", 0) or 0),
        "end_line": int(entry.get("end_line", 0) or 0),
        "text": str(entry.get("text", "") or ""),
    }


def _declaration_text_from_location(file_path: Path, location: Mapping[str, Any]) -> str:
    try:
        decl_start = int(location.get("decl_start", 0) or 0)
        proof_end = int(location.get("proof_end", 0) or 0)
        decl_end = int(location.get("decl_end", 0) or 0)
    except (TypeError, ValueError):
        return ""
    end_line = proof_end or decl_end
    if decl_start <= 0 or end_line < decl_start:
        return ""
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if decl_start > len(lines):
        return ""
    start_idx = decl_start - 1
    end_idx = min(end_line, len(lines))
    return "\n".join(lines[start_idx:end_idx]).strip()
