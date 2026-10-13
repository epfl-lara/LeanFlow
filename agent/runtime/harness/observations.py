"""Project public native events for readers without influencing controller verdicts."""

from __future__ import annotations

from typing import Any

Observation = tuple[str, dict[str, Any]]


def native_observations(backend: str, event: dict[str, Any]) -> list[Observation]:
    """Extract only emitted text, reasoning, plans and tool observations.

    Project public cumulative snapshots; the stream adapter handles additive deltas.
    Reasoning is optional provider output; encrypted or redacted thinking is
    never decoded or presented as readable text.
    """
    kind = event.get("type")
    if not isinstance(kind, str):
        return []
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
                                {
                                    **data,
                                    "text": text,
                                    **(
                                        {"reasoning_kind": "trace"}
                                        if block_kind == "thinking"
                                        else {}
                                    ),
                                },
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
    if not isinstance(item, dict) or kind not in {"item.started", "item.updated", "item.completed"}:
        return []
    data = {**common, "item_id": str(item.get("id", ""))}
    item_kind = item.get("type")
    if not isinstance(item_kind, str):
        return []
    if item_kind in {"agent_message", "reasoning"} and kind != "item.started":
        result = []
        for field, reasoning_kind in (("text", "summary"), ("raw_text", "trace")):
            if field == "raw_text" and item_kind != "reasoning":
                continue
            text = item.get(field)
            if isinstance(text, str) and text:
                metadata = {"reasoning_kind": reasoning_kind} if item_kind == "reasoning" else {}
                result.append(
                    (
                        "agent-output" if item_kind == "agent_message" else "agent-reasoning",
                        {**data, **metadata, "text": text, "partial": kind == "item.updated"},
                    )
                )
        return result
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
                (
                    "agent-tool-start"
                    if kind == "item.started"
                    else "agent-tool-progress" if kind == "item.updated" else "agent-tool-end"
                ),
                {
                    **data,
                    "tool": tool,
                    "arguments": arguments,
                    "result": tool_result,
                    "status": item.get("status", ""),
                    "exit_code": item.get("exit_code"),
                    "error": item.get("error"),
                    "partial": kind == "item.updated",
                    "description": item.get("description") or tool_description(str(tool)),
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


_TOOL_DESCRIPTIONS = {
    "Shell": "Run a command in the agent workspace",
    "Web search": "Search the web for supporting material",
    "File changes": "Update files in the agent workspace",
    "get_assignment": "Read the assigned goal and controller constraints",
    "get_guidance": "Read new user guidance",
    "lean_check": "Check Lean code for errors and unfinished proofs",
    "lean_check_target": "Check a declaration in its project context",
    "start_lean_check": "Start a Lean check and return an operation ID",
    "lean_proof_state": "Inspect the current Lean goals",
    "lean_tactic": "Try a tactic in the current proof state",
    "lean_close_proof": "Verify that the explored proof closes all goals",
    "start_command": "Start a command in the agent workspace",
    "poll_operation": "Read progress and results from a running operation",
    "submit_candidate": "Submit a proof for independent controller verification",
    "submit_report": "Submit a report for controller review",
}


def tool_description(tool: str) -> str:
    """Describe known tools without guessing the behavior of unfamiliar tools."""
    return _TOOL_DESCRIPTIONS.get(tool.rsplit("__", 1)[-1], "")
