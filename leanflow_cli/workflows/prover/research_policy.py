"""Bind local-only research restrictions to campaign configuration and saved provenance."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from agent.runtime.harness.network_policy import restricted_research
from leanflow_cli.workflows.prover.config import ENV_NAMES, ProverConfig

RESTRICTED_POLICY = "restricted-v1"
ONLINE_POLICY = "online-v1"


def restrict_config(config: ProverConfig) -> ProverConfig:
    """Persist explicit environment restrictions as local-only tool configuration."""
    return (
        replace(config, allow_internet=False)
        if restricted_research(allow_internet=config.allow_internet)
        else config
    )


def restore_research_config(
    requested: ProverConfig, state: Mapping[str, Any], *, env: Mapping[str, str] | None = None
) -> ProverConfig:
    """Reject contaminated history before restoring its graph or accepting saved candidates."""
    saved = dict(state["config"])
    saved["allowed_axioms"] = tuple(saved["allowed_axioms"])
    # Older native runs predate role limits. New defaults must never tighten a resume.
    for name in ("prover_sessions", "orchestrator_sessions", "negation_sessions"):
        saved.setdefault(name, None)
    config = restrict_config(ProverConfig(**saved))
    # Only explicitly supplied limit controls can change a saved allowance.
    # Native settings, source policy, spending and active time remain durable.
    source = os.environ if env is None else env
    limits = {
        name: getattr(requested, name)
        for name in (
            "agent_calls",
            "tokens",
            "wall_time_s",
            "prover_sessions",
            "orchestrator_sessions",
            "negation_sessions",
        )
        if ENV_NAMES[name] in source
    }
    if "agent_calls" in limits:
        limits["total_api_calls"] = limits["agent_calls"]
    config = replace(config, **limits)
    restricted = (
        not requested.allow_internet
        or not config.allow_internet
        or state.get("research_policy") == RESTRICTED_POLICY
    )
    if restricted and state.get("research_policy") != RESTRICTED_POLICY:
        raise ValueError(
            "This run was created under a different or unknown research policy. "
            "Start a new run to use clean room or offline mode; previous plans, "
            "research, and proof candidates cannot be reused for a clean-room test."
        )
    return replace(config, allow_internet=False) if restricted else config
