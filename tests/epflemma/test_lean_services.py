from __future__ import annotations

import sys
import types
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


def test_lean_search_uses_leanexplore_summary_results(monkeypatch, tmp_path):
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
            mcp_tools={"leanexplore": "mcp_lean_explore_search_summary"},
            search_providers=["mcp-leanexplore"],
            helper_tools={"search_fallback": True},
            workers=[],
            degraded_reasons=[],
        ),
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def _fake_invoke(tool_name, arguments):
        calls.append((tool_name, dict(arguments)))
        return {
            "result": (
                '{"results": ['
                '{"id": 12345, "name": "Nat.Prime.dvd_mul", '
                '"module": "Mathlib.Data.Nat.Prime.Basic", '
                '"description": "Divisibility of a product by a prime"}'
                "], \"count\": 1}"
            )
        }

    monkeypatch.setattr(lean_services, "_invoke_json_tool", _fake_invoke)
    monkeypatch.setattr(lean_services, "_rg_search", lambda root, query, *, limit=10: [])

    result = lean_services.lean_search("prime number divisibility", cwd=project, mode="semantic", limit=3)

    assert calls == [
        (
            "mcp_lean_explore_search_summary",
            {
                "query": "prime number divisibility",
                "q": "prime number divisibility",
                "path": "",
                "file_path": "",
                "limit": 3,
            },
        )
    ]
    assert result.attempted_providers == ["mcp-leanexplore"]
    assert result.results == [
        {
            "provider": "mcp-leanexplore",
            "match": "Nat.Prime.dvd_mul - [Mathlib.Data.Nat.Prime.Basic] - Divisibility of a product by a prime",
        }
    ]


def test_leanexplore_local_search_retries_without_reranker_on_meta_tensor(monkeypatch):
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_local_status",
        lambda: {"package_available": True, "data_ready": True, "cache_path": "/tmp/cache", "available": True},
    )
    monkeypatch.setattr(lean_services, "_LEANEXPLORE_LOCAL_SERVICE", None)
    monkeypatch.setattr(lean_services, "_LEANEXPLORE_LOCAL_RERANK_DISABLED", False)
    calls: list[int | None] = []

    class FakeService:
        async def search(self, *, query, limit, rerank_top):
            calls.append(rerank_top)
            if rerank_top == 50:
                raise RuntimeError(
                    "Cannot copy out of meta tensor; no data! Please use "
                    "torch.nn.Module.to_empty() instead of torch.nn.Module.to()"
                )
            return types.SimpleNamespace(
                results=[
                    {
                        "name": "Nat.sum_divisors",
                        "module": "Mathlib.NumberTheory.ArithmeticFunction.Misc",
                    }
                ]
            )

    fake_package = types.ModuleType("lean_explore")
    fake_search = types.ModuleType("lean_explore.search")
    fake_search.Service = FakeService
    monkeypatch.setitem(sys.modules, "lean_explore", fake_package)
    monkeypatch.setitem(sys.modules, "lean_explore.search", fake_search)

    results, error = lean_services._leanexplore_local_search("Nat.sumDivisors", limit=3)

    assert error == ""
    assert calls == [50, 0]
    assert results == [
        {
            "provider": "leanexplore-local",
            "match": "Nat.sum_divisors - [Mathlib.NumberTheory.ArithmeticFunction.Misc]",
            "name": "Nat.sum_divisors",
            "module": "Mathlib.NumberTheory.ArithmeticFunction.Misc",
        }
    ]


def test_leanexplore_local_search_reuses_service_and_suppresses_noise(monkeypatch, capsys):
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_local_status",
        lambda: {"package_available": True, "data_ready": True, "cache_path": "/tmp/cache", "available": True},
    )
    monkeypatch.setattr(lean_services, "_LEANEXPLORE_LOCAL_SERVICE", None)
    monkeypatch.setattr(lean_services, "_LEANEXPLORE_LOCAL_RERANK_DISABLED", False)
    monkeypatch.delenv("EPFLEMMA_LEANEXPLORE_VERBOSE", raising=False)
    monkeypatch.delenv("LEANEXPLORE_VERBOSE", raising=False)
    constructed = 0
    calls: list[int | None] = []

    class FakeService:
        def __init__(self):
            nonlocal constructed
            constructed += 1

        async def search(self, *, query, limit, rerank_top):
            print("BM25S noisy progress")
            print("torch cuda warning", file=sys.stderr)
            calls.append(rerank_top)
            return types.SimpleNamespace(
                results=[
                    {
                        "name": "Nat.mod_eq_of_lt",
                        "module": "Init.Data.Nat.Div.Basic",
                    }
                ]
            )

    fake_package = types.ModuleType("lean_explore")
    fake_search = types.ModuleType("lean_explore.search")
    fake_search.Service = FakeService
    monkeypatch.setitem(sys.modules, "lean_explore", fake_package)
    monkeypatch.setitem(sys.modules, "lean_explore.search", fake_search)

    first_results, first_error = lean_services._leanexplore_local_search("Nat.mod_eq_of_lt", limit=1)
    second_results, second_error = lean_services._leanexplore_local_search("Nat.mod_eq_of_lt", limit=1)
    captured = capsys.readouterr()

    assert first_error == ""
    assert second_error == ""
    assert first_results == second_results
    assert constructed == 1
    assert calls == [50, 50]
    assert "BM25S noisy progress" not in captured.out
    assert "torch cuda warning" not in captured.err


def test_probe_capabilities_reports_direct_leanexplore_api(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    monkeypatch.setenv("LEANEXPLORE_API_KEY", "sk-test")
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
            "state_search": "",
            "hammer_premise": "",
            "hover_info": "",
            "file_outline": "",
            "declaration_file": "",
            "profile_proof": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "leanexplore": "",
            "proof_context": "",
            "auto_probe": "",
            "auto_search": "",
            "auto_try": "",
        },
    )
    monkeypatch.setattr("tools.mcp_tool.get_mcp_status", lambda: [])
    monkeypatch.setattr(
        "epflemma_cli.mcp_bootstrap.managed_mcp_power_status",
        lambda project_root=None: {},
    )

    report = lean_services.probe_capabilities(project)

    assert "leanexplore-api" in report.search_providers
    assert "no search providers available" not in report.degraded_reasons


def test_probe_capabilities_reports_local_leanexplore_when_data_is_ready(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    home = tmp_path / "lean_explore"
    cache = home / "cache" / "20260127_103630"
    cache.mkdir(parents=True)
    (home / "active_version").write_text("20260127_103630", encoding="utf-8")
    for entry in (
        "lean_explore.db",
        "informalization_faiss.index",
        "informalization_faiss_ids_map.json",
        "bm25_ids_map.json",
    ):
        (cache / entry).write_text("stub", encoding="utf-8")
    (cache / "bm25_name_raw").mkdir()
    (cache / "bm25_name_spaced").mkdir()
    monkeypatch.setenv("LEAN_EXPLORE_CACHE_DIR", str(home / "cache"))
    monkeypatch.setattr(lean_services.importlib.util, "find_spec", lambda name: object())
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
            "state_search": "",
            "hammer_premise": "",
            "hover_info": "",
            "file_outline": "",
            "declaration_file": "",
            "profile_proof": "",
            "local_search": "",
            "leanfinder": "",
            "leansearch": "",
            "loogle": "",
            "leanexplore": "",
            "proof_context": "",
            "auto_probe": "",
            "auto_search": "",
            "auto_try": "",
        },
    )
    monkeypatch.setattr("tools.mcp_tool.get_mcp_status", lambda: [])
    monkeypatch.setattr(
        "epflemma_cli.mcp_bootstrap.managed_mcp_power_status",
        lambda project_root=None: {},
    )

    report = lean_services.probe_capabilities(project)

    assert report.search_providers[0] == "leanexplore-local"
    assert report.power_modes["leanexplore_local_available"] is True
    assert report.power_modes["leanexplore_local_cache_path"] == str(cache)


def test_lean_search_uses_direct_leanexplore_api_before_mcp_semantic_provider(monkeypatch, tmp_path):
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
            mcp_tools={
                "leanfinder": "mcp_lean_lsp_leanfinder",
                "leanexplore": "mcp_lean_explore_search_summary",
            },
            search_providers=["leanexplore-api", "mcp-leanexplore", "mcp-leanfinder"],
            helper_tools={"search_fallback": True},
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_api_search",
        lambda query, *, limit=10: (
            [
                {
                    "provider": "leanexplore-api",
                    "match": "Nat.Prime.dvd_mul - [Mathlib.Data.Nat.Prime.Basic] - Divisibility of a product by a prime",
                    "id": 12345,
                    "name": "Nat.Prime.dvd_mul",
                    "module": "Mathlib.Data.Nat.Prime.Basic",
                }
            ],
            "",
        ),
    )
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("MCP semantic provider should not be called")),
    )
    monkeypatch.setattr(lean_services, "_rg_search", lambda root, query, *, limit=10: [])

    result = lean_services.lean_search("prime number divisibility", cwd=project, mode="semantic", limit=3)

    assert result.attempted_providers == ["leanexplore-api"]
    assert result.results[0]["provider"] == "leanexplore-api"
    assert result.results[0]["name"] == "Nat.Prime.dvd_mul"


def test_lean_search_prefers_local_leanexplore_before_api_and_mcp(monkeypatch, tmp_path):
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
            mcp_tools={
                "leanfinder": "mcp_lean_lsp_leanfinder",
                "leanexplore": "mcp_lean_explore_search_summary",
            },
            search_providers=["leanexplore-local", "leanexplore-api", "mcp-leanexplore", "mcp-leanfinder"],
            helper_tools={"search_fallback": True},
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_local_search",
        lambda query, *, limit=10: (
            [
                {
                    "provider": "leanexplore-local",
                    "match": "Nat.Prime.dvd_mul - [Mathlib.Data.Nat.Prime.Basic] - Divisibility of a product by a prime",
                    "id": 12345,
                    "name": "Nat.Prime.dvd_mul",
                    "module": "Mathlib.Data.Nat.Prime.Basic",
                }
            ],
            "",
        ),
    )
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_api_search",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("API fallback should not be called")),
    )
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("MCP semantic provider should not be called")),
    )
    monkeypatch.setattr(lean_services, "_rg_search", lambda root, query, *, limit=10: [])

    result = lean_services.lean_search("prime number divisibility", cwd=project, mode="semantic", limit=3)

    assert result.attempted_providers == ["leanexplore-local"]
    assert result.results[0]["provider"] == "leanexplore-local"
    assert result.results[0]["name"] == "Nat.Prime.dvd_mul"


def test_lean_search_local_mode_uses_local_leanexplore_cache(monkeypatch, tmp_path):
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
            search_providers=["leanexplore-local", "project-rg", "mathlib-rg"],
            helper_tools={"search_fallback": True},
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_local_search",
        lambda query, *, limit=10: (
            [
                {
                    "provider": "leanexplore-local",
                    "match": "isUnit_gcd_of_eq_mul_gcd - [Mathlib.Algebra.GCDMonoid.Basic]",
                    "name": "isUnit_gcd_of_eq_mul_gcd",
                }
            ],
            "",
        ),
    )
    monkeypatch.setattr(lean_services, "_rg_search", lambda *args, **kwargs: [])

    result = lean_services.lean_search("isUnit_gcd_of_eq_mul_gcd", cwd=project, mode="local", limit=3)

    assert result.attempted_providers == ["leanexplore-local"]
    assert result.results[0]["provider"] == "leanexplore-local"
    assert result.results[0]["name"] == "isUnit_gcd_of_eq_mul_gcd"


def test_lean_search_type_pattern_falls_back_to_local_leanexplore(monkeypatch, tmp_path):
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
            mcp_tools={"loogle": "mcp_lean_lsp_lean_loogle"},
            search_providers=["leanexplore-local", "mcp-loogle", "project-rg", "mathlib-rg"],
            helper_tools={"search_fallback": True},
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(lean_services, "_invoke_json_tool", lambda *args, **kwargs: {"results": []})
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_local_search",
        lambda query, *, limit=10: (
            [
                {
                    "provider": "leanexplore-local",
                    "match": "isUnit_gcd_of_eq_mul_gcd - [Mathlib.Algebra.GCDMonoid.Basic]",
                    "name": "isUnit_gcd_of_eq_mul_gcd",
                }
            ],
            "",
        ),
    )
    monkeypatch.setattr(lean_services, "_rg_search", lambda *args, **kwargs: [])

    result = lean_services.lean_search(
        "isUnit_gcd_of_eq_mul_gcd : GCDMonoid",
        cwd=project,
        mode="type-pattern",
        limit=3,
    )

    assert result.attempted_providers == ["mcp-loogle", "leanexplore-local"]
    assert result.results[0]["provider"] == "leanexplore-local"
    assert result.results[0]["name"] == "isUnit_gcd_of_eq_mul_gcd"


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


def test_lean_inspect_queue_includes_diagnostic_declaration_range(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "\n".join(
            [
                "theorem broken : True := by",
                "  exact ?missing",
                "",
                "theorem later : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )
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
            search_providers=[],
            helper_tools={},
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(
        lean_services,
        "_diagnostics_text",
        lambda file_path, project_root, mcp_tools: (
            '{"severity": "error", "message": "unsolved goals", "line": 2, "column": 9}'
        ),
    )
    monkeypatch.setattr(lean_services, "_goals_text", lambda *args, **kwargs: "no goals")
    monkeypatch.setattr(lean_services, "_project_sorry_stats", lambda project_root: (1, ["Main.lean"]))
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args, **kwargs: None)

    inspection = lean_services.lean_inspect(str(target), cwd=project)

    assert inspection.queue_items[0]["label"] == "broken"
    assert "diagnostic near line 2" in inspection.queue_items[0]["reasons"]
    assert inspection.queue_items[1]["label"] == "later"


def test_lean_inspect_queue_ignores_info_and_leaves_style_warning_for_final_sweep(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "\n".join(
            [
                "def isLipschitz (f : Nat -> Nat) : Prop := True",
                "#check isLipschitz",
                "",
                "lemma style_warning : True := by",
                "  have h : True := by trivial",
                "  cases' h",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )
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
            search_providers=[],
            helper_tools={},
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(
        lean_services,
        "_diagnostics_text",
        lambda file_path, project_root, mcp_tools: (
            '{"items":[{"severity":"info","message":"isLipschitz : Prop","line":2,"column":1},'
            '{"severity":"warning","message":"The cases tactic is discouraged","line":6,"column":3}]}'
        ),
    )
    monkeypatch.setattr(lean_services, "_goals_text", lambda *args, **kwargs: "no goals")
    monkeypatch.setattr(lean_services, "_project_sorry_stats", lambda project_root: (0, []))
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args, **kwargs: None)

    inspection = lean_services.lean_inspect(str(target), cwd=project)

    assert inspection.queue_items == []


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
            "mcp_lean_lsp_lean_state_search",
            "mcp_lean_lsp_lean_hammer_premise",
            "mcp_lean_lsp_lean_hover_info",
            "mcp_lean_lsp_lean_file_outline",
            "mcp_lean_lsp_lean_declaration_file",
            "mcp_lean_lsp_lean_profile_proof",
            "mcp_lean_explore_search_summary",
            "mcp_lean_proof_auto_get_proof_context",
            "mcp_lean_proof_auto_probe",
            "mcp_lean_proof_auto_try_automated_proof",
        ],
    )

    discovered = lean_services._discover_lean_mcp_tools()

    assert discovered["diagnostics"] == "mcp_lean_lsp_lean_diagnostic_messages"
    assert discovered["goals"] == "mcp_lean_lsp_lean_goal"
    assert discovered["multi_attempt"] == "mcp_lean_lsp_lean_multi_attempt"
    assert discovered["state_search"] == "mcp_lean_lsp_lean_state_search"
    assert discovered["hammer_premise"] == "mcp_lean_lsp_lean_hammer_premise"
    assert discovered["hover_info"] == "mcp_lean_lsp_lean_hover_info"
    assert discovered["file_outline"] == "mcp_lean_lsp_lean_file_outline"
    assert discovered["declaration_file"] == "mcp_lean_lsp_lean_declaration_file"
    assert discovered["profile_proof"] == "mcp_lean_lsp_lean_profile_proof"
    assert discovered["leanexplore"] == "mcp_lean_explore_search_summary"
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


def test_proof_context_and_auto_search_use_expected_backend_arguments(monkeypatch, tmp_path):
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
            "auto_search": "mcp_lean_proof_auto_search_automated_proof",
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


def test_lean_auto_try_preflights_unsupported_project_option(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n".join(
            [
                "import Mathlib",
                "set_option linter.style.longLine false",
                "theorem demo : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )
    tool_name = "mcp_lean_proof_auto_try_automated_proof"
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
            "auto_probe": "",
            "auto_search": "",
            "auto_try": tool_name,
        },
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    calls = []
    monkeypatch.setattr(lean_services, "_invoke_json_tool", lambda *args, **kwargs: calls.append(args) or {})
    outcomes = []
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args: outcomes.append(args))

    payload = lean_services.lean_auto_try("Demo/Main.lean", "demo", "exact trivial", cwd=project)

    assert payload["success"] is False
    reasons = " ".join(payload["degraded_reasons"])
    assert "linter.style.longLine" in reasons
    assert "before MCP call" in reasons
    assert payload["setup_blocker"]["kind"] == "unsupported_project_option"
    assert tool_name in lean_services._disabled_mcp_tools_for_run(project)
    assert calls == []
    assert outcomes[-1][1]["success"] is False


def test_lean_auto_try_marks_harness_construction_failure_as_setup_blocker(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    tool_name = "mcp_lean_proof_auto_try_automated_proof"
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
            "auto_probe": "",
            "auto_search": "",
            "auto_try": tool_name,
        },
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "auto-try-harness-failure")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: {
            "result": {
                "success": False,
                "status": "error",
                "error_message": "Validation error: Failed to construct harness: Target theorem 'demo' has unsafe value range shape: empty",
            }
        },
    )
    outcomes = []
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args: outcomes.append(args))

    payload = lean_services.lean_auto_try("Demo/Main.lean", "demo", "exact trivial", cwd=project)

    assert payload["success"] is False
    assert payload["setup_blocker"]["kind"] == "proof_auto_harness_construction"
    reasons = " ".join(payload["degraded_reasons"])
    assert "disabled for this run" in reasons
    assert "managed patch verification" in reasons
    assert tool_name in lean_services._disabled_mcp_tools_for_run(project)
    assert len(outcomes) == 1
    assert outcomes[0][1]["setup_blocker"]["kind"] == "proof_auto_harness_construction"


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


def test_lean_proof_context_falls_back_when_backend_returns_empty_context(monkeypatch, tmp_path):
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
                "theorem demo : True := by",
                "  trivial",
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
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda capability: "")
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: {
            "result": {
                "status": "success",
                "theorem_statement": "",
                "original_proof": "",
                "value_range": {"start": {"line": 1, "column": 0}, "end": {"line": 1, "column": 0}},
            }
        },
    )

    payload = lean_services.lean_proof_context("Demo/Main.lean", "demo", cwd=project)

    assert payload["success"] is True
    assert payload["status"] == "local-fallback"
    assert payload["backend_tool"] == "local-declaration-slice"
    assert payload["theorem_statement"] == "theorem demo : True"
    assert payload["original_proof"] == "trivial"
    assert any("empty declaration context" in reason for reason in payload["degraded_reasons"])


def test_local_proof_context_uses_scan_location_to_avoid_next_doc_comment(monkeypatch, tmp_path):
    target = tmp_path / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  sorry",
                "",
                "/-- next theorem doc comment -/",
                "theorem next_demo : True := by",
                "  trivial",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lean_services,
        "_declaration_index",
        lambda path: [
            {
                "name": "demo",
                "kind": "theorem",
                "line": 1,
                "end_line": 6,
                "text": (
                    "theorem demo : True := by\n"
                    "  sorry\n\n"
                    "/-- next theorem doc comment -/\n"
                    "theorem next_demo : True := by\n"
                    "  trivial"
                ),
            },
            {"name": "next_demo", "kind": "theorem", "line": 5, "end_line": 6, "text": "theorem next_demo : True := by\n  trivial"},
        ],
    )

    payload = lean_services._local_proof_context_payload(
        target,
        "demo",
        degraded_reasons=["empty declaration context"],
        scan_payload={
            "theorem": {
                "name": "demo",
                "kind": "theorem",
                "location": {"decl_start": 1, "decl_end": 1, "proof_start": 2, "proof_end": 2},
            }
        },
    )

    assert payload is not None
    assert payload["theorem_statement"] == "theorem demo : True"
    assert payload["original_proof"] == "sorry"
    assert "next theorem doc comment" not in payload["original_proof"]


def test_lean_proof_context_falls_back_to_local_slice_without_disabling_proof_auto_backend(monkeypatch, tmp_path):
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
    assert any("without disabling proof-auto MCP" in reason for reason in payload["degraded_reasons"])
    assert not any("proof-auto backend disabled for current run" in reason for reason in payload["degraded_reasons"])
    assert report.mcp_tools["proof_context"] == "mcp_lean_proof_auto_get_proof_context"
    assert report.mcp_tools["auto_search"] == "mcp_lean_proof_auto_search_automated_proof"
    assert "auto_probe" not in report.mcp_tools
    assert "auto_try" not in report.mcp_tools
    assert not any("disabled for current run" in reason for reason in report.degraded_reasons)


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
    assert "patch the file" in " ".join(payload["degraded_reasons"])


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


def test_project_root_prefers_native_project_env_when_cwd_omitted(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    (project / "lakefile.toml").write_text("[package]\nname = \"Demo\"\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))

    root, error = lean_services._project_root()

    assert root == project.resolve()
    assert error == ""


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


def test_auto_probe_prefers_incremental_probe_when_available(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
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
        incremental={"available": True},
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(
        lean_services,
        "_local_incremental_auto_probe",
        lambda **kwargs: {
            "success": False,
            "backend_tool": "lean_incremental_check",
            "degraded_reasons": [],
            "file_path": kwargs["file_path"],
            "theorem_id": kwargs["theorem_id"],
            "attempts": [{"mode": "aesop", "status": "failed"}],
            "recommended_mode": "aesop",
        },
    )
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("MCP probe should not be called")),
    )

    payload = lean_services.lean_auto_probe("Demo/Main.lean", "demo", cwd=project, methods=["aesop"])

    assert payload["backend_tool"] == "lean_incremental_check"
    assert payload["file_path"] == str(target.resolve())


def test_incremental_auto_probe_clamps_short_timeout(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={},
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
        incremental={"available": True},
    )
    captured: dict[str, object] = {}

    def _fake_incremental_check(**kwargs):
        captured.update(kwargs)
        return {
            "success": True,
            "ok": False,
            "messages": [],
            "cache": {},
            "valid_without_sorry": False,
            "has_errors": True,
            "has_sorry": False,
        }

    import epflemma_cli.lean_incremental as lean_incremental

    monkeypatch.setattr(lean_incremental, "lean_incremental_check", _fake_incremental_check)

    payload = lean_services._local_incremental_auto_probe(
        file_path=str(target),
        theorem_id="demo",
        cwd=project,
        methods=["aesop"],
        timeout_s=30,
        report=report,
    )

    assert captured["timeout_s"] == 60
    assert payload is not None
    assert payload["attempts"][0]["timing"]["budget_s"] == 60.0
