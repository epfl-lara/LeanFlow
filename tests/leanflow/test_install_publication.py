"""Protect both command generations across abrupt installer termination."""

import json
import os
import subprocess
import sys
from pathlib import Path

from leanflow_cli.runtime import runtime_install
from leanflow_cli.runtime.storage import StoragePolicy, cleanup_storage
from leanflow_cli.runtime.storage.entries import protected_install_paths


def test_interrupted_pointer_switch_pins_both_runtimes(tmp_path, monkeypatch):
    """Keep an already-switched command usable after the publisher is killed."""
    home = tmp_path / "state"
    home.mkdir()
    (home / "config.yaml").write_text("storage:\n  min_free_gib: 0\n")
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")

    def create(plan):
        (plan.venv_dir / "bin").mkdir(parents=True)
        for name in ("leanflow", "python"):
            path = plan.venv_dir / "bin" / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)

    first = runtime_install.prepare_install(source)
    create(first)
    binaries = tmp_path / "bin"
    runtime_install.finalize_install(source, first.venv_dir, binaries)
    second = runtime_install.prepare_install(source, recreate=True)
    create(second)
    child = """
import os, sys
from pathlib import Path
from leanflow_cli.runtime import runtime_install
original = runtime_install._atomic_text
def interrupted(path, text, **kwargs):
    if path.name == 'install-root': os._exit(9)
    original(path, text, **kwargs)
runtime_install._atomic_text = interrupted
runtime_install.finalize_install(*map(Path, sys.argv[1:]))
"""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-c", child, str(source), str(second.venv_dir), str(binaries)],
        env={**os.environ, "PYTHONPATH": str(root)},
        check=False,
        timeout=20,
    )
    assert result.returncode == 9
    protected = protected_install_paths(home)
    assert protected is not None
    assert first.venv_dir in protected and second.venv_dir in protected
    cleanup_storage(policy=StoragePolicy(0, 0))
    assert first.venv_dir.exists() and second.venv_dir.exists()
    assert subprocess.run([str(binaries / "leanflow")], check=False).returncode == 0
    runtime_install.finalize_install(source, second.venv_dir, binaries)
    assert not (home / "install-switch.json").exists()


def test_malformed_switch_journal_fails_closed(tmp_path):
    """Pin runtime collection when an interrupted switch cannot be reconstructed."""
    (tmp_path / "install-switch.json").write_text(json.dumps({"paths": ["relative"]}))
    assert protected_install_paths(tmp_path) is None


def test_unchanged_runtime_reuse_needs_no_new_storage_admission(tmp_path, monkeypatch):
    """Allow an already validated runtime to be selected on a nearly full disk."""
    home = tmp_path / "state"
    home.mkdir()
    (home / "config.yaml").write_text("storage:\n  min_free_gib: 0\n")
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    first = runtime_install.prepare_install(source)
    (first.venv_dir / "bin").mkdir(parents=True)
    for name in ["leanflow", "python"]:
        path = first.venv_dir / "bin" / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    runtime_install.finalize_install(source, first.venv_dir, tmp_path / "bin")

    def refuse(*args, **kwargs):
        raise AssertionError("No-op rerun requested new storage")

    monkeypatch.setattr("leanflow_cli.runtime.storage.ensure_storage_capacity", refuse)
    assert runtime_install.prepare_install(source).reuse is True
