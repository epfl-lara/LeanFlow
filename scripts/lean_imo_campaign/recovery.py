"""Resume transient provider failures without changing a cell's original budgets."""

from __future__ import annotations

import copy
import time
from typing import Any


def requires_inspection(cell: dict[str, Any]) -> bool:
    """Pause after infrastructure errors or a scheduler stop with unused campaign budget."""
    return (
        cell.get("status")
        in {
            "environment_error",
            "error",
            "provider_error",
            "infrastructure_error",
            "source_conflict",
            "unverified_exit",
            "watchdog_timeout",
            "interrupted",
            "blocked",
            "context_limit",
        }
        or (cell.get("stop_reason") or {}).get("scope") == "scheduler"
    )


def schedule_recovery(cell: dict[str, Any]) -> bool:
    """Queue at most three provider reconnects while preserving the saved run lineage."""
    attempts = int(cell.get("recovery_attempts", 0))
    metrics = cell.get("metrics", {})
    config = cell.get("config", {})
    error = str(cell.get("error") or "").lower()
    if (
        cell.get("status") != "provider_error"
        or attempts >= 3
        or int(metrics.get("api_calls", 0)) >= int(config.get("total_api_calls", 2000))
        or float(metrics.get("elapsed_s", 0)) >= float(config.get("wall_time_s", 28800))
        or any(
            text in error
            for text in (
                "invalid prompt",
                "usage policy",
                "authentication",
                "invalid api key",
                "unauthorized",
                "context_length_exceeded",
                "insufficient_quota",
            )
        )
    ):
        return False
    cell.setdefault("executions", []).append(
        {
            key: copy.deepcopy(cell.get(key))
            for key in (
                "run_id",
                "status",
                "returncode",
                "started_at",
                "finished_at",
                "error",
                "metrics",
            )
        }
    )
    cell.update(
        status="pending",
        resume_run_id=cell["run_id"],
        recovery_attempts=attempts + 1,
        not_before=time.time() + 30 * (attempts + 1),
    )
    return True
