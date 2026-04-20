"""Runtime provider resolution for the OpenGauss kernel."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from opengauss_cli.config import get_env_value, load_config
from opengauss_cli.local_models import resolve_active_local_runtime


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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
    "openrouter": "Use the OpenRouter chat-completions endpoint.",
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
        {"name": "custom", "kind": "openai-compatible", "description": PROVIDER_DESCRIPTIONS["custom"]},
        {"name": "openrouter", "kind": "openai-compatible", "description": PROVIDER_DESCRIPTIONS["openrouter"]},
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


def resolve_requested_provider(requested: Optional[str] = None) -> str:
    if requested and requested.strip():
        return _normalize_provider_name(requested)

    model_cfg = _get_model_config()
    cfg_provider = model_cfg.get("provider")
    if isinstance(cfg_provider, str) and cfg_provider.strip():
        return _normalize_provider_name(cfg_provider)

    env_provider = os.getenv("OPENGAUSS_INFERENCE_PROVIDER", "").strip()
    if env_provider:
        return _normalize_provider_name(env_provider)

    return "auto"


def _load_named_custom_provider(requested_provider: str) -> Optional[dict[str, str]]:
    requested_norm = _normalize_provider_name(requested_provider or "")
    if not requested_norm or requested_norm in {
        "auto",
        "openrouter",
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
    explicit_api_key: Optional[str] = None,
    explicit_base_url: Optional[str] = None,
) -> dict[str, Any]:
    model_cfg = _get_model_config()
    cfg_base_url = str(model_cfg.get("base_url", "") or "").strip()
    cfg_provider = _normalize_provider_name(str(model_cfg.get("provider", "") or ""))

    env_openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    env_openrouter_base_url = os.getenv("OPENROUTER_BASE_URL", "").strip()

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
    ).rstrip("/")

    is_openrouter_url = "openrouter.ai" in base_url.lower()
    if is_openrouter_url:
        api_key = explicit_api_key or os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    else:
        api_key = explicit_api_key or os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY") or ""

    return {
        "provider": "openrouter" if is_openrouter_url else "custom",
        "api_mode": "chat_completions",
        "base_url": base_url,
        "api_key": api_key,
        "source": "explicit" if (explicit_api_key or explicit_base_url) else "env/config",
    }


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
    base_url = (os.getenv(spec.base_url_env_var, "").strip() if spec.base_url_env_var else "") or spec.base_url
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
            "No active local runtime configured. Use `opengauss models local use <runtime> <model>` first."
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
    requested: Optional[str] = None,
    explicit_api_key: Optional[str] = None,
    explicit_base_url: Optional[str] = None,
) -> dict[str, Any]:
    requested_provider = resolve_requested_provider(requested)

    if requested_provider == "local":
        resolved = _resolve_local_runtime()
        resolved["requested_provider"] = requested_provider
        if not resolved.get("model"):
            resolved["model"] = str(_get_model_config().get("default", "") or "")
        return resolved

    custom_provider = _load_named_custom_provider(requested_provider)
    if custom_provider:
        resolved = {
            "provider": "custom",
            "api_mode": "chat_completions",
            "base_url": (explicit_base_url or custom_provider["base_url"]).rstrip("/"),
            "api_key": explicit_api_key or custom_provider["api_key"] or os.getenv("OPENAI_API_KEY", "").strip(),
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
