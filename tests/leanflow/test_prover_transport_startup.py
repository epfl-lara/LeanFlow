"""Keep unused legacy MCP startup out of the bounded prover transport."""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover import session_transport
from leanflow_cli.workflows.prover.session_transport import build_transport


@pytest.mark.parametrize("previous", [None, "0", "1"])
@pytest.mark.parametrize("fails", [False, True])
def test_transport_suppresses_only_import_discovery_and_restores_environment(
    monkeypatch, previous, fails
):
    monkeypatch.setattr(session_transport, "_agent_class", None)
    if previous is None:
        monkeypatch.delenv("LEANFLOW_DISABLE_MCP", raising=False)
    else:
        monkeypatch.setenv("LEANFLOW_DISABLE_MCP", previous)
    real_import = builtins.__import__
    seen = []

    def constructor(**kwargs):
        assert os.getenv("LEANFLOW_DISABLE_MCP") == previous
        return SimpleNamespace(**kwargs)

    def importer(name, *args, **kwargs):
        if name != "run_agent":
            return real_import(name, *args, **kwargs)
        seen.append(os.getenv("LEANFLOW_DISABLE_MCP"))
        if fails:
            raise RuntimeError("import failed")
        return SimpleNamespace(AIAgent=constructor)

    monkeypatch.setattr(builtins, "__import__", importer)
    if fails:
        with pytest.raises(RuntimeError, match="import failed"):
            build_transport({"model": "test"}, [])
    else:
        agent = build_transport({"model": "test"}, [])
        assert agent.tools == []
    assert seen == ["1"]
    assert os.getenv("LEANFLOW_DISABLE_MCP") == previous


def test_fresh_transport_skips_unused_mcp_but_explicit_lean_service_can_discover(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent("""
        import os
        os.environ.pop('LEANFLOW_DISABLE_MCP', None)
        from tools.mcp import mcp_tool
        from leanflow_cli import config
        config.load_config = lambda: {'mcp_servers': {'unused-test-server': {'command': 'unused'}}}
        discoveries = []
        async def discovered(name, cfg):
            discoveries.append(name)
            return []
        mcp_tool._discover_and_register_server = discovered
        from leanflow_cli.workflows.prover.session_transport import build_transport, close_transport
        from leanflow_cli.workflows.prover.session_tools import SessionTools
        from pathlib import Path
        tools = SessionTools(role='prover', project_root=Path.cwd(), workspace=Path.cwd(), context={})
        schemas = tools.schemas()
        agent = build_transport({'model': 'bounded-startup-test'}, schemas)
        try:
            assert discoveries == [], discoveries
            assert agent.tools == schemas
            assert {'lean_check', 'lean_search', 'search_project'} <= agent.valid_tool_names
            assert 'LEANFLOW_DISABLE_MCP' not in os.environ
            from leanflow_cli.lean.lean_services import _discover_raw_mcp_tool_names
            _discover_raw_mcp_tool_names()
            assert discoveries == ['unused-test-server'], discoveries
        finally:
            close_transport(agent)
        """)
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "LEANFLOW_HOME": str(tmp_path),
        "LEANFLOW_NATIVE_PROVIDER": "local",
        "LEANFLOW_NATIVE_API_MODE": "chat_completions",
        "LEANFLOW_NATIVE_BASE_URL": "http://127.0.0.1:9/v1",
        "LEANFLOW_NATIVE_API_KEY": "test",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_parallel_transport_imports_share_one_suppressed_initialization(monkeypatch):
    monkeypatch.setattr(session_transport, "_agent_class", None)
    monkeypatch.delenv("LEANFLOW_DISABLE_MCP", raising=False)
    original = builtins.__import__
    imports = []
    constructors = []

    def constructor(**kwargs):
        constructors.append(os.getenv("LEANFLOW_DISABLE_MCP"))
        return SimpleNamespace(**kwargs)

    def importer(name, *args, **kwargs):
        if name != "run_agent":
            return original(name, *args, **kwargs)
        imports.append(os.getenv("LEANFLOW_DISABLE_MCP"))
        time.sleep(0.03)
        return SimpleNamespace(AIAgent=constructor)

    monkeypatch.setattr(builtins, "__import__", importer)
    with ThreadPoolExecutor(max_workers=4) as pool:
        agents = list(pool.map(lambda _: build_transport({"model": "test"}, []), range(4)))
    assert len(agents) == 4
    assert imports == ["1"]
    assert constructors == [None] * 4
    assert "LEANFLOW_DISABLE_MCP" not in os.environ
