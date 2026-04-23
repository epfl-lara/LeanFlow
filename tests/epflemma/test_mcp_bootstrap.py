from __future__ import annotations

from pathlib import Path

from epflemma_cli.mcp_bootstrap import (
    bootstrap_lean_mcp,
    managed_mcp_command_path,
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
    assert "role: primary-state-search" in rendered
    assert "role: secondary-automation-context" in rendered


def test_managed_mcp_server_status_marks_missing_servers_for_bootstrap(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    status = managed_mcp_server_status(home)

    assert status["lean-lsp"]["configured"] is False
    assert status["lean-lsp"]["installed"] is False
    assert status["lean-lsp"]["bootstrap_recommended"] is True
    assert status["lean-proof-auto"]["configured"] is False
    assert status["lean-proof-auto"]["installed"] is False


def test_bootstrap_lean_mcp_repairs_missing_managed_command(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    def _fake_install(
        venv_dir: Path,
        install_spec: str,
        *,
        python_bin: str | None = None,
        extra_install_specs: tuple[str, ...] = (),
    ) -> None:
        del install_spec, python_bin, extra_install_specs
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script_name = "lean-lsp-mcp" if venv_dir.name == "lean-lsp" else "lean-proof-auto-mcp"
        target = bin_dir / script_name
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)

    monkeypatch.setattr(
        "epflemma_cli.mcp_bootstrap._install_into_managed_venv",
        _fake_install,
    )

    first = bootstrap_lean_mcp(home=home)
    assert first["success"] is True
    assert managed_mcp_command_path("lean-lsp", home).exists()
    assert managed_mcp_command_path("lean-proof-auto", home).exists()

    managed_mcp_command_path("lean-proof-auto", home).unlink()
    assert not managed_mcp_command_path("lean-proof-auto", home).exists()

    second = bootstrap_lean_mcp(home=home)
    assert second["success"] is True
    assert managed_mcp_command_path("lean-proof-auto", home).exists()

    rendered = (home / "config.yaml").read_text(encoding="utf-8")
    assert str(managed_mcp_command_path("lean-lsp", home)) in rendered
    assert str(managed_mcp_command_path("lean-proof-auto", home)) in rendered
