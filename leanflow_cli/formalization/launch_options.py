"""Resolve independent reviewer settings without crossing native backend defaults."""

from __future__ import annotations

import os
from typing import Any

from leanflow_cli.runtime.runtime_provider import (
    resolve_requested_provider,
    resolve_runtime_provider,
)

REVIEW_EFFORTS = ("", "auto", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def reviewer_settings(
    author: dict[str, Any],
    *,
    provider: str = "",
    model: str = "",
    effort: str = "",
    correction_rounds: int | None = None,
) -> dict[str, str]:
    """Apply explicit review settings, inheriting author settings only on the same backend."""
    selected = provider or os.environ.get("LEANFLOW_FORMALIZATION_REVIEW_PROVIDER", "").strip()
    backend = resolve_requested_provider(selected or str(author["provider"]))
    same = backend == author["provider"]
    if not same:
        # Validate normal native routing, but a global author-model override is
        # not a default for another CLI. Empty values preserve that CLI's own config.
        resolve_runtime_provider(requested=backend)
    chosen_model = model or os.environ.get("LEANFLOW_FORMALIZATION_REVIEW_MODEL", "").strip()
    chosen_effort = effort or os.environ.get("LEANFLOW_FORMALIZATION_REVIEW_EFFORT", "").strip()
    if chosen_effort not in REVIEW_EFFORTS:
        raise ValueError(
            "Unsupported reviewer effort; choose auto, low, medium, high, xhigh, max, or ultra"
        )
    if correction_rounds is None:
        try:
            correction_rounds = int(os.environ.get("LEANFLOW_FORMALIZATION_CORRECTION_ROUNDS", "2"))
        except ValueError as exc:
            raise ValueError("Formalization correction rounds must be an integer") from exc
    if not 0 <= correction_rounds <= 8:
        raise ValueError("Formalization correction rounds must be between 0 and 8")
    return {
        "LEANFLOW_FORMALIZATION_REVIEW_PROVIDER": backend,
        "LEANFLOW_FORMALIZATION_REVIEW_MODEL": chosen_model
        or (str(author.get("model") or "") if same else ""),
        "LEANFLOW_FORMALIZATION_REVIEW_EFFORT": (
            ""
            if chosen_effort == "auto"
            else chosen_effort or (str(author.get("reasoning_effort") or "") if same else "")
        ),
        "LEANFLOW_FORMALIZATION_CORRECTION_ROUNDS": str(correction_rounds),
    }
