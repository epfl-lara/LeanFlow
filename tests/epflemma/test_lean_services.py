from __future__ import annotations

from pathlib import Path

from epflemma_cli import lean_services
from epflemma_cli.lean_services import LeanCapabilityReport


def test_probe_capabilities_reports_managed_mcp_roles(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(
        lean_services,
        "_discover_lean_mcp_tools",
        lambda: {
            "diagnostics": "mcp_lean_lsp_diagnostics",
            "goals": "mcp_lean_lsp_goals",
            "code_actions": "",
            "multi_attempt": "mcp_lean_lsp_multi_attempt",
            "run_code": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "proof_context": "mcp_lean_proof_auto_get_proof_context",
            "auto_probe": "mcp_lean_proof_auto_probe",
            "auto_search": "mcp_lean_proof_auto_search_automated_proof",
            "auto_try": "mcp_lean_proof_auto_try_automated_proof",
        },
    )
    monkeypatch.setattr(
        "tools.mcp_tool.get_mcp_status",
        lambda: [
            {
                "name": "lean-lsp",
                "role": "primary-state-search",
                "managed": True,
                "healthy": True,
                "connected": True,
            },
            {
                "name": "lean-proof-auto",
                "role": "secondary-automation-context",
                "managed": True,
                "healthy": False,
                "connected": False,
            },
        ],
    )

    report = lean_services.probe_capabilities(project)

    assert report.mcp_server_roles["lean-lsp"] == "primary-state-search"
    assert report.managed_mcp_servers["lean-lsp"] is True
    assert report.managed_mcp_servers["lean-proof-auto"] is False


def test_lean_search_marks_semantic_provider_fallback(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    monkeypatch.setattr(
        lean_services,
        "probe_capabilities",
        lambda cwd=None: LeanCapabilityReport(
            cwd=str(project),
            project_root=str(project),
            project_valid=True,
            project_error="",
            binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
            mcp_tools={},
            search_providers=["project-rg"],
            helper_tools={"search_fallback": True},
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(
        lean_services,
        "_rg_search",
        lambda root, query, *, limit=10: [{"file": "Demo/Main.lean", "line": 12, "preview": "theorem map_id"}],
    )

    result = lean_services.lean_search("map_id", cwd=project)

    assert result.attempted_providers == ["project-rg"]
    assert result.results[0]["provider"] == "project-rg"
    assert "semantic providers unavailable" in result.degraded_reasons


def test_lean_axioms_reports_custom_axioms(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text("theorem demo : True := by trivial\n", encoding="utf-8")

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(lean_services, "_module_name_for_file", lambda root, file_path: "Demo.Main")
    monkeypatch.setattr(
        lean_services,
        "_run_command",
        lambda cmd, cwd=None: (
            0,
            "Demo.demo uses Classical.choice My.customAxiom Quot.sound",
        ),
    )

    report = lean_services.lean_axioms("demo", cwd=project, file_path=str(target))

    assert report.choice is True
    assert report.custom_axioms == ["My.customAxiom"]
    assert "Classical.choice" in report.axioms
    assert report.ok is False


def test_lean_search_marks_repeated_empty_search_loop(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
    monkeypatch.setattr(
        lean_services,
        "probe_capabilities",
        lambda cwd=None: LeanCapabilityReport(
            cwd=str(project),
            project_root=str(project),
            project_valid=True,
            project_error="",
            binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
            mcp_tools={},
            search_providers=["project-rg"],
            helper_tools={"search_fallback": True},
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(lean_services, "_rg_search", lambda root, query, *, limit=10: [])
    outcomes = project / ".epflemma-outcomes.jsonl"
    outcomes.write_text(
        "\n".join(
            [
                '{"kind":"lean-search","workflow_command":"/prove Demo/Main.lean","payload":{"results":[]}}',
                '{"kind":"lean-search","workflow_command":"/prove Demo/Main.lean","payload":{"results":[]}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(lean_services, "workflow_outcomes_path", lambda: outcomes)

    result = lean_services.lean_search("hard theorem name", cwd=project)

    assert "repeated empty search loop detected; stop searching and change tactic" in result.degraded_reasons


def test_route_workflow_step_marks_search_exhausted_from_recent_empty_search_streak(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
    monkeypatch.setattr(
        lean_services,
        "probe_capabilities",
        lambda cwd=None: LeanCapabilityReport(
            cwd=str(project),
            project_root=str(project),
            project_valid=True,
            project_error="",
            binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
            mcp_tools={},
            search_providers=["project-rg", "mathlib-rg"],
            helper_tools={"search_fallback": True},
            workers=["sorry-filler-deep"],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(lean_services, "recent_empty_search_streak", lambda workflow_command, limit=6: 3)

    decision = lean_services.route_workflow_step(
        "prove",
        {
            "active_file": str(project / "Demo" / "Main.lean"),
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
            "current_blocker": "contains sorry",
            "diagnostics": "warning: declaration uses sorry",
            "goals": "Lean goals unavailable.",
            "build_status": "unknown",
        },
        configured_skill="lean-theorem-queue-worker",
        autonomy_state={},
        cwd=project,
    )

    assert decision.search_exhausted is True
    assert decision.recommended_worker == "sorry-filler-deep"


def test_discover_lean_mcp_tools_prefers_raw_managed_tools_over_native_wrappers(monkeypatch):
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: None)
    monkeypatch.setattr(
        "tools.registry.registry.get_all_tool_names",
        lambda: [
            "lean_proof_context",
            "lean_multi_attempt",
            "mcp_lean_lsp_lean_diagnostic_messages",
            "mcp_lean_lsp_lean_goal",
            "mcp_lean_lsp_lean_multi_attempt",
            "mcp_lean_proof_auto_get_proof_context",
            "mcp_lean_proof_auto_probe",
            "mcp_lean_proof_auto_try_automated_proof",
        ],
    )

    discovered = lean_services._discover_lean_mcp_tools()

    assert discovered["diagnostics"] == "mcp_lean_lsp_lean_diagnostic_messages"
    assert discovered["goals"] == "mcp_lean_lsp_lean_goal"
    assert discovered["multi_attempt"] == "mcp_lean_lsp_lean_multi_attempt"
    assert discovered["proof_context"] == "mcp_lean_proof_auto_get_proof_context"


def test_managed_mcp_wrapper_failure_disables_tool_for_current_run(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "run-demo")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda capability: "")
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(
        lean_services,
        "_discover_lean_mcp_tools",
        lambda: {
            "diagnostics": "",
            "goals": "",
            "code_actions": "",
            "multi_attempt": "",
            "run_code": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "proof_context": "mcp_lean_proof_auto_get_proof_context",
            "auto_probe": "",
            "auto_search": "",
            "auto_try": "",
        },
    )
    monkeypatch.setattr(
        "tools.mcp_tool.get_mcp_status",
        lambda: [
            {
                "name": "lean-proof-auto",
                "role": "secondary-automation-context",
                "managed": True,
                "healthy": True,
                "connected": True,
            }
        ],
    )
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda tool_name, arguments: {"error": "schema mismatch"},
    )

    payload = lean_services.lean_proof_context("Demo/Main.lean", "demo", cwd=project)
    report = lean_services.probe_capabilities(project)

    assert payload["success"] is False
    assert any("disabled for current run" in reason for reason in payload["degraded_reasons"])
    assert report.mcp_tools["proof_context"] == ""
    assert any("disabled for current run" in reason for reason in report.degraded_reasons)


def test_proof_auto_wrappers_use_expected_backend_arguments(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={
            "diagnostics": "",
            "goals": "",
            "code_actions": "",
            "multi_attempt": "",
            "run_code": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "proof_context": "mcp_lean_proof_auto_get_proof_context",
            "auto_probe": "mcp_lean_proof_auto_probe",
            "auto_search": "mcp_lean_proof_auto_search_automated_proof",
            "auto_try": "mcp_lean_proof_auto_try_automated_proof",
        },
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda capability: "")
    calls: list[tuple[str, dict[str, object]]] = []

    def _fake_invoke(tool_name, arguments):
        calls.append((tool_name, dict(arguments)))
        return {"result": {"success": True}}

    monkeypatch.setattr(lean_services, "_invoke_json_tool", _fake_invoke)

    lean_services.lean_proof_context("Demo/Main.lean", "demo", cwd=project)
    lean_services.lean_auto_search("Demo/Main.lean", "demo", cwd=project, timeout_s=42, objective="balanced")
    lean_services.lean_auto_try("Demo/Main.lean", "demo", "exact trivial", cwd=project, timeout_s=17)

    proof_context_args = calls[0][1]
    assert proof_context_args == {
        "file": str(target.resolve()),
        "theorem_id": "demo",
        "include_similar_proofs": True,
        "similarity_threshold": 0.7,
    }

    auto_search_args = calls[1][1]
    assert auto_search_args["file"] == str(target.resolve())
    assert auto_search_args["theorem_id"] == "demo"
    assert auto_search_args["search_budget_s"] == 42.0
    assert auto_search_args["search_depth"] == "normal"
    assert "file_path" not in auto_search_args

    auto_try_args = calls[2][1]
    assert auto_try_args["file"] == str(target.resolve())
    assert auto_try_args["theorem_id"] == "demo"
    assert auto_try_args["proof_attempt"] == "exact trivial"
    assert auto_try_args["timeout_s"] == 17
    assert auto_try_args["return_proof_state"] is True
    assert "file_path" not in auto_try_args


def test_lean_proof_context_prefers_range_scan_when_local_declaration_exists(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n".join(
            [
                "theorem first : True := by",
                "  trivial",
                "",
                "lemma abs_add_diff (a b : Nat) :",
                "    a = a := by",
                "  rfl",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={
            "diagnostics": "",
            "goals": "",
            "code_actions": "",
            "multi_attempt": "",
            "run_code": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "proof_context": "mcp_lean_proof_auto_get_proof_context",
            "auto_probe": "",
            "auto_search": "",
            "auto_try": "",
        },
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda capability: "mcp_lean_proof_auto_scan_theorem")
    calls: list[tuple[str, dict[str, object]]] = []

    def _fake_invoke(tool_name, arguments):
        calls.append((tool_name, dict(arguments)))
        if tool_name == "mcp_lean_proof_auto_scan_theorem":
            return {
                "result": {
                    "status": "success",
                    "theorem": {
                        "name": "abs_add_diff",
                        "kind": "lemma",
                        "location": {"decl_start": 4, "decl_end": 5, "proof_start": 6, "proof_end": 6},
                    },
                }
            }
        return {"result": {"status": "success", "theorem_statement": "lemma abs_add_diff ...", "original_proof": "rfl"}}

    monkeypatch.setattr(lean_services, "_invoke_json_tool", _fake_invoke)

    payload = lean_services.lean_proof_context("Demo/Main.lean", "abs_add_diff", cwd=project)

    assert [tool_name for tool_name, _ in calls[:2]] == [
        "mcp_lean_proof_auto_scan_theorem",
        "mcp_lean_proof_auto_get_proof_context",
    ]
    assert calls[0][1] == {
        "file": str(target.resolve()),
        "target": {"range": {"start_line": 4, "end_line": 6}},
    }
    assert calls[1][1]["theorem_id"] == "abs_add_diff"
    assert payload["success"] is True
    assert payload["backend_tool"] == "mcp_lean_proof_auto_get_proof_context"


def test_lean_proof_context_falls_back_to_local_slice_and_disables_proof_auto_backend(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n".join(
            [
                "theorem first : True := by",
                "  trivial",
                "",
                "lemma abs_add_diff (a b : Nat) :",
                "    a = a := by",
                "  rfl",
                "",
                "theorem next_demo : True := by",
                "  trivial",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "run-proof-auto-fallback")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(
        lean_services,
        "_discover_lean_mcp_tools",
        lambda: {
            "diagnostics": "",
            "goals": "",
            "code_actions": "",
            "multi_attempt": "",
            "run_code": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "proof_context": "mcp_lean_proof_auto_get_proof_context",
            "auto_probe": "mcp_lean_proof_auto_probe",
            "auto_search": "mcp_lean_proof_auto_search_automated_proof",
            "auto_try": "mcp_lean_proof_auto_try_automated_proof",
        },
    )
    monkeypatch.setattr(
        "tools.mcp_tool.get_mcp_status",
        lambda: [
            {
                "name": "lean-proof-auto",
                "role": "secondary-automation-context",
                "managed": True,
                "healthy": True,
                "connected": True,
            }
        ],
    )
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda capability: "mcp_lean_proof_auto_scan_theorem")

    def _fake_invoke(tool_name, arguments):
        if tool_name == "mcp_lean_proof_auto_scan_theorem":
            return {
                "result": {
                    "status": "success",
                    "theorem": {
                        "name": "abs_add_diff",
                        "kind": "lemma",
                        "location": {"decl_start": 4, "decl_end": 5, "proof_start": 6, "proof_end": 6},
                    },
                }
            }
        return {
            "result": {
                "status": "fail",
                "metadata": {
                    "fail_code": "theorem_not_found",
                    "fail_message": "Theorem not found: abs_add_diff",
                },
            }
        }

    monkeypatch.setattr(lean_services, "_invoke_json_tool", _fake_invoke)

    payload = lean_services.lean_proof_context("Demo/Main.lean", "abs_add_diff", cwd=project)
    report = lean_services.probe_capabilities(project)

    assert payload["success"] is True
    assert payload["status"] == "local-fallback"
    assert payload["backend_tool"] == "local-declaration-slice"
    assert "lemma abs_add_diff (a b : Nat) :" in payload["theorem_statement"]
    assert payload["original_proof"] == "rfl"
    assert "first" in payload["in_scope"]
    assert "next_demo" in payload["in_scope"]
    assert any("Theorem not found: abs_add_diff" in reason for reason in payload["degraded_reasons"])
    assert any("proof-auto backend disabled for current run" in reason for reason in payload["degraded_reasons"])
    assert report.mcp_tools["proof_context"] == ""
    assert report.mcp_tools["auto_probe"] == ""
    assert report.mcp_tools["auto_search"] == ""
    assert report.mcp_tools["auto_try"] == ""


def test_auto_probe_and_multi_attempt_use_expected_backend_arguments(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={
            "diagnostics": "",
            "goals": "",
            "code_actions": "",
            "multi_attempt": "mcp_lean_lsp_lean_multi_attempt",
            "run_code": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "proof_context": "",
            "auto_probe": "mcp_lean_proof_auto_probe",
            "auto_search": "",
            "auto_try": "",
        },
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    calls: list[tuple[str, dict[str, object]]] = []

    def _fake_invoke(tool_name, arguments):
        calls.append((tool_name, dict(arguments)))
        if tool_name == "mcp_lean_proof_auto_probe":
            return {"result": {"classification": "failed", "status": "failed"}}
        return {"result": {"success": True, "results": []}}

    monkeypatch.setattr(lean_services, "_invoke_json_tool", _fake_invoke)

    probe_payload = lean_services.lean_auto_probe(
        "Demo/Main.lean",
        "demo",
        cwd=project,
        methods=["aesop", "grind"],
        timeout_s=30,
    )
    lean_services.lean_multi_attempt(
        "Demo/Main.lean",
        12,
        ["simp", "ring"],
        cwd=project,
        column=4,
    )

    probe_calls = [arguments for tool_name, arguments in calls if tool_name == "mcp_lean_proof_auto_probe"]
    assert [entry["mode"] for entry in probe_calls] == ["aesop", "grind"]
    assert all(entry["file"] == str(target.resolve()) for entry in probe_calls)
    assert all(entry["theorem_id"] == "demo" for entry in probe_calls)
    assert all(entry["budget_s"] == 30.0 for entry in probe_calls)
    assert probe_payload["recommended_mode"] == "aesop"

    multi_attempt_args = [arguments for tool_name, arguments in calls if tool_name == "mcp_lean_lsp_lean_multi_attempt"][0]
    assert multi_attempt_args["file_path"] == str(target.resolve())
    assert multi_attempt_args["line"] == 12
    assert multi_attempt_args["column"] == 4
    assert multi_attempt_args["snippets"] == ["simp", "ring"]
    assert "attempts" not in multi_attempt_args


def test_lean_multi_attempt_rejects_invalid_candidate_count_before_backend_call(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={
            "diagnostics": "",
            "goals": "",
            "code_actions": "",
            "multi_attempt": "mcp_lean_lsp_lean_multi_attempt",
            "run_code": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "proof_context": "",
            "auto_probe": "",
            "auto_search": "",
            "auto_try": "",
        },
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("backend should not be called")),
    )

    payload = lean_services.lean_multi_attempt("Demo/Main.lean", 12, ["ring"], cwd=project)

    assert payload["success"] is False
    assert any("expects 2-6 concrete tactic candidates" in reason for reason in payload["degraded_reasons"])
    assert "use `lean_auto_try`" in " ".join(payload["degraded_reasons"])


def test_lean_multi_attempt_rejects_full_proof_blocks_and_sorry(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={
            "diagnostics": "",
            "goals": "",
            "code_actions": "",
            "multi_attempt": "mcp_lean_lsp_lean_multi_attempt",
            "run_code": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "proof_context": "",
            "auto_probe": "",
            "auto_search": "",
            "auto_try": "",
        },
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("backend should not be called")),
    )

    payload = lean_services.lean_multi_attempt(
        "Demo/Main.lean",
        12,
        [
            "theorem demo : True := by\n  sorry",
            "have h : True := by\n  trivial\nexact h",
        ],
        cwd=project,
    )

    assert payload["success"] is False
    assert any("must not contain `sorry`" in reason for reason in payload["degraded_reasons"])
    assert any("expects short local tactic candidates" in reason for reason in payload["degraded_reasons"])


def test_canonical_tool_file_path_prefers_active_file_for_basename_matches(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_FILE", "Demo/Main.lean")

    resolved = lean_services._canonical_tool_file_path("Main.lean", cwd=project)

    assert resolved == str(target.resolve())


def test_auto_probe_surfaces_attempt_diagnostic_summary(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={
            "diagnostics": "",
            "goals": "",
            "code_actions": "",
            "multi_attempt": "",
            "run_code": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "proof_context": "",
            "auto_probe": "mcp_lean_proof_auto_probe",
            "auto_search": "",
            "auto_try": "",
        },
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: {
            "status": "error",
            "diagnostics": [{"severity": "error", "message": "harness_error: Failed to extract declarations"}],
        },
    )

    payload = lean_services.lean_auto_probe("Demo/Main.lean", "demo", cwd=project, methods=["aesop"])

    assert payload["file_path"] == str(target.resolve())
    assert any("harness_error: Failed to extract declarations" in reason for reason in payload["degraded_reasons"])
