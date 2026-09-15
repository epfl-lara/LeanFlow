"""Retry only identified transport failures within the existing campaign allowance."""

from __future__ import annotations

import copy

import pytest

from scripts.lean_imo_campaign.dispatch_policy import pause_after_failure
from scripts.lean_imo_campaign.recovery import schedule_recovery


def cell(error: str, **changes):
    """Create a saved failed cell with enough of its original allocation remaining."""
    return {
        "status": "provider_error",
        "run_id": "original",
        "error": error,
        "metrics": {"api_calls": 101, "elapsed_s": 6093.228},
        "config": {"total_api_calls": 5000, "wall_time_s": 57600},
        **changes,
    }


@pytest.mark.parametrize(
    "error",
    [
        "",
        "unknown provider failure",
        "NameError: missing_symbol",
        "Provider report requested an invalid declaration",
        "Error code: 400 - Unsupported parameter: 'tool_choice'",
        "Error code: 400 - the request timeout parameter is invalid",
        "Error code: 401 - connection error",
        "Error code: 403 - rate limit exceeded",
        "Error code: 429 - insufficient_quota",
    ],
)
def test_unclassified_or_permanent_failures_pause_without_mutating_lineage(error):
    saved = cell(error)
    before = copy.deepcopy(saved)
    assert not schedule_recovery(saved)
    assert saved == before
    assert pause_after_failure(saved, False)
    assert pause_after_failure(saved, True)


@pytest.mark.parametrize(
    "error",
    [
        "Connection error.",
        "Request timed out.",
        "peer closed connection (incomplete chunked read)",
        "connection refused",
        "connection reset by peer",
        "upstream connect error",
        "Unable to verify model access right now",
        "Error code: 429 - Too many requests",
        "Error code: 503 - unavailable",
        "HTTP 502 Bad Gateway",
    ],
)
def test_identified_transients_share_retry_and_opt_in_advancement_policy(error):
    saved = cell(error)
    assert schedule_recovery(saved)
    assert saved["resume_run_id"] == "original"
    assert saved["recovery_attempts"] == 1
    assert saved["metrics"] == {"api_calls": 101, "elapsed_s": 6093.228}
    saved["status"] = "provider_error"
    assert pause_after_failure(saved, False)
    assert not pause_after_failure(saved, True)


def test_scheduler_scope_cannot_be_bypassed_by_transient_wording():
    saved = cell("Connection error.", stop_reason={"scope": "scheduler"})
    assert not schedule_recovery(saved)
    assert pause_after_failure(saved, True)


def test_planning_stall_preserves_evidence_and_pauses_even_with_transient_wording():
    saved = cell("Repeated Connection error. feedback", status="planning_stalled")
    saved["planning_evidence"] = {"rejections": 3}
    before = copy.deepcopy(saved)
    assert not schedule_recovery(saved)
    assert pause_after_failure(saved, True)
    assert saved == before
