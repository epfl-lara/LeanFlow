"""Managed Loogle index-flag compatibility: client patch, status, repair, and doctor.

The readiness check found the managed lean-lsp-mcp client sending ``--write-index``
to a Loogle build that only accepts ``--index-mode``/``--index-file``. These tests
pin the negotiation patch, the build record ``loogle_local`` writes, the
compatibility status behind ``leanflow doctor``, and the offline
``leanflow mcp repair`` path that applies the patch.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.cli import loogle_index_compat as compat
from leanflow_cli.cli import loogle_local, mcp_bootstrap
from leanflow_cli.cli.doctor import run_doctor

#: The parts of lean-lsp-mcp 0.26.1 ``loogle.py`` the patch rewrites, with ``start``
#: reduced to returning the command it would spawn.
_CLIENT_SOURCE = """
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class LoogleManager:
    def __init__(self, cache_dir, usage):
        self.cache_dir = Path(cache_dir)
        self.repo_dir = self.cache_dir / "repo"
        self.index_dir = self.cache_dir / "index"
        self._extra_paths = []
        self.usage = usage
        self.commands = []

    @property
    def binary_path(self):
        return self.repo_dir / ".lake" / "build" / "bin" / "loogle"

    @property
    def is_installed(self):
        return True

    def _run(self, cmd, timeout=300, cwd=None):
        self.commands.append(list(cmd))
        if cmd[1:] == ["--help"]:
            return SimpleNamespace(returncode=0, stdout=self.usage, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _get_index_path(self):
        return self.index_dir / "mathlib-abc.idx"

    def _cleanup_old_indices(self):
        pass

    def _build_index(self) -> Path | None:
        index_path = self._get_index_path()
        if index_path.exists():
            return index_path
        if not self.is_installed:
            return None
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_old_indices()

        # Build command with extra paths
        cmd = [str(self.binary_path), "--write-index", str(index_path), "--json"]
        for path in self._extra_paths:
            cmd.extend(["--path", str(path)])
        cmd.append("")  # Empty query for index building

        try:
            self._run(cmd, timeout=600)
            return index_path if index_path.exists() else None
        except Exception as e:
            logger.error(f"Index build error: {e}")
            return None

    def start(self):
        cmd = [str(self.binary_path), "--json", "--interactive"]
        if (idx := self._get_index_path()).exists():
            cmd.extend(["--read-index", str(idx)])
        # Add extra paths for runtime search (in case not all are indexed)
        for path in self._extra_paths:
            cmd.extend(["--path", str(path)])
        return cmd
"""

_MODERN_USAGE = "  --index-mode MODE   how to manage the on-disk index\n  --index-file PATH\n"
_LEGACY_USAGE = "  --write-index FILE    write the index\n  --read-index FILE\n"


def _write_client(venv: Path, source: str = _CLIENT_SOURCE) -> Path:
    package = venv / "lib" / "python3.12" / "site-packages" / "lean_lsp_mcp"
    package.mkdir(parents=True, exist_ok=True)
    path = package / "loogle.py"
    path.write_text(source, encoding="utf-8")
    return path


def _load_manager_class(path: Path):
    namespace: dict = {"SimpleNamespace": SimpleNamespace}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace["LoogleManager"]


def test_patch_index_flags_is_valid_and_idempotent(tmp_path):
    venv = tmp_path / "venv"
    path = _write_client(venv)

    assert compat.patch_lean_lsp_loogle_index_flags(venv) is True
    patched = path.read_text(encoding="utf-8")
    ast.parse(patched)
    assert "_leanflow_index_dialect" in patched
    assert '"--index-file"' in patched and '"write"' in patched
    assert '"--index-mode", "use", "--index-file"' in patched
    assert compat.patch_lean_lsp_loogle_index_flags(venv) is True
    assert path.read_text(encoding="utf-8") == patched


def test_patch_refuses_an_unknown_client_layout(tmp_path):
    venv = tmp_path / "venv"
    _write_client(venv, "class LoogleManager:\n    pass\n")

    assert compat.patch_lean_lsp_loogle_index_flags(venv) is False


@pytest.mark.parametrize(
    ("usage", "expected_build", "expected_start"),
    [
        (
            _MODERN_USAGE,
            ["--index-mode", "write", "--index-file"],
            ["--index-mode", "use", "--index-file"],
        ),
        (_LEGACY_USAGE, ["--write-index"], []),
        ("USAGE: loogle [OPTIONS] [QUERY]\n", None, []),
    ],
)
def test_patched_client_negotiates_flags_against_the_binary(
    tmp_path, usage, expected_build, expected_start
):
    venv = tmp_path / "venv"
    path = _write_client(venv)
    assert compat.patch_lean_lsp_loogle_index_flags(venv) is True
    manager_class = _load_manager_class(path)
    manager = manager_class(tmp_path / "cache", usage)

    manager._build_index()
    start_cmd = manager.start()

    build_cmds = [cmd for cmd in manager.commands if cmd[1:] != ["--help"]]
    probe_cmds = [cmd for cmd in manager.commands if cmd[1:] == ["--help"]]
    assert len(probe_cmds) == 1, "the dialect probe must be cached per manager"
    if expected_build is None:
        assert build_cmds == []
    else:
        (build_cmd,) = build_cmds
        assert build_cmd[1 : 1 + len(expected_build)] == expected_build
        if expected_build[0] == "--write-index":
            assert build_cmd[-1] == ""
        else:
            assert "" not in build_cmd
    assert start_cmd[:3] == [str(manager.binary_path), "--json", "--interactive"]
    assert start_cmd[3 : 3 + len(expected_start)] == expected_start
    if expected_start:
        assert start_cmd[6].endswith("mathlib-abc.idx")
        assert manager.index_dir.is_dir()


def test_legacy_start_reads_an_existing_index_only(tmp_path):
    venv = tmp_path / "venv"
    path = _write_client(venv)
    compat.patch_lean_lsp_loogle_index_flags(venv)
    manager = _load_manager_class(path)(tmp_path / "cache", _LEGACY_USAGE)
    manager.index_dir.mkdir(parents=True)
    manager._get_index_path().write_text("", encoding="utf-8")

    assert manager.start()[3:5] == ["--read-index", str(manager._get_index_path())]


_REAL_CLIENTS = sorted(
    (Path.home() / ".leanflow" / "mcp" / "venvs" / "lean-lsp").glob(
        "lib/python*/site-packages/lean_lsp_mcp/loogle.py"
    )
)


@pytest.mark.skipif(not _REAL_CLIENTS, reason="managed lean-lsp-mcp is not installed here")
def test_installed_client_accepts_the_index_flags_patch(tmp_path):
    """Patch a copy of the real managed client; the installed file is never touched."""
    venv = tmp_path / "venv"
    copy = _write_client(venv, _REAL_CLIENTS[0].read_text(encoding="utf-8"))

    assert compat.patch_lean_lsp_loogle_index_flags(venv) is True
    patched = copy.read_text(encoding="utf-8")
    ast.parse(patched)
    assert compat.client_index_flags(patched) == "negotiated"
    assert compat.patch_lean_lsp_loogle_index_flags(venv) is True


def test_binary_dialect_probe_classifies_usage_text(tmp_path, monkeypatch):
    binary = tmp_path / "loogle"
    assert compat.loogle_binary_index_dialect(binary) == "missing"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    responses = iter([_MODERN_USAGE, _LEGACY_USAGE, "usage only"])
    monkeypatch.setattr(
        compat.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, next(responses), ""),
    )

    assert compat.loogle_binary_index_dialect(binary) == "index-mode"
    assert compat.loogle_binary_index_dialect(binary) == "write-index"
    assert compat.loogle_binary_index_dialect(binary) == "none"

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(compat.subprocess, "run", boom)
    assert compat.loogle_binary_index_dialect(binary).startswith("error")


@pytest.mark.parametrize(
    ("client", "binary", "expected"),
    [
        ("legacy-write-index", "index-mode", False),
        ("legacy-write-index", "write-index", True),
        ("negotiated", "index-mode", True),
        ("negotiated", "write-index", True),
        ("negotiated", "none", True),
        ("index-mode", "write-index", False),
        ("negotiated", "missing", None),
        ("unknown", "index-mode", None),
    ],
)
def test_index_flag_compatibility_matrix(client, binary, expected):
    assert compat.index_flags_compatible(client, binary) is expected


def _make_home_with_client_and_build(tmp_path: Path, *, source_flag: str) -> tuple[Path, Path]:
    home = tmp_path / "home"
    _write_client(mcp_bootstrap.managed_mcp_venv_dir("lean-lsp", home))
    cache = mcp_bootstrap.managed_loogle_cache_dir(home, toolchain="leanprover/lean4:v4.33.1")
    repo = cache / "repo"
    (repo / ".lake" / "build" / "bin").mkdir(parents=True)
    (repo / ".lake" / "build" / "bin" / "loogle").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n", encoding="utf-8")
    (repo / "LoogleMain.lean").write_text(
        f'def lakeLongOption\n| "{source_flag}" => pure ()\n', encoding="utf-8"
    )
    return home, cache


def test_client_status_detects_the_observed_write_index_mismatch(tmp_path):
    home, cache = _make_home_with_client_and_build(tmp_path, source_flag="--index-mode")

    status = compat.managed_loogle_client_status(home, cache_dir=cache)

    assert status["client_found"] is True
    assert status["client_index_flags"] == "legacy-write-index"
    assert status["binary_index_flags"] == "index-mode"
    assert status["binary_index_flags_source"] == "source-scan"
    assert status["compatible"] is False
    assert "--write-index" in status["detail"] and "--index-mode" in status["detail"]

    compat.patch_lean_lsp_loogle_index_flags(mcp_bootstrap.managed_mcp_venv_dir("lean-lsp", home))
    repaired = compat.managed_loogle_client_status(home, cache_dir=cache)
    assert repaired["client_patched"] is True
    assert repaired["compatible"] is True


def test_client_status_resolves_the_project_cache_dir_when_not_given(tmp_path):
    home, cache = _make_home_with_client_and_build(tmp_path, source_flag="--write-index")
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n", encoding="utf-8")

    status = compat.managed_loogle_client_status(home, project_root=project)

    assert status["binary_path"] == str(cache / "repo" / ".lake" / "build" / "bin" / "loogle")
    assert status["binary_index_flags"] == "write-index"
    assert status["compatible"] is True
    assert status["detail"] == "client and Loogle both use --write-index"


def test_client_status_prefers_the_build_record_for_the_same_toolchain(tmp_path):
    home, cache = _make_home_with_client_and_build(tmp_path, source_flag="--index-mode")
    (cache / compat.LOOGLE_BUILD_RECORD).write_text(
        json.dumps({"toolchain": "leanprover/lean4:v4.33.1", "index_dialect": "write-index"}),
        encoding="utf-8",
    )

    status = compat.managed_loogle_client_status(home, cache_dir=cache)

    assert status["binary_index_flags"] == "write-index"
    assert status["binary_index_flags_source"] == "build-record"
    assert status["compatible"] is True


def test_power_status_reports_client_incompatibility(tmp_path):
    home, _cache = _make_home_with_client_and_build(tmp_path, source_flag="--index-mode")
    (home / "config.yaml").write_text(
        "mcp_servers:\n  lean-lsp:\n    env:\n      LEAN_LOOGLE_LOCAL: 'true'\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n", encoding="utf-8")

    power = mcp_bootstrap.managed_mcp_power_status(home, project_root=project)

    assert power["loogle_toolchain_compatible"] is True
    assert power["loogle_index_flags_compatible"] is False
    assert power["loogle_local_status"] == "client-incompatible"
    assert power["loogle_local_ready"] is False
    assert power["loogle_client"]["client_index_flags"] == "legacy-write-index"

    compat.patch_lean_lsp_loogle_index_flags(mcp_bootstrap.managed_mcp_venv_dir("lean-lsp", home))
    repaired = mcp_bootstrap.managed_mcp_power_status(home, project_root=project)
    assert repaired["loogle_local_status"] == "ready"
    assert repaired["loogle_index_flags_compatible"] is True


def test_ensure_local_loogle_records_the_built_binary_dialect(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n", encoding="utf-8")
    cache = tmp_path / "cache" / "loogle"
    monkeypatch.setattr(loogle_local, "local_loogle_supported", lambda: True)
    monkeypatch.setattr(loogle_local.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(loogle_local, "loogle_cache_dir_for_project", lambda home, proj: cache)

    def fake_run(cmd, **kwargs):
        repo = cache / "repo"
        if cmd[:2] == ["git", "clone"]:
            repo.mkdir(parents=True, exist_ok=True)
            (repo / "lakefile.toml").write_text("", encoding="utf-8")
        elif cmd[:2] == ["lake", "build"]:
            binp = repo / ".lake" / "build" / "bin"
            binp.mkdir(parents=True, exist_ok=True)
            (binp / "loogle").write_text("#!/bin/sh\n", encoding="utf-8")
        elif cmd[1:] == ["--help"]:
            return subprocess.CompletedProcess(cmd, 0, _MODERN_USAGE, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(loogle_local.subprocess, "run", fake_run)

    built = loogle_local.ensure_local_loogle_for_project(project, home=tmp_path)
    assert built["action"] == "built"
    assert built["index_dialect"] == "index-mode"
    record = json.loads((cache / compat.LOOGLE_BUILD_RECORD).read_text(encoding="utf-8"))
    assert record["toolchain"] == "leanprover/lean4:v4.33.1"
    assert record["index_dialect"] == "index-mode"

    def boom(*args, **kwargs):
        raise AssertionError("an existing matching build must not be probed or rebuilt")

    monkeypatch.setattr(loogle_local.subprocess, "run", boom)
    again = loogle_local.ensure_local_loogle_for_project(project, home=tmp_path)
    assert again["action"] == "already-built"
    assert again["index_dialect"] == "index-mode"


def test_repair_reapplies_patches_without_reinstalling(tmp_path):
    home, _cache = _make_home_with_client_and_build(tmp_path, source_flag="--index-mode")

    payload = mcp_bootstrap.repair_managed_mcp_patches(home)

    assert payload["success"] is True
    lean_lsp = payload["patches"]["lean-lsp"]
    assert lean_lsp["installed"] is True
    assert lean_lsp["index_flags"] is True
    assert payload["patches"]["lean-proof-auto"] == {"installed": False}
    client = compat.managed_loogle_client_status(home)
    assert client["client_patched"] is True


def test_main_mcp_repair_prints_applied_patches(monkeypatch, tmp_path, capsys):
    from leanflow_cli.main import main

    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "leanflow_cli.main.repair_managed_mcp_patches",
        lambda: {
            "success": True,
            "home": "/tmp/home",
            "patches": {
                "lean-lsp": {"installed": True, "index_flags": True, "lifecycle": False},
                "lean-proof-auto": {"installed": False},
            },
            "power_modes": {
                "loogle_local_status": "ready",
                "loogle_client": {"client_found": True, "detail": "client negotiates index flags"},
            },
        },
    )

    assert main(["mcp", "repair"]) == 0
    output = capsys.readouterr().out
    assert "Managed Lean MCP patches" in output
    assert "index_flags=ok" in output and "lifecycle=skipped" in output
    assert "lean-proof-auto: not installed" in output
    assert "loogle client: client negotiates index flags" in output

    assert main(["mcp", "repair", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["success"] is True


def test_doctor_flags_the_incompatible_client_path(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.resolve_runtime_provider",
        lambda: {"provider": "custom", "base_url": "https://example.test/v1", "model": "m"},
    )
    detail = (
        "client sends --write-index/--read-index but the built Loogle only accepts "
        "--index-mode/--index-file, so the managed index is never written"
    )
    monkeypatch.setattr(
        "leanflow_cli.cli.doctor.get_mcp_status",
        lambda: [
            {
                "name": "lean-lsp",
                "transport": "stdio",
                "tools": 0,
                "connected": False,
                "managed": True,
                "installed": True,
                "configured": True,
                "bootstrap_recommended": False,
                "power_modes": {
                    "loogle_local_status": "client-incompatible",
                    "repl_status": "ready",
                    "loogle_client": {"client_found": True, "detail": detail},
                },
            }
        ],
    )

    issues, payload = run_doctor(tmp_path, mode="mcp", json_output=True)
    assert any("mcp repair" in issue and "--write-index" in issue for issue in issues)

    _issues, report = run_doctor(tmp_path, mode="mcp", json_output=False)
    assert "local Loogle=client-incompatible" in report
    assert f"loogle client: {detail}" in report
