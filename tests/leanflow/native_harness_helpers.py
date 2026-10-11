"""Exercise the parent MCP broker from a deterministic fake native CLI."""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any


def call_tool(request: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Send a real authenticated MCP request through the fake CLI's generated config."""
    servers = json.loads(request.mcp_config.read_text())["mcpServers"]
    environment = next(iter(servers.values()))["env"]
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    message = urllib.request.Request(
        environment["LEANFLOW_BRIDGE_URL"],
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + environment["LEANFLOW_BRIDGE_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(message, timeout=5) as response:
        result = json.load(response)["result"]
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def finish_operation(request: Any, receipt: dict[str, Any]) -> dict[str, Any]:
    """Poll test operations with a test-only hang guard, leaving production work unlimited."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = call_tool(request, "poll_operation", {"operation_id": receipt["operation_id"]})
        if result["status"] != "running":
            assert result["status"] == "completed", result
            return result["result"]
        time.sleep(0.01)
    raise AssertionError("Fake native operation failed to complete")
