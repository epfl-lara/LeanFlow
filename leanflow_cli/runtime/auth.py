"""Minimal provider metadata for LeanFlow auxiliary routing."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None

from leanflow_cli.config import get_env_value, get_leanflow_home

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    name: str
    auth_type: str
    inference_base_url: str
    api_key_env_vars: tuple[str, ...] = ()
    base_url_env_var: str = ""
    extra: dict[str, Any] | None = None


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
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_MAIN_DEFAULT_MODEL = "gpt-5.5"
CODEX_MAIN_DEFAULT_REASONING_EFFORT = "xhigh"
CODEX_AUX_DEFAULT_MODEL = "gpt-5.2-codex"


def _read_provider_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
        file_value = str(get_env_value(name, "") or "").strip()
        if file_value:
            return file_value
    return ""


def _resolve_kimi_base_url(api_key: str, default_url: str, env_override: str) -> str:
    if env_override:
        return env_override
    if api_key.startswith("sk-kimi-"):
        return KIMI_CODE_BASE_URL
    return default_url


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("Could not read auth data from %s: %s", path, exc)
        return {}


def _codex_home() -> Path:
    configured = os.getenv("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _read_toml_file(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():
            return {}
        if tomllib is not None:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        # Minimal fallback for old Python runtimes. We only need root-level
        # Codex settings such as model and model_reasoning_effort.
        values: dict[str, Any] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r'\s*([A-Za-z0-9_]+)\s*=\s*"([^"]*)"\s*$', line)
            if match:
                values[match.group(1)] = match.group(2)
        return values
    except Exception as exc:
        logger.debug("Could not read TOML data from %s: %s", path, exc)
        return {}


def read_codex_cli_config() -> dict[str, Any]:
    """Read Codex CLI config from CODEX_HOME / ~/.codex."""

    return _read_toml_file(_codex_home() / "config.toml")


def read_codex_cli_model() -> str:
    value = read_codex_cli_config().get("model", "")
    return value.strip() if isinstance(value, str) else ""


def read_codex_cli_reasoning_effort() -> str:
    value = read_codex_cli_config().get("model_reasoning_effort", "")
    return value.strip().lower() if isinstance(value, str) else ""


def _clean_token_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    tokens: dict[str, str] = {}
    for key, raw in value.items():
        if isinstance(raw, str) and raw.strip():
            tokens[str(key)] = raw.strip()
    return tokens


def _read_codex_tokens(*, allow_legacy_store: bool | None = None) -> dict[str, str]:
    """Read Codex OAuth tokens from LeanFlow auth, optionally falling back to Codex CLI auth.

    LeanFlow-owned auth remains the default to avoid surprising provider
    routing. Callers that explicitly request the Codex provider can opt into the
    Codex CLI store so an existing `codex login` works without duplicating auth.
    """

    auth_data = _read_json_file(get_leanflow_home() / "auth.json")
    provider = auth_data.get("providers", {}).get("openai-codex", {}) if auth_data else {}
    tokens = _clean_token_map(provider.get("tokens", {}))
    if tokens.get("access_token"):
        return tokens

    if allow_legacy_store is None:
        allow_legacy_store = _truthy_env("LEANFLOW_USE_LEGACY_CODEX_AUTH")
    if not allow_legacy_store:
        return {}

    legacy_tokens = _clean_token_map(_read_json_file(_codex_home() / "auth.json").get("tokens", {}))
    if legacy_tokens.get("access_token"):
        return legacy_tokens
    return {}


def resolve_codex_runtime_credentials(
    *,
    force_refresh: bool = True,
    allow_legacy_store: bool | None = None,
) -> dict[str, str]:
    del force_refresh
    tokens = _read_codex_tokens(allow_legacy_store=allow_legacy_store)
    return {"api_key": tokens.get("access_token", ""), "base_url": CODEX_BASE_URL}


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
