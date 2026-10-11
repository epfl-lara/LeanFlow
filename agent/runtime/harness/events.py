"""Normalize native JSON events without inventing token usage or model outcomes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _count(value: Any) -> int | None:
    """Accept only nonnegative integer usage counters."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(value: Any, *, claude: bool = False) -> dict[str, int] | None:
    """Normalize complete native input/output counters, retaining cache subtotals."""
    if not isinstance(value, Mapping):
        return None
    incoming, outgoing = _count(value.get("input_tokens")), _count(value.get("output_tokens"))
    if incoming is None or outgoing is None:
        return None
    result = {"input_tokens": incoming, "output_tokens": outgoing}
    for key in (
        "cached_input_tokens",
        "cache_write_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "reasoning_output_tokens",
    ):
        count = _count(value.get(key))
        if count is not None:
            result[key] = count
    # Anthropic reports uncached input separately, unlike the Codex event schema.
    if claude:
        result["input_tokens"] += result.get("cache_read_input_tokens", 0) + result.get(
            "cache_creation_input_tokens", 0
        )
    result["total_tokens"] = result["input_tokens"] + outgoing
    return result


def usage_delta(
    total: dict[str, int] | None,
    previous: Mapping[str, Any] | None,
    *,
    resumed: bool,
    cumulative: bool,
) -> dict[str, int] | None:
    """Subtract a known resume baseline; unknown or decreasing totals stay unknown."""
    if total is None:
        return None
    if not cumulative or not resumed:
        return dict(total)
    baseline = normalize_usage(previous)
    if baseline is None:
        return None
    if any(total[key] < baseline[key] for key in ("input_tokens", "output_tokens")):
        return None
    # Cache/reasoning counters are optional across native versions. A newly
    # reported cumulative subtotal has no trustworthy delta without its baseline.
    return {
        key: value - baseline[key]
        for key, value in total.items()
        if key in baseline and value >= baseline[key]
    }


def normalize_claude_model_usage(value: Any) -> dict[str, int] | None:
    """Sum Claude's cumulative model totals, including compaction and sidechains.

    The native result.usage field covers only the main loop. Missing modelUsage
    cannot establish full invocation cost, so do not substitute that partial count.
    """
    if not isinstance(value, Mapping) or not value:
        return None
    fields = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "cacheReadInputTokens": "cache_read_input_tokens",
        "cacheCreationInputTokens": "cache_creation_input_tokens",
    }
    totals = dict.fromkeys(fields.values(), 0)
    reasoning = 0
    reasoning_known = True
    for model in value.values():
        if not isinstance(model, Mapping):
            return None
        for native, normalized in fields.items():
            count = _count(model.get(native))
            if count is None:
                return None
            totals[normalized] += count
        count = _count(model.get("thinkingTokens"))
        if count is None:
            reasoning_known = False
        else:
            reasoning += count
    if reasoning_known:
        totals["reasoning_output_tokens"] = reasoning
    return normalize_usage(totals, claude=True)


@dataclass
class EventState:
    """Collect authoritative terminal events while preserving unknown events for logs."""

    backend: str
    session_id: str = ""
    final_response: str = ""
    usage_total: dict[str, int] | None = None
    completed: bool = False
    error: str = ""
    diagnostics: list[str] = field(default_factory=list)

    def consume(self, event: dict[str, Any]) -> None:
        """Update state from recognized backend events without trusting tool success."""
        kind = event.get("type")
        if self.backend == "claude":
            if isinstance(event.get("session_id"), str):
                self.session_id = event["session_id"]
            if kind == "result":
                self.completed = (
                    not bool(event.get("is_error")) and event.get("subtype", "success") == "success"
                )
                self.final_response = str(event.get("result", ""))
                self.usage_total = normalize_claude_model_usage(event.get("modelUsage"))
                if not self.completed:
                    self.error = str(
                        event.get("errors")
                        or event.get("result")
                        or event.get("subtype")
                        or "Claude invocation failed"
                    )
            return
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            self.session_id = event["thread_id"]
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                self.final_response = str(item.get("text", ""))
        elif kind == "turn.completed":
            self.completed = True
            self.usage_total = normalize_usage(event.get("usage"))
        elif kind == "turn.failed":
            self.completed = False
            failure = event.get("error", {})
            self.error = (
                str(failure.get("message", failure)) if isinstance(failure, dict) else str(failure)
            )
        elif kind == "error":
            # The native harness may recover a stream error before turn.completed.
            self.diagnostics.append(str(event.get("message", "Native stream error")))
