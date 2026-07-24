"""Tests for model_tools.py — function call dispatch, agent-loop interception, legacy toolsets."""

import json
import subprocess
import sys
from pathlib import Path

from core.model_tools import (
    _AGENT_LOOP_TOOLS,
    _LEGACY_TOOLSET_MAP,
    TOOL_TO_TOOLSET_MAP,
    get_all_tool_names,
    get_toolset_for_tool,
    handle_function_call,
)

# =========================================================================
# handle_function_call
# =========================================================================


class TestHandleFunctionCall:
    def test_agent_loop_tool_returns_error(self):
        for tool_name in _AGENT_LOOP_TOOLS:
            result = json.loads(handle_function_call(tool_name, {}))
            assert "error" in result
            assert "agent loop" in result["error"].lower()

    def test_unknown_tool_returns_error(self):
        result = json.loads(handle_function_call("totally_fake_tool_xyz", {}))
        assert "error" in result
        assert "totally_fake_tool_xyz" in result["error"]

    def test_exception_returns_json_error(self):
        # Even if something goes wrong, should return valid JSON
        result = handle_function_call("web_search", None)  # None args may cause issues
        parsed = json.loads(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed
        assert len(parsed["error"]) > 0
        assert "error" in parsed["error"].lower() or "failed" in parsed["error"].lower()


# =========================================================================
# Agent loop tools
# =========================================================================


class TestAgentLoopTools:
    def test_expected_tools_in_set(self):
        assert "todo" in _AGENT_LOOP_TOOLS
        assert "memory" in _AGENT_LOOP_TOOLS
        assert "session_search" in _AGENT_LOOP_TOOLS
        assert "delegate_task" in _AGENT_LOOP_TOOLS

    def test_no_regular_tools_in_set(self):
        assert "web_search" not in _AGENT_LOOP_TOOLS
        assert "terminal" not in _AGENT_LOOP_TOOLS


# =========================================================================
# Legacy toolset map
# =========================================================================


class TestLegacyToolsetMap:
    def test_expected_legacy_names(self):
        expected = [
            "web_tools",
            "file_tools",
        ]
        for name in expected:
            assert name in _LEGACY_TOOLSET_MAP, f"Missing legacy toolset: {name}"

    def test_values_are_lists_of_strings(self):
        for name, tools in _LEGACY_TOOLSET_MAP.items():
            assert isinstance(tools, list), f"{name} is not a list"
            for tool in tools:
                assert isinstance(tool, str), f"{name} contains non-string: {tool}"

    def test_web_tools_legacy_alias_excludes_web_extract(self):
        # The legacy alias exposes the key-free web tools (web_search + web_fetch) but never
        # the Firecrawl-gated web_extract, which is unavailable without an API key.
        assert _LEGACY_TOOLSET_MAP["web_tools"] == ["web_search", "web_fetch", "web_download"]
        assert "web_extract" not in _LEGACY_TOOLSET_MAP["web_tools"]


# =========================================================================
# Backward-compat wrappers
# =========================================================================


class TestBackwardCompat:
    def test_get_all_tool_names_returns_list(self):
        names = get_all_tool_names()
        assert isinstance(names, list)
        assert len(names) > 0
        # Should contain well-known tools
        assert "web_search" in names
        assert "read_file" in names

    def test_get_toolset_for_tool(self):
        result = get_toolset_for_tool("web_search")
        assert result is not None
        assert isinstance(result, str)

    def test_get_toolset_for_unknown_tool(self):
        result = get_toolset_for_tool("totally_nonexistent_tool")
        assert result is None

    def test_tool_to_toolset_map(self):
        assert isinstance(TOOL_TO_TOOLSET_MAP, dict)
        assert len(TOOL_TO_TOOLSET_MAP) > 0


def test_model_tools_discovery_stays_on_minimal_leanflow_surface():
    repo_root = Path(__file__).resolve().parent.parent
    code = f"""
import json
import sys
sys.path.insert(0, {str(repo_root)!r})
import core.model_tools as model_tools
from tools.registry import registry
print(json.dumps(sorted(set(entry.toolset for entry in registry._tools.values()))))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
        cwd=repo_root,
    )

    toolsets = json.loads(result.stdout.strip())
    assert toolsets == [
        "coordination",
        "delegation",
        "document",
        "empirical-compute",
        "file",
        "lean",
        "session_search",
        "skills",
        "terminal",
        "web",
    ]
