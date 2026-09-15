"""Preserve transport causes through session reports and activity without credentials."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from leanflow_cli.workflows.prover import agent_session as session


def test_session_failure_keeps_safe_causes_in_report_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the session exception boundary using a real SDK exception and cause."""
    secret = "private-route-credential"
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    error = openai.APIConnectionError(request=request)
    error.__cause__ = httpx.ConnectError(f"Connection reset using {secret}", request=request)
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    monkeypatch.setattr(
        session, "build_transport", lambda *_: SimpleNamespace(model="test", api_key=secret)
    )
    monkeypatch.setattr(session, "close_transport", lambda *_: None)

    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(session, "request_once", fail)
    result = session.run_session(
        role="orchestrator",
        prompt="Return the plan.",
        project_root=tmp_path,
        workspace=tmp_path / "job",
        config={"model": "test"},
        api_budget=50,
        log_path=tmp_path / "events.jsonl",
        context={},
    )
    assert result["status"] == "provider_error"
    assert result["api_calls"] == 1
    assert result["error"] == "Connection error."
    details = result["error_details"]
    assert details["exception_type"] == "APIConnectionError"
    assert details["causes"][0]["exception_type"] == "ConnectError"
    events_text = (tmp_path / "events.jsonl").read_text()
    report_text = (tmp_path / "job/report.json").read_text()
    assert secret not in events_text + report_text + json.dumps(result)
    event = next(json.loads(e) for e in events_text.splitlines() if '"api-error"' in e)
    assert event["details"]["error_details"] == details
    assert json.loads(report_text)["error_details"] == details
