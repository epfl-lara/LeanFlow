"""Unified model-capabilities façade: context/metadata + pricing.

``agent.model_metadata`` (context lengths, /models metadata, token estimation)
and ``agent.usage_pricing`` (per-token pricing + cost estimation) are two halves
of one "what can this model do, and what does it cost" concern. Both normalize a
model name before looking it up.

This module is the unified façade over both:

  * It exposes the single canonical generic model-name normalizer as
    ``normalize_model_key`` — re-exported from ``model_metadata._normalize_model_name``,
    which is the one implementation used for case-insensitive context-length
    lookups. (``usage_pricing.get_pricing`` keeps its own provider-prefix-stripping
    normalization — ``model.split("/")[-1].lower()`` — because that is a
    semantically different, behaviour-load-bearing step, not a duplicate of the
    generic normalizer; and ``agent.anthropic_adapter.normalize_model_name`` is a
    third, Anthropic-slug-specific normalizer that is unrelated to either.)

  * It re-exports the public API of BOTH modules so callers can reach the whole
    "model capabilities" surface from one place, while every existing import
    (``from agent.accounting.usage_pricing import estimate_cost_usd, ...`` and
    ``from agent.providers.model_metadata import ...``) keeps working unchanged.

Import direction (no cycle): this façade imports FROM ``model_metadata`` and
``usage_pricing``; neither of those imports this module back, so pulling in
``model_capabilities`` is always safe.
"""

from __future__ import annotations

from agent.accounting.usage_pricing import (
    DEFAULT_PRICING,
    MODEL_PRICING,
    estimate_cost_usd,
    format_duration_compact,
    format_token_count_compact,
    get_pricing,
    has_known_pricing,
)
from agent.providers.model_metadata import (
    CONTEXT_PROBE_TIERS,
    DEFAULT_CONTEXT_LENGTHS,
    UNKNOWN_CONTEXT_LENGTH_FALLBACK,
    estimate_messages_tokens_rough,
    estimate_tokens_rough,
    fetch_model_metadata,
    fetch_provider_model_metadata,
    get_cached_context_length,
    get_model_context_length,
    get_next_probe_tier,
    parse_context_limit_from_error,
    save_context_length,
)
from agent.providers.model_metadata import (
    _normalize_model_name as normalize_model_key,
)

__all__ = [
    "normalize_model_key",
    # model_metadata
    "CONTEXT_PROBE_TIERS",
    "DEFAULT_CONTEXT_LENGTHS",
    "UNKNOWN_CONTEXT_LENGTH_FALLBACK",
    "estimate_messages_tokens_rough",
    "estimate_tokens_rough",
    "fetch_model_metadata",
    "fetch_provider_model_metadata",
    "get_cached_context_length",
    "get_model_context_length",
    "get_next_probe_tier",
    "parse_context_limit_from_error",
    "save_context_length",
    # usage_pricing
    "DEFAULT_PRICING",
    "MODEL_PRICING",
    "estimate_cost_usd",
    "format_duration_compact",
    "format_token_count_compact",
    "get_pricing",
    "has_known_pricing",
]
