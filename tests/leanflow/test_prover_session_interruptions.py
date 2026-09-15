"""Keep controller cancellation distinct from provider errors and admissions."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover import agent_session
from leanflow_cli.workflows.prover.runtime import InfrastructureFailure
from leanflow_cli.workflows.prover.session_admission import provider_request_slot
from tests.leanflow.test_prover_live_progress import make_runtime


def test_cancelled_response_callback_preserves_usage_and_interruption(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    runtime.session = agent_session.run_session
    adapter = SimpleNamespace(model="test", base_url="https://provider.invalid")
    monkeypatch.setattr(agent_session, "build_transport", lambda *_: adapter)
    monkeypatch.setattr(agent_session, "close_transport", lambda *_: None)

    def response(*_, **__):
        runtime.cancelled.set()
        return {"role": "assistant", "content": "Response arriving after sibling failure"}, {
            "input_tokens": 17,
            "output_tokens": 5,
        }

    monkeypatch.setattr(agent_session, "request_once", response)
    job, context = runtime._new_job("prover", node=runtime.dag.nodes[0])
    result = runtime._invoke(job, context, "Prove the goal")
    assert result["status"] == "interrupted", result
    assert result["api_calls"] == 1
    assert result["input_tokens"] == job["input_tokens"] == 17
    assert result["output_tokens"] == job["output_tokens"] == 5
    report = json.loads((runtime.store.directory / "jobs" / job["id"] / "report.json").read_text())
    assert report["status"] == "interrupted"


def test_already_cancelled_callback_does_not_become_runtime_error(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.session = agent_session.run_session
    job, context = runtime._new_job("prover", node=runtime.dag.nodes[0])
    runtime.cancelled.set()
    result = runtime._invoke(job, context, "Prove the goal")
    assert result["status"] == "interrupted", result
    assert result["api_calls"] == 0


@pytest.mark.parametrize(
    "status", ["error", "provider_error", "environment_error", "source_conflict", "context_limit"]
)
def test_session_failure_kind_survives_controller_stop(tmp_path, status):
    runtime = make_runtime(tmp_path)
    job, _ = runtime._new_job("research", prompt="Inspect the local goal")
    with pytest.raises(InfrastructureFailure) as raised:
        runtime._finish_job(job, {"status": status, "api_calls": 1, "error": "recorded failure"})
    assert raised.value.status == status
    assert job["status"] == status


@pytest.mark.parametrize("at_wait", [False, True])
def test_controller_cancellation_before_admission_does_not_spend_call(
    tmp_path, monkeypatch, at_wait
):
    runtime = make_runtime(tmp_path)
    runtime.session = agent_session.run_session
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    adapter = SimpleNamespace(
        model="test",
        base_url="https://inference.rcp.epfl.ch/v1" if at_wait else "https://provider.invalid",
        api_key="test-queue-key",
    )

    def build(*_):
        if not at_wait:
            runtime.cancelled.set()
        return adapter

    original_event = runtime._session_event

    def event(job, kind, details):
        if kind == "provider-wait":
            runtime.cancelled.set()
        original_event(job, kind, details)

    monkeypatch.setattr(runtime, "_session_event", event)
    monkeypatch.setattr(agent_session, "build_transport", build)
    monkeypatch.setattr(agent_session, "close_transport", lambda *_: None)
    monkeypatch.setattr(
        agent_session, "request_once", lambda *_: pytest.fail("cancelled request was sent")
    )
    job, context = runtime._new_job("prover", node=runtime.dag.nodes[0])
    # The busy RCP slot forces the real controller callback through provider-wait.
    with provider_request_slot(
        adapter, deadline=time.monotonic() + 2, cancelled=lambda: False, on_wait=lambda: None
    ):
        result = runtime._invoke(job, context, "Prove the goal")
    assert result["status"] == "interrupted", result
    assert result["api_calls"] == job["api_calls"] == 0
    ledger = runtime.store.directory / "jobs/.runtime" / job["id"] / "request-count.json"
    assert json.loads(ledger.read_text())["used"] == 0
