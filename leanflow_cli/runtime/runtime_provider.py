"""Select a native agent backend without taking ownership of its configuration."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent.runtime.harness.commands import BACKENDS
from leanflow_cli.config import load_config, native_config_migration_required

NATIVE_BACKENDS = BACKENDS
PROVIDER_DESCRIPTIONS: dict[str, str] = {
    "codex": "Codex CLI with its existing login or standard OpenAI API key.",
    "claude": "Claude Code with its existing login or Anthropic API key.",
    "lemex": "Lemex with its own configuration, including LEMEX_BASE_URL and LEMEX_API_KEY.",
}


class RuntimeProviderError(RuntimeError):
    """Report unsupported legacy routing or invalid native-backend settings."""


def list_runtime_provider_targets() -> list[dict[str, str]]:
    """Return the complete supported backend list, without inspecting credentials."""
    return [
        {"name": name, "kind": "native", "description": PROVIDER_DESCRIPTIONS[name]}
        for name in NATIVE_BACKENDS
    ]


def _get_model_config() -> dict[str, Any]:
    """Return explicit LeanFlow model overrides, leaving native defaults opaque."""
    model = load_config().get("model")
    return dict(model) if isinstance(model, Mapping) else {}


def resolve_requested_provider(requested: str | None = None) -> str:
    """Select an explicit backend, environment override, saved backend, or Codex."""
    value = (
        (requested or "").strip()
        or os.getenv("LEANFLOW_INFERENCE_PROVIDER", "").strip()
        or str(_get_model_config().get("provider", "") or "").strip()
        or "codex"
    ).lower()
    if value not in NATIVE_BACKENDS:
        raise RuntimeProviderError(
            f"Unsupported backend {value!r}. LeanFlow now supports only codex, claude, and lemex. "
            "Select one with --provider or model.provider. For external endpoints, choose lemex "
            "and configure Lemex directly with LEMEX_BASE_URL and LEMEX_API_KEY. "
            "Existing run artifacts remain readable; start a new run after migrating."
        )
    return value


def resolve_runtime_provider(
    *,
    requested: str | None = None,
    explicit_api_key: str | None = None,
    explicit_base_url: str | None = None,
) -> dict[str, Any]:
    """Describe a native launch without reading, copying, or validating runtime auth.

    The CLI owns login, endpoint selection, sampling, compression, and retries.
    Empty model and reasoning fields deliberately preserve that CLI's defaults.
    """
    if explicit_api_key or explicit_base_url:
        raise RuntimeProviderError(
            "LeanFlow no longer accepts provider keys or endpoints. Configure the native CLI "
            "directly: OPENAI_API_KEY for Codex, ANTHROPIC_API_KEY for Claude, or "
            "LEMEX_BASE_URL and LEMEX_API_KEY for Lemex. Native CLI login is also supported."
        )
    backend = resolve_requested_provider(requested)
    config = load_config()
    if native_config_migration_required(config):
        raise RuntimeProviderError(
            "This LeanFlow config uses the retired provider engine. Keep a backup, then remove "
            "legacy endpoints/keys, custom_providers, local_models, compression, sampling, and "
            "per-turn limits; set schema_version: 2 and model.provider to codex, claude, or lemex. "
            "Clear model.default and agent.reasoning_effort to inherit native defaults, or keep "
            "deliberate overrides. Configure credentials in the native CLI. Existing settings "
            "and run artifacts have not been rewritten."
        )
    if backend == "codex" and os.getenv("OPENAI_BASE_URL", "").strip():
        endpoint = urlparse(os.environ["OPENAI_BASE_URL"].strip())
        if (
            endpoint.scheme != "https"
            or endpoint.hostname != "api.openai.com"
            or endpoint.username
            or endpoint.password
        ):
            raise RuntimeProviderError(
                "Codex requires the standard OpenAI endpoint. For an external OPENAI_BASE_URL, "
                "select lemex and configure LEMEX_BASE_URL and LEMEX_API_KEY instead."
            )
    model = config.get("model", {})
    agent = config.get("agent", {})
    effort = (
        str(agent.get("reasoning_effort", "") or "").strip() if isinstance(agent, Mapping) else ""
    )
    return {
        "provider": backend,
        "requested_provider": backend,
        "api_mode": "native",
        "base_url": "",
        "api_key": "",
        "source": "native-cli",
        "model": str(model.get("default", "") or "").strip() if isinstance(model, Mapping) else "",
        "reasoning_effort": "" if effort == "auto" else effort,
    }


def resolve_backend_executable(backend: str) -> str:
    """Locate the selected CLI without reading its authentication or making a request."""
    name = resolve_requested_provider(backend)
    if name == "lemex":
        from leanflow_cli.runtime.lemex_install import resolve_lemex_executable

        executable = resolve_lemex_executable()
    else:
        found = shutil.which(name)
        executable = Path(found) if found else None
    if executable is None:
        setup = {
            "codex": "Install Codex CLI, then use codex login or export OPENAI_API_KEY.",
            "claude": "Install Claude Code, then use its login or export ANTHROPIC_API_KEY.",
            "lemex": "Run the LeanFlow installer with --with-lemex; configure its native LEMEX_BASE_URL and LEMEX_API_KEY.",
        }
        raise RuntimeProviderError(f"{name} executable is missing. {setup[name]}")
    return str(executable)


def apply_runtime_model_override(
    runtime: Mapping[str, Any], *, requested_provider: str, model: str
) -> dict[str, Any]:
    """Apply an explicit workflow model while leaving runtime configuration intact."""
    resolve_requested_provider(requested_provider)
    resolved = dict(runtime)
    if model.strip():
        resolved["model"] = model.strip()
    return resolved


def format_runtime_provider_error(error: Exception) -> str:
    """Return the actionable backend error for command-line display."""
    return str(error)
