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


def workflow_runs_root() -> Path:
    return workflow_state_root() / "runs"


def _workflow_run_id() -> str:
    run_id = str(os.getenv("EPFLEMMA_WORKFLOW_RUN_ID", "") or "").strip()
    if run_id:
        return run_id
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{started}-pid{os.getpid()}"
    os.environ["EPFLEMMA_WORKFLOW_RUN_ID"] = run_id
    return run_id


def workflow_timestamped_run_log_path() -> Path:
    return workflow_runs_root() / f"{_workflow_run_id()}.log"


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


def _read_all_workflow_activity() -> list[dict[str, Any]]:
    path = workflow_activity_path()
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


def _shorten_text(text: Any, limit: int = 120) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _agent_event_preview(event: Mapping[str, Any]) -> str:
    details = event.get("details")
    details = details if isinstance(details, dict) else {}
    event_type = str(event.get("type", "") or "")
    if event_type == "assistant-response":
        content = str(details.get("content", "") or "")
        if content.strip():
            return _shorten_text(content, limit=140)
        tool_calls = details.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            names = [
                str(call.get("name", "") or "")
                for call in tool_calls
                if isinstance(call, dict) and str(call.get("name", "") or "")
            ]
            if names:
                return f"Requested tools: {', '.join(names[:4])}"
    if event_type == "tool-call":
        tool_name = str(details.get("tool", "") or "")
        if tool_name:
            return f"Call {tool_name}"
    if event_type == "tool-result":
        tool_name = str(details.get("tool", "") or "")
        is_error = bool(details.get("is_error"))
        if tool_name:
            return f"{tool_name} {'failed' if is_error else 'completed'}"
    if event_type == "api-request":
        iteration = details.get("iteration")
        if iteration is not None:
            return f"API call #{iteration}"
    if event_type == "conversation-start":
        return _shorten_text(details.get("user_message", ""), limit=140) or str(event.get("message", "") or "")
    if event_type == "conversation-end":
        if details.get("interrupted"):
            return "Interrupted"
        if details.get("completed"):
            return "Completed"
    return _shorten_text(event.get("message", ""), limit=140)


def summarize_workflow_agents(*, activity_limit: int = 5) -> list[dict[str, Any]]:
    events = _read_all_workflow_activity()
    by_agent: dict[str, dict[str, Any]] = {}
    for event in events:
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        agent_id = str(details.get("agent_session_id", "") or "")
        if not agent_id:
            continue
        summary = by_agent.setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "parent_agent_id": "",
                "delegate_depth": 0,
                "model": "",
                "provider": "",
                "base_url": "",
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
        try:
            summary["delegate_depth"] = int(details.get("delegate_depth", summary["delegate_depth"]) or 0)
        except Exception:
            pass
        for key in ("model", "provider", "base_url"):
            value = str(details.get(key, "") or "")
            if value:
                summary[key] = value
        timestamp = str(event.get("timestamp", "") or "")
        event_type = str(event.get("type", "") or "")
        summary["last_event_type"] = event_type
        summary["last_event_at"] = timestamp
        summary["last_message"] = _agent_event_preview(event)
        if event_type == "conversation-start" and not summary["started_at"]:
            summary["started_at"] = timestamp
            summary["status"] = "active"
        elif event_type == "conversation-end":
            summary["finished_at"] = timestamp
            if details.get("interrupted"):
                summary["status"] = "interrupted"
            elif details.get("completed"):
                summary["status"] = "completed"
            else:
                summary["status"] = "stopped"
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
        summary["recent_activity"] = summary.pop("_recent_activity")
    return ordered


def workflow_agent_detail(agent_id: str, *, activity_limit: int = 5) -> dict[str, Any]:
    for summary in summarize_workflow_agents(activity_limit=activity_limit):
        if str(summary.get("agent_id", "") or "") == agent_id:
            return summary
    return {}


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
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
    with timestamped_path.open("a", encoding="utf-8") as handle:
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
