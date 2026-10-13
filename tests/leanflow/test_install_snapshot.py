"""Verify explicit-runtime provenance and private concurrent packaging inputs."""

import json
from pathlib import Path

import pytest

from leanflow_cli.runtime import runtime_install
from leanflow_cli.runtime.install_inputs import companion_identity
from leanflow_cli.runtime.install_snapshot import materialize_companion_input


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Create compatible package inputs and a disk-independent tiny fixture home."""
    home = tmp_path / "state"
    home.mkdir()
    (home / "config.yaml").write_text("storage:\n  min_free_gib: 0\n")
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    source = tmp_path / "LeanFlow"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    companion = tmp_path / "LeanProbe"
    (companion / "src/lean_probe").mkdir(parents=True)
    (companion / "pyproject.toml").write_text(
        "[project]\nname='lean-probe'\nversion='0.4.1.dev0'\n"
    )
    (companion / "src/lean_probe/__init__.py").write_text("API_REVISION = 1\n")
    return source, companion


def test_explicit_runtime_rejects_changed_source(inputs, tmp_path):
    source, companion = inputs
    plan = runtime_install.prepare_install(
        source, explicit_venv=tmp_path / "explicit", lean_probe_source=companion
    )
    runtime_install.validate_install_inputs(source, plan.venv_dir)
    (companion / "src/lean_probe/changed.py").write_text("changed = True\n")
    with pytest.raises(RuntimeError, match="Source inputs changed"):
        runtime_install.validate_install_inputs(source, plan.venv_dir)


def test_parallel_runtime_inputs_share_no_writable_files(inputs):
    source, companion = inputs
    original = companion_identity(companion)
    first = runtime_install.prepare_install(source, lean_probe_source=companion)
    second = runtime_install.prepare_install(source, lean_probe_source=companion)
    left = materialize_companion_input(first.venv_dir)
    right = materialize_companion_input(second.venv_dir)
    assert left is not None and right is not None and left != right
    a = left / "src/lean_probe/__init__.py"
    b = right / "src/lean_probe/__init__.py"
    assert a.stat().st_ino != b.stat().st_ino
    a.write_text("build_mutation = True\n")
    assert companion_identity(right) == companion_identity(companion) == original


def test_unprepared_explicit_runtime_cannot_publish(inputs, tmp_path):
    source, _companion = inputs
    venv = tmp_path / "unprepared"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/leanflow").write_text("#!/bin/sh\nexit 0\n")
    with pytest.raises(RuntimeError, match="Missing captured installation inputs"):
        runtime_install.finalize_install(source, venv, tmp_path / "bin")
    assert not (tmp_path / "bin/leanflow").exists()


def test_explicit_receipt_preserves_unrelated_parent_metadata(inputs, tmp_path):
    """Use the explicit environment receipt without adopting another owner marker."""
    source, companion = inputs
    parent = tmp_path / "user parent"
    parent.mkdir()
    marker = parent / ".leanflow-storage.json"
    previous = json.dumps({"kind": "runtime", "identity": {"source": "unrelated"}})
    marker.write_text(previous)
    plan = runtime_install.prepare_install(
        source, explicit_venv=parent / "explicit", lean_probe_source=companion
    )
    runtime_install.validate_install_inputs(source, plan.venv_dir)
    (plan.venv_dir / "bin").mkdir()
    executable = plan.venv_dir / "bin/leanflow"
    executable.write_text("#!/bin/sh\nexit 0\n")
    runtime_install.finalize_install(source, plan.venv_dir, tmp_path / "bin")
    assert marker.read_text() == previous
