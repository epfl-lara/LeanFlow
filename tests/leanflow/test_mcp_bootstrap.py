from __future__ import annotations

import subprocess
from pathlib import Path

from leanflow_cli.cli import loogle_local, mcp_bootstrap
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
        "leanflow_cli.cli.mcp_bootstrap._install_into_managed_venv",
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
