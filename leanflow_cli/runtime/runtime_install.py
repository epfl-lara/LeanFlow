"""Prepare immutable Python runtimes and switch future CLI invocations safely."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from core.home import leanflow_home
from leanflow_cli.runtime.mcp_protocol import mcp_protocol_ready

_MARKER = ".leanflow-storage.json"
_SOURCE_DIRS = (
    "agent",
    "core",
    "leanflow_cli",
    "tools",
    "leanflow_skills",
    "leanflow_specs",
    "scripts",
)
_SOURCE_FILES = (
    "pyproject.toml",
    "README.md",
    "leanflow_agent.py",
    "run_agent.py",
    "model_tools.py",
    "toolsets.py",
    "utils.py",
)
_EXCLUDED = {"__pycache__", ".lake", ".venv", "node_modules", ".git"}


@dataclass(frozen=True)
class InstallPlan:
    """Describe a newly prepared runtime or an unchanged completed runtime."""

    venv_dir: Path
    identity: str
    reuse: bool
    managed: bool


def source_identity(source: Path, *, mode: str = "editable") -> str:
    """Hash installed source inputs without depending on Git or copying caches."""
    source = source.resolve()
    digest = hashlib.sha256()
    digest.update(
        f"v1:{sys.version_info[:2]}:{platform.system()}:{platform.machine()}:{mode}".encode()
    )
    files = [source / name for name in _SOURCE_FILES if (source / name).is_file()]
    for name in _SOURCE_DIRS:
        directory = source / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        for current, directories, names in os.walk(directory, followlinks=False):
            directories[:] = sorted(
                item
                for item in directories
                if item not in _EXCLUDED and not (Path(current) / item).is_symlink()
            )
            files.extend(
                Path(current) / item
                for item in names
                if not item.endswith((".pyc", ".pyo")) and not (Path(current) / item).is_symlink()
            )
    for path in sorted(files):
        digest.update(path.relative_to(source).as_posix().encode())
        digest.update(b"\0")
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def _marker(path: Path) -> dict[str, object]:
    """Read a valid ownership marker without following a linked marker."""
    marker = path / _MARKER
    if marker.is_symlink():
        return {}
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def active_runtime_paths() -> tuple[str, ...]:
    """Read process commands conservatively; fail closed if inspection fails."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Cannot inspect active runtimes; choose a fresh venv path.") from exc
    return tuple(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith(f"{os.getpid()} ")
    )


def _reject_existing(path: Path) -> None:
    """Reject in-place replacement, with a specific diagnostic for a live runtime."""
    commands = active_runtime_paths()
    candidates = {str(path.absolute()), str(path.resolve())}
    if any(candidate in command for command in commands for candidate in candidates):
        raise RuntimeError(f"Runtime {path} is in use; installation will not modify it.")
    raise RuntimeError(
        f"Runtime {path} already exists. Choose a fresh --venv-dir; "
        "in-place upgrades and deletion are disabled to protect running jobs."
    )


def prepare_install(
    source: Path,
    *,
    explicit_venv: Path | None = None,
    recreate: bool = False,
    mode: str = "editable",
    installer_pid: int | None = None,
    local_search: bool = False,
) -> InstallPlan:
    """Claim a fresh permanent runtime without changing an existing environment."""
    from leanflow_cli.runtime.storage import (
        StoragePolicy,
        ensure_storage_capacity,
        register_entry,
        storage_lock,
    )

    source = source.resolve()
    home = leanflow_home().resolve()
    identity = source_identity(source, mode=f"{mode}:local-search" if local_search else mode)
    managed = explicit_venv is None
    runtime = home / "runtimes" / identity
    venv = explicit_venv.expanduser().absolute() if explicit_venv else runtime / "venv"
    # Fresh Python installations cannot load YAML yet. Admission still uses the
    # safe defaults; the installed CLI subsequently reads configured limits.
    bootstrap_policy = StoragePolicy() if importlib.util.find_spec("yaml") is None else None
    ensure_storage_capacity(venv, policy=bootstrap_policy)
    with storage_lock():
        if managed and runtime.exists():
            marker = _marker(runtime)
            if (
                not recreate
                and marker.get("kind") == "runtime"
                and marker.get("ready") is True
                and marker.get("source_root") == str(source)
                and marker.get("install_mode") == mode
                and (venv / "bin" / "python").is_file()
            ):
                return InstallPlan(venv, identity, True, True)
            # A runtime's shebangs contain this permanent path. A fresh sibling
            # also avoids changing imports underneath another running process.
            runtime = home / "runtimes" / f"{identity}-{uuid.uuid4().hex[:8]}"
            venv = runtime / "venv"
        if not managed and (venv.exists() or venv.is_symlink()):
            _reject_existing(venv)
        if managed:
            runtime.mkdir(parents=True)
            register_entry(
                runtime,
                kind="runtime",
                identity={"source": identity, "mode": mode},
                metadata={
                    "ready": False,
                    "installer_pid": installer_pid or os.getpid(),
                    "source_root": str(source),
                    "venv_dir": str(venv),
                    "install_mode": mode,
                },
            )
    return InstallPlan(venv, identity, False, managed)


def _atomic_text(path: Path, text: str, *, executable: bool = False) -> None:
    """Replace one local text artifact only after its complete contents are durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    staging = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        staging.chmod(0o755 if executable else 0o600)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def finalize_install(source: Path, venv: Path, bin_dir: Path) -> None:
    """Publish a validated runtime for future commands, retaining previous runtimes."""
    from leanflow_cli.runtime.storage import register_entry, storage_lock

    source, venv, bin_dir = source.resolve(), venv.absolute(), bin_dir.absolute()
    executable = venv / "bin" / "leanflow"
    if not executable.is_file():
        raise RuntimeError(f"Missing installed LeanFlow executable: {executable}")
    home = leanflow_home().resolve()
    wrapper = bin_dir / "leanflow"
    metadata = home / "install-root"
    wrapper_text = (
        "#!/usr/bin/env bash\n"
        f'if [[ -z "${{LEANFLOW_HOME:-}}" ]]; then LEANFLOW_HOME={shlex.quote(str(home))}; fi\n'
        "export LEANFLOW_HOME\n"
        f'export PATH={shlex.quote(str(bin_dir))}:"$PATH"\n'
        f'exec {shlex.quote(str(executable))} "$@"\n'
    )
    metadata_text = (
        f"repo_root={source}\nvenv_dir={venv}\nbin_dir={bin_dir}\n"
        f"installed_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
    )
    with storage_lock():
        old_wrapper = wrapper.read_text(encoding="utf-8") if wrapper.is_file() else None
        _atomic_text(wrapper, wrapper_text, executable=True)
        try:
            _atomic_text(metadata, metadata_text)
        except OSError:
            if old_wrapper is None:
                wrapper.unlink(missing_ok=True)
            else:
                _atomic_text(wrapper, old_wrapper, executable=True)
            raise
        runtime = venv.parent
        marker = _marker(runtime)
        if marker.get("kind") == "runtime":
            identity = marker.get("identity")
            register_entry(
                runtime,
                kind="runtime",
                identity=identity if isinstance(identity, dict) else None,
                metadata={
                    "ready": True,
                    "installer_pid": None,
                    "source_root": str(source),
                    "venv_dir": str(venv),
                    "install_mode": marker.get("install_mode", "editable"),
                },
            )
        release = source.parent
        if source.name == "source" and release.parent == home / "releases":
            register_entry(
                release,
                kind="runtime-release",
                metadata={"source_root": str(source)},
            )


def ensure_backends() -> None:
    """Reuse healthy MCP environments and refuse to modify running backends."""
    from leanflow_cli.cli.mcp_bootstrap import bootstrap_lean_mcp, managed_mcp_server_status

    status = managed_mcp_server_status()
    if status and all(
        item.get("healthy") and mcp_protocol_ready(Path(str(item["venv_dir"])))
        for item in status.values()
    ):
        return
    managed = str(leanflow_home().resolve() / "mcp" / "venvs")
    if any(managed in command for command in active_runtime_paths()):
        raise RuntimeError(
            "Managed MCP backends are in use and need repair; "
            "installation will not modify them while jobs are running."
        )
    bootstrap_lean_mcp()


@contextmanager
def running_runtime_lease() -> Iterator[None]:
    """Pin this process's managed interpreter and source release until it exits."""
    from leanflow_cli.runtime.storage import lease_entry

    with ExitStack() as stack:
        runtime = Path(sys.prefix).absolute().parent
        if runtime.parent.name == "runtimes" and _marker(runtime).get("kind") == "runtime":
            # LEANFLOW_HOME may select a separate state/cache home for this run;
            # the interpreter remains owned by its installation's original home.
            stack.enter_context(lease_entry(runtime, home=runtime.parent.parent))
        source = Path(__file__).resolve().parents[2]
        release = source.parent
        if release.parent.name == "releases" and _marker(release).get("kind") == "runtime-release":
            stack.enter_context(lease_entry(release, home=release.parent.parent))
        yield


def main() -> None:
    """Expose installer preparation and publication without shell interpolation."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--venv-dir", type=Path)
    prepare.add_argument("--recreate", action="store_true")
    prepare.add_argument("--mode", choices=("editable", "wheel"), default="editable")
    prepare.add_argument("--installer-pid", type=int)
    prepare.add_argument("--local-search", action="store_true")
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--source", type=Path, required=True)
    finalize.add_argument("--venv-dir", type=Path, required=True)
    finalize.add_argument("--bin-dir", type=Path, required=True)
    subparsers.add_parser("ensure-backends")
    arguments = parser.parse_args()
    try:
        if arguments.command == "prepare":
            plan = prepare_install(
                arguments.source,
                explicit_venv=arguments.venv_dir,
                recreate=arguments.recreate,
                mode=arguments.mode,
                installer_pid=arguments.installer_pid,
                local_search=arguments.local_search,
            )
            print("reuse" if plan.reuse else "new")
            print(plan.venv_dir)
        elif arguments.command == "finalize":
            finalize_install(arguments.source, arguments.venv_dir, arguments.bin_dir)
        else:
            ensure_backends()
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"LeanFlow installation refused: {exc}\n")


if __name__ == "__main__":
    main()
