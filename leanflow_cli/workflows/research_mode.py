"""Resolve the complete relentless-research workflow profile.

``LEANFLOW_RESEARCH_MODE=1`` activates the existing plan, retrieval,
orchestration, dispatch, feasibility, reporting, and learning surfaces as one
coherent profile. Difficulty becomes a route or epoch transition, never a
terminal result. The 120-cycle limit is an epoch boundary, so research mode
does not multiply it into one oversized model context.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass

from core.provider_capacity import BACKGROUND_PROVIDER_CAPACITY_ENV

#: Stop reasons research mode refuses to treat as terminal.
ROUTABLE_STOPS = frozenset({"stalled", "blocked", "budget-breakpoint", "parked"})

_PROFILE_DEFAULTS = {
    "LEANFLOW_PLAN_STATE": "1",
    "LEANFLOW_PREMISE_RETRIEVAL": "1",
    "LEANFLOW_BUDGET_BREAKPOINT": "1",
    "LEANFLOW_ORCHESTRATOR_ENABLED": "1",
    "LEANFLOW_ORCHESTRATOR_LLM_ENABLED": "1",
    "LEANFLOW_ORCHESTRATOR_CADENCE_CYCLES": "4",
    "LEANFLOW_ORCHESTRATOR_MAX_ROUTES": "4",
    "LEANFLOW_FIDELITY_AUDIT": "1",
    "LEANFLOW_GRAPH_FRONTIER_SELECTION": "1",
    "LEANFLOW_PLANNER_ENABLED": "1",
    "LEANFLOW_DISPATCH_ENABLED": "1",
    "LEANFLOW_NEGATION_PROBE": "1",
    "LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK": "1",
    "LEANFLOW_FINAL_REPORT": "1",
    "LEANFLOW_LEARNINGS": "1",
    "LEANFLOW_CURRICULUM_ORDERING": "1",
    "LEANFLOW_MANAGER_LLM_MODE": "live",
    # A dispatch worker may retain a multi-gigabyte Lean/LSP session after a
    # tool response. The project admission policy reclaims background-worker
    # state while keeping the single foreground prover's incremental cache warm.
    "LEANFLOW_PROJECT_LEAN_ADMISSION": "1",
    # Keep foreground lean-lsp diagnostics and remote/native search, but avoid
    # eagerly retaining a separate multi-gigabyte local Loogle index during a
    # full research campaign. Memory-provisioned runs may explicitly opt in.
    "LEANFLOW_RESEARCH_LOCAL_LOOGLE": "0",
}

# An explicit CLI profile is an authoritative product contract, so inherited
# feature-disable switches cannot silently reduce it to a partial campaign.
# Operational tuning and documented debug controls (for example manager mode,
# cadence, and local Loogle opt-in) remain caller-configurable.
_PROFILE_REQUIRED_FEATURES = frozenset(
    key for key, value in _PROFILE_DEFAULTS.items() if value == "1"
)
_PROFILE_CAPACITY_KEYS = frozenset(
    {
        "LEANFLOW_RESEARCH_WORKERS",
        "LEANFLOW_DISPATCH_MAX_CONCURRENT",
        BACKGROUND_PROVIDER_CAPACITY_ENV,
    }
)


def research_mode_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_RESEARCH_MODE", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def research_local_loogle_enabled(*, research: bool | None = None) -> bool:
    """Return whether this profile may start the private local Loogle index.

    Non-research workflows retain the established local-Loogle default. A
    research workflow requires ``LEANFLOW_RESEARCH_LOCAL_LOOGLE=1`` so the
    foreground Lean language server remains available without its additional
    resident index. ``research`` lets CLI resolution apply the policy before
    the child process receives ``LEANFLOW_RESEARCH_MODE=1``.
    """
    active = research_mode_enabled() if research is None else bool(research)
    if not active:
        return True
    raw = str(os.getenv("LEANFLOW_RESEARCH_LOCAL_LOOGLE", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Multipliers:
    """Independent job budget raises for research runs."""

    max_cycles: int = 1
    prover_job_turns: int = 2
    planner_lane_iterations: int = 2


def research_budget_multipliers() -> Multipliers:
    return Multipliers()


def research_profile_env(workers: int = 2) -> dict[str, str]:
    """Return environment defaults that activate the complete profile."""
    values = dict(_PROFILE_DEFAULTS)
    values["LEANFLOW_DISPATCH_MAX_CONCURRENT"] = str(max(1, int(workers)))
    values["LEANFLOW_RESEARCH_WORKERS"] = str(max(0, int(workers)))
    # Dispatch workers intentionally disable nested research mode and set
    # LEANFLOW_RESEARCH_WORKERS=0 inside their assignment-local environment.
    # Preserve the parent campaign's live-actor cap under a stable key
    # so planner delegates and process workers still share the same gate.
    values[BACKGROUND_PROVIDER_CAPACITY_ENV] = str(max(0, int(workers)))
    return values


def apply_research_profile_env(
    env: MutableMapping[str, str],
    *,
    workers: int = 2,
    explicit_cli: bool = False,
) -> None:
    """Apply the research profile while preserving its activation contract.

    Explicit ``--research`` and ``--research-workers`` requests force every
    required feature on, even when a parent shell or project environment
    inherited a stale ``0``. Environment-only activation keeps deliberate
    per-feature overrides, while missing values still receive the complete
    defaults. Worker capacity is always normalized from the selected research
    worker count so the process portfolio has one authority.
    """
    for key, value in research_profile_env(workers).items():
        if key in _PROFILE_CAPACITY_KEYS or (explicit_cli and key in _PROFILE_REQUIRED_FEATURES):
            env[key] = value
        else:
            env.setdefault(key, value)


def research_worker_count(default: int = 2) -> int:
    """Return the configured background-worker capacity."""
    try:
        return max(0, int(os.getenv("LEANFLOW_RESEARCH_WORKERS", str(default)) or default))
    except ValueError:
        return default


def planner_lane_parallelism(max_lanes: int) -> int:
    """Return the planner's live delegate cap under the research profile.

    The provider-request gate is the cross-process authority.  This local cap
    also avoids constructing more simultaneous in-process planner agents than
    the configured background parallelism. ``--no-parallel`` retains one
    synchronous lane at a time.
    """
    bounded_max = max(1, int(max_lanes))
    if not research_mode_enabled():
        return bounded_max
    return min(bounded_max, max(1, research_worker_count()))


def scaled_max_cycles(base: int) -> int:
    """Return the per-epoch cycle budget; research uses fresh epochs."""
    return base


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

    The ``orchestrator_on`` argument remains for call compatibility and
    observability, but router availability is not surrender authority. A
    disabled or failed consult must not turn mathematical difficulty into an
    exit.
    """
    return research_mode_enabled() and str(stop_reason or "") in ROUTABLE_STOPS


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
