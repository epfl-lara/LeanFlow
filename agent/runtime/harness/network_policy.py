"""Resolve research restrictions without weakening an explicit offline boundary."""

from __future__ import annotations

import os
from collections.abc import Mapping


def restricted_research(
    env: Mapping[str, str] | None = None, *, allow_internet: bool | None = None
) -> bool:
    """Require local tools when any campaign setting prohibits external research.

    Native model connections remain available. The controller supplies only local
    research tools and denies network access to agent-command descendants.
    """
    source = os.environ if env is None else env
    return (
        any(
            source.get(name, "").strip().lower() in {"1", "true", "yes", "on"}
            for name in (
                "LEANFLOW_DISABLE_SOLUTION_RESEARCH",
                "LEANFLOW_DISABLE_REPOSITORY_RESEARCH",
            )
        )
        or allow_internet is False
        or source.get("LEANFLOW_PROVER_ALLOW_INTERNET", "").strip().lower()
        in {"0", "false", "no", "off"}
    )
