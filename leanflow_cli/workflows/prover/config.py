"""Resolve native backends, Lean policy, and campaign, role, and recovery limits."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

ENV_NAMES = {
    "mode": "LEANFLOW_PROVER_MODE",
    "search_order": "LEANFLOW_PROVER_SEARCH_ORDER",
    "parallelism": "LEANFLOW_PROVER_PARALLELISM",
    "agent_calls": "LEANFLOW_PROVER_AGENT_CALLS",
    "prover_sessions": "LEANFLOW_PROVER_PROVER_SESSIONS",
    "orchestrator_sessions": "LEANFLOW_PROVER_ORCHESTRATOR_SESSIONS",
    "negation_sessions": "LEANFLOW_PROVER_NEGATION_SESSIONS",
    "tokens": "LEANFLOW_PROVER_TOKENS",
    "max_nodes": "LEANFLOW_PROVER_MAX_NODES",
    "max_restarts": "LEANFLOW_PROVER_MAX_RESTARTS",
    "plan_refinements": "LEANFLOW_PROVER_PLAN_REFINEMENTS",
    "max_decompositions": "LEANFLOW_PROVER_MAX_DECOMPOSITIONS",
    "wall_time_s": "LEANFLOW_PROVER_WALL_TIME_S",
    "model": "LEANFLOW_PROVER_MODEL",
    "orchestrator_model": "LEANFLOW_PROVER_ORCHESTRATOR_MODEL",
    "provider": "LEANFLOW_PROVER_PROVIDER",
    "orchestrator_provider": "LEANFLOW_PROVER_ORCHESTRATOR_PROVIDER",
    "reasoning_effort": "LEANFLOW_PROVER_REASONING_EFFORT",
    "orchestrator_reasoning_effort": "LEANFLOW_PROVER_ORCHESTRATOR_REASONING_EFFORT",
    "fill_definitions": "LEANFLOW_PROVER_FILL_DEFINITIONS",
    "allow_internet": "LEANFLOW_PROVER_ALLOW_INTERNET",
    "allowed_axioms": "LEANFLOW_PROVER_ALLOWED_AXIOMS",
}
_RETIRED_SETTINGS = (
    "BASE_URL",
    "API_KEY_ENV",
    "TIMEOUT_S",
    "CONTEXT_TOKENS",
    "COMPRESSION",
    "COMPRESSION_THRESHOLD",
    "COMPRESSION_TARGET",
    "MODEL_CONTEXTS",
    "JOB_API_CALLS",
    "TOTAL_API_CALLS",
    "ORCHESTRATOR_API_CALLS",
    "NEGATION_API_CALLS",
)


@dataclass(frozen=True)
class ProverConfig:
    """Keep native settings explicit and global resource limits unlimited when unset."""

    mode: str = "research"
    search_order: str = "bottom-up"
    parallelism: int = 2
    agent_calls: int | None = None
    prover_sessions: int | None = 15
    orchestrator_sessions: int | None = 5
    negation_sessions: int | None = None
    tokens: int | None = None
    wall_time_s: float | None = None
    max_nodes: int = 128
    max_restarts: int | None = None
    plan_refinements: int | None = None
    max_decompositions: int | None = None
    model: str = ""
    orchestrator_model: str = ""
    provider: str = ""
    orchestrator_provider: str = ""
    reasoning_effort: str = ""
    orchestrator_reasoning_effort: str = ""
    fill_definitions: bool = False
    allow_internet: bool = True
    allowed_axioms: tuple[str, ...] = ("propext", "Classical.choice", "Quot.sound")
    # Internal controller compatibility; these are not public launch settings.
    # Native invocations always reserve one call, regardless of these old role values.
    total_api_calls: int | None = None
    job_api_calls: int = 1
    orchestrator_api_calls: int = 1
    negation_api_calls: int = 1

    def __post_init__(self) -> None:
        """Reject ambiguous limits and unsupported backend names before allocating work."""
        if (
            self.agent_calls is not None
            and self.total_api_calls is not None
            and self.agent_calls != self.total_api_calls
        ):
            raise ValueError("agent_calls and total_api_calls disagree")
        calls = self.agent_calls if self.agent_calls is not None else self.total_api_calls
        object.__setattr__(self, "agent_calls", calls)
        object.__setattr__(self, "total_api_calls", calls)
        if self.mode not in {"standard", "research"}:
            raise ValueError("prover mode must be standard or research")
        if self.search_order not in {"bottom-up", "top-down"}:
            raise ValueError("search order must be bottom-up or top-down")
        for key in ("parallelism", "max_nodes"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{key} must be a positive integer")
        for key in (
            "agent_calls",
            "prover_sessions",
            "orchestrator_sessions",
            "negation_sessions",
            "tokens",
            "wall_time_s",
            "max_restarts",
            "plan_refinements",
            "max_decompositions",
        ):
            value = getattr(self, key)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{key} must be a finite non-negative number or unset")
            if key != "wall_time_s" and value is not None and not isinstance(value, int):
                raise ValueError(f"{key} must be an integer or unset")
        for key in ("provider", "orchestrator_provider"):
            if getattr(self, key) not in {"", "codex", "claude", "lemex"}:
                raise ValueError(
                    f"{key} must select codex, claude, or lemex; configure credentials and endpoints in that runtime"
                )
        if "sorryAx" in self.allowed_axioms:
            raise ValueError("sorryAx cannot be an allowed completion axiom")

    @property
    def timeout_s(self) -> None:
        """Keep old deterministic helper call sites unlimited until given remaining campaign time."""
        return None

    def to_mapping(self, role: str | None = None) -> dict[str, Any]:
        """Serialize public settings and explicit controller recovery constraints."""
        values = asdict(self)
        for key in (
            "total_api_calls",
            "job_api_calls",
            "orchestrator_api_calls",
            "negation_api_calls",
        ):
            values.pop(key)
        for key in ("max_restarts", "plan_refinements", "max_decompositions"):
            if values[key] is None:
                values.pop(key)
        if role in {"orchestrator", "review", "research"}:
            prover_provider = self.provider or os.getenv("LEANFLOW_NATIVE_PROVIDER") or "codex"
            same_backend = (self.orchestrator_provider or prover_provider) == prover_provider
            values["model"] = self.orchestrator_model or (self.model if same_backend else "")
            values["reasoning_effort"] = self.orchestrator_reasoning_effort or (
                self.reasoning_effort
                if same_backend
                and (not self.orchestrator_model or self.orchestrator_model == self.model)
                else ""
            )
            values["provider"] = self.orchestrator_provider or self.provider
        return values

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProverConfig:
        """Read explicit LeanFlow launch choices while leaving native environment untouched."""
        source = os.environ if env is None else env
        for suffix in _RETIRED_SETTINGS:
            for prefix in ("LEANFLOW_PROVER_", "LEANFLOW_PROVER_ORCHESTRATOR_"):
                if source.get(prefix + suffix, "").strip():
                    raise ValueError(
                        f"{prefix + suffix} is retired. Use native runtime configuration, role session limits, or global agent_calls, tokens, and wall_time_s limits."
                    )
        values: dict[str, Any] = {"mode": resolve_prover_mode(source)}
        defaults = asdict(cls())
        for name, env_name in ENV_NAMES.items():
            raw = source.get(env_name, "").strip()
            if not raw:
                if env_name in source and name in {"prover_sessions", "orchestrator_sessions"}:
                    # Clearing a visible limit means Unlimited, not the new-run default.
                    values[name] = None
                continue
            default = defaults[name]
            if default is None:
                values[name] = float(raw) if name == "wall_time_s" else int(raw)
            elif isinstance(default, bool):
                if raw.lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
                    raise ValueError(f"{name} must be a boolean")
                values[name] = raw.lower() in {"1", "true", "yes", "on"}
            elif isinstance(default, int):
                values[name] = int(raw)
            elif name == "allowed_axioms":
                values[name] = tuple(part.strip() for part in raw.split(",") if part.strip())
            else:
                values[name] = raw
        launch_provider = source.get("LEANFLOW_NATIVE_PROVIDER", "").strip() or "codex"
        values.setdefault("provider", launch_provider)
        # Native model names and reasoning levels are scoped to their own backend.
        if values["provider"] == launch_provider:
            launch_model = source.get("LEANFLOW_NATIVE_MODEL", "").strip()
            values.setdefault("model", launch_model)
            if values["model"] == launch_model:
                values.setdefault(
                    "reasoning_effort", source.get("LEANFLOW_NATIVE_REASONING_EFFORT", "").strip()
                )
        if "allowed_axioms" not in values and source.get("LEANFLOW_NATIVE_ALLOWED_AXIOMS"):
            values["allowed_axioms"] = tuple(
                part.strip()
                for part in source["LEANFLOW_NATIVE_ALLOWED_AXIOMS"].split(",")
                if part.strip()
            )
        return cls(**values)


def resolve_prover_mode(env: Mapping[str, str]) -> str:
    """Resolve explicit native choices, then legacy switches, then the new-run default."""
    if "LEANFLOW_PROVER_MODE" in env:
        return env["LEANFLOW_PROVER_MODE"].strip() or ProverConfig.mode
    legacy = env.get("LEANFLOW_RESEARCH_MODE", "").strip().lower()
    if legacy:
        return "research" if legacy in {"1", "true", "yes", "on"} else "standard"
    return ProverConfig.mode
