"""Resolve one role's provider without mutating shared process credentials."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def resolve_session_provider(config: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve an explicit route or retain the legacy native provider unchanged."""
    provider = str(config.get("provider") or "").strip()
    if not provider:
        return {
            "provider": os.getenv("LEANFLOW_NATIVE_PROVIDER", ""),
            "api_mode": os.getenv("LEANFLOW_NATIVE_API_MODE", ""),
            "base_url": os.getenv("LEANFLOW_NATIVE_BASE_URL", ""),
            "api_key": os.getenv("LEANFLOW_NATIVE_API_KEY", ""),
        }
    from leanflow_cli.runtime.runtime_provider import resolve_runtime_provider

    key_env = str(config.get("api_key_env") or "").strip()
    api_key = os.environ.get(key_env, "") if key_env else ""
    if key_env and not api_key:
        raise ValueError(f"Required provider credential environment variable is unset: {key_env}")
    base_url = str(config.get("base_url") or "").strip()
    if provider == "rcp":
        if not api_key or not base_url:
            raise ValueError("An explicit RCP route requires base_url and api_key_env")
        # The general RCP resolver chooses a key by model family. An explicit
        # job route instead binds the requested virtual key to this experiment.
        provider = "custom"
    return resolve_runtime_provider(
        requested=provider,
        explicit_api_key=api_key or None,
        explicit_base_url=base_url or None,
    )
