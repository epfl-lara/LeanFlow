from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.project import (
    LEANFLOW_PROJECT_TEMPLATE_ENV,
    ProjectNotFoundError,
    detect_blueprint_markers,
    discover_leanflow_project,
    find_lean_project_root,
    initialize_leanflow_project,
    is_lean_project_root,
    resolve_repl_revision,
    resolve_template_source,
    setup_project_power_modes,
)


def _stub_repl_tags(monkeypatch, tags):
    """Replace the network preflight with a fixed tag list (``None`` = offline)."""
    monkeypatch.setattr(
        "leanflow_cli.workflows.project._list_repl_remote_tags", lambda *a, **k: tags
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


def test_setup_project_power_modes_adds_repl_to_lakefile_toml(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    repl = root / ".lake" / "build" / "bin" / "repl"

    def _fake_run(command, *, cwd):
        assert cwd == root.resolve()
        if command == ["lake", "build", "repl"]:
            repl.parent.mkdir(parents=True, exist_ok=True)
            repl.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        return 0, "", 0.1

    monkeypatch.setattr(
        "leanflow_cli.workflows.project.shutil.which",
        lambda name: "/usr/bin/lake" if name == "lake" else None,
    )
    monkeypatch.setattr("leanflow_cli.workflows.project._run_power_setup_command", _fake_run)
    _stub_repl_tags(monkeypatch, ["v4.19.0", "v4.20.0", "v4.21.0"])
    progress: list[str] = []

    report = setup_project_power_modes(root, progress=progress.append)

    rendered = (root / "lakefile.toml").read_text(encoding="utf-8")
    assert 'name = "repl"' in rendered
    assert 'rev = "v4.20.0"' in rendered
    assert report["status"] == "ready"
    assert report["repl_available"] is True
    assert report["repl_rev_status"] == "exact"
    assert any("lake build repl" in message for message in progress)


def test_setup_project_power_modes_does_not_duplicate_existing_repl(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lakefile.toml").write_text(
        'name = "demo"\n\n[[require]]\nname = "repl"\ngit = "https://github.com/leanprover-community/repl"\nrev = "v4.20.0"\n',
        encoding="utf-8",
    )
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", encoding="utf-8")
    monkeypatch.setattr("leanflow_cli.workflows.project.shutil.which", lambda name: None)

    report = setup_project_power_modes(root)

    rendered = (root / "lakefile.toml").read_text(encoding="utf-8")
    assert rendered.count('name = "repl"') == 1
    assert report["status"] == "lake-missing"


def test_setup_project_power_modes_leaves_lakefile_lean_manual(monkeypatch, tmp_path):
    root = _make_lean_root(tmp_path / "proj")
    original = (root / "lakefile.lean").read_text(encoding="utf-8")
    monkeypatch.setattr("leanflow_cli.workflows.project.shutil.which", lambda name: "/usr/bin/lake")
    _stub_repl_tags(monkeypatch, ["v4.19.0", "v4.20.0"])

    report = setup_project_power_modes(root)

    assert (root / "lakefile.lean").read_text(encoding="utf-8") == original
    assert report["status"] == "manual-setup-needed"
    assert "manual_steps" in report
    assert '@ "v4.20.0"' in report["manual_steps"][0]
    assert report["repl_rev_status"] == "exact"


@pytest.mark.parametrize(
    ("version", "tags", "expected_rev", "expected_status"),
    [
        ("v4.33.1", ["v4.33.0", "v4.33.1", "v4.34.0"], "v4.33.1", "exact"),
        # The readiness campaign: no v4.33.1 REPL tag exists; v4.33.0 builds under 4.33.1.
        ("v4.33.1", ["v4.32.0", "v4.33.0-rc1", "v4.33.0", "v4.34.0"], "v4.33.0", "nearest"),
        ("v4.33.1", ["v4.33.0-rc1", "v4.33.0-rc2"], "v4.33.0-rc2", "nearest"),
        # A release candidate must not pick up the final release of the same version.
        ("v4.34.0-rc2", ["v4.33.0", "v4.34.0-rc1", "v4.34.0"], "v4.34.0-rc1", "nearest"),
        ("v4.35.0", ["v4.34.0", "v4.36.0"], "", "unresolved"),
        ("nightly-2026-09-01", ["v4.34.0"], "", "unresolved"),
    ],
)
def test_resolve_repl_revision_prefers_exact_then_nearest_same_minor(
    version, tags, expected_rev, expected_status
):
    resolution = resolve_repl_revision(version, tags=tags)

    assert resolution["rev"] == expected_rev
    assert resolution["status"] == expected_status
    assert resolution["requested"] == version


def test_resolve_repl_revision_reports_an_unavailable_preflight():
    resolution = resolve_repl_revision("v4.33.1", list_tags=lambda: None)

    assert resolution["status"] == "unverified"
    assert resolution["rev"] == "v4.33.1"
    assert "unverified" in resolution["detail"]


def test_list_repl_remote_tags_parses_ls_remote_output(monkeypatch):
    from leanflow_cli.workflows import project as project_mod

    monkeypatch.setattr(project_mod.shutil, "which", lambda name: "/usr/bin/git")

    def fake_run(command, **kwargs):
        assert command[:3] == ["git", "ls-remote", "--tags"]
        assert kwargs["timeout"] == project_mod.REPL_TAG_PREFLIGHT_TIMEOUT_S
        return SimpleNamespace(
            returncode=0,
            stdout="abc\trefs/tags/v4.33.0\ndef\trefs/tags/v4.34.0-rc1\nzzz\trefs/heads/master\n",
        )

    monkeypatch.setattr(project_mod.subprocess, "run", fake_run)
    assert project_mod._list_repl_remote_tags() == ["v4.33.0", "v4.34.0-rc1"]

    monkeypatch.setattr(
        project_mod.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=128, stdout="")
    )
    assert project_mod._list_repl_remote_tags() is None
    monkeypatch.setattr(project_mod.shutil, "which", lambda name: None)
    assert project_mod._list_repl_remote_tags() is None


def test_setup_project_power_modes_pins_the_nearest_existing_repl_tag(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n", encoding="utf-8")
    monkeypatch.setattr("leanflow_cli.workflows.project.shutil.which", lambda name: None)
    _stub_repl_tags(monkeypatch, ["v4.33.0", "v4.34.0"])
    progress: list[str] = []

    report = setup_project_power_modes(root, progress=progress.append)

    rendered = (root / "lakefile.toml").read_text(encoding="utf-8")
    assert 'rev = "v4.33.0"' in rendered
    assert 'rev = "v4.33.1"' not in rendered
    assert report["repl_rev"] == "v4.33.0"
    assert report["repl_rev_status"] == "nearest"
    assert report["status"] == "lake-missing"
    assert any("no tag v4.33.1" in message for message in progress)


def test_setup_project_power_modes_stops_when_no_compatible_repl_tag_exists(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lakefile.toml").write_text('name = "demo"\n', encoding="utf-8")
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n", encoding="utf-8")
    monkeypatch.setattr("leanflow_cli.workflows.project.shutil.which", lambda name: "/usr/bin/lake")
    monkeypatch.setattr(
        "leanflow_cli.workflows.project._run_power_setup_command",
        lambda *a, **k: pytest.fail("no lake command may run for an unresolved pin"),
    )
    _stub_repl_tags(monkeypatch, ["v4.34.0"])

    report = setup_project_power_modes(root)

    assert report["status"] == "repl-rev-unresolved"
    assert "manual_steps" in report
    assert (root / "lakefile.toml").read_text(encoding="utf-8") == 'name = "demo"\n'
