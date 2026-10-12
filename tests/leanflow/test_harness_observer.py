"""Keep native workflow status discoverable without exposing private agent content."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.campaign_limits import CampaignLimits
from leanflow_cli.native.harness_observer import NativeObserver
from leanflow_cli.workflows.workflow_state import (
    load_workflow_live_status,
    workflow_run_activity_path,
    workflow_run_metadata_path,
)


@pytest.fixture
def observer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(root))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "outer-run")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "test-launch-token")
    monkeypatch.setenv("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", "PRIVATE source prompt")
    monkeypatch.setenv("LEMEX_API_KEY", "arbitrary-opaque-secret-value")
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "false")
    return NativeObserver(root, "native-run", "formalize", "lemex")


def _events(observer):
    return [
        json.loads(line)
        for line in workflow_run_activity_path(observer.run_id).read_text().splitlines()
    ]


def test_native_observer_claims_same_run_before_activity(observer):
    assert os.environ["LEANFLOW_WORKFLOW_RUN_ID"] == "outer-run"
    observer.start({"status": "running", "final_response": "PRIVATE transcript"})
    assert os.environ["LEANFLOW_WORKFLOW_RUN_ID"] == observer.run_id
    live = load_workflow_live_status()
    assert live["run_id"] == observer.run_id
    assert live["process_id"] == os.getpid()
    assert live["process_token_sha256"]
    assert live["phase"] == "starting" and not live["terminal"]
    assert live["provider"] == "lemex"
    assert live["native_state_path"].endswith("native-workflows/native-run/state.json")
    events = _events(observer)
    assert [event["type"] for event in events] == ["runner-start"]
    assert events[0]["run_id"] == live["run_id"]
    metadata = json.loads(workflow_run_metadata_path(observer.run_id).read_text())
    assert metadata["workflow_kind"] == "formalize"
    assert metadata["process_id"] == os.getpid()
    assert "PRIVATE" not in json.dumps([live, events, metadata])


def test_native_observer_budget_keeps_unknown_usage_and_ignores_refunded_calls(observer):
    observer.start({"status": "running"})
    budget = CampaignLimits(observer.root / "limits.json", {"agent_calls": 4})
    known = budget.reserve()
    budget.finish(known, {"input_tokens": 7, "output_tokens": 3}, started=True)
    missing = budget.reserve()
    budget.finish(missing, None, started=True)
    refund = budget.reserve()
    budget.finish(refund, None, started=False)
    observer.publish({"status": "running", "session_id": "native-session"}, budget, "reviewing")
    live = load_workflow_live_status()
    assert live["phase"] == "reviewing"
    assert live["native_session_id"] == "native-session"
    assert live["runtime_heartbeat_at"]
    metrics = live["native_metrics"]
    assert metrics["agent_calls"] == 2
    assert metrics["known_reported_tokens"] == 10
    assert metrics["reported_tokens"] is None and metrics["usage_known"] is False
    assert metrics["limits"]["agent_calls"] == 4


def test_native_observer_keeps_only_receipt_scalars_and_redacts_secrets(observer):
    observer.start({"status": "running"})
    observer.event(
        "tool-result",
        {
            "tool": "submit_files",
            "operation_id": "operation-1",
            "arguments": {"response": "PRIVATE model transcript"},
            "result": {
                "accepted": False,
                "status": "rejected",
                "error": "PRIVATE tool output arbitrary-opaque-secret-value",
                "files": {"Main.lean": "PRIVATE source"},
            },
        },
    )
    observer.publish({"status": "failed", "error": "API rejected arbitrary-opaque-secret-value"})
    events = _events(observer)
    details = events[-1]["details"]
    assert details["accepted"] is False and details["status"] == "rejected"
    assert details["operation_id"] == "operation-1"
    assert "arguments" not in details and "result" not in details and "error" not in details
    encoded = json.dumps([events, load_workflow_live_status()])
    assert "PRIVATE" not in encoded and "arbitrary-opaque-secret-value" not in encoded
    assert "[REDACTED]" in encoded


def test_native_observer_publishes_model_metadata_without_private_config(observer):
    observer.start({"status": "running"})
    observer.event(
        "agent-settings",
        {
            "invocation_id": "call-1",
            "model": "actual-model",
            "reasoning_effort": "high",
            "model_source": "native_session",
            "reasoning_source": "native_session",
            "configuration": {"API_KEY": "arbitrary-opaque-secret-value"},
        },
    )
    event = _events(observer)[-1]
    assert event["type"] == "agent-settings"
    assert event["details"]["model"] == "actual-model"
    assert event["details"]["reasoning_effort"] == "high"
    assert "configuration" not in event["details"]


@pytest.mark.parametrize(
    "status,code", [("draft_ready", 0), ("completed", 0), ("blocked", 1), ("cancelled", 130)]
)
def test_native_observer_terminal_state_is_not_overwritten_by_late_publish(observer, status, code):
    observer.start({"status": "running"})
    observer.finish({"status": status})
    observer.publish({"status": "running"})
    observer.finish({"status": "running"})
    observer.event("tool-result", {"accepted": True})
    live = load_workflow_live_status()
    assert live["status"] == status and live["terminal"]
    events = _events(observer)
    assert [event["type"] for event in events] == ["runner-start", "runner-exit"]
    assert events[-1]["details"]["exit_code"] == code


def test_native_observer_unfinished_exit_is_an_error(observer):
    observer.start({"status": "running"})
    observer.finish({"status": "running"})
    live = load_workflow_live_status()
    assert live["status"] == "error" and live["terminal"]
    assert "without a terminal result" in live["current_blocker"]
