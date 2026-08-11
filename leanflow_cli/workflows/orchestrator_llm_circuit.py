"""Persist campaign-scoped backoff for failed research advisory calls.

The research orchestrator and persistence coach can resolve to the same small
auxiliary endpoint.  Availability is therefore remembered by provider/model
failure identity rather than by advisory task: two identical connection
failures open one shared circuit, while deterministic routing and coaching
fallbacks continue immediately.  The state is bound to the durable campaign
identity so a new campaign never inherits an old provider outage.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

CIRCUIT_VERSION = 3
TIMEOUT_THRESHOLD = 1
PROVIDER_FAILURE_THRESHOLD = 2
COOLDOWN_SECONDS = 300
MAX_COOLDOWN_SECONDS = 1800


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: Any) -> datetime | None:
    """Return one normalized UTC timestamp, or None for malformed state."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def _campaign_id(explicit: str | None = None) -> str:
    """Return the durable campaign identity, falling back to the managed run."""
    normalized = str(explicit or "").strip()
    if normalized:
        return normalized
    summary = read_json_file(workflow_state_root() / "summary.json")
    campaign = summary.get("campaign")
    if isinstance(campaign, dict):
        normalized = str(campaign.get("campaign_id", "") or "").strip()
        if normalized:
            return normalized
    return str(os.getenv("LEANFLOW_WORKFLOW_RUN_ID", "") or "").strip() or "unscoped"


def _failure_fingerprint(
    status: str,
    *,
    provider: str = "",
    model: str = "",
    error: str = "",
) -> str:
    """Return a task-independent credential-free availability fingerprint."""
    normalized_status = str(status or "unavailable").strip().lower()
    failure_class = "timeout" if normalized_status == "timeout" else "provider-failure"
    fields = (
        failure_class,
        str(provider or "").strip().lower()[:200],
        str(model or "").strip().lower()[:200],
        " ".join(str(error or "").split()).lower()[:500],
    )
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()[:20]


def _cooldown_seconds(open_count: int) -> int:
    """Return the bounded exponential cooldown for one half-open failure."""
    exponent = max(0, min(16, int(open_count) - 1))
    return min(MAX_COOLDOWN_SECONDS, COOLDOWN_SECONDS * (2**exponent))


def circuit_path() -> Path:
    """Return the project-local advisory circuit state path."""
    return workflow_state_root() / "orchestrator-llm-circuit.json"


def circuit_snapshot() -> dict[str, Any]:
    """Return the persisted circuit payload."""
    return dict(read_json_file(circuit_path()))


def request_allowed(
    *,
    now: datetime | None = None,
    campaign_id: str | None = None,
    task: str = "",
) -> bool:
    """Return whether a research advisory may consume foreground wall time."""
    current = now or _now()
    snapshot = circuit_snapshot()
    if str(snapshot.get("campaign_id", "") or "") != _campaign_id(campaign_id):
        return True
    open_until = _parse_time(snapshot.get("open_until"))
    if open_until is None or current >= open_until:
        return True
    normalized_task = str(task or "").strip()
    affected_tasks = {
        str(item or "").strip()
        for item in (snapshot.get("failure_tasks") or [])
        if str(item or "").strip()
    }
    return bool(normalized_task and affected_tasks and normalized_task not in affected_tasks)


def _record_failure(
    status: str,
    *,
    now: str | datetime | None = None,
    campaign_id: str | None = None,
    provider: str = "",
    model: str = "",
    error: str = "",
    task: str = "",
) -> dict[str, Any]:
    """Record one advisory failure and open its bounded backoff at threshold."""
    if isinstance(now, datetime):
        current = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    else:
        current = _parse_time(now) or _now()
    normalized_status = str(status or "unavailable").strip().lower()
    current_campaign = _campaign_id(campaign_id)
    fingerprint = _failure_fingerprint(
        normalized_status,
        provider=provider,
        model=model,
        error=error,
    )

    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("campaign_id", "") or "") != current_campaign:
            payload.clear()
        same_failure = str(payload.get("failure_fingerprint", "") or "") == fingerprint
        if not same_failure:
            payload.pop("open_until", None)
            payload["open_count"] = 0
            payload["cooldown_seconds"] = 0
        consecutive_failures = (
            max(0, int(payload.get("consecutive_failures", 0) or 0)) + 1 if same_failure else 1
        )
        if normalized_status == "timeout":
            consecutive_timeouts = (
                max(0, int(payload.get("consecutive_timeouts", 0) or 0)) + 1 if same_failure else 1
            )
        else:
            consecutive_timeouts = 0
        failure_tasks = (
            {
                str(item or "").strip()
                for item in (payload.get("failure_tasks") or [])
                if str(item or "").strip()
            }
            if same_failure
            else set()
        )
        if str(task or "").strip():
            failure_tasks.add(str(task).strip())
        payload.update(
            {
                "version": CIRCUIT_VERSION,
                "campaign_id": current_campaign,
                "consecutive_failures": consecutive_failures,
                "consecutive_timeouts": consecutive_timeouts,
                "failure_fingerprint": fingerprint,
                "failure_tasks": sorted(failure_tasks),
                "last_failure_at": _iso(current),
                "last_failure_status": normalized_status,
                "last_failure_task": str(task or "")[:120],
                "last_failure_provider": str(provider or "")[:200],
                "last_failure_model": str(model or "")[:200],
            }
        )
        if normalized_status == "timeout":
            payload["last_timeout_at"] = _iso(current)
        threshold_reached = (
            normalized_status == "timeout" and consecutive_timeouts >= TIMEOUT_THRESHOLD
        ) or consecutive_failures >= PROVIDER_FAILURE_THRESHOLD
        existing_open_until = _parse_time(payload.get("open_until"))
        if threshold_reached and (existing_open_until is None or current >= existing_open_until):
            open_count = max(0, int(payload.get("open_count", 0) or 0)) + 1
            cooldown_seconds = _cooldown_seconds(open_count)
            payload.update(
                {
                    "open_count": open_count,
                    "cooldown_seconds": cooldown_seconds,
                    "open_until": _iso(current + timedelta(seconds=cooldown_seconds)),
                }
            )
        return dict(payload)

    return dict(update_json_file(circuit_path(), mutate) or {})


def record_timeout(
    *,
    now: str | datetime | None = None,
    campaign_id: str | None = None,
    provider: str = "",
    model: str = "",
    error: str = "",
    task: str = "",
) -> dict[str, Any]:
    """Open the persisted cooldown after the configured timeout threshold."""
    return _record_failure(
        "timeout",
        now=now,
        campaign_id=campaign_id,
        provider=provider,
        model=model,
        error=error,
        task=task,
    )


def record_provider_failure(
    status: str,
    *,
    now: str | datetime | None = None,
    campaign_id: str | None = None,
    provider: str = "",
    model: str = "",
    error: str = "",
    task: str = "",
) -> dict[str, Any]:
    """Record a connection/unavailable failure without changing route authority."""
    normalized = str(status or "unavailable").strip().lower()
    if normalized not in {"error", "unavailable"}:
        normalized = "unavailable"
    return _record_failure(
        normalized,
        now=now,
        campaign_id=campaign_id,
        provider=provider,
        model=model,
        error=error,
        task=task,
    )


def record_success(
    *,
    now: datetime | None = None,
    campaign_id: str | None = None,
    task: str = "",
) -> dict[str, Any]:
    """Close the cooldown after one successful normal or half-open response."""
    current = now or _now()
    current_campaign = _campaign_id(campaign_id)

    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("campaign_id", "") or "") != current_campaign:
            payload.clear()
        normalized_task = str(task or "").strip()
        affected_tasks = {
            str(item or "").strip()
            for item in (payload.get("failure_tasks") or [])
            if str(item or "").strip()
        }
        if normalized_task and affected_tasks and normalized_task not in affected_tasks:
            payload.update(
                {
                    "version": CIRCUIT_VERSION,
                    "campaign_id": current_campaign,
                    "last_success_at": _iso(current),
                    "last_success_task": normalized_task,
                }
            )
            return dict(payload)
        payload.update(
            {
                "version": CIRCUIT_VERSION,
                "campaign_id": current_campaign,
                "consecutive_failures": 0,
                "consecutive_timeouts": 0,
                "failure_fingerprint": "",
                "failure_tasks": [],
                "open_count": 0,
                "cooldown_seconds": 0,
                "open_until": "",
                "last_success_at": _iso(current),
            }
        )
        return dict(payload)

    return dict(update_json_file(circuit_path(), mutate) or {})


__all__ = [
    "COOLDOWN_SECONDS",
    "MAX_COOLDOWN_SECONDS",
    "PROVIDER_FAILURE_THRESHOLD",
    "TIMEOUT_THRESHOLD",
    "circuit_path",
    "circuit_snapshot",
    "record_provider_failure",
    "record_success",
    "record_timeout",
    "request_allowed",
]
