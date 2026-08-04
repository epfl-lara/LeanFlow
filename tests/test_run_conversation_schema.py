"""Contract test: the exact shape of ``AIAgent.run_conversation()``'s return value.

This is a *characterization* test added as part of the refactor groundwork (Phase 0). The
``AIAgent`` god-class is slated to be decomposed into collaborators (Phase 4); this test pins
the public result schema so that decomposition cannot silently add/drop/rename a key.

The schema is defined at run_agent.py (the ``result = {...}`` built at the end of
``run_conversation``): ``final_response``, ``last_reasoning``, ``messages``, ``api_calls``,
``usage``, ``completed``, ``exit_reason``, ``partial``, ``interrupted``, ``response_previewed``.
``wall_timed_out`` reports whether the optional conversation wall deadline ended the run.
``interrupt_message`` is added only when interrupted; ``error`` only on error paths.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent

# The exact key set returned on a normal (non-interrupted, non-error) completion.
EXPECTED_KEYS = {
    "final_response",
    "last_reasoning",
    "messages",
    "api_calls",
    "usage",
    "completed",
    "exit_reason",
    "partial",
    "interrupted",
    "response_previewed",
    "wall_timed_out",
}


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
    """Minimal AIAgent with a mocked OpenAI client (no network)."""
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


def _stop_response(content="Final answer"):
    msg = SimpleNamespace(content=content, role="assistant", tool_calls=None, reasoning=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _run(agent, prompt="hello"):
    agent.client.chat.completions.create.return_value = _stop_response()
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(prompt)


def test_run_conversation_returns_exactly_the_contract_keys(agent):
    result = _run(agent)
    assert set(result.keys()) == EXPECTED_KEYS, (
        "run_conversation result schema changed. If intentional, update EXPECTED_KEYS and any "
        "downstream consumers (native_runner, leanflow_cli shell status, tests)."
    )


def test_run_conversation_result_value_types(agent):
    result = _run(agent)
    assert isinstance(result["final_response"], str)
    assert isinstance(result["messages"], list)
    assert isinstance(result["api_calls"], int)
    assert isinstance(result["completed"], bool)
    assert isinstance(result["partial"], bool)
    assert isinstance(result["interrupted"], bool)
    assert isinstance(result["response_previewed"], bool)
    assert isinstance(result["wall_timed_out"], bool)


def test_run_conversation_normal_completion_flags(agent):
    result = _run(agent)
    assert result["completed"] is True
    assert result["interrupted"] is False
    # Normal completion must NOT carry the interrupt-only / error-only keys.
    assert "interrupt_message" not in result
    assert "error" not in result
