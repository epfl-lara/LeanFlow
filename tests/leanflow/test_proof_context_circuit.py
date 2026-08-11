"""Tests for campaign-scoped proof-context timeout suppression."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.lean import lean_proof_context_circuit as circuit
from leanflow_cli.lean import lean_services
from leanflow_cli.lean.lean_models import LeanCapabilityReport
from leanflow_cli.workflows import campaign_epoch

PROOF_CONTEXT_TOOL = "mcp_lean_proof_auto_get_proof_context"
SCAN_TOOL = "mcp_lean_proof_auto_scan_theorem"
AUTO_SEARCH_TOOL = "mcp_lean_proof_auto_search_automated_proof"


def _write_target(project: Path) -> Path:
    """Write a minimal target whose local declaration slice is sufficient."""
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    return target


def _campaign_aware_report(project: Path) -> LeanCapabilityReport:
    """Return a report after applying process and campaign circuit state."""
    mcp_tools = {"proof_context": PROOF_CONTEXT_TOOL}
    degraded = lean_services._apply_disabled_mcp_tools(mcp_tools, cwd=project)
    return LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={},
        mcp_tools=mcp_tools,
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=degraded,
    )


def test_timeout_uses_local_context_and_skips_backend_after_campaign_restart(monkeypatch, tmp_path):
    """A new run in one campaign must not repay a known proof-context timeout."""
    project = tmp_path / "DemoProject"
    project.mkdir()
    target = _write_target(project)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-first-process")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})
    campaign_epoch.ensure_campaign({})
    monkeypatch.setattr(
        lean_services, "probe_capabilities", lambda cwd=None: _campaign_aware_report(project)
    )
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda _name: "")
    calls: list[str] = []

    def slow_generic_failure(tool_name, _arguments):
        calls.append(tool_name)
        # This is the exact lossy shape returned by lean-proof-auto after its
        # internal Lean server logged a 120-second declaration-scan timeout.
        return {"result": {"status": "error", "metadata": {"fail_message": "error"}}}

    monotonic_values = iter((1000.0, 1121.0))
    monkeypatch.setattr(
        lean_services,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )
    monkeypatch.setattr(lean_services, "_invoke_json_tool", slow_generic_failure)

    first = lean_services.lean_proof_context("Demo/Main.lean", "demo", cwd=project)

    assert calls == [PROOF_CONTEXT_TOOL]
    assert first["success"] is True
    assert first["backend_tool"] == "local-declaration-slice"
    assert first["theorem_statement"] == "theorem demo : True"
    assert first["timing"] == {
        "backend_phase": "proof_context",
        "backend_elapsed_s": 121.0,
    }
    assert any("current campaign" in reason for reason in first["degraded_reasons"])
    summary = json.loads(
        (project / ".leanflow" / "workflow-state" / "summary.json").read_text(encoding="utf-8")
    )
    entries = summary["campaign"][circuit.PROOF_CONTEXT_CIRCUIT_KEY]
    assert entries[0]["tool_name"] == PROOF_CONTEXT_TOOL
    assert entries[0]["file_path"] == str(target.resolve())
    assert entries[0]["theorem_id"] == "demo"
    assert entries[0]["failure"] == "error"
    assert entries[0]["elapsed_s"] == 121.0

    # Model a fresh native-runner process: it has a new run id and no in-memory
    # disabled-tool set, but it resumes the same durable campaign summary.
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-second-process")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})

    def unexpected_backend(*_args, **_kwargs):
        pytest.fail("campaign timeout circuit must take the local fast path")

    monkeypatch.setattr(lean_services, "_invoke_json_tool", unexpected_backend)
    monkeypatch.setattr(
        lean_services,
        "probe_capabilities",
        lambda *_args, **_kwargs: pytest.fail(
            "campaign timeout circuit must bypass capability probing"
        ),
    )

    resumed = lean_services.lean_proof_context("Demo/Main.lean", "demo", cwd=project)

    assert resumed["success"] is True
    assert resumed["backend_tool"] == "local-declaration-slice"
    assert resumed["original_proof"] == "trivial"
    assert any(
        "disabled for current campaign after previous backend timeout" in reason
        for reason in resumed["degraded_reasons"]
    )


def test_auto_search_skips_shared_declaration_scanner_after_campaign_timeout(monkeypatch, tmp_path):
    """Do not repay a known proof-auto declaration-scan timeout through search."""
    project = tmp_path / "DemoProject"
    project.mkdir()
    _write_target(project)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-auto-search-circuit")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})
    campaign_epoch.ensure_campaign({})
    assert circuit.record_timeout(
        PROOF_CONTEXT_TOOL,
        "MCP call failed: TimeoutError: declaration scan",
        cwd=project,
        file_path="Demo/Main.lean",
        theorem_id="demo",
        elapsed_s=121.0,
    )
    report = _campaign_aware_report(project)
    report.mcp_tools["auto_search"] = AUTO_SEARCH_TOOL
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)

    def unexpected_backend(*_args, **_kwargs):
        pytest.fail("known declaration-scan timeout must suppress auto-search backend")

    monkeypatch.setattr(lean_services, "_invoke_json_tool", unexpected_backend)

    payload = lean_services.lean_auto_search("Demo/Main.lean", "demo", cwd=project, timeout_s=20)

    assert payload["success"] is False
    assert payload["backend_tool"] == AUTO_SEARCH_TOOL
    assert any(
        "shared proof-auto declaration scanner timed out" in reason
        for reason in payload["degraded_reasons"]
    )


def test_range_scan_timeout_does_not_start_second_proof_auto_call(monkeypatch, tmp_path):
    """A timed-out range scan must fall back locally without another full wait."""
    project = tmp_path / "DemoProject"
    project.mkdir()
    _write_target(project)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-range-timeout")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})
    campaign_epoch.ensure_campaign({})
    monkeypatch.setattr(
        lean_services, "probe_capabilities", lambda cwd=None: _campaign_aware_report(project)
    )
    monkeypatch.setattr(
        lean_services,
        "_discover_internal_managed_mcp_tool",
        lambda capability: SCAN_TOOL if capability == "scan_theorem" else "",
    )
    calls: list[str] = []

    def timeout_scan(tool_name, _arguments):
        calls.append(tool_name)
        if tool_name != SCAN_TOOL:
            pytest.fail("get_proof_context must not run after the range scan times out")
        return {"error": "MCP call failed: TimeoutExpired: declaration scan"}

    monkeypatch.setattr(lean_services, "_invoke_json_tool", timeout_scan)

    payload = lean_services.lean_proof_context("Demo/Main.lean", "demo", cwd=project)

    assert calls == [SCAN_TOOL]
    assert payload["success"] is True
    assert payload["backend_tool"] == "local-declaration-slice"
    assert PROOF_CONTEXT_TOOL in circuit.timed_out_tools(cwd=project)


def test_non_timeout_failure_does_not_open_campaign_circuit(monkeypatch, tmp_path):
    """Only explicit timeouts may survive into a later process."""
    project = tmp_path / "DemoProject"
    project.mkdir()
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-schema-failure")
    campaign_epoch.ensure_campaign({})

    recorded = circuit.record_timeout(
        PROOF_CONTEXT_TOOL,
        "MCP call failed: schema mismatch",
        cwd=project,
        file_path="Demo/Main.lean",
        theorem_id="demo",
    )

    assert recorded is False
    assert circuit.timed_out_tools(cwd=project) == set()


def test_timeout_without_local_declaration_does_not_open_campaign_circuit(monkeypatch, tmp_path):
    """Do not persist a timeout when disabling MCP would remove the only context path."""
    project = tmp_path / "DemoProject"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem other : True := by trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-missing-local-target")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})
    campaign_epoch.ensure_campaign({})
    monkeypatch.setattr(
        lean_services, "probe_capabilities", lambda cwd=None: _campaign_aware_report(project)
    )
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda _name: "")
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *_args, **_kwargs: {"error": "MCP call failed: TimeoutError: "},
    )

    payload = lean_services.lean_proof_context("Demo/Main.lean", "missing", cwd=project)

    assert payload["success"] is False
    assert circuit.timed_out_tools(cwd=project) == set()


def test_healthy_campaign_still_uses_proof_context_backend(monkeypatch, tmp_path):
    """An unopened circuit must preserve the richer healthy MCP response."""
    project = tmp_path / "DemoProject"
    project.mkdir()
    _write_target(project)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "prove-healthy-context")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})
    campaign_epoch.ensure_campaign({})
    monkeypatch.setattr(
        lean_services, "probe_capabilities", lambda cwd=None: _campaign_aware_report(project)
    )
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda _name: "")
    calls: list[str] = []

    def healthy_backend(tool_name, _arguments):
        calls.append(tool_name)
        return {
            "result": {
                "status": "success",
                "theorem_statement": "theorem demo : True",
                "original_proof": "trivial",
            }
        }

    monkeypatch.setattr(lean_services, "_invoke_json_tool", healthy_backend)

    payload = lean_services.lean_proof_context("Demo/Main.lean", "demo", cwd=project)

    assert calls == [PROOF_CONTEXT_TOOL]
    assert payload["success"] is True
    assert payload["backend_tool"] == PROOF_CONTEXT_TOOL
