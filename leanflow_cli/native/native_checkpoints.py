"""Persist, replay, and roll back native workflow checkpoints.

The helpers maintain the workflow-state journal, checkpoint index, and current
pointer. ``AIAgent`` is imported only while type checking so persistence remains
provider- and MCP-independent.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.utils import atomic_json_write
from leanflow_cli.native.native_config import (
    _managed_home,
    _project_root,
    _read_native_env,
    _workflow_kind,
)
from leanflow_cli.native.native_utils import _message_text
from leanflow_cli.workflows.workflow_json_io import read_json_file

if TYPE_CHECKING:
    from run_agent import AIAgent

logger = logging.getLogger(__name__)

__all__ = [
    "WORKFLOW_CHECKPOINT_PREFIX",
    "_workflow_state_root",
    "_workflow_state_index_path",
    "_workflow_state_current_path",
    "_ensure_workflow_state_root",
    "_read_json_file",
    "_write_json_file",
    "_load_workflow_index",
    "_save_workflow_index",
    "_write_current_checkpoint",
    "_load_checkpoint_snapshot",
    "_checkpoint_matches_current_workflow",
    "_load_current_checkpoint",
    "_workflow_replay_message",
    "_checkpoint_replay_history",
    "_latest_filesystem_checkpoint_hash",
]


WORKFLOW_CHECKPOINT_PREFIX = (
    "[LEANFLOW-NATIVE WORKFLOW CHECKPOINT] This persisted workflow handoff captures a "
    "previous autonomous milestone. Use it as the source of truth for resuming this "
    "managed session, and reconcile it with the current filesystem before redoing work."
)


def _workflow_state_root() -> Path:
    project_root = Path(_project_root()).expanduser().resolve()
    if project_root.exists():
        return project_root / ".leanflow" / "workflow-state"
    return _managed_home() / "workflow-state"


def _workflow_state_index_path() -> Path:
    return _workflow_state_root() / "index.json"


def _workflow_state_current_path() -> Path:
    return _workflow_state_root() / "current.json"


def _ensure_workflow_state_root() -> Path:
    root = _workflow_state_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_json_file(path: Path) -> dict[str, Any]:
    # Shared loud-on-corruption reader: missing/empty files are tolerated ({}),
    # but a corrupt non-empty checkpoint file raises WorkflowStateCorruptionError
    # instead of silently dropping resume state.
    return read_json_file(path)


def _write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    # Crash-atomic: checkpoint/journal state must never be truncated mid-write.
    atomic_json_write(path, payload, sort_keys=True)


def _load_workflow_index() -> list[dict[str, Any]]:
    payload = _read_json_file(_workflow_state_index_path())
    checkpoints = payload.get("checkpoints")
    if isinstance(checkpoints, list):
        return [dict(entry) for entry in checkpoints if isinstance(entry, Mapping)]
    return []


def _save_workflow_index(entries: list[dict[str, Any]]) -> None:
    _write_json_file(
        _workflow_state_index_path(),
        {"version": 1, "checkpoints": entries},
    )


def _write_current_checkpoint(entry: Mapping[str, Any]) -> None:
    payload = {
        "version": 1,
        "checkpoint_id": entry.get("checkpoint_id", ""),
        "label": entry.get("label", ""),
        "created_at": entry.get("created_at", ""),
        "snapshot_path": entry.get("snapshot_path", ""),
        "linked_filesystem_checkpoint": entry.get("linked_filesystem_checkpoint", ""),
    }
    _write_json_file(_workflow_state_current_path(), payload)


def _load_checkpoint_snapshot(snapshot_path: str) -> dict[str, Any] | None:
    if not snapshot_path:
        return None
    path = Path(snapshot_path)
    payload = _read_json_file(path)
    return payload or None


def _checkpoint_matches_current_workflow(snapshot: Mapping[str, Any]) -> bool:
    """Return whether a persisted checkpoint belongs to this workflow launch."""
    current_kind = _workflow_kind()
    checkpoint_kind = str(snapshot.get("workflow_kind", "") or "").strip().lower()
    if current_kind and checkpoint_kind and checkpoint_kind != current_kind:
        return False

    current_command = " ".join(_read_native_env("WORKFLOW_COMMAND").split())
    checkpoint_command = " ".join(str(snapshot.get("workflow_command", "") or "").split())
    if current_command and checkpoint_command and checkpoint_command != current_command:
        return False

    current_root = str(Path(_project_root()).expanduser().resolve())
    checkpoint_root_raw = str(snapshot.get("project_root", "") or "").strip()
    if checkpoint_root_raw:
        try:
            checkpoint_root = str(Path(checkpoint_root_raw).expanduser().resolve())
        except Exception:
            checkpoint_root = checkpoint_root_raw
        if checkpoint_root != current_root:
            return False

    return True


def _load_current_checkpoint() -> dict[str, Any] | None:
    payload = _read_json_file(_workflow_state_current_path())
    checkpoint_id = str(payload.get("checkpoint_id", "") or "").strip()
    snapshot_path = str(payload.get("snapshot_path", "") or "").strip()
    if not checkpoint_id or not snapshot_path:
        return None
    snapshot = _load_checkpoint_snapshot(snapshot_path)
    if snapshot is None:
        return None
    if not _checkpoint_matches_current_workflow(snapshot):
        return None
    return snapshot


def _workflow_replay_message(summary_text: str) -> dict[str, Any]:
    body = summary_text.strip()
    if not body.startswith(WORKFLOW_CHECKPOINT_PREFIX):
        body = f"{WORKFLOW_CHECKPOINT_PREFIX}\n\n{body}" if body else WORKFLOW_CHECKPOINT_PREFIX
    return {"role": "assistant", "content": body}


def _checkpoint_replay_history(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    summary_text = _message_text(entry.get("summary_text")).strip()
    if not summary_text:
        return []
    return [_workflow_replay_message(summary_text)]


def _latest_filesystem_checkpoint_hash(
    agent: AIAgent, *, reason: str = "", force: bool = False
) -> str:
    """Return the latest source snapshot hash, forcing current state when requested."""
    checkpoint_mgr = getattr(agent, "_checkpoint_mgr", None)
    if checkpoint_mgr is None or not getattr(checkpoint_mgr, "enabled", False):
        return ""
    working_dir = _project_root()
    if force:
        try:
            checkpoint_mgr.ensure_checkpoint(
                working_dir,
                reason or "workflow checkpoint",
                force=True,
            )
        except Exception:
            return ""
    try:
        checkpoints = checkpoint_mgr.list_checkpoints(working_dir)
    except Exception:
        return ""
    if not checkpoints:
        return ""
    return str(checkpoints[0].get("hash", "") or "")
