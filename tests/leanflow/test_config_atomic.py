"""Publish generic settings without truncation or races with MCP selection."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from leanflow_cli import config
from leanflow_cli.cli import mcp_install_generation
from leanflow_cli.cli.mcp_bootstrap import write_managed_mcp_config


def test_atomic_save_preserves_the_existing_serialized_payload(monkeypatch, tmp_path):
    """Keep save_config's complete payload semantics while making publication atomic."""
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    payload = {"model": {"default": "test"}, "custom": {"enabled": False, "nested": [1, 2]}}

    config.save_config(payload)

    assert yaml.safe_load((home / "config.yaml").read_text()) == payload
    if os.name != "nt":
        assert (home / "config.yaml").stat().st_mode & 0o777 == 0o600


def test_atomic_save_failure_preserves_previous_complete_document(monkeypatch, tmp_path):
    """Failing the final replace cannot leave empty or partially written settings."""
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    config.save_config({"model": {"default": "original"}})
    before = (home / "config.yaml").read_bytes()

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(mcp_install_generation.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        config.save_config({"model": {"default": "changed"}})

    assert (home / "config.yaml").read_bytes() == before
    assert list(home.glob(".config.yaml-*")) == []


def test_generic_save_preserves_dotfile_symlink_and_updates_target(monkeypatch, tmp_path):
    """Use the same symlink-safe publication policy for generic settings edits."""
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "settings.yaml"
    target.write_text("model:\n  default: original\n")
    config_path = home / "config.yaml"
    config_path.symlink_to(target)
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    config.save_config({"model": {"default": "changed"}})
    assert config_path.is_symlink()
    assert yaml.safe_load(target.read_text()) == {"model": {"default": "changed"}}


@pytest.mark.parametrize("operation", ["ensure", "save"])
def test_env_replace_failure_preserves_complete_existing_entries(monkeypatch, tmp_path, operation):
    """Neither default merging nor a value update can truncate an existing environment file."""
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    config.ensure_leanflow_home()
    env_path = home / ".env"
    existing = "# retained\nCUSTOM=original\n"
    if operation == "save":
        existing += config.DEFAULT_ENV_TEMPLATE
    env_path.write_text(existing)

    def fail(*args):
        assert env_path.read_text() == existing
        raise OSError("env replace failed")

    monkeypatch.setattr(mcp_install_generation.os, "replace", fail)
    with pytest.raises(OSError, match="env replace failed"):
        if operation == "ensure":
            config.ensure_leanflow_home()
        else:
            config.save_env_value("NEW_ENTRY", "updated")
    assert env_path.read_text() == existing
    assert list(home.glob("..env-*")) == []


def test_env_merge_waits_and_preserves_concurrent_entry_update(monkeypatch, tmp_path):
    """Merge defaults only after reading the latest locked environment document."""
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    config.ensure_leanflow_home()
    env_path = home / ".env"
    env_path.write_text("CUSTOM=original\n")
    script = (
        "from leanflow_cli.config import ensure_leanflow_home, save_env_value\n"
        "print('waiting', flush=True)\n"
        "ensure_leanflow_home()\n"
        "save_env_value('SECOND', 'concurrent')\n"
    )
    process = None
    try:
        with mcp_install_generation.managed_mcp_install_lock(home):
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                env={**os.environ, "LEANFLOW_HOME": str(home)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "waiting"
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.05)
            config.save_env_value("FIRST", "published-before-unlock")
        _output, error = process.communicate(timeout=5)
        assert process.returncode == 0, error
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    values = config.load_env_file()
    assert values["CUSTOM"] == "original"
    assert values["FIRST"] == "published-before-unlock"
    assert values["SECOND"] == "concurrent"
    assert "LEANFLOW_INFERENCE_PROVIDER" in values


def test_setting_update_waits_for_selection_and_reads_the_new_command(monkeypatch, tmp_path):
    """A waiting single-setting edit must retain the generation selected before it acquires ownership."""
    home = tmp_path / "home"
    monkeypatch.setenv("LEANFLOW_HOME", str(home))
    config.save_config({"model": {"default": "original"}})
    selected = home / "mcp/venvs/generation/lean-lsp"
    script = (
        "from leanflow_cli.config import load_config, set_config_value\n"
        "load_config()\n"
        "print('waiting', flush=True)\n"
        "set_config_value('model.default', 'edited')\n"
    )
    process = None
    try:
        with mcp_install_generation.managed_mcp_install_lock(home):
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                env={**os.environ, "LEANFLOW_HOME": str(home)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "waiting"
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.05)
            write_managed_mcp_config(home, venv_dirs={"lean-lsp": selected})
        _output, error = process.communicate(timeout=5)
        assert process.returncode == 0, error
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    payload = yaml.safe_load((home / "config.yaml").read_text())
    assert payload["model"]["default"] == "edited"
    assert Path(payload["mcp_servers"]["lean-lsp"]["command"]).parent.parent == selected
