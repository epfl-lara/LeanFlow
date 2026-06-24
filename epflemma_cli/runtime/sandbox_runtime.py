"""Container-backed EPFLemma sandbox runtime."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from epflemma_cli.config import get_epflemma_home, load_config
from epflemma_cli.workflows.project import EPFLemmaProject, discover_epflemma_project

DEFAULT_SANDBOX_IMAGE = "epflemma/sandbox:local"
DEFAULT_CONTAINERFILE = "containers/epflemma-sandbox.Containerfile"
WORKFLOW_ALIASES = {
    "draft",
    "review",
    "checkpoint",
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
    ".epflemma-venv",
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
EPFLEMMA_EXCLUDES = {
    "cache",
    "runtime",
    "workflow-state",
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
    project: EPFLemmaProject
    run_dir: Path
    worktree: Path
    patch_path: Path
    status_path: Path
    command: tuple[str, ...]


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]  # epflemma_cli/runtime/X.py -> repo root


def default_sandbox_root() -> Path:
    return get_epflemma_home() / "sandbox"


def default_env_file() -> Path:
    return get_epflemma_home() / ".env"


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
    """Build SandboxSettings from config file and optional overrides, resolving paths and defaults. Merges epflemma.sandbox config dict with provided engine/image/env_file/network parameters, filling missing values from fallbacks."""
    config = load_config()
    epflemma = config.get("epflemma") if isinstance(config, Mapping) else {}
    sandbox = epflemma.get("sandbox") if isinstance(epflemma, Mapping) else {}
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
    raise SandboxRuntimeError("Install Docker or Podman before using `epflemma sandbox`.")


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


def normalize_epflemma_args(args: Sequence[str]) -> tuple[str, ...]:
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
        if rel == Path(".epflemma") and name in EPFLEMMA_EXCLUDES:
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
    _git(["config", "user.email", "sandbox@epflemma.local"], worktree)
    _git(["config", "user.name", "EPFLemma Sandbox"], worktree)
    _git(["add", "-A"], worktree)
    _git(["commit", "-q", "--allow-empty", "-m", "epflemma sandbox baseline"], worktree)


def prepare_sandbox_run(
    *,
    active_cwd: Path,
    command_args: Sequence[str],
    run_id: str | None = None,
    settings: SandboxSettings | None = None,
) -> SandboxRun:
    settings = settings or settings_from_config()
    project = discover_epflemma_project(active_cwd)
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
        command=normalize_epflemma_args(command_args),
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
    command.extend(["--build-arg", f"EPFLEMMA_SANDBOX_EXTRAS={extras}"])
    command.append(str(repo))
    return subprocess.call(command)


def _mount_arg(source: Path, target: str, *, read_only: bool = False) -> list[str]:
    source = source.expanduser().resolve()
    mode = ",readonly" if read_only else ""
    return ["--mount", f"type=bind,src={source},dst={target}{mode}"]


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
    home_dir.mkdir(parents=True, exist_ok=True)

    command = [engine, "run", "--rm", "-i"]
    use_tty = bool(tty) if tty is not None else sys.stdin.isatty()
    if use_tty:
        command.append("-t")
    command.extend(["--name", f"epflemma-{sandbox_run.run_id}"])
    if settings.read_only_root:
        command.append("--read-only")
    command.extend(["--workdir", "/workspace"])
    command.extend(_mount_arg(sandbox_run.worktree, "/workspace"))
    command.extend(_mount_arg(sandbox_run.run_dir, "/sandbox-run"))
    command.extend(_mount_arg(cache_dir, "/epflemma-cache"))
    command.extend(_mount_arg(home_dir, "/epflemma-home"))
    command.extend(["--tmpfs", "/tmp:rw,nosuid,nodev,size=1g"])
    command.extend(["--tmpfs", "/run:rw,nosuid,nodev,noexec,size=128m"])
    command.extend(["--env", "EPFLEMMA_HOME=/epflemma-home"])
    command.extend(["--env", "HOME=/epflemma-home"])
    command.extend(["--env", "EPFLEMMA_SANDBOX=1"])
    command.extend(["--env", f"EPFLEMMA_SANDBOX_RUN_ID={sandbox_run.run_id}"])
    command.extend(["--env", "ELAN_HOME=/epflemma-cache/elan"])
    command.extend(["--env", "XDG_CACHE_HOME=/epflemma-cache/xdg"])
    command.extend(["--env", "PIP_CACHE_DIR=/epflemma-cache/pip"])
    if settings.env_file.exists():
        command.extend(["--env-file", str(settings.env_file.expanduser().resolve())])
    if not settings.network:
        command.extend(["--network", "none"])
    if engine == "podman":
        command.extend(["--userns=keep-id"])
    elif uid is not None and gid is not None:
        command.extend(["--user", f"{uid}:{gid}"])
    command.extend(["--cap-drop=ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512"])

    bootstrap = ""
    if settings.bootstrap_mcp:
        bootstrap = (
            'if [ ! -f "$EPFLEMMA_HOME/.sandbox-bootstrap-ok" ]; then '
            '/opt/epflemma/.venv/bin/epflemma mcp bootstrap lean && touch "$EPFLEMMA_HOME/.sandbox-bootstrap-ok"; '
            "fi; "
        )
    command.extend(
        [
            image,
            "bash",
            "-lc",
            bootstrap + 'exec /opt/epflemma/.venv/bin/epflemma "$@"',
            "epflemma-sandbox",
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
    """Execute an EPFLemma workflow in a container: prepare worktree, verify image, launch container, export patch, write status.json. Returns the container's exit code; writes patch and status artifacts to run_dir."""
    settings = settings_from_config(engine=engine, image=image, env_file=env_file, network=network)
    resolved_engine = resolve_container_engine(settings.engine)
    ensure_container_engine_usable(resolved_engine)
    if not image_exists(resolved_engine, settings.image):
        raise SandboxRuntimeError(
            f"Sandbox image `{settings.image}` is not built. Run `epflemma sandbox build` first."
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
) -> dict[str, Any]:
    """Probe container engine, image availability, and recent sandbox runs; return aggregated status dict. Includes engine_ready flag, image_ready flag, and last 8 status.json files sorted by mtime."""
    settings = settings_from_config(engine=engine, image=image, env_file=env_file)
    engine_error = ""
    resolved_engine = ""
    image_ready = False
    try:
        resolved_engine = resolve_container_engine(settings.engine)
        engine_error = check_container_engine_usable(resolved_engine)
        if not engine_error:
            image_ready = image_exists(resolved_engine, settings.image)
    except Exception as exc:
        engine_error = str(exc)
    runs_dir = settings.runs_dir.expanduser()
    runs: list[dict[str, Any]] = []
    if runs_dir.exists():
        for status_path in sorted(
            runs_dir.glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True
        )[:8]:
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            runs.append(payload)
    return {
        "engine": resolved_engine or settings.engine,
        "engine_requested": settings.engine,
        "engine_ready": bool(resolved_engine and not engine_error),
        "engine_error": engine_error,
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
    lines = ["EPFLemma sandbox status"]
    engine = str(payload.get("engine", "") or "[missing]")
    lines.append(f"- engine: {engine} ({'ready' if payload.get('engine_ready') else 'not ready'})")
    if payload.get("engine_error"):
        lines.append(f"  error: {payload.get('engine_error')}")
    lines.append(
        f"- image: {payload.get('image')} ({'built' if payload.get('image_ready') else 'missing'})"
    )
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
