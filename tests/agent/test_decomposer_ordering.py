"""Pin decomposition ordering relative to prerequisite source discovery."""

from __future__ import annotations

from types import SimpleNamespace

import run_agent


def _tool_call(name: str):
    return SimpleNamespace(function=SimpleNamespace(name=name))


def test_decomposer_batch_uses_sequential_dispatch():
    calls: list[str] = []
    agent = SimpleNamespace(
        _execute_tool_calls_sequential=lambda *args: calls.append("sequential"),
        _execute_tool_calls_concurrent=lambda *args: calls.append("concurrent"),
    )
    message = SimpleNamespace(
        tool_calls=[_tool_call("lean_outline"), _tool_call("lean_decompose_helpers")]
    )

    run_agent.AIAgent._execute_tool_calls(agent, message, [], "task")

    assert calls == ["sequential"]


def test_non_decomposer_batch_remains_concurrent():
    calls: list[str] = []
    agent = SimpleNamespace(
        _execute_tool_calls_sequential=lambda *args: calls.append("sequential"),
        _execute_tool_calls_concurrent=lambda *args: calls.append("concurrent"),
    )
    message = SimpleNamespace(tool_calls=[_tool_call("lean_outline"), _tool_call("lean_search")])

    run_agent.AIAgent._execute_tool_calls(agent, message, [], "task")

    assert calls == ["concurrent"]
