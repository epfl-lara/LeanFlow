"""Persistent managed-workflow state for the OpenGauss shell."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _opengauss_home() -> Path:
    explicit = str(os.getenv("OPENGAUSS_HOME", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    legacy = str(os.getenv("GAUSS_HOME", "") or "").strip()
    if legacy and Path(legacy).expanduser().name == ".opengauss":
        return Path(legacy).expanduser()
    return Path.home() / ".opengauss"


def workflow_state_root() -> Path:
    return _opengauss_home() / "workflow-state"


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
