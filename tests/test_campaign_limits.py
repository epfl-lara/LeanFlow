"""Exercise native invocation admission, unknown usage and cumulative campaign time."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from core.campaign_limits import CampaignLimits, LimitReached, optional_limit


@pytest.mark.parametrize("value", [True, False, -1, float("inf"), float("nan"), "1.5"])
def test_invalid_call_limit(value):
    with pytest.raises(ValueError):
        optional_limit(value, integer=True)


def test_zero_is_exhausted_but_none_is_unlimited(tmp_path):
    zero = CampaignLimits(tmp_path / "zero.json", {"agent_calls": 0})
    with pytest.raises(LimitReached):
        zero.reserve()
    unlimited = CampaignLimits(tmp_path / "open.json", {})
    for _ in range(120):
        call = unlimited.reserve()
        unlimited.started_call(call)
        unlimited.finish(call, None, started=True)


def test_parallel_last_invocation_is_atomic(tmp_path):
    ledger = CampaignLimits(tmp_path / "limits.json", {"agent_calls": 1})

    def reserve():
        try:
            return ledger.reserve()
        except LimitReached:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(lambda _: reserve(), range(8)))
    assert len([value for value in values if value]) == 1


def test_failed_start_releases_admission_and_duplicate_completion_does_not_charge(tmp_path):
    ledger = CampaignLimits(tmp_path / "limits.json", {"agent_calls": 1, "tokens": 10})
    call = ledger.reserve()
    ledger.finish(call, None, started=False)
    second = ledger.reserve()
    ledger.started_call(second)
    ledger.finish(second, {"input_tokens": 7, "output_tokens": 5}, started=True)
    ledger.finish(second, {"input_tokens": 10000}, started=True)
    assert ledger.state["invocations"][second]["usage"]["input_tokens"] == 7
    with pytest.raises(LimitReached):
        ledger.reserve()


def test_missing_usage_cannot_be_treated_as_zero_under_token_limit(tmp_path):
    ledger = CampaignLimits(tmp_path / "limits.json", {"tokens": 10})
    call = ledger.reserve()
    ledger.finish(call, None, started=True)
    with pytest.raises(LimitReached, match="unavailable"):
        ledger.reserve()


def test_active_time_survives_resume_but_offline_time_is_not_charged(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("core.campaign_limits.time.monotonic", lambda: clock[0])
    path = tmp_path / "limits.json"
    ledger = CampaignLimits(path, {"wall_time_s": 5000})
    clock[0] = 3610
    ledger.persist()
    assert ledger.remaining() == 1400
    clock[0] += 20000
    resumed = CampaignLimits(path, {"wall_time_s": 5000})
    assert resumed.remaining() == 1400
    clock[0] += 1400
    with pytest.raises(LimitReached, match="time"):
        resumed.reserve()


@pytest.mark.parametrize("started", [False, True])
def test_resume_keeps_uncertain_interrupted_call_and_token_usage(tmp_path, started):
    path = tmp_path / "limits.json"
    first = CampaignLimits(path, {"tokens": 100})
    call = first.reserve()
    if started:
        first.started_call(call)
    resumed = CampaignLimits(path, {"tokens": 100})
    assert resumed.state["invocations"][call]["interrupted"] is True
    with pytest.raises(LimitReached, match="unavailable"):
        resumed.reserve()
