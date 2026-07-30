"""Bound repeated non-progress Lean tool results within one theorem turn."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

STATE_KEY = "tool_result_loop_guard"
TRACKED_TOOLS = frozenset(
    {
        "lean_incremental_check:feedback",
        "lean_inspect",
        "lean_multi_attempt",
        "lean_proof_context",
    }
)
NUDGE_LIMIT = 3
HARD_LIMIT = 6


@dataclass(frozen=True)
class LoopDecision:
    """Describe the deterministic response to one repeated tool result."""

    tool_key: str = ""
    signature: str = ""
    streak: int = 0
    nudge: bool = False
    close_turn: bool = False


def tool_key(function_name: str, args: Mapping[str, Any] | None = None) -> str:
    """Return the tracked tool identity, including modes with different semantics."""
    name = str(function_name or "").strip()
    if name == "lean_incremental_check":
        action = (
            str(dict(args or {}).get("action", "check_target") or "check_target")
            .strip()
            .lower()
            .replace("-", "_")
        )
        name = f"{name}:{action}"
    return name if name in TRACKED_TOOLS else ""


def _single_line(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit]


def _first_diagnostic(payload: Mapping[str, Any]) -> str:
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            diagnostics = item.get("diagnostics")
            if not isinstance(diagnostics, list):
                continue
            for diagnostic in diagnostics:
                if not isinstance(diagnostic, Mapping):
                    continue
                severity = str(diagnostic.get("severity", "") or "").strip().lower()
                if severity and severity != "error":
                    continue
                message = _single_line(diagnostic.get("message", ""), 220)
                if not message:
                    continue
                line = int(diagnostic.get("line", 0) or 0)
                column = int(diagnostic.get("column", 0) or 0)
                return f"{message}|{line}:{column}"
    messages = payload.get("messages")
    if isinstance(messages, list):
        for diagnostic in messages:
            if not isinstance(diagnostic, Mapping):
                continue
            severity = str(diagnostic.get("severity", "") or "").strip().lower()
            if severity and severity != "error":
                continue
            message = _single_line(diagnostic.get("message", ""), 220)
            if not message:
                continue
            start = dict(diagnostic.get("file_start") or diagnostic.get("start") or {})
            line = int(start.get("line", 0) or 0)
            column = int(start.get("column", 0) or 0)
            return f"{message}|{line}:{column}"
    return ""


def result_signature(result_text: str) -> str:
    """Return a stable blocker fingerprint while discarding candidate verbosity."""
    text = str(result_text or "")
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
    if isinstance(decoded, Mapping):
        diagnostic = _first_diagnostic(decoded)
        fallback = (
            decoded.get("error")
            or decoded.get("action_required")
            or decoded.get("message")
            or decoded.get("status")
            or decoded.get("success")
        )
        material = "|".join(
            (
                str(decoded.get("backend_tool", "") or ""),
                str(decoded.get("status", "") or ""),
                diagnostic or _single_line(fallback, 260),
            )
        )
    else:
        material = _single_line(re.sub(r"\b\d+(?:\.\d+)?s\b", "<time>", text), 320)
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


def _made_progress(payload: Mapping[str, Any]) -> bool:
    verified_attempts = payload.get("verified_attempts")
    return bool(
        payload.get("target_verified")
        or payload.get("ok")
        or (isinstance(verified_attempts, list) and verified_attempts)
    )


def observe(
    state: dict[str, Any],
    *,
    function_name: str,
    args: Mapping[str, Any] | None,
    result_text: str,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
    nudge_limit: int = NUDGE_LIMIT,
    hard_limit: int = HARD_LIMIT,
) -> LoopDecision:
    """Track one assignment-local result and return nudge or handoff boundaries."""
    key = tool_key(function_name, args)
    if not key or not target_symbol or not active_file:
        return LoopDecision()
    try:
        payload = json.loads(str(result_text or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, Mapping) and _made_progress(payload):
        state.pop(STATE_KEY, None)
        return LoopDecision(tool_key=key)

    signature = result_signature(result_text)
    previous = dict(state.get(STATE_KEY) or {})
    identity = (
        target_symbol,
        active_file,
        source_revision_sha256,
        key,
        signature,
    )
    previous_identity = (
        str(previous.get("target_symbol", "") or ""),
        str(previous.get("active_file", "") or ""),
        str(previous.get("source_revision_sha256", "") or ""),
        str(previous.get("tool_key", "") or ""),
        str(previous.get("signature", "") or ""),
    )
    streak = int(previous.get("streak", 0) or 0) + 1 if identity == previous_identity else 1
    tracker = {
        "target_symbol": target_symbol,
        "active_file": active_file,
        "source_revision_sha256": source_revision_sha256,
        "tool_key": key,
        "signature": signature,
        "streak": streak,
    }
    state[STATE_KEY] = tracker

    bounded_nudge = max(2, int(nudge_limit))
    bounded_hard = max(bounded_nudge + 1, int(hard_limit))
    return LoopDecision(
        tool_key=key,
        signature=signature,
        streak=streak,
        nudge=streak == bounded_nudge,
        close_turn=streak >= bounded_hard,
    )
