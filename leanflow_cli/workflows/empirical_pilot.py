"""Bound process-isolated empirical planner probes before foreground handoff."""

from __future__ import annotations

import threading
from typing import Any

PILOT_CASE_LIMIT = 12
PILOT_COMPUTE_CALL_LIMIT = 2
PILOT_COMPUTE_TIMEOUT_S = 8


def prompt_contract() -> str:
    """Return the empirical lane's deterministic pilot-budget contract."""
    return (
        "Pilot budget (mandatory): test at most "
        f"{PILOT_CASE_LIMIT} deliberately chosen small cases, make at most "
        f"{PILOT_COMPUTE_CALL_LIMIT} empirical_compute calls, and set each compute timeout "
        f"to at most {PILOT_COMPUTE_TIMEOUT_S} seconds. `empirical_compute` is the only "
        "numerical execution surface in this lane and has no filesystem or project-mutation "
        "authority. Start with one or two cases when the per-case "
        "cost is uncertain. Never exhaustively enumerate a large residue range, trial-divide "
        "a squared denominator, or enumerate all divisors of a growing integer. Stop after the "
        "first useful counterexample or stable pattern. Before returning `supports` for a "
        "universal hypothesis involving integrality or divisibility, test a complete compatible "
        "residue basis for every small modulus introduced by the proposed construction. Do not "
        "infer a divisibility side condition from one or two favorable examples. If that residue "
        "basis would exceed the case cap and no symbolic check proves the side condition, return "
        "`inconclusive`. If the pilot reaches a cap, return "
        "`inconclusive` with the exact tested cases, observed pattern, and a scalable next probe; "
        "do not extend the search inside this planner turn."
    )


class BoundedEmpiricalPilot:
    """Clamp and count isolated compute calls made by one empirical planner child."""

    def __init__(
        self,
        *,
        timeout_s: int = PILOT_COMPUTE_TIMEOUT_S,
        max_calls: int = PILOT_COMPUTE_CALL_LIMIT,
    ) -> None:
        self.timeout_s = max(1, int(timeout_s))
        self.max_calls = max(1, int(max_calls))
        self._calls = 0
        self._lock = threading.Lock()

    def __call__(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Clamp an empirical compute call or reject it after the pilot cap."""
        if str(tool_name or "") != "empirical_compute":
            return None
        with self._lock:
            if self._calls >= self.max_calls:
                return {
                    "error": (
                        "BLOCKED: empirical pilot compute-call budget exhausted. "
                        "Return an inconclusive structured deliverable with the cases and "
                        "evidence already collected; do not continue exhaustive search."
                    ),
                    "status": "empirical_pilot_limit",
                    "compute_calls": self._calls,
                    "max_compute_calls": self.max_calls,
                }
            self._calls += 1

        requested = args.get("timeout_s")
        try:
            requested_timeout = int(requested) if requested is not None else self.timeout_s
        except (TypeError, ValueError):
            requested_timeout = self.timeout_s
        args["timeout_s"] = max(1, min(requested_timeout, self.timeout_s))
        return None
