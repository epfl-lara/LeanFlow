"""Tests for exact native process-artifact cleanup."""

from __future__ import annotations

from leanflow_cli.native import process_artifact_cleanup


def test_native_artifact_cleanup_attempts_owner_and_waiter_release(monkeypatch, tmp_path):
    calls: list[object] = []
    monkeypatch.setattr(
        process_artifact_cleanup,
        "release_workflow_run_log_owner",
        lambda: calls.append("owner") or True,
    )
    monkeypatch.setattr(
        process_artifact_cleanup,
        "reclaim_process_foreground_waiters",
        lambda root, *, process_id: calls.append((root, process_id)) or (),
    )
    monkeypatch.setattr(process_artifact_cleanup.os, "getpid", lambda: 4242)

    process_artifact_cleanup.release_native_process_artifacts(tmp_path)

    assert calls == ["owner", (tmp_path, 4242)]


def test_native_artifact_cleanup_reports_residual_locked_waiter(monkeypatch, tmp_path):
    monkeypatch.setattr(
        process_artifact_cleanup,
        "release_workflow_run_log_owner",
        lambda: True,
    )
    monkeypatch.setattr(
        process_artifact_cleanup,
        "reclaim_process_foreground_waiters",
        lambda *_args, **_kwargs: ("waiter-4242-live.lock",),
    )

    try:
        process_artifact_cleanup.release_native_process_artifacts(tmp_path)
    except RuntimeError as exc:
        assert "locked foreground waiters remain" in str(exc)
    else:
        raise AssertionError("locked waiter cleanup unexpectedly succeeded")
