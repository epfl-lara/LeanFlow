"""Limit active campaign cells independently of their persistent lane assignments."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from scripts.lean_imo_campaign.recovery import is_transient_provider_error, requires_inspection


def active_limit(lane_count: int) -> int:
    """Read the admission limit without reassigning existing problem lanes."""
    limit = int(os.environ.get("LEANFLOW_CAMPAIGN_MAX_ACTIVE", str(lane_count)))
    if not 1 <= limit <= lane_count:
        raise ValueError("LEANFLOW_CAMPAIGN_MAX_ACTIVE must be between 1 and the lane count")
    return limit


def reserved_active_count(reservations: list[dict[str, str]]) -> int:
    """Reserve slots for recorded workers draining in an earlier campaign.

    Read dispatcher evidence, which releases a cell only after its process exits.
    Missing or partial evidence retains the slot rather than oversubscribing it.
    This lets a replacement queue progress without interrupting surviving work.
    """
    count = 0
    for reservation in reservations:
        try:
            saved = json.loads(Path(reservation["campaign"]).read_text())
            cell = next(c for c in saved["cells"] if c["id"] == reservation["cell_id"])
            count += cell["status"] in {"running", "preparing"}
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            count += 1
    return count


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
    if (cell.get("stop_reason") or {}).get("scope") == "scheduler":
        return True
    access_failure = any(
        marker in error
        for marker in ("unauthorized", "authentication", "invalid api key", "insufficient_quota")
    )
    # A model exhausted its report allowance for this problem. Preserve its
    # failure, but do not strand independent problems behind a global pause.
    if (
        cell.get("status") == "provider_error"
        and error.startswith("empty or truncated stage response;")
        and not access_failure
        and (cell.get("stop_reason") or {}).get("scope") != "scheduler"
    ):
        return False
    if (
        continue_transient
        and cell.get("status") == "provider_error"
        and is_transient_provider_error(error)
        and not access_failure
    ):
        return False
    return requires_inspection(cell)
