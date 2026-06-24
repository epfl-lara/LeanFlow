"""RCP (EPFL inference cluster) helpers for the auxiliary router.

Pure, stdlib-only predicates that recognise an EPFL RCP inference base URL and
normalise a reasoning-effort string into the subset of levels the RCP endpoint
accepts. They hold no auxiliary routing state, so they form a closed cluster
that ``auxiliary_client`` re-exports unchanged (importers keep resolving
``auxiliary_client.<name>``).

This module must NOT import ``agent.auxiliary_client`` — that would create an
import cycle, since ``auxiliary_client`` re-exports these names.
"""


def _is_rcp_base_url(base_url: str) -> bool:
    normalized = str(base_url or "").lower()
    return "inference.rcp.epfl.ch" in normalized or "inference-rcp.epfl.ch" in normalized


def _map_rcp_reasoning_effort(effort: str) -> str:
    normalized = str(effort or "medium").strip().lower()
    if normalized in {"low", "medium", "high"}:
        return normalized
    if normalized == "minimal":
        return "low"
    if normalized in {"xhigh", "auto"}:
        return "high" if normalized == "xhigh" else "medium"
    return "medium"
