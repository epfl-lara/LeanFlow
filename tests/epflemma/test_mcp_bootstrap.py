from __future__ import annotations

import subprocess
from pathlib import Path

from epflemma_cli.cli import mcp_bootstrap
from epflemma_cli.cli.mcp_bootstrap import (
    bootstrap_lean_mcp,
    managed_mcp_command_path,
    managed_mcp_power_status,
    managed_mcp_server_status,
    write_managed_mcp_config,
)


def test_write_managed_mcp_config_preserves_comments_and_unrelated_keys(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "# top comment\n"
        "model:\n"
        "  default: demo-model\n"
        "# keep this note\n"
        "mcp_servers:\n"
        "  custom:\n"
        "    command: /usr/bin/custom-mcp\n",
        encoding="utf-8",
    )

    payload = write_managed_mcp_config(home)

    rendered = config_path.read_text(encoding="utf-8")
    assert payload["config_path"] == str(config_path)
    assert "# top comment" in rendered
    assert "# keep this note" in rendered
    assert "model:" in rendered
    assert "custom:" in rendered
    assert "lean-lsp:" in rendered
    assert "lean-proof-auto:" in rendered
    assert "lean-explore:" in rendered
    assert "role: primary-state-search" in rendered
    assert "role: secondary-automation-context" in rendered
    assert "role: semantic-declaration-search" in rendered
    assert "args:" in rendered
    assert "- mcp" in rendered
    assert "- serve" in rendered
    assert "- --backend" in rendered
    assert "- local" in rendered
    assert "LEAN_REPL: 'true'" in rendered or "LEAN_REPL: true" in rendered
    assert "LEAN_REPL_TIMEOUT: '60'" in rendered or "LEAN_REPL_TIMEOUT: 60" in rendered
    assert "LEAN_REPL_MEM_MB: '8192'" in rendered or "LEAN_REPL_MEM_MB: 8192" in rendered
    assert "LEAN_LOOGLE_CACHE_DIR:" in rendered
    assert "LEAN_MCP_INSTRUCTIONS:" in rendered


def test_write_managed_mcp_config_migrates_leanexplore_to_local_enabled(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        "mcp_servers:\n"
        "  lean-explore:\n"
        "    command: /old/lean-explore\n"
        "    args:\n"
        "      - mcp\n"
        "      - serve\n"
        "      - --backend\n"
        "      - api\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    write_managed_mcp_config(home)

    rendered = config_path.read_text(encoding="utf-8")
    assert "- local" in rendered
    assert "enabled: true" in rendered


def test_managed_mcp_server_status_marks_missing_servers_for_bootstrap(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    status = managed_mcp_server_status(home)

    assert status["lean-lsp"]["configured"] is False
    assert status["lean-lsp"]["installed"] is False
    assert status["lean-lsp"]["bootstrap_recommended"] is True
    assert status["lean-proof-auto"]["configured"] is False
    assert status["lean-proof-auto"]["installed"] is False
    assert status["lean-explore"]["configured"] is False
    assert status["lean-explore"]["installed"] is False
    assert status["lean-lsp"]["power_modes"]["remote_search_policy"] == "public-fallbacks-enabled"


def test_managed_mcp_power_status_marks_local_loogle_toolchain_mismatch(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    cache = tmp_path / "loogle-cache"
    repo = cache / "repo"
    repo.mkdir(parents=True)
    (repo / "lean-toolchain").write_text(
        "leanprover/lean4:v4.30.0-rc1\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text(
        "leanprover/lean4:v4.30.0-rc2\n",
        encoding="utf-8",
    )
    config_path.write_text(
        "mcp_servers:\n"
        "  lean-lsp:\n"
        "    env:\n"
        "      LEAN_LOOGLE_LOCAL: 'true'\n"
        f"      LEAN_LOOGLE_CACHE_DIR: {cache}\n",
        encoding="utf-8",
    )

    power = managed_mcp_power_status(home, project_root=project)

    assert power["loogle_local_status"] == "incompatible"
    assert power["loogle_local_available"] is False
    assert power["loogle_local_ready"] is False
    assert power["loogle_toolchain"] == "leanprover/lean4:v4.30.0-rc1"
    assert power["project_toolchain"] == "leanprover/lean4:v4.30.0-rc2"


def test_bootstrap_patches_lean_lsp_loogle_project_paths(tmp_path):
    venv = tmp_path / "venv"
    package_dir = venv / "lib" / "python3.11" / "site-packages" / "lean_lsp_mcp"
    package_dir.mkdir(parents=True)
    loogle_py = package_dir / "loogle.py"
    loogle_py.write_text(
        "        paths = []\n"
        "        # Check packages directory\n"
        "        lake_packages = self.project_path / \".lake\" / \"packages\"\n",
        encoding="utf-8",
    )

    assert mcp_bootstrap._patch_lean_lsp_loogle_project_paths(venv) is True

    rendered = loogle_py.read_text(encoding="utf-8")
    assert '"lean", "--print-libdir"' in rendered
    assert "loogle_lib = self.repo_dir" in rendered
    assert mcp_bootstrap._patch_lean_lsp_loogle_project_paths(venv) is True
    assert loogle_py.read_text(encoding="utf-8") == rendered


def test_managed_mcp_bootstrap_pins_setuptools_below_torch_conflict(monkeypatch, tmp_path):
    python_path = tmp_path / "python"
    python_path.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(mcp_bootstrap, "_ensure_venv", lambda *_args, **_kwargs: python_path)

    def _fake_run(argv, *, check):
        del check
        calls.append([str(item) for item in argv])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(mcp_bootstrap.subprocess, "run", _fake_run)

    mcp_bootstrap._install_into_managed_venv(tmp_path / "venv", "demo-mcp")

    assert "setuptools<82" in calls[0]
    assert "setuptools" not in calls[0]


def test_bootstrap_lean_mcp_repairs_missing_managed_command(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    def _fake_install(
        venv_dir: Path,
        install_spec: str,
        *,
        python_bin: str | None = None,
        min_python: tuple[int, int] | None = None,
        extra_install_specs: tuple[str, ...] = (),
    ) -> None:
        del install_spec, python_bin, min_python, extra_install_specs
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script_name = {
            "lean-lsp": "lean-lsp-mcp",
            "lean-proof-auto": "lean-proof-auto-mcp",
            "lean-explore": "lean-explore",
        }[venv_dir.name]
        target = bin_dir / script_name
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)

    monkeypatch.setattr(
        "epflemma_cli.cli.mcp_bootstrap._install_into_managed_venv",
        _fake_install,
    )

    first = bootstrap_lean_mcp(home=home)
    assert first["success"] is True
    assert managed_mcp_command_path("lean-lsp", home).exists()
    assert managed_mcp_command_path("lean-proof-auto", home).exists()
    assert managed_mcp_command_path("lean-explore", home).exists()
    assert first["remote_search_policy"] == "public-fallbacks-enabled"
    assert first["power_modes"]["repl_configured"] is True

    managed_mcp_command_path("lean-proof-auto", home).unlink()
    assert not managed_mcp_command_path("lean-proof-auto", home).exists()

    second = bootstrap_lean_mcp(home=home)
    assert second["success"] is True
    assert managed_mcp_command_path("lean-proof-auto", home).exists()

    rendered = (home / "config.yaml").read_text(encoding="utf-8")
    assert str(managed_mcp_command_path("lean-lsp", home)) in rendered
    assert str(managed_mcp_command_path("lean-proof-auto", home)) in rendered
    assert str(managed_mcp_command_path("lean-explore", home)) in rendered
