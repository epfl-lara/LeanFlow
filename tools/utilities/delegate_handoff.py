"""Build bounded evidence handoffs for interrupted delegated research turns."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from core.constants import WORKFLOW_STEP_BOUNDARY_INTERRUPT

HANDOFF_KIND = "managed_search_route_boundary"
MAX_EVIDENCE_ITEMS = 10
MAX_ARGUMENT_CHARS = 500
MAX_RESULT_CHARS = 1200
MAX_REASONING_ITEMS = 4
MAX_REASONING_CHARS = 500

# Only preserve outputs whose normal responsibility is mathematical grounding.
# In particular, terminal/file outputs are excluded because a generic child may
# accidentally print credentials or unrelated workspace data there.
_EVIDENCE_TOOLS = frozenset(
    {
        "lean_auto_search",
        "lean_axioms",
        "lean_decompose_helpers",
        "lean_incremental_check",
        "lean_inspect",
        "lean_lemma_suggest",
        "lean_multi_attempt",
        "lean_outline",
        "lean_proof_context",
        "lean_reasoning_help",
        "lean_search",
        "lean_sorries",
        "lean_verify",
        "web_download",
        "web_fetch",
        "web_search",
    }
)
_NON_EVIDENCE_KEYS = frozenset(
    {
        "backend",
        "cached",
        "degraded",
        "degraded_reasons",
        "duration",
        "elapsed",
        "error",
        "errors",
        "ok",
        "query",
        "status",
        "success",
        "truncated",
        "url",
    }
)


def _compact_text(value: Any, limit: int) -> str:
    """Return one bounded whitespace-normalized representation."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(value)
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)].rstrip() + "..."


def _parsed_json(value: Any) -> Any:
    """Return decoded JSON when available, otherwise the original value."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _meaningful_payload(value: Any, *, depth: int = 0) -> bool:
    """Return whether a tool result contains more than request/error metadata."""
    if depth >= 6:
        return bool(str(value or "").strip())
    if isinstance(value, Mapping):
        return any(
            _meaningful_payload(item, depth=depth + 1)
            for key, item in value.items()
            if str(key or "").strip().casefold() not in _NON_EVIDENCE_KEYS
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_meaningful_payload(item, depth=depth + 1) for item in value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if not text:
            return False
        return not any(
            marker in text
            for marker in (
                "no results found",
                "no matching results",
                "request failed",
                "tool error",
            )
        )
    return value is not None and value is not False


def _tool_call_id(call: Mapping[str, Any]) -> str:
    """Return the provider-neutral identifier for one tool call."""
    return str(call.get("id", "") or call.get("call_id", "") or "").strip()


def _bounded_selection(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep both early grounding and recent discoveries within the item cap."""
    if len(items) <= MAX_EVIDENCE_ITEMS:
        return items
    early_count = MAX_EVIDENCE_ITEMS // 2
    return items[:early_count] + items[-(MAX_EVIDENCE_ITEMS - early_count) :]


def _reasoning_summaries(message: Mapping[str, Any]) -> list[str]:
    """Return visible reasoning summaries without retaining encrypted payloads."""
    values: list[str] = []
    direct = message.get("reasoning")
    if isinstance(direct, str) and direct.strip():
        values.append(direct)
    raw_items = message.get("codex_reasoning_items")
    if not isinstance(raw_items, list):
        return values
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        raw_summary = raw_item.get("summary")
        if not isinstance(raw_summary, list):
            continue
        for raw_part in raw_summary:
            if not isinstance(raw_part, Mapping):
                continue
            text = raw_part.get("text")
            if isinstance(text, str) and text.strip():
                values.append(text)
    return values


def build_managed_interrupt_handoff(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact handoff for a managed step-boundary interruption.

    A handoff is emitted only when at least one completed grounding tool has a
    substantive result. Empty interruptions therefore retain their ordinary
    failure semantics instead of being promoted into fake research progress.
    """
    if not bool(result.get("interrupted")):
        return {}
    interrupt_message = str(result.get("interrupt_message", "") or "").strip()
    if interrupt_message != WORKFLOW_STEP_BOUNDARY_INTERRUPT:
        return {}
    messages = result.get("messages")
    if not isinstance(messages, list):
        return {}

    evidence_by_id: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    reasoning: list[str] = []
    for raw_message in messages:
        if not isinstance(raw_message, Mapping):
            continue
        role = str(raw_message.get("role", "") or "")
        if role == "assistant":
            for reason in _reasoning_summaries(raw_message):
                compact_reason = _compact_text(reason, MAX_REASONING_CHARS)
                if compact_reason and compact_reason not in reasoning:
                    reasoning.append(compact_reason)
            for raw_call in raw_message.get("tool_calls") or []:
                if not isinstance(raw_call, Mapping):
                    continue
                function = raw_call.get("function")
                function_map = dict(function) if isinstance(function, Mapping) else {}
                tool_name = str(function_map.get("name", "") or "").strip()
                call_id = _tool_call_id(raw_call)
                if tool_name not in _EVIDENCE_TOOLS or not call_id:
                    continue
                call_entry: dict[str, Any] = {
                    "tool": tool_name,
                    "arguments": _compact_text(
                        _parsed_json(function_map.get("arguments", "")),
                        MAX_ARGUMENT_CHARS,
                    ),
                }
                evidence_by_id[call_id] = call_entry
                ordered.append(call_entry)
        elif role == "tool":
            call_id = str(raw_message.get("tool_call_id", "") or "").strip()
            result_entry = evidence_by_id.get(call_id)
            if result_entry is None:
                continue
            content = raw_message.get("content", "")
            parsed = _parsed_json(content)
            if not _meaningful_payload(parsed):
                continue
            result_entry["result_excerpt"] = _compact_text(parsed, MAX_RESULT_CHARS)

    substantive = [item for item in ordered if str(item.get("result_excerpt", "") or "").strip()]
    if not substantive:
        return {}
    selected = _bounded_selection(substantive)
    return {
        "kind": HANDOFF_KIND,
        "boundary_marker": WORKFLOW_STEP_BOUNDARY_INTERRUPT,
        "completed_tool_calls": len(substantive),
        "evidence": selected,
        "reasoning": reasoning[-MAX_REASONING_ITEMS:],
    }
