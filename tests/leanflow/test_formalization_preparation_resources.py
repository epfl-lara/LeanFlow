"""Measure whole-preparation descendants while preserving preexisting controller processes."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import threading
import time

import pytest

from leanflow_cli.formalization.preparation_resources import supervise_preparation
from leanflow_cli.workflows.project_setup.process import SetupCancelled, SetupFailure
from tests.leanflow import test_project_setup_execution as execution_tests

policy = execution_tests.policy


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
