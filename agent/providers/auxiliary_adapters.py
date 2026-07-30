"""OpenAI-client-compatible provider adapters for the auxiliary router.

Houses the drop-in shim classes that let non-OpenAI backends be driven through
the standard ``client.chat.completions.create(**kwargs)`` /
``response.choices[0].message.content`` surface that every auxiliary consumer
expects:

  * Codex OAuth → Responses API (``CodexAuxiliaryClient`` and its async twin),
    plus the ``chat.completions`` content-block converter used to translate
    multimodal payloads into the Responses ``input_text`` / ``input_image`` form.
  * Native Anthropic Messages API (``AnthropicAuxiliaryClient`` and its async
    twin), reusing ``agent.anthropic_adapter`` for request/response shaping.

These adapters are pure translation layers: they hold a reference to a real
backend client and reshape kwargs/results.  They never read auxiliary routing
state, env vars, or config, so they form a closed cluster that ``auxiliary_client``
re-exports unchanged (importers keep resolving ``auxiliary_client.<name>``).

This module must NOT import ``agent.auxiliary_client`` — that would create an
import cycle, since ``auxiliary_client`` re-exports these names.
"""

import logging
from types import SimpleNamespace
from typing import Any

from openai import OpenAI

logger = logging.getLogger(__name__)


# ── Codex Responses → chat.completions adapter ─────────────────────────────
# All auxiliary consumers call client.chat.completions.create(**kwargs) and
# read response.choices[0].message.content. This adapter translates those
# calls to the Codex Responses API so callers don't need any changes.


def _convert_content_for_responses(content: Any) -> Any:
    """Convert chat.completions content to Responses API format.

    chat.completions uses:
      {"type": "text", "text": "..."}
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}

    Responses API uses:
      {"type": "input_text", "text": "..."}
      {"type": "input_image", "image_url": "data:image/png;base64,..."}

    If content is a plain string, it's returned as-is (the Responses API
    accepts strings directly for text-only messages).
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content else ""

    converted: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type", "")
        if ptype == "text":
            converted.append({"type": "input_text", "text": part.get("text", "")})
        elif ptype == "image_url":
            # chat.completions nests the URL: {"image_url": {"url": "..."}}
            image_data = part.get("image_url", {})
            url = image_data.get("url", "") if isinstance(image_data, dict) else str(image_data)
            entry: dict[str, Any] = {"type": "input_image", "image_url": url}
            # Preserve detail if specified
            detail = image_data.get("detail") if isinstance(image_data, dict) else None
            if detail:
                entry["detail"] = detail
            converted.append(entry)
        elif ptype in ("input_text", "input_image"):
            # Already in Responses format — pass through
            converted.append(part)
        else:
            # Unknown content type — try to preserve as text
            text = part.get("text", "")
            if text:
                converted.append({"type": "input_text", "text": text})

    return converted or ""


class _CodexCompletionsAdapter:
    """Drop-in shim that accepts chat.completions.create() kwargs and
    routes them through the Codex Responses streaming API."""

    def __init__(self, real_client: OpenAI, model: str):
        self._client = real_client
        self._model = model

    def create(self, **kwargs) -> Any:
        """Translate OpenAI chat.completions kwargs to Codex Responses API, stream the response, and return a chat.completions-shaped result. Extracts system/user messages, converts multimodal content blocks (text/image_url) to Responses format, handles tool definitions, and reshapes output (text, function calls, usage) to match OpenAI response objects."""
        messages = kwargs.get("messages", [])
        model = kwargs.get("model", self._model)
        temperature = kwargs.get("temperature")

        # Separate system/instructions from conversation messages.
        # Convert chat.completions multimodal content blocks to Responses
        # API format (input_text / input_image instead of text / image_url).
        instructions = "You are a helpful assistant."
        input_msgs: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content") or ""
            if role == "system":
                instructions = content if isinstance(content, str) else str(content)
            else:
                input_msgs.append(
                    {
                        "role": role,
                        "content": _convert_content_for_responses(content),
                    }
                )

        resp_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_msgs or [{"role": "user", "content": ""}],
            "store": False,
        }
        timeout = kwargs.get("timeout")
        if timeout is not None:
            # Auxiliary callers rely on a bounded read timeout. Dropping this
            # value here can otherwise pin the entire foreground planner while
            # a Responses stream waits indefinitely for its first event.
            resp_kwargs["timeout"] = timeout

        # Note: the Codex endpoint (chatgpt.com/backend-api/codex) does NOT
        # support max_output_tokens or temperature — omit to avoid 400 errors.

        # Tools support for flush_memories and similar callers
        tools = kwargs.get("tools")
        if tools:
            converted = []
            for t in tools:
                fn = t.get("function", {}) if isinstance(t, dict) else {}
                name = fn.get("name")
                if not name:
                    continue
                converted.append(
                    {
                        "type": "function",
                        "name": name,
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                    }
                )
            if converted:
                resp_kwargs["tools"] = converted

        # Stream and collect the response
        text_parts: list[str] = []
        streamed_text_parts: list[str] = []
        tool_calls_raw: list[Any] = []
        usage = None

        try:
            request_client = self._client
            with_options = getattr(request_client, "with_options", None)
            if timeout is not None and callable(with_options):
                # The summarizer/coach layer owns any retry policy. Letting the
                # SDK retry each timed-out stream multiplies one nominal
                # 30-second attempt into roughly 90 seconds before the caller's
                # own retries even begin.
                request_client = with_options(max_retries=0)
            with request_client.responses.stream(**resp_kwargs) as stream:
                for event in stream:
                    # The ChatGPT Codex endpoint may emit complete text deltas
                    # while returning an empty final ``output`` envelope.
                    # Retain deltas as a repair source instead of silently
                    # translating a valid response into ``no_answer``.
                    if getattr(event, "type", None) == "response.output_text.delta":
                        delta = getattr(event, "delta", "")
                        if delta:
                            streamed_text_parts.append(str(delta))
                final = stream.get_final_response()

            # Extract text and tool calls from the Responses output
            for item in getattr(final, "output", []):
                item_type = getattr(item, "type", None)
                if item_type == "message":
                    for part in getattr(item, "content", []):
                        ptype = getattr(part, "type", None)
                        if ptype in ("output_text", "text"):
                            text_parts.append(getattr(part, "text", ""))
                elif item_type == "function_call":
                    tool_calls_raw.append(
                        SimpleNamespace(
                            id=getattr(item, "call_id", ""),
                            type="function",
                            function=SimpleNamespace(
                                name=getattr(item, "name", ""),
                                arguments=getattr(item, "arguments", "{}"),
                            ),
                        )
                    )

            resp_usage = getattr(final, "usage", None)
            if resp_usage:
                usage = SimpleNamespace(
                    prompt_tokens=getattr(resp_usage, "input_tokens", 0),
                    completion_tokens=getattr(resp_usage, "output_tokens", 0),
                    total_tokens=getattr(resp_usage, "total_tokens", 0),
                )
        except Exception as exc:
            logger.debug("Codex auxiliary Responses API call failed: %s", exc)
            raise

        content = ("".join(text_parts) or "".join(streamed_text_parts)).strip() or None

        # Build a response that looks like chat.completions
        message = SimpleNamespace(
            role="assistant",
            content=content,
            tool_calls=tool_calls_raw or None,
        )
        choice = SimpleNamespace(
            index=0,
            message=message,
            finish_reason="stop" if not tool_calls_raw else "tool_calls",
        )
        return SimpleNamespace(
            choices=[choice],
            model=model,
            usage=usage,
        )


class _CodexChatShim:
    """Wraps the adapter to provide client.chat.completions.create()."""

    def __init__(self, adapter: _CodexCompletionsAdapter):
        self.completions = adapter


class CodexAuxiliaryClient:
    """OpenAI-client-compatible wrapper that routes through Codex Responses API.

    Consumers can call client.chat.completions.create(**kwargs) as normal.
    Also exposes .api_key and .base_url for introspection by async wrappers.
    """

    def __init__(self, real_client: OpenAI, model: str):
        self._real_client = real_client
        adapter = _CodexCompletionsAdapter(real_client, model)
        self.chat = _CodexChatShim(adapter)
        self.api_key = real_client.api_key
        self.base_url = real_client.base_url

    def close(self):
        self._real_client.close()


class _AsyncCodexCompletionsAdapter:
    """Async version of the Codex Responses adapter.

    Wraps the sync adapter via asyncio.to_thread() so async consumers
    (web_tools, session_search) can await it as normal.
    """

    def __init__(self, sync_adapter: _CodexCompletionsAdapter):
        self._sync = sync_adapter

    async def create(self, **kwargs) -> Any:
        import asyncio

        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncCodexChatShim:
    def __init__(self, adapter: _AsyncCodexCompletionsAdapter):
        self.completions = adapter


class AsyncCodexAuxiliaryClient:
    """Async-compatible wrapper matching AsyncOpenAI.chat.completions.create()."""

    def __init__(self, sync_wrapper: "CodexAuxiliaryClient"):
        self._sync_wrapper = sync_wrapper
        sync_adapter = sync_wrapper.chat.completions
        async_adapter = _AsyncCodexCompletionsAdapter(sync_adapter)
        self.chat = _AsyncCodexChatShim(async_adapter)
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url

    async def close(self) -> None:
        """Close the wrapped synchronous client without blocking the event loop."""
        import asyncio

        await asyncio.to_thread(self._sync_wrapper.close)


class _AnthropicCompletionsAdapter:
    """OpenAI-client-compatible adapter for Anthropic Messages API."""

    def __init__(self, real_client: Any, model: str):
        self._client = real_client
        self._model = model

    def create(self, **kwargs) -> Any:
        """Translate OpenAI chat.completions kwargs through Anthropic Messages API using anthropic_adapter helpers, then reshape the response back to chat.completions format. Normalizes tool_choice format, delegates request building to build_anthropic_kwargs, and wraps Anthropic response (message, finish_reason, usage) in a compatible result object."""
        from agent.providers.anthropic_adapter import (
            build_anthropic_kwargs,
            normalize_anthropic_response,
        )

        messages = kwargs.get("messages", [])
        model = kwargs.get("model", self._model)
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")
        max_tokens = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens") or 2000
        temperature = kwargs.get("temperature")

        normalized_tool_choice = None
        if isinstance(tool_choice, str):
            normalized_tool_choice = tool_choice
        elif isinstance(tool_choice, dict):
            choice_type = str(tool_choice.get("type", "")).lower()
            if choice_type == "function":
                normalized_tool_choice = tool_choice.get("function", {}).get("name")
            elif choice_type in {"auto", "required", "none"}:
                normalized_tool_choice = choice_type

        anthropic_kwargs = build_anthropic_kwargs(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_config=None,
            tool_choice=normalized_tool_choice,
        )
        if temperature is not None:
            anthropic_kwargs["temperature"] = temperature

        response = self._client.messages.create(**anthropic_kwargs)
        assistant_message, finish_reason = normalize_anthropic_response(response)

        usage = None
        if hasattr(response, "usage") and response.usage:
            prompt_tokens = getattr(response.usage, "input_tokens", 0) or 0
            completion_tokens = getattr(response.usage, "output_tokens", 0) or 0
            total_tokens = getattr(response.usage, "total_tokens", 0) or (
                prompt_tokens + completion_tokens
            )
            usage = SimpleNamespace(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )

        choice = SimpleNamespace(
            index=0,
            message=assistant_message,
            finish_reason=finish_reason,
        )
        return SimpleNamespace(
            choices=[choice],
            model=model,
            usage=usage,
        )


class _AnthropicChatShim:
    def __init__(self, adapter: _AnthropicCompletionsAdapter):
        self.completions = adapter


class AnthropicAuxiliaryClient:
    """OpenAI-client-compatible wrapper over a native Anthropic client."""

    def __init__(self, real_client: Any, model: str, api_key: str, base_url: str):
        self._real_client = real_client
        adapter = _AnthropicCompletionsAdapter(real_client, model)
        self.chat = _AnthropicChatShim(adapter)
        self.api_key = api_key
        self.base_url = base_url

    def close(self):
        close_fn = getattr(self._real_client, "close", None)
        if callable(close_fn):
            close_fn()


class _AsyncAnthropicCompletionsAdapter:
    def __init__(self, sync_adapter: _AnthropicCompletionsAdapter):
        self._sync = sync_adapter

    async def create(self, **kwargs) -> Any:
        import asyncio

        return await asyncio.to_thread(self._sync.create, **kwargs)


class _AsyncAnthropicChatShim:
    def __init__(self, adapter: _AsyncAnthropicCompletionsAdapter):
        self.completions = adapter


class AsyncAnthropicAuxiliaryClient:
    def __init__(self, sync_wrapper: "AnthropicAuxiliaryClient"):
        self._sync_wrapper = sync_wrapper
        sync_adapter = sync_wrapper.chat.completions
        async_adapter = _AsyncAnthropicCompletionsAdapter(sync_adapter)
        self.chat = _AsyncAnthropicChatShim(async_adapter)
        self.api_key = sync_wrapper.api_key
        self.base_url = sync_wrapper.base_url

    async def close(self) -> None:
        """Close the wrapped synchronous client without blocking the event loop."""
        import asyncio

        await asyncio.to_thread(self._sync_wrapper.close)
