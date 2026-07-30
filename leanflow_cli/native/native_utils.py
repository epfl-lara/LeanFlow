"""Provide shared text, JSON, path, and diagnostic helpers for native workflows."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.config import load_config
from leanflow_cli.lean.lean_services import (
    actionable_diagnostic_line_numbers,
    diagnostic_items,
)
from leanflow_cli.native.native_config import _project_root

__all__ = [
    "_logging_config",
    "_positive_int_config",
    "_single_line",
    "_message_text",
    "_collect_message_text",
    "_collect_assistant_report_text",
    "_diagnostic_counts_from_messages",
    "_extract_json_payload",
    "_bounded_verifier_response",
    "_relative_file_label",
    "_relative_project_file_label",
    "_normalize_blocker_summary",
    "_extract_blocker_summary",
    "_extract_diagnostics_summary",
    "_extract_next_steps",
    "_extract_recent_build_status",
    "_extract_diagnostic_line_numbers",
    "_format_diagnostic_for_model",
    "_format_declaration_queue",
    "_format_turns_for_snapshot",
]


def _logging_config() -> Mapping[str, Any]:
    try:
        config = load_config()
    except Exception:
        return {}
    logging_cfg = config.get("logging", {})
    return logging_cfg if isinstance(logging_cfg, dict) else {}


def _positive_int_config(name: str, default: int) -> int:
    try:
        value = int(_logging_config().get(name, default))
    except Exception:
        return default
    return value if value > 0 else default


def _single_line(text: Any, limit: int | None = None) -> str:
    effective_limit = (
        limit
        if limit is not None
        else max(_positive_int_config("activity_preview_chars", 420) + 140, 560)
    )
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= effective_limit:
        return collapsed
    return collapsed[: effective_limit - 3] + "..."


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def _collect_message_text(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(_message_text(message.get("content")) for message in messages if message)


def _collect_assistant_report_text(messages: list[dict[str, Any]]) -> str:
    """Return assistant-authored prose suitable for semantic report parsing.

    Tool results and runner-authored user prompts often embed workflow contracts
    containing words such as ``blocker`` or ``failed``. They are evidence inputs,
    not model blocker reports, so including them can turn a successful JSON tool
    response into the shell-visible mathematical blocker.
    """
    return _collect_message_text(
        [
            message
            for message in messages
            if str(message.get("role", "") or "").strip().lower() == "assistant"
        ]
    )


def _diagnostic_counts_from_messages(
    *,
    output: str = "",
    messages: Any = None,
) -> tuple[int, int, int]:
    items: list[Mapping[str, Any]] = []
    if isinstance(messages, list):
        items.extend(item for item in messages if isinstance(item, Mapping))
    items.extend(diagnostic_items(output))
    errors = 0
    warnings = 0
    sorry_count = 0
    for item in items:
        severity = str(item.get("severity", "") or "").strip().lower()
        message = str(item.get("message", "") or "").strip().lower()
        if severity == "error":
            errors += 1
        elif severity == "warning":
            warnings += 1
        # ``sorryAx`` is an axiom-profile name, not a source placeholder.
        # Count only Lean's standalone ``sorry`` token so policy diagnostics
        # cannot fabricate unresolved-source evidence.
        if re.search(r"\bsorry\b", message):
            sorry_count += 1
    return errors, warnings, sorry_count


def _extract_json_payload(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except Exception:
                continue
    return None


def _bounded_verifier_response(text: str, limit: int = 12000) -> str:
    raw = str(text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 48)].rstrip() + "\n\n[truncated by LeanFlow verifier logging]"


def _relative_file_label(active_file: str) -> str:
    if not active_file:
        return ""
    try:
        return str(Path(active_file).resolve().relative_to(Path(_project_root()).resolve()))
    except Exception:
        return active_file


def _relative_project_file_label(
    path: str | os.PathLike[str], project_root: str | os.PathLike[str] | None = None
) -> str:
    raw = Path(path)
    root = Path(project_root or _project_root())
    try:
        return str(raw.resolve().relative_to(root.resolve()))
    except Exception:
        return str(raw)


def _normalize_blocker_summary(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    lowered = normalized.lower()
    cleared_tokens = (
        "all blockers resolved",
        "none. all blockers resolved",
        "no blocker declared",
        "no blockers remain",
    )
    if any(token in lowered for token in cleared_tokens):
        return ""
    return normalized


def _extract_blocker_summary(text: str) -> str:
    lowered = text.lower()
    blocker_tokens = [
        "blocked",
        "blocker",
        "stuck",
        "cannot proceed",
        "can't proceed",
        "unable to",
        "failed to",
    ]
    if not any(token in lowered for token in blocker_tokens):
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines[-8:]):
        lowered_line = line.lower()
        if lowered_line.startswith("## "):
            continue
        if "no blocker declared" in lowered_line or "all blockers resolved" in lowered_line:
            continue
        if any(token in lowered_line for token in blocker_tokens):
            return line[:280]
    return ""


def _extract_diagnostics_summary(messages: list[dict[str, Any]]) -> str:
    snippets: list[str] = []
    for message in messages[-12:]:
        content = _message_text(message.get("content")).strip()
        if not content:
            continue
        lowered = content.lower()
        if any(
            token in lowered
            for token in ("error", "warning", "sorry", "no errors found", "diagnostic")
        ):
            snippets.append(content[:240])
    return "\n".join(snippets[:4])


def _extract_next_steps(summary_text: str) -> str:
    lines = [line.strip("- ").strip() for line in summary_text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if line.lower() == "## next steps" and idx + 1 < len(lines):
            return lines[idx + 1][:320]
    return ""


def _extract_recent_build_status(history: list[dict[str, Any]]) -> str:
    return ""


def _extract_diagnostic_line_numbers(text: str) -> list[int]:
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


def _format_diagnostic_for_model(item: Mapping[str, Any]) -> str:
    severity = str(item.get("severity", "") or "diagnostic").strip().lower()
    line = item.get("line")
    column = item.get("column")
    location = ""
    if isinstance(line, int) and line > 0:
        location = f" line {line}"
        if isinstance(column, int) and column > 0:
            location += f":{column}"
    message = _single_line(str(item.get("message", "") or ""), 260)
    return f"- {severity}{location}: {message}" if message else f"- {severity}{location}"


def _format_declaration_queue(queue: list[dict[str, Any]], *, limit: int = 8) -> str:
    if not queue:
        return "[none]"
    lines: list[str] = []
    for item in queue[:limit]:
        label = str(item.get("label", "") or "[unnamed]")
        reasons = ", ".join(item.get("reasons", []) or [])
        file_path = str(item.get("file", "") or "")
        try:
            file_label = (
                str(Path(file_path).resolve().relative_to(Path(_project_root()).resolve()))
                if file_path
                else ""
            )
        except Exception:
            file_label = file_path
        if file_label and label != file_label:
            lines.append(f"- {label} [{file_label}] — {reasons or 'pending'}")
        else:
            lines.append(f"- {label} — {reasons or 'pending'}")
    remaining = len(queue) - min(len(queue), limit)
    if remaining > 0:
        lines.append(f"- ... plus {remaining} more pending item(s)")
    return "\n".join(lines)


def _format_turns_for_snapshot(turns: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in turns:
        role = str(msg.get("role", "unknown") or "unknown").upper()
        content = _message_text(msg.get("content"))
        if len(content) > 2500:
            content = content[:1500] + "\n...[truncated]...\n" + content[-700:]
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            tool_names = []
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    tool_names.append(tool_call.get("function", {}).get("name", "?"))
                else:
                    tool_names.append(getattr(getattr(tool_call, "function", None), "name", "?"))
            content = f"{content}\n[Tool calls: {', '.join(tool_names)}]"
        parts.append(f"[{role}]\n{content}".strip())
    return "\n\n".join(parts).strip()
