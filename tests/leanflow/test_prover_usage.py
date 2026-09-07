"""Verify request-level cost coverage and durable resume telemetry."""

import json

import pytest

from leanflow_cli.workflows.prover.usage import CostLedger, aggregate_cost, merge_usage
from tests.leanflow.test_prover_sessions import run_fake


@pytest.mark.parametrize(
    "model,provider", [("gpt-5.6-terra", "openai"), ("gpt-5", "openai-codex"), ("glm-5", "openai")]
)
def test_unknown_or_subscription_prices_are_not_guessed(model, provider):
    ledger = CostLedger()
    ledger.record({"input_tokens": 100, "output_tokens": 10}, model=model, provider=provider)
    assert ledger.snapshot(1) == {
        "cost_usd": None,
        "costed_api_calls": 0,
        "cost_source": "unavailable",
        "cost_complete": False,
    }


def test_exact_price_is_labelled_as_an_estimate():
    ledger = CostLedger()
    ledger.record({"input_tokens": 100, "output_tokens": 10}, model="gpt-4o")
    assert ledger.snapshot(1)["cost_source"] == "estimated"
    assert ledger.snapshot(1)["cost_complete"] is True


def test_each_request_retains_cost_source_and_coverage():
    ledger = CostLedger()
    ledger.record({"cost_usd": 0.2}, model="unknown")
    ledger.record({"estimated_cost_usd": 0.3}, model="unknown")
    ledger.record({}, model="unknown")
    snapshot = ledger.snapshot(3)
    assert snapshot == {
        "cost_usd": 0.5,
        "costed_api_calls": 2,
        "cost_source": "mixed",
        "cost_complete": False,
    }
    assert aggregate_cost([{**snapshot, "api_calls": 3}])["cost_complete"] is False


def test_live_and_final_resume_totals_are_not_added_twice():
    job = {
        "api_calls": 3,
        "previous_cost_usd": 1.25,
        "previous_costed_api_calls": 2,
        "previous_cost_source": "provider_reported",
        "previous_input_tokens": 10,
    }
    usage = {
        "cost_usd": 0.5,
        "costed_api_calls": 1,
        "cost_source": "provider_reported",
        "input_tokens": 7,
    }
    merge_usage(job, usage)
    merge_usage(job, usage)
    assert job["cost_usd"] == 1.75
    assert job["costed_api_calls"] == 3
    assert job["input_tokens"] == 17
    assert job["cost_complete"] is True


def test_response_usage_is_durable_before_observer_notification(tmp_path, monkeypatch):
    def request(*_args):
        return {"role": "assistant", "content": '{"proof":"trivial"}'}, {
            "cost_usd": 0.25,
            "input_tokens": 100,
            "output_tokens": 10,
        }

    def event(kind, details):
        if kind == "api-response":
            saved = json.loads((tmp_path / ".runtime/job/request-count.json").read_text())
            assert saved["usage"]["input_tokens"] == 100
            assert saved["usage"]["cost_usd"] == 0.25
            assert saved["usage"]["costed_api_calls"] == 1

    run_fake(tmp_path, monkeypatch, request, on_event=event)
    run_fake(tmp_path, monkeypatch, request)
    saved = json.loads((tmp_path / ".runtime/job/request-count.json").read_text())
    assert saved["used"] == 2
    assert saved["usage"]["input_tokens"] == 200
    assert saved["usage"]["cost_usd"] == 0.5
