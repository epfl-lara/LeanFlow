"""OpenGauss project discovery and local manifest management."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml


OPENGAUSS_PROJECT_DIRNAME = ".opengauss"
LEGACY_PROJECT_DIRNAME = ".gauss"
OPENGAUSS_PROJECT_MANIFEST_FILENAME = "project.yaml"
OPENGAUSS_PROJECT_SCHEMA_VERSION = 1
OPENGAUSS_PROJECT_TEMPLATE_ENV = "OPENGAUSS_BLUEPRINT_TEMPLATE_SOURCE"
OPENGAUSS_PROJECT_TEMPLATE_CONFIG_KEY = "opengauss.project.template_source"

_BLUEPRINT_MARKERS = (
    "lean-toolchain",
    "lakefile.lean",
    "lakefile.toml",
    "templates/blueprint.yml",
)


class OpenGaussProjectError(RuntimeError):
    """Base class for OpenGauss project failures."""


class ProjectNotFoundError(OpenGaussProjectError):
    """Raised when no active OpenGauss project can be found."""


class ProjectManifestError(OpenGaussProjectError):
    """Raised when a project manifest is missing or malformed."""


class ProjectCommandError(OpenGaussProjectError):
    """Raised when a project-management command cannot be completed."""


class ProjectTemplateUnavailableError(ProjectCommandError):
    """Raised when `/project create` has no configured template source."""


@dataclass(frozen=True)
class OpenGaussProject:
    root: Path
    opengauss_dir: Path
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


def format_project_summary(project: OpenGaussProject) -> str:
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
    configured = str(env_map.get(OPENGAUSS_PROJECT_TEMPLATE_ENV, "") or "").strip()
    if configured:
        return configured

    if not isinstance(config, Mapping):
        return ""

    opengauss_cfg = config.get("opengauss")
    if not isinstance(opengauss_cfg, Mapping):
        return ""

    project_cfg = opengauss_cfg.get("project")
    if not isinstance(project_cfg, Mapping):
        return ""

    return str(project_cfg.get("template_source", "") or "").strip()


def _resolve_relative_path(root: Path, raw_value: Any, *, field_name: str) -> Path:
    value = str(raw_value or "").strip() or "."
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.expanduser().resolve()


def _legacy_manifest_path(root: Path) -> Path:
    return root / LEGACY_PROJECT_DIRNAME / OPENGAUSS_PROJECT_MANIFEST_FILENAME


def _project_manifest_path(root: Path) -> Path:
    return root / OPENGAUSS_PROJECT_DIRNAME / OPENGAUSS_PROJECT_MANIFEST_FILENAME


def _transform_legacy_manifest(root: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    paths_payload = payload.get("paths") if isinstance(payload.get("paths"), Mapping) else {}
    blueprint_payload = payload.get("blueprint") if isinstance(payload.get("blueprint"), Mapping) else {}
    source_payload = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}

    return {
        "schema_version": OPENGAUSS_PROJECT_SCHEMA_VERSION,
        "name": str(payload.get("name", "") or root.name).strip() or root.name,
        "kind": str(payload.get("kind", "lean4") or "lean4").strip() or "lean4",
        "lean_root": str(payload.get("lean_root", ".")),
        "created_at": str(payload.get("created_at", "") or ""),
        "paths": {
            "runtime": str(paths_payload.get("runtime", ".opengauss/runtime")).replace(".gauss/", ".opengauss/"),
            "cache": str(paths_payload.get("cache", ".opengauss/cache")).replace(".gauss/", ".opengauss/"),
            "workflows": str(paths_payload.get("workflows", ".opengauss/workflows")).replace(".gauss/", ".opengauss/"),
        },
        "source": dict(source_payload),
        "blueprint": {
            "markers": list(blueprint_payload.get("markers") or []),
        },
    }


def _maybe_import_legacy_project(root: Path) -> None:
    new_manifest = _project_manifest_path(root)
    if new_manifest.exists():
        return

    legacy_manifest = _legacy_manifest_path(root)
    if not legacy_manifest.exists():
        return

    try:
        payload = yaml.safe_load(legacy_manifest.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(payload, Mapping):
        return

    opengauss_dir = root / OPENGAUSS_PROJECT_DIRNAME
    opengauss_dir.mkdir(parents=True, exist_ok=True)
    transformed = _transform_legacy_manifest(root, payload)
    new_manifest.write_text(yaml.safe_dump(transformed, sort_keys=False), encoding="utf-8")


def load_opengauss_project(target: str | Path) -> OpenGaussProject:
    candidate = Path(target).expanduser().resolve()

    if candidate.is_file():
        manifest_path = candidate
        opengauss_dir = manifest_path.parent
        root = opengauss_dir.parent
    elif candidate.name in {OPENGAUSS_PROJECT_DIRNAME, LEGACY_PROJECT_DIRNAME}:
        root = candidate.parent
        _maybe_import_legacy_project(root)
        opengauss_dir = root / OPENGAUSS_PROJECT_DIRNAME
        manifest_path = opengauss_dir / OPENGAUSS_PROJECT_MANIFEST_FILENAME
    else:
        root = candidate
        _maybe_import_legacy_project(root)
        opengauss_dir = root / OPENGAUSS_PROJECT_DIRNAME
        manifest_path = opengauss_dir / OPENGAUSS_PROJECT_MANIFEST_FILENAME

    if not opengauss_dir.is_dir():
        raise ProjectNotFoundError(f"OpenGauss project directory not found at {opengauss_dir}.")
    if not manifest_path.is_file():
        raise ProjectManifestError(
            f"Found `{opengauss_dir}` but `{OPENGAUSS_PROJECT_MANIFEST_FILENAME}` is missing."
        )

    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ProjectManifestError(f"Failed to read {manifest_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ProjectManifestError(f"{manifest_path} must contain a mapping.")

    schema_version = payload.get("schema_version")
    if schema_version != OPENGAUSS_PROJECT_SCHEMA_VERSION:
        raise ProjectManifestError(
            f"{manifest_path} has schema_version={schema_version!r}; expected {OPENGAUSS_PROJECT_SCHEMA_VERSION}."
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

    runtime_dir = _resolve_relative_path(root, paths_payload.get("runtime", ".opengauss/runtime"), field_name="paths.runtime")
    cache_dir = _resolve_relative_path(root, paths_payload.get("cache", ".opengauss/cache"), field_name="paths.cache")
    workflows_dir = _resolve_relative_path(
        root,
        paths_payload.get("workflows", ".opengauss/workflows"),
        field_name="paths.workflows",
    )

    source_payload = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    blueprint_payload = payload.get("blueprint") if isinstance(payload.get("blueprint"), Mapping) else {}
    markers = tuple(str(marker).strip() for marker in (blueprint_payload.get("markers") or []) if str(marker).strip())

    for path in (opengauss_dir, runtime_dir, cache_dir, workflows_dir):
        path.mkdir(parents=True, exist_ok=True)

    return OpenGaussProject(
        root=root,
        opengauss_dir=opengauss_dir,
        manifest_path=manifest_path,
        name=name,
        kind=kind,
        schema_version=OPENGAUSS_PROJECT_SCHEMA_VERSION,
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


def discover_opengauss_project(start: str | Path) -> OpenGaussProject:
    resolved = Path(start).expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / OPENGAUSS_PROJECT_DIRNAME).exists() or (candidate / LEGACY_PROJECT_DIRNAME).exists():
            return load_opengauss_project(candidate)
    raise ProjectNotFoundError(
        "No active OpenGauss project found. Use `opengauss project init` first."
    )


def initialize_opengauss_project(root: str | Path, *, name: str | None = None) -> OpenGaussProject:
    project_root = Path(root).expanduser().resolve()
    if not project_root.exists():
        raise ProjectCommandError(f"Project root does not exist: {project_root}")

    manifest_path = project_root / OPENGAUSS_PROJECT_DIRNAME / OPENGAUSS_PROJECT_MANIFEST_FILENAME
    legacy_manifest_path = project_root / LEGACY_PROJECT_DIRNAME / OPENGAUSS_PROJECT_MANIFEST_FILENAME
    if manifest_path.is_file() or legacy_manifest_path.is_file():
        return load_opengauss_project(project_root)

    lean_root = find_lean_project_root(project_root)
    if lean_root is None:
        raise ProjectCommandError(
            "Lean project root not found. `opengauss project init` only works inside Lean 4 repositories."
        )

    opengauss_dir = project_root / OPENGAUSS_PROJECT_DIRNAME
    opengauss_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = opengauss_dir / "runtime"
    cache_dir = opengauss_dir / "cache"
    workflows_dir = opengauss_dir / "workflows"
    for path in (runtime_dir, cache_dir, workflows_dir):
        path.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": OPENGAUSS_PROJECT_SCHEMA_VERSION,
        "name": (name or project_root.name or "opengauss-project").strip(),
        "kind": "lean4",
        "lean_root": ".",
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "paths": {
            "runtime": ".opengauss/runtime",
            "cache": ".opengauss/cache",
            "workflows": ".opengauss/workflows",
        },
        "source": {
            "mode": "init",
            "template_source": "",
        },
        "blueprint": {
            "markers": list(detect_blueprint_markers(project_root)),
        },
    }
    manifest_path = opengauss_dir / OPENGAUSS_PROJECT_MANIFEST_FILENAME
    manifest_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return load_opengauss_project(project_root)


def clone_project_template(destination: str | Path, *, template_source: str, name: str | None = None) -> OpenGaussProject:
    destination_path = Path(destination).expanduser().resolve()
    if destination_path.exists():
        raise ProjectCommandError(f"Destination already exists: {destination_path}")
    if not shutil.which("git"):
        raise ProjectCommandError("Git is required for `opengauss project create`.")
    subprocess.run(["git", "clone", template_source, str(destination_path)], check=True)
    return initialize_opengauss_project(destination_path, name=name)
