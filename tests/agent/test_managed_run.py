"""Characterization tests for the managed-run contract (Phase 1.5).

Pins the post-tool-result appendix mechanism that ``native_runner`` relies on to inject guidance
into the agent loop, so the upcoming decomposition of both monoliths cannot silently break it.
The sequential and concurrent tool-execution paths now share one helper
(``AIAgent._apply_post_tool_result_appendix``); testing that helper + the loop wiring covers both.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.runtime.managed_run import MANAGED_SCRATCH_ATTRS, ManagedRunAgent
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


# ---- unit: the staging/consuming helpers ---------------------------------------------------


def test_stage_accumulates_and_backs_legacy_attr(agent):
    agent.stage_tool_result_appendix("first")
    assert agent._post_tool_result_appendix == "first"
    agent.stage_tool_result_appendix("second")
    assert agent._post_tool_result_appendix == "first\n\nsecond"


def test_stage_ignores_empty(agent):
    agent._post_tool_result_appendix = None
    agent.stage_tool_result_appendix("")
    agent.stage_tool_result_appendix("   ")
    agent.stage_tool_result_appendix(None)  # type: ignore[arg-type]
    assert getattr(agent, "_post_tool_result_appendix", None) in (None, "")


def test_apply_appends_then_clears(agent):
    agent._post_tool_result_appendix = "EXTRA GUIDANCE"
    tool_msg = {"role": "tool", "content": "tool result"}
    agent._apply_post_tool_result_appendix(tool_msg)
    assert tool_msg["content"] == "tool result\n\nEXTRA GUIDANCE"
    # one-shot: cleared after consumption
    assert agent._post_tool_result_appendix is None
    # second apply is a no-op
    agent._apply_post_tool_result_appendix(tool_msg)
    assert tool_msg["content"] == "tool result\n\nEXTRA GUIDANCE"


def test_apply_noop_when_unset(agent):
    if hasattr(agent, "_post_tool_result_appendix"):
        agent._post_tool_result_appendix = None
    tool_msg = {"role": "tool", "content": "unchanged"}
    agent._apply_post_tool_result_appendix(tool_msg)
    assert tool_msg["content"] == "unchanged"


# ---- contract surface ----------------------------------------------------------------------


def test_aiagent_satisfies_managed_run_protocol(agent):
    assert isinstance(agent, ManagedRunAgent)
    for attr in (
        "pre_tool_call_callback",
        "post_tool_result_callback",
        "tool_result_projection_callback",
        "tool_progress_callback",
        "step_callback",
    ):
        assert hasattr(agent, attr)
    assert callable(agent.stage_tool_result_appendix)


def test_managed_scratch_attrs_are_not_read_by_aiagent():
    # Guard against AIAgent starting to depend on native_runner's private scratch state.
    import inspect

    import run_agent

    src = inspect.getsource(run_agent)
    for attr in MANAGED_SCRATCH_ATTRS:
        assert f"self.{attr}" not in src, f"AIAgent must not read runner scratch attr {attr}"


# ---- integration: the loop wires the appendix through both via the shared helper ------------


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


def test_post_tool_result_callback_appendix_reaches_tool_message(agent):
    """A post_tool_result_callback that stages an appendix => it is appended to the tool result."""

    def _cb(name, args, result):
        agent.stage_tool_result_appendix("[MANAGED HANDOFF GUIDANCE]")

    agent.post_tool_result_callback = _cb
    tc = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
    resp1 = _mock_response(content="", finish_reason="tool_calls", tool_calls=[tc])
    resp2 = _mock_response(content="done", finish_reason="stop")
    agent.client.chat.completions.create.side_effect = [resp1, resp2]
    with (
        patch("run_agent.handle_function_call", return_value="raw tool result"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("go")

    tool_msgs = [m for m in result["messages"] if m.get("role") == "tool"]
    assert tool_msgs, "expected a tool message in the conversation"
    assert "[MANAGED HANDOFF GUIDANCE]" in tool_msgs[0]["content"]
    assert tool_msgs[0]["content"].startswith("raw tool result")
