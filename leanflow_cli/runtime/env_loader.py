"""Helpers for loading LeanFlow environment files consistently."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

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


def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    try:
        load_dotenv(dotenv_path=path, override=override, encoding="utf-8")
    except UnicodeDecodeError:
        load_dotenv(dotenv_path=path, override=override, encoding="latin-1")


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
    """Force every model-backed native-workflow lane onto one launch runtime.

    Native foreground and dispatch-worker processes reload the user dotenv at
    import time. Reapply this explicit per-launch override afterwards so blank
    or independently configured ``AUXILIARY_*`` entries cannot silently route
    a nested proving lane through another provider, endpoint, credential,
    model, or reasoning effort.
    """
    provider = str(os.getenv(NATIVE_AUXILIARY_PROVIDER_ENV, "") or "").strip()
    if not provider:
        return ""
    for name in NATIVE_AUXILIARY_PROVIDER_TARGETS:
        os.environ[name] = provider
    for suffix, source_name in (
        ("BASE_URL", NATIVE_AUXILIARY_BASE_URL_ENV),
        ("API_KEY", NATIVE_AUXILIARY_API_KEY_ENV),
        ("MODEL", NATIVE_AUXILIARY_MODEL_ENV),
        ("REASONING_EFFORT", NATIVE_AUXILIARY_REASONING_EFFORT_ENV),
    ):
        value = str(os.getenv(source_name, "") or "").strip()
        if not value:
            continue
        for name in _native_auxiliary_targets(suffix):
            os.environ[name] = value
    return provider
