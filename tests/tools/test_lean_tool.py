from __future__ import annotations

import json
from types import SimpleNamespace

import model_tools
import tools.lean_tool as lean_tool
from epflemma_cli.workflow_state import load_verified_patch_status
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


def test_apply_verified_patch_tool_applies_patch_and_records_verified_status(tmp_path, monkeypatch):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    monkeypatch.setattr(
        lean_tool,
        "lean_verify",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "ok": True,
                "mode": kwargs["mode"],
                "command": "lake env lean Demo.lean",
                "target": kwargs["target"],
                "output": "ok",
            }
        ),
    )

    patch = f"""\
*** Begin Patch
*** Update File: {target}
 theorem demo : True := by
-  sorry
+  trivial
*** End Patch"""

    payload = json.loads(
        lean_tool.apply_verified_patch_tool(str(target), patch, cwd=str(tmp_path), theorem_id="demo")
    )

    assert payload["success"] is True
    assert payload["status"] == "verified"
    assert payload["patch_applied"] is True
    assert payload["check_passed"] is True
    assert payload["checkpoint_id"].startswith("vpatch-")
    assert "trivial" in target.read_text(encoding="utf-8")
    assert load_verified_patch_status()["status"] == "verified"


def test_apply_verified_patch_tool_persists_check_failed_status(tmp_path, monkeypatch):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    monkeypatch.setattr(
        lean_tool,
        "lean_verify",
        lambda **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "ok": False,
                "mode": kwargs["mode"],
                "command": "lake env lean Demo.lean",
                "target": kwargs["target"],
                "output": "unsolved goals",
            }
        ),
    )

    patch = f"""\
*** Begin Patch
*** Update File: {target}
 theorem demo : True := by
-  sorry
+  exact False.elim (by contradiction)
*** End Patch"""

    payload = json.loads(lean_tool.apply_verified_patch_tool(str(target), patch, cwd=str(tmp_path)))

    assert payload["success"] is False
    assert payload["status"] == "check_failed"
    assert payload["patch_applied"] is True
    assert payload["verification"]["output"] == "unsolved goals"
    assert load_verified_patch_status()["status"] == "check_failed"


def test_apply_verified_patch_tool_reports_no_changes_without_verifying(tmp_path, monkeypatch):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    original = "theorem demo : True := by\n  trivial\n"
    target.write_text(original, encoding="utf-8")
    verify_called = {"value": False}

    def _fake_verify(**kwargs):
        verify_called["value"] = True
        return SimpleNamespace(to_dict=lambda: {"ok": True})

    monkeypatch.setattr(lean_tool, "lean_verify", _fake_verify)

    patch = f"""\
*** Begin Patch
*** Update File: {target}
 theorem demo : True := by
-  trivial
+  trivial
*** End Patch"""

    payload = json.loads(lean_tool.apply_verified_patch_tool(str(target), patch, cwd=str(tmp_path)))

    assert payload["success"] is False
    assert payload["status"] == "no_changes"
    assert payload["patch_applied"] is False
    assert payload["check_passed"] is False
    assert payload["changed_ranges"] == []
    assert "unchanged" in payload["message"]
    assert target.read_text(encoding="utf-8") == original
    assert verify_called["value"] is False
    assert load_verified_patch_status()["status"] == "no_changes"


def test_apply_verified_patch_tool_blocks_statement_changes_before_verify(tmp_path, monkeypatch):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    original = "theorem demo : True := by\n  trivial\n"
    target.write_text(original, encoding="utf-8")
    verify_called = {"value": False}

    def _fake_verify(**kwargs):
        verify_called["value"] = True
        return SimpleNamespace(to_dict=lambda: {"ok": True})

    monkeypatch.setattr(lean_tool, "lean_verify", _fake_verify)
    patch = f"""\
*** Begin Patch
*** Update File: {target}
-theorem demo : True := by
+theorem demo : False := by
   trivial
*** End Patch"""

    payload = json.loads(lean_tool.apply_verified_patch_tool(str(target), patch, cwd=str(tmp_path)))

    assert payload["success"] is False
    assert payload["status"] == "patch_failed"
    assert payload["patch_applied"] is False
    assert "Lean statement guard blocked" in payload["message"]
    assert target.read_text(encoding="utf-8") == original
    assert verify_called["value"] is False


def test_lean_reasoning_help_tool_returns_advice(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_call_llm(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            model="moonshotai/Kimi-K2.6-int4",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Try proving the monotonicity lemma first, then finish with nlinarith."
                    )
                )
            ],
        )

    monkeypatch.setattr(
        lean_tool,
        "call_llm",
        _fake_call_llm,
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
    assert payload["model"] == "moonshotai/Kimi-K2.6-int4"
    assert "monotonicity lemma" in payload["advice"]
    assert "advice only" in payload["next_step"]
    assert "placeholder proof" in payload["next_step"]
    assert captured["task"] == "lean_reasoning"
    assert captured["max_tokens"] == 5000
    system_prompt = captured["messages"][0]["content"]
    assert "advisory only" in system_prompt
    assert "not verification evidence" in system_prompt
    assert "Do not suggest deleting, weakening, renaming, moving, or splitting" in system_prompt
    assert "sorry, admit, axiom, unsafe code, or a placeholder" in system_prompt


def test_lean_reasoning_help_tool_reports_no_answer(monkeypatch):
    monkeypatch.setattr(
        lean_tool,
        "call_llm",
        lambda **kwargs: SimpleNamespace(
            model="moonshotai/Kimi-K2.6-int4",
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
