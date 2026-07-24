"""Tests for the complete relentless-research semantics profile."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import research_mode


@pytest.fixture()
def research_on(monkeypatch):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")


# ---------------------------------------------------------------------------
# Flag + multipliers
# ---------------------------------------------------------------------------


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)
    assert research_mode.research_mode_enabled() is False
    assert research_mode.scaled_max_cycles(120) == 120
    assert research_mode.scaled_prover_job_turns(40) == 40
    assert research_mode.scaled_lane_iterations(24) == 24


def test_job_multipliers_raise_but_epoch_cycles_stay_bounded(research_on):
    assert research_mode.scaled_max_cycles(120) == 120
    assert research_mode.scaled_prover_job_turns(40) == 80
    assert research_mode.scaled_lane_iterations(24) == 48


def test_runner_ceiling_scales(research_on, monkeypatch):
    monkeypatch.delenv("LEANFLOW_NATIVE_AUTONOMOUS_MAX_CYCLES", raising=False)
    assert runner._autonomous_max_cycles() == 120
    monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)
    assert runner._autonomous_max_cycles() == 120


# ---------------------------------------------------------------------------
# Stop suppression: the effective_stop_reason matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "research", "orchestrator", "suppressed"),
    [
        ("stalled", True, True, True),
        ("blocked", True, True, True),
        ("stalled", True, False, True),  # router failure is not surrender authority
        ("blocked", False, True, False),  # flag off is byte-identical
        ("budget-breakpoint", True, True, True),
        ("failed", True, True, False),
        ("verified", True, True, False),
        ("parked", True, True, True),
    ],
)
def test_suppression_matrix(monkeypatch, reason, research, orchestrator, suppressed):
    if research:
        monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    else:
        monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)

    assert research_mode.suppress_terminal_stop(reason, orchestrator_on=orchestrator) is suppressed


def test_suppressed_stop_nudge_requests_a_route():
    nudge = research_mode.suppressed_stop_nudge("stalled")
    assert nudge.startswith("[LEANFLOW RESEARCH MODE]")
    assert "'stalled'" in nudge
    assert "requested route" in nudge
    for route_word in ("decompose", "negate", "plan"):
        assert route_word in nudge


def test_profile_env_enables_all_research_surfaces():
    env = research_mode.research_profile_env(2)
    assert env["LEANFLOW_PLAN_STATE"] == "1"
    assert env["LEANFLOW_ORCHESTRATOR_ENABLED"] == "1"
    assert env["LEANFLOW_ORCHESTRATOR_LLM_ENABLED"] == "1"
    assert env["LEANFLOW_DISPATCH_ENABLED"] == "1"
    assert env["LEANFLOW_NEGATION_PROBE"] == "1"
    assert env["LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK"] == "1"
    assert env["LEANFLOW_MANAGER_LLM_MODE"] == "live"
    assert env["LEANFLOW_ORCHESTRATOR_CADENCE_CYCLES"] == "4"
    assert env["LEANFLOW_DISPATCH_MAX_CONCURRENT"] == "2"
    assert env["LEANFLOW_BACKGROUND_PROVIDER_CAPACITY"] == "2"
    assert env["LEANFLOW_PROJECT_LEAN_ADMISSION"] == "1"
    assert env["LEANFLOW_RESEARCH_LOCAL_LOOGLE"] == "0"


def test_explicit_profile_forces_features_but_keeps_debug_overrides():
    env = {
        "LEANFLOW_ORCHESTRATOR_ENABLED": "0",
        "LEANFLOW_DISPATCH_ENABLED": "false",
        "LEANFLOW_MANAGER_LLM_MODE": "off",
        "LEANFLOW_RESEARCH_LOCAL_LOOGLE": "1",
    }

    research_mode.apply_research_profile_env(env, workers=3, explicit_cli=True)

    assert env["LEANFLOW_ORCHESTRATOR_ENABLED"] == "1"
    assert env["LEANFLOW_DISPATCH_ENABLED"] == "1"
    assert env["LEANFLOW_MANAGER_LLM_MODE"] == "off"
    assert env["LEANFLOW_RESEARCH_LOCAL_LOOGLE"] == "1"
    assert env["LEANFLOW_RESEARCH_WORKERS"] == "3"
    assert env["LEANFLOW_DISPATCH_MAX_CONCURRENT"] == "3"
    assert env["LEANFLOW_BACKGROUND_PROVIDER_CAPACITY"] == "3"


def test_environment_profile_respects_feature_overrides_without_surrender(monkeypatch):
    env = {
        "LEANFLOW_ORCHESTRATOR_ENABLED": "0",
        "LEANFLOW_DISPATCH_ENABLED": "0",
    }

    research_mode.apply_research_profile_env(env, workers=1, explicit_cli=False)

    assert env["LEANFLOW_ORCHESTRATOR_ENABLED"] == "0"
    assert env["LEANFLOW_DISPATCH_ENABLED"] == "0"
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    assert research_mode.suppress_terminal_stop("blocked", orchestrator_on=False)


def test_research_local_loogle_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.delenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", raising=False)
    assert research_mode.research_local_loogle_enabled() is False

    monkeypatch.setenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", "1")
    assert research_mode.research_local_loogle_enabled() is True

    monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)
    monkeypatch.delenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", raising=False)
    assert research_mode.research_local_loogle_enabled() is True


def test_planner_parallelism_shares_research_worker_capacity(research_on, monkeypatch):
    monkeypatch.setenv("LEANFLOW_RESEARCH_WORKERS", "2")
    assert research_mode.planner_lane_parallelism(3) == 2
    monkeypatch.setenv("LEANFLOW_RESEARCH_WORKERS", "0")
    assert research_mode.planner_lane_parallelism(3) == 1


# ---------------------------------------------------------------------------
# Budget-pressure message (run_agent branch): text only, math unchanged
# ---------------------------------------------------------------------------


def _agent_stub() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        _budget_pressure_enabled=True,
        max_iterations=10,
        _budget_warning_threshold=0.9,
        _budget_caution_threshold=0.7,
    )


def test_budget_warning_research_branch(monkeypatch, research_on):
    from run_agent import AIAgent

    stub = _agent_stub()
    warning = AIAgent._get_budget_warning(stub, 9)
    assert "decision packet" in warning and "route request" in warning
    assert "Iteration 9/10" in warning  # the math is untouched
    caution = AIAgent._get_budget_warning(stub, 7)
    assert "decision packet" in caution

    monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)
    classic = AIAgent._get_budget_warning(stub, 9)
    assert "Provide your final response NOW" in classic
    assert AIAgent._get_budget_warning(stub, 1) is None


# ---------------------------------------------------------------------------
# Loop composition: a suppressed stop continues the cycle with a nudge
# ---------------------------------------------------------------------------


def test_suppression_resets_counters_and_nudges_history(research_on, monkeypatch):
    """Unit-level composition check against the same primitives the loop
    uses: suppression true => counters reset + nudge appended (the loop
    then continues instead of terminating)."""
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_ENABLED", "1")
    autonomy_state: dict[str, Any] = {
        "continuation_stable_cycles": 4,
        "continuation_blocked_runs": 3,
    }
    history: list[dict[str, Any]] = []

    stop_reason = "stalled"
    from leanflow_cli.workflows import orchestrator as orchestrator_floor

    if research_mode.suppress_terminal_stop(
        stop_reason, orchestrator_on=orchestrator_floor.orchestrator_enabled()
    ):
        autonomy_state["continuation_stable_cycles"] = 0
        autonomy_state["continuation_blocked_runs"] = 0
        history.append(
            {"role": "user", "content": research_mode.suppressed_stop_nudge(stop_reason)}
        )

    assert autonomy_state["continuation_stable_cycles"] == 0
    assert autonomy_state["continuation_blocked_runs"] == 0
    assert history and "[LEANFLOW RESEARCH MODE]" in history[0]["content"]
