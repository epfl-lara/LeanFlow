from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import model_tools
import tools.implementations.lean_experts as lean_experts
import tools.implementations.lean_patch as lean_patch
import tools.implementations.lean_tool as lean_tool
from epflemma_cli.lean.lean_services import (
    LeanCapabilityReport,
    LeanSearchResult,
)
from epflemma_cli.workflows.workflow_state import load_verified_patch_status


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
            workers=[],
            degraded_reasons=[],
        ),
    )

    payload = json.loads(lean_tool.lean_capabilities("/tmp/project"))

    assert payload["success"] is True
    assert payload["project_valid"] is True
    assert payload["workers"] == []


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


def test_lean_incremental_check_tool_dispatches_structured_payload(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_incremental_check(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "backend": "lean_interact",
            "action": kwargs["action"],
            "target": kwargs["theorem_id"],
            "elapsed_s": 0.01,
            "cache": {"cache_hit": True},
        }

    monkeypatch.setattr(lean_tool, "lean_incremental_check", _fake_incremental_check)

    payload = json.loads(
        model_tools.handle_function_call(
            "lean_incremental_check",
            {
                "file_path": "Demo/Main.lean",
                "theorem_id": "demo",
                "action": "check_target",
                "cwd": "/tmp/project",
                "include_tactics": True,
                "timeout_s": 12,
            },
        )
    )

    assert payload["success"] is True
    assert payload["ok"] is True
    assert payload["backend"] == "lean_interact"
    assert captured == {
        "action": "check_target",
        "file_path": "Demo/Main.lean",
        "theorem_id": "demo",
        "cwd": "/tmp/project",
        "replacement": "",
        "include_tactics": True,
        "timeout_s": 12,
    }


def test_handle_function_call_reports_lean_worker_dispatch_disabled():
    payload = json.loads(
        model_tools.handle_function_call(
            "lean_worker_dispatch",
            {"worker": "proof-repair", "goal": "repair theorem"},
        )
    )

    assert payload == {"error": "Unknown tool: lean_worker_dispatch"}


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

    payload = json.loads(lean_tool.lean_proof_context_tool("Demo/Main.lean", "demo"))

    assert payload["success"] is True
    assert payload["theorem_id"] == "demo"
    assert payload["similar_proofs"][0]["name"] == "demo2"


def test_unreliable_automation_tools_are_not_model_facing():
    tool_names = set(lean_tool.registry.get_all_tool_names())

    assert "lean_auto_probe" not in tool_names
    assert "lean_auto_try" not in tool_names


def test_apply_verified_patch_tool_applies_patch_and_records_verified_status(tmp_path, monkeypatch):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    monkeypatch.setattr(
        lean_patch,
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
        lean_tool.apply_verified_patch_tool(
            str(target), patch, cwd=str(tmp_path), theorem_id="demo"
        )
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
        lean_patch,
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

    monkeypatch.setattr(lean_patch, "lean_verify", _fake_verify)

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

    monkeypatch.setattr(lean_patch, "lean_verify", _fake_verify)
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
        lean_experts,
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
    assert captured["max_tokens"] == 64000
    assert captured["timeout"] == 1200
    system_prompt = captured["messages"][0]["content"]
    assert "advisory only" in system_prompt
    assert "world-class mathematical strategist" in system_prompt
    assert "Mathlib expertise" in system_prompt
    assert "not verification evidence" in system_prompt
    assert "Do not suggest deleting, weakening, renaming, moving, or splitting" in system_prompt
    assert "sorry, admit, axiom, unsafe code, or a placeholder" in system_prompt


def test_lean_reasoning_help_tool_uses_command_provider(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def _fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout="Use `simpa` after proving the helper lemma.",
            stderr="",
        )

    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AUXILIARY_LEAN_REASONING_PROVIDER", "codex")
    monkeypatch.setenv("AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE", "codex-helper --read-only")
    monkeypatch.setattr("epflemma_cli.cli.expert_help.subprocess.run", _fake_run)

    payload = json.loads(
        lean_tool.lean_reasoning_help_tool(
            "demo",
            "Demo/Main.lean",
            theorem_statement="theorem demo : True := by",
            cwd=str(tmp_path),
            timeout_s=45,
        )
    )

    assert payload["success"] is True
    assert payload["provider"] == "codex"
    assert payload["mode"] == "command"
    assert payload["command"] == ["codex-helper", "--read-only"]
    assert payload["exit_status"] == 0
    assert payload["truncated"] is False
    assert "helper lemma" in payload["advice"]
    assert captured["input"].startswith("System instructions:")
    assert "theorem demo : True := by" in captured["input"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["timeout"] == 1200


def test_lean_reasoning_help_codex_default_reads_last_message_file(monkeypatch, tmp_path):
    def _fake_run(command, **kwargs):
        output_path = command[command.index("--output-last-message") + 1]
        Path(output_path).write_text("Final Codex advisor answer.", encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout="transcript wrapper that should not become advice",
            stderr="",
        )

    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AUXILIARY_LEAN_REASONING_PROVIDER", "codex")
    monkeypatch.setattr("epflemma_cli.cli.expert_help.subprocess.run", _fake_run)

    payload = json.loads(
        lean_tool.lean_reasoning_help_tool(
            "demo",
            "Demo/Main.lean",
            theorem_statement="theorem demo : True := by",
            cwd=str(tmp_path),
        )
    )

    assert payload["success"] is True
    assert payload["provider"] == "codex"
    assert "--output-last-message" in payload["command"]
    assert payload["advice"] == "Final Codex advisor answer."


def test_lean_reasoning_help_tool_clamps_short_timeout(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_call_llm(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            model="moonshotai/Kimi-K2.6-int4",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Use a coordinate normalization first.")
                )
            ],
        )

    monkeypatch.setattr(lean_experts, "call_llm", _fake_call_llm)

    payload = json.loads(lean_tool.lean_reasoning_help_tool("demo", "Demo/Main.lean", timeout_s=45))

    assert payload["success"] is True
    assert captured["timeout"] == 1200


def test_lean_reasoning_help_tool_reports_no_answer(monkeypatch):
    monkeypatch.setattr(
        lean_experts,
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

    monkeypatch.setattr(lean_experts, "call_llm", _raise_unavailable)

    payload = json.loads(lean_tool.lean_reasoning_help_tool("demo", "Demo/Main.lean"))

    assert payload["success"] is False
    assert payload["status"] == "unavailable"
    assert "No LLM provider configured" in payload["message"]


def test_lean_decompose_helpers_returns_checked_structured_plan(monkeypatch, tmp_path):
    target = tmp_path / "Demo.lean"
    original = "theorem demo : True := by\n  sorry\n"
    target.write_text(original, encoding="utf-8")
    captured: dict[str, object] = {}
    replacements: list[str] = []

    def _fake_call_llm(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            model="moonshotai/Kimi-K2.6-int4",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "obstacle_summary": "The main proof needs a reusable invariant.",
                                "recommended_split": "Prove helper_ok first, then use it in demo.",
                                "insertion_guidance": "Insert helpers immediately before demo.",
                                "first_concrete_next_edit": "Patch helper_ok skeleton first.",
                                "helpers": [
                                    {
                                        "name": "helper_ok",
                                        "purpose": "Expose the trivial fact.",
                                        "lean_skeleton": "private lemma helper_ok : True := by\n  sorry",
                                        "dependencies": [],
                                        "proof_hints": ["exact trivial"],
                                        "insertion_point": "before demo",
                                    },
                                    {
                                        "name": "helper_bad",
                                        "purpose": "Malformed helper.",
                                        "lean_skeleton": "private lemma helper_bad : True := by\n  exact missing_name",
                                        "dependencies": ["helper_ok"],
                                        "proof_hints": ["fix the missing term"],
                                    },
                                ],
                            }
                        )
                    )
                )
            ],
        )

    def _fake_incremental_check(**kwargs):
        replacements.append(kwargs["replacement"])
        assert target.read_text(encoding="utf-8") == original
        if "helper_bad" in kwargs["replacement"]:
            return {
                "success": True,
                "ok": False,
                "errors": 1,
                "messages": [{"severity": "error", "message": "unknown identifier 'missing_name'"}],
                "output": "error: unknown identifier 'missing_name'",
            }
        return {
            "success": True,
            "ok": False,
            "errors": 0,
            "warnings": 1,
            "sorry": 1,
            "output": "warning: declaration uses `sorry`",
        }

    monkeypatch.setattr(lean_experts, "call_llm", _fake_call_llm)
    monkeypatch.setattr(lean_experts, "lean_incremental_check", _fake_incremental_check)

    payload = json.loads(
        lean_tool.lean_decompose_helpers_tool(
            "demo",
            str(target),
            theorem_statement="theorem demo : True := by",
            current_goals="⊢ True",
            cwd=str(tmp_path),
        )
    )

    assert payload["success"] is True
    assert payload["status"] == "answered"
    assert payload["obstacle_summary"].startswith("The main proof")
    assert payload["helpers"][0]["check_status"] == "ok"
    assert payload["helpers"][0]["ready_to_insert"] is True
    assert payload["helpers"][1]["check_status"] == "failed"
    assert payload["helpers"][1]["ready_to_insert"] is False
    assert "unknown identifier" in payload["helpers"][1]["check_diagnostics"]
    assert payload["skeleton_validation"]["validated_count"] == 2
    assert payload["skeleton_validation"]["ready_count"] == 1
    assert payload["skeleton_validation"]["allows_sorry_warnings"] is True
    assert "theorem demo : True := by\n  sorry" in replacements[0]
    assert target.read_text(encoding="utf-8") == original
    assert captured["task"] == "lean_decompose_helpers"
    assert "Return strict JSON only" in captured["messages"][0]["content"]


def test_lean_decompose_helpers_uses_fallback_command_provider(monkeypatch, tmp_path):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    captured: dict[str, object] = {}
    response_text = json.dumps(
        {
            "obstacle_summary": "Split out the trivial fact.",
            "recommended_split": "Insert helper_ok before demo.",
            "insertion_guidance": "Before demo.",
            "first_concrete_next_edit": "Add helper_ok.",
            "helpers": [
                {
                    "name": "helper_ok",
                    "purpose": "Expose True.",
                    "lean_skeleton": "private lemma helper_ok : True := by\n  sorry",
                    "dependencies": [],
                    "proof_hints": ["exact trivial"],
                    "insertion_point": "before demo",
                }
            ],
        }
    )

    def _fake_run_command_expert_help(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            provider=kwargs["provider"],
            command=["codex-helper"],
            exit_status=0,
            response=response_text,
            stderr="",
            truncated=False,
            response_chars=len(response_text),
            max_response_chars=64000,
            timed_out=False,
        )

    monkeypatch.setenv("AUXILIARY_LEAN_REASONING_PROVIDER", "codex")
    monkeypatch.setenv("AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE", "codex-helper")
    monkeypatch.setattr(lean_experts, "run_command_expert_help", _fake_run_command_expert_help)
    monkeypatch.setattr(
        lean_experts,
        "lean_incremental_check",
        lambda **kwargs: {"success": True, "ok": False, "errors": 0, "warnings": 1},
    )

    payload = json.loads(
        lean_tool.lean_decompose_helpers_tool(
            "demo",
            str(target),
            theorem_statement="theorem demo : True := by",
            cwd=str(tmp_path),
        )
    )

    assert payload["success"] is True
    assert payload["mode"] == "command"
    assert payload["provider"] == "codex"
    assert captured["task"] == "lean_decompose_helpers"
    assert captured["provider"] == "codex"


def test_lean_decompose_helpers_reports_malformed_json(monkeypatch):
    monkeypatch.setattr(
        lean_experts,
        "call_llm",
        lambda **kwargs: SimpleNamespace(
            model="moonshotai/Kimi-K2.6-int4",
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json"))],
        ),
    )

    payload = json.loads(
        lean_tool.lean_decompose_helpers_tool(
            "demo",
            "Demo/Main.lean",
            theorem_statement="theorem demo : True := by",
        )
    )

    assert payload["success"] is False
    assert payload["status"] == "invalid_json"
    assert "did not return a JSON object" in payload["message"]
    assert payload["raw_response"] == "not json"
