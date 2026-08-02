"""Bound repeated non-progress Lean tool results within one theorem turn."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

STATE_KEY = "tool_result_loop_guard"
ADVISOR_STATE_KEY = "advisor_failure_loop_guard"
TRACKED_TOOLS = frozenset(
    {
        "lean_incremental_check:check_helper",
        "lean_incremental_check:feedback",
        "lean_inspect",
        "lean_multi_attempt",
        "lean_outline",
        "lean_proof_context",
        "lean_advisor",
        "terminal",
    }
)
NUDGE_LIMIT = 3
HARD_LIMIT = 6
OUTLINE_NUDGE_LIMIT = 8
OUTLINE_HARD_LIMIT = 16
ADVISOR_NUDGE_LIMIT = 2
ADVISOR_HARD_LIMIT = 3
_ADVISOR_TOOL_NAMES = frozenset({"lean_reasoning_help", "lean_decompose_helpers"})


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
    if name in _ADVISOR_TOOL_NAMES:
        return "lean_advisor"
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


def _multi_attempt_site_signature(args: Mapping[str, Any] | None) -> str:
    """Return a candidate-insensitive fingerprint for one tactic-screening site."""
    payload = dict(args or {})
    material = "|".join(
        (
            "lean_multi_attempt",
            str(payload.get("file_path", "") or "").strip(),
            str(payload.get("line", 0) or 0),
            str(payload.get("column", 0) or 0),
        )
    )
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


def _helper_candidate_statement_signature(args: Mapping[str, Any] | None) -> str:
    """Return a proof-insensitive fingerprint for checked helper statements."""
    replacement = str(dict(args or {}).get("replacement", "") or "")
    declaration_starts = list(
        re.finditer(
            r"(?m)^\s*(?:private\s+)?" r"(?:theorem|lemma|example|def|instance|class|structure)\b",
            replacement,
        )
    )
    statements: list[str] = []
    for index, start in enumerate(declaration_starts):
        end = (
            declaration_starts[index + 1].start()
            if index + 1 < len(declaration_starts)
            else len(replacement)
        )
        block = replacement[start.start() : end]
        proof = re.search(r"\s*:=\s*by\b", block)
        statement = block[: proof.start()] if proof else block
        normalized = " ".join(statement.split())
        if normalized:
            statements.append(normalized)
    material = "\n".join(statements) or " ".join(replacement.split())
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


def _made_progress(payload: Mapping[str, Any]) -> bool:
    verified_attempts = payload.get("verified_attempts")
    return bool(
        payload.get("target_verified")
        or payload.get("ok")
        or (isinstance(verified_attempts, list) and verified_attempts)
    )


def _advisor_failed(payload: Mapping[str, Any]) -> bool:
    """Return whether an advisor result supplied no usable answer."""
    if payload.get("success") is True:
        return False
    status = str(payload.get("status", "") or "").strip().lower()
    return payload.get("success") is False or status in {
        "error",
        "invalid_json",
        "no_answer",
        "timeout",
        "unavailable",
    }


def _terminal_policy_denied(payload: Mapping[str, Any]) -> bool:
    """Return whether the terminal was deterministically denied before execution."""
    status = str(payload.get("status", "") or "").strip().lower()
    return status.endswith("_terminal_denied") or status in {
        "clean_room_policy_denied",
        "terminal_policy_denied",
    }


def advisor_preflight_blocked(
    state: Mapping[str, Any],
    *,
    function_name: str,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
) -> bool:
    """Return whether two unchanged-source advisor failures already occurred."""
    if str(function_name or "").strip() not in _ADVISOR_TOOL_NAMES:
        return False
    previous = dict(state.get(ADVISOR_STATE_KEY) or {})
    return bool(
        str(previous.get("target_symbol", "") or "") == str(target_symbol or "")
        and str(previous.get("active_file", "") or "") == str(active_file or "")
        and str(previous.get("source_revision_sha256", "") or "")
        == str(source_revision_sha256 or "")
        and str(previous.get("tool_key", "") or "") == "lean_advisor"
        and int(previous.get("streak", 0) or 0) >= ADVISOR_NUDGE_LIMIT
    )


def hydrate_advisor_failure_streak(
    state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
    streak: int = ADVISOR_NUDGE_LIMIT,
) -> None:
    """Restore a durable advisor streak into the process-local loop guard."""
    incoming: dict[str, Any] = {
        "target_symbol": str(target_symbol or ""),
        "active_file": str(active_file or ""),
        "source_revision_sha256": str(source_revision_sha256 or ""),
        "tool_key": "lean_advisor",
        "signature": "unchanged-source-advisor-failure",
        "streak": max(0, int(streak)),
    }
    previous = dict(state.get(ADVISOR_STATE_KEY) or {})
    same_identity = all(
        str(previous.get(key, "") or "") == str(incoming[key])
        for key in (
            "target_symbol",
            "active_file",
            "source_revision_sha256",
            "tool_key",
            "signature",
        )
    )
    if same_identity:
        incoming["streak"] = max(
            int(incoming["streak"]),
            max(0, int(previous.get("streak", 0) or 0)),
        )
    state[ADVISOR_STATE_KEY] = incoming


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
    if key == "lean_advisor":
        if not isinstance(payload, Mapping) or not _advisor_failed(payload):
            state.pop(ADVISOR_STATE_KEY, None)
            previous = dict(state.get(STATE_KEY) or {})
            if str(previous.get("tool_key", "") or "") == "lean_advisor":
                state.pop(STATE_KEY, None)
            return LoopDecision(tool_key=key)
    elif key == "terminal":
        if not isinstance(payload, Mapping) or not _terminal_policy_denied(payload):
            state.pop(STATE_KEY, None)
            return LoopDecision(tool_key=key)
    elif isinstance(payload, Mapping) and _made_progress(payload):
        state.pop(STATE_KEY, None)
        return LoopDecision(tool_key=key)

    # Varying candidate text and backend rejection shapes do not constitute
    # progress when the model keeps screening the same unchanged proof site.
    # Other tools retain their diagnostic-sensitive blocker fingerprint.
    if key == "lean_advisor":
        # Reasoning and decomposition advisors share one expensive provider
        # family. Alternating them after equivalent failures is not progress.
        signature = "unchanged-source-advisor-failure"
    elif key == "terminal":
        # Varying a forbidden Python/shell command does not make a fresh route.
        signature = "unchanged-source-terminal-policy-denial"
    elif key == "lean_multi_attempt":
        signature = _multi_attempt_site_signature(args)
    elif key == "lean_incremental_check:check_helper":
        signature = _helper_candidate_statement_signature(args)
    elif key == "lean_outline":
        # Different symbols can still form one inspection cycle. Count the
        # whole unchanged-source sequence instead of waiting for an exact
        # consecutive symbol repeat.
        signature = "unchanged-source-outline-budget"
    else:
        signature = result_signature(result_text)
    tracker_state_key = ADVISOR_STATE_KEY if key == "lean_advisor" else STATE_KEY
    previous = dict(state.get(tracker_state_key) or {})
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
    state[tracker_state_key] = tracker

    if key == "lean_outline":
        bounded_nudge = max(2, OUTLINE_NUDGE_LIMIT)
        bounded_hard = max(bounded_nudge + 1, OUTLINE_HARD_LIMIT)
    elif key == "lean_advisor":
        bounded_nudge = ADVISOR_NUDGE_LIMIT
        bounded_hard = ADVISOR_HARD_LIMIT
    else:
        bounded_nudge = max(2, int(nudge_limit))
        bounded_hard = max(bounded_nudge + 1, int(hard_limit))
    return LoopDecision(
        tool_key=key,
        signature=signature,
        streak=streak,
        nudge=streak == bounded_nudge,
        close_turn=streak >= bounded_hard,
    )
