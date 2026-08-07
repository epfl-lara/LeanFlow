"""Compress long conversations without losing the continuation handoff.

Middle turns are summarized through the configured auxiliary route, with one
provider-aware retry through the agent's main model.  If both provider calls
fail, a bounded deterministic extract preserves evidence locally instead of
silently dropping the compacted turns.
"""

import hashlib
import logging
from typing import Any

from agent.compression.summary_handoff import (
    LEGACY_SUMMARY_PREFIX,  # noqa: F401 - backwards-compatible re-export
    SUMMARY_PREFIX,  # noqa: F401 - backwards-compatible re-export
    CompressionSummaryHandoff,
)
from agent.providers.auxiliary_client import call_llm
from agent.providers.model_metadata import (
    estimate_messages_tokens_rough,
    get_model_context_length,
)

logger = logging.getLogger(__name__)

_PRODUCTION_SUMMARY_CALL = call_llm

STALE_TOOL_OUTPUT_MARKER = "[Old tool result content cleared during context compaction]"


class ContextCompressor:
    """Compress conversation context when approaching the model's limit.

    Protect the first and last turns, then replace the middle with an LLM
    handoff or a deterministic local extract. Token tracking uses actual API
    usage when available.
    """

    def __init__(
        self,
        model: str,
        threshold_percent: float = 0.75,
        protect_first_n: int = 3,
        protect_last_n: int = 4,
        summary_target_tokens: int = 2500,
        quiet_mode: bool = False,
        summary_model_override: str | None = None,
        base_url: str = "",
        api_key: str = "",
        main_provider: str = "",
        main_api_mode: str = "",
        reserved_output_tokens: int = 0,
        absolute_threshold_tokens: int | None = None,
        prune_tool_output: bool = False,
        prune_keep_recent_user_turns: int = 2,
    ) -> None:
        self.model = model
        self.main_model = model
        self.base_url = base_url
        self.api_key = api_key
        self.main_provider = (main_provider or "").strip().lower()
        self.main_api_mode = (main_api_mode or "").strip().lower()
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.summary_target_tokens = summary_target_tokens
        self.quiet_mode = quiet_mode
        self.reserved_output_tokens = max(0, int(reserved_output_tokens or 0))
        self.prune_tool_output = bool(prune_tool_output)
        self.prune_keep_recent_user_turns = max(1, int(prune_keep_recent_user_turns or 2))

        self.context_length = get_model_context_length(model, base_url=base_url, api_key=api_key)
        percent_threshold = int(self.context_length * threshold_percent)
        self.percent_threshold_tokens = percent_threshold
        reserved_threshold = (
            max(0, self.context_length - self.reserved_output_tokens)
            if self.reserved_output_tokens
            else percent_threshold
        )
        self.base_threshold_tokens = (
            min(percent_threshold, reserved_threshold) if reserved_threshold else percent_threshold
        )
        self.absolute_threshold_tokens = max(0, int(absolute_threshold_tokens or 0))
        self.threshold_tokens = (
            min(self.base_threshold_tokens, self.absolute_threshold_tokens)
            if self.absolute_threshold_tokens
            else self.base_threshold_tokens
        )
        self.compression_count = 0
        self._context_probed = False  # True after a step-down from context error

        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0

        self.summary_model = summary_model_override or ""
        # A dead auxiliary summary route otherwise pays its full timeout
        # timeout before every main-model fallback in one long conversation.
        # Keep the first failure observable, then use the already-supported
        # main/local recovery chain for subsequent compactions.
        self._summary_auxiliary_failure = ""
        self._summary_main_failure = ""
        self._summary_main_route = self._main_summary_route_identity()

    def bind_main_summary_route(
        self,
        *,
        model: str,
        provider: str,
        api_mode: str,
        base_url: str,
        api_key: str,
    ) -> None:
        """Synchronize main-summary fallback after an agent provider switch."""
        previous_route = self._summary_main_route
        self.main_model = model
        self.main_provider = (provider or "").strip().lower()
        self.main_api_mode = (api_mode or "").strip().lower()
        self.base_url = base_url
        self.api_key = api_key
        current_route = self._main_summary_route_identity()
        self._summary_main_route = current_route
        if current_route != previous_route:
            self._summary_main_failure = ""

    def _main_summary_route_identity(self) -> tuple[str, str, str, str]:
        """Return a credential-safe identity for the effective main route."""
        route = CompressionSummaryHandoff(
            model=self.main_model,
            main_provider=self.main_provider,
            main_api_mode=self.main_api_mode,
            base_url=self.base_url,
            api_key=self.api_key,
        )
        effective_provider = route._effective_main_provider()
        custom_base_url = str(self.base_url or "").strip() if effective_provider == "custom" else ""
        key_digest = ""
        if effective_provider == "custom" and self.api_key:
            key_digest = hashlib.sha256(self.api_key.encode("utf-8", "replace")).hexdigest()
        return (
            str(self.main_model or "").strip(),
            effective_provider,
            custom_base_url,
            key_digest,
        )

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Update tracked token usage from API response."""
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        """Check if context exceeds the compression threshold."""
        tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        return tokens >= self.threshold_tokens

    def should_compress_preflight(self, messages: list[dict[str, Any]]) -> bool:
        """Quick pre-flight check using rough estimate (before API call)."""
        rough_estimate = estimate_messages_tokens_rough(messages)
        return rough_estimate >= self.threshold_tokens

    def get_status(self) -> dict[str, Any]:
        """Get current compression status for display/logging."""
        return {
            "last_prompt_tokens": self.last_prompt_tokens,
            "threshold_tokens": self.threshold_tokens,
            "percent_threshold_tokens": self.percent_threshold_tokens,
            "base_threshold_tokens": self.base_threshold_tokens,
            "absolute_threshold_tokens": self.absolute_threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": (
                (self.last_prompt_tokens / self.context_length * 100) if self.context_length else 0
            ),
            "compression_count": self.compression_count,
            "reserved_output_tokens": self.reserved_output_tokens,
            "prune_tool_output": self.prune_tool_output,
        }

    def _summary_handoff(self) -> CompressionSummaryHandoff:
        """Bind a handoff builder to the current effective main route."""
        return CompressionSummaryHandoff(
            model=self.main_model,
            main_provider=self.main_provider,
            main_api_mode=self.main_api_mode,
            base_url=self.base_url,
            api_key=self.api_key,
            summary_model=self.summary_model,
            summary_target_tokens=self.summary_target_tokens,
            call=call_llm,
            # Production provider calls must be killable at the configured
            # wall deadline. Dependency-injected test calls remain in-process
            # so their deterministic response/call assertions stay useful.
            process_isolated=call_llm is _PRODUCTION_SUMMARY_CALL,
            auxiliary_enabled=not bool(self._summary_auxiliary_failure),
            on_auxiliary_failure=self._disable_summary_auxiliary,
            main_enabled=not bool(self._summary_main_failure),
            main_circuit_failure=self._summary_main_failure,
            on_main_failure=self._disable_summary_main,
        )

    def threshold_description(self) -> str:
        """Describe the effective threshold without mislabeling an absolute cap."""
        percent = self.threshold_tokens / self.context_length * 100 if self.context_length else 0.0
        if self.base_threshold_tokens == self.percent_threshold_tokens:
            base = (
                f"base policy {self.threshold_percent * 100:.0f}% = "
                f"{self.base_threshold_tokens:,}"
            )
        else:
            base = (
                f"base threshold {self.base_threshold_tokens:,} after output reserve; "
                f"percentage policy {self.threshold_percent * 100:.0f}% = "
                f"{self.percent_threshold_tokens:,}"
            )
        if self.absolute_threshold_tokens and self.threshold_tokens < self.base_threshold_tokens:
            return f"managed cap {self.threshold_tokens:,} = {percent:.0f}%; {base}"
        return base

    def _disable_summary_auxiliary(self, failure_type: str) -> None:
        """Open this compressor's circuit after one auxiliary exception."""
        if not self._summary_auxiliary_failure:
            self._summary_auxiliary_failure = str(failure_type or "UnknownFailure")[:100]

    def _disable_summary_main(self, failure_type: str) -> None:
        """Open this compressor's main-summary circuit after one failure."""
        if not self._summary_main_failure:
            self._summary_main_failure = str(failure_type or "UnknownFailure")[:100]

    def _deterministic_extractive_summary(self, turns_to_summarize: list[dict[str, Any]]) -> str:
        """Build a bounded local handoff without a provider call."""
        return self._summary_handoff().deterministic_extract(turns_to_summarize)

    def _generate_summary(self, turns_to_summarize: list[dict[str, Any]]) -> str:
        """Generate a handoff through auxiliary, main, then local extraction."""
        return self._summary_handoff().generate(turns_to_summarize)

    @staticmethod
    def _with_summary_prefix(summary: str) -> str:
        """Normalize summary text to the current compaction handoff format."""
        return CompressionSummaryHandoff.with_summary_prefix(summary)

    # ------------------------------------------------------------------
    # Tool-call / tool-result pair integrity helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_tool_call_id(tc) -> str:
        """Extract the call ID from a tool_call entry (dict or SimpleNamespace)."""
        if isinstance(tc, dict):
            return str(tc.get("id", "") or "")
        return str(getattr(tc, "id", "") or "")

    def _sanitize_tool_pairs(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fix orphaned tool_call / tool_result pairs after compression.

        Two failure modes:
        1. A tool *result* references a call_id whose assistant tool_call was
           removed (summarized/truncated).  The API rejects this with
           "No tool call found for function call output with call_id ...".
        2. An assistant message has tool_calls whose results were dropped.
           The API rejects this because every tool_call must be followed by
           a tool result with the matching call_id.

        This method removes orphaned results and inserts stub results for
        orphaned calls so the message list is always well-formed.
        """
        surviving_call_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = self._get_tool_call_id(tc)
                    if cid:
                        surviving_call_ids.add(cid)

        result_call_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "tool":
                result_cid = str(msg.get("tool_call_id") or "")
                if result_cid:
                    result_call_ids.add(result_cid)

        # 1. Remove tool results whose call_id has no matching assistant tool_call
        orphaned_results = result_call_ids - surviving_call_ids
        if orphaned_results:
            messages = [
                m
                for m in messages
                if not (
                    m.get("role") == "tool" and str(m.get("tool_call_id") or "") in orphaned_results
                )
            ]
            if not self.quiet_mode:
                logger.info(
                    "Compression sanitizer: removed %d orphaned tool result(s)",
                    len(orphaned_results),
                )

        # 2. Add stub results for assistant tool_calls whose results were dropped
        missing_results = surviving_call_ids - result_call_ids
        if missing_results:
            patched: list[dict[str, Any]] = []
            for msg in messages:
                patched.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = self._get_tool_call_id(tc)
                        if cid in missing_results:
                            patched.append(
                                {
                                    "role": "tool",
                                    "content": "[Result from earlier conversation — see context summary above]",
                                    "tool_call_id": cid,
                                }
                            )
            messages = patched
            if not self.quiet_mode:
                logger.info(
                    "Compression sanitizer: added %d stub tool result(s)", len(missing_results)
                )

        return messages

    def _prune_stale_tool_outputs(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Trim old tool-result payloads while preserving recent turns.

        This is a lightweight version of Kilo/OpenCode-style stale output pruning:
        keep the most recent user turns intact, then replace older tool result bodies
        with a fixed marker so the assistant can keep the execution history without
        paying the full token cost of stale command output.
        """
        if not self.prune_tool_output:
            return [dict(message) for message in messages], 0

        pruned_messages: list[dict[str, Any]] = []
        recent_user_turns = 0
        pruned_count = 0

        for message in reversed(messages):
            cloned = dict(message)
            if cloned.get("role") == "user":
                recent_user_turns += 1

            should_prune = (
                cloned.get("role") == "tool"
                and recent_user_turns >= self.prune_keep_recent_user_turns
                and cloned.get("content") not in (None, "", STALE_TOOL_OUTPUT_MARKER)
            )
            if should_prune:
                cloned["content"] = STALE_TOOL_OUTPUT_MARKER
                pruned_count += 1

            pruned_messages.append(cloned)

        pruned_messages.reverse()
        return pruned_messages, pruned_count

    def _align_boundary_forward(self, messages: list[dict[str, Any]], idx: int) -> int:
        """Push a compress-start boundary forward past any orphan tool results.

        If ``messages[idx]`` is a tool result, slide forward until we hit a
        non-tool message so we don't start the summarised region mid-group.
        """
        while idx < len(messages) and messages[idx].get("role") == "tool":
            idx += 1
        return idx

    def _align_boundary_backward(self, messages: list[dict[str, Any]], idx: int) -> int:
        """Pull a compress-end boundary backward to avoid splitting a
        tool_call / result group.

        If the message just before ``idx`` is an assistant message with
        tool_calls, those tool results will start at ``idx`` and would be
        separated from their parent.  Move backwards to include the whole
        group in the summarised region.
        """
        if idx <= 0 or idx >= len(messages):
            return idx
        prev = messages[idx - 1]
        if prev.get("role") == "assistant" and prev.get("tool_calls"):
            # The results for this assistant turn sit at idx..idx+k.
            # Include the assistant message in the summarised region too.
            idx -= 1
        return idx

    def compress(
        self, messages: list[dict[str, Any]], current_tokens: int | None = None
    ) -> list[dict[str, Any]]:
        """Compress middle turns while preserving a continuation handoff.

        Keep the protected head and tail, insert a generated or deterministic
        handoff, and repair orphaned tool-call/result pairs before returning.
        """
        working_messages, pruned_count = self._prune_stale_tool_outputs(messages)
        n_messages = len(working_messages)
        if n_messages <= self.protect_first_n + self.protect_last_n + 1:
            if not self.quiet_mode:
                print(
                    f"⚠️  Cannot compress: only {n_messages} messages (need > {self.protect_first_n + self.protect_last_n + 1})"
                )
            return messages

        compress_start = self.protect_first_n
        compress_end = n_messages - self.protect_last_n
        if compress_start >= compress_end:
            return messages

        # Adjust boundaries to avoid splitting tool_call/result groups.
        compress_start = self._align_boundary_forward(working_messages, compress_start)
        compress_end = self._align_boundary_backward(working_messages, compress_end)
        if compress_start >= compress_end:
            return working_messages

        turns_to_summarize = working_messages[compress_start:compress_end]
        display_tokens = (
            current_tokens
            if current_tokens
            else self.last_prompt_tokens or estimate_messages_tokens_rough(working_messages)
        )

        if not self.quiet_mode:
            print(
                f"\n📦 Context compression triggered ({display_tokens:,} tokens ≥ {self.threshold_tokens:,} threshold)"
            )
            print(
                f"   📊 Model context limit: {self.context_length:,} tokens "
                f"({self.threshold_description()})"
            )

        if not self.quiet_mode:
            print(
                f"   🗜️  Summarizing turns {compress_start + 1}-{compress_end} ({len(turns_to_summarize)} turns)"
            )

        summary = self._generate_summary(turns_to_summarize)
        if not summary:
            # Keep this invariant even when tests or extensions replace the
            # generator: compression may shrink context, but never erase its
            # continuation handoff.
            summary = self._deterministic_extractive_summary(turns_to_summarize)

        compressed = []
        for i in range(compress_start):
            msg = working_messages[i].copy()
            if i == 0 and msg.get("role") == "system" and self.compression_count == 0:
                msg["content"] = (
                    (msg.get("content") or "")
                    + "\n\n[Note: Some earlier conversation turns have been compacted into a handoff summary to preserve context space. The current session state may still reflect earlier work, so build on that summary and state rather than re-doing work.]"
                )
            compressed.append(msg)

        last_head_role = (
            working_messages[compress_start - 1].get("role", "user")
            if compress_start > 0
            else "user"
        )
        summary_role = "user" if last_head_role in ("assistant", "tool") else "assistant"
        compressed.append({"role": summary_role, "content": summary})

        for i in range(compress_end, n_messages):
            compressed.append(working_messages[i].copy())

        self.compression_count += 1

        compressed = self._sanitize_tool_pairs(compressed)

        if not self.quiet_mode:
            new_estimate = estimate_messages_tokens_rough(compressed)
            saved_estimate = display_tokens - new_estimate
            print(
                f"   ✅ Compressed: {n_messages} → {len(compressed)} messages (~{saved_estimate:,} tokens saved)"
            )
            if pruned_count:
                print(f"   ✂️  Pruned stale tool outputs: {pruned_count}")
            print(f"   💡 Compression #{self.compression_count} complete")

        return compressed
