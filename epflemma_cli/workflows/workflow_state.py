"""Persistent managed-workflow state for the EPFLemma shell."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from epflemma_cli.config import load_config
from epflemma_cli.workflows.activity_preview import (  # noqa: F401
    _activity_preview_limit,
    _agent_event_preview,
    _agent_status_from_live_phase,
    _coerce_tool_arguments,
    _shorten_text,
    _summarize_requested_tools,
    _tool_call_preview,
    _tool_result_preview,
)
from epflemma_cli.workflows.workflow_json_io import (  # noqa: F401
    read_json_file,
    write_json_file,
)
from epflemma_cli.workflows.workflow_state_paths import (  # noqa: F401
    LEGACY_PROJECT_DIRNAMES,
    PROJECT_STATE_DIRNAME,
    _discover_project_root,
    _epflemma_home,
    _project_root_from_env,
    _project_state_root,
    workflow_state_root,
)

logger = logging.getLogger(__name__)

try:
    import fcntl  # POSIX advisory file locking
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

# Serializes activity/outcome/log appends so concurrent /swarm agents cannot interleave or
# lose JSON-lines: _APPEND_LOCK guards threads within this process; fcntl.flock guards across
# subprocesses. Best-effort — degrades to in-process-only if flock is unavailable.
_APPEND_LOCK = threading.Lock()


def _locked_append(path: Path, text: str) -> None:
    """Append ``text`` to ``path`` under an exclusive in-process + cross-process lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except OSError:
                    logger.debug(
                        "flock unavailable for %s; append not cross-process locked", path, exc_info=True
                    )
            handle.write(text)
            handle.flush()

WORKFLOW_TASK_LABELS = {
    "autoprove": "prove",
    "autoformalize": "formalize",
    "prove": "prove",
    "formalize": "formalize",
    "draft": "draft",
    "review": "review",
    "checkpoint": "checkpoint",
    "refactor": "refactor",
    "golf": "golf",
}
WORKFLOW_RUN_SCOPE_TOP_LEVEL = "top-level"
WORKFLOW_RUN_SCOPE_BACKGROUND = "background-session"


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


def workflow_activity_root() -> Path:
    return workflow_state_root() / "activity"


def workflow_run_activity_root() -> Path:
    return workflow_activity_root() / "runs"


def workflow_run_metadata_root() -> Path:
    return workflow_activity_root() / "run-metadata"


def workflow_agent_activity_root() -> Path:
    return workflow_activity_root() / "agents"


def workflow_run_log_path() -> Path:
    return workflow_state_root() / "latest-run.log"


def workflow_runs_root() -> Path:
    return workflow_state_root() / "runs"


def workflow_agent_inbox_root() -> Path:
    return workflow_state_root() / "agent-inbox"


def workflow_agent_inbox_path(agent_id: str) -> Path:
    safe_agent_id = "".join(ch for ch in str(agent_id or "").strip() if ch.isalnum() or ch in {"-", "_"})
    return workflow_agent_inbox_root() / f"{safe_agent_id or 'unknown'}.jsonl"


def workflow_outcomes_path() -> Path:
    return workflow_state_root() / "outcomes.jsonl"


def workflow_verified_patch_status_path() -> Path:
    return workflow_state_root() / "verified_patch_status.json"


def workflow_verified_patch_checkpoint_root() -> Path:
    return workflow_state_root() / "verified-patch-checkpoints"


def workflow_run_activity_path(run_id: str) -> Path:
    safe_run_id = "".join(ch for ch in str(run_id or "").strip() if ch.isalnum() or ch in {"-", "_"})
    return workflow_run_activity_root() / f"{safe_run_id or 'unknown'}.jsonl"


def workflow_run_metadata_path(run_id: str) -> Path:
    safe_run_id = "".join(ch for ch in str(run_id or "").strip() if ch.isalnum() or ch in {"-", "_"})
    return workflow_run_metadata_root() / f"{safe_run_id or 'unknown'}.json"


def workflow_agent_activity_path(agent_id: str, task_label: str = "") -> Path:
    safe_agent_id = "".join(ch for ch in str(agent_id or "").strip() if ch.isalnum() or ch in {"-", "_"})
    safe_task = "".join(ch for ch in str(task_label or "").strip() if ch.isalnum() or ch in {"-", "_"}).strip() or "agent"
    return workflow_agent_activity_root() / f"{safe_task}-{safe_agent_id or 'unknown'}.jsonl"


def _read_workflow_run_metadata(run_id: str) -> dict[str, Any]:
    return read_json_file(workflow_run_metadata_path(run_id))


def _workflow_run_scope_from_event(event_type: str, details: Mapping[str, Any] | None = None) -> str:
    if event_type == "runner-start":
        return WORKFLOW_RUN_SCOPE_TOP_LEVEL
    normalized_details = details if isinstance(details, Mapping) else {}
    explicit = str(normalized_details.get("run_scope", "") or os.getenv("EPFLEMMA_WORKFLOW_RUN_SCOPE", "") or "").strip()
    if explicit:
        return explicit
    return WORKFLOW_RUN_SCOPE_BACKGROUND


def _persist_workflow_run_metadata(
    run_id: str,
    *,
    run_scope: str,
    parent_run_id: str = "",
    task_label: str = "",
    workflow_kind: str = "",
    workflow_command: str = "",
    effective_prompt: str = "",
    active_skill: str = "",
    project_root: str = "",
    process_id: int = 0,
) -> None:
    if not run_id:
        return
    path = workflow_run_metadata_path(run_id)
    existing = read_json_file(path)
    payload = dict(existing) if isinstance(existing, dict) else {}
    payload.setdefault("run_id", run_id)
    payload["run_scope"] = str(run_scope or payload.get("run_scope", "") or WORKFLOW_RUN_SCOPE_BACKGROUND)
    if parent_run_id:
        payload["parent_run_id"] = parent_run_id
    elif "parent_run_id" not in payload:
        payload["parent_run_id"] = ""
    if task_label:
        payload["task_label"] = task_label
    if workflow_kind:
        payload["workflow_kind"] = workflow_kind
    if workflow_command:
        payload["workflow_command"] = workflow_command
    if effective_prompt:
        payload["effective_prompt"] = effective_prompt
    if active_skill:
        payload["active_skill"] = active_skill
    if project_root:
        payload["project_root"] = project_root
    if process_id > 0:
        payload["process_id"] = process_id
    payload["updated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    if "created_at" not in payload:
        payload["created_at"] = payload["updated_at"]
    write_json_file(path, payload)


def workflow_latest_run_activity_path(*, prefer_top_level: bool = True) -> Path | None:
    current_run_id = str(os.getenv("EPFLEMMA_WORKFLOW_RUN_ID", "") or "").strip()
    if current_run_id:
        path = workflow_run_activity_path(current_run_id)
        if path.is_file():
            return path
    root = workflow_run_activity_root()
    if not root.is_dir():
        return None
    candidates = sorted(root.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not prefer_top_level:
        return candidates[0] if candidates else None
    top_level_candidates: list[Path] = []
    for path in candidates:
        metadata = _read_workflow_run_metadata(path.stem)
        if str(metadata.get("run_scope", "") or "") == WORKFLOW_RUN_SCOPE_TOP_LEVEL:
            top_level_candidates.append(path)
    if top_level_candidates:
        return top_level_candidates[0]
    return candidates[0] if candidates else None


def _workflow_task_label(kind: str, active_skill: str = "", delegate_depth: int = 0) -> str:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind in WORKFLOW_TASK_LABELS:
        return WORKFLOW_TASK_LABELS[normalized_kind]
    if delegate_depth > 0:
        return "swarm"
    skill = str(active_skill or "").strip()
    if skill:
        return skill
    return "agent"


def _workflow_run_id() -> str:
    run_id = str(os.getenv("EPFLEMMA_WORKFLOW_RUN_ID", "") or "").strip()
    if run_id:
        return run_id
    started = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    workflow_kind = str(os.getenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", ""))
    task = _workflow_task_label(
        workflow_kind,
        str(os.getenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "")) if workflow_kind else "",
        0,
    )
    safe_task = "".join(ch for ch in task if ch.isalnum() or ch in {"-", "_"}).strip() or "agent"
    run_id = f"{safe_task}-{started}-pid{os.getpid()}"
    os.environ["EPFLEMMA_WORKFLOW_RUN_ID"] = run_id
    return run_id


def workflow_timestamped_run_log_path() -> Path:
    return workflow_runs_root() / f"{_workflow_run_id()}.log"


def load_workflow_live_status() -> dict[str, Any]:
    payload = read_json_file(workflow_live_status_path())
    normalized, changed = _normalize_workflow_live_status_payload(payload)
    if changed:
        write_json_file(workflow_live_status_path(), normalized)
    return normalized


def save_workflow_live_status(payload: Mapping[str, Any]) -> None:
    write_json_file(workflow_live_status_path(), payload)


def append_workflow_activity(event_type: str, message: str, **details: Any) -> None:
    ensure_workflow_state_root()
    normalized_details = dict(details)
    env_workflow_kind = str(os.getenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", ""))
    normalized_details.setdefault("workflow_kind", env_workflow_kind)
    normalized_details.setdefault("workflow_command", str(os.getenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "")))
    normalized_details.setdefault(
        "effective_prompt",
        str(
            os.getenv("EPFLEMMA_NATIVE_EFFECTIVE_PROMPT", "")
            or os.getenv("EPFLEMMA_NATIVE_USER_PROMPT", "")
            or os.getenv("EPFLEMMA_NATIVE_EXPLICIT_GOAL", "")
        ),
    )
    normalized_details.setdefault(
        "active_skill",
        str(os.getenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "")) if env_workflow_kind else "",
    )
    project_root = _project_root_from_env()
    normalized_details.setdefault("project_root", str(project_root) if project_root else "")
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    run_id = _workflow_run_id()
    agent_id = str(normalized_details.get("agent_session_id", "") or "")
    try:
        delegate_depth = int(normalized_details.get("delegate_depth", 0) or 0)
    except Exception:
        delegate_depth = 0
    task_label = _workflow_task_label(
        str(normalized_details.get("workflow_kind", "") or ""),
        str(normalized_details.get("active_skill", "") or ""),
        delegate_depth,
    )
    run_scope = _workflow_run_scope_from_event(event_type, normalized_details)
    normalized_details.setdefault("run_scope", run_scope)
    parent_run_id = str(
        normalized_details.get("parent_run_id", "")
        or os.getenv("EPFLEMMA_WORKFLOW_PARENT_RUN_ID", "")
        or ""
    ).strip()
    normalized_details.setdefault("parent_run_id", parent_run_id)
    try:
        process_id = int(normalized_details.get("process_id", 0) or 0)
    except Exception:
        process_id = 0
    _persist_workflow_run_metadata(
        run_id,
        run_scope=run_scope,
        parent_run_id=parent_run_id,
        task_label=task_label,
        workflow_kind=str(normalized_details.get("workflow_kind", "") or ""),
        workflow_command=str(normalized_details.get("workflow_command", "") or ""),
        effective_prompt=str(normalized_details.get("effective_prompt", "") or ""),
        active_skill=str(normalized_details.get("active_skill", "") or ""),
        project_root=str(normalized_details.get("project_root", "") or ""),
        process_id=process_id,
    )
    event = {
        "event_id": uuid.uuid4().hex[:12],
        "timestamp": timestamp,
        "type": event_type,
        "run_id": run_id,
        "agent_id": agent_id,
        "task_label": task_label,
        "run_scope": run_scope,
        "message": message,
        "details": normalized_details,
    }
    serialized = json.dumps(event, sort_keys=True)
    paths = [workflow_run_activity_path(run_id)]
    if agent_id:
        paths.append(workflow_agent_activity_path(agent_id, task_label))
    for path in paths:
        _locked_append(path, serialized + "\n")


def append_workflow_outcome(kind: str, payload: Mapping[str, Any]) -> None:
    ensure_workflow_state_root()
    entry = {
        "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "kind": str(kind or "").strip() or "outcome",
        "workflow_kind": str(os.getenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "")),
        "workflow_command": str(os.getenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "")),
        "payload": dict(payload or {}),
    }
    path = workflow_outcomes_path()
    _locked_append(path, json.dumps(entry, sort_keys=True) + "\n")


def write_verified_patch_checkpoint(
    *,
    file_path: str,
    cwd: str = "",
    before_content: str = "",
    patch: str = "",
    check_mode: str = "",
    theorem_id: str = "",
) -> dict[str, Any]:
    """Persist a pre-edit snapshot for apply_verified_patch."""
    ensure_workflow_state_root()
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    checkpoint_id = f"vpatch-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    before_bytes = before_content.encode("utf-8", errors="replace")
    payload = {
        "version": 1,
        "checkpoint_id": checkpoint_id,
        "created_at": timestamp,
        "file_path": str(file_path or ""),
        "cwd": str(cwd or ""),
        "theorem_id": str(theorem_id or ""),
        "check_mode": str(check_mode or ""),
        "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
        "before_bytes": len(before_bytes),
        "before_content": before_content,
        "patch": patch,
    }
    path = workflow_verified_patch_checkpoint_root() / f"{checkpoint_id}.json"
    write_json_file(path, payload)
    return {
        "checkpoint_id": checkpoint_id,
        "snapshot_path": str(path),
        "before_sha256": payload["before_sha256"],
        "before_bytes": payload["before_bytes"],
    }


def save_verified_patch_status(payload: Mapping[str, Any]) -> None:
    """Persist the latest apply_verified_patch status for resume/queue logic."""
    status = dict(payload or {})
    status.setdefault("timestamp", datetime.now(UTC).replace(microsecond=0).isoformat())
    write_json_file(
        workflow_verified_patch_status_path(),
        {
            "version": 1,
            "latest": status,
        },
    )


def load_verified_patch_status() -> dict[str, Any]:
    payload = read_json_file(workflow_verified_patch_status_path())
    latest = payload.get("latest")
    return dict(latest) if isinstance(latest, Mapping) else {}


def _read_activity_file(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _read_all_workflow_activity() -> list[dict[str, Any]]:
    root = workflow_run_activity_root()
    if not root.is_dir():
        latest = workflow_latest_run_activity_path()
        return _read_activity_file(latest)
    events: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.jsonl")):
        events.extend(_read_activity_file(path))
    return events


def read_workflow_agent_inbox(agent_id: str) -> list[dict[str, Any]]:
    path = workflow_agent_inbox_path(agent_id)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    commands: list[dict[str, Any]] = []
    for idx, line in enumerate(lines, start=1):
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            entry = dict(payload)
            entry.setdefault("seq", idx)
            commands.append(entry)
    return commands


def _process_seems_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


_LIVE_STATUS_TERMINAL_PHASES = {"completed", "dead", "exited", "failed", "interrupted", "stopped", "verified"}


def _normalize_workflow_live_status_payload(payload: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
    if not isinstance(payload, Mapping):
        return {}, False
    normalized = dict(payload)
    changed = False
    if normalized.get("stale_snapshot"):
        try:
            held_locks = int(normalized.get("held_locks", 0) or 0)
        except Exception:
            held_locks = 0
        if held_locks > 0:
            normalized.setdefault("stale_held_locks", held_locks)
            normalized["held_locks"] = 0
            changed = True
    try:
        process_id = int(normalized.get("process_id", 0) or 0)
    except Exception:
        process_id = 0
    if process_id <= 0 or _process_seems_alive(process_id):
        return normalized, changed

    normalized["stale_snapshot"] = True
    normalized["stale_process_id"] = process_id
    normalized["process_id"] = 0
    try:
        stale_held_locks = int(normalized.get("held_locks", 0) or 0)
    except Exception:
        stale_held_locks = 0
    if stale_held_locks > 0:
        normalized["stale_held_locks"] = stale_held_locks
    normalized["held_locks"] = 0
    changed = True

    phase = str(normalized.get("phase", "") or "").strip().lower()
    if phase not in _LIVE_STATUS_TERMINAL_PHASES:
        normalized["phase"] = "dead"
    return normalized, changed


def enqueue_workflow_agent_message(agent_ref: str, text: str, *, kind: str = "message") -> dict[str, Any]:
    agent_id = resolve_workflow_agent_id(agent_ref)
    if not agent_id:
        return {"success": False, "error": "Agent not found or ambiguous."}
    detail = workflow_agent_detail(agent_id, activity_limit=1)
    process_id = int(detail.get("process_id", 0) or 0)
    status = str(detail.get("status", "") or "")
    if process_id <= 0 or not _process_seems_alive(process_id):
        return {"success": False, "error": "Agent process is no longer running.", "agent_id": agent_id}
    if status in {"exited", "stopped", "interrupted"}:
        return {"success": False, "error": f"Agent is no longer accepting input ({status}).", "agent_id": agent_id}
    message = str(text or "").strip()
    if not message:
        return {"success": False, "error": "Message is empty.", "agent_id": agent_id}
    path = workflow_agent_inbox_path(agent_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    seq = len(read_workflow_agent_inbox(agent_id)) + 1
    entry = {
        "seq": seq,
        "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "kind": str(kind or "message"),
        "text": message,
    }
    _locked_append(path, json.dumps(entry, sort_keys=True) + "\n")
    append_workflow_activity(
        "agent-input-queued",
        "Queued user message for workflow agent",
        agent_session_id=agent_id,
        input_kind=entry["kind"],
        text=message,
        seq=seq,
    )
    return {"success": True, "agent_id": agent_id, "seq": seq, "kind": entry["kind"]}


def read_workflow_activity(
    limit: int = 20,
    *,
    agent_id: str | None = None,
    event_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    events = _read_all_workflow_activity()
    if agent_id:
        filtered: list[dict[str, Any]] = []
        for event in events:
            details = event.get("details")
            if not isinstance(details, dict):
                continue
            if str(details.get("agent_session_id", "") or "") != agent_id:
                continue
            filtered.append(event)
        events = filtered
    if event_types:
        events = [event for event in events if str(event.get("type", "") or "") in event_types]
    return events[-max(1, limit):]


_TERMINAL_AGENT_STATUSES = {"completed", "exited", "stopped", "interrupted", "dead", "failed"}


def summarize_workflow_agents(*, activity_limit: int = 5) -> list[dict[str, Any]]:
    events = _read_all_workflow_activity()
    by_agent: dict[str, dict[str, Any]] = {}
    run_metadata_cache: dict[str, dict[str, Any]] = {}
    for event in events:
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        run_id = str(event.get("run_id", "") or "")
        if run_id:
            metadata = run_metadata_cache.setdefault(run_id, _read_workflow_run_metadata(run_id))
        else:
            metadata = {}
        agent_id = str(details.get("agent_session_id", "") or "")
        if not agent_id:
            continue
        summary = by_agent.setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "parent_agent_id": "",
                "_run_scope": "",
                "project_root": "",
                "task_label": "",
                "workflow_kind": "",
                "workflow_command": "",
                "active_skill": "",
                "delegate_depth": 0,
                "model": "",
                "provider": "",
                "base_url": "",
                "process_id": 0,
                "status": "active",
                "started_at": "",
                "finished_at": "",
                "api_calls": 0,
                "tool_calls": 0,
                "last_event_type": "",
                "last_event_at": "",
                "last_message": "",
                "_recent_activity": [],
            },
        )
        summary["parent_agent_id"] = str(details.get("parent_agent_session_id", "") or summary["parent_agent_id"])
        run_scope = str(details.get("run_scope", "") or metadata.get("run_scope", "") or "")
        if run_scope:
            summary["_run_scope"] = run_scope
        project_root = str(details.get("project_root", "") or metadata.get("project_root", "") or "")
        if project_root:
            summary["project_root"] = project_root
        workflow_kind = str(details.get("workflow_kind", "") or "")
        if workflow_kind:
            summary["workflow_kind"] = workflow_kind
        workflow_command = str(details.get("workflow_command", "") or "")
        if workflow_command:
            summary["workflow_command"] = workflow_command
        active_skill = str(details.get("active_skill", "") or "")
        if active_skill:
            summary["active_skill"] = active_skill
        try:
            summary["delegate_depth"] = int(details.get("delegate_depth", summary["delegate_depth"]) or 0)
        except Exception:
            pass
        summary["task_label"] = _workflow_task_label(
            str(summary.get("workflow_kind", "") or ""),
            str(summary.get("active_skill", "") or ""),
            int(summary.get("delegate_depth", 0) or 0),
        )
        for key in ("model", "provider", "base_url"):
            value = str(details.get(key, "") or "")
            if value:
                summary[key] = value
        try:
            process_id = int(details.get("process_id", 0) or 0)
            if process_id > 0:
                summary["process_id"] = process_id
        except Exception:
            pass
        timestamp = str(event.get("timestamp", "") or "")
        event_type = str(event.get("type", "") or "")
        summary["last_event_type"] = event_type
        summary["last_event_at"] = timestamp
        summary["last_message"] = _agent_event_preview(event)
        if event_type == "conversation-start" and not summary["started_at"]:
            summary["started_at"] = timestamp
            summary["status"] = "active"
        elif event_type == "conversation-end":
            is_background_workflow_session = (
                str(summary.get("_run_scope", "") or "") == WORKFLOW_RUN_SCOPE_BACKGROUND
                and not str(summary.get("parent_agent_id", "") or "")
                and bool(str(summary.get("workflow_kind", "") or ""))
            )
            if details.get("interrupted"):
                summary["status"] = "interrupted"
                summary["finished_at"] = timestamp
            elif is_background_workflow_session and details.get("completed"):
                summary["status"] = "active"
                summary["finished_at"] = ""
            elif details.get("completed"):
                summary["status"] = "completed"
                summary["finished_at"] = timestamp
            else:
                summary["status"] = "stopped"
                summary["finished_at"] = timestamp
            try:
                summary["api_calls"] = max(int(details.get("api_calls", 0) or 0), int(summary["api_calls"] or 0))
            except Exception:
                pass
        elif event_type == "api-request":
            try:
                summary["api_calls"] = max(int(details.get("iteration", 0) or 0), int(summary["api_calls"] or 0))
            except Exception:
                pass
        elif event_type == "tool-call":
            summary["tool_calls"] = int(summary["tool_calls"] or 0) + 1
        elif event_type == "agent-input-queued":
            summary["status"] = "queued"
            summary["finished_at"] = ""
        elif event_type == "agent-resume":
            summary["status"] = "active"
            summary["finished_at"] = ""
        elif event_type == "agent-awaiting-input":
            summary["status"] = str(details.get("status", "") or "paused")
        elif event_type == "runner-exit":
            summary["status"] = "exited"
            summary["finished_at"] = timestamp
        summary["_recent_activity"].append(
            {
                "timestamp": timestamp,
                "type": event_type,
                "message": str(event.get("message", "") or ""),
                "preview": _agent_event_preview(event),
            }
        )
        summary["_recent_activity"] = summary["_recent_activity"][-max(1, activity_limit):]

    ordered = sorted(
        by_agent.values(),
        key=lambda item: (str(item.get("last_event_at", "") or ""), str(item.get("agent_id", "") or "")),
        reverse=True,
    )
    for summary in ordered:
        process_id = int(summary.get("process_id", 0) or 0)
        status = str(summary.get("status", "") or "")
        if process_id > 0 and status not in _TERMINAL_AGENT_STATUSES and not _process_seems_alive(process_id):
            summary["status"] = "dead"
            if not str(summary.get("finished_at", "") or ""):
                summary["finished_at"] = str(summary.get("last_event_at", "") or "")
    live_status = load_workflow_live_status()
    live_phase = _agent_status_from_live_phase(str(live_status.get("phase", "") or ""))
    live_task_label = _workflow_task_label(
        str(live_status.get("workflow_kind", "") or ""),
        str(live_status.get("active_skill", "") or ""),
        0,
    )
    try:
        live_process_id = int(live_status.get("process_id", 0) or 0)
    except Exception:
        live_process_id = 0
    live_process_alive = live_process_id > 0 and _process_seems_alive(live_process_id)
    if live_phase:
        for summary in ordered:
            if int(summary.get("delegate_depth", 0) or 0) != 0:
                continue
            if live_task_label and str(summary.get("task_label", "") or "") != live_task_label:
                continue
            summary_process_id = int(summary.get("process_id", 0) or 0)
            if summary_process_id > 0 and not _process_seems_alive(summary_process_id):
                continue
            if live_process_id > 0 and summary_process_id > 0 and summary_process_id != live_process_id:
                continue
            if live_process_id > 0 and not live_process_alive:
                continue
            summary["status"] = live_phase
            if live_phase in {"active", "blocked", "paused"}:
                summary["finished_at"] = ""
            break
    for summary in ordered:
        summary.pop("_run_scope", None)
        summary["recent_activity"] = summary.pop("_recent_activity")
    return ordered


def workflow_agent_detail(agent_id: str, *, activity_limit: int = 5) -> dict[str, Any]:
    for summary in summarize_workflow_agents(activity_limit=activity_limit):
        if str(summary.get("agent_id", "") or "") == agent_id:
            return summary
    return {}


def workflow_agent_transcript(agent_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
    events = read_workflow_activity(
        limit=max(1, limit * 8),
        agent_id=agent_id,
        event_types={
            "agent-input-queued",
            "agent-resume",
            "conversation-start",
            "assistant-response",
            "tool-call",
            "tool-result",
            "conversation-end",
            "agent-awaiting-input",
            "runner-exit",
        },
    )
    transcript: list[dict[str, Any]] = []
    for event in events:
        details = event.get("details")
        details = details if isinstance(details, dict) else {}
        event_type = str(event.get("type", "") or "")
        role = "event"
        content = str(event.get("message", "") or "")
        if event_type == "conversation-start":
            role = "user"
            content = str(details.get("user_message", "") or content)
        elif event_type == "agent-input-queued":
            role = "user"
            content = str(details.get("text", "") or _agent_event_preview(event))
        elif event_type == "agent-resume":
            role = "event"
            content = _agent_event_preview(event)
        elif event_type == "assistant-response":
            role = "assistant"
            content = str(details.get("content", "") or _agent_event_preview(event))
        elif event_type == "tool-call":
            role = "tool-call"
            content = _agent_event_preview(event)
        elif event_type == "tool-result":
            role = "tool-result"
            content = _agent_event_preview(event)
        elif event_type == "conversation-end":
            role = "event"
            content = _agent_event_preview(event)
        elif event_type == "agent-awaiting-input":
            role = "event"
            content = _agent_event_preview(event)
        elif event_type == "runner-exit":
            role = "event"
            content = _agent_event_preview(event)
        transcript.append(
            {
                "timestamp": str(event.get("timestamp", "") or ""),
                "type": event_type,
                "role": role,
                "content": content.strip(),
            }
        )
    return transcript[-max(1, limit):]


def workflow_agent_transcript_all(agent_id: str) -> list[dict[str, Any]]:
    return workflow_agent_transcript(agent_id, limit=10000)


def resolve_workflow_agent_id(agent_ref: str) -> str:
    ref = str(agent_ref or "").strip()
    if not ref:
        return ""
    summaries = summarize_workflow_agents(activity_limit=1)
    exact = [str(summary.get("agent_id", "") or "") for summary in summaries if str(summary.get("agent_id", "") or "") == ref]
    if exact:
        return exact[0]
    prefix = [str(summary.get("agent_id", "") or "") for summary in summaries if str(summary.get("agent_id", "") or "").startswith(ref)]
    if len(prefix) == 1:
        return prefix[0]
    return ""


def terminate_workflow_agent(agent_ref: str) -> dict[str, Any]:
    agent_id = resolve_workflow_agent_id(agent_ref)
    if not agent_id:
        return {"success": False, "error": "Agent not found or ambiguous."}
    detail = workflow_agent_detail(agent_id, activity_limit=1)
    process_id = int(detail.get("process_id", 0) or 0)
    if process_id <= 0:
        return {"success": False, "error": "No process id recorded for this agent.", "agent_id": agent_id}
    try:
        os.killpg(process_id, signal.SIGINT)
    except Exception:
        try:
            os.kill(process_id, signal.SIGINT)
        except ProcessLookupError:
            return {"success": False, "error": "Process already exited.", "agent_id": agent_id, "process_id": process_id}
        except Exception as exc:
            return {"success": False, "error": str(exc), "agent_id": agent_id, "process_id": process_id}
    return {"success": True, "agent_id": agent_id, "process_id": process_id}


def terminate_workflow_agent_descendants(agent_ref: str) -> dict[str, Any]:
    agent_id = resolve_workflow_agent_id(agent_ref)
    if not agent_id:
        return {"success": False, "error": "Agent not found or ambiguous."}
    summaries = summarize_workflow_agents(activity_limit=1)
    by_parent: dict[str, list[str]] = {}
    for summary in summaries:
        child_id = str(summary.get("agent_id", "") or "")
        parent_id = str(summary.get("parent_agent_id", "") or "")
        if child_id and parent_id:
            by_parent.setdefault(parent_id, []).append(child_id)

    descendants: list[str] = []
    stack = list(by_parent.get(agent_id, []))
    seen: set[str] = set()
    while stack:
        child_id = stack.pop()
        if child_id in seen:
            continue
        seen.add(child_id)
        descendants.append(child_id)
        stack.extend(by_parent.get(child_id, []))

    results: list[dict[str, Any]] = []
    for child_id in descendants:
        results.append(terminate_workflow_agent(child_id))

    success_count = sum(1 for item in results if item.get("success"))
    failed = [item for item in results if not item.get("success")]
    return {
        "success": not failed,
        "agent_id": agent_id,
        "terminated": [item.get("agent_id") for item in results if item.get("success")],
        "failed": failed,
        "count": success_count,
    }


def terminate_all_workflow_agents(*, exclude_agent_id: str = "", exclude_process_id: int = 0) -> dict[str, Any]:
    summaries = summarize_workflow_agents(activity_limit=1)
    results: list[dict[str, Any]] = []
    for summary in summaries:
        agent_id = str(summary.get("agent_id", "") or "")
        process_id = int(summary.get("process_id", 0) or 0)
        status = str(summary.get("status", "") or "")
        if not agent_id or process_id <= 0:
            continue
        if exclude_agent_id and agent_id == exclude_agent_id:
            continue
        if exclude_process_id and process_id == exclude_process_id:
            continue
        if status in _TERMINAL_AGENT_STATUSES or not _process_seems_alive(process_id):
            continue
        results.append(terminate_workflow_agent(agent_id))

    success_count = sum(1 for item in results if item.get("success"))
    failed = [item for item in results if not item.get("success")]
    return {
        "success": not failed,
        "terminated": [item.get("agent_id") for item in results if item.get("success")],
        "failed": failed,
        "count": success_count,
    }


def request_project_workflow_runner_exit(
    project_root: str,
    *,
    exclude_agent_id: str = "",
    exclude_process_id: int = 0,
) -> dict[str, Any]:
    normalized_root = str(project_root or "").strip()
    summaries = summarize_workflow_agents(activity_limit=1)
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for summary in summaries:
        agent_id = str(summary.get("agent_id", "") or "")
        process_id = int(summary.get("process_id", 0) or 0)
        agent_root = str(summary.get("project_root", "") or "")
        parent_agent_id = str(summary.get("parent_agent_id", "") or "")
        status = str(summary.get("status", "") or "")
        if not agent_id or agent_id in seen:
            continue
        if process_id <= 0 or not _process_seems_alive(process_id):
            continue
        if normalized_root and agent_root != normalized_root:
            continue
        if exclude_agent_id and agent_id == exclude_agent_id:
            continue
        if exclude_process_id and process_id == exclude_process_id:
            continue
        if parent_agent_id:
            continue
        if status in {"exited", "stopped", "interrupted", "completed"}:
            continue
        seen.add(agent_id)
        results.append(enqueue_workflow_agent_message(agent_id, "exit", kind="exit"))

    success_count = sum(1 for item in results if item.get("success"))
    failed = [item for item in results if not item.get("success")]
    return {
        "success": not failed,
        "queued": [item.get("agent_id") for item in results if item.get("success")],
        "failed": failed,
        "count": success_count,
    }


def terminate_project_workflow_agents(
    project_root: str,
    *,
    exclude_agent_id: str = "",
    exclude_process_id: int = 0,
) -> dict[str, Any]:
    normalized_root = str(project_root or "").strip()
    summaries = summarize_workflow_agents(activity_limit=1)
    results: list[dict[str, Any]] = []
    for summary in summaries:
        agent_id = str(summary.get("agent_id", "") or "")
        process_id = int(summary.get("process_id", 0) or 0)
        agent_root = str(summary.get("project_root", "") or "")
        status = str(summary.get("status", "") or "")
        if not agent_id or process_id <= 0:
            continue
        if normalized_root and agent_root != normalized_root:
            continue
        if exclude_agent_id and agent_id == exclude_agent_id:
            continue
        if exclude_process_id and process_id == exclude_process_id:
            continue
        if status in _TERMINAL_AGENT_STATUSES or not _process_seems_alive(process_id):
            continue
        results.append(terminate_workflow_agent(agent_id))

    success_count = sum(1 for item in results if item.get("success"))
    failed = [item for item in results if not item.get("success")]
    return {
        "success": not failed,
        "terminated": [item.get("agent_id") for item in results if item.get("success")],
        "failed": failed,
        "count": success_count,
    }


def reset_workflow_run_log() -> Path:
    path = workflow_run_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    workflow_runs_root().mkdir(parents=True, exist_ok=True)
    os.environ["EPFLEMMA_WORKFLOW_RUN_ID"] = _workflow_run_id()
    path.write_text("", encoding="utf-8")
    workflow_timestamped_run_log_path().write_text("", encoding="utf-8")
    return path


def append_workflow_run_log(text: str) -> None:
    if not text:
        return
    path = workflow_run_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamped_path = workflow_timestamped_run_log_path()
    timestamped_path.parent.mkdir(parents=True, exist_ok=True)
    _locked_append(path, text)
    _locked_append(timestamped_path, text)


def read_workflow_run_log(tail_lines: int = 120) -> str:
    path = workflow_run_log_path()
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        logger.debug("Failed to read workflow run log %s", path, exc_info=True)
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
