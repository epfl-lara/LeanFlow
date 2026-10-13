from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from leanflow_cli.cli import loogle_local, mcp_bootstrap, mcp_install_generation
from leanflow_cli.cli.mcp_bootstrap import (
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
    assert "- api" in rendered
    assert "LEAN_REPL: 'true'" in rendered or "LEAN_REPL: true" in rendered
    assert "LEAN_REPL_TIMEOUT: '60'" in rendered or "LEAN_REPL_TIMEOUT: 60" in rendered
    assert "LEAN_REPL_MEM_MB: '8192'" in rendered or "LEAN_REPL_MEM_MB: 8192" in rendered
    assert "LEAN_LOOGLE_CACHE_DIR:" in rendered
    assert "LEAN_MCP_INSTRUCTIONS:" in rendered


def test_write_managed_mcp_config_preserves_disabled_semantic_search(tmp_path):
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
    assert "- api" in rendered
    assert "enabled: false" in rendered


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
    # Per-toolchain model: status resolves the project's per-toolchain cache dir. A
    # wrong-toolchain (rc1) Loogle sitting in the rc2 project's dir is still flagged
    # "incompatible" so it falls back to remote rather than loading mismatched oleans.
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text(
        "leanprover/lean4:v4.30.0-rc2\n",
        encoding="utf-8",
    )
    cache = mcp_bootstrap.managed_loogle_cache_dir(home, toolchain="leanprover/lean4:v4.30.0-rc2")
    repo = cache / "repo"
    repo.mkdir(parents=True)
    (repo / "lean-toolchain").write_text("leanprover/lean4:v4.30.0-rc1\n", encoding="utf-8")
    config_path.write_text(
        "mcp_servers:\n  lean-lsp:\n    env:\n      LEAN_LOOGLE_LOCAL: 'true'\n",
        encoding="utf-8",
    )

    power = managed_mcp_power_status(home, project_root=project)

    assert power["loogle_local_status"] == "incompatible"
    assert power["loogle_local_available"] is False
    assert power["loogle_local_ready"] is False
    assert power["loogle_toolchain"] == "leanprover/lean4:v4.30.0-rc1"
    assert power["project_toolchain"] == "leanprover/lean4:v4.30.0-rc2"


def test_managed_loogle_cache_dir_is_per_toolchain(tmp_path):
    home = tmp_path / "home"
    generic = mcp_bootstrap.managed_loogle_cache_dir(home)
    per_tc = mcp_bootstrap.managed_loogle_cache_dir(home, toolchain="leanprover/lean4:v4.30.0-rc2")
    assert generic.name == "loogle"
    assert per_tc.name == "loogle-leanprover-lean4-v4.30.0-rc2"
    assert per_tc.parent == generic.parent  # both under <home>/mcp/cache


def test_loogle_resolvers_agree_on_per_toolchain_dir(tmp_path):
    # build, status, and the lean-lsp server launch MUST resolve the SAME per-toolchain dir,
    # or local Loogle silently goes "incompatible" again.
    from tools.mcp.mcp_transport import _augment_lean_stdio_env

    home = tmp_path / "home"
    home.mkdir()
    write_managed_mcp_config(home)
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.30.0-rc2\n", encoding="utf-8")

    build_dir = loogle_local.loogle_cache_dir_for_project(home, project)
    status_dir = Path(managed_mcp_power_status(home, project_root=project)["loogle_cache_dir"])
    cfg_cache = mcp_bootstrap._lean_lsp_env_from_home(home)["LEAN_LOOGLE_CACHE_DIR"]
    server_dir = Path(
        _augment_lean_stdio_env("lean-lsp", {"LEAN_LOOGLE_CACHE_DIR": cfg_cache}, str(project))[
            "LEAN_LOOGLE_CACHE_DIR"
        ]
    )
    assert build_dir == status_dir == server_dir
    assert build_dir.name == "loogle-leanprover-lean4-v4.30.0-rc2"


def test_patch_lean_lsp_loogle_build_lock_is_valid_and_idempotent(tmp_path):
    import ast

    pkg = tmp_path / "lib" / "python3.12" / "site-packages" / "lean_lsp_mcp"
    pkg.mkdir(parents=True)
    (pkg / "loogle.py").write_text(
        "class LocalLoogle:\n"
        "    def _build_loogle(self) -> bool:\n"
        "        if self.is_installed:\n"
        "            return True\n"
        "        return self._do_build()\n",
        encoding="utf-8",
    )
    assert loogle_local.patch_lean_lsp_loogle_build_lock(tmp_path) is True
    patched = (pkg / "loogle.py").read_text(encoding="utf-8")
    ast.parse(patched)  # must remain valid Python
    assert "_leanflow_build_loogle_inner" in patched
    assert ".loogle-build.lock" in patched
    # Idempotent: a second pass is a no-op and does not double-wrap.
    assert loogle_local.patch_lean_lsp_loogle_build_lock(tmp_path) is True
    assert (pkg / "loogle.py").read_text(encoding="utf-8") == patched


def test_patch_lean_lsp_loogle_lifecycle_is_valid_and_idempotent(tmp_path):
    import ast

    pkg = tmp_path / "lib" / "python3.12" / "site-packages" / "lean_lsp_mcp"
    pkg.mkdir(parents=True)
    loogle_path = pkg / "loogle.py"
    loogle_path.write_text(
        "import asyncio\n"
        "class LocalLoogle:\n"
        "    async def start(self):\n"
        "        try:\n"
        "            await self.ready()\n"
        "        except asyncio.TimeoutError:\n"
        '            logger.error("Loogle startup timeout")\n'
        "            return False\n",
        encoding="utf-8",
    )
    server_path = pkg / "server.py"
    server_path.write_text(
        "async def app_lifespan():\n"
        "    try:\n"
        "        yield\n"
        "    finally:\n"
        '        logger.info("Session ending — cleaning up per-session resources")\n'
        "\n"
        "        cleanup()\n",
        encoding="utf-8",
    )

    assert loogle_local.patch_lean_lsp_loogle_lifecycle(tmp_path) is True
    patched_loogle = loogle_path.read_text(encoding="utf-8")
    patched_server = server_path.read_text(encoding="utf-8")
    ast.parse(patched_loogle)
    ast.parse(patched_server)
    assert "await self.stop()" in patched_loogle
    assert "await context.loogle_manager.stop()" in patched_server

    assert loogle_local.patch_lean_lsp_loogle_lifecycle(tmp_path) is True
    assert loogle_path.read_text(encoding="utf-8") == patched_loogle
    assert server_path.read_text(encoding="utf-8") == patched_server


def test_bootstrap_patches_lean_lsp_loogle_project_paths(tmp_path):
    venv = tmp_path / "venv"
    package_dir = venv / "lib" / "python3.11" / "site-packages" / "lean_lsp_mcp"
    package_dir.mkdir(parents=True)
    loogle_py = package_dir / "loogle.py"
    loogle_py.write_text(
        "        paths = []\n"
        "        # Check packages directory\n"
        '        lake_packages = self.project_path / ".lake" / "packages"\n',
        encoding="utf-8",
    )

    assert mcp_bootstrap._patch_lean_lsp_loogle_project_paths(venv) is True

    rendered = loogle_py.read_text(encoding="utf-8")
    assert '"lean", "--print-libdir"' in rendered
    assert "loogle_lib = self.repo_dir" in rendered
    assert mcp_bootstrap._patch_lean_lsp_loogle_project_paths(venv) is True
    assert loogle_py.read_text(encoding="utf-8") == rendered


def test_bootstrap_patches_proof_auto_rich_logging_to_stderr(tmp_path):
    package_dir = tmp_path / "lib" / "python3.12" / "site-packages" / "lean_interact"
    package_dir.mkdir(parents=True)
    utils_py = package_dir / "utils.py"
    utils_py.write_text(
        "from rich.logging import RichHandler\n" "handler = RichHandler(rich_tracebacks=True)\n",
        encoding="utf-8",
    )

    assert mcp_bootstrap._patch_lean_proof_auto_stdio_logging(tmp_path) is True

    rendered = utils_py.read_text(encoding="utf-8")
    assert "from rich.console import Console" in rendered
    assert "console=Console(stderr=True)" in rendered
    assert mcp_bootstrap._patch_lean_proof_auto_stdio_logging(tmp_path) is True
    assert utils_py.read_text(encoding="utf-8") == rendered


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

    monkeypatch.setattr(
        "leanflow_cli.cli.mcp_bootstrap._install_into_managed_venv",
        _fake_managed_install,
    )
    monkeypatch.setattr(
        mcp_bootstrap,
        "_validate_managed_mcp_environment",
        lambda *args, **kwargs: {"console_script": True, "fastmcp": True, "initialized": True},
    )

    first = bootstrap_lean_mcp(home=home)
    assert first["success"] is True
    assert managed_mcp_command_path("lean-lsp", home).exists()
    assert managed_mcp_command_path("lean-proof-auto", home).exists()
    assert managed_mcp_command_path("lean-explore", home).exists()
    assert first["remote_search_policy"] == "public-fallbacks-enabled"
    assert first["power_modes"]["repl_configured"] is True
    first_command = managed_mcp_command_path("lean-lsp", home)

    managed_mcp_command_path("lean-proof-auto", home).unlink()
    assert not managed_mcp_command_path("lean-proof-auto", home).exists()

    second = bootstrap_lean_mcp(home=home)
    assert second["success"] is True
    assert managed_mcp_command_path("lean-proof-auto", home).exists()
    assert managed_mcp_command_path("lean-lsp", home) != first_command
    assert first_command.exists()

    rendered = (home / "config.yaml").read_text(encoding="utf-8")
    assert str(managed_mcp_command_path("lean-lsp", home)) in rendered
    assert str(managed_mcp_command_path("lean-proof-auto", home)) in rendered
    assert str(managed_mcp_command_path("lean-explore", home)) in rendered


def _fake_managed_install(venv_dir: Path, install_spec: str, **kwargs) -> None:
    """Create console markers without installing packages during transaction tests."""
    spec = next(
        spec
        for spec in mcp_bootstrap.MANAGED_LEAN_MCP_SPECS.values()
        if spec.venv_name == venv_dir.name
    )
    command = mcp_bootstrap._managed_command_in_venv(spec, venv_dir)
    command.parent.mkdir(parents=True)
    command.write_text("#!/bin/sh\nexit 0\n")
    command.chmod(0o755)
    _fake_package_metadata(venv_dir, spec)
    if spec.name == "lean-lsp":
        package = venv_dir / "lib/python3.12/site-packages/lean_lsp_mcp"
        package.mkdir()
        (package / "loogle.py").write_text(
            "# When any --path is passed to loogle\n"
            "# _leanflow_build_loogle_inner\n# _leanflow_index_dialect\n"
            "# LeanFlow: reap Loogle after startup timeout\n"
        )
        (package / "server.py").write_text(
            "# LeanFlow: stop the shared local Loogle with the stdio session\n"
        )
    elif spec.name == "lean-proof-auto":
        package = venv_dir / "lib/python3.12/site-packages/lean_interact"
        package.mkdir()
        (package / "utils.py").write_text(
            "# RichHandler(rich_tracebacks=True, console=Console(stderr=True))\n"
        )


def _fake_package_metadata(venv_dir, spec):
    """Describe isolated test package versions without downloading distributions."""
    for package in mcp_bootstrap._managed_mcp_packages(spec):
        metadata = venv_dir / "lib/python3.12/site-packages" / f"{package}-1.0.dist-info/METADATA"
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(f"Name: {package}\nVersion: 1.0\n")


@pytest.mark.parametrize("failure", ["install", "validation", "selection"])
def test_failed_generation_preserves_previous_config_and_backend_paths(
    monkeypatch, tmp_path, failure
):
    """Discard only this incomplete generation when any pre-selection gate fails."""
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    old = home / "mcp/venvs/old-generation/lean-lsp"
    command = mcp_bootstrap._managed_command_in_venv(
        mcp_bootstrap.MANAGED_LEAN_MCP_SPECS["lean-lsp"], old
    )
    command.parent.mkdir(parents=True)
    command.write_text("original active server")
    config.write_text(
        f"# preserve\nmcp_servers:\n  lean-lsp:\n    managed: true\n    command: {command}\n"
    )
    before = config.read_bytes()

    def fail(*args, **kwargs):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(mcp_bootstrap, "_install_into_managed_venv", _fake_managed_install)
    monkeypatch.setattr(
        mcp_bootstrap,
        "_validate_managed_mcp_environment",
        lambda *args, **kwargs: {"console_script": True, "fastmcp": True, "initialized": True},
    )
    if failure == "install":
        monkeypatch.setattr(mcp_bootstrap, "_install_into_managed_venv", fail)
    elif failure == "validation":
        monkeypatch.setattr(mcp_bootstrap, "_validate_managed_mcp_environment", fail)
    else:
        monkeypatch.setattr(mcp_install_generation.os, "replace", fail)

    with pytest.raises(RuntimeError, match="injected"):
        bootstrap_lean_mcp(home=home)

    assert config.read_bytes() == before
    assert command.read_text() == "original active server"
    assert mcp_bootstrap.managed_mcp_venv_dir("lean-lsp", home) == old
    assert list((home / "mcp/venvs").iterdir()) == [old.parent]


def test_selection_preserves_latest_comments_env_disabled_and_unrelated_entries(
    monkeypatch, tmp_path
):
    """Read user changes made during installation immediately before atomic selection."""
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("# initial\nmodel:\n  default: original\n")

    def install(venv_dir, install_spec, **kwargs):
        _fake_managed_install(venv_dir, install_spec, **kwargs)
        config.write_text(
            "# latest user comment\nmodel:\n  default: latest\nmcp_servers:\n"
            "  custom:\n    command: /custom/server\n"
            "  lean-lsp:\n    enabled: false\n    env:\n      CUSTOM: user-value\n"
        )

    monkeypatch.setattr(mcp_bootstrap, "_install_into_managed_venv", install)
    monkeypatch.setattr(
        mcp_bootstrap,
        "_validate_managed_mcp_environment",
        lambda *args, **kwargs: {"console_script": True, "fastmcp": True, "initialized": True},
    )

    result = bootstrap_lean_mcp(home=home)

    rendered = config.read_text()
    assert result["success"]
    assert "# latest user comment" in rendered
    assert "default: latest" in rendered
    assert "command: /custom/server" in rendered
    assert "enabled: false" in rendered
    assert "CUSTOM: user-value" in rendered


def test_failure_after_config_publication_never_removes_selected_generation(monkeypatch, tmp_path):
    """An error after an atomic switch cannot justify deleting its selected backends."""
    home = tmp_path / "home"
    home.mkdir()
    original = mcp_bootstrap.write_managed_mcp_config
    monkeypatch.setattr(mcp_bootstrap, "_install_into_managed_venv", _fake_managed_install)
    monkeypatch.setattr(
        mcp_bootstrap,
        "_validate_managed_mcp_environment",
        lambda *args, **kwargs: {"console_script": True, "fastmcp": True, "initialized": True},
    )

    def published_then_failed(home, *, venv_dirs):
        original(home, venv_dirs=venv_dirs)
        raise RuntimeError("failure after selection")

    monkeypatch.setattr(mcp_bootstrap, "write_managed_mcp_config", published_then_failed)

    with pytest.raises(RuntimeError, match="after selection"):
        bootstrap_lean_mcp(home=home)

    for name in mcp_bootstrap.MANAGED_LEAN_MCP_SPECS:
        assert managed_mcp_command_path(name, home).is_file()


@pytest.mark.parametrize("failure", ["timeout", "uncertain-cleanup"])
def test_uncertain_validation_keeps_marked_candidate_and_old_selection(
    monkeypatch, tmp_path, failure
):
    """Keep potentially live candidate paths instead of removing them after incomplete shutdown."""
    home = tmp_path / "home"
    home.mkdir()
    write_managed_mcp_config(home)
    before = (home / "config.yaml").read_bytes()
    monkeypatch.setattr(mcp_bootstrap, "_install_into_managed_venv", _fake_managed_install)

    def fail(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("initialize", 45)
        raise mcp_install_generation.MCPValidationCleanupUncertain("shutdown not confirmed")

    monkeypatch.setattr(mcp_bootstrap, "_validate_managed_mcp_environment", fail)
    with pytest.raises(
        (subprocess.TimeoutExpired, mcp_install_generation.MCPValidationCleanupUncertain)
    ):
        bootstrap_lean_mcp(home=home)

    assert (home / "config.yaml").read_bytes() == before
    generations = list((home / "mcp/venvs").glob("gen-*"))
    assert len(generations) == 1
    marker = json.loads((generations[0] / ".leanflow-mcp-generation.json").read_text())
    assert marker["owner"] == "leanflow-mcp-bootstrap"
    assert marker["state"] == "failed-cleanup-uncertain"


def test_installer_refuses_to_upgrade_existing_environment(monkeypatch, tmp_path):
    """An active or previously published path is immutable even when repair is requested."""
    existing = tmp_path / "environment"
    existing.mkdir()
    marker = existing / "loaded.py"
    marker.write_text("original")
    monkeypatch.setattr(mcp_bootstrap.subprocess, "run", lambda *a, **kw: pytest.fail("pip ran"))

    with pytest.raises(RuntimeError, match="existing"):
        mcp_bootstrap._install_into_managed_venv(existing, "server")

    assert marker.read_text() == "original"


@pytest.mark.parametrize("case", ["outside", "wrong-script", "unmanaged", "symlink-escape"])
def test_selected_environment_resolution_rejects_unsafe_or_unmanaged_paths(tmp_path, case):
    """Resolve only matching managed console commands contained in the MCP installation root."""
    home = tmp_path / "home"
    home.mkdir()
    spec = mcp_bootstrap.MANAGED_LEAN_MCP_SPECS["lean-lsp"]
    legacy = home / "mcp/venvs/lean-lsp"
    candidate = home / "mcp/venvs/generation/lean-lsp"
    managed = True
    if case == "outside":
        candidate = tmp_path / "outside/lean-lsp"
    elif case == "unmanaged":
        managed = False
    elif case == "symlink-escape":
        outside = tmp_path / "outside/lean-lsp"
        outside.mkdir(parents=True)
        candidate.parent.mkdir(parents=True)
        candidate.symlink_to(outside, target_is_directory=True)
    command = mcp_bootstrap._managed_command_in_venv(spec, candidate)
    if case == "wrong-script":
        command = command.with_name("different-server")
    (home / "config.yaml").write_text(
        f"mcp_servers:\n  lean-lsp:\n    managed: {str(managed).lower()}\n    command: {command}\n"
    )

    assert mcp_bootstrap.managed_mcp_venv_dir("lean-lsp", home) == legacy


def test_explicit_selection_rejects_command_symlink_escape_without_changing_config(tmp_path):
    """Explicit target maps cannot select a nominal managed command outside its environment."""
    home = tmp_path / "home"
    home.mkdir()
    write_managed_mcp_config(home)
    before = (home / "config.yaml").read_bytes()
    spec = mcp_bootstrap.MANAGED_LEAN_MCP_SPECS["lean-lsp"]
    candidate = home / "mcp/venvs/generation/lean-lsp"
    command = mcp_bootstrap._managed_command_in_venv(spec, candidate)
    command.parent.mkdir(parents=True)
    outside = tmp_path / "outside-command"
    outside.write_text("outside")
    command.symlink_to(outside)

    with pytest.raises(ValueError, match="escapes"):
        write_managed_mcp_config(home, venv_dirs={"lean-lsp": candidate})

    assert (home / "config.yaml").read_bytes() == before


def test_atomic_config_replace_failure_never_truncates_existing_document(monkeypatch, tmp_path):
    """A failed final replace leaves the old complete bytes and cleans its temporary file."""
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("# stable\nmodel:\n  default: stable\n")
    before = config.read_bytes()

    def fail(*args, **kwargs):
        assert config.read_bytes() == before
        raise OSError("replace failed")

    monkeypatch.setattr(mcp_install_generation.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        write_managed_mcp_config(home)

    assert config.read_bytes() == before
    assert list(home.glob(".config.yaml-*")) == []


@pytest.mark.skipif(os.name == "nt", reason="the local console fixture uses a POSIX shebang")
def test_managed_candidate_validation_performs_real_protocol_initialization(tmp_path):
    """Initialize a tiny local MCP server and list tools using the candidate interpreter."""
    pytest.importorskip("mcp")
    home = tmp_path / "home"
    (home / "mcp").mkdir(parents=True)
    spec = mcp_bootstrap.MANAGED_LEAN_MCP_SPECS["lean-lsp"]
    candidate = home / "mcp/venvs/generation/lean-lsp"
    command = mcp_bootstrap._managed_command_in_venv(spec, candidate)
    command.parent.mkdir(parents=True)
    python = command.parent / "python"
    python.symlink_to(sys.executable)
    command.write_text(
        f"#!{sys.executable}\nfrom mcp.server.fastmcp import FastMCP\n"
        "server = FastMCP('validation-test')\n"
        "@server.tool()\ndef test_tool() -> str:\n    return 'ok'\n"
        "server.run(transport='stdio')\n"
    )
    command.chmod(0o755)

    validation = mcp_bootstrap._validate_managed_mcp_environment(spec, candidate, home)

    assert validation == {"console_script": True, "fastmcp": True, "initialized": True}
    _fake_package_metadata(candidate, spec)
    mcp_bootstrap.write_managed_mcp_receipt(
        candidate,
        expected_fingerprint=mcp_bootstrap._managed_mcp_fingerprint(spec),
        packages=mcp_bootstrap._managed_mcp_packages(spec),
        installed_files=mcp_bootstrap._managed_mcp_installed_files(spec, candidate),
        arguments=spec.args,
        validation=validation,
    )
    write_managed_mcp_config(home, venv_dirs={spec.name: candidate})
    before = (candidate / ".leanflow-mcp-receipt.json").read_bytes()
    assert managed_mcp_server_status(home)[spec.name]["healthy"] is True
    assert managed_mcp_server_status(home)[spec.name]["healthy"] is True
    assert (candidate / ".leanflow-mcp-receipt.json").read_bytes() == before


def _bootstrap_fake_generation(monkeypatch, home):
    """Publish a validated receipt fixture through the real generation transaction."""
    monkeypatch.setattr(mcp_bootstrap, "_install_into_managed_venv", _fake_managed_install)
    monkeypatch.setattr(
        mcp_bootstrap,
        "_validate_managed_mcp_environment",
        lambda *args, **kwargs: {"console_script": True, "fastmcp": True, "initialized": True},
    )
    return bootstrap_lean_mcp(home=home)


@pytest.mark.parametrize(
    "change", ["specification", "patch", "console", "metadata", "args", "missing"]
)
def test_receipt_rejects_changed_generation_and_repair_preserves_old_paths(
    monkeypatch, tmp_path, change
):
    """A current receipt reuses a backend; changed provenance demands a fresh generation."""
    home = tmp_path / "home"
    _bootstrap_fake_generation(monkeypatch, home)
    assert all(entry["healthy"] for entry in managed_mcp_server_status(home).values())
    old_command = managed_mcp_command_path("lean-lsp", home)
    candidate = old_command.parent.parent
    if change == "specification":
        spec = mcp_bootstrap.MANAGED_LEAN_MCP_SPECS["lean-lsp"]
        monkeypatch.setitem(
            mcp_bootstrap.MANAGED_LEAN_MCP_SPECS,
            spec.name,
            replace(spec, install_spec="lean-lsp-mcp==0.26.2"),
        )
    elif change == "patch":
        fingerprint = mcp_bootstrap._managed_mcp_fingerprint
        monkeypatch.setattr(
            mcp_bootstrap, "_managed_mcp_fingerprint", lambda spec: fingerprint(spec) + "changed"
        )
    elif change == "console":
        old_command.write_text("#!/bin/sh\n# changed content\nexit 0\n")
    elif change == "metadata":
        metadata = next(
            candidate.glob("lib/python*/site-packages/lean-lsp-mcp-*.dist-info/METADATA")
        )
        metadata.write_text("Name: lean-lsp-mcp\nVersion: 9.9\n")
    elif change == "args":
        config = home / "config.yaml"
        config.write_text(
            config.read_text()
            .replace("  lean-lsp:\n", "  lean-lsp:\n    args: [custom]\n", 1)
            .replace("    args: []\n", "", 1)
        )
    else:
        (candidate / ".leanflow-mcp-receipt.json").unlink()
    status = managed_mcp_server_status(home)["lean-lsp"]
    assert status["installed"] is True
    assert status["healthy"] is False
    assert status["receipt"]["current"] is False
    preserved = old_command.read_bytes()
    _bootstrap_fake_generation(monkeypatch, home)
    assert managed_mcp_server_status(home)["lean-lsp"]["healthy"] is True
    assert managed_mcp_command_path("lean-lsp", home) != old_command
    assert old_command.read_bytes() == preserved


def test_customized_arguments_are_preserved_and_initialized(monkeypatch, tmp_path):
    """Validate the exact custom command arguments selected in the new config."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "mcp_servers:\n  lean-explore:\n    args: [mcp, serve, --backend, custom]\n"
    )
    observed = {}
    monkeypatch.setattr(mcp_bootstrap, "_install_into_managed_venv", _fake_managed_install)

    def validate(name, venv, command, *, args, env, cwd):
        observed[name] = args
        return {"console_script": True, "fastmcp": True, "initialized": True}

    monkeypatch.setattr(mcp_bootstrap, "validate_managed_mcp_candidate", validate)
    bootstrap_lean_mcp(home=home)
    assert observed["lean-explore"] == ("mcp", "serve", "--backend", "custom")
    _yaml, document = mcp_bootstrap._load_bootstrap_document(home / "config.yaml")
    assert document["mcp_servers"]["lean-explore"]["args"] == list(observed["lean-explore"])
    assert managed_mcp_server_status(home)["lean-explore"]["healthy"] is True
    assert (
        mcp_bootstrap.MANAGED_LEAN_MCP_SPECS["lean-explore"].install_spec == "lean-explore==1.3.0"
    )


def test_configured_arguments_changed_during_validation_do_not_publish(monkeypatch, tmp_path):
    """Never select arguments that have not passed the completed initialization."""
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("model:\n  default: stable\n")
    monkeypatch.setattr(mcp_bootstrap, "_install_into_managed_venv", _fake_managed_install)

    def validate(spec, venv, home_path, **kwargs):
        if spec.name == "lean-explore":
            config.write_text("mcp_servers:\n  lean-lsp:\n    args: [changed-after-validation]\n")
        return {"console_script": True, "fastmcp": True, "initialized": True}

    monkeypatch.setattr(mcp_bootstrap, "_validate_managed_mcp_environment", validate)
    with pytest.raises(ValueError, match="changed during initialization"):
        bootstrap_lean_mcp(home=home)
    assert "command:" not in config.read_text()
    assert list((home / "mcp/venvs").iterdir()) == []


def test_config_symlink_preserves_link_and_atomically_updates_regular_target(monkeypatch, tmp_path):
    """Retain a dotfile-managed link and its existing comments when selecting a generation."""
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "dotfiles/settings.yaml"
    target.parent.mkdir()
    target.write_text("# dotfile comment\nmodel:\n  default: stable\n")
    config = home / "config.yaml"
    config.symlink_to(target)
    _bootstrap_fake_generation(monkeypatch, home)
    assert config.is_symlink()
    assert config.resolve() == target
    assert "# dotfile comment" in target.read_text()
    assert all(entry["healthy"] for entry in managed_mcp_server_status(home).values())


@pytest.mark.parametrize("target_kind", ["missing", "directory"])
def test_invalid_config_symlink_rejects_before_environment_install(
    monkeypatch, tmp_path, target_kind
):
    """Reject ambiguous dotfile targets before creating any managed backend generation."""
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "invalid-target"
    if target_kind == "directory":
        target.mkdir()
    config = home / "config.yaml"
    config.symlink_to(target)
    monkeypatch.setattr(
        mcp_bootstrap, "_install_into_managed_venv", lambda *a, **kw: pytest.fail("installed")
    )
    with pytest.raises((ValueError, FileNotFoundError)):
        bootstrap_lean_mcp(home=home)
    assert config.is_symlink()
    assert not (home / "mcp/venvs").exists()


def test_candidate_validation_rejects_fastmcp_failure_before_server_start(monkeypatch, tmp_path):
    """A present console command cannot stand in for the required server API."""
    spec = mcp_bootstrap.MANAGED_LEAN_MCP_SPECS["lean-lsp"]
    candidate = tmp_path / "candidate"
    command = mcp_bootstrap._managed_command_in_venv(spec, candidate)
    command.parent.mkdir(parents=True)
    command.write_text("console marker")
    command.chmod(0o755)
    monkeypatch.setattr(mcp_install_generation, "mcp_protocol_ready", lambda root: False)
    monkeypatch.setattr(
        mcp_install_generation.subprocess, "run", lambda *a, **kw: pytest.fail("server started")
    )

    with pytest.raises(RuntimeError, match="FastMCP import failed"):
        mcp_install_generation.validate_managed_mcp_candidate(
            spec.name, candidate, command, args=spec.args, env={}, cwd=tmp_path
        )


@pytest.mark.parametrize("timeout", [False, True])
def test_candidate_validation_reports_bounded_redacted_stderr_tail(monkeypatch, tmp_path, timeout):
    """Include the actionable final failure without credentials or unbounded log output."""
    command = tmp_path / "bin/server"
    command.parent.mkdir()
    command.write_text("fixture")
    command.chmod(0o755)
    monkeypatch.setattr(mcp_install_generation, "mcp_protocol_ready", lambda root: True)
    diagnostic = (
        "early diagnostic "
        + "x" * 12000
        + "\nModuleNotFoundError: tested-module\nsecret-example-value\n"
    )

    def run(*args, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired("candidate", 45, stderr=diagnostic.encode())
        return subprocess.CompletedProcess(args, 1, "", diagnostic)

    monkeypatch.setattr(mcp_install_generation.subprocess, "run", run)
    with pytest.raises(mcp_install_generation.MCPValidationCleanupUncertain) as failure:
        mcp_install_generation.validate_managed_mcp_candidate(
            "fixture",
            tmp_path,
            command,
            args=(),
            env={"OPENAI_API_KEY": "secret-example-value"},
            cwd=tmp_path,
        )
    message = str(failure.value)
    assert "ModuleNotFoundError: tested-module" in message
    assert "early diagnostic" not in message
    assert "secret-example-value" not in message
    assert "[REDACTED]" in message
    assert len(message) < 2300


@pytest.mark.parametrize("server", ["lean-lsp", "lean-proof-auto"])
def test_failed_required_source_patch_preserves_selected_generation(monkeypatch, tmp_path, server):
    """A successful protocol handshake cannot excuse a missing required managed patch."""
    home = tmp_path / "home"
    _bootstrap_fake_generation(monkeypatch, home)
    before = (home / "config.yaml").read_bytes()
    old_command = managed_mcp_command_path(server, home)
    if server == "lean-lsp":
        monkeypatch.setattr(
            mcp_bootstrap, "_apply_lean_lsp_patches", lambda venv: {"lifecycle": False}
        )
    else:
        monkeypatch.setattr(
            mcp_bootstrap, "_patch_lean_proof_auto_stdio_logging", lambda venv: False
        )
    with pytest.raises(RuntimeError, match="Required managed MCP patches failed"):
        bootstrap_lean_mcp(home=home)
    assert (home / "config.yaml").read_bytes() == before
    assert old_command.is_file()
    assert len(list((home / "mcp/venvs").iterdir())) == 1


def test_proof_auto_pins_observed_source_commit_and_patch_dependencies():
    spec = mcp_bootstrap.MANAGED_LEAN_MCP_SPECS["lean-proof-auto"]
    assert spec.install_spec.endswith("@8d829ecf8e25ac7597fd5cc41b043a40aa404ec1")
    assert spec.extra_install_specs == ("lean-interact==0.11.5", "PyYAML==6.0.3")


def test_install_lock_serializes_another_process(tmp_path):
    """A second bootstrap process cannot enter while the first owns installation."""
    home = tmp_path / "home"
    script = (
        "from pathlib import Path\n"
        "from leanflow_cli.cli.mcp_install_generation import managed_mcp_install_lock\n"
        "import sys\nprint('waiting', flush=True)\n"
        "with managed_mcp_install_lock(Path(sys.argv[1])):\n print('entered', flush=True)\n"
    )
    with mcp_install_generation.managed_mcp_install_lock(home):
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(home)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "waiting"
        with pytest.raises(subprocess.TimeoutExpired):
            process.communicate(timeout=0.05)
    output, error = process.communicate(timeout=5)
    assert process.returncode == 0, error
    assert "entered" in output


def _make_loogle_project(tmp_path, toolchain):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "lean-toolchain").write_text(toolchain + "\n", encoding="utf-8")
    return proj


def test_ensure_local_loogle_builds_against_project_toolchain(tmp_path, monkeypatch):
    # The decisive behavior: Loogle must be (re)pinned to the PROJECT toolchain and built,
    # so its toolchain matches the project and local Loogle stops being "incompatible".
    proj = _make_loogle_project(tmp_path, "leanprover/lean4:v9.9-rc7")
    cache = tmp_path / "cache" / "loogle"
    monkeypatch.setattr(loogle_local, "local_loogle_supported", lambda: True)
    monkeypatch.setattr(loogle_local.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(loogle_local, "loogle_cache_dir_for_project", lambda home, proj: cache)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        repo = cache / "repo"
        if cmd[:2] == ["git", "clone"]:
            repo.mkdir(parents=True, exist_ok=True)
            (repo / "lakefile.toml").write_text("", encoding="utf-8")
        elif cmd[:2] == ["lake", "build"]:
            binp = repo / ".lake" / "build" / "bin"
            binp.mkdir(parents=True, exist_ok=True)
            (binp / "loogle").write_text("#!/bin/sh\n", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(loogle_local.subprocess, "run", fake_run)

    res = loogle_local.ensure_local_loogle_for_project(proj, home=tmp_path)
    assert res["ok"] is True
    assert res["action"] == "built"
    assert (cache / "repo" / "lean-toolchain").read_text().strip() == "leanprover/lean4:v9.9-rc7"
    assert any(c[:2] == ["git", "clone"] for c in calls)
    assert any(c[:2] == ["lake", "build"] for c in calls)


def test_ensure_local_loogle_noop_when_already_matching(tmp_path, monkeypatch):
    proj = _make_loogle_project(tmp_path, "tc:v1")
    cache = tmp_path / "cache" / "loogle"
    binp = cache / "repo" / ".lake" / "build" / "bin"
    binp.mkdir(parents=True)
    (binp / "loogle").write_text("x", encoding="utf-8")
    (cache / "repo" / "lean-toolchain").write_text("tc:v1\n", encoding="utf-8")
    monkeypatch.setattr(loogle_local, "local_loogle_supported", lambda: True)
    monkeypatch.setattr(loogle_local.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(loogle_local, "loogle_cache_dir_for_project", lambda home, proj: cache)

    def boom(*a, **k):
        raise AssertionError("must not rebuild when Loogle already matches the project")

    monkeypatch.setattr(loogle_local.subprocess, "run", boom)
    res = loogle_local.ensure_local_loogle_for_project(proj, home=tmp_path)
    assert res["ok"] is True
    assert res["action"] == "already-built"


def test_local_loogle_needs_build_and_async_gate(tmp_path, monkeypatch):
    proj = _make_loogle_project(tmp_path, "tc:v2")
    cache = tmp_path / "cache" / "loogle"
    monkeypatch.setattr(loogle_local, "local_loogle_supported", lambda: True)
    monkeypatch.setattr(loogle_local.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        loogle_local,
        "_lean_lsp_env_from_home",
        lambda home: {"LEAN_LOOGLE_LOCAL": "true", "LEAN_LOOGLE_CACHE_DIR": str(cache)},
    )
    # Binary missing -> a build is needed.
    assert loogle_local.local_loogle_needs_build(proj, home=tmp_path) is True

    # The async launcher must NOT spawn a process when no build is needed.
    monkeypatch.setattr(loogle_local, "local_loogle_needs_build", lambda *a, **k: False)

    def no_popen(*a, **k):
        raise AssertionError("Popen must not be called when no build is needed")

    monkeypatch.setattr(loogle_local.subprocess, "Popen", no_popen)
    assert loogle_local.ensure_local_loogle_for_project_async(proj, home=tmp_path) is False
