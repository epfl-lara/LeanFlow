"""Pure Lean source-text and declaration parsing helpers for the native managed runner.

Extracted verbatim from ``native_runner.py`` (refactor Phase 2, step 2 — the pure-parsing
cluster). These helpers operate purely on strings/Lean source text (comment/string stripping,
declaration-name/signature/kind extraction, ``theorem``/``lemma``/``example`` detection, sorry
scanning over text, declaration line indexing and region trimming) and depend only on the
standard library and on each other — no ``native_runner`` module-level state, env readers, Lean
services, or queue objects. They live here and are re-exported from ``native_runner`` for
backwards compatibility; the names are referenced throughout that module and by tests.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "LEAN_DECLARATION_PREAMBLE_RE",
    "_strip_lean_comments_and_strings",
    "_text_has_theorem_or_lemma",
    "_text_has_sorry",
    "_text_has_theorem_or_lemma_without_sorry",
    "_text_self_approves_document_formalization_blueprint",
    "_text_has_any_completed_theorem_or_lemma",
    "_extract_target_symbol",
    "_find_assignment_marker_for_statement",
    "_trim_declaration_region_end",
    "_declaration_line_index_from_text",
    "_declaration_names_from_text",
    "_declaration_entries_by_name_from_text",
    "_declaration_matches_target",
    "_declaration_stable_key",
]


LEAN_DECLARATION_PREAMBLE_RE = (
    r"^\s*(?:(?:@\[[^\]]*\]|@[A-Za-z0-9_.]+|private|protected|noncomputable|unsafe|partial)\s+)*"
    r"(theorem|lemma|example|def|instance|class|structure)\s+([A-Za-z0-9_'.-]+)?"
)


def _strip_lean_comments_and_strings(text: str) -> str:
    """Remove Lean comments and string literals before token inspection."""
    out: list[str] = []
    i = 0
    n = len(text)
    block_depth = 0
    in_string = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if block_depth > 0:
            if ch == "/" and nxt == "-":
                block_depth += 1
                i += 2
                continue
            if ch == "-" and nxt == "/":
                block_depth -= 1
                i += 2
                continue
            if ch == "\n":
                out.append("\n")
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

        if ch == "-" and nxt == "-":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue

        if ch == "/" and nxt == "-":
            block_depth = 1
            i += 2
            continue

        if ch == '"':
            in_string = True
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _text_has_theorem_or_lemma(text: str) -> bool:
    sanitized = _strip_lean_comments_and_strings(str(text or ""))
    return bool(re.search(r"^\s*(?:@[A-Za-z0-9_.]+\s+)*(?:theorem|lemma|example)\b", sanitized, flags=re.MULTILINE))


def _text_has_sorry(text: str) -> bool:
    return bool(re.search(r"\bsorry\b", _strip_lean_comments_and_strings(str(text or ""))))


def _text_has_theorem_or_lemma_without_sorry(text: str) -> bool:
    for entry in _declaration_line_index_from_text(str(text or "")):
        kind = str(entry.get("kind", "") or "").strip().lower()
        if kind in {"theorem", "lemma", "example"} and not _text_has_sorry(str(entry.get("text", "") or "")):
            return True
    return False


def _text_self_approves_document_formalization_blueprint(text: str) -> bool:
    proposed = str(text or "")
    if not proposed.strip():
        return False
    if re.search(
        r"Statement verification status\s*:\s*[^\n]*(?:approved|pass(?:ed)?)\b",
        proposed,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"^\s*-\s*\[[xX]\]\s*Run independent statement/source verification review and apply corrections\.",
        proposed,
        flags=re.MULTILINE,
    ):
        return True
    if re.search(
        r"^\s*-\s*\[[xX]\]\s*(?:Hand stable (?:theorem/lemma/example )?`sorry` declarations to the managed prover queue|Mark stable theorem/lemma/example `sorry` declarations ready for a user-started prove workflow)\.",
        proposed,
        flags=re.MULTILINE,
    ):
        return True
    return False


def _text_has_any_completed_theorem_or_lemma(text: str) -> bool:
    return any(
        str(entry.get("kind", "") or "").strip().lower() in {"theorem", "lemma", "example"}
        and not _text_has_sorry(str(entry.get("text", "") or ""))
        for entry in _declaration_line_index_from_text(str(text or ""))
    )


def _declaration_matches_target(entry: Mapping[str, Any], target_symbol: str) -> bool:
    name = str(entry.get("name", "") or "").strip()
    wanted = str(target_symbol or "").strip()
    short = wanted.split(".")[-1]
    return bool(name and wanted and name in {wanted, short})


def _declaration_stable_key(entry: Mapping[str, Any]) -> tuple[str, str] | None:
    kind = str(entry.get("kind", "") or "").strip()
    name = str(entry.get("name", "") or "").strip()
    if not kind or not name or name.startswith("[anonymous "):
        return None
    return (kind, name)


def _find_assignment_marker_for_statement(text: str) -> int:
    depth = 0
    in_line_comment = False
    in_string = False
    escaped = False
    i = 0
    while i < len(text) - 1:
        ch = text[i]
        nxt = text[i + 1]
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if depth:
            if ch == "/" and nxt == "-":
                depth += 1
                i += 2
                continue
            if ch == "-" and nxt == "/":
                depth -= 1
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "-":
            depth = 1
            i += 2
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == ":" and nxt == "=":
            return i
        i += 1
    return -1


def _extract_target_symbol(text: str) -> str:
    patterns = [
        r"\btheorem\s+([A-Za-z_][A-Za-z0-9_']*)",
        r"\blemma\s+([A-Za-z_][A-Za-z0-9_']*)",
        r"\bdef\s+([A-Za-z_][A-Za-z0-9_']*)",
    ]
    combined = str(text or "")
    for pattern in patterns:
        match = re.search(pattern, combined)
        if match:
            return match.group(1)
    return ""


def _trim_declaration_region_end(lines: list[str], *, start: int, next_start: int | None) -> int:
    """Return the last line owned by a declaration before the next declaration preamble."""
    if not next_start:
        return len(lines)
    end = max(start, min(len(lines), next_start - 1))
    idx = end

    def _skip_blank_lines(value: int) -> int:
        while value >= start and not lines[value - 1].strip():
            value -= 1
        return value

    idx = _skip_blank_lines(idx)
    changed = True
    while changed and idx >= start:
        changed = False
        while idx >= start and lines[idx - 1].strip().startswith("--"):
            idx -= 1
            changed = True
        idx = _skip_blank_lines(idx)
        if idx >= start and lines[idx - 1].strip().endswith("-/"):
            stripped = lines[idx - 1].strip()
            if stripped.startswith("/-"):
                idx -= 1
            else:
                original_idx = idx
                idx -= 1
                found_start = False
                while idx >= start:
                    stripped = lines[idx - 1].strip()
                    idx -= 1
                    if stripped.startswith("/-"):
                        found_start = True
                        break
                if not found_start:
                    idx = original_idx
                    break
            changed = True
            idx = _skip_blank_lines(idx)
    return max(start, idx)


def _declaration_line_index_from_text(content: str) -> list[dict[str, Any]]:
    lines = str(content or "").splitlines()

    entries: list[dict[str, Any]] = []
    pattern = re.compile(LEAN_DECLARATION_PREAMBLE_RE)
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        decl_kind = match.group(1)
        decl_name = (match.group(2) or "").strip()
        if not decl_name:
            decl_name = f"[anonymous {decl_kind} @ line {line_number}]"
        entries.append(
            {
                "name": decl_name,
                "kind": decl_kind,
                "line": line_number,
            }
        )

    if not entries:
        return []

    for idx, entry in enumerate(entries):
        start = int(entry["line"])
        next_start: int | None = None
        if idx + 1 < len(entries):
            next_start = int(entries[idx + 1]["line"])
        end = _trim_declaration_region_end(lines, start=start, next_start=next_start)
        region = "\n".join(lines[start - 1:end]).strip()
        entry["end_line"] = end
        entry["text"] = region
        entry["has_sorry"] = bool(re.search(r"\bsorry\b", _strip_lean_comments_and_strings(region)))
    return entries


def _declaration_names_from_text(text: str) -> set[str]:
    return {
        str(entry.get("name", "") or "").strip()
        for entry in _declaration_line_index_from_text(text)
        if str(entry.get("name", "") or "").strip()
    }


def _declaration_entries_by_name_from_text(text: str) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for entry in _declaration_line_index_from_text(text):
        name = str(entry.get("name", "") or "").strip()
        if name:
            entries[name] = dict(entry)
    return entries
