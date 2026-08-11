"""Parse Lean source text without invoking the compiler.

The helpers strip comments and strings, identify declarations, split statements
from proofs, and scan declaration regions for placeholders. They remain
re-exported from ``native_runner`` for compatibility.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "LEAN_DECLARATION_PREAMBLE_RE",
    "_contains_lean_suggestion_tactic",
    "_lean_suggestion_tactic_markers",
    "_is_lean_inspection_only_helper_candidate",
    "_is_lean_inspection_only_target_candidate",
    "_strip_lean_comments_and_strings",
    "_text_has_theorem_or_lemma",
    "_text_has_sorry",
    "_text_has_theorem_or_lemma_without_sorry",
    "_text_self_approves_document_formalization_blueprint",
    "_text_has_any_completed_theorem_or_lemma",
    "_extract_target_symbol",
    "_find_assignment_marker_for_statement",
    "declaration_statement_text",
    "_statement_signature_text",
    "_trim_declaration_region_end",
    "_declaration_line_index_from_text",
    "_declaration_names_from_text",
    "_declaration_entries_by_name_from_text",
    "_declaration_matches_target",
    "_declaration_stable_key",
]


_LEAN_SCOPED_COMMAND_PREFIX_RE = (
    r"(?:set_option|variable|include|omit|attribute|open(?:\s+scoped)?)"
)
LEAN_DECLARATION_PREAMBLE_RE = (
    rf"^\s*(?:{_LEAN_SCOPED_COMMAND_PREFIX_RE}\b[^\n]*\bin\s+)*"
    r"(?:(?:@\[[^\]]*\]|@[A-Za-z0-9_.]+|private|protected|noncomputable|unsafe|partial|nonrec|scoped|local)\s+)*"
    r"(theorem|lemma|example|def|abbrev|opaque|axiom|instance|class|structure|inductive)\s+"
    r"([A-Za-z0-9_'-]+(?:\.[A-Za-z0-9_'-]+)*)?"
)

_DECLARATION_OPENERS = {"(": ")", "{": "}", "[": "]", "⦃": "⦄", "⟨": "⟩"}
_DECLARATION_CLOSERS = {closer: opener for opener, closer in _DECLARATION_OPENERS.items()}
_SCOPED_COMMAND_WRAPPER_LINE_RE = re.compile(rf"^\s*{_LEAN_SCOPED_COMMAND_PREFIX_RE}\b.*\bin\s*$")
_TYPE_ASSIGNMENT_KEYWORDS = ("let", "have")
_SUGGESTION_TACTIC_RE = re.compile(
    r"(?m)(?:^[ \t]*set_option\b[^\n]*\bin[ \t]+)?"
    r"(?<![A-Za-z0-9_'])"
    r"(?P<tactic>(?:exact|apply|simp|rw|aesop|grind|linarith|omega|norm_num|ring)\?"
    r"|library_search)"
    r"(?=\s|$|[\)\]\},;|])"
)
_LEAN_INSPECTION_COMMAND_RE = re.compile(r"(?m)^\s*(?:#(?:check|print|eval|reduce)\b|run_cmd\b)")
_STANDALONE_TRACE_STATE_RE = re.compile(r"(?m)^\s*trace_state\s*$")
_HELPER_DECLARATION_START_RE = re.compile(
    r"(?m)^\s*(?:private\s+)?(?:theorem|lemma|example|def|abbrev)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_']*)(?P<header>[^\n]*)"
)
_TRIVIAL_TRUE_DECLARATION_RE = re.compile(r":\s*True\s*(?::=|where|$)")
_INSPECTION_DECLARATION_NAME_RE = re.compile(
    r"(?:^|_)(?:inspect|inspection|probe|lookup|typecheck|scratch|temp|test|tmp)(?:_|$)",
    flags=re.IGNORECASE,
)
_BARE_IDENTIFIER_PATTERN = r"(?:[A-Za-z_][A-Za-z0-9_']*\.)*[A-Za-z_][A-Za-z0-9_']*"
_FALSE_IDENTIFIER_PROBE_RE = re.compile(
    rf":\s*False\s*:=\s*by\s+(?:exact\s+|simpa\s+using\s+){_BARE_IDENTIFIER_PATTERN}\s*$",
    flags=re.DOTALL,
)
_TRIVIAL_BINDING_PROBE_RE = re.compile(
    r":\s*True\s*:=\s*by\s+(?:have|let)\b.+?(?:\n|;)\s*" r"(?:trivial|exact\s+True\.intro)\s*$",
    flags=re.DOTALL,
)
_TRUE_TERM_TYPE_PROBE_RE = re.compile(
    r":\s*True\s*:=\s*by\s+"
    r"(?:exact\s+|simpa(?:\s+only)?(?:\s*\[[^\]]*\])?\s+using\s+)"
    r"\S.+\s*$",
    flags=re.DOTALL,
)
_TRIVIAL_TRUE_PROBE_RE = re.compile(
    r":\s*True\s*:=\s*by\b.+?(?:trivial|exact\s+True\.intro)\s*$",
    flags=re.DOTALL,
)
_TRACED_FAILURE_PROBE_RE = re.compile(
    rf":=\s*by\b(?=[\s\S]*?^\s*trace_state\s*(?:--.*)?$)"
    rf"(?=[\s\S]*?^\s*(?:all_goals\s+)?fail_if_success\s+done\s*(?:--.*)?$)"
    rf"[\s\S]*?^\s*(?:exact\s+|simpa\s+using\s+){_BARE_IDENTIFIER_PATTERN}\s*$",
    flags=re.MULTILINE,
)
_BOUND_DECLARATION_TYPE_PROBE_RE = re.compile(
    rf"^\s*have\s+(?P<bound>[A-Za-z_][A-Za-z0-9_']*)\s*:=\s*@{_BARE_IDENTIFIER_PATTERN}"
    rf"(?:\s+\([^\n]*\))*\s*$[\s\S]*?^\s*exact\s+(?P=bound)\s*$",
    flags=re.MULTILINE,
)


def _top_level_relation_sides(expression: str) -> tuple[str, str] | None:
    """Return the two sides of one top-level equality or equivalence."""
    opening_to_closing = {"(": ")", "[": "]", "{": "}", "⦃": "⦄", "⟨": "⟩"}
    closing = {value: key for key, value in opening_to_closing.items()}
    stack: list[str] = []
    for index, char in enumerate(expression):
        if char in opening_to_closing:
            stack.append(char)
            continue
        if char in closing:
            if stack and stack[-1] == closing[char]:
                stack.pop()
            continue
        if stack:
            continue
        if char == "↔":
            return expression[:index], expression[index + 1 :]
        if char == "=" and not (index > 0 and expression[index - 1] in {":", "!", "<", ">"}):
            return expression[:index], expression[index + 1 :]
    return None


def _reflexive_helper_statement(source: str) -> bool:
    """Return whether a declaration merely states ``P ↔ P`` or ``x = x``."""
    sanitized = _strip_lean_comments_and_strings(source)
    proof = re.search(r":=\s*by\b", sanitized)
    if proof is None:
        return False
    header = sanitized[: proof.start()]
    stack: list[str] = []
    last_conclusion_colon = -1
    opening_to_closing = {"(": ")", "[": "]", "{": "}", "⦃": "⦄", "⟨": "⟩"}
    closing = {value: key for key, value in opening_to_closing.items()}
    for index, char in enumerate(header):
        if char in opening_to_closing:
            stack.append(char)
        elif char in closing:
            if stack and stack[-1] == closing[char]:
                stack.pop()
        elif char == ":" and not stack:
            last_conclusion_colon = index
    if last_conclusion_colon < 0:
        return False
    sides = _top_level_relation_sides(header[last_conclusion_colon + 1 :])
    if sides is None:
        return False
    left, right = (" ".join(side.strip().split()) for side in sides)
    return bool(left and left == right)


def _is_lean_inspection_only_helper_candidate(source: str) -> bool:
    """Return whether helper source is a dummy wrapper for environment inspection."""
    replacement = str(source or "")
    declarations = list(_HELPER_DECLARATION_START_RE.finditer(replacement))
    if _LEAN_INSPECTION_COMMAND_RE.search(replacement):
        return not declarations or all(
            _TRIVIAL_TRUE_DECLARATION_RE.search(match.group("header") or "")
            for match in declarations
        )
    if not declarations:
        return False
    for index, declaration in enumerate(declarations):
        name = str(declaration.group("name") or "")
        end = declarations[index + 1].start() if index + 1 < len(declarations) else len(replacement)
        declaration_source = replacement[declaration.start() : end].strip()
        # Reflexive facts elaborate but cannot discharge a distinct open
        # dependency. Treat them as non-advancing evidence regardless of the
        # model-authored name so they never reserve production integration.
        if _reflexive_helper_statement(declaration_source):
            continue
        # A helper that binds a declaration only to finish the proposition
        # ``True`` with ``trivial`` cannot provide reusable proof progress.
        # Classify this semantic shape before consulting naming conventions;
        # models otherwise evade the discovery budget by replacing ``probe``
        # with names such as ``try`` or ``candidate``.
        if _TRIVIAL_BINDING_PROBE_RE.search(declaration_source):
            continue
        if not _INSPECTION_DECLARATION_NAME_RE.search(name):
            return False
        if not (
            _FALSE_IDENTIFIER_PROBE_RE.search(declaration_source)
            # A common type-inspection idiom deliberately asks Lean to use an
            # existing declaration as a proof of ``True``.  The resulting type
            # mismatch is useful discovery evidence, never a reusable helper.
            or _TRUE_TERM_TYPE_PROBE_RE.search(declaration_source)
            # Inspection-named declarations with a deliberately trivial
            # conclusion are diagnostic wrappers even when setup begins with
            # ``letI``/``haveI`` or contains several nested local facts.  The
            # useful inner fact must be checked as its own proposition before
            # LeanFlow treats it as durable proof progress.
            or _TRIVIAL_TRUE_PROBE_RE.search(declaration_source)
            # Inspection-named helpers sometimes wrap a substantive target
            # solely to expose a local context or dependency type.  The
            # deliberate ``fail_if_success done`` plus terminal bare-term
            # mismatch makes this diagnostic regardless of the proposition.
            or _TRACED_FAILURE_PROBE_RE.search(declaration_source)
            # Another signature-discovery idiom binds an unapplied declaration
            # head and deliberately submits that function/type as the proof.
            # It is diagnostic when the helper is explicitly inspection-named,
            # even if the wrapper proposition itself is nontrivial.
            or _BOUND_DECLARATION_TYPE_PROBE_RE.search(declaration_source)
        ):
            return False
    return True


def _is_lean_inspection_only_target_candidate(source: str) -> bool:
    """Return whether assigned-target source contains temporary inspection commands.

    LeanFlow may run an instrumented replacement to expose proof state, but it
    must not count as a production proof attempt or pass the target gate until
    the model resubmits a clean declaration.
    """
    sanitized = _strip_lean_comments_and_strings(str(source or ""))
    return bool(
        _LEAN_INSPECTION_COMMAND_RE.search(sanitized)
        or _STANDALONE_TRACE_STATE_RE.search(sanitized)
        or _BOUND_DECLARATION_TYPE_PROBE_RE.search(sanitized)
    )


def _next_significant_character(text: str, start: int) -> tuple[int, str] | None:
    """Return the next source character outside Lean comments and strings."""
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if char == "-" and text.startswith("--", index):
            newline = text.find("\n", index)
            index = length if newline < 0 else newline + 1
            continue
        if char == "/" and text.startswith("/-", index):
            depth = 1
            index += 2
            while index < length and depth:
                if text.startswith("/-", index):
                    depth += 1
                    index += 2
                elif text.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue
        if char == '"':
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
            continue
        if char == "«":
            close = text.find("»", index + 1)
            index = length if close < 0 else close + 1
            continue
        return index, char
    return None


def _standalone_keyword_at(text: str, position: int, keyword: str) -> bool:
    """Return whether one source position starts a complete Lean keyword."""
    if not text.startswith(keyword, position):
        return False
    before = text[position - 1] if position else ""
    after_index = position + len(keyword)
    after = text[after_index] if after_index < len(text) else ""
    identifier_chars = "_'"
    return not (before.isalnum() or before in identifier_chars) and not (
        after.isalnum() or after in identifier_chars
    )


def _type_assignment_keyword_at(text: str, position: int) -> bool:
    """Return whether one position starts a result-type assignment form."""
    return any(
        _standalone_keyword_at(text, position, keyword) for keyword in _TYPE_ASSIGNMENT_KEYWORDS
    )


def _declaration_statement_end(text: str) -> int:
    """Return the assignment starting a declaration body, or ``len(text)``.

    Top-level ``let`` assignments after the declaration's type colon belong to
    the result type. Count them before accepting the next assignment as the
    body marker, independent of whether the proof is a ``by`` block or term.
    """
    depth = 0
    index = 0
    seen_type_colon = False
    pending_type_let_assignments = 0
    while True:
        found = _next_significant_character(text, index)
        if found is None:
            return len(text)
        position, char = found
        if char in _DECLARATION_OPENERS:
            depth += 1
        elif char in _DECLARATION_CLOSERS:
            depth = max(0, depth - 1)
        elif (
            depth == 0
            and seen_type_colon
            and char in {"l", "h"}
            and _type_assignment_keyword_at(text, position)
        ):
            pending_type_let_assignments += 1
        elif depth == 0 and char == ":":
            if text.startswith(":=", position):
                if seen_type_colon and pending_type_let_assignments:
                    pending_type_let_assignments -= 1
                    index = position + 2
                    continue
                return position
            seen_type_colon = True
        index = position + 1


def declaration_statement_text(text: str) -> str:
    """Return one declaration through its complete statement, excluding its body."""
    declaration = str(text or "").strip()
    return declaration[: _declaration_statement_end(declaration)].strip()


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


def _contains_lean_suggestion_tactic(text: str) -> bool:
    """Return whether executable source contains a diagnostic suggestion tactic."""
    return bool(_lean_suggestion_tactic_markers(text))


def _lean_suggestion_tactic_markers(text: str) -> tuple[str, ...]:
    """Return normalized suggestion tactics present in executable Lean source."""
    sanitized = _strip_lean_comments_and_strings(str(text or ""))
    return tuple(
        " ".join(match.group(0).strip().split())
        for match in _SUGGESTION_TACTIC_RE.finditer(sanitized)
    )


def _text_has_theorem_or_lemma(text: str) -> bool:
    sanitized = _strip_lean_comments_and_strings(str(text or ""))
    return bool(
        re.search(
            r"^\s*(?:@[A-Za-z0-9_.]+\s+)*(?:theorem|lemma|example)\b", sanitized, flags=re.MULTILINE
        )
    )


def _text_has_sorry(text: str) -> bool:
    return bool(re.search(r"\bsorry\b", _strip_lean_comments_and_strings(str(text or ""))))


def _text_has_theorem_or_lemma_without_sorry(text: str) -> bool:
    for entry in _declaration_line_index_from_text(str(text or "")):
        kind = str(entry.get("kind", "") or "").strip().lower()
        if kind in {"theorem", "lemma", "example"} and not _text_has_sorry(
            str(entry.get("text", "") or "")
        ):
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
    block_comment_depth = 0
    delimiter_stack: list[str] = []
    in_line_comment = False
    in_string = False
    escaped = False
    visible_markers: list[tuple[int, int]] = []
    i = 0
    while i < len(text) - 1:
        ch = text[i]
        nxt = text[i + 1]
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if block_comment_depth:
            if ch == "/" and nxt == "-":
                block_comment_depth += 1
                i += 2
                continue
            if ch == "-" and nxt == "/":
                block_comment_depth -= 1
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
            block_comment_depth = 1
            i += 2
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch in "([{":
            delimiter_stack.append(ch)
            i += 1
            continue
        if ch in ")]}" and delimiter_stack:
            expected = {")": "(", "]": "[", "}": "{"}[ch]
            if delimiter_stack[-1] == expected:
                delimiter_stack.pop()
            i += 1
            continue
        if ch == ":" and nxt == "=":
            visible_markers.append((i, len(delimiter_stack)))
            i += 2
            continue
        i += 1
    if not visible_markers:
        return -1
    for marker, delimiter_depth in visible_markers:
        if delimiter_depth:
            continue
        suffix = text[marker + 2 :].lstrip()
        if re.match(r"by\b", suffix):
            return marker
    return visible_markers[0][0]


def _statement_signature_text(text: str) -> str:
    """Return a declaration slice through its top-level assignment marker.

    The proof body is irrelevant to statement-fidelity review and changes on
    nearly every prover attempt. Trimming at the comment/string-aware ``:=``
    marker makes the audit hash stable until the declaration itself is re-stated.
    """
    proposed = str(text or "").strip()
    marker = _find_assignment_marker_for_statement(proposed)
    if marker < 0:
        return proposed
    return proposed[:marker].rstrip()


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
    end = len(lines) if not next_start else max(start, min(len(lines), next_start - 1))
    idx = end

    def _skip_standalone_attribute(value: int) -> int:
        """Skip one comment-aware standalone attribute suffix, including multiline forms."""
        if value < start:
            return value
        region_start = start - 1
        sanitized = _strip_lean_comments_and_strings(
            "\n".join(lines[region_start:value])
        ).splitlines()
        if not sanitized:
            return value
        last = sanitized[-1].strip()
        if re.fullmatch(r"@[A-Za-z0-9_.]+", last):
            return value - 1
        if not last.endswith("]"):
            return value
        for candidate in range(len(sanitized) - 1, -1, -1):
            fragment = "\n".join(sanitized[candidate:]).strip()
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
                return region_start + candidate
            continue
        return value

    def _skip_blank_lines(value: int) -> int:
        while value >= start and not lines[value - 1].strip():
            value -= 1
        return value

    idx = _skip_blank_lines(idx)
    changed = True
    while changed and idx >= start:
        changed = False
        while idx >= start:
            attribute_start = _skip_standalone_attribute(idx)
            if attribute_start == idx:
                break
            idx = attribute_start
            changed = True
        idx = _skip_blank_lines(idx)
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
        while idx >= start and _SCOPED_COMMAND_WRAPPER_LINE_RE.match(lines[idx - 1]):
            idx -= 1
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
        region = "\n".join(lines[start - 1 : end]).strip()
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
