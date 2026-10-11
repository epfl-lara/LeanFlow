from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_tools
import tools.implementations.lean_patch as lean_patch
import tools.implementations.lean_tool as lean_tool
from core import verified_edit_authority
from leanflow_cli.lean.lean_services import (
    LeanAxiomReport,
    LeanCapabilityReport,
    LeanInspection,
    LeanSearchResult,
)
from leanflow_cli.workflows.workflow_state import (
    load_verified_patch_status,
    save_verified_patch_status,
)
from tools.utilities.read_freshness import check_freshness, record_read


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
    assert payload["worker_specs"] == []
    assert payload["workers_scope"] == "registered_lean_workflow_specs"
    assert "not live research" in payload["workers_note"]


def _inspection_fixture(target: Path) -> LeanInspection:
    return LeanInspection(
        target=str(target),
        project_root=str(target.parent),
        diagnostics=json.dumps(
            {
                "items": [
                    {"severity": "error", "message": "global import failure", "line": 1},
                    {"severity": "error", "message": "location-free backend error"},
                    {"severity": "warning", "message": "unrelated style warning", "line": 2},
                    {"severity": "info", "message": "target elaboration note", "line": 4},
                    {"severity": "error", "message": "target type mismatch", "line": 5},
                    {"severity": "warning", "message": "target style warning", "line": 6},
                ]
            }
        ),
        goals="target goal",
        sorry_count=2,
        project_sorry_count=3,
        blocker_kind="compile_error",
        queue_items=[
            {
                "label": "target",
                "kind": "theorem",
                "line": 1,
                "end_line": 3,
                "reasons": ["contains sorry"],
            },
            {
                "label": "target",
                "kind": "theorem",
                "line": 4,
                "end_line": 6,
                "reasons": ["contains sorry", "diagnostic in declaration"],
            },
        ],
        capability_report={"project_valid": True},
    )


def test_lean_inspect_tool_without_symbol_preserves_full_file_payload(tmp_path, monkeypatch):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem other : True := by\n  sorry\n\n" "theorem target : True := by\n  sorry\n\n",
        encoding="utf-8",
    )
    inspection = _inspection_fixture(target)
    monkeypatch.setattr(lean_tool, "lean_inspect", lambda *args, **kwargs: inspection)

    payload = json.loads(lean_tool.lean_inspect_tool(str(target)))

    assert payload == {"success": True, **inspection.to_dict()}


def test_lean_inspect_tool_enforces_end_to_end_wall_timeout(tmp_path, monkeypatch):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem target : True := by\n  trivial\n", encoding="utf-8")

    def _slow_inspection(*args, **kwargs):
        time.sleep(0.15)
        return _inspection_fixture(target)

    monkeypatch.setattr(lean_tool, "lean_inspect", _slow_inspection)
    monkeypatch.setenv("LEANFLOW_LEAN_INSPECT_WALL_TIMEOUT_S", "0.03")

    started = time.monotonic()
    payload = json.loads(lean_tool.lean_inspect_tool(str(target), symbol="target"))
    elapsed = time.monotonic() - started

    assert elapsed < 0.12
    assert payload["success"] is False
    assert payload["status"] == "lean_inspect_timeout"
    assert payload["timed_out"] is True
    assert payload["no_progress"] is True
    assert "Do not repeat" in payload["action_required"]


def test_lean_inspect_registry_prefers_valid_file_path_over_symbol_target(tmp_path, monkeypatch):
    target = tmp_path / "Demo.lean"
    target.write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")
    inspection = _inspection_fixture(target)
    captured: dict[str, object] = {}

    def _inspect(selected_target, **kwargs):
        captured["target"] = selected_target
        captured.update(kwargs)
        return inspection

    monkeypatch.setattr(lean_tool, "lean_inspect", _inspect)

    payload = json.loads(
        lean_tool.registry.dispatch(
            "lean_inspect",
            {
                "target": "Demo.target",
                "file_path": "Demo.lean",
                "cwd": str(tmp_path),
                "symbol": "Demo.target",
            },
        )
    )

    assert payload["success"] is True
    assert captured["target"] == str(target.resolve())
    assert captured["symbol"] == "Demo.target"


def test_lean_inspect_registry_rejects_conflicting_real_file_paths(tmp_path):
    target = tmp_path / "Target.lean"
    target.write_text("theorem target : True := by\n  trivial\n", encoding="utf-8")
    other = tmp_path / "Other.lean"
    other.write_text("theorem other : True := by\n  trivial\n", encoding="utf-8")

    payload = json.loads(
        lean_tool.registry.dispatch(
            "lean_inspect",
            {
                "target": str(target),
                "file_path": str(other),
                "cwd": str(tmp_path),
            },
        )
    )

    assert "conflicting Lean file paths" in payload["error"]


def test_lean_inspect_schema_documents_explicit_file_path_precedence():
    properties = lean_tool.LEAN_INSPECT_SCHEMA["parameters"]["properties"]

    assert "file_path" in properties
    assert "wins when target is not a file" in properties["file_path"]["description"]


def test_lean_inspect_tool_unresolved_symbol_fails_open_to_full_file_payload(tmp_path, monkeypatch):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem other : True := by\n  sorry\n\n" "theorem target : True := by\n  sorry\n\n",
        encoding="utf-8",
    )
    inspection = _inspection_fixture(target)
    monkeypatch.setattr(lean_tool, "lean_inspect", lambda *args, **kwargs: inspection)

    payload = json.loads(lean_tool.lean_inspect_tool(str(target), symbol="missing"))

    assert payload == {"success": True, **inspection.to_dict()}


def test_lean_inspect_tool_exact_symbol_bounds_warnings_and_queue_but_keeps_all_errors(
    tmp_path, monkeypatch
):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem other : True := by\n  sorry\n\n" "theorem target : True := by\n  sorry\n\n",
        encoding="utf-8",
    )
    inspection = _inspection_fixture(target)
    monkeypatch.setattr(lean_tool, "lean_inspect", lambda *args, **kwargs: inspection)

    payload = json.loads(lean_tool.lean_inspect_tool(str(target), symbol="Demo.target"))

    diagnostics = json.loads(payload["diagnostics"])["items"]
    assert [item["message"] for item in diagnostics] == [
        "global import failure",
        "location-free backend error",
        "target elaboration note",
        "target type mismatch",
    ]
    assert payload["queue_items"] == [{**inspection.queue_items[1], "line": 4, "end_line": 5}]
    assert payload["inspection_scope"] == "symbol"
    assert payload["inspected_symbol"] == "Demo.target"
    assert payload["declaration_region"] == {
        "kind": "theorem",
        "name": "target",
        "line": 4,
        "end_line": 5,
    }
    assert payload["file_diagnostic_count"] == 6
    assert payload["returned_diagnostic_count"] == 4
    assert payload["omitted_diagnostic_count"] == 2
    assert payload["file_queue_item_count"] == 2
    assert payload["returned_queue_item_count"] == 1
    assert payload["omitted_queue_item_count"] == 1
    assert payload["sorry_count"] == 2
    assert payload["project_sorry_count"] == 3
    capability = payload["capability_report"]
    assert capability["projection"] == "compact_exact_symbol"
    assert capability["full_report_tool"] == "lean_capabilities"
    assert capability["project_valid"] is True
    assert capability["degraded"] is False
    assert capability["report_char_count"] > 0
    assert capability["projected_char_count"] > 0
    assert capability["omitted_top_level_key_count"] == 0


def test_lean_inspect_exact_symbol_reclassifies_empty_goals_with_target_sorry(
    tmp_path, monkeypatch
):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem other : True := by\n  sorry\n\n" "theorem target : True := by\n  sorry\n",
        encoding="utf-8",
    )
    inspection = replace(
        _inspection_fixture(target),
        diagnostics="",
        goals=(
            '{"line_context":"theorem target : True :=", "goals":null, '
            '"goals_before":[], "goals_after":[]}'
        ),
        blocker_kind="open_goals",
    )
    monkeypatch.setattr(lean_tool, "lean_inspect", lambda *args, **kwargs: inspection)

    payload = json.loads(lean_tool.lean_inspect_tool(str(target), symbol="target"))

    assert payload["blocker_kind"] == "sorry"
    assert payload["queue_items"] == [{**inspection.queue_items[1], "line": 4, "end_line": 5}]


def test_lean_inspect_exact_symbol_drops_unrelated_file_queue_blocker(tmp_path, monkeypatch):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem other : True := by\n  sorry\n\n" "theorem target : True := by\n  trivial\n",
        encoding="utf-8",
    )
    inspection = replace(
        _inspection_fixture(target),
        diagnostics="",
        goals='{"goals":null,"goals_before":[],"goals_after":[]}',
        blocker_kind="sorry",
        queue_items=[
            {
                "label": "other",
                "kind": "theorem",
                "line": 1,
                "end_line": 2,
                "reasons": ["contains sorry"],
            }
        ],
    )
    monkeypatch.setattr(lean_tool, "lean_inspect", lambda *args, **kwargs: inspection)

    payload = json.loads(lean_tool.lean_inspect_tool(str(target), symbol="target"))

    assert payload["queue_items"] == []
    assert payload["blocker_kind"] == "none"


def test_lean_inspect_exact_symbol_compacts_capabilities_but_keeps_failure_signals(
    tmp_path, monkeypatch
):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem other : True := by\n  sorry\n\n" "theorem target : True := by\n  sorry\n\n",
        encoding="utf-8",
    )
    full_report = {
        "cwd": str(tmp_path),
        "project_root": str(tmp_path),
        "project_valid": False,
        "project_error": "lake manifest is unavailable",
        "binaries": {"lean": True, "lake": False, "elan": True, "git": True, "rg": True},
        "mcp_tools": {f"tool_{index}": f"mcp_tool_{index}" for index in range(80)},
        "search_providers": [f"provider-{index}" for index in range(30)],
        "helper_tools": {"sorry_analyzer": True, "axiom_checker": False},
        "workers": [f"worker-{index}" for index in range(20)],
        "degraded_reasons": ["diagnostics backend unavailable", "semantic search offline"],
        "managed_mcp_servers": {"lean-lsp": False, "lean-proof-auto": True},
        "power_modes": {f"cache_path_{index}": "x" * 80 for index in range(20)},
        "incremental": {
            "available": False,
            "degraded_reasons": ["REPL unavailable"],
            "degraded_codes": ["repl_missing"],
            "resource_admission": {"large_debug_value": "y" * 2_000},
        },
    }
    inspection = replace(_inspection_fixture(target), capability_report=full_report)
    monkeypatch.setattr(lean_tool, "lean_inspect", lambda *args, **kwargs: inspection)

    payload = json.loads(lean_tool.lean_inspect_tool(str(target), symbol="target"))

    capability = payload["capability_report"]
    source_text = json.dumps(
        full_report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    projected_text = json.dumps(
        capability,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert capability["project_valid"] is False
    assert capability["project_error"] == "lake manifest is unavailable"
    assert capability["degraded"] is True
    assert capability["degraded_reasons"] == [
        "diagnostics backend unavailable",
        "semantic search offline",
    ]
    assert capability["incremental_degraded_reasons"] == ["REPL unavailable"]
    assert capability["incremental_degraded_codes"] == ["repl_missing"]
    assert capability["unavailable_binaries"] == ["lake"]
    assert capability["unavailable_helper_tools"] == ["axiom_checker"]
    assert capability["unavailable_managed_mcp_servers"] == ["lean-lsp"]
    assert capability["report_sha256"] == hashlib.sha256(source_text.encode()).hexdigest()
    assert capability["report_char_count"] == len(source_text)
    assert capability["projected_char_count"] == len(projected_text)
    assert capability["omitted_char_count"] == len(source_text) - len(projected_text)
    assert capability["omitted_top_level_key_count"] > 0
    assert len(projected_text) < 1_100
    assert len(projected_text) * 5 < len(source_text)


def test_lean_inspect_tool_exact_symbol_preserves_unparseable_diagnostics(tmp_path, monkeypatch):
    target = tmp_path / "Demo.lean"
    target.write_text(
        "theorem other : True := by\n  sorry\n\n" "theorem target : True := by\n  sorry\n\n",
        encoding="utf-8",
    )
    inspection = replace(
        _inspection_fixture(target),
        diagnostics="opaque backend output that cannot be classified safely",
    )
    monkeypatch.setattr(lean_tool, "lean_inspect", lambda *args, **kwargs: inspection)

    payload = json.loads(lean_tool.lean_inspect_tool(str(target), symbol="target"))

    assert payload["diagnostics"] == inspection.diagnostics
    assert payload["diagnostics_projection"] == "unparsed_full"
    assert payload["file_diagnostic_count"] is None
    assert payload["returned_diagnostic_count"] is None
    assert payload["omitted_diagnostic_count"] == 0


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


def test_lean_search_tool_filters_sibling_benchmark_matches(monkeypatch, tmp_path):
    benchmark = tmp_path / "IMO2026"
    benchmark.mkdir()
    active = benchmark / "P6.lean"
    sibling = benchmark / "P1.lean"
    active.write_text("theorem active : True := by trivial\n", encoding="utf-8")
    sibling.write_text("theorem hidden : True := by trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", str(active))
    monkeypatch.setattr(
        lean_tool,
        "lean_search",
        lambda *args, **kwargs: LeanSearchResult(
            query="dvd_prod_of_mem",
            mode="local",
            attempted_providers=["project-rg"],
            results=[
                {
                    "provider": "project-rg",
                    "file": str(sibling),
                    "line": 12,
                    "preview": "exact hidden_solution",
                },
                {
                    "provider": "project-rg",
                    "file": str(active),
                    "line": 4,
                    "preview": "exact active_fact",
                },
            ],
            degraded_reasons=[],
        ),
    )

    payload = json.loads(lean_tool.lean_search_tool("dvd_prod_of_mem", cwd=str(tmp_path)))

    assert payload["results"] == [
        {
            "provider": "project-rg",
            "file": str(active),
            "line": 4,
            "preview": "exact active_fact",
        }
    ]
    assert payload["clean_room_omitted_results"] == 1
    assert "Sibling benchmark" in payload["clean_room_guidance"]


def test_lean_search_tool_partitions_confirmed_future_same_file_results(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem assigned : True := by\n"
        "  sorry\n\n"
        "theorem future_result : True := by\n"
        "  trivial\n",
        encoding="utf-8",
    )
    future = {
        "provider": "leanexplore-local",
        "name": "Demo.future_result",
        "module": "Demo",
        "source_link": "https://example.test/Demo.lean#L1-L2",
    }
    imported = {
        "provider": "leanexplore-local",
        "name": "Nat.ModEq",
        "module": "Mathlib.Data.Nat.ModEq",
    }
    monkeypatch.setattr(
        lean_tool,
        "lean_search",
        lambda *args, **kwargs: LeanSearchResult(
            query="demo",
            mode="local",
            attempted_providers=["leanexplore-local"],
            results=[future, imported],
            degraded_reasons=[],
        ),
    )

    payload = json.loads(
        lean_tool.lean_search_tool(
            "demo",
            cwd=str(tmp_path),
            mode="local",
            _leanflow_source_horizon_file=str(active),
            _leanflow_source_horizon_target="assigned",
        )
    )

    assert payload["success"] is True
    assert payload["results"] == [imported]
    assert payload["source_order_inaccessible_results"][0]["provider"] == "leanexplore-local"
    assert payload["source_order_inaccessible_results"][0]["name"] == "Demo.future_result"


def test_lean_search_tool_all_future_results_stay_successful_with_guidance(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem assigned : True := by\n"
        "  sorry\n\n"
        "theorem future_result : True := by\n"
        "  trivial\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lean_tool,
        "lean_search",
        lambda *args, **kwargs: LeanSearchResult(
            query="demo",
            mode="local",
            attempted_providers=["leanexplore-local"],
            results=[
                {
                    "provider": "leanexplore-local",
                    "name": "Demo.future_result",
                    "module": "Demo",
                }
            ],
            degraded_reasons=[],
        ),
    )

    payload = json.loads(
        lean_tool.lean_search_tool(
            "demo",
            cwd=str(tmp_path),
            mode="local",
            _leanflow_source_horizon_file=str(active),
            _leanflow_source_horizon_target="assigned",
        )
    )

    assert payload["success"] is True
    assert payload["results"] == []
    assert payload["source_order_inaccessible_count"] == 1
    assert "assigned proof" in payload["source_order_guidance"]


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
                "include_axiom_profile": True,
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
        "include_axiom_profile": True,
        "timeout_s": 12,
    }


def test_lean_incremental_schema_exposes_target_and_helper_axiom_profiles():
    action = lean_tool.LEAN_INCREMENTAL_CHECK_SCHEMA["parameters"]["properties"]["action"]
    profile = lean_tool.LEAN_INCREMENTAL_CHECK_SCHEMA["parameters"]["properties"][
        "include_axiom_profile"
    ]

    assert action["enum"] == [
        "prepare_file",
        "check_file",
        "check_target",
        "check_helper",
        "feedback",
    ]
    assert profile["type"] == "boolean"
    assert profile["default"] is False
    assert "For `check_target`" in profile["description"]
    assert "For `check_helper`" in profile["description"]
    assert "fail closed" in profile["description"]
    assert (
        "Do not use this option with `prepare_file`, `check_file`, or `feedback`"
        in profile["description"]
    )


def test_apply_verified_patch_schema_defaults_to_cached_incremental_check():
    mode = lean_tool.APPLY_VERIFIED_PATCH_SCHEMA["parameters"]["properties"]["check_mode"]

    assert mode["default"] == "incremental"
    assert "default warm LeanFlow check" in mode["description"]
    timeout = lean_tool.APPLY_VERIFIED_PATCH_SCHEMA["parameters"]["properties"]["timeout_s"]
    assert timeout["default"] == 300
    assert timeout["minimum"] == 1


def test_lean_incremental_schema_explains_dispatch_worker_timeout_floor():
    timeout = lean_tool.LEAN_INCREMENTAL_CHECK_SCHEMA["parameters"]["properties"]["timeout_s"]

    assert timeout["default"] == 60
    assert timeout["minimum"] == 1
    assert "300-second cold-start floor" in timeout["description"]


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


def test_lean_lemma_suggest_tool_returns_ranked_candidates(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_suggest(file_path, theorem_id, *, cwd=None, max_candidates=12):
        captured.update(
            {
                "file_path": file_path,
                "theorem_id": theorem_id,
                "cwd": cwd,
                "max_candidates": max_candidates,
            }
        )
        return {
            "success": True,
            "file_path": file_path,
            "theorem_id": theorem_id,
            "queries": ["List.length ≤", "length"],
            "candidates": [
                {
                    "name": "List.length_append",
                    "signature": "List.length_append : ...",
                    "provider": "leanexplore",
                    "why_relevant": "shares goal symbols: List.length",
                }
            ],
            "degraded_reasons": [],
        }

    monkeypatch.setattr(lean_tool, "lean_lemma_suggest", _fake_suggest)

    payload = json.loads(
        model_tools.handle_function_call(
            "lean_lemma_suggest",
            {"file_path": "Demo/Main.lean", "theorem_id": "demo", "max_candidates": 5},
        )
    )

    assert payload["success"] is True
    assert payload["queries"][0] == "List.length ≤"
    assert payload["candidates"][0]["name"] == "List.length_append"
    assert captured == {
        "file_path": "Demo/Main.lean",
        "theorem_id": "demo",
        "cwd": None,
        "max_candidates": 5,
    }


def test_lean_lemma_suggest_and_outline_are_registered():
    tool_names = set(lean_tool.registry.get_all_tool_names())

    assert "lean_lemma_suggest" in tool_names
    assert "lean_outline" in tool_names


def test_apply_verified_patch_tool_records_broad_check_without_claiming_target_verified(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    calls = []

    def _fake_incremental_check(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "ok": True,
            "has_errors": False,
            "has_sorry": False,
            "action": "check_file",
            "backend": "lean_interact",
            "cache": {"reused_server": True},
        }

    monkeypatch.setattr(lean_patch, "lean_incremental_check", _fake_incremental_check)

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
    assert payload["status"] == "patch_elaborated"
    assert payload["patch_applied"] is True
    assert payload["check_passed"] is True
    assert payload["patch_elaborated"] is True
    assert payload["target_verified"] is False
    assert payload["verified"] is False
    assert len(payload["verified_source_revision_sha256"]) == 64
    assert payload["verification_source_unchanged"] is True
    assert payload["check_mode"] == "incremental"
    assert payload["verification"]["backend"] == "lean_interact"
    assert payload["verification"]["cache"]["reused_server"] is True
    assert calls == [
        {
            "action": "check_file",
            "file_path": str(target),
            "cwd": str(tmp_path),
            "timeout_s": 300,
        }
    ]
    assert payload["checkpoint_id"].startswith("vpatch-")
    assert "trivial" in target.read_text(encoding="utf-8")
    status = load_verified_patch_status()
    assert status["status"] == "patch_elaborated"
    assert status["target_verified"] is False
    assert "exact target gate is still required" in status["message"]


def test_apply_verified_patch_rejects_stale_fuzzy_context_before_verification(
    tmp_path, monkeypatch
):
    """Do not relocate a verified patch when its trailing declaration is stale."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    before = """\
private lemma safe_branch : True := by
  trivial

def result : Nat := 0
"""
    target.write_text(before, encoding="utf-8")

    monkeypatch.setattr(
        lean_patch,
        "lean_incremental_check",
        lambda **_kwargs: pytest.fail(
            "a stale fuzzy patch must be rejected before Lean verification"
        ),
    )
    patch = f"""\
*** Begin Patch
*** Update File: {target}
@@
-private lemma safe_branch : True := by
+private lemma safe_branch : True := by
@@
   trivial
+
+private lemma checked_helper : True := by
+  trivial
 
 def stale_successor : Nat := 0
*** End Patch"""

    payload = json.loads(
        lean_tool.apply_verified_patch_tool(
            str(target), patch, cwd=str(tmp_path), theorem_id="result"
        )
    )
    assert payload["success"] is False
    assert payload["status"] == "patch_failed"
    assert target.read_text(encoding="utf-8") == before


def test_apply_verified_patch_refreshes_caller_file_freshness(tmp_path, monkeypatch):
    """Treat a committed verified patch as the caller's current source image."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    before = "theorem demo : True := by\n  sorry\n"
    after = "theorem demo : True := by\n  trivial\n"
    target.write_text(before, encoding="utf-8")
    record_read("provider-turn", str(target), before)
    monkeypatch.setattr(
        lean_patch,
        "lean_incremental_check",
        lambda **_kwargs: {
            "success": True,
            "ok": True,
            "has_errors": False,
            "has_sorry": False,
        },
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
            str(target),
            patch,
            cwd=str(tmp_path),
            theorem_id="demo",
            task_id="provider-turn",
        )
    )

    assert payload["success"] is True
    assert check_freshness("provider-turn", str(target), after).status == "fresh"


def test_apply_verified_patch_reuses_hash_bound_parent_helper_authority(tmp_path, monkeypatch):
    """Retain one exact helper insertion without replaying an open target."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    verified_edit_authority.clear_for_tests()
    target = tmp_path / "Demo.lean"
    before = "theorem demo : True := by\n  sorry\n"
    helper = "private lemma checked_helper : True := by\n  trivial"
    after = helper + "\n\n" + before
    target.write_text(before, encoding="utf-8")
    token = verified_edit_authority.register(
        path=str(target),
        theorem_id="demo",
        before_sha256=hashlib.sha256(before.encode()).hexdigest(),
        after_sha256=hashlib.sha256(after.encode()).hexdigest(),
        verified_declaration="checked_helper",
        axiom_profile_axioms=("Classical.choice",),
    )
    monkeypatch.setattr(
        lean_patch,
        "lean_incremental_check",
        lambda **_kwargs: pytest.fail("authenticated helper insertion replayed Lean"),
    )
    patch = f"""\
*** Begin Patch
*** Update File: {target}
@@
+{helper.replace(chr(10), chr(10) + '+')}
+
 theorem demo : True := by
*** End Patch"""

    payload = json.loads(
        lean_tool.apply_verified_patch_tool(
            str(target),
            patch,
            cwd=str(tmp_path),
            theorem_id="demo",
            verified_edit_authority_token=token,
        )
    )

    assert payload["success"] is True
    assert payload["authenticated_helper_insertion"] is True
    assert payload["broad_verification_skipped"] is True
    assert payload["target_verified"] is False
    assert payload["verification"]["target"] == "checked_helper"
    assert payload["verification"]["axiom_profile_axioms"] == ["Classical.choice"]
    assert target.read_text(encoding="utf-8") == after


def test_apply_verified_patch_rejects_authority_for_a_different_source_image(tmp_path, monkeypatch):
    """Fall back to Lean and rollback when an authorized patch gains extra content."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    verified_edit_authority.clear_for_tests()
    target = tmp_path / "Demo.lean"
    before = "theorem demo : True := by\n  sorry\n"
    helper = "private lemma checked_helper : True := by\n  trivial"
    authorized_after = helper + "\n\n" + before
    target.write_text(before, encoding="utf-8")
    token = verified_edit_authority.register(
        path=str(target),
        theorem_id="demo",
        before_sha256=hashlib.sha256(before.encode()).hexdigest(),
        after_sha256=hashlib.sha256(authorized_after.encode()).hexdigest(),
        verified_declaration="checked_helper",
        axiom_profile_axioms=(),
    )
    calls: list[dict[str, object]] = []

    def reject_unverified_image(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "ok": False,
            "has_errors": True,
            "action": "check_file",
            "backend": "lean_interact",
            "output": "unverified source image",
        }

    monkeypatch.setattr(lean_patch, "lean_incremental_check", reject_unverified_image)
    patch = f"""\
*** Begin Patch
*** Update File: {target}
@@
+{helper.replace(chr(10), chr(10) + '+')}
+
+-- unverified extra content
+
 theorem demo : True := by
*** End Patch"""

    payload = json.loads(
        lean_tool.apply_verified_patch_tool(
            str(target),
            patch,
            cwd=str(tmp_path),
            theorem_id="demo",
            verified_edit_authority_token=token,
        )
    )

    assert payload["success"] is False
    assert payload["authenticated_helper_insertion"] is False
    assert payload["broad_verification_skipped"] is False
    assert payload["rolled_back"] is True
    assert calls
    assert target.read_text(encoding="utf-8") == before


def test_apply_verified_patch_rejects_verification_crossing_source_revision(tmp_path, monkeypatch):
    """Do not authenticate a broad check when its file changes before return."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    def verify_and_mutate(**kwargs):
        target.write_text(
            "theorem demo : True := by\n  trivial\n\n-- concurrent change\n",
            encoding="utf-8",
        )
        return {
            "success": True,
            "ok": True,
            "has_errors": False,
            "action": "check_file",
            "backend": "lean_interact",
        }

    monkeypatch.setattr(lean_patch, "lean_incremental_check", verify_and_mutate)
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

    assert payload["success"] is False
    assert payload["status"] == "check_failed"
    assert payload["verification"]["ok"] is True
    assert payload["verification_source_unchanged"] is False
    assert "source changed during verification" in payload["message"]


def test_apply_verified_patch_tool_persists_check_failed_status(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    monkeypatch.setattr(
        lean_patch,
        "lean_incremental_check",
        lambda **kwargs: {
            "success": True,
            "ok": False,
            "has_errors": True,
            "action": "check_file",
            "backend": "lean_interact",
            "output": "unsolved goals",
        },
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
    assert payload["patch_applied"] is False
    assert payload["patch_applied_before_rollback"] is True
    assert payload["rolled_back"] is True
    assert payload["verification"]["output"] == "unsolved goals"
    assert target.read_text(encoding="utf-8") == "theorem demo : True := by\n  sorry\n"
    assert load_verified_patch_status()["status"] == "check_failed"


def test_apply_verified_patch_forwards_requested_incremental_timeout(tmp_path, monkeypatch):
    """Let hard declarations opt into a realistic LeanProbe deadline."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        lean_patch,
        "lean_incremental_check",
        lambda **kwargs: calls.append(kwargs)
        or {
            "success": True,
            "ok": True,
            "has_errors": False,
            "action": "check_file",
        },
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
            str(target),
            patch,
            cwd=str(tmp_path),
            timeout_s=600,
        )
    )

    assert payload["success"] is True
    assert calls[0]["timeout_s"] == 600


def test_apply_verified_patch_denies_scratch_worker_without_replacing_status(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "ResearchScratch.lean"
    target.write_text("theorem scratch : True := by\n  sorry\n", encoding="utf-8")
    save_verified_patch_status(
        {
            "status": "verified",
            "path": str(tmp_path / "Authoritative.lean"),
            "verified": True,
        }
    )
    before_status = load_verified_patch_status()
    monkeypatch.setenv("LEANFLOW_DISPATCH_SCRATCH_ONLY", "1")
    patch = f"""\
*** Begin Patch
*** Update File: {target}
 theorem scratch : True := by
-  sorry
+  trivial
*** End Patch"""

    payload = json.loads(lean_tool.apply_verified_patch_tool(str(target), patch, cwd=str(tmp_path)))

    assert payload["status"] == "scratch_only_write_denied"
    assert payload["patch_applied"] is False
    assert "sorry" in target.read_text(encoding="utf-8")
    assert load_verified_patch_status() == before_status


def test_apply_verified_patch_tool_reports_no_changes_without_verifying(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    original = "theorem demo : True := by\n  trivial\n"
    target.write_text(original, encoding="utf-8")
    verify_called = {"value": False}

    def _fake_verify(**kwargs):
        verify_called["value"] = True
        return SimpleNamespace(to_dict=lambda: {"ok": True})

    monkeypatch.setattr(lean_patch, "lean_incremental_check", _fake_verify)

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


def test_apply_verified_patch_rejects_exact_duplicate_helper_before_verifying(
    tmp_path, monkeypatch
):
    """Do not mutate or invoke Lean for an already-present helper insertion."""
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    helper = "private lemma checked_map : True := by\n  trivial"
    original = helper + "\n\ntheorem demo : True := by\n  sorry\n"
    target.write_text(original, encoding="utf-8")
    verify_called = {"value": False}

    def _fake_verify(**kwargs):
        verify_called["value"] = True
        return {"success": True, "has_errors": False, "timed_out": False}

    monkeypatch.setattr(lean_patch, "lean_incremental_check", _fake_verify)
    added_helper = helper.replace("\n", "\n+")
    patch = f"""\
*** Begin Patch
*** Update File: {target}
@@
+{added_helper}
+
 private lemma checked_map : True := by
*** End Patch"""

    payload = json.loads(lean_tool.apply_verified_patch_tool(str(target), patch, cwd=str(tmp_path)))

    assert payload["success"] is False
    assert payload["status"] == "patch_failed"
    assert payload["patch_applied"] is False
    assert "duplicated existing lemma checked_map" in payload["message"]
    assert target.read_text(encoding="utf-8") == original
    assert verify_called["value"] is False


def test_apply_verified_patch_tool_blocks_statement_changes_before_verify(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    target = tmp_path / "Demo.lean"
    original = "theorem demo : True := by\n  trivial\n"
    target.write_text(original, encoding="utf-8")
    verify_called = {"value": False}

    def _fake_verify(**kwargs):
        verify_called["value"] = True
        return SimpleNamespace(to_dict=lambda: {"ok": True})

    monkeypatch.setattr(lean_patch, "lean_incremental_check", _fake_verify)
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


def test_lean_axioms_tool_reports_inspection_failure(monkeypatch):
    monkeypatch.setattr(
        lean_tool,
        "lean_axioms",
        lambda *args, **kwargs: LeanAxiomReport(
            target="demo",
            file_path="Demo.lean",
            ok=False,
            axioms=[],
            custom_axioms=[],
            classical=False,
            choice=False,
            note="Lean axiom inspection exited with status 1.",
            inspection_succeeded=False,
        ),
    )

    payload = json.loads(lean_tool.lean_axioms_tool("demo", file_path="Demo.lean"))

    assert payload["success"] is False
    assert payload["inspection_succeeded"] is False
