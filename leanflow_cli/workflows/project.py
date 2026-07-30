"""LeanFlow project discovery and local manifest management."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

LEANFLOW_PROJECT_DIRNAME = ".leanflow"
LEANFLOW_PROJECT_MANIFEST_FILENAME = "project.yaml"
LEANFLOW_PROJECT_SCHEMA_VERSION = 1
LEANFLOW_PROJECT_TEMPLATE_ENV = "LEANFLOW_BLUEPRINT_TEMPLATE_SOURCE"

_BLUEPRINT_MARKERS = (
    "lean-toolchain",
    "lakefile.lean",
    "lakefile.toml",
    "templates/blueprint.yml",
)


class LeanFlowProjectError(RuntimeError):
    """Base class for LeanFlow project failures."""


class ProjectNotFoundError(LeanFlowProjectError):
    """Raised when no active LeanFlow project can be found."""


class ProjectManifestError(LeanFlowProjectError):
    """Raised when a project manifest is missing or malformed."""


class ProjectCommandError(LeanFlowProjectError):
    """Raised when a project-management command cannot be completed."""


@dataclass(frozen=True)
class LeanFlowProject:
    root: Path
    leanflow_dir: Path
    manifest_path: Path
    name: str
    kind: str
    schema_version: int
    lean_root: Path
    runtime_dir: Path
    cache_dir: Path
    workflows_dir: Path
    created_at: str
    source_mode: str
    template_source: str
    blueprint_markers: tuple[str, ...]
    manifest: dict[str, Any]

    @property
    def label(self) -> str:
        return self.name or self.root.name


def format_project_summary(project: LeanFlowProject) -> str:
    return f"{project.label} ({project.root})"


ProgressCallback = Callable[[str], None]


def _emit_progress(progress: ProgressCallback | None, message: str) -> None:
    if progress:
        progress(message)


def _read_lean_toolchain_version(lean_root: Path) -> str:
    try:
        raw = (lean_root / "lean-toolchain").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if ":" in raw:
        return raw.rsplit(":", 1)[-1].strip()
    return raw


def _repl_dependency_present(lean_root: Path) -> bool:
    for lakefile in (lean_root / "lakefile.toml", lean_root / "lakefile.lean"):
        if not lakefile.is_file():
            continue
        try:
            text = lakefile.read_text(encoding="utf-8")
        except OSError:
            continue
        if (
            "leanprover-community/repl" in text
            or 'name = "repl"' in text
            or "name = 'repl'" in text
            or "require repl" in text
        ):
            return True
    return False


def _append_repl_to_lakefile_toml(lean_root: Path, rev: str) -> bool:
    lakefile = lean_root / "lakefile.toml"
    if not lakefile.is_file():
        return False
    text = lakefile.read_text(encoding="utf-8")
    if "leanprover-community/repl" in text or 'name = "repl"' in text or "name = 'repl'" in text:
        return False
    addition = (
        "\n"
        "[[require]]\n"
        'name = "repl"\n'
        'git = "https://github.com/leanprover-community/repl"\n'
        f'rev = "{rev}"\n'
    )
    lakefile.write_text(text.rstrip() + "\n" + addition, encoding="utf-8")
    return True


def _detect_project_repl_binary(lean_root: Path) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [
        lean_root / ".lake" / "build" / "bin" / f"repl{suffix}",
        lean_root / ".lake" / "packages" / "repl" / ".lake" / "build" / "bin" / f"repl{suffix}",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def _run_power_setup_command(command: list[str], *, cwd: Path) -> tuple[int, str, float]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=900,
        )
        return completed.returncode, completed.stdout or "", time.monotonic() - started
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return 124, output, time.monotonic() - started
    except Exception as exc:
        return 1, str(exc), time.monotonic() - started


def setup_project_power_modes(
    lean_root: str | Path,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Configure REPL acceleration by detecting and building the leanprover-community/repl dependency. Probes for an existing REPL binary, appends the dependency to lakefile.toml if absent, runs `lake update` and `lake build repl`, then returns a structured report (repl_available, repl_path, status, messages)."""
    root = Path(lean_root).expanduser().resolve()
    messages: list[str] = []

    def note(message: str) -> None:
        messages.append(message)
        _emit_progress(progress, message)

    report: dict[str, Any] = {
        "repl_configured": False,
        "repl_available": False,
        "repl_path": "",
        "status": "unknown",
        "messages": messages,
    }
    note("[1/6] Inspecting Lean project for REPL acceleration")
    version = _read_lean_toolchain_version(root)
    if version:
        note(f"[2/6] Detected Lean toolchain: {version}")
    else:
        note("[2/6] Lean toolchain version unavailable; skipping automatic REPL setup")
        report["status"] = "toolchain-missing"
        return report

    existing_repl = _detect_project_repl_binary(root)
    if existing_repl:
        note(f"[3/6] Existing REPL binary found: {existing_repl}")
        report.update(
            {
                "repl_configured": True,
                "repl_available": True,
                "repl_path": existing_repl,
                "status": "ready",
            }
        )
        return report

    note("[3/6] Checking Lake dependency for leanprover-community/repl")
    dependency_present = _repl_dependency_present(root)
    if not dependency_present:
        if (root / "lakefile.toml").is_file():
            note("[4/6] Adding REPL dependency to lakefile.toml")
            dependency_present = _append_repl_to_lakefile_toml(root, version)
        else:
            note(
                "[4/6] lakefile.lean detected; automatic REPL edit is not safe, leaving manual setup instructions"
            )
            report.update(
                {
                    "status": "manual-setup-needed",
                    "manual_steps": [
                        f'Add `require repl from git "https://github.com/leanprover-community/repl.git" @ "{version}"` to lakefile.lean.',
                        "Run `lake update repl`.",
                        "Run `lake build repl`.",
                    ],
                }
            )
            return report
    else:
        note("[4/6] REPL dependency already present")

    report["repl_configured"] = bool(dependency_present)
    if not shutil.which("lake"):
        note("[5/6] Lake executable not found; REPL dependency is configured but build is deferred")
        report["status"] = "lake-missing"
        return report

    note("[5/6] Running `lake update repl` (this may take a minute)")
    update_code, update_output, update_elapsed = _run_power_setup_command(
        ["lake", "update", "repl"], cwd=root
    )
    if update_code != 0:
        note(
            f"[5/6] `lake update repl` failed after {update_elapsed:.1f}s; continuing without REPL acceleration"
        )
        report.update({"status": "update-failed", "output": update_output[-2000:]})
        return report
    note(f"[5/6] `lake update repl` completed in {update_elapsed:.1f}s")

    note("[6/6] Building REPL binary with `lake build repl` (this can take several minutes)")
    build_code, build_output, build_elapsed = _run_power_setup_command(
        ["lake", "build", "repl"], cwd=root
    )
    if build_code != 0:
        note(
            f"[6/6] `lake build repl` failed after {build_elapsed:.1f}s; continuing without REPL acceleration"
        )
        report.update({"status": "build-failed", "output": build_output[-2000:]})
        return report
    repl_path = _detect_project_repl_binary(root)
    note(f"[6/6] `lake build repl` completed in {build_elapsed:.1f}s")
    if repl_path:
        note(f"REPL acceleration ready: {repl_path}")
        report.update({"repl_available": True, "repl_path": repl_path, "status": "ready"})
    else:
        note("REPL build completed, but no repl binary was detected")
        report["status"] = "binary-missing"
    return report


def is_lean_project_root(path: Path) -> bool:
    return (path / "lakefile.lean").exists() or (path / "lakefile.toml").exists()


def find_lean_project_root(start: Path) -> Path | None:
    resolved = start.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if is_lean_project_root(candidate):
            return candidate
    return None


def detect_blueprint_markers(root: Path) -> tuple[str, ...]:
    markers = [marker for marker in _BLUEPRINT_MARKERS if (root / marker).exists()]
    return tuple(markers)


def resolve_template_source(
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    env_map = env or {}
    for env_name in (LEANFLOW_PROJECT_TEMPLATE_ENV,):
        configured = str(env_map.get(env_name, "") or "").strip()
        if configured:
            return configured

    if not isinstance(config, Mapping):
        return ""

    leanflow_cfg = config.get("leanflow")
    if not isinstance(leanflow_cfg, Mapping):
        return ""

    project_cfg = leanflow_cfg.get("project")
    if not isinstance(project_cfg, Mapping):
        return ""

    return str(project_cfg.get("template_source", "") or "").strip()


def _resolve_relative_path(root: Path, raw_value: Any, *, field_name: str) -> Path:
    value = str(raw_value or "").strip() or "."
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.expanduser().resolve()


def _project_manifest_path(root: Path) -> Path:
    return root / LEANFLOW_PROJECT_DIRNAME / LEANFLOW_PROJECT_MANIFEST_FILENAME


def load_leanflow_project(target: str | Path) -> LeanFlowProject:
    """Load an existing LeanFlow project from a manifest file (or directory path), validate its schema and structure, auto-migrate legacy manifests if needed, and return an LeanFlowProject with all resolved paths, metadata, and blueprint markers."""
    candidate = Path(target).expanduser().resolve()

    if candidate.is_file():
        manifest_path = candidate
        leanflow_dir = manifest_path.parent
        root = leanflow_dir.parent
    elif candidate.name == LEANFLOW_PROJECT_DIRNAME:
        root = candidate.parent
        leanflow_dir = root / LEANFLOW_PROJECT_DIRNAME
        manifest_path = leanflow_dir / LEANFLOW_PROJECT_MANIFEST_FILENAME
    else:
        root = candidate
        leanflow_dir = root / LEANFLOW_PROJECT_DIRNAME
        manifest_path = leanflow_dir / LEANFLOW_PROJECT_MANIFEST_FILENAME

    if not leanflow_dir.is_dir():
        raise ProjectNotFoundError(f"LeanFlow project directory not found at {leanflow_dir}.")
    if not manifest_path.is_file():
        raise ProjectManifestError(
            f"Found `{leanflow_dir}` but `{LEANFLOW_PROJECT_MANIFEST_FILENAME}` is missing."
        )

    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ProjectManifestError(f"Failed to read {manifest_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ProjectManifestError(f"{manifest_path} must contain a mapping.")

    schema_version = payload.get("schema_version")
    if schema_version != LEANFLOW_PROJECT_SCHEMA_VERSION:
        raise ProjectManifestError(
            f"{manifest_path} has schema_version={schema_version!r}; expected {LEANFLOW_PROJECT_SCHEMA_VERSION}."
        )

    name = str(payload.get("name", "") or "").strip()
    if not name:
        raise ProjectManifestError(f"{manifest_path} is missing a non-empty `name`.")

    kind = str(payload.get("kind", "") or "").strip() or "lean4"
    if kind != "lean4":
        raise ProjectManifestError(f"{manifest_path} has unsupported `kind`: {kind!r}.")

    lean_root = _resolve_relative_path(root, payload.get("lean_root", "."), field_name="lean_root")
    if not is_lean_project_root(lean_root):
        raise ProjectManifestError(
            f"{manifest_path} declares lean_root={payload.get('lean_root')!r}, but that directory is not a Lean 4 project root."
        )

    paths_payload = payload.get("paths")
    if not isinstance(paths_payload, Mapping):
        raise ProjectManifestError(f"{manifest_path} has invalid `paths` metadata.")

    runtime_dir = _resolve_relative_path(
        root, paths_payload.get("runtime", ".leanflow/runtime"), field_name="paths.runtime"
    )
    cache_dir = _resolve_relative_path(
        root, paths_payload.get("cache", ".leanflow/cache"), field_name="paths.cache"
    )
    workflows_dir = _resolve_relative_path(
        root,
        paths_payload.get("workflows", ".leanflow/workflows"),
        field_name="paths.workflows",
    )

    source_payload = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    blueprint_payload = (
        payload.get("blueprint") if isinstance(payload.get("blueprint"), Mapping) else {}
    )
    markers = tuple(
        str(marker).strip()
        for marker in (blueprint_payload.get("markers") or [])
        if str(marker).strip()
    )

    for path in (leanflow_dir, runtime_dir, cache_dir, workflows_dir):
        path.mkdir(parents=True, exist_ok=True)

    return LeanFlowProject(
        root=root,
        leanflow_dir=leanflow_dir,
        manifest_path=manifest_path,
        name=name,
        kind=kind,
        schema_version=LEANFLOW_PROJECT_SCHEMA_VERSION,
        lean_root=lean_root,
        runtime_dir=runtime_dir,
        cache_dir=cache_dir,
        workflows_dir=workflows_dir,
        created_at=str(payload.get("created_at", "") or "").strip(),
        source_mode=str(source_payload.get("mode", "") or "").strip() or "init",
        template_source=str(source_payload.get("template_source", "") or "").strip(),
        blueprint_markers=markers,
        manifest=dict(payload),
    )


def discover_leanflow_project(start: str | Path) -> LeanFlowProject:
    """Return the nearest ancestor carrying a complete LeanFlow manifest."""
    resolved = Path(start).expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if _project_manifest_path(candidate).is_file():
            return load_leanflow_project(candidate)
    raise ProjectNotFoundError(
        "No active LeanFlow project found. Use `leanflow project init` first."
    )


def initialize_leanflow_project(root: str | Path, *, name: str | None = None) -> LeanFlowProject:
    """Initialize a new LeanFlow project by creating the .leanflow directory structure and manifest within a Lean 4 project root, or return the loaded project if one already exists. Detects blueprint markers and records creation timestamp."""
    project_root = Path(root).expanduser().resolve()
    if not project_root.exists():
        raise ProjectCommandError(f"Project root does not exist: {project_root}")

    manifest_path = project_root / LEANFLOW_PROJECT_DIRNAME / LEANFLOW_PROJECT_MANIFEST_FILENAME
    if manifest_path.is_file():
        return load_leanflow_project(project_root)

    lean_root = find_lean_project_root(project_root)
    if lean_root is None:
        raise ProjectCommandError(
            "Lean project root not found. `leanflow project init` only works inside Lean 4 repositories."
        )

    leanflow_dir = project_root / LEANFLOW_PROJECT_DIRNAME
    leanflow_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = leanflow_dir / "runtime"
    cache_dir = leanflow_dir / "cache"
    workflows_dir = leanflow_dir / "workflows"
    for path in (runtime_dir, cache_dir, workflows_dir):
        path.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": LEANFLOW_PROJECT_SCHEMA_VERSION,
        "name": (name or project_root.name or "leanflow-project").strip(),
        "kind": "lean4",
        "lean_root": ".",
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "paths": {
            "runtime": ".leanflow/runtime",
            "cache": ".leanflow/cache",
            "workflows": ".leanflow/workflows",
        },
        "source": {
            "mode": "init",
            "template_source": "",
        },
        "blueprint": {
            "markers": list(detect_blueprint_markers(project_root)),
        },
    }
    manifest_path = leanflow_dir / LEANFLOW_PROJECT_MANIFEST_FILENAME
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return load_leanflow_project(project_root)


def clone_project_template(
    destination: str | Path, *, template_source: str, name: str | None = None
) -> LeanFlowProject:
    destination_path = Path(destination).expanduser().resolve()
    if destination_path.exists():
        raise ProjectCommandError(f"Destination already exists: {destination_path}")
    if not shutil.which("git"):
        raise ProjectCommandError("Git is required for `leanflow project create`.")
    subprocess.run(["git", "clone", template_source, str(destination_path)], check=True)
    return initialize_leanflow_project(destination_path, name=name)
