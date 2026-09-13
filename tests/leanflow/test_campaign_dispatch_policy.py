"""Exercise serial admission and automatic queue advancement across saved lanes."""

import copy
import json
import time

import pytest

from scripts.lean_imo_campaign.dispatch_policy import active_limit, pause_after_failure
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
