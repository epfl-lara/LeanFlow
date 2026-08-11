"""Pure activity/event-shaping helpers for managed-workflow status views.

These functions turn workflow activity events (and their tool-call / tool-result
payloads) into short human-facing preview strings and normalized status labels.
They are pure with respect to disk state — the only side-effecting dependency is
``load_config`` (for the activity-preview character budget) — so they live apart
from the persistent ``workflow_state`` module and are re-exported from it for
backwards compatibility.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from typing import Any

from leanflow_cli.config import load_config


def _activity_preview_limit(default: int = 420) -> int:
    try:
        logging_cfg = load_config().get("logging", {})
    except Exception:
        return default
    if not isinstance(logging_cfg, dict):
        return default
    try:
        value = int(logging_cfg.get("activity_preview_chars", default))
    except Exception:
        return default
    return value if value > 0 else default


def _shorten_text(text: Any, limit: int = 240) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _coerce_tool_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            payload = json.loads(arguments)
        except Exception:
            return {}
        if isinstance(payload, dict):
            return payload
    return {}


def _summarize_requested_tools(tool_calls: list[dict[str, Any]]) -> str:
    if not tool_calls:
        return ""
    previews: list[str] = []
    for call in tool_calls[:3]:
        name = str(call.get("name", "") or "").strip()
        if not name:
            continue
        args = _coerce_tool_arguments(call.get("arguments"))
        preview = _tool_call_preview(name, args)
        if preview:
            if name == "terminal":
                previews.append(f"Run {preview}")
            elif name in {"patch", "apply_verified_patch"}:
                previews.append(f"Edit {preview}")
            elif name == "read_file":
                previews.append(f"Read {preview}")
            else:
                previews.append(preview)
    if not previews:
        names = [
            str(call.get("name", "") or "")
            for call in tool_calls[:3]
            if str(call.get("name", "") or "")
        ]
        return f"Requested tools: {', '.join(names)}" if names else ""
    if len(tool_calls) == 1:
        return previews[0]
    return "Queued tools: " + "; ".join(previews)


def _agent_event_preview(event: Mapping[str, Any]) -> str:
    """Convert a workflow activity event into a human-facing preview string. Routes on event type (assistant-response, tool-call, tool-result, api-request, etc.) and extracts a concise snippet—content, reasoning, queued tools, error status—respecting the configured character budget; returns a formatted preview or generic fallback message."""
    details = event.get("details")
    details = details if isinstance(details, dict) else {}
    archived_preview = str(details.get("archived_preview", "") or "").strip()
    if archived_preview:
        return archived_preview
    event_type = str(event.get("type", "") or "")
    activity_limit = _activity_preview_limit()
    if event_type == "assistant-response":
        content = str(details.get("content", "") or "")
        if content.strip():
            return _shorten_text(content, limit=activity_limit)
        reasoning = str(details.get("reasoning_content", "") or details.get("reasoning", "") or "")
        if reasoning.strip():
            return "Reasoning: " + _shorten_text(reasoning, limit=max(activity_limit - 20, 40))
        tool_calls = details.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            normalized = [dict(call) for call in tool_calls if isinstance(call, dict)]
            requested = _summarize_requested_tools(normalized)
            if requested:
                return requested
    if event_type == "tool-call":
        tool_name = str(details.get("tool", "") or "")
        if tool_name:
            return _tool_call_preview(tool_name, details.get("arguments"))
    if event_type == "tool-result":
        tool_name = str(details.get("tool", "") or "")
        is_error = bool(details.get("is_error"))
        if tool_name:
            return _tool_result_preview(tool_name, details.get("result"), is_error=is_error)
    if event_type == "api-request":
        iteration = details.get("iteration")
        if iteration is not None:
            parts = [f"API step #{iteration}"]
            message_count = details.get("message_count")
            if message_count is not None:
                parts.append(f"{message_count} messages")
            approx_tokens = details.get("approx_tokens")
            if approx_tokens is not None:
                with contextlib.suppress(Exception):
                    parts.append(f"~{int(approx_tokens):,} tokens")
            return " · ".join(parts)
    if event_type == "conversation-start":
        prompt = _shorten_text(details.get("user_message", ""), limit=activity_limit)
        return f"Prompt: {prompt}" if prompt else str(event.get("message", "") or "")
    if event_type == "conversation-end":
        if details.get("interrupted"):
            return "Interrupted"
        if details.get("completed"):
            return "Completed"
    if event_type == "agent-input-queued":
        return f"Queued prompt: {_shorten_text(details.get('text', ''), limit=max(activity_limit - 20, 40))}"
    if event_type == "agent-awaiting-input":
        status = str(details.get("status", "") or "paused")
        return f"Waiting for input ({status})"
    if event_type == "agent-resume":
        return (
            _shorten_text(details.get("text", ""), limit=max(activity_limit - 20, 40))
            or "Processing queued prompt"
        )
    if event_type == "runner-exit":
        return (
            _shorten_text(event.get("message", ""), limit=max(activity_limit - 20, 40))
            or "Runner exited"
        )
    return _shorten_text(event.get("message", ""), limit=max(activity_limit - 20, 40))


def _tool_call_preview(tool_name: str, arguments: Any) -> str:
    activity_limit = _activity_preview_limit()
    args = _coerce_tool_arguments(arguments)
    if tool_name == "terminal":
        command = str(args.get("command", "") or "").strip()
        return _shorten_text(command or "Call terminal", limit=max(activity_limit - 20, 40))
    if tool_name in {"patch", "read_file", "write_file", "apply_verified_patch"}:
        path = str(args.get("path", "") or "").strip()
        mode = str(args.get("mode", "") or args.get("check_mode", "") or "").strip()
        if path and mode:
            return _shorten_text(f"{path} ({mode})", limit=max(activity_limit - 20, 40))
        if path:
            return _shorten_text(path, limit=max(activity_limit - 20, 40))
    path = str(args.get("path", "") or "").strip()
    if path:
        return _shorten_text(f"{tool_name}: {path}", limit=max(activity_limit - 20, 40))
    return f"Call {tool_name}"


def _tool_result_preview(tool_name: str, result: Any, *, is_error: bool) -> str:
    activity_limit = _activity_preview_limit()
    raw = str(result or "")
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None

    if isinstance(payload, dict):
        if tool_name == "terminal":
            exit_code = payload.get("exit_code")
            output = _shorten_text(payload.get("output", ""), limit=activity_limit + 40)
            if output:
                return f"exit {exit_code}: {output}" if exit_code is not None else output
            return f"{tool_name} {'failed' if is_error else 'completed'}"
        if tool_name in {"patch", "apply_verified_patch"}:
            success = payload.get("success")
            status = _shorten_text(payload.get("status", ""), limit=80)
            error = _shorten_text(payload.get("error", ""), limit=max(activity_limit - 100, 40))
            message = _shorten_text(payload.get("message", ""), limit=max(activity_limit - 100, 40))
            files_modified = payload.get("files_modified")
            modified_list = files_modified if isinstance(files_modified, list) else []
            if tool_name == "apply_verified_patch" and not modified_list and payload.get("path"):
                modified_list = [str(payload.get("path") or "")]
            first_file = str(modified_list[0] or "") if modified_list else ""
            diff = str(payload.get("diff", "") or "")
            if not diff and isinstance(payload.get("patch"), dict):
                diff = str(payload.get("patch", {}).get("diff", "") or "")
            hunk_count = diff.count("\n@@ ")
            if diff.startswith("@@ "):
                hunk_count += 1
            if success or status in {"patch_elaborated", "verified"}:
                summary_parts: list[str] = []
                if first_file:
                    summary_parts.append(_shorten_text(first_file, limit=100))
                if modified_list:
                    summary_parts.append(f"{len(modified_list)} file(s)")
                if hunk_count:
                    summary_parts.append(f"{hunk_count} hunk(s)")
                if tool_name == "apply_verified_patch":
                    summary_parts.append(
                        "target verified"
                        if payload.get("target_verified") is True
                        else "broad check passed"
                    )
                if summary_parts:
                    return "updated " + " · ".join(summary_parts)
                return "patch applied"
            if tool_name == "apply_verified_patch" and status:
                return f"checked patch {status}: {message or error or '[no details]'}"
            if error:
                return f"patch failed: {error}"
            return "patch failed"
        if tool_name == "read_file":
            total_lines = payload.get("total_lines")
            path = payload.get("path")
            if path and total_lines is not None:
                return f"{path} ({total_lines} lines)"
    return f"{tool_name} {'failed' if is_error else 'completed'}"


def _agent_status_from_live_phase(phase: str) -> str:
    normalized = str(phase or "").strip().lower()
    if normalized in {
        "starting",
        "reconciling",
        "busy",
        "verifying",
        "in-progress",
        "compacted",
    }:
        return "active"
    if normalized in {"blocked", "failed", "stalled"}:
        return "blocked"
    if normalized == "paused":
        return "paused"
    if normalized == "dead":
        return "dead"
    if normalized == "exited":
        return "exited"
    if normalized == "verified":
        return "completed"
    return ""
