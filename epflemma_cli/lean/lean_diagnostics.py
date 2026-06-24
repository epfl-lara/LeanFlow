"""Pure Lean diagnostic / blocker / goal text parsers extracted from lean_services (Phase 5).

This module collects the side-effect-free text parsers that turn raw Lean tool / build output
(JSON payloads or human-readable ``file:line:col: severity:`` lines) into structured diagnostic
records, plus the small classifiers built on top of them (actionable-failure detection, blocker
kind). Each function here is the fixpoint closure under "calls": its only non-stdlib callees are
other functions in this module. Nothing here invokes a Lean backend (MCP / REPL / Lake), touches
the filesystem, or reads module-mutable state — those callers stay in ``lean_services`` and reach
these helpers through the re-export shim.

Because this module imports ONLY stdlib (``re``, ``json``, ``typing``) and does NOT import
``lean_services`` or ``native_runner``, re-exporting these names back from ``lean_services``
introduces no import cycle. The many existing importers (``lean_tool``, ``native_runner``,
``native_utils``, ``doctor``, tests) keep resolving them as ``lean_services.<name>`` unchanged.

``diagnostic_items`` carries a recently-fixed anchored (``^...$`` + ``MULTILINE``) regex that
prevents catastrophic backtracking on long no-match lines; it is moved here VERBATIM.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

ACTIONABLE_DIAGNOSTIC_SEVERITIES = {"error", "warning"}

__all__ = [
    "ACTIONABLE_DIAGNOSTIC_SEVERITIES",
    "_coerce_positive_int",
    "_diagnostic_line_from_mapping",
    "_normalise_diagnostic_item",
    "_json_diagnostic_values",
    "_collect_diagnostic_items",
    "diagnostic_items",
    "actionable_diagnostic_items",
    "actionable_diagnostic_line_numbers",
    "diagnostics_indicate_actionable_failure",
    "_diagnostic_line_numbers",
    "_diagnostic_reason_for_entry",
    "classify_blocker_kind",
]


def _coerce_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if number > 0 else None


def _diagnostic_line_from_mapping(item: Mapping[str, Any]) -> int | None:
    for key in ("line", "startLine", "start_line"):
        line = _coerce_positive_int(item.get(key))
        if line is not None:
            return line
    location = item.get("location")
    if isinstance(location, Mapping):
        line = _diagnostic_line_from_mapping(location)
        if line is not None:
            return line
    range_value = item.get("range")
    if isinstance(range_value, Mapping):
        start = range_value.get("start")
        if isinstance(start, Mapping):
            line = _coerce_positive_int(start.get("line"))
            if line is not None:
                return line
    return None


def _normalise_diagnostic_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    has_diagnostic_shape = any(
        key in item for key in ("severity", "level", "kind", "message", "line", "range", "location")
    )
    if not has_diagnostic_shape:
        return None
    message = str(
        item.get("message", "") or item.get("text", "") or item.get("detail", "") or ""
    ).strip()
    severity = (
        str(item.get("severity", "") or item.get("level", "") or item.get("kind", "") or "")
        .strip()
        .lower()
    )
    lowered_message = message.lower()
    if not severity:
        if "error:" in lowered_message:
            severity = "error"
        elif "warning:" in lowered_message:
            severity = "warning"
        elif lowered_message:
            severity = "info"
    line = _diagnostic_line_from_mapping(item)
    if not severity and not message and line is None:
        return None
    return {"severity": severity, "message": message, "line": line}


def _json_diagnostic_values(text: str) -> list[Any]:
    stripped = str(text or "").strip()
    if not stripped:
        return []
    try:
        return [json.loads(stripped)]
    except Exception:
        values: list[Any] = []
        for line in stripped.splitlines():
            candidate = line.strip()
            if not candidate or candidate[0] not in "[{":
                continue
            try:
                values.append(json.loads(candidate))
            except Exception:
                continue
        return values


def _collect_diagnostic_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for child in value:
            items.extend(_collect_diagnostic_items(child))
        return items
    if not isinstance(value, Mapping):
        return []
    collected: list[dict[str, Any]] = []
    normalized = _normalise_diagnostic_item(value)
    if normalized is not None:
        collected.append(normalized)
    for key in ("items", "diagnostics", "messages", "errors", "warnings"):
        child = value.get(key)
        if isinstance(child, (list, Mapping)):
            collected.extend(_collect_diagnostic_items(child))
    return collected


def diagnostic_items(text: str) -> list[dict[str, Any]]:
    """Extract and deduplicate diagnostics from JSON payloads or file:line:col:severity:message text. Tries JSON parsing first, then falls back to anchored regex (^...$ MULTILINE) to parse diagnostics from structured text output; the anchoring prevents O(n^2) backtracking on long lines without diagnostic tokens."""
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None, str]] = set()
    for value in _json_diagnostic_values(text):
        for item in _collect_diagnostic_items(value):
            key = (
                str(item.get("severity", "") or ""),
                item.get("line") if isinstance(item.get("line"), int) else None,
                str(item.get("message", "") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
    if items:
        return items

    # Anchor each diagnostic at a line start (^...$ + MULTILINE). Without the anchor, finditer
    # re-attempts the lazy `.*?` prefix at every character offset, which is O(n^2) — and on a
    # single long line that carries `:line:col:` coordinates but no `error:`/`warning:` token
    # (e.g. a long goal-state / typeclass-trace line) it spins at ~100% CPU effectively forever.
    # The `^` anchor only changes WHERE finditer restarts (line boundaries vs every offset); it
    # does not change what any individual match consumes, so the parsed output is unchanged for
    # realistic diagnostics. (Note: `\s*` can still span a newline, exactly as before — so a
    # diagnostic whose message wraps to a continuation line parses identically to the old regex.)
    pattern = re.compile(
        r"^(?P<prefix>.*?):(?P<line>\d+):(?P<column>\d+):\s*(?P<severity>error|warning):\s*(?P<message>.*)$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    for match in pattern.finditer(text or ""):
        line = _coerce_positive_int(match.group("line"))
        items.append(
            {
                "severity": match.group("severity").lower(),
                "message": match.group("message").strip(),
                "line": line,
            }
        )
    return items


def actionable_diagnostic_items(text: str) -> list[dict[str, Any]]:
    return [
        item
        for item in diagnostic_items(text)
        if str(item.get("severity", "") or "").strip().lower() in ACTIONABLE_DIAGNOSTIC_SEVERITIES
    ]


def actionable_diagnostic_line_numbers(text: str) -> list[int]:
    values: list[int] = []
    for item in actionable_diagnostic_items(text):
        line = item.get("line")
        if isinstance(line, int) and line > 0 and line not in values:
            values.append(line)
    return values


def diagnostics_indicate_actionable_failure(text: str) -> bool:
    if actionable_diagnostic_items(text):
        return True
    if diagnostic_items(text):
        return False
    lowered = (text or "").lower()
    cleared_tokens = (
        "no errors found",
        "no errors",
        "without errors",
    )
    if any(token in lowered for token in cleared_tokens):
        lowered = (
            lowered.replace("no errors found", "")
            .replace("no errors", "")
            .replace("without errors", "")
        )
    failure_patterns = (
        r"\berror\b",
        r"\berrors\b",
        r"\bwarning\b",
        r"\bwarnings\b",
        r"\bsorry\b",
        r"\bunsolved\b",
        r"\bfailed\b",
        r"declaration uses sorry",
    )
    return any(re.search(pattern, lowered) for pattern in failure_patterns)


def _diagnostic_line_numbers(text: str) -> list[int]:
    actionable_lines = actionable_diagnostic_line_numbers(text)
    if actionable_lines or diagnostic_items(text):
        return actionable_lines
    values: list[int] = []
    patterns = (
        r":(\d+):\d+",
        r"\bline\s+(\d+)\b",
        r"""["']line["']\s*:\s*(\d+)""",
        r"\((\d+),\s*\d+\)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            try:
                value = int(match.group(1))
            except Exception:
                continue
            if value > 0 and value not in values:
                values.append(value)
    return values


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


def classify_blocker_kind(text: str) -> str:
    lowered = str(text or "").lower()
    if not lowered.strip():
        return "none"
    patterns = {
        "axiom-risk": ("axiom", "#print axioms", "classical.choice"),
        "unknown_ident": ("unknown constant", "unknown identifier", "unknown namespace"),
        "synth_instance": ("failed to synthesize", "type class", "instance"),
        "type_mismatch": ("type mismatch", "application type mismatch"),
        "timeout": ("timeout", "maximum recursion depth", "maximum number of heartbeats"),
        "open_goals": ("⊢", "unsolved goals", "goal"),
        "sorry": ("sorry",),
        "warnings": ("warning:",),
    }
    for name, tokens in patterns.items():
        if any(token in lowered for token in tokens):
            return name
    return "diagnostics"
