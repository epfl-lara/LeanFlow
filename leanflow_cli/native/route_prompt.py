"""Render foreground obligations for active orchestrator strategy routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_ACTIVE_STRATEGY_ROUTES = frozenset({"decompose", "negate", "plan", "refresh-portfolio"})


def active_route_obligation_block(route_decision: Mapping[str, Any] | None) -> str:
    """Require route-specific evidence before an older proof shape resumes."""
    decision = dict(route_decision or {})
    route = str(decision.get("route_action", "") or "").strip().lower()
    if route not in _ACTIVE_STRATEGY_ROUTES:
        return ""

    lines = [
        "[LEANFLOW ACTIVE ROUTE OBLIGATION]",
        f"- assigned strategy: `{route}`",
        "- execute this strategy before returning to a previously exhausted proof shape",
        "- only a complete kernel-verified proof of the assigned target may supersede this route",
    ]
    if route == "negate":
        lines.extend(
            [
                "- foreground objective: perform a counterexample or consistency audit of the exact target and its premises",
                "- do not resume the prior constructive proof, helper inventory, or transport attempt until the negation audit records concrete evidence or the manager assigns another route",
                "- when a mechanical negation check is deferred, continue with a fresh exact check, a bounded small-case witness search, or a checked negation helper",
            ]
        )
    elif route == "decompose":
        lines.extend(
            [
                "- foreground objective: produce a checked helper decomposition or a concrete first split",
                "- do not replay the unchanged monolithic attempt before recording structural evidence",
            ]
        )
    elif route == "plan":
        lines.extend(
            [
                "- foreground objective: turn retained evidence into a concrete, ordered proof plan with a first checkable action",
                "- do not repeat the prior tactic family without new plan evidence",
            ]
        )
    else:
        lines.extend(
            [
                "- foreground objective: retire exhausted hypotheses, refresh evidence, and select a materially new proof shape",
                "- do not restart an exhausted route merely because a fresh model context was opened",
            ]
        )
    return "\n".join(lines)
