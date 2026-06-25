"""Automatic context window compression for long conversations.

Self-contained class with its own OpenAI client for summarization.
Uses Gemini Flash (cheap/fast) to summarize middle turns while
protecting head and tail context.
"""

import logging
import os
from typing import Any

from agent.providers.auxiliary_client import call_llm
from agent.providers.model_metadata import (
    estimate_messages_tokens_rough,
    get_model_context_length,
)

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION] Earlier turns in this conversation were compacted "
    "to save context space. The summary below describes work that was "
    "already completed, and the current session state may still reflect "
    "that work (for example, files may already be changed). Use the summary "
    "and the current state to continue from where things left off, and "
    "avoid repeating work:"
)
LEGACY_SUMMARY_PREFIX = "[CONTEXT SUMMARY]:"
STALE_TOOL_OUTPUT_MARKER = "[Old tool result content cleared during context compaction]"


class ContextCompressor:
    """Compresses conversation context when approaching the model's context limit.

    Algorithm: protect first N + last N turns, summarize everything in between.
    Token tracking uses actual counts from API responses for accuracy.
    """

    def __init__(
        self,
        model: str,
        threshold_percent: float = 0.75,
        protect_first_n: int = 3,
        protect_last_n: int = 4,
        summary_target_tokens: int = 2500,
        quiet_mode: bool = False,
        summary_model_override: str = None,
        base_url: str = "",
        api_key: str = "",
        reserved_output_tokens: int = 0,
        prune_tool_output: bool = False,
        prune_keep_recent_user_turns: int = 2,
    ):
        self.model = model
        self.base_url = base_url
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
        reserved_threshold = (
            max(0, self.context_length - self.reserved_output_tokens)
            if self.reserved_output_tokens
            else percent_threshold
        )
        self.threshold_tokens = (
            min(percent_threshold, reserved_threshold) if reserved_threshold else percent_threshold
        )
        self.compression_count = 0
        self._context_probed = False  # True after a step-down from context error

        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0

        self.summary_model = summary_model_override or ""

    def update_from_response(self, usage: dict[str, Any]):
        """Update tracked token usage from API response."""
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)

    def should_compress(self, prompt_tokens: int = None) -> bool:
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
            "context_length": self.context_length,
            "usage_percent": (
                (self.last_prompt_tokens / self.context_length * 100) if self.context_length else 0
            ),
            "compression_count": self.compression_count,
            "reserved_output_tokens": self.reserved_output_tokens,
            "prune_tool_output": self.prune_tool_output,
        }

    def _generate_summary(self, turns_to_summarize: list[dict[str, Any]]) -> str | None:
        """Generate a concise summary of conversation turns.

        Tries the auxiliary model first, then falls back to the user's main
        model.  Returns None if all attempts fail — the caller should drop
        the middle turns without a summary rather than inject a useless
        placeholder.
        """
        parts = []
        for msg in turns_to_summarize:
            role = msg.get("role", "unknown")
            content = msg.get("content") or ""
            if len(content) > 2000:
                content = content[:1000] + "\n...[truncated]...\n" + content[-500:]
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                tool_names = [
                    tc.get("function", {}).get("name", "?")
                    for tc in tool_calls
                    if isinstance(tc, dict)
                ]
                content += f"\n[Tool calls: {', '.join(tool_names)}]"
            parts.append(f"[{role.upper()}]: {content}")

        content_to_summarize = "\n\n".join(parts)
        prompt = f"""Create a concise but high-signal handoff for a later assistant that will continue this conversation after earlier turns are compacted.

Use this structure:
## Goal
[What the user is trying to accomplish]

## Instructions
- [Important user instructions, constraints, and preferences]

## Discoveries
[Important findings, tool results, file names, and technical facts]

## Accomplished
[What is already done, what changed, and what remains]

## Next Steps
- [Concrete next action]

Keep it factual and resume-oriented. Mention relevant files and avoid repeating stale tool output unless it matters. Target ~{self.summary_target_tokens} tokens.

---
TURNS TO SUMMARIZE:
{content_to_summarize}
---

Write only the summary body. Do not include any preamble or prefix; the system will add the handoff wrapper."""

        # Use the centralized LLM router — handles provider resolution,
        # auth, and fallback internally.
        try:
            call_kwargs = {
                "task": "compression",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": self.summary_target_tokens * 2,
                "timeout": 30.0,
            }
            if self.summary_model:
                call_kwargs["model"] = self.summary_model
            response = call_llm(**call_kwargs)
            content = response.choices[0].message.content
            # Handle cases where content is not a string (e.g., dict from llama.cpp)
            if not isinstance(content, str):
                content = str(content) if content else ""
            summary = content.strip()
            return self._with_summary_prefix(summary)
        except RuntimeError:
            logging.warning(
                "Context compression: no provider available for "
                "summary. Middle turns will be dropped without summary."
            )
            return None
        except Exception as e:
            logging.warning("Failed to generate context summary: %s", e)
            return None

    @staticmethod
    def _with_summary_prefix(summary: str) -> str:
        """Normalize summary text to the current compaction handoff format."""
        text = (summary or "").strip()
        for prefix in (LEGACY_SUMMARY_PREFIX, SUMMARY_PREFIX):
            if text.startswith(prefix):
                text = text[len(prefix) :].lstrip()
                break
        return f"{SUMMARY_PREFIX}\n{text}" if text else SUMMARY_PREFIX

    # ------------------------------------------------------------------
    # Tool-call / tool-result pair integrity helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_tool_call_id(tc) -> str:
        """Extract the call ID from a tool_call entry (dict or SimpleNamespace)."""
        if isinstance(tc, dict):
            return tc.get("id", "")
        return getattr(tc, "id", "") or ""

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
        surviving_call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = self._get_tool_call_id(tc)
                    if cid:
                        surviving_call_ids.add(cid)

        result_call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    result_call_ids.add(cid)

        # 1. Remove tool results whose call_id has no matching assistant tool_call
        orphaned_results = result_call_ids - surviving_call_ids
        if orphaned_results:
            messages = [
                m
                for m in messages
                if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned_results)
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
        self, messages: list[dict[str, Any]], current_tokens: int = None
    ) -> list[dict[str, Any]]:
        """Compress conversation messages by summarizing middle turns.

        Keeps first N + last N turns, summarizes everything in between.
        After compression, orphaned tool_call / tool_result pairs are cleaned
        up so the API never receives mismatched IDs.
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
                f"   📊 Model context limit: {self.context_length:,} tokens ({self.threshold_percent * 100:.0f}% = {self.threshold_tokens:,})"
            )

        if not self.quiet_mode:
            print(
                f"   🗜️  Summarizing turns {compress_start + 1}-{compress_end} ({len(turns_to_summarize)} turns)"
            )

        summary = self._generate_summary(turns_to_summarize)

        compressed = []
        for i in range(compress_start):
            msg = working_messages[i].copy()
            if i == 0 and msg.get("role") == "system" and self.compression_count == 0:
                msg["content"] = (
                    (msg.get("content") or "")
                    + "\n\n[Note: Some earlier conversation turns have been compacted into a handoff summary to preserve context space. The current session state may still reflect earlier work, so build on that summary and state rather than re-doing work.]"
                )
            compressed.append(msg)

        if summary:
            last_head_role = (
                working_messages[compress_start - 1].get("role", "user")
                if compress_start > 0
                else "user"
            )
            summary_role = "user" if last_head_role in ("assistant", "tool") else "assistant"
            compressed.append({"role": summary_role, "content": summary})
        else:
            if not self.quiet_mode:
                print("   ⚠️  No summary model available — middle turns dropped without summary")

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
