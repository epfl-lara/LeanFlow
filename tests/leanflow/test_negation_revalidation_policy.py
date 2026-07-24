"""Cold-start policy tests for authoritative source-negation revalidation."""

from __future__ import annotations

from leanflow_cli.workflows import negation_revalidation_policy as policy


def test_source_promotion_timeout_has_independent_cold_floor(monkeypatch):
    """A short scratch-probe budget cannot undercut full-module elaboration."""
    monkeypatch.delenv("LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S", raising=False)

    assert policy.source_promotion_timeout_s(probe_timeout_s=120) == 300
    assert policy.source_promotion_timeout_s(probe_timeout_s=480) == 480


def test_source_promotion_timeout_override_is_raise_only(monkeypatch):
    """Malformed and lower overrides retain the floor while larger values win."""
    monkeypatch.setenv("LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S", "30")
    assert policy.source_promotion_timeout_s(probe_timeout_s=120) == 300

    monkeypatch.setenv("LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S", "bad")
    assert policy.source_promotion_timeout_s(probe_timeout_s=120) == 300

    monkeypatch.setenv("LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S", "720")
    assert policy.source_promotion_timeout_s(probe_timeout_s=120) == 720


def test_source_promotion_timeout_is_hard_bounded(monkeypatch):
    """An accidental override cannot create an unbounded child lifetime."""
    monkeypatch.setenv("LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S", "999999")

    assert (
        policy.source_promotion_timeout_s(probe_timeout_s=120)
        == policy.SOURCE_PROMOTION_TIMEOUT_MAX_S
    )
