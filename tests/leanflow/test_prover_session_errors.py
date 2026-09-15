"""Preserve safe SDK causes without exposing request credentials or payloads."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from leanflow_cli.workflows.prover.session_errors import request_error_details


def test_real_sdk_timeout_retains_transport_cause():
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    error = openai.APITimeoutError(request=request)
    error.__cause__ = httpx.ReadTimeout("The read operation timed out", request=request)
    details = request_error_details(error)
    assert details["exception_type"] == "APITimeoutError"
    assert details["message"] == "Request timed out."
    assert details["causes"] == [
        {"exception_type": "ReadTimeout", "message": "The read operation timed out"}
    ]


def test_real_sdk_status_error_keeps_safe_identifiers():
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://provider.invalid"),
        headers={"x-request-id": "req-safe-123", "authorization": "Bearer ignored-secret"},
    )
    error = openai.RateLimitError(
        "Too many requests", response=response, body={"code": "rate_limit_exceeded"}
    )
    details = request_error_details(error)
    assert details["status_code"] == 429
    assert details["code"] == "rate_limit_exceeded"
    assert details["request_id"] == "req-safe-123"
    assert "ignored-secret" not in json.dumps(details)


def test_details_never_inspect_request_response_body_or_headers():
    class GuardedError(Exception):
        def __getattribute__(self, name):
            if name in {"request", "response", "body", "headers", "client"}:
                pytest.fail("diagnostics inspected private request or response material")
            return super().__getattribute__(name)

    error = GuardedError("Transport stopped")
    error.status_code = 503
    error.request_id = "req-safe"
    assert request_error_details(error)["request_id"] == "req-safe"


def test_credentials_are_removed_even_when_optional_redaction_is_disabled(monkeypatch):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    key = "custom-private-api-key"
    error = RuntimeError(f"Connection error using {key}; sk-testkey123456789")
    error.__cause__ = OSError(
        "Connect https://user:password@example.invalid/private?auth=hidden-query failed; "
        "Authorization: Basic secret-header"
    )
    payload = json.dumps(request_error_details(error, exact_secrets=(key,)))
    for secret in (key, "sk-testkey123456789", "password", "hidden-query", "secret-header"):
        assert secret not in payload
    assert "OSError" in payload


def test_error_records_are_bounded_and_cycles_are_not_followed():
    error = RuntimeError("x" * 5000)
    error.__cause__ = ValueError("cause")
    error.__cause__.__cause__ = error
    details = request_error_details(error)
    assert len(details["message"]) == 1000
    assert len(details["causes"]) == 1
    assert details["cause_chain_truncated"]
    chain = RuntimeError("first")
    current = chain
    for _ in range(10):
        current.__cause__ = RuntimeError("next")
        current = current.__cause__
    details = request_error_details(chain)
    assert len(details["causes"]) == 3
    assert details["cause_chain_truncated"]


def test_suppressed_context_is_not_reported_but_explicit_cause_is():
    error = RuntimeError("current")
    error.__context__ = ValueError("suppressed")
    error.__suppress_context__ = True
    assert "causes" not in request_error_details(error)
    error.__cause__ = OSError("explicit")
    assert request_error_details(error)["causes"][0]["message"] == "explicit"


def test_malformed_metadata_does_not_get_serialized():
    error = RuntimeError("failure")
    error.status_code = True
    error.code = {"payload": "private-body"}
    error.request_id = SimpleNamespace(secret="private-header")
    assert request_error_details(error) == {
        "exception_type": "RuntimeError",
        "message": "failure",
    }


def test_exception_formatting_failure_does_not_hide_its_type():
    class UnprintableError(Exception):
        def __str__(self):
            raise RuntimeError("printing failed")

        @property
        def code(self):
            raise RuntimeError("reading failed")

    assert request_error_details(UnprintableError())["exception_type"] == "UnprintableError"


def test_safe_diagnostics_survive_job_controller_and_campaign_then_clear_on_resume(tmp_path):
    from leanflow_cli.workflows.prover.config import ProverConfig
    from leanflow_cli.workflows.prover.runtime import ProverRuntime
    from scripts.lean_imo_campaign.recovery import schedule_recovery
    from scripts.lean_imo_campaign.runner import refresh
    from tests.leanflow.test_prover_runtime import Verifier, project

    path = project(tmp_path)
    cause = httpx.ReadTimeout("The read operation timed out")
    error = TimeoutError("Request timed out.")
    error.__cause__ = cause
    details = request_error_details(error)

    def unavailable(**_):
        return {
            "status": "provider_error",
            "api_calls": 1,
            "error": details["message"],
            "error_details": details,
        }

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        session=unavailable,
        verifier=Verifier(),
    )
    state = runtime.run()
    assert state["error_details"] == details
    assert state["jobs"][0]["error_details"] == details
    cell = {"project": str(tmp_path), "run_id": runtime.run_id, "status": state["status"]}
    refresh(cell)
    assert cell["error_details"] == details
    assert schedule_recovery(cell)
    assert cell["executions"][0]["error_details"] == details
    resumed = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(),
        run_id=runtime.run_id,
        resume=True,
        session=unavailable,
        verifier=Verifier(),
    )
    assert "error_details" not in resumed.state
    refresh(cell)
    assert "error" not in cell
    assert "error_details" not in cell
