"""Session/message persistence and per-turn API-message shaping extracted from
run_agent.AIAgent.

``ConversationManager`` owns the cohesive cluster of behavior around persisting a
conversation and shaping it for the wire: writing the JSON session log, flushing
new messages to the SQLite session store, saving ShareGPT trajectories, and
building the exact message payload sent for one chat-completions turn. It mirrors
the ``TokenAccounter`` (65d037d), ``ProviderClientFactory`` (6027ca4) and
``ToolExecutor`` (74e006b) extractions.

The manager holds a reference to the owning ``AIAgent`` and reaches the
intrinsic conversation state through it -- ``_session_db`` (optional SessionDB),
``session_id``, ``session_log_file``, ``_session_messages``, ``_last_flushed_db_idx``,
``_cached_system_prompt``, model/provider metadata, ``tools``, ``prefill_messages``,
the prompt-caching flags, the ``context_compressor`` and the persist-override
fields. That state is intrinsically the agent's, so reaching it via the agent is
the genuine boundary; the persistence/log/trajectory/message-build *logic* (the
methods below) has left the god class.

A couple of names the moved logic depends on stay on ``AIAgent`` because they
are tested directly on the class / used at multiple non-persistence sites and are
reached back through the agent reference:
- ``_sanitize_tool_calls_for_strict_api`` (a staticmethod used at three send
  sites, only one of which is the per-turn build),
- ``_session_usage_summary`` / ``_log_session_usage_summary`` (driven by the
  ``TokenAccounter`` and logged from several exit paths).

This module does NOT import ``run_agent`` at module load, so importing it never
creates a cycle; it relies only on the leaf helpers it shares with run_agent
(``agent.trajectory``, ``agent.prompt_caching``, ``utils.atomic_json_write``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agent.prompting.prompt_caching import apply_anthropic_cache_control
from agent.runtime.trajectory import convert_scratchpad_to_think
from agent.runtime.trajectory import save_trajectory as _save_trajectory_to_file

if TYPE_CHECKING:  # pragma: no cover - typing only
    from run_agent import AIAgent

logger = logging.getLogger(__name__)


# ── Pure helpers (no agent state) ───────────────────────────────────────────
def clean_session_content(content: str) -> str:
    """Convert REASONING_SCRATCHPAD to think tags and clean up whitespace.

    Behavior-identical to the former ``AIAgent._clean_session_content``
    staticmethod; that staticmethod is now a thin wrapper around this function.
    """
    if not content:
        return content
    content = convert_scratchpad_to_think(content)
    content = re.sub(r"\n+(<think>)", r"\n\1", content)
    content = re.sub(r"(</think>)\n+", r"\1\n", content)
    return content.strip()


class ConversationManager:
    """Persists and shapes a conversation on behalf of an :class:`AIAgent`.

    Constructed with the owning agent; all conversation state is reached through
    it. The persistence / session-log / trajectory / message-build logic is
    behavior-preserving relative to the former ``AIAgent`` methods of the same
    names.
    """

    def __init__(self, agent: AIAgent) -> None:
        self._agent = agent

    # ── User-message persistence override ───────────────────────────────────
    def apply_persist_user_message_override(self, messages: list[dict]) -> None:
        """Rewrite the current-turn user message before persistence/return.

        Some call paths need an API-only user-message variant without letting
        that synthetic text leak into persisted transcripts or resumed session
        history. When an override is configured for the active turn, mutate the
        in-memory messages list in place so both persistence and returned
        history stay clean.
        """
        agent = self._agent
        idx = getattr(agent, "_persist_user_message_idx", None)
        override = getattr(agent, "_persist_user_message_override", None)
        if override is None or idx is None:
            return
        if 0 <= idx < len(messages):
            msg = messages[idx]
            if isinstance(msg, dict) and msg.get("role") == "user":
                msg["content"] = override

    # ── Persistence orchestration ───────────────────────────────────────────
    def persist_session(self, messages: list[dict], conversation_history: list[dict] = None):
        """Save session state to both JSON log and SQLite on any exit path.

        Ensures conversations are never lost, even on errors or early returns.
        """
        agent = self._agent
        self.apply_persist_user_message_override(messages)
        agent._session_messages = messages
        agent._log_session_usage_summary()
        # Route the save/flush steps through the AGENT's wrapper methods (not
        # ``self.save_session_log`` / ``self.flush_messages_to_session_db``
        # directly) so callers/tests that patch ``agent._save_session_log`` /
        # ``agent._flush_messages_to_session_db`` still intercept them exactly as
        # before the extraction.
        agent._save_session_log(messages)
        agent._flush_messages_to_session_db(messages, conversation_history)

    def flush_messages_to_session_db(
        self, messages: list[dict], conversation_history: list[dict] = None
    ):
        """Persist any un-flushed messages to the SQLite session store.

        Uses _last_flushed_db_idx to track which messages have already been
        written, so repeated calls (from multiple exit paths) only write
        truly new messages — preventing the duplicate-write bug (#860).
        """
        agent = self._agent
        if not agent._session_db:
            return
        self.apply_persist_user_message_override(messages)
        try:
            start_idx = len(conversation_history) if conversation_history else 0
            flush_from = max(start_idx, agent._last_flushed_db_idx)
            for msg in messages[flush_from:]:
                role = msg.get("role", "unknown")
                content = msg.get("content")
                tool_calls_data = None
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    tool_calls_data = [
                        {"name": tc.function.name, "arguments": tc.function.arguments}
                        for tc in msg.tool_calls
                    ]
                elif isinstance(msg.get("tool_calls"), list):
                    tool_calls_data = msg["tool_calls"]
                agent._session_db.append_message(
                    session_id=agent.session_id,
                    role=role,
                    content=content,
                    tool_name=msg.get("tool_name"),
                    tool_calls=tool_calls_data,
                    tool_call_id=msg.get("tool_call_id"),
                    finish_reason=msg.get("finish_reason"),
                )
            agent._last_flushed_db_idx = len(messages)
        except Exception as e:
            logger.debug("Session DB append_message failed: %s", e)

    # ── JSON session log ────────────────────────────────────────────────────
    def save_session_log(self, messages: list[dict[str, Any]] = None):
        """
        Save the full raw session to a JSON file.

        Stores every message exactly as the agent sees it: user messages,
        assistant messages (with reasoning, finish_reason, tool_calls),
        tool responses (with tool_call_id, tool_name), and injected system
        messages (compression summaries, todo snapshots, etc.).

        REASONING_SCRATCHPAD tags are converted to <think> blocks for consistency.
        Overwritten after each turn so it always reflects the latest state.
        """
        agent = self._agent
        messages = messages or agent._session_messages
        if not messages:
            return

        try:
            # Clean assistant content for session logs
            cleaned = []
            for msg in messages:
                if msg.get("role") == "assistant" and msg.get("content"):
                    msg = dict(msg)
                    msg["content"] = clean_session_content(msg["content"])
                cleaned.append(msg)

            entry = {
                "session_id": agent.session_id,
                "model": agent.model,
                "base_url": agent.base_url,
                "platform": agent.platform,
                "session_start": agent.session_start.isoformat(),
                "last_updated": datetime.now().isoformat(),
                "system_prompt": agent._cached_system_prompt or "",
                "tools": agent.tools or [],
                "usage": agent._session_usage_summary(),
                "message_count": len(cleaned),
                "messages": cleaned,
            }

            # Resolve atomic_json_write through the run_agent module namespace at
            # call time so tests that ``patch("run_agent.atomic_json_write")``
            # still intercept the session-log write after this extraction. The
            # lazy import avoids an import cycle at module load.
            import run_agent

            run_agent.atomic_json_write(
                agent.session_log_file,
                entry,
                indent=2,
                default=str,
            )

        except Exception as e:
            if agent.verbose_logging:
                logging.warning(f"Failed to save session log: {e}")

    # ── ShareGPT trajectory ─────────────────────────────────────────────────
    def format_tools_for_system_message(self) -> str:
        """
        Format tool definitions for the system message in the trajectory format.

        Returns:
            str: JSON string representation of tool definitions
        """
        agent = self._agent
        if not agent.tools:
            return "[]"

        # Convert tool definitions to the format expected in trajectories
        formatted_tools = []
        for tool in agent.tools:
            func = tool["function"]
            formatted_tool = {
                "name": func["name"],
                "description": func.get("description", ""),
                "parameters": func.get("parameters", {}),
                "required": None,  # Match the format in the example
            }
            formatted_tools.append(formatted_tool)

        return json.dumps(formatted_tools, ensure_ascii=False)

    def convert_to_trajectory_format(
        self, messages: list[dict[str, Any]], user_query: str, completed: bool
    ) -> list[dict[str, Any]]:
        """
        Convert internal message format to trajectory format for saving.

        Args:
            messages (List[Dict]): Internal message history
            user_query (str): Original user query
            completed (bool): Whether the conversation completed successfully

        Returns:
            List[Dict]: Messages in trajectory format
        """
        trajectory = []

        # Add system message with tool definitions
        system_msg = (
            "You are a function calling AI model. You are provided with function signatures within <tools> </tools> XML tags. "
            "You may call one or more functions to assist with the user query. If available tools are not relevant in assisting "
            "with user query, just respond in natural conversational language. Don't make assumptions about what values to plug "
            "into functions. After calling & executing the functions, you will be provided with function results within "
            "<tool_response> </tool_response> XML tags. Here are the available tools:\n"
            f"<tools>\n{self.format_tools_for_system_message()}\n</tools>\n"
            "For each function call return a JSON object, with the following pydantic model json schema for each:\n"
            "{'title': 'FunctionCall', 'type': 'object', 'properties': {'name': {'title': 'Name', 'type': 'string'}, "
            "'arguments': {'title': 'Arguments', 'type': 'object'}}, 'required': ['name', 'arguments']}\n"
            "Each function call should be enclosed within <tool_call> </tool_call> XML tags.\n"
            "Example:\n<tool_call>\n{'name': <function-name>,'arguments': <args-dict>}\n</tool_call>"
        )

        trajectory.append({"from": "system", "value": system_msg})

        # Add the actual user prompt (from the dataset) as the first human message
        trajectory.append({"from": "human", "value": user_query})

        # Skip the first message (the user query) since we already added it above.
        # Prefill messages are injected at API-call time only (not in the messages
        # list), so no offset adjustment is needed here.
        i = 1

        while i < len(messages):
            msg = messages[i]

            if msg["role"] == "assistant":
                # Check if this message has tool calls
                if "tool_calls" in msg and msg["tool_calls"]:
                    # Format assistant message with tool calls
                    # Add <think> tags around reasoning for trajectory storage
                    content = ""

                    # Prepend reasoning in <think> tags if available (native thinking tokens)
                    if msg.get("reasoning") and msg["reasoning"].strip():
                        content = f"<think>\n{msg['reasoning']}\n</think>\n"

                    if msg.get("content") and msg["content"].strip():
                        # Convert any <REASONING_SCRATCHPAD> tags to <think> tags
                        # (used when native thinking is disabled and model reasons via XML)
                        content += convert_scratchpad_to_think(msg["content"]) + "\n"

                    # Add tool calls wrapped in XML tags
                    for tool_call in msg["tool_calls"]:
                        # Parse arguments - should always succeed since we validate during conversation
                        # but keep try-except as safety net
                        try:
                            arguments = (
                                json.loads(tool_call["function"]["arguments"])
                                if isinstance(tool_call["function"]["arguments"], str)
                                else tool_call["function"]["arguments"]
                            )
                        except json.JSONDecodeError:
                            # This shouldn't happen since we validate and retry during conversation,
                            # but if it does, log warning and use empty dict
                            logging.warning(
                                f"Unexpected invalid JSON in trajectory conversion: {tool_call['function']['arguments'][:100]}"
                            )
                            arguments = {}

                        tool_call_json = {
                            "name": tool_call["function"]["name"],
                            "arguments": arguments,
                        }
                        content += f"<tool_call>\n{json.dumps(tool_call_json, ensure_ascii=False)}\n</tool_call>\n"

                    # Ensure every gpt turn has a <think> block (empty if no reasoning)
                    # so the format is consistent for training data
                    if "<think>" not in content:
                        content = "<think>\n</think>\n" + content

                    trajectory.append({"from": "gpt", "value": content.rstrip()})

                    # Collect all subsequent tool responses
                    tool_responses = []
                    j = i + 1
                    while j < len(messages) and messages[j]["role"] == "tool":
                        tool_msg = messages[j]
                        # Format tool response with XML tags
                        tool_response = "<tool_response>\n"

                        # Try to parse tool content as JSON if it looks like JSON
                        tool_content = tool_msg["content"]
                        try:
                            if tool_content.strip().startswith(("{", "[")):
                                tool_content = json.loads(tool_content)
                        except (json.JSONDecodeError, AttributeError):
                            pass  # Keep as string if not valid JSON

                        tool_index = len(tool_responses)
                        tool_name = (
                            msg["tool_calls"][tool_index]["function"]["name"]
                            if tool_index < len(msg["tool_calls"])
                            else "unknown"
                        )
                        tool_response += json.dumps(
                            {
                                "tool_call_id": tool_msg.get("tool_call_id", ""),
                                "name": tool_name,
                                "content": tool_content,
                            },
                            ensure_ascii=False,
                        )
                        tool_response += "\n</tool_response>"
                        tool_responses.append(tool_response)
                        j += 1

                    # Add all tool responses as a single message
                    if tool_responses:
                        trajectory.append({"from": "tool", "value": "\n".join(tool_responses)})
                        i = j - 1  # Skip the tool messages we just processed

                else:
                    # Regular assistant message without tool calls
                    # Add <think> tags around reasoning for trajectory storage
                    content = ""

                    # Prepend reasoning in <think> tags if available (native thinking tokens)
                    if msg.get("reasoning") and msg["reasoning"].strip():
                        content = f"<think>\n{msg['reasoning']}\n</think>\n"

                    # Convert any <REASONING_SCRATCHPAD> tags to <think> tags
                    # (used when native thinking is disabled and model reasons via XML)
                    raw_content = msg["content"] or ""
                    content += convert_scratchpad_to_think(raw_content)

                    # Ensure every gpt turn has a <think> block (empty if no reasoning)
                    if "<think>" not in content:
                        content = "<think>\n</think>\n" + content

                    trajectory.append({"from": "gpt", "value": content.strip()})

            elif msg["role"] == "user":
                trajectory.append({"from": "human", "value": msg["content"]})

            i += 1

        return trajectory

    def save_trajectory(self, messages: list[dict[str, Any]], user_query: str, completed: bool):
        """
        Save conversation trajectory to JSONL file.

        Args:
            messages (List[Dict]): Complete message history
            user_query (str): Original user query
            completed (bool): Whether the conversation completed successfully
        """
        agent = self._agent
        if not agent.save_trajectories:
            return

        trajectory = self.convert_to_trajectory_format(messages, user_query, completed)
        _save_trajectory_to_file(trajectory, agent.model, completed)

    # ── Per-turn API message build ──────────────────────────────────────────
    def build_api_messages_for_turn(self, messages: list, active_system_prompt: str) -> list:
        """Build the exact message payload sent for one chat-completions turn."""
        agent = self._agent
        api_messages = []
        # Replay assistant reasoning only on the MOST RECENT assistant message by default.
        # Replaying every prior <think>/reasoning block (Moonshot/Novita/OpenRouter
        # `reasoning_content`) multiplies hidden-input tokens across every tool turn — a large,
        # uncosted tax during long Lean loops — with little continuity benefit on chat-completions
        # routes. Set LEANFLOW_REPLAY_ALL_REASONING=1 to restore replaying every block.
        replay_all_reasoning = os.getenv("LEANFLOW_REPLAY_ALL_REASONING", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        last_reasoning_idx = -1
        if not replay_all_reasoning:
            for _i, _m in enumerate(messages):
                if _m.get("role") == "assistant" and _m.get("reasoning"):
                    last_reasoning_idx = _i
        for idx, msg in enumerate(messages):
            api_msg = msg.copy()

            # Pass assistant reasoning back to the API as reasoning_content (Moonshot AI, Novita,
            # OpenRouter). By default only the most recent assistant reasoning is replayed; the
            # full history is gated behind LEANFLOW_REPLAY_ALL_REASONING.
            if msg.get("role") == "assistant":
                reasoning_text = msg.get("reasoning")
                if reasoning_text and (replay_all_reasoning or idx == last_reasoning_idx):
                    api_msg["reasoning_content"] = reasoning_text

            # Remove 'reasoning' field - it is trajectory storage only.
            # It has already been copied to reasoning_content when needed.
            if "reasoning" in api_msg:
                api_msg.pop("reasoning")
            # Remove finish_reason - not accepted by strict APIs.
            if "finish_reason" in api_msg:
                api_msg.pop("finish_reason")
            # Strip Codex Responses API fields for strict providers like Mistral.
            if "api.mistral.ai" in agent.base_url.lower():
                agent._sanitize_tool_calls_for_strict_api(api_msg)
            # Keep reasoning_details: OpenRouter uses it for reasoning continuity.
            api_messages.append(api_msg)

        effective_system = active_system_prompt or ""
        if agent.ephemeral_system_prompt:
            effective_system = (effective_system + "\n\n" + agent.ephemeral_system_prompt).strip()
        if effective_system:
            api_messages = [{"role": "system", "content": effective_system}] + api_messages

        if agent.prefill_messages:
            sys_offset = 1 if effective_system else 0
            for idx, pfm in enumerate(agent.prefill_messages):
                api_messages.insert(sys_offset + idx, pfm.copy())

        if agent._use_prompt_caching:
            api_messages = apply_anthropic_cache_control(api_messages, cache_ttl=agent._cache_ttl)

        if hasattr(agent, "context_compressor") and agent.context_compressor:
            api_messages = agent.context_compressor._sanitize_tool_pairs(api_messages)

        return api_messages
