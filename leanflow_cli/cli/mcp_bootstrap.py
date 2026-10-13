"""Managed Lean MCP bootstrap helpers.

This module owns the installer/bootstrap path for the default Lean MCP stack.
It intentionally does not replace the existing generic config helpers; it uses
comment-preserving YAML edits only for the managed MCP entries.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from leanflow_cli.cli.mcp_install_generation import (
    MCPValidationCleanupUncertain,
    atomic_managed_config_write,
    managed_config_write_target,
    managed_mcp_install_lock,
    validate_managed_mcp_candidate,
)
from leanflow_cli.cli.mcp_install_receipts import (
    managed_mcp_receipt_status,
    mcp_install_fingerprint,
    write_managed_mcp_receipt,
)
from leanflow_cli.config import (
    ensure_leanflow_home,
    get_config_path,
    get_leanflow_home,
    invalidate_config_cache,
)
from leanflow_cli.runtime.mcp_protocol import MCP_REQUIREMENT


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
        install_spec="lean-proof-auto-mcp @ git+https://github.com/padieul/lean-proof-auto-mcp.git@8d829ecf8e25ac7597fd5cc41b043a40aa404ec1",
        console_script="lean-proof-auto-mcp",
        extra_install_specs=("lean-interact==0.11.5", "PyYAML==6.0.3"),
    ),
    "lean-explore": ManagedMCPServerSpec(
        name="lean-explore",
        role="semantic-declaration-search",
        venv_name="lean-explore",
        install_spec="lean-explore==1.3.0",
        console_script="lean-explore",
        args=("mcp", "serve", "--backend", "api"),
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
    """Resolve the selected contained managed environment, or the legacy default."""
    spec = MANAGED_LEAN_MCP_SPECS[name]
    home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
    root = managed_mcp_root(home_path) / "venvs"
    _yaml, doc = _load_bootstrap_document(home_path / get_config_path().name)
    configured = doc.get("mcp_servers", {})
    entry = configured.get(name, {}) if isinstance(configured, Mapping) else {}
    if isinstance(entry, Mapping) and entry.get("managed") is True:
        command = Path(str(entry.get("command", "") or "")).expanduser()
        suffix = ".exe" if os.name == "nt" else ""
        if (
            command.is_absolute()
            and command.name == f"{spec.console_script}{suffix}"
            and command.parent.name == ("Scripts" if os.name == "nt" else "bin")
            and command.parent.parent.name == spec.venv_name
        ):
            candidate = command.parent.parent
            try:
                candidate.resolve().relative_to(root.resolve())
                command.resolve().relative_to(candidate.resolve())
            except (OSError, ValueError):
                pass
            else:
                return candidate
    return root / spec.venv_name


def _venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def managed_mcp_command_path(name: str, home: str | os.PathLike[str] | None = None) -> Path:
    spec = MANAGED_LEAN_MCP_SPECS[name]
    suffix = ".exe" if os.name == "nt" else ""
    return _venv_bin_dir(managed_mcp_venv_dir(name, home)) / f"{spec.console_script}{suffix}"


def _managed_command_in_venv(spec: ManagedMCPServerSpec, venv_dir: Path) -> Path:
    """Locate one server's console command in an explicitly selected generation."""
    return _venv_bin_dir(venv_dir) / f"{spec.console_script}{'.exe' if os.name == 'nt' else ''}"


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
    """Atomically replace complete YAML while retaining its comments and quotes."""
    handle = io.StringIO()
    yaml.dump(payload, handle)
    atomic_managed_config_write(path, handle.getvalue())
    # This writes the managed config file directly (preserving comments via ruamel) rather than
    # through config.save_config(), so it must drop the in-process load_config() cache itself —
    # otherwise a later load_config() in the same process returns the pre-bootstrap config.
    if path.name == get_config_path().name:
        invalidate_config_cache()


def _ensure_managed_server_entry(
    entry: CommentedMap, *, spec: ManagedMCPServerSpec, home: Path, venv_dir: Path
) -> None:
    entry["command"] = str(_managed_command_in_venv(spec, venv_dir))
    if "args" not in entry:
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
    if "enabled" not in entry:
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


def write_managed_mcp_config(
    home: str | os.PathLike[str] | None = None,
    *,
    venv_dirs: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Publish explicit managed targets while preserving the latest user configuration."""
    home_path = Path(home).expanduser().resolve() if home else ensure_leanflow_home()
    home_path.mkdir(parents=True, exist_ok=True)
    _secure_dir(home_path)
    with managed_mcp_install_lock(home_path):
        return _write_managed_mcp_config_locked(home_path, venv_dirs=venv_dirs)


def _write_managed_mcp_config_locked(
    home_path: Path, *, venv_dirs: Mapping[str, Path] | None
) -> dict[str, Any]:
    """Read the current document only after candidate environments are validated."""
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
        venv_dir = (
            Path(venv_dirs[spec.name])
            if venv_dirs is not None and spec.name in venv_dirs
            else managed_mcp_venv_dir(spec.name, home_path)
        )
        if venv_dir.name != spec.venv_name:
            raise ValueError(f"Invalid managed environment name for {spec.name}")
        try:
            venv_dir.resolve().relative_to((managed_mcp_root(home_path) / "venvs").resolve())
            _managed_command_in_venv(spec, venv_dir).resolve().relative_to(venv_dir.resolve())
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"Managed environment or command escapes the installation root: {venv_dir}"
            ) from exc
        _ensure_managed_server_entry(existing, spec=spec, home=home_path, venv_dir=venv_dir)
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
    """Create a fresh environment without repairing or deleting any existing one."""
    if venv_dir.exists() or venv_dir.is_symlink():
        raise RuntimeError(f"Refusing to modify an existing managed MCP environment: {venv_dir}")
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    _secure_dir(venv_dir.parent)
    existing_python = _venv_bin_dir(venv_dir) / ("python.exe" if os.name == "nt" else "python")

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
    """Install only into a new permanent environment path."""
    if venv_dir.exists() or venv_dir.is_symlink():
        raise RuntimeError(f"Refusing to upgrade an existing managed MCP environment: {venv_dir}")
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
    subprocess.run(
        [
            str(python_path),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--quiet",
            "--upgrade",
            install_spec,
            *extra_install_specs,
            MCP_REQUIREMENT,
        ],
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


def _patch_lean_proof_auto_stdio_logging(venv_dir: Path) -> bool:
    """Route lean-interact's Rich logger to stderr for JSON-RPC stdio safety.

    ``lean-interact`` installs a module-level ``RichHandler`` whose default
    console writes to stdout.  The proof-auto MCP server later emits REPL/Git
    warnings through that logger, corrupting its protocol stream even though
    LeanFlow separately captures subprocess stderr.  Patch the managed isolated
    dependency at bootstrap so diagnostics remain available in the MCP stderr
    log while stdout contains JSON-RPC frames only.
    """
    candidates = [
        *venv_dir.glob("lib/python*/site-packages/lean_interact/utils.py"),
        venv_dir / "Lib" / "site-packages" / "lean_interact" / "utils.py",
    ]
    target = next((candidate for candidate in candidates if candidate.is_file()), None)
    if target is None:
        return False
    text = target.read_text(encoding="utf-8")
    marker = "RichHandler(rich_tracebacks=True, console=Console(stderr=True))"
    if marker in text:
        return True
    import_needle = "from rich.logging import RichHandler\n"
    handler_needle = "handler = RichHandler(rich_tracebacks=True)\n"
    if import_needle not in text or handler_needle not in text:
        return False
    patched = text.replace(
        import_needle,
        "from rich.console import Console\nfrom rich.logging import RichHandler\n",
        1,
    ).replace(
        handler_needle,
        f"handler = {marker}\n",
        1,
    )
    target.write_text(patched, encoding="utf-8")
    return True


def _apply_lean_lsp_patches(venv_dir: Path) -> dict[str, bool]:
    """Apply every managed lean-lsp-mcp source patch and report which ones took."""
    from leanflow_cli.cli.loogle_index_compat import patch_lean_lsp_loogle_index_flags
    from leanflow_cli.cli.loogle_local import (
        patch_lean_lsp_loogle_build_lock,
        patch_lean_lsp_loogle_lifecycle,
    )

    return {
        "project_paths": _patch_lean_lsp_loogle_project_paths(venv_dir),
        "build_lock": patch_lean_lsp_loogle_build_lock(venv_dir),
        "lifecycle": patch_lean_lsp_loogle_lifecycle(venv_dir),
        "index_flags": patch_lean_lsp_loogle_index_flags(venv_dir),
    }


def repair_managed_mcp_patches(home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Rebuild, patch and validate fresh backends before selecting their generation.

    Reuse pip's package cache when available. Never mutate loaded package files
    or relocate an existing environment whose console-script paths are fixed.
    """
    home_path = Path(home).expanduser().resolve() if home else get_leanflow_home()
    result = bootstrap_lean_mcp(home=home_path)
    result["patches"] = {
        entry["name"]: {"installed": True, **entry.get("patches", {})}
        for entry in result["servers"]
        if entry["name"] in {"lean-lsp", "lean-proof-auto"}
    }
    return result


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
        from leanflow_cli.cli.loogle_local import loogle_cache_dir_for_project

        loogle_cache_dir = loogle_cache_dir_for_project(home_path, project_root)
    else:
        loogle_cache_dir = Path(
            str(
                lean_lsp_env.get("LEAN_LOOGLE_CACHE_DIR", "") or managed_loogle_cache_dir(home_path)
            )
        ).expanduser()
    loogle_supported = local_loogle_supported()
    loogle_configured = _truthy(lean_lsp_env.get("LEAN_LOOGLE_LOCAL"))
    loogle_toolchain = _read_lean_toolchain(loogle_cache_dir / "repo")
    project_toolchain = _read_lean_toolchain(project_root)
    loogle_toolchain_compatible = bool(
        not loogle_toolchain or not project_toolchain or loogle_toolchain == project_toolchain
    )
    # A matching toolchain is not enough: the managed client must also speak the
    # index flags of the binary it built, or its index build fails on every start.
    from leanflow_cli.cli.loogle_index_compat import managed_loogle_client_status

    loogle_client = managed_loogle_client_status(
        home_path, project_root=project_root, cache_dir=loogle_cache_dir
    )
    loogle_client_compatible = loogle_client.get("compatible")
    loogle_ready = bool(
        loogle_configured
        and loogle_toolchain_compatible
        and loogle_client_compatible is not False
        and loogle_cache_dir.is_dir()
        and any(loogle_cache_dir.iterdir())
    )
    if not loogle_supported:
        loogle_status = "unsupported"
    elif not loogle_configured:
        loogle_status = "disabled"
    elif not loogle_toolchain_compatible:
        loogle_status = "incompatible"
    elif loogle_client_compatible is False:
        loogle_status = "client-incompatible"
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
        "loogle_index_flags_compatible": loogle_client_compatible,
        "loogle_client": loogle_client,
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
    """Check selected commands and initialization receipts against current packages and patches."""
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
        try:
            arguments = _managed_server_arguments(spec, configured)
            receipt_status = managed_mcp_receipt_status(
                venv_dir,
                expected_fingerprint=_managed_mcp_fingerprint(spec),
                packages=_managed_mcp_packages(spec),
                arguments=arguments,
                installed_files=_managed_mcp_installed_files(spec, venv_dir),
            )
        except ValueError as exc:
            receipt_status = {"current": False, "reason": str(exc)}
        entry = {
            "name": spec.name,
            "role": spec.role,
            "managed": True,
            "venv_dir": str(venv_dir),
            "command": str(command_path),
            "installed": command_path.is_file()
            and (os.name == "nt" or os.access(command_path, os.X_OK)),
            "configured": bool(cfg),
            "command_matches": configured_command == str(command_path),
            "enabled": bool(cfg.get("enabled", True)) if cfg else False,
            "receipt": receipt_status,
        }
        if spec.name == "lean-lsp":
            entry["power_modes"] = power_status
        entry["healthy"] = bool(
            entry["installed"]
            and entry["configured"]
            and entry["command_matches"]
            and receipt_status["current"]
        )
        entry["bootstrap_recommended"] = not bool(entry["healthy"])
        status[spec.name] = entry
    return status


def _lean_lsp_env_from_home(home_path: Path) -> dict[str, Any]:
    """Read the persisted lean-lsp server env block from the LeanFlow config."""
    config_path = home_path / get_config_path().name
    _yaml, doc = _load_bootstrap_document(config_path)
    configured = doc.get("mcp_servers")
    configured = dict(configured) if isinstance(configured, Mapping) else {}
    return _server_env_from_config(configured, "lean-lsp")


def bootstrap_lean_mcp(
    *, home: str | os.PathLike[str] | None = None, python_bin: str | None = None
) -> dict[str, Any]:
    """Build and validate immutable permanent environments before atomic selection."""
    home_path = Path(home).expanduser().resolve() if home else ensure_leanflow_home()
    with managed_mcp_install_lock(home_path):
        return _bootstrap_lean_mcp_locked(home_path, python_bin=python_bin)


def _validate_managed_mcp_environment(
    spec: ManagedMCPServerSpec,
    venv_dir: Path,
    home_path: Path,
    *,
    args: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Check console execution, FastMCP imports and a real isolated MCP handshake."""
    command = _managed_command_in_venv(spec, venv_dir)
    _yaml, document = _load_bootstrap_document(home_path / get_config_path().name)
    configured = document.get("mcp_servers", {})
    user_env = _server_env_from_config(
        configured if isinstance(configured, Mapping) else {}, spec.name
    )
    # This protocol probe must not start a Lean project or build a Loogle index.
    # Project-specific functional checks remain the installation smoke gate.
    env = {
        **os.environ,
        **{str(key): str(value) for key, value in spec.env.items()},
        **{str(key): str(value) for key, value in user_env.items()},
        "LEAN_REPL": "false",
        "LEAN_LOOGLE_LOCAL": "false",
    }
    return validate_managed_mcp_candidate(
        spec.name,
        venv_dir,
        command,
        args=args if args is not None else _managed_server_arguments(spec, configured),
        env=env,
        cwd=home_path / "mcp",
    )


def _managed_server_arguments(spec: ManagedMCPServerSpec, configured: Any) -> tuple[str, ...]:
    """Preserve and validate explicit user arguments before initialization and selection."""
    entry = configured.get(spec.name, {}) if isinstance(configured, Mapping) else {}
    arguments = entry.get("args", spec.args) if isinstance(entry, Mapping) else spec.args
    if not isinstance(arguments, (list, tuple)) or not all(
        isinstance(argument, str) for argument in arguments
    ):
        raise ValueError(f"Managed MCP arguments must be a list of strings: {spec.name}")
    return tuple(arguments)


def _managed_mcp_packages(spec: ManagedMCPServerSpec) -> tuple[str, ...]:
    """Identify package metadata needed to verify the installed MCP backend."""
    return tuple(
        re.split(r"\s|==", requirement, maxsplit=1)[0]
        for requirement in (spec.install_spec, *spec.extra_install_specs)
    ) + ("mcp",)


def _managed_mcp_fingerprint(spec: ManagedMCPServerSpec) -> str:
    """Hash requested package settings and the source of every managed patch."""
    patches = []
    patch_constants: dict[str, str] = {}
    if spec.name == "lean-lsp":
        from leanflow_cli.cli import loogle_index_compat
        from leanflow_cli.cli.loogle_local import (
            patch_lean_lsp_loogle_build_lock,
            patch_lean_lsp_loogle_lifecycle,
        )

        patches = [
            _patch_lean_lsp_loogle_project_paths,
            patch_lean_lsp_loogle_build_lock,
            patch_lean_lsp_loogle_lifecycle,
            loogle_index_compat.patch_lean_lsp_loogle_index_flags,
        ]
        patch_constants["index_marker"] = loogle_index_compat._INDEX_FLAGS_MARKER
    elif spec.name == "lean-proof-auto":
        patches = [_patch_lean_proof_auto_stdio_logging]
    return mcp_install_fingerprint(
        {**spec.to_dict(), "mcp_requirement": MCP_REQUIREMENT, "patch_constants": patch_constants},
        (inspect.getsource(patch) for patch in patches),
    )


def _managed_mcp_installed_files(spec: ManagedMCPServerSpec, venv_dir: Path) -> list[Path]:
    """Fingerprint the command and concrete package files managed patches can change."""
    relatives = {
        "lean-lsp": ("lean_lsp_mcp/loogle.py", "lean_lsp_mcp/server.py"),
        "lean-proof-auto": ("lean_interact/utils.py",),
    }.get(spec.name, ())
    files = [_managed_command_in_venv(spec, venv_dir)]
    for relative in relatives:
        files.extend(venv_dir.glob(f"lib/python*/site-packages/{relative}"))
        windows = venv_dir / "Lib/site-packages" / relative
        if windows.is_file():
            files.append(windows)
    return files


def _generation_is_selected(home_path: Path, generation: Path) -> bool:
    """Retain a generation whenever configuration might already reference it."""
    try:
        _yaml, doc = _load_bootstrap_document(home_path / get_config_path().name)
        entries = doc.get("mcp_servers", {})
        if isinstance(entries, Mapping):
            for entry in entries.values():
                if isinstance(entry, Mapping):
                    command = Path(str(entry.get("command", "") or "")).expanduser()
                    if command.is_absolute() and command.resolve().is_relative_to(
                        generation.resolve()
                    ):
                        return True
        return False
    except Exception:
        return True


def _bootstrap_lean_mcp_locked(home_path: Path, *, python_bin: str | None) -> dict[str, Any]:
    """Install only into a newly owned generation and leave older paths untouched."""
    managed_config_write_target(home_path / get_config_path().name)
    managed_root = managed_mcp_root(home_path)
    (managed_root / "venvs").mkdir(parents=True, exist_ok=True)
    managed_loogle_cache_dir(home_path).mkdir(parents=True, exist_ok=True)
    _secure_dir(managed_root)
    _secure_dir(managed_root / "venvs")
    _secure_dir(managed_root / "cache")
    _secure_dir(managed_loogle_cache_dir(home_path))

    generation = managed_root / "venvs" / f"gen-{uuid.uuid4().hex}"
    generation.mkdir(mode=0o700)
    installed_servers: list[dict[str, Any]] = []
    venv_dirs: dict[str, Path] = {}
    validated_arguments: dict[str, tuple[str, ...]] = {}
    selected = False
    try:
        marker = generation / ".leanflow-mcp-generation.json"
        marker.write_text(
            json.dumps(
                {
                    "owner": "leanflow-mcp-bootstrap",
                    "generation": generation.name,
                    "state": "building",
                    "installer_pid": os.getpid(),
                }
            )
            + "\n"
        )
        _secure_file(marker)
        for spec in MANAGED_LEAN_MCP_SPECS.values():
            venv_dir = generation / spec.venv_name
            _install_into_managed_venv(
                venv_dir,
                spec.install_spec,
                python_bin=python_bin,
                min_python=spec.min_python,
                extra_install_specs=spec.extra_install_specs,
            )
            patches: dict[str, bool] = {}
            if spec.name == "lean-lsp":
                patches = _apply_lean_lsp_patches(venv_dir)
            elif spec.name == "lean-proof-auto":
                patches["stdio_logging"] = _patch_lean_proof_auto_stdio_logging(venv_dir)
            required_patches = {
                "lean-lsp": ("project_paths", "build_lock", "lifecycle", "index_flags"),
                "lean-proof-auto": ("stdio_logging",),
            }.get(spec.name, ())
            failed_patches = [name for name in required_patches if patches.get(name) is not True]
            if failed_patches:
                raise RuntimeError(
                    f"Required managed MCP patches failed for {spec.name}: "
                    + ", ".join(failed_patches)
                )
            _yaml, document = _load_bootstrap_document(home_path / get_config_path().name)
            arguments = _managed_server_arguments(spec, document.get("mcp_servers", {}))
            validation = _validate_managed_mcp_environment(
                spec, venv_dir, home_path, args=arguments
            )
            write_managed_mcp_receipt(
                venv_dir,
                expected_fingerprint=_managed_mcp_fingerprint(spec),
                packages=_managed_mcp_packages(spec),
                installed_files=_managed_mcp_installed_files(spec, venv_dir),
                arguments=arguments,
                validation=validation,
            )
            validated_arguments[spec.name] = arguments
            venv_dirs[spec.name] = venv_dir
            installed_servers.append(
                {
                    "name": spec.name,
                    "role": spec.role,
                    "venv_dir": str(venv_dir),
                    "command": str(_managed_command_in_venv(spec, venv_dir)),
                    "install_spec": spec.install_spec,
                    "patches": patches,
                    "validation": validation,
                }
            )
        _yaml, document = _load_bootstrap_document(home_path / get_config_path().name)
        for spec in MANAGED_LEAN_MCP_SPECS.values():
            if (
                _managed_server_arguments(spec, document.get("mcp_servers", {}))
                != validated_arguments[spec.name]
            ):
                raise ValueError(
                    f"Managed MCP arguments changed during initialization: {spec.name}"
                )
        config_result = write_managed_mcp_config(home_path, venv_dirs=venv_dirs)
        selected = True
        with contextlib.suppress(OSError):
            atomic_managed_config_write(
                marker,
                json.dumps(
                    {
                        "owner": "leanflow-mcp-bootstrap",
                        "generation": generation.name,
                        "state": "selected",
                    }
                )
                + "\n",
            )
    except BaseException as exc:
        uncertain = isinstance(
            exc,
            (
                subprocess.TimeoutExpired,
                TimeoutError,
                KeyboardInterrupt,
                SystemExit,
                MCPValidationCleanupUncertain,
            ),
        )
        if uncertain:
            with contextlib.suppress(OSError):
                atomic_managed_config_write(
                    marker,
                    json.dumps(
                        {
                            "owner": "leanflow-mcp-bootstrap",
                            "generation": generation.name,
                            "state": "failed-cleanup-uncertain",
                            "failure_kind": type(exc).__name__,
                        }
                    )
                    + "\n",
                )
        elif not selected and not _generation_is_selected(home_path, generation):
            shutil.rmtree(generation, ignore_errors=True)
        raise
    power_status = managed_mcp_power_status(home_path)
    for entry in installed_servers:
        if entry.get("name") == "lean-lsp":
            entry["power_modes"] = power_status
    return {
        "success": True,
        "home": str(home_path),
        "managed_root": str(managed_root),
        "generation": generation.name,
        "config_path": config_result["config_path"],
        "remote_search_policy": REMOTE_SEARCH_POLICY,
        "power_modes": power_status,
        "servers": installed_servers,
    }
