"""Limit active campaign cells independently of their persistent lane assignments."""

from __future__ import annotations

import os
from typing import Any

from scripts.lean_imo_campaign.recovery import requires_inspection


def active_limit(lane_count: int) -> int:
    """Read the admission limit without reassigning existing problem lanes."""
    limit = int(os.environ.get("LEANFLOW_CAMPAIGN_MAX_ACTIVE", str(lane_count)))
    if not 1 <= limit <= lane_count:
        raise ValueError("LEANFLOW_CAMPAIGN_MAX_ACTIVE must be between 1 and the lane count")
    return limit


def can_admit(rows: list[dict[str, Any]], lane: int, active_count: int, limit: int) -> bool:
    """Allow admission within capacity and preserve manifest order in serial mode."""
    if active_count >= limit:
        return False
    if limit == 1:
        pending = next((row for row in rows if row["status"] == "pending"), None)
        if pending is None or pending["lane"] not in (None, lane):
            return False
    return True


def pause_after_failure(cell: dict[str, Any], continue_transient: bool) -> bool:
    """Optionally advance past exhausted transport retries; retain integrity stops."""
    error = str(cell.get("error") or "").lower()
    transient = any(
        marker in error
        for marker in (
            "incomplete chunked read",
            "connection refused",
            "connection reset",
            "upstream connect error",
            "unable to verify model access right now",
        )
    )
    access_failure = any(
        marker in error
        for marker in ("unauthorized", "authentication", "invalid api key", "insufficient_quota")
    )
    if (
        continue_transient
        and cell.get("status") == "provider_error"
        and transient
        and not access_failure
    ):
        return False
    return requires_inspection(cell)
