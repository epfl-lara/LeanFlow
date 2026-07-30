"""Render tool arguments and results as bounded, readable log lines.

The helpers are independent of ``AIAgent`` state and remain re-exported from
``run_agent`` for compatibility with existing integrations.
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

__all__ = [
    "_wrap_log_text",
    "_truncate_log_lines",
    "_summarize_arg_value",
    "_format_tool_args_for_log",
    "_format_tool_result_for_log",
    "_format_tool_result_for_log_with_limits",
]


def _wrap_log_text(text: str, width: int = 96) -> list[str]:
    lines: list[str] = []
    for raw_line in str(text).splitlines() or [""]:
        wrapped = textwrap.wrap(
            raw_line,
            width=width,
            replace_whitespace=False,
            drop_whitespace=False,
            break_long_words=False,
            break_on_hyphens=False,
        )
        lines.extend(wrapped or [""])
    return lines or [""]


def _truncate_log_lines(
    lines: list[str],
    *,
    head: int,
    tail: int = 0,
    truncated_label: str = "output truncated",
) -> list[str]:
    """Keep the start and optional end of long log blocks."""
    if len(lines) <= head + tail:
        return lines
    kept = list(lines[:head])
    omitted = len(lines) - head - tail
    kept.append(f"  [{truncated_label}: {omitted} more line(s)]")
    if tail:
        kept.extend(lines[-tail:])
    return kept


def _summarize_arg_value(key: str, value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if key in {"old_string", "new_string", "patch", "patch_content", "content"}:
            line_count = value.count("\n") + 1 if value else 0
            return f"{len(value):,} chars across {line_count} line(s)"
        return value
    if isinstance(value, list):
        return f"{len(value)} item(s)"
    if isinstance(value, dict):
        return f"{len(value)} field(s)"
    return str(value)


def _format_tool_args_for_log(function_name: str, function_args: dict[str, Any]) -> list[str]:
    if not function_args:
        return ["args: {}"]

    preferred_order = [
        "path",
        "command",
        "workdir",
        "query",
        "pattern",
        "file_glob",
        "target",
        "mode",
        "old_string",
        "new_string",
        "patch",
        "patch_content",
        "content",
    ]
    ordered_keys = [key for key in preferred_order if key in function_args]
    ordered_keys.extend(key for key in function_args if key not in ordered_keys)

    lines: list[str] = []
    for key in ordered_keys:
        value = function_args[key]
        if (
            isinstance(value, list)
            and value
            and all(not isinstance(item, (dict, list)) for item in value)
        ):
            lines.append(f"{key}: {len(value)} item(s)")
            for item in value[:12]:
                lines.extend([f"  - {part}" for part in _wrap_log_text(str(item), width=104)])
            if len(value) > 12:
                lines.append(f"  [{len(value) - 12} more item(s) omitted]")
            continue
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                summary = _summarize_arg_value(sub_key, sub_value)
                lines.extend(
                    [f"  {part}" for part in _wrap_log_text(f"{sub_key}: {summary}", width=104)]
                )
            continue
        summary = _summarize_arg_value(key, value)
        lines.extend(_wrap_log_text(f"{key}: {summary}", width=106))
    return lines


def _format_tool_result_for_log(function_name: str, function_result: str) -> list[str]:
    return _format_tool_result_for_log_with_limits(
        function_name,
        function_result,
        multiline_head=20,
        multiline_tail=8,
        wrapped_head=10,
        wrapped_tail=4,
        plain_head=18,
        plain_tail=6,
        string_char_threshold=600,
    )


def _format_tool_result_for_log_with_limits(
    function_name: str,
    function_result: str,
    *,
    multiline_head: int,
    multiline_tail: int,
    wrapped_head: int,
    wrapped_tail: int,
    plain_head: int,
    plain_tail: int,
    string_char_threshold: int,
) -> list[str]:
    """Format a tool result string for log display with configurable truncation per content type. Parses JSON dicts and lists to apply type-aware truncation (multiline strings, long strings, structured objects get independent head/tail limits); falls back to plain-text wrapping and truncation for non-JSON. Wraps all output lines at 104–106 chars."""
    try:
        parsed = json.loads(function_result)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        lines: list[str] = []
        preferred_order = [
            "success",
            "error",
            "exit_code",
            "output",
            "total_count",
            "count",
            "files",
            "matches",
        ]
        ordered_keys = [key for key in preferred_order if key in parsed]
        ordered_keys.extend(key for key in parsed if key not in ordered_keys)

        for key in ordered_keys:
            value = parsed[key]
            if (
                isinstance(value, list)
                and value
                and all(not isinstance(item, (dict, list)) for item in value)
            ):
                lines.append(f"{key}: {len(value)} item(s)")
                for item in value[:14]:
                    lines.extend([f"  - {part}" for part in _wrap_log_text(str(item), width=104)])
                if len(value) > 14:
                    lines.append(f"  [{len(value) - 14} more item(s) omitted]")
                continue
            if isinstance(value, str) and "\n" in value:
                value_lines = value.splitlines()
                lines.append(f"{key}:")
                wrapped_lines: list[str] = []
                for raw_line in value_lines:
                    wrapped_lines.extend(
                        [f"  {part}" for part in _wrap_log_text(raw_line, width=104)]
                    )
                lines.extend(
                    _truncate_log_lines(wrapped_lines, head=multiline_head, tail=multiline_tail)
                )
                continue
            if isinstance(value, str) and len(value) > string_char_threshold:
                wrapped = _wrap_log_text(value, width=104)
                lines.append(f"{key}:")
                for part in _truncate_log_lines(wrapped, head=wrapped_head, tail=wrapped_tail):
                    lines.append(f"  {part}")
                continue
            if isinstance(value, (dict, list)):
                pretty = json.dumps(value, indent=2, ensure_ascii=False)
                pretty_lines = pretty.splitlines()
                lines.append(f"{key}:")
                wrapped_pretty: list[str] = []
                for raw_line in pretty_lines:
                    wrapped_pretty.extend(
                        [f"  {part}" for part in _wrap_log_text(raw_line, width=104)]
                    )
                lines.extend(
                    _truncate_log_lines(
                        wrapped_pretty,
                        head=max(multiline_head - 4, 1),
                        tail=max(multiline_tail - 2, 0),
                        truncated_label="structured output truncated",
                    )
                )
                continue
            lines.extend(_wrap_log_text(f"{key}: {value}", width=106))
        return lines

    if isinstance(parsed, list):
        lines = [f"items: {len(parsed)}"]
        for item in parsed[:14]:
            lines.extend([f"  - {part}" for part in _wrap_log_text(str(item), width=104)])
        if len(parsed) > 14:
            lines.append(f"  [{len(parsed) - 14} more item(s) omitted]")
        return lines

    wrapped = _wrap_log_text(function_result, width=106)
    return _truncate_log_lines(wrapped, head=plain_head, tail=plain_tail)
