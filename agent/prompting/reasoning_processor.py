"""Thinking/reasoning-block helpers extracted from run_agent.AIAgent.

``ReasoningProcessor`` owns the small cluster of (almost entirely pure) text
helpers that deal with model "thinking": detecting/stripping ``<think>...</think>``
blocks, extracting structured reasoning fields from a provider's assistant
message, and building a compact reasoning preview for managed-runner logs. It
mirrors the ``TokenAccounter`` (65d037d), ``ConversationManager`` (c3bbb31),
``ProviderClientFactory`` (6027ca4), ``ToolExecutor`` (74e006b),
``InterruptController`` (06e67ca) and ``ResponseNormalizer`` extractions.

What moved here (behavior-preserving, logic copied verbatim):
- ``has_content_after_think_block`` — True iff content has non-whitespace text
  after removing every ``<think>...</think>`` block.
- ``strip_think_blocks`` — remove ``<think>...</think>`` blocks, returning the
  visible text.
- ``extract_reasoning`` — combine ``reasoning`` / ``reasoning_content`` /
  ``reasoning_details`` fields off an assistant message into one string.
- ``reasoning_preview_lines`` — compact multiline preview of reasoning text.

These helpers are stateless, so they are implemented as ``@staticmethod`` on
``ReasoningProcessor``. AIAgent keeps thin wrapper methods that delegate (tests
call ``agent._has_content_after_think_block`` / ``agent._strip_think_blocks`` /
``agent._extract_reasoning`` on instances and ``AIAgent._reasoning_preview_lines``
on the class, so all four wrappers stay on AIAgent).

This module does NOT import run_agent at load, so it never triggers an import
cycle.
"""

from __future__ import annotations

import re
from typing import Any


class ReasoningProcessor:
    """Stateless helpers for model thinking/reasoning blocks.

    Implemented as static methods because none of these helpers needs agent
    state; AIAgent delegates to them through thin wrappers.
    """

    @staticmethod
    def has_content_after_think_block(content: str) -> bool:
        """
        Check if content has actual text after any <think></think> blocks.

        This detects cases where the model only outputs reasoning but no actual
        response, which indicates an incomplete generation that should be retried.

        Args:
            content: The assistant message content to check

        Returns:
            True if there's meaningful content after think blocks, False otherwise
        """
        if not content:
            return False

        # Remove all <think>...</think> blocks (including nested ones, non-greedy)
        cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)

        # Check if there's any non-whitespace content remaining
        return bool(cleaned.strip())

    @staticmethod
    def strip_think_blocks(content: str) -> str:
        """Remove <think>...</think> blocks from content, returning only visible text."""
        if not content:
            return ""
        return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)

    @staticmethod
    def extract_reasoning(assistant_message: Any) -> str | None:
        """
        Extract reasoning/thinking content from an assistant message.

        OpenRouter and various providers can return reasoning in multiple formats:
        1. message.reasoning - Direct reasoning field (DeepSeek, Qwen, etc.)
        2. message.reasoning_content - Alternative field (Moonshot AI, Novita, etc.)
        3. message.reasoning_details - Array of {type, summary, ...} objects (OpenRouter unified)

        Args:
            assistant_message: The assistant message object from the API response

        Returns:
            Combined reasoning text, or None if no reasoning found
        """
        reasoning_parts = []

        # Check direct reasoning field
        if hasattr(assistant_message, "reasoning") and assistant_message.reasoning:
            reasoning_parts.append(assistant_message.reasoning)

        # Check reasoning_content field (alternative name used by some providers)
        if hasattr(assistant_message, "reasoning_content") and assistant_message.reasoning_content:
            # Don't duplicate if same as reasoning
            if assistant_message.reasoning_content not in reasoning_parts:
                reasoning_parts.append(assistant_message.reasoning_content)

        # Check reasoning_details array (OpenRouter unified format)
        # Format: [{"type": "reasoning.summary", "summary": "...", ...}, ...]
        if hasattr(assistant_message, "reasoning_details") and assistant_message.reasoning_details:
            for detail in assistant_message.reasoning_details:
                if isinstance(detail, dict):
                    # Extract summary from reasoning detail object
                    summary = detail.get("summary") or detail.get("content") or detail.get("text")
                    if summary and summary not in reasoning_parts:
                        reasoning_parts.append(summary)

        # Combine all reasoning parts
        if reasoning_parts:
            return "\n\n".join(reasoning_parts)

        return None

    @staticmethod
    def reasoning_preview_lines(
        reasoning_text: str | None,
        *,
        max_lines: int = 8,
        max_chars: int = 1600,
    ) -> list[str]:
        """Build a compact reasoning preview suitable for managed runner logs."""
        if not reasoning_text:
            return []
        lines = [line.strip() for line in str(reasoning_text).splitlines() if line.strip()]
        if not lines:
            stripped = str(reasoning_text).strip()
            lines = [stripped] if stripped else []
        if not lines:
            return []

        preview_lines = lines[:max_lines]
        preview_text = "\n".join(preview_lines)
        if len(preview_text) > max_chars:
            preview_text = preview_text[: max_chars - 3] + "..."
            return preview_text.splitlines() or [preview_text]
        if len(lines) > max_lines:
            preview_lines[-1] = preview_lines[-1] + " ..."
        return preview_lines
