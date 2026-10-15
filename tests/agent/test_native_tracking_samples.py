"""Bound process-sampling failures without relaxing strict metadata or cleanup checks."""

from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from agent.runtime.harness import processes
from agent.runtime.harness.models import BackendError
from agent.runtime.harness.processes import (
    ProcessSampler,
    ProcessTrackingUnavailable,
    ProcessTree,
)


@pytest.mark.parametrize("bound", [True, False, 0, -1, 1.0, float("inf"), "5", None])
def test_sampler_rejects_nonpositive_or_noninteger_bounds(bound):
    with pytest.raises(ValueError, match="positive integer"):
        ProcessSampler(ProcessTree(101), max_failures=bound)


@pytest.mark.parametrize(
    "error",
    [
        OSError("unavailable"),
        subprocess.SubprocessError("unavailable"),
        subprocess.TimeoutExpired("ps", 2),
        ValueError("unavailable"),
    ],
)
def test_default_sampler_stops_on_fifth_consecutive_metadata_failure(monkeypatch, error):
    tree = ProcessTree(101, {202: "existing-child"})
    refresh = Mock(side_effect=error)
    monkeypatch.setattr(tree, "refresh", refresh)
    sampler = ProcessSampler(tree)
    assert [sampler.sample() for _ in range(4)] == [False] * 4
    with pytest.raises(ProcessTrackingUnavailable, match="metadata is unavailable") as caught:
        sampler.sample()
    assert isinstance(caught.value, BackendError)
    assert caught.value.__cause__ is error
    assert refresh.call_count == 5
    assert tree.descendants == {202: "existing-child"}


def test_success_resets_consecutive_failures_and_preserves_the_same_tracker(monkeypatch):
    tree = ProcessTree(101)
    tables = Mock(
        side_effect=[
            OSError("first gap"),
            OSError("second gap"),
            {101: (1, "root"), 202: (101, "child")},
            OSError("new first gap"),
            OSError("new second gap"),
            OSError("new third gap"),
        ]
    )
    monkeypatch.setattr(processes, "_processes", tables)
    sampler = ProcessSampler(tree, max_failures=3)
    assert [sampler.sample() for _ in range(5)] == [False, False, True, False, False]
    assert tree.descendants == {202: "child"}
    with pytest.raises(ProcessTrackingUnavailable):
        sampler.sample()
    assert tables.call_count == 6


def test_one_failure_bound_stops_immediately_with_actionable_generic_error(monkeypatch):
    tree = ProcessTree(101)
    monkeypatch.setattr(tree, "refresh", Mock(side_effect=OSError("sensitive environment data")))
    with pytest.raises(ProcessTrackingUnavailable) as caught:
        ProcessSampler(tree, max_failures=1).sample()
    assert "sensitive environment data" not in str(caught.value)
    assert "verify owned process cleanup" in str(caught.value)
    assert "retry" in str(caught.value)


@pytest.mark.parametrize("error", [RuntimeError("programming error"), KeyboardInterrupt()])
def test_sampler_does_not_hide_unexpected_errors_or_interruption(monkeypatch, error):
    tree = ProcessTree(101)
    monkeypatch.setattr(tree, "refresh", Mock(side_effect=error))
    with pytest.raises(type(error)) as caught:
        ProcessSampler(tree).sample()
    assert caught.value is error


def test_tree_refresh_remains_strict_outside_sampler(monkeypatch):
    error = OSError("metadata unavailable")
    monkeypatch.setattr(processes, "_processes", Mock(side_effect=error))
    with pytest.raises(OSError) as caught:
        ProcessTree(101).refresh()
    assert caught.value is error


def test_final_owned_stop_verification_does_not_use_sampling_retries(monkeypatch):
    error = ValueError("unverifiable final table")
    metadata = Mock(side_effect=error)
    monkeypatch.setattr(processes, "_cleanup_states", metadata)
    with pytest.raises(ValueError) as caught:
        processes.verify_owned_stopped(ProcessTree(101), group=101)
    assert caught.value is error
    assert metadata.call_count == 1
