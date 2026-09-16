"""Resolve validated model context limits and proactive compaction budgets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_OUTPUT_TOKENS = 65536


def is_context_error(error: Exception) -> bool:
    """Recognize provider context rejections so reconnect logic cannot retry them."""
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "context_length_exceeded",
            "context_window_exceeded",
            "maximum context length",
            "prompt is too long",
            "input is too long",
            "exceeds the context window",
        )
    )


def validate_context_settings(settings: Mapping[str, Any]) -> None:
    """Reject invalid context caps and compression fractions before dispatch."""
    for key in ("context_tokens", "max_output_tokens"):
        if key in settings and (type(settings[key]) is not int or settings[key] < 1):
            raise ValueError(f"{key} must be a positive integer")
    for key in ("compression_threshold", "compression_target"):
        if key not in settings:
            continue
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
            raise ValueError(f"{key} must be a fraction strictly between 0 and 1")


def validate_model_contexts(value: Any) -> None:
    """Validate exact model overrides, rejecting misspelled or unused settings."""
    if not isinstance(value, dict):
        raise ValueError("model_contexts must be a JSON object keyed by exact model name")
    for model, settings in value.items():
        if not isinstance(model, str) or not model.strip() or not isinstance(settings, dict):
            raise ValueError("model_contexts requires nonempty model names and settings objects")
        if set(settings) - {
            "context_tokens",
            "compression_threshold",
            "compression_target",
            "max_output_tokens",
        }:
            raise ValueError(f"Unknown context setting for model {model}")
        validate_context_settings(settings)


@dataclass(frozen=True)
class ContextBudget:
    """Reserve output and compact below the input ceiling for every role."""

    context_tokens: int
    output_tokens: int
    input_limit: int
    trigger_tokens: int
    target_tokens: int

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> ContextBudget:
        """Resolve an explicit role/model configuration without provider calls."""
        validate_context_settings(config)
        context = int(config.get("context_tokens", 256000))
        # Keep small test/operator windows usable. Normal 256K windows receive
        # the full 64Ki output allowance, with the same reserve on the input side.
        default_output = (
            DEFAULT_OUTPUT_TOKENS
            if context >= 256000
            else min(DEFAULT_OUTPUT_TOKENS, max(1, context // 4))
        )
        output = int(config.get("max_output_tokens", default_output))
        if "max_output_tokens" in config and output >= context:
            raise ValueError("max_output_tokens must be smaller than context_tokens")
        limit = max(1, context - output)
        # Keep the operator's fraction of the total window, but never let its
        # output reservation collapse the soft trigger onto the hard ceiling.
        trigger = max(
            1,
            min(int(limit * 0.9), int(context * float(config.get("compression_threshold", 0.75)))),
        )
        target = max(1, int(trigger * float(config.get("compression_target", 0.5))))
        return cls(context, output, limit, trigger, target)
