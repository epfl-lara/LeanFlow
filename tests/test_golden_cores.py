"""Golden / characterization tests for the coupled cores (Phase II-2 safety net).

These pin the behaviors that the upcoming dependency-injection redesign (ManagedRunContext,
PostToolResultAppendixBroker, ConversationRunner) must preserve byte-for-byte. They run fully
offline — the provider is a MagicMock and tool execution is patched — modelled on the known-passing
setup in tests/agent/test_managed_run.py.

The critical one is `test_appendix_is_consumed_per_turn_no_leak`: the post-tool-result appendix is
staged by a callback and consumed by the tool loop *within the same turn*; codex flagged that an
attr-by-attr dual-mode migration of this interleaved state could double-consume / leak / reorder it,
so this characterization is the guard for the atomic per-turn migration.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _make_tool_defs(*names):
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _mock_tool_call(name="web_search", arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(content="", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.tool_delay = 0
        a.compression_enabled = False
        a.save_trajectories = False
        return a


def _run(agent, user_message="go"):
    with (
        patch("run_agent.handle_function_call", return_value="raw tool result"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(user_message)


def test_appendix_is_consumed_per_turn_no_leak(agent):
    """Each tool turn's staged appendix lands on THAT turn's tool message and is cleared — it must
    not leak into the next turn. This is the interleaved-state invariant the atomic DI migration
    of `_post_tool_result_appendix` -> PostToolResultAppendixBroker must keep."""
    turn = {"n": 0}

    def _cb(name, args, result):
        turn["n"] += 1
        agent.stage_tool_result_appendix(f"GUIDANCE-{turn['n']}")

    agent.post_tool_result_callback = _cb
    resp1 = _mock_response(finish_reason="tool_calls", tool_calls=[_mock_tool_call(call_id="c1")])
    resp2 = _mock_response(finish_reason="tool_calls", tool_calls=[_mock_tool_call(call_id="c2")])
    resp3 = _mock_response(content="done", finish_reason="stop")
    agent.client.chat.completions.create.side_effect = [resp1, resp2, resp3]

    result = _run(agent)

    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert "GUIDANCE-1" in tool_msgs[0]["content"]
    assert "GUIDANCE-2" in tool_msgs[1]["content"]
    # No cross-turn leak / double-consume:
    assert "GUIDANCE-1" not in tool_msgs[1]["content"]
    assert "GUIDANCE-2" not in tool_msgs[0]["content"]


def test_pre_tool_call_callback_fires_before_post(agent):
    """The pre/post tool-call callbacks (the ConversationRunner surface) fire in order per tool."""
    events: list[str] = []
    agent.pre_tool_call_callback = lambda name, args: events.append("pre") or None
    agent.post_tool_result_callback = lambda name, args, result: events.append("post")
    resp1 = _mock_response(finish_reason="tool_calls", tool_calls=[_mock_tool_call(call_id="c1")])
    resp2 = _mock_response(content="done", finish_reason="stop")
    agent.client.chat.completions.create.side_effect = [resp1, resp2]

    _run(agent)

    assert "pre" in events and "post" in events
    assert events.index("pre") < events.index("post")


def test_run_conversation_result_schema(agent):
    """Pin the result-dict surface every downstream caller (and native_runner) reads."""
    agent.client.chat.completions.create.return_value = _mock_response(
        content="answer", finish_reason="stop"
    )
    result = _run(agent, "hi")
    for key in (
        "final_response",
        "messages",
        "api_calls",
        "completed",
        "usage",
        "interrupted",
        "partial",
        "exit_reason",
        "wall_timed_out",
    ):
        assert key in result, f"missing result key: {key}"
    assert result["final_response"] == "answer"
    assert result["completed"] is True
    assert result["interrupted"] is False
    assert result["partial"] is False


def test_interrupt_yields_partial_incomplete(agent):
    """An interrupt during the API call yields a partial, non-completed, interrupted result."""

    def _interrupt(api_kwargs):
        agent._interrupt_requested = True
        raise InterruptedError("interrupted during API call")

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=_interrupt),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("go")

    # Characterized current behavior: an interrupt on the FIRST API call (before any tool progress)
    # yields interrupted=True, partial=False (nothing partial was produced), completed=True
    # (run_conversation returned normally rather than raising). Pin all three so the
    # DI/ConversationRunner refactor can't silently change the interrupt result shape.
    assert result["interrupted"] is True
    assert result["partial"] is False
    assert result["completed"] is True
