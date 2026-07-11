"""Research-mode semantics profile (Phase 6 §6.10, roadmap §4.7).

``LEANFLOW_RESEARCH_MODE=1`` turns difficulty from a terminal state into a
routing signal: ``stalled``/``blocked`` stop being terminal (they already
fire the orchestrator's stall consult; when even that does not resume,
the stop is SUPPRESSED and the loop continues), budgets are raised (never
removed — the hard cycle ceiling stays the backstop, multiplied), and the
budget-pressure message asks for a checkpointed decision packet plus a
route request instead of "respond now".

Composition guarantees:
- Suppression requires the orchestrator to be ON — research mode without
  the router would spin blindly, so stop semantics are untouched then.
- The N1 closed set survives: terminal outcomes remain {verified,
  kernel-false, interrupt, park-with-packet, raised hard ceiling}; the
  orchestrator's max-routes park is the pressure valve.
- Everything here is dark: flag off means every helper returns the
  identity/False and no call site changes behavior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Stop reasons research mode refuses to treat as terminal (routable stops).
ROUTABLE_STOPS = frozenset({"stalled", "blocked"})


def research_mode_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_RESEARCH_MODE", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Multipliers:
    """Budget raises for research runs (raised, never removed)."""

    max_cycles: int = 4
    prover_job_turns: int = 2
    planner_lane_iterations: int = 2


def research_budget_multipliers() -> Multipliers:
    return Multipliers()


def scaled_max_cycles(base: int) -> int:
    """The hard cycle ceiling — multiplied in research mode, still finite."""
    if not research_mode_enabled():
        return base
    return base * research_budget_multipliers().max_cycles


def scaled_prover_job_turns(base: int) -> int:
    if not research_mode_enabled():
        return base
    return base * research_budget_multipliers().prover_job_turns


def scaled_lane_iterations(base: int) -> int:
    if not research_mode_enabled():
        return base
    return base * research_budget_multipliers().planner_lane_iterations


def suppress_terminal_stop(stop_reason: str, *, orchestrator_on: bool) -> bool:
    """True when a would-be-terminal stop must instead continue the loop.

    Only for the routable stops, only in research mode, and ONLY when the
    orchestrator is on (the stall consult has already had its chance; a
    noop/failed consult must not end the scope, but a router-less run
    keeps classic stop semantics). budget-breakpoint stays terminal-or-
    routed exactly as Phase 4 wired it; the hard ceiling stays terminal.
    """
    return research_mode_enabled() and orchestrator_on and str(stop_reason or "") in ROUTABLE_STOPS


def suppressed_stop_nudge(stop_reason: str) -> str:
    """The user-turn nudge injected when a stop is suppressed — the stop
    becomes work: checkpoint the evidence and request a route."""
    return "\n".join(
        [
            "[LEANFLOW RESEARCH MODE]",
            f"- a '{stop_reason}' stop was suppressed: difficulty is a routing "
            "signal here, never a terminal state.",
            "- checkpoint what you learned into the decision packet, then either "
            "make a concrete edit or report a blocker WITH a requested route "
            "(`decompose` | `negate` | `plan`) and the evidence for it.",
        ]
    )


def research_budget_message(kind: str) -> str:
    """Budget-pressure text for research runs (message only; math unchanged).

    ``kind`` is 'warning' (>=90%) or 'caution' (>=70%).
    """
    if kind == "warning":
        return (
            "Checkpoint your findings into the decision packet NOW, then "
            "escalate a route request (`decompose` | `negate` | `plan`) — "
            "do not start new exploration on this budget."
        )
    return (
        "Budget pressure: consolidate findings into the decision packet and "
        "prefer route-able progress (state helpers, request probes) over "
        "open-ended exploration."
    )
