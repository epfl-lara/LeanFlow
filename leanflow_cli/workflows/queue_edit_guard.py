"""Queue-edit guard helpers for the native managed runner.

Extracted from ``native_runner.py`` (refactor Phase 2, step 4 — the queue-edit-guard cluster).
During a single-queue-item prover turn the runner protects the assigned declaration (and any
pre-existing "future queue" declarations in the same file) against out-of-scope edits: it
snapshots the source statement and the surrounding protected declarations before a file-editing
tool runs, then detects and restores any out-of-scope change afterwards. The cleanly-pure parts
of that machinery live here.

These helpers operate purely on Lean source text and the small dicts the runner derives from it:
a per-(symbol, file) guard key, the initial declaration-key set cached on the agent, the assigned
statement signature, the protected-declaration inventory, the protected-declaration diff between a
snapshot and the current text, and the two text-restoration routines. They depend only on the
standard library and on the already-extracted pure-parsing helpers in ``lean_parsing`` — no
``native_runner`` module-level state, env readers, Lean services, or queue objects. They live here
and are re-exported from ``native_runner`` for backwards compatibility; the names are referenced
within that module and by tests, so this module must NOT import ``native_runner`` (that would
create a circular import).

Conservatively left in ``native_runner`` (this wave): ``_queue_edit_protect_assigned_statement``
and ``_document_formalization_source_declaration_names`` (they reach into the document-formalization
blueprint helpers), and ``_restore_out_of_scope_queue_edit`` (it calls ``_find_declaration_entry``,
a Lean-services-backed helper). Those still resolve the names moved here via the re-export.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _declaration_matches_target,
    _declaration_stable_key,
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
)

__all__ = [
    "_queue_edit_guard_key",
    "_queue_edit_initial_declaration_keys",
    "_queue_edit_statement_signature",
    "_queue_edit_assigned_statement_signature",
    "_queue_edit_protected_declarations",
    "_queue_edit_changed_protected_declarations",
    "_restore_changed_protected_declarations",
    "_restore_assigned_declaration_against_before_text",
    "_axiom_declaration_names",
    "_introduced_forbidden_axioms",
]


# Matches a top-level `axiom` declaration, tolerating modifiers/attributes
# (`@[...] private noncomputable axiom foo : ...`). Comments/strings are stripped first.
# The name group is deliberately broad so it catches every syntactically valid Lean axiom name —
# ASCII (`foo`, `foo.bar`), Unicode (`α`, `f₁`), and guillemet-quoted (`«cheat ax»`) — rather than
# only ASCII identifiers, which would let a cheating edit slip an axiom past the guard.
_AXIOM_DECL_RE = re.compile(
    r"^[ \t]*(?:@\[[^\]]*\][ \t]*)*"
    r"(?:private[ \t]+|protected[ \t]+|noncomputable[ \t]+|scoped[ \t]+|local[ \t]+|unsafe[ \t]+)*"
    r"axiom[ \t]+(«[^»]+»|[^\s:({\[]+)",
    re.MULTILINE,
)


def _axiom_declaration_names(text: str) -> set[str]:
    """Return the names of top-level ``axiom`` declarations in Lean source.

    Comments and string literals are stripped first so that the word "axiom" inside a comment or
    string does not produce a false match.
    """
    stripped = _strip_lean_comments_and_strings(str(text or ""))
    return {match.group(1) for match in _AXIOM_DECL_RE.finditer(stripped)}


def _introduced_forbidden_axioms(
    before_text: str,
    after_text: str,
    allowed: Sequence[str],
) -> list[str]:
    """Names of ``axiom`` declarations an edit NEWLY introduced that are not in the allowed set.

    Declaring an axiom in a proof assumes the goal instead of proving it, so the prover must not do
    it. ``allowed`` lets a run explicitly permit specific axiom names (e.g. via ``--axioms``); the
    standard dependency axioms are included by the caller's default allowed set.
    """
    allowed_set = {str(name).strip() for name in (allowed or []) if str(name).strip()}
    introduced = _axiom_declaration_names(after_text) - _axiom_declaration_names(before_text)
    return sorted(name for name in introduced if name not in allowed_set)


def _queue_edit_guard_key(target_symbol: str, active_file: str) -> str:
    try:
        resolved = str(Path(active_file).resolve())
    except Exception:
        resolved = str(active_file or "")
    return f"{target_symbol}\0{resolved}"


def _queue_edit_initial_declaration_keys(
    agent: Any,
    active_file: str,
    before_text: str,
) -> set[tuple[str, str]]:
    file_key = _queue_edit_guard_key("__file__", active_file)
    state = dict(getattr(agent, "_managed_initial_declaration_keys_by_file", {}) or {})
    stored = state.get(file_key)
    if isinstance(stored, list):
        return {
            tuple(item) for item in stored if isinstance(item, (list, tuple)) and len(item) == 2
        }
    keys = {
        key
        for entry in _declaration_line_index_from_text(before_text)
        if (key := _declaration_stable_key(entry)) is not None
    }
    state[file_key] = [list(key) for key in sorted(keys)]
    agent._managed_initial_declaration_keys_by_file = state
    return keys


def _queue_edit_statement_signature(entry: Mapping[str, Any]) -> str:
    text = str(entry.get("text", "") or "")
    idx = _find_assignment_marker_for_statement(text)
    statement = text[:idx] if idx >= 0 else text
    return re.sub(r"\s+", " ", _strip_lean_comments_and_strings(statement)).strip()


def _queue_edit_assigned_statement_signature(content: str, target_symbol: str) -> str:
    for entry in _declaration_line_index_from_text(content):
        if _declaration_matches_target(entry, target_symbol):
            return _queue_edit_statement_signature(entry)
    return ""


def _queue_edit_protected_declarations(content: str, target_symbol: str) -> list[dict[str, Any]]:
    protected: list[dict[str, Any]] = []
    for entry in _declaration_line_index_from_text(content):
        key = _declaration_stable_key(entry)
        if key is None or _declaration_matches_target(entry, target_symbol):
            continue
        protected.append(
            {
                "kind": key[0],
                "name": key[1],
                "text": str(entry.get("text", "") or "").strip(),
                "line": int(entry.get("line", 0) or 0),
            }
        )
    return protected


def _queue_edit_changed_protected_declarations(
    protected_declarations: Sequence[Mapping[str, Any]],
    current_text: str,
) -> list[dict[str, Any]]:
    current_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in _declaration_line_index_from_text(current_text):
        key = _declaration_stable_key(entry)
        if key is not None and key not in current_by_key:
            current_by_key[key] = entry

    changed: list[dict[str, Any]] = []
    for protected in protected_declarations:
        key = (str(protected.get("kind", "") or ""), str(protected.get("name", "") or ""))
        if not key[0] or not key[1]:
            continue
        current = current_by_key.get(key)
        if current is None:
            changed.append({"reason": "missing", "protected": dict(protected)})
            continue
        if (
            str(current.get("text", "") or "").strip()
            != str(protected.get("text", "") or "").strip()
        ):
            changed.append({"reason": "changed", "protected": dict(protected), "current": current})
    return changed


def _restore_changed_protected_declarations(
    current_text: str, changed: Sequence[Mapping[str, Any]]
) -> str | None:
    if not changed:
        return current_text
    if any(str(item.get("reason", "") or "") == "missing" for item in changed):
        return None
    lines = current_text.splitlines()
    replacements = sorted(
        (dict(item) for item in changed),
        key=lambda item: int(dict(item.get("current") or {}).get("line", 0) or 0),
        reverse=True,
    )
    for item in replacements:
        current = dict(item.get("current") or {})
        protected = dict(item.get("protected") or {})
        start = int(current.get("line", 0) or 0)
        end = int(current.get("end_line", 0) or 0)
        if start <= 0 or end < start:
            return None
        replacement_lines = str(protected.get("text", "") or "").splitlines()
        lines = lines[: start - 1] + replacement_lines + lines[end:]
    restored = "\n".join(lines)
    if current_text.endswith("\n"):
        restored += "\n"
    return restored


def _restore_assigned_declaration_against_before_text(
    before_text: str,
    current_slice: str,
    *,
    start: int,
    end: int,
) -> str:
    before_lines = before_text.splitlines()
    replacement_lines = current_slice.splitlines()
    restored_lines = before_lines[: start - 1] + replacement_lines + before_lines[end:]
    restored_text = "\n".join(restored_lines)
    if before_text.endswith("\n"):
        restored_text += "\n"
    return restored_text
