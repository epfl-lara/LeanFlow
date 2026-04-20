"""Persistent managed-workflow state for the EPFLemma shell."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

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


def ensure_workflow_state_root() -> Path:
    root = workflow_state_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def workflow_index_path() -> Path:
    return workflow_state_root() / "index.json"


def workflow_current_path() -> Path:
    return workflow_state_root() / "current.json"


def workflow_live_status_path() -> Path:
    return workflow_state_root() / "live_status.json"


def workflow_activity_path() -> Path:
    return workflow_state_root() / "activity.jsonl"


def workflow_run_log_path() -> Path:
    return workflow_state_root() / "latest-run.log"


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {}


def write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def load_workflow_live_status() -> dict[str, Any]:
    return read_json_file(workflow_live_status_path())


def save_workflow_live_status(payload: Mapping[str, Any]) -> None:
    write_json_file(workflow_live_status_path(), payload)


def append_workflow_activity(event_type: str, message: str, **details: Any) -> None:
    ensure_workflow_state_root()
    event = {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "type": event_type,
        "message": message,
        "details": details,
    }
    with workflow_activity_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True))
        handle.write("\n")


def read_workflow_activity(limit: int = 20) -> list[dict[str, Any]]:
    path = workflow_activity_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for line in lines[-max(1, limit):]:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def reset_workflow_run_log() -> Path:
    path = workflow_run_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def append_workflow_run_log(text: str) -> None:
    if not text:
        return
    path = workflow_run_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def read_workflow_run_log(tail_lines: int = 120) -> str:
    path = workflow_run_log_path()
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    tail = lines[-max(1, tail_lines):]
    return "\n".join(tail)


def load_workflow_checkpoints() -> list[dict[str, Any]]:
    payload = read_json_file(workflow_index_path())
    checkpoints = payload.get("checkpoints")
    if isinstance(checkpoints, list):
        return [dict(entry) for entry in checkpoints if isinstance(entry, Mapping)]
    return []


def load_current_workflow_checkpoint() -> dict[str, Any]:
    payload = read_json_file(workflow_current_path())
    checkpoint_id = str(payload.get("checkpoint_id", "") or "").strip()
    snapshot_path = str(payload.get("snapshot_path", "") or "").strip()
    if not checkpoint_id or not snapshot_path:
        return {}
    snapshot = read_json_file(Path(snapshot_path))
    return snapshot if snapshot else {}
