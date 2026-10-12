from __future__ import annotations

from pathlib import Path

import pytest

from leanflow_cli.workflows.project import (
    LEANFLOW_PROJECT_TEMPLATE_ENV,
    ProjectNotFoundError,
    detect_blueprint_markers,
    discover_leanflow_project,
    find_lean_project_root,
    initialize_leanflow_project,
    is_lean_project_root,
    resolve_template_source,
)


def _make_lean_root(path: Path, lakefile_name: str = "lakefile.lean") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / lakefile_name).write_text(
        "import Lake\nopen Lake DSL\npackage demo\n", encoding="utf-8"
    )
    (path / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    return path


def test_is_lean_project_root_recognizes_lakefile_lean(tmp_path):
    root = _make_lean_root(tmp_path / "proj-lean")

    assert is_lean_project_root(root) is True


def test_is_lean_project_root_recognizes_lakefile_toml(tmp_path):
    root = tmp_path / "proj-toml"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")

    assert is_lean_project_root(root) is True


def test_is_lean_project_root_returns_false_without_lakefile(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    assert is_lean_project_root(empty) is False


def test_find_lean_project_root_walks_up_to_lakefile(tmp_path):
    root = _make_lean_root(tmp_path / "proj")
    nested = root / "src" / "deep" / "nested"
    nested.mkdir(parents=True)

    found = find_lean_project_root(nested)

    assert found == root.resolve()


def test_find_lean_project_root_returns_none_outside_project(tmp_path):
    outside = tmp_path / "not-a-project"
    outside.mkdir()

    assert find_lean_project_root(outside) is None


def test_detect_blueprint_markers_lists_present_markers(tmp_path):
    root = _make_lean_root(tmp_path / "proj")
    templates = root / "templates"
    templates.mkdir()
    (templates / "blueprint.yml").write_text("name: demo\n", encoding="utf-8")

    markers = detect_blueprint_markers(root)

    assert "lean-toolchain" in markers
    assert "lakefile.lean" in markers
    assert "templates/blueprint.yml" in markers
    # No lakefile.toml in this project
    assert "lakefile.toml" not in markers


def test_detect_blueprint_markers_returns_empty_tuple_for_empty_dir(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    assert detect_blueprint_markers(empty) == ()


def test_resolve_template_source_prefers_explicit_env_over_config():
    env = {LEANFLOW_PROJECT_TEMPLATE_ENV: "https://env.example/template.git"}
    config = {"leanflow": {"project": {"template_source": "https://config.example/template.git"}}}

    assert resolve_template_source(config, env) == "https://env.example/template.git"


def test_resolve_template_source_uses_config_when_env_empty():
    config = {"leanflow": {"project": {"template_source": "https://config.example/template.git"}}}

    assert resolve_template_source(config, {}) == "https://config.example/template.git"


def test_resolve_template_source_returns_empty_when_nothing_set():
    assert resolve_template_source(None, {}) == ""
    assert resolve_template_source({}, {}) == ""
    assert resolve_template_source({"leanflow": {"project": {}}}, {}) == ""


def test_discover_leanflow_project_raises_when_no_lean_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    not_lean = tmp_path / "nowhere"
    not_lean.mkdir()

    with pytest.raises(ProjectNotFoundError):
        discover_leanflow_project(not_lean)


def test_discovery_skips_ancestor_state_directory_without_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    ancestor = _make_lean_root(tmp_path / "ancestor")
    (ancestor / ".leanflow").mkdir()
    nested = _make_lean_root(ancestor / "nested")
    initialized = initialize_leanflow_project(nested)

    discovered = discover_leanflow_project(nested / "Demo")

    assert discovered.root == initialized.root


def test_initialize_then_discover_produces_equal_project(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    root = _make_lean_root(tmp_path / "proj")

    initialized = initialize_leanflow_project(root)
    discovered = discover_leanflow_project(root)

    assert discovered.root == initialized.root
    assert discovered.name == initialized.name
    assert discovered.manifest_path == initialized.manifest_path
    assert discovered.runtime_dir == root.resolve() / ".leanflow" / "runtime"
    assert discovered.cache_dir == root.resolve() / ".leanflow" / "cache"
    assert discovered.workflows_dir == root.resolve() / ".leanflow" / "workflows"


def test_initialize_from_nested_folder_registers_at_lake_root(tmp_path):
    root = _make_lean_root(tmp_path / "proj")
    nested = root / "Demo" / "Subfolder"
    nested.mkdir(parents=True)
    project = initialize_leanflow_project(nested)
    assert project.root == root
    assert project.lean_root == root
    assert not (nested / ".leanflow").exists()
