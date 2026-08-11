#!/usr/bin/env python3
"""Handle MCP server-initiated LLM requests (``sampling/createMessage``).

This module owns the ``SamplingHandler`` callback that an MCP server uses to ask
the agent's LLM to complete a message (text or tool-use responses), plus the
small numeric-coercion and audit-path helpers it depends on.

The module does not import ``tools.mcp.mcp_tool``: it imports only
stdlib, the optional MCP sampling types (graceful import, mirroring the origin),
and ``_sanitize_error`` from ``tools.mcp.mcp_transport``. The auxiliary LLM client is
imported lazily inside the callback so it stays monkeypatch-friendly and avoids a
load-time dependency.
"""

import asyncio
import json
import logging
import math
import time
from datetime import UTC, datetime
from pathlib import Path

from core.home import leanflow_home
from tools.mcp.mcp_transport import _sanitize_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graceful import -- MCP sampling types are an optional dependency.  Mirrors the
# guarded import in tools/mcp_tool.py so SamplingHandler can build SDK result
# objects when the types are available and degrade gracefully when they aren't.
# ---------------------------------------------------------------------------

_MCP_SAMPLING_TYPES = False
try:
    from mcp.types import (
        CreateMessageResult,
        CreateMessageResultWithTools,
        ErrorData,
        SamplingCapability,
        SamplingToolsCapability,
        TextContent,
        ToolUseContent,
    )

    _MCP_SAMPLING_TYPES = True
except ImportError:
    logger.debug("MCP sampling types not available -- sampling disabled")

# ---------------------------------------------------------------------------
# Home / audit-path helpers
# ---------------------------------------------------------------------------


def _leanflow_home() -> Path:
    return leanflow_home()


def _default_sampling_audit_path() -> Path:
    return _leanflow_home() / "logs" / "mcp-sampling.jsonl"


# ---------------------------------------------------------------------------
# Sampling -- server-initiated LLM requests (MCP sampling/createMessage)
# ---------------------------------------------------------------------------


def _safe_numeric(value, default, coerce=int, minimum=1):
    """Coerce a config value to a numeric type, returning *default* on failure.

    Handles string values from YAML (e.g. ``"10"`` instead of ``10``),
    non-finite floats, and values below *minimum*.
    """
    try:
        result = coerce(value)
        if isinstance(result, float) and not math.isfinite(result):
            return default
        return max(result, minimum)
    except (TypeError, ValueError, OverflowError):
        return default


class SamplingHandler:
    """Handles sampling/createMessage requests for a single MCP server.

    Each MCPServerTask that has sampling enabled creates one SamplingHandler.
    The handler is callable and passed directly to ``ClientSession`` as
    the ``sampling_callback``.  All state (rate-limit timestamps, metrics,
    tool-loop counters) lives on the instance -- no module-level globals.

    The callback is async and runs on the MCP background event loop.  The
    sync LLM call is offloaded to a thread via ``asyncio.to_thread()`` so
    it doesn't block the event loop.
    """

    _STOP_REASON_MAP = {"stop": "endTurn", "length": "maxTokens", "tool_calls": "toolUse"}

    def __init__(self, server_name: str, config: dict):
        self.server_name = server_name
        self.max_rpm = _safe_numeric(config.get("max_rpm", 10), 10, int)
        self.timeout = _safe_numeric(config.get("timeout", 30), 30, float)
        self.max_tokens_cap = _safe_numeric(config.get("max_tokens_cap", 4096), 4096, int)
        self.max_tool_rounds = _safe_numeric(
            config.get("max_tool_rounds", 5),
            5,
            int,
            minimum=0,
        )
        self.model_override = config.get("model")
        self.allowed_models = config.get("allowed_models", [])

        _log_levels = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING}
        self.audit_level = _log_levels.get(
            str(config.get("log_level", "info")).lower(),
            logging.INFO,
        )
        self.audit_jsonl_enabled = bool(config.get("audit_jsonl", False))
        configured_path = str(config.get("audit_jsonl_path", "") or "").strip()
        self.audit_jsonl_path = (
            Path(configured_path).expanduser()
            if configured_path
            else _default_sampling_audit_path()
        )

        # Per-instance state
        self._rate_timestamps: list[float] = []
        self._tool_loop_count = 0
        self.metrics = {"requests": 0, "errors": 0, "tokens_used": 0, "tool_use_count": 0}

    def _append_audit_event(self, event: str, **payload) -> None:
        if not self.audit_jsonl_enabled:
            return
        entry = {
            "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "server": self.server_name,
            "event": event,
            "payload": payload,
        }
        try:
            self.audit_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True))
                handle.write("\n")
        except Exception:
            logger.debug(
                "Failed to write MCP sampling audit log for %s", self.server_name, exc_info=True
            )

    # -- Rate limiting -------------------------------------------------------

    def _check_rate_limit(self) -> bool:
        """Sliding-window rate limiter.  Returns True if request is allowed."""
        now = time.time()
        window = now - 60
        self._rate_timestamps[:] = [t for t in self._rate_timestamps if t > window]
        if len(self._rate_timestamps) >= self.max_rpm:
            return False
        self._rate_timestamps.append(now)
        return True

    # -- Model resolution ----------------------------------------------------

    def _resolve_model(self, preferences) -> str | None:
        """Config override > server hint > None (use default)."""
        if self.model_override:
            return self.model_override
        if preferences and hasattr(preferences, "hints") and preferences.hints:
            for hint in preferences.hints:
                if hasattr(hint, "name") and hint.name:
                    return hint.name
        return None

    # -- Message conversion --------------------------------------------------

    @staticmethod
    def _extract_tool_result_text(block) -> str:
        """Extract text from a ToolResultContent block."""
        if not hasattr(block, "content") or block.content is None:
            return ""
        items = block.content if isinstance(block.content, list) else [block.content]
        return "\n".join(item.text for item in items if hasattr(item, "text"))

    def _convert_messages(self, params) -> list[dict]:
        """Convert MCP SamplingMessages to OpenAI format.

        Uses ``msg.content_as_list`` (SDK helper) so single-block and
        list-of-blocks are handled uniformly.  Dispatches per block type
        with ``isinstance`` on real SDK types when available, falling back
        to duck-typing via ``hasattr`` for compatibility.
        """
        messages: list[dict] = []
        for msg in params.messages:
            blocks = (
                msg.content_as_list
                if hasattr(msg, "content_as_list")
                else (msg.content if isinstance(msg.content, list) else [msg.content])
            )

            # Separate blocks by kind
            tool_results = [b for b in blocks if hasattr(b, "toolUseId")]
            tool_uses = [
                b
                for b in blocks
                if hasattr(b, "name") and hasattr(b, "input") and not hasattr(b, "toolUseId")
            ]
            content_blocks = [
                b
                for b in blocks
                if not hasattr(b, "toolUseId") and not (hasattr(b, "name") and hasattr(b, "input"))
            ]

            # Emit tool result messages (role: tool)
            for tr in tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tr.toolUseId,
                        "content": self._extract_tool_result_text(tr),
                    }
                )

            # Emit assistant tool_calls message
            if tool_uses:
                tc_list: list[dict] = []
                for tu in tool_uses:
                    tc_list.append(
                        {
                            "id": getattr(tu, "id", f"call_{len(tc_list)}"),
                            "type": "function",
                            "function": {
                                "name": tu.name,
                                "arguments": (
                                    json.dumps(tu.input)
                                    if isinstance(tu.input, dict)
                                    else str(tu.input)
                                ),
                            },
                        }
                    )
                msg_dict: dict = {"role": msg.role, "tool_calls": tc_list}
                # Include any accompanying text
                text_parts = [b.text for b in content_blocks if hasattr(b, "text")]
                if text_parts:
                    msg_dict["content"] = "\n".join(text_parts)
                messages.append(msg_dict)
            elif content_blocks:
                # Pure text/image content
                if len(content_blocks) == 1 and hasattr(content_blocks[0], "text"):
                    messages.append({"role": msg.role, "content": content_blocks[0].text})
                else:
                    parts = []
                    for block in content_blocks:
                        if hasattr(block, "text"):
                            parts.append({"type": "text", "text": block.text})
                        elif hasattr(block, "data") and hasattr(block, "mimeType"):
                            parts.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{block.mimeType};base64,{block.data}"
                                    },
                                }
                            )
                        else:
                            logger.warning(
                                "Unsupported sampling content block type: %s (skipped)",
                                type(block).__name__,
                            )
                    if parts:
                        messages.append({"role": msg.role, "content": parts})

        return messages

    # -- Error helper --------------------------------------------------------

    @staticmethod
    def _error(message: str, code: int = -1):
        """Return ErrorData (MCP spec) or raise as fallback."""
        if _MCP_SAMPLING_TYPES:
            return ErrorData(code=code, message=message)
        raise Exception(message)

    # -- Response building ---------------------------------------------------

    def _build_tool_use_result(self, choice, response):
        """Build a CreateMessageResultWithTools from an LLM tool_calls response."""
        self.metrics["tool_use_count"] += 1

        # Tool loop governance
        if self.max_tool_rounds == 0:
            self._tool_loop_count = 0
            self._append_audit_event("error", kind="tool-loop-disabled")
            return self._error(
                f"Tool loops disabled for server '{self.server_name}' (max_tool_rounds=0)"
            )

        self._tool_loop_count += 1
        if self._tool_loop_count > self.max_tool_rounds:
            self._tool_loop_count = 0
            self._append_audit_event(
                "error",
                kind="tool-loop-limit",
                max_tool_rounds=self.max_tool_rounds,
            )
            return self._error(
                f"Tool loop limit exceeded for server '{self.server_name}' "
                f"(max {self.max_tool_rounds} rounds)"
            )

        content_blocks = []
        for tc in choice.message.tool_calls:
            args = tc.function.arguments
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                except (json.JSONDecodeError, ValueError):
                    logger.warning(
                        "MCP server '%s': malformed tool_calls arguments "
                        "from LLM (wrapping as raw): %.100s",
                        self.server_name,
                        args,
                    )
                    parsed = {"_raw": args}
            else:
                parsed = args if isinstance(args, dict) else {"_raw": str(args)}

            content_blocks.append(
                ToolUseContent(
                    type="tool_use",
                    id=tc.id,
                    name=tc.function.name,
                    input=parsed,
                )
            )

        logger.log(
            self.audit_level,
            "MCP server '%s' sampling response: model=%s, tokens=%s, tool_calls=%d",
            self.server_name,
            response.model,
            getattr(getattr(response, "usage", None), "total_tokens", "?"),
            len(content_blocks),
        )
        self._append_audit_event(
            "response",
            kind="tool_use",
            model=str(response.model or ""),
            total_tokens=int(getattr(getattr(response, "usage", None), "total_tokens", 0) or 0),
            tool_calls=len(content_blocks),
        )

        return CreateMessageResultWithTools(
            role="assistant",
            content=content_blocks,
            model=response.model,
            stopReason="toolUse",
        )

    def _build_text_result(self, choice, response):
        """Build a CreateMessageResult from a normal text response."""
        self._tool_loop_count = 0  # reset on text response
        response_text = choice.message.content or ""

        logger.log(
            self.audit_level,
            "MCP server '%s' sampling response: model=%s, tokens=%s",
            self.server_name,
            response.model,
            getattr(getattr(response, "usage", None), "total_tokens", "?"),
        )
        self._append_audit_event(
            "response",
            kind="text",
            model=str(response.model or ""),
            total_tokens=int(getattr(getattr(response, "usage", None), "total_tokens", 0) or 0),
            finish_reason=str(choice.finish_reason or ""),
        )

        return CreateMessageResult(
            role="assistant",
            content=TextContent(type="text", text=_sanitize_error(response_text)),
            model=response.model,
            stopReason=self._STOP_REASON_MAP.get(choice.finish_reason, "endTurn"),
        )

    # -- Session kwargs helper -----------------------------------------------

    def session_kwargs(self) -> dict:
        """Return kwargs to pass to ClientSession for sampling support."""
        return {
            "sampling_callback": self,
            "sampling_capabilities": SamplingCapability(
                tools=SamplingToolsCapability(),
            ),
        }

    # -- Main callback -------------------------------------------------------

    async def __call__(self, context, params):
        """Sampling callback invoked by the MCP SDK.

        Conforms to ``SamplingFnT`` protocol.  Returns
        ``CreateMessageResult``, ``CreateMessageResultWithTools``, or
        ``ErrorData``.
        """
        # Rate limit
        if not self._check_rate_limit():
            logger.warning(
                "MCP server '%s' sampling rate limit exceeded (%d/min)",
                self.server_name,
                self.max_rpm,
            )
            self.metrics["errors"] += 1
            self._append_audit_event("error", kind="rate-limit", max_rpm=self.max_rpm)
            return self._error(
                f"Sampling rate limit exceeded for server '{self.server_name}' "
                f"({self.max_rpm} requests/minute)"
            )

        # Resolve model
        model = self._resolve_model(getattr(params, "modelPreferences", None))

        # Get auxiliary LLM client via centralized router
        from agent.providers.auxiliary_client import call_llm

        # Model whitelist check (we need to resolve model before calling)
        resolved_model = model or self.model_override or ""

        if self.allowed_models and resolved_model and resolved_model not in self.allowed_models:
            logger.warning(
                "MCP server '%s' requested model '%s' not in allowed_models",
                self.server_name,
                resolved_model,
            )
            self.metrics["errors"] += 1
            self._append_audit_event(
                "error",
                kind="model-not-allowed",
                model=str(resolved_model or ""),
            )
            return self._error(
                f"Model '{resolved_model}' not allowed for server "
                f"'{self.server_name}'. Allowed: {', '.join(self.allowed_models)}"
            )

        # Convert messages
        messages = self._convert_messages(params)
        if hasattr(params, "systemPrompt") and params.systemPrompt:
            messages.insert(0, {"role": "system", "content": params.systemPrompt})

        # Build LLM call kwargs
        max_tokens = min(params.maxTokens, self.max_tokens_cap)
        call_temperature = None
        if hasattr(params, "temperature") and params.temperature is not None:
            call_temperature = params.temperature

        # Forward server-provided tools
        call_tools = None
        server_tools = getattr(params, "tools", None)
        if server_tools:
            call_tools = [
                {
                    "type": "function",
                    "function": {
                        "name": getattr(t, "name", ""),
                        "description": getattr(t, "description", "") or "",
                        "parameters": getattr(t, "inputSchema", {}) or {},
                    },
                }
                for t in server_tools
            ]

        logger.log(
            self.audit_level,
            "MCP server '%s' sampling request: model=%s, max_tokens=%d, messages=%d",
            self.server_name,
            resolved_model,
            max_tokens,
            len(messages),
        )
        self._append_audit_event(
            "request",
            model=str(resolved_model or ""),
            max_tokens=max_tokens,
            message_count=len(messages),
            tool_count=len(call_tools or []),
        )

        # Offload sync LLM call to thread (non-blocking)
        def _sync_call():
            return call_llm(
                task="mcp",
                model=resolved_model or None,
                messages=messages,
                temperature=call_temperature,
                max_tokens=max_tokens,
                tools=call_tools,
                timeout=self.timeout,
            )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(_sync_call),
                timeout=self.timeout,
            )
        except TimeoutError:
            self.metrics["errors"] += 1
            self._append_audit_event("error", kind="timeout", timeout=self.timeout)
            return self._error(
                f"Sampling LLM call timed out after {self.timeout}s for server '{self.server_name}'"
            )
        except Exception as exc:
            self.metrics["errors"] += 1
            self._append_audit_event("error", kind="exception", message=_sanitize_error(str(exc)))
            return self._error(f"Sampling LLM call failed: {_sanitize_error(str(exc))}")

        # Guard against empty choices (content filtering, provider errors)
        if not getattr(response, "choices", None):
            self.metrics["errors"] += 1
            self._append_audit_event("error", kind="empty-response")
            return self._error(
                f"LLM returned empty response (no choices) for server '{self.server_name}'"
            )

        # Track metrics
        choice = response.choices[0]
        self.metrics["requests"] += 1
        total_tokens = getattr(getattr(response, "usage", None), "total_tokens", 0)
        if isinstance(total_tokens, int):
            self.metrics["tokens_used"] += total_tokens

        # Dispatch based on response type
        if (
            choice.finish_reason == "tool_calls"
            and hasattr(choice.message, "tool_calls")
            and choice.message.tool_calls
        ):
            return self._build_tool_use_result(choice, response)

        return self._build_text_result(choice, response)
