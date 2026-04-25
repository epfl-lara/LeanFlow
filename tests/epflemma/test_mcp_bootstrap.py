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
    assert "lean-explore:" in rendered
    assert "role: primary-state-search" in rendered
    assert "role: secondary-automation-context" in rendered
    assert "role: semantic-declaration-search" in rendered
    assert "args:" in rendered
    assert "- mcp" in rendered
    assert "- serve" in rendered
    assert "- --backend" in rendered
    assert "- api" in rendered
    assert "LEAN_REPL: 'true'" in rendered or "LEAN_REPL: true" in rendered
    assert "LEAN_REPL_TIMEOUT: '60'" in rendered or "LEAN_REPL_TIMEOUT: 60" in rendered
    assert "LEAN_REPL_MEM_MB: '8192'" in rendered or "LEAN_REPL_MEM_MB: 8192" in rendered
    assert "LEAN_LOOGLE_CACHE_DIR:" in rendered
    assert "LEAN_MCP_INSTRUCTIONS:" in rendered


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
        script_name = {
            "lean-lsp": "lean-lsp-mcp",
            "lean-proof-auto": "lean-proof-auto-mcp",
            "lean-explore": "lean-explore",
        }[venv_dir.name]
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
