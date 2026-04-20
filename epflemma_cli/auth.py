"""Minimal provider metadata for EPFLemma auxiliary routing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict

from epflemma_cli.config import get_env_value


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    name: str
    auth_type: str
    inference_base_url: str
    api_key_env_vars: tuple[str, ...] = ()
    base_url_env_var: str = ""
    extra: Dict[str, Any] | None = None


PROVIDER_REGISTRY = {
    "zai": ProviderConfig(
        id="zai",
        name="Z.AI / GLM",
        auth_type="api_key",
        inference_base_url="https://api.z.ai/api/paas/v4",
        api_key_env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        base_url_env_var="GLM_BASE_URL",
    ),
    "kimi-coding": ProviderConfig(
        id="kimi-coding",
        name="Kimi / Moonshot",
        auth_type="api_key",
        inference_base_url="https://api.moonshot.ai/v1",
        api_key_env_vars=("KIMI_API_KEY",),
        base_url_env_var="KIMI_BASE_URL",
    ),
    "minimax": ProviderConfig(
        id="minimax",
        name="MiniMax",
        auth_type="api_key",
        inference_base_url="https://api.minimax.io/v1",
        api_key_env_vars=("MINIMAX_API_KEY",),
        base_url_env_var="MINIMAX_BASE_URL",
    ),
    "minimax-cn": ProviderConfig(
        id="minimax-cn",
        name="MiniMax (China)",
        auth_type="api_key",
        inference_base_url="https://api.minimaxi.com/v1",
        api_key_env_vars=("MINIMAX_CN_API_KEY",),
        base_url_env_var="MINIMAX_CN_BASE_URL",
    ),
    "deepseek": ProviderConfig(
        id="deepseek",
        name="DeepSeek",
        auth_type="api_key",
        inference_base_url="https://api.deepseek.com/v1",
        api_key_env_vars=("DEEPSEEK_API_KEY",),
        base_url_env_var="DEEPSEEK_BASE_URL",
    ),
    "anthropic": ProviderConfig(
        id="anthropic",
        name="Anthropic",
        auth_type="api_key",
        inference_base_url="https://api.anthropic.com",
        api_key_env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
    ),
}


KIMI_CODE_BASE_URL = "https://api.kimi.com/coding/v1"


def _resolve_kimi_base_url(api_key: str, default_url: str, env_override: str) -> str:
    if env_override:
        return env_override
    if api_key.startswith("sk-kimi-"):
        return KIMI_CODE_BASE_URL
    return default_url


def _read_codex_tokens() -> dict[str, str]:
    return {}


def resolve_codex_runtime_credentials(*, force_refresh: bool = True) -> dict[str, str]:
    del force_refresh
    api_key = (
        os.getenv("OPENAI_API_KEY", "").strip()
        or get_env_value("OPENAI_API_KEY", "").strip()
        or os.getenv("OPENROUTER_API_KEY", "").strip()
        or get_env_value("OPENROUTER_API_KEY", "").strip()
    )
    base_url = (
        os.getenv("OPENAI_BASE_URL", "").strip()
        or get_env_value("OPENAI_BASE_URL", "").strip()
        or "https://api.openai.com/v1"
    )
    return {"api_key": api_key, "base_url": base_url.rstrip("/")}


def resolve_nous_runtime_credentials(
    *,
    min_key_ttl_seconds: int = 1800,
    timeout_seconds: float = 15.0,
    force_mint: bool = False,
) -> dict[str, str]:
    del min_key_ttl_seconds, timeout_seconds, force_mint
    api_key = os.getenv("NOUS_API_KEY", "").strip() or get_env_value("NOUS_API_KEY", "").strip()
    base_url = (
        os.getenv("NOUS_BASE_URL", "").strip()
        or get_env_value("NOUS_BASE_URL", "").strip()
        or "https://inference-api.nousresearch.com/v1"
    )
    return {"api_key": api_key, "base_url": base_url.rstrip("/")}
