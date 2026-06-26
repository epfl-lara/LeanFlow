"""Path-root discovery helpers for LeanFlow managed-workflow state.

These foundational helpers resolve where workflow-state lives on disk: the
per-user home directory and the per-project state root. They are imported by
``leanflow_cli.workflows.workflow_state`` (which re-exports them) and called by many of
its functions.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.home import leanflow_home

PROJECT_STATE_DIRNAME = ".leanflow"


def _leanflow_home() -> Path:
    return leanflow_home()


def _project_root_from_env() -> Path | None:
    explicit = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.exists():
            return candidate.resolve()
    return None


def _discover_project_root(start: Path | None = None) -> Path | None:
    base = (start or Path.cwd()).expanduser().resolve()
    for candidate in (base, *base.parents):
        leanflow_manifest = candidate / PROJECT_STATE_DIRNAME / "project.yaml"
        if leanflow_manifest.is_file():
            return candidate
    return None


def _project_state_root() -> Path | None:
    project_root = _project_root_from_env() or _discover_project_root()
    if project_root is None:
        return None
    return project_root / PROJECT_STATE_DIRNAME / "workflow-state"


def workflow_state_root() -> Path:
    return _project_state_root() or (_leanflow_home() / "workflow-state")
