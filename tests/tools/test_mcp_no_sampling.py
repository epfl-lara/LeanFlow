"""Keep MCP tool transports functional without advertising removed model sampling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp import mcp_tool


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("sampling", [None, {"enabled": True, "model": "retired-model"}])
def test_mcp_transports_never_advertise_model_sampling(transport, sampling):
    streams = (object(), object())
    transport_context = MagicMock()
    transport_context.__aenter__ = AsyncMock(
        return_value=streams if transport == "stdio" else (*streams, lambda: None)
    )
    transport_context.__aexit__ = AsyncMock(return_value=False)
    tool = SimpleNamespace(name="deterministic_check", inputSchema={})
    session = MagicMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[tool]))
    session_context = MagicMock()
    session_context.__aenter__ = AsyncMock(return_value=session)
    session_context.__aexit__ = AsyncMock(return_value=False)
    config = {"command": "fake-server"} if transport == "stdio" else {"url": "https://invalid.test"}
    if sampling is not None:
        config["sampling"] = sampling

    async def exercise():
        with (
            patch.object(mcp_tool, "_MCP_HTTP_AVAILABLE", True),
            patch.object(mcp_tool, "stdio_client", return_value=transport_context),
            patch.object(mcp_tool, "streamablehttp_client", return_value=transport_context),
            patch.object(mcp_tool, "ClientSession", return_value=session_context) as client,
            patch.object(mcp_tool, "open_mcp_stderr_log", return_value=None),
        ):
            server = mcp_tool.MCPServerTask("deterministic-test")
            try:
                await server.start(config)
                assert server.session is session
                assert server._tools == [tool]
                client.assert_called_once_with(*streams)
            finally:
                await server.shutdown()

    asyncio.run(exercise())
    assert not hasattr(mcp_tool, "SamplingHandler")
