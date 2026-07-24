"""Parse provider reset windows without trusting unbounded error payloads."""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_PROVIDER_RESET_SECONDS = 31 * 24 * 60 * 60
MAX_PROVIDER_RESET_WAIT_SECONDS = 60 * 60
DEFAULT_PROVIDER_RESET_WAIT_SECONDS = 15 * 60
UNKNOWN_PROVIDER_RESET_SECONDS = 60 * 60
PROVIDER_RESET_SAFETY_SECONDS = 1
_TIMING_TOLERANCE_SECONDS = 5

_USAGE_LIMIT_TYPE_RE = re.compile(
    r"['\"]type['\"]\s*:\s*['\"]usage_limit_reached['\"]",
    re.IGNORECASE,
)
_RESETS_IN_RE = re.compile(
    r"['\"]resets_in_seconds['\"]\s*:\s*([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_RESETS_AT_RE = re.compile(
    r"['\"]resets_at['\"]\s*:\s*([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProviderUsageLimit:
    """Describe one bounded provider usage-limit reset window."""

    retry_after_seconds: int
    unavailable_until_epoch: int
    resets_at_epoch: int
    reported_resets_in_seconds: int
    timing_consistent: bool
    timing_clamped: bool
    source: str
    kind: str = "usage_limit_reached"

    def to_mapping(self) -> dict[str, Any]:
        """Return stable JSON-compatible retry metadata for results and events."""
        return {
            "kind": self.kind,
            "retry_after_seconds": self.retry_after_seconds,
            "unavailable_until_epoch": self.unavailable_until_epoch,
            "resets_at_epoch": self.resets_at_epoch,
            "reported_resets_in_seconds": self.reported_resets_in_seconds,
            "timing_consistent": self.timing_consistent,
            "timing_clamped": self.timing_clamped,
            "source": self.source,
        }


def _finite_nonnegative_number(value: Any) -> float | None:
    """Return one finite nonnegative numeric payload or reject it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, str) and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", value.strip()):
        try:
            candidate = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(candidate) or candidate < 0:
        return None
    return candidate


def _usage_limit_mapping(value: Any, *, depth: int = 0) -> Mapping[str, Any] | None:
    """Find a usage-limit object in a shallow structured exception payload."""
    if depth > 3 or not isinstance(value, Mapping):
        return None
    if str(value.get("type", "") or "").strip().lower() == "usage_limit_reached":
        return value
    for key in ("error", "body", "detail"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            found = _usage_limit_mapping(nested, depth=depth + 1)
            if found is not None:
                return found
    return None


def _structured_usage_limit(error: BaseException | Any) -> tuple[Mapping[str, Any], str] | None:
    """Return the provider's structured usage-limit body when exposed."""
    for attribute, source in (
        ("body", "exception.body"),
        ("error", "exception.error"),
    ):
        found = _usage_limit_mapping(getattr(error, attribute, None))
        if found is not None:
            return found, source
    for argument in getattr(error, "args", ()) or ():
        found = _usage_limit_mapping(argument)
        if found is not None:
            return found, "exception.args"
    return None


def _string_usage_limit(error: BaseException | Any) -> tuple[dict[str, Any], str] | None:
    """Parse the narrow compatible-provider string fallback without evaluation."""
    text = str(error or "")
    if not _USAGE_LIMIT_TYPE_RE.search(text):
        return None
    payload: dict[str, Any] = {"type": "usage_limit_reached"}
    resets_in = _RESETS_IN_RE.search(text)
    resets_at = _RESETS_AT_RE.search(text)
    if resets_in is not None:
        payload["resets_in_seconds"] = resets_in.group(1)
    if resets_at is not None:
        payload["resets_at"] = resets_at.group(1)
    return payload, "exception.string"


def extract_provider_usage_limit(
    error: BaseException | Any,
    *,
    now_epoch: float | None = None,
) -> ProviderUsageLimit | None:
    """Extract and cross-check a bounded usage-limit reset from one error.

    Structured exception bodies outrank the string fallback. When both reset
    fields exist, the later window wins so contradictory data cannot trigger
    an early retry. Every automatic delay is capped to keep hostile or corrupt
    provider payloads from creating an unbounded sleep.
    """
    observed = _structured_usage_limit(error) or _string_usage_limit(error)
    if observed is None:
        return None
    payload, source = observed
    now = float(time.time() if now_epoch is None else now_epoch)
    reported_in = _finite_nonnegative_number(payload.get("resets_in_seconds"))
    reported_at = _finite_nonnegative_number(payload.get("resets_at"))
    remaining_at = max(0.0, reported_at - now) if reported_at is not None else None
    candidates = [value for value in (reported_in, remaining_at) if value is not None]

    if not candidates:
        raw_delay = float(UNKNOWN_PROVIDER_RESET_SECONDS)
        timing_consistent = False
        timing_clamped = True
    else:
        raw_delay = max(candidates)
        timing_consistent = len(candidates) == 1 or (
            abs(candidates[0] - candidates[1]) <= _TIMING_TOLERANCE_SECONDS
        )
        timing_clamped = raw_delay + PROVIDER_RESET_SAFETY_SECONDS > (MAX_PROVIDER_RESET_SECONDS)

    retry_after = min(
        MAX_PROVIDER_RESET_SECONDS,
        max(1, int(math.ceil(raw_delay)) + PROVIDER_RESET_SAFETY_SECONDS),
    )
    unavailable_until = int(math.ceil(now + retry_after))
    bounded_resets_at = (
        int(math.ceil(reported_at))
        if reported_at is not None and reported_at <= now + MAX_PROVIDER_RESET_SECONDS
        else 0
    )
    bounded_reported_in = (
        int(math.ceil(reported_in))
        if reported_in is not None and reported_in <= MAX_PROVIDER_RESET_SECONDS
        else (MAX_PROVIDER_RESET_SECONDS if reported_in is not None else 0)
    )
    return ProviderUsageLimit(
        retry_after_seconds=retry_after,
        unavailable_until_epoch=unavailable_until,
        resets_at_epoch=bounded_resets_at,
        reported_resets_in_seconds=bounded_reported_in,
        timing_consistent=timing_consistent,
        timing_clamped=timing_clamped,
        source=source,
    )


def provider_reset_wait_max_seconds() -> int:
    """Return the bounded window LeanFlow may wait in-process for a reset."""
    raw = str(
        os.getenv(
            "LEANFLOW_PROVIDER_RESET_MAX_WAIT_SECONDS",
            str(DEFAULT_PROVIDER_RESET_WAIT_SECONDS),
        )
        or DEFAULT_PROVIDER_RESET_WAIT_SECONDS
    ).strip()
    try:
        value = int(float(raw))
    except (OverflowError, TypeError, ValueError):
        value = DEFAULT_PROVIDER_RESET_WAIT_SECONDS
    return min(MAX_PROVIDER_RESET_WAIT_SECONDS, max(0, value))


def normalize_provider_retry_after(
    value: Any,
    *,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Return canonical bounded retry metadata or an empty mapping.

    The absolute unavailable-until epoch is the durable authority. The
    relative delay is retained as observation metadata and never used to
    extend that deadline when a subprocess result is harvested later.
    """
    if not isinstance(value, Mapping):
        return {}
    if str(value.get("kind", "") or "").strip().lower() != "usage_limit_reached":
        return {}
    retry_after = _finite_nonnegative_number(value.get("retry_after_seconds"))
    unavailable_until = _finite_nonnegative_number(value.get("unavailable_until_epoch"))
    if retry_after is None or unavailable_until is None or unavailable_until <= 0:
        return {}
    now = float(time.time() if now_epoch is None else now_epoch)
    max_unavailable_until = int(math.ceil(now + MAX_PROVIDER_RESET_SECONDS))
    if unavailable_until > max_unavailable_until:
        return {}
    resets_at = _finite_nonnegative_number(value.get("resets_at_epoch"))
    reported_in = _finite_nonnegative_number(value.get("reported_resets_in_seconds"))
    return {
        "kind": "usage_limit_reached",
        "retry_after_seconds": min(
            MAX_PROVIDER_RESET_SECONDS,
            max(1, int(math.ceil(retry_after))),
        ),
        "unavailable_until_epoch": int(math.ceil(unavailable_until)),
        "resets_at_epoch": int(math.ceil(resets_at)) if resets_at is not None else 0,
        "reported_resets_in_seconds": (
            min(MAX_PROVIDER_RESET_SECONDS, int(math.ceil(reported_in)))
            if reported_in is not None
            else 0
        ),
        "timing_consistent": bool(value.get("timing_consistent")),
        "timing_clamped": bool(value.get("timing_clamped")),
        "source": str(value.get("source", "") or "")[:80],
    }
