"""Keep deterministic worker planning without the retired in-process SDK path."""

from __future__ import annotations

from leanflow_cli.lean import lean_worker_dispatch
from leanflow_cli.lean.lean_models import LeanWorkerRequest


def test_legacy_worker_delegation_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(lean_worker_dispatch, "artifact_context_block", lambda: "")
    outcomes = []
    monkeypatch.setattr(
        lean_worker_dispatch, "append_workflow_outcome", lambda *args: outcomes.append(args)
    )
    request = LeanWorkerRequest(
        worker="prove", goal="prove True", context="", allow_delegation=True, use_file_lock=False
    )
    result = lean_worker_dispatch.dispatch_worker(request, parent_agent=object())
    assert not result.dispatched
    assert result.mode == "native_required"
    assert "codex, claude, or lemex" in result.summary
    assert outcomes[0][1]["dispatched"] is False


def test_worker_plan_remains_available_without_an_agent(monkeypatch):
    monkeypatch.setattr(lean_worker_dispatch, "artifact_context_block", lambda: "")
    monkeypatch.setattr(lean_worker_dispatch, "append_workflow_outcome", lambda *args: None)
    result = lean_worker_dispatch.dispatch_worker(
        LeanWorkerRequest(worker="prove", goal="prove True", context="", use_file_lock=False)
    )
    assert result.mode == "plan"
    assert not result.dispatched
    assert "prove True" in result.summary
