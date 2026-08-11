"""Runtime provider resolution for the LeanFlow kernel."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from leanflow_cli.config import DEFAULT_CONFIG, get_env_value, load_config
from leanflow_cli.local_models import resolve_active_local_runtime
from leanflow_cli.runtime.auth import (
    CODEX_BASE_URL,
    CODEX_MAIN_DEFAULT_MODEL,
    CODEX_MAIN_DEFAULT_REASONING_EFFORT,
    read_codex_cli_model,
    read_codex_cli_reasoning_effort,
    resolve_codex_runtime_credentials,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _read_provider_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
        file_value = str(get_env_value(name, "") or "").strip()
        if file_value:
            return file_value
    return ""


def _read_provider_env_with_source(*names: str) -> tuple[str, str]:
    """Return the first configured provider value and its exact variable name."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value, name
        file_value = str(get_env_value(name, "") or "").strip()
        if file_value:
            return file_value, name
    return "", ""


def _validate_openai_compatible_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        return normalized

    hostname = (urlparse(normalized).hostname or "").strip().lower()
    if hostname == "your-rcp-endpoint":
        raise RuntimeProviderError(
            "LeanFlow is still configured with the placeholder host `your-rcp-endpoint`. "
            "Set LEANFLOW_OPENAI_BASE_URL in ~/.leanflow/.env to your real RCP/OpenAI-compatible endpoint."
        )
    return normalized


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    base_url: str
    api_key_env_vars: tuple[str, ...]
    base_url_env_var: str = ""


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "zai": ProviderSpec(
        id="zai",
        base_url="https://api.z.ai/api/paas/v4",
        api_key_env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        base_url_env_var="GLM_BASE_URL",
    ),
    "kimi-coding": ProviderSpec(
        id="kimi-coding",
        base_url="https://api.moonshot.ai/v1",
        api_key_env_vars=("KIMI_API_KEY",),
        base_url_env_var="KIMI_BASE_URL",
    ),
    "minimax": ProviderSpec(
        id="minimax",
        base_url="https://api.minimax.io/v1",
        api_key_env_vars=("MINIMAX_API_KEY",),
        base_url_env_var="MINIMAX_BASE_URL",
    ),
    "minimax-cn": ProviderSpec(
        id="minimax-cn",
        base_url="https://api.minimaxi.com/v1",
        api_key_env_vars=("MINIMAX_CN_API_KEY",),
        base_url_env_var="MINIMAX_CN_BASE_URL",
    ),
    "deepseek": ProviderSpec(
        id="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key_env_vars=("DEEPSEEK_API_KEY",),
        base_url_env_var="DEEPSEEK_BASE_URL",
    ),
}


PROVIDER_DESCRIPTIONS: dict[str, str] = {
    "auto": "Resolve from config and environment, preferring direct keys or custom OpenAI-compatible endpoints.",
    "local": "Use the active managed local runtime such as vllm, ollama, or llama.cpp.",
    "custom": "Use an OpenAI-compatible remote endpoint such as RCP.",
    "rcp": "Use EPFL RCP with model-family-specific credentials when available.",
    "openrouter": "Use the OpenRouter chat-completions endpoint.",
    "codex": "Use the Codex CLI/ChatGPT OAuth session through the Codex Responses endpoint.",
    "anthropic": "Use Anthropic's native Messages API directly.",
    "zai": "Use ZAI / GLM chat-completions directly.",
    "kimi-coding": "Use Moonshot Kimi Coding through its native API.",
    "minimax": "Use Minimax through its global API.",
    "minimax-cn": "Use Minimax through its China endpoint.",
    "deepseek": "Use DeepSeek through its native API.",
}


class RuntimeProviderError(RuntimeError):
    """Raised when provider resolution fails."""


def _normalize_provider_name(value: str) -> str:
    return value.strip().lower().replace(" ", "-")


def list_runtime_provider_targets() -> list[dict[str, str]]:
    targets: list[dict[str, str]] = [
        {"name": "auto", "kind": "selector", "description": PROVIDER_DESCRIPTIONS["auto"]},
        {"name": "local", "kind": "managed-local", "description": PROVIDER_DESCRIPTIONS["local"]},
        {
            "name": "custom",
            "kind": "openai-compatible",
            "description": PROVIDER_DESCRIPTIONS["custom"],
        },
        {
            "name": "rcp",
            "kind": "openai-compatible",
            "description": PROVIDER_DESCRIPTIONS["rcp"],
        },
        {
            "name": "openrouter",
            "kind": "openai-compatible",
            "description": PROVIDER_DESCRIPTIONS["openrouter"],
        },
        {"name": "codex", "kind": "direct", "description": PROVIDER_DESCRIPTIONS["codex"]},
        {"name": "anthropic", "kind": "direct", "description": PROVIDER_DESCRIPTIONS["anthropic"]},
    ]
    for provider_id, spec in PROVIDER_SPECS.items():
        targets.append(
            {
                "name": provider_id,
                "kind": "direct",
                "description": PROVIDER_DESCRIPTIONS.get(provider_id, provider_id),
                "credentials": ", ".join(spec.api_key_env_vars),
            }
        )
    return targets


def _get_model_config() -> dict[str, Any]:
    config = load_config()
    model_cfg = config.get("model")
    if isinstance(model_cfg, Mapping):
        return dict(model_cfg)
    if isinstance(model_cfg, str) and model_cfg.strip():
        return {"default": model_cfg.strip()}
    return {}


def resolve_requested_provider(requested: str | None = None) -> str:
    if requested and requested.strip():
        return _normalize_provider_name(requested)

    model_cfg = _get_model_config()
    cfg_provider = model_cfg.get("provider")
    if isinstance(cfg_provider, str) and cfg_provider.strip():
        return _normalize_provider_name(cfg_provider)

    env_provider = os.getenv("LEANFLOW_INFERENCE_PROVIDER", "").strip()
    if env_provider:
        return _normalize_provider_name(env_provider)

    return "auto"


def _load_named_custom_provider(requested_provider: str) -> dict[str, str] | None:
    requested_norm = _normalize_provider_name(requested_provider or "")
    if not requested_norm or requested_norm in {
        "auto",
        "openrouter",
        "codex",
        "openai-codex",
        "anthropic",
        "custom",
        "local",
        *PROVIDER_SPECS.keys(),
    }:
        return None

    config = load_config()
    custom_providers = config.get("custom_providers")
    if not isinstance(custom_providers, list):
        return None

    for entry in custom_providers:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name", "") or "").strip()
        base_url = str(entry.get("base_url", "") or "").strip()
        if not name or not base_url:
            continue
        normalized = _normalize_provider_name(name)
        if requested_norm not in {normalized, f"custom:{normalized}"}:
            continue
        return {
            "name": name,
            "base_url": base_url.rstrip("/"),
            "api_key": str(entry.get("api_key", "") or "").strip(),
        }
    return None


def _resolve_openai_compatible_runtime(
    *,
    requested_provider: str,
    explicit_api_key: str | None = None,
    explicit_base_url: str | None = None,
) -> dict[str, Any]:
    model_cfg = _get_model_config()
    cfg_base_url = str(model_cfg.get("base_url", "") or "").strip()
    cfg_provider = _normalize_provider_name(str(model_cfg.get("provider", "") or ""))

    env_openai_base_url = _read_provider_env(
        "LEANFLOW_OPENAI_BASE_URL",
        "OPENAI_BASE_URL",
    )
    env_openrouter_base_url = _read_provider_env(
        "LEANFLOW_OPENROUTER_BASE_URL",
        "OPENROUTER_BASE_URL",
    )

    use_config_base_url = False
    if cfg_base_url and not explicit_base_url and not env_openai_base_url:
        if requested_provider == "auto":
            use_config_base_url = cfg_provider in {"", "auto", "custom"}
        elif requested_provider == "custom":
            use_config_base_url = cfg_provider == "custom"

    skip_openai_base = requested_provider == "openrouter"
    base_url = (
        (explicit_base_url or "").strip()
        or ("" if skip_openai_base else env_openai_base_url)
        or (cfg_base_url if use_config_base_url else "")
        or env_openrouter_base_url
        or OPENROUTER_BASE_URL
    )
    base_url = _validate_openai_compatible_base_url(base_url)

    is_openrouter_url = "openrouter.ai" in base_url.lower()
    if is_openrouter_url:
        api_key = explicit_api_key or _read_provider_env(
            "LEANFLOW_OPENROUTER_API_KEY",
            "OPENROUTER_API_KEY",
            "LEANFLOW_OPENAI_API_KEY",
            "OPENAI_API_KEY",
        )
    else:
        api_key = explicit_api_key or _read_provider_env(
            "LEANFLOW_OPENAI_API_KEY",
            "OPENAI_API_KEY",
            "LEANFLOW_OPENROUTER_API_KEY",
            "OPENROUTER_API_KEY",
        )

    return {
        "provider": "openrouter" if is_openrouter_url else "custom",
        "api_mode": "chat_completions",
        "base_url": base_url,
        "api_key": api_key,
        "source": "explicit" if (explicit_api_key or explicit_base_url) else "env/config",
    }


def _resolve_rcp_runtime(model: str) -> dict[str, Any]:
    """Resolve EPFL RCP credentials for the selected model family.

    RCP deployments can issue disjoint virtual keys for GLM and general model
    pools. Selecting a model must therefore select its matching key before the
    first request instead of reusing whichever OpenAI-compatible key happens
    to be the global default.
    """
    normalized_model = str(model or "").strip()
    glm_model = "glm" in normalized_model.lower()
    if glm_model:
        api_key, source = _read_provider_env_with_source(
            "GLM_API_KEY",
            "LEANFLOW_OPENAI_API_KEY",
            "OPENAI_API_KEY",
            "RCP_OPENAI_API_KEY",
        )
        base_url = _read_provider_env(
            "GLM_BASE_URL",
            "RCP_OPENAI_BASE_URL",
            "LEANFLOW_OPENAI_BASE_URL",
            "OPENAI_BASE_URL",
        )
    else:
        api_key, source = _read_provider_env_with_source(
            "RCP_OPENAI_API_KEY",
            "LEANFLOW_OPENAI_API_KEY",
            "OPENAI_API_KEY",
        )
        base_url = _read_provider_env(
            "RCP_OPENAI_BASE_URL",
            "LEANFLOW_OPENAI_BASE_URL",
            "OPENAI_BASE_URL",
        )
    if not api_key:
        expected = "GLM_API_KEY or RCP_OPENAI_API_KEY" if glm_model else "RCP_OPENAI_API_KEY"
        raise RuntimeProviderError(
            f"No EPFL RCP credential found for model {normalized_model or '[unset]'}. "
            f"Set {expected}."
        )
    if not base_url:
        raise RuntimeProviderError(
            "No EPFL RCP base URL found. Set RCP_OPENAI_BASE_URL or GLM_BASE_URL."
        )
    return {
        "provider": "custom",
        "api_mode": "chat_completions",
        "base_url": _validate_openai_compatible_base_url(base_url),
        "api_key": api_key,
        "source": source,
        "requested_provider": "rcp",
        "model": normalized_model,
    }


def apply_runtime_model_override(
    runtime: Mapping[str, Any],
    *,
    requested_provider: str,
    model: str,
) -> dict[str, Any]:
    """Apply one workflow-local model override and refresh coupled credentials."""
    normalized_model = str(model or "").strip()
    if not normalized_model:
        return dict(runtime)
    normalized_provider = _normalize_provider_name(requested_provider or "")
    if normalized_provider == "rcp":
        return _resolve_rcp_runtime(normalized_model)
    resolved = dict(runtime)
    resolved["model"] = normalized_model
    return resolved


def _resolve_anthropic_runtime() -> dict[str, Any]:
    token = (
        os.getenv("ANTHROPIC_TOKEN")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
        or ""
    ).strip()
    if not token:
        raise RuntimeProviderError(
            "No Anthropic credentials found. Set ANTHROPIC_TOKEN or ANTHROPIC_API_KEY."
        )
    return {
        "provider": "anthropic",
        "api_mode": "anthropic_messages",
        "base_url": "https://api.anthropic.com",
        "api_key": token,
        "source": "env",
    }


def _resolve_codex_model() -> str:
    env_model = _read_provider_env("LEANFLOW_CODEX_MODEL", "CODEX_MODEL")
    if env_model:
        return env_model

    codex_cli_model = read_codex_cli_model()
    if codex_cli_model:
        return codex_cli_model

    configured = str(_get_model_config().get("default", "") or "").strip()
    install_default = str(DEFAULT_CONFIG.get("model", {}).get("default", "") or "").strip()
    if configured and configured != install_default:
        return configured
    return CODEX_MAIN_DEFAULT_MODEL


def _resolve_codex_reasoning_effort() -> str:
    env_effort = _read_provider_env("LEANFLOW_CODEX_REASONING_EFFORT", "CODEX_REASONING_EFFORT")
    if env_effort:
        return env_effort.strip().lower()

    codex_cli_effort = read_codex_cli_reasoning_effort()
    if codex_cli_effort:
        return codex_cli_effort
    return CODEX_MAIN_DEFAULT_REASONING_EFFORT


def _resolve_codex_runtime() -> dict[str, Any]:
    credentials = resolve_codex_runtime_credentials(allow_legacy_store=True)
    token = str(credentials.get("api_key", "") or "").strip()
    if not token:
        raise RuntimeProviderError(
            "No Codex OAuth credentials found. Run `codex login` first, or write an LeanFlow "
            "auth.json entry for provider `openai-codex`."
        )
    return {
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "base_url": str(credentials.get("base_url", "") or CODEX_BASE_URL).rstrip("/"),
        "api_key": token,
        "source": "codex-oauth",
        "model": _resolve_codex_model(),
        "reasoning_effort": _resolve_codex_reasoning_effort(),
    }


def _resolve_direct_provider(provider: str) -> dict[str, Any]:
    spec = PROVIDER_SPECS[provider]
    api_key = ""
    source = ""
    for env_var in spec.api_key_env_vars:
        candidate = (os.getenv(env_var) or get_env_value(env_var) or "").strip()
        if candidate:
            api_key = candidate
            source = env_var
            break
    if not api_key:
        raise RuntimeProviderError(
            f"No credentials found for {provider}. Set one of: {', '.join(spec.api_key_env_vars)}."
        )
    base_url = (
        os.getenv(spec.base_url_env_var, "").strip() if spec.base_url_env_var else ""
    ) or spec.base_url
    return {
        "provider": provider,
        "api_mode": "chat_completions",
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "source": source or "env",
    }


def _resolve_local_runtime() -> dict[str, Any]:
    runtime = resolve_active_local_runtime()
    if not runtime:
        raise RuntimeProviderError(
            "No active local runtime configured. Use `leanflow models local use <runtime> <model>` first."
        )
    return {
        "provider": "local",
        "api_mode": "chat_completions",
        "base_url": runtime["base_url"].rstrip("/"),
        "api_key": runtime.get("api_key", "local"),
        "source": f"local:{runtime['runtime']}",
        "runtime": runtime["runtime"],
        "model": runtime.get("model", ""),
    }


def resolve_runtime_provider(
    *,
    requested: str | None = None,
    explicit_api_key: str | None = None,
    explicit_base_url: str | None = None,
) -> dict[str, Any]:
    """Return a resolved runtime provider configuration dict by dispatching on the requested provider (local, codex, anthropic, named custom, or OpenAI-compatible). Explicit API key/base_url override env/config values; missing model defaults to config. Raises RuntimeProviderError if credentials cannot be located."""
    requested_provider = resolve_requested_provider(requested)
    configured_model = str(_get_model_config().get("default", "") or "")

    if requested_provider == "rcp":
        return _resolve_rcp_runtime(configured_model)

    if requested_provider == "local":
        resolved = _resolve_local_runtime()
        resolved["requested_provider"] = requested_provider
        if not resolved.get("model"):
            resolved["model"] = str(_get_model_config().get("default", "") or "")
        return resolved

    if requested_provider in {"codex", "openai-codex"}:
        resolved = _resolve_codex_runtime()
        resolved["requested_provider"] = requested_provider
        return resolved

    custom_provider = _load_named_custom_provider(requested_provider)
    if custom_provider:
        resolved = {
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": _validate_openai_compatible_base_url(
                explicit_base_url or custom_provider["base_url"]
            ),
            "api_key": explicit_api_key
            or custom_provider["api_key"]
            or _read_provider_env(
                "LEANFLOW_OPENAI_API_KEY",
                "OPENAI_API_KEY",
            ),
            "source": f"custom_provider:{custom_provider['name']}",
            "requested_provider": requested_provider,
            "model": str(_get_model_config().get("default", "") or ""),
        }
        return resolved

    if requested_provider == "anthropic":
        resolved = _resolve_anthropic_runtime()
        resolved["requested_provider"] = requested_provider
        resolved["model"] = str(_get_model_config().get("default", "") or "")
        return resolved

    if requested_provider in PROVIDER_SPECS:
        resolved = _resolve_direct_provider(requested_provider)
        resolved["requested_provider"] = requested_provider
        resolved["model"] = str(_get_model_config().get("default", "") or "")
        return resolved

    resolved = _resolve_openai_compatible_runtime(
        requested_provider=requested_provider,
        explicit_api_key=explicit_api_key,
        explicit_base_url=explicit_base_url,
    )
    resolved["requested_provider"] = requested_provider
    resolved["model"] = str(_get_model_config().get("default", "") or "")
    return resolved


def format_runtime_provider_error(error: Exception) -> str:
    return str(error)
