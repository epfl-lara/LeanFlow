"""Track cost provenance and request coverage without guessing model-family prices."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from agent.accounting.usage_pricing import MODEL_PRICING


def _amount(value: Any) -> float | None:
    """Accept only finite, nonnegative monetary values."""
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


def combine_sources(sources: list[str]) -> str:
    """Label a subtotal by its actual contributing cost sources."""
    known = set(sources) - {"", "unavailable"}
    return next(iter(known)) if len(known) == 1 else "mixed" if known else "unavailable"


@dataclass
class CostLedger:
    """Account each response separately; failed or unpriced requests remain uncovered."""

    known_usd: float = 0.0
    costed_calls: int = 0
    sources: list[str] = field(default_factory=list)

    def record(self, usage: Any, *, model: str, provider: str = "") -> None:
        """Record reported cost or an exact-model estimate, never a family fallback."""

        def get(key: str) -> Any:
            return usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)

        amount, source = None, "unavailable"
        for key in (
            "cost_usd",
            "total_cost_usd",
            "total_cost",
            "cost",
            "estimated_cost_usd",
            "estimated_cost",
        ):
            amount = _amount(get(key))
            if amount is not None:
                source = (
                    "provider_estimated" if key.startswith("estimated") else "provider_reported"
                )
                break
        if amount is None and provider != "openai-codex":
            # A subscription transport has no per-token API bill. Unknown variants
            # such as gpt-5.6-terra must not inherit the gpt-5 entry either.
            bare = model.split("/")[-1].lower()
            table = {key.lower(): value for key, value in MODEL_PRICING.items()}
            pricing = table.get(bare) or table.get(re.sub(r"-\d{4}-\d{2}-\d{2}$", "", bare))
            inp = get("input_tokens") if get("input_tokens") is not None else get("prompt_tokens")
            out = (
                get("output_tokens")
                if get("output_tokens") is not None
                else get("completion_tokens")
            )
            if (
                pricing
                and any(value > 0 for value in pricing.values())
                and all(
                    isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (inp, out)
                )
            ):
                amount = (inp * pricing["input"] + out * pricing["output"]) / 1_000_000
                source = "estimated"
        if amount is not None:
            self.known_usd += amount
            self.costed_calls += 1
            self.sources.append(source)

    def snapshot(self, calls: int) -> dict[str, Any]:
        """Describe the known subtotal and whether every admitted request is covered."""
        return {
            "cost_usd": self.known_usd if self.costed_calls else None,
            "costed_api_calls": self.costed_calls,
            "cost_source": combine_sources(self.sources),
            "cost_complete": self.costed_calls == calls,
        }


def merge_usage(job: dict[str, Any], usage: dict[str, Any]) -> None:
    """Merge invocation counters with retained usage exactly once, including unknown costs."""
    for field_name in ("input_tokens", "output_tokens"):
        if isinstance(usage.get(field_name), int):
            job[field_name] = max(
                int(job.get(field_name, 0) or 0),
                int(job.get("previous_" + field_name, 0) or 0) + usage[field_name],
            )
    if "cost_usd" in usage:
        previous, current = _amount(job.get("previous_cost_usd")), _amount(usage["cost_usd"])
        job["cost_usd"] = (
            None if previous is None and current is None else (previous or 0) + (current or 0)
        )
        job["costed_api_calls"] = int(job.get("previous_costed_api_calls", 0) or 0) + int(
            usage.get("costed_api_calls", 0) or 0
        )
        job["cost_source"] = combine_sources(
            [
                str(job.get("previous_cost_source") or "unavailable"),
                str(usage.get("cost_source") or "unavailable"),
            ]
        )
        job["cost_complete"] = job["costed_api_calls"] == int(job.get("api_calls", 0))


def aggregate_cost(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep unknown coverage and legacy totals distinct from a complete cost."""
    known = [_amount(job.get("cost_usd")) for job in jobs]
    values = [value for value in known if value is not None]
    costed = sum(int(job.get("costed_api_calls", 0) or 0) for job in jobs)
    calls = sum(int(job.get("api_calls", 0) or 0) for job in jobs)
    return {
        "cost_usd": sum(values) if values else None,
        "cost_source": combine_sources(
            [str(job.get("cost_source") or "unavailable") for job in jobs]
        ),
        "costed_api_calls": costed,
        "cost_complete": bool(jobs)
        and costed == calls
        and all(
            int(job.get("api_calls", 0) or 0) == 0 or _amount(job.get("cost_usd")) is not None
            for job in jobs
        ),
    }


def usage_checkpoint(
    previous: dict[str, Any], current: dict[str, Any], calls: int
) -> dict[str, Any]:
    """Persist cumulative usage alongside admission, independent of observer flush timing."""
    combined = {"previous_" + key: value for key, value in previous.items()}
    combined["api_calls"] = calls
    merge_usage(combined, current)
    return {key: value for key, value in combined.items() if not key.startswith("previous_")}
