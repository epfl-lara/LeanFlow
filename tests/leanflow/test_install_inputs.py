"""Check companion discovery and provenance before publishing an installer runtime."""

from pathlib import Path

import pytest

from leanflow_cli.runtime import install_inputs, runtime_install


def companion(root: Path, revision: int = 1) -> Path:
    """Create minimal companion packaging inputs without requiring dependencies."""
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='lean-probe'\nversion='0.4.1.dev0'\n")
    (root / "src/lean_probe").mkdir(parents=True)
    (root / "src/lean_probe/__init__.py").write_text(f"API_REVISION = {revision}\n")
    return root


def test_companion_discovery_requires_supported_api(tmp_path: Path) -> None:
    source = tmp_path / "LeanFlow"
    source.mkdir()
    assert install_inputs.resolve_lean_probe_source(source) is None
    other = companion(tmp_path / "LeanProbe", revision=0)
    with pytest.raises(RuntimeError, match="matching LeanProbe"):
        install_inputs.resolve_lean_probe_source(source)
    (other / "src/lean_probe/__init__.py").write_text("API_REVISION = 1\n")
    assert install_inputs.resolve_lean_probe_source(source) == other


def test_explicit_missing_or_malformed_companion_fails_early(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not compatible"):
        install_inputs.resolve_lean_probe_source(tmp_path, tmp_path / "missing")
    other = companion(tmp_path / "other")
    (other / "src/lean_probe/__init__.py").write_text("invalid Python syntax +\n")
    with pytest.raises(RuntimeError, match="not compatible"):
        install_inputs.resolve_lean_probe_source(tmp_path, other)


def test_companion_changes_create_distinct_runtime_identity(tmp_path: Path) -> None:
    source = tmp_path / "LeanFlow"
    source.mkdir()
    other = companion(tmp_path / "LeanProbe")
    first = runtime_install.source_identity(source, lean_probe_source=other)
    generated = other / "src/lean_probe.egg-info"
    generated.mkdir()
    (generated / "PKG-INFO").write_text("generated packaging metadata")
    assert runtime_install.source_identity(source, lean_probe_source=other) == first
    (other / "src/lean_probe/probe.py").write_text("behavior = 'changed'\n")
    assert runtime_install.source_identity(source, lean_probe_source=other) != first


def test_companion_source_mutation_blocks_publication(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home/config.yaml").write_text("storage:\n  min_free_gib: 0\n")
    source = tmp_path / "LeanFlow"
    source.mkdir()
    other = companion(tmp_path / "LeanProbe")
    plan = runtime_install.prepare_install(source, lean_probe_source=other)
    runtime_install.validate_install_inputs(source, plan.venv_dir)
    (other / "src/lean_probe/probe.py").write_text("changed_during_install = True\n")
    with pytest.raises(RuntimeError, match="Source inputs changed"):
        runtime_install.validate_install_inputs(source, plan.venv_dir)


def test_unsupported_runtime_path_fails_before_creating_state(tmp_path, monkeypatch) -> None:
    home = tmp_path / "state $literal"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    with pytest.raises(RuntimeError, match="dollar signs"):
        install_inputs.validate_install_paths(tmp_path, tmp_path / "bin", None)
    assert not home.exists()
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "state with spaces"))
    install_inputs.validate_install_paths(
        tmp_path / "source $literal", tmp_path / "bin $literal", None
    )
    with pytest.raises(RuntimeError, match="line breaks"):
        install_inputs.validate_install_paths(tmp_path, tmp_path / "bin\nnew line", None)


def test_companion_directory_links_are_rejected(tmp_path: Path) -> None:
    """Do not omit linked package subdirectories from the integrity receipt."""
    source = companion(tmp_path / "companion")
    outside = tmp_path / "outside"
    outside.mkdir()
    (source / "src/lean_probe/linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symbolic links"):
        install_inputs.companion_identity(source)
