"""Guard the removal of in-process SDK engines and delegation."""

from __future__ import annotations

import importlib
import json

import pytest

from tools.implementations.delegate_tool import check_delegate_requirements, delegate_task


@pytest.mark.parametrize(
    "module_name",
    ["run_agent", "leanflow_cli.native.native_runner", "leanflow_cli.native.dispatch_worker"],
)
def test_old_runner_entrypoints_require_native_workflows(module_name):
    module = importlib.import_module(module_name)
    assert not hasattr(module, "AIAgent")
    with pytest.raises(SystemExit, match="--provider codex.*--provider claude.*--provider lemex"):
        module.main()


def test_legacy_delegation_is_unavailable_and_never_reads_parent_credentials():
    class Parent:
        @property
        def api_key(self):
            raise AssertionError("native authentication must remain runtime-owned")

    assert not check_delegate_requirements()
    result = json.loads(delegate_task(goal="prove", parent_agent=Parent()))
    assert result["status"] == "removed"
    assert "LEMEX_API_KEY" in result["error"]


def test_retired_advisors_are_not_advertised_by_tool_registry():
    from core.model_tools import get_all_tool_names
    from tools.implementations.lean_experts import (
        lean_decompose_helpers_tool,
        lean_reasoning_help_tool,
    )

    assert {"lean_reasoning_help", "lean_decompose_helpers", "delegate_task"}.isdisjoint(
        get_all_tool_names()
    )
    for tool in (lean_reasoning_help_tool, lean_decompose_helpers_tool):
        result = json.loads(tool(theorem_id="test"))
        assert result["success"] is False
        assert result["provider_called"] is False
        assert result["status"] == "removed"
