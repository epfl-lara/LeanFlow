"""Assistant-response normalization extracted from run_agent.AIAgent.

``ResponseNormalizer`` owns the cluster of methods that turn a raw provider
response (OpenAI/OpenRouter chat-completions message objects, or an OpenAI
Responses-API "codex" response object) into the unified internal assistant
message schema that ``run_conversation`` depends on:

    {
        "role": "assistant",
        "content": str,
        "reasoning": str | None,
        "finish_reason": str,
        # optional: "reasoning_details", "codex_reasoning_items", "tool_calls"
    }

It mirrors the ``TokenAccounter`` (65d037d), ``ConversationManager`` (c3bbb31),
``ProviderClientFactory`` (6027ca4), ``ToolExecutor`` (74e006b) and
``InterruptController`` (06e67ca) extractions.

What moved here (behavior-preserving, logic copied verbatim):
- ``build_assistant_message`` — the unified builder used by both the
  tool-call path and the final-response path. Preserves the EXACT output
  schema (content/reasoning/finish_reason, plus the conditional
  reasoning_details, codex_reasoning_items and tool_calls keys).
- ``normalize_codex_response`` — turn a Responses-API object into an
  ``assistant_message``-like ``SimpleNamespace`` plus its finish_reason.
- ``extract_responses_message_text`` / ``extract_responses_reasoning_text`` —
  small pure helpers used ONLY by ``normalize_codex_response``.

What stays on AIAgent and is reached through the held agent reference:
- ``_extract_reasoning`` (still on AIAgent; extracted separately as the
  ReasoningProcessor), the ``verbose_logging`` / ``reasoning_callback``
  attributes, and the shared id helpers ``_split_responses_tool_id`` /
  ``_derive_responses_function_call_id`` (also used by the API-message build
  paths that remain on AIAgent). The normalizer calls them on ``self._agent``
  so any test that patches them on the agent still intercepts.

This module does NOT import run_agent at load — it only holds an agent
reference — so creating a normalizer never triggers an import cycle.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import uuid
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


class ResponseNormalizer:
    """Normalize provider responses into the unified assistant-message schema.

    Holds a reference to the owning ``AIAgent`` so it can reach the few
    collaborators that remain on the agent (reasoning extraction, the
    verbose/reasoning-callback config, and the shared Responses id helpers)
    while keeping the bulk of the normalization logic here.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    # -- Responses-API field extraction (used only by normalize_codex_response)

    def extract_responses_message_text(self, item: Any) -> str:
        """Extract assistant text from a Responses message output item."""
        content = getattr(item, "content", None)
        if not isinstance(content, list):
            return ""

        chunks: list[str] = []
        for part in content:
            ptype = getattr(part, "type", None)
            if ptype not in {"output_text", "text"}:
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                chunks.append(text)
        return "".join(chunks).strip()

    def extract_responses_reasoning_text(self, item: Any) -> str:
        """Extract a compact reasoning text from a Responses reasoning item."""
        summary = getattr(item, "summary", None)
        if isinstance(summary, list):
            chunks: list[str] = []
            for part in summary:
                text = getattr(part, "text", None)
                if isinstance(text, str) and text:
                    chunks.append(text)
            if chunks:
                return "\n".join(chunks).strip()
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            return text.strip()
        return ""

    # -- Codex / Responses-API normalization -------------------------------

    def normalize_codex_response(self, response: Any) -> tuple[Any, str]:
        """Normalize a Responses API object to an assistant_message-like object."""
        agent = self._agent
        output = getattr(response, "output", None)
        if not isinstance(output, list) or not output:
            raise RuntimeError("Responses API returned no output items")

        response_status = getattr(response, "status", None)
        if isinstance(response_status, str):
            response_status = response_status.strip().lower()
        else:
            response_status = None

        if response_status in {"failed", "cancelled"}:
            error_obj = getattr(response, "error", None)
            if isinstance(error_obj, dict):
                error_msg = error_obj.get("message") or str(error_obj)
            else:
                error_msg = (
                    str(error_obj)
                    if error_obj
                    else f"Responses API returned status '{response_status}'"
                )
            raise RuntimeError(error_msg)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_items_raw: list[dict[str, Any]] = []
        tool_calls: list[Any] = []
        has_incomplete_items = response_status in {"queued", "in_progress", "incomplete"}
        saw_commentary_phase = False
        saw_final_answer_phase = False

        for item in output:
            item_type = getattr(item, "type", None)
            item_status = getattr(item, "status", None)
            if isinstance(item_status, str):
                item_status = item_status.strip().lower()
            else:
                item_status = None

            if item_status in {"queued", "in_progress", "incomplete"}:
                has_incomplete_items = True

            if item_type == "message":
                item_phase = getattr(item, "phase", None)
                if isinstance(item_phase, str):
                    normalized_phase = item_phase.strip().lower()
                    if normalized_phase in {"commentary", "analysis"}:
                        saw_commentary_phase = True
                    elif normalized_phase in {"final_answer", "final"}:
                        saw_final_answer_phase = True
                message_text = self.extract_responses_message_text(item)
                if message_text:
                    content_parts.append(message_text)
            elif item_type == "reasoning":
                reasoning_text = self.extract_responses_reasoning_text(item)
                if reasoning_text:
                    reasoning_parts.append(reasoning_text)
                # Capture the full reasoning item for multi-turn continuity.
                # encrypted_content is an opaque blob the API needs back on
                # subsequent turns to maintain coherent reasoning chains.
                encrypted = getattr(item, "encrypted_content", None)
                if isinstance(encrypted, str) and encrypted:
                    raw_item: dict[str, Any] = {"type": "reasoning", "encrypted_content": encrypted}
                    item_id = getattr(item, "id", None)
                    if isinstance(item_id, str) and item_id:
                        raw_item["id"] = item_id
                    # Capture summary — required by the API when replaying reasoning items
                    summary = getattr(item, "summary", None)
                    if isinstance(summary, list):
                        raw_summary = []
                        for part in summary:
                            text = getattr(part, "text", None)
                            if isinstance(text, str):
                                raw_summary.append({"type": "summary_text", "text": text})
                        raw_item["summary"] = raw_summary
                    reasoning_items_raw.append(raw_item)
            elif item_type == "function_call":
                if item_status in {"queued", "in_progress", "incomplete"}:
                    continue
                fn_name = getattr(item, "name", "") or ""
                arguments = getattr(item, "arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                raw_call_id = getattr(item, "call_id", None)
                raw_item_id = getattr(item, "id", None)
                embedded_call_id, _ = agent._split_responses_tool_id(raw_item_id)
                call_id = (
                    raw_call_id
                    if isinstance(raw_call_id, str) and raw_call_id.strip()
                    else embedded_call_id
                )
                if not isinstance(call_id, str) or not call_id.strip():
                    call_id = f"call_{uuid.uuid4().hex[:12]}"
                call_id = call_id.strip()
                response_item_id = raw_item_id if isinstance(raw_item_id, str) else None
                response_item_id = agent._derive_responses_function_call_id(
                    call_id, response_item_id
                )
                tool_calls.append(
                    SimpleNamespace(
                        id=call_id,
                        call_id=call_id,
                        response_item_id=response_item_id,
                        type="function",
                        function=SimpleNamespace(name=fn_name, arguments=arguments),
                    )
                )
            elif item_type == "custom_tool_call":
                fn_name = getattr(item, "name", "") or ""
                arguments = getattr(item, "input", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                raw_call_id = getattr(item, "call_id", None)
                raw_item_id = getattr(item, "id", None)
                embedded_call_id, _ = agent._split_responses_tool_id(raw_item_id)
                call_id = (
                    raw_call_id
                    if isinstance(raw_call_id, str) and raw_call_id.strip()
                    else embedded_call_id
                )
                if not isinstance(call_id, str) or not call_id.strip():
                    call_id = f"call_{uuid.uuid4().hex[:12]}"
                call_id = call_id.strip()
                response_item_id = raw_item_id if isinstance(raw_item_id, str) else None
                response_item_id = agent._derive_responses_function_call_id(
                    call_id, response_item_id
                )
                tool_calls.append(
                    SimpleNamespace(
                        id=call_id,
                        call_id=call_id,
                        response_item_id=response_item_id,
                        type="function",
                        function=SimpleNamespace(name=fn_name, arguments=arguments),
                    )
                )

        final_text = "\n".join([p for p in content_parts if p]).strip()
        if not final_text and hasattr(response, "output_text"):
            out_text = getattr(response, "output_text", "")
            if isinstance(out_text, str):
                final_text = out_text.strip()

        assistant_message = SimpleNamespace(
            content=final_text,
            tool_calls=tool_calls,
            reasoning="\n\n".join(reasoning_parts).strip() if reasoning_parts else None,
            reasoning_content=None,
            reasoning_details=None,
            codex_reasoning_items=reasoning_items_raw or None,
        )

        if tool_calls:
            finish_reason = "tool_calls"
        elif has_incomplete_items or (saw_commentary_phase and not saw_final_answer_phase):
            finish_reason = "incomplete"
        else:
            finish_reason = "stop"
        return assistant_message, finish_reason

    # -- Unified assistant-message build (chat-completions + codex) ---------

    def build_assistant_message(self, assistant_message: Any, finish_reason: str) -> dict:
        """Build a normalized assistant message dict from an API response message.

        Handles reasoning extraction, reasoning_details, and optional tool_calls
        so both the tool-call path and the final-response path share one builder.
        """
        agent = self._agent
        reasoning_text = agent._extract_reasoning(assistant_message)

        # Fallback: extract inline <think> blocks from content when no structured
        # reasoning fields are present (some models/providers embed thinking
        # directly in the content rather than returning separate API fields).
        if not reasoning_text:
            content = assistant_message.content or ""
            think_blocks = re.findall(r"<think>(.*?)</think>", content, flags=re.DOTALL)
            if think_blocks:
                combined = "\n\n".join(b.strip() for b in think_blocks if b.strip())
                reasoning_text = combined or None

        if reasoning_text and agent.verbose_logging:
            logging.debug(f"Captured reasoning ({len(reasoning_text)} chars): {reasoning_text}")

        if reasoning_text and agent.reasoning_callback:
            with contextlib.suppress(Exception):
                agent.reasoning_callback(reasoning_text)

        msg = {
            "role": "assistant",
            "content": assistant_message.content or "",
            "reasoning": reasoning_text,
            "finish_reason": finish_reason,
        }

        if hasattr(assistant_message, "reasoning_details") and assistant_message.reasoning_details:
            # Pass reasoning_details back unmodified so providers (OpenRouter,
            # Anthropic, OpenAI) can maintain reasoning continuity across turns.
            # Each provider may include opaque fields (signature, encrypted_content)
            # that must be preserved exactly.
            raw_details = assistant_message.reasoning_details
            preserved = []
            for d in raw_details:
                if isinstance(d, dict):
                    preserved.append(d)
                elif hasattr(d, "__dict__"):
                    preserved.append(d.__dict__)
                elif hasattr(d, "model_dump"):
                    preserved.append(d.model_dump())
            if preserved:
                msg["reasoning_details"] = preserved

        # Codex Responses API: preserve encrypted reasoning items for
        # multi-turn continuity. These get replayed as input on the next turn.
        codex_items = getattr(assistant_message, "codex_reasoning_items", None)
        if codex_items:
            msg["codex_reasoning_items"] = codex_items

        if assistant_message.tool_calls:
            tool_calls = []
            for tool_call in assistant_message.tool_calls:
                raw_id = getattr(tool_call, "id", None)
                call_id = getattr(tool_call, "call_id", None)
                if not isinstance(call_id, str) or not call_id.strip():
                    embedded_call_id, _ = agent._split_responses_tool_id(raw_id)
                    call_id = embedded_call_id
                if not isinstance(call_id, str) or not call_id.strip():
                    if isinstance(raw_id, str) and raw_id.strip():
                        call_id = raw_id.strip()
                    else:
                        call_id = f"call_{uuid.uuid4().hex[:12]}"
                call_id = call_id.strip()

                response_item_id = getattr(tool_call, "response_item_id", None)
                if not isinstance(response_item_id, str) or not response_item_id.strip():
                    _, embedded_response_item_id = agent._split_responses_tool_id(raw_id)
                    response_item_id = embedded_response_item_id

                response_item_id = agent._derive_responses_function_call_id(
                    call_id,
                    response_item_id if isinstance(response_item_id, str) else None,
                )

                tc_dict = {
                    "id": call_id,
                    "call_id": call_id,
                    "response_item_id": response_item_id,
                    "type": tool_call.type,
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                # Preserve extra_content (e.g. Gemini thought_signature) so it
                # is sent back on subsequent API calls.  Without this, Gemini 3
                # thinking models reject the request with a 400 error.
                extra = getattr(tool_call, "extra_content", None)
                if extra is not None:
                    if hasattr(extra, "model_dump"):
                        extra = extra.model_dump()
                    tc_dict["extra_content"] = extra
                tool_calls.append(tc_dict)
            msg["tool_calls"] = tool_calls

        return msg
