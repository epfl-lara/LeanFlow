from pathlib import Path

import yaml

from leanflow_cli.workflows.project_guidance import attach_project_guidance


def _write_manifest(root: Path, entries: list[dict]) -> None:
    manifest = root / ".leanflow" / "project.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        yaml.safe_dump({"schema_version": 1, "workflow_guidance": entries}),
        encoding="utf-8",
    )


def test_attach_project_guidance_matches_assignment_and_deduplicates(tmp_path):
    guidance = tmp_path / "P6_HANDOFF.md"
    guidance.write_text("Use the prime-support descent.", encoding="utf-8")
    _write_manifest(
        tmp_path,
        [
            {
                "path": "P6_HANDOFF.md",
                "targets": ["result"],
                "active_files": ["IMO2026/P6.lean"],
            }
        ],
    )
    active_file = tmp_path / "IMO2026" / "P6.lean"

    first = attach_project_guidance(
        project_root=str(tmp_path),
        target_symbol="result",
        active_file=str(active_file),
        user_message="Continue.",
        conversation_history=[],
    )
    assert "Use the prime-support descent." in first
    assert "[LEANFLOW PROJECT GUIDANCE sha256=" in first

    second = attach_project_guidance(
        project_root=str(tmp_path),
        target_symbol="result",
        active_file=str(active_file),
        user_message="Continue again.",
        conversation_history=[{"role": "user", "content": first}],
    )
    assert second == "Continue again."


def test_attach_project_guidance_ignores_other_targets_and_escaping_paths(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("must not load", encoding="utf-8")
    local = tmp_path / "local.md"
    local.write_text("wrong target", encoding="utf-8")
    _write_manifest(
        tmp_path,
        [
            {"path": str(outside), "targets": ["result"]},
            {"path": "local.md", "targets": ["other"]},
        ],
    )

    message = attach_project_guidance(
        project_root=str(tmp_path),
        target_symbol="result",
        active_file=str(tmp_path / "IMO2026" / "P6.lean"),
        user_message="Continue.",
        conversation_history=[],
    )

    assert message == "Continue."
