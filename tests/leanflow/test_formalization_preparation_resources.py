"""Measure whole-preparation descendants while preserving preexisting controller processes."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from leanflow_cli.formalization.preparation_resources import supervise_preparation
from leanflow_cli.workflows.project_setup.process import SetupCancelled, SetupFailure
from tests.leanflow import test_project_setup_execution as execution_tests

policy = execution_tests.policy


@pytest.fixture
def inert_supervision(policy, monkeypatch):
    """Supply stable owned identities without launching or signaling real children."""
    from agent.runtime.harness import processes
    from leanflow_cli.formalization import preparation_resources
    from leanflow_cli.workflows.project_setup.execution import SetupExecutionPolicy

    root = os.getpid()
    root_start = "Fri Oct 16 12:00:00 2026"
    child = root + 1000000
    state = SimpleNamespace(
        root_start=root_start,
        tables=0,
        signals=[],
        closure_calls=[],
        observed_rss=[],
        additional={},
    )

    def table():
        state.tables += 1
        result = {root: (1, state.root_start)}
        if state.tables > 1:
            result[child] = (root, "Fri Oct 16 12:00:01 2026")
        result.update(state.additional)
        return result

    def closed(tree):
        state.closure_calls.append(dict(tree.descendants))
        return True

    original_observe_rss = SetupExecutionPolicy.observe_rss

    def observe_rss(selected, value):
        state.observed_rss.append(value)
        original_observe_rss(selected, value)

    monkeypatch.setattr(processes, "_processes", table)
    monkeypatch.setattr(preparation_resources, "_processes", table)
    monkeypatch.setattr(
        processes.ProcessTree,
        "signal",
        lambda tree, sig: state.signals.append((sig, dict(tree.descendants))),
    )
    monkeypatch.setattr(preparation_resources, "verify_owned_stopped", closed)
    monkeypatch.setattr(SetupExecutionPolicy, "observe_rss", observe_rss)
    monkeypatch.setattr(SetupExecutionPolicy, "observe_disk", lambda selected: None)
    state.module = preparation_resources
    state.policy = policy
    return state


def test_monitor_measures_independent_checker_child_and_cleans_new_background_process(policy):
    child = None
    with supervise_preparation(policy, interval_s=0.05) as cancel:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; memory=bytearray(4*1024*1024); time.sleep(30)"]
        )
        deadline = time.monotonic() + 3
        while policy.usage.peak_owned_rss_bytes < 4 * 1024**2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not cancel.is_set()
    assert child is not None
    child.wait(timeout=3)
    assert policy.usage.peak_owned_rss_bytes >= 4 * 1024**2
    assert policy.usage.cleanup_verified


def test_monitor_stops_only_new_children_when_aggregate_memory_limit_hits(policy):
    previous = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child = None
    event = threading.Event()
    selected = dataclasses.replace(policy, max_owned_rss_bytes=1024)
    try:
        with pytest.raises(SetupFailure) as error:
            with supervise_preparation(selected, cancel=event, interval_s=0.05):
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import time; memory=bytearray(4*1024*1024); time.sleep(30)",
                    ]
                )
                deadline = time.monotonic() + 3
                while not event.is_set() and time.monotonic() < deadline:
                    time.sleep(0.05)
        assert error.value.code == "setup_memory_limit"
        assert previous.poll() is None
        assert event.is_set()
        child.wait(timeout=3)
    finally:
        previous.kill()
        previous.wait()
        if child is not None and child.poll() is None:
            child.kill()
            child.wait()


def test_preexisting_processes_remain_alive_after_successful_preparation_context(policy):
    previous = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with supervise_preparation(policy, interval_s=0.05):
            time.sleep(0.3)
        assert previous.poll() is None
    finally:
        previous.kill()
        previous.wait()


@pytest.mark.parametrize("deadline", [False, True])
def test_cancellation_and_deadline_cover_work_without_command_wrapper(policy, deadline):
    selected = dataclasses.replace(policy, deadline=time.monotonic() + (0.2 if deadline else 30))
    event = threading.Event()
    with pytest.raises(SetupFailure if deadline else SetupCancelled) as error:
        with supervise_preparation(selected, cancel=event, interval_s=0.05):
            if not deadline:
                event.set()
            time.sleep(0.5)
    assert event.is_set()
    if deadline:
        assert error.value.code == "setup_deadline"


def test_context_rejects_parallel_preparation_in_same_controller(policy):
    with supervise_preparation(policy):
        with pytest.raises(SetupFailure) as error:
            with supervise_preparation(policy):
                raise AssertionError("parallel preparation started")
        assert error.value.code == "setup_resource_concurrency"


def test_unverified_postkill_identity_keeps_cleanup_false_and_blocks_disposal(policy, monkeypatch):
    from leanflow_cli.formalization import preparation_resources

    monkeypatch.setattr(preparation_resources, "verify_owned_stopped", lambda tree: False)
    with pytest.raises(SetupFailure) as error:
        with supervise_preparation(policy):
            pass
    assert error.value.code == "setup_cleanup_unverified"
    assert policy.usage.cleanup_verified is False


def test_monitor_rejects_malformed_rss_metadata_without_accepting_zero(policy, monkeypatch):
    from agent.runtime.harness import resources

    original = resources.subprocess.run

    def malformed_rss(command, *args, **kwargs):
        if command == ["/bin/ps", "-axo", "pid=,rss=,lstart="]:
            return SimpleNamespace(stdout="not a process memory table\n")
        return original(command, *args, **kwargs)

    monkeypatch.setattr(resources.subprocess, "run", malformed_rss)
    cancel = threading.Event()
    with pytest.raises(ValueError, match="Native memory measurement"):
        with supervise_preparation(policy, cancel=cancel, interval_s=0.05):
            assert cancel.wait(timeout=3)
    assert cancel.is_set()
    assert policy.usage.peak_owned_rss_bytes == 0
    assert policy.usage.cleanup_verified


def test_one_transient_metadata_timeout_recovers_to_positive_rss(inert_supervision, monkeypatch):
    state = inert_supervision
    calls = []
    healthy = threading.Event()

    def rss(identities):
        calls.append(dict(identities))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("ps", 2)
        healthy.set()
        return 4096

    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", rss)
    with state.module.supervise_preparation(state.policy, interval_s=0.05) as cancel:
        assert healthy.wait(2)
        assert not cancel.is_set()
    assert len(calls) >= 2 and state.policy.usage.peak_owned_rss_bytes == 4096
    assert state.observed_rss and set(state.observed_rss) == {4096}
    assert state.closure_calls and state.policy.usage.cleanup_verified


def test_fifth_initial_metadata_gap_blocks_body_and_preserves_prior_peak(
    inert_supervision, monkeypatch
):
    state = inert_supervision
    state.policy.usage.peak_owned_rss_bytes = 8192
    attempts = []
    entered = []
    cancel = threading.Event()

    def rss(identities):
        attempts.append(dict(identities))
        raise subprocess.TimeoutExpired("ps", 2)

    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", rss)
    with pytest.raises(SetupFailure) as error:
        with state.module.supervise_preparation(state.policy, cancel=cancel, interval_s=0.05):
            entered.append(True)
    assert error.value.code == "setup_resource_inspection_failed"
    assert len(attempts) == 5 and entered == [] and cancel.is_set()
    assert state.policy.usage.peak_owned_rss_bytes == 8192
    assert state.observed_rss == []


def test_body_exit_during_gap_requires_fresh_healthy_sample_before_commit(
    inert_supervision, monkeypatch
):
    state = inert_supervision
    armed = threading.Event()
    gap_seen = threading.Event()
    fresh_healthy = threading.Event()
    gap_count = []
    committed = []

    def rss(identities):
        if armed.is_set() and not gap_count:
            gap_count.append(True)
            gap_seen.set()
            raise subprocess.TimeoutExpired("ps", 2)
        if gap_seen.is_set():
            fresh_healthy.set()
        return 4096

    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", rss)
    with state.module.supervise_preparation(state.policy, interval_s=0.05) as cancel:
        armed.set()
        assert gap_seen.wait(2)
    assert fresh_healthy.is_set() and not cancel.is_set()
    committed.append(True)
    assert committed == [True] and state.policy.usage.cleanup_verified


def test_body_exit_during_two_gaps_uses_only_remaining_retry_budget(inert_supervision, monkeypatch):
    state = inert_supervision
    armed = threading.Event()
    two_gaps = threading.Event()
    failures = []
    committed = []

    def rss(identities):
        if armed.is_set():
            failures.append(True)
            if len(failures) == 2:
                two_gaps.set()
            raise subprocess.TimeoutExpired("ps", 2)
        return 4096

    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", rss)
    with pytest.raises(SetupFailure) as error:
        with state.module.supervise_preparation(state.policy, interval_s=0.05):
            armed.set()
            assert two_gaps.wait(2)
        committed.append(True)
    assert error.value.code == "setup_resource_inspection_failed"
    assert len(failures) == 5 and committed == []
    assert state.policy.usage.peak_owned_rss_bytes == 4096
    assert set(state.observed_rss) == {4096}


def test_last_instant_memory_breach_blocks_commit(inert_supervision, monkeypatch):
    state = inert_supervision
    leaving = threading.Event()
    committed = []
    limit = state.policy.max_owned_rss_bytes

    def rss(identities):
        return limit + 1 if leaving.is_set() else 4096

    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", rss)
    with pytest.raises(SetupFailure) as error:
        with state.module.supervise_preparation(state.policy, interval_s=1):
            leaving.set()
        committed.append(True)
    assert error.value.code == "setup_memory_limit" and committed == []
    assert state.policy.usage.peak_owned_rss_bytes == limit + 1
    assert state.closure_calls and state.policy.usage.cleanup_verified


def test_last_instant_controller_identity_change_blocks_commit(inert_supervision, monkeypatch):
    state = inert_supervision
    committed = []
    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", lambda identities: 4096)
    with pytest.raises((ValueError, SetupFailure)) as error:
        with state.module.supervise_preparation(state.policy, interval_s=1):
            state.root_start = "Fri Oct 16 12:00:02 2026"
        committed.append(True)
    assert committed == []
    if isinstance(error.value, SetupFailure):
        assert error.value.code == "setup_cleanup_unverified"


def test_initial_diagnostic_io_failure_blocks_body(inert_supervision, tmp_path):
    state = inert_supervision
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("preserve")
    entered = []
    with pytest.raises((OSError, ValueError)):
        with state.module.supervise_preparation(
            state.policy, diagnostic_path=blocked / "inspection.json"
        ):
            entered.append(True)
    assert entered == [] and blocked.read_text() == "preserve"


def test_unjoined_monitor_keeps_campaign_ownership_until_delayed_cleanup_finishes(
    inert_supervision, monkeypatch
):
    """A timed-out old sampler cannot acquire the next campaign's children during cleanup."""
    state = inert_supervision
    observing = threading.Event()
    release_observation = threading.Event()
    cleaning = threading.Event()
    release_cleanup = threading.Event()
    old_worker = []
    forced_join = []
    admitted = []
    signaled = []
    new_child = os.getpid() + 2000000
    original_join = threading.Thread.join

    def rss(identities):
        current = threading.current_thread()
        if current.name == "leanflow-preparation-resources" and not old_worker:
            old_worker.append(current)
            observing.set()
            assert release_observation.wait(3), "Test did not release the delayed observation"
            raise ValueError("delayed strict metadata failure")
        return 4096

    def join(thread, timeout=None):
        if old_worker and thread is old_worker[0] and timeout == 5 and not forced_join:
            forced_join.append(True)
            return
        original_join(thread, timeout)

    def closed(tree):
        if old_worker and threading.current_thread() is old_worker[0]:
            cleaning.set()
            assert release_cleanup.wait(3), "Test did not release the delayed cleanup"
        return True

    def signal_owned(tree, sig):
        signaled.append((threading.current_thread(), set(tree.descendants)))

    def require_exclusive_owner():
        candidate = dataclasses.replace(state.policy, usage=type(state.policy.usage)())
        with pytest.raises(SetupFailure) as error:
            with state.module.supervise_preparation(candidate, interval_s=0.05):
                admitted.append("overlapping")
                state.additional[new_child] = (os.getpid(), "Fri Oct 16 12:00:03 2026")
                release_observation.set()
                assert cleaning.wait(2)
        assert error.value.code == "setup_resource_concurrency"

    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", rss)
    monkeypatch.setattr(state.module, "verify_owned_stopped", closed)
    monkeypatch.setattr(state.module.ProcessTree, "signal", signal_owned)
    monkeypatch.setattr(threading.Thread, "join", join)
    try:
        with pytest.raises(SetupFailure) as error:
            with state.module.supervise_preparation(state.policy, interval_s=0.05):
                assert observing.wait(2)
        assert error.value.code == "setup_cleanup_unverified"
        assert forced_join == [True] and old_worker[0].is_alive()
        assert state.policy.usage.cleanup_verified is False
        require_exclusive_owner()
        release_observation.set()
        assert cleaning.wait(2)
        assert old_worker[0].is_alive()
        require_exclusive_owner()
        assert admitted == []
        assert all(new_child not in identities for _, identities in signaled)
        release_cleanup.set()
        original_join(old_worker[0], timeout=3)
        assert not old_worker[0].is_alive()
        deadline = time.monotonic() + 2
        while state.module._campaign_owner.locked() and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert not state.module._campaign_owner.locked()
        # A new campaign receives fresh usage; the old uncertain lease cannot be reused.
        fresh = dataclasses.replace(state.policy, usage=type(state.policy.usage)())
        with state.module.supervise_preparation(fresh, interval_s=0.05):
            admitted.append("fresh")
            state.additional[new_child] = (os.getpid(), "Fri Oct 16 12:00:03 2026")
        assert admitted == ["fresh"] and fresh.usage.cleanup_verified
        assert any(new_child in identities for _, identities in signaled)
        assert all(
            new_child not in identities
            for thread, identities in signaled
            if thread is old_worker[0]
        )
    finally:
        release_observation.set()
        release_cleanup.set()
        if old_worker:
            original_join(old_worker[0], timeout=3)


def test_background_disk_oserror_is_not_reclassified_or_retried_as_metadata(
    inert_supervision, monkeypatch, tmp_path
):
    state = inert_supervision
    armed = threading.Event()
    failed = OSError("storage observation unavailable")
    attempts = []
    diagnostic = tmp_path / "inspection.json"
    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", lambda identities: 4096)

    def disk(selected):
        if armed.is_set():
            attempts.append(True)
            raise failed

    monkeypatch.setattr(type(state.policy), "observe_disk", disk)
    with pytest.raises(OSError) as error:
        with state.module.supervise_preparation(
            state.policy, interval_s=0.05, diagnostic_path=diagnostic
        ) as cancel:
            armed.set()
            assert cancel.wait(2)
    assert error.value is failed and attempts == [True]
    assert state.closure_calls and state.policy.usage.cleanup_verified
    snapshot = json.loads(diagnostic.read_text())
    assert snapshot["state"] == "failed" and snapshot["total_gaps"] == 0
    assert snapshot["first_failure"] is snapshot["last_failure"] is None


def test_unjoined_monitor_releases_without_starting_a_fallible_owner_helper(
    inert_supervision, monkeypatch
):
    """Finishing the old monitor must release ownership even if auxiliary threads cannot start."""
    state = inert_supervision
    observing = threading.Event()
    release_observation = threading.Event()
    old_worker = []
    forced_join = []
    starts = []
    asynchronous_errors = []
    original_start = threading.Thread.start
    original_join = threading.Thread.join

    def start(thread):
        starts.append(thread.name)
        if thread.name != "leanflow-preparation-resources":
            raise RuntimeError("Auxiliary owner-release thread cannot start")
        original_start(thread)

    def join(thread, timeout=None):
        if old_worker and thread is old_worker[0] and timeout == 5 and not forced_join:
            forced_join.append(True)
            return
        original_join(thread, timeout)

    def rss(identities):
        current = threading.current_thread()
        if current.name == "leanflow-preparation-resources" and not old_worker:
            old_worker.append(current)
            observing.set()
            assert release_observation.wait(3)
            raise ValueError("Delayed metadata failure")
        return 4096

    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(threading.Thread, "join", join)
    monkeypatch.setattr(threading, "excepthook", asynchronous_errors.append)
    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", rss)
    try:
        with pytest.raises(SetupFailure) as error:
            with state.module.supervise_preparation(state.policy, interval_s=0.05):
                assert observing.wait(2)
        assert error.value.code == "setup_cleanup_unverified"
        assert state.module._campaign_owner.locked() and old_worker[0].is_alive()
        assert starts == ["leanflow-preparation-resources"]
        release_observation.set()
        original_join(old_worker[0], timeout=3)
        assert not old_worker[0].is_alive()
        assert not state.module._campaign_owner.locked()
        fresh = dataclasses.replace(state.policy, usage=type(state.policy.usage)())
        with state.module.supervise_preparation(fresh, interval_s=0.05):
            pass
        assert fresh.usage.cleanup_verified
        assert starts == ["leanflow-preparation-resources"] * 2
        assert asynchronous_errors == []
    finally:
        release_observation.set()
        if old_worker:
            original_join(old_worker[0], timeout=3)


def test_deferred_diagnostic_has_one_writer_after_all_effects_and_does_not_hold_new_setup(
    inert_supervision, monkeypatch, tmp_path
):
    """Slow or failed terminal diagnostic IO must follow process handoff without racing sampling."""
    state = inert_supervision
    observing = threading.Event()
    release_observation = threading.Event()
    terminal_started = threading.Event()
    release_terminal = threading.Event()
    fresh_admitted = threading.Event()
    old_active = threading.Event()
    old_worker = []
    instances = []
    terminal_calls = []
    overlapping_writes = []
    old_effects_after_new_setup = []
    asynchronous_errors = []
    forced_join = []
    diagnostic = tmp_path / "old-inspection.json"
    secret = "DO_NOT_EMIT_ASYNC_TERMINAL_DIAGNOSTIC_SECRET"
    original_join = threading.Thread.join
    original_inspection = state.module.PreparationInspection

    class ControlledInspection(original_inspection):
        def __init__(self, path):
            super().__init__(path)
            instances.append(self)

        def sample(self, observe):
            if self is not instances[0]:
                return super().sample(observe)
            old_active.set()
            try:
                return super().sample(observe)
            finally:
                if fresh_admitted.is_set():
                    old_effects_after_new_setup.append("sample")
                old_active.clear()

        def abort(self, *, cleanup_verified):
            if self is not instances[0]:
                return super().abort(cleanup_verified=cleanup_verified)
            terminal_calls.append(threading.current_thread())
            if old_active.is_set():
                overlapping_writes.append(True)
            if old_worker and threading.current_thread() is old_worker[0]:
                terminal_started.set()
                assert release_terminal.wait(3)
                raise OSError(secret)
            return super().abort(cleanup_verified=cleanup_verified)

    def join(thread, timeout=None):
        if old_worker and thread is old_worker[0] and timeout == 5 and not forced_join:
            forced_join.append(True)
            return
        original_join(thread, timeout)

    def rss(identities):
        current = threading.current_thread()
        if current.name == "leanflow-preparation-resources" and not old_worker:
            old_worker.append(current)
            observing.set()
            assert release_observation.wait(3)
            raise ValueError("Delayed metadata failure")
        return 4096

    def signal_owned(tree, sig):
        if old_worker and threading.current_thread() is old_worker[0] and fresh_admitted.is_set():
            old_effects_after_new_setup.append("signal")

    def closed(tree):
        if old_worker and threading.current_thread() is old_worker[0] and fresh_admitted.is_set():
            old_effects_after_new_setup.append("cleanup")
        return True

    monkeypatch.setattr(state.module, "PreparationInspection", ControlledInspection)
    monkeypatch.setattr(state.module, "observed_descendant_rss_bytes", rss)
    monkeypatch.setattr(state.module.ProcessTree, "signal", signal_owned)
    monkeypatch.setattr(state.module, "verify_owned_stopped", closed)
    monkeypatch.setattr(threading.Thread, "join", join)
    monkeypatch.setattr(threading, "excepthook", asynchronous_errors.append)
    try:
        with pytest.raises(SetupFailure) as error:
            with state.module.supervise_preparation(
                state.policy, interval_s=0.05, diagnostic_path=diagnostic
            ):
                assert observing.wait(2)
        assert error.value.code == "setup_cleanup_unverified"
        assert overlapping_writes == [] and terminal_calls == []
        release_observation.set()
        assert terminal_started.wait(2)
        assert terminal_calls == [old_worker[0]]
        assert old_worker[0].is_alive(), "The terminal diagnostic IO is deliberately blocked"
        assert not state.module._campaign_owner.locked()
        fresh = dataclasses.replace(state.policy, usage=type(state.policy.usage)())
        with state.module.supervise_preparation(fresh, interval_s=0.05):
            fresh_admitted.set()
        assert fresh.usage.cleanup_verified
        assert old_effects_after_new_setup == []
        release_terminal.set()
        original_join(old_worker[0], timeout=3)
        assert not old_worker[0].is_alive()
        assert terminal_calls == [old_worker[0]] and overlapping_writes == []
        assert asynchronous_errors == []
        assert secret not in diagnostic.read_text()
    finally:
        release_observation.set()
        release_terminal.set()
        if old_worker:
            original_join(old_worker[0], timeout=3)
