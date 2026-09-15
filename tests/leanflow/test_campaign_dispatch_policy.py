"""Exercise serial admission and automatic queue advancement across saved lanes."""

import copy
import json
import time

import pytest

from scripts.lean_imo_campaign.dispatch_policy import (
    active_limit,
    pause_after_failure,
    reserved_active_count,
)
from scripts.lean_imo_campaign.matrix import CONDITION_SETS, cells


def test_active_limit_is_independent_of_lane_count(monkeypatch):
    monkeypatch.delenv("LEANFLOW_CAMPAIGN_MAX_ACTIVE", raising=False)
    assert active_limit(3) == 3
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_MAX_ACTIVE", "1")
    assert active_limit(3) == 1
    for bad in ("0", "4", "-1"):
        monkeypatch.setenv("LEANFLOW_CAMPAIGN_MAX_ACTIVE", bad)
        with pytest.raises(ValueError, match="MAX_ACTIVE"):
            active_limit(3)


@pytest.mark.parametrize(
    "status,error,keep_going",
    [
        ("provider_error", "incomplete chunked read", True),
        ("provider_error", "Connection refused", True),
        ("provider_error", "Unable to verify model access right now", True),
        ("provider_error", "authentication failed; connection reset", False),
        ("provider_error", "insufficient_quota", False),
        ("source_conflict", "connection reset", False),
        ("environment_error", "Connection refused", False),
        ("provider_error", "unknown provider failure", False),
    ],
)
def test_queue_advancement_is_opt_in_and_transport_only(status, error, keep_going):
    row = {"status": status, "error": error}
    assert pause_after_failure(row, False)
    assert pause_after_failure(row, True) is not keep_going


def test_serial_runner_automatically_advances_without_reassigning_saved_lanes(
    tmp_path, monkeypatch
):
    from scripts.lean_imo_campaign import runner

    rows = cells([{"id": str(i)} for i in range(3)], CONDITION_SETS["astra-low-luna-high-top"])
    rows[0].update(lane=3, recovery_attempts=3)
    rows[1]["lane"] = 1
    original_configs = json.loads(json.dumps([row["config"] for row in rows]))
    (tmp_path / "campaign.json").write_text(json.dumps({"cells": rows}))
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_LANES", "3")
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_MAX_ACTIVE", "1")
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_CONTINUE_TRANSIENT_ERRORS", "1")
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    launched, snapshots = [], []
    outcomes = {"0": "provider_error", "1": "budget_exhausted", "2": "completed"}

    class FinishedProcess:
        def poll(self):
            return 0

    def launch(directory, campaign, cell):
        launched.append(cell["problem"]["id"])
        cell.update(status="running", started_epoch=time.time())
        return FinishedProcess()

    def refresh(cell):
        outcome = outcomes[cell["problem"]["id"]]
        cell.update(controller_status=outcome, verified=outcome == "completed")
        if outcome == "provider_error":
            cell["error"] = "peer closed connection (incomplete chunked read)"

    def report(directory, campaign):
        snapshots.append(copy.deepcopy(campaign))

    monkeypatch.setattr(runner, "launch", launch)
    monkeypatch.setattr(runner, "refresh", refresh)
    monkeypatch.setattr(runner, "report", report)
    runner.run(tmp_path)
    assert launched == ["0", "1", "2"]
    assert max(sum(c["status"] == "running" for c in s["cells"]) for s in snapshots) == 1
    final = snapshots[-1]
    assert final["status"] == "completed"
    assert [row["config"] for row in final["cells"]] == original_configs
    assert [row["lane"] for row in final["cells"][:2]] == [3, 1]
    assert final["cells"][0]["status"] == "provider_error"
    assert final["cells"][1]["status"] == "budget_exhausted"
    assert final["cells"][2]["verified"]


def test_truncated_report_does_not_strand_queue_or_interrupt_active_peer(tmp_path, monkeypatch):
    """Replay one report failure while another problem stays active through new admission."""
    from leanflow_cli.workflows.prover.session_finalization import REPORT_FAILURE
    from scripts.lean_imo_campaign import runner

    rows = cells([{"id": str(i)} for i in range(3)], CONDITION_SETS["kimi-glm-flash-top"])
    original_configs = json.loads(json.dumps([r["config"] for r in rows]))
    (tmp_path / "campaign.json").write_text(json.dumps({"cells": rows}))
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_LANES", "2")
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_MAX_ACTIVE", "2")
    monkeypatch.delenv("LEANFLOW_CAMPAIGN_CONTINUE_TRANSIENT_ERRORS", raising=False)
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    launched, snapshots = [], []

    class Process:
        def __init__(self, problem):
            self.problem = problem

        def poll(self):
            return None if self.problem == "1" and "2" not in launched else 0

    def launch(directory, campaign, cell):
        problem = cell["problem"]["id"]
        launched.append(problem)
        cell.update(status="running", started_epoch=time.time())
        return Process(problem)

    def refresh(cell):
        failed = cell["problem"]["id"] == "0"
        cell.update(
            controller_status="provider_error" if failed else "completed", verified=not failed
        )
        if failed:
            cell["error"] = REPORT_FAILURE

    monkeypatch.setattr(runner, "launch", launch)
    monkeypatch.setattr(runner, "refresh", refresh)
    monkeypatch.setattr(runner, "report", lambda _, c: snapshots.append(copy.deepcopy(c)))
    runner.run(tmp_path)
    assert launched == ["0", "1", "2"]
    assert any(s["cells"][1]["status"] == s["cells"][2]["status"] == "running" for s in snapshots)
    assert max(sum(c["status"] == "running" for c in s["cells"]) for s in snapshots) == 2
    final = snapshots[-1]
    assert final["status"] == "completed" and final["cells"][0]["status"] == "provider_error"
    assert [row["config"] for row in final["cells"]] == original_configs
    assert not final["cells"][0].get("resume_run_id")


@pytest.mark.parametrize("error", ["authentication failed", "insufficient_quota", "unknown error"])
def test_report_failure_isolation_does_not_bypass_infrastructure_stops(error):
    assert pause_after_failure({"status": "provider_error", "error": error}, False)


def test_replacement_queue_reserves_a_draining_peer_then_releases_its_slot(tmp_path, monkeypatch):
    from scripts.lean_imo_campaign import runner

    previous = tmp_path / "previous.json"
    reservations = [{"campaign": str(previous), "cell_id": "survivor"}]
    assert reserved_active_count(reservations) == 1
    previous.write_text("{")
    assert reserved_active_count(reservations) == 1
    previous.write_text(json.dumps({"cells": [{"id": "survivor", "status": "running"}]}))
    rows = cells([{"id": str(i)} for i in range(3)], CONDITION_SETS["kimi-glm-flash-top"])
    (tmp_path / "campaign.json").write_text(
        json.dumps({"cells": rows, "reserved_campaign_cells": reservations})
    )
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_LANES", "2")
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_MAX_ACTIVE", "2")
    launched, snapshots = [], []

    class Process:
        def poll(self):
            return 0 if len(launched) >= 2 else None

    def launch(directory, campaign, cell):
        launched.append(cell["id"])
        cell.update(status="running", started_epoch=time.time())
        return Process()

    def tick(_seconds):
        assert len(launched) >= 1
        previous.write_text(json.dumps({"cells": [{"id": "survivor", "status": "completed"}]}))

    monkeypatch.setattr(runner.time, "sleep", tick)
    monkeypatch.setattr(runner, "launch", launch)
    monkeypatch.setattr(
        runner, "refresh", lambda c: c.update(controller_status="completed", verified=True)
    )
    monkeypatch.setattr(runner, "report", lambda _, c: snapshots.append(copy.deepcopy(c)))
    runner.run(tmp_path)
    assert len(launched) == 3
    assert reserved_active_count(reservations) == 0
    assert any(
        s.get("reserved_active_count") == 1
        and sum(c["status"] == "running" for c in s["cells"]) == 1
        for s in snapshots
    )
    assert all(
        sum(c["status"] == "running" for c in s["cells"]) + s.get("reserved_active_count", 0) <= 2
        for s in snapshots
    )
    assert any(sum(c["status"] == "running" for c in s["cells"]) == 2 for s in snapshots)
