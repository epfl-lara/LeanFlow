"""Explain unsupported native network restrictions before allocating workflow work."""

from __future__ import annotations

import os
from collections.abc import Mapping


def network_policy_error(
    env: Mapping[str, str] | None = None, *, allow_internet: bool | None = None
) -> str:
    """Return an actionable refusal without weakening an explicit research boundary."""
    source = os.environ if env is None else env
    restrictions = [
        name
        for name in (
            "LEANFLOW_DISABLE_SOLUTION_RESEARCH",
            "LEANFLOW_DISABLE_REPOSITORY_RESEARCH",
        )
        if source.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
    ]
    offline = allow_internet is False or source.get(
        "LEANFLOW_PROVER_ALLOW_INTERNET", ""
    ).strip().lower() in {"0", "false", "no", "off"}
    if offline:
        restrictions.append("LEANFLOW_PROVER_ALLOW_INTERNET")
    if not restrictions:
        return ""
    choices = []
    if "LEANFLOW_DISABLE_SOLUTION_RESEARCH" in restrictions:
        choices.append("turn off Clean room in Workflow options (CLI: omit --clean-room)")
    if "LEANFLOW_DISABLE_REPOSITORY_RESEARCH" in restrictions:
        choices.append("clear LEANFLOW_DISABLE_REPOSITORY_RESEARCH")
    if offline:
        choices.append("enable Allow internet in Prover setup")
    return (
        "Codex, Claude, and Lemex cannot enforce clean-room or offline research yet: "
        "model-only network isolation is unavailable. "
        f"Active restrictions: {', '.join(restrictions)}. "
        "For an online run, " + "; ".join(choices) + ". "
        "Otherwise keep these restrictions; no agent was started."
    )
