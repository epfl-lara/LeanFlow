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
