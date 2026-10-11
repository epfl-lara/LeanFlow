"""Bind local-only research restrictions to campaign configuration and saved provenance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from agent.runtime.harness.network_policy import restricted_research
from leanflow_cli.workflows.prover.config import ProverConfig

RESTRICTED_POLICY = "restricted-v1"
ONLINE_POLICY = "online-v1"


def restrict_config(config: ProverConfig) -> ProverConfig:
    """Persist explicit environment restrictions as local-only tool configuration."""
    return (
        replace(config, allow_internet=False)
        if restricted_research(allow_internet=config.allow_internet)
        else config
    )


def restore_research_config(requested: ProverConfig, state: Mapping[str, Any]) -> ProverConfig:
    """Reject contaminated history before restoring its graph or accepting saved candidates."""
    saved = dict(state["config"])
    saved["allowed_axioms"] = tuple(saved["allowed_axioms"])
    # Older native runs predate role limits. New defaults must never tighten a resume.
    for name in ("prover_sessions", "orchestrator_sessions", "negation_sessions"):
        saved.setdefault(name, None)
    config = restrict_config(ProverConfig(**saved))
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
