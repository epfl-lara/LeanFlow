"""Show current owned preparation progress without leaking text or overwriting a later owner."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent.runtime.harness import HarnessResult
from core.campaign_limits import CampaignLimits
from leanflow_cli.formalization import preflight_owner as owner
from leanflow_cli.formalization import preparation_discovery as discovery
from leanflow_cli.formalization import preparation_launch as launch
from leanflow_cli.runtime import project_operations
from leanflow_cli.workflows import workflow_json_io as json_io
from leanflow_cli.workflows import workflow_state
from tests.leanflow.test_formalization_preparation_discovery import (
    _response,
)
from tests.leanflow.test_formalization_preparation_discovery import (
    paper as _paper,
)

paper = _paper


class Resources:
    """Keep visibility fixtures independent of actual subprocess/resource experiments."""

    def __init__(self, environment, cancelled):
        self.cancelled = cancelled
        self.failure = None
        self.closed = False

    def start(self):
        pass

    def check(self, **kwargs):
        pass

    def close(self):
        self.closed = True

    def evidence(self):
        return {"cleanup_verified": self.closed}


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "formalize-current-progress")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "owned-progress-token")
    monkeypatch.setattr(owner, "inherited_process_token", lambda: True)
    monkeypatch.setattr(owner, "WorkflowResourceWindow", Resources)
    workflow_state.save_workflow_live_status(
        {
            "phase": "completed",
            "terminal": True,
            "proof_solved": 17,
            "native_metrics": {"agent_calls": 99},
            "native_session_id": "previous-session",
            "current_blocker": "Previous mathematical ambiguity",
            "preflight_error": "Earlier preparation failure",
            "preflight_error_code": "earlier_error",
        }
    )
    return tmp_path


def _status():
    return workflow_state.read_json_file(workflow_state.workflow_live_status_path())


@pytest.fixture
def progress(preflight, monkeypatch):
    monkeypatch.setattr(owner.PreflightProgress, "start", lambda self: None)
    with owner.preflight_owner(preflight, dict(owner.os.environ)) as (_, _, progress):
        yield progress


def _activities(progress):
    return [json.loads(line) for line in progress.activity_path.read_text().splitlines()]


@contextmanager
def _held_status_lock(path, *, sidecar=True):
    """Hold the actual sidecar in an independent process with fixture-owned release."""
    lock_path = path.with_suffix(path.suffix + ".lock") if sidecar else path
    ready = path.with_suffix(".holder-ready")
    script = """
import fcntl, pathlib, sys
with pathlib.Path(sys.argv[1]).open('a') as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    pathlib.Path(sys.argv[2]).write_text('ready')
    sys.stdin.buffer.read(1)
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(lock_path), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.01)
        assert ready.exists()
        yield process
    finally:
        try:
            if process.stdin:
                process.stdin.write(b"release")
                process.stdin.close()
            process.wait(timeout=3)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if process.stderr:
                process.stderr.close()


def test_new_preflight_clears_old_native_metrics_but_preserves_prior_math(preflight):
    with owner.preflight_owner(preflight, dict(owner.os.environ)):
        value = _status()
        assert value["native_metrics"] == {}
        assert value["native_session_id"] == ""
        assert value["proof_solved"] == 17 and value["startup_reconciliation_pending"] is True
        assert value["current_blocker"] == "Previous mathematical ambiguity"
        assert value["preflight_error"] == value["preflight_error_code"] == ""


def test_owned_phase_and_actual_shared_budget_replace_prior_run_counts(progress, preflight):
    budget = CampaignLimits(preflight / "budget.json", {"agent_calls": 64, "wall_time_s": 7200})
    progress.bind_budget(budget)
    progress.phase({"phase": "dependency_plan", "message": "untrusted source text"})
    call = budget.reserve()
    budget.started_call(call)
    progress._publish()
    value = _status()
    assert value["phase"] == "dependency_plan"
    assert value["native_metrics"]["agent_calls"] == 1
    assert value["native_metrics"]["running_calls"] == 1
    assert value["native_metrics"]["reported_tokens"] is None
    assert value["native_metrics"]["limits"] == budget.limits
    assert value["proof_solved"] == 17 and value["startup_reconciliation_pending"] is True
    assert value["current_blocker"] == "Previous mathematical ambiguity"
    assert value["last_activity_message"] == "Estimating mathematical dependencies"
    budget.finish(call, {"input_tokens": 40, "output_tokens": 3}, started=True)
    progress._publish()
    assert _status()["native_metrics"]["reported_tokens"] == 43
    assert _status()["native_metrics"]["running_calls"] == 0
    assert "untrusted source text" not in progress.activity_path.read_text()


def test_heartbeat_refreshes_liveness_without_advancing_actual_activity_time(progress):
    progress.phase({"phase": "dependency_plan"})
    progress._publish()
    first = _status()
    progress._publish()
    second = _status()
    assert second["runtime_heartbeat_at"] > first["runtime_heartbeat_at"]
    assert second["last_activity_at"] == first["last_activity_at"]
    assert len(_activities(progress)) == 1
    progress.native_event({"type": "item.completed", "item": {"type": "mcp_tool_call"}})
    progress._publish()
    third = _status()
    assert third["last_activity_at"] > second["last_activity_at"]
    assert third["last_activity_message"] == "Dependency planner is reading frozen evidence"
    progress.native_event({"type": "item.completed", "item": {"type": "mcp_tool_call"}})
    progress._publish()
    assert _status()["last_activity_at"] > third["last_activity_at"]


def test_native_callbacks_are_throttled_metadata_and_do_not_take_file_or_budget_locks(
    progress, monkeypatch, preflight
):
    budget = CampaignLimits(preflight / "budget.json", {})
    progress.bind_budget(budget)
    original = owner.publish_workflow_preflight_progress
    writes = []

    def publish(*args, **kwargs):
        assert not budget.lock._is_owned()
        writes.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, "publish_workflow_preflight_progress", publish)
    for _ in range(100):
        progress.native_event({"type": "item.completed", "item": {"type": "mcp_tool_call"}})
    assert not writes
    progress._publish()
    assert len(writes) == len(_activities(progress)) == 1
    assert _status()["native_metrics"]["agent_calls"] == 0


def test_callback_settings_are_redacted_before_truncation_and_never_persist_prompts(
    preflight, monkeypatch
):
    secret = "credential-material-" * 40
    monkeypatch.setenv("EXAMPLE_API_KEY", secret)
    monkeypatch.setattr(owner.PreflightProgress, "start", lambda self: None)
    with owner.preflight_owner(preflight, dict(owner.os.environ)) as (_, _, progress):
        progress.native_event(
            {
                "type": "harness.settings",
                "model": secret,
                "reasoning_effort": "max",
                "prompt": "raw prompt must not persist",
                "env": {"EXAMPLE_API_KEY": secret},
            }
        )
        progress.native_event(
            {"type": "item.completed", "item": {"type": "agent_message", "text": secret}}
        )
        progress._publish()
        events = _activities(progress)
        assert events[-1]["type"] == "agent-settings"
        assert events[-1]["details"]["model"] == "[REDACTED]"
        assert events[-1]["details"]["reasoning_effort"] == "max"
        progress.native_event(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "arguments": "private tool arguments",
                    "result": secret,
                },
            }
        )
        progress._publish()
        persisted = progress.status_path.read_text() + progress.activity_path.read_text()
        for text in (secret[:500], "raw prompt must not persist", "private tool arguments"):
            assert text not in persisted


@pytest.mark.parametrize(
    "replacement",
    [
        {"run_id": "new-controller-run"},
        {"owner_role": "native-controller"},
        {"process_token_sha256": "0" * 64},
        {"process_id": 99999999},
    ],
)
def test_progress_rejects_later_owner_without_mutating_its_status_or_run_sidecar(
    preflight, monkeypatch, replacement
):
    monkeypatch.setattr(owner.PreflightProgress, "start", lambda self: None)
    with pytest.raises(RuntimeError, match="before handoff"):
        with owner.preflight_owner(preflight, dict(owner.os.environ)) as (_, _, progress):
            progress._publish()
            workflow_state.save_workflow_live_status(
                {
                    **_status(),
                    "phase": "translation",
                    "native_metrics": {"agent_calls": 7},
                    **replacement,
                }
            )
            sidecar = workflow_state.workflow_run_metadata_path("new-controller-run")
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text('{"owner":"later-controller"}')
            sidecar_before = sidecar.read_bytes()
            before = progress.status_path.read_bytes()
            activity_before = progress.activity_path.read_bytes()
            progress.native_event({"type": "turn.started"})
            with pytest.raises(RuntimeError, match="ownership changed"):
                progress._publish()
    assert progress.status_path.read_bytes() == before
    assert progress.activity_path.read_bytes() == activity_before
    assert sidecar.read_bytes() == sidecar_before


def test_cross_process_locked_claim_is_bounded_and_never_admits_work(preflight):
    path = workflow_state.workflow_live_status_path()
    before = path.read_bytes()
    with _held_status_lock(path):
        started = time.monotonic()
        with pytest.raises(json_io.WorkflowStateLockTimeout, match="ownership retained"):
            with owner.preflight_owner(preflight, dict(owner.os.environ)):
                pytest.fail("Locked preflight claim admitted work")
        assert time.monotonic() - started < 2
        assert path.read_bytes() == before


def test_cross_process_lock_never_blocks_progress_close_or_final_release(preflight):
    holder = None
    handoff = False
    try:
        with pytest.raises(json_io.WorkflowStateLockTimeout, match="ownership retained"):
            with owner.preflight_owner(preflight, dict(owner.os.environ)) as (
                _,
                resources,
                progress,
            ):
                holder = _held_status_lock(progress.status_path)
                holder.__enter__()
                before = progress.status_path.read_bytes()
                started = time.monotonic()
                progress.phase({"phase": "dependency_plan"})
                progress._publish()
                assert time.monotonic() - started < 0.25
                # Busy status publication must not monopolize unrelated JSON writes.
                other = preflight / "unrelated.json"
                json_io.update_json_file(
                    other, lambda value: value.update(other=True), lock_timeout_s=0.1
                )
                assert json.loads(other.read_text()) == {"other": True}
            handoff = True
        assert time.monotonic() - started < 2
        assert not handoff and resources.closed
        assert progress._thread is not None and not progress._thread.is_alive()
        assert progress.status_path.read_bytes() == before
        retained = _status()
        assert retained["owner_role"] == "formalization-preflight"
        assert retained["process_id"] == owner.os.getpid() and retained["terminal"] is False
        assert "preflight_release" not in retained
    finally:
        if holder is not None:
            holder.__exit__(None, None, None)


def test_bounded_json_update_cannot_wait_for_the_process_lock_or_mutate_on_timeout(preflight):
    path = preflight / "locked.json"
    path.write_text('{"unchanged":true}')
    with json_io._WRITE_LOCK:
        started = time.monotonic()
        with pytest.raises(json_io.WorkflowStateLockTimeout):
            json_io.update_json_file(
                path, lambda value: pytest.fail("Mutation under unowned lock"), lock_timeout_s=0.05
            )
        assert time.monotonic() - started < 0.2
    assert path.read_text() == '{"unchanged":true}'


def test_unjoined_progress_writer_retains_ownership_and_cannot_handoff(preflight, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocked_append(*args, **kwargs):
        entered.set()
        assert release.wait(6)

    monkeypatch.setattr(workflow_state, "_locked_append", blocked_append)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="progress did not stop"):
            with owner.preflight_owner(preflight, dict(owner.os.environ)) as (
                _,
                resources,
                progress,
            ):
                assert entered.wait(1)
        assert time.monotonic() - started < 4
        assert resources.closed and progress._thread.is_alive()
        assert _status()["owner_role"] == "formalization-preflight"
        assert "preflight_release" not in _status()
    finally:
        release.set()
        progress._thread.join(2)
        assert not progress._thread.is_alive()


def test_quiet_preflight_checkpoints_and_cancels_under_real_activity_lock(preflight):
    """A suspended activity writer cannot starve the accounting worker or Stop."""
    run_id = owner.os.environ["LEANFLOW_WORKFLOW_RUN_ID"]
    path = workflow_state.workflow_run_activity_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    budget = CampaignLimits(preflight / "budget.json", {"wall_time_s": 30})
    with _held_status_lock(path, sidecar=False):
        with pytest.raises(ValueError, match="preparation cancelled"):
            with owner.preflight_owner(preflight, dict(owner.os.environ)) as (
                cancelled,
                _,
                progress,
            ):
                progress.bind_budget(budget)
                progress.phase({"phase": "dependency_plan"})
                deadline = time.monotonic() + 3
                while json.loads(budget.path.read_text())["elapsed_s"] < 0.9:
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                started = time.monotonic()
                cancelled.set()
        assert time.monotonic() - started < 0.5
        assert progress._thread is not None and not progress._thread.is_alive()
        assert not path.read_text()


def test_skipped_activity_append_does_not_invent_new_activity_each_heartbeat(progress):
    """Consume a phase shown in Live even when its best-effort history append is skipped."""
    progress.activity_path.parent.mkdir(parents=True, exist_ok=True)
    with _held_status_lock(progress.activity_path, sidecar=False):
        progress.phase({"phase": "dependency_plan"})
        progress._publish()
        first = _status()
        progress._publish()
        second = _status()
        assert second["runtime_heartbeat_at"] > first["runtime_heartbeat_at"]
        assert second["last_activity_at"] == first["last_activity_at"]
        assert not progress.activity_path.read_text()


def test_only_parent_clears_preflight_marker_after_worker_and_resource_effects(
    preflight, monkeypatch
):
    budget = CampaignLimits(preflight / "budget.json", {"wall_time_s": 30})
    original_close = Resources.close
    observed = []

    def close(resources):
        # This is the outer parent's final opportunity to mutate accounting.
        observed.append(json.loads(budget.path.read_text())["checkpoint_worker_active"])
        budget.persist()
        original_close(resources)

    monkeypatch.setattr(Resources, "close", close)
    with owner.preflight_owner(preflight, dict(owner.os.environ)) as (_, _, progress):
        progress.bind_budget(budget)
        assert json.loads(budget.path.read_text())["checkpoint_worker_active"]
    assert observed == [True]
    assert progress._thread is not None and not progress._thread.is_alive()
    assert not json.loads(budget.path.read_text()).get("checkpoint_worker_active")
    CampaignLimits(budget.path, budget.limits)


def test_close_joins_worker_and_late_callbacks_cannot_overwrite_handoff(preflight):
    with owner.preflight_owner(preflight, dict(owner.os.environ)) as (_, _, progress):
        progress.native_event({"type": "turn.started"})
    assert progress._thread is not None and not progress._thread.is_alive()
    before = progress.status_path.read_bytes(), progress.activity_path.read_bytes()
    progress.native_event({"type": "turn.started"})
    progress.phase({"phase": "dependency_plan"})
    assert (progress.status_path.read_bytes(), progress.activity_path.read_bytes()) == before
    assert _status()["preflight_release"]["outcome"] == "prepared"


def test_current_discovery_failure_is_separate_from_historical_mathematical_blocker(preflight):
    with pytest.raises(ValueError, match="Representation notes"):
        with owner.preflight_owner(preflight, dict(owner.os.environ)) as (_, _, progress):
            progress.phase({"phase": "dependency_plan"})
            error = ValueError(
                "Formalization preparation discovery needs correction: "
                "Representation notes must be bounded nonempty text"
            )
            progress.failed(error, code="preparation_discovery_error")
            raise error
    value = _status()
    assert value["preflight_error_code"] == "preparation_discovery_error"
    assert value["preflight_error"].endswith("Representation notes must be bounded nonempty text")
    assert value["last_activity_type"] == "preflight-error"
    assert value["current_blocker"] == "Previous mathematical ambiguity"
    assert value["proof_solved"] == 17 and value["startup_reconciliation_pending"] is True
    assert value["preflight_release"]["outcome"] == "failed"
    assert value["terminal"] is True


def test_reported_failure_keeps_active_owner_nonterminal_until_cleanup_releases_it(progress):
    progress.failed(ValueError("Discovery failed"), code="preparation_discovery_error")
    progress._publish()
    value = _status()
    assert value["preflight_error_code"] == "preparation_discovery_error"
    assert value["terminal"] is False and value["status"] == "running"


def test_public_preparation_launch_reports_the_real_discovery_failure_and_budget(
    preflight, paper, monkeypatch
):
    root, _, _ = paper
    assert root == preflight
    monkeypatch.setattr(launch, "inspect_project", lambda root: {"ready": True, "configured": True})
    monkeypatch.setattr(launch, "load_preparation_receipt", lambda root: None)
    monkeypatch.setattr(launch, "load_config", lambda: {"limits": {"agent_calls": 64}})

    def failed_discovery(*args, **kwargs):
        budget = kwargs["budget"]
        invocation = budget.reserve()
        budget.started_call(invocation)
        kwargs["on_event"]({"type": "turn.started"})
        progress = kwargs["on_event"].__self__
        progress._publish()
        assert _status()["phase"] == "dependency_plan"
        assert _status()["native_metrics"]["agent_calls"] == 1
        budget.finish(invocation, {"input_tokens": 9, "output_tokens": 4}, started=True)
        return {
            "accepted": False,
            "status": "preparation_discovery_error",
            "error": "Representation notes must be bounded nonempty text",
        }

    monkeypatch.setattr(launch, "discover_preparation_plan", failed_discovery)
    monkeypatch.setattr(
        launch, "prepare_document_project", lambda *args, **kwargs: pytest.fail("Unexpected setup")
    )
    with pytest.raises(ValueError, match="Representation notes"):
        launch.prepare_formalization_launch(
            root, "paper.tex", {"provider": "codex"}, environment=dict(owner.os.environ)
        )
    value = _status()
    assert value["preflight_release"]["outcome"] == "failed"
    assert value["preflight_error_code"] == "preparation_discovery_error"
    assert value["native_metrics"]["agent_calls"] == 1
    assert value["native_metrics"]["reported_tokens"] == 13
    assert value["current_blocker"] == "Previous mathematical ambiguity"
    assert value["preflight_error"].endswith("Representation notes must be bounded nonempty text")


def test_progress_publication_failure_cancels_and_cleanup_still_runs(preflight, monkeypatch):
    entered = threading.Event()
    allow_failure = threading.Event()
    instances = []

    class RecordedResources(Resources):
        def __init__(self, *args):
            super().__init__(*args)
            instances.append(self)

    def fail(*args, **kwargs):
        entered.set()
        assert allow_failure.wait(2)
        raise OSError("failed status write")

    monkeypatch.setattr(owner, "WorkflowResourceWindow", RecordedResources)
    monkeypatch.setattr(owner, "publish_workflow_preflight_progress", fail)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="could not be published safely"):
        with owner.preflight_owner(preflight, dict(owner.os.environ)) as (cancelled, _, progress):
            assert entered.wait(1)
            allow_failure.set()
            progress._thread.join(1)
            assert cancelled.is_set()
            progress.check()
    assert instances[0].closed and time.monotonic() - started < 3
    assert not progress._thread.is_alive()
    assert _status()["preflight_release"]["outcome"] == "cancelled"


@pytest.mark.parametrize("phase", ["dependency_plan", "project_setup"])
def test_exact_self_setup_exception_accepts_only_new_preflight_phases(progress, monkeypatch, phase):
    monkeypatch.setattr(project_operations, "_process_token", lambda pid: "owned-progress-token")
    monkeypatch.setattr(project_operations, "_process_is_live", lambda payload: True)
    progress.phase({"phase": phase})
    progress._publish()
    project_operations._check_status(
        progress.status_path, Path(owner.os.environ["LEANFLOW_PROJECT_ROOT"])
    )


def test_discovery_forwards_events_from_the_same_charged_native_invocation(paper, monkeypatch):
    root, record, plan = paper
    observed = []
    budget = CampaignLimits(root / "limits.json", {"agent_calls": 1})

    def invoke(request, on_event):
        request.on_started()
        on_event({"type": "turn.started"})
        assert budget.state["invocations"]
        return HarnessResult(
            status="completed",
            final_response=json.dumps(_response(record, plan)),
            usage={"input_tokens": 5, "output_tokens": 2},
            cleanup_verified=True,
        )

    monkeypatch.setattr(discovery, "run_harness", invoke)
    monkeypatch.setattr(discovery, "build_runtime_isolation", lambda *args, **kwargs: object())
    result = discovery.discover_preparation_plan(
        root,
        record,
        plan,
        backend="codex",
        model="fixture",
        effort="max",
        environment={},
        budget=budget,
        directory=root / ".leanflow/discovery",
        on_event=observed.append,
    )
    assert result["accepted"] is True and result["semantic_approval"] is False
    assert observed == [{"type": "turn.started"}]
    assert len(budget.state["invocations"]) == 1
    assert next(iter(budget.state["invocations"].values())) == {
        "status": "finished",
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
