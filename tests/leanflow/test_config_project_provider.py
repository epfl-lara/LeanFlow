from __future__ import annotations

from leanflow_cli.workflows.project import discover_leanflow_project, initialize_leanflow_project


def test_initialize_project_uses_leanflow_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")

    project = initialize_leanflow_project(root)
    loaded = discover_leanflow_project(root)

    assert project.manifest_path == root / ".leanflow" / "project.yaml"
    assert loaded.manifest_path == project.manifest_path


def test_initialize_project_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = tmp_path / "Demo"
    root.mkdir()
    (root / "lakefile.lean").write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")

    project = initialize_leanflow_project(root)
    manifest_before = project.manifest_path.read_text(encoding="utf-8")

    reloaded = initialize_leanflow_project(root)
    manifest_after = project.manifest_path.read_text(encoding="utf-8")

    assert reloaded.manifest_path == project.manifest_path
    assert manifest_after == manifest_before
