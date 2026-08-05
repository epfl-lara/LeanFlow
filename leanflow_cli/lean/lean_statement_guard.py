"""Conservative guards for Lean declaration statement edits."""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ALLOW_STATEMENT_EDITS_ENV = "LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS"

_DECL_START_RE = re.compile(
    r"^\s*(?:(?:@\[[^\]]*\]|private|protected|noncomputable|unsafe|partial)\s+)*"
    r"(?P<kind>theorem|lemma|example)\b(?P<rest>.*)$"
)
_NAMED_IDENT_RE = re.compile(r"^\s*(?P<name>[^\s:({]+)")


@dataclass(frozen=True)
class LeanStatementEntry:
    kind: str
    name: str
    signature: str
    line: int

    @property
    def key(self) -> str:
        if self.name:
            return f"{self.kind} {self.name}"
        return f"{self.kind} at line {self.line}"


@dataclass(frozen=True)
class LeanStatementGuardResult:
    ok: bool
    violations: tuple[str, ...] = ()

    @property
    def error(self) -> str:
        if self.ok:
            return ""
        details = "; ".join(self.violations)
        return (
            "Lean statement guard blocked this edit: "
            f"{details}. Existing theorem/lemma/example statements may not be deleted, moved, renamed, "
            "or changed; edit proof bodies for existing declarations. New helper declarations are allowed."
        )


def should_guard_lean_statement_path(path: str) -> bool:
    """Return whether statement guard should run for this path."""
    if os.environ.get(ALLOW_STATEMENT_EDITS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return Path(str(path or "")).suffix == ".lean"


def validate_lean_statement_edit(before: str, after: str) -> LeanStatementGuardResult:
    """Reject deletion or mutation of existing theorem/lemma/example statements.

    This is intentionally syntactic. It protects each existing declaration header
    up to the first `:=`, while allowing edits after that point.
    """
    before_entries = _extract_statement_entries(before or "")
    if not before_entries:
        return LeanStatementGuardResult(ok=True)

    after_entries = _extract_statement_entries(after or "")
    after_named = {(entry.kind, entry.name): entry for entry in after_entries if entry.name}
    before_examples = Counter(entry.signature for entry in before_entries if not entry.name)
    after_examples = Counter(entry.signature for entry in after_entries if not entry.name)

    violations: list[str] = []
    before_identities = Counter(_entry_identity(entry) for entry in before_entries)
    after_identities = Counter(_entry_identity(entry) for entry in after_entries)
    reported_duplicates: set[tuple[str, str, str]] = set()
    for entry in before_entries:
        identity = _entry_identity(entry)
        if (
            entry.name
            and identity not in reported_duplicates
            and after_identities[identity] > before_identities[identity]
        ):
            violations.append(f"duplicated existing {entry.kind} {entry.name}")
            reported_duplicates.add(identity)
    for entry in before_entries:
        if entry.name:
            replacement = after_named.get((entry.kind, entry.name))
            if replacement is None:
                violations.append(f"deleted {entry.key}")
            elif replacement.signature != entry.signature:
                violations.append(f"changed statement of {entry.key}")
        elif after_examples[entry.signature] < before_examples[entry.signature]:
            violations.append(f"deleted or changed {entry.key}")

    if violations:
        return LeanStatementGuardResult(ok=False, violations=tuple(violations))
    if _existing_statement_order_changed(before_entries, after_entries):
        return LeanStatementGuardResult(
            ok=False,
            violations=("moved or reordered existing theorem/lemma/example statements",),
        )
    return LeanStatementGuardResult(ok=True)


def _extract_statement_entries(content: str) -> list[LeanStatementEntry]:
    lines = content.splitlines()
    starts: list[tuple[int, re.Match[str]]] = []
    for idx, line in enumerate(lines):
        match = _DECL_START_RE.match(line)
        if match:
            starts.append((idx, match))

    entries: list[LeanStatementEntry] = []
    for position, (start_idx, match) in enumerate(starts):
        end_idx = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        block = "\n".join(lines[start_idx:end_idx])
        kind = match.group("kind")
        name = _declaration_name(kind, match.group("rest"))
        signature = _normalize_statement_signature(_statement_prefix(block))
        if signature:
            entries.append(
                LeanStatementEntry(
                    kind=kind,
                    name=name,
                    signature=signature,
                    line=start_idx + 1,
                )
            )
    return entries


def _entry_identity(entry: LeanStatementEntry) -> tuple[str, str, str]:
    return (entry.kind, entry.name, entry.signature)


def _existing_statement_order_changed(
    before_entries: list[LeanStatementEntry],
    after_entries: list[LeanStatementEntry],
) -> bool:
    """Return whether existing declarations changed relative order.

    New declarations may be inserted, but existing theorem/lemma/example
    statements must keep their relative order so agents cannot move a hard
    statement elsewhere and claim progress.
    """
    before_sequence = [_entry_identity(entry) for entry in before_entries]
    before_counts = Counter(before_sequence)
    seen: Counter[tuple[str, str, str]] = Counter()
    after_existing_sequence: list[tuple[str, str, str]] = []

    for entry in after_entries:
        identity = _entry_identity(entry)
        if seen[identity] < before_counts[identity]:
            seen[identity] += 1
            after_existing_sequence.append(identity)

    return after_existing_sequence != before_sequence


def _declaration_name(kind: str, rest: str) -> str:
    if kind == "example":
        return ""
    match = _NAMED_IDENT_RE.match(rest or "")
    return match.group("name") if match else ""


def _statement_prefix(block: str) -> str:
    idx = _find_assignment_marker(block)
    if idx >= 0:
        return block[:idx]
    return block


def _find_assignment_marker(text: str) -> int:
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


def _normalize_statement_signature(text: str) -> str:
    return re.sub(r"\s+", " ", _strip_lean_comments(text)).strip()


def _strip_lean_comments(text: str) -> str:
    out: list[str] = []
    depth = 0
    in_line_comment = False
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
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
            out.append(ch)
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
        out.append(ch)
        i += 1
    return "".join(out)
