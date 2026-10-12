"""Exercise managed MCP dependency resolution and non-inference readiness probes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.cli import mcp_bootstrap
from leanflow_cli.runtime import mcp_protocol


@pytest.mark.parametrize("returncode", [0, 1])
def test_protocol_probe_checks_import_without_starting_server(monkeypatch, returncode: int) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(mcp_protocol.subprocess, "run", run)
    assert mcp_protocol.mcp_protocol_ready(Path("/fixture")) is (returncode == 0)
    assert calls[0][-2:] == ["-c", "from mcp.server.fastmcp import FastMCP"]


def test_protocol_probe_fails_closed_on_timeout(monkeypatch) -> None:
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(mcp_protocol.subprocess, "run", timeout)
    assert not mcp_protocol.mcp_protocol_ready(Path("/fixture"))


def test_server_dependencies_resolve_with_protocol_constraint_together(monkeypatch) -> None:
    """Give pip the server, companions and compatibility constraint in one resolution."""
    calls = []
    monkeypatch.setattr(
        mcp_bootstrap, "_ensure_venv", lambda *args, **kwargs: Path("/fixture/python")
    )
    monkeypatch.setattr(
        mcp_bootstrap.subprocess, "run", lambda command, **kwargs: calls.append(command)
    )
    mcp_bootstrap._install_into_managed_venv(
        Path("/fixture"), "example-server", extra_install_specs=("PyYAML",)
    )
    assert "example-server" in calls[-1]
    assert "PyYAML" in calls[-1]
    assert mcp_protocol.MCP_REQUIREMENT in calls[-1]
