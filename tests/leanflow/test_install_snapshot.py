"""Verify explicit-runtime provenance and private concurrent packaging inputs."""

import json
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from leanflow_cli.runtime import runtime_install
from leanflow_cli.runtime.install_identity import source_package_files
from leanflow_cli.runtime.install_inputs import companion_identity
from leanflow_cli.runtime.install_snapshot import (
    materialize_companion_input,
    materialize_leanflow_input,
)


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


def test_clean_wheel_excludes_deleted_modules_from_stale_checkout_build(inputs, tmp_path):
    """Reproduce setuptools stale output inclusion and verify isolated wheel contents."""
    source, companion = inputs
    (source / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='1.0'\n"
        "[tool.setuptools.packages.find]\ninclude=['leanflow_cli*']\n"
    )
    package = source / "leanflow_cli/lean"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "native.py").write_text("native = True\n")
    (source / "NOTICE").write_text("Required upstream attribution\n")
    stale = source / "build/lib/leanflow_cli/lean"
    stale.mkdir(parents=True)
    (stale / "lean_interact_compat.py").write_text("removed_wrapper = True\n")
    plan = runtime_install.prepare_install(source, mode="wheel", lean_probe_source=companion)

    def wheel_files(root: Path, name: str) -> set[str]:
        """Build a real offline wheel through the same setuptools backend as pip."""
        destination = tmp_path / name
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys;from setuptools.build_meta import build_wheel;build_wheel(sys.argv[1])",
                str(destination),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        with zipfile.ZipFile(next(destination.glob("*.whl"))) as wheel:
            return set(wheel.namelist())

    removed = "leanflow_cli/lean/lean_interact_compat.py"
    assert removed in wheel_files(source, "contaminated-wheel")
    snapshot = materialize_leanflow_input(plan.venv_dir)
    assert not (snapshot / "build").exists()
    assert not list(snapshot.glob("*.egg-info"))
    assert not (snapshot / ".leanflow").exists()
    clean = wheel_files(snapshot, "clean-wheel")
    assert "leanflow_cli/lean/native.py" in clean
    assert removed not in clean
    assert any(path.endswith("/NOTICE") for path in clean)
    runtime_install.validate_install_inputs(source, plan.venv_dir)


def test_wheel_inputs_are_independent_and_source_mutations_are_rejected(inputs):
    """Keep simultaneous wheel builds separate and enforce captured source bytes."""
    source, companion = inputs
    (source / "leanflow_cli").mkdir()
    module = source / "leanflow_cli/__init__.py"
    module.write_text("original = True\n")
    first = runtime_install.prepare_install(source, mode="wheel", lean_probe_source=companion)
    second = runtime_install.prepare_install(source, mode="wheel", lean_probe_source=companion)
    left, right = (materialize_leanflow_input(plan.venv_dir) for plan in (first, second))
    assert (left / "leanflow_cli/__init__.py").stat().st_ino != (
        right / "leanflow_cli/__init__.py"
    ).stat().st_ino
    (left / "leanflow_cli/__init__.py").write_text("builder_mutation = True\n")
    assert (right / "leanflow_cli/__init__.py").read_bytes() == module.read_bytes()
    third = runtime_install.prepare_install(source, mode="wheel", lean_probe_source=companion)
    module.write_text("changed = True\n")
    with pytest.raises(RuntimeError, match="changed before packaging"):
        materialize_leanflow_input(third.venv_dir)


def test_real_wheel_excludes_retired_formalization_modules_from_stale_build(inputs, tmp_path):
    """Build actual shipped package inputs with retired modules deliberately left in build/lib."""
    source, companion = inputs
    repository = Path(__file__).resolve().parents[2]
    removed = {
        "leanflow_cli/formalization/document_extraction.py",
        "leanflow_cli/formalization/formalization_generated_lean.py",
        "leanflow_cli/formalization/formalization_markdown.py",
        "leanflow_cli/formalization/formalization_document_runner.py",
        "leanflow_cli/native/harness_formalization.py",
        "leanflow_cli/native/support_module_materialization.py",
        "leanflow_cli/native/harness_draft.py",
    }
    assert not any((repository / name).exists() for name in removed)
    for original in source_package_files(repository):
        destination = source / original.relative_to(repository)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
    for name in removed:
        stale = source / "build/lib" / name
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("raise RuntimeError('retired formalization module shipped')\n")
    plan = runtime_install.prepare_install(source, mode="wheel", lean_probe_source=companion)

    def wheel_files(root: Path, label: str) -> set[str]:
        destination = tmp_path / label
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys;from setuptools.build_meta import build_wheel;build_wheel(sys.argv[1])",
                str(destination),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        with zipfile.ZipFile(next(destination.glob("*.whl"))) as wheel:
            return set(wheel.namelist())

    assert removed <= wheel_files(source, "contaminated-formalization-wheel")
    clean = wheel_files(materialize_leanflow_input(plan.venv_dir), "clean-formalization-wheel")
    assert not removed & clean
    assert {
        "leanflow_cli/formalization/source_display.py",
        "leanflow_cli/formalization/source_inventory.py",
        "leanflow_cli/formalization/review.py",
        "leanflow_cli/formalization/publication.py",
        "leanflow_skills/lean-formalization/SKILL.md",
    } <= clean


def test_editable_inputs_keep_original_source_path(inputs):
    """Never turn an editable install into an edit of an internal build copy."""
    source, companion = inputs
    plan = runtime_install.prepare_install(source, lean_probe_source=companion)
    with pytest.raises(RuntimeError, match="Editable installations"):
        materialize_leanflow_input(plan.venv_dir)


def test_shipped_package_configuration_is_covered_by_snapshot_inputs():
    """Fail when maintained package metadata gains inputs absent from clean wheel snapshots."""
    root = Path(__file__).resolve().parents[2]
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    selected = set(source_package_files(root))
    assert not any((root / name).exists() for name in ("MANIFEST.in", "setup.cfg", "setup.py"))
    assert "license-files" not in metadata["project"]
    assert root / metadata["project"]["readme"] in selected
    setuptools = metadata["tool"]["setuptools"]
    assert set(setuptools) == {"py-modules", "packages", "package-data"}
    assert set(setuptools["packages"]["find"]) == {"include"}
    for name in setuptools["py-modules"]:
        assert root / f"{name}.py" in selected
    for pattern in setuptools["packages"]["find"]["include"]:
        package = root / pattern.split(".")[0]
        assert package / "__init__.py" in selected
    for package, patterns in setuptools["package-data"].items():
        for pattern in patterns:
            assert set((root / package).glob(pattern)) <= selected
    for pattern in ("LICEN[CS]E*", "COPYING*", "NOTICE*", "AUTHORS*"):
        assert {path for path in root.glob(pattern) if path.is_file()} <= selected
