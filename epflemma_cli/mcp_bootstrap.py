"""Managed Lean MCP bootstrap helpers.

This module owns the installer/bootstrap path for the default Lean MCP stack.
It intentionally does not replace the existing generic config helpers; it uses
comment-preserving YAML edits only for the managed MCP entries.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from epflemma_cli.config import ensure_epflemma_home, get_config_path, get_epflemma_home


@dataclass(frozen=True)
class ManagedMCPServerSpec:
    name: str
    role: str
    venv_name: str
    install_spec: str
    console_script: str
    extra_install_specs: tuple[str, ...] = ()
    timeout: int = 600
    connect_timeout: int = 120

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
}


def managed_lean_mcp_specs() -> dict[str, ManagedMCPServerSpec]:
    return dict(MANAGED_LEAN_MCP_SPECS)


def managed_mcp_root(home: str | os.PathLike[str] | None = None) -> Path:
    base = Path(home).expanduser().resolve() if home else get_epflemma_home()
    return base / "mcp"


def managed_mcp_venv_dir(name: str, home: str | os.PathLike[str] | None = None) -> Path:
    spec = MANAGED_LEAN_MCP_SPECS[name]
    return managed_mcp_root(home) / "venvs" / spec.venv_name


def _venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def managed_mcp_command_path(name: str, home: str | os.PathLike[str] | None = None) -> Path:
    spec = MANAGED_LEAN_MCP_SPECS[name]
    suffix = ".exe" if os.name == "nt" else ""
    return _venv_bin_dir(managed_mcp_venv_dir(name, home)) / f"{spec.console_script}{suffix}"


def managed_mcp_python_path(name: str, home: str | os.PathLike[str] | None = None) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return _venv_bin_dir(managed_mcp_venv_dir(name, home)) / f"python{suffix}"


def _secure_file(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _secure_dir(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass


def _commented_map(value: Mapping[str, Any] | None = None) -> CommentedMap:
    node = CommentedMap()
    for key, item in (value or {}).items():
        node[key] = item
    return node


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


def _ensure_managed_server_entry(entry: CommentedMap, *, spec: ManagedMCPServerSpec, home: Path) -> None:
    entry["command"] = str(managed_mcp_command_path(spec.name, home))
    entry["args"] = []
    entry["role"] = spec.role
    entry["managed"] = True
    if "enabled" not in entry:
        entry["enabled"] = True
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
    home_path = Path(home).expanduser().resolve() if home else ensure_epflemma_home(import_legacy=False)
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


def _ensure_venv(venv_dir: Path, *, python_bin: str | None = None) -> Path:
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(venv_dir.parent)
    existing_python = _venv_bin_dir(venv_dir) / ("python.exe" if os.name == "nt" else "python")
    if existing_python.exists():
        try:
            subprocess.run([str(existing_python), "-V"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return existing_python
        except Exception:
            shutil.rmtree(venv_dir, ignore_errors=True)
    if existing_python.exists():
        return existing_python

    candidates: list[str] = []
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
    extra_install_specs: tuple[str, ...] = (),
) -> None:
    python_path = _ensure_venv(venv_dir, python_bin=python_bin)
    subprocess.run([str(python_path), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], check=True)
    for spec in (install_spec, *extra_install_specs):
        subprocess.run([str(python_path), "-m", "pip", "install", "--upgrade", spec], check=True)


def managed_mcp_server_status(home: str | os.PathLike[str] | None = None) -> dict[str, dict[str, Any]]:
    home_path = Path(home).expanduser().resolve() if home else get_epflemma_home()
    config_path = home_path / get_config_path().name
    _yaml, doc = _load_bootstrap_document(config_path)
    configured = doc.get("mcp_servers")
    configured = dict(configured) if isinstance(configured, Mapping) else {}
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
        entry["healthy"] = bool(entry["installed"] and entry["configured"] and entry["command_matches"])
        entry["bootstrap_recommended"] = not bool(entry["healthy"])
        status[spec.name] = entry
    return status


def bootstrap_lean_mcp(*, home: str | os.PathLike[str] | None = None, python_bin: str | None = None) -> dict[str, Any]:
    home_path = Path(home).expanduser().resolve() if home else ensure_epflemma_home(import_legacy=False)
    managed_root = managed_mcp_root(home_path)
    (managed_root / "venvs").mkdir(parents=True, exist_ok=True)
    _secure_dir(managed_root)
    _secure_dir(managed_root / "venvs")

    installed_servers: list[dict[str, Any]] = []
    for spec in MANAGED_LEAN_MCP_SPECS.values():
        venv_dir = managed_mcp_venv_dir(spec.name, home_path)
        _install_into_managed_venv(
            venv_dir,
            spec.install_spec,
            python_bin=python_bin,
            extra_install_specs=spec.extra_install_specs,
        )
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
    return {
        "success": True,
        "home": str(home_path),
        "managed_root": str(managed_root),
        "config_path": config_result["config_path"],
        "servers": installed_servers,
    }
