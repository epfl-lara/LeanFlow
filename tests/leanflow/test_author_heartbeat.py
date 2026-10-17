"""Pin bounded author display and exact accounting ownership under adversarial schedules."""

from __future__ import annotations

import json
import threading
import time

import pytest

from core.campaign_limits import AccountingError, CampaignLimits
from leanflow_cli.formalization import preparation_campaign as campaign
from leanflow_cli.native.harness_heartbeat import AuthorHeartbeat
from leanflow_cli.native.harness_observer import NativeObserver
from leanflow_cli.workflows import workflow_state
from tests.leanflow.test_formalization_preflight_progress import _held_status_lock


@pytest.mark.parametrize("held", ["status", "activity"])
def test_quiet_author_checkpoints_and_closes_under_real_display_lock(tmp_path, monkeypatch, held):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "author-fixture-token")
    observer = NativeObserver(tmp_path, "fixture", "formalize", "codex")
    state = {"status": "running"}
    observer.start(state)
    path = (
        workflow_state.workflow_live_status_path()
        if held == "status"
        else workflow_state.workflow_run_activity_path("fixture")
    )
    budget = CampaignLimits(tmp_path / "budget.json", {"wall_time_s": 30})
    cancelled = threading.Event()

    def checkpoint():
        observer.publish(state, budget)
        observer.event("tool-start", {"tool": "read_evidence"})

    with _held_status_lock(path, sidecar=held == "status"):
        ticker = AuthorHeartbeat(budget, cancelled, checkpoint, interval_s=0.02)
        ticker.start()
        deadline = time.monotonic() + 2
        while json.loads(budget.path.read_text())["elapsed_s"] < 0.08:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        started = time.monotonic()
        cancelled.set()
        assert ticker.close(timeout_s=0.5)
        ticker.check()
        budget.finish_checkpoints()
        observer.finish({"status": "cancelled"})
        assert time.monotonic() - started < 0.5
    assert not json.loads(budget.path.read_text()).get("checkpoint_worker_active")


@pytest.mark.parametrize("parent_first", [True, False])
def test_exact_author_writer_releases_owner_without_a_reaper(tmp_path, monkeypatch, parent_first):
    entered, release, relinquished, physically_return = (threading.Event() for _ in range(4))
    budget = CampaignLimits(tmp_path / "budget.json", {})
    cancelled = threading.Event()
    ticker = None
    worker = None
    try:
        with campaign.campaign_lock(tmp_path, "fixture") as owner:
            original_close = owner.close

            def close(*, worker=False):
                original_close(worker=worker)
                if worker:
                    relinquished.set()
                    assert physically_return.wait(3)

            monkeypatch.setattr(owner, "close", close)

            def checkpoint():
                entered.set()
                assert release.wait(3)

            ticker = AuthorHeartbeat(budget, cancelled, checkpoint, owner=owner, interval_s=0.01)
            ticker.start()
            worker = ticker._thread
            assert entered.wait(2)
            assert not ticker.close(timeout_s=0.01)
            if not parent_first:
                release.set()
                assert relinquished.wait(2)
                assert worker is not None and worker.is_alive()
            with pytest.raises(ValueError, match="active accounting owner"):
                with campaign.campaign_lock(tmp_path, "fixture"):
                    pytest.fail("Accounting owner released before its parent")
        if parent_first:
            with pytest.raises(ValueError, match="active accounting owner"):
                with campaign.campaign_lock(tmp_path, "fixture"):
                    pytest.fail("A pending checkpoint lost serial ownership")
            release.set()
            assert relinquished.wait(2)
        # Physical liveness after the final effect must not require a third
        # thread to notice death and release ownership.
        assert worker is not None and worker.is_alive()
        with campaign.campaign_lock(tmp_path, "fixture"):
            pass
        assert json.loads(budget.path.read_text())["checkpoint_worker_active"]
    finally:
        release.set()
        physically_return.set()
        if worker is not None:
            worker.join(3)
            assert not worker.is_alive()


def test_active_writer_marker_blocks_reopen_without_mutating_old_ledger(tmp_path):
    budget = CampaignLimits(tmp_path / "budget.json", {"wall_time_s": 30})
    call = budget.reserve()
    budget.started_call(call)
    budget.begin_checkpoints()
    before = budget.path.read_bytes()
    with pytest.raises(AccountingError, match="accounting is unresolved"):
        CampaignLimits(budget.path, budget.limits)
    assert budget.path.read_bytes() == before
    budget.finish(call, None, started=True)
    budget.finish_checkpoints()
    reopened = CampaignLimits(budget.path, budget.limits)
    assert reopened.state["invocations"][call]["status"] == "finished"
