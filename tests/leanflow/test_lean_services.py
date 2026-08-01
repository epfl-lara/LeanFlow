from __future__ import annotations

import re
import signal
import subprocess
import sys
import types
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.lean import (
    lean_axiom_batch,
    lean_command_timeout,
    lean_incremental,
    lean_search_providers,
    lean_services,
)
from leanflow_cli.lean.lean_services import LeanCapabilityReport


def test_low_memory_mode_disables_in_process_leanexplore(monkeypatch):
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "yes")
    monkeypatch.setenv("LEANFLOW_LEANEXPLORE_BACKEND", "local")

    assert lean_search_providers._leanexplore_backend_preference() == "off"


def test_dispatch_worker_disables_in_process_leanexplore_only_for_worker(monkeypatch):
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "0")
    monkeypatch.setenv("LEANFLOW_LEANEXPLORE_BACKEND", "local")
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")

    assert lean_search_providers._leanexplore_backend_preference() == "off"

    monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER")
    assert lean_search_providers._leanexplore_backend_preference() == "local"


def test_dispatch_worker_can_opt_into_remote_leanexplore(monkeypatch):
    monkeypatch.setenv("LEANFLOW_LOW_MEMORY", "0")
    monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
    monkeypatch.setenv("LEANFLOW_DISPATCH_LEANEXPLORE_BACKEND", "api")

    assert lean_search_providers._leanexplore_backend_preference() == "api"


def test_run_command_terminates_the_process_group_on_timeout(monkeypatch, tmp_path):
    class Process:
        pid = 4321
        returncode = None
        communicate_calls = 0

        def communicate(self, timeout):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired(["lake", "env", "lean"], timeout)
            return "", None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired(["lake", "env", "lean"], timeout)

    process = Process()
    popen_kwargs = {}
    signals = []

    def popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return process

    monkeypatch.setattr(lean_services.subprocess, "Popen", popen)
    monkeypatch.setattr(
        lean_services.os,
        "killpg",
        lambda pid, sent: signals.append((pid, sent)),
    )

    code, output = lean_services._run_command(["lake", "env", "lean", "Demo.lean"], cwd=tmp_path)

    assert code == 1
    assert "timed out after 120 seconds" in output
    assert popen_kwargs["start_new_session"] is True
    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


def test_run_command_terminates_the_process_group_on_keyboard_interrupt(monkeypatch, tmp_path):
    """Reap the canonical Lean process tree before propagating an interrupt."""

    class Process:
        pid = 4323
        returncode = None
        communicate_calls = 0

        def communicate(self, timeout):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise KeyboardInterrupt
            return "", None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired(["lake", "env", "lean"], timeout)

    process = Process()
    signals = []
    monkeypatch.setattr(lean_services.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        lean_services.os,
        "killpg",
        lambda pid, sent: signals.append((pid, sent)),
    )

    with pytest.raises(KeyboardInterrupt):
        lean_services._run_command(["lake", "env", "lean", "Demo.lean"], cwd=tmp_path)

    assert signals == [(4323, signal.SIGTERM), (4323, signal.SIGKILL)]
    assert process.communicate_calls == 2


def test_research_file_check_uses_cold_start_timeout_floor(monkeypatch):
    """Do not let the canonical research gate expire before cold Lean startup."""
    command = ["lake", "env", "lean", "FormalConjectures/ErdosProblems/242.lean"]
    monkeypatch.delenv("LEANFLOW_LEAN_COMMAND_TIMEOUT_S", raising=False)
    monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)

    assert lean_command_timeout.effective_command_timeout_s(command) == 120

    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    assert lean_command_timeout.effective_command_timeout_s(command) == 900

    monkeypatch.setenv("LEANFLOW_LEAN_COMMAND_TIMEOUT_S", "240")
    assert lean_command_timeout.effective_command_timeout_s(command) == 900

    monkeypatch.setenv("LEANFLOW_LEAN_COMMAND_TIMEOUT_S", "1200")
    assert lean_command_timeout.effective_command_timeout_s(command) == 1200


def test_research_timeout_floor_applies_to_run_command(monkeypatch, tmp_path):
    """Pass the research floor to subprocess communication without weakening cleanup."""

    class Process:
        pid = 4322
        returncode = None
        communicate_calls = 0
        observed_timeout = 0

        def communicate(self, timeout):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                self.observed_timeout = timeout
                raise subprocess.TimeoutExpired(["lake", "env", "lean"], timeout)
            return "", None

        def wait(self, timeout):
            self.returncode = 1

    process = Process()
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.delenv("LEANFLOW_LEAN_COMMAND_TIMEOUT_S", raising=False)
    monkeypatch.setattr(lean_services.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(lean_services.os, "killpg", lambda *_args: None)

    code, output = lean_services._run_command(
        ["lake", "env", "lean", "FormalConjectures/ErdosProblems/242.lean"],
        cwd=tmp_path,
    )

    assert code == 1
    assert process.observed_timeout == 900
    assert "timed out after 900 seconds" in output


def test_explicit_run_command_timeout_overrides_research_floor(monkeypatch, tmp_path):
    """A bounded startup probe must not inherit the 15-minute research floor."""

    class Process:
        pid = 4324
        returncode = None
        observed_timeouts = []

        def communicate(self, timeout):
            self.observed_timeouts.append(timeout)
            raise subprocess.TimeoutExpired(["lake", "env", "lean"], timeout)

        def wait(self, timeout):
            self.returncode = 1

    process = Process()
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setattr(lean_services.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(lean_services.os, "killpg", lambda *_args: None)

    code, output = lean_services._run_command(
        ["lake", "env", "lean", "Demo.lean"],
        cwd=tmp_path,
        timeout_s=17,
    )

    assert code == 1
    assert process.observed_timeouts[0] == 17
    assert "timed out after 17.0 seconds" in output


def test_diagnostics_fallback_uses_project_admission_before_local_lean(monkeypatch, tmp_path):
    """Serialize and reclaim before the diagnostics fallback starts Lake."""
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    calls = []
    admitted = SimpleNamespace()

    monkeypatch.setattr(
        lean_services,
        "project_lean_heavy_admission",
        lambda root: calls.append(("admit", root)) or nullcontext(admitted),
    )
    monkeypatch.setattr(
        lean_services,
        "_reclaim_incremental_before_local_lean",
        lambda admission: calls.append(("reclaim", admission)) or True,
    )
    monkeypatch.setattr(
        lean_services,
        "_run_command",
        lambda command, cwd=None: calls.append(("run", command, cwd)) or (0, "checked"),
    )

    output = lean_services._diagnostics_text(target, project, {})

    assert output == "checked"
    assert calls == [
        ("admit", project),
        ("reclaim", admitted),
        ("run", ["lake", "env", "lean", "Main.lean"], project),
    ]


def test_diagnostics_fallback_does_not_start_lean_after_reclaim_failure(monkeypatch, tmp_path):
    """Fail closed when resident LeanProbe state cannot be reclaimed."""
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    admitted = SimpleNamespace()

    monkeypatch.setattr(
        lean_services,
        "project_lean_heavy_admission",
        lambda root: nullcontext(admitted),
    )
    monkeypatch.setattr(
        lean_services,
        "_reclaim_incremental_before_local_lean",
        lambda admission: False,
    )
    monkeypatch.setattr(
        lean_services,
        "_run_command",
        lambda *args, **kwargs: pytest.fail("Lean started after failed resource reclaim"),
    )

    output = lean_services._diagnostics_text(target, project, {})

    assert "could not be closed before diagnostics" in output


def test_lean_goals_reuses_known_capability_report_without_probe(monkeypatch, tmp_path):
    target = tmp_path / "Main.lean"
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        lean_services,
        "probe_capabilities",
        lambda cwd=None: pytest.fail("known capability report must bypass probing"),
    )
    monkeypatch.setattr(
        lean_services,
        "_goals_text",
        lambda file_path, project_root, mcp_tools, **kwargs: calls.append(
            (file_path, project_root, dict(mcp_tools), kwargs)
        )
        or "known goals",
    )

    goals = lean_services.lean_goals(
        str(target),
        cwd=tmp_path,
        symbol="demo",
        capability_report={
            "project_root": str(tmp_path),
            "mcp_tools": {"goals": "mcp_lean_lsp_lean_goal"},
        },
    )

    assert goals == "known goals"
    assert calls == [
        (
            target.resolve(),
            tmp_path.resolve(),
            {"goals": "mcp_lean_lsp_lean_goal"},
            {"line": None, "symbol": "demo"},
        )
    ]


def test_lean_goals_invokes_mcp_at_explicit_or_symbol_line(monkeypatch, tmp_path):
    target = tmp_path / "Main.lean"
    target.write_text(
        "\n".join(
            [
                "theorem first : True := by trivial",
                "",
                "theorem selected : True := by",
                "  trivial",
                "",
            ]
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda tool_name, arguments: calls.append((tool_name, arguments))
        or {"goals": "⊢ True", "line_context": "theorem selected"},
    )
    report = {
        "project_root": str(tmp_path),
        "mcp_tools": {"goals": "mcp_lean_lsp_lean_goal"},
    }

    by_symbol = lean_services.lean_goals(str(target), symbol="selected", capability_report=report)
    by_line = lean_services.lean_goals(
        str(target), line=4, symbol="selected", capability_report=report
    )

    assert "⊢ True" in by_symbol
    assert "theorem selected" in by_symbol
    assert "⊢ True" in by_line
    assert calls == [
        (
            "mcp_lean_lsp_lean_goal",
            {"file_path": str(target), "path": str(target), "line": 3},
        ),
        (
            "mcp_lean_lsp_lean_goal",
            {"file_path": str(target), "path": str(target), "line": 4},
        ),
    ]


def test_lean_goals_unavailable_known_report_returns_without_broad_inspection(
    monkeypatch, tmp_path
):
    target = tmp_path / "Main.lean"
    target.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(
        lean_services,
        "probe_capabilities",
        lambda cwd=None: pytest.fail("empty known report must not trigger probing"),
    )
    monkeypatch.setattr(
        lean_services,
        "_diagnostics_text",
        lambda *args, **kwargs: pytest.fail("goals lookup must not run diagnostics"),
    )
    monkeypatch.setattr(
        lean_services,
        "_count_sorries",
        lambda *args, **kwargs: pytest.fail("goals lookup must not scan file sorries"),
    )
    monkeypatch.setattr(
        lean_services,
        "_project_sorry_stats",
        lambda *args, **kwargs: pytest.fail("goals lookup must not scan project sorries"),
    )
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: pytest.fail("unavailable goals must not invoke MCP"),
    )

    goals = lean_services.lean_goals(
        str(target),
        symbol="demo",
        capability_report={"project_root": str(tmp_path), "mcp_tools": {"goals": ""}},
    )

    assert goals == "Lean goals unavailable."


def test_direct_verify_reclaims_preexisting_incremental_session(monkeypatch, tmp_path):
    """Parent verification closes owned LeanProbe state before spawning Lake."""
    project = tmp_path / "Demo"
    project.mkdir()
    (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")

    class _Probe:
        closed = False

        def close(self):
            self.closed = True

    probe = _Probe()
    commands = []
    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(
        lean_services,
        "_run_command",
        lambda command, cwd=None: (commands.append(command) or 0, "ok"),
    )

    result = lean_services.lean_verify(cwd=project)

    assert result.ok is True
    assert probe.closed is True
    assert commands == [["lake", "build"]]


def test_direct_verify_does_not_spawn_after_incremental_close_failure(monkeypatch, tmp_path):
    """Retain admission instead of overlapping Lake with an unclosed LeanProbe."""
    project = tmp_path / "Demo"
    project.mkdir()
    (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")

    class _Probe:
        def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.setattr(lean_incremental, "_PROBE", _Probe())
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    monkeypatch.setattr(
        lean_services,
        "_run_command",
        lambda *args, **kwargs: pytest.fail("Lake spawned after failed LeanProbe close"),
    )

    result = lean_services.lean_verify(cwd=project)
    retry = lean_services.lean_verify(cwd=project)

    assert result.ok is False
    assert "admission retained" in result.output.lower()
    assert retry.ok is False
    assert "admission retained" in retry.output.lower()


def test_file_exact_verify_uses_explicit_targets_nested_project(monkeypatch, tmp_path):
    """Keep file-exact checks scoped to a nested target when caller cwd is foreign."""
    project = tmp_path / "Nested"
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
    target.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    foreign_cwd = tmp_path / "Harness"
    foreign_cwd.mkdir()
    commands = []
    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (None, "missing"))
    monkeypatch.setattr(
        lean_services,
        "_run_command",
        lambda command, cwd=None: (commands.append((command, cwd)) or 0, "ok"),
    )

    result = lean_services.lean_verify(
        target=str(target),
        cwd=foreign_cwd,
        mode="file_exact",
    )

    assert result.ok is True
    assert result.mode == "file_exact"
    assert result.command == "lake env lean Demo/Main.lean"
    assert commands == [(["lake", "env", "lean", "Demo/Main.lean"], project)]


def test_direct_axiom_harness_reclaims_preexisting_incremental_session(monkeypatch, tmp_path):
    """A parent axiom check cannot overlap its own retained LeanProbe child."""
    project = tmp_path / "Demo"
    project.mkdir()
    (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")

    class _Probe:
        closed = False

        def close(self):
            self.closed = True

    probe = _Probe()
    commands = []
    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.setattr(lean_incremental, "_PROBE", probe)
    monkeypatch.setattr(
        lean_services,
        "_run_command",
        lambda command, cwd=None: (commands.append(command) or 0, "ok"),
    )

    code, _output = lean_services._run_axiom_harness(
        project, "theorem demo : True := by trivial\n#print axioms demo\n"
    )

    assert code == 0
    assert probe.closed is True
    assert commands and commands[0][:3] == ["lake", "env", "lean"]


def test_direct_axiom_harness_refuses_after_incremental_close_failure(monkeypatch, tmp_path):
    """Return non-success promptly after an axiom gate becomes sticky."""
    project = tmp_path / "Demo"
    project.mkdir()
    (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")

    class _Probe:
        def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
    monkeypatch.setattr(lean_incremental, "_PROBE", _Probe())
    monkeypatch.setattr(
        lean_services,
        "_run_command",
        lambda *args, **kwargs: pytest.fail("Lean spawned after failed LeanProbe close"),
    )
    harness = "theorem demo : True := by trivial\n#print axioms demo\n"

    first_code, first_output = lean_services._run_axiom_harness(project, harness)
    retry_code, retry_output = lean_services._run_axiom_harness(project, harness)

    assert first_code == retry_code == 1
    assert "admission retained" in first_output.lower()
    assert "admission retained" in retry_output.lower()


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
        "tools.mcp.mcp_tool.get_mcp_status",
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
        lambda root, query, *, limit=10: [
            {"file": "Demo/Main.lean", "line": 12, "preview": "theorem map_id"}
        ],
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
                '], "count": 1}'
            )
        }

    monkeypatch.setattr(lean_services, "_invoke_json_tool", _fake_invoke)
    monkeypatch.setattr(lean_services, "_rg_search", lambda root, query, *, limit=10: [])

    result = lean_services.lean_search(
        "prime number divisibility", cwd=project, mode="semantic", limit=3
    )

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
    monkeypatch.setenv("LEANFLOW_LEANEXPLORE_RERANK_TOP", "50")
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_local_status",
        lambda: {
            "package_available": True,
            "data_ready": True,
            "cache_path": "/tmp/cache",
            "available": True,
        },
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


def test_leanexplore_local_search_quarantines_corrupt_db(monkeypatch):
    # A corrupt SQLite index must yield a concise message + quarantine, NOT a raw
    # SQLAlchemy dump (the 450-parameter statement that polluted the run log).
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_local_status",
        lambda: {
            "package_available": True,
            "data_ready": True,
            "cache_path": "/tmp/cache",
            "available": True,
        },
    )
    monkeypatch.setattr(lean_services, "_LEANEXPLORE_LOCAL_SERVICE", None)
    monkeypatch.setattr(lean_services, "_LEANEXPLORE_LOCAL_RERANK_DISABLED", False)

    class FakeService:
        async def search(self, *, query, limit, rerank_top):
            raise RuntimeError(
                "(sqlite3.DatabaseError) database disk image is malformed "
                "[SQL: SELECT declarations.id ...] [parameters: (1, 2, 3, ...)]"
            )

    fake_package = types.ModuleType("lean_explore")
    fake_search = types.ModuleType("lean_explore.search")
    fake_search.Service = FakeService
    monkeypatch.setitem(sys.modules, "lean_explore", fake_package)
    monkeypatch.setitem(sys.modules, "lean_explore.search", fake_search)

    quarantined: list[bool] = []
    monkeypatch.setattr(
        lean_services,
        "_quarantine_corrupt_leanexplore_db",
        lambda: quarantined.append(True) or "/tmp/cache/lean_explore.db.corrupt",
    )

    results, error = lean_services._leanexplore_local_search("IsRelPrime 2 (2 ^ k)", limit=10)

    assert results == []
    assert quarantined == [True]
    assert "quarantined" in error
    assert "lean-explore data fetch" in error
    # The raw SQL / bound parameters must NOT leak into the surfaced message.
    assert "SELECT" not in error
    assert "parameters" not in error


def test_leanexplore_local_search_reuses_service_and_suppresses_noise(monkeypatch, capsys):
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_local_status",
        lambda: {
            "package_available": True,
            "data_ready": True,
            "cache_path": "/tmp/cache",
            "available": True,
        },
    )
    monkeypatch.setattr(lean_services, "_LEANEXPLORE_LOCAL_SERVICE", None)
    monkeypatch.setattr(lean_services, "_LEANEXPLORE_LOCAL_RERANK_DISABLED", False)
    monkeypatch.delenv("LEANFLOW_LEANEXPLORE_VERBOSE", raising=False)
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

    first_results, first_error = lean_services._leanexplore_local_search(
        "Nat.mod_eq_of_lt", limit=1
    )
    second_results, second_error = lean_services._leanexplore_local_search(
        "Nat.mod_eq_of_lt", limit=1
    )
    captured = capsys.readouterr()

    assert first_error == ""
    assert second_error == ""
    assert first_results == second_results
    assert constructed == 1
    assert calls == [0, 0]
    assert "BM25S noisy progress" not in captured.out
    assert "torch cuda warning" not in captured.err


def test_leanexplore_local_reranker_is_opt_in_and_bounded(monkeypatch):
    monkeypatch.delenv("LEANFLOW_LEANEXPLORE_RERANK_TOP", raising=False)
    assert lean_search_providers._leanexplore_local_rerank_top() == 0

    monkeypatch.setenv("LEANFLOW_LEANEXPLORE_RERANK_TOP", "12")
    assert lean_search_providers._leanexplore_local_rerank_top() == 12

    monkeypatch.setenv("LEANFLOW_LEANEXPLORE_RERANK_TOP", "999")
    assert lean_search_providers._leanexplore_local_rerank_top() == 50

    monkeypatch.setenv("LEANFLOW_LEANEXPLORE_RERANK_TOP", "not-an-int")
    assert lean_search_providers._leanexplore_local_rerank_top() == 0


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
    monkeypatch.setattr("tools.mcp.mcp_tool.get_mcp_status", lambda: [])
    monkeypatch.setattr(
        "leanflow_cli.cli.mcp_bootstrap.managed_mcp_power_status",
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
    monkeypatch.setattr("tools.mcp.mcp_tool.get_mcp_status", lambda: [])
    monkeypatch.setattr(
        "leanflow_cli.cli.mcp_bootstrap.managed_mcp_power_status",
        lambda project_root=None: {},
    )

    report = lean_services.probe_capabilities(project)

    assert report.search_providers[0] == "leanexplore-local"
    assert report.power_modes["leanexplore_local_available"] is True
    assert report.power_modes["leanexplore_local_cache_path"] == str(cache)


def test_lean_search_uses_direct_leanexplore_api_before_mcp_semantic_provider(
    monkeypatch, tmp_path
):
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
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("MCP semantic provider should not be called")
        ),
    )
    monkeypatch.setattr(lean_services, "_rg_search", lambda root, query, *, limit=10: [])

    result = lean_services.lean_search(
        "prime number divisibility", cwd=project, mode="semantic", limit=3
    )

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
            search_providers=[
                "leanexplore-local",
                "leanexplore-api",
                "mcp-leanexplore",
                "mcp-leanfinder",
            ],
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
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("API fallback should not be called")
        ),
    )
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("MCP semantic provider should not be called")
        ),
    )
    monkeypatch.setattr(lean_services, "_rg_search", lambda root, query, *, limit=10: [])

    result = lean_services.lean_search(
        "prime number divisibility", cwd=project, mode="semantic", limit=3
    )

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

    result = lean_services.lean_search(
        "isUnit_gcd_of_eq_mul_gcd", cwd=project, mode="local", limit=3
    )

    assert result.attempted_providers == ["project-rg", "leanexplore-local"]
    assert result.results[0]["provider"] == "leanexplore-local"
    assert result.results[0]["name"] == "isUnit_gcd_of_eq_mul_gcd"


def test_lean_search_local_mode_prefers_exact_project_match(monkeypatch, tmp_path):
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
            mcp_tools={"local_search": "mcp_lean_lsp_lean_local_search"},
            search_providers=["mcp-local-search", "project-rg", "leanexplore-local"],
            helper_tools={"search_fallback": True},
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(lean_services, "_invoke_json_tool", lambda *args, **kwargs: {"results": []})
    monkeypatch.setattr(
        lean_services,
        "_rg_search",
        lambda root, query, *, limit=10: [
            {
                "file": str(project / "Main.lean"),
                "line": 12,
                "preview": "private lemma exact_project_helper : True := by",
            }
        ],
    )
    monkeypatch.setattr(
        lean_services,
        "_leanexplore_local_search",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("LeanExplore must not suppress an exact project match")
        ),
    )

    result = lean_services.lean_search("exact_project_helper", cwd=project, mode="local", limit=3)

    assert result.attempted_providers == ["mcp-local-search", "project-rg"]
    assert result.results == [
        {
            "provider": "project-rg",
            "file": str(project / "Main.lean"),
            "line": 12,
            "preview": "private lemma exact_project_helper : True := by",
        }
    ]


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
            "'Demo.demo' depends on axioms: [Classical.choice, My.customAxiom, Quot.sound]",
        ),
    )

    report = lean_services.lean_axioms("demo", cwd=project, file_path=str(target))

    assert report.choice is True
    assert report.custom_axioms == ["My.customAxiom"]
    assert "Classical.choice" in report.axioms
    assert report.ok is False


def test_lean_axioms_does_not_parse_command_failure_as_custom_axiom(monkeypatch, tmp_path):
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
            1,
            "/tmp/tmpy21js89i.lean:1:39: error: unexpected token; expected identifier",
        ),
    )

    report = lean_services.lean_axioms("demo", cwd=project, file_path=str(target))

    assert report.ok is False
    assert report.axioms == []
    assert report.custom_axioms == []
    assert report.inspection_succeeded is False
    assert "unexpected token" in report.note


def test_lean_axioms_harness_stays_outside_project_and_is_removed(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    observed: dict[str, object] = {}

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))

    def fake_run(cmd, cwd=None):
        harness = Path(cmd[-1]).resolve()
        observed["path"] = harness
        observed["exists_during_check"] = harness.is_file()
        return 0, "'demo' depends on axioms: []"

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    report = lean_services.lean_axioms("demo", cwd=project, file_path=str(target))

    harness = observed["path"]
    assert isinstance(harness, Path)
    assert observed["exists_during_check"] is True
    assert project.resolve() not in harness.parents
    assert not harness.exists()
    assert report.ok is True


def _axiom_batch_output(harness: str) -> str:
    """Return distinct synthetic profiles for every marked axiom query."""
    lines = harness.splitlines()
    output: list[str] = []
    for index, line in enumerate(lines):
        marker_match = re.search(r'"(LEANFLOW_AXIOMS_BEGIN_[A-Fa-f0-9]+)"', line)
        if marker_match is None:
            continue
        target = next(
            candidate.removeprefix("#print axioms ").strip()
            for candidate in lines[index + 1 :]
            if candidate.startswith("#print axioms ")
        )
        end_marker = next(
            re.search(r'"(LEANFLOW_AXIOMS_END_[A-Fa-f0-9]+)"', candidate).group(1)
            for candidate in lines[index + 1 :]
            if re.search(r'"(LEANFLOW_AXIOMS_END_[A-Fa-f0-9]+)"', candidate)
        )
        axioms = "[propext, sorryAx]" if target == "first" else "[Quot.sound]"
        output.extend(
            [
                marker_match.group(1),
                f"'{target}' depends on axioms: {axioms}",
                end_marker,
            ]
        )
    return "\n".join(output)


def test_lean_axioms_reuses_one_exact_batch_for_distinct_targets(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "theorem first : True := by trivial\n\ntheorem second : True := by trivial\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    lean_services._clear_axiom_batch_cache_for_tests()

    def fake_run(cmd, cwd=None):
        harness = Path(cmd[-1]).read_text(encoding="utf-8")
        calls.append(harness)
        return 0, _axiom_batch_output(harness)

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    first = lean_services.lean_axioms("first", cwd=project, file_path=str(target))
    second = lean_services.lean_axioms("second", cwd=project, file_path=str(target))

    assert len(calls) == 1
    assert calls[0].count("#print axioms") == 2
    assert first.axioms == ["propext", "sorryAx"]
    assert first.custom_axioms == ["sorryAx"]
    assert first.ok is False
    assert second.axioms == ["Quot.sound"]
    assert second.custom_axioms == []
    assert second.ok is True


def test_lean_axioms_can_skip_sibling_prefetch_for_exact_gate(monkeypatch, tmp_path):
    """An exact manager gate emits only its requested axiom query."""
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "theorem first : True := by trivial\n\ntheorem second : True := by trivial\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    lean_services._clear_axiom_batch_cache_for_tests()

    def fake_run(cmd, cwd=None):
        harness = Path(cmd[-1]).read_text(encoding="utf-8")
        calls.append(harness)
        return 0, _axiom_batch_output(harness)

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    report = lean_services.lean_axioms(
        "first",
        cwd=project,
        file_path=str(target),
        prefetch_siblings=False,
    )

    assert len(calls) == 1
    assert calls[0].count("#print axioms") == 1
    assert "#print axioms first" in calls[0]
    assert "#print axioms second" not in calls[0]
    assert report.inspection_succeeded is True
    assert report.axioms == ["propext", "sorryAx"]
    assert report.ok is False


def test_lean_axioms_exact_gate_does_not_repeat_failed_cold_check(monkeypatch, tmp_path):
    """Do not run the same single-target axiom compile twice after a timeout."""
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text("theorem first : True := by trivial\n", encoding="utf-8")
    calls: list[str] = []

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    lean_services._clear_axiom_batch_cache_for_tests()

    def fake_run(cmd, cwd=None):
        calls.append(Path(cmd[-1]).read_text(encoding="utf-8"))
        return 1, "Command timed out after 900 seconds"

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    report = lean_services.lean_axioms(
        "first",
        cwd=project,
        file_path=str(target),
        prefetch_siblings=False,
    )

    assert len(calls) == 1
    assert calls[0].count("#print axioms") == 1
    assert report.inspection_succeeded is False
    assert "timed out" in report.note


def test_lean_axioms_many_returns_exact_profiles_from_one_harness(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "theorem first : True := by trivial\n\ntheorem second : True := by trivial\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    lean_services._clear_axiom_batch_cache_for_tests()

    def fake_run(cmd, cwd=None):
        harness = Path(cmd[-1]).read_text(encoding="utf-8")
        calls.append(harness)
        return 0, _axiom_batch_output(harness)

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    reports = lean_services.lean_axioms_many(
        ["first", "second"], cwd=project, file_path=str(target)
    )

    assert len(calls) == 1
    assert calls[0].count("#print axioms") == 2
    assert reports["first"].inspection_succeeded is True
    assert reports["first"].axioms == ["propext", "sorryAx"]
    assert reports["second"].inspection_succeeded is True
    assert reports["second"].axioms == ["Quot.sound"]


def test_lean_axioms_many_rejects_ambiguous_batch_without_single_fallback(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "theorem first : True := by trivial\n\ntheorem second : True := by trivial\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    lean_services._clear_axiom_batch_cache_for_tests()

    def fake_run(cmd, cwd=None):
        harness = Path(cmd[-1]).read_text(encoding="utf-8")
        calls.append(harness)
        output = _axiom_batch_output(harness)
        begin = re.search(r"LEANFLOW_AXIOMS_BEGIN_[A-Fa-f0-9]+", output)
        assert begin is not None
        return 0, f"{output}\n{begin.group(0)}"

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    reports = lean_services.lean_axioms_many(
        ["first", "second"], cwd=project, file_path=str(target)
    )

    assert len(calls) == 1
    assert all(report.inspection_succeeded is False for report in reports.values())
    assert all(report.ok is False for report in reports.values())
    assert all("incomplete, ambiguous" in report.note for report in reports.values())


def test_lean_axioms_batch_cache_invalidates_on_source_change(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    source = "theorem first : True := by trivial\n\ntheorem second : True := by trivial\n"
    target.write_text(source, encoding="utf-8")
    calls = 0

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    lean_services._clear_axiom_batch_cache_for_tests()

    def fake_run(cmd, cwd=None):
        nonlocal calls
        calls += 1
        return 0, _axiom_batch_output(Path(cmd[-1]).read_text(encoding="utf-8"))

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    lean_services.lean_axioms("first", cwd=project, file_path=str(target))
    target.write_text(source + "\n", encoding="utf-8")
    lean_services.lean_axioms("second", cwd=project, file_path=str(target))

    assert calls == 2


def test_lean_axioms_batch_cache_invalidates_on_import_environment_change(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "theorem first : True := by trivial\n\ntheorem second : True := by trivial\n",
        encoding="utf-8",
    )
    manifest = project / "lake-manifest.json"
    manifest.write_text('{"version": 1}\n', encoding="utf-8")
    calls = 0

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    lean_services._clear_axiom_batch_cache_for_tests()

    def fake_run(cmd, cwd=None):
        nonlocal calls
        calls += 1
        return 0, _axiom_batch_output(Path(cmd[-1]).read_text(encoding="utf-8"))

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    lean_services.lean_axioms("first", cwd=project, file_path=str(target))
    manifest.write_text('{"version": 2}\n', encoding="utf-8")
    lean_services.lean_axioms("second", cwd=project, file_path=str(target))

    assert calls == 2


def test_lean_axioms_batch_cache_invalidates_on_compiled_import_change(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "theorem first : True := by trivial\n\ntheorem second : True := by trivial\n",
        encoding="utf-8",
    )
    compiled_import = project / ".lake" / "build" / "lib" / "lean" / "Demo" / "Dep.olean"
    compiled_import.parent.mkdir(parents=True)
    compiled_import.write_bytes(b"first compiled revision")
    calls = 0

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    lean_services._clear_axiom_batch_cache_for_tests()

    def fake_run(cmd, cwd=None):
        nonlocal calls
        calls += 1
        return 0, _axiom_batch_output(Path(cmd[-1]).read_text(encoding="utf-8"))

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    lean_services.lean_axioms("first", cwd=project, file_path=str(target))
    compiled_import.write_bytes(b"second, different compiled revision")
    lean_services.lean_axioms("second", cwd=project, file_path=str(target))

    assert calls == 2


def test_lean_axioms_batch_failure_falls_back_to_exact_single_target(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "theorem first : True := by trivial\n\ntheorem second : True := by trivial\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    monkeypatch.setattr(lean_services, "_project_root", lambda cwd=None: (project, ""))
    lean_services._clear_axiom_batch_cache_for_tests()

    def fake_run(cmd, cwd=None):
        harness = Path(cmd[-1]).read_text(encoding="utf-8")
        calls.append(harness)
        if harness.count("#print axioms") > 1:
            return 1, "an opportunistic sibling query failed"
        return 0, "'first' depends on axioms: [Quot.sound]"

    monkeypatch.setattr(lean_services, "_run_command", fake_run)

    report = lean_services.lean_axioms("first", cwd=project, file_path=str(target))

    assert len(calls) == 2
    assert calls[0].count("#print axioms") == 2
    assert calls[1].count("#print axioms") == 1
    assert report.inspection_succeeded is True
    assert report.axioms == ["Quot.sound"]
    assert report.ok is True


def test_axiom_batch_harness_keeps_queries_in_declaration_scope(tmp_path):
    target = tmp_path / "Main.lean"
    source = (
        "namespace Demo\n\n"
        "private lemma helper : True := by trivial\n\n"
        "/-- Main theorem docs. -/\n"
        "@[simp]\n"
        "theorem verified : True := by trivial\n\n"
        "end Demo\n"
    )
    target.write_text(source, encoding="utf-8")

    plan = lean_axiom_batch.build_axiom_batch_plan(
        source,
        lean_services._declaration_index(target),
        "helper",
    )

    assert plan is not None
    harness = plan.source
    helper_query = "#print axioms helper"
    verified_query = "#print axioms verified"
    assert harness.index("private lemma helper") < harness.index(helper_query)
    assert harness.index(helper_query) < harness.index("/-- Main theorem docs. -/")
    assert harness.index("theorem verified") < harness.index(verified_query)
    assert harness.index(verified_query) < harness.index("end Demo")


def test_module_name_for_numeric_file_component_uses_lean_quoted_identifier(tmp_path):
    target = tmp_path / "FormalConjectures" / "ErdosProblems" / "242.lean"

    assert (
        lean_services._module_name_for_file(tmp_path, target)
        == "FormalConjectures.ErdosProblems.\u00ab242\u00bb"
    )


def test_axiom_harness_queries_last_declaration_before_namespace_end(tmp_path):
    target = tmp_path / "Main.lean"
    target.write_text(
        "namespace Demo\n\ntheorem verified : True := by trivial\n\nend Demo\n",
        encoding="utf-8",
    )

    harness = lean_services._axiom_harness_source(target, "verified")

    assert harness.index("theorem verified") < harness.index("#print axioms verified")
    assert harness.index("#print axioms verified") < harness.index("end Demo")


def test_axiom_harness_queries_before_next_declaration_metadata(tmp_path):
    target = tmp_path / "Main.lean"
    target.write_text(
        "namespace Demo\n\n"
        "private lemma helper : True := by trivial\n\n"
        "/-- Main theorem docs. -/\n"
        "@[simp]\n"
        "theorem verified : True := by trivial\n\n"
        "end Demo\n",
        encoding="utf-8",
    )

    harness = lean_services._axiom_harness_source(target, "helper")

    assert harness.index("private lemma helper") < harness.index("#print axioms helper")
    assert harness.index("#print axioms helper") < harness.index("/-- Main theorem docs. -/")
    assert harness.index("/-- Main theorem docs. -/") < harness.index("@[simp]")
    assert harness.index("@[simp]") < harness.index("theorem verified")


def test_lean_search_marks_repeated_empty_search_loop(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
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
    outcomes = project / ".leanflow-outcomes.jsonl"
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

    assert (
        "repeated empty search loop detected; stop searching and change tactic"
        in result.degraded_reasons
    )


def test_recent_empty_search_streak_streams_large_outcome_history(monkeypatch, tmp_path):
    outcomes = tmp_path / "outcomes.jsonl"
    unrelated = (
        '{"kind":"lean-search","workflow_command":"/prove Other.lean",'
        '"payload":{"results":[]}}\n'
    )
    outcomes.write_text(
        unrelated * 20_000 + '{"kind":"lean-search","workflow_command":"/prove Demo/Main.lean",'
        '"payload":{"results":[]}}\n'
        + '{"kind":"manager-route","workflow_command":"/prove Demo/Main.lean"}\n'
        + '{"kind":"lean-search","workflow_command":"/prove Demo/Main.lean",'
        '"payload":{"results":[]}}\n'
        + '{"kind":"lean-search","workflow_command":"/prove Demo/Main.lean",'
        '"payload":{"results":[]}}\n'
        + '{"kind":"manager-route","workflow_command":"/prove Demo/Main.lean"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(lean_services, "workflow_outcomes_path", lambda: outcomes)
    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if path == outcomes:
            raise AssertionError("outcome history must be streamed")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert (
        lean_services.recent_empty_search_streak(workflow_command="/prove Demo/Main.lean", limit=6)
        == 2
    )


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
    monkeypatch.setattr(
        lean_services, "_project_sorry_stats", lambda project_root: (1, ["Main.lean"])
    )
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args, **kwargs: None)

    inspection = lean_services.lean_inspect(str(target), cwd=project)

    assert inspection.queue_items[0]["label"] == "broken"
    assert "diagnostic near line 2" in inspection.queue_items[0]["reasons"]
    assert inspection.queue_items[1]["label"] == "later"


def test_lean_inspect_queue_ignores_info_and_leaves_style_warning_for_final_sweep(
    monkeypatch, tmp_path
):
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


def test_lean_inspect_exact_sorry_uses_queue_evidence_when_current_goals_are_empty(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")
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
        lambda file_path, project_root, mcp_tools: '{"items": []}',
    )
    monkeypatch.setattr(
        lean_services,
        "_goals_text",
        lambda *args, **kwargs: (
            '{"line_context": "theorem target : True :=", "goals": null, '
            '"goals_before": [], "goals_after": []}'
        ),
    )
    monkeypatch.setattr(
        lean_services, "_project_sorry_stats", lambda project_root: (1, ["Main.lean"])
    )
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args, **kwargs: None)

    inspection = lean_services.lean_inspect(str(target), cwd=project, symbol="target")

    assert inspection.blocker_kind == "sorry"
    assert inspection.queue_items[0]["label"] == "target"
    assert inspection.queue_items[0]["reasons"] == ["contains sorry"]


def test_route_workflow_step_marks_search_exhausted_from_recent_empty_search_streak(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    project.mkdir()
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
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
            workers=[],
            degraded_reasons=[],
        ),
    )
    monkeypatch.setattr(
        lean_services, "recent_empty_search_streak", lambda workflow_command, limit=6: 3
    )

    decision = lean_services.route_workflow_step(
        "prove",
        {
            "active_file": str(project / "Demo" / "Main.lean"),
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
            "current_blocker": "unsolved goals from a prior rejected attempt",
            "diagnostics": "warning: declaration uses sorry",
            "goals": "Lean goals unavailable.",
            "build_status": "unknown",
        },
        configured_skill="lean-theorem-queue-worker",
        autonomy_state={},
        cwd=project,
    )

    assert decision.search_exhausted is True
    assert decision.blocker_kind == "sorry"
    assert decision.recommended_worker == ""
    assert decision.route_action == "queue-worker"


def test_discover_lean_mcp_tools_prefers_raw_managed_tools_over_native_wrappers(monkeypatch):
    monkeypatch.setattr("tools.mcp.mcp_tool.discover_mcp_tools", lambda: None)
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
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-demo")
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
        "tools.mcp.mcp_tool.get_mcp_status",
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
    lean_services.lean_auto_search(
        "Demo/Main.lean", "demo", cwd=project, timeout_s=42, objective="balanced"
    )

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


def test_auto_search_failed_outcome_is_not_reported_as_success(monkeypatch, tmp_path):
    """Preserve a completed backend call without converting search failure to success."""
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
        binaries={"lean": True, "lake": True},
        mcp_tools={"auto_search": "mcp_lean_proof_auto_search_automated_proof"},
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    outcomes: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        lean_services,
        "append_workflow_outcome",
        lambda kind, payload: outcomes.append((kind, dict(payload))),
    )
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *_args, **_kwargs: {
            "result": {
                "status": "fail",
                "outcome": "failed",
                "attempts": 0,
                "explored_sets": 0,
            }
        },
    )

    payload = lean_services.lean_auto_search("Demo/Main.lean", "demo", cwd=project, timeout_s=20)

    assert payload["success"] is False
    assert payload["status"] == "fail"
    assert payload["outcome"] == "failed"
    assert outcomes[-1][0] == "lean-auto-search"
    assert outcomes[-1][1]["success"] is False


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
    monkeypatch.setattr(
        lean_services, "_invoke_json_tool", lambda *args, **kwargs: calls.append(args) or {}
    )
    outcomes = []
    monkeypatch.setattr(
        lean_services, "append_workflow_outcome", lambda *args: outcomes.append(args)
    )

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
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "auto-try-harness-failure")
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
    monkeypatch.setattr(
        lean_services, "append_workflow_outcome", lambda *args: outcomes.append(args)
    )

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
    monkeypatch.setattr(
        lean_services,
        "_discover_internal_managed_mcp_tool",
        lambda capability: "mcp_lean_proof_auto_scan_theorem",
    )
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
                        "location": {
                            "decl_start": 4,
                            "decl_end": 5,
                            "proof_start": 6,
                            "proof_end": 6,
                        },
                    },
                }
            }
        return {
            "result": {
                "status": "success",
                "theorem_statement": "lemma abs_add_diff ...",
                "original_proof": "rfl",
            }
        }

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


def test_lean_proof_context_enriches_backend_that_drops_private_lemma_binders(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n".join(
            [
                "private lemma residual_case (t : ℕ) (ht : t % 5 = 1) :",
                "    ∃ x : ℕ, x = 168 * t + 121 := by",
                "  sorry",
                "",
                "private lemma later_case : True := by",
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
        mcp_tools={"proof_context": "mcp_lean_proof_auto_get_proof_context"},
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda _name: "")
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *_args, **_kwargs: {
            "result": {
                "status": "success",
                # lean-proof-auto v0.4.0 exposes declaration.type here but currently
                # leaves its initial-proof-state hypothesis extraction unimplemented.
                "theorem_statement": "∃ x : ℕ, x = 168 * t + 121",
                "original_proof": "by sorry",
                "hypotheses": [],
                "in_scope": ["residual_case", "later_case", "Imported.safe"],
                "metadata": {"api_version": "0.4.0"},
            }
        },
    )

    payload = lean_services.lean_proof_context("Demo/Main.lean", "residual_case", cwd=project)

    assert payload["success"] is True
    assert payload["backend_tool"] == "mcp_lean_proof_auto_get_proof_context"
    assert payload["theorem_statement"] == (
        "private lemma residual_case (t : ℕ) (ht : t % 5 = 1) :\n" "    ∃ x : ℕ, x = 168 * t + 121"
    )
    assert payload["hypotheses"] == ["t : ℕ", "ht : t % 5 = 1"]
    assert payload["in_scope"] == ["Imported.safe"]
    assert payload["metadata"]["api_version"] == "0.4.0"
    assert payload["metadata"]["source_order_filter"] == {
        "removed_same_file_names": 2,
        "reason": "target and later same-file declarations are unavailable",
    }
    assert payload["metadata"]["local_context_enrichment"] == {
        "theorem_statement": True,
        "hypotheses": True,
        "reason": "backend omitted explicit declaration binders",
    }


def test_proof_context_binder_enrichment_defers_all_empty_backend_to_local_fallback():
    backend_payload = {
        "theorem_statement": "",
        "original_proof": "",
        "hypotheses": [],
        "metadata": {},
    }
    local_payload = {
        "theorem_statement": "lemma demo (t : ℕ) : True",
        "original_proof": "sorry",
        "hypotheses": ["t : ℕ"],
    }

    enriched = lean_services._enrich_backend_proof_context(backend_payload, local_payload)

    assert enriched == backend_payload
    assert enriched["hypotheses"] == []


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


def test_lean_proof_context_uses_local_slice_when_backend_is_unavailable(monkeypatch, tmp_path):
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
        mcp_tools={"proof_context": ""},
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=["lean proof context MCP disabled for current run"],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(lean_services, "_discover_internal_managed_mcp_tool", lambda _name: "")
    outcomes = []
    monkeypatch.setattr(
        lean_services, "append_workflow_outcome", lambda *args: outcomes.append(args)
    )

    payload = lean_services.lean_proof_context("Demo/Main.lean", "demo", cwd=project)

    assert payload["success"] is True
    assert payload["status"] == "local-fallback"
    assert payload["backend_tool"] == "local-declaration-slice"
    assert payload["theorem_statement"] == "theorem demo : True"
    assert payload["original_proof"] == "trivial"
    assert any("MCP is unavailable" in reason for reason in payload["degraded_reasons"])
    assert outcomes[-1][1]["backend_tool"] == "local-declaration-slice"


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
            {
                "name": "next_demo",
                "kind": "theorem",
                "line": 5,
                "end_line": 6,
                "text": "theorem next_demo : True := by\n  trivial",
            },
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


def test_local_proof_context_extracts_balanced_explicit_binders(tmp_path):
    target = tmp_path / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n".join(
            [
                "private lemma demo {α : Type} [Group α]",
                "    (f : α → (α × α)) (x y : α) (h : f x = (y, y)) : True := by",
                "  sorry",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    payload = lean_services._local_proof_context_payload(
        target,
        "demo",
        degraded_reasons=["proof context MCP unavailable"],
    )

    assert payload is not None
    assert payload["hypotheses"] == [
        "α : Type",
        "Group α",
        "f : α → (α × α)",
        "x y : α",
        "h : f x = (y, y)",
    ]


def test_declaration_index_recognizes_noncomputable_def_boundaries(tmp_path):
    target = tmp_path / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n".join(
            [
                "import Mathlib",
                "",
                "noncomputable def qRationalNum : Nat := by",
                "  sorry",
                "",
                "/-- Source proof: trivial. -/",
                "theorem qRationalTheorem : True := by",
                "  trivial",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    entries = lean_services._declaration_index(target)

    assert [entry["name"] for entry in entries] == ["qRationalNum", "qRationalTheorem"]
    assert entries[0]["kind"] == "def"
    assert entries[0]["line"] == 3
    assert entries[0]["end_line"] == 4
    assert entries[1]["kind"] == "theorem"
    assert entries[1]["line"] == 7


def test_lean_proof_context_falls_back_to_local_slice_without_disabling_proof_auto_backend(
    monkeypatch, tmp_path
):
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
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "run-proof-auto-fallback")
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
        "tools.mcp.mcp_tool.get_mcp_status",
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
        "_discover_internal_managed_mcp_tool",
        lambda capability: "mcp_lean_proof_auto_scan_theorem",
    )

    def _fake_invoke(tool_name, arguments):
        if tool_name == "mcp_lean_proof_auto_scan_theorem":
            return {
                "result": {
                    "status": "success",
                    "theorem": {
                        "name": "abs_add_diff",
                        "kind": "lemma",
                        "location": {
                            "decl_start": 4,
                            "decl_end": 5,
                            "proof_start": 6,
                            "proof_end": 6,
                        },
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
    assert payload["in_scope"] == ["first"]
    assert "next_demo" not in payload["in_scope"]
    assert any(
        "Theorem not found: abs_add_diff" in reason for reason in payload["degraded_reasons"]
    )
    assert any(
        "without disabling proof-auto MCP" in reason for reason in payload["degraded_reasons"]
    )
    assert not any(
        "proof-auto backend disabled for current run" in reason
        for reason in payload["degraded_reasons"]
    )
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

    probe_calls = [
        arguments for tool_name, arguments in calls if tool_name == "mcp_lean_proof_auto_probe"
    ]
    assert [entry["mode"] for entry in probe_calls] == ["aesop", "grind"]
    assert all(entry["file"] == str(target.resolve()) for entry in probe_calls)
    assert all(entry["theorem_id"] == "demo" for entry in probe_calls)
    assert all(entry["budget_s"] == 30.0 for entry in probe_calls)
    assert probe_payload["recommended_mode"] == "aesop"

    multi_attempt_args = [
        arguments
        for tool_name, arguments in calls
        if tool_name == "mcp_lean_lsp_lean_multi_attempt"
    ][0]
    assert multi_attempt_args["file_path"] == str(target.resolve())
    assert multi_attempt_args["line"] == 12
    assert multi_attempt_args["column"] == 4
    assert multi_attempt_args["snippets"] == ["simp", "ring"]
    assert "attempts" not in multi_attempt_args


def test_lean_multi_attempt_resolves_blank_immediately_after_tactic_proof(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "theorem target : True := by\n  sorry\n\ntheorem next_target : True := by\n  trivial\n",
        encoding="utf-8",
    )
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={"multi_attempt": "mcp_lean_lsp_lean_multi_attempt"},
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    calls: list[dict[str, object]] = []

    def fake_invoke(_tool_name, arguments):
        calls.append(dict(arguments))
        return {"result": {"success": True, "results": []}}

    monkeypatch.setattr(lean_services, "_invoke_json_tool", fake_invoke)
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args: None)

    payload = lean_services.lean_multi_attempt(
        "Main.lean", 3, ["simp", "exact True.intro"], cwd=project, column=11
    )

    assert calls[0]["line"] == 2
    assert calls[0]["column"] == 3
    assert payload["line"] == 2
    assert payload["column"] == 3
    assert payload["requested_line"] == 3
    assert payload["requested_column"] == 11
    assert payload["line_adjustment"] == "trailing_placeholder"


def test_lean_multi_attempt_resolves_inline_sorry_tactic_column(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text(
        "\n" * 1080 + "private lemma erdos_242_family_one_ordering (s : ℕ) (hs : 1 ≤ s) :\n"
        "    1 ≤ 210 * s + 1 ∧ 210 * s + 1 < 840 * s * (210 * s + 1) ∧\n"
        "      840 * s * (210 * s + 1) < 840 * s * (210 * s + 1) + "
        "(210 * s + 1) := by sorry\n"
        "private lemma next_target : True := by trivial\n",
        encoding="utf-8",
    )
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={"multi_attempt": "mcp_lean_lsp_lean_multi_attempt"},
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    calls: list[dict[str, object]] = []

    def fake_invoke(_tool_name, arguments):
        calls.append(dict(arguments))
        return {"result": {"success": True, "results": []}}

    monkeypatch.setattr(lean_services, "_invoke_json_tool", fake_invoke)
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args: None)

    payload = lean_services.lean_multi_attempt("Main.lean", 1083, ["omega", "simp"], cwd=project)

    entry = lean_services._find_declaration_entry(target, "erdos_242_family_one_ordering")
    assert entry is not None
    assert entry["line"] == 1081
    assert entry["end_line"] == 1083
    target_line = target.read_text(encoding="utf-8").splitlines()[1082]
    assert target_line.index("sorry") + 1 == 79
    assert target_line[78:] == "sorry"
    assert calls[0]["line"] == 1083
    assert calls[0]["column"] == 79
    assert payload["line"] == 1083
    assert payload["column"] == 79
    assert payload["column_adjustment"] == "inline_tactic_body"

    explicit_payload = lean_services.lean_multi_attempt(
        "Main.lean", 1083, ["omega", "simp"], cwd=project, column=10
    )

    assert calls[1]["line"] == 1083
    assert calls[1]["column"] == 10
    assert explicit_payload["column"] == 10
    assert "column_adjustment" not in explicit_payload


def test_lean_multi_attempt_requires_exact_target_check_for_probe_success(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={"multi_attempt": "mcp_lean_lsp_lean_multi_attempt"},
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *_args: pytest.fail("a replaceable tactic hole must use LeanProbe first"),
    )
    exact_calls: list[dict[str, object]] = []

    def reject_exact_candidate(**kwargs):
        exact_calls.append(dict(kwargs))
        if kwargs["action"] == "prepare_file":
            return {"success": True, "ok": True, "cache": {"cache_hit": False}}
        return {
            "success": True,
            "target_verified": False,
            "status": "failed",
            "error": "linarith failed to find a contradiction",
        }

    monkeypatch.setattr(lean_incremental, "lean_incremental_check", reject_exact_candidate)
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args: None)

    payload = lean_services.lean_multi_attempt(
        "Main.lean",
        1,
        ["nlinarith", "ring"],
        cwd=project,
    )

    assert exact_calls[0]["action"] == "prepare_file"
    assert exact_calls[1]["theorem_id"] == "target"
    assert exact_calls[1]["replacement"] == "theorem target : True := by\n  nlinarith"
    assert exact_calls[2]["replacement"] == "theorem target : True := by\n  ring"
    assert payload["backend_success"] is True
    assert payload["backend_tool"] == "lean_probe"
    assert payload["screening_backend"] == "lean_probe"
    assert payload["success"] is False
    assert payload["target_verified"] is False
    assert payload["status"] == "screened_no_verified_candidate"
    assert payload["verified_attempts"] == []
    assert payload["items"][0]["probe_closed_goal"] is False
    assert payload["items"][0]["verified"] is False
    assert "exact-target" in payload["action_required"]


def test_lean_multi_attempt_stops_after_first_exact_leanprobe_success(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    target = project / "Main.lean"
    target.write_text("theorem target : True := by\n  sorry\n", encoding="utf-8")
    report = LeanCapabilityReport(
        cwd=str(project),
        project_root=str(project),
        project_valid=True,
        project_error="",
        binaries={"lean": True, "lake": True, "elan": True, "git": True, "rg": True},
        mcp_tools={"multi_attempt": "mcp_lean_lsp_lean_multi_attempt"},
        search_providers=[],
        helper_tools={},
        workers=[],
        degraded_reasons=[],
    )
    monkeypatch.setattr(lean_services, "probe_capabilities", lambda cwd=None: report)
    monkeypatch.setattr(
        lean_services,
        "_invoke_json_tool",
        lambda *_args: pytest.fail("a replaceable tactic hole must not start LSP screening"),
    )
    calls: list[dict[str, object]] = []

    def accept_first_candidate(**kwargs):
        calls.append(dict(kwargs))
        if kwargs["action"] == "prepare_file":
            return {"success": True, "ok": True}
        return {
            "success": True,
            "target_verified": True,
            "ok": True,
            "has_sorry": False,
        }

    monkeypatch.setattr(lean_incremental, "lean_incremental_check", accept_first_candidate)
    monkeypatch.setattr(lean_services, "append_workflow_outcome", lambda *args: None)

    payload = lean_services.lean_multi_attempt(
        "Main.lean",
        2,
        ["exact True.intro", "trivial"],
        cwd=project,
    )

    assert len(calls) == 2
    assert calls[1]["timeout_s"] == 30
    assert payload["success"] is True
    assert payload["verified_attempts"] == ["exact True.intro"]
    assert payload["items"][1]["screening_skipped"] == "earlier exact candidate verified"


def test_lean_multi_attempt_rejects_invalid_candidate_count_before_backend_call(
    monkeypatch, tmp_path
):
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
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("backend should not be called")
        ),
    )

    payload = lean_services.lean_multi_attempt("Demo/Main.lean", 12, ["ring"], cwd=project)

    assert payload["success"] is False
    assert any(
        "expects 2-6 concrete tactic candidates" in reason for reason in payload["degraded_reasons"]
    )
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
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("backend should not be called")
        ),
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
    assert any(
        "expects short local tactic candidates" in reason for reason in payload["degraded_reasons"]
    )


def test_lean_multi_attempt_rejects_multiline_local_have_proof(monkeypatch, tmp_path):
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
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("backend should not be called")
        ),
    )

    payload = lean_services.lean_multi_attempt(
        "Demo/Main.lean",
        12,
        [
            "have h : True := by\n  trivial\nexact h",
            "have h : True := by\n  simp\nexact h",
        ],
        cwd=project,
    )

    assert payload["success"] is False
    assert any(
        "expects short local tactic candidates" in reason for reason in payload["degraded_reasons"]
    )


def test_canonical_tool_file_path_prefers_active_file_for_basename_matches(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/Main.lean")

    resolved = lean_services._canonical_tool_file_path("Main.lean", cwd=project)

    assert resolved == str(target.resolve())


def test_project_root_prefers_native_project_env_when_cwd_omitted(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    (project / "lakefile.toml").write_text('[package]\nname = "Demo"\n', encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    root, error = lean_services._project_root()

    assert root == project.resolve()
    assert error == ""


def test_project_root_keeps_native_authority_when_tool_cwd_is_foreign(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    foreign = tmp_path / "Harness"
    project.mkdir()
    foreign.mkdir()
    (project / "lakefile.toml").write_text('[package]\nname = "Demo"\n', encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")

    root, error = lean_services._project_root(foreign)

    assert root == project.resolve()
    assert error == ""


def test_project_root_normalizes_dependency_package_cwd_to_native_project(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    dependency = project / ".lake" / "packages" / "mathlib"
    dependency.mkdir(parents=True)
    (project / "lakefile.toml").write_text('[package]\nname = "Demo"\n', encoding="utf-8")
    (dependency / "lakefile.toml").write_text(
        '[package]\nname = "mathlib"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    root, error = lean_services._project_root(dependency)

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
            "diagnostics": [
                {"severity": "error", "message": "harness_error: Failed to extract declarations"}
            ],
        },
    )

    payload = lean_services.lean_auto_probe(
        "Demo/Main.lean", "demo", cwd=project, methods=["aesop"]
    )

    assert payload["file_path"] == str(target.resolve())
    assert any(
        "harness_error: Failed to extract declarations" in reason
        for reason in payload["degraded_reasons"]
    )


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
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("MCP probe should not be called")
        ),
    )

    payload = lean_services.lean_auto_probe(
        "Demo/Main.lean", "demo", cwd=project, methods=["aesop"]
    )

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

    import leanflow_cli.lean.lean_incremental as lean_incremental

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


def test_diagnostic_items_parses_standard_lines():
    out = lean_services.diagnostic_items(
        "File.lean:12:7: error: unexpected token\nC:/proj/File.lean:3:0: warning: unused variable x"
    )
    assert out == [
        {"severity": "error", "message": "unexpected token", "line": 12},
        {"severity": "warning", "message": "unused variable x", "line": 3},
    ]


def test_diagnostic_items_does_not_catastrophically_backtrack():
    # Regression guard: a long single line carrying many `:n:n:` coordinates but NO
    # error:/warning: token previously caused O(n^2) regex backtracking that pinned the
    # autonomous runner at ~100% CPU forever. The anchored pattern parses it in well under a
    # second (pre-fix this 32 KB input took ~10s; it scales quadratically).
    import time

    pathological = "1:" * 16000  # ~32 KB, no error/warning token -> no matches
    start = time.perf_counter()
    result = lean_services.diagnostic_items(pathological)
    elapsed = time.perf_counter() - start
    assert result == []
    assert elapsed < 1.0, f"diagnostic_items took {elapsed:.2f}s — regex backtracking regression"
