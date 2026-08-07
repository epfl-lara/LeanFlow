"""Bound diagnostic-only Lean feedback loops inside one provider turn."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_STATE_ATTR = "_managed_diagnostic_loop_guard_state"
_DEFAULT_FEEDBACK_LIMIT = 12
_MIN_FEEDBACK_LIMIT = 4
_MAX_FEEDBACK_LIMIT = 40


@dataclass(frozen=True)
class DiagnosticLoopDecision:
    """Describe an exhausted diagnostic feedback window."""

    attempts: int
    limit: int
    source_revision_sha256: str


def feedback_limit() -> int:
    """Return the bounded per-turn diagnostic feedback allowance."""
    raw = str(os.environ.get("LEANFLOW_DIAGNOSTIC_FEEDBACK_LIMIT", "") or "").strip()
    try:
        configured = int(raw) if raw else _DEFAULT_FEEDBACK_LIMIT
    except ValueError:
        configured = _DEFAULT_FEEDBACK_LIMIT
    return max(_MIN_FEEDBACK_LIMIT, min(configured, _MAX_FEEDBACK_LIMIT))


def reset(agent: Any) -> None:
    """Start a fresh diagnostic allowance for one provider conversation."""
    setattr(
        agent,
        _STATE_ATTR,
        {
            "attempts": 0,
            "source_revision_sha256": "",
            "exhausted": False,
        },
    )


def observe(
    agent: Any,
    *,
    function_name: str,
    args: Mapping[str, Any] | None,
    source_revision_sha256: str,
) -> DiagnosticLoopDecision | None:
    """End a turn that spends its bounded allowance on diagnostic feedback.

    The guard counts only ``lean_incremental_check(action=feedback)`` calls.
    Exact target/helper checks and source edits remain unrestricted. A source
    revision change also resets the allowance, so productive construction can
    keep using fast Lean feedback without inheriting stale diagnostic debt.
    """
    if str(function_name or "") != "lean_incremental_check":
        return None
    action = str(dict(args or {}).get("action", "check_target") or "check_target")
    if action.strip().lower().replace("-", "_") != "feedback":
        return None

    state = getattr(agent, _STATE_ATTR, None)
    if not isinstance(state, dict):
        reset(agent)
        state = getattr(agent, _STATE_ATTR)

    revision = str(source_revision_sha256 or "").strip()
    prior_revision = str(state.get("source_revision_sha256", "") or "").strip()
    if revision and prior_revision and revision != prior_revision:
        state["attempts"] = 0
        state["exhausted"] = False
    if revision:
        state["source_revision_sha256"] = revision

    attempts = max(0, int(state.get("attempts", 0) or 0)) + 1
    state["attempts"] = attempts
    limit = feedback_limit()
    if attempts < limit or bool(state.get("exhausted")):
        return None
    state["exhausted"] = True
    return DiagnosticLoopDecision(
        attempts=attempts,
        limit=limit,
        source_revision_sha256=revision,
    )
