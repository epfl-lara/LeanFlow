"""Resolve the bounded prover configuration without legacy research profiles."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

ENV_NAMES = {
    "mode": "LEANFLOW_PROVER_MODE",
    "search_order": "LEANFLOW_PROVER_SEARCH_ORDER",
    "job_api_calls": "LEANFLOW_PROVER_JOB_API_CALLS",
    "max_restarts": "LEANFLOW_PROVER_MAX_RESTARTS",
    "plan_refinements": "LEANFLOW_PROVER_PLAN_REFINEMENTS",
    "parallelism": "LEANFLOW_PROVER_PARALLELISM",
    "total_api_calls": "LEANFLOW_PROVER_TOTAL_API_CALLS",
    "orchestrator_api_calls": "LEANFLOW_PROVER_ORCHESTRATOR_API_CALLS",
    "max_nodes": "LEANFLOW_PROVER_MAX_NODES",
    "max_decompositions": "LEANFLOW_PROVER_MAX_DECOMPOSITIONS",
    "wall_time_s": "LEANFLOW_PROVER_WALL_TIME_S",
    "timeout_s": "LEANFLOW_PROVER_TIMEOUT_S",
    "model": "LEANFLOW_PROVER_MODEL",
    "orchestrator_model": "LEANFLOW_PROVER_ORCHESTRATOR_MODEL",
    "context_tokens": "LEANFLOW_PROVER_CONTEXT_TOKENS",
    "orchestrator_context_tokens": "LEANFLOW_PROVER_ORCHESTRATOR_CONTEXT_TOKENS",
    "compression": "LEANFLOW_PROVER_COMPRESSION",
    "orchestrator_compression": "LEANFLOW_PROVER_ORCHESTRATOR_COMPRESSION",
    "fill_definitions": "LEANFLOW_PROVER_FILL_DEFINITIONS",
    "allow_internet": "LEANFLOW_PROVER_ALLOW_INTERNET",
    "allowed_axioms": "LEANFLOW_PROVER_ALLOWED_AXIOMS",
}


@dataclass(frozen=True)
class ProverConfig:
    """Keep all campaign limits explicit and independent of conversation resets."""

    mode: str = "standard"
    search_order: str = "bottom-up"
    job_api_calls: int = 300
    max_restarts: int = 3
    plan_refinements: int = 16
    parallelism: int = 2
    total_api_calls: int = 10000
    orchestrator_api_calls: int = 40
    max_nodes: int = 128
    max_decompositions: int = 32
    wall_time_s: int = 14400
    timeout_s: int = 180
    model: str = ""
    orchestrator_model: str = ""
    context_tokens: int = 64000
    orchestrator_context_tokens: int = 64000
    compression: bool = True
    orchestrator_compression: bool = True
    fill_definitions: bool = False
    allow_internet: bool = True
    allowed_axioms: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound")

    def __post_init__(self) -> None:
        if self.mode not in {"standard", "research"}:
            raise ValueError("prover mode must be standard or research")
        if self.search_order not in {"bottom-up", "top-down"}:
            raise ValueError("search order must be bottom-up or top-down")
        for key in (
            "job_api_calls",
            "parallelism",
            "total_api_calls",
            "orchestrator_api_calls",
            "max_nodes",
            "wall_time_s",
            "timeout_s",
            "context_tokens",
            "orchestrator_context_tokens",
        ):
            if getattr(self, key) < 1:
                raise ValueError(f"{key} must be positive")
        for key in ("max_restarts", "plan_refinements", "max_decompositions"):
            if getattr(self, key) < 0:
                raise ValueError(f"{key} must be non-negative")
        if "sorryAx" in self.allowed_axioms:
            raise ValueError("sorryAx cannot be an allowed completion axiom")

    def to_mapping(self, role: str = "prover") -> dict[str, Any]:
        """Select model and context settings for one role."""
        values = asdict(self)
        if role in {"orchestrator", "review", "research"}:
            values["model"] = self.orchestrator_model or self.model
            values["context_tokens"] = self.orchestrator_context_tokens
            values["compression"] = self.orchestrator_compression
        return values

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProverConfig:
        """Read only explicit prover settings and shared provider defaults."""
        source = os.environ if env is None else env
        values: dict[str, Any] = {}
        defaults = cls()
        for name, default in asdict(defaults).items():
            raw = source.get(ENV_NAMES[name], "").strip()
            if not raw:
                continue
            if isinstance(default, bool):
                if raw.lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
                    raise ValueError(f"{name} must be a boolean")
                values[name] = raw.lower() in {"1", "true", "yes", "on"}
            elif isinstance(default, int):
                values[name] = int(raw)
            elif name == "allowed_axioms":
                values[name] = tuple(part.strip() for part in raw.split(",") if part.strip())
            else:
                values[name] = raw
        values.setdefault("model", source.get("LEANFLOW_NATIVE_MODEL", ""))
        if "allowed_axioms" not in values and source.get("LEANFLOW_NATIVE_ALLOWED_AXIOMS"):
            values["allowed_axioms"] = tuple(
                part.strip()
                for part in source["LEANFLOW_NATIVE_ALLOWED_AXIOMS"].split(",")
                if part.strip()
            )
        return cls(**values)
