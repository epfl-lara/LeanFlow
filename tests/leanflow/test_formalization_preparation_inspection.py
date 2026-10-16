"""Bound preparation metadata gaps and retain safe durable failure evidence."""

from __future__ import annotations

import errno
import json
import subprocess

import pytest

from leanflow_cli.formalization import preparation_inspection as inspection
from leanflow_cli.workflows.project_setup.process import SetupCancelled, SetupFailure

SECRET = "DO_NOT_RETAIN_RESOURCE_PROBE_SECRET"


def _gap(operation="rss", error=None):
    """Raise a recoverable observation error through the actual classification seam."""
    selected = error or subprocess.TimeoutExpired(["ps", SECRET], 2, output=SECRET, stderr=SECRET)

    def unavailable():
        raise selected

    return inspection.observe_metadata(operation, unavailable)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (subprocess.TimeoutExpired(["ps", SECRET], 2, output=SECRET, stderr=SECRET), "timeout"),
        (
            subprocess.CalledProcessError(7, ["ps", SECRET], output=SECRET, stderr=SECRET),
            "nonzero_exit",
        ),
        (OSError(errno.EIO, SECRET), "os_error"),
        (subprocess.SubprocessError(SECRET), "subprocess_error"),
    ],
)
@pytest.mark.parametrize("operation", ["parentage_refresh", "parentage_exclusion", "rss"])
def test_durable_gap_diagnostics_record_only_allowlisted_category(
    tmp_path, error, category, operation
):
    path = tmp_path / "inspection.json"
    observer = inspection.PreparationInspection(path)
    assert observer.sample(lambda: _gap(operation, error)) is False
    snapshot = json.loads(path.read_text())
    expected = {"operation": operation, "category": category}
    assert snapshot["first_failure"] == snapshot["last_failure"] == expected
    assert snapshot["total_samples"] == snapshot["total_gaps"] == snapshot["consecutive_gaps"] == 1
    assert snapshot["successful_samples"] == 0
    assert snapshot["max_consecutive_gaps"] == 5
    assert SECRET not in path.read_text()
    assert observer.sample(lambda: None) is True
    observer.finish(cleanup_verified=True)
    final = json.loads(path.read_text())
    assert final["state"] == "closed" and final["cleanup_verified"] is True
    assert final["successful_samples"] == 1 and final["consecutive_gaps"] == 0
    assert final["total_samples"] == 2 and final["total_gaps"] == 1
    assert final["first_failure"] == final["last_failure"] == expected
    assert SECRET not in path.read_text()


def test_fifth_consecutive_gap_fails_and_success_resets_only_the_consecutive_budget(tmp_path):
    path = tmp_path / "inspection.json"
    observer = inspection.PreparationInspection(path)
    for _ in range(4):
        assert observer.sample(_gap) is False
    assert observer.sample(lambda: None) is True
    for _ in range(4):
        assert observer.sample(_gap) is False
    with pytest.raises(SetupFailure) as error:
        observer.sample(_gap)
    assert error.value.code == "setup_resource_inspection_failed"
    snapshot = json.loads(path.read_text())
    assert snapshot["state"] == "failed"
    assert snapshot["total_samples"] == 10 and snapshot["successful_samples"] == 1
    assert snapshot["total_gaps"] == 9 and snapshot["consecutive_gaps"] == 5
    assert snapshot["cleanup_verified"] is False
    assert SECRET not in json.dumps(snapshot) + str(error.value)


@pytest.mark.parametrize(
    "error",
    [
        ValueError("unverifiable live controller identity"),
        SetupCancelled(),
        SetupFailure("setup_deadline", "deadline", "retry"),
        SetupFailure("setup_memory_limit", "memory", "retry"),
        SetupFailure("setup_disk_growth_limit", "disk", "retry"),
    ],
)
def test_nonmetadata_failures_are_immediate_and_never_consume_a_gap_retry(tmp_path, error):
    path = tmp_path / "inspection.json"
    observer = inspection.PreparationInspection(path)
    calls = []

    def fail():
        calls.append(True)
        raise error

    with pytest.raises(type(error)) as caught:
        observer.sample(lambda: inspection.observe_metadata("rss", fail))
    assert caught.value is error and calls == [True]
    snapshot = json.loads(path.read_text())
    assert snapshot["state"] == "failed"
    assert snapshot["total_gaps"] == snapshot["consecutive_gaps"] == 0


def test_unknown_metadata_operation_is_rejected_before_callback():
    called = []
    with pytest.raises(ValueError):
        inspection.observe_metadata(SECRET, lambda: called.append(True))
    assert called == []


@pytest.mark.parametrize("healthy", [False, True])
def test_positive_cleanup_alone_cannot_close_an_unresolved_or_unobserved_sample(tmp_path, healthy):
    path = tmp_path / "inspection.json"
    observer = inspection.PreparationInspection(path)
    if healthy:
        assert observer.sample(lambda: None) is True
    assert observer.sample(_gap) is False
    with pytest.raises(SetupFailure) as error:
        observer.finish(cleanup_verified=True)
    assert error.value.code == "setup_resource_inspection_failed"
    assert json.loads(path.read_text())["state"] == "failed"


def test_healthy_sample_cannot_override_unverified_final_cleanup(tmp_path):
    path = tmp_path / "inspection.json"
    observer = inspection.PreparationInspection(path)
    assert observer.sample(lambda: None) is True
    with pytest.raises(SetupFailure) as error:
        observer.finish(cleanup_verified=False)
    assert error.value.code == "setup_cleanup_unverified"
    snapshot = json.loads(path.read_text())
    assert snapshot["state"] == "failed" and snapshot["cleanup_verified"] is False


@pytest.mark.parametrize("phase", ["initial", "sample", "finish"])
def test_diagnostic_io_failure_is_immediate_and_not_a_recoverable_metadata_gap(
    tmp_path, monkeypatch, phase
):
    path = tmp_path / "inspection.json"
    calls = []

    def fail_write(*args, **kwargs):
        calls.append(True)
        raise OSError(errno.EIO, SECRET)

    if phase == "initial":
        monkeypatch.setattr(inspection, "durable_json", fail_write)
        with pytest.raises(OSError):
            inspection.PreparationInspection(path)
    else:
        observer = inspection.PreparationInspection(path)
        if phase == "finish":
            assert observer.sample(lambda: None) is True
        monkeypatch.setattr(inspection, "durable_json", fail_write)
        with pytest.raises(OSError):
            if phase == "sample":
                observer.sample(lambda: None)
            else:
                observer.finish(cleanup_verified=True)
    assert calls == [True]
    if path.exists():
        assert SECRET not in path.read_text()


def test_diagnostic_symlink_cannot_overwrite_an_unrelated_file(tmp_path):
    original = tmp_path / "unrelated.json"
    original.write_text("original evidence")
    link = tmp_path / "inspection.json"
    link.symlink_to(original)
    with pytest.raises((OSError, ValueError)):
        inspection.PreparationInspection(link)
    assert original.read_text() == "original evidence"


def test_failed_diagnostic_write_is_not_retried_during_abort(tmp_path, monkeypatch):
    observer = inspection.PreparationInspection(tmp_path / "inspection.json")
    calls = []

    def fail_write(*args, **kwargs):
        calls.append(True)
        raise OSError(errno.EIO, SECRET)

    monkeypatch.setattr(inspection, "durable_json", fail_write)
    with pytest.raises(OSError):
        observer.sample(lambda: None)
    with pytest.raises(OSError):
        observer.abort(cleanup_verified=False)
    assert calls == [True]


def test_replaced_diagnostic_cannot_be_overwritten_by_a_later_sample(tmp_path):
    path = tmp_path / "inspection.json"
    observer = inspection.PreparationInspection(path)
    path.unlink()
    path.write_text("external replacement")
    with pytest.raises(ValueError):
        observer.sample(lambda: None)
    assert path.read_text() == "external replacement"


def test_failed_unwind_diagnostic_never_becomes_success_from_cleanup_alone(tmp_path):
    path = tmp_path / "inspection.json"
    observer = inspection.PreparationInspection(path)
    assert observer.sample(lambda: None) is True
    observer.abort(cleanup_verified=True)
    snapshot = json.loads(path.read_text())
    assert snapshot["state"] == "failed" and snapshot["cleanup_verified"] is True
    with pytest.raises(SetupFailure):
        observer.sample(lambda: None)


def test_gap_wrapper_preserves_original_cause_only_in_memory():
    error = subprocess.TimeoutExpired(["ps", SECRET], 2, output=SECRET, stderr=SECRET)
    with pytest.raises(inspection.InspectionGap) as caught:
        _gap("rss", error)
    assert caught.value.__cause__ is error
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("first", ["parent", "monitor"])
def test_preparation_ownership_assigns_one_terminal_writer_in_both_handoff_orders(first):
    from leanflow_cli.formalization import preparation_resources

    releases = []
    owner = preparation_resources._PreparationOwnership(lambda: releases.append(True))
    if first == "parent":
        parent_writes = owner.transfer()
        assert parent_writes is False and releases == []
        monitor_writes = owner.relinquish()
    else:
        monitor_writes = owner.relinquish()
        assert monitor_writes is False and releases == []
        parent_writes = owner.transfer()
    assert int(parent_writes) + int(monitor_writes) == 1
    assert releases == [True]
    with pytest.raises(RuntimeError):
        owner.transfer()
    with pytest.raises(RuntimeError):
        owner.relinquish()
    with pytest.raises(RuntimeError):
        owner.release_normal()
    assert releases == [True]


def test_normally_joined_preparation_releases_once_without_a_deferred_writer():
    from leanflow_cli.formalization import preparation_resources

    releases = []
    owner = preparation_resources._PreparationOwnership(lambda: releases.append(True))
    assert owner.relinquish() is False and releases == []
    owner.release_normal()
    owner.release_normal()
    assert releases == [True]
