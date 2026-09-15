"""Resume transient provider failures without changing a cell's original budgets."""

from __future__ import annotations

import copy
import re
import time
from typing import Any

_HTTP_STATUS = re.compile(r"\b(?:error code:|http(?: status)?(?: code)?[: ]?)\s*([1-5]\d\d)\b")
_PERMANENT_ERRORS = (
    "invalid prompt",
    "invalid request",
    "unsupported parameter",
    "usage policy",
    "authentication",
    "invalid api key",
    "unauthorized",
    "forbidden",
    "context_length_exceeded",
    "insufficient_quota",
    "empty or truncated stage response",
    "nameerror",
    "typeerror",
    "attributeerror",
    "valueerror",
    "assertionerror",
    "keyerror",
)
_TRANSIENT_ERRORS = (
    "connection error",
    "request timed out",
    "provider timed out",
    "provider stream exceeded the request deadline",
    "incomplete chunked read",
    "connection refused",
    "connection reset",
    "upstream connect error",
    "unable to verify model access right now",
    "rate limit",
    "rate_limit_exceeded",
    "too many requests",
)


def is_transient_provider_error(error: str) -> bool:
    """Recognize recorded transport failures; leave unknown or invalid requests paused.

    Recovery and queue advancement share this policy so SDK connection/timeout
    messages do not get contradictory treatment. Explicit permanent failures
    and non-retriable HTTP statuses take precedence over transient wording.
    """
    message = error.lower()
    if any(marker in message for marker in _PERMANENT_ERRORS):
        return False
    match = _HTTP_STATUS.search(message)
    if match is not None:
        status = int(match.group(1))
        return status in {408, 429} or 500 <= status <= 599
    return any(marker in message for marker in _TRANSIENT_ERRORS)


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
            "planning_stalled",
        }
        or (cell.get("stop_reason") or {}).get("scope") == "scheduler"
    )


def schedule_recovery(cell: dict[str, Any]) -> bool:
    """Queue at most three provider reconnects while preserving the saved run lineage."""
    attempts = int(cell.get("recovery_attempts", 0))
    metrics = cell.get("metrics", {})
    config = cell.get("config", {})
    if (
        cell.get("status") != "provider_error"
        or (cell.get("stop_reason") or {}).get("scope") == "scheduler"
        or attempts >= 3
        or int(metrics.get("api_calls", 0)) >= int(config.get("total_api_calls", 2000))
        or float(metrics.get("elapsed_s", 0)) >= float(config.get("wall_time_s", 28800))
        or not is_transient_provider_error(str(cell.get("error") or ""))
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
                "error_details",
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
