"""Separate Lean heartbeat exhaustion from process deadlines in check reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_HEARTBEAT_MARKERS = (
    "maximum number of heartbeats",
    "maxheartbeats",
    "deterministic timeout",
    "(deterministic) timeout",
)
_DEADLINE_MARKERS = ("wall-clock deadline", "timed out")
_DEADLINE_CODES = {"check_timeout", "lean_probe_wall_clock_timeout"}


def _diagnostic_text(result: Mapping[str, Any]) -> str:
    """Collect one report's diagnostics without folding in independent nested checks."""
    messages = result.get("messages")
    return "\n".join(
        [str(result.get(key, "") or "") for key in ("error", "output", "message", "stderr")]
        + [
            str(item.get("message", "") or "")
            for item in (messages if isinstance(messages, list) else [])
            if isinstance(item, Mapping) and str(item.get("severity", "error")).lower() == "error"
        ]
    ).lower()


def _heartbeat_limit(result: Mapping[str, Any], diagnostic: str) -> bool:
    """Recognize Lean's deterministic elaboration limit, including older saved reports."""
    return result.get("error_code") == "lean_heartbeat_limit" or any(
        marker in diagnostic for marker in _HEARTBEAT_MARKERS
    )


def process_timed_out(result: Mapping[str, Any]) -> bool:
    """Keep real deadlines while discounting historically inferred heartbeat timeouts.

    A true timeout flag without the inference marker is authoritative: a process
    may time out after already printing a Lean heartbeat diagnostic.
    """
    if result.get("error_code") in _DEADLINE_CODES:
        return True
    if result.get("timed_out") is not True:
        return False
    if result.get("timed_out_inferred_from_diagnostics") is not True:
        return True
    diagnostic = _diagnostic_text(result)
    return not _heartbeat_limit(result, diagnostic) or any(
        marker in diagnostic for marker in _DEADLINE_MARKERS
    )


def normalize_check_outcome(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Label heartbeat rejection without turning it into a process timeout."""
    result = dict(payload)
    diagnostic = _diagnostic_text(result)
    diagnostic_deadline = any(marker in diagnostic for marker in _DEADLINE_MARKERS)
    if (
        (result.get("ok") is not True or result.get("has_errors") is True)
        and _heartbeat_limit(result, diagnostic)
        and not process_timed_out(result)
        and not diagnostic_deadline
    ):
        result["timed_out"] = False
        result.pop("timed_out_inferred_from_diagnostics", None)
        if not result.get("error_code"):
            result["error_code"] = "lean_heartbeat_limit"
    elif diagnostic_deadline and not bool(result.get("timed_out")):
        result["timed_out"] = True
        result["timed_out_inferred_from_diagnostics"] = True
    return result
