from __future__ import annotations

import json
from types import SimpleNamespace

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


def test_lean_reasoning_help_tool_returns_advice(monkeypatch):
    monkeypatch.setattr(
        lean_tool,
        "call_llm",
        lambda **kwargs: SimpleNamespace(
            model="zai-org/GLM-5.1",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Try proving the monotonicity lemma first, then finish with nlinarith."
                    )
                )
            ],
        ),
    )

    payload = json.loads(
        lean_tool.lean_reasoning_help_tool(
            "demo",
            "Demo/Main.lean",
            theorem_statement="theorem demo : True := by",
            current_diagnostics="unsolved goals",
            recent_failed_attempts="simp did not close the arithmetic goal",
        )
    )

    assert payload["success"] is True
    assert payload["status"] == "answered"
    assert payload["model"] == "zai-org/GLM-5.1"
    assert "monotonicity lemma" in payload["advice"]


def test_lean_reasoning_help_tool_reports_no_answer(monkeypatch):
    monkeypatch.setattr(
        lean_tool,
        "call_llm",
        lambda **kwargs: SimpleNamespace(
            model="zai-org/GLM-5.1",
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))],
        ),
    )

    payload = json.loads(lean_tool.lean_reasoning_help_tool("demo", "Demo/Main.lean"))

    assert payload["success"] is False
    assert payload["status"] == "no_answer"
    assert "not working" in payload["message"]
    assert "Continue with the main proof workflow" in payload["message"]


def test_lean_reasoning_help_tool_reports_unavailable(monkeypatch):
    def _raise_unavailable(**kwargs):
        raise RuntimeError("No LLM provider configured")

    monkeypatch.setattr(lean_tool, "call_llm", _raise_unavailable)

    payload = json.loads(lean_tool.lean_reasoning_help_tool("demo", "Demo/Main.lean"))

    assert payload["success"] is False
    assert payload["status"] == "unavailable"
    assert "No LLM provider configured" in payload["message"]
