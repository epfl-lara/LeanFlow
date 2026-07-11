"""Phase 6 §6.10 tests: the research-mode semantics profile.

The invariants: flag off is byte-identical everywhere; suppression only
fires for routable stops WITH the orchestrator on; budgets are raised,
never removed; the N1 closed set survives (ceiling and park stay
terminal).
"""

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


def test_multipliers_raise_but_keep_finite(research_on):
    assert research_mode.scaled_max_cycles(120) == 480
    assert research_mode.scaled_prover_job_turns(40) == 80
    assert research_mode.scaled_lane_iterations(24) == 48


def test_runner_ceiling_scales(research_on, monkeypatch):
    monkeypatch.delenv("LEANFLOW_NATIVE_AUTONOMOUS_MAX_CYCLES", raising=False)
    assert runner._autonomous_max_cycles() == 480
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
        ("stalled", True, False, False),  # router-less runs keep classic stops
        ("blocked", False, True, False),  # flag off is byte-identical
        ("budget-breakpoint", True, True, False),  # Phase 4 owns this one
        ("failed", True, True, False),
        ("verified", True, True, False),
        ("parked", True, True, False),  # park stays terminal (N1 valve)
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
