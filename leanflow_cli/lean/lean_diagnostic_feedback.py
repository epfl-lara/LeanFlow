"""Convert Lean diagnostics and goals into per-declaration feedback signals."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_diagnostics import _goals_still_open  # noqa: F401
from leanflow_cli.lean.lean_services import (
    diagnostic_items,
    diagnostics_indicate_actionable_failure,
)
from leanflow_cli.native.native_utils import (
    _extract_diagnostic_line_numbers,
    _single_line,
)
from leanflow_cli.proof_state_builder import (
    _declaration_line_index,
    _find_declaration_entry,
)


def _declaration_prefix_text(active_file: str, label: str, *, max_lines: int = 200) -> str:
    entry = _find_declaration_entry(active_file, label)
    if not entry:
        return ""
    cutoff = int(entry.get("end_line", 0) or 0)
    if cutoff <= 0:
        return ""
    path = Path(active_file)
    try:
        all_lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    start = 1
    end = min(cutoff, len(all_lines))
    text = "\n".join(all_lines[:end]).strip()
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
        start = end - max_lines + 1
    return f"Current file prefix ending at `{label}` ({start}-{end}):\n{text}"


def _declaration_slice_text(active_file: str, label: str, *, max_lines: int = 40) -> str:
    entry = _find_declaration_entry(active_file, label)
    if not entry:
        return ""
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or 0)
    text = str(entry.get("text", "") or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + "\n-- [truncated declaration slice]"
    return f"Assigned declaration slice ({start}-{end}):\n{text}"


def _nearest_declaration_name(active_file: str, line_number: int | None) -> str:
    if not active_file or not isinstance(line_number, int) or line_number <= 0:
        return ""
    entries = _declaration_line_index(active_file)
    current = ""
    for entry in entries:
        if int(entry.get("line", 0) or 0) > line_number:
            break
        current = str(entry.get("name", "") or "")
    return current


def _queue_diagnostic_items(text: str) -> list[dict[str, Any]]:
    return [
        item
        for item in diagnostic_items(text)
        if str(item.get("severity", "") or "").strip().lower() == "error"
    ]


def _queue_diagnostic_line_numbers(text: str) -> list[int]:
    items = diagnostic_items(text)
    if items:
        values: list[int] = []
        for item in _queue_diagnostic_items(text):
            line = item.get("line")
            if isinstance(line, int) and line > 0 and line not in values:
                values.append(line)
        return values
    return _extract_diagnostic_line_numbers(text)


def _diagnostics_indicate_queue_blocker(text: str) -> bool:
    items = diagnostic_items(text)
    if items:
        return bool(_queue_diagnostic_items(text))
    lowered = (text or "").lower()
    cleared_tokens = (
        "no errors found",
        "no errors",
        "without errors",
    )
    if any(token in lowered for token in cleared_tokens):
        for token in cleared_tokens:
            lowered = lowered.replace(token, "")
    blocker_patterns = (
        r"\berror\b",
        r"\berrors\b",
        r"\bunsolved\b",
        r"\bfailed\b",
        r"\btype mismatch\b",
        r"\bunknown option\b",
    )
    return any(re.search(pattern, lowered) for pattern in blocker_patterns)


def _is_anonymous_declaration_label(label: str) -> bool:
    normalized = str(label or "").strip().lower()
    return normalized.startswith("[anonymous ")


def _declaration_name_safe_for_diagnostic_match(name: str) -> bool:
    normalized = str(name or "").strip()
    if not normalized or _is_anonymous_declaration_label(normalized):
        return False
    if len(normalized) >= 4:
        return True
    return bool(re.search(r"[^A-Za-z]", normalized))


def _diagnostic_reason_for_entry(entry: Mapping[str, Any], diagnostic_lines: list[int]) -> str:
    if not diagnostic_lines:
        return ""
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or start)
    if start <= 0:
        return ""
    for line_number in diagnostic_lines:
        if start <= int(line_number) <= max(start, end):
            return f"diagnostic near line {line_number}"
    return ""


def _declaration_diagnostic_feedback_reason(
    active_file: str,
    label: str,
    *texts: str,
    structured_items: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Return a human-readable diagnostic reason (error/warning message with line number) for a declaration, or empty string if none found. Prefers structured diagnostic items (from manager_check) to locate warnings that plain-text regex cannot; falls back to parsing text diagnostics in `<file>:<line>:<col>:` format."""
    entry = _find_declaration_entry(active_file, label)
    if not entry:
        return ""
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or start)
    if start <= 0:
        return ""

    def _structured_diagnostic_line(diagnostic: Mapping[str, Any]) -> int | None:
        for key in ("line", "start_line", "file_line"):
            value = diagnostic.get(key)
            if isinstance(value, int):
                return value
        for key in ("file_start", "start"):
            value = diagnostic.get(key)
            if isinstance(value, Mapping):
                line = value.get("line")
                if isinstance(line, int):
                    return line
        return None

    # Prefer the manager_check's structured messages when available. The text
    # fallbacks below only catch diagnostics that come in `<file>:<line>:<col>:`
    # form (lake / lean_inspect output); `lean_incremental_check` returns
    # warnings as plain `warning: ...` lines that the regex cannot locate, so
    # the structured path is the only way to honour the spec's per-theorem
    # warning-cleanup opportunity for warnings the targeted check surfaced.
    structured_in_range: list[tuple[int, Mapping[str, Any]]] = []
    for position, diagnostic in enumerate(structured_items or ()):
        if not isinstance(diagnostic, Mapping):
            continue
        line = _structured_diagnostic_line(diagnostic)
        if not (isinstance(line, int) and start <= line <= max(start, end)):
            continue
        severity = str(diagnostic.get("severity", "") or "diagnostic").strip().lower()
        if severity not in {"warning", "error"}:
            continue
        structured_in_range.append((position, diagnostic))
    structured_in_range.sort(
        key=lambda item: (
            0 if str(item[1].get("severity", "") or "").strip().lower() == "error" else 1,
            item[0],
        )
    )
    for _, diagnostic in structured_in_range:
        line = _structured_diagnostic_line(diagnostic)
        assert isinstance(line, int)
        severity = str(diagnostic.get("severity", "") or "diagnostic").strip().lower()
        message = _single_line(str(diagnostic.get("message", "") or ""), 180)
        return (
            f"{severity} near line {line}: {message}" if message else f"{severity} near line {line}"
        )
    for text in texts:
        if not text:
            continue
        parsed_items = diagnostic_items(text)
        parsed_items.sort(
            key=lambda diagnostic: (
                0 if str(diagnostic.get("severity", "") or "").strip().lower() == "error" else 1
            )
        )
        for diagnostic in parsed_items:
            line = diagnostic.get("line")
            if isinstance(line, int) and start <= line <= max(start, end):
                severity = str(diagnostic.get("severity", "") or "diagnostic").strip().lower()
                if severity not in {"warning", "error"}:
                    continue
                message = _single_line(str(diagnostic.get("message", "") or ""), 180)
                return (
                    f"{severity} near line {line}: {message}"
                    if message
                    else f"{severity} near line {line}"
                )
        if not parsed_items:
            lowered_text = text.lower()
            if re.search(r":\d+:\d+:\s*info:", lowered_text) and not re.search(
                r":\d+:\d+:\s*(?:warning|error):",
                lowered_text,
            ):
                continue
            reason = _diagnostic_reason_for_entry(entry, _extract_diagnostic_line_numbers(text))
            if reason:
                return reason
    return ""


def _diagnostics_indicate_failure(diagnostics: str) -> bool:
    return diagnostics_indicate_actionable_failure(diagnostics)


def _diagnostics_indicate_hard_failure(diagnostics: str) -> bool:
    items = diagnostic_items(diagnostics)
    if items:
        return any(str(item.get("severity", "") or "").strip().lower() == "error" for item in items)
    lowered = (diagnostics or "").lower()
    for token in ("no errors found", "no errors", "without errors"):
        lowered = lowered.replace(token, "")
    hard_patterns = (
        r"\berror\b",
        r"\berrors\b",
        r"\bunsolved\b",
        r"\btype mismatch\b",
        r"\bunknown constant\b",
        r"\bfailed to synthesize\b",
        r"\bdeterministic timeout\b",
        r"\bmaximum number of heartbeats\b",
        r"\btactic execution\b",
    )
    return any(re.search(pattern, lowered) for pattern in hard_patterns)
