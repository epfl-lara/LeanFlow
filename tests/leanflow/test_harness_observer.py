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
    "code,summary",
    [
        ("check_busy", "Another Lean check still owns this workspace."),
        ("check_cancelled", "This Lean check was cancelled."),
        ("check_capture_failed", "Lean check output capture was interrupted."),
        ("check_environment_changed", "The Lean environment changed during this check."),
        ("check_request_too_large", "The Lean check request exceeded its size limit."),
        ("check_response_too_large", "Lean check feedback exceeded its size limit."),
        ("check_setup_failed", "The Lean checker could not complete this request."),
        ("check_timeout", "The Lean check exceeded its time limit."),
        ("isolated_lean_check_failed", "The isolated Lean checker could not complete this check."),
        ("isolation_unavailable", "The required Lean check isolation is unavailable."),
    ],
)
def test_native_observer_exposes_only_allowlisted_check_failure_summaries(observer, code, summary):
    observer.start({"status": "running"})
    observer.event(
        "tool-result",
        {
            "tool": "start_lean_check",
            "operation_id": "check-1",
            "arguments": {"file": "/PRIVATE/source.lean"},
            "failure_summary": "PRIVATE caller-written summary",
            "result": {
                "success": False,
                "ok": False,
                "error_code": code,
                "error": "/PRIVATE/path arbitrary-opaque-secret-value",
                "stdout": "PRIVATE output",
            },
        },
    )
    details = _events(observer)[-1]["details"]
    assert details["success"] is False
    assert details["error_code"] == code
    assert details["failure_summary"] == summary
    assert details["operation_id"] == "check-1"
    assert not {"arguments", "error", "result", "stdout", "ok"} & details.keys()
    assert "PRIVATE" not in json.dumps(_events(observer))
    assert "arbitrary-opaque-secret-value" not in json.dumps(_events(observer))


@pytest.mark.parametrize("envelope", [None, "completed", "successful_transport"])
def test_native_observer_prioritizes_failed_check_over_successful_transport(observer, envelope):
    observer.start({"status": "running"})
    result = {
        "success": False,
        "error_code": "check_timeout",
        "error": "PRIVATE checker output",
    }
    if envelope == "completed":
        result = {"status": "completed", "success": True, "result": result}
    elif envelope == "successful_transport":
        result = {"success": True, "result": result}
    observer.event(
        "tool-result",
        {"tool": "start_lean_check", "operation_id": "nested-1", "success": True, "result": result},
    )
    details = _events(observer)[-1]["details"]
    assert details["success"] is False
    assert details["error_code"] == "check_timeout"
    assert details["failure_summary"] == "The Lean check exceeded its time limit."
    assert details["operation_id"] == "nested-1"
    assert "PRIVATE" not in json.dumps(details)


@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "error_code": "PRIVATE_" + "x" * 10000},
        {"success": False, "error": "PRIVATE exception arbitrary-opaque-secret-value"},
        {"success": False, "error": ""},
        {"status": "failed", "error": ""},
        {"success": False, "error_code": {"PRIVATE": "unhashable"}},
    ],
)
def test_native_observer_unknown_check_failures_keep_generic_receipt_only(observer, result):
    observer.start({"status": "running"})
    observer.event(
        "tool-result",
        {"tool": "start_lean_check", "operation_id": "unknown-1", "result": result},
    )
    details = _events(observer)[-1]["details"]
    assert details["success"] is False
    assert details["operation_id"] == "unknown-1"
    assert (
        details["failure_summary"]
        == "The Lean check failed; details remain in the private tool log."
    )
    assert "error_code" not in details and "error" not in details
    assert "PRIVATE" not in json.dumps(details)
    assert "arbitrary-opaque-secret-value" not in json.dumps(details)


def test_native_observer_does_not_promote_codes_from_other_tools_or_successes(observer):
    observer.start({"status": "running"})
    for kind, tool, success in [
        ("tool-start", "start_lean_check", False),
        ("tool-result", "start_command", False),
        ("tool-result", "start_lean_check", True),
    ]:
        observer.event(
            kind,
            {
                "tool": tool,
                "operation_id": "other-1",
                "result": {"success": success, "error_code": "check_timeout"},
            },
        )
        details = _events(observer)[-1]["details"]
        assert "error_code" not in details and "failure_summary" not in details


@pytest.mark.parametrize("error", ["", None, "PRIVATE rejected-candidate diagnostic"])
def test_native_observer_successful_check_execution_is_not_a_transport_failure(observer, error):
    observer.start({"status": "running"})
    observer.event(
        "tool-result",
        {
            "tool": "start_lean_check",
            "operation_id": "checked-1",
            "result": {"success": True, "ok": False, "error": error},
        },
    )
    details = _events(observer)[-1]["details"]
    assert details["success"] is True
    assert "failure_summary" not in details and "error_code" not in details
    assert "PRIVATE" not in json.dumps(details)


@pytest.mark.parametrize("error", ["", "PRIVATE exception without a failure flag"])
def test_native_observer_does_not_infer_failure_from_raw_error_text(observer, error):
    observer.start({"status": "running"})
    observer.event(
        "tool-result",
        {
            "tool": "start_lean_check",
            "operation_id": "unclassified-1",
            "result": {"error": error, "error_code": "check_timeout"},
        },
    )
    details = _events(observer)[-1]["details"]
    assert not {"success", "failure_summary", "error_code", "error"} & details.keys()
    assert details["operation_id"] == "unclassified-1"


def test_native_observer_failure_metadata_is_bounded_and_forcibly_redacted(observer):
    observer._secrets += ("Lean check",)
    observer.start({"status": "running"})
    observer.event(
        "tool-result",
        {
            "tool": "start_lean_check",
            "operation_id": "arbitrary-opaque-secret-value" + "x" * 1000,
            "result": {"success": False, "error_code": "check_timeout"},
        },
    )
    details = _events(observer)[-1]["details"]
    assert len(details["operation_id"]) == 200
    assert "arbitrary-opaque-secret-value" not in details["operation_id"]
    assert details["failure_summary"] == "The [REDACTED] exceeded its time limit."
    assert len(details["failure_summary"]) <= 200


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
