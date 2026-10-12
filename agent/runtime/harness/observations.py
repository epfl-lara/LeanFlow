"""Project public native events for readers without influencing controller verdicts."""

from __future__ import annotations

from typing import Any

Observation = tuple[str, dict[str, Any]]


def native_observations(backend: str, event: dict[str, Any]) -> list[Observation]:
    """Extract only emitted text, reasoning, plans and tool observations.

    Use completed message blocks, avoiding duplicate partial stream updates.
    Reasoning is optional provider output; encrypted or redacted thinking is
    never decoded or presented as readable text.
    """
    kind = event.get("type")
    if kind == "harness.settings":
        return [("agent-settings", {key: value for key, value in event.items() if key != "type"})]
    common = {"backend": backend, "native_event": kind}
    if backend == "claude":
        message = event.get("message")
        message = message if isinstance(message, dict) else {}
        blocks = message.get("content", [])
        result: list[Observation] = []
        if kind in {"assistant", "user"} and isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                block_kind = block.get("type")
                data = {**common, "item_id": str(block.get("id") or block.get("tool_use_id") or "")}
                if kind == "assistant" and block_kind in {"text", "thinking"}:
                    text = block.get("text" if block_kind == "text" else "thinking")
                    if isinstance(text, str) and text:
                        result.append(
                            (
                                "agent-output" if block_kind == "text" else "agent-reasoning",
                                {**data, "text": text},
                            )
                        )
                elif kind == "assistant" and block_kind == "tool_use":
                    result.append(
                        (
                            "agent-tool-start",
                            {
                                **data,
                                "tool": block.get("name", "tool"),
                                "arguments": block.get("input", {}),
                            },
                        )
                    )
                elif kind == "user" and block_kind == "tool_result":
                    result.append(
                        (
                            "agent-tool-end",
                            {
                                **data,
                                "result": block.get("content"),
                                "status": "failed" if block.get("is_error") else "completed",
                            },
                        )
                    )
        if kind == "system" and event.get("subtype") == "compact_boundary":
            result.append(("agent-status", {**common, "text": "Native context compacted"}))
        return result
    item = event.get("item")
    if not isinstance(item, dict) or kind not in {"item.started", "item.completed"}:
        return []
    data = {**common, "item_id": str(item.get("id", ""))}
    item_kind = item.get("type")
    if item_kind in {"agent_message", "reasoning"} and kind == "item.completed":
        text = item.get("text")
        return (
            [
                (
                    "agent-output" if item_kind == "agent_message" else "agent-reasoning",
                    {**data, "text": text},
                )
            ]
            if isinstance(text, str) and text
            else []
        )
    if item_kind == "todo_list" and kind == "item.completed":
        return [("agent-plan", {**data, "result": item.get("items", [])})]
    if item_kind in {"command_execution", "mcp_tool_call", "web_search", "file_change"}:
        tool = item.get("tool") or {
            "command_execution": "Shell",
            "web_search": "Web search",
            "file_change": "File changes",
        }.get(str(item_kind), "MCP tool")
        arguments = (
            item.get("arguments") or item.get("command") or item.get("query") or item.get("changes")
        )
        tool_result = item.get("result") if "result" in item else item.get("aggregated_output")
        return [
            (
                "agent-tool-start" if kind == "item.started" else "agent-tool-end",
                {
                    **data,
                    "tool": tool,
                    "arguments": arguments,
                    "result": tool_result,
                    "status": item.get("status", ""),
                    "exit_code": item.get("exit_code"),
                    "error": item.get("error"),
                },
            )
        ]
    if item_kind == "error":
        return [
            (
                "agent-status",
                {**data, "text": item.get("message", "Native agent error"), "status": "error"},
            )
        ]
    return []
