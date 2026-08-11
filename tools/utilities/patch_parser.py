#!/usr/bin/env python3
"""
V4A Patch Format Parser

Parses the V4A patch format used by codex, cline, and other coding agents.

V4A Format:
    *** Begin Patch
    *** Update File: path/to/file.py
    @@ optional context hint @@
     context line (space prefix)
    -removed line (minus prefix)
    +added line (plus prefix)
    *** Add File: path/to/new.py
    +new file content
    +line 2
    *** Delete File: path/to/old.py
    *** Move File: old/path.py -> new/path.py
    *** End Patch

Usage:
    from tools.utilities.patch_parser import parse_v4a_patch, apply_v4a_operations

    operations, error = parse_v4a_patch(patch_content)
    if error:
        print(f"Parse error: {error}")
    else:
        result = apply_v4a_operations(operations, file_ops)
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.implementations.file_operations import PatchResult


class OperationType(Enum):
    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    MOVE = "move"


def _diff_display_path(file_path: str) -> str:
    text = str(file_path or "")
    try:
        path = Path(text).expanduser()
        if path.is_absolute():
            try:
                return str(path.resolve().relative_to(Path.cwd().resolve()))
            except Exception:
                return path.name
    except Exception:
        return text.lstrip("/")
    return text


@dataclass
class HunkLine:
    """A single line in a patch hunk."""

    prefix: str  # ' ', '-', or '+'
    content: str


@dataclass
class Hunk:
    """A group of changes within a file."""

    context_hint: str | None = None
    lines: list[HunkLine] = field(default_factory=list)


@dataclass
class PatchOperation:
    """A single operation in a V4A patch."""

    operation: OperationType
    file_path: str
    new_path: str | None = None  # For move operations
    hunks: list[Hunk] = field(default_factory=list)
    content: str | None = None  # For add file operations


def parse_v4a_patch(patch_content: str) -> tuple[list[PatchOperation], str | None]:
    """
    Parse a V4A format patch.

    Args:
        patch_content: The patch text in V4A format

    Returns:
        Tuple of (operations, error_message)
        - If successful: (list_of_operations, None)
        - If failed: ([], error_description)
    """
    lines = patch_content.split("\n")
    operations: list[PatchOperation] = []

    # Find patch boundaries
    start_idx = None
    end_idx = None

    for i, line in enumerate(lines):
        if "*** Begin Patch" in line or "***Begin Patch" in line:
            start_idx = i
        elif "*** End Patch" in line or "***End Patch" in line:
            end_idx = i
            break

    if start_idx is None:
        # Try to parse without explicit begin marker
        start_idx = -1

    if end_idx is None:
        end_idx = len(lines)

    # Parse operations between boundaries
    i = start_idx + 1
    current_op: PatchOperation | None = None
    current_hunk: Hunk | None = None

    while i < end_idx:
        line = lines[i]

        # Check for file operation markers
        update_match = re.match(r"\*\*\*\s*Update\s+File:\s*(.+)", line)
        add_match = re.match(r"\*\*\*\s*Add\s+File:\s*(.+)", line)
        delete_match = re.match(r"\*\*\*\s*Delete\s+File:\s*(.+)", line)
        move_match = re.match(r"\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)", line)

        if update_match:
            # Save previous operation
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.UPDATE, file_path=update_match.group(1).strip()
            )
            current_hunk = None

        elif add_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.ADD, file_path=add_match.group(1).strip()
            )
            current_hunk = Hunk()

        elif delete_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.DELETE, file_path=delete_match.group(1).strip()
            )
            operations.append(current_op)
            current_op = None
            current_hunk = None

        elif move_match:
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)
                operations.append(current_op)

            current_op = PatchOperation(
                operation=OperationType.MOVE,
                file_path=move_match.group(1).strip(),
                new_path=move_match.group(2).strip(),
            )
            operations.append(current_op)
            current_op = None
            current_hunk = None

        elif line.startswith("@@"):
            # Context hint / hunk marker
            if current_op:
                if current_hunk and current_hunk.lines:
                    current_op.hunks.append(current_hunk)

                # Extract context hint
                hint_match = re.match(r"@@\s*(.+?)\s*@@", line)
                hint = hint_match.group(1) if hint_match else None
                current_hunk = Hunk(context_hint=hint)

        elif current_op and line:
            # Parse hunk line
            if current_hunk is None:
                current_hunk = Hunk()

            if line.startswith("+"):
                current_hunk.lines.append(HunkLine("+", line[1:]))
            elif line.startswith("-"):
                current_hunk.lines.append(HunkLine("-", line[1:]))
            elif line.startswith(" "):
                current_hunk.lines.append(HunkLine(" ", line[1:]))
            elif line.startswith("\\"):
                # "\ No newline at end of file" marker - skip
                pass
            else:
                # Treat as context line (implicit space prefix)
                current_hunk.lines.append(HunkLine(" ", line))

        i += 1

    # Don't forget the last operation
    if current_op:
        if current_hunk and current_hunk.lines:
            current_op.hunks.append(current_hunk)
        operations.append(current_op)

    return operations, None


def preview_v4a_update(
    patch_content: str,
    current_content: str,
) -> tuple[str | None, str | None]:
    """Return the exact in-memory result of one V4A update operation.

    Use the strict hunk matcher so policy preflights can compare semantic source
    regions without writing the file or accepting a fuzzy relocation. Unknown,
    multi-file, and non-update patches return an error for the caller to handle
    conservatively.
    """
    from tools.utilities.fuzzy_match import STRICT_CONFIG

    operations, error = parse_v4a_patch(patch_content)
    if error:
        return None, error
    if len(operations) != 1 or operations[0].operation != OperationType.UPDATE:
        return None, "preview requires exactly one V4A update operation"

    new_content = current_content
    for hunk in operations[0].hunks:
        patched, hunk_error, _strategy, _similarity = _apply_hunk(
            new_content,
            hunk,
            STRICT_CONFIG,
        )
        if patched is None:
            return None, hunk_error or "could not apply update hunk"
        new_content = patched
    return new_content, None


def apply_v4a_operations(
    operations: list[PatchOperation], file_ops: Any, *, strict: bool = False
) -> "PatchResult":
    """
    Apply V4A patch operations using a file operations interface.

    Args:
        operations: List of PatchOperation from parse_v4a_patch
        file_ops: Object with read_file, write_file methods
        strict: When True, UPDATE hunks are exact-or-fail (no fuzzy relocation) — for
            high-risk edits that must never be applied to a merely-similar region.

    Returns:
        PatchResult with results of all operations
    """
    # Import here to avoid circular imports
    from tools.implementations.file_operations import PatchResult

    files_modified = []
    files_created = []
    files_deleted = []
    all_diffs = []
    errors = []
    # Track the least-confident fuzzy match across all UPDATE hunks (F2).
    matched_via: str | None = None
    worst_similarity: float | None = None

    for op in operations:
        try:
            if op.operation == OperationType.ADD:
                result = _apply_add(op, file_ops)
                if result[0]:
                    files_created.append(op.file_path)
                    all_diffs.append(result[1])
                else:
                    errors.append(f"Failed to add {op.file_path}: {result[1]}")

            elif op.operation == OperationType.DELETE:
                result = _apply_delete(op, file_ops)
                if result[0]:
                    files_deleted.append(op.file_path)
                    all_diffs.append(result[1])
                else:
                    errors.append(f"Failed to delete {op.file_path}: {result[1]}")

            elif op.operation == OperationType.MOVE:
                result = _apply_move(op, file_ops)
                if result[0]:
                    files_modified.append(f"{op.file_path} -> {op.new_path}")
                    all_diffs.append(result[1])
                else:
                    errors.append(f"Failed to move {op.file_path}: {result[1]}")

            elif op.operation == OperationType.UPDATE:
                ok, diff_or_err, strategy, similarity = _apply_update(op, file_ops, strict=strict)
                if ok:
                    files_modified.append(op.file_path)
                    all_diffs.append(diff_or_err)
                    # Report the least-confident hunk match for this patch so a
                    # fuzzy V4A hit is observable just like a replace-mode one.
                    if strategy is not None and (
                        worst_similarity is None
                        or (similarity is not None and similarity < worst_similarity)
                    ):
                        matched_via = strategy
                        worst_similarity = similarity
                else:
                    errors.append(f"Failed to update {op.file_path}: {diff_or_err}")

        except Exception as e:
            errors.append(f"Error processing {op.file_path}: {str(e)}")

    # Run lint on all modified/created files
    lint_results = {}
    for f in files_modified + files_created:
        if hasattr(file_ops, "_check_lint"):
            lint_result = file_ops._check_lint(f)
            lint_results[f] = lint_result.to_dict()

    combined_diff = "\n".join(all_diffs)

    if errors:
        return PatchResult(
            success=False,
            diff=combined_diff,
            files_modified=files_modified,
            files_created=files_created,
            files_deleted=files_deleted,
            lint=lint_results if lint_results else None,
            error="; ".join(errors),
            matched_via=matched_via,
            similarity=worst_similarity,
        )

    return PatchResult(
        success=True,
        diff=combined_diff,
        files_modified=files_modified,
        files_created=files_created,
        files_deleted=files_deleted,
        lint=lint_results if lint_results else None,
        matched_via=matched_via,
        similarity=worst_similarity,
    )


def _apply_add(op: PatchOperation, file_ops: Any) -> tuple[bool, str]:
    """Apply an add file operation."""
    # Extract content from hunks (all + lines)
    content_lines = []
    for hunk in op.hunks:
        for line in hunk.lines:
            if line.prefix == "+":
                content_lines.append(line.content)

    content = "\n".join(content_lines)

    result = file_ops.write_file(op.file_path, content)
    if result.error:
        return False, result.error

    diff = f"--- /dev/null\n+++ b/{op.file_path}\n"
    diff += "\n".join(f"+{line}" for line in content_lines)

    return True, diff


def _read_raw_for_guard(file_ops: Any, path: str) -> str | None:
    """Read raw file contents for guards without line-number decoration."""
    if hasattr(file_ops, "_exec") and hasattr(file_ops, "_escape_shell_arg"):
        result = file_ops._exec(f"cat {file_ops._escape_shell_arg(path)} 2>/dev/null")
        if result.exit_code == 0:
            return result.stdout
        return None

    read_result = file_ops.read_file(path, limit=10000)
    if getattr(read_result, "error", None):
        return None
    lines = []
    for line in str(getattr(read_result, "content", "")).split("\n"):
        match = re.match(r"^\s*\d+\|(.*)$", line)
        lines.append(match.group(1) if match else line)
    return "\n".join(lines)


def _lean_statement_delete_error(file_ops: Any, path: str, *, action: str) -> str | None:
    """Return a guard error for deleting or moving Lean statements."""
    from leanflow_cli.lean.lean_statement_guard import (
        should_guard_lean_statement_path,
        validate_lean_statement_edit,
    )

    if not should_guard_lean_statement_path(path):
        return None

    before = _read_raw_for_guard(file_ops, path)
    if before is None:
        return None

    result = validate_lean_statement_edit(before, "")
    if result.ok:
        return None

    if action == "move":
        details = "; ".join(v.replace("deleted ", "moved ") for v in result.violations)
        return (
            "Lean statement guard blocked this move: "
            f"{details}. Existing theorem/lemma/example statements may not be deleted, moved, "
            "renamed, or changed; edit proof bodies for existing declarations. New helper declarations are allowed."
        )
    return result.error


def _apply_delete(op: PatchOperation, file_ops: Any) -> tuple[bool, str]:
    """Apply a delete file operation."""
    # Read file first for diff
    read_result = file_ops.read_file(op.file_path)

    if read_result.error and "not found" in read_result.error.lower():
        # File doesn't exist, nothing to delete
        return True, f"# {op.file_path} already deleted or doesn't exist"

    guard_error = _lean_statement_delete_error(file_ops, op.file_path, action="delete")
    if guard_error:
        return False, guard_error

    # Delete directly via shell command using the underlying environment
    rm_result = file_ops._exec(f"rm -f {file_ops._escape_shell_arg(op.file_path)}")

    if rm_result.exit_code != 0:
        return False, rm_result.stdout

    diff = f"--- a/{op.file_path}\n+++ /dev/null\n# File deleted"
    return True, diff


def _apply_move(op: PatchOperation, file_ops: Any) -> tuple[bool, str]:
    """Apply a move file operation."""
    guard_error = _lean_statement_delete_error(file_ops, op.file_path, action="move")
    if guard_error:
        return False, guard_error

    # Use shell mv command
    mv_result = file_ops._exec(
        f"mv {file_ops._escape_shell_arg(op.file_path)} {file_ops._escape_shell_arg(op.new_path)}"
    )

    if mv_result.exit_code != 0:
        return False, mv_result.stdout

    diff = f"# Moved: {op.file_path} -> {op.new_path}"
    return True, diff


# Anchors that scope where a hunk applies. `@@ hint @@` is the explicit V4A form; a
# bare `def`/`class`/Lean-declaration line inside the hunk context acts as an implicit
# enclosing-declaration anchor when no `@@` hint is given.
_DECL_ANCHOR_RE = re.compile(
    r"^\s*(?:async\s+def\b|def\b|class\b|theorem\b|lemma\b|example\b|structure\b|inductive\b|instance\b)"
)


def _hunk_search_replace(hunk: Hunk) -> tuple[str, str]:
    """Return (search_pattern, replacement) for a hunk from its context/-/+ lines."""
    search_lines: list[str] = []
    replace_lines: list[str] = []
    for line in hunk.lines:
        if line.prefix == " ":
            search_lines.append(line.content)
            replace_lines.append(line.content)
        elif line.prefix == "-":
            search_lines.append(line.content)
        elif line.prefix == "+":
            replace_lines.append(line.content)
    return "\n".join(search_lines), "\n".join(replace_lines)


def _hunk_anchor(hunk: Hunk) -> str | None:
    """Return the anchor line that scopes this hunk, or None.

    Prefers the explicit `@@ hint @@`; otherwise falls back to the first
    enclosing-declaration context line (a `def`/`class`/Lean statement) so a hunk
    without a hint still gets located inside its own declaration rather than
    whole-file. Anchors let two identical bodies in different declarations be told apart.
    """
    if hunk.context_hint:
        return hunk.context_hint
    for line in hunk.lines:
        if line.prefix == " " and _DECL_ANCHOR_RE.match(line.content):
            return line.content.strip()
    return None


def _anchor_region(content: str, anchor: str) -> tuple[int, int] | None:
    """Return a (start, end) window around the anchor, or None if not found.

    The window runs from the anchor to just before the next declaration (or a bounded
    span), so a search/replace within it cannot escape into an unrelated region that
    happens to hold the same text. Prefer a whole-line match before the legacy
    substring fallback so an earlier comment or string mentioning the declaration
    cannot capture a hunk intended for the live declaration.
    """
    lines = content.split("\n")
    anchor_norm = anchor.strip()
    anchor_line = next(
        (i for i, line in enumerate(lines) if line.strip() == anchor_norm),
        None,
    )
    # Context hints are often intentionally abbreviated (for example
    # ``@@ def greet @@``), so preserve substring matching only when no exact
    # source line exists.
    if anchor_line is None:
        anchor_line = next(
            (i for i, line in enumerate(lines) if anchor_norm and anchor_norm in line),
            None,
        )
    if anchor_line is None:
        return None

    # Extend to the next top-level-ish declaration so the region is the anchor's block.
    end_line = len(lines)
    for j in range(anchor_line + 1, len(lines)):
        if _DECL_ANCHOR_RE.match(lines[j]) and not lines[j][:1].isspace():
            end_line = j
            break

    start = sum(len(line) + 1 for line in lines[:anchor_line])
    end = sum(len(line) + 1 for line in lines[:end_line])
    return start, min(end, len(content))


def _apply_hunk(
    content: str, hunk: Hunk, config: Any
) -> tuple[str | None, str | None, str | None, float | None]:
    """Apply one hunk to `content`, anchor-primary.

    Resolution:
      * If the hunk has a locatable anchor, that region is AUTHORITATIVE: resolve
        exact-first then the fuzzy chain strictly inside it, and never edit outside it.
      * Otherwise (no anchor), resolve against the whole file exact-first; a fuzzy hit is
        never taken when an exact match exists, and multiple exact matches are refused.

    Returns (new_content|None, error|None, strategy, similarity). new_content is None on
    failure; error then carries a near-miss snippet when the chain produced one.
    """
    from tools.utilities.fuzzy_match import (
        _strategy_exact,
        fuzzy_find_and_replace_ex,
    )

    search_pattern, replacement = _hunk_search_replace(hunk)
    if not search_pattern:
        return content, None, None, None

    anchor = _hunk_anchor(hunk)
    region = _anchor_region(content, anchor) if anchor else None

    # (A) ANCHORED: when the hunk carries a locatable `@@` / enclosing-declaration anchor, the
    # anchor is AUTHORITATIVE — resolve the hunk (exact-first, then the fuzzy chain) strictly
    # WITHIN the anchor region and NEVER fall back to the whole file. Otherwise a slightly-drifted
    # anchored hunk could edit an unrelated region that happens to still contain the old text.
    if region is not None:
        r_start, r_end = region
        window = content[r_start:r_end]
        window_exact_hits = _strategy_exact(window, search_pattern)
        if len(window_exact_hits) == 1:
            start, end = window_exact_hits[0]
            new_window = window[:start] + replacement + window[end:]
            return (
                content[:r_start] + new_window + content[r_end:],
                None,
                "exact",
                1.0,
            )

        # An implicit declaration anchor can be a trailing context line when the
        # edit inserts immediately before that declaration. Its region begins too
        # late to contain the preceding context, so accept only a unique exact
        # whole-file match before attempting anchored fuzzy recovery. Explicit
        # ``@@ hint @@`` anchors remain authoritative and never escape their region.
        if not hunk.context_hint and not window_exact_hits:
            whole_file_exact_hits = _strategy_exact(content, search_pattern)
            if len(whole_file_exact_hits) == 1:
                start, end = whole_file_exact_hits[0]
                return (
                    content[:start] + replacement + content[end:],
                    None,
                    "exact",
                    1.0,
                )
            if len(whole_file_exact_hits) > 1:
                return (
                    None,
                    (
                        f"Found {len(whole_file_exact_hits)} exact matches for the hunk; "
                        "add an explicit @@ anchor @@ or more context lines to make it unique."
                    ),
                    None,
                    None,
                )

        wm = fuzzy_find_and_replace_ex(window, search_pattern, replacement, config=config)
        if wm.count > 0 and wm.error is None:
            new_content = content[:r_start] + wm.content + content[r_end:]
            return new_content, None, wm.strategy, wm.similarity
        return (
            None,
            f"Could not apply hunk within its `@@` anchor region: {wm.error or 'no match'}",
            None,
            None,
        )

    # (B) UNANCHORED: resolve against the whole file, exact-first. A single exact match wins; a
    # fuzzy hit is never taken when an exact match exists; multiple exact matches are ambiguous
    # and refused rather than guessed.
    exact_hits = _strategy_exact(content, search_pattern)
    if len(exact_hits) == 1:
        start, end = exact_hits[0]
        return content[:start] + replacement + content[end:], None, "exact", 1.0
    if len(exact_hits) > 1:
        return (
            None,
            (
                f"Found {len(exact_hits)} exact matches for the hunk; add a distinguishing "
                "@@ anchor @@ or more context lines to make it unique."
            ),
            None,
            None,
        )
    fm = fuzzy_find_and_replace_ex(content, search_pattern, replacement, config=config)
    if fm.count > 0 and fm.error is None:
        return fm.content, None, fm.strategy, fm.similarity

    return None, f"Could not apply hunk: {fm.error}", None, None


def _apply_update(
    op: PatchOperation, file_ops: Any, *, strict: bool = False
) -> tuple[bool, str, str | None, float | None]:
    """Apply an update file operation, anchor-primary with a strictness ladder.

    Each hunk is located FIRST inside its anchor region (explicit `@@` hint or enclosing
    declaration) and applied there, with an exact whole-file pass ahead of any fuzzy
    strategy; hunks apply top-down over the running content. `strict=True` forbids
    fuzzy strategies (exact-or-fail) for high-risk edits.

    Returns (ok, diff_or_error, matched_via, similarity). The last two surface the
    least-confident strategy used across the file's hunks (F2 observability); they are
    None when nothing matched fuzzily (e.g. a pure exact/structural hit).
    """
    from tools.utilities.fuzzy_match import DEFAULT_CONFIG, STRICT_CONFIG

    # Read current content RAW. The previous path read line-number-decorated content with a
    # 10000-line cap and then stripped "NNN|" prefixes — which silently lost data on files larger
    # than 10000 lines and corrupted any genuine source line shaped like "  12|x" (Lean tables,
    # comments, Vector literals). `_read_raw_for_guard` cats the file with no cap and no
    # decorate/strip round trip (falling back to the old behavior only when `_exec` is unavailable).
    current_content = _read_raw_for_guard(file_ops, op.file_path)
    if current_content is None:
        return False, f"Cannot read file: {op.file_path}", None, None

    config = STRICT_CONFIG if strict else DEFAULT_CONFIG

    # Apply hunks top-down over the running content (offsets fall out naturally because
    # each hunk re-locates against the already-patched text).
    new_content = current_content
    matched_via: str | None = None
    worst_similarity: float | None = None

    for hunk in op.hunks:
        patched, error, strategy, similarity = _apply_hunk(new_content, hunk, config)
        if patched is None:
            return False, error or "Could not apply hunk", None, None
        new_content = patched

        # Track the least-confident matched strategy across hunks.
        if strategy is not None and (
            worst_similarity is None or (similarity is not None and similarity < worst_similarity)
        ):
            matched_via = strategy
            worst_similarity = similarity

    # Write new content
    write_result = file_ops.write_file(op.file_path, new_content)
    if write_result.error:
        return False, write_result.error, None, None

    # Generate diff
    import difflib

    diff_lines = difflib.unified_diff(
        current_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{_diff_display_path(op.file_path)}",
        tofile=f"b/{_diff_display_path(op.file_path)}",
    )
    diff = "".join(diff_lines)

    return True, diff, matched_via, worst_similarity
