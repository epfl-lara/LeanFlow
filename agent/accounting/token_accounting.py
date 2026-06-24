"""Token and cost accounting for an agent session.

``TokenAccounter`` owns the cumulative token/cost counters that were previously
held directly on ``AIAgent``. It is a self-contained collaborator: it does not
import ``run_agent`` (avoiding an import cycle) and never reaches back into the
agent. Agent-owned values such as the model name are passed in as parameters.

The public counter attributes keep the exact names AIAgent used, so AIAgent can
expose them via thin delegating ``@property`` shims and external/test access is
unchanged.
"""

from __future__ import annotations

from typing import Any

from agent.accounting.usage_pricing import estimate_cost_usd, has_known_pricing


class TokenAccounter:
    """Owns the session/turn token counters and reported cost for one agent."""

    def __init__(self) -> None:
        # Cumulative token usage for the session.
        self.session_prompt_tokens: int = 0
        self.session_completion_tokens: int = 0
        self.session_total_tokens: int = 0
        self.session_api_calls: int = 0
        self.session_reported_cost_usd: float | None = None
        # Snapshot of the session counters at the start of the current turn.
        self._turn_start_prompt_tokens: int = 0
        self._turn_start_completion_tokens: int = 0
        self._turn_start_total_tokens: int = 0
        self._turn_start_api_calls: int = 0

    def start_turn(self) -> None:
        """Snapshot the session counters as the turn-start baseline."""
        self._turn_start_prompt_tokens = self.session_prompt_tokens
        self._turn_start_completion_tokens = self.session_completion_tokens
        self._turn_start_total_tokens = self.session_total_tokens
        self._turn_start_api_calls = self.session_api_calls

    def record_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        reported_cost_usd: float | None = None,
    ) -> None:
        """Accumulate one API response's usage into the session counters."""
        if reported_cost_usd is not None:
            self.session_reported_cost_usd = (
                self.session_reported_cost_usd or 0.0
            ) + reported_cost_usd
        self.session_prompt_tokens += prompt_tokens
        self.session_completion_tokens += completion_tokens
        self.session_total_tokens += total_tokens
        self.session_api_calls += 1

    @staticmethod
    def extract_reported_cost_usd(usage: Any) -> float | None:
        """Pull a provider-reported cost out of a usage object/dict, if present."""
        if usage is None:
            return None
        names = (
            "cost",
            "total_cost",
            "total_cost_usd",
            "cost_usd",
            "estimated_cost",
            "estimated_cost_usd",
        )
        for name in names:
            if isinstance(usage, dict):
                raw = usage.get(name)
            else:
                raw = getattr(usage, name, None)
            if raw is None:
                continue
            try:
                if isinstance(raw, str):
                    raw = raw.strip().removeprefix("$")
                return float(raw)
            except (TypeError, ValueError):
                continue
        return None

    def session_summary(
        self,
        model: str,
        *,
        provider: Any = None,
        api_mode: Any = None,
        current_run_api_calls: int = 0,
    ) -> dict[str, Any]:
        """Build the session/turn usage summary dict.

        ``model``/``provider``/``api_mode``/``current_run_api_calls`` are
        agent-owned values supplied by the caller. The returned dict shape is
        identical to the former ``AIAgent._session_usage_summary`` output.
        """
        turn_prompt_tokens = max(0, self.session_prompt_tokens - self._turn_start_prompt_tokens)
        turn_completion_tokens = max(
            0, self.session_completion_tokens - self._turn_start_completion_tokens
        )
        turn_total_tokens = max(0, self.session_total_tokens - self._turn_start_total_tokens)
        turn_metered_api_calls = max(0, self.session_api_calls - self._turn_start_api_calls)
        known_pricing = has_known_pricing(model)
        estimated_cost = (
            estimate_cost_usd(model, self.session_prompt_tokens, self.session_completion_tokens)
            if known_pricing
            else None
        )
        turn_estimated_cost = (
            estimate_cost_usd(model, turn_prompt_tokens, turn_completion_tokens)
            if known_pricing
            else None
        )
        reported_cost = self.session_reported_cost_usd
        if reported_cost is not None:
            cost_source = "provider_reported"
            total_cost = reported_cost
        elif estimated_cost is not None:
            cost_source = "estimated"
            total_cost = estimated_cost
        else:
            cost_source = "unavailable"
            total_cost = None
        return {
            "model": model,
            "provider": provider,
            "api_mode": api_mode,
            "api_calls": int(current_run_api_calls or 0),
            "metered_api_calls": int(turn_metered_api_calls),
            "session_api_calls": int(self.session_api_calls),
            "turn": {
                "prompt_tokens": int(turn_prompt_tokens),
                "completion_tokens": int(turn_completion_tokens),
                "total_tokens": int(turn_total_tokens),
            },
            "session": {
                "prompt_tokens": int(self.session_prompt_tokens),
                "completion_tokens": int(self.session_completion_tokens),
                "total_tokens": int(self.session_total_tokens),
            },
            "cost": {
                "source": cost_source,
                "total_usd": total_cost,
                "estimated_total_usd": estimated_cost,
                "estimated_turn_usd": turn_estimated_cost,
                "provider_reported_total_usd": reported_cost,
                "pricing_known": known_pricing,
            },
        }
