"""Tests for process-isolated dispatch-worker ownership cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.model_tools import get_tool_definitions
from core.process_identity import PROCESS_TOKEN_ENV
from leanflow_cli.native import dispatch_worker, native_runner, runtime_cleanup
from leanflow_cli.runtime import file_locks
from leanflow_cli.workflows import dispatch_incremental_evidence, research_mode
from leanflow_cli.workflows.dispatch_models import JobBudget, JobSpec
from leanflow_cli.workflows.dispatch_service import DispatchService


def _spec() -> JobSpec:
    return JobSpec(
        job_id="run.orchestrator.ds-001",
        archetype="deep_search",
        requester_role="orchestrator",
        objective="find a distinct proof route",
        budget=JobBudget(api_steps=4, wall_clock_s=60),
        deliverable="findings_report",
        inputs={
            "target_symbol": "erdos_242",
            "active_file": "/tmp/FormalConjectures/Erdos242.lean",
        },
        scope={"scratch_only": True},
        parent_job_id="run.orchestrator",
    )


def _patch_worker_ownership(monkeypatch, *, backend):
    calls: list[object] = []
    agent = SimpleNamespace(session_id="worker-session")
    monkeypatch.setattr(native_runner, "_build_agent", lambda: agent)
    monkeypatch.setattr(DispatchService, "_run_backend", backend)
    monkeypatch.setattr(
        runtime_cleanup,
        "shutdown_native_runtime_services",
        lambda value: calls.append(("shutdown", value)),
    )
    monkeypatch.setattr(
        file_locks,
        "release_all_file_locks",
        lambda *, owner_id: calls.append(("locks", owner_id)),
    )
    return agent, calls


def test_worker_reaps_runtime_services_after_success(monkeypatch):
    agent, calls = _patch_worker_ownership(
        monkeypatch,
        backend=lambda self, spec: {"status": "done", "job_id": spec.job_id},
    )

    result = dispatch_worker.run_worker(_spec())

    assert result["status"] == "done"
    assert calls == [("shutdown", agent), ("locks", "worker-session")]


def test_worker_check_journal_survives_backend_interrupt(monkeypatch, tmp_path):
    declaration = "lemma erdos_242_checked_leaf : True := by trivial"
    helper = {
        "anchor_target_symbol": "erdos_242",
        "active_file": "/tmp/FormalConjectures/Erdos242.lean",
        "declaration": declaration,
        "declaration_sha256": hashlib.sha256(declaration.encode("utf-8")).hexdigest(),
        "worker_check": {
            "tool": "lean_incremental_check",
            "action": "check_helper",
            "valid_without_sorry": True,
            "has_errors": False,
            "has_sorry": False,
            "verification_scope": "helper_candidate",
            "replacement_matches_target": False,
            "replacement_declarations": ["erdos_242_checked_leaf"],
        },
        "parent_recheck_required": True,
    }

    def backend(self, _spec):
        assert self._incremental_evidence_sink is not None
        self._incremental_evidence_sink([helper])
        raise KeyboardInterrupt

    _patch_worker_ownership(monkeypatch, backend=backend)
    path = tmp_path / "worker.evidence.json"

    with pytest.raises(KeyboardInterrupt):
        dispatch_worker.run_worker(
            _spec(),
            evidence_file=str(path),
            launch_nonce="launch",
        )

    assert dispatch_incremental_evidence.load_checked_helpers(
        path,
        launch_nonce="launch",
        spec=_spec(),
    ) == [helper]


def test_worker_publishes_identity_and_binds_result_to_launch_nonce(monkeypatch, tmp_path):
    launch_nonce = "launch-nonce-1"
    spec_path = tmp_path / "job.spec.json"
    result_path = tmp_path / "job.result.json"
    identity_path = tmp_path / "job.identity.json"
    spec_path.write_text(
        json.dumps(
            {
                "version": 2,
                "launch_nonce": launch_nonce,
                "spec": _spec().to_mapping(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(PROCESS_TOKEN_ENV, "worker-process-token")
    monkeypatch.setattr(
        dispatch_worker,
        "_parse_args",
        lambda: SimpleNamespace(
            spec_file=str(spec_path),
            result_file=str(result_path),
            identity_file=str(identity_path),
            launch_nonce=launch_nonce,
            parent_pid=0,
        ),
    )
    monkeypatch.setattr(
        dispatch_worker,
        "run_worker",
        lambda spec, *, parent_guard, **_kwargs: {"status": "done", "job_id": spec.job_id},
    )
    monkeypatch.setattr(runtime_cleanup, "install_native_termination_handlers", lambda cb: {})
    monkeypatch.setattr(runtime_cleanup, "restore_native_termination_handlers", lambda value: None)

    assert dispatch_worker.main() == 0

    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert identity["launch_nonce"] == launch_nonce
    assert identity["process_id"] == os.getpid()
    assert identity["process_token_sha256"]
    assert result == {
        "launch_nonce": launch_nonce,
        "ok": True,
        "result": {"status": "done", "job_id": _spec().job_id},
    }


def test_worker_rechecks_launch_nonce_before_backend_and_publishes_no_stale_result(
    monkeypatch, tmp_path
):
    spec_path = tmp_path / "job.spec.json"
    result_path = tmp_path / "job.result.json"
    identity_path = tmp_path / "job.identity.json"
    launch_lock_path = tmp_path / "job.launch.lock"
    reads = 0
    inside_fence = False
    fenced_paths: list[str] = []
    identity_publications: list[tuple[str, str]] = []

    def load_spec(_path, _nonce):
        nonlocal reads
        reads += 1
        if reads == 1:
            return _spec().to_mapping()
        assert inside_fence
        raise dispatch_worker._LaunchNonceMismatch("launch rotated during handshake")

    @contextmanager
    def launch_fence(path):
        nonlocal inside_fence
        fenced_paths.append(path)
        inside_fence = True
        try:
            yield
        finally:
            inside_fence = False

    monkeypatch.setattr(
        dispatch_worker,
        "_parse_args",
        lambda: SimpleNamespace(
            spec_file=str(spec_path),
            result_file=str(result_path),
            identity_file=str(identity_path),
            launch_nonce="stale-launch",
            launch_lock_file=str(launch_lock_path),
            parent_pid=0,
        ),
    )
    monkeypatch.setattr(dispatch_worker, "_load_nonce_bound_spec", load_spec)
    monkeypatch.setattr(dispatch_worker, "_launch_spec_fence", launch_fence)
    monkeypatch.setattr(
        dispatch_worker,
        "_publish_launch_identity",
        lambda path, nonce: identity_publications.append((path, nonce)),
    )
    monkeypatch.setattr(
        dispatch_worker,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("stale launch entered the backend"),
    )
    monkeypatch.setattr(runtime_cleanup, "install_native_termination_handlers", lambda cb: {})
    monkeypatch.setattr(runtime_cleanup, "restore_native_termination_handlers", lambda value: None)

    assert dispatch_worker.main() == 1
    assert reads == 2
    assert fenced_paths == [str(launch_lock_path)]
    assert identity_publications == [(str(identity_path), "stale-launch")]
    assert not result_path.exists()


def test_worker_second_spec_read_rejects_nonce_rotated_by_parent(tmp_path):
    """The job-global spec is the authoritative fence for a suspended worker."""
    spec_path = tmp_path / "job.spec.json"
    old_nonce = "old-launch"
    new_nonce = "new-launch"
    spec_path.write_text(
        json.dumps({"launch_nonce": old_nonce, "spec": _spec().to_mapping()}),
        encoding="utf-8",
    )
    assert dispatch_worker._load_nonce_bound_spec(spec_path, old_nonce)["job_id"] == (
        _spec().job_id
    )

    spec_path.write_text(
        json.dumps({"launch_nonce": new_nonce, "spec": _spec().to_mapping()}),
        encoding="utf-8",
    )

    with pytest.raises(dispatch_worker._LaunchNonceMismatch):
        dispatch_worker._load_nonce_bound_spec(spec_path, old_nonce)
    assert dispatch_worker._load_nonce_bound_spec(spec_path, new_nonce)["job_id"] == (
        _spec().job_id
    )


def test_worker_rejects_dead_parent_inside_final_launch_fence(monkeypatch, tmp_path):
    """A parentless child cannot cross the final fence into provider work."""

    spec_path = tmp_path / "job.spec.json"
    result_path = tmp_path / "job.result.json"
    identity_path = tmp_path / "job.identity.json"
    launch_lock_path = tmp_path / "job.launch.lock"
    reads = 0
    inside_fence = False
    parent_checks: list[int] = []

    class Guard:
        def __init__(self, parent_pid):
            self.parent_pid = parent_pid

        def request_shutdown(self, _reason):
            return None

        def start(self):
            return None

        def set_wall_clock_budget(self, _wall_clock_s):
            return None

        def stop(self):
            return None

    def load_spec(_path, _nonce):
        nonlocal reads
        reads += 1
        return _spec().to_mapping()

    @contextmanager
    def launch_fence(path):
        nonlocal inside_fence
        assert path == str(launch_lock_path)
        inside_fence = True
        try:
            yield
        finally:
            inside_fence = False

    def parent_alive(parent_pid):
        assert reads == 2
        assert inside_fence
        parent_checks.append(parent_pid)
        return False

    monkeypatch.setattr(
        dispatch_worker,
        "_parse_args",
        lambda: SimpleNamespace(
            spec_file=str(spec_path),
            result_file=str(result_path),
            identity_file=str(identity_path),
            launch_nonce="parentless-launch",
            launch_lock_file=str(launch_lock_path),
            parent_pid=4242,
        ),
    )
    monkeypatch.setattr(dispatch_worker, "ParentLivenessGuard", Guard)
    monkeypatch.setattr(dispatch_worker, "_load_nonce_bound_spec", load_spec)
    monkeypatch.setattr(dispatch_worker, "_launch_spec_fence", launch_fence)
    monkeypatch.setattr(dispatch_worker, "_parent_process_alive", parent_alive)
    monkeypatch.setattr(dispatch_worker, "_publish_launch_identity", lambda *_args: None)
    monkeypatch.setattr(
        dispatch_worker,
        "run_worker",
        lambda *_args, **_kwargs: pytest.fail("parentless launch entered backend"),
    )
    monkeypatch.setattr(runtime_cleanup, "install_native_termination_handlers", lambda cb: {})
    monkeypatch.setattr(runtime_cleanup, "restore_native_termination_handlers", lambda value: None)

    assert dispatch_worker.main() == 1
    assert reads == 2
    assert parent_checks == [4242]
    assert not result_path.exists()


def test_worker_acquires_actor_capacity_before_building_agent(monkeypatch):
    entered = False
    agent = SimpleNamespace(session_id="worker-session")

    @contextmanager
    def actor_lease():
        nonlocal entered
        entered = True
        try:
            yield object()
        finally:
            entered = False

    def build_agent():
        assert entered
        return agent

    monkeypatch.setattr(dispatch_worker, "background_actor_lease", actor_lease)
    monkeypatch.setattr(native_runner, "_build_agent", build_agent)
    monkeypatch.setattr(
        DispatchService,
        "_run_backend",
        lambda self, spec: {"status": "done", "job_id": spec.job_id},
    )
    monkeypatch.setattr(runtime_cleanup, "shutdown_native_runtime_services", lambda value: None)
    monkeypatch.setattr(file_locks, "release_all_file_locks", lambda *, owner_id: None)

    assert dispatch_worker.run_worker(_spec())["status"] == "done"
    assert not entered


def test_worker_reaps_runtime_services_after_backend_failure(monkeypatch):
    def fail(self, spec):
        raise RuntimeError(f"failed {spec.job_id}")

    agent, calls = _patch_worker_ownership(monkeypatch, backend=fail)

    with pytest.raises(RuntimeError, match="failed run.orchestrator.ds-001"):
        dispatch_worker.run_worker(_spec())

    assert calls == [("shutdown", agent), ("locks", "worker-session")]


def test_worker_reports_parent_registry_and_effective_delegate_tools_separately(
    monkeypatch, capsys
):
    def backend(self, spec):
        reporter = self._parent_agent._delegated_tool_availability_reporter
        reporter(
            requested_toolsets=["lean-research", "web"],
            effective_tool_names=["lean_incremental_check", "lean_search", "web_search"],
        )
        return {"status": "done", "job_id": spec.job_id}

    agent, _calls = _patch_worker_ownership(monkeypatch, backend=backend)
    agent.valid_tool_names = {
        "apply_verified_patch",
        "lean_incremental_check",
        "lean_search",
        "web_search",
        "write_file",
    }

    dispatch_worker.run_worker(_spec())

    output = capsys.readouterr().out
    registry_line = next(
        line for line in output.splitlines() if "parent configured tool registry" in line
    )
    effective_line = next(
        line for line in output.splitlines() if "effective delegated tool availability" in line
    )
    assert "5 schemas" in registry_line
    assert "apply_verified_patch" in registry_line
    assert "write_file" in registry_line
    assert "3 schemas after runtime filtering" in effective_line
    assert "lean-research, web" in effective_line
    assert "lean_incremental_check" in effective_line
    assert "apply_verified_patch" not in effective_line
    assert "write_file" not in effective_line


def test_worker_sets_assignment_state_and_disables_nested_research(monkeypatch):
    observed: dict[str, object] = {}
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "parent-worker")
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_RESEARCH_WORKERS", "2")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "/tmp/Parent.lean")
    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", "parent-value")
    monkeypatch.setenv("LEANFLOW_DISPATCH_ARCHETYPE", "parent-archetype")

    def backend(self, spec):
        state = dict(self._parent_agent._managed_autonomy_state)
        observed.update(
            {
                "state": state,
                "dispatch_worker": os.environ.get("LEANFLOW_DISPATCH_WORKER"),
                "research_mode": research_mode.research_mode_enabled(),
                "research_workers": os.environ.get("LEANFLOW_RESEARCH_WORKERS"),
                "workflow_kind": os.environ.get("LEANFLOW_NATIVE_WORKFLOW_KIND"),
                "active_file": os.environ.get("LEANFLOW_NATIVE_ACTIVE_FILE"),
                "scratch_only": os.environ.get("LEANFLOW_DISPATCH_SCRATCH_ONLY"),
                "archetype": os.environ.get("LEANFLOW_DISPATCH_ARCHETYPE"),
            }
        )
        return {"status": "done", "job_id": spec.job_id}

    _patch_worker_ownership(monkeypatch, backend=backend)

    dispatch_worker.run_worker(_spec())

    state = dict(observed["state"])
    assert state["current_queue_assignment"] == {
        "target_symbol": "erdos_242",
        "active_file": "/tmp/FormalConjectures/Erdos242.lean",
    }
    assert state["dispatch_worker_job_id"] == "run.orchestrator.ds-001"
    assert observed["research_mode"] is False
    assert observed["dispatch_worker"] == "1"
    assert observed["research_workers"] == "0"
    assert observed["workflow_kind"] == "prove"
    assert observed["active_file"] == "/tmp/FormalConjectures/Erdos242.lean"
    assert observed["scratch_only"] == "1"
    assert observed["archetype"] == "deep_search"
    assert os.environ["LEANFLOW_RESEARCH_MODE"] == "1"
    assert os.environ["LEANFLOW_RESEARCH_WORKERS"] == "2"
    assert os.environ["LEANFLOW_NATIVE_ACTIVE_FILE"] == "/tmp/Parent.lean"
    assert os.environ["LEANFLOW_DISPATCH_SCRATCH_ONLY"] == "parent-value"
    assert os.environ["LEANFLOW_DISPATCH_ARCHETYPE"] == "parent-archetype"
    assert os.environ["LEANFLOW_DISPATCH_WORKER"] == "parent-worker"


def test_worker_archetype_exposes_compute_only_during_empirical_assignment(monkeypatch):
    observed: dict[str, set[str]] = {}

    def backend(self, spec):
        definitions = get_tool_definitions(["empirical-compute"], quiet_mode=True)
        observed[spec.archetype] = {str(item["function"]["name"]) for item in definitions}
        return {"status": "done", "job_id": spec.job_id}

    _patch_worker_ownership(monkeypatch, backend=backend)
    dispatch_worker.run_worker(_spec())
    empirical = replace(
        _spec(),
        job_id="run.orchestrator.em-001",
        archetype="empirical",
        deliverable="experiment_result",
    )
    dispatch_worker.run_worker(empirical)

    assert observed == {
        "deep_search": set(),
        "empirical": {"empirical_compute"},
    }


def test_worker_delegate_search_callback_reaches_assignment_boundary(monkeypatch):
    class Agent:
        session_id = "worker-session"
        quiet_mode = True

        def __init__(self):
            self.interrupt_messages: list[str | None] = []
            self.appendices: list[str] = []
            self._interrupted = False
            self._managed_delegated_post_tool_result_callback = lambda executing_agent, function_name, args, result: native_runner._handle_delegated_managed_search_result(
                self, executing_agent, function_name, args, result
            )

        def is_interrupted(self):
            return self._interrupted

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)
            self._interrupted = True

        def stage_tool_result_appendix(self, message):
            self.appendices.append(message)

    class Child:
        session_id = "worker-deep-search-lane"
        _parent_session_id = "worker-session"
        _delegate_depth = 1
        quiet_mode = True

        def __init__(self):
            self.interrupt_messages: list[str | None] = []
            self.appendices: list[str] = []
            self._interrupted = False

        def is_interrupted(self):
            return self._interrupted

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)
            self._interrupted = True

        def stage_tool_result_appendix(self, message):
            self.appendices.append(message)

    agent = Agent()
    child = Child()
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv("LEANFLOW_SEARCH_PROGRESS_HARD_LIMIT", "3")
    monkeypatch.setattr(native_runner, "_build_agent", lambda: agent)
    monkeypatch.setattr(
        native_runner,
        "_record_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        native_runner.research_portfolio,
        "maintain_portfolio",
        lambda **kwargs: pytest.fail("dispatch worker spawned a nested research portfolio"),
    )
    monkeypatch.setattr(runtime_cleanup, "shutdown_native_runtime_services", lambda value: None)
    monkeypatch.setattr(file_locks, "release_all_file_locks", lambda *, owner_id: None)

    def backend(self, spec):
        callback = self._parent_agent._managed_delegated_post_tool_result_callback
        for index in range(3):
            callback(
                child,
                "web_search",
                {"query": f"route {index}"},
                '{"success": true, "results": []}',
            )
        return {"status": "done", "job_id": spec.job_id}

    monkeypatch.setattr(DispatchService, "_run_backend", backend)

    dispatch_worker.run_worker(_spec())

    state = child._managed_autonomy_state
    assert state["search_progress"]["search_count"] == 3
    assert state["prover_requested_route"] == {
        "route": "plan",
        "target_symbol": "erdos_242",
        "active_file": "/tmp/FormalConjectures/Erdos242.lean",
    }
    assert agent.interrupt_messages == []
    assert "search_progress" not in agent._managed_autonomy_state
    assert child.interrupt_messages == []
    assert state["search_progress"]["synthesis_grace_pending"] is True
    assert any("SEARCH ROUTE BOUNDARY" in appendix for appendix in child.appendices)
    assert any("do not call another tool" in appendix for appendix in child.appendices)
    route_events = [details for args, details in events if args[0] == "search-route-change"]
    assert len(route_events) == 1
    assert route_events[0]["agent_session_id"] == "worker-deep-search-lane"
    assert route_events[0]["parent_agent_session_id"] == "worker-session"
    assert research_mode.research_mode_enabled() is True


def test_parent_guard_interrupts_then_forces_exit_when_parent_disappears(monkeypatch):
    interrupted = threading.Event()
    interrupt_reasons: list[str] = []
    forced = threading.Event()
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(dispatch_worker, "_parent_process_alive", lambda _pid: False)
    monkeypatch.setattr(dispatch_worker, "PARENT_CLEANUP_GRACE_S", 0.0)
    monkeypatch.setattr(
        dispatch_worker.os,
        "kill",
        lambda pid, signum: signals.append((pid, signum)),
    )
    monkeypatch.setattr(
        dispatch_worker,
        "_force_exit_orphaned_worker",
        forced.set,
    )
    guard = dispatch_worker.ParentLivenessGuard(4321)
    guard.set_interrupt_callback(
        lambda reason: (interrupt_reasons.append(reason), interrupted.set())
    )

    guard.start()

    assert forced.wait(timeout=2)
    guard.stop()
    assert interrupted.is_set()
    assert interrupt_reasons == ["dispatch worker parent exited"]
    assert signals == [(os.getpid(), signal.SIGTERM)]


def test_parent_guard_allows_graceful_signal_cleanup_to_cancel_force_exit(monkeypatch):
    interrupted = threading.Event()
    interrupt_reasons: list[str] = []
    forced = threading.Event()
    monkeypatch.setattr(dispatch_worker, "PARENT_CLEANUP_GRACE_S", 1.0)
    monkeypatch.setattr(
        dispatch_worker,
        "_force_exit_orphaned_worker",
        forced.set,
    )
    guard = dispatch_worker.ParentLivenessGuard(0)
    guard.set_interrupt_callback(
        lambda reason: (interrupt_reasons.append(reason), interrupted.set())
    )
    guard.start()

    guard.request_shutdown(signal.SIGTERM)

    assert interrupted.wait(timeout=2)
    guard.stop()
    assert interrupt_reasons == ["dispatch worker received SIGTERM"]
    assert not forced.is_set()


def test_parent_guard_enforces_wall_clock_without_parent_polling(monkeypatch):
    """A blocked parent cannot let an isolated worker outlive its hard budget."""
    interrupted = threading.Event()
    interrupt_reasons: list[str] = []
    forced = threading.Event()
    monkeypatch.setattr(dispatch_worker, "PARENT_CLEANUP_GRACE_S", 0.0)
    monkeypatch.setattr(dispatch_worker, "_force_exit_orphaned_worker", forced.set)
    guard = dispatch_worker.ParentLivenessGuard(0)
    guard.set_interrupt_callback(
        lambda reason: (interrupt_reasons.append(reason), interrupted.set())
    )
    guard.start()

    guard.set_wall_clock_budget(0)

    assert forced.wait(timeout=2)
    guard.stop()
    assert interrupted.is_set()
    assert interrupt_reasons == ["dispatch worker wall-clock budget exhausted"]


def test_parent_liveness_requires_the_original_direct_parent(monkeypatch):
    probes: list[tuple[int, int]] = []
    monkeypatch.setattr(dispatch_worker.os, "getppid", lambda: 1234)
    monkeypatch.setattr(
        dispatch_worker.os,
        "kill",
        lambda pid, signum: probes.append((pid, signum)),
    )

    assert dispatch_worker._parent_process_alive(1234)
    assert probes == [(1234, 0)]
    assert not dispatch_worker._parent_process_alive(9999)
    assert probes == [(1234, 0)]
