"""Shared auxiliary client router for side tasks.

Provides a single resolution chain so every consumer (context compression,
session search, web extraction) picks up the best available backend without
duplicating fallback logic.

Resolution order for text tasks (auto mode):
  1. OpenRouter  (OPENROUTER_API_KEY)
  2. Nous Portal (~/.leanflow/auth.json active provider)
  3. Custom endpoint (OPENAI_BASE_URL + OPENAI_API_KEY)
  4. Codex OAuth (Responses API via chatgpt.com with gpt-5.3-codex,
     wrapped to look like a chat.completions client)
  5. Native Anthropic
  6. Direct API-key providers (z.ai/GLM, Kimi/Moonshot, MiniMax, MiniMax-CN)
  7. None

Per-task provider overrides (e.g. CONTEXT_COMPRESSION_PROVIDER,
AUXILIARY_WEB_EXTRACT_PROVIDER) can force a specific provider for each task.
Default "auto" follows the chain above.

Per-task model overrides (e.g. CONTEXT_COMPRESSION_MODEL,
AUXILIARY_WEB_EXTRACT_MODEL) let callers use a different model slug
than the provider's default.

Per-task direct endpoint overrides (e.g. AUXILIARY_WEB_EXTRACT_BASE_URL,
AUXILIARY_WEB_EXTRACT_API_KEY) let callers route a specific auxiliary task to a
custom OpenAI-compatible endpoint without touching the main model settings.
"""

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from openai import OpenAI

from core.constants import OPENROUTER_BASE_URL
from core.provider_capacity import (
    acquire_background_provider_lease,
    background_actor_context_active,
    background_provider_lease,
)
from leanflow_cli.runtime.auth import (
    CODEX_AUX_DEFAULT_MODEL,
    CODEX_BASE_URL,
    PROVIDER_REGISTRY,
    _read_codex_tokens,
    _resolve_kimi_base_url,
)
from leanflow_cli.runtime.runtime_provider import resolve_runtime_provider

logger = logging.getLogger(__name__)

# Default auxiliary models for direct API-key providers (cheap/fast for side tasks)
_API_KEY_PROVIDER_AUX_MODELS: dict[str, str] = {
    "zai": "glm-4.5-flash",
    "kimi-coding": "kimi-k2-turbo-preview",
    "minimax": "MiniMax-M2.5-highspeed",
    "minimax-cn": "MiniMax-M2.5-highspeed",
    "anthropic": "claude-haiku-4-5-20251001",
}

# OpenRouter app attribution headers
_OR_HEADERS = {
    "HTTP-Referer": "https://leanflow.dev",
    "X-OpenRouter-Title": "LeanFlow Agent",
    "X-OpenRouter-Categories": "productivity,cli-agent",
}

# Nous Portal extra_body for product attribution.
# Callers should pass this as extra_body in chat.completions.create()
# when the auxiliary client is backed by Nous Portal.
NOUS_EXTRA_BODY = {"tags": ["product=leanflow-agent"]}

# Set at resolve time — True if the auxiliary client points to Nous Portal
auxiliary_is_nous: bool = False

# NOTE: no fallback for "orchestration" — its D1 default is the STRONG
# main-agent model (provider "main", model ""), and a lean_reasoning
# fallback would silently inherit the advisor model into that slot.
# planner_synthesis -> orchestration is consistent with that rule: the
# synthesizer inherits the strong-model default, never the advisor.
# Keep in sync with TASK_FALLBACKS in leanflow_cli/cli/expert_help.py.
_AUXILIARY_TASK_FALLBACKS: dict[str, str] = {
    "lean_decompose_helpers": "lean_reasoning",
    "planner_synthesis": "orchestration",
    # Fidelity is another short, strict verdict turn. Inherit the strong main
    # endpoint so RCP can apply the same non-thinking JSON/text-turn controls;
    # raw `auto` routing does not retain enough provider identity to attach
    # RCP chat-template kwargs after client auto-detection.
    "statement_fidelity": "orchestration",
}

# Orchestration, planner synthesis, and fidelity replies are strict structured
# turns. On RCP thinking models, the default hidden reasoning can consume the
# output allowance before any final JSON/text is emitted. Users can opt back
# into reasoning through each task's AUXILIARY_*_REASONING_EFFORT or config.
_AUXILIARY_TASK_REASONING_DEFAULTS: dict[str, str] = {
    "orchestration": "off",
    "planner_synthesis": "off",
    "statement_fidelity": "off",
}

# Default auxiliary models per provider
_OPENROUTER_MODEL = "google/gemini-3-flash-preview"
_NOUS_MODEL = "gemini-3-flash"
_ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
# Codex fallback: uses the Responses API (the only endpoint the Codex
# OAuth token can access). Explicit Codex routes inherit the main runtime's
# configured model; auto-routing retains this conservative standalone fallback.
_CODEX_AUX_MODEL = CODEX_AUX_DEFAULT_MODEL
_CODEX_AUX_BASE_URL = CODEX_BASE_URL


def _compatible_explicit_model(provider: str, model: str | None) -> str | None:
    """Return a provider-compatible explicit model override.

    OpenRouter-style vendor/model slugs are invalid on the ChatGPT Codex
    Responses endpoint. An all-lanes provider override must fall back to the
    Codex auxiliary default instead of retaining the old provider's model.
    """
    normalized_provider = str(provider or "").strip().lower()
    normalized_model = str(model or "").strip()
    if normalized_provider in {"codex", "openai-codex"} and "/" in normalized_model:
        logger.info(
            "Dropping incompatible auxiliary model %r for provider %s",
            normalized_model,
            normalized_provider,
        )
        return None
    return normalized_model or None


# ── Provider client adapters ───────────────────────────────────────────────
# The OpenAI-client-compatible adapters for Codex (Responses API) and native
# Anthropic (Messages API) live in agent/auxiliary_adapters.py. They are pure
# translation shims with no auxiliary-routing state, so they were extracted as
# a closed cluster and re-exported here for backwards compatibility — every
# importer and test keeps resolving auxiliary_client.<name>. The class objects
# are the SAME objects, so isinstance() checks (here and in tests) still hold.
from agent.providers.auxiliary_adapters import (  # noqa: E402,F401
    AnthropicAuxiliaryClient,
    AsyncAnthropicAuxiliaryClient,
    AsyncCodexAuxiliaryClient,
    CodexAuxiliaryClient,
    _AnthropicChatShim,
    _AnthropicCompletionsAdapter,
    _AsyncAnthropicChatShim,
    _AsyncAnthropicCompletionsAdapter,
    _AsyncCodexChatShim,
    _AsyncCodexCompletionsAdapter,
    _CodexChatShim,
    _CodexCompletionsAdapter,
    _convert_content_for_responses,
)

# Nous Portal auth/endpoint helpers live in agent/auxiliary_nous.py. They read
# ~/.leanflow/auth.json and resolve the Nous API key/base URL with no auxiliary
# routing state, so they were extracted as a closed cluster (with the default
# base-URL constant) and re-exported here — every importer and test keeps
# resolving auxiliary_client.<name>.
from agent.providers.auxiliary_nous import (  # noqa: E402,F401
    _NOUS_DEFAULT_BASE_URL,
    _nous_api_key,
    _nous_base_url,
    _read_nous_auth,
)


def _read_codex_access_token(*, allow_legacy_store: bool | None = None) -> str | None:
    """Read a valid Codex OAuth access token from LeanFlow auth state.

    LeanFlow's auth.json is authoritative when present. Legacy ``~/.codex``
    fallback is opt-in to avoid unrelated desktop auth state silently changing
    auxiliary routing and tests.
    """
    tokens = _read_codex_tokens(allow_legacy_store=allow_legacy_store)
    access_token = tokens.get("access_token", "")
    return access_token or None


def _explicit_codex_model() -> str:
    """Return the main Codex runtime model for an explicitly selected Codex lane."""
    try:
        runtime = resolve_runtime_provider(requested="codex")
    except Exception:
        return _CODEX_AUX_MODEL
    return str(runtime.get("model", "") or "").strip() or _CODEX_AUX_MODEL


def _load_runtime_config() -> dict[str, Any]:
    try:
        from leanflow_cli import config as config_module

        loaded = config_module.load_config()
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _resolve_api_key_provider() -> tuple[OpenAI | None, str | None]:
    """Try each API-key provider in PROVIDER_REGISTRY order.

    Returns (client, model) for the first provider whose env var is set,
    or (None, None) if none are configured.
    """
    for provider_id, pconfig in PROVIDER_REGISTRY.items():
        if pconfig.auth_type != "api_key":
            continue
        # Check if any of the provider's env vars are set
        api_key = ""
        for env_var in pconfig.api_key_env_vars:
            val = os.getenv(env_var, "").strip()
            if val:
                api_key = val
                break
        if not api_key:
            continue
        if provider_id == "anthropic":
            return _try_anthropic()

        # Resolve base URL (with optional env-var override)
        # Kimi Code keys (sk-kimi-) need api.kimi.com/coding/v1
        env_url = ""
        if pconfig.base_url_env_var:
            env_url = os.getenv(pconfig.base_url_env_var, "").strip()
        if env_url:
            base_url = env_url.rstrip("/")
        elif provider_id == "kimi-coding" and api_key.startswith("sk-kimi-"):
            base_url = "https://api.kimi.com/coding/v1"
        else:
            base_url = pconfig.inference_base_url
        model = _API_KEY_PROVIDER_AUX_MODELS.get(provider_id, "default")
        logger.debug("Auxiliary text client: %s (%s)", pconfig.name, model)
        extra = {}
        if "api.kimi.com" in base_url.lower():
            extra["default_headers"] = {"User-Agent": "KimiCLI/1.0"}
        return OpenAI(api_key=api_key, base_url=base_url, **extra), model

    return None, None


# ── Provider resolution helpers ─────────────────────────────────────────────


def _get_auxiliary_provider(task: str = "") -> str:
    """Read the provider override for a specific auxiliary task.

    Checks AUXILIARY_{TASK}_PROVIDER first (e.g. AUXILIARY_WEB_EXTRACT_PROVIDER),
    then CONTEXT_{TASK}_PROVIDER (for the compression section's summary_provider),
    then falls back to "auto".  Returns one of: "auto", "openrouter", "nous", "main".
    """
    if task:
        for prefix in ("AUXILIARY_", "CONTEXT_"):
            val = os.getenv(f"{prefix}{task.upper()}_PROVIDER", "").strip().lower()
            if val and val != "auto":
                return val
    return "auto"


def _get_auxiliary_env_override(task: str, suffix: str) -> str | None:
    """Read an auxiliary env override from AUXILIARY_* or CONTEXT_* prefixes."""
    if not task:
        return None
    for prefix in ("AUXILIARY_", "CONTEXT_"):
        val = os.getenv(f"{prefix}{task.upper()}_{suffix}", "").strip()
        if val:
            return val
    return None


def _auxiliary_fallback_task(task: str = None) -> str | None:
    return _AUXILIARY_TASK_FALLBACKS.get(str(task or "").strip())


def _auxiliary_task_config(config: dict[str, Any], task: str = None) -> dict[str, Any]:
    if not task:
        return {}
    aux = config.get("auxiliary", {}) if isinstance(config, dict) else {}
    task_config = aux.get(task, {}) if isinstance(aux, dict) else {}
    return task_config if isinstance(task_config, dict) else {}


def _task_config_text(config: dict[str, Any], key: str) -> str | None:
    value = config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _try_openrouter() -> tuple[OpenAI | None, str | None]:
    or_key = os.getenv("OPENROUTER_API_KEY")
    if not or_key:
        return None, None
    logger.debug("Auxiliary client: OpenRouter")
    return (
        OpenAI(api_key=or_key, base_url=OPENROUTER_BASE_URL, default_headers=_OR_HEADERS),
        _OPENROUTER_MODEL,
    )


def _try_nous() -> tuple[OpenAI | None, str | None]:
    nous = _read_nous_auth()
    if not nous:
        return None, None
    global auxiliary_is_nous
    auxiliary_is_nous = True
    logger.debug("Auxiliary client: Nous Portal")
    return (
        OpenAI(api_key=_nous_api_key(nous), base_url=_nous_base_url()),
        _NOUS_MODEL,
    )


def _read_main_model() -> str:
    """Read the user's configured main model from config/env.

    Falls back through OPENAI_MODEL → LLM_MODEL → config.yaml model.default
    so the auxiliary client can use the same model as the main agent when no
    dedicated auxiliary model is available.
    """
    from_env = os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL")
    if from_env:
        return from_env.strip()
    try:
        cfg = _load_runtime_config()
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, str) and model_cfg.strip():
            return model_cfg.strip()
        if isinstance(model_cfg, dict):
            default = model_cfg.get("default", "")
            if isinstance(default, str) and default.strip():
                return default.strip()
    except Exception:
        logger.debug("Could not read main model from runtime config", exc_info=True)
    return ""


def _resolve_custom_runtime() -> tuple[str | None, str | None]:
    """Resolve the active custom/main endpoint the same way the main CLI does.

    This covers both env-driven OPENAI_BASE_URL setups and config-saved custom
    endpoints where the base URL lives in config.yaml instead of the live
    environment.
    """
    try:
        runtime = resolve_runtime_provider(requested="custom")
    except Exception as exc:
        logger.debug("Auxiliary client: custom runtime resolution failed: %s", exc)
        return None, None

    custom_base = runtime.get("base_url")
    custom_key = runtime.get("api_key")
    if not isinstance(custom_base, str) or not custom_base.strip():
        return None, None
    if not isinstance(custom_key, str) or not custom_key.strip():
        return None, None

    custom_base = custom_base.strip().rstrip("/")
    if "openrouter.ai" in custom_base.lower():
        # requested='custom' falls back to OpenRouter when no custom endpoint is
        # configured. Treat that as "no custom endpoint" for auxiliary routing.
        return None, None

    return custom_base, custom_key.strip()


def _current_custom_base_url() -> str:
    custom_base, _ = _resolve_custom_runtime()
    return custom_base or ""


def _try_custom_endpoint() -> tuple[OpenAI | None, str | None]:
    custom_base, custom_key = _resolve_custom_runtime()
    if not custom_base or not custom_key:
        return None, None
    model = _read_main_model() or "gpt-4o-mini"
    logger.debug("Auxiliary client: custom endpoint (%s)", model)
    return OpenAI(api_key=custom_key, base_url=custom_base), model


def _try_codex(*, allow_legacy_store: bool | None = None) -> tuple[Any | None, str | None]:
    # Keep the no-argument call shape for the auto-routing patch surface.
    # Explicit Codex routes pass True and intentionally opt into CLI auth.
    codex_token = (
        _read_codex_access_token()
        if allow_legacy_store is None
        else _read_codex_access_token(allow_legacy_store=allow_legacy_store)
    )
    if not codex_token:
        return None, None
    codex_model = _explicit_codex_model() if allow_legacy_store else _CODEX_AUX_MODEL
    logger.debug("Auxiliary client: Codex OAuth (%s via Responses API)", codex_model)
    real_client = OpenAI(api_key=codex_token, base_url=_CODEX_AUX_BASE_URL)
    return CodexAuxiliaryClient(real_client, codex_model), codex_model


def _try_anthropic() -> tuple[Any | None, str | None]:
    try:
        from agent.providers.anthropic_adapter import (
            build_anthropic_client,
            resolve_anthropic_token,
        )
    except ImportError:
        return None, None

    token = resolve_anthropic_token()
    if not token:
        return None, None

    model = _API_KEY_PROVIDER_AUX_MODELS.get("anthropic", "claude-haiku-4-5-20251001")
    logger.debug("Auxiliary client: Anthropic native (%s)", model)
    real_client = build_anthropic_client(token, _ANTHROPIC_DEFAULT_BASE_URL)
    return AnthropicAuxiliaryClient(real_client, model, token, _ANTHROPIC_DEFAULT_BASE_URL), model


def _resolve_forced_provider(forced: str) -> tuple[OpenAI | None, str | None]:
    """Resolve a specific forced provider.  Returns (None, None) if creds missing."""
    if forced == "openrouter":
        client, model = _try_openrouter()
        if client is None:
            logger.warning("auxiliary.provider=openrouter but OPENROUTER_API_KEY not set")
        return client, model

    if forced == "nous":
        client, model = _try_nous()
        if client is None:
            logger.warning(
                "auxiliary.provider=nous but Nous Portal not configured (run: leanflow provider)"
            )
        return client, model

    if forced == "codex":
        client, model = _try_codex()
        if client is None:
            logger.warning(
                "auxiliary.provider=codex but no Codex OAuth token found (run: codex login)"
            )
        return client, model

    if forced == "main":
        # "main" = skip OpenRouter/Nous, use the main chat model's credentials.
        for try_fn in (_try_custom_endpoint, _try_codex, _resolve_api_key_provider):
            client, model = try_fn()
            if client is not None:
                return client, model
        logger.warning("auxiliary.provider=main but no main endpoint credentials found")
        return None, None

    # Unknown provider name — fall through to auto
    logger.warning("Unknown auxiliary.provider=%r, falling back to auto", forced)
    return None, None


def _resolve_auto() -> tuple[OpenAI | None, str | None]:
    """Full auto-detection chain: OpenRouter → Nous → custom → Codex → API-key → None."""
    for try_fn in (
        _try_openrouter,
        _try_nous,
        _try_custom_endpoint,
        _try_codex,
        _resolve_api_key_provider,
    ):
        client, model = try_fn()
        if client is not None:
            return client, model
    logger.debug("Auxiliary client: none available")
    return None, None


# ── Centralized Provider Router ─────────────────────────────────────────────
#
# resolve_provider_client() is the single entry point for creating a properly
# configured client given a (provider, model) pair.  It handles auth lookup,
# base URL resolution, provider-specific headers, and API format differences
# (Chat Completions vs Responses API for Codex).
#
# All auxiliary consumer code should go through this or the public helpers
# below — never look up auth env vars ad-hoc.


def _to_async_client(sync_client, model: str):
    """Convert a sync client to its async counterpart, preserving Codex routing."""
    from openai import AsyncOpenAI

    if isinstance(sync_client, CodexAuxiliaryClient):
        return AsyncCodexAuxiliaryClient(sync_client), model
    if isinstance(sync_client, AnthropicAuxiliaryClient):
        return AsyncAnthropicAuxiliaryClient(sync_client), model

    async_kwargs = {
        "api_key": sync_client.api_key,
        "base_url": str(sync_client.base_url),
        # async_call_llm callers own their retry policy and timeout. Hidden SDK
        # retries otherwise multiply a bounded web/coach call before control
        # returns to that caller.
        "max_retries": 0,
    }
    base_lower = str(sync_client.base_url).lower()
    if "openrouter" in base_lower:
        async_kwargs["default_headers"] = dict(_OR_HEADERS)
    elif "api.kimi.com" in base_lower:
        async_kwargs["default_headers"] = {"User-Agent": "KimiCLI/1.0"}
    return AsyncOpenAI(**async_kwargs), model


def resolve_provider_client(
    provider: str,
    model: str = None,
    async_mode: bool = False,
    raw_codex: bool = False,
    explicit_base_url: str = None,
    explicit_api_key: str = None,
) -> tuple[Any | None, str | None]:
    """Central router: given a provider name and optional model, return a
    configured client with the correct auth, base URL, and API format.

    The returned client always exposes ``.chat.completions.create()`` — for
    Codex/Responses API providers, an adapter handles the translation
    transparently.

    Args:
        provider: Provider identifier.  One of:
            "openrouter", "nous", "openai-codex" (or "codex"),
            "zai", "kimi-coding", "minimax", "minimax-cn",
            "custom" (OPENAI_BASE_URL + OPENAI_API_KEY),
            "auto" (full auto-detection chain).
        model: Model slug override.  If None, uses the provider's default
               auxiliary model.
        async_mode: If True, return an async-compatible client.
        raw_codex: If True, return a raw OpenAI client for Codex providers
            instead of wrapping in CodexAuxiliaryClient.  Use this when
            the caller needs direct access to responses.stream() (e.g.,
            the main agent loop).
        explicit_base_url: Optional direct OpenAI-compatible endpoint.
        explicit_api_key: Optional API key paired with explicit_base_url.

    Returns:
        (client, resolved_model) or (None, None) if auth is unavailable.
    """
    # Normalise aliases
    provider = (provider or "auto").strip().lower()
    if provider == "codex":
        provider = "openai-codex"
    if provider == "main":
        provider = "custom"
    model = _compatible_explicit_model(provider, model)

    # ── Auto: try all providers in priority order ────────────────────
    if provider == "auto":
        client, resolved = _resolve_auto()
        if client is None:
            return None, None
        # When auto-detection lands on a non-OpenRouter provider (e.g. a
        # local server), an OpenRouter-formatted model override like
        # "google/gemini-3-flash-preview" won't work.  Drop it and use
        # the provider's own default model instead.
        if model and "/" in model and resolved and "/" not in resolved:
            logger.debug(
                "Dropping OpenRouter-format model %r for non-OpenRouter "
                "auxiliary provider (using %r instead)",
                model,
                resolved,
            )
            model = None
        final_model = model or resolved
        return _to_async_client(client, final_model) if async_mode else (client, final_model)

    # ── OpenRouter ───────────────────────────────────────────────────
    if provider == "openrouter":
        client, default = _try_openrouter()
        if client is None:
            logger.warning(
                "resolve_provider_client: openrouter requested but OPENROUTER_API_KEY not set"
            )
            return None, None
        final_model = model or default
        return _to_async_client(client, final_model) if async_mode else (client, final_model)

    # ── Nous Portal (OAuth) ──────────────────────────────────────────
    if provider == "nous":
        client, default = _try_nous()
        if client is None:
            logger.warning(
                "resolve_provider_client: nous requested "
                "but Nous Portal not configured (run: leanflow provider)"
            )
            return None, None
        final_model = model or default
        return _to_async_client(client, final_model) if async_mode else (client, final_model)

    # ── OpenAI Codex (OAuth → Responses API) ─────────────────────────
    if provider == "openai-codex":
        if raw_codex:
            # Return the raw OpenAI client for callers that need direct
            # access to responses.stream() (e.g., the main agent loop).
            codex_token = _read_codex_access_token(allow_legacy_store=True)
            if not codex_token:
                logger.warning(
                    "resolve_provider_client: openai-codex requested "
                    "but no Codex OAuth token found (run: codex login)"
                )
                return None, None
            final_model = model or _explicit_codex_model()
            raw_client = OpenAI(api_key=codex_token, base_url=_CODEX_AUX_BASE_URL)
            return (raw_client, final_model)
        # Standard path: wrap in CodexAuxiliaryClient adapter
        client, default = _try_codex(allow_legacy_store=True)
        if client is None:
            logger.warning(
                "resolve_provider_client: openai-codex requested "
                "but no Codex OAuth token found (run: codex login)"
            )
            return None, None
        final_model = model or default
        return _to_async_client(client, final_model) if async_mode else (client, final_model)

    # ── Custom endpoint (OPENAI_BASE_URL + OPENAI_API_KEY) ───────────
    if provider == "custom":
        if explicit_base_url:
            custom_base = explicit_base_url.strip()
            custom_key = (explicit_api_key or "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
            if not custom_base or not custom_key:
                logger.warning(
                    "resolve_provider_client: explicit custom endpoint requested "
                    "but no API key was found (set explicit_api_key or OPENAI_API_KEY)"
                )
                return None, None
            final_model = model or _read_main_model() or "gpt-4o-mini"
            client = OpenAI(api_key=custom_key, base_url=custom_base)
            return _to_async_client(client, final_model) if async_mode else (client, final_model)
        # Try custom first, then codex, then API-key providers
        for try_fn in (_try_custom_endpoint, _try_codex, _resolve_api_key_provider):
            client, default = try_fn()
            if client is not None:
                final_model = model or default
                return (
                    _to_async_client(client, final_model) if async_mode else (client, final_model)
                )
        logger.warning(
            "resolve_provider_client: custom/main requested but no endpoint credentials found"
        )
        return None, None

    # ── API-key providers from PROVIDER_REGISTRY ─────────────────────
    pconfig = PROVIDER_REGISTRY.get(provider)
    if pconfig is None:
        logger.warning("resolve_provider_client: unknown provider %r", provider)
        return None, None

    if pconfig.auth_type == "api_key":
        if provider == "anthropic":
            client, default_model = _try_anthropic()
            if client is None:
                logger.warning(
                    "resolve_provider_client: anthropic requested but no Anthropic credentials found"
                )
                return None, None
            final_model = model or default_model
            return _to_async_client(client, final_model) if async_mode else (client, final_model)

        # Find the first configured API key
        api_key = ""
        for env_var in pconfig.api_key_env_vars:
            api_key = os.getenv(env_var, "").strip()
            if api_key:
                break
        if not api_key:
            logger.warning(
                "resolve_provider_client: provider %s has no API key configured (tried: %s)",
                provider,
                ", ".join(pconfig.api_key_env_vars),
            )
            return None, None

        # Resolve base URL (env override → provider-specific logic → default)
        base_url_override = (
            os.getenv(pconfig.base_url_env_var, "").strip() if pconfig.base_url_env_var else ""
        )
        if provider == "kimi-coding":
            base_url = _resolve_kimi_base_url(
                api_key, pconfig.inference_base_url, base_url_override
            )
        elif base_url_override:
            base_url = base_url_override
        else:
            base_url = pconfig.inference_base_url

        default_model = _API_KEY_PROVIDER_AUX_MODELS.get(provider, "")
        final_model = model or default_model

        # Provider-specific headers
        headers = {}
        if "api.kimi.com" in base_url.lower():
            headers["User-Agent"] = "KimiCLI/1.0"

        client = OpenAI(
            api_key=api_key, base_url=base_url, **({"default_headers": headers} if headers else {})
        )
        logger.debug("resolve_provider_client: %s (%s)", provider, final_model)
        return _to_async_client(client, final_model) if async_mode else (client, final_model)

    elif pconfig.auth_type in ("oauth_device_code", "oauth_external"):
        # OAuth providers — route through their specific try functions
        if provider == "nous":
            return resolve_provider_client("nous", model, async_mode)
        if provider == "openai-codex":
            return resolve_provider_client("openai-codex", model, async_mode)
        # Other OAuth providers not directly supported
        logger.warning(
            "resolve_provider_client: OAuth provider %s not directly supported, try 'auto'",
            provider,
        )
        return None, None

    logger.warning(
        "resolve_provider_client: unhandled auth_type %s for %s", pconfig.auth_type, provider
    )
    return None, None


# ── Public API ──────────────────────────────────────────────────────────────


def get_text_auxiliary_client(task: str = "") -> tuple[OpenAI | None, str | None]:
    """Return (client, default_model_slug) for text-only auxiliary tasks.

    Args:
        task: Optional task name ("compression", "web_extract") to check
              for a task-specific provider override.

    Callers may override the returned model with a per-task env var
    (e.g. CONTEXT_COMPRESSION_MODEL, AUXILIARY_WEB_EXTRACT_MODEL).
    """
    provider, model, base_url, api_key = _resolve_task_provider_model(task or None)
    return resolve_provider_client(
        provider,
        model=model,
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )


def get_async_text_auxiliary_client(task: str = ""):
    """Return (async_client, model_slug) for async consumers.

    For standard providers returns (AsyncOpenAI, model). For Codex returns
    (AsyncCodexAuxiliaryClient, model) which wraps the Responses API.
    Returns (None, None) when no provider is available.
    """
    provider, model, base_url, api_key = _resolve_task_provider_model(task or None)
    return resolve_provider_client(
        provider,
        model=model,
        async_mode=True,
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )


def get_auxiliary_extra_body() -> dict:
    """Return extra_body kwargs for auxiliary API calls.

    Includes Nous Portal product tags when the auxiliary client is backed
    by Nous Portal. Returns empty dict otherwise.
    """
    return dict(NOUS_EXTRA_BODY) if auxiliary_is_nous else {}


def auxiliary_max_tokens_param(value: int) -> dict:
    """Return the correct max tokens kwarg for the auxiliary client's provider.

    OpenRouter and local models use 'max_tokens'. Direct OpenAI with newer
    models (gpt-4o, o-series, gpt-5+) requires 'max_completion_tokens'.
    The Codex adapter translates max_tokens internally, so we use max_tokens
    for it as well.
    """
    custom_base = _current_custom_base_url()
    or_key = os.getenv("OPENROUTER_API_KEY")
    # Only use max_completion_tokens for direct OpenAI custom endpoints
    if not or_key and _read_nous_auth() is None and "api.openai.com" in custom_base.lower():
        return {"max_completion_tokens": value}
    return {"max_tokens": value}


# ── Centralized LLM Call API ────────────────────────────────────────────────
#
# call_llm() and async_call_llm() own the full request lifecycle:
#   1. Resolve provider + model from task config (or explicit args)
#   2. Get or create a cached client for that provider
#   3. Format request args for the provider + model (max_tokens handling, etc.)
#   4. Make the API call
#   5. Return the response
#
# Every auxiliary LLM consumer should use these instead of manually
# constructing clients and calling .chat.completions.create().

# Client cache: (provider, async_mode, base_url, api_key) -> (client, default_model)
_client_cache: dict[tuple, tuple] = {}


@dataclass(frozen=True)
class AuxiliaryCallIdentity:
    """Carry the credential-free provider/model identity for one auxiliary call."""

    provider: str
    model: str


def _get_cached_client(
    provider: str,
    model: str = None,
    async_mode: bool = False,
    base_url: str = None,
    api_key: str = None,
) -> tuple[Any | None, str | None]:
    """Get or create a cached client for the given provider.

    Only explicit endpoint credentials are cached. Env/config-driven provider
    resolution is intentionally resolved fresh each call so auxiliary routing
    cannot be polluted by stale process-global state from earlier tasks/tests.
    """
    # Async clients are bound to the event loop that owns their connection
    # pool. Auxiliary callers commonly use short-lived ``asyncio.run`` loops,
    # so reusing one across calls both crosses loop boundaries and defers its
    # destructor until after the owning loop has closed.
    use_cache = not async_mode and bool((base_url or "").strip() or (api_key or "").strip())
    cache_key = (provider, async_mode, base_url or "", api_key or "")
    if use_cache and cache_key in _client_cache:
        cached_client, cached_default = _client_cache[cache_key]
        compatible_model = _compatible_explicit_model(provider, model)
        return cached_client, compatible_model or cached_default
    client, default_model = resolve_provider_client(
        provider,
        model,
        async_mode,
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )
    if use_cache and client is not None:
        _client_cache[cache_key] = (client, default_model)
    # ``resolve_provider_client`` already applied the explicit override or
    # rejected it as incompatible. Returning the raw input here would
    # resurrect a model slug that the provider deliberately replaced.
    return client, default_model


def _canonical_auxiliary_provider(provider: str, client: Any | None) -> str:
    """Return a stable credential-free provider label for telemetry."""
    normalized = str(provider or "auto").strip().lower() or "auto"
    if normalized == "main":
        return "custom"
    if normalized == "codex":
        return "openai-codex"
    if normalized != "auto":
        return normalized
    if isinstance(client, CodexAuxiliaryClient):
        return "openai-codex"
    if isinstance(client, AnthropicAuxiliaryClient):
        return "anthropic"

    base_url = str(getattr(client, "base_url", "") or "").lower()
    if "openrouter.ai" in base_url:
        return "openrouter"
    if "chatgpt.com/backend-api/codex" in base_url:
        return "openai-codex"
    if "api.anthropic.com" in base_url:
        return "anthropic"
    custom_base = _current_custom_base_url().lower()
    if custom_base and base_url.rstrip("/") == custom_base.rstrip("/"):
        return "custom"
    return "auto"


def resolve_auxiliary_call_identity(
    task: str = None,
    *,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
) -> AuxiliaryCallIdentity:
    """Resolve only the non-secret provider/model identity for an auxiliary call.

    This performs the same local configuration and client selection as
    ``call_llm`` without issuing a provider request. It is used after a failed
    isolated request so telemetry can identify the failing route without
    serializing endpoints or credentials.
    """
    resolved_provider, resolved_model, resolved_base_url, resolved_api_key = (
        _resolve_task_provider_model(task, provider, model, base_url, api_key)
    )
    client, final_model = _get_cached_client(
        resolved_provider,
        resolved_model,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
    )
    effective_provider = resolved_provider
    if client is None and resolved_provider != "openrouter" and not resolved_base_url:
        client, final_model = _get_cached_client(
            "openrouter",
            resolved_model or _OPENROUTER_MODEL,
        )
        if client is not None:
            effective_provider = "openrouter"
    return AuxiliaryCallIdentity(
        provider=_canonical_auxiliary_provider(effective_provider, client),
        model=str(final_model or resolved_model or "").strip(),
    )


def _resolve_task_provider_model(
    task: str = None,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
) -> tuple[str, str | None, str | None, str | None]:
    """Determine provider + model for a call.

    Priority:
      1. Explicit provider/model/base_url/api_key args (always win)
      2. Task env var overrides (AUXILIARY_{TASK}_*, CONTEXT_{TASK}_*)
      3. Task config file (auxiliary.{task}.* or compression.*)
      4. Fallback task env/config, for tasks with an explicit inheritance rule
      5. "auto" (full auto-detection chain)

    Returns (provider, model, base_url, api_key) where model may be None
    (use provider default). When base_url is set, provider is forced to
    "custom" and the task uses that direct endpoint.
    """
    config = {}
    task_config: dict[str, Any] = {}
    fallback_config: dict[str, Any] = {}
    fallback_task = _auxiliary_fallback_task(task)

    if task:
        try:
            config = _load_runtime_config()
        except Exception:
            config = {}

        task_config = _auxiliary_task_config(config, task)
        if fallback_task:
            fallback_config = _auxiliary_task_config(config, fallback_task)

        # Backwards compat: compression section has its own keys
        if task == "compression" and not _task_config_text(task_config, "provider"):
            comp = config.get("compression", {}) if isinstance(config, dict) else {}
            if isinstance(comp, dict):
                task_config = dict(task_config)
                task_config["provider"] = comp.get("summary_provider", "")

    task_env_model = _get_auxiliary_env_override(task, "MODEL") if task else None
    fallback_env_model = (
        _get_auxiliary_env_override(fallback_task, "MODEL") if fallback_task else None
    )
    resolved_model = (
        model
        or task_env_model
        or _task_config_text(task_config, "model")
        or fallback_env_model
        or _task_config_text(fallback_config, "model")
    )

    if base_url:
        return "custom", resolved_model, base_url, api_key
    if provider:
        return provider, resolved_model, base_url, api_key

    if task:
        task_env_base_url = _get_auxiliary_env_override(task, "BASE_URL")
        task_env_api_key = _get_auxiliary_env_override(task, "API_KEY")
        cfg_base_url = _task_config_text(task_config, "base_url")
        cfg_api_key = _task_config_text(task_config, "api_key")
        fallback_env_base_url = (
            _get_auxiliary_env_override(fallback_task, "BASE_URL") if fallback_task else None
        )
        fallback_env_api_key = (
            _get_auxiliary_env_override(fallback_task, "API_KEY") if fallback_task else None
        )
        fallback_cfg_base_url = _task_config_text(fallback_config, "base_url")
        fallback_cfg_api_key = _task_config_text(fallback_config, "api_key")

        if task_env_base_url:
            return (
                "custom",
                resolved_model,
                task_env_base_url,
                task_env_api_key or cfg_api_key or fallback_env_api_key or fallback_cfg_api_key,
            )

        task_env_provider = _get_auxiliary_env_override(task, "PROVIDER")
        if task_env_provider:
            normalized_env_provider = task_env_provider.strip().lower()
            if normalized_env_provider != "auto":
                return normalized_env_provider, resolved_model, None, None
            return "auto", resolved_model, None, None

        if cfg_base_url:
            return "custom", resolved_model, cfg_base_url, task_env_api_key or cfg_api_key
        cfg_provider = _task_config_text(task_config, "provider")
        if cfg_provider:
            if cfg_provider != "auto":
                return cfg_provider, resolved_model, None, None
            return "auto", resolved_model, None, None

        if fallback_task:
            if fallback_env_base_url:
                return (
                    "custom",
                    resolved_model,
                    fallback_env_base_url,
                    task_env_api_key or cfg_api_key or fallback_env_api_key or fallback_cfg_api_key,
                )

            fallback_env_provider = _get_auxiliary_env_override(fallback_task, "PROVIDER")
            if fallback_env_provider:
                normalized_fallback_provider = fallback_env_provider.strip().lower()
                if normalized_fallback_provider != "auto":
                    return normalized_fallback_provider, resolved_model, None, None
                return "auto", resolved_model, None, None

            if fallback_cfg_base_url:
                return (
                    "custom",
                    resolved_model,
                    fallback_cfg_base_url,
                    task_env_api_key or cfg_api_key or fallback_env_api_key or fallback_cfg_api_key,
                )
            fallback_cfg_provider = _task_config_text(fallback_config, "provider")
            if fallback_cfg_provider:
                if fallback_cfg_provider != "auto":
                    return fallback_cfg_provider, resolved_model, None, None
                return "auto", resolved_model, None, None
        return "auto", resolved_model, None, None

    return "auto", resolved_model, None, None


def _build_call_kwargs(
    provider: str,
    model: str,
    messages: list,
    temperature: float | None = None,
    max_tokens: int | None = None,
    tools: list | None = None,
    timeout: float = 30.0,
    extra_body: dict | None = None,
    base_url: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Build kwargs for .chat.completions.create() with model/provider adjustments."""
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "timeout": timeout,
    }

    if temperature is not None:
        kwargs["temperature"] = temperature

    if max_tokens is not None:
        # Codex adapter handles max_tokens internally; OpenRouter/Nous use max_tokens.
        # Direct OpenAI api.openai.com with newer models needs max_completion_tokens.
        if provider == "custom":
            custom_base = base_url or _current_custom_base_url()
            if "api.openai.com" in custom_base.lower():
                kwargs["max_completion_tokens"] = max_tokens
            else:
                kwargs["max_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

    if tools:
        kwargs["tools"] = tools

    # Provider-specific extra_body
    merged_extra = dict(extra_body or {})
    if reasoning_effort:
        custom_base = base_url or _current_custom_base_url()
        if provider in {"custom", "main"} and _is_rcp_base_url(custom_base):
            template_kwargs = dict(merged_extra.get("chat_template_kwargs") or {})
            normalized_effort = str(reasoning_effort).strip().lower()
            thinking_disabled = normalized_effort in {
                "off",
                "none",
                "disabled",
                "false",
            }
            template_kwargs["enable_thinking"] = not thinking_disabled
            merged_extra["chat_template_kwargs"] = template_kwargs
            if thinking_disabled:
                merged_extra.pop("reasoning_effort", None)
            else:
                merged_extra["reasoning_effort"] = _map_rcp_reasoning_effort(reasoning_effort)
    if provider == "nous" or auxiliary_is_nous:
        merged_extra.setdefault("tags", []).extend(["product=leanflow-agent"])
    if merged_extra:
        kwargs["extra_body"] = merged_extra

    return kwargs


# RCP (EPFL inference cluster) base-URL/reasoning-effort helpers live in
# agent/auxiliary_rcp.py. They are pure stdlib predicates with no auxiliary
# routing state, so they were extracted as a closed cluster and re-exported here
# — every importer and test keeps resolving auxiliary_client.<name>.
from agent.providers.auxiliary_rcp import (  # noqa: E402,F401
    _is_rcp_base_url,
    _map_rcp_reasoning_effort,
)


def _resolve_task_reasoning_effort(task: str = None) -> str | None:
    if not task:
        return None
    env_value = _get_auxiliary_env_override(task, "REASONING_EFFORT")
    if env_value:
        return env_value.strip()
    try:
        config = _load_runtime_config()
    except Exception:
        config = {}
    task_config = _auxiliary_task_config(config, task)
    value = _task_config_text(task_config, "reasoning_effort")
    if value:
        return value

    fallback_task = _auxiliary_fallback_task(task)
    if fallback_task:
        fallback_env_value = _get_auxiliary_env_override(fallback_task, "REASONING_EFFORT")
        if fallback_env_value:
            return fallback_env_value.strip()
        fallback_config = _auxiliary_task_config(config, fallback_task)
        fallback_value = _task_config_text(fallback_config, "reasoning_effort")
        if fallback_value:
            return fallback_value
    return _AUXILIARY_TASK_REASONING_DEFAULTS.get(str(task or "").strip())


def call_llm(
    task: str = None,
    *,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    messages: list,
    temperature: float = None,
    max_tokens: int = None,
    tools: list = None,
    timeout: float = 30.0,
    extra_body: dict = None,
    isolate: bool = False,
) -> Any:
    """Centralized synchronous LLM call.

    Resolves provider + model (from task config, explicit args, or auto-detect),
    handles auth, request formatting, and model-specific arg adjustments.

    Args:
        task: Auxiliary task name ("compression", "web_extract",
              "session_search", "mcp", "flush_memories").
              Reads provider:model from config/env. Ignored if provider is set.
        provider: Explicit provider override.
        model: Explicit model override.
        messages: Chat messages list.
        temperature: Sampling temperature (None = provider default).
        max_tokens: Max output tokens (handles max_tokens vs max_completion_tokens).
        tools: Tool definitions (for function calling).
        timeout: Request timeout in seconds.
        extra_body: Additional request body fields.
        isolate: Enforce the timeout in a disposable child process. Use this
            for synchronous control-plane calls whose provider SDK may outlive
            its transport timeout.

    Returns:
        Response object with .choices[0].message.content

    Raises:
        RuntimeError: If no provider is configured.
    """
    if isolate:
        if tools or extra_body:
            raise ValueError("isolated auxiliary text calls do not support tools or extra_body")
        # Import lazily because the isolated worker uses call_llm() for the
        # actual provider request. The worker does not set isolate=True, so the
        # child performs exactly one direct SDK call while the parent owns the
        # hard wall-clock deadline and process-tree cleanup.
        from agent.providers.isolated_auxiliary import run_isolated_auxiliary_text

        isolated = run_isolated_auxiliary_text(
            task=task,
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            messages=messages,
            timeout=timeout,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return SimpleNamespace(
            model=isolated.model,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=isolated.content),
                )
            ],
        )

    resolved_provider, resolved_model, resolved_base_url, resolved_api_key = (
        _resolve_task_provider_model(task, provider, model, base_url, api_key)
    )
    reasoning_effort = _resolve_task_reasoning_effort(task)

    client, final_model = _get_cached_client(
        resolved_provider,
        resolved_model,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
    )
    if client is None:
        # Fallback: try openrouter
        if resolved_provider != "openrouter" and not resolved_base_url:
            logger.warning("Provider %s unavailable, falling back to openrouter", resolved_provider)
            client, final_model = _get_cached_client(
                "openrouter", resolved_model or _OPENROUTER_MODEL
            )
    if client is None:
        raise RuntimeError(
            f"No LLM provider configured for task={task} provider={resolved_provider}. "
            f"Run: leanflow setup"
        )

    kwargs = _build_call_kwargs(
        resolved_provider,
        final_model,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        timeout=timeout,
        extra_body=extra_body,
        base_url=resolved_base_url,
        reasoning_effort=reasoning_effort,
    )

    # A tool-side helper invoked inside a background actor retains that actor's
    # lease. Main-thread manager/orchestrator/synthesis calls remain part of
    # the foreground control plane and must never wait for a long research job.
    with background_provider_lease(enabled=background_actor_context_active()):
        # Handle max_tokens vs max_completion_tokens retry under one lease.
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as first_err:
            err_str = str(first_err)
            if "max_tokens" in err_str or "unsupported_parameter" in err_str:
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = max_tokens
                return client.chat.completions.create(**kwargs)
            raise


async def async_call_llm(
    task: str = None,
    *,
    provider: str = None,
    model: str = None,
    base_url: str = None,
    api_key: str = None,
    messages: list,
    temperature: float = None,
    max_tokens: int = None,
    tools: list = None,
    timeout: float = 30.0,
    extra_body: dict = None,
) -> Any:
    """Centralized asynchronous LLM call.

    Same as call_llm() but async. See call_llm() for full documentation.
    """
    resolved_provider, resolved_model, resolved_base_url, resolved_api_key = (
        _resolve_task_provider_model(task, provider, model, base_url, api_key)
    )
    reasoning_effort = _resolve_task_reasoning_effort(task)

    client, final_model = _get_cached_client(
        resolved_provider,
        resolved_model,
        async_mode=True,
        base_url=resolved_base_url,
        api_key=resolved_api_key,
    )
    if client is None:
        if resolved_provider != "openrouter" and not resolved_base_url:
            logger.warning("Provider %s unavailable, falling back to openrouter", resolved_provider)
            client, final_model = _get_cached_client(
                "openrouter", resolved_model or _OPENROUTER_MODEL, async_mode=True
            )
    if client is None:
        raise RuntimeError(
            f"No LLM provider configured for task={task} provider={resolved_provider}. "
            f"Run: leanflow setup"
        )

    kwargs = _build_call_kwargs(
        resolved_provider,
        final_model,
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        tools=tools,
        timeout=timeout,
        extra_body=extra_body,
        base_url=resolved_base_url,
        reasoning_effort=reasoning_effort,
    )

    lease = await asyncio.to_thread(
        acquire_background_provider_lease,
        enabled=background_actor_context_active(),
    )
    try:
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as first_err:
            err_str = str(first_err)
            if "max_tokens" in err_str or "unsupported_parameter" in err_str:
                kwargs.pop("max_tokens", None)
                kwargs["max_completion_tokens"] = max_tokens
                return await client.chat.completions.create(**kwargs)
            raise
    finally:
        if lease is not None:
            lease.release()
        await _close_async_client(client)


async def _close_async_client(client: Any) -> None:
    """Close one uncached async auxiliary client in its owning event loop."""
    close = getattr(client, "close", None)
    if not callable(close):
        close = getattr(client, "aclose", None)
    if not callable(close):
        return
    try:
        outcome = close()
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        # Cleanup must never replace the response or provider exception that
        # the caller is already handling.
        logger.debug("Failed to close async auxiliary client", exc_info=True)
