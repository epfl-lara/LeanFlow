from __future__ import annotations

from pathlib import Path

from epflemma_cli import lean_services
from epflemma_cli.lean_services import LeanCapabilityReport


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
