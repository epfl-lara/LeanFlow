"""Derive checkpoint handoff status from structured workflow authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def checkpoint_success_state(
    live_state: Mapping[str, Any] | None,
    *,
    verified: bool,
    blocker_summary: str,
) -> str:
    """Return one status for both checkpoint metadata and summary prose.

    A signal-interrupted campaign remains mathematically in progress even when
    its target has a concrete blocker. Blocker text is resume evidence, not an
    authoritative terminal verdict.
    """
    if verified:
        return "verified"
    current = dict(live_state or {})
    try:
        exit_code = int(current.get("exit_code", 0) or 0)
    except (TypeError, ValueError):
        exit_code = 0
    interrupt_source = str(current.get("interrupt_source", "") or "").strip().lower()
    if exit_code == 130 or interrupt_source == "signal":
        return "in-progress"
    return "blocked" if str(blocker_summary or "").strip() else "in-progress"
