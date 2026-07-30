"""Container-backed LeanFlow sandbox runtime."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from core.filesystem import ensure_directory
from leanflow_cli.config import get_leanflow_home, load_config
from leanflow_cli.workflows.project import LeanFlowProject, discover_leanflow_project
from tools.utilities.repository_research_policy import (
    CLEAN_ROOM_TASK_LABELS_ENV,
    DISABLE_REPOSITORY_RESEARCH_ENV,
    DISABLE_SOLUTION_RESEARCH_ENV,
    clean_room_task_labels,
    repository_research_disabled,
    solution_research_disabled,
)

DEFAULT_SANDBOX_IMAGE = "leanflow/sandbox:local"
DEFAULT_CONTAINERFILE = "containers/leanflow-sandbox.Containerfile"
SANDBOX_BASE_IMAGE_ENV = "LEANFLOW_SANDBOX_BASE_IMAGE"
WORKFLOW_ALIASES = {
    "draft",
    "review",
    "refactor",
    "golf",
    "prove",
    "formalize",
    "autoprove",
    "autoformalize",
}
ROOT_EXCLUDES = {
    ".DS_Store",
    ".env",
    ".leanflow-venv",
    ".git",
    ".hg",
    ".lake",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}
LEANFLOW_EXCLUDES = {
    "cache",
    "runtime",
    "workflow-state",
    "workspace",
}


class SandboxRuntimeError(RuntimeError):
    """Raised when a sandbox run cannot be prepared or launched."""


@dataclasses.dataclass(frozen=True)
class SandboxSettings:
    engine: str
    image: str
    env_file: Path
    cache_dir: Path
    runs_dir: Path
    network: bool = True
    read_only_root: bool = True
    bootstrap_mcp: bool = True


@dataclasses.dataclass(frozen=True)
class SandboxRun:
    run_id: str
    project: LeanFlowProject
    run_dir: Path
    worktree: Path
    patch_path: Path
    status_path: Path
    command: tuple[str, ...]


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]  # leanflow_cli/runtime/X.py -> repo root


def default_sandbox_root() -> Path:
    return get_leanflow_home() / "sandbox"


def default_env_file() -> Path:
    return get_leanflow_home() / ".env"


def _path_from_config(value: Any, fallback: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    return Path(raw).expanduser()


def settings_from_config(
    *,
    engine: str | None = None,
    image: str | None = None,
    env_file: str | Path | None = None,
    network: bool | None = None,
) -> SandboxSettings:
    """Build SandboxSettings from config file and optional overrides, resolving paths and defaults. Merges leanflow.sandbox config dict with provided engine/image/env_file/network parameters, filling missing values from fallbacks."""
    config = load_config()
    leanflow = config.get("leanflow") if isinstance(config, Mapping) else {}
    sandbox = leanflow.get("sandbox") if isinstance(leanflow, Mapping) else {}
    if not isinstance(sandbox, Mapping):
        sandbox = {}
    root = default_sandbox_root()
    resolved_engine = str(engine or sandbox.get("engine") or "auto").strip() or "auto"
    resolved_image = (
        str(image or sandbox.get("image") or DEFAULT_SANDBOX_IMAGE).strip() or DEFAULT_SANDBOX_IMAGE
    )
    resolved_env = (
        Path(env_file).expanduser()
        if env_file
        else _path_from_config(sandbox.get("env_file"), default_env_file())
    )
    cache_dir = _path_from_config(sandbox.get("cache_dir"), root / "cache")
    runs_dir = _path_from_config(sandbox.get("runs_dir"), root / "runs")
    cfg_network = sandbox.get("network", True)
    resolved_network = bool(cfg_network) if network is None else bool(network)
    return SandboxSettings(
        engine=resolved_engine,
        image=resolved_image,
        env_file=resolved_env,
        cache_dir=cache_dir,
        runs_dir=runs_dir,
        network=resolved_network,
        read_only_root=bool(sandbox.get("read_only_root", True)),
        bootstrap_mcp=bool(sandbox.get("bootstrap_mcp", True)),
    )


def resolve_container_engine(requested: str = "auto") -> str:
    requested = (requested or "auto").strip().lower()
    if requested not in {"auto", "docker", "podman"}:
        raise SandboxRuntimeError(f"Unsupported sandbox engine: {requested}")
    if requested != "auto":
        if not shutil.which(requested):
            raise SandboxRuntimeError(
                f"Sandbox engine `{requested}` is not installed or not on PATH."
            )
        return requested
    installed: list[str] = []
    if sys.platform.startswith("linux") and shutil.which("podman"):
        installed.append("podman")
    if shutil.which("docker"):
        installed.append("docker")
    if not sys.platform.startswith("linux") and shutil.which("podman"):
        installed.append("podman")
    for candidate in installed:
        if not check_container_engine_usable(candidate):
            return candidate
    if installed:
        return installed[0]
    raise SandboxRuntimeError("Install Docker or Podman before using `leanflow sandbox`.")


def check_container_engine_usable(engine: str) -> str:
    try:
        result = subprocess.run(
            [engine, "info"],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return f"Sandbox engine `{engine}` did not answer `info` within 15 seconds."
    except Exception as exc:
        return f"Sandbox engine `{engine}` is not usable: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"{engine} info exited with {result.returncode}"
        return f"Sandbox engine `{engine}` is installed but not usable: {message}"
    return ""


def ensure_container_engine_usable(engine: str) -> None:
    error = check_container_engine_usable(engine)
    if error:
        raise SandboxRuntimeError(error)


def normalize_leanflow_args(args: Sequence[str]) -> tuple[str, ...]:
    cleaned = [arg for arg in args]
    if cleaned and cleaned[0] == "--":
        cleaned = cleaned[1:]
    if not cleaned:
        return tuple()
    first = cleaned[0].strip()
    if first.startswith("/"):
        first = first[1:]
        cleaned = [first, *cleaned[1:]]
    if first in WORKFLOW_ALIASES:
        return tuple(["workflow", first, *cleaned[1:]])
    return tuple(cleaned)


def _should_ignore(src_dir: Path, project_root: Path, names: Iterable[str]) -> set[str]:
    ignored: set[str] = set()
    try:
        rel = src_dir.relative_to(project_root)
    except ValueError:
        rel = Path()
    for name in names:
        if name in ROOT_EXCLUDES and rel == Path():
            ignored.add(name)
            continue
        if name.endswith(".pyc") or name == "__pycache__":
            ignored.add(name)
            continue
        if rel == Path(".leanflow") and name in LEANFLOW_EXCLUDES:
            ignored.add(name)
    return ignored


def copy_project_tree(project_root: Path, destination: Path) -> None:
    project_root = project_root.resolve()
    if destination.exists():
        raise SandboxRuntimeError(f"Sandbox worktree already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        project_root,
        destination,
        ignore=lambda src, names: _should_ignore(Path(src), project_root, names),
        symlinks=True,
    )


def _git(
    args: Sequence[str], cwd: Path, *, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _commit_sandbox_baseline(worktree: Path) -> None:
    if not shutil.which("git"):
        raise SandboxRuntimeError("git is required to record and export sandbox patches.")
    _git(["init", "-q"], worktree)
    _git(["config", "user.email", "sandbox@leanflow.local"], worktree)
    _git(["config", "user.name", "LeanFlow Sandbox"], worktree)
    _git(["add", "-A"], worktree)
    _git(["commit", "-q", "--allow-empty", "-m", "leanflow sandbox baseline"], worktree)


def prepare_sandbox_run(
    *,
    active_cwd: Path,
    command_args: Sequence[str],
    run_id: str | None = None,
    settings: SandboxSettings | None = None,
) -> SandboxRun:
    settings = settings or settings_from_config()
    project = discover_leanflow_project(active_cwd)
    resolved_run_id = (
        run_id or _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    run_dir = (settings.runs_dir / resolved_run_id).expanduser().resolve()
    worktree = run_dir / "worktree"
    run_dir.mkdir(parents=True, exist_ok=False)
    copy_project_tree(project.root, worktree)
    _commit_sandbox_baseline(worktree)
    return SandboxRun(
        run_id=resolved_run_id,
        project=project,
        run_dir=run_dir,
        worktree=worktree,
        patch_path=run_dir / "changes.patch",
        status_path=run_dir / "status.json",
        command=normalize_leanflow_args(command_args),
    )


def image_exists(engine: str, image: str) -> bool:
    result = subprocess.run(
        [engine, "image", "inspect", image],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_sandbox_image(
    *,
    engine: str | None = None,
    image: str | None = None,
    pull: bool = False,
    no_cache: bool = False,
    local_lean_explore: bool = False,
) -> int:
    settings = settings_from_config(engine=engine, image=image)
    resolved_engine = resolve_container_engine(settings.engine)
    ensure_container_engine_usable(resolved_engine)
    repo = repository_root()
    containerfile = repo / DEFAULT_CONTAINERFILE
    if not containerfile.exists():
        raise SandboxRuntimeError(f"Sandbox Containerfile not found: {containerfile}")
    command = [resolved_engine, "build", "-t", settings.image, "-f", str(containerfile)]
    if pull:
        command.append("--pull")
    if no_cache:
        command.append("--no-cache")
    extras = "mcp,lean-explore" if local_lean_explore else "mcp"
    command.extend(["--build-arg", f"LEANFLOW_SANDBOX_EXTRAS={extras}"])
    base_image = str(os.getenv(SANDBOX_BASE_IMAGE_ENV, "") or "").strip()
    if base_image:
        command.extend(["--build-arg", f"LEANFLOW_SANDBOX_BASE={base_image}"])
    command.append(str(repo))
    return subprocess.call(command)


def _mount_arg(source: Path, target: str, *, read_only: bool = False) -> list[str]:
    source = source.expanduser().resolve()
    mode = ",readonly" if read_only else ""
    return ["--mount", f"type=bind,src={source},dst={target}{mode}"]


def _sandbox_package_overlay_root(
    settings: SandboxSettings,
    project: LeanFlowProject,
) -> Path:
    """Return a dependency-revision-specific container package cache root."""
    fingerprint = hashlib.sha256()
    fingerprint.update(b"leanflow-sandbox-package-overlay-v2")
    fingerprint.update(str(project.root.resolve()).encode())
    for filename in ("lean-toolchain", "lake-manifest.json"):
        path = project.root / filename
        try:
            fingerprint.update(path.read_bytes())
        except OSError:
            continue
    return ensure_directory(
        settings.cache_dir.expanduser().resolve()
        / "lake-package-overlays"
        / fingerprint.hexdigest()[:20]
    )


def _sandbox_package_needs_overlay(package: Path) -> bool:
    """Return whether a package needs writable container-native build state."""
    return package.name.casefold() == "repl" or not (package / ".lake").is_dir()


def _prepare_sandbox_package_overlay(package: Path, cache_root: Path) -> Path:
    """Copy immutable package source once, excluding host VCS and build state."""
    destination = cache_root / package.name
    if destination.is_dir():
        return destination
    temporary = cache_root / f".{package.name}.{uuid.uuid4().hex}.tmp"
    shutil.copytree(
        package,
        temporary,
        symlinks=True,
        # Lake uses the dependency repository metadata to verify that the
        # manifest URL/revision still matches. Retain the small package-local
        # `.git`; clean-room policy still denies model Git commands.
        ignore=shutil.ignore_patterns(".lake", "__pycache__"),
    )
    try:
        temporary.rename(destination)
    except OSError as exc:
        if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not destination.is_dir():
            raise
        shutil.rmtree(temporary, ignore_errors=True)
    return destination


def _append_sandbox_package_overlays(
    command: list[str],
    *,
    settings: SandboxSettings,
    project: LeanFlowProject,
    project_packages: Path,
) -> None:
    """Overlay runtime packages with source-only, container-writable copies."""
    cache_root = _sandbox_package_overlay_root(settings, project)
    for package in sorted(project_packages.iterdir(), key=lambda path: path.name):
        if (
            not package.is_dir()
            or "," in package.name
            or not _sandbox_package_needs_overlay(package)
        ):
            continue
        package_overlay = _prepare_sandbox_package_overlay(package, cache_root)
        command.extend(
            _mount_arg(
                package_overlay,
                f"/workspace/.lake/packages/{package.name}",
            )
        )


def _command_requests_codex(command: Sequence[str]) -> bool:
    """Return whether a sandbox workflow explicitly selects the Codex provider."""
    for index, token in enumerate(command[:-1]):
        if token == "--provider" and command[index + 1].strip().lower() in {
            "codex",
            "openai-codex",
        }:
            return True
    return False


def _append_codex_auth_mounts(command: list[str], workflow_command: Sequence[str]) -> None:
    """Mount only Codex auth/config files for an explicitly Codex-backed sandbox."""
    if not _command_requests_codex(workflow_command):
        return
    configured = str(os.getenv("CODEX_HOME", "") or "").strip()
    codex_home = (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".codex").resolve()
    )
    mounted = False
    for filename in ("auth.json", "config.toml"):
        source = codex_home / filename
        if not source.is_file():
            continue
        command.extend(_mount_arg(source, f"/opt/leanflow/{filename}", read_only=True))
        mounted = True
    if mounted:
        command.extend(["--env", "CODEX_HOME=/opt/leanflow"])


def container_run_command(
    *,
    engine: str,
    image: str,
    sandbox_run: SandboxRun,
    settings: SandboxSettings,
    tty: bool | None = None,
) -> list[str]:
    """Build a container engine CLI invocation for a sandbox run with mounts, environment, security constraints, and optional MCP bootstrap. Returns the full [engine, run, ...] command array ready for subprocess.call()."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    gid = os.getgid() if hasattr(os, "getgid") else None
    cache_dir = settings.cache_dir.expanduser().resolve()
    home_dir = (default_sandbox_root() / "home").expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "tmp").mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)

    # Lean and REPL subprocesses may outlive the direct worker that launched
    # them during cancellation. Give the container a real PID 1 reaper so
    # exited descendants cannot accumulate as zombies during long campaigns.
    command = [engine, "run", "--rm", "--init", "-i"]
    use_tty = bool(tty) if tty is not None else sys.stdin.isatty()
    if use_tty:
        command.append("-t")
    command.extend(["--name", f"leanflow-{sandbox_run.run_id}"])
    if settings.read_only_root:
        command.append("--read-only")
    command.extend(["--workdir", "/workspace"])
    command.extend(_mount_arg(sandbox_run.worktree, "/workspace"))
    project_packages = sandbox_run.project.root / ".lake" / "packages"
    if project_packages.is_dir():
        # Reuse only third-party Lake dependencies. Project build output stays
        # excluded so a sandbox cannot inherit compiled target declarations.
        # Runtime packages such as `repl` are overlaid below with source-only,
        # container-writable copies so host executables are never reused as
        # Linux ones.
        command.extend(_mount_arg(project_packages, "/workspace/.lake/packages", read_only=True))
        _append_sandbox_package_overlays(
            command,
            settings=settings,
            project=sandbox_run.project,
            project_packages=project_packages,
        )
    command.extend(_mount_arg(sandbox_run.run_dir, "/sandbox-run"))
    command.extend(_mount_arg(cache_dir, "/leanflow-cache"))
    command.extend(_mount_arg(home_dir, "/leanflow-home"))
    _append_codex_auth_mounts(command, sandbox_run.command)
    command.extend(["--tmpfs", "/tmp:rw,nosuid,nodev,size=1g"])
    command.extend(["--tmpfs", "/run:rw,nosuid,nodev,noexec,size=128m"])
    command.extend(
        [
            "--tmpfs",
            (
                "/opt/leanflow/.venv/lib/python3.12/site-packages/lean_interact/cache:"
                "rw,nosuid,nodev,noexec,size=256m,mode=1777"
            ),
        ]
    )
    command.extend(["--env", "LEANFLOW_HOME=/leanflow-home"])
    command.extend(["--env", "HOME=/leanflow-home"])
    command.extend(["--env", "LEANFLOW_SANDBOX=1"])
    command.extend(["--env", f"LEANFLOW_SANDBOX_RUN_ID={sandbox_run.run_id}"])
    command.extend(["--env", "ELAN_HOME=/leanflow-cache/elan"])
    command.extend(["--env", "XDG_CACHE_HOME=/leanflow-cache/xdg"])
    command.extend(["--env", "PIP_CACHE_DIR=/leanflow-cache/pip"])
    command.extend(["--env", "TMPDIR=/leanflow-cache/tmp"])
    if settings.env_file.exists():
        command.extend(["--env-file", str(settings.env_file.expanduser().resolve())])
    # Append clean-room policy after the user env file so it cannot be weakened
    # accidentally by a stale setting in that file.
    if repository_research_disabled():
        command.extend(["--env", f"{DISABLE_REPOSITORY_RESEARCH_ENV}=1"])
        command.extend(["--env", "GIT_CONFIG_COUNT=1"])
        command.extend(["--env", "GIT_CONFIG_KEY_0=protocol.allow"])
        command.extend(["--env", "GIT_CONFIG_VALUE_0=never"])
    if solution_research_disabled():
        command.extend(["--env", f"{DISABLE_SOLUTION_RESEARCH_ENV}=1"])
        labels = "|".join(clean_room_task_labels())
        if labels:
            command.extend(["--env", f"{CLEAN_ROOM_TASK_LABELS_ENV}={labels}"])
    if not settings.network:
        command.extend(["--network", "none"])
    if engine == "podman":
        command.extend(["--userns=keep-id"])
    elif uid is not None and gid is not None:
        command.extend(["--user", f"{uid}:{gid}"])
    command.extend(["--cap-drop=ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512"])

    bootstrap_parts = ["set -e; "]
    if (project_packages / "repl").is_dir():
        bootstrap_parts.append(
            'if [ ! -x ".lake/packages/repl/.lake/build/bin/repl" ]; then '
            "echo 'Preparing sandbox-local Lean REPL...'; "
            "lake build repl; "
            "fi; "
        )
    if settings.bootstrap_mcp:
        bootstrap_parts.append(
            'if [ ! -f "$LEANFLOW_HOME/.sandbox-bootstrap-ok" ]; then '
            "/opt/leanflow/.venv/bin/leanflow mcp bootstrap lean "
            "|| { status=$?; echo 'LeanFlow sandbox MCP bootstrap failed.' >&2; exit \"$status\"; }; "
            'touch "$LEANFLOW_HOME/.sandbox-bootstrap-ok"; '
            "fi; "
        )
    bootstrap = "".join(bootstrap_parts)
    command.extend(
        [
            image,
            "bash",
            "-lc",
            bootstrap + 'exec /opt/leanflow/.venv/bin/leanflow "$@"',
            "leanflow-sandbox",
            *sandbox_run.command,
        ]
    )
    return command


def export_sandbox_patch(sandbox_run: SandboxRun) -> bool:
    result = _git(["diff", "--binary", "HEAD"], sandbox_run.worktree, capture=True)
    patch = result.stdout or ""
    sandbox_run.patch_path.write_text(patch, encoding="utf-8")
    (sandbox_run.run_dir / "git-status.txt").write_text(
        _git(["status", "--short"], sandbox_run.worktree, capture=True).stdout or "",
        encoding="utf-8",
    )
    return bool(patch.strip())


def _write_run_status(sandbox_run: SandboxRun, payload: Mapping[str, Any]) -> None:
    sandbox_run.status_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )


def run_sandbox(
    *,
    command_args: Sequence[str],
    active_cwd: Path | None = None,
    engine: str | None = None,
    image: str | None = None,
    env_file: str | Path | None = None,
    network: bool | None = None,
    tty: bool | None = None,
) -> int:
    """Execute an LeanFlow workflow in a container: prepare worktree, verify image, launch container, export patch, write status.json. Returns the container's exit code; writes patch and status artifacts to run_dir."""
    settings = settings_from_config(engine=engine, image=image, env_file=env_file, network=network)
    resolved_engine = resolve_container_engine(settings.engine)
    ensure_container_engine_usable(resolved_engine)
    if not image_exists(resolved_engine, settings.image):
        raise SandboxRuntimeError(
            f"Sandbox image `{settings.image}` is not built. Run `leanflow sandbox build` first."
        )
    sandbox_run = prepare_sandbox_run(
        active_cwd=(active_cwd or Path.cwd()).resolve(),
        command_args=command_args,
        settings=settings,
    )
    launch = container_run_command(
        engine=resolved_engine,
        image=settings.image,
        sandbox_run=sandbox_run,
        settings=settings,
        tty=tty,
    )
    started = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    _write_run_status(
        sandbox_run,
        {
            "run_id": sandbox_run.run_id,
            "status": "running",
            "engine": resolved_engine,
            "image": settings.image,
            "project_root": str(sandbox_run.project.root),
            "worktree": str(sandbox_run.worktree),
            "command": list(sandbox_run.command),
            "started_at": started,
        },
    )
    print(f"Sandbox run: {sandbox_run.run_id}")
    print(f"Worktree   : {sandbox_run.worktree}")
    print(f"Engine     : {resolved_engine} ({settings.image})")
    if not settings.env_file.exists():
        print(
            f"Env file   : missing ({settings.env_file}); provider keys must come from the process environment"
        )
    else:
        print(f"Env file   : {settings.env_file}")
    sys.stdout.flush()
    exit_code = subprocess.call(launch)
    has_patch = export_sandbox_patch(sandbox_run)
    finished = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    _write_run_status(
        sandbox_run,
        {
            "run_id": sandbox_run.run_id,
            "status": "succeeded" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "engine": resolved_engine,
            "image": settings.image,
            "project_root": str(sandbox_run.project.root),
            "worktree": str(sandbox_run.worktree),
            "patch_path": str(sandbox_run.patch_path),
            "patch_nonempty": has_patch,
            "command": list(sandbox_run.command),
            "started_at": started,
            "finished_at": finished,
        },
    )
    print(f"Patch      : {sandbox_run.patch_path if has_patch else '[no changes]'}")
    print(f"Status     : {sandbox_run.status_path}")
    return exit_code


def sandbox_status(
    *,
    engine: str | None = None,
    image: str | None = None,
    env_file: str | Path | None = None,
    probe_engine: bool = True,
    recent_run_limit: int = 8,
) -> dict[str, Any]:
    """Return sandbox configuration, bounded history, and optional engine probes."""
    settings = settings_from_config(engine=engine, image=image, env_file=env_file)
    engine_error = ""
    resolved_engine = ""
    engine_ready: bool | None = None
    image_ready: bool | None = None
    if probe_engine:
        try:
            resolved_engine = resolve_container_engine(settings.engine)
            engine_error = check_container_engine_usable(resolved_engine)
            engine_ready = bool(resolved_engine and not engine_error)
            image_ready = image_exists(resolved_engine, settings.image) if engine_ready else False
        except Exception as exc:
            engine_error = str(exc)
            engine_ready = False
            image_ready = False
    runs_dir = settings.runs_dir.expanduser()
    runs: list[dict[str, Any]] = []
    history_limit = max(0, min(100, int(recent_run_limit)))
    if runs_dir.exists():
        for status_path in sorted(
            runs_dir.glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True
        )[:history_limit]:
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            runs.append(payload)
    return {
        "engine": resolved_engine or settings.engine,
        "engine_requested": settings.engine,
        "engine_ready": engine_ready,
        "engine_error": engine_error,
        "engine_probe": "complete" if probe_engine else "skipped",
        "image": settings.image,
        "image_ready": image_ready,
        "env_file": str(settings.env_file),
        "env_file_exists": settings.env_file.exists(),
        "cache_dir": str(settings.cache_dir.expanduser()),
        "runs_dir": str(runs_dir),
        "network": settings.network,
        "read_only_root": settings.read_only_root,
        "bootstrap_mcp": settings.bootstrap_mcp,
        "recent_runs": runs,
    }


def format_sandbox_status(payload: Mapping[str, Any]) -> str:
    lines = ["LeanFlow sandbox status"]
    engine = str(payload.get("engine", "") or "[missing]")
    engine_ready = payload.get("engine_ready")
    engine_state = (
        "ready" if engine_ready is True else "not ready" if engine_ready is False else "not probed"
    )
    lines.append(f"- engine: {engine} ({engine_state})")
    if payload.get("engine_error"):
        lines.append(f"  error: {payload.get('engine_error')}")
    image_ready = payload.get("image_ready")
    image_state = (
        "built" if image_ready is True else "missing" if image_ready is False else "not probed"
    )
    lines.append(f"- image: {payload.get('image')} ({image_state})")
    lines.append(
        f"- env file: {payload.get('env_file')} ({'present' if payload.get('env_file_exists') else 'missing'})"
    )
    lines.append(f"- cache: {payload.get('cache_dir')}")
    lines.append(f"- runs: {payload.get('runs_dir')}")
    recent = list(payload.get("recent_runs", []) or [])
    if recent:
        lines.append("- recent runs:")
        for run in recent[:5]:
            lines.append(
                f"  {run.get('run_id', '[unknown]')}: {run.get('status', 'unknown')} "
                f"exit={run.get('exit_code', '-')}, patch={run.get('patch_path', '[none]')}"
            )
    else:
        lines.append("- recent runs: none")
    return "\n".join(lines)
