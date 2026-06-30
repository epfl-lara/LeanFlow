"""Managed Lean MCP bootstrap helpers.

This module owns the installer/bootstrap path for the default Lean MCP stack.
It intentionally does not replace the existing generic config helpers; it uses
comment-preserving YAML edits only for the managed MCP entries.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from leanflow_cli.config import (
    ensure_leanflow_home,
    get_config_path,
    get_leanflow_home,
    invalidate_config_cache,
)


@dataclass(frozen=True)
class ManagedMCPServerSpec:
    name: str
    role: str
    venv_name: str
    install_spec: str
    console_script: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    extra_install_specs: tuple[str, ...] = ()
    default_enabled: bool = True
    timeout: int = 600
    connect_timeout: int = 120
    min_python: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


MANAGED_LEAN_MCP_SPECS: dict[str, ManagedMCPServerSpec] = {
    "lean-lsp": ManagedMCPServerSpec(
        name="lean-lsp",
        role="primary-state-search",
        venv_name="lean-lsp",
        install_spec="lean-lsp-mcp==0.26.1",
        console_script="lean-lsp-mcp",
    ),
    "lean-proof-auto": ManagedMCPServerSpec(
        name="lean-proof-auto",
        role="secondary-automation-context",
        venv_name="lean-proof-auto",
        install_spec="lean-proof-auto-mcp @ git+https://github.com/padieul/lean-proof-auto-mcp.git@v0.4.0",
        console_script="lean-proof-auto-mcp",
        extra_install_specs=("PyYAML",),
    ),
    "lean-explore": ManagedMCPServerSpec(
        name="lean-explore",
        role="semantic-declaration-search",
        venv_name="lean-explore",
        install_spec="lean-explore[local]",
        console_script="lean-explore",
        args=("mcp", "serve", "--backend", "local"),
        min_python=(3, 12),
    ),
}


REMOTE_SEARCH_POLICY = "public-fallbacks-enabled"
LEAN_REPL_TIMEOUT_SECONDS = "60"
LEAN_REPL_MEM_MB = "8192"


def managed_mcp_root(home: str | os.PathLike[str] | None = None) -> Path:
    base = Path(home).expanduser().resolve() if home else get_leanflow_home()
    return base / "mcp"


def loogle_toolchain_slug(toolchain: str | None) -> str:
    """Filesystem-safe slug for a Lean toolchain (e.g. ``leanprover/lean4:v4.30.0-rc2`` ->
    ``leanprover-lean4-v4.30.0-rc2``). MUST match the convention in
    ``tools/mcp/mcp_transport._augment_lean_stdio_env`` so the server, the builder, and the
    status check all resolve the SAME per-toolchain cache dir."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(toolchain or "").strip()).strip("-")


def managed_loogle_cache_dir(
    home: str | os.PathLike[str] | None = None, toolchain: str | None = None
) -> Path:
    """Loogle cache dir. With a toolchain, returns a per-toolchain dir (``loogle-<slug>``) so
    projects on different toolchains don't thrash a single shared Loogle build; without one,
    the generic ``loogle`` dir (the base/fallback)."""
    base = managed_mcp_root(home) / "cache"
    slug = loogle_toolchain_slug(toolchain)
    return base / (f"loogle-{slug}" if slug else "loogle")


def local_loogle_supported() -> bool:
    return os.name != "nt"


def managed_mcp_venv_dir(name: str, home: str | os.PathLike[str] | None = None) -> Path:
    spec = MANAGED_LEAN_MCP_SPECS[name]
    return managed_mcp_root(home) / "venvs" / spec.venv_name


def _venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def managed_mcp_command_path(name: str, home: str | os.PathLike[str] | None = None) -> Path:
    spec = MANAGED_LEAN_MCP_SPECS[name]
    suffix = ".exe" if os.name == "nt" else ""
    return _venv_bin_dir(managed_mcp_venv_dir(name, home)) / f"{spec.console_script}{suffix}"


def _secure_file(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _secure_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _commented_map(value: Mapping[str, Any] | None = None) -> CommentedMap:
    node = CommentedMap()
    for key, item in (value or {}).items():
        node[key] = item
    return node


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _read_lean_toolchain(path: str | os.PathLike[str] | None) -> str:
    if not path:
        return ""
    try:
        root = Path(path).expanduser().resolve()
        return (root / "lean-toolchain").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _lean_lsp_power_env(home: Path) -> dict[str, str]:
    env = {
        "LEAN_REPL": "true",
        "LEAN_REPL_TIMEOUT": LEAN_REPL_TIMEOUT_SECONDS,
        "LEAN_REPL_MEM_MB": LEAN_REPL_MEM_MB,
        "LEAN_LOOGLE_CACHE_DIR": str(managed_loogle_cache_dir(home)),
        "LEAN_MCP_INSTRUCTIONS": (
            "Prefer local Lean project search, local Loogle, and REPL-backed tactic screening. "
            "Public remote Lean search fallbacks are allowed when local search is unavailable."
        ),
    }
    if local_loogle_supported():
        env["LEAN_LOOGLE_LOCAL"] = "true"
    return env


def _server_env_from_config(configured: Mapping[str, Any], name: str) -> dict[str, Any]:
    cfg = configured.get(name, {})
    if not isinstance(cfg, Mapping):
        return {}
    env = cfg.get("env", {})
    return dict(env) if isinstance(env, Mapping) else {}


def _detect_repl_binary(project_root: str | os.PathLike[str] | None = None) -> str:
    if not project_root:
        return ""
    root = Path(project_root).expanduser().resolve()
    candidates = [
        root / ".lake" / "build" / "bin" / ("repl.exe" if os.name == "nt" else "repl"),
        root
        / ".lake"
        / "packages"
        / "repl"
        / ".lake"
        / "build"
        / "bin"
        / ("repl.exe" if os.name == "nt" else "repl"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def _load_bootstrap_document(path: Path) -> tuple[YAML, CommentedMap]:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    if path.exists():
        loaded = yaml.load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, CommentedMap):
            return yaml, loaded
        if isinstance(loaded, Mapping):
            return yaml, _commented_map(loaded)
    return yaml, CommentedMap()


def _write_bootstrap_document(path: Path, yaml: YAML, payload: CommentedMap) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(payload, handle)
    _secure_file(path)
    # This writes the managed config file directly (preserving comments via ruamel) rather than
    # through config.save_config(), so it must drop the in-process load_config() cache itself —
    # otherwise a later load_config() in the same process returns the pre-bootstrap config.
    if path.name == get_config_path().name:
        invalidate_config_cache()


def _ensure_managed_server_entry(
    entry: CommentedMap, *, spec: ManagedMCPServerSpec, home: Path
) -> None:
    previous_args = list(entry.get("args") or []) if isinstance(entry.get("args"), list) else []
    previous_enabled = entry.get("enabled")
    entry["command"] = str(managed_mcp_command_path(spec.name, home))
    entry["args"] = list(spec.args)
    env_defaults = dict(spec.env)
    if spec.name == "lean-lsp":
        env_defaults.update(_lean_lsp_power_env(home))
    if env_defaults:
        env = entry.get("env")
        if not isinstance(env, Mapping):
            env = CommentedMap()
            entry["env"] = env
        elif not isinstance(env, CommentedMap):
            env = _commented_map(dict(env))
            entry["env"] = env
        for key, value in env_defaults.items():
            if key not in env:
                env[key] = value
        if spec.name == "lean-lsp":
            # Migration: heal a stale MANAGED LEAN_LOOGLE_CACHE_DIR (e.g. an older per-toolchain
            # value ".../cache/loogle-v4.30.0-rc2") back to the generic base, so the runtime's
            # per-toolchain re-suffixing applies consistently. Only paths under the managed
            # cache root are touched; user-custom cache dirs are left untouched.
            current = str(env.get("LEAN_LOOGLE_CACHE_DIR", "") or "").strip()
            generic = str(managed_loogle_cache_dir(home))
            cache_root = str(managed_mcp_root(home) / "cache") + os.sep
            if current and current != generic and current.startswith(cache_root):
                env["LEAN_LOOGLE_CACHE_DIR"] = generic
    entry["role"] = spec.role
    entry["managed"] = True
    if "enabled" not in entry or (
        spec.name == "lean-explore"
        and previous_enabled is False
        and previous_args == ["mcp", "serve", "--backend", "api"]
    ):
        entry["enabled"] = spec.default_enabled
    if "timeout" not in entry:
        entry["timeout"] = spec.timeout
    if "connect_timeout" not in entry:
        entry["connect_timeout"] = spec.connect_timeout
    sampling = entry.get("sampling")
    if not isinstance(sampling, Mapping):
        sampling = CommentedMap()
        entry["sampling"] = sampling
    elif not isinstance(sampling, CommentedMap):
        sampling = _commented_map(dict(sampling))
        entry["sampling"] = sampling
    if "enabled" not in sampling:
        sampling["enabled"] = False


def write_managed_mcp_config(home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Ensure the LeanFlow config file contains entries for all managed Lean MCP servers with correct command paths, roles, and power-mode environments (REPL timeout, Loogle cache dir, local-search instructions)."""
    home_path = Path(home).expanduser().resolve() if home else ensure_leanflow_home()
    home_path.mkdir(parents=True, exist_ok=True)
    _secure_dir(home_path)
    path = home_path / get_config_path().name
    yaml, doc = _load_bootstrap_document(path)
    mcp_servers = doc.get("mcp_servers")
    if not isinstance(mcp_servers, Mapping):
        mcp_servers = CommentedMap()
        doc["mcp_servers"] = mcp_servers
    elif not isinstance(mcp_servers, CommentedMap):
        mcp_servers = _commented_map(dict(mcp_servers))
        doc["mcp_servers"] = mcp_servers

    updated: list[str] = []
    for spec in MANAGED_LEAN_MCP_SPECS.values():
        existing = mcp_servers.get(spec.name)
        if not isinstance(existing, Mapping):
            existing = CommentedMap()
            mcp_servers[spec.name] = existing
        elif not isinstance(existing, CommentedMap):
            existing = _commented_map(dict(existing))
            mcp_servers[spec.name] = existing
        _ensure_managed_server_entry(existing, spec=spec, home=home_path)
        updated.append(spec.name)

    _write_bootstrap_document(path, yaml, doc)
    return {
        "config_path": str(path),
        "updated_servers": updated,
    }


def _python_version_tuple(python_path: str | os.PathLike[str]) -> tuple[int, int] | None:
    try:
        completed = subprocess.run(
            [
                str(python_path),
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception:
        return None
    text = completed.stdout.strip()
    try:
        major, minor = text.split(".", 1)
        return int(major), int(minor)
    except Exception:
        return None


def _ensure_venv(
    venv_dir: Path,
    *,
    python_bin: str | None = None,
    min_python: tuple[int, int] | None = None,
) -> Path:
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(venv_dir.parent)
    existing_python = _venv_bin_dir(venv_dir) / ("python.exe" if os.name == "nt" else "python")
    if existing_python.exists():
        try:
            subprocess.run(
                [str(existing_python), "-V"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if min_python is None:
                return existing_python
            version = _python_version_tuple(existing_python)
            if version is not None and version >= min_python:
                return existing_python
            shutil.rmtree(venv_dir, ignore_errors=True)
        except Exception:
            shutil.rmtree(venv_dir, ignore_errors=True)
    if existing_python.exists():
        return existing_python

    candidates: list[str] = []
    if min_python and min_python >= (3, 12):
        for name in ("python3.13", "python3.12"):
            path = shutil.which(name)
            if path:
                candidates.append(path)
    for candidate in (
        python_bin,
        sys.executable,
        shutil.which("python3"),
        shutil.which("python"),
    ):
        text = str(candidate or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    last_error: Exception | None = None
    for candidate in candidates:
        if min_python is not None:
            version = _python_version_tuple(candidate)
            if version is None or version < min_python:
                continue
        try:
            subprocess.run([candidate, "-m", "venv", str(venv_dir)], check=True)
            break
        except Exception as exc:
            last_error = exc
            shutil.rmtree(venv_dir, ignore_errors=True)
    else:
        raise RuntimeError(f"Failed to create managed MCP virtualenv at {venv_dir}") from last_error
    return existing_python


def _install_into_managed_venv(
    venv_dir: Path,
    install_spec: str,
    *,
    python_bin: str | None = None,
    min_python: tuple[int, int] | None = None,
    extra_install_specs: tuple[str, ...] = (),
) -> None:
    python_path = _ensure_venv(venv_dir, python_bin=python_bin, min_python=min_python)
    subprocess.run(
        [
            str(python_path),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--quiet",
            "--upgrade",
            "pip",
            "setuptools<82",
            "wheel",
        ],
        check=True,
    )
    for spec in (install_spec, *extra_install_specs):
        subprocess.run(
            [str(python_path), "-m", "pip", "install", "--quiet", "--quiet", "--upgrade", spec],
            check=True,
        )


def _patch_lean_lsp_loogle_project_paths(venv_dir: Path) -> bool:
    """Patch lean-lsp-mcp so local Loogle project paths keep Lean/Loogle imports visible."""
    candidates = [
        *venv_dir.glob("lib/python*/site-packages/lean_lsp_mcp/loogle.py"),
        venv_dir / "Lib" / "site-packages" / "lean_lsp_mcp" / "loogle.py",
    ]
    target = next((candidate for candidate in candidates if candidate.is_file()), None)
    if target is None:
        return False
    text = target.read_text(encoding="utf-8")
    marker = "When any --path is passed to loogle"
    if marker in text:
        return True
    needle = "        paths = []\n        # Check packages directory\n"
    replacement = (
        "        paths = []\n"
        "        # When any --path is passed to loogle, the explicit path list must also\n"
        "        # include Lean's stdlib and loogle's own build lib; otherwise imports\n"
        "        # such as Init and Loogle cannot be resolved.\n"
        "        try:\n"
        '            lean_lib = self._run(["lean", "--print-libdir"], timeout=30)\n'
        "            if lean_lib.returncode == 0:\n"
        "                lean_lib_path = Path(lean_lib.stdout.strip())\n"
        "                if lean_lib_path.exists():\n"
        "                    paths.append(lean_lib_path)\n"
        "        except Exception:\n"
        "            pass\n"
        '        loogle_lib = self.repo_dir / ".lake" / "build" / "lib" / "lean"\n'
        "        if loogle_lib.exists():\n"
        "            paths.append(loogle_lib)\n"
        "        # Check packages directory\n"
    )
    if needle not in text:
        return False
    target.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
    return True


def _patch_lean_lsp_loogle_build_lock(venv_dir: Path) -> bool:
    """Patch lean-lsp-mcp so its Loogle build takes the same lock LeanFlow uses.

    Without this, the lean-lsp-mcp server could run ``lake build`` in the Loogle repo at the
    same time as LeanFlow's managed build, corrupting the build dir. Wrapping ``_build_loogle``
    in an exclusive flock on ``<cache_dir>/.loogle-build.lock`` (the same file LeanFlow locks)
    makes the two serialize: whoever wins builds, the loser re-checks ``is_installed`` and skips.
    Idempotent via the ``_leanflow_build_loogle_inner`` marker.
    """
    candidates = [
        *venv_dir.glob("lib/python*/site-packages/lean_lsp_mcp/loogle.py"),
        venv_dir / "Lib" / "site-packages" / "lean_lsp_mcp" / "loogle.py",
    ]
    target = next((candidate for candidate in candidates if candidate.is_file()), None)
    if target is None:
        return False
    text = target.read_text(encoding="utf-8")
    if "_leanflow_build_loogle_inner" in text:
        return True
    needle = "    def _build_loogle(self) -> bool:\n        if self.is_installed:\n            return True\n"
    if needle not in text:
        return False
    replacement = (
        "    def _build_loogle(self) -> bool:\n"
        "        import fcntl\n"
        "        self.cache_dir.mkdir(parents=True, exist_ok=True)\n"
        '        _lf_lock = open(self.cache_dir / ".loogle-build.lock", "w")\n'
        "        try:\n"
        "            try:\n"
        "                fcntl.flock(_lf_lock, fcntl.LOCK_EX)\n"
        "            except Exception:\n"
        "                pass\n"
        "            return self._leanflow_build_loogle_inner()\n"
        "        finally:\n"
        "            try:\n"
        "                fcntl.flock(_lf_lock, fcntl.LOCK_UN)\n"
        "            except Exception:\n"
        "                pass\n"
        "            _lf_lock.close()\n"
        "\n"
        "    def _leanflow_build_loogle_inner(self) -> bool:\n"
        "        if self.is_installed:\n"
        "            return True\n"
    )
    target.write_text(text.replace(needle, replacement, 1), encoding="utf-8")
    return True


def managed_mcp_power_status(
    home: str | os.PathLike[str] | None = None,
    *,
    project_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Detect the readiness state of REPL and local Loogle backend for lean-lsp: report REPL binary path, availability, and configuration; scan Loogle cache directory for compatibility with project Lean toolchain; return a status dict for each subsystem."""
    home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
    config_path = home_path / get_config_path().name
    _yaml, doc = _load_bootstrap_document(config_path)
    configured = doc.get("mcp_servers")
    configured = dict(configured) if isinstance(configured, Mapping) else {}
    lean_lsp_env = _server_env_from_config(configured, "lean-lsp")
    repl_path = str(lean_lsp_env.get("LEAN_REPL_PATH", "") or "").strip() or _detect_repl_binary(
        project_root
    )
    repl_configured = _truthy(lean_lsp_env.get("LEAN_REPL"))
    repl_available = bool(repl_path and Path(repl_path).is_file())
    # Resolve the per-toolchain cache dir for the project (matching the build + the server
    # launch). Without a project, fall back to the config value or the generic dir.
    if _read_lean_toolchain(project_root):
        loogle_cache_dir = loogle_cache_dir_for_project(home_path, project_root)
    else:
        loogle_cache_dir = Path(
            str(lean_lsp_env.get("LEAN_LOOGLE_CACHE_DIR", "") or managed_loogle_cache_dir(home_path))
        ).expanduser()
    loogle_supported = local_loogle_supported()
    loogle_configured = _truthy(lean_lsp_env.get("LEAN_LOOGLE_LOCAL"))
    loogle_toolchain = _read_lean_toolchain(loogle_cache_dir / "repo")
    project_toolchain = _read_lean_toolchain(project_root)
    loogle_toolchain_compatible = bool(
        not loogle_toolchain or not project_toolchain or loogle_toolchain == project_toolchain
    )
    loogle_ready = bool(
        loogle_configured
        and loogle_toolchain_compatible
        and loogle_cache_dir.is_dir()
        and any(loogle_cache_dir.iterdir())
    )
    if not loogle_supported:
        loogle_status = "unsupported"
    elif not loogle_configured:
        loogle_status = "disabled"
    elif not loogle_toolchain_compatible:
        loogle_status = "incompatible"
    elif loogle_ready:
        loogle_status = "ready"
    elif loogle_configured:
        loogle_status = "configured"
    else:
        loogle_status = "disabled"
    return {
        "remote_search_policy": REMOTE_SEARCH_POLICY,
        "loogle_local_configured": loogle_configured,
        "loogle_local_available": bool(
            loogle_supported and loogle_configured and loogle_toolchain_compatible
        ),
        "loogle_local_ready": loogle_ready,
        "loogle_local_status": loogle_status,
        "loogle_local_supported": loogle_supported,
        "loogle_toolchain": loogle_toolchain,
        "project_toolchain": project_toolchain,
        "loogle_toolchain_compatible": loogle_toolchain_compatible,
        "loogle_cache_dir": str(loogle_cache_dir),
        "repl_configured": repl_configured,
        "repl_available": repl_available,
        "repl_path": repl_path,
        "repl_status": (
            "ready" if repl_available else ("configured" if repl_configured else "disabled")
        ),
    }


def managed_mcp_server_status(
    home: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Check installation and configuration health of each managed MCP server (lean-lsp, lean-proof-auto, lean-explore): verify venv and command exist, config matches expected paths, and enabled flag is set; attach lean-lsp power modes; flag if bootstrap is recommended."""
    home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
    config_path = home_path / get_config_path().name
    _yaml, doc = _load_bootstrap_document(config_path)
    configured = doc.get("mcp_servers")
    configured = dict(configured) if isinstance(configured, Mapping) else {}
    power_status = managed_mcp_power_status(home_path)
    status: dict[str, dict[str, Any]] = {}
    for spec in MANAGED_LEAN_MCP_SPECS.values():
        venv_dir = managed_mcp_venv_dir(spec.name, home_path)
        command_path = managed_mcp_command_path(spec.name, home_path)
        cfg = configured.get(spec.name, {})
        cfg = dict(cfg) if isinstance(cfg, Mapping) else {}
        configured_command = str(cfg.get("command", "") or "")
        entry = {
            "name": spec.name,
            "role": spec.role,
            "managed": True,
            "venv_dir": str(venv_dir),
            "command": str(command_path),
            "installed": command_path.exists(),
            "configured": bool(cfg),
            "command_matches": configured_command == str(command_path),
            "enabled": bool(cfg.get("enabled", True)) if cfg else False,
        }
        if spec.name == "lean-lsp":
            entry["power_modes"] = power_status
        entry["healthy"] = bool(
            entry["installed"] and entry["configured"] and entry["command_matches"]
        )
        entry["bootstrap_recommended"] = not bool(entry["healthy"])
        status[spec.name] = entry
    return status


_LOOGLE_REPO_URL = "https://github.com/nomeata/loogle.git"


def _lean_lsp_env_from_home(home_path: Path) -> dict[str, Any]:
    """Read the persisted lean-lsp server env block from the LeanFlow config."""
    config_path = home_path / get_config_path().name
    _yaml, doc = _load_bootstrap_document(config_path)
    configured = doc.get("mcp_servers")
    configured = dict(configured) if isinstance(configured, Mapping) else {}
    return _server_env_from_config(configured, "lean-lsp")


def _active_loogle_cache_dir(home_path: Path) -> Path:
    """Resolve the Loogle cache dir the runtime actually uses (config value, else default)."""
    raw = str(_lean_lsp_env_from_home(home_path).get("LEAN_LOOGLE_CACHE_DIR", "") or "").strip()
    return Path(raw).expanduser() if raw else managed_loogle_cache_dir(home_path)


def _loogle_binary_path(repo_dir: Path) -> Path:
    return repo_dir / ".lake" / "build" / "bin" / ("loogle.exe" if os.name == "nt" else "loogle")


def loogle_cache_dir_for_project(
    home_path: Path, project_root: str | os.PathLike[str] | None
) -> Path:
    """Resolve the per-toolchain Loogle cache dir for a project.

    Keyed on the project's Lean toolchain so different toolchains get isolated builds (no
    rebuild thrash when switching projects). Falls back to the config/generic dir when the
    project toolchain is unknown. The lean-lsp server is pointed at the SAME dir by
    ``_augment_lean_stdio_env``, so build, server, and status stay consistent.
    """
    tc = _read_lean_toolchain(project_root)
    if tc:
        return managed_loogle_cache_dir(home_path, toolchain=tc)
    return _active_loogle_cache_dir(home_path)


def _loogle_build_lock_path(cache_dir: Path) -> Path:
    return cache_dir / ".loogle-build.lock"


def local_loogle_needs_build(
    project_root: str | os.PathLike[str] | None, home: str | os.PathLike[str] | None = None
) -> bool:
    """Fast check (no build): does local Loogle need a (re)build for this project's toolchain?

    True only when local Loogle is supported, enabled in config, a project toolchain is
    known, git+lake are present, and the cached Loogle is either missing or was built for
    a *different* toolchain. False otherwise (including when a build couldn't succeed), so
    callers can gate work without ever shelling out.
    """
    if not local_loogle_supported():
        return False
    project_tc = _read_lean_toolchain(project_root)
    if not project_tc:
        return False
    if not (shutil.which("git") and shutil.which("lake")):
        return False
    home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
    if not _truthy(_lean_lsp_env_from_home(home_path).get("LEAN_LOOGLE_LOCAL")):
        return False
    repo_dir = loogle_cache_dir_for_project(home_path, project_root) / "repo"
    if not _loogle_binary_path(repo_dir).is_file():
        return True
    return _read_lean_toolchain(repo_dir) != project_tc


def ensure_local_loogle_for_project(
    project_root: str | os.PathLike[str] | None,
    home: str | os.PathLike[str] | None = None,
    *,
    timeout: int = 1200,
) -> dict[str, Any]:
    """Build managed local Loogle against the PROJECT's Lean toolchain (idempotent).

    lean-lsp-mcp clones Loogle and builds it with Loogle's OWN pinned toolchain, but
    LeanFlow only enables local Loogle when that toolchain equals the project's — which it
    almost never does, so local Loogle stays ``incompatible`` and search silently falls back
    to remote. This repins Loogle's ``lean-toolchain`` to the project's and rebuilds the
    binary so the two match and local Loogle actually activates. Loogle has no Mathlib
    dependency, so the build is light (~1-2 min) and cached until the project toolchain
    changes.

    Best-effort: returns a status dict and never raises. A no-op (``action="already-built"``)
    when the binary already exists and was built for the project's toolchain.
    """
    result: dict[str, Any] = {"ok": False, "action": "skipped", "reason": "", "toolchain": ""}
    try:
        if not local_loogle_supported():
            result["reason"] = "unsupported-platform"
            return result
        project_tc = _read_lean_toolchain(project_root)
        if not project_tc:
            result["reason"] = "no-project-toolchain"
            return result
        if not (shutil.which("git") and shutil.which("lake")):
            result["reason"] = "missing-git-or-lake"
            return result
        home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
        cache_dir = loogle_cache_dir_for_project(home_path, project_root)
        repo_dir = cache_dir / "repo"
        binary = _loogle_binary_path(repo_dir)
        result.update(toolchain=project_tc, cache_dir=str(cache_dir))

        if binary.is_file() and _read_lean_toolchain(repo_dir) == project_tc:
            result.update(ok=True, action="already-built")
            return result

        cache_dir.mkdir(parents=True, exist_ok=True)
        # Exclusive build lock: serialize with any other builder of THIS cache dir — a
        # concurrent LeanFlow workflow or the lean-lsp-mcp server (which is patched to take
        # the same lock). Whoever gets the lock builds; the others re-check is_installed and
        # skip. fcntl is POSIX-only, which is fine: local Loogle is gated to non-Windows.
        import fcntl

        lock_handle = open(_loogle_build_lock_path(cache_dir), "w", encoding="utf-8")
        try:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_handle, fcntl.LOCK_EX)
            # Re-check after acquiring: another builder may have just finished.
            if binary.is_file() and _read_lean_toolchain(repo_dir) == project_tc:
                result.update(ok=True, action="already-built")
                return result

            has_lakefile = (repo_dir / "lakefile.lean").exists() or (
                repo_dir / "lakefile.toml"
            ).exists()
            if not has_lakefile:
                clone = subprocess.run(
                    ["git", "clone", "--depth", "1", _LOOGLE_REPO_URL, str(repo_dir)],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if clone.returncode != 0:
                    result.update(
                        reason="clone-failed", detail=((clone.stderr or clone.stdout) or "")[:400]
                    )
                    return result

            # Pin Loogle to the project's toolchain, then force a rebuild of the binary so it
            # is compiled with that exact toolchain (and can therefore read the project oleans).
            (repo_dir / "lean-toolchain").write_text(project_tc + "\n", encoding="utf-8")
            with contextlib.suppress(OSError):
                binary.unlink()
            build = subprocess.run(
                ["lake", "build"],
                cwd=str(repo_dir),
                env={**os.environ, "LAKE_ARTIFACT_CACHE": "false"},
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if build.returncode != 0 or not binary.is_file():
                result.update(
                    reason="build-failed", detail=((build.stderr or build.stdout) or "")[-400:]
                )
                return result
            result.update(ok=True, action="built")
            return result
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            lock_handle.close()
    except Exception as exc:  # never break callers; remote Loogle remains the fallback
        result["reason"] = f"error: {exc}"
        return result


def ensure_local_loogle_for_project_async(
    project_root: str | os.PathLike[str] | None, home: str | os.PathLike[str] | None = None
) -> bool:
    """Kick off :func:`ensure_local_loogle_for_project` in a detached background process.

    Returns True if a build was launched, False when no build was needed (the common
    steady state) or one could not be started. Non-blocking: the current run proceeds and
    uses remote Loogle while the build is in flight; the next run picks up local Loogle.
    """
    try:
        if not local_loogle_needs_build(project_root, home):
            return False
        home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
        log_dir = _active_loogle_cache_dir(home_path)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_dir / "loogle-build.log", "a", encoding="utf-8")
        try:
            code = (
                "import sys;"
                "from leanflow_cli.cli.mcp_bootstrap import ensure_local_loogle_for_project as e;"
                "print(e(sys.argv[1] or None, sys.argv[2] or None))"
            )
            subprocess.Popen(  # noqa: S603 - fixed argv, detached best-effort build
                [sys.executable, "-c", code, str(project_root or ""), str(home or "")],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        return True
    except Exception:
        return False


def bootstrap_lean_mcp(
    *, home: str | os.PathLike[str] | None = None, python_bin: str | None = None
) -> dict[str, Any]:
    """Install all managed MCP servers into isolated virtualenvs, patch lean-lsp Loogle search paths, generate config entries with power-mode environment variables, and return detailed installation report including server venvs, command paths, and power-mode status."""
    home_path = Path(home).expanduser().resolve() if home else ensure_leanflow_home()
    managed_root = managed_mcp_root(home_path)
    (managed_root / "venvs").mkdir(parents=True, exist_ok=True)
    managed_loogle_cache_dir(home_path).mkdir(parents=True, exist_ok=True)
    _secure_dir(managed_root)
    _secure_dir(managed_root / "venvs")
    _secure_dir(managed_root / "cache")
    _secure_dir(managed_loogle_cache_dir(home_path))

    installed_servers: list[dict[str, Any]] = []
    for spec in MANAGED_LEAN_MCP_SPECS.values():
        venv_dir = managed_mcp_venv_dir(spec.name, home_path)
        _install_into_managed_venv(
            venv_dir,
            spec.install_spec,
            python_bin=python_bin,
            min_python=spec.min_python,
            extra_install_specs=spec.extra_install_specs,
        )
        if spec.name == "lean-lsp":
            _patch_lean_lsp_loogle_project_paths(venv_dir)
            _patch_lean_lsp_loogle_build_lock(venv_dir)
        installed_servers.append(
            {
                "name": spec.name,
                "role": spec.role,
                "venv_dir": str(venv_dir),
                "command": str(managed_mcp_command_path(spec.name, home_path)),
                "install_spec": spec.install_spec,
            }
        )

    config_result = write_managed_mcp_config(home_path)
    power_status = managed_mcp_power_status(home_path)
    for entry in installed_servers:
        if entry.get("name") == "lean-lsp":
            entry["power_modes"] = power_status
    return {
        "success": True,
        "home": str(home_path),
        "managed_root": str(managed_root),
        "config_path": config_result["config_path"],
        "remote_search_policy": REMOTE_SEARCH_POLICY,
        "power_modes": power_status,
        "servers": installed_servers,
    }
