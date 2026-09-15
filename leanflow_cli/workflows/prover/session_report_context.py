"""Preserve bounded, paired report evidence behind the current assignment on resume."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from core.utils import atomic_json_write
from leanflow_cli.workflows.prover.session_context import compact_history

_MAX_BYTES = 2 * 1024 * 1024
_MAX_MESSAGES = 4096
_NOTES_PREFIX = "Current proof notes:\n"
_SECRET_FIELDS = {
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "access_token",
    "refresh_token",
    "auth_token",
}


class ReportContextError(RuntimeError):
    """Stop provider dispatch when protected report evidence cannot be restored."""

    status = "environment_error"
    code = "report_context_invalid"
    scope = "job"


def _redact(value: Any) -> Any:
    """Redact evidence recursively without breaking its enclosing JSON structure."""
    if isinstance(value, str):
        return redact_sensitive_text(value, force=True)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "***" if str(key).lower() in _SECRET_FIELDS else _redact(item)
            for key, item in value.items()
        }
    return value


def _message_copy(message: dict[str, Any]) -> dict[str, Any]:
    """Copy textual evidence while preserving opaque provider continuation fields exactly."""
    result = copy.deepcopy(message)
    for field in ("content", "reasoning", "reasoning_content"):
        if field in result:
            result[field] = _redact(result[field])
    for call in result.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if isinstance(function, dict) and "arguments" in function:
            function["arguments"] = _redact(function["arguments"])
    # reasoning_details and codex_reasoning_items contain provider signatures and
    # encrypted continuation blobs. Editing those makes otherwise valid history fail.
    return result


def _paired_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep answered calls and their replies, including the completed part of a batch."""
    paired: list[dict[str, Any]] = []
    index = 0
    while index < len(history):
        message = history[index]
        index += 1
        if message.get("role") not in {"user", "assistant"}:
            continue
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            paired.append(_message_copy(message))
            continue
        replies: dict[str, list[dict[str, Any]]] = {}
        while index < len(history) and history[index].get("role") == "tool":
            reply = history[index]
            index += 1
            call_id = reply.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                replies.setdefault(call_id, []).append(reply)
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        selected, results = [], []
        seen: set[str] = set()
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id") or call.get("call_id")
            function = call.get("function")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in seen
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not function["name"]
                or len(replies.get(call_id, [])) != 1
            ):
                continue
            seen.add(call_id)
            selected.append(call)
            results.append(replies[call_id][0])
        if selected:
            partial = {**message, "tool_calls": selected}
            paired.append(_message_copy(partial))
            paired.extend(_message_copy(result) for result in results)
        elif message.get("content"):
            partial = {key: value for key, value in message.items() if key != "tool_calls"}
            paired.append(_message_copy(partial))
    return paired


def _valid_message(message: Any) -> bool:
    """Validate persisted message and call shapes before copying provider metadata."""
    if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
        return False
    if message.get("content") is not None and not isinstance(message["content"], str):
        return False
    if message["role"] == "tool" and not isinstance(message.get("tool_call_id"), str):
        return False
    if "tool_calls" not in message:
        return True
    if message["role"] != "assistant" or not isinstance(message["tool_calls"], list):
        return False
    return all(
        isinstance(call, dict)
        and isinstance(call.get("id") or call.get("call_id"), str)
        and isinstance(call.get("function"), dict)
        and isinstance(call["function"].get("name"), str)
        and isinstance(call["function"].get("arguments"), (str, dict))
        for call in message["tool_calls"]
    )


class SessionReportContext:
    """Checkpoint evidence outside the writable job workspace before report handoff.

    Save only the history tail. The current system message and assignment always
    remain authoritative on restore; a snapshot cannot reinstate an older plan.
    Unanswered calls are omitted from snapshots without changing the live batch.
    """

    def __init__(self, runtime_directory: Path, *, workspace: Path, token_budget: int) -> None:
        """Set fixed storage and token bounds for this job's report checkpoint."""
        self.path = runtime_directory / "report-context.json"
        self.workspace = workspace.resolve()
        self.token_budget = min(max(1, token_budget), (_MAX_BYTES - 1024) // 3)

    def _bounded(
        self, pinned: list[dict[str, Any]], history: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Combine current authority with paired evidence and bounded scratch notes."""
        tail = _paired_history(history[-(_MAX_MESSAGES - 1) :])
        notes_path = self.workspace / "PLAN_job.md"
        try:
            with notes_path.open(encoding="utf-8") as handle:
                notes = handle.read(16000)
        except FileNotFoundError:
            notes = ""
        if notes:
            tail = [
                item
                for item in tail
                if not (
                    item.get("role") == "user"
                    and str(item.get("content", "")).startswith(_NOTES_PREFIX)
                )
            ]
            tail.insert(0, {"role": "user", "content": _NOTES_PREFIX + _redact(notes)})
        messages, _ = compact_history(
            pinned + tail, context_tokens=self.token_budget, workspace=self.workspace
        )
        # compact_history may reread notes; redact those before persisting too.
        return pinned + _paired_history(messages[2:])

    def save(self, messages: list[dict[str, Any]]) -> None:
        """Atomically save bounded paired findings before making report-only mode durable."""
        if len(messages) < 2:
            raise ReportContextError("Report context requires a current system and assignment")
        bounded = self._bounded(messages[:2], messages[2:])
        payload = {"version": 1, "messages": bounded[2:]}
        if len(json.dumps(payload, ensure_ascii=False, indent=0).encode("utf-8")) > _MAX_BYTES:
            raise ReportContextError("The bounded report context exceeded its storage limit")
        atomic_json_write(self.path, payload, indent=0)

    def restore(self, current_pinned: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Restore validated evidence after fresh authority, without consulting old event logs."""
        if len(current_pinned) != 2:
            raise ReportContextError("Restore requires exactly the current system and assignment")
        history: list[dict[str, Any]] = []
        if self.path.exists():
            try:
                if self.path.stat().st_size > _MAX_BYTES:
                    raise ValueError("Report context exceeds its size bound")
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    not isinstance(saved, dict)
                    or type(saved.get("version")) is not int
                    or saved["version"] != 1
                    or not isinstance(saved.get("messages"), list)
                    or len(saved["messages"]) > _MAX_MESSAGES
                    or any(not _valid_message(item) for item in saved["messages"])
                ):
                    raise ValueError("Invalid report context structure")
                history = saved["messages"]
            except (OSError, ValueError, TypeError, UnicodeError) as exc:
                raise ReportContextError(
                    "Saved report context could not be restored; preserved findings require "
                    "inspection before another report request"
                ) from exc
        return self._bounded(current_pinned, history)
