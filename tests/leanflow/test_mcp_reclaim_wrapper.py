"""Native Lean wrapper behavior across managed MCP recycle boundaries."""

from __future__ import annotations

from types import SimpleNamespace

from leanflow_cli.lean import lean_services
from leanflow_cli.lean.lean_models import LeanCapabilityReport


def _report() -> LeanCapabilityReport:
    """Return the smallest connected capability report for wrapper tests."""
    return LeanCapabilityReport(
        cwd="/tmp/project",
        project_root="/tmp/project",
        project_valid=True,
        project_error="",
        binaries={},
        mcp_tools={"goals": "mcp_lean_lsp_lean_goal"},
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )


def test_recycle_pending_does_not_disable_native_capability(monkeypatch) -> None:
    """Treat a bounded lifecycle handoff as retryable, not backend failure."""
    monkeypatch.setattr(
        lean_services,
        "_BACKEND",
        SimpleNamespace(
            invoke_tool=lambda _name, _arguments: {
                "error": "lean-lsp recycle is still draining an active request",
                "mcp_recycling": True,
                "retryable": True,
            }
        ),
    )
    disabled: list[str] = []
    monkeypatch.setattr(
        lean_services,
        "_disable_mcp_tool_for_run",
        lambda tool_name, **_kwargs: disabled.append(tool_name),
    )
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *_args: None)

    payload = lean_services._invoke_native_mcp_wrapper(
        "mcp_lean_lsp_lean_goal",
        {"file_path": "Main.lean"},
        report=_report(),
        unavailable_reason="goals unavailable",
        outcome_kind="lean-goals",
    )

    assert payload["success"] is False
    assert payload["retryable"] is True
    assert payload["mcp_recycling"] is True
    assert disabled == []
    assert any("retry this capability" in reason for reason in payload["degraded_reasons"])


def test_recycle_cleanup_failure_remains_retryable_and_does_not_circuit_break(
    monkeypatch,
) -> None:
    """Preserve the capability while fail-closed ownership awaits teardown retry."""
    monkeypatch.setattr(
        lean_services,
        "_BACKEND",
        SimpleNamespace(
            invoke_tool=lambda _name, _arguments: {
                "error": "retained old server ownership",
                "mcp_recycling": True,
                "retryable": True,
                "cleanup_failed": True,
            }
        ),
    )
    disabled: list[str] = []
    monkeypatch.setattr(
        lean_services,
        "_disable_mcp_tool_for_run",
        lambda tool_name, **_kwargs: disabled.append(tool_name),
    )
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *_args: None)

    payload = lean_services._invoke_native_mcp_wrapper(
        "mcp_lean_lsp_lean_goal",
        {"file_path": "Main.lean"},
        report=_report(),
        unavailable_reason="goals unavailable",
        outcome_kind="lean-goals",
    )

    assert payload["success"] is False
    assert payload["retryable"] is True
    assert payload["mcp_recycling"] is True
    assert disabled == []
