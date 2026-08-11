"""Tests for the cross-process research-provider request gate."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from pathlib import Path

import pytest

from core.provider_capacity import (
    BACKGROUND_PROVIDER_CAPACITY_ENV,
    BACKGROUND_PROVIDER_NAMESPACE_ENV,
    acquire_background_provider_lease,
    background_actor_lease,
    background_provider_capacity,
    background_provider_lease,
)


@pytest.fixture()
def capacity_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv(BACKGROUND_PROVIDER_NAMESPACE_ENV, f"test-{tmp_path.name}")


def _observed_parallelism(workers: int = 6) -> int:
    active = 0
    peak = 0
    state_lock = threading.Lock()

    def request() -> None:
        nonlocal active, peak
        with background_provider_lease():
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04)
            with state_lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(lambda _index: request(), range(workers)))
    return peak


def test_capacity_limits_simultaneous_background_threads(capacity_env, monkeypatch):
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "2")
    assert _observed_parallelism() == 2


def test_zero_workers_retains_one_sequential_request_slot(capacity_env, monkeypatch):
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "0")
    assert background_provider_capacity() == 1
    assert _observed_parallelism(workers=4) == 1


def test_nested_request_reuses_the_same_slot_without_deadlock(capacity_env, monkeypatch):
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "1")
    acquired_after_outer = threading.Event()

    with background_provider_lease() as outer:
        with background_provider_lease() as inner:
            assert inner is outer

        def contender() -> None:
            with background_provider_lease():
                acquired_after_outer.set()

        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        assert not acquired_after_outer.wait(0.15)

    assert acquired_after_outer.wait(2.0)
    thread.join(timeout=2.0)


def test_cancelled_nested_request_does_not_retain_actor(capacity_env, monkeypatch):
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "1")
    with background_actor_lease() as actor:
        with pytest.raises(InterruptedError):
            acquire_background_provider_lease(cancelled=lambda: True)
        with background_provider_lease() as nested:
            assert nested is actor


def test_copied_tool_context_reuses_actor_lease(capacity_env, monkeypatch):
    """Concurrent tool threads inherit an actor instead of self-deadlocking."""
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "1")
    observed: list[object] = []
    with background_actor_lease() as actor:
        context = copy_context()

        def nested_request() -> None:
            with background_provider_lease() as request_lease:
                observed.append(request_lease)

        thread = threading.Thread(target=context.run, args=(nested_request,), daemon=True)
        thread.start()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
    assert observed == [actor]


def test_delegate_conversations_are_actor_capped_before_construction(capacity_env, monkeypatch):
    """Planner-style delegates never enter AIAgent construction above N."""
    from tools.implementations import delegate_tool

    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "2")
    active = 0
    peak = 0
    state_lock = threading.Lock()

    def fake_unleased(**kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.04)
            return {"task_index": kwargs["task_index"], "status": "completed"}
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(delegate_tool, "_run_single_child_unleased", fake_unleased)
    arguments = {
        "goal": "g",
        "context": None,
        "toolsets": None,
        "model": None,
        "max_iterations": 1,
        "parent_agent": object(),
    }
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(delegate_tool._run_single_child, task_index=index, **arguments)
            for index in range(5)
        ]
        [future.result() for future in futures]
    assert peak == 2


def test_busy_planner_delegate_returns_capacity_deferred(capacity_env, monkeypatch):
    """A foreground planner tick never waits for a whole background job."""
    from tools.implementations import delegate_tool

    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "1")
    constructed = False

    def should_not_construct(**_kwargs):
        nonlocal constructed
        constructed = True
        return {}

    monkeypatch.setattr(delegate_tool, "_run_single_child_unleased", should_not_construct)
    with background_actor_lease():
        context = copy_context()
        result_holder: list[dict] = []

        # A fresh context represents the foreground planner, not a nested tool
        # inside the actor held above.
        empty_context = Context()

        def attempt() -> None:
            result_holder.append(
                delegate_tool._run_single_child(
                    task_index=0,
                    goal="plan",
                    context=None,
                    toolsets=None,
                    model=None,
                    max_iterations=1,
                    parent_agent=object(),
                    background_capacity_timeout_s=0.05,
                )
            )

        thread = threading.Thread(target=empty_context.run, args=(attempt,), daemon=True)
        thread.start()
        thread.join(timeout=1.0)
        assert not thread.is_alive()

    assert not constructed
    assert result_holder[0]["status"] == "capacity-deferred"


@pytest.mark.parametrize("error_type", [TimeoutError, KeyboardInterrupt])
def test_exception_paths_release_capacity(capacity_env, monkeypatch, error_type):
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "1")
    with pytest.raises(error_type):
        with background_provider_lease():
            raise error_type("request stopped")
    with background_provider_lease() as lease:
        assert lease is not None


def test_cancelled_wait_does_not_leak_local_permit(capacity_env, monkeypatch):
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "1")
    cancel = threading.Event()
    errors: list[BaseException] = []

    with background_provider_lease():

        def waiter() -> None:
            try:
                acquire_background_provider_lease(cancelled=cancel.is_set)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(0.05)
        cancel.set()
        thread.join(timeout=2.0)

    assert len(errors) == 1
    assert isinstance(errors[0], InterruptedError)
    with background_provider_lease() as lease:
        assert lease is not None


def test_slot_directory_failure_releases_local_permit(capacity_env, monkeypatch):
    from core import provider_capacity

    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "1")
    real_slot_root = provider_capacity._slot_root
    monkeypatch.setattr(
        provider_capacity,
        "_slot_root",
        lambda _namespace: (_ for _ in ()).throw(OSError("read-only runtime")),
    )
    with pytest.raises(OSError, match="read-only runtime"):
        acquire_background_provider_lease()

    monkeypatch.setattr(provider_capacity, "_slot_root", real_slot_root)
    with background_provider_lease() as lease:
        assert lease is not None


def test_crashed_process_releases_its_file_slot(capacity_env, monkeypatch, tmp_path):
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "1")
    ready = tmp_path / "child-ready"
    script = "\n".join(
        (
            "import time",
            "from pathlib import Path",
            "from core.provider_capacity import acquire_background_provider_lease",
            "lease = acquire_background_provider_lease()",
            f"Path({str(ready)!r}).write_text('ready', encoding='utf-8')",
            "time.sleep(30)",
        )
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ),
    )
    contender_acquired = threading.Event()
    try:
        deadline = time.monotonic() + 5.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()

        def contender() -> None:
            with background_provider_lease():
                contender_acquired.set()

        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        assert not contender_acquired.wait(0.15)
        child.kill()  # OS-level lock cleanup is the stale-owner recovery path.
        child.wait(timeout=5.0)
        assert contender_acquired.wait(3.0)
        thread.join(timeout=2.0)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5.0)
