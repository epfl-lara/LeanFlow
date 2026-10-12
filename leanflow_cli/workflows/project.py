"""LeanFlow project discovery and local manifest management."""

from __future__ import annotations

from collections.abc import Mapping
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
    """Validate and resolve a project manifest without creating or changing files."""
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

    source_value = payload.get("source")
    source_payload = source_value if isinstance(source_value, Mapping) else {}
    blueprint_value = payload.get("blueprint")
    blueprint_payload = blueprint_value if isinstance(blueprint_value, Mapping) else {}
    markers = tuple(
        str(marker).strip()
        for marker in (blueprint_payload.get("markers") or [])
        if str(marker).strip()
    )

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
        "No active LeanFlow project found. Use `leanflow project setup` first."
    )


def initialize_leanflow_project(root: str | Path, *, name: str | None = None) -> LeanFlowProject:
    """Initialize a new LeanFlow project by creating the .leanflow directory structure and manifest within a Lean 4 project root, or return the loaded project if one already exists. Detects blueprint markers and records creation timestamp."""
    project_root = Path(root).expanduser().resolve()
    if not project_root.exists():
        raise ProjectCommandError(f"Project root does not exist: {project_root}")

    lean_root = find_lean_project_root(project_root)
    if lean_root is None:
        raise ProjectCommandError("Lean project root not found. Set up a Lean project first.")
    # Registration belongs to the Lake root even when requested from a subfolder.
    project_root = lean_root
    manifest_path = project_root / LEANFLOW_PROJECT_DIRNAME / LEANFLOW_PROJECT_MANIFEST_FILENAME
    if manifest_path.is_file():
        return load_leanflow_project(project_root)

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
