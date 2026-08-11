"""Tests for bounded model-verification provider dispatch."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.providers.isolated_auxiliary import AuxiliaryTextResponse, IsolatedAuxiliaryError
from leanflow_cli.workflows import verification_providers
from tools.utilities.interrupt import CooperativeInterrupt, set_interrupt


def test_model_review_uses_normalized_isolated_result(monkeypatch):
    monkeypatch.setattr(
        verification_providers,
        "run_isolated_auxiliary_text",
        lambda **_kwargs: AuxiliaryTextResponse(
            content='{"route":"probe"}',
            model="control-model",
        ),
    )
    monkeypatch.setattr(
        verification_providers,
        "_record_verification_activity",
        lambda *_args, **_kwargs: None,
    )

    result = verification_providers.run_model_verification_review(
        provider="main",
        task="orchestration",
        prompt="choose a route",
        timeout_s=7,
    )

    assert result.status == "ok"
    assert result.response == '{"route":"probe"}'
    assert result.model == "control-model"
    assert result.timed_out is False


def test_model_review_emits_progress_heartbeat(monkeypatch):
    events: list[tuple[str, dict]] = []

    def fake_call(**kwargs):
        kwargs["progress_callback"](31.0, 180.0)
        return AuxiliaryTextResponse(content="PASS", model="gpt-5.6-sol")

    monkeypatch.setattr(verification_providers, "run_isolated_auxiliary_text", fake_call)
    monkeypatch.setattr(
        verification_providers,
        "resolve_auxiliary_call_identity",
        lambda **_kwargs: SimpleNamespace(provider="openai-codex", model="gpt-5.6-sol"),
    )
    monkeypatch.setattr(
        verification_providers,
        "_record_verification_activity",
        lambda event, _message, **details: events.append((event, details)),
    )

    result = verification_providers.run_model_verification_review(
        provider="codex",
        task="blueprint_verification",
        prompt="review",
        timeout_s=180,
    )

    assert result.status == "ok"
    heartbeat = next(
        details for event, details in events if event == "verification-review-heartbeat"
    )
    assert heartbeat["provider"] == "openai-codex"
    assert heartbeat["elapsed_s"] == 31.0


def test_model_review_does_not_translate_cooperative_interrupt_to_provider_error(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(
        verification_providers,
        "run_isolated_auxiliary_text",
        lambda **_kwargs: calls.append(True),
    )
    set_interrupt(True)
    try:
        with pytest.raises(CooperativeInterrupt, match="before launch"):
            verification_providers.run_model_verification_review(
                provider="main",
                task="planner_synthesis",
                prompt="choose a plan",
            )
    finally:
        set_interrupt(False)

    assert calls == []


def test_model_review_correlates_request_and_result_telemetry(monkeypatch):
    events: list[tuple[str, dict]] = []
    times = iter((100.0, 102.5))
    monkeypatch.setattr(verification_providers.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        verification_providers,
        "run_isolated_auxiliary_text",
        lambda **_kwargs: AuxiliaryTextResponse(content="PASS", model="control-model"),
    )
    monkeypatch.setattr(
        verification_providers,
        "_record_verification_activity",
        lambda event_type, _message, **details: events.append((event_type, details)),
    )

    verification_providers.run_model_verification_review(
        provider="main",
        task="orchestration",
        prompt="choose a route",
        timeout_s=7,
    )

    assert [event_type for event_type, _details in events] == [
        "verification-review-request",
        "verification-review-result",
    ]
    request = events[0][1]
    result = events[1][1]
    assert request["review_id"] == result["review_id"]
    assert request["review_id"]
    assert request["timeout_s"] == result["timeout_s"] == 7
    assert request["elapsed_s"] == 0.0
    assert result["elapsed_s"] == 2.5


def test_command_review_correlates_request_and_result_telemetry(monkeypatch):
    events: list[tuple[str, dict]] = []
    times = iter((200.0, 201.25))
    monkeypatch.setattr(verification_providers.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        verification_providers,
        "run_command_expert_help",
        lambda **_kwargs: SimpleNamespace(
            provider="codex",
            response="PASS",
            command=["codex", "exec"],
            exit_status=0,
            truncated=False,
            response_chars=4,
            max_response_chars=1000,
            timed_out=False,
        ),
    )
    monkeypatch.setattr(
        verification_providers,
        "_record_verification_activity",
        lambda event_type, _message, **details: events.append((event_type, details)),
    )

    verification_providers.run_command_verification_review(
        provider="codex",
        task="blueprint_verification",
        prompt="review this blueprint",
        timeout_s=11,
    )

    request = events[0][1]
    result = events[1][1]
    assert request["review_id"] == result["review_id"]
    assert request["timeout_s"] == result["timeout_s"] == 11
    assert request["elapsed_s"] == 0.0
    assert result["elapsed_s"] == 1.25


def test_model_review_reports_hard_timeout(monkeypatch):
    def timeout(**_kwargs):
        raise verification_providers.IsolatedAuxiliaryTimeout("auxiliary call exceeded 7 seconds")

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(verification_providers, "run_isolated_auxiliary_text", timeout)
    monkeypatch.setattr(
        verification_providers,
        "_record_verification_activity",
        lambda event_type, _message, **details: events.append((event_type, details)),
    )

    result = verification_providers.run_model_verification_review(
        provider="main",
        task="orchestration",
        prompt="choose a route",
        timeout_s=7,
    )

    assert result.status == "timeout"
    assert result.timed_out is True
    assert result.response == ""
    assert "7 seconds" in result.error
    assert events[-1][0] == "verification-review-result"
    assert events[-1][1]["status"] == "timeout"
    assert events[-1][1]["timed_out"] is True


def test_model_review_preserves_unavailable_status(monkeypatch):
    def unavailable(**_kwargs):
        raise verification_providers.IsolatedAuxiliaryUnavailable("provider missing")

    monkeypatch.setattr(verification_providers, "run_isolated_auxiliary_text", unavailable)
    monkeypatch.setattr(
        verification_providers,
        "_record_verification_activity",
        lambda *_args, **_kwargs: None,
    )

    result = verification_providers.run_model_verification_review(
        provider="main",
        task="orchestration",
        prompt="choose a route",
        timeout_s=7,
    )

    assert result.status == "unavailable"
    assert result.timed_out is False
    assert result.error == "provider missing"


def test_model_review_redacts_errors_before_activity_telemetry(monkeypatch):
    secret = "sk-telemetryCredential1234567890"

    def failed(**_kwargs):
        raise IsolatedAuxiliaryError(f"Authorization: Bearer {secret}")

    events: list[tuple[str, dict]] = []
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    monkeypatch.setattr(verification_providers, "run_isolated_auxiliary_text", failed)
    monkeypatch.setattr(
        verification_providers,
        "_record_verification_activity",
        lambda event_type, _message, **details: events.append((event_type, details)),
    )

    result = verification_providers.run_model_verification_review(
        provider="main",
        task="orchestration",
        prompt="choose a route",
        timeout_s=7,
    )

    assert result.status == "error"
    assert secret not in result.error
    assert "Bearer [REDACTED]" in result.error
    assert secret not in events[-1][1]["error"]


def test_model_review_records_resolved_identity_on_error(monkeypatch):
    def failed(**_kwargs):
        raise IsolatedAuxiliaryError(
            "Connection error.",
            provider="custom",
            model="zai-org/GLM-5.2",
        )

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(verification_providers, "run_isolated_auxiliary_text", failed)
    monkeypatch.setattr(
        verification_providers,
        "_record_verification_activity",
        lambda event_type, _message, **details: events.append((event_type, details)),
    )

    result = verification_providers.run_model_verification_review(
        provider="auto",
        task="orchestration",
        prompt="choose a route",
        timeout_s=7,
    )

    assert result.status == "error"
    assert result.provider == "custom"
    assert result.model == "zai-org/GLM-5.2"
    assert events[-1][1]["provider"] == "custom"
    assert events[-1][1]["requested_provider"] == "auto"
    assert events[-1][1]["model"] == "zai-org/GLM-5.2"
