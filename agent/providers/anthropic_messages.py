"""Anthropic message-preparation cluster extracted from run_agent.AIAgent.

``AnthropicMessagePreparer`` owns the cohesive cluster of helpers that turn
OpenAI-style chat ``api_messages`` (which may carry multimodal ``image_url`` /
``input_image`` content parts) into a flattened, text-only message list that the
native Anthropic Messages API can consume. The native Anthropic route in this
codebase does not forward images directly; instead each image is described via
the vision-analysis tool and replaced inline with a textual note, and those
descriptions are memoized so re-sending the same conversation never re-analyzes
the same image.

It mirrors the ``ResponseNormalizer`` (a-prior), ``TokenAccounter`` (65d037d),
``ConversationManager`` (c3bbb31), ``ProviderClientFactory`` (6027ca4) and
``ToolExecutor`` (74e006b) collaborator extractions.

What moved here (behavior-preserving, logic copied verbatim):
- ``prepare_anthropic_messages_for_api`` — the entry point used by the api_caller
  build path. Deep-copies and rewrites only when an image part is present.
- ``preprocess_anthropic_content`` — flatten one message's multimodal content
  list into a single text string (image notes first, then the text parts).
- ``describe_image_for_anthropic_fallback`` — analyze one image (materializing a
  ``data:`` URL to a temp file when needed) and build the cached textual note.
- ``content_has_image_parts`` / ``materialize_data_url_for_vision`` — the two
  pure static helpers used only by the cluster.

Owned state: ``image_fallback_cache`` (the sha256(image_url) → note memo). The
former ``AIAgent._anthropic_image_fallback_cache`` dict now lives here; AIAgent
exposes it through a property shim so existing reads/writes keep working.

This module does NOT import run_agent at load — it only holds an agent
reference — so creating a preparer never triggers an import cycle.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def content_has_image_parts(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    for part in content:
        if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
            return True
    return False


def materialize_data_url_for_vision(image_url: str) -> tuple[str, Path | None]:
    header, _, data = str(image_url or "").partition(",")
    mime = "image/jpeg"
    if header.startswith("data:"):
        mime_part = header[len("data:"):].split(";", 1)[0].strip()
        if mime_part.startswith("image/"):
            mime = mime_part
    suffix = {
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
    }.get(mime, ".jpg")
    tmp = tempfile.NamedTemporaryFile(prefix="anthropic_image_", suffix=suffix, delete=False)
    with tmp:
        tmp.write(base64.b64decode(data))
    path = Path(tmp.name)
    return str(path), path


class AnthropicMessagePreparer:
    """Flatten multimodal chat messages into Anthropic-compatible text.

    Holds a reference to the owning ``AIAgent`` so the cache it materializes can
    be shared back through the agent's ``_anthropic_image_fallback_cache``
    property shim. Owns the per-image description memo directly.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self.image_fallback_cache: dict[str, str] = {}

    def describe_image_for_anthropic_fallback(self, image_url: str, role: str) -> str:
        cache_key = hashlib.sha256(str(image_url or "").encode("utf-8")).hexdigest()
        cached = self.image_fallback_cache.get(cache_key)
        if cached:
            return cached

        role_label = {
            "assistant": "assistant",
            "tool": "tool result",
        }.get(role, "user")
        analysis_prompt = (
            "Describe everything visible in this image in thorough detail. "
            "Include any text, code, UI, data, objects, people, layout, colors, "
            "and any other notable visual information."
        )

        vision_source = str(image_url or "")
        cleanup_path: Path | None = None
        if vision_source.startswith("data:"):
            vision_source, cleanup_path = materialize_data_url_for_vision(vision_source)

        description = ""
        try:
            from tools.implementations.vision_tools import vision_analyze_tool

            result_json = asyncio.run(
                vision_analyze_tool(image_url=vision_source, user_prompt=analysis_prompt)
            )
            result = json.loads(result_json) if isinstance(result_json, str) else {}
            description = (result.get("analysis") or "").strip()
        except Exception as e:
            description = f"Image analysis failed: {e}"
        finally:
            if cleanup_path and cleanup_path.exists():
                with contextlib.suppress(OSError):
                    cleanup_path.unlink()

        if not description:
            description = "Image analysis failed."

        note = f"[The {role_label} attached an image. Here's what it contains:\n{description}]"
        if vision_source and not str(image_url or "").startswith("data:"):
            note += (
                f"\n[If you need a closer look, use vision_analyze with image_url: {vision_source}]"
            )

        self.image_fallback_cache[cache_key] = note
        return note

    def preprocess_anthropic_content(self, content: Any, role: str) -> Any:
        if not content_has_image_parts(content):
            return content

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
                image_data = part.get("image_url", {})
                image_url = image_data.get("url", "") if isinstance(image_data, dict) else str(image_data or "")
                if image_url:
                    image_notes.append(self.describe_image_for_anthropic_fallback(image_url, role))
                else:
                    image_notes.append("[An image was attached but no image source was available.]")
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
