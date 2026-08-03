"""Keep process-owner maintenance alive during synchronous foreground work."""

from __future__ import annotations

import threading

import pytest

from leanflow_cli.native.parent_maintenance import (
    quiesce_parent_maintained_actions,
    run_with_parent_maintenance,
    start_parent_maintained_action,
)


def test_without_maintenance_action_stays_on_calling_thread():
    caller = threading.get_ident()

    result = run_with_parent_maintenance(
        lambda: threading.get_ident(), maintenance=None, interval_s=0.01
    )

    assert result == caller


def test_maintenance_runs_on_parent_while_blocking_action_runs_in_worker():
    caller = threading.get_ident()
    maintained = threading.Event()
    action_thread_ids: list[int] = []
    maintenance_thread_ids: list[int] = []

    def action() -> str:
        action_thread_ids.append(threading.get_ident())
        assert maintained.wait(timeout=2)
        return "done"

    def maintenance() -> None:
        maintenance_thread_ids.append(threading.get_ident())
        maintained.set()

    result = run_with_parent_maintenance(action, maintenance=maintenance, interval_s=0.01)

    assert result == "done"
    assert action_thread_ids and action_thread_ids[0] != caller
    assert maintenance_thread_ids == [caller]


def test_action_error_is_reraised_after_maintenance_supervision():
    maintained = threading.Event()

    def action() -> None:
        assert maintained.wait(timeout=2)
        raise ValueError("planner failed")

    with pytest.raises(ValueError, match="planner failed"):
        run_with_parent_maintenance(
            action,
            maintenance=maintained.set,
            interval_s=0.01,
        )


def test_base_exception_cancels_and_joins_parent_maintained_writer():
    release = threading.Event()
    maintenance_started = threading.Event()

    class _Termination(BaseException):
        pass

    def action() -> None:
        release.wait(timeout=2)

    def maintenance() -> None:
        maintenance_started.set()
        raise _Termination("terminate owner")

    with pytest.raises(_Termination, match="terminate owner"):
        run_with_parent_maintenance(
            action,
            maintenance=maintenance,
            cancel=release.set,
            interval_s=0.01,
            cancellation_join_timeout_s=1.0,
        )

    assert maintenance_started.is_set()
    assert quiesce_parent_maintained_actions(timeout_s=0.1) == ()


def test_uncooperative_parent_maintained_writer_remains_visible_to_finalizer():
    release = threading.Event()

    class _Termination(BaseException):
        pass

    def action() -> None:
        release.wait(timeout=2)

    with pytest.raises(_Termination):
        run_with_parent_maintenance(
            action,
            maintenance=lambda: (_ for _ in ()).throw(_Termination()),
            interval_s=0.01,
            cancellation_join_timeout_s=0.01,
        )

    assert quiesce_parent_maintained_actions(timeout_s=0.01) == (
        "leanflow-parent-maintained-action",
    )
    release.set()
    assert quiesce_parent_maintained_actions(timeout_s=1.0) == ()


def test_named_auxiliary_action_is_single_flight_and_never_blocks_caller():
    started = threading.Event()
    release = threading.Event()
    duplicate_ran = threading.Event()

    def action() -> None:
        started.set()
        release.wait(timeout=2)

    assert start_parent_maintained_action(action, name="research-maintenance") is True
    assert started.wait(timeout=1)
    assert (
        start_parent_maintained_action(
            duplicate_ran.set,
            name="research-maintenance",
        )
        is False
    )
    assert not duplicate_ran.is_set()

    release.set()
    assert quiesce_parent_maintained_actions(timeout_s=1) == ()


def test_named_auxiliary_action_failure_is_contained():
    finished = threading.Event()

    def action() -> None:
        try:
            raise RuntimeError("optional maintenance failed")
        finally:
            finished.set()

    assert start_parent_maintained_action(action, name="failing-maintenance") is True
    assert finished.wait(timeout=1)
    assert quiesce_parent_maintained_actions(timeout_s=1) == ()
