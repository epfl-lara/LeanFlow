"""Characterize provider usage-limit reset parsing and shared outage state."""

from __future__ import annotations

from types import SimpleNamespace

from core.provider_availability import (
    DEFAULT_PROVIDER_RESET_WAIT_SECONDS,
    MAX_PROVIDER_RESET_SECONDS,
    extract_provider_usage_limit,
    normalize_provider_retry_after,
    provider_reset_wait_max_seconds,
)

LIVE_CODEX_USAGE_LIMIT = (
    "Error code: 429 - {'error': {'type': 'usage_limit_reached', "
    "'message': 'The usage limit has been reached', 'plan_type': 'pro', "
    "'resets_at': 1784949879, 'eligible_promo': None, "
    "'resets_in_seconds': 453096}}"
)


def test_live_codex_usage_limit_string_yields_bounded_reset_metadata():
    reset = extract_provider_usage_limit(
        RuntimeError(LIVE_CODEX_USAGE_LIMIT),
        now_epoch=1784496783,
    )

    assert reset is not None
    assert reset.kind == "usage_limit_reached"
    assert reset.resets_at_epoch == 1784949879
    assert reset.reported_resets_in_seconds == 453096
    assert reset.retry_after_seconds == 453097
    assert reset.unavailable_until_epoch == 1784949880
    assert reset.timing_consistent is True
    assert reset.timing_clamped is False


def test_structured_exception_body_outranks_misleading_string():
    error = SimpleNamespace(
        body={
            "type": "usage_limit_reached",
            "resets_at": 1_700_000_120,
            "resets_in_seconds": 120,
        },
        status_code=429,
        __str__=lambda: "resets_in_seconds: 9",
    )

    reset = extract_provider_usage_limit(error, now_epoch=1_700_000_000)

    assert reset is not None
    assert reset.retry_after_seconds == 121
    assert reset.source == "exception.body"


def test_untrusted_reset_values_are_cross_checked_and_clamped():
    error = RuntimeError(
        "Error code: 429 - {'error': {'type': 'usage_limit_reached', "
        "'resets_at': 1700000060, 'resets_in_seconds': 999999999999999999}}"
    )

    reset = extract_provider_usage_limit(error, now_epoch=1_700_000_000)

    assert reset is not None
    assert reset.retry_after_seconds == MAX_PROVIDER_RESET_SECONDS
    assert reset.timing_consistent is False
    assert reset.timing_clamped is True


def test_normalized_absolute_deadline_is_bounded_against_observation_time():
    assert (
        normalize_provider_retry_after(
            {
                "kind": "usage_limit_reached",
                "retry_after_seconds": 60,
                "unavailable_until_epoch": 10**30,
            },
            now_epoch=1_700_000_000,
        )
        == {}
    )


def test_nonfinite_wait_configuration_uses_bounded_default(monkeypatch):
    monkeypatch.setenv("LEANFLOW_PROVIDER_RESET_MAX_WAIT_SECONDS", "inf")
    assert provider_reset_wait_max_seconds() == DEFAULT_PROVIDER_RESET_WAIT_SECONDS


def test_cyclic_structured_error_is_rejected_without_unbounded_recursion():
    body: dict = {"type": "other_error"}
    body["error"] = body
    assert extract_provider_usage_limit(SimpleNamespace(body=body)) is None
