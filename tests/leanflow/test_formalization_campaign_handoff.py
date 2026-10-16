"""Pin accounting checkpoints and first-author handoff independently of live display status."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from core.campaign_limits import AccountingError, CampaignLimits
from leanflow_cli.formalization import preparation_campaign as campaign
from leanflow_cli.formalization.preflight_owner import PreflightProgress


def test_silent_preflight_checkpoints_elapsed_without_live_status(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("core.campaign_limits.time.monotonic", lambda: clock[0])
    budget = CampaignLimits(tmp_path / "budget.json", {})
    progress = PreflightProgress("fixture", threading.Event(), SimpleNamespace(), enabled=False)
    progress.bind_budget(budget)
    monkeypatch.setattr(
        "leanflow_cli.formalization.preflight_owner.publish_workflow_preflight_progress",
        lambda *args, **kwargs: pytest.fail("Unclaimed live status write"),
    )
    clock[0] += 17
    progress._publish()
    assert json.loads(budget.path.read_text())["elapsed_s"] == 17


def test_checkpoint_io_failure_cancels_and_surfaces_before_handoff(tmp_path, monkeypatch):
    budget = CampaignLimits(tmp_path / "budget.json", {})
    failed = threading.Event()

    def fail():
        failed.set()
        raise OSError("Fixture checkpoint unavailable")

    cancelled = threading.Event()
    progress = PreflightProgress("fixture", cancelled, SimpleNamespace(), enabled=False)
    progress.bind_budget(budget)
    monkeypatch.setattr(budget, "persist", fail)
    progress.start()
    assert failed.wait(2)
    with pytest.raises(RuntimeError, match="published safely"):
        progress.close()
    assert cancelled.is_set()


def test_blocked_checkpoint_retains_campaign_owner_until_exact_worker_returns(
    tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    budget = CampaignLimits(tmp_path / "budget.json", {})
    original_persist = budget.persist

    def blocked():
        if threading.current_thread() is threading.main_thread():
            return original_persist()
        entered.set()
        assert release.wait(5)
        original_persist()

    monkeypatch.setattr(budget, "persist", blocked)
    progress = None
    worker = None
    try:
        with campaign.campaign_lock(tmp_path, "fixture") as owner:
            progress = PreflightProgress(
                "fixture",
                threading.Event(),
                SimpleNamespace(),
                enabled=False,
                accounting_owner=owner,
            )
            progress.bind_budget(budget)
            progress.start()
            assert entered.wait(2)
            worker = progress._thread
            assert worker is not None
            # Exercise the existing finite-close failure without spending its full grace.
            with monkeypatch.context() as patch:
                patch.setattr(worker, "join", lambda timeout=None: None)
                with pytest.raises(RuntimeError, match="did not stop"):
                    progress.close()
        with pytest.raises(ValueError, match="active accounting owner"):
            with campaign.campaign_lock(tmp_path, "fixture"):
                pytest.fail("A second writer entered before the old checkpoint returned")
    finally:
        release.set()
        if worker is not None:
            worker.join(3)
            assert not worker.is_alive()
    with campaign.campaign_lock(tmp_path, "fixture"):
        pass
    with pytest.raises(AccountingError, match="accounting is unresolved"):
        CampaignLimits(budget.path, {})


def test_killed_quiet_preflight_keeps_admission_and_blocks_uncertain_wall_time(tmp_path):
    """A process exit cannot turn an unclosed checkpoint interval into fresh allowance."""
    import multiprocessing
    import os

    path = tmp_path / "budget.json"

    def interrupted():
        with campaign.campaign_lock(tmp_path, "crashed") as owner:
            budget = CampaignLimits(path, {"wall_time_s": 60})
            progress = PreflightProgress(
                "crashed",
                threading.Event(),
                SimpleNamespace(),
                enabled=False,
                accounting_owner=owner,
            )
            progress.bind_budget(budget)
            call = budget.reserve()
            budget.started_call(call)
            checkpointed = threading.Event()
            original_persist = budget.persist

            def persist():
                original_persist()
                if threading.current_thread() is not threading.main_thread():
                    checkpointed.set()

            budget.persist = persist
            progress.start()
            assert checkpointed.wait(2)
            os._exit(89)

    child = multiprocessing.get_context("fork").Process(target=interrupted)
    child.start()
    child.join(5)
    if child.is_alive():
        child.kill()
        child.join(2)
        pytest.fail("Quiet preflight fixture failed to exit")
    assert child.exitcode == 89
    before = path.read_bytes()
    saved = json.loads(before)
    assert saved["checkpoint_worker_active"] is True
    assert len(saved["invocations"]) == 1
    assert next(iter(saved["invocations"].values()))["status"] == "running"
    with pytest.raises(AccountingError, match="accounting is unresolved"):
        CampaignLimits(path, {"wall_time_s": 60})
    assert path.read_bytes() == before


def test_checkpoint_worker_can_finish_before_parent_releases_accounting(tmp_path):
    with campaign.campaign_lock(tmp_path, "fixture") as owner:
        progress = PreflightProgress(
            "fixture",
            threading.Event(),
            SimpleNamespace(),
            enabled=False,
            accounting_owner=owner,
        )
        progress.start()
        progress.close()
        with pytest.raises(ValueError, match="active accounting owner"):
            with campaign.campaign_lock(tmp_path, "fixture"):
                pytest.fail("Worker closure released its still-live parent")
    with campaign.campaign_lock(tmp_path, "fixture"):
        pass


def test_owner_cannot_attach_after_worker_snapshots_its_final_owner(tmp_path, monkeypatch):
    """Reject late admission even while the finalized worker remains physically alive."""
    snapshotted, release = threading.Event(), threading.Event()
    progress = PreflightProgress("fixture", threading.Event(), SimpleNamespace(), enabled=False)
    underlying = threading.Lock()

    class FinalizationBarrier:
        def __enter__(self):
            underlying.acquire()

        def __exit__(self, *args):
            underlying.release()
            if threading.current_thread() is progress._thread:
                snapshotted.set()
                assert release.wait(3)

    monkeypatch.setattr(progress, "_lock", FinalizationBarrier())
    monkeypatch.setattr(progress, "_publish", lambda: None)
    progress._finished.set()
    progress.start()
    worker = progress._thread
    assert worker is not None
    owner = None
    try:
        assert snapshotted.wait(2)
        assert worker.is_alive()
        with campaign.campaign_lock(tmp_path, "fixture") as owner:
            with pytest.raises(RuntimeError, match="finalized"):
                progress.bind_accounting_owner(owner)
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        # Recover fixture ownership on the old-code RED path after its exact worker closed.
        if owner is not None:
            owner.close(worker=True)
    with campaign.campaign_lock(tmp_path, "fixture"):
        pass
