"""Check problem-lane ordering, independent sources and conservative scoring."""

from pathlib import Path

import pytest

from scripts.lean_imo_campaign.artifacts import digest, environment, prepare, save
from scripts.lean_imo_campaign.matrix import CONDITIONS, cells, next_cell
from scripts.lean_imo_campaign.runner import refresh


def test_two_lanes_finish_conditions_before_claiming_next_problem() -> None:
    rows = cells([{"id": f"problem-{i}"} for i in range(1, 4)])
    first = next_cell(rows, 1)
    second = next_cell(rows, 2)
    assert first is not None and second is not None
    assert (first["id"], second["id"]) == ("problem-1-astra-bottom", "problem-2-astra-bottom")
    first["status"] = second["status"] = "running"
    assert next_cell(rows, 1) is None
    assert next_cell(rows, 2) is None
    for label, _, _ in CONDITIONS:
        assert first["condition"] == label
        first["status"] = "budget_exhausted"
        first = next_cell(rows, 1)
        assert first is not None
        first["status"] = "running"
    assert first["id"] == "problem-3-astra-bottom"
    assert second["status"] == "running"
    assert sum(row["status"] == "running" for row in rows) == 2


def test_only_model_and_order_change_across_conditions() -> None:
    rows = cells([{"id": "p"}])
    comparable = [
        {
            k: v
            for k, v in row["config"].items()
            if k not in {"model", "orchestrator_model", "search_order"}
        }
        for row in rows
    ]
    assert all(c == comparable[0] for c in comparable)
    config = comparable[0]
    assert (
        config["parallelism"],
        config["job_api_calls"],
        config["orchestrator_api_calls"],
        config["total_api_calls"],
        config["wall_time_s"],
    ) == (4, 200, 50, 2000, 28800)
    assert config["allow_internet"] is False
    assert all(c["effort"] == "xhigh" for c in rows)


def test_each_cell_starts_from_frozen_statement_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from scripts.lean_imo_campaign import artifacts

    baseline = tmp_path / "baseline"
    baseline.mkdir()
    for name in ("lakefile.toml", "lake-manifest.json", "lean-toolchain"):
        (baseline / name).write_text("fixture")
    source = baseline / "LeanIMOBench/Test.lean"
    source.parent.mkdir()
    source.write_text("theorem test : True := by sorry\n")
    (baseline / "Solution.lean").write_text("this must not leak")
    packages = baseline / ".lake/packages/mathlib"
    packages.mkdir(parents=True)
    (packages / "Example.lean").write_text("local library")
    for skill in ("lean-bounded-prover", "lean-prover-orchestrator"):
        path = tmp_path / "runtime/leanflow_skills" / skill / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("frozen contract")
    save(
        tmp_path / "provenance.json",
        {"statement_files": {"LeanIMOBench/Test.lean": digest(source)}},
    )
    monkeypatch.setattr(artifacts, "clone", shutil.copytree)
    monkeypatch.setattr(artifacts, "initialize_lake", lambda _: {})
    rows = cells([{"id": "p", "file": "LeanIMOBench/Test.lean"}])
    a = prepare(tmp_path, rows[0])
    (a / "LeanIMOBench/Test.lean").write_text("theorem test : True := by trivial\n")
    b = prepare(tmp_path, rows[1])
    assert (b / "LeanIMOBench/Test.lean").read_bytes() == source.read_bytes()
    assert not (b / "Solution.lean").exists()
    assert not (b / ".lake/packages").is_symlink()
    with pytest.raises(FileExistsError):
        prepare(tmp_path, rows[0])


@pytest.mark.parametrize(
    "status,terminal,accepted,expected",
    [
        ("completed", True, True, True),
        ("completed", True, False, False),
        ("running", False, True, False),
        ("completed", False, True, False),
    ],
)
def test_scoring_requires_terminal_independent_acceptance(
    tmp_path: Path, status: str, terminal: bool, accepted: bool, expected: bool
) -> None:
    cell = {"project": str(tmp_path), "run_id": "test"}
    path = tmp_path / ".leanflow/workflow-state/prover/test/state.json"
    save(
        path,
        {
            "status": status,
            "terminal": terminal,
            "verification": {"accepted": accepted},
            "metrics": {"api_calls": 12},
        },
    )
    refresh(cell)
    assert cell["verified"] is expected
    assert cell["metrics"]["api_calls"] == 12


def test_environment_discards_old_budget_and_python_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEANFLOW_PROVER_TOTAL_API_CALLS", "999999")
    monkeypatch.setenv("LEANFLOW_NATIVE_ADDITIONAL_SKILLS", "/wrong/skill")
    monkeypatch.setenv("PYTHONPATH", "/wrong/runtime")
    env = environment(tmp_path)
    assert "LEANFLOW_PROVER_TOTAL_API_CALLS" not in env
    assert "LEANFLOW_NATIVE_ADDITIONAL_SKILLS" not in env
    assert env["PYTHONPATH"] == str(tmp_path / "runtime")
    assert env["LEANFLOW_DISABLE_REPOSITORY_RESEARCH"] == "1"


def test_provider_recovery_preserves_cumulative_metrics_and_is_bounded() -> None:
    from scripts.lean_imo_campaign.recovery import schedule_recovery

    cell = {
        "status": "provider_error",
        "run_id": "original",
        "metrics": {"api_calls": 132, "elapsed_s": 400},
    }
    for attempt in range(1, 4):
        cell["status"] = "provider_error"
        assert schedule_recovery(cell)
        assert cell["recovery_attempts"] == attempt
        assert cell["resume_run_id"] == "original"
        assert cell["metrics"] == {"api_calls": 132, "elapsed_s": 400}
    cell["status"] = "provider_error"
    assert not schedule_recovery(cell)
    assert len(cell["executions"]) == 3


@pytest.mark.parametrize(
    "status,calls,elapsed",
    [
        ("source_conflict", 1, 1),
        ("provider_error", 2000, 1),
        ("provider_error", 1, 28800),
        ("environment_error", 0, 0),
    ],
)
def test_recovery_never_bypasses_exhaustion_or_retries_invalid_environment(
    status: str, calls: int, elapsed: int
) -> None:
    from scripts.lean_imo_campaign.recovery import schedule_recovery

    assert not schedule_recovery(
        {"status": status, "metrics": {"api_calls": calls, "elapsed_s": elapsed}}
    )


def test_incomplete_observer_snapshot_does_not_crash_dispatcher(tmp_path: Path) -> None:
    path = tmp_path / ".leanflow/workflow-state/prover/test/state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")
    cell = {"project": str(tmp_path), "run_id": "test", "verified": False}
    refresh(cell)
    assert cell["refresh_error"] and cell["verified"] is False


@pytest.mark.parametrize(
    "error",
    [
        "Invalid prompt: usage policy",
        "authentication failed",
        "context_length_exceeded",
        "insufficient_quota",
    ],
)
def test_recovery_does_not_retry_permanent_provider_errors(error):
    from scripts.lean_imo_campaign.recovery import schedule_recovery

    assert not schedule_recovery({"status": "provider_error", "error": error, "metrics": {}})


def test_recovery_obeys_custom_budget_and_copies_historical_metrics():
    from scripts.lean_imo_campaign.recovery import schedule_recovery

    cell = {
        "status": "provider_error",
        "run_id": "saved",
        "config": {"total_api_calls": 10, "wall_time_s": 100},
        "metrics": {"api_calls": 9, "elapsed_s": 99},
    }
    assert schedule_recovery(cell)
    cell["metrics"]["api_calls"] = 10
    assert cell["executions"][0]["metrics"]["api_calls"] == 9
    cell["status"] = "provider_error"
    assert not schedule_recovery(cell)


def test_scheduler_stops_pause_dispatch_even_with_legacy_status():
    from scripts.lean_imo_campaign.recovery import requires_inspection

    assert requires_inspection(
        {"status": "budget_exhausted", "stop_reason": {"scope": "scheduler"}}
    )
    assert requires_inspection({"status": "blocked"})
    assert not requires_inspection(
        {"status": "budget_exhausted", "stop_reason": {"scope": "campaign"}}
    )


def test_csv_moves_unverified_legacy_cost_out_of_comparable_cost_column(tmp_path):
    import csv

    from scripts.lean_imo_campaign.runner import report

    cell = {
        "id": "test",
        "problem": {"id": "test"},
        "condition": "terra-bottom",
        "status": "budget_exhausted",
        "verified": False,
        "metrics": {"cost_usd": 152.81, "cost_complete": True},
        "stop_reason": {"code": "no_runnable_obligations", "scope": "scheduler"},
    }
    report(tmp_path, {"cells": [cell]})
    with (tmp_path / "metrics.csv").open() as handle:
        row = next(csv.DictReader(handle))
    assert row["cost_usd"] == ""
    assert row["legacy_unverified_cost_usd"] == "152.81"
    assert row["cost_source"] == "unavailable"
    assert row["stop_scope"] == "scheduler"
    assert row["runtime_sha256"] == ""
    assert cell["metrics"]["cost_usd"] == 152.81
