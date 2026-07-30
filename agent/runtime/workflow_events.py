"""Emit managed-workflow activity events and per-agent detail payloads."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from agent.accounting.redact import redact_sensitive_text

# Keep the original "run_agent" logger name so the failure-path debug record is byte-identical
# to before this code was extracted from run_agent.py (no behavior change).
logger = logging.getLogger("run_agent")

_API_REQUEST_PREVIEW_CHARS = 500
_API_REQUEST_RECENT_TOOLS = 8


def _json_sha256(value: Any) -> str:
    """Hash one JSON-compatible value without constructing its full serialization."""
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8", errors="replace"))
    return digest.hexdigest()


def _bounded_content_text(value: Any, *, limit: int) -> str:
    """Return a bounded text rendering of model content blocks."""
    if limit <= 0 or value is None:
        return ""
    if isinstance(value, str):
        # Include enough lookahead for the secret redactor to see a complete
        # ordinary credential even when it begins near the display boundary.
        lookahead = value[: max(limit * 2, limit + 200)]
        return redact_sensitive_text(lookahead)[:limit]
    if isinstance(value, Mapping):
        fragments: list[str] = []
        remaining = limit
        for key in ("text", "content", "output", "name", "type"):
            if key not in value:
                continue
            fragment = _bounded_content_text(value.get(key), limit=remaining)
            if fragment:
                fragments.append(fragment)
                remaining -= len(fragment) + 1
            if remaining <= 0:
                break
        return " ".join(fragments)[:limit]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        fragments = []
        remaining = limit
        for item in value:
            fragment = _bounded_content_text(item, limit=remaining)
            if fragment:
                fragments.append(fragment)
                remaining -= len(fragment) + 1
            if remaining <= 0:
                break
        return " ".join(fragments)[:limit]
    return redact_sensitive_text(str(value)[: max(limit * 2, limit + 200)])[:limit]


def _recent_message_tool_names(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return recent requested/result tool names in newest-first discovery order."""
    names: list[str] = []
    seen: set[str] = set()
    for message in reversed(messages):
        candidates: list[str] = []
        direct_name = str(message.get("name", "") or "").strip()
        if direct_name:
            candidates.append(direct_name)
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in reversed(tool_calls):
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function")
                if isinstance(function, Mapping):
                    name = str(function.get("name", "") or "").strip()
                else:
                    name = str(call.get("name", "") or "").strip()
                if name:
                    candidates.append(name)
        for name in candidates:
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
            if len(names) >= _API_REQUEST_RECENT_TOOLS:
                return names
    return names


def build_api_request_activity_details(
    messages: Sequence[Mapping[str, Any]],
    *,
    iteration: int,
    approx_tokens: int,
    total_chars: int,
    available_tools: Sequence[str],
) -> dict[str, Any]:
    """Build bounded API-request telemetry while preserving status-reader fields.

    Hash the complete request incrementally for correlation, but retain only a
    redacted preview of its final message. This prevents each activity event
    from duplicating the accumulated conversation.
    """
    roles = Counter(str(message.get("role", "unknown") or "unknown") for message in messages)
    last_message = messages[-1] if messages else {}
    last_content = last_message.get("content") if isinstance(last_message, Mapping) else None
    return {
        "activity_schema_version": 2,
        "iteration": int(iteration),
        "message_count": len(messages),
        "approx_tokens": int(approx_tokens),
        "total_chars": int(total_chars),
        "available_tools": [str(name) for name in available_tools],
        "message_roles": dict(sorted(roles.items())),
        "last_message_role": str(last_message.get("role", "") or ""),
        "last_message_preview": _bounded_content_text(
            last_content,
            limit=_API_REQUEST_PREVIEW_CHARS,
        ),
        "last_message_sha256": _json_sha256(last_message) if last_message else "",
        "message_history_sha256": _json_sha256(messages),
        "recent_tool_names": _recent_message_tool_names(messages),
    }


def _emit_workflow_event(event_type: str, message: str, **details: Any) -> None:
    if not (os.getenv("LEANFLOW_PROJECT_ROOT")):
        return
    try:
        from leanflow_cli.workflows.workflow_state import append_workflow_activity

        append_workflow_activity(event_type, message, **details)
    except Exception:
        logger.debug("Failed to append workflow event %s", event_type, exc_info=True)


def _workflow_agent_event_details(agent: Any, **details: Any) -> dict[str, Any]:
    payload = dict(details)
    payload.setdefault("agent_session_id", str(getattr(agent, "session_id", "") or ""))
    payload.setdefault(
        "parent_agent_session_id",
        str(getattr(agent, "_parent_session_id", "") or ""),
    )
    try:
        payload.setdefault("delegate_depth", int(getattr(agent, "_delegate_depth", 0) or 0))
    except Exception:
        payload.setdefault("delegate_depth", 0)
    payload.setdefault("model", str(getattr(agent, "model", "") or ""))
    payload.setdefault("provider", str(getattr(agent, "provider", "") or ""))
    payload.setdefault("api_mode", str(getattr(agent, "api_mode", "") or ""))
    payload.setdefault("base_url", str(getattr(agent, "base_url", "") or ""))
    payload.setdefault("process_id", os.getpid())
    return payload
