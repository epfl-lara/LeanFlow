"""Reject display cache drift while retaining strict byte verification for actual acceptance."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from leanflow_cli.formalization import environment_display as display


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n")
    packages = root / ".lake/packages/lib"
    packages.mkdir(parents=True)
    (packages / "Test.olean").write_bytes(b"original")
    compiler = tmp_path / "compiler"
    (compiler / "bin").mkdir(parents=True)
    (compiler / "lib/lean").mkdir(parents=True)
    (compiler / "bin/lean").write_bytes(b"compiler")
    base = tmp_path / "repository"
    (base / "core").mkdir(parents=True)
    (base / "core/checker.py").write_text("original checker")
    monkeypatch.setattr(
        display, "__file__", str(base / "leanflow_cli/formalization/environment_display.py")
    )
    monkeypatch.setattr(display, "_compiler_root", lambda root: compiler)
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    return root, compiler, base


def test_repeated_display_reuses_bytes_but_same_size_restored_mtime_edit_invalidates(inputs):
    root, compiler, base = inputs
    calls = []

    def checked(root):
        calls.append(root)
        return "environment-" + str(len(calls))

    first = display.display_environment_identity(root, checker=checked)
    assert display.display_environment_identity(root, checker=checked) == first
    assert len(calls) == 1
    file = root / ".lake/packages/lib/Test.olean"
    before = file.stat()
    file.write_bytes(b"modified")
    os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert display.display_environment_identity(root, checker=checked) != first
    assert len(calls) == 2
    (base / "core/checker.py").write_text("modified checker")
    display.display_environment_identity(root, checker=checked)
    assert len(calls) == 3
    (compiler / "bin/lean").write_bytes(b"modified compiler")
    display.display_environment_identity(root, checker=checked)
    assert len(calls) == 4


def test_directory_aliases_disable_display_cache(inputs):
    root, _, _ = inputs
    target = root / "external"
    target.mkdir()
    (root / ".lake/packages/alias").symlink_to(target, target_is_directory=True)
    calls = []
    checker = lambda root: calls.append(root) or "checked"
    assert display.display_environment_identity(root, checker=checker) == "checked"
    assert display.display_environment_identity(root, checker=checker) == "checked"
    assert len(calls) == 2


def test_cache_corruption_and_changes_during_byte_check_cannot_reuse(inputs):
    root, _, _ = inputs
    display.display_environment_identity(root, checker=lambda root: "first")
    path = next(
        (Path(os.environ["LEANFLOW_HOME"]) / "formalization-display-environments").glob("*.json")
    )
    value = json.loads(path.read_text())
    value["environment_id"] = "forged"
    path.write_text(json.dumps(value))
    assert display.display_environment_identity(root, checker=lambda root: "checked") == "checked"
    (root / "lakefile.toml").write_text('name = "changed"\n')

    def changing(root):
        (root / "lakefile.toml").write_text('name = "changed again"\n')
        return "unstable"

    with pytest.raises(ValueError, match="Environment changed"):
        display.display_environment_identity(root, checker=changing)
    assert json.loads(path.read_text())["environment_id"] == "checked"
