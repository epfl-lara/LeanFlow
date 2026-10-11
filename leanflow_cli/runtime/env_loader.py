"""Helpers for loading LeanFlow environment files consistently."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from leanflow_cli.config import get_leanflow_home

NATIVE_AUXILIARY_PROVIDER_ENV = "LEANFLOW_NATIVE_AUXILIARY_PROVIDER"
NATIVE_AUXILIARY_BASE_URL_ENV = "LEANFLOW_NATIVE_AUXILIARY_BASE_URL"
NATIVE_AUXILIARY_API_KEY_ENV = "LEANFLOW_NATIVE_AUXILIARY_API_KEY"
NATIVE_AUXILIARY_MODEL_ENV = "LEANFLOW_NATIVE_AUXILIARY_MODEL"
NATIVE_AUXILIARY_REASONING_EFFORT_ENV = "LEANFLOW_NATIVE_AUXILIARY_REASONING_EFFORT"
NATIVE_AUXILIARY_PROVIDER_TARGETS = (
    "AUXILIARY_AUTOFORMALIZER_VERIFICATION_PROVIDER",
    "AUXILIARY_BLUEPRINT_VERIFICATION_PROVIDER",
    "AUXILIARY_COMPRESSION_PROVIDER",
    "AUXILIARY_LEAN_DECOMPOSE_HELPERS_PROVIDER",
    "AUXILIARY_LEAN_REASONING_PROVIDER",
    "AUXILIARY_MANAGER_NUDGE_PROVIDER",
    "AUXILIARY_ORCHESTRATION_PROVIDER",
    "AUXILIARY_PLANNER_SYNTHESIS_PROVIDER",
    "AUXILIARY_PROVE_MANAGER_PROVIDER",
    "AUXILIARY_STATEMENT_FIDELITY_PROVIDER",
    "AUXILIARY_WEB_EXTRACT_PROVIDER",
    "CONTEXT_COMPRESSION_PROVIDER",
)


def _native_auxiliary_targets(suffix: str) -> tuple[str, ...]:
    """Return every task-specific auxiliary variable for one runtime field."""
    return tuple(
        f"{name.removesuffix('_PROVIDER')}_{suffix}" for name in NATIVE_AUXILIARY_PROVIDER_TARGETS
    )


def _runtime_owned(name: str) -> bool:
    """Keep native configuration and removed credential routes out of LeanFlow env files."""
    native = ("OPENAI_", "CODEX_", "ANTHROPIC_", "CLAUDE_", "LEMEX_")
    legacy = (
        "LEANFLOW_OPENAI_",
        "LEANFLOW_OPENROUTER_",
        "OPENROUTER_",
        "RCP_",
        "GLM_",
        "ZAI_",
        "Z_AI_",
        "KIMI_",
        "MINIMAX_",
        "DEEPSEEK_",
    )
    return (
        name.startswith(native + legacy)
        or (
            name.startswith(("AUXILIARY_", "LEANFLOW_NATIVE_", "LEANFLOW_PROVER_"))
            and name.endswith(("API_KEY", "API_KEY_ENV", "BASE_URL"))
        )
        or name in {"ANTHROPIC_TOKEN", "LEANFLOW_CODEX_MODEL", "LEANFLOW_CODEX_REASONING_EFFORT"}
    )


def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    """Load ordinary workflow variables without importing native runtime configuration."""
    try:
        values = dotenv_values(path, encoding="utf-8", interpolate=False)
    except UnicodeDecodeError:
        values = dotenv_values(path, encoding="latin-1", interpolate=False)
    for name, value in values.items():
        if value is not None and not _runtime_owned(name) and (override or name not in os.environ):
            os.environ[name] = value


def load_leanflow_dotenv(
    *,
    leanflow_home: str | Path | None = None,
    project_env: str | Path | None = None,
) -> list[Path]:
    loaded: list[Path] = []

    home_path = Path(leanflow_home) if leanflow_home else get_leanflow_home()
    user_env = home_path / ".env"
    project_env_path = Path(project_env) if project_env else None

    if user_env.exists():
        _load_dotenv_with_fallback(user_env, override=False)
        loaded.append(user_env)

    if project_env_path and project_env_path.exists():
        _load_dotenv_with_fallback(project_env_path, override=False)
        loaded.append(project_env_path)

    return loaded


def reassert_native_auxiliary_provider() -> str:
    """Propagate only native backend/model overrides to transitional auxiliary callers."""
    provider = str(os.getenv(NATIVE_AUXILIARY_PROVIDER_ENV, "") or "").strip()
    if provider not in {"codex", "claude", "lemex"}:
        return ""
    for name in NATIVE_AUXILIARY_PROVIDER_TARGETS:
        os.environ[name] = provider
    for suffix, source_name in (
        ("MODEL", NATIVE_AUXILIARY_MODEL_ENV),
        ("REASONING_EFFORT", NATIVE_AUXILIARY_REASONING_EFFORT_ENV),
    ):
        value = str(os.getenv(source_name, "") or "").strip()
        if value:
            for name in _native_auxiliary_targets(suffix):
                os.environ[name] = value
    return provider
