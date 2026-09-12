"""Check problem-lane ordering, independent sources and conservative scoring."""

import json
from pathlib import Path

import pytest

from scripts.lean_imo_campaign.artifacts import digest, environment, prepare, save
from scripts.lean_imo_campaign.matrix import CONDITION_SETS, CONDITIONS, WIDE, cells, next_cell
from scripts.lean_imo_campaign.runner import refresh


@pytest.mark.parametrize(
    "name,model",
    [
        ("astra-low-glm-flash-top", "zai-org/GLM-5.3-Flash"),
        ("astra-low-deepseek-flash-top", "deepseek-ai/DeepSeek-V4-Flash-0731"),
    ],
)
def test_rcp_arms_route_workers_and_planning_separately(name, model):
    from leanflow_cli.workflows.prover.config import ProverConfig

    row = cells([{"id": "p"}], CONDITION_SETS[name])[0]
    config = ProverConfig(**row["config"])
    worker = config.to_mapping("prover")
    planner = config.to_mapping("orchestrator")
    assert worker["model"] == model and worker["provider"] == "rcp"
    assert worker["api_key_env"] == "RCP_API_KEY"
    assert planner["model"] == "gpt-6-astra" and planner["provider"] == "openai-codex"
    assert planner["reasoning_effort"] == "low" and planner["api_key_env"] == ""
    assert worker["search_order"] == "top-down"
    assert (worker["parallelism"], worker["job_api_calls"], worker["total_api_calls"]) == (
        1,
        150,
        5000,
    )


def test_campaign_environment_carries_only_assigned_rcp_key(tmp_path, monkeypatch):
    monkeypatch.setenv("RCP_API_KEY", "assigned-test-key")
    monkeypatch.setenv("RCP_API_KEY_RESERVE", "other-test-key")
    env = environment(tmp_path)
    assert env["RCP_API_KEY"] == "assigned-test-key"
    assert "RCP_API_KEY_RESERVE" not in env


def test_two_lanes_finish_conditions_before_claiming_next_problem() -> None:
    rows = cells([{"id": f"problem-{i}"} for i in range(1, 4)])
    first = next_cell(rows, 1)
    second = next_cell(rows, 2)
    assert first is not None and second is not None
    assert (first["id"], second["id"]) == ("problem-1-astra-bottom", "problem-2-astra-bottom")
    first["status"] = second["status"] = "running"
    assert next_cell(rows, 1) is None
    assert next_cell(rows, 2) is None
    for condition in CONDITIONS:
        assert first["condition"] == condition.label
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


def test_top_down_split_arm_pins_the_requested_comparison() -> None:
    """One top-down arm whose planning outthinks its prover, at the agreed budget."""
    conditions = CONDITION_SETS["top-down-split"]
    assert len(conditions) == 1
    (condition,) = conditions
    assert (condition.order, condition.prover_effort, condition.orchestrator_effort) == (
        "top-down",
        "low",
        "xhigh",
    )

    rows = cells([{"id": "p"}, {"id": "q"}], conditions)
    assert len(rows) == 2  # one cell per problem, not one per model x order
    config = rows[0]["config"]
    assert config["search_order"] == "top-down"
    assert config["reasoning_effort"] == "low"
    assert config["orchestrator_reasoning_effort"] == "xhigh"
    assert config["model"] == config["orchestrator_model"] == "gpt-6-astra"
    assert (
        config["parallelism"],
        config["job_api_calls"],
        config["orchestrator_api_calls"],
        config["total_api_calls"],
        config["wall_time_s"],
    ) == (4, 200, 50, 2000, 28800)
    assert config["allow_internet"] is False


def test_luna_arm_replicates_the_split_at_the_tighter_budget() -> None:
    """The gpt-5.6-luna replication: same roles and order, fewer calls per pass."""
    conditions = CONDITION_SETS["luna-top-down-split"]
    assert len(conditions) == 1
    (condition,) = conditions
    assert condition.model == "gpt-5.6-luna"
    assert (condition.order, condition.prover_effort, condition.orchestrator_effort) == (
        "top-down",
        "medium",
        "xhigh",
    )

    rows = cells([{"id": "p"}, {"id": "q"}], conditions)
    assert [row["id"] for row in rows] == ["p-luna-top-split", "q-luna-top-split"]
    config = rows[0]["config"]
    assert config["model"] == config["orchestrator_model"] == "gpt-5.6-luna"
    assert config["reasoning_effort"] == "medium"
    assert config["orchestrator_reasoning_effort"] == "xhigh"
    assert all(row["effort"] == "xhigh" for row in rows)
    assert (
        config["parallelism"],
        config["job_api_calls"],
        config["orchestrator_api_calls"],
        config["total_api_calls"],
        config["wall_time_s"],
        config["timeout_s"],
    ) == (4, 200, 50, 2000, 28800, 1200)
    assert config["search_order"] == "top-down"
    assert config["mode"] == "research"
    assert config["allow_internet"] is False


def test_only_model_and_prover_effort_differ_between_the_two_split_arms() -> None:
    """The luna arm must be a model comparison, not a budget comparison."""
    (astra,) = cells([{"id": "p"}], CONDITION_SETS["top-down-split"])
    (luna,) = cells([{"id": "p"}], CONDITION_SETS["luna-top-down-split"])
    differing = {k for k, v in astra["config"].items() if luna["config"][k] != v}
    assert differing == {"model", "orchestrator_model", "reasoning_effort"}
    for field in WIDE._fields:
        assert astra["config"][field] == luna["config"][field] == getattr(WIDE, field)


def test_planning_and_proving_can_run_on_different_models() -> None:
    """The cross-model arm: astra plans, luna proves, one budget for both."""
    from leanflow_cli.workflows.prover.config import ProverConfig

    (condition,) = CONDITION_SETS["astra-plan-luna-prove"]
    assert (condition.model, condition.orchestrator_model) == ("gpt-5.6-luna", "gpt-6-astra")

    (cell,) = cells([{"id": "p"}], CONDITION_SETS["astra-plan-luna-prove"])
    assert cell["model"] == "gpt-5.6-luna"
    assert cell["orchestrator_model"] == "gpt-6-astra"
    assert cell["effort"] == "medium"
    config = cell["config"]
    assert config["model"] == "gpt-5.6-luna"
    assert config["orchestrator_model"] == "gpt-6-astra"
    assert config["reasoning_effort"] == config["orchestrator_reasoning_effort"] == "medium"

    # The split is only real if to_mapping routes each role to its own model;
    # session_transport prefers config["model"] over the launch env var, so a
    # role that resolved to the wrong model would silently run the wrong one.
    resolved = ProverConfig(
        **{k: (tuple(v) if isinstance(v, list) else v) for k, v in config.items()}
    )
    assert {role: resolved.to_mapping(role)["model"] for role in ("prover", "negation")} == {
        "prover": "gpt-5.6-luna",
        "negation": "gpt-5.6-luna",
    }
    assert {
        role: resolved.to_mapping(role)["model"] for role in ("orchestrator", "review", "research")
    } == {
        "orchestrator": "gpt-6-astra",
        "review": "gpt-6-astra",
        "research": "gpt-6-astra",
    }


def test_a_single_model_arm_keeps_both_roles_on_that_model() -> None:
    """An empty orchestrator_model must still mean 'same model', not empty."""
    for label in ("full", "top-down-split", "luna-top-down-split"):
        for condition in CONDITION_SETS[label]:
            assert condition.orchestrator_model == ""
    for label in ("top-down-split", "luna-top-down-split"):
        (cell,) = cells([{"id": "p"}], CONDITION_SETS[label])
        config = cell["config"]
        assert config["model"] == config["orchestrator_model"] == cell["model"]
        assert cell["orchestrator_model"] == cell["model"]


def test_the_launch_record_names_the_model_each_role_actually_uses() -> None:
    """A provenance record that assumes one model per cell misreports the split."""
    import scripts.lean_imo_campaign.worker as worker

    source = Path(worker.__file__).read_text()
    assert '"orchestrator_model": cell["model"],' not in source
    assert '"orchestrator_model": cell["config"].get("orchestrator_model")' in source

    (cell,) = cells([{"id": "p"}], CONDITION_SETS["astra-plan-luna-prove"])
    recorded = cell["config"].get("orchestrator_model") or cell["model"]
    assert recorded == "gpt-6-astra" != cell["model"]


def test_bottom_up_arm_changes_only_the_search_order() -> None:
    """Traversal is the single variable, so a difference cannot be blamed elsewhere."""
    (top,) = CONDITION_SETS["astra-plan-luna-prove"]
    (bottom,) = CONDITION_SETS["astra-plan-luna-prove-bottom"]
    differing = [f for f in top._fields if getattr(top, f) != getattr(bottom, f)]
    assert differing == ["label", "order"]
    assert (top.order, bottom.order) == ("top-down", "bottom-up")

    (cell,) = cells([{"id": "p"}], CONDITION_SETS["astra-plan-luna-prove-bottom"])
    config = cell["config"]
    assert config["search_order"] == "bottom-up"
    assert config["model"] == "gpt-5.6-luna"
    assert config["orchestrator_model"] == "gpt-6-astra"
    assert (config["total_api_calls"], config["job_api_calls"]) == (5000, 150)


def test_luna_high_astra_low_preserves_the_calibrated_top_down_budget() -> None:
    """Change role efforts without changing the earlier hybrid's proof limits."""
    from leanflow_cli.workflows.prover.config import ProverConfig

    (old,) = cells([{"id": "p"}], CONDITION_SETS["astra-plan-luna-prove"])
    (new,) = cells([{"id": "p"}], CONDITION_SETS["astra-low-luna-high-top"])
    differing = {key for key, value in old["config"].items() if new["config"][key] != value}
    assert differing == {"reasoning_effort", "orchestrator_reasoning_effort"}
    config = ProverConfig(**new["config"])
    for role in ("prover", "negation"):
        settings = config.to_mapping(role)
        assert (settings["model"], settings["reasoning_effort"]) == ("gpt-5.6-luna", "high")
    for role in ("orchestrator", "review", "research"):
        settings = config.to_mapping(role)
        assert (settings["model"], settings["reasoning_effort"]) == ("gpt-6-astra", "low")
    assert config.search_order == "top-down"
    assert (config.parallelism, config.job_api_calls, config.total_api_calls) == (4, 150, 5000)
    assert (config.orchestrator_api_calls, config.wall_time_s, config.timeout_s) == (
        50,
        28800,
        1200,
    )


def test_calibrated_budget_matches_what_the_top_down_arm_actually_ran() -> None:
    """The four proofs were produced at 5000/150; the code must say so."""
    from scripts.lean_imo_campaign.matrix import LUNA_CALIBRATED

    for label in ("astra-plan-luna-prove", "astra-plan-luna-prove-bottom"):
        (condition,) = CONDITION_SETS[label]
        assert condition.budget is LUNA_CALIBRATED
    assert LUNA_CALIBRATED.total_api_calls == 5000
    assert LUNA_CALIBRATED.job_api_calls == 150
    # Untouched arms keep the original limits they were frozen under.
    for label in ("full", "top-down-split", "luna-top-down-split"):
        for condition in CONDITION_SETS[label]:
            assert condition.budget is WIDE


def test_every_arm_shares_one_named_budget() -> None:
    """Limits live in named constants, not hardcoded inside configuration()."""
    from scripts.lean_imo_campaign.matrix import Budget

    for label, conditions in CONDITION_SETS.items():
        for condition in conditions:
            assert isinstance(condition.budget, Budget), label
    for condition in (
        *CONDITIONS,
        *CONDITION_SETS["top-down-split"],
        *CONDITION_SETS["luna-top-down-split"],
    ):
        assert condition.budget is WIDE
    config = cells([{"id": "p"}], CONDITION_SETS["top-down-split"])[0]["config"]
    assert (config["job_api_calls"], config["total_api_calls"]) == (200, 2000)


def test_default_condition_set_is_unchanged_by_the_split_arm() -> None:
    """Adding an arm must not perturb the original four-way comparison."""
    assert CONDITION_SETS["full"] is CONDITIONS
    assert [c.label for c in CONDITIONS] == [
        "astra-bottom",
        "astra-top",
        "terra-bottom",
        "terra-top",
    ]
    for condition in CONDITIONS:
        assert condition.prover_effort == "" and condition.orchestrator_effort == ""
    assert len(cells([{"id": "p"}])) == 4


def test_lane_count_is_configurable_and_defaults_to_two(monkeypatch) -> None:
    """Lanes are scheduling only; widening them must not touch cell config."""
    from scripts.lean_imo_campaign.runner import lanes

    monkeypatch.delenv("LEANFLOW_CAMPAIGN_LANES", raising=False)
    assert lanes() == (1, 2)
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_LANES", "3")
    assert lanes() == (1, 2, 3)
    for bad in ("0", "9", "-1"):
        monkeypatch.setenv("LEANFLOW_CAMPAIGN_LANES", bad)
        with pytest.raises(ValueError, match="between 1 and 8"):
            lanes()


def test_run_refuses_to_narrow_lanes_below_a_claimed_lane(tmp_path: Path, monkeypatch) -> None:
    """Narrowing LEANFLOW_CAMPAIGN_LANES must not silently strand claimed cells.

    A cell claimed by lane 3 keeps lane=3 for the campaign's life; a dispatcher
    restarted with only two lanes would never iterate lane 3 and the cell would
    wait forever. run() must refuse rather than strand it.
    """
    from scripts.lean_imo_campaign.runner import run

    (tmp_path / "campaign.json").write_text(
        json.dumps(
            {
                "cells": [
                    {"id": "c1", "lane": 1, "status": "pending"},
                    {"id": "c3", "lane": 3, "status": "pending"},
                ]
            }
        )
    )
    monkeypatch.setenv("LEANFLOW_CAMPAIGN_LANES", "2")
    with pytest.raises(ValueError, match=r"would strand cells already claimed by lane\(s\) \[3\]"):
        run(tmp_path)


def test_three_lanes_each_hold_one_problem_at_a_time() -> None:
    """A third lane claims its own problem without disturbing the other two."""
    rows = cells([{"id": f"problem-{i}"} for i in range(1, 5)])
    claimed = [next_cell(rows, lane) for lane in (1, 2, 3)]
    assert all(c is not None for c in claimed)
    assert [c["problem"]["id"] for c in claimed] == ["problem-1", "problem-2", "problem-3"]
    for c in claimed:
        c["status"] = "running"
    # Every lane is busy, so no lane may claim anything further.
    assert all(next_cell(rows, lane) is None for lane in (1, 2, 3))
    # Each problem is owned by exactly one lane.
    owners = {}
    for row in rows:
        if row["lane"] is not None:
            owners.setdefault(row["problem"]["id"], set()).add(row["lane"])
    assert all(len(v) == 1 for v in owners.values())
