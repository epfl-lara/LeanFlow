"""Path-root discovery helpers for EPFLemma managed-workflow state.

These foundational helpers resolve where workflow-state lives on disk: the
per-user home directory and the per-project state root. They are imported by
``epflemma_cli.workflow_state`` (which re-exports them) and called by many of
its functions.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_STATE_DIRNAME = ".epflemma"
LEGACY_PROJECT_DIRNAMES = (".opengauss", ".gauss")


def _epflemma_home() -> Path:
    explicit = str(os.getenv("EPFLEMMA_HOME", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    branded_legacy = str(os.getenv("OPENGAUSS_HOME", "") or "").strip()
    if branded_legacy:
        return Path(branded_legacy).expanduser()
    legacy = str(os.getenv("GAUSS_HOME", "") or "").strip()
    if legacy and Path(legacy).expanduser().name in {".epflemma", ".opengauss"}:
        return Path(legacy).expanduser()
    return Path.home() / ".epflemma"


def _project_root_from_env() -> Path | None:
    explicit = str(os.getenv("EPFLEMMA_PROJECT_ROOT", "") or "").strip()
    if not explicit:
        explicit = str(os.getenv("OPENGAUSS_PROJECT_ROOT", "") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.exists():
            return candidate.resolve()
    return None


def _discover_project_root(start: Path | None = None) -> Path | None:
    base = (start or Path.cwd()).expanduser().resolve()
    for candidate in (base, *base.parents):
        epflemma_manifest = candidate / PROJECT_STATE_DIRNAME / "project.yaml"
        if epflemma_manifest.is_file():
            return candidate
        for dirname in LEGACY_PROJECT_DIRNAMES:
            if (candidate / dirname / "project.yaml").is_file():
                return candidate
    return None


def _project_state_root() -> Path | None:
    project_root = _project_root_from_env() or _discover_project_root()
    if project_root is None:
        return None
    return project_root / PROJECT_STATE_DIRNAME / "workflow-state"


def workflow_state_root() -> Path:
    return _project_state_root() or (_epflemma_home() / "workflow-state")
