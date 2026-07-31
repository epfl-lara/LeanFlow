"""Protect assigned and future Lean declarations from out-of-scope edits.

The pure helpers snapshot statements, compare protected declaration inventories,
and restore changed source text. They remain independent of runner state and are
re-exported from ``native_runner`` for compatibility.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    "_queue_edit_assigned_preamble",
    "_queue_edit_preserves_doc_comments",
    "_queue_edit_protected_declarations",
    "_queue_edit_changed_protected_declarations",
    "_queue_edit_named_declarations",
    "_queue_edit_placeholder_regressions",
    "_queue_edit_declaration_delta",
    "_queue_edit_removed_generated_assignment_is_safe",
    "QueueEditDeclarationDelta",
    "_restore_changed_protected_declarations",
    "_restore_assigned_declaration_against_before_text",
    "_axiom_declaration_names",
    "_introduced_forbidden_axioms",
]


@dataclass(frozen=True)
class QueueEditDeclarationDelta:
    """Describe assignment and helper changes left after queue guarding."""

    assigned_changed: bool | None
    helper_names: tuple[str, ...]


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


def _standalone_attribute_start(lines: Sequence[str], boundary: int) -> int | None:
    """Return the line starting one standalone attribute before ``boundary``."""
    end = max(0, min(len(lines), int(boundary)))
    while end > 0 and not str(lines[end - 1]).strip():
        end -= 1
    if end <= 0:
        return None
    sanitized = _strip_lean_comments_and_strings("".join(str(line) for line in lines[:end]))
    sanitized_lines = sanitized.splitlines()
    for candidate in range(len(sanitized_lines) - 1, -1, -1):
        fragment = "\n".join(sanitized_lines[candidate:end]).strip()
        if not fragment.startswith("@["):
            continue
        depth = 0
        closed_at = -1
        for offset, char in enumerate(fragment[1:], start=1):
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    closed_at = offset
                    break
        if closed_at >= 0 and not fragment[closed_at + 1 :].strip():
            return candidate
    return None


def _queue_edit_assigned_preamble(content: str, target_symbol: str) -> str | None:
    """Return the exact doc-comment and attribute preamble attached to a target.

    ``None`` means the target cannot be resolved unambiguously. An empty string
    is a valid declaration with no attached preamble. Keeping this exact slice
    stable prevents a helper insertion from silently stealing target docs.
    """
    source = str(content or "")
    wanted = str(target_symbol or "").strip()
    if not source or not wanted:
        return None
    entries = _declaration_line_index_from_text(source)
    exact = [entry for entry in entries if str(entry.get("name", "") or "") == wanted]
    if len(exact) == 1:
        target = exact[0]
    elif exact:
        return None
    else:
        short = wanted.split(".")[-1]
        matches = [
            entry for entry in entries if str(entry.get("name", "") or "").split(".")[-1] == short
        ]
        if len(matches) != 1:
            return None
        target = matches[0]

    lines = source.splitlines(keepends=True)
    declaration_line = int(target.get("line", 0) or 0)
    if declaration_line <= 0 or declaration_line > len(lines):
        return None
    declaration_index = declaration_line - 1
    boundary = declaration_index
    while True:
        attribute_start = _standalone_attribute_start(lines, boundary)
        if attribute_start is None:
            break
        boundary = attribute_start

    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    declaration_offset = offsets[declaration_index]
    preamble_offset = offsets[boundary]

    cursor = preamble_offset
    while cursor > 0 and source[cursor - 1] in " \t\r\n":
        cursor -= 1
    if cursor >= 2 and source[:cursor].endswith("-/"):
        doc_start = source.rfind("/-", 0, cursor)
        if (
            doc_start >= 0
            and source.startswith("/--", doc_start)
            and not source[cursor:preamble_offset].strip()
        ):
            preamble_offset = source.rfind("\n", 0, doc_start) + 1
    return source[preamble_offset:declaration_offset]


def _lean_doc_comments(content: str) -> tuple[str, ...]:
    """Return exact top-level Lean declaration doc comments in source order."""
    source = str(content or "")
    comments: list[str] = []
    index = 0
    while index < len(source):
        if source.startswith("--", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source[index] == '"':
            index += 1
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if source.startswith("/-", index):
            start = index
            is_doc = source.startswith("/--", index)
            depth = 1
            index += 2
            while index < len(source) and depth:
                if source.startswith("/-", index):
                    depth += 1
                    index += 2
                elif source.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if is_doc and depth == 0:
                comments.append(source[start:index])
            continue
        index += 1
    return tuple(comments)


def _queue_edit_preserves_doc_comments(before_text: str, after_text: str) -> bool:
    """Return whether every pre-edit declaration doc comment remains exact.

    Moving an unchanged comment atomically is allowed. Removing one copy or
    editing its text is rejected, including when a prior bad insertion made
    the comment attach to a helper instead of its intended target.
    """
    before = Counter(_lean_doc_comments(before_text))
    after = Counter(_lean_doc_comments(after_text))
    return not bool(before - after)


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


def _queue_edit_named_declarations(
    content: str,
    names: Sequence[str],
) -> list[dict[str, Any]]:
    """Return exact declaration snapshots for the requested names."""
    wanted = {str(name or "").strip() for name in names if str(name or "").strip()}
    if not wanted:
        return []
    declarations: list[dict[str, Any]] = []
    for entry in _declaration_line_index_from_text(content):
        name = str(entry.get("name", "") or "").strip()
        if not name or not any(_declaration_matches_target(entry, item) for item in wanted):
            continue
        key = _declaration_stable_key(entry)
        if key is None:
            continue
        declarations.append(
            {
                "kind": key[0],
                "name": key[1],
                "text": str(entry.get("text", "") or "").strip(),
                "line": int(entry.get("line", 0) or 0),
            }
        )
    return declarations


def _queue_edit_placeholder_regressions(
    before_declarations: Sequence[Mapping[str, Any]],
    current_text: str,
) -> list[dict[str, Any]]:
    """Return proved declarations regressed to placeholders without statement changes.

    Generated dependency helpers remain editable so a parent theorem can repair
    their statements. This narrower check prevents a broad later edit from
    erasing an already-banked proof while preserving that statement-revision
    workflow.
    """

    def placeholders(source: str) -> tuple[str, ...]:
        sanitized = _strip_lean_comments_and_strings(str(source or ""))
        return tuple(
            token
            for token in ("sorry", "admit")
            if re.search(rf"\b{re.escape(token)}\b", sanitized)
        )

    current_by_key = {
        key: entry
        for entry in _declaration_line_index_from_text(current_text)
        if (key := _declaration_stable_key(entry)) is not None
    }
    regressions: list[dict[str, Any]] = []
    for before in before_declarations:
        key = (str(before.get("kind", "") or ""), str(before.get("name", "") or ""))
        if not key[0] or not key[1]:
            continue
        before_source = str(before.get("text", "") or "")
        if placeholders(before_source):
            continue
        current = current_by_key.get(key)
        if current is None:
            continue
        current_source = str(current.get("text", "") or "")
        current_placeholders = placeholders(current_source)
        if not current_placeholders:
            continue
        if _queue_edit_statement_signature(before) != _queue_edit_statement_signature(current):
            continue
        regressions.append(
            {
                "reason": "placeholder_regression",
                "protected": dict(before),
                "current": current,
                "placeholders": current_placeholders,
            }
        )
    return regressions


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
        current_source = str(current.get("text", "") or "").strip()
        protected_source = str(protected.get("text", "") or "").strip()
        if current_source == protected_source:
            continue
        # The lightweight declaration index starts a declaration at its
        # ``theorem``/``lemma`` keyword. Lean's declaration-local commands
        # therefore appear as a suffix of the preceding declaration:
        #
        #   theorem old ... := ...
        #   open scoped Classical in
        #   theorem helper ... := ...
        #
        # Treat that exact appended command as the next declaration's scope
        # prefix. Otherwise the guard "restores" ``old`` by deleting the
        # prefix after a successful whole-file verification, leaving the
        # newly banked helper in a source state that was never checked.
        appended = (
            current_source[len(protected_source) :].strip()
            if current_source.startswith(protected_source)
            else ""
        )
        declaration_local_prefix = bool(
            appended
            and all(
                re.fullmatch(
                    r"(?:open(?:\s+scoped)?|include|omit|set_option)\b.*\bin",
                    line.strip(),
                )
                for line in appended.splitlines()
                if line.strip()
            )
        )
        if not declaration_local_prefix:
            changed.append({"reason": "changed", "protected": dict(protected), "current": current})
    return changed


def _queue_edit_declaration_delta(
    before_text: str,
    after_text: str,
    target_symbol: str,
    protected_declarations: Sequence[Mapping[str, Any]],
) -> QueueEditDeclarationDelta:
    """Classify the accepted declaration changes without inferring proof success.

    Historical non-target declarations are guard-owned and therefore excluded
    from helper candidates. A helper candidate is a newly introduced theorem
    or lemma, or a later edit to a helper introduced during this assignment.
    """
    before_entries = _declaration_line_index_from_text(before_text)
    after_entries = _declaration_line_index_from_text(after_text)
    before_target = next(
        (entry for entry in before_entries if _declaration_matches_target(entry, target_symbol)),
        None,
    )
    after_target = next(
        (entry for entry in after_entries if _declaration_matches_target(entry, target_symbol)),
        None,
    )
    if before_target is None or after_target is None:
        assigned_changed: bool | None = None
    else:
        assigned_changed = (
            str(before_target.get("text", "") or "").strip()
            != str(after_target.get("text", "") or "").strip()
        )

    before_by_key = {
        key: entry
        for entry in before_entries
        if (key := _declaration_stable_key(entry)) is not None
    }
    protected_keys = {
        (str(item.get("kind", "") or ""), str(item.get("name", "") or ""))
        for item in protected_declarations
    }
    helper_names: list[str] = []
    for entry in after_entries:
        if _declaration_matches_target(entry, target_symbol):
            continue
        key = _declaration_stable_key(entry)
        if key is None or key[0] not in {"theorem", "lemma"} or key in protected_keys:
            continue
        previous = before_by_key.get(key)
        if (
            previous is not None
            and str(previous.get("text", "") or "").strip()
            == str(entry.get("text", "") or "").strip()
        ):
            continue
        helper_names.append(str(entry.get("name", "") or key[1]).strip())
    return QueueEditDeclarationDelta(
        assigned_changed=assigned_changed,
        helper_names=tuple(name for name in helper_names if name),
    )


def _queue_edit_removed_generated_assignment_is_safe(
    current_text: str,
    target_symbol: str,
    *,
    removal_authorized: bool,
    protected_declarations: Sequence[Mapping[str, Any]],
) -> bool:
    """Return whether an authorized generated assignment vanished without collateral edits.

    The caller owns provenance: this predicate only accepts a target already
    classified as an optional runtime-generated helper. It then fails closed
    if the declaration remains, any other protected declaration changed, or
    the remaining Lean source still references the helper identifier.
    """
    if not removal_authorized or not target_symbol:
        return False
    entries = _declaration_line_index_from_text(current_text)
    if any(_declaration_matches_target(entry, target_symbol) for entry in entries):
        return False
    if _queue_edit_changed_protected_declarations(protected_declarations, current_text):
        return False
    leaf = str(target_symbol).rsplit(".", 1)[-1]
    if not leaf:
        return False
    sanitized = _strip_lean_comments_and_strings(current_text)
    return re.search(rf"(?<![\w']){re.escape(leaf)}(?![\w'])", sanitized) is None


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
