"""Coalesce research events into safe-boundary orchestrator consultations."""

from __future__ import annotations

import threading
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

_LOCK = threading.RLock()
_SOURCE_CAP = 512

_SCOPE_KEY = "orchestrator_event_scope"
_PRODUCED_KEY = "orchestrator_event_watermark"
_ACKNOWLEDGED_KEY = "orchestrator_event_acknowledged"
_CAPTURED_KEY = "orchestrator_event_captured"
_SOURCES_KEY = "orchestrator_event_sources"
_REASONS_KEY = "orchestrator_event_reasons"
_FOREGROUND_GRACE_KEY = "orchestrator_event_foreground_grace"

# A pending research event may end the foreground model turn only after a tool
# whose completed callback cannot leave shared state half-mutated. Keep this
# fail-closed: a newly registered tool must be reviewed before it can become a
# routing boundary. In particular, coordination and dispatch tools are not
# safe merely because they do not edit the assigned Lean declaration.
_SAFE_POST_TOOL_BOUNDARIES = frozenset(
    {
        "formalization_document_inspect",
        "lean_auto_search",
        "lean_axioms",
        "lean_capabilities",
        "lean_decompose_helpers",
        "lean_inspect",
        "lean_lemma_suggest",
        "lean_multi_attempt",
        "lean_outline",
        "lean_proof_context",
        "lean_reasoning_help",
        "lean_search",
        "lean_sorries",
        "list_file_locks",
        "read_file",
        "read_pdf",
        "search_files",
        "session_search",
        "skill_view",
        "skills_list",
        "web_fetch",
        "web_search",
    }
)

# Basic orientation reads are safe places to harvest completed research, but
# ending a fresh prover turn after one of them wastes the model's setup work.
# Only substantive search/reasoning results justify an immediate reroute. A
# pending event observed after an orientation read stays staged and will be
# consumed at the next routing boundary or the natural end of the turn.
_ROUTING_POST_TOOL_BOUNDARIES = frozenset(
    {
        "formalization_document_inspect",
        "lean_auto_search",
        "lean_decompose_helpers",
        "lean_lemma_suggest",
        "lean_multi_attempt",
        "lean_reasoning_help",
        "lean_search",
        "read_pdf",
        "search_files",
        "session_search",
        "web_fetch",
        "web_search",
    }
)


@dataclass(frozen=True)
class EventCapture:
    """Describe the exact event prefix owned by one consultation."""

    watermark: int
    reasons: tuple[str, ...]


def is_safe_post_tool_boundary(function_name: str) -> bool:
    """Return whether a completed tool is a reviewed read/search boundary.

    Unknown tools fail closed so state-changing additions cannot accidentally
    interrupt a lock, dispatch, edit, download, clone, or verification
    protocol before its owner reaches the corresponding cleanup/commit step.
    """
    return str(function_name or "").strip() in _SAFE_POST_TOOL_BOUNDARIES


def is_routing_post_tool_boundary(function_name: str) -> bool:
    """Return whether a completed tool produced enough work to end the turn.

    This is deliberately narrower than :func:`is_safe_post_tool_boundary`.
    Capability discovery, source inspection, and plain file reads may harvest
    and stage research but must not preempt the prover before its first actual
    proof or research attempt.
    """
    return str(function_name or "").strip() in _ROUTING_POST_TOOL_BOUNDARIES


def _counter(value: Any) -> int:
    """Return a nonnegative integer for a persisted counter value."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _reset_for_scope(state: MutableMapping[str, Any], scope: str) -> None:
    """Reset theorem-local notification state when the assignment changes."""
    normalized = str(scope or "[project-scope]")
    if str(state.get(_SCOPE_KEY, "") or "") == normalized:
        return
    state[_SCOPE_KEY] = normalized
    state[_PRODUCED_KEY] = 0
    state[_ACKNOWLEDGED_KEY] = 0
    state.pop(_CAPTURED_KEY, None)
    state[_SOURCES_KEY] = {}
    state[_REASONS_KEY] = {}
    state.pop(_FOREGROUND_GRACE_KEY, None)


def synchronize_scope(state: MutableMapping[str, Any], *, scope: str) -> None:
    """Align the coalescer with the current theorem assignment."""
    with _LOCK:
        _reset_for_scope(state, scope)


def arm_foreground_grace(state: MutableMapping[str, Any], *, scope: str) -> bool:
    """Reserve one research-event interrupt followed by an uninterrupted turn.

    Return ``True`` only for the boundary that newly arms the target-scoped
    grace period. While it remains armed, callers must continue harvesting and
    staging research evidence but may not issue another research-event
    interrupt. A natural foreground completion or an authoritative queue
    boundary releases the reservation.
    """
    normalized = str(scope or "[project-scope]")
    with _LOCK:
        _reset_for_scope(state, normalized)
        if str(state.get(_FOREGROUND_GRACE_KEY, "") or "") == normalized:
            return False
        state[_FOREGROUND_GRACE_KEY] = normalized
        return True


def foreground_grace_active(state: MutableMapping[str, Any], *, scope: str) -> bool:
    """Return whether the current scope is owed a non-preempted foreground turn."""
    normalized = str(scope or "[project-scope]")
    with _LOCK:
        _reset_for_scope(state, normalized)
        return str(state.get(_FOREGROUND_GRACE_KEY, "") or "") == normalized


def release_foreground_grace(state: MutableMapping[str, Any], *, scope: str) -> bool:
    """Release the current scope's foreground grace reservation."""
    normalized = str(scope or "[project-scope]")
    with _LOCK:
        _reset_for_scope(state, normalized)
        if str(state.get(_FOREGROUND_GRACE_KEY, "") or "") != normalized:
            return False
        state.pop(_FOREGROUND_GRACE_KEY, None)
        return True


def publish_once(
    state: MutableMapping[str, Any],
    *,
    scope: str,
    source: str,
    reason: str,
) -> int:
    """Publish one uniquely identified event and return its monotonic watermark.

    A source is remembered before any consultation is attempted. If routing
    fails, the pending watermark remains retryable without rediscovering or
    duplicating the underlying job/frontier event.
    """
    normalized_source = str(source or "").strip()
    if not normalized_source:
        raise ValueError("orchestrator event source must be non-empty")
    with _LOCK:
        _reset_for_scope(state, scope)
        raw_sources = state.get(_SOURCES_KEY)
        sources = dict(raw_sources) if isinstance(raw_sources, dict) else {}
        known = _counter(sources.get(normalized_source))
        if known:
            return known
        watermark = _counter(state.get(_PRODUCED_KEY)) + 1
        state[_PRODUCED_KEY] = watermark
        sources[normalized_source] = watermark
        acknowledged = _counter(state.get(_ACKNOWLEDGED_KEY))
        if len(sources) > _SOURCE_CAP:
            acknowledged_sources = sorted(
                (
                    (name, _counter(source_watermark))
                    for name, source_watermark in sources.items()
                    if _counter(source_watermark) <= acknowledged
                ),
                key=lambda item: item[1],
            )
            for name, _source_watermark in acknowledged_sources[
                : max(0, len(sources) - _SOURCE_CAP)
            ]:
                sources.pop(name, None)
        state[_SOURCES_KEY] = sources
        raw_reasons = state.get(_REASONS_KEY)
        reasons = dict(raw_reasons) if isinstance(raw_reasons, dict) else {}
        reasons[str(watermark)] = str(reason or "research event")[:300]
        state[_REASONS_KEY] = reasons
        return watermark


def has_pending(state: MutableMapping[str, Any], *, scope: str) -> bool:
    """Return whether at least one event is newer than the acknowledged prefix."""
    with _LOCK:
        _reset_for_scope(state, scope)
        return _counter(state.get(_PRODUCED_KEY)) > _counter(state.get(_ACKNOWLEDGED_KEY))


def claim_pending(state: MutableMapping[str, Any], *, scope: str) -> EventCapture | None:
    """Atomically capture the current pending prefix for one consultation.

    Only one capture may be in flight. Events published after this snapshot
    remain above its watermark and therefore require a later consultation.
    """
    with _LOCK:
        _reset_for_scope(state, scope)
        acknowledged = _counter(state.get(_ACKNOWLEDGED_KEY))
        captured = _counter(state.get(_CAPTURED_KEY))
        if captured > acknowledged:
            return None
        produced = _counter(state.get(_PRODUCED_KEY))
        if produced <= acknowledged:
            state.pop(_CAPTURED_KEY, None)
            return None
        state[_CAPTURED_KEY] = produced
        raw_reasons = state.get(_REASONS_KEY)
        reasons = dict(raw_reasons) if isinstance(raw_reasons, dict) else {}
        captured_reasons = tuple(
            str(reasons.get(str(watermark), "research event"))
            for watermark in range(acknowledged + 1, produced + 1)
        )
        return EventCapture(watermark=produced, reasons=captured_reasons)


def ensure_capture(state: MutableMapping[str, Any], *, scope: str) -> EventCapture | None:
    """Return the active capture or atomically claim the pending prefix."""
    with _LOCK:
        _reset_for_scope(state, scope)
        acknowledged = _counter(state.get(_ACKNOWLEDGED_KEY))
        captured = _counter(state.get(_CAPTURED_KEY))
        if captured <= acknowledged:
            return claim_pending(state, scope=scope)
        raw_reasons = state.get(_REASONS_KEY)
        reasons = dict(raw_reasons) if isinstance(raw_reasons, dict) else {}
        captured_reasons = tuple(
            str(reasons.get(str(watermark), "research event"))
            for watermark in range(acknowledged + 1, captured + 1)
        )
        return EventCapture(watermark=captured, reasons=captured_reasons)


def acknowledge(state: MutableMapping[str, Any], *, scope: str, capture: EventCapture) -> None:
    """Advance only through the prefix captured by a successful consultation."""
    with _LOCK:
        _reset_for_scope(state, scope)
        captured = _counter(state.get(_CAPTURED_KEY))
        if captured != capture.watermark:
            return
        acknowledged = max(_counter(state.get(_ACKNOWLEDGED_KEY)), capture.watermark)
        state[_ACKNOWLEDGED_KEY] = acknowledged
        state.pop(_CAPTURED_KEY, None)
        raw_reasons = state.get(_REASONS_KEY)
        reasons = dict(raw_reasons) if isinstance(raw_reasons, dict) else {}
        state[_REASONS_KEY] = {
            key: value for key, value in reasons.items() if _counter(key) > acknowledged
        }


def release(state: MutableMapping[str, Any], *, scope: str, capture: EventCapture) -> None:
    """Release a failed consultation capture without acknowledging any event."""
    with _LOCK:
        _reset_for_scope(state, scope)
        if _counter(state.get(_CAPTURED_KEY)) == capture.watermark:
            state.pop(_CAPTURED_KEY, None)
