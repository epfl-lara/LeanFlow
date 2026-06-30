"""Conversation logging / usage-reporting display helpers extracted from AIAgent.

``OutputManager`` owns the verbose (non-quiet) logging + usage-reporting display
helpers that previously lived directly on ``run_agent.AIAgent``: the conversation
preamble, per-step token/cost reporting, the end-of-conversation session usage
summary, and the reasoning-replay accounting notice. It mirrors the
``PromptManager`` (519f...), ``TokenAccounter`` (65d037d),
``ConversationManager``, ``ProviderClientFactory`` (6027ca4),
``InterruptController`` (06e67ca), ``ResponseNormalizer`` and
``ReasoningProcessor`` extractions.

What moved here (behavior-preserving, logic copied verbatim — the printed/log
text is byte-for-byte identical, since tests assert exact log output, e.g.
``test_non_quiet_logging_*``):
- ``text_preview_lines`` — compact multiline preview builder (pure).
- ``log_conversation_start`` — the "💬 Starting conversation" preamble + preview.
- ``session_usage_summary`` — assemble the usage/cost summary dict (delegates to
  the agent's TokenAccounter).
- ``log_session_usage_summary`` — the once-per-session "📈 Session usage summary".
- ``log_token_usage`` — the per-step "📊 Tokens"/"💵 Cost estimate" line.
- ``reasoning_context_payload_stats`` — count outgoing reasoning fields (pure).
- ``log_reasoning_replay_accounting`` — the "🧠 Reasoning replay attached" notice.

How it reaches AIAgent: OutputManager holds an ``_agent`` reference and reads the
agent's config/state (``log_prefix``, ``log_preview_lines``, ``log_preview_chars``,
``quiet_mode``, ``model``, ``provider``, ``api_mode``, ``_tokens``,
``session_total_tokens``, ``session_prompt_tokens``, ``session_completion_tokens``,
``_current_run_api_calls``) and writes ``_usage_summary_logged``. All printing
routes back through ``agent._vprint(...)`` so the streaming-TTS suppression and
any test that monkeypatches ``agent._vprint`` keep working unchanged. The pricing
helpers (``has_known_pricing`` / ``estimate_cost_usd``) are resolved on
``run_agent`` at call time via a lazy ``import run_agent`` so a test that patches
``run_agent.has_known_pricing`` / ``run_agent.estimate_cost_usd`` still intercepts.

The module does NOT import run_agent at load (only lazily, inside methods), so
creating a manager never triggers an import cycle.
"""

from __future__ import annotations

from typing import Any


class OutputManager:
    """Owns the verbose conversation-logging / usage-reporting display helpers."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    # -- pure preview helpers ----------------------------------------------

    @staticmethod
    def text_preview_lines(
        text: Any,
        *,
        max_lines: int = 8,
        max_chars: int = 1600,
    ) -> list[str]:
        """Build a compact multiline preview without losing all context."""
        raw = str(text or "").strip()
        if not raw:
            return []
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            return []

        max_lines = max(1, int(max_lines or 1))
        max_chars = max(8, int(max_chars or 8))
        preview_lines = lines[:max_lines]
        preview_text = "\n".join(preview_lines)
        if len(preview_text) > max_chars:
            preview_text = preview_text[: max_chars - 4].rstrip() + " ..."
            return preview_text.splitlines() or [preview_text]
        if len(lines) > max_lines:
            preview_lines[-1] = preview_lines[-1] + " ..."
        return preview_lines

    @staticmethod
    def reasoning_context_payload_stats(api_messages: list) -> dict[str, int]:
        reasoning_chars = 0
        assistant_messages = 0
        for msg in api_messages or []:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            message_chars = 0
            for key in ("reasoning_content", "reasoning_details", "codex_reasoning_items"):
                value = msg.get(key)
                if not value:
                    continue
                message_chars += len(str(value))
            if message_chars:
                assistant_messages += 1
                reasoning_chars += message_chars
        return {
            "assistant_messages": assistant_messages,
            "chars": reasoning_chars,
            "approx_tokens": reasoning_chars // 4,
        }

    # -- conversation-start preamble ---------------------------------------

    def log_conversation_start(self, user_message: str) -> None:
        agent = self._agent
        preview_lines = self.text_preview_lines(
            user_message,
            max_lines=min(max(agent.log_preview_lines, 1), 8),
            max_chars=min(max(agent.log_preview_chars, 480), 1600),
        )
        agent._vprint(f"{agent.log_prefix}💬 Starting conversation ({len(user_message):,} chars)")
        for line in preview_lines:
            agent._vprint(f"{agent.log_prefix}   {line}")

    # -- session usage summary ---------------------------------------------

    def session_usage_summary(self) -> dict[str, Any]:
        agent = self._agent
        return agent._tokens.session_summary(
            agent.model,
            provider=agent.provider,
            api_mode=agent.api_mode,
            current_run_api_calls=agent._current_run_api_calls,
        )

    def log_session_usage_summary(self) -> None:
        """Print the session usage summary (once) with API call counts, token totals for this turn and full session, and cost estimate; skips if already logged or in quiet mode. Pulls data from the TokenAccounter and formats provider-reported or estimated costs."""
        agent = self._agent
        if agent._usage_summary_logged:
            return
        agent._usage_summary_logged = True
        if agent.quiet_mode:
            return

        summary = self.session_usage_summary()
        turn = dict(summary.get("turn") or {})
        session = dict(summary.get("session") or {})
        cost = dict(summary.get("cost") or {})
        api_calls = int(summary.get("api_calls") or 0)
        metered_calls = int(summary.get("metered_api_calls") or 0)
        agent._vprint(f"\n{agent.log_prefix}📈 Session usage summary")
        agent._vprint(
            f"{agent.log_prefix}   API calls: {api_calls:,} this conversation "
            f"({metered_calls:,} with provider token usage)"
        )
        agent._vprint(
            f"{agent.log_prefix}   Tokens this conversation: "
            f"input {int(turn.get('prompt_tokens') or 0):,} · "
            f"output {int(turn.get('completion_tokens') or 0):,} · "
            f"total {int(turn.get('total_tokens') or 0):,}"
        )
        if (
            int(session.get("prompt_tokens") or 0) != int(turn.get("prompt_tokens") or 0)
            or int(session.get("completion_tokens") or 0) != int(turn.get("completion_tokens") or 0)
            or int(session.get("total_tokens") or 0) != int(turn.get("total_tokens") or 0)
        ):
            agent._vprint(
                f"{agent.log_prefix}   Session tokens total: "
                f"input {int(session.get('prompt_tokens') or 0):,} · "
                f"output {int(session.get('completion_tokens') or 0):,} · "
                f"total {int(session.get('total_tokens') or 0):,}"
            )

        source = str(cost.get("source") or "unavailable")
        total_cost = cost.get("total_usd")
        if source == "provider_reported" and total_cost is not None:
            agent._vprint(
                f"{agent.log_prefix}   Total cost: ${float(total_cost):.4f} (provider reported)"
            )
        elif source == "estimated" and total_cost is not None:
            agent._vprint(f"{agent.log_prefix}   Total cost estimate: ${float(total_cost):.4f}")
        else:
            agent._vprint(
                f"{agent.log_prefix}   Total cost: unavailable "
                f"(no provider cost or pricing metadata for {agent.model})"
            )

    # -- per-step token / cost reporting -----------------------------------

    def log_token_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        # Resolve pricing helpers on run_agent at call time so tests that patch
        # run_agent.has_known_pricing / run_agent.estimate_cost_usd intercept.
        # Imported lazily to avoid an import cycle.
        """Print per-step token counts and cost estimate. Shows input/output/total tokens for this call plus session running total; if pricing is available for the model, estimates step and session costs; otherwise reports pricing unavailable."""
        import run_agent

        agent = self._agent
        agent._vprint(
            f"{agent.log_prefix}   📊 Tokens: "
            f"input {prompt_tokens:,} · output {completion_tokens:,} · total {total_tokens:,} "
            f"(session {agent.session_total_tokens:,})"
        )
        if run_agent.has_known_pricing(agent.model):
            step_cost = run_agent.estimate_cost_usd(agent.model, prompt_tokens, completion_tokens)
            session_cost = run_agent.estimate_cost_usd(
                agent.model,
                agent.session_prompt_tokens,
                agent.session_completion_tokens,
            )
            agent._vprint(
                f"{agent.log_prefix}   💵 Cost estimate: "
                f"step ${step_cost:.4f} · session ${session_cost:.4f}"
            )
        elif run_agent.has_listed_pricing(agent.model):
            # Known self-hosted / unmetered model (e.g. EPFL RCP): no per-token USD cost — token
            # counts above are the meaningful cost signal. Avoid the misleading "no metadata" line.
            agent._vprint(
                f"{agent.log_prefix}   💵 Cost estimate: $0.0000 "
                f"(self-hosted / unmetered model {agent.model}; track tokens above)"
            )
        else:
            agent._vprint(
                f"{agent.log_prefix}   💵 Cost estimate: unavailable "
                f"(no pricing metadata for {agent.model})"
            )

    # -- reasoning-replay accounting notice --------------------------------

    def log_reasoning_replay_accounting(
        self,
        *,
        api_messages: list,
        approx_tokens: int,
        provider_prompt_tokens: int,
    ) -> None:
        agent = self._agent
        if agent.quiet_mode:
            return
        stats = self.reasoning_context_payload_stats(api_messages)
        reasoning_tokens = int(stats.get("approx_tokens") or 0)
        reasoning_chars = int(stats.get("chars") or 0)
        provider_prompt_tokens = int(provider_prompt_tokens or 0)
        approx_tokens = int(approx_tokens or 0)
        if reasoning_tokens < 4_000 or provider_prompt_tokens <= 0:
            return
        if approx_tokens < provider_prompt_tokens * 2:
            return
        agent._vprint(
            f"{agent.log_prefix}   🧠 Reasoning replay attached: "
            f"{int(stats.get('assistant_messages') or 0):,} assistant msg(s), "
            f"~{reasoning_tokens:,} local tokens ({reasoning_chars:,} chars). "
            f"Provider input accounting reported {provider_prompt_tokens:,}; "
            "compare with the local request estimate above."
        )
