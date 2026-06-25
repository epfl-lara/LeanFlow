"""Anthropic message-preparation cluster extracted from run_agent.AIAgent.

``AnthropicMessagePreparer`` turns OpenAI-style chat ``api_messages`` (which may
carry multimodal ``image_url`` / ``input_image`` content parts) into a flattened,
text-only message list that the native Anthropic Messages API can consume. The
native Anthropic route does not forward images directly, and this build does not
analyze image content; each image part is therefore replaced inline with a short
text placeholder and the surrounding text parts are preserved. This keeps the
route robust if image content ever arrives (e.g. via an MCP sampling request)
without depending on any vision/multimodal tooling.

What lives here:
- ``prepare_anthropic_messages_for_api`` — the entry point used by the api_caller
  build path. Deep-copies and rewrites only when an image part is present.
- ``preprocess_anthropic_content`` — flatten one message's multimodal content
  list into a single text string (image placeholders first, then the text parts).
- ``content_has_image_parts`` — the pure detector used by both.

This module does NOT import run_agent at load — it only holds an agent
reference — so creating a preparer never triggers an import cycle.
"""

from __future__ import annotations

import copy
from typing import Any


def content_has_image_parts(content: Any) -> bool:
    """Return True when ``content`` is a list carrying any image content part."""
    if not isinstance(content, list):
        return False
    for part in content:
        if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
            return True
    return False


class AnthropicMessagePreparer:
    """Flatten multimodal chat messages into Anthropic-compatible text.

    The native Anthropic route does not forward image content, so image parts are
    replaced with a short role-aware text placeholder. Stateless apart from the
    owning-agent reference kept for collaborator parity.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    def preprocess_anthropic_content(self, content: Any, role: str) -> Any:
        """Flatten multimodal content (text + image parts) into a single text string.

        Image parts become a role-aware placeholder note (placed first), followed by the
        text parts. Returns the original content unchanged when no image parts are present.
        """
        if not content_has_image_parts(content):
            return content

        role_label = {"assistant": "assistant", "tool": "tool result"}.get(role, "user")
        text_parts: list[str] = []
        image_notes: list[str] = []
        for part in content:
            if isinstance(part, str):
                if part.strip():
                    text_parts.append(part.strip())
                continue
            if not isinstance(part, dict):
                continue

            ptype = part.get("type")
            if ptype in {"text", "input_text"}:
                text = str(part.get("text", "") or "").strip()
                if text:
                    text_parts.append(text)
                continue

            if ptype in {"image_url", "input_image"}:
                image_notes.append(
                    f"[The {role_label} attached an image; "
                    "image content is not processed in this build.]"
                )
                continue

            text = str(part.get("text", "") or "").strip()
            if text:
                text_parts.append(text)

        prefix = "\n\n".join(note for note in image_notes if note).strip()
        suffix = "\n".join(text for text in text_parts if text).strip()
        if prefix and suffix:
            return f"{prefix}\n\n{suffix}"
        if prefix:
            return prefix
        if suffix:
            return suffix
        return "[A multimodal message was converted to text for Anthropic compatibility.]"

    def prepare_anthropic_messages_for_api(self, api_messages: list) -> list:
        if not any(
            isinstance(msg, dict) and content_has_image_parts(msg.get("content"))
            for msg in api_messages
        ):
            return api_messages

        transformed = copy.deepcopy(api_messages)
        for msg in transformed:
            if not isinstance(msg, dict):
                continue
            msg["content"] = self.preprocess_anthropic_content(
                msg.get("content"),
                str(msg.get("role", "user") or "user"),
            )
        return transformed
