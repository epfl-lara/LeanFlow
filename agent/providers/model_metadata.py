"""Model metadata, context lengths, and token estimation utilities.

Pure utility functions with no AIAgent dependency. Used by ContextCompressor
and run_agent.py for pre-flight context checks.
"""

import logging
import re
import time
from pathlib import Path
from typing import Any

import requests
import yaml

from core.constants import OPENROUTER_MODELS_URL
from core.home import leanflow_home

logger = logging.getLogger(__name__)

_model_metadata_cache: dict[str, dict[str, Any]] = {}
_model_metadata_cache_time: float = 0
_provider_model_metadata_cache: dict[str, dict[str, dict[str, Any]]] = {}
_provider_model_metadata_cache_time: dict[str, float] = {}
_MODEL_CACHE_TTL = 3600
UNKNOWN_CONTEXT_LENGTH_FALLBACK = 200_000

# Descending tiers for context length probing when the model is unknown.
# We start high and step down on context-length errors until one works.
CONTEXT_PROBE_TIERS = [
    2_000_000,
    1_000_000,
    512_000,
    200_000,
    128_000,
    64_000,
    32_000,
]

DEFAULT_CONTEXT_LENGTHS = {
    "anthropic/claude-opus-4": 200000,
    "anthropic/claude-opus-4.5": 200000,
    "anthropic/claude-opus-4.6": 200000,
    "anthropic/claude-sonnet-4": 200000,
    "anthropic/claude-sonnet-4-20250514": 200000,
    "anthropic/claude-haiku-4.5": 200000,
    # Bare Anthropic model IDs (for native API provider)
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-opus-4-5-20251101": 200000,
    "claude-sonnet-4-5-20250929": 200000,
    "claude-opus-4-1-20250805": 200000,
    "claude-opus-4-20250514": 200000,
    "claude-sonnet-4-20250514": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "openai/gpt-4o": 128000,
    "openai/gpt-4-turbo": 128000,
    "openai/gpt-4o-mini": 128000,
    "google/gemini-2.0-flash": 1048576,
    "google/gemini-2.5-pro": 1048576,
    "meta-llama/llama-3.3-70b-instruct": 131072,
    "deepseek/deepseek-chat-v3": 65536,
    "qwen/qwen-2.5-72b-instruct": 32768,
    "glm-4.7": 202752,
    "glm-5": 202752,
    "glm-5.1": 200000,
    "zai-org/glm-5.1": 200000,
    "glm-4.5": 131072,
    "glm-4.5-flash": 131072,
    "kimi-for-coding": 262144,
    "kimi-k2.5": 262144,
    "kimi-k2-thinking": 262144,
    "kimi-k2-thinking-turbo": 262144,
    "moonshotai/kimi-k2.6": 262144,
    "moonshotai/kimi-k2.6-int4": 262144,
    "kimi-k2-turbo-preview": 262144,
    "kimi-k2-0905-preview": 131072,
    "MiniMax-M2.5": 204800,
    "MiniMax-M2.5-highspeed": 204800,
    "MiniMax-M2.1": 204800,
}


def fetch_model_metadata(force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """Fetch model metadata from OpenRouter (cached for 1 hour)."""
    global _model_metadata_cache, _model_metadata_cache_time

    if (
        not force_refresh
        and _model_metadata_cache
        and (time.time() - _model_metadata_cache_time) < _MODEL_CACHE_TTL
    ):
        return _model_metadata_cache

    try:
        response = requests.get(OPENROUTER_MODELS_URL, timeout=10)
        response.raise_for_status()
        data = response.json()

        cache = {}
        for model in data.get("data", []):
            model_id = model.get("id", "")
            cache[model_id] = {
                "context_length": model.get("context_length", 128000),
                "max_completion_tokens": model.get("top_provider", {}).get(
                    "max_completion_tokens", 4096
                ),
                "name": model.get("name", model_id),
                "pricing": model.get("pricing", {}),
            }
            canonical = model.get("canonical_slug", "")
            if canonical and canonical != model_id:
                cache[canonical] = cache[model_id]

        _model_metadata_cache = cache
        _model_metadata_cache_time = time.time()
        logger.debug("Fetched metadata for %s models from OpenRouter", len(cache))
        return cache

    except Exception as e:
        logging.warning(f"Failed to fetch model metadata from OpenRouter: {e}")
        return _model_metadata_cache or {}


def _normalize_model_name(model: str) -> str:
    """Canonical generic model-name normalization: trimmed + lowercased.

    Single source of truth for case-insensitive model lookups. Re-exported by
    agent.model_capabilities as ``normalize_model_key`` (the unified
    model-capabilities façade), so context and pricing code share one
    implementation without an import cycle (the façade imports this; this module
    never imports the façade).
    """
    return str(model or "").strip().lower()


def _extract_context_length_from_entry(entry: dict[str, Any] | None) -> int | None:
    if not isinstance(entry, dict):
        return None
    candidates = [entry]
    for key in ("metadata", "capabilities", "top_provider"):
        nested = entry.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for candidate in candidates:
        for key in (
            "context_length",
            "max_context_length",
            "input_token_limit",
            "max_input_tokens",
            "max_model_len",
            "n_ctx",
            "context_window",
        ):
            value = candidate.get(key)
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
    return None


def _lookup_metadata_context_length(
    metadata: dict[str, dict[str, Any]],
    model: str,
) -> tuple[bool, int | None]:
    if not metadata:
        return False, None
    normalized_model = _normalize_model_name(model)
    if model in metadata:
        return True, _extract_context_length_from_entry(metadata.get(model))
    for key, value in metadata.items():
        if _normalize_model_name(key) == normalized_model:
            return True, _extract_context_length_from_entry(value)
    return False, None


def _lookup_default_context_length(model: str) -> int | None:
    normalized_model = _normalize_model_name(model)
    for default_model, length in DEFAULT_CONTEXT_LENGTHS.items():
        if _normalize_model_name(default_model) == normalized_model:
            return length
    for default_model, length in DEFAULT_CONTEXT_LENGTHS.items():
        normalized_default = _normalize_model_name(default_model)
        if normalized_default in normalized_model or normalized_model in normalized_default:
            return length
    return None


def _lookup_configured_context_length(model: str) -> int | None:
    try:
        from leanflow_cli.config import load_config

        config = load_config()
    except Exception:
        return None
    model_cfg = config.get("model") if isinstance(config, dict) else {}
    if not isinstance(model_cfg, dict):
        return None
    configured = model_cfg.get("context_lengths")
    if not isinstance(configured, dict):
        return None
    matched, length = _lookup_metadata_context_length(
        {str(key): {"context_length": value} for key, value in configured.items()},
        model,
    )
    return length if matched else None


def _should_use_openrouter_metadata(base_url: str) -> bool:
    normalized = str(base_url or "").strip().lower()
    return not normalized or "openrouter.ai" in normalized


def fetch_provider_model_metadata(
    base_url: str,
    api_key: str = "",
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch and cache model metadata from a custom base_url endpoint's /models API. Handles varying response formats (data/models keys), extracts context lengths, and returns normalized model lookups; caches per-provider with 1-hour TTL."""
    normalized_base_url = str(base_url or "").strip().rstrip("/")
    if not normalized_base_url:
        return {}

    cached = _provider_model_metadata_cache.get(normalized_base_url, {})
    cached_time = float(_provider_model_metadata_cache_time.get(normalized_base_url, 0) or 0)
    if not force_refresh and cached and (time.time() - cached_time) < _MODEL_CACHE_TTL:
        return cached

    url = f"{normalized_base_url}/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = requests.get(url, headers=headers or None, timeout=10)
        response.raise_for_status()
        data = response.json()

        models = data.get("data")
        if not isinstance(models, list):
            models = data.get("models")
        if not isinstance(models, list):
            models = []

        cache: dict[str, dict[str, Any]] = {}
        for entry in models:
            if not isinstance(entry, dict):
                continue
            model_id = str(
                entry.get("id", "") or entry.get("model", "") or entry.get("name", "") or ""
            ).strip()
            if not model_id:
                continue
            context_length = _extract_context_length_from_entry(entry)
            payload = {"context_length": context_length} if context_length else dict(entry)
            cache[model_id] = payload
            normalized = _normalize_model_name(model_id)
            if normalized and normalized != model_id:
                cache.setdefault(normalized, payload)

        _provider_model_metadata_cache[normalized_base_url] = cache
        _provider_model_metadata_cache_time[normalized_base_url] = time.time()
        return cache
    except Exception as e:
        logger.debug("Failed to fetch provider model metadata from %s: %s", url, e)
        return cached or {}


def _get_context_cache_path() -> Path:
    """Return path to the persistent context length cache file."""
    return leanflow_home() / "context_length_cache.yaml"


def _load_context_cache() -> dict[str, int]:
    """Load the model+provider → context_length cache from disk."""
    path = _get_context_cache_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("context_lengths", {})
    except Exception as e:
        logger.debug("Failed to load context length cache: %s", e)
        return {}


def save_context_length(model: str, base_url: str, length: int) -> None:
    """Persist a discovered context length for a model+provider combo.

    Cache key is ``model@base_url`` so the same model name served from
    different providers can have different limits.
    """
    key = f"{model}@{base_url}"
    cache = _load_context_cache()
    if cache.get(key) == length:
        return  # already stored
    cache[key] = length
    path = _get_context_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            yaml.dump({"context_lengths": cache}, f, default_flow_style=False)
        logger.info("Cached context length %s → %s tokens", key, f"{length:,}")
    except Exception as e:
        logger.debug("Failed to save context length cache: %s", e)


def get_cached_context_length(model: str, base_url: str) -> int | None:
    """Look up a previously discovered context length for model+provider."""
    key = f"{model}@{base_url}"
    cache = _load_context_cache()
    return cache.get(key)


def get_next_probe_tier(current_length: int) -> int | None:
    """Return the next lower probe tier, or None if already at minimum."""
    for tier in CONTEXT_PROBE_TIERS:
        if tier < current_length:
            return tier
    return None


def parse_context_limit_from_error(error_msg: str) -> int | None:
    """Try to extract the actual context limit from an API error message.

    Many providers include the limit in their error text, e.g.:
      - "maximum context length is 32768 tokens"
      - "context_length_exceeded: 131072"
      - "Maximum context size 32768 exceeded"
      - "model's max context length is 65536"
    """
    error_lower = error_msg.lower()
    # Pattern: look for numbers near context-related keywords
    patterns = [
        r"(?:max(?:imum)?|limit)\s*(?:context\s*)?(?:length|size|window)?\s*(?:is|of|:)?\s*(\d{4,})",
        r"context\s*(?:length|size|window)\s*(?:is|of|:)?\s*(\d{4,})",
        r"(\d{4,})\s*(?:token)?\s*(?:context|limit)",
        r">\s*(\d{4,})\s*(?:max|limit|token)",  # "250000 tokens > 200000 maximum"
        r"(\d{4,})\s*(?:max(?:imum)?)\b",  # "200000 maximum"
    ]
    for pattern in patterns:
        match = re.search(pattern, error_lower)
        if match:
            limit = int(match.group(1))
            # Sanity check: must be a reasonable context length
            if 1024 <= limit <= 10_000_000:
                return limit
    return None


def get_model_context_length(model: str, base_url: str = "", api_key: str = "") -> int:
    """Get the context length for a model.

    Resolution order:
    1. Persistent cache (previously discovered via probing)
    2. User-configured model.context_lengths overrides
    3. Provider /models metadata (when base_url is set)
    4. Hardcoded DEFAULT_CONTEXT_LENGTHS (case-insensitive exact/fuzzy match)
    5. OpenRouter API metadata (only for OpenRouter/no-endpoint routes)
    6. Conservative unknown-model fallback (200k)
    """
    # 1. Check persistent cache (model+provider)
    if base_url:
        cached = get_cached_context_length(model, base_url)
        if cached is not None:
            return cached

    # 2. Explicit user overrides in ~/.leanflow/config.yaml.
    configured_length = _lookup_configured_context_length(model)
    if configured_length is not None:
        return configured_length

    # 3. Provider /models metadata for the active route
    if base_url:
        provider_metadata = fetch_provider_model_metadata(base_url, api_key=api_key)
        matched, provider_length = _lookup_metadata_context_length(provider_metadata, model)
        if matched and provider_length is not None:
            return provider_length

    # 4. Hardcoded defaults (case-insensitive exact match first, then fuzzy match)
    default_length = _lookup_default_context_length(model)
    if default_length is not None:
        return default_length

    # 5. OpenRouter metadata is only authoritative for OpenRouter/no-endpoint
    # routes. A custom endpoint may serve a model id with a different window,
    # and should use config/provider metadata or conservative fallback instead.
    if _should_use_openrouter_metadata(base_url):
        metadata = fetch_model_metadata()
        matched, metadata_length = _lookup_metadata_context_length(metadata, model)
        if matched and metadata_length is not None:
            return metadata_length

    # 6. Unknown model — be conservative rather than optimistic
    return UNKNOWN_CONTEXT_LENGTH_FALLBACK


def estimate_tokens_rough(text: str) -> int:
    """Rough token estimate (~4 chars/token) for pre-flight checks."""
    if not text:
        return 0
    return len(text) // 4


def estimate_messages_tokens_rough(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate for a message list (pre-flight only)."""
    total_chars = sum(len(str(msg)) for msg in messages)
    return total_chars // 4
