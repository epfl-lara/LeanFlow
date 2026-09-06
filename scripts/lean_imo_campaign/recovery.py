"""Resume transient provider failures without changing a cell's original budgets."""

from __future__ import annotations

import time
from typing import Any


def schedule_recovery(cell: dict[str, Any]) -> bool:
    """Queue at most three provider reconnects while preserving the saved run lineage."""
    attempts = int(cell.get("recovery_attempts", 0))
    metrics = cell.get("metrics", {})
    if (
        cell.get("status") != "provider_error"
        or attempts >= 3
        or int(metrics.get("api_calls", 0)) >= 2000
        or float(metrics.get("elapsed_s", 0)) >= 28800
    ):
        return False
    cell.setdefault("executions", []).append(
        {
            key: cell.get(key)
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
