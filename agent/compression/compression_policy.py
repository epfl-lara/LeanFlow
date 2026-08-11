"""Context-compression gating extracted from ``run_agent.AIAgent``.

``CompressionPolicy`` owns the *when/how* of context compression: it decides
whether the conversation is large enough to compress, drives the head/tail
preserving compressor, and runs the retry-after-compression loops used at the
various send sites. It wraps the agent's existing ``ContextCompressor`` (which
does the actual head/tail summarization) -- the policy only decides timing and
orchestration. It mirrors the ``ConversationManager`` (c3bbb31),
``TokenAccounter`` (65d037d), ``ProviderClientFactory`` (6027ca4) and
``ToolExecutor`` (74e006b) extractions.

The policy holds a reference to the owning ``AIAgent`` and reaches the
intrinsic compression state through it -- ``context_compressor``,
``compression_enabled``, ``_todo_store``, ``_session_db``, ``session_id``,
``platform``/``model``, ``_cached_system_prompt``, ``_last_flushed_db_idx``,
the ``_advisor_result_context_reserve_tokens`` reserve, ``quiet_mode`` /
``log_prefix`` and the build/flush helpers (``flush_memories``,
``_invalidate_system_prompt``, ``_build_system_prompt``). That state is
intrinsically the agent's, so reaching it via the agent is the genuine
boundary; the gating/retry *logic* (the methods below) has left the god class.

Routing rule (mirrors ``ConversationManager.persist_session``): when one policy
method needs to (re)compress, it routes back through the AGENT wrapper
``agent._compress_context`` (and ``agent._build_api_messages_for_turn`` /
``agent._api_payload_size_estimate``) rather than calling its own
``compress_context`` directly, so callers/tests that patch those wrappers on the
agent still intercept the compression exactly as before the extraction.

This module does NOT import ``run_agent`` at module load, so importing it never
creates a cycle; the one module-level run_agent name it needs
(``_generate_short_session_id``) is reached through a lazy ``import run_agent``
at call time (also keeping any test monkeypatch of it intercepting).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agent.providers.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough

if TYPE_CHECKING:  # pragma: no cover - typing only
    from run_agent import AIAgent

logger = logging.getLogger(__name__)


class CompressionPolicy:
    """Decides when/how to compress a conversation on behalf of an :class:`AIAgent`.

    Constructed with the owning agent; all compression state is reached through
    it. The gating / retry logic is behavior-preserving relative to the former
    ``AIAgent`` methods of the same names.
    """

    def __init__(self, agent: AIAgent) -> None:
        self._agent = agent

    def advisor_precompression_admitted(self, tool_names: set[str]) -> bool:
        """Return whether a managed advisor call may justify context compression.

        Native workflows can install a side-effect-free admission callback so
        an advisor request already rejected by a durable circuit does not erase
        useful context before ordinary tool preflight returns that rejection.
        Callback failures fail open and preserve the existing behavior.
        """
        callback = getattr(
            self._agent,
            "_advisor_precompression_admission_callback",
            None,
        )
        if not callable(callback):
            return True
        try:
            return bool(callback(frozenset(tool_names)))
        except Exception:
            logger.debug("Advisor precompression admission callback failed", exc_info=True)
            return True

    # ── Core compression ────────────────────────────────────────────────────
    def compress_context(
        self,
        messages: list,
        system_message: str,
        *,
        approx_tokens: int = None,
        task_id: str = "default",
    ) -> tuple:
        """Compress conversation context and split the session in SQLite.

        Returns:
            (compressed_messages, new_system_prompt) tuple
        """
        agent = self._agent
        # Pre-compression memory flush: let the model save memories before they're lost
        agent.flush_memories(messages, min_turns=0)

        compressed = agent.context_compressor.compress(messages, current_tokens=approx_tokens)

        todo_snapshot = agent._todo_store.format_for_injection()
        if todo_snapshot:
            compressed.append({"role": "user", "content": todo_snapshot})

        # Preserve file-read history so the model doesn't re-read files
        # it already examined before compression.
        try:
            from tools.implementations.file_tools import get_read_files_summary

            read_files = get_read_files_summary(task_id)
            if read_files:
                file_list = "\n".join(
                    f"  - {f['path']} ({', '.join(f['regions'])})" for f in read_files
                )
                compressed.append(
                    {
                        "role": "user",
                        "content": (
                            "[Files already read in this session — do NOT re-read these]\n"
                            f"{file_list}\n"
                            "Use the information from the context summary above. "
                            "Proceed with writing, editing, or responding."
                        ),
                    }
                )
        except Exception:
            pass  # Don't break compression if file tracking fails

        agent._invalidate_system_prompt()
        new_system_prompt = agent._build_system_prompt(system_message)
        agent._cached_system_prompt = new_system_prompt

        if agent._session_db:
            try:
                # Resolved lazily at call time so a test monkeypatch of
                # run_agent._generate_short_session_id still intercepts and no
                # import cycle is created at module load.
                import run_agent

                # Propagate title to the new session with auto-numbering
                old_title = agent._session_db.get_session_title(agent.session_id)
                agent._session_db.end_session(agent.session_id, "compression")
                old_session_id = agent.session_id
                agent.session_id = run_agent._generate_short_session_id()
                agent._session_db.create_session(
                    session_id=agent.session_id,
                    source=agent.platform or "cli",
                    model=agent.model,
                    parent_session_id=old_session_id,
                )
                # Auto-number the title for the continuation session
                if old_title:
                    try:
                        new_title = agent._session_db.get_next_title_in_lineage(old_title)
                        agent._session_db.set_session_title(agent.session_id, new_title)
                    except (ValueError, Exception) as e:
                        logger.debug("Could not propagate title on compression: %s", e)
                agent._session_db.update_system_prompt(agent.session_id, new_system_prompt)
                # Reset flush cursor — new session starts with no messages written
                agent._last_flushed_db_idx = 0
            except Exception as e:
                logger.debug("Session DB compression split failed: %s", e)

        return compressed, new_system_prompt

    # ── Token-payload estimate ──────────────────────────────────────────────
    def api_payload_size_estimate(self, api_messages: list) -> tuple[int, int]:
        total_chars = sum(len(str(msg)) for msg in api_messages)
        return total_chars // 4, total_chars

    # ── Pre-advisor on-demand compression ───────────────────────────────────
    def maybe_precompress_before_advisor_tool(
        self,
        messages: list,
        system_message: str,
        active_system_prompt: str,
        *,
        effective_task_id: str,
    ) -> tuple[list, str]:
        """Compress prior history before adding large advisor output."""
        agent = self._agent
        if not agent.compression_enabled:
            return messages, active_system_prompt
        reserve = int(getattr(agent, "_advisor_result_context_reserve_tokens", 0) or 0)
        if reserve <= 0:
            return messages, active_system_prompt
        compressor = agent.context_compressor
        estimated_with_advisor = (
            estimate_tokens_rough(active_system_prompt or "")
            + estimate_messages_tokens_rough(messages)
            + reserve
        )
        if not compressor.should_compress(estimated_with_advisor):
            return messages, active_system_prompt
        if not agent.quiet_mode:
            print(
                f"{agent.log_prefix}📦 Pre-advisor compression: reserving ~{reserve:,} tokens "
                "so Lean advisor output stays unsummarized."
            )
        # One pass is the useful limit here. If the advisor reserve alone keeps
        # the estimate above the threshold, another pass can only summarize the
        # fresh handoff produced below; it cannot create more reserve safely.
        # Post-tool suffix-preserving compression handles a large advisor result.
        messages, active_system_prompt = agent._compress_context(
            messages,
            system_message,
            approx_tokens=estimated_with_advisor,
            task_id=effective_task_id,
        )
        return messages, active_system_prompt

    # ── Suffix-preserving compression ───────────────────────────────────────
    def compress_context_preserving_suffix(
        self,
        messages: list,
        suffix_start: int,
        system_message: str,
        *,
        approx_tokens: int,
        task_id: str,
    ) -> tuple[list, str]:
        """Compress old history while keeping the latest advisor turn verbatim."""
        agent = self._agent
        if suffix_start <= 0 or suffix_start >= len(messages):
            return agent._compress_context(
                messages,
                system_message,
                approx_tokens=approx_tokens,
                task_id=task_id,
            )
        prefix = messages[:suffix_start]
        suffix = messages[suffix_start:]
        suffix_tokens = estimate_messages_tokens_rough(suffix)
        compressed_prefix, active_system_prompt = agent._compress_context(
            prefix,
            system_message,
            approx_tokens=max(0, approx_tokens - suffix_tokens),
            task_id=task_id,
        )
        return compressed_prefix + suffix, active_system_prompt

    # ── Pre-send (preflight) compression ────────────────────────────────────
    def maybe_compress_before_api_send(
        self,
        messages: list,
        system_message: str,
        active_system_prompt: str,
        *,
        api_messages: list,
        approx_tokens: int,
        task_id: str,
    ) -> tuple[list, str, list, int, int]:
        """Compress before send when the exact outgoing payload crosses threshold."""
        agent = self._agent
        total_chars = sum(len(str(msg)) for msg in api_messages)
        if not agent.compression_enabled:
            return messages, active_system_prompt, api_messages, approx_tokens, total_chars

        compressor = agent.context_compressor
        if approx_tokens < compressor.threshold_tokens:
            return messages, active_system_prompt, api_messages, approx_tokens, total_chars

        for attempt in range(1, 4):
            if not agent.quiet_mode:
                agent._vprint(
                    f"{agent.log_prefix}📦 Pre-send compression: outgoing request estimate "
                    f"~{approx_tokens:,} tokens >= {compressor.threshold_tokens:,} threshold "
                    f"(attempt {attempt}/3)"
                )
            previous_tokens = approx_tokens
            previous_len = len(messages)
            # Route compression + payload rebuild through the agent wrappers so
            # test patches of ``agent._compress_context`` /
            # ``agent._build_api_messages_for_turn`` /
            # ``agent._api_payload_size_estimate`` still intercept.
            messages, active_system_prompt = agent._compress_context(
                messages,
                system_message,
                approx_tokens=approx_tokens,
                task_id=task_id,
            )
            api_messages = agent._build_api_messages_for_turn(messages, active_system_prompt)
            approx_tokens, total_chars = agent._api_payload_size_estimate(api_messages)
            if approx_tokens < compressor.threshold_tokens:
                break
            if approx_tokens >= previous_tokens and len(messages) >= previous_len:
                break

        return messages, active_system_prompt, api_messages, approx_tokens, total_chars
