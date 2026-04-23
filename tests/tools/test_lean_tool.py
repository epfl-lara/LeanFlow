from __future__ import annotations

import json

import model_tools
import tools.lean_tool as lean_tool
from epflemma_cli.lean_services import (
    LeanCapabilityReport,
    LeanSearchResult,
    LeanWorkerResult,
)


def test_lean_capabilities_tool_returns_structured_json(monkeypatch):
    monkeypatch.setattr(
        lean_tool,
        "probe_capabilities",
        lambda cwd=None: LeanCapabilityReport(
            cwd=str(cwd or ""),
            project_root="/tmp/project",
            project_valid=True,
            project_error="",
            binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
            mcp_tools={"diagnostics": "mcp_lean_diagnostics"},
            search_providers=["mcp-local-search"],
            helper_tools={"sorry_analyzer": True},
            workers=["proof-repair"],
            degraded_reasons=[],
        ),
    )

    payload = json.loads(lean_tool.lean_capabilities("/tmp/project"))

    assert payload["success"] is True
    assert payload["project_valid"] is True
    assert payload["workers"] == ["proof-repair"]


def test_lean_search_tool_preserves_provider_provenance(monkeypatch):
    monkeypatch.setattr(
        lean_tool,
        "lean_search",
        lambda *args, **kwargs: LeanSearchResult(
            query="map",
            mode="semantic",
            attempted_providers=["mcp-leanfinder"],
            results=[{"provider": "mcp-leanfinder", "match": "List.map"}],
            degraded_reasons=[],
        ),
    )

    payload = json.loads(lean_tool.lean_search_tool("map", mode="semantic"))

    assert payload["success"] is True
    assert payload["results"][0]["provider"] == "mcp-leanfinder"


def test_lean_search_tool_marks_repeated_empty_search_loop_as_action_required(monkeypatch):
    monkeypatch.setattr(
        lean_tool,
        "lean_search",
        lambda *args, **kwargs: LeanSearchResult(
            query="hard theorem",
            mode="auto",
            attempted_providers=["project-rg", "mathlib-rg"],
            results=[],
            degraded_reasons=[
                "lean diagnostics MCP unavailable",
                "semantic providers unavailable",
                "search returned no results",
                "repeated empty search loop detected; stop searching and change tactic",
            ],
        ),
    )

    payload = json.loads(lean_tool.lean_search_tool("hard theorem"))

    assert payload["success"] is False
    assert "action_required" in payload


def test_handle_function_call_passes_parent_agent_to_lean_worker_dispatch(monkeypatch):
    captured: dict[str, object] = {}
    parent_agent = object()

    def _fake_dispatch(request, *, parent_agent=None, owner_id=""):
        captured["worker"] = request.worker
        captured["parent_agent"] = parent_agent
        captured["owner_id"] = owner_id
        return LeanWorkerResult(
            worker=request.worker,
            mode="delegate",
            dispatched=True,
            summary="delegated",
        )

    monkeypatch.setattr(lean_tool, "dispatch_worker", _fake_dispatch)

    payload = json.loads(
        model_tools.handle_function_call(
            "lean_worker_dispatch",
            {"worker": "proof-repair", "goal": "repair theorem"},
            parent_agent=parent_agent,
            owner_id="agent-123",
        )
    )

    assert payload["success"] is True
    assert captured["worker"] == "proof-repair"
    assert captured["parent_agent"] is parent_agent
    assert captured["owner_id"] == "agent-123"


def test_lean_proof_context_tool_returns_normalized_payload(monkeypatch):
    monkeypatch.setattr(
        lean_tool,
        "lean_proof_context",
        lambda *args, **kwargs: {
            "success": True,
            "backend_tool": "mcp_lean_proof_auto_get_proof_context",
            "file_path": "Demo/Main.lean",
            "theorem_id": "demo",
            "theorem_statement": "theorem demo : True",
            "hypotheses": ["h : True"],
            "in_scope": ["trivial"],
            "similar_proofs": [{"name": "demo2"}],
            "degraded_reasons": [],
        },
    )

    payload = json.loads(
        lean_tool.lean_proof_context_tool("Demo/Main.lean", "demo")
    )

    assert payload["success"] is True
    assert payload["theorem_id"] == "demo"
    assert payload["similar_proofs"][0]["name"] == "demo2"


def test_lean_auto_probe_tool_surfaces_degraded_reasons(monkeypatch):
    monkeypatch.setattr(
        lean_tool,
        "lean_auto_probe",
        lambda *args, **kwargs: {
            "success": False,
            "backend_tool": "",
            "file_path": "Demo/Main.lean",
            "theorem_id": "demo",
            "degraded_reasons": [
                "lean automation MCP unavailable",
                "lean automation probe MCP unavailable",
            ],
        },
    )

    payload = json.loads(
        lean_tool.lean_auto_probe_tool("Demo/Main.lean", "demo")
    )

    assert payload["success"] is False
    assert "lean automation probe MCP unavailable" in payload["degraded_reasons"]
