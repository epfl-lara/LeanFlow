#!/usr/bin/env python3
"""Local managed Lean workflow runner for the epflemma-native backend."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from difflib import unified_diff
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.auxiliary_client import call_llm
from agent.context_compressor import ContextCompressor
from agent.model_metadata import estimate_messages_tokens_rough
from epflemma_cli.file_locks import list_file_locks, release_all_file_locks
from epflemma_cli.lean_services import (
    lean_inspect,
    recent_empty_search_streak,
    lean_verify,
    probe_capabilities,
    route_workflow_step,
)
from epflemma_cli.skill_core import build_skill_prompt
from epflemma_cli.config import load_config
from epflemma_cli.workflow_state import (
    append_workflow_activity,
    append_workflow_run_log,
    read_workflow_agent_inbox,
    reset_workflow_run_log,
    save_workflow_live_status,
    summarize_workflow_agents,
    terminate_all_workflow_agents,
    terminate_project_workflow_agents,
    terminate_workflow_agent_descendants,
    workflow_agent_detail,
)
from run_agent import AIAgent

MANAGED_SNAPSHOT_PREFIX = (
    "[EPFLEMMA-NATIVE MANAGED SNAPSHOT] Earlier managed workflow turns were compacted "
    "to preserve context space. Use this snapshot as the authoritative handoff "
    "for prior work, but still inspect the live project state before repeating work."
)
WORKFLOW_CHECKPOINT_PREFIX = (
    "[EPFLEMMA-NATIVE WORKFLOW CHECKPOINT] This persisted workflow handoff captures a "
    "previous autonomous milestone. Use it as the source of truth for resuming this "
    "managed session, and reconcile it with the current filesystem before redoing work."
)
LIVE_PROOF_STATE_PREFIX = (
    "[EPFLEMMA-NATIVE LIVE PROOF STATE] This is the latest runner-refreshed Lean state "
    "for the active workflow. Treat it as current unless newer tool results contradict it."
)
AUTONOMOUS_WORKFLOW_KINDS = {"prove", "formalize"}
PROJECT_SCAN_SKIP_DIRS = {".git", ".lake", ".epflemma", ".opengauss", ".gauss", "build"}
WORKFLOW_STEP_BOUNDARY_INTERRUPT = "[epflemma-native workflow step boundary]"
ACTIVE_AGENT_STATUSES = {"active"}
LIVE_AGENT_STATUSES = {"active", "blocked", "paused", "queued"}
DEAD_AGENT_STATUSES = {"dead"}


def _utc_now_isoformat() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_text_env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _read_native_env(name: str, default: str = "") -> str:
    return _read_text_env(
        f"EPFLEMMA_NATIVE_{name}",
        _read_text_env(f"OPENGAUSS_NATIVE_{name}", _read_text_env(f"GAUSS_NATIVE_{name}", default)),
    )


def _managed_home() -> Path:
    return Path(
        _read_text_env(
            "EPFLEMMA_HOME",
            _read_text_env("OPENGAUSS_HOME", _read_text_env("GAUSS_HOME", str(Path.home() / ".epflemma"))),
        )
    ).expanduser()


def _project_root() -> str:
    return _read_text_env(
        "EPFLEMMA_PROJECT_ROOT",
        _read_text_env("OPENGAUSS_PROJECT_ROOT", _read_text_env("GAUSS_PROJECT_ROOT", os.getcwd())),
    )


def _workflow_kind() -> str:
    return _read_native_env("WORKFLOW_KIND", "workflow").strip().lower()


def _workflow_display_name(workflow_kind: str | None = None) -> str:
    return str(workflow_kind or _workflow_kind() or "")


def _native_interactive_enabled() -> bool:
    raw = _read_native_env("INTERACTIVE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _is_autonomous_workflow() -> bool:
    return _workflow_kind() in AUTONOMOUS_WORKFLOW_KINDS


def _parallel_agents() -> int:
    raw = _read_native_env("PARALLEL_AGENTS", "1")
    try:
        return max(1, int(raw))
    except ValueError:
        return 1


def _single_queue_item_turn_enabled() -> bool:
    return _is_autonomous_workflow() and bool(_read_native_env("ACTIVE_FILE", "").strip())


def _base_active_skill() -> str:
    return _read_native_env("ACTIVE_SKILL", "").strip()


def _effective_skill_name(live_state: Mapping[str, Any] | None = None) -> str:
    configured = _base_active_skill()
    state = dict(live_state or {})
    route = dict(state.get("route_decision") or {})
    if route.get("skill_name"):
        return str(route.get("skill_name") or "")
    if not _single_queue_item_turn_enabled():
        return configured
    if configured and configured not in {"lean-proof-loop", "lean-theorem-queue-worker"}:
        return configured
    if state and not state.get("current_queue_item"):
        return "lean-proof-loop"
    return "lean-theorem-queue-worker"


def _set_runtime_active_skill(skill_name: str) -> None:
    normalized = str(skill_name or "").strip()
    if normalized:
        os.environ["EPFLEMMA_NATIVE_ACTIVE_SKILL"] = normalized


def _is_step_boundary_interrupt(result: Mapping[str, Any] | None) -> bool:
    if not result:
        return False
    return str(result.get("interrupt_message", "") or "").strip() == WORKFLOW_STEP_BOUNDARY_INTERRUPT


def _swarm_enabled() -> bool:
    return _parallel_agents() > 1 and _read_native_env("USER_APPROVED_SWARM", "0") == "1"


def _runner_lean_prompt_enabled() -> bool:
    raw = _read_text_env("EPFLEMMA_RUNNER_LEAN_PROMPT", "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _runner_owner_id() -> str:
    return _read_native_env("RUNNER_OWNER", "")


def _autonomous_followup_limit() -> int:
    raw = _read_native_env("AUTONOMOUS_FOLLOWUPS", "6")
    try:
        return max(1, int(raw))
    except ValueError:
        return 6


def _autonomous_blocked_limit() -> int:
    raw = _read_native_env("AUTONOMOUS_BLOCKED_LIMIT", "3")
    try:
        return max(2, int(raw))
    except ValueError:
        return 3


def _autonomous_stalled_limit() -> int:
    raw = _read_native_env("AUTONOMOUS_STALLED_LIMIT", "4")
    try:
        return max(2, int(raw))
    except ValueError:
        return 4


def _workflow_state_root() -> Path:
    project_root = Path(_project_root()).expanduser().resolve()
    if project_root.exists():
        return project_root / ".epflemma" / "workflow-state"
    return _managed_home() / "workflow-state"


def _workflow_state_index_path() -> Path:
    return _workflow_state_root() / "index.json"


def _workflow_state_current_path() -> Path:
    return _workflow_state_root() / "current.json"


def _active_skill() -> str:
    return _effective_skill_name()


def _ensure_workflow_state_root() -> Path:
    root = _workflow_state_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
    except Exception:
        pass
    return {}


def _write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


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


def _load_current_checkpoint() -> dict[str, Any] | None:
    payload = _read_json_file(_workflow_state_current_path())
    checkpoint_id = str(payload.get("checkpoint_id", "") or "").strip()
    snapshot_path = str(payload.get("snapshot_path", "") or "").strip()
    if not checkpoint_id or not snapshot_path:
        return None
    snapshot = _load_checkpoint_snapshot(snapshot_path)
    if snapshot is None:
        return None
    return snapshot


def _journal_status() -> dict[str, Any]:
    entries = _load_workflow_index()
    current = _load_current_checkpoint()
    return {
        "count": len(entries),
        "current": current,
        "latest_label": str((current or {}).get("label", "") or ""),
        "latest_filesystem_checkpoint": str(
            (current or {}).get("linked_filesystem_checkpoint", "") or ""
        ),
    }


def _workflow_phase(
    live_state: Mapping[str, Any] | None = None,
    *,
    explicit: str = "",
    compaction_state: Mapping[str, Any] | None = None,
) -> str:
    if explicit:
        return explicit
    if compaction_state and compaction_state.get("compacted"):
        return "compacted"
    if _live_state_is_verified(live_state):
        return "verified"
    blocker_summary = str((live_state or {}).get("blocker_summary", "") or "")
    diagnostics = str((live_state or {}).get("diagnostics", "") or "")
    if blocker_summary or _diagnostics_indicate_failure(diagnostics):
        return "blocked"
    return "in-progress"


def _persist_live_status(
    history: list[dict[str, Any]],
    compaction_state: Mapping[str, Any] | None = None,
    checkpoint_state: Mapping[str, Any] | None = None,
    live_state: Mapping[str, Any] | None = None,
    *,
    phase: str = "",
) -> None:
    checkpoint_state = dict(checkpoint_state or _journal_status())
    live_state = dict(live_state or _build_live_proof_state(history, checkpoint_state))
    current_checkpoint = dict(checkpoint_state.get("current") or {})
    payload = {
        "version": 1,
        "updated_at": _utc_now_isoformat(),
        "phase": _workflow_phase(live_state, explicit=phase, compaction_state=compaction_state),
        "workflow_kind": _workflow_kind(),
        "workflow_command": _read_native_env("WORKFLOW_COMMAND", "[unset]"),
        "project_root": _project_root(),
        "provider": _read_native_env("PROVIDER"),
        "model": _read_native_env("MODEL"),
        "base_url": _read_native_env("BASE_URL"),
        "process_id": os.getpid(),
        "active_skill": _effective_skill_name(live_state),
        "parallel_agents": _parallel_agents(),
        "active_file": str(live_state.get("active_file", "") or ""),
        "active_file_label": str(live_state.get("active_file_label", "") or "[unknown]"),
        "target_symbol": str(live_state.get("target_symbol", "") or "[unknown]"),
        "declaration_scope": str(live_state.get("declaration_scope", "") or _declaration_queue_scope()),
        "declaration_queue_total": int(live_state.get("declaration_queue_total", 0) or 0),
        "declaration_queue_preview": list(live_state.get("declaration_queue_preview", []) or []),
        "declaration_queue_summary": str(live_state.get("declaration_queue_summary", "") or "[none]"),
        "current_queue_item": dict(live_state.get("current_queue_item", {}) or {}),
        "current_queue_item_prefix": str(live_state.get("current_queue_item_prefix", "") or ""),
        "current_queue_item_slice": str(live_state.get("current_queue_item_slice", "") or ""),
        "current_blocker": str(live_state.get("current_blocker", "") or ""),
        "diagnostics": str(live_state.get("diagnostics", "") or "unavailable"),
        "goals": str(live_state.get("goals", "") or "unavailable"),
        "build_status": str(live_state.get("build_status", "") or "unknown"),
        "proof_state_message": str(live_state.get("message", "") or ""),
        "sorry_count": live_state.get("sorry_count"),
        "project_sorry_count": live_state.get("project_sorry_count"),
        "capability_report": dict(live_state.get("capability_report", {}) or {}),
        "route_decision": dict(live_state.get("route_decision", {}) or {}),
        "checkpoint_count": int(checkpoint_state.get("count", 0) or 0),
        "latest_checkpoint_label": str(current_checkpoint.get("label", "") or "[none]"),
        "latest_filesystem_checkpoint": str(current_checkpoint.get("linked_filesystem_checkpoint", "") or "[none]"),
        "last_compaction_reason": str((compaction_state or {}).get("reason", "[none]") or "[none]"),
        "snapshot_present": bool((compaction_state or {}).get("snapshot_text")),
        "held_locks": _held_lock_count(_runner_owner_id()),
    }
    save_workflow_live_status(payload)


def _record_activity(event_type: str, message: str, **details: Any) -> None:
    active_skill = str(details.pop("active_skill", "") or _active_skill())
    append_workflow_activity(
        event_type,
        message,
        workflow_kind=_workflow_kind(),
        workflow_command=_read_native_env("WORKFLOW_COMMAND", "[unset]"),
        active_skill=active_skill,
        **details,
    )


def _agent_activity_details(agent: Any) -> dict[str, Any]:
    return {
        "agent_session_id": str(getattr(agent, "session_id", "") or ""),
        "parent_agent_session_id": str(getattr(agent, "_parent_session_id", "") or ""),
        "delegate_depth": int(getattr(agent, "_delegate_depth", 0) or 0),
        "project_root": _project_root(),
        "model": _read_native_env("MODEL"),
        "provider": _read_native_env("PROVIDER"),
        "base_url": _read_native_env("BASE_URL"),
        "api_mode": _read_native_env("API_MODE"),
        "process_id": os.getpid(),
    }


def _record_agent_activity(agent: Any, event_type: str, message: str, **details: Any) -> None:
    payload = _agent_activity_details(agent)
    payload.update(details)
    _record_activity(event_type, message, **payload)


def _record_queue_assignment(
    live_state: Mapping[str, Any],
    *,
    cycle: int | None = None,
    phase: str = "",
) -> None:
    item = dict(live_state.get("current_queue_item") or {})
    if not item:
        return
    label = str(item.get("label", "") or live_state.get("target_symbol", "") or "[unknown]")
    payload: dict[str, Any] = {
        "queue_item": item,
        "target_symbol": label,
        "active_file": str(live_state.get("active_file_label", "") or live_state.get("active_file", "") or ""),
        "reasons": list(item.get("reasons", []) or []),
        "blocker_signature": str(item.get("blocker_signature", "") or ""),
        "search_hints": list(item.get("search_hints", []) or []),
        "verification_gate": str(item.get("verification_gate", "") or ""),
        "recommended_worker": str(
            item.get("recommended_worker", "")
            or dict(live_state.get("route_decision", {}) or {}).get("recommended_worker", "")
            or ""
        ),
    }
    if cycle is not None:
        payload["cycle"] = cycle
    if phase:
        payload["phase"] = phase
    payload["active_skill"] = _effective_skill_name(live_state)
    _record_activity("queue-item-assigned", f"Queue assigned theorem {label}", **payload)


_CURRENT_AGENT_ACTIVITY_DETAILS: dict[str, Any] = {}


def _logging_config() -> Mapping[str, Any]:
    try:
        config = load_config()
    except Exception:
        return {}
    logging_cfg = config.get("logging", {})
    return logging_cfg if isinstance(logging_cfg, dict) else {}


def _agent_config() -> Mapping[str, Any]:
    try:
        config = load_config()
    except Exception:
        return {}
    agent_cfg = config.get("agent", {})
    return agent_cfg if isinstance(agent_cfg, dict) else {}


def _parse_managed_reasoning_config(effort: str) -> dict[str, Any] | None:
    normalized = str(effort or "").strip().lower()
    if not normalized:
        return None
    if normalized == "auto":
        return {"mode": "auto"}
    if normalized == "none":
        return {"enabled": False}
    if normalized in {"low", "minimal", "medium", "high", "xhigh"}:
        return {"enabled": True, "effort": normalized}
    return None


def _positive_int_config(name: str, default: int) -> int:
    try:
        value = int(_logging_config().get(name, default))
    except Exception:
        return default
    return value if value > 0 else default


def _single_line(text: Any, limit: int | None = None) -> str:
    effective_limit = limit if limit is not None else max(_positive_int_config("activity_preview_chars", 420) + 140, 560)
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= effective_limit:
        return collapsed
    return collapsed[: effective_limit - 3] + "..."


def _active_file_candidates(active_file: str) -> set[str]:
    normalized = str(active_file or "").strip()
    if not normalized:
        return set()
    candidates = {normalized}
    try:
        path = Path(normalized)
        if path.is_absolute():
            candidates.add(str(path.resolve()))
            try:
                candidates.add(str(path.resolve().relative_to(Path(_project_root()).resolve())))
            except Exception:
                pass
    except Exception:
        pass
    return {value for value in candidates if value}


def _same_active_file(left: str, right: str) -> bool:
    left_value = str(left or "").strip()
    right_value = str(right or "").strip()
    if not left_value or not right_value:
        return False
    left_candidates = _active_file_candidates(left_value)
    right_candidates = _active_file_candidates(right_value)
    if not left_candidates.isdisjoint(right_candidates):
        return True

    try:
        left_parts = Path(left_value).parts
        right_parts = Path(right_value).parts
    except Exception:
        return False
    if left_parts and right_parts:
        if len(left_parts) >= len(right_parts) and left_parts[-len(right_parts):] == right_parts:
            return True
        if len(right_parts) >= len(left_parts) and right_parts[-len(left_parts):] == left_parts:
            return True
    return False


def _scoped_failed_attempt_entries(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> list[dict[str, Any]]:
    normalized_target = str(target_symbol or "").strip()
    normalized_file = str(active_file or "").strip()
    if not normalized_target or not normalized_file:
        return []
    attempts = [dict(item) for item in autonomy_state.get("failed_attempts", []) if isinstance(item, Mapping)]
    return [
        attempt
        for attempt in attempts
        if str(attempt.get("target_symbol", "") or "").strip() == normalized_target
        and _same_active_file(str(attempt.get("active_file", "") or ""), normalized_file)
    ]


def _failed_attempt_count_for_theorem(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> int:
    scoped = _scoped_failed_attempt_entries(
        autonomy_state,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    if not scoped:
        return 0
    numbered = [int(attempt.get("attempt", 0) or 0) for attempt in scoped if int(attempt.get("attempt", 0) or 0) > 0]
    if numbered:
        return max(numbered)
    return len(scoped)


def _resolve_managed_reasoning_config(
    base_reasoning_config: Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None,
    autonomy_state: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    base = dict(base_reasoning_config or {})
    if not base:
        return None
    if base.get("enabled") is False:
        return {"enabled": False}
    if base.get("mode") != "auto":
        return base

    current = dict(live_state or {})
    autonomy = dict(autonomy_state or {})
    if _queue_needs_final_file_sweep(current):
        return {"enabled": True, "effort": "high"}

    target_symbol, active_file = _queue_assignment_identity(current)
    if target_symbol and active_file:
        attempts = _failed_attempt_count_for_theorem(
            autonomy,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        effort = "high" if attempts >= _failed_attempt_reasoning_threshold() else "medium"
        return {"enabled": True, "effort": effort}

    return {"enabled": True, "effort": "high"}


def _apply_managed_reasoning_policy(
    agent: AIAgent,
    live_state: Mapping[str, Any] | None,
    autonomy_state: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    base_reasoning = getattr(agent, "_managed_base_reasoning_config", None)
    effective = _resolve_managed_reasoning_config(base_reasoning, live_state, autonomy_state)
    agent.reasoning_config = effective
    return effective


def _record_managed_reasoning_policy(
    live_state: Mapping[str, Any] | None,
    autonomy_state: Mapping[str, Any] | None,
    effective: Mapping[str, Any] | None,
    *,
    phase: str,
    cycle: int | None = None,
) -> None:
    current = dict(live_state or {})
    autonomy = dict(autonomy_state or {})
    target_symbol, active_file = _queue_assignment_identity(current)
    failed_attempt_count = 0
    if target_symbol and active_file:
        failed_attempt_count = _failed_attempt_count_for_theorem(
            autonomy,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    effort = ""
    enabled = True
    if effective:
        enabled = bool(effective.get("enabled", True))
        effort = str(effective.get("effort", "") or "")
    message = "Managed reasoning policy applied"
    if effort:
        message += f": {effort}"
    elif not enabled:
        message += ": disabled"
    details = {
        "phase": phase,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "failed_attempt_count": failed_attempt_count,
        "failed_attempt_reasoning_threshold": _failed_attempt_reasoning_threshold(),
        "effective_reasoning_effort": effort,
        "reasoning_enabled": enabled,
    }
    if cycle is not None:
        details["cycle"] = cycle
    _record_activity("managed-reasoning-policy", message, **details)


def _tool_result_counts_as_theorem_feedback(function_name: str, args: Mapping[str, Any] | None = None) -> bool:
    if function_name in {"lean_inspect", "lean_verify", "apply_verified_patch"}:
        return True
    if function_name != "terminal":
        return False
    arguments = dict(args or {})
    command = str(arguments.get("command", "") or arguments.get("cmd", "") or "").lower()
    if not command:
        return False
    return any(token in command for token in ("lake env lean", "lake build", " lean", " typecheck"))


def _prepare_managed_turn_state(agent: Any, autonomy_state: dict[str, Any]) -> None:
    agent._managed_autonomy_state = autonomy_state
    agent._managed_pending_theorem_feedback = None


def _handle_managed_tool_result(
    agent: Any,
    function_name: str,
    args: Mapping[str, Any] | None,
    _result: str,
) -> None:
    del _result
    if not _single_queue_item_turn_enabled() or agent.is_interrupted():
        return

    if function_name == "apply_verified_patch":
        baseline = dict(getattr(agent, "_managed_autonomy_state", {}) or {}).get("current_queue_assignment", {})
        target_symbol = str(
            dict(baseline or {}).get("target_symbol", "")
            or dict(args or {}).get("theorem_id", "")
            or ""
        ).strip()
        active_file = str(
            dict(baseline or {}).get("active_file", "")
            or dict(args or {}).get("path", "")
            or ""
        ).strip()
        if not target_symbol or not active_file:
            live_state = _build_live_proof_state(list(getattr(agent, "_session_messages", []) or []))
            live_target, live_file = _queue_assignment_identity(live_state)
            target_symbol = target_symbol or live_target
            active_file = active_file or live_file
        if target_symbol and active_file:
            agent._managed_pending_theorem_feedback = {
                "target_symbol": target_symbol,
                "active_file": active_file,
            }

    if function_name in {"patch", "write_file"}:
        baseline = dict(getattr(agent, "_managed_autonomy_state", {}) or {}).get("current_queue_assignment", {})
        target_symbol = str(dict(baseline or {}).get("target_symbol", "") or "").strip()
        active_file = str(dict(baseline or {}).get("active_file", "") or "").strip()
        if not target_symbol or not active_file:
            live_state = _build_live_proof_state(list(getattr(agent, "_session_messages", []) or []))
            target_symbol, active_file = _queue_assignment_identity(live_state)
        if target_symbol and active_file:
            agent._managed_pending_theorem_feedback = {
                "target_symbol": target_symbol,
                "active_file": active_file,
            }
        return

    pending = dict(getattr(agent, "_managed_pending_theorem_feedback", None) or {})
    pending_target = str(pending.get("target_symbol", "") or "").strip()
    pending_file = str(pending.get("active_file", "") or "").strip()
    if not pending_target or not pending_file:
        return
    if not _tool_result_counts_as_theorem_feedback(function_name, args):
        return

    live_state = _build_live_proof_state(list(getattr(agent, "_session_messages", []) or []))
    still_blocked = _same_queue_assignment_still_blocked(
        {
            "current_queue_assignment": {
                "target_symbol": pending_target,
                "active_file": pending_file,
            }
        },
        live_state,
    )
    if still_blocked:
        autonomy_state = getattr(agent, "_managed_autonomy_state", None)
        if isinstance(autonomy_state, dict):
            _remember_failed_attempt(
                autonomy_state,
                live_state,
                cycle_number=int(autonomy_state.get("current_cycle", 0) or 0),
            )
            attempt_number = _failed_attempt_count_for_theorem(
                autonomy_state,
                target_symbol=pending_target,
                active_file=pending_file,
            )
            _record_activity(
                "failed-attempt-recorded",
                f"Recorded failed theorem attempt #{attempt_number} for {pending_target}",
                target_symbol=pending_target,
                active_file=pending_file,
                attempt=attempt_number,
                verification_tool=function_name,
            )

    item = dict(live_state.get("current_queue_item") or {})
    _record_activity(
        "queue-step-boundary",
        (
            f"Yielding after failed verification feedback for {pending_target}"
            if still_blocked
            else f"Yielding after verification feedback for {pending_target}"
        ),
        queue_item=item,
        target_symbol=pending_target,
        active_file=pending_file,
        reasons=list(item.get("reasons", []) or []),
        verification_tool=function_name,
        still_blocked=still_blocked,
    )
    agent._managed_pending_theorem_feedback = None
    agent.interrupt(WORKFLOW_STEP_BOUNDARY_INTERRUPT)


def _managed_agent_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _managed_agent_seed(value: Any) -> int | None:
    return _managed_agent_int(value)


def _managed_agent_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class _WorkflowLogTee:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, data: str) -> int:
        append_workflow_run_log(data)
        return self._stream.write(data)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        return self._stream.fileno()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _install_workflow_run_log_capture() -> None:
    reset_workflow_run_log()
    if not isinstance(sys.stdout, _WorkflowLogTee):
        sys.stdout = _WorkflowLogTee(sys.stdout)
    if not isinstance(sys.stderr, _WorkflowLogTee):
        sys.stderr = _WorkflowLogTee(sys.stderr)


def _tool_progress_callback(name: str, preview: str, args: Mapping[str, Any] | None = None) -> None:
    activity_limit = _positive_int_config("activity_preview_chars", 420)
    if name == "_thinking":
        _record_activity("assistant-plan", _single_line(preview, activity_limit))
        return
    arguments = dict(args or {})
    payload = dict(_CURRENT_AGENT_ACTIVITY_DETAILS)
    payload.update(
        {
            "tool": name,
            "args_preview": _single_line(json.dumps(arguments, ensure_ascii=False), activity_limit + 40) if arguments else "",
        }
    )
    _record_activity(
        "tool-start",
        _single_line(preview or name, activity_limit),
        **payload,
    )


def _step_callback(iteration: int, previous_tools: list[str]) -> None:
    label = f"API call #{iteration}"
    if previous_tools:
        label += f" after {', '.join(previous_tools[:4])}"
    payload = dict(_CURRENT_AGENT_ACTIVITY_DETAILS)
    payload.update(
        {
            "iteration": iteration,
            "previous_tools": list(previous_tools or []),
        }
    )
    _record_activity(
        "api-call",
        label,
        **payload,
    )


def _held_lock_count(owner_id: str) -> int:
    if not owner_id:
        return 0
    return len([lock for lock in list_file_locks() if str(lock.get("owner_id", "") or "") == owner_id])


def _workflow_startup_guidance(workflow_kind: str, workflow_command: str) -> str:
    workflow_kind = workflow_kind.strip().lower()
    guidance_map = {
        "prove": (
            "autonomous proving session",
            "Load the native proving contract from the active skill/spec, begin with `lean_capabilities` and `lean_inspect`, use `lean_search` before guessing, and use `lean_worker_dispatch` only when the route recommends it; the live queue, route decision, and verification gate below are the state for this turn.",
        ),
        "review": (
            "proof review session",
            "Use the native review/checkpoint contract from the active skill/spec and the live Lean state below.",
        ),
        "checkpoint": (
            "proof checkpoint session",
            "Use the native review/checkpoint contract from the active skill/spec and the live Lean state below.",
        ),
        "refactor": (
            "proof refactor session",
            "Use the native refactor/golf contract from the active skill/spec and the live Lean state below.",
        ),
        "golf": (
            "proof golfing session",
            "Use the native refactor/golf contract from the active skill/spec and the live Lean state below.",
        ),
        "draft": (
            "declaration drafting session",
            "Use the native drafting/formalization contract from the active skill/spec and the live Lean state below.",
        ),
        "formalize": (
            "autonomous formalization session",
            "Load the native formalization contract from the active skill/spec, begin with `lean_capabilities` and `lean_inspect`, use `lean_search` before redrafting blindly, and use `lean_worker_dispatch` only when the route recommends it; the live queue, route decision, and verification gate below are the state for this turn.",
        ),
    }
    label, detail = guidance_map.get(
        workflow_kind,
        (
            "managed Lean workflow session",
            "Use the active native workflow spec plus the live state below.",
        ),
    )
    guidance = (
        f"Begin the requested {label} now.\n\n"
        f"Workflow request: {workflow_command or '[missing workflow command]'}\n"
        f"Execution guidance: {detail}"
    )
    if _swarm_enabled():
        agent_count = _parallel_agents()
        guidance += (
            "\n\n"
            f"User-approved swarm mode is enabled for this workflow ({agent_count} agents total).\n"
            "You may use delegate_task because the user explicitly requested multi-agent execution.\n"
            "If you delegate, assign concrete Lean goals, avoid duplicate file ownership, and keep one verifier path focused on final compilation."
        )
    return guidance


def _print_runner_help() -> None:
    print("epflemma-native session commands:")
    print("  /status                Show workflow, checkpoint, and compaction status")
    print("  /status <agent> [N]    Show one workflow agent and its latest N events")
    print("  /proof-state           Show the latest runner-refreshed Lean proof state")
    print("  /diagnostics           Show current Lean diagnostics for the active file")
    print("  /goals                 Show current Lean goals for the active target")
    print("  /swarm [agent] [N]     List workflow agents or inspect one directly")
    print("  /history               List persisted workflow checkpoints")
    print("  /checkpoint [note]     Save a manual workflow checkpoint")
    print("  /resume-plan [N]       Reload the structured plan from checkpoint N")
    print("  /rollback <N>          Restore files and workflow state from checkpoint N")
    print("  /compact               Force managed-session compaction now")
    print("  /exit                  Leave the managed session")
    print("  Ctrl+C                 Interrupt the active agent turn and return here")


def _all_checkpoint_entries_latest_first() -> list[dict[str, Any]]:
    entries = _load_workflow_index()
    return list(reversed(entries))


def _resolve_checkpoint_ref(ref: str) -> dict[str, Any] | None:
    entries = _all_checkpoint_entries_latest_first()
    if not ref:
        return entries[0] if entries else None
    if ref.isdigit():
        idx = int(ref)
        if 1 <= idx <= len(entries):
            return entries[idx - 1]
        return None
    for entry in entries:
        checkpoint_id = str(entry.get("checkpoint_id", "") or "")
        if checkpoint_id.startswith(ref):
            return entry
    return None


def _discover_lean_mcp_tool_names() -> dict[str, str]:
    capability = probe_capabilities(_project_root()).to_dict()
    mcp_tools = dict(capability.get("mcp_tools", {}) or {})
    return {
        "diagnostics": str(mcp_tools.get("diagnostics", "") or ""),
        "goals": str(mcp_tools.get("goals", "") or ""),
    }


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def _collect_message_text(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(_message_text(message.get("content")) for message in messages if message)


def _snapshot_metadata() -> dict[str, Any]:
    return {
        "workflow_kind": _workflow_kind(),
        "workflow_command": _read_native_env("WORKFLOW_COMMAND", "[unset]"),
        "project_root": _project_root(),
        "model": _read_native_env("MODEL"),
    }


def _extract_active_files(text: str) -> list[str]:
    seen: list[str] = []
    for match in re.findall(r"[\w./-]+\.lean\b", text or ""):
        normalized = match.strip()
        if normalized.startswith("a//") or normalized.startswith("b//"):
            normalized = normalized[2:]
        project_root = _project_root()
        try:
            path = Path(normalized).expanduser()
            if not path.is_absolute() and project_root:
                candidate = (Path(project_root) / path).resolve()
                if candidate.is_file():
                    normalized = str(candidate.relative_to(Path(project_root).resolve()))
            elif path.is_absolute() and path.is_file() and project_root:
                try:
                    normalized = str(path.resolve().relative_to(Path(project_root).resolve()))
                except Exception:
                    normalized = str(path.resolve())
        except Exception:
            normalized = match.strip()
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen[:8]


def _extract_target_symbol(text: str) -> str:
    patterns = [
        r"\btheorem\s+([A-Za-z_][A-Za-z0-9_']*)",
        r"\blemma\s+([A-Za-z_][A-Za-z0-9_']*)",
        r"\bdef\s+([A-Za-z_][A-Za-z0-9_']*)",
    ]
    combined = str(text or "")
    for pattern in patterns:
        match = re.search(pattern, combined)
        if match:
            return match.group(1)
    return ""


def _extract_diagnostics_summary(messages: list[dict[str, Any]]) -> str:
    snippets: list[str] = []
    for message in messages[-12:]:
        content = _message_text(message.get("content")).strip()
        if not content:
            continue
        lowered = content.lower()
        if any(token in lowered for token in ("error", "warning", "sorry", "no errors found", "diagnostic")):
            snippets.append(content[:240])
    return "\n".join(snippets[:4])


def _extract_blocker_summary(text: str) -> str:
    lowered = text.lower()
    blocker_tokens = [
        "blocked",
        "blocker",
        "stuck",
        "cannot proceed",
        "can't proceed",
        "unable to",
        "failed to",
    ]
    if not any(token in lowered for token in blocker_tokens):
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines[-8:]):
        lowered_line = line.lower()
        if lowered_line.startswith("## "):
            continue
        if "no blocker declared" in lowered_line or "all blockers resolved" in lowered_line:
            continue
        if any(token in lowered_line for token in blocker_tokens):
            return line[:280]
    return ""


def _normalize_blocker_summary(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    lowered = normalized.lower()
    cleared_tokens = (
        "all blockers resolved",
        "none. all blockers resolved",
        "no blocker declared",
        "no blockers remain",
    )
    if any(token in lowered for token in cleared_tokens):
        return ""
    return normalized


def _extract_next_steps(summary_text: str) -> str:
    lines = [line.strip("- ").strip() for line in summary_text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if line.lower() == "## next steps" and idx + 1 < len(lines):
            return lines[idx + 1][:320]
    return ""


def _extract_recent_build_status(history: list[dict[str, Any]]) -> str:
    recent_text = _collect_message_text(history[-12:]).lower()
    if "build completed successfully" in recent_text or "lake build succeeded" in recent_text:
        return "lake build succeeded"
    if "no errors found" in recent_text:
        return "lean diagnostics clean"
    if "error" in recent_text and "build" in recent_text:
        return "build reported errors"
    return "unknown"


def _resolve_active_file(history: list[dict[str, Any]], checkpoint_state: Mapping[str, Any] | None = None) -> str:
    configured_active_file = _read_native_env("ACTIVE_FILE")
    if configured_active_file:
        configured_path = Path(configured_active_file)
        if configured_path.is_file():
            return str(configured_path.resolve())
        candidate = Path(_project_root()) / configured_active_file
        if candidate.is_file():
            return str(candidate.resolve())

    workflow_command = _read_native_env("WORKFLOW_COMMAND")
    command_files = _extract_active_files(workflow_command)
    if command_files:
        candidate = Path(_project_root()) / command_files[0]
        if candidate.is_file():
            return str(candidate)

    current = (checkpoint_state or {}).get("current") or {}
    for file_name in current.get("active_files") or []:
        candidate = Path(_project_root()) / str(file_name)
        if candidate.is_file():
            return str(candidate)

    recent_text = _collect_message_text(history[-16:])
    for file_name in _extract_active_files(recent_text):
        direct = Path(file_name)
        if direct.is_file():
            return str(direct)
        candidate = Path(_project_root()) / file_name
        if candidate.is_file():
            return str(candidate)

    return ""


def _resolve_target_symbol(history: list[dict[str, Any]], checkpoint_state: Mapping[str, Any] | None = None) -> str:
    current = (checkpoint_state or {}).get("current") or {}
    target = str(current.get("target_symbol", "") or "").strip()
    if target:
        return target
    return _extract_target_symbol(_read_native_env("WORKFLOW_COMMAND"))


def _find_symbol_line(active_file: str, target_symbol: str) -> int | None:
    if not active_file or not target_symbol:
        return None
    try:
        lines = Path(active_file).read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    pattern = re.compile(rf"\b(?:theorem|lemma|def)\s+{re.escape(target_symbol)}\b")
    for idx, line in enumerate(lines, start=1):
        if pattern.search(line):
            return idx
    return None


def _strip_lean_comments_and_strings(text: str) -> str:
    """Remove Lean comments and string literals before token inspection."""
    out: list[str] = []
    i = 0
    n = len(text)
    block_depth = 0
    in_string = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if block_depth > 0:
            if ch == "/" and nxt == "-":
                block_depth += 1
                i += 2
                continue
            if ch == "-" and nxt == "/":
                block_depth -= 1
                i += 2
                continue
            if ch == "\n":
                out.append("\n")
            i += 1
            continue

        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue

        if ch == "/" and nxt == "-":
            block_depth = 1
            i += 2
            continue

        if ch == '"':
            in_string = True
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _count_sorries(active_file: str) -> int | None:
    if not active_file:
        return None
    try:
        text = Path(active_file).read_text(encoding="utf-8")
    except Exception:
        return None
    sanitized = _strip_lean_comments_and_strings(text)
    return len(re.findall(r"\bsorry\b", sanitized))


def _project_lean_files(project_root: str) -> list[Path]:
    root = Path(project_root)
    if not root.is_dir():
        return []
    paths: list[Path] = []
    for path in root.rglob("*.lean"):
        if any(part in PROJECT_SCAN_SKIP_DIRS for part in path.parts):
            continue
        paths.append(path)
    return sorted(paths)


def _count_project_sorries(project_root: str) -> tuple[int | None, list[str]]:
    if not project_root:
        return None, []
    total = 0
    files: list[str] = []
    try:
        for path in _project_lean_files(project_root):
            count = _count_sorries(str(path))
            if not isinstance(count, int) or count <= 0:
                continue
            total += count
            try:
                label = str(path.resolve().relative_to(Path(project_root).resolve()))
            except Exception:
                label = str(path)
            files.append(f"{label} ({count})")
    except Exception:
        return None, []
    return total, files[:8]


def _failed_attempt_history_limit() -> int:
    raw = _read_native_env("FAILED_ATTEMPT_HISTORY", "10")
    try:
        return max(1, int(raw))
    except ValueError:
        return 10


def _failed_attempt_reasoning_threshold() -> int:
    raw = _read_native_env("FAILED_ATTEMPT_REASONING_THRESHOLD", "5")
    try:
        return max(1, int(raw))
    except ValueError:
        return 5


def _failed_attempt_entry_limit() -> int:
    return max(2, _failed_attempt_history_limit() + 1)


def _declaration_queue_scope() -> str:
    active_file = _read_native_env("ACTIVE_FILE", "")
    return "file" if active_file else "project"


def _declaration_line_index(active_file: str) -> list[dict[str, Any]]:
    if not active_file:
        return []
    path = Path(active_file)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    entries: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^\s*(?:@[A-Za-z0-9_.]+\s+)*(theorem|lemma|example|def|instance|class|structure)\s+([A-Za-z0-9_'.-]+)?"
    )
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        decl_kind = match.group(1)
        decl_name = (match.group(2) or "").strip()
        if not decl_name:
            decl_name = f"[anonymous {decl_kind} @ line {line_number}]"
        entries.append(
            {
                "name": decl_name,
                "kind": decl_kind,
                "line": line_number,
            }
        )

    if not entries:
        return []

    for idx, entry in enumerate(entries):
        start = int(entry["line"])
        end = len(lines)
        if idx + 1 < len(entries):
            end = int(entries[idx + 1]["line"]) - 1
        region = "\n".join(lines[start - 1:end]).strip()
        entry["end_line"] = end
        entry["text"] = region
        entry["has_sorry"] = bool(re.search(r"\bsorry\b", _strip_lean_comments_and_strings(region)))
    return entries


def _find_declaration_entry(active_file: str, label: str) -> dict[str, Any] | None:
    wanted = str(label or "").strip()
    if not active_file or not wanted:
        return None
    for entry in _declaration_line_index(active_file):
        if str(entry.get("name", "") or "").strip() == wanted:
            return entry
    return None


def _declaration_prefix_text(active_file: str, label: str, *, max_lines: int = 160) -> str:
    entry = _find_declaration_entry(active_file, label)
    if not entry:
        return ""
    cutoff = int(entry.get("end_line", 0) or 0)
    if cutoff <= 0:
        return ""
    path = Path(active_file)
    try:
        all_lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    start = 1
    end = min(cutoff, len(all_lines))
    text = "\n".join(all_lines[:end]).strip()
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[-max_lines:])
        start = end - max_lines + 1
    return f"Current file prefix ending at `{label}` ({start}-{end}):\n{text}"


def _declaration_slice_text(active_file: str, label: str, *, max_lines: int = 40) -> str:
    entry = _find_declaration_entry(active_file, label)
    if not entry:
        return ""
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or 0)
    text = str(entry.get("text", "") or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines]) + "\n-- [truncated declaration slice]"
    return f"Assigned declaration slice ({start}-{end}):\n{text}"


def _nearest_declaration_name(active_file: str, line_number: int | None) -> str:
    if not active_file or not isinstance(line_number, int) or line_number <= 0:
        return ""
    entries = _declaration_line_index(active_file)
    current = ""
    for entry in entries:
        if int(entry.get("line", 0) or 0) > line_number:
            break
        current = str(entry.get("name", "") or "")
    return current


def _extract_diagnostic_line_numbers(text: str) -> list[int]:
    values: list[int] = []
    patterns = (
        r":(\d+):\d+",
        r"\bline\s+(\d+)\b",
        r"\((\d+),\s*\d+\)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            try:
                value = int(match.group(1))
            except Exception:
                continue
            if value > 0 and value not in values:
                values.append(value)
    return values


def _is_anonymous_declaration_label(label: str) -> bool:
    normalized = str(label or "").strip().lower()
    return normalized.startswith("[anonymous ")


def _declaration_name_safe_for_diagnostic_match(name: str) -> bool:
    normalized = str(name or "").strip()
    if not normalized or _is_anonymous_declaration_label(normalized):
        return False
    if len(normalized) >= 4:
        return True
    return bool(re.search(r"[^A-Za-z]", normalized))


def _declaration_work_queue(
    active_file: str,
    issue_text: str,
    *,
    project_root: str = "",
    scope: str = "",
) -> list[dict[str, Any]]:
    requested_scope = scope or _declaration_queue_scope()
    queue: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    diagnostics_active = _diagnostics_indicate_failure(issue_text)
    diagnostic_lines = _extract_diagnostic_line_numbers(issue_text)

    def _append(file_path: str, label: str, reasons: list[str], *, kind: str = "") -> None:
        key = (file_path, label)
        normalized_reasons = [reason for reason in reasons if reason]
        if key in seen:
            for item in queue:
                if item["file"] == file_path and item["label"] == label:
                    merged = list(item.get("reasons", []) or [])
                    for reason in normalized_reasons:
                        if reason not in merged:
                            merged.append(reason)
                    item["reasons"] = merged
                    return
            return
        seen.add(key)
        queue.append(
            {
                "file": file_path,
                "label": label,
                "kind": kind,
                "reasons": normalized_reasons,
            }
        )

    if requested_scope == "file" and active_file:
        for entry in _declaration_line_index(active_file):
            reasons: list[str] = []
            if entry.get("has_sorry"):
                reasons.append("contains sorry")
            name = str(entry.get("name", "") or "")
            line_number = int(entry.get("line", 0) or 0)
            anonymous = _is_anonymous_declaration_label(name)
            if diagnostics_active and _declaration_name_safe_for_diagnostic_match(name):
                if re.search(rf"\b{re.escape(name)}\b", issue_text or ""):
                    reasons.append("referenced in diagnostics")
            if diagnostics_active and line_number and line_number in diagnostic_lines:
                reasons.append(f"diagnostic near line {line_number}")
            if anonymous and reasons and not entry.get("has_sorry") and not any(
                reason.startswith("diagnostic near line ") for reason in reasons
            ):
                continue
            if reasons:
                _append(
                    active_file,
                    name,
                    reasons,
                    kind=str(entry.get("kind", "") or ""),
                )
        if not queue and diagnostics_active:
            fallback = _nearest_declaration_name(active_file, next(iter(_extract_diagnostic_line_numbers(issue_text)), None))
            _append(active_file, fallback or "[file-level blocker]", ["diagnostics unresolved"])
        return queue

    root = project_root or _project_root()
    for path in _project_lean_files(root):
        count = _count_sorries(str(path))
        if not isinstance(count, int) or count <= 0:
            continue
        try:
            label = str(path.resolve().relative_to(Path(root).resolve()))
        except Exception:
            label = str(path)
        _append(str(path.resolve()), label, [f"{count} sorry placeholder(s)"])
    if not queue and active_file and diagnostics_active:
        try:
            active_label = str(Path(active_file).resolve().relative_to(Path(root).resolve()))
        except Exception:
            active_label = active_file
        _append(active_file, active_label, ["diagnostics unresolved"])
    return queue


def _format_declaration_queue(queue: list[dict[str, Any]], *, limit: int = 8) -> str:
    if not queue:
        return "[none]"
    lines: list[str] = []
    for item in queue[:limit]:
        label = str(item.get("label", "") or "[unnamed]")
        reasons = ", ".join(item.get("reasons", []) or [])
        file_path = str(item.get("file", "") or "")
        try:
            file_label = str(Path(file_path).resolve().relative_to(Path(_project_root()).resolve())) if file_path else ""
        except Exception:
            file_label = file_path
        if file_label and label != file_label:
            lines.append(f"- {label} [{file_label}] — {reasons or 'pending'}")
        else:
            lines.append(f"- {label} — {reasons or 'pending'}")
    remaining = len(queue) - min(len(queue), limit)
    if remaining > 0:
        lines.append(f"- ... plus {remaining} more pending item(s)")
    return "\n".join(lines)


def _current_queue_item(queue: list[dict[str, Any]], active_file: str) -> dict[str, Any] | None:
    if not queue:
        return None
    if not active_file:
        return dict(queue[0])
    for item in queue:
        label = str(item.get("label", "") or "")
        reasons = list(item.get("reasons", []) or [])
        if _find_declaration_entry(active_file, label) and "contains sorry" in reasons:
            return dict(item)
    for item in queue:
        label = str(item.get("label", "") or "")
        if _find_declaration_entry(active_file, label):
            return dict(item)
    return dict(queue[0])


def _current_queue_status(live_state: Mapping[str, Any]) -> str:
    blocker = str(live_state.get("current_blocker", "") or "").strip()
    if blocker:
        return "blocked"
    item = dict(live_state.get("current_queue_item") or {})
    reasons = ", ".join(item.get("reasons", []) or []).strip()
    if "sorry" in reasons:
        return "pending"
    return "in-progress"


def _attempt_proof_shape(live_state: Mapping[str, Any] | None) -> str:
    item = dict((live_state or {}).get("current_queue_item") or {})
    active_file = str((live_state or {}).get("active_file", "") or "")
    label = str(item.get("label", "") or (live_state or {}).get("target_symbol", "") or "").strip()
    slice_text = _declaration_slice_text(active_file, label) if active_file and label else ""
    if not slice_text:
        slice_text = str((live_state or {}).get("current_queue_item_slice", "") or "").strip()
    if not slice_text:
        return "[no attempted proof shape recorded]"
    _, _, body = slice_text.partition(":\n")
    snippet = body.strip() or slice_text
    lines = [line.rstrip() for line in snippet.splitlines() if line.strip()]
    if len(lines) > 6:
        lines = lines[:6]
    text = " ".join(lines)
    return _single_line(text, 240)


def _prepare_queue_assignment_state(
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None,
) -> None:
    current = dict(live_state or {})
    item = dict(current.get("current_queue_item") or {})
    label = str(item.get("label", "") or current.get("target_symbol", "") or "").strip()
    active_file = str(current.get("active_file", "") or current.get("active_file_label", "") or "").strip()
    slice_text = str(current.get("current_queue_item_slice", "") or "").strip()
    autonomy_state["current_queue_assignment"] = {
        "target_symbol": label,
        "active_file": active_file,
        "slice": slice_text,
    }


def _queue_assignment_identity(live_state: Mapping[str, Any] | None) -> tuple[str, str]:
    current = dict(live_state or {})
    item = dict(current.get("current_queue_item") or {})
    label = str(item.get("label", "") or current.get("target_symbol", "") or "").strip()
    active_file = str(current.get("active_file", "") or current.get("active_file_label", "") or "").strip()
    return label, active_file


def _display_file_label(live_state: Mapping[str, Any] | None) -> str:
    current = dict(live_state or {})
    return str(current.get("active_file_label", "") or current.get("active_file", "") or "").strip()


def _queue_assignment_transition(
    autonomy_state: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
) -> dict[str, str] | None:
    baseline = dict(autonomy_state.get("current_queue_assignment") or {})
    previous_target = str(baseline.get("target_symbol", "") or "").strip()
    previous_file = str(baseline.get("active_file", "") or "").strip()
    current_target, current_file = _queue_assignment_identity(live_state)
    if not previous_target or not previous_file or not current_target or not current_file:
        return None
    if previous_target == current_target and _same_active_file(previous_file, current_file):
        return None
    return {
        "previous_target": previous_target,
        "previous_file": previous_file,
        "current_target": current_target,
        "current_file": current_file,
    }


def _attempt_proof_shape_from_delta(
    autonomy_state: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
) -> str:
    current = dict(live_state or {})
    current_slice = str(current.get("current_queue_item_slice", "") or "").strip()
    baseline = dict(autonomy_state.get("current_queue_assignment") or {})
    previous_slice = str(baseline.get("slice", "") or "").strip()
    if previous_slice and current_slice and previous_slice != current_slice:
        prev_lines = previous_slice.splitlines()
        curr_lines = current_slice.splitlines()
        diff_lines = [
            line
            for line in unified_diff(prev_lines, curr_lines, n=0, lineterm="")
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        if diff_lines:
            return _single_line(" ".join(diff_lines[:8]), 240)
    return _attempt_proof_shape(live_state)


def _prune_failed_attempt_entries(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not attempts:
        return []
    limit = _failed_attempt_entry_limit()
    keep_counts: dict[tuple[str, str], int] = {}
    keep_indices: set[int] = set()
    for idx in range(len(attempts) - 1, -1, -1):
        attempt = dict(attempts[idx] or {})
        key = (
            str(attempt.get("target_symbol", "") or "").strip(),
            str(attempt.get("active_file", "") or "").strip(),
        )
        if not key[0] or not key[1]:
            keep_indices.add(idx)
            continue
        count = int(keep_counts.get(key, 0) or 0)
        if count >= limit:
            continue
        keep_counts[key] = count + 1
        keep_indices.add(idx)
    return [dict(attempts[idx]) for idx in range(len(attempts)) if idx in keep_indices]


def _clear_failed_attempts_for_theorem(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> None:
    attempts = [dict(item) for item in autonomy_state.get("failed_attempts", []) if isinstance(item, Mapping)]
    if not attempts:
        return
    filtered = [
        attempt
        for attempt in attempts
        if not (
            str(attempt.get("target_symbol", "") or "").strip() == str(target_symbol).strip()
            and _same_active_file(str(attempt.get("active_file", "") or ""), active_file)
        )
    ]
    if filtered:
        autonomy_state["failed_attempts"] = filtered
    else:
        autonomy_state.pop("failed_attempts", None)


def _refresh_failed_attempt_baseline(
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None,
) -> None:
    current = dict(live_state or {})
    item = dict(current.get("current_queue_item") or {})
    target_symbol = str(item.get("label", "") or current.get("target_symbol", "") or "").strip()
    active_file = str(current.get("active_file", "") or current.get("active_file_label", "") or "").strip()
    if not target_symbol or not active_file:
        return
    baseline = dict(autonomy_state.get("current_queue_assignment") or {})
    baseline["target_symbol"] = target_symbol
    baseline["active_file"] = active_file
    baseline["slice"] = str(current.get("current_queue_item_slice", "") or "").strip()
    autonomy_state["current_queue_assignment"] = baseline


def _queue_assignment_block(
    live_state: Mapping[str, Any],
    autonomy_state: Mapping[str, Any] | None = None,
) -> str:
    item = dict(live_state.get("current_queue_item") or {})
    if not item:
        return ""
    label = str(item.get("label", "") or "[unknown]")
    reasons = ", ".join(item.get("reasons", []) or []) or "pending"
    active_file = str(live_state.get("active_file", "") or live_state.get("active_file_label", "") or "")
    file_label = _display_file_label(live_state) or active_file or "[unknown]"
    current_status = _current_queue_status(live_state)
    current_blocker = str(live_state.get("current_blocker", "") or reasons or "[none]").strip()
    route_decision = dict(live_state.get("route_decision", {}) or {})
    recommended_worker = str(
        item.get("recommended_worker", "")
        or route_decision.get("recommended_worker", "")
        or ""
    ).strip()
    search_hints = [str(value) for value in item.get("search_hints", []) or [] if str(value).strip()]
    parts = [
        "Assigned queue item:",
        f"- declaration: {label}",
        f"- file: {file_label}",
        f"- exact tool path: {active_file or '[unknown]'}",
        f"- current status: {current_status}",
        f"- current blocker: {current_blocker}",
        "",
        "Focus:",
        f"- solve `{label}`",
        "- local helper lemmas or intermediate facts are allowed if they directly help this theorem",
        "- do not start solving unrelated later queue items",
        "- after a meaningful edit, stop and let the manager re-check the queue",
        "- for this file-scoped theorem turn, the only acceptable final verification step is `lean_verify(mode=file_exact)` for the active file",
    ]
    verification_hint = _queue_item_verification_hint(active_file)
    if verification_hint:
        parts.extend(["", "Verification for this queue item:", verification_hint])
    prefix_text = str(live_state.get("current_queue_item_prefix", "") or "").strip()
    if prefix_text:
        parts.extend(["", prefix_text])
    slice_text = str(live_state.get("current_queue_item_slice", "") or "").strip()
    if slice_text and not prefix_text:
        parts.extend(["", slice_text])
    failed = _recent_failed_attempts_summary(dict(autonomy_state or {}), live_state)
    if failed:
        parts.extend(["", failed])
    if search_hints:
        parts.extend(["", "Search hints:", f"- {', '.join(search_hints[:4])}"])
    if recommended_worker:
        parts.extend(
            [
                "",
                "Recommended worker:",
                f"- `{recommended_worker}`",
                f"- dispatch with `lean_worker_dispatch` if the blocker persists after the next focused attempt",
            ]
        )
    if bool(live_state.get("search_exhausted")):
        parts.extend(
            [
                "",
                "Search exhaustion:",
                "- repeated search attempts have already failed for this theorem",
                "- do not call `lean_search` again in this turn unless you are changing the query strategy materially",
                "- your next move should be an edit, `lean_verify`, `lean_worker_dispatch`, or a concrete blocker report",
            ]
        )
    parts.extend(["", "Task:", f"Repair `{label}` from its current state."])
    return "\n".join(parts)


def _remember_failed_attempt(
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None,
    *,
    cycle_number: int,
) -> None:
    if not live_state:
        return
    item = dict(live_state.get("current_queue_item") or {})
    target_symbol = str(item.get("label", "") or live_state.get("target_symbol", "") or "").strip()
    active_file = str(live_state.get("active_file", "") or live_state.get("active_file_label", "") or "").strip()
    if not target_symbol or not active_file:
        return
    reason = str(
        live_state.get("blocker_summary", "")
        or live_state.get("diagnostics", "")
        or live_state.get("goals", "")
        or live_state.get("build_status", "")
        or ""
    ).strip()
    if not reason:
        return
    attempts = [dict(item) for item in autonomy_state.get("failed_attempts", []) if isinstance(item, Mapping)]
    scoped = _scoped_failed_attempt_entries(
        autonomy_state,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    entry = {
        "attempt": _failed_attempt_count_for_theorem(
            autonomy_state,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        + 1,
        "cycle": cycle_number,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "proof_shape": _attempt_proof_shape_from_delta(autonomy_state, live_state),
        "reason": _single_line(reason, 240),
    }
    attempts.append(entry)
    autonomy_state["failed_attempts"] = _prune_failed_attempt_entries(attempts)
    _refresh_failed_attempt_baseline(autonomy_state, live_state)


def _record_theorem_outcome(autonomy_state: dict[str, Any], outcome: Mapping[str, Any]) -> None:
    target_symbol = str(outcome.get("target_symbol", "") or "").strip()
    active_file = str(outcome.get("active_file", "") or "").strip()
    if not target_symbol or not active_file:
        return
    outcome_map = {
        str(key): dict(value)
        for key, value in dict(autonomy_state.get("theorem_outcomes", {}) or {}).items()
        if isinstance(value, Mapping)
    }
    outcome_map[f"{active_file}::{target_symbol}"] = {
        "target_symbol": target_symbol,
        "active_file": active_file,
        "status": str(outcome.get("status", "") or "unknown"),
        "note": str(outcome.get("note", "") or ""),
        "build_status": str(outcome.get("build_status", "") or ""),
    }
    autonomy_state["theorem_outcomes"] = outcome_map


def _remember_transition_failed_attempt(
    autonomy_state: dict[str, Any],
    outcome: Mapping[str, Any],
) -> None:
    current = dict(outcome or {})
    status = str(current.get("status", "") or "").strip().lower()
    if not status or status == "solved":
        return
    target_symbol = str(current.get("target_symbol", "") or "").strip()
    active_file = str(current.get("active_file", "") or "").strip()
    if not target_symbol or not active_file:
        return

    baseline = dict(autonomy_state.get("current_queue_assignment") or {})
    slice_text = str(baseline.get("slice", "") or "").strip()
    _, _, body = slice_text.partition(":\n")
    snippet = body.strip() or slice_text or "[no attempted proof shape recorded]"
    lines = [line.rstrip() for line in snippet.splitlines() if line.strip()]
    if len(lines) > 6:
        lines = lines[:6]
    proof_shape = _single_line(" ".join(lines) if lines else snippet, 240)

    reason = _single_line(
        str(current.get("note", "") or current.get("build_status", "") or status),
        240,
    )
    attempts = [dict(item) for item in autonomy_state.get("failed_attempts", []) if isinstance(item, Mapping)]
    attempts.append(
        {
            "attempt": _failed_attempt_count_for_theorem(
                autonomy_state,
                target_symbol=target_symbol,
                active_file=active_file,
            )
            + 1,
            "cycle": 0,
            "target_symbol": target_symbol,
            "active_file": active_file,
            "proof_shape": proof_shape or "[no attempted proof shape recorded]",
            "reason": reason,
        }
    )
    autonomy_state["failed_attempts"] = _prune_failed_attempt_entries(attempts)


def _has_unresolved_theorem_outcomes(autonomy_state: Mapping[str, Any]) -> bool:
    outcome_map = dict(autonomy_state.get("theorem_outcomes", {}) or {})
    for value in outcome_map.values():
        if not isinstance(value, Mapping):
            continue
        status = str(value.get("status", "") or "").strip().lower()
        if status and status != "solved":
            return True
    return False


def _recent_failed_attempts_summary(
    autonomy_state: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
) -> str:
    item = dict((live_state or {}).get("current_queue_item") or {})
    target_symbol = str(item.get("label", "") or (live_state or {}).get("target_symbol", "") or "").strip()
    active_file = str((live_state or {}).get("active_file", "") or (live_state or {}).get("active_file_label", "") or "").strip()
    if not target_symbol or not active_file:
        return ""
    scoped = _scoped_failed_attempt_entries(
        autonomy_state,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    previous_attempts = scoped[:-1]
    if not previous_attempts:
        return ""
    lines = ["PREVIOUS ATTEMPTS:"]
    for item in previous_attempts[-_failed_attempt_history_limit():]:
        lines.append(f"- attempt: {item.get('attempt', '?')}")
        lines.append(f"  proof shape: {item.get('proof_shape', '[no proof shape recorded]')}")
        lines.append(f"  why it failed: {item.get('reason', '[no reason recorded]')}")
    return "\n".join(lines)


def _latest_failed_attempt_for_theorem(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any] | None:
    scoped = _scoped_failed_attempt_entries(
        autonomy_state,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    if not scoped:
        return None
    return scoped[-1]


def _theorem_is_still_pending(live_state: Mapping[str, Any] | None, target_symbol: str) -> bool:
    target = str(target_symbol or "").strip()
    if not target:
        return False
    current = dict(live_state or {})
    item = dict(current.get("current_queue_item") or {})
    if str(item.get("label", "") or "").strip() == target:
        return True
    summary = str(current.get("declaration_queue_summary", "") or "")
    return target in summary


def _summarize_theorem_transition_outcome(
    autonomy_state: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
    history: list[dict[str, Any]],
) -> dict[str, str]:
    transition = _queue_assignment_transition(autonomy_state, live_state) or {}
    previous_target = str(transition.get("previous_target", "") or "").strip()
    previous_file = str(transition.get("previous_file", "") or "").strip()
    recent_text = _collect_message_text(history[-12:])
    lowered = recent_text.lower()
    latest_failed_attempt = _latest_failed_attempt_for_theorem(
        autonomy_state,
        target_symbol=previous_target,
        active_file=previous_file,
    )
    previous_reason = _single_line(str((latest_failed_attempt or {}).get("reason", "") or ""), 240)
    blocker = _single_line(
        str((live_state or {}).get("current_blocker", "") or (live_state or {}).get("blocker_summary", "") or ""),
        240,
    )
    pending = _theorem_is_still_pending(live_state, previous_target)
    if pending and ("reverted to `sorry`" in recent_text or "reverted to sorry" in lowered):
        status = "reverted-to-sorry"
        note = previous_reason or blocker or f"{previous_target} remains pending after being reverted to `sorry`."
    elif pending and (previous_reason or blocker):
        status = "blocked"
        note = previous_reason or blocker
    elif pending:
        status = "skipped"
        note = f"{previous_target} remains pending in the declaration queue."
    else:
        status = "solved"
        note = f"{previous_target} no longer appears in the pending declaration queue."
    return {
        "target_symbol": previous_target,
        "active_file": previous_file,
        "status": status,
        "note": note,
        "build_status": _single_line(str((live_state or {}).get("build_status", "") or "unknown"), 220),
    }


def _workflow_transition_snapshot(
    compaction_state: Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None,
) -> str:
    snapshot_text = str((compaction_state or {}).get("snapshot_text", "") or "").strip()
    if snapshot_text:
        return snapshot_text
    current = dict(live_state or {})
    return "\n".join(
        [
            MANAGED_SNAPSHOT_PREFIX,
            "",
            f"Workflow: {_workflow_kind()}",
            f"Active file: {_display_file_label(current) or '[unknown]'}",
            f"Active file path: {str(current.get('active_file', '') or '[unknown]')}",
            "Current queue summary:",
            str(current.get("declaration_queue_summary", "") or "[none]"),
            "",
            "Latest verification/build status:",
            str(current.get("build_status", "") or "unknown"),
            "",
            "Current blocker:",
            str(current.get("current_blocker", "") or "[none]"),
        ]
    ).strip()


def _theorem_transition_handoff_message(
    outcome: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
) -> str:
    current_target, current_file = _queue_assignment_identity(live_state)
    current = dict(live_state or {})
    current_file_label = _display_file_label(current) or current_file or "[unknown]"
    return "\n".join(
        [
            "[EPFLEMMA-NATIVE THEOREM TRANSITION HANDOFF]",
            "",
            "Previous theorem outcome:",
            f"- declaration: {str(outcome.get('target_symbol', '') or '[unknown]')}",
            f"- file: {str(outcome.get('active_file', '') or '[unknown]')}",
            f"- final status: {str(outcome.get('status', '') or 'unknown')}",
            f"- note: {str(outcome.get('note', '') or '[none]')}",
            "",
            "Current queue focus:",
            f"- declaration: {current_target or '[unknown]'}",
            f"- file: {current_file_label}",
            f"- exact tool path: {current_file or '[unknown]'}",
            "",
            "Queue summary:",
            str(current.get("declaration_queue_summary", "") or "[none]"),
            "",
            "Latest verification/build status:",
            str(outcome.get("build_status", "") or str(current.get("build_status", "") or "unknown")),
        ]
    ).strip()


def _rebuild_history_for_theorem_transition(
    history: list[dict[str, Any]],
    compaction_state: Mapping[str, Any] | None,
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, str]] | tuple[None, None]:
    transition = _queue_assignment_transition(autonomy_state, live_state)
    if not transition:
        return None, None
    outcome = _summarize_theorem_transition_outcome(autonomy_state, live_state, history)
    _record_theorem_outcome(autonomy_state, outcome)
    _remember_transition_failed_attempt(autonomy_state, outcome)
    if str(outcome.get("status", "") or "").strip().lower() == "solved":
        _clear_failed_attempts_for_theorem(
            autonomy_state,
            target_symbol=str(outcome.get("target_symbol", "") or ""),
            active_file=str(outcome.get("active_file", "") or ""),
        )
    rebuilt_history = [
        {"role": "assistant", "content": _workflow_transition_snapshot(compaction_state, live_state)},
        {"role": "assistant", "content": _theorem_transition_handoff_message(outcome, live_state)},
    ]
    autonomy_state["last_theorem_outcome"] = outcome
    autonomy_state["continuation_blocked_runs"] = 0
    autonomy_state["continuation_stable_cycles"] = 0
    autonomy_state["continuation_live_state_signature"] = None
    return rebuilt_history, transition


def _queue_needs_final_file_sweep(live_state: Mapping[str, Any] | None) -> bool:
    current = dict(live_state or {})
    return (
        _single_queue_item_turn_enabled()
        and str(current.get("declaration_scope", "") or "") == "file"
        and bool(str(current.get("active_file", "") or "").strip())
        and int(current.get("declaration_queue_total", 0) or 0) == 0
        and not _live_state_is_verified(current)
    )


def _final_file_sweep_block(live_state: Mapping[str, Any]) -> str:
    active_file = str(live_state.get("active_file", "") or live_state.get("active_file_label", "") or "[unknown]")
    active_file_label = _display_file_label(live_state) or active_file
    blocker = str(live_state.get("current_blocker", "") or live_state.get("diagnostics", "") or "unknown remaining issue").strip()
    verification_hint = _queue_item_verification_hint(str(live_state.get("active_file", "") or ""))
    return "\n".join(
        [
            "Queue status:",
            "- declaration queue is empty",
            f"- file: {active_file_label}",
            f"- exact tool path: {active_file}",
            f"- current blocker: {blocker}",
            (
                f"- canonical file verification: {verification_hint}"
                if verification_hint
                else "- canonical file verification: [unknown]"
            ),
            "",
            "Final file sweep:",
            f"- inspect the full file `{active_file_label}` now",
            "- do one final whole-file pass for any remaining errors, warnings, malformed partial proofs, or missed declarations",
            "- you are no longer restricted to a single assigned theorem for this pass",
            "- if you make a meaningful edit, stop and let the manager re-check the file",
        ]
    )


def _same_queue_assignment_still_blocked(
    autonomy_state: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
) -> bool:
    baseline = dict(autonomy_state.get("current_queue_assignment") or {})
    current = dict(live_state or {})
    item = dict(current.get("current_queue_item") or {})
    baseline_target = str(baseline.get("target_symbol", "") or "").strip()
    baseline_file = str(baseline.get("active_file", "") or "").strip()
    current_target = str(item.get("label", "") or current.get("target_symbol", "") or "").strip()
    current_file = str(current.get("active_file", "") or current.get("active_file_label", "") or "").strip()
    if not baseline_target or not baseline_file:
        return False
    if baseline_target != current_target or not _same_active_file(baseline_file, current_file):
        return False
    blocker_summary = str(current.get("blocker_summary", "") or "").strip()
    diagnostics = str(current.get("diagnostics", "") or "")
    goals = str(current.get("goals", "") or "")
    build_status = str(current.get("build_status", "") or "")
    return bool(
        blocker_summary
        or _diagnostics_indicate_failure(diagnostics)
        or _goals_still_open(goals)
        or ("error" in build_status.lower())
    )


def _flatten_text_fragments(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, Mapping):
        fragments: list[str] = []
        preferred_keys = ("message", "content", "text", "severity", "line", "column", "goal", "range", "file_path")
        for key in preferred_keys:
            if key in value:
                fragments.extend(_flatten_text_fragments(value[key]))
        if fragments:
            return fragments
        for nested in value.values():
            fragments.extend(_flatten_text_fragments(nested))
        return fragments
    if isinstance(value, list):
        fragments: list[str] = []
        for item in value:
            fragments.extend(_flatten_text_fragments(item))
        return fragments
    return [str(value)]


def _summarize_tool_payload(payload: Mapping[str, Any], *, limit: int = 6) -> str:
    if payload.get("error"):
        return f"error: {payload['error']}"
    fragments = _flatten_text_fragments(payload)
    deduped: list[str] = []
    for fragment in fragments:
        if fragment and fragment not in deduped:
            deduped.append(fragment)
    return "\n".join(deduped[:limit]) if deduped else "unavailable"


def _query_live_diagnostics(active_file: str, target_symbol: str = "") -> str:
    if not active_file:
        return "No active Lean file identified."
    try:
        return lean_inspect(active_file, cwd=_project_root(), symbol=target_symbol or None).diagnostics
    except Exception as exc:
        return f"Lean diagnostics unavailable: {exc}"


def _query_live_goals(active_file: str, target_symbol: str) -> str:
    if not active_file:
        return "No active Lean file identified."
    try:
        return lean_inspect(active_file, cwd=_project_root(), symbol=target_symbol or None).goals
    except Exception as exc:
        return f"Lean goals unavailable: {exc}"


def _build_live_proof_state(
    history: list[dict[str, Any]],
    checkpoint_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    active_file = _resolve_active_file(history, checkpoint_state)
    target_symbol = _resolve_target_symbol(history, checkpoint_state)
    capability_report = probe_capabilities(_project_root()).to_dict()
    inspection = None
    if active_file:
        try:
            inspection = lean_inspect(
                active_file,
                cwd=_project_root(),
                symbol=target_symbol or None,
            )
        except Exception:
            inspection = None
    diagnostics = inspection.diagnostics if inspection else _query_live_diagnostics(active_file, target_symbol)
    goals = inspection.goals if inspection else _query_live_goals(active_file, target_symbol)
    sorry_count = inspection.sorry_count if inspection else _count_sorries(active_file)
    if inspection and inspection.capability_report:
        capability_report = dict(inspection.capability_report)
    project_sorry_count, project_sorry_files = _count_project_sorries(_project_root())
    if inspection and inspection.project_sorry_count is not None:
        project_sorry_count = inspection.project_sorry_count
    build_status = _extract_recent_build_status(history)
    recent_issue_text = _collect_message_text(history[-10:])
    blocker_summary = _normalize_blocker_summary(_extract_blocker_summary(recent_issue_text))
    declaration_scope = _declaration_queue_scope()
    queue_issue_text = str(diagnostics or "").strip()
    declaration_queue = _declaration_work_queue(
        active_file,
        queue_issue_text,
        project_root=_project_root(),
        scope=declaration_scope,
    )
    inspection_queue_items: dict[str, dict[str, Any]] = {}
    if inspection:
        for item in inspection.queue_items:
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("label", "") or "").strip()
            if label:
                inspection_queue_items[label] = dict(item)
    if inspection_queue_items:
        enriched_queue: list[dict[str, Any]] = []
        seen_labels: set[str] = set()
        for item in declaration_queue:
            merged = dict(item)
            label = str(merged.get("label", "") or "").strip()
            extra = inspection_queue_items.get(label, {})
            if extra:
                seen_labels.add(label)
                for key, value in extra.items():
                    if key not in merged or merged.get(key) in (None, "", [], {}):
                        merged[key] = value
            if not merged.get("search_hints"):
                merged["search_hints"] = [label, str(merged.get("kind", "") or "").strip()]
            if not merged.get("verification_gate"):
                merged["verification_gate"] = _canonical_file_verification_command(active_file)
            if not merged.get("blocker_signature"):
                merged["blocker_signature"] = f"{label or 'queue'}:{merged.get('line', '?')}"
            enriched_queue.append(merged)
        for label, extra in inspection_queue_items.items():
            if label not in seen_labels:
                merged = dict(extra)
                if not merged.get("search_hints"):
                    merged["search_hints"] = [label, str(merged.get("kind", "") or "").strip()]
                if not merged.get("verification_gate"):
                    merged["verification_gate"] = _canonical_file_verification_command(active_file)
                if not merged.get("blocker_signature"):
                    merged["blocker_signature"] = f"{label or 'queue'}:{merged.get('line', '?')}"
                enriched_queue.append(merged)
        declaration_queue = enriched_queue
    current_queue_item = _current_queue_item(declaration_queue, active_file)
    current_queue_label = str((current_queue_item or {}).get("label", "") or "").strip()
    queue_needs_final_file_sweep = declaration_scope == "file" and bool(active_file) and not declaration_queue
    if declaration_scope == "file" and current_queue_label:
        target_symbol = current_queue_label
    elif queue_needs_final_file_sweep:
        target_symbol = ""
    current_queue_prefix = _declaration_prefix_text(active_file, current_queue_label) if current_queue_label else ""
    current_queue_slice = _declaration_slice_text(active_file, current_queue_label) if current_queue_label else ""
    declaration_queue_summary = _format_declaration_queue(declaration_queue)
    current_blocker = blocker_summary or ", ".join((current_queue_item or {}).get("reasons", []) or [])
    active_file_label = ""
    verification_hint = _recommended_verification_command(active_file)
    if active_file:
        try:
            active_file_label = str(Path(active_file).resolve().relative_to(Path(_project_root()).resolve()))
        except Exception:
            active_file_label = active_file
    workflow_command = str(
        _read_native_env("WORKFLOW_COMMAND", "")
        or _read_text_env("OPENGAUSS_NATIVE_WORKFLOW_COMMAND", "")
    ).strip()
    empty_search_streak = recent_empty_search_streak(workflow_command=workflow_command) if workflow_command else 0
    search_exhausted = empty_search_streak >= 3
    provisional_state = {
        "active_file": active_file,
        "active_file_label": active_file_label,
        "target_symbol": target_symbol,
        "diagnostics": diagnostics,
        "goals": goals,
        "build_status": build_status,
        "declaration_scope": declaration_scope,
        "declaration_queue_total": len(declaration_queue),
        "declaration_queue_preview": list(declaration_queue[:8]),
        "declaration_queue_summary": declaration_queue_summary,
        "current_queue_item": dict(current_queue_item or {}),
        "current_queue_item_prefix": current_queue_prefix,
        "current_queue_item_slice": current_queue_slice,
        "current_blocker": current_blocker,
        "queue_needs_final_file_sweep": queue_needs_final_file_sweep,
        "sorry_count": sorry_count,
        "project_sorry_count": project_sorry_count,
        "project_sorry_files": list(project_sorry_files),
        "blocker_summary": blocker_summary,
        "verification_hint": verification_hint,
        "capability_report": capability_report,
        "recent_empty_search_streak": empty_search_streak,
        "search_exhausted": search_exhausted,
    }
    route_decision = route_workflow_step(
        _workflow_kind(),
        provisional_state,
        configured_skill=_base_active_skill(),
        cwd=_project_root(),
    ).to_dict()
    if current_queue_item and route_decision.get("recommended_worker"):
        current_queue_item = dict(current_queue_item)
        current_queue_item.setdefault(
            "recommended_worker",
            str(route_decision.get("recommended_worker", "") or ""),
        )
    degraded_summary = ", ".join(capability_report.get("degraded_reasons", []) or []) or "[none]"
    route_summary = str(route_decision.get("reason", "") or "[none]")
    route_action = str(route_decision.get("route_action", "") or "[none]")
    body = "\n".join(
        [
            LIVE_PROOF_STATE_PREFIX,
            "",
            f"Workflow: {_workflow_kind()}",
            f"Active file: {active_file_label or '[unknown]'}",
            f"Active file path: {active_file or '[unknown]'}",
            f"Target theorem: {target_symbol or ('[full-file verification sweep]' if queue_needs_final_file_sweep else '[unknown]')}",
            "",
            "Diagnostics:",
            diagnostics,
            "",
            "Goals:",
            goals,
            "",
            "Build:",
            build_status,
            "",
            f"Pending {declaration_scope} queue:",
            declaration_queue_summary,
            "",
            "Route:",
            f"{route_action} via {route_decision.get('skill_name', '[unknown]')}",
            route_summary,
            "",
            "Recommended verification path:",
            verification_hint or "`lean_inspect` first, then `lean_verify` when close to clean",
            "",
            "Search state:",
            (
                f"empty search streak: {empty_search_streak} (search exhausted for this theorem)"
                if search_exhausted
                else f"empty search streak: {empty_search_streak}"
            ),
            "",
            "Capabilities:",
            f"degraded reasons: {degraded_summary}",
            "",
            "Proof status:",
            f"sorry count: {sorry_count if sorry_count is not None else '[unknown]'}",
            f"project sorry count: {project_sorry_count if project_sorry_count is not None else '[unknown]'}",
            (
                "project files with sorry: " + ", ".join(project_sorry_files)
                if project_sorry_files
                else "project files with sorry: [none]"
            ),
        ]
    ).strip()
    live_state = {
        "active_file": active_file,
        "active_file_label": active_file_label,
        "target_symbol": target_symbol,
        "diagnostics": diagnostics,
        "goals": goals,
        "build_status": build_status,
        "declaration_scope": declaration_scope,
        "declaration_queue_total": len(declaration_queue),
        "declaration_queue_preview": list(declaration_queue[:8]),
        "declaration_queue_summary": declaration_queue_summary,
        "current_queue_item": dict(current_queue_item or {}),
        "current_queue_item_prefix": current_queue_prefix,
        "current_queue_item_slice": current_queue_slice,
        "current_blocker": current_blocker,
        "queue_needs_final_file_sweep": queue_needs_final_file_sweep,
        "sorry_count": sorry_count,
        "project_sorry_count": project_sorry_count,
        "project_sorry_files": list(project_sorry_files),
        "blocker_summary": blocker_summary,
        "verification_hint": verification_hint,
        "capability_report": capability_report,
        "route_decision": route_decision,
        "recent_empty_search_streak": empty_search_streak,
        "search_exhausted": search_exhausted,
        "message": body,
    }
    if _workflow_kind() in AUTONOMOUS_WORKFLOW_KINDS:
        live_state = _promote_live_state_to_verified(live_state)
        live_state["message"] = "\n".join(
            [
                LIVE_PROOF_STATE_PREFIX,
                "",
                f"Workflow: {_workflow_kind()}",
                f"Active file: {live_state.get('active_file_label') or '[unknown]'}",
                f"Active file path: {live_state.get('active_file') or '[unknown]'}",
                f"Target theorem: {live_state.get('target_symbol') or '[unknown]'}",
                "",
                "Diagnostics:",
                str(live_state.get("diagnostics", "") or "unavailable"),
                "",
                "Goals:",
                str(live_state.get("goals", "") or "unavailable"),
                "",
                "Build:",
                str(live_state.get("build_status", "") or "unknown"),
                "",
                f"Pending {live_state.get('declaration_scope', declaration_scope)} queue:",
                str(live_state.get("declaration_queue_summary", "") or "[none]"),
                "",
                "Route:",
                (
                    f"{dict(live_state.get('route_decision', {}) or {}).get('route_action', '[none]')} "
                    f"via {dict(live_state.get('route_decision', {}) or {}).get('skill_name', '[unknown]')}"
                ),
                str(dict(live_state.get("route_decision", {}) or {}).get("reason", "") or "[none]"),
                "",
                "Recommended verification path:",
                str(live_state.get("verification_hint", "") or "`lean_inspect` first, then `lean_verify` when close to clean"),
                "",
                "Capabilities:",
                "degraded reasons: "
                + (
                    ", ".join(dict(live_state.get("capability_report", {}) or {}).get("degraded_reasons", []) or [])
                    or "[none]"
                ),
                "",
                "Proof status:",
                f"sorry count: {live_state.get('sorry_count', '[unknown]')}",
                f"project sorry count: {live_state.get('project_sorry_count', '[unknown]')}",
                (
                    "project files with sorry: " + ", ".join(live_state.get("project_sorry_files", []) or [])
                    if live_state.get("project_sorry_files")
                    else "project files with sorry: [none]"
                ),
            ]
        ).strip()
    return live_state


def _attach_live_proof_state(user_message: str, live_state: Mapping[str, Any]) -> str:
    block = str(live_state.get("message", "") or "").strip()
    if not block:
        return user_message
    return f"{user_message}\n\n{block}".strip()


def _diagnostics_indicate_failure(diagnostics: str) -> bool:
    lowered = (diagnostics or "").lower()
    cleared_tokens = (
        "no errors found",
        "no errors",
        "without errors",
    )
    if any(token in lowered for token in cleared_tokens):
        lowered = lowered.replace("no errors found", "").replace("no errors", "").replace("without errors", "")
    failure_patterns = (
        r"\berror\b",
        r"\berrors\b",
        r"\bwarning\b",
        r"\bwarnings\b",
        r"\bsorry\b",
        r"\bunsolved\b",
        r"\bfailed\b",
        r"declaration uses sorry",
    )
    return any(re.search(pattern, lowered) for pattern in failure_patterns)


def _goals_still_open(goals: str) -> bool:
    def _structured_goals_still_open(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            lowered_value = value.lower()
            if not lowered_value or "unavailable" in lowered_value:
                return False
            cleared_tokens = (
                "no goals",
                "goals accomplished",
                "proof complete",
                "no remaining goals",
            )
            if any(token in lowered_value for token in cleared_tokens):
                return False
            return "⊢" in value or bool(re.search(r"\bgoal\b", lowered_value))
        if isinstance(value, list):
            return any(_structured_goals_still_open(item) for item in value)
        if isinstance(value, Mapping):
            if "goals" in value:
                return _structured_goals_still_open(value.get("goals"))
            if "goal" in value:
                return _structured_goals_still_open(value.get("goal"))
            if "term_goal" in value:
                return _structured_goals_still_open(value.get("term_goal"))
            return False
        return False

    lowered = (goals or "").lower()
    if not lowered or "unavailable" in lowered:
        return False
    try:
        parsed = json.loads(goals)
    except Exception:
        parsed = None
    if parsed is not None:
        return _structured_goals_still_open(parsed)
    cleared_tokens = (
        "no goals",
        "goals accomplished",
        "proof complete",
        "no remaining goals",
    )
    if any(token in lowered for token in cleared_tokens):
        return False
    return "⊢" in goals or "goal" in lowered


def _live_state_is_verified(live_state: Mapping[str, Any] | None) -> bool:
    if not live_state:
        return False
    active_file = str(live_state.get("active_file", "") or "")
    diagnostics = str(live_state.get("diagnostics", "") or "")
    goals = str(live_state.get("goals", "") or "")
    build_status = str(live_state.get("build_status", "") or "")
    declaration_scope = str(live_state.get("declaration_scope", "") or _declaration_queue_scope())
    sorry_count = live_state.get("sorry_count")
    project_sorry_count = live_state.get("project_sorry_count")
    verification_ok = live_state.get("verification_ok")

    if not active_file:
        return False
    if isinstance(sorry_count, int) and sorry_count > 0:
        return False
    if declaration_scope != "file" and isinstance(project_sorry_count, int) and project_sorry_count > 0:
        return False
    if _diagnostics_indicate_failure(diagnostics):
        return False
    if "reported errors" in build_status:
        return False
    if _goals_still_open(goals):
        return False
    return bool(verification_ok)


def _module_name_for_file(active_file: str) -> str:
    if not active_file:
        return ""
    try:
        relative = Path(active_file).resolve().relative_to(Path(_project_root()).resolve())
    except Exception:
        return ""
    parts = list(relative.parts)
    if not parts or not parts[-1].endswith(".lean"):
        return ""
    parts[-1] = parts[-1][:-5]
    if any(not re.match(r"^[A-Za-z_][A-Za-z0-9_']*$", part) for part in parts):
        return ""
    return ".".join(parts)


def _relative_file_label(active_file: str) -> str:
    if not active_file:
        return ""
    try:
        return str(Path(active_file).resolve().relative_to(Path(_project_root()).resolve()))
    except Exception:
        return active_file


def _canonical_file_verification_command(active_file: str) -> str:
    relative_label = _relative_file_label(active_file)
    if not relative_label:
        return ""
    return f"lake env lean {relative_label}"


def _queue_item_verification_hint(active_file: str) -> str:
    command = _canonical_file_verification_command(active_file)
    if not command:
        return ""
    return (
        "- canonical acceptance tool: `lean_verify(mode=file_exact)` on the active file\n"
        f"- backend check performed by the tool: `{command}`\n"
        "- use `lean_inspect` for iteration, but do not accept the theorem as solved until `lean_verify(mode=file_exact)` succeeds\n"
        "- do not treat `lake build`, `grep`, `head`, or truncated output as proof that this theorem-sized repair is clean"
    )


def _recommended_verification_command(active_file: str) -> str:
    relative_label = _relative_file_label(active_file)
    if _single_queue_item_turn_enabled() and active_file:
        return (
            f"`lean_inspect` on {relative_label}, then the required acceptance check "
            f"`lean_verify(mode=file_exact)` for this file-scoped theorem turn"
        )
    module_name = _module_name_for_file(active_file)
    if module_name:
        return f"`lean_inspect` first, then `lean_verify(mode=module)` when the file is close to clean"
    return f"`lean_inspect` on {relative_label}, then final `lean_verify(mode=file_exact)` when close to clean"


def _run_explicit_verification_build(active_file: str = "", *, full_project: bool = False) -> tuple[bool, str]:
    mode = "project"
    if not full_project and active_file:
        mode = "module" if _module_name_for_file(active_file) else "file_exact"
    result = lean_verify(target=active_file, cwd=_project_root(), mode=mode)
    if result.ok:
        return True, f"{result.command} succeeded"
    detail = str(result.output or "").strip() or "verification failed"
    return False, f"{result.command} reported errors: {detail[:280]}"


def _promote_live_state_to_verified(live_state: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = dict(live_state or {})
    if not normalized or not normalized.get("active_file"):
        return normalized
    normalized["verification_ok"] = False
    declaration_scope = str(normalized.get("declaration_scope", "") or _declaration_queue_scope())
    if _diagnostics_indicate_failure(str(normalized.get("diagnostics", "") or "")):
        return normalized
    if _goals_still_open(str(normalized.get("goals", "") or "")):
        return normalized
    sorry_count = normalized.get("sorry_count")
    if isinstance(sorry_count, int) and sorry_count > 0:
        return normalized

    project_sorry_count, project_sorry_files = _count_project_sorries(_project_root())
    normalized["project_sorry_count"] = project_sorry_count
    normalized["project_sorry_files"] = project_sorry_files
    active_file = str(normalized.get("active_file", "") or "")
    ok, build_status = _run_explicit_verification_build(active_file, full_project=False)
    verification_ok = bool(ok)
    needs_full_project_build = (
        verification_ok
        and isinstance(project_sorry_count, int)
        and project_sorry_count == 0
        and bool(_module_name_for_file(active_file))
    )
    if needs_full_project_build:
        verification_ok, build_status = _run_explicit_verification_build(active_file, full_project=True)
    normalized["build_status"] = build_status
    normalized["verification_ok"] = bool(verification_ok)
    if declaration_scope != "file" and isinstance(project_sorry_count, int) and project_sorry_count > 0:
        normalized["blocker_summary"] = (
            f"project still contains {project_sorry_count} sorry placeholder(s): "
            + ", ".join(project_sorry_files[:4])
        )
    elif not verification_ok:
        normalized["blocker_summary"] = build_status
    else:
        normalized["blocker_summary"] = ""
    return normalized


def _success_state(text: str, blocker_summary: str) -> str:
    if blocker_summary:
        return "blocked"
    return "in-progress"


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


def _format_turns_for_snapshot(turns: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in turns:
        role = str(msg.get("role", "unknown") or "unknown").upper()
        content = _message_text(msg.get("content"))
        if len(content) > 2500:
            content = content[:1500] + "\n...[truncated]...\n" + content[-700:]
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            tool_names = []
            for tool_call in tool_calls:
                if isinstance(tool_call, dict):
                    tool_names.append(tool_call.get("function", {}).get("name", "?"))
                else:
                    tool_names.append(getattr(getattr(tool_call, "function", None), "name", "?"))
            content = f"{content}\n[Tool calls: {', '.join(tool_names)}]"
        parts.append(f"[{role}]\n{content}".strip())
    return "\n\n".join(parts).strip()


def _fallback_checkpoint_summary(
    history: list[dict[str, Any]],
    *,
    label: str,
    trigger: str,
    note: str = "",
    live_state: Mapping[str, Any] | None = None,
) -> str:
    metadata = _snapshot_metadata()
    text = _collect_message_text(history[-12:])
    active_files = _extract_active_files(text)
    target_symbol = _extract_target_symbol(text)
    diagnostics = _extract_diagnostics_summary(history)
    blocker_summary = _extract_blocker_summary(text)
    state = "verified" if _live_state_is_verified(live_state) else _success_state(text, blocker_summary)
    sections = [
        f"## Goal\nContinue the {_workflow_kind()} workflow for `{metadata['workflow_command']}`.",
        f"## Workflow\nLabel: {label}\nTrigger: {trigger}\nProject root: {metadata['project_root']}",
        f"## Current state\nSuccess state: {state}\nTarget: {target_symbol or '[unknown]'}",
        f"## Lean findings\n{diagnostics or 'No recent diagnostics were captured.'}",
        f"## Relevant files\n{', '.join(active_files) if active_files else '[no specific Lean file identified]'}",
        f"## Blockers\n{blocker_summary or 'No blocker declared at this checkpoint.'}",
        f"## Next steps\n{note or 'Resume from the latest verified or in-progress state and inspect the active Lean file before continuing.'}",
    ]
    return "\n\n".join(sections)


def _generate_managed_snapshot(
    compressor: ContextCompressor,
    turns_to_summarize: list[dict[str, Any]],
) -> str | None:
    metadata = _snapshot_metadata()
    transcript = _format_turns_for_snapshot(turns_to_summarize)
    prompt = f"""Create a compact managed-workflow handoff for a later assistant continuing a Lean session after compaction.

Keep it factual and compact. Preserve theorem-solving continuity.

Use exactly this structure:
## Goal
## Workflow
## Current state
## Lean findings
## Relevant files
## Blockers
## Next steps

Requirements:
- Mention the workflow kind and workflow command.
- Preserve the active project root and any files or declarations being edited.
- Keep concrete Lean diagnostics, proof goals, theorem names, and blockers when available.
- Mention important tool usage and results only if they matter for the next steps.
- Focus on what is already done and what the next assistant should do next.
- Do not add preamble or markdown fences.

Workflow kind: {metadata["workflow_kind"]}
Workflow command: {metadata["workflow_command"]}
Project root: {metadata["project_root"]}
Model: {metadata["model"]}

TURNS TO COMPACT:
{transcript}
"""

    try:
        call_kwargs: dict[str, Any] = {
            "task": "compression",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": compressor.summary_target_tokens * 2,
            "timeout": 30.0,
        }
        if compressor.summary_model:
            call_kwargs["model"] = compressor.summary_model
        response = call_llm(**call_kwargs)
        content = response.choices[0].message.content
        if not isinstance(content, str):
            content = str(content) if content else ""
        summary = content.strip()
        if not summary:
            return None
        return f"{MANAGED_SNAPSHOT_PREFIX}\n\n{summary}"
    except RuntimeError:
        return None
    except Exception:
        return None


def _generate_checkpoint_summary(
    compressor: ContextCompressor,
    history: list[dict[str, Any]],
    *,
    label: str,
    trigger: str,
    note: str = "",
    live_state: Mapping[str, Any] | None = None,
) -> str:
    metadata = _snapshot_metadata()
    transcript = _format_turns_for_snapshot(history[-18:])
    prompt = f"""Create a persisted workflow checkpoint handoff for a later assistant resuming an autonomous Lean session.

Use exactly this structure:
## Goal
## Workflow
## Current state
## Lean findings
## Relevant files
## Blockers
## Next steps

Requirements:
- Mention the checkpoint label and trigger.
- Preserve theorem targets, Lean files, diagnostics, and blockers.
- Emphasize what changed since the previous milestone and what should happen next.
- Be concrete enough that the next assistant can resume without the older transcript.
- Do not add preamble or markdown fences.

Workflow kind: {metadata["workflow_kind"]}
Workflow command: {metadata["workflow_command"]}
Project root: {metadata["project_root"]}
Checkpoint label: {label}
Checkpoint trigger: {trigger}
Checkpoint note: {note or '[none]'}

RECENT SESSION STATE:
{transcript}
"""
    try:
        call_kwargs: dict[str, Any] = {
            "task": "compression",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": compressor.summary_target_tokens * 2,
            "timeout": 30.0,
        }
        if compressor.summary_model:
            call_kwargs["model"] = compressor.summary_model
        response = call_llm(**call_kwargs)
        content = response.choices[0].message.content
        if not isinstance(content, str):
            content = str(content) if content else ""
        summary = content.strip()
        if summary:
            return summary
    except Exception:
        pass
    return _fallback_checkpoint_summary(history, label=label, trigger=trigger, note=note, live_state=live_state)


def _build_snapshot_message(messages: list[dict[str, Any]], insert_at: int, summary: str) -> dict[str, Any]:
    previous_role = messages[insert_at - 1].get("role", "user") if insert_at > 0 else "user"
    summary_role = "user" if previous_role in ("assistant", "tool") else "assistant"
    return {"role": summary_role, "content": summary}


def _prune_history(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Trim stale tool payloads while preserving recent turns verbatim."""
    protect_recent_user_turns = 2
    max_old_tool_chars = 2000
    pruned: list[dict[str, Any]] = []
    recent_user_turns = 0
    pruned_messages = 0

    for message in reversed(messages):
        role = str(message.get("role", "") or "")
        keep_full = recent_user_turns < protect_recent_user_turns
        updated = dict(message)

        if role == "user":
            recent_user_turns += 1

        if (
            role == "tool"
            and not keep_full
            and isinstance(updated.get("content"), str)
            and len(updated["content"]) > max_old_tool_chars
        ):
            updated["content"] = (
                updated["content"][:max_old_tool_chars]
                + "\n\n[epflemma-native pruned older tool output to preserve context budget]"
            )
            pruned_messages += 1

        pruned.append(updated)

    pruned.reverse()
    return pruned, pruned_messages


def _latest_filesystem_checkpoint_hash(agent: AIAgent, *, reason: str = "", force: bool = False) -> str:
    checkpoint_mgr = getattr(agent, "_checkpoint_mgr", None)
    if checkpoint_mgr is None or not getattr(checkpoint_mgr, "enabled", False):
        return ""
    working_dir = _project_root()
    if force:
        try:
            checkpoint_mgr.ensure_checkpoint(working_dir, reason or "workflow checkpoint")
        except Exception:
            return ""
    try:
        checkpoints = checkpoint_mgr.list_checkpoints(working_dir)
    except Exception:
        return ""
    if not checkpoints:
        return ""
    return str(checkpoints[0].get("hash", "") or "")


def _write_workflow_checkpoint(
    history: list[dict[str, Any]],
    agent: AIAgent,
    *,
    label: str,
    trigger: str,
    note: str = "",
    force_filesystem_checkpoint: bool = False,
    live_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_workflow_state_root()
    metadata = _snapshot_metadata()
    if live_state is None:
        live_state = _build_live_proof_state(history)
    summary_text = _generate_checkpoint_summary(
        agent.context_compressor,
        history,
        label=label,
        trigger=trigger,
        note=note,
        live_state=live_state,
    )
    combined_text = _collect_message_text(history[-18:])
    active_files = _extract_active_files(combined_text + "\n" + summary_text)
    diagnostics_summary = _extract_diagnostics_summary(history)
    blocker_summary = _extract_blocker_summary(summary_text + "\n" + combined_text)
    target_symbol = _extract_target_symbol(summary_text + "\n" + combined_text)
    checkpoint_id = f"ckpt-{int(time.time() * 1000)}"
    snapshot_path = _workflow_state_root() / f"{checkpoint_id}.json"
    linked_hash = _latest_filesystem_checkpoint_hash(
        agent,
        reason=label,
        force=force_filesystem_checkpoint,
    )
    entry = {
        "checkpoint_id": checkpoint_id,
        "created_at": _utc_now_isoformat(),
        "label": label,
        "trigger": trigger,
        "note": note,
        "workflow_kind": metadata["workflow_kind"],
        "workflow_command": metadata["workflow_command"],
        "project_root": metadata["project_root"],
        "model": metadata["model"],
        "active_files": active_files,
        "target_symbol": target_symbol,
        "diagnostics_summary": diagnostics_summary,
        "blocker_summary": blocker_summary,
        "next_steps": _extract_next_steps(summary_text) or note,
        "success_state": (
            "verified"
            if _live_state_is_verified(live_state)
            else _success_state(summary_text + "\n" + combined_text, blocker_summary)
        ),
        "rough_tokens": estimate_messages_tokens_rough(history),
        "linked_filesystem_checkpoint": linked_hash,
        "summary_text": summary_text,
        "snapshot_path": str(snapshot_path),
    }
    _write_json_file(snapshot_path, {"version": 1, **entry})
    index_entries = _load_workflow_index()
    index_entries.append(entry)
    _save_workflow_index(index_entries)
    _write_current_checkpoint(entry)
    _record_activity(
        "checkpoint",
        f"{label} ({trigger})",
        checkpoint_id=checkpoint_id,
        success_state=entry["success_state"],
        target_symbol=target_symbol,
        active_files=active_files,
    )
    return entry


def _compact_history(
    history: list[dict[str, Any]],
    compressor: ContextCompressor,
    force: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pruned_history, pruned_messages = _prune_history(history)
    rough_tokens = estimate_messages_tokens_rough(pruned_history)
    threshold = compressor.threshold_tokens
    status = {
        "compacted": False,
        "forced": force,
        "rough_tokens_before": rough_tokens,
        "rough_tokens_after": rough_tokens,
        "snapshot_created": False,
        "pruned_messages": pruned_messages,
        "snapshot_text": "",
        "reason": "below-threshold",
    }

    minimum_messages = compressor.protect_first_n + compressor.protect_last_n + 1
    if not force and rough_tokens < threshold:
        return pruned_history, status
    if len(pruned_history) <= minimum_messages:
        status["reason"] = "too-short"
        return pruned_history, status

    compress_start = compressor._align_boundary_forward(pruned_history, compressor.protect_first_n)
    compress_end = compressor._align_boundary_backward(
        pruned_history, len(pruned_history) - compressor.protect_last_n
    )
    if compress_start >= compress_end:
        status["reason"] = "no-middle-region"
        return pruned_history, status

    summary = _generate_managed_snapshot(compressor, pruned_history[compress_start:compress_end])
    compacted = [msg.copy() for msg in pruned_history[:compress_start]]
    if summary:
        compacted.append(_build_snapshot_message(pruned_history, compress_start, summary))
        status["snapshot_created"] = True
        status["snapshot_text"] = summary
    compacted.extend(msg.copy() for msg in pruned_history[compress_end:])
    compacted = compressor._sanitize_tool_pairs(compacted)
    status["compacted"] = compacted != pruned_history
    status["rough_tokens_after"] = estimate_messages_tokens_rough(compacted)
    status["reason"] = "forced" if force else "threshold"
    if status["compacted"]:
        _record_activity(
            "compaction",
            f"Managed history compacted ({status['reason']})",
            rough_tokens_before=status["rough_tokens_before"],
            rough_tokens_after=status["rough_tokens_after"],
            pruned_messages=pruned_messages,
        )
    return compacted, status


def _auto_compact_history(
    history: list[dict[str, Any]],
    agent: AIAgent,
    force: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if force or getattr(agent, "compression_enabled", True):
        return _compact_history(history, agent.context_compressor, force=force)
    pruned_history, pruned_messages = _prune_history(history)
    rough_tokens = estimate_messages_tokens_rough(pruned_history)
    return pruned_history, {
        "compacted": False,
        "forced": force,
        "rough_tokens_before": rough_tokens,
        "rough_tokens_after": rough_tokens,
        "snapshot_created": False,
        "pruned_messages": pruned_messages,
        "snapshot_text": "",
        "reason": "disabled",
    }


def _build_agent() -> AIAgent:
    model = _read_native_env("MODEL")
    base_url = _read_native_env("BASE_URL")
    api_key = _read_native_env("API_KEY")
    provider = _read_native_env("PROVIDER")
    api_mode = _read_native_env("API_MODE")
    max_turns_raw = _read_text_env("AGENT_MAX_TURNS", "120")
    try:
        max_turns = max(1, int(max_turns_raw))
    except ValueError:
        max_turns = 120

    if not model:
        raise SystemExit("epflemma-native: EPFLEMMA_NATIVE_MODEL is not configured")
    if not base_url or not api_key:
        raise SystemExit("epflemma-native: provider credentials are incomplete")

    toolset_name = _read_native_env("TOOLSET", "epflemma-native") or "epflemma-native"
    logging_cfg = _logging_config()
    agent_cfg = _agent_config()
    reasoning_cfg = _parse_managed_reasoning_config(str(agent_cfg.get("reasoning_effort", "auto")))
    agent = AIAgent(
        model=model,
        base_url=base_url,
        api_key=api_key,
        provider=provider or None,
        api_mode=api_mode or None,
        max_iterations=max_turns,
        enabled_toolsets=[toolset_name],
        quiet_mode=False,
        verbose_logging=False,
        platform="cli",
        checkpoints_enabled=True,
        checkpoint_max_snapshots=50,
        tool_progress_callback=_tool_progress_callback,
        step_callback=_step_callback,
        reasoning_config=reasoning_cfg,
        seed=_managed_agent_seed(agent_cfg.get("seed")),
        temperature=_managed_agent_float(agent_cfg.get("temperature")),
        top_p=_managed_agent_float(agent_cfg.get("top_p")),
        top_k=_managed_agent_int(agent_cfg.get("top_k")),
        min_p=_managed_agent_float(agent_cfg.get("min_p")),
        log_preview_lines=logging_cfg.get("preview_lines", 8),
        log_preview_chars=logging_cfg.get("preview_chars", 1600),
        tool_output_head_lines=logging_cfg.get("tool_output_head_lines", 28),
        tool_output_tail_lines=logging_cfg.get("tool_output_tail_lines", 12),
    )
    agent._managed_base_reasoning_config = dict(reasoning_cfg or {}) if reasoning_cfg else None

    def _post_tool_result_callback(function_name: str, _args: Mapping[str, Any], _result: str) -> None:
        _handle_managed_tool_result(agent, function_name, _args, _result)

    agent.post_tool_result_callback = _post_tool_result_callback
    owner_id = str(getattr(agent, "session_id", "") or "")
    if owner_id:
        os.environ["EPFLEMMA_NATIVE_RUNNER_OWNER"] = owner_id
    global _CURRENT_AGENT_ACTIVITY_DETAILS
    _CURRENT_AGENT_ACTIVITY_DETAILS = _agent_activity_details(agent)
    return agent


def _print_header() -> None:
    workflow_kind = _workflow_display_name()
    project_root = _project_root()
    model = _read_native_env("MODEL")
    provider = _read_native_env("PROVIDER")
    base_url = _read_native_env("BASE_URL")
    print("EPFLemma Native Managed Workflow")
    print("=" * 32)
    print(f"Workflow: {workflow_kind}")
    print(f"Model: {model} [{provider}]")
    print(f"Endpoint: {base_url}")
    print(f"Active skill: {_active_skill() or '(none)'}")
    print(f"Parallel agents: {_parallel_agents()}")
    if project_root:
        print(f"Project: {project_root}")
    print(f"Run log: {_workflow_state_root() / 'latest-run.log'}")
    print("")
    print("Commands: /help, /status, /status <agent> [N], /swarm [agent] [N], /proof-state, /diagnostics, /goals, /history, /checkpoint [note], /rollback <N>, /resume-plan [N], /compact, /exit, Ctrl+C")
    print("Inspect later from the shell with /workflow activity or /workflow log 120.")
    print("")


def _print_interactive_mode_header(live_state: Mapping[str, Any] | None = None) -> None:
    live_state = dict(live_state or {})
    phase = str(live_state.get("build_status", "") or "managed session")
    active_file = str(live_state.get("active_file_label", "") or "[unknown file]")
    theorem = str(live_state.get("target_symbol", "") or "[unknown target]")
    print("")
    print("─" * 78)
    print(f"prover-agent mode  ·  {phase}")
    print(f"file: {active_file}  ·  target: {theorem}")
    print("commands: /status  /status <agent> [N]  /swarm [agent] [N]  /proof-state  /diagnostics  /goals  /history  /compact  /exit  Ctrl+C")
    print("─" * 78)


def _run_managed_conversation(
    agent: AIAgent,
    *,
    on_interrupt: Callable[[], None] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    result_holder: dict[str, Any] = {}
    error_holder: dict[str, BaseException] = {}

    def _target() -> None:
        try:
            result_holder["result"] = agent.run_conversation(**kwargs)
        except BaseException as exc:  # pragma: no cover - exercised through caller behavior
            error_holder["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()

    interrupt_requested = False
    while worker.is_alive():
        try:
            worker.join(timeout=0.1)
        except KeyboardInterrupt:
            if not interrupt_requested:
                interrupt_requested = True
                print("\nInterrupt requested. Stopping the active agent turn...")
                if on_interrupt is not None:
                    try:
                        on_interrupt()
                    except Exception:
                        pass
                agent.interrupt()
            else:
                print("\nStill stopping the active agent turn...")

    if "error" in error_holder:
        error = error_holder["error"]
        if isinstance(error, (KeyboardInterrupt, InterruptedError)):
            try:
                agent.clear_interrupt()
            except Exception:
                pass
            result = {
                "messages": list(getattr(agent, "_session_messages", []) or kwargs.get("conversation_history") or []),
                "api_calls": 0,
                "completed": False,
                "interrupted": True,
                "final_response": "Operation interrupted by user.",
            }
            print("Returned to prover-agent mode after interrupt.")
            return result
        raise error

    result = result_holder.get("result")
    if interrupt_requested and not isinstance(result, dict):
        try:
            agent.clear_interrupt()
        except Exception:
            pass
        result = {
            "messages": list(getattr(agent, "_session_messages", []) or kwargs.get("conversation_history") or []),
            "api_calls": 0,
            "completed": False,
            "interrupted": True,
            "final_response": "Operation interrupted by user.",
        }
        print("Returned to prover-agent mode after interrupt.")
        return result
    if not isinstance(result, dict):
        raise RuntimeError("Managed conversation did not return a result payload")

    if result.get("interrupted") and not _is_step_boundary_interrupt(result):
        print("Returned to prover-agent mode after interrupt.")
    return result


def _history_status_lines(
    history: list[dict[str, Any]],
    compaction_state: dict[str, Any] | None = None,
    checkpoint_state: dict[str, Any] | None = None,
    live_state: dict[str, Any] | None = None,
) -> list[str]:
    tool_messages = 0
    user_messages = 0
    assistant_messages = 0
    for message in history:
        role = str(message.get("role", "") or "")
        if role == "tool":
            tool_messages += 1
        elif role == "user":
            user_messages += 1
        elif role == "assistant":
            assistant_messages += 1
    rough_tokens = estimate_messages_tokens_rough(history)
    compaction_state = compaction_state or {}
    checkpoint_state = checkpoint_state or {}
    live_state = live_state or {}
    snapshot_exists = bool(compaction_state.get("snapshot_text"))
    current_checkpoint = checkpoint_state.get("current") or {}
    agents = summarize_workflow_agents(activity_limit=1)
    active_agents = sum(1 for agent in agents if str(agent.get("status", "") or "") in ACTIVE_AGENT_STATUSES)
    live_agents = sum(1 for agent in agents if str(agent.get("status", "") or "") in LIVE_AGENT_STATUSES)
    dead_agents = sum(1 for agent in agents if str(agent.get("status", "") or "") in DEAD_AGENT_STATUSES)
    return [
        f"Messages: {len(history)}",
        f"Users: {user_messages}",
        f"Assistants: {assistant_messages}",
        f"Tools: {tool_messages}",
        f"Rough tokens: {rough_tokens}",
        f"Workflow: {_workflow_display_name()}",
        f"Command: {_read_native_env('WORKFLOW_COMMAND', '[unset]')}",
        f"Model: {_read_native_env('MODEL')}",
        f"Project: {_project_root()}",
        f"Snapshot: {'yes' if snapshot_exists else 'no'}",
        f"Last compaction: {compaction_state.get('reason', '[none]')}",
        f"Checkpoints: {checkpoint_state.get('count', 0)}",
        f"Latest checkpoint: {str(current_checkpoint.get('label', '') or '[none]')}",
        f"Latest filesystem checkpoint: {str(current_checkpoint.get('linked_filesystem_checkpoint', '') or '[none]')}",
        f"Active file: {str(live_state.get('active_file_label', '') or '[unknown]')}",
        f"Target theorem: {str(live_state.get('target_symbol', '') or '[unknown]')}",
        f"Project sorries: {str(live_state.get('project_sorry_count', '') or '[unknown]')}",
        f"Agents: {len(agents)} total / {live_agents} live / {active_agents} active / {dead_agents} dead",
    ]


def _print_swarm_overview(activity_limit: int = 5) -> None:
    agents = summarize_workflow_agents(activity_limit=activity_limit)
    if not agents:
        print("No workflow agents have been recorded yet.")
        return
    print("Agents:")
    for agent in agents:
        agent_id = str(agent.get("agent_id", "") or "[unknown]")
        status = str(agent.get("status", "") or "active")
        depth = str(agent.get("delegate_depth", 0))
        api_calls = str(agent.get("api_calls", 0))
        model = str(agent.get("model", "") or "[unknown]")
        latest = str(agent.get("last_message", "") or "")
        print(f"- {agent_id}  [{status}]  depth={depth}  api_calls={api_calls}  model={model}")
        if latest:
            print(f"  latest: {latest}")


def _print_agent_detail(agent_id: str, recent_limit: int = 5) -> bool:
    agent = workflow_agent_detail(agent_id, activity_limit=recent_limit)
    if not agent:
        print(f"Agent not found: {agent_id}")
        return False
    print(f"Agent: {agent.get('agent_id', '[unknown]')}")
    print(f"Parent: {agent.get('parent_agent_id') or '[root]'}")
    print(f"State: {agent.get('status', '[unknown]')}")
    print(f"Depth: {agent.get('delegate_depth', 0)}")
    print(f"Model: {agent.get('model') or '[unknown]'}")
    print(f"Provider: {agent.get('provider') or '[unknown]'}")
    print(f"Base URL: {agent.get('base_url') or '[unknown]'}")
    print(f"API calls: {agent.get('api_calls', 0)}")
    print(f"Tool calls: {agent.get('tool_calls', 0)}")
    print(f"Started: {agent.get('started_at') or '[unknown]'}")
    print(f"Finished: {agent.get('finished_at') or '[active]'}")
    recent = agent.get("recent_activity")
    if isinstance(recent, list) and recent:
        print("Recent activity:")
        for event in recent[-max(1, recent_limit):]:
            timestamp = str(event.get("timestamp", "") or "")
            event_type = str(event.get("type", "") or "")
            preview = str(event.get("preview", "") or "")
            print(f"- {timestamp}  {event_type}  {preview}")
    return True


def _record_turn_activity(
    previous_history: list[dict[str, Any]],
    new_history: list[dict[str, Any]],
    *,
    phase: str,
) -> None:
    delta = new_history[len(previous_history):]
    if not delta:
        return
    tool_names: list[str] = []
    for message in delta:
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict):
                name = str(tool_call.get("function", {}).get("name", "") or "")
            else:
                name = str(getattr(getattr(tool_call, "function", None), "name", "") or "")
            if name and name not in tool_names:
                tool_names.append(name)
    summary_text = _collect_message_text(delta).strip().replace("\n", " ")
    if len(summary_text) > 180:
        summary_text = summary_text[:177] + "..."
    if tool_names and not summary_text:
        summary_text = f"Completed {phase} step via {', '.join(tool_names[:4])}"
    _record_activity(
        "turn",
        summary_text or f"Managed {phase} step completed",
        phase=phase,
        tools=tool_names,
        tool_count=len(tool_names),
    )


def _terminate_descendant_agents(agent: Any) -> None:
    agent_id = str(getattr(agent, "session_id", "") or "")
    if not agent_id:
        return
    result = terminate_workflow_agent_descendants(agent_id)
    count = int(result.get("count", 0) or 0)
    failed = result.get("failed")
    if count:
        _record_agent_activity(
            agent,
            "descendants-terminated",
            f"Interrupted {count} descendant agent(s) during runner exit",
            terminated=result.get("terminated", []),
        )
    if failed:
        _record_agent_activity(
            agent,
            "descendants-termination-failed",
            "Some descendant agents could not be interrupted during runner exit",
            failed=failed,
        )


def _terminate_other_agents(agent: Any) -> None:
    agent_id = str(getattr(agent, "session_id", "") or "")
    result = terminate_project_workflow_agents(
        _project_root(),
        exclude_agent_id=agent_id,
        exclude_process_id=os.getpid(),
    )
    count = int(result.get("count", 0) or 0)
    failed = result.get("failed")
    if count:
        _record_agent_activity(
            agent,
            "agents-terminated",
            f"Interrupted {count} other workflow agent(s) during runner exit",
            terminated=result.get("terminated", []),
        )
    if failed:
        _record_agent_activity(
            agent,
            "agents-termination-failed",
            "Some workflow agents could not be interrupted during runner exit",
            failed=failed,
        )


def _run_background_control_loop(
    agent: Any,
    system_prompt: str,
    history: list[dict[str, Any]],
    compaction_state: dict[str, Any],
    checkpoint_state: dict[str, Any],
    live_state: dict[str, Any],
    autonomy_state: dict[str, Any],
) -> int:
    agent_id = str(getattr(agent, "session_id", "") or "")
    last_seq = 0
    announced_waiting = False

    try:
        while True:
            waiting_phase = "verified" if _live_state_is_verified(live_state) else "paused"
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase=waiting_phase)
            if not announced_waiting:
                _record_agent_activity(
                    agent,
                    "agent-awaiting-input",
                    "Background workflow agent is waiting for input",
                    status=waiting_phase,
                )
                announced_waiting = True

            pending = [entry for entry in read_workflow_agent_inbox(agent_id) if int(entry.get("seq", 0) or 0) > last_seq]
            if not pending:
                time.sleep(0.5)
                continue

            for command in pending:
                last_seq = int(command.get("seq", 0) or last_seq)
                kind = str(command.get("kind", "message") or "message")
                text = str(command.get("text", "") or "").strip()
                if not text:
                    continue
                if kind == "exit":
                    _terminate_descendant_agents(agent)
                    _terminate_other_agents(agent)
                    _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="exited")
                    _record_agent_activity(agent, "runner-exit", "Managed workflow runner exited by remote command")
                    return 0

                announced_waiting = False
                _record_agent_activity(agent, "agent-resume", "Processing queued prompt", text=text)
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _maybe_checkpoint_before_compaction(history, agent, live_state=live_state)
                history, compaction_state = _auto_compact_history(history, agent)
                previous_history = history[:]
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="busy")
                _record_queue_assignment(live_state, phase="background")
                _prepare_queue_assignment_state(autonomy_state, live_state)
                augmented_text = _attach_live_proof_state(text, live_state)
                _set_runtime_active_skill(_effective_skill_name(live_state))
                effective_reasoning = _apply_managed_reasoning_policy(agent, live_state, autonomy_state)
                _record_managed_reasoning_policy(
                    live_state,
                    autonomy_state,
                    effective_reasoning,
                    phase="background",
                )
                _prepare_managed_turn_state(agent, autonomy_state)
                result = _run_managed_conversation(
                    agent,
                    on_interrupt=lambda: _persist_live_status(
                        history,
                        compaction_state,
                        checkpoint_state,
                        live_state,
                        phase="paused",
                    ),
                    user_message=augmented_text,
                    system_message=system_prompt,
                    conversation_history=history,
                    persist_user_message=text,
                )
                history = result["messages"]
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _record_turn_activity(previous_history, history, phase="interactive")
                _maybe_write_milestone_checkpoint(previous_history, history, agent, autonomy_state, live_state=live_state)
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state)
                if result.get("interrupted") and not _is_step_boundary_interrupt(result):
                    _record_activity("interactive-interrupted", "Interactive agent turn interrupted by user")
                    _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="paused")
                else:
                    history, compaction_state, checkpoint_state, live_state = _drive_autonomous_followups(
                        agent,
                        system_prompt,
                        history,
                        compaction_state,
                        checkpoint_state,
                        autonomy_state,
                    )
                    if _live_state_is_verified(live_state):
                        _terminate_descendant_agents(agent)
                        _terminate_other_agents(agent)
                        _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="exited")
                        _record_agent_activity(agent, "runner-exit", "Managed workflow runner exited after verified completion")
                        return 0
    except KeyboardInterrupt:
        _terminate_descendant_agents(agent)
        _terminate_other_agents(agent)
        _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="exited")
        _record_agent_activity(agent, "runner-exit", "Managed workflow runner interrupted by signal")
        return 0


def _milestone_label_for_delta(
    previous_history: list[dict[str, Any]],
    new_history: list[dict[str, Any]],
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    delta = new_history[len(previous_history):]
    if not delta:
        return "", ""
    delta_text = _collect_message_text(delta)
    lowered = delta_text.lower()
    workflow_kind = _workflow_kind()

    blocker_tokens = (
        "blocked",
        "blocker",
        "stuck",
        "cannot proceed",
        "can't proceed",
        "unable to",
        "failed to",
    )

    if _live_state_is_verified(live_state):
        autonomy_state["blocked_runs"] = 0
        if workflow_kind == "prove":
            return "verified proof milestone", "verified-progress"
        if workflow_kind == "formalize":
            return "verified formalization milestone", "verified-progress"
        return "verified milestone", "verified-progress"

    mutated = False
    for message in delta:
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict):
                name = str(tool_call.get("function", {}).get("name", "") or "")
            else:
                name = str(getattr(getattr(tool_call, "function", None), "name", "") or "")
            if name in {"patch", "write_file", "terminal"}:
                mutated = True
                break
        if mutated:
            break

    if mutated and any(token in lowered for token in ("lake build", "lean_inspect", "diagnostic", "typecheck")):
        if workflow_kind == "formalize":
            return "formalization draft stabilized", "draft-stabilized"
        return "successful build/typecheck after edits", "build-verified"

    if any(token in lowered for token in blocker_tokens):
        autonomy_state["blocked_runs"] = int(autonomy_state.get("blocked_runs", 0)) + 1
        if autonomy_state["blocked_runs"] >= 2:
            return "blocker checkpoint", "blocker-declared"
        return "", ""

    autonomy_state["blocked_runs"] = 0
    return "", ""


def _print_history(entries: list[dict[str, Any]]) -> None:
    if not entries:
        print("No workflow checkpoints recorded yet.")
        return
    for idx, entry in enumerate(entries, start=1):
        active_files = entry.get("active_files") or []
        target = str(entry.get("target_symbol", "") or "")
        label = str(entry.get("label", "") or "[unnamed]")
        created_at = str(entry.get("created_at", "") or "")
        success = str(entry.get("success_state", "") or "in-progress")
        blocker = str(entry.get("blocker_summary", "") or "")
        file_part = active_files[0] if active_files else "[no file]"
        target_part = target or "[no target]"
        line = f"{idx}. {label} [{success}] {created_at} :: {file_part} :: {target_part}"
        print(line)
        if blocker:
            print(f"   blocker: {blocker}")


def _startup_user_message(
    resumed_checkpoint: Mapping[str, Any] | None = None,
    *,
    live_state: Mapping[str, Any] | None = None,
    autonomy_state: Mapping[str, Any] | None = None,
) -> str:
    startup_prompt = _read_native_env("STARTUP_PROMPT")
    workflow_command = _read_native_env("WORKFLOW_COMMAND")
    workflow_kind = _workflow_kind()
    selected_skill = _effective_skill_name(live_state)
    skill_prompt = build_skill_prompt(selected_skill, _project_root()) if selected_skill else ""
    explicit_goal = _read_native_env("EXPLICIT_GOAL", "")
    goal_block = f"\n\nUser goal: {explicit_goal}" if explicit_goal else ""
    route_block = ""
    route_decision = route_workflow_step(
        workflow_kind,
        live_state,
        configured_skill=selected_skill,
        autonomy_state=autonomy_state,
        cwd=_project_root(),
    ).to_dict()
    if route_decision:
        route_lines = [
            "Route decision:",
            f"- skill: {route_decision.get('skill_name') or '[unknown]'}",
            f"- action: {route_decision.get('route_action') or '[none]'}",
            f"- blocker kind: {route_decision.get('blocker_kind') or '[none]'}",
            f"- reason: {route_decision.get('reason') or '[none]'}",
        ]
        if route_decision.get("recommended_worker"):
            route_lines.append(f"- recommended worker: {route_decision.get('recommended_worker')}")
            route_lines.append("- use `lean_worker_dispatch` if the next attempt confirms this route")
        route_block = f"\n\n{chr(10).join(route_lines)}"
    queue_block = ""
    if _single_queue_item_turn_enabled():
        queue_text = _queue_assignment_block(dict(live_state or {}), autonomy_state)
        if queue_text:
            queue_block = f"\n\n{queue_text}"
    swarm_block = ""
    if _swarm_enabled():
        swarm_block = (
            "\n\nSwarm mode instructions:\n"
            f"- The user explicitly approved up to {_parallel_agents()} concurrent agents for this workflow.\n"
            "- Do not delegate unless the subtask is concrete and bounded.\n"
            "- Each delegated agent should own a distinct Lean file or a distinct verifier/planner role.\n"
            "- Before editing a shared file, acquire a file lock. If another agent owns the lock, choose a different task or wait.\n"
            "- The finish condition remains strict: explicit successful build, no diagnostics, no open goals, no `sorry`."
        )
    if resumed_checkpoint:
        label = str(resumed_checkpoint.get("label", "") or "checkpoint")
        resume_text = f"Resume this managed workflow from persisted {label} and continue carefully from the checkpoint handoff."
        if startup_prompt:
            body = f"{resume_text}\n\n{startup_prompt}{goal_block}{route_block}{queue_block}{swarm_block}"
            return f"{body}\n\n{skill_prompt}".strip() if skill_prompt else body
        if workflow_command:
            body = f"{resume_text}\n\n{_workflow_startup_guidance(workflow_kind, workflow_command)}{goal_block}{route_block}{queue_block}{swarm_block}"
            return f"{body}\n\n{skill_prompt}".strip() if skill_prompt else body
        return resume_text
    if startup_prompt:
        body = f"{startup_prompt}{goal_block}{route_block}{queue_block}{swarm_block}"
        return f"{body}\n\n{skill_prompt}".strip() if skill_prompt else body
    if workflow_command:
        body = f"{_workflow_startup_guidance(workflow_kind, workflow_command)}{goal_block}{route_block}{queue_block}{swarm_block}"
        return f"{body}\n\n{skill_prompt}".strip() if skill_prompt else body
    return f"Begin the requested managed Lean workflow now.\n\n{skill_prompt}".strip() if skill_prompt else "Begin the requested managed Lean workflow now."


def _managed_system_prompt() -> str:
    context_path = _read_text_env("EPFLEMMA_WORKFLOW_CONTEXT", _read_text_env("GAUSS_AUTOFORMALIZE_CONTEXT"))
    context_text = ""
    if context_path:
        try:
            context_text = Path(context_path).read_text(encoding="utf-8")
        except OSError:
            context_text = ""

    if _runner_lean_prompt_enabled():
        sections = [
            "You are the epflemma-native managed Lean workflow backend.",
            "Work inside the active Lean project only.",
            "Treat `/prove`, `/formalize`, `/review`, `/checkpoint`, `/refactor`, and `/golf` as native workflow labels and instructions, not shell commands.",
            "The loaded workflow and worker specs are the policy manuals; runner-injected blocks below are turn-local state only.",
            "Use `lean_capabilities` and `lean_inspect` to refresh state first, then follow the active spec.",
            "When a persisted workflow checkpoint exists, treat it as the canonical resume handoff.",
            "Do not use multi-agent delegation unless the user explicitly enabled swarm mode for this workflow.",
        ]
    else:
        sections = [
            "You are the epflemma-native managed Lean workflow backend.",
            "Work inside the active Lean project only.",
            "Treat `/prove`, `/formalize`, `/review`, `/checkpoint`, `/refactor`, and `/golf` as native workflow labels and instructions, not shell commands.",
            "The loaded workflow and worker specs are the policy manuals for tool order, verification ladders, escalation rules, and stop conditions.",
            "Runner-injected blocks below are turn-local state only: queue assignment, route decision, attempt history, blockers, and verification hints.",
            "Use `lean_capabilities` and `lean_inspect` to refresh state first, then follow the active spec rather than inventing a parallel process.",
            "When a persisted workflow checkpoint exists, treat it as the canonical resume handoff instead of reconstructing the full transcript from memory.",
            "Do not use multi-agent delegation unless the user explicitly enabled swarm mode for this workflow.",
        ]
    if _swarm_enabled():
        sections.extend(
            [
                f"User-approved swarm mode is active with {_parallel_agents()} agents total.",
                "Use delegate_task only for concrete bounded subgoals with clear ownership.",
                "Child agents must not edit the same Lean file concurrently.",
                "Require file reservations before editing shared files and keep one path focused on final verification.",
            ]
        )
    if context_text:
        sections.extend(["", "## Startup Context", context_text.strip()])
    return "\n".join(sections).strip()


def _maybe_write_milestone_checkpoint(
    previous_history: list[dict[str, Any]],
    history: list[dict[str, Any]],
    agent: AIAgent,
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not _is_autonomous_workflow():
        return None
    label, trigger = _milestone_label_for_delta(previous_history, history, autonomy_state, live_state=live_state)
    if not label:
        return None
    return _write_workflow_checkpoint(
        history,
        agent,
        label=label,
        trigger=trigger,
        force_filesystem_checkpoint=True,
        live_state=live_state,
    )


def _maybe_checkpoint_before_compaction(
    history: list[dict[str, Any]],
    agent: AIAgent,
    *,
    live_state: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not _is_autonomous_workflow():
        return None
    compressor = agent.context_compressor
    rough_tokens = estimate_messages_tokens_rough(history)
    minimum_messages = compressor.protect_first_n + compressor.protect_last_n + 1
    if rough_tokens < compressor.threshold_tokens or len(history) <= minimum_messages:
        return None
    return _write_workflow_checkpoint(
        history,
        agent,
        label="pre-compaction checkpoint",
        trigger="pre-compaction",
        force_filesystem_checkpoint=True,
        live_state=live_state,
    )


def _autonomous_stop_reason(
    history: list[dict[str, Any]],
    live_state: Mapping[str, Any] | None,
    autonomy_state: dict[str, Any],
) -> str:
    signature = (
        str((live_state or {}).get("active_file_label", "") or ""),
        str((live_state or {}).get("target_symbol", "") or ""),
        str((live_state or {}).get("diagnostics", "") or ""),
        str((live_state or {}).get("goals", "") or ""),
        str((live_state or {}).get("build_status", "") or ""),
        str((live_state or {}).get("sorry_count", "") or ""),
    )
    previous_signature = autonomy_state.get("continuation_live_state_signature")
    stable_cycles = int(autonomy_state.get("continuation_stable_cycles", 0))
    if signature == previous_signature:
        stable_cycles += 1
    else:
        stable_cycles = 0
    autonomy_state["continuation_live_state_signature"] = signature
    autonomy_state["continuation_stable_cycles"] = stable_cycles

    if _live_state_is_verified(live_state):
        if _has_unresolved_theorem_outcomes(autonomy_state):
            autonomy_state["continuation_blocked_runs"] = 0
            autonomy_state["continuation_stable_cycles"] = 0
            return "blocked"
        autonomy_state["continuation_blocked_runs"] = 0
        autonomy_state["continuation_stable_cycles"] = 0
        return "verified"

    recent_text = _collect_message_text(history[-8:])
    blocker_summary = _extract_blocker_summary(recent_text)
    if blocker_summary:
        blocked_runs = int(autonomy_state.get("continuation_blocked_runs", 0)) + 1
        autonomy_state["continuation_blocked_runs"] = blocked_runs
        if blocked_runs >= _autonomous_blocked_limit():
            return "blocked"
    else:
        autonomy_state["continuation_blocked_runs"] = 0

    if stable_cycles >= _autonomous_stalled_limit():
        return "stalled"

    return "continue"


def _autonomous_continuation_prompt(
    live_state: Mapping[str, Any],
    cycle_number: int,
    autonomy_state: Mapping[str, Any] | None = None,
) -> str:
    declaration_scope = str(live_state.get("declaration_scope", "") or _declaration_queue_scope())
    if _runner_lean_prompt_enabled():
        if declaration_scope == "file":
            verification_gate = str(
                live_state.get("verification_hint", "") or "`lean_inspect` on the active file, then the canonical file verification gate"
            )
        else:
            verification_gate = str(
                live_state.get("verification_hint", "") or "`lean_inspect` first, then the project/module verification gate for the requested scope"
            )
        prompt = (
            "Continue the autonomous workflow.\n\n"
            "Follow the loaded native workflow spec as the policy manual. "
            "Use the refreshed live proof state below as the current turn state.\n\n"
            f"This is autonomous continuation cycle {cycle_number}.\n"
            f"Current verification gate: {verification_gate}\n"
            "Do not stop until that gate is satisfied or you have a concrete blocker to report."
        )
    else:
        if declaration_scope == "file":
            verification_lines = (
                "- explicit successful file verification\n"
                "- clean Lean diagnostics in the active file\n"
                "- no warnings in the requested file\n"
                "- no open goals for the active work\n"
                "- no remaining `sorry` in the active file\n\n"
            )
            conclusion = "make the next strongest move, and re-check the active file before concluding."
        else:
            verification_lines = (
                "- explicit successful `lake build`\n"
                "- clean Lean diagnostics\n"
                "- no warnings in the requested scope\n"
                "- no open goals\n"
                "- no remaining `sorry` in the active file\n"
                "- no remaining `sorry` anywhere else in the project outside dependencies\n\n"
            )
            conclusion = "make the next strongest move, and re-check the whole project before concluding."
        prompt = (
            "Continue the autonomous workflow. Do not stop yet unless the workflow is truly verified or "
            "you have a concrete blocker that still remains after another attempt.\n\n"
            "Follow the loaded native workflow spec as the policy manual. "
            "Use the refreshed live proof state below as the current turn state.\n\n"
            "Verification requires all of the following:\n"
            f"{verification_lines}"
            f"This is autonomous continuation cycle {cycle_number}. Use the refreshed live proof state below, "
            f"{conclusion}"
        )
    route_decision = route_workflow_step(
        _workflow_kind(),
        live_state,
        configured_skill=_effective_skill_name(live_state),
        autonomy_state=autonomy_state,
        cwd=_project_root(),
    ).to_dict()
    if route_decision:
        prompt += (
            "\n\nRoute decision:\n"
            f"- skill: {route_decision.get('skill_name') or '[unknown]'}\n"
            f"- action: {route_decision.get('route_action') or '[none]'}\n"
            f"- blocker kind: {route_decision.get('blocker_kind') or '[none]'}\n"
            f"- reason: {route_decision.get('reason') or '[none]'}"
        )
        if route_decision.get("recommended_worker"):
            prompt += (
                f"\n- recommended worker: {route_decision.get('recommended_worker')}\n"
                "- use `lean_worker_dispatch` if the blocker still fits this route after the next focused attempt"
            )
    if _queue_needs_final_file_sweep(live_state):
        prompt += f"\n\n{_final_file_sweep_block(live_state)}"
    else:
        queue_text = _queue_assignment_block(live_state, autonomy_state)
        if queue_text:
            prompt += f"\n\n{queue_text}"
            active_file = str(live_state.get("active_file", "") or "")
            command = _canonical_file_verification_command(active_file)
            if command and not _runner_lean_prompt_enabled():
                prompt += (
                    "\n\n"
                    "For this assigned file-scoped queue item, use `lean_inspect` for iteration, "
                    f"but only accept the theorem as solved after `{command}` succeeds. "
                    "Do not use `lake build`, `grep`, `head`, or truncated output as the acceptance check for this theorem."
                )
    if _swarm_enabled():
        prompt += (
            "\n\nSwarm remains user-approved for this continuation. "
            "Delegate only if the next step splits cleanly across files or verifier/planner roles."
        )
    return prompt


def _drive_autonomous_followups(
    agent: AIAgent,
    system_prompt: str,
    history: list[dict[str, Any]],
    compaction_state: dict[str, Any],
    checkpoint_state: dict[str, Any],
    autonomy_state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not _is_autonomous_workflow():
        live_state = _build_live_proof_state(history, checkpoint_state)
        return history, compaction_state, checkpoint_state, live_state

    for cycle in range(1, _autonomous_followup_limit() + 1):
        autonomy_state["current_cycle"] = cycle
        checkpoint_state = _journal_status()
        live_state = _build_live_proof_state(history, checkpoint_state)
        live_state = _promote_live_state_to_verified(live_state)
        rebuilt_history, transition = _rebuild_history_for_theorem_transition(
            history,
            compaction_state,
            autonomy_state,
            live_state,
        )
        if rebuilt_history is not None and transition is not None:
            previous_outcome = dict(autonomy_state.get("last_theorem_outcome") or {})
            history = rebuilt_history
            _record_activity(
                "theorem-transition",
                "Theorem transition detected",
                previous_theorem=transition["previous_target"],
                current_theorem=transition["current_target"],
                previous_file=transition["previous_file"],
                current_file=transition["current_file"],
                previous_status=str(previous_outcome.get("status", "") or "unknown"),
            )
            _record_activity(
                "theorem-context-cleared",
                f"Cleared theorem-local context for {transition['previous_target']}",
                previous_theorem=transition["previous_target"],
                current_theorem=transition["current_target"],
            )
            _record_activity(
                "theorem-handoff-rebuilt",
                f"Rebuilt compact handoff for {transition['current_target']}",
                previous_theorem=transition["previous_target"],
                current_theorem=transition["current_target"],
                previous_status=str(previous_outcome.get("status", "") or "unknown"),
                previous_note=str(previous_outcome.get("note", "") or ""),
            )
        _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="verifying")
        stop_reason = _autonomous_stop_reason(history, live_state, autonomy_state)
        if stop_reason != "continue":
            _record_activity("autonomy-stop", f"Autonomous workflow stop reason: {stop_reason}", cycle=cycle)
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase=stop_reason)
            return history, compaction_state, checkpoint_state, live_state

        _maybe_checkpoint_before_compaction(history, agent, live_state=live_state)
        history, compaction_state = _auto_compact_history(history, agent)
        checkpoint_state = _journal_status()
        live_state = _build_live_proof_state(history, checkpoint_state)
        previous_history = history[:]
        _record_activity("autonomous-followup", f"Autonomous continuation #{cycle}", cycle=cycle)
        _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="busy")
        _record_queue_assignment(live_state, cycle=cycle, phase="autonomous")
        _prepare_queue_assignment_state(autonomy_state, live_state)
        augmented_text = _attach_live_proof_state(
            _autonomous_continuation_prompt(live_state, cycle, autonomy_state),
            live_state,
        )
        _set_runtime_active_skill(_effective_skill_name(live_state))
        effective_reasoning = _apply_managed_reasoning_policy(agent, live_state, autonomy_state)
        _record_managed_reasoning_policy(
            live_state,
            autonomy_state,
            effective_reasoning,
            phase="autonomous",
            cycle=cycle,
        )
        _prepare_managed_turn_state(agent, autonomy_state)
        result = _run_managed_conversation(
            agent,
            on_interrupt=lambda: _persist_live_status(
                history,
                compaction_state,
                checkpoint_state,
                live_state,
                phase="paused",
            ),
            user_message=augmented_text,
            system_message=system_prompt,
            conversation_history=history,
            persist_user_message=f"[epflemma-native autonomous continuation #{cycle}]",
        )
        history = result["messages"]
        checkpoint_state = _journal_status()
        live_state = _build_live_proof_state(history, checkpoint_state)
        live_state = _promote_live_state_to_verified(live_state)
        if _same_queue_assignment_still_blocked(autonomy_state, live_state):
            _remember_failed_attempt(autonomy_state, live_state, cycle_number=cycle)
        _record_turn_activity(previous_history, history, phase="autonomous")
        _maybe_write_milestone_checkpoint(previous_history, history, agent, autonomy_state, live_state=live_state)
        _persist_live_status(history, compaction_state, checkpoint_state, live_state)
        if result.get("interrupted") and not _is_step_boundary_interrupt(result):
            _record_activity("autonomy-interrupted", f"Autonomous continuation #{cycle} interrupted by user", cycle=cycle)
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="paused")
            return history, compaction_state, checkpoint_state, live_state

    checkpoint_state = _journal_status()
    live_state = _build_live_proof_state(history, checkpoint_state)
    live_state = _promote_live_state_to_verified(live_state)
    if not _live_state_is_verified(live_state):
        print(
            "Autonomous workflow paused after additional continuation cycles without reaching verification. "
            "Inspect /proof-state or continue manually."
        )
    _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="paused")
    return history, compaction_state, checkpoint_state, live_state


def _print_live_proof_state(live_state: Mapping[str, Any], section: str = "") -> None:
    if section == "diagnostics":
        print(str(live_state.get("diagnostics", "unavailable") or "unavailable"))
        return
    if section == "goals":
        print(str(live_state.get("goals", "unavailable") or "unavailable"))
        return
    print(str(live_state.get("message", "No live proof state available.") or "No live proof state available."))


def _resume_plan_from_checkpoint(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    _write_current_checkpoint(entry)
    return _checkpoint_replay_history(entry)


def _rollback_to_checkpoint(agent: AIAgent, entry: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    checkpoint_hash = str(entry.get("linked_filesystem_checkpoint", "") or "").strip()
    if not checkpoint_hash:
        return _checkpoint_replay_history(entry), "Checkpoint has no linked filesystem snapshot; only plan state was resumed."
    checkpoint_mgr = getattr(agent, "_checkpoint_mgr", None)
    if checkpoint_mgr is None or not getattr(checkpoint_mgr, "enabled", False):
        return _checkpoint_replay_history(entry), "Filesystem checkpoints are unavailable; only plan state was resumed."
    result = checkpoint_mgr.restore(_project_root(), checkpoint_hash)
    if not result.get("success"):
        error = str(result.get("error", "restore failed") or "restore failed")
        return _checkpoint_replay_history(entry), f"Filesystem rollback failed: {error}"
    message = (
        f"Restored filesystem to {result.get('restored_to', checkpoint_hash[:8])} "
        f"({result.get('reason', 'unknown')})."
    )
    return _checkpoint_replay_history(entry), message


def main() -> int:
    _install_workflow_run_log_capture()
    agent = _build_agent()
    try:
        system_prompt = _managed_system_prompt()
        history: list[dict[str, Any]] = []
        compaction_state: dict[str, Any] = {
            "snapshot_text": "",
            "reason": "[none]",
            "rough_tokens_before": 0,
            "rough_tokens_after": 0,
            "pruned_messages": 0,
        }
        autonomy_state: dict[str, Any] = {"blocked_runs": 0}
        checkpoint_state = _journal_status()
        resumed_checkpoint = checkpoint_state.get("current")
        if resumed_checkpoint:
            history = _checkpoint_replay_history(resumed_checkpoint)
        live_state = _build_live_proof_state(history, checkpoint_state)
        _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="resumed" if resumed_checkpoint else "ready")
        _record_agent_activity(agent, "runner-start", "Managed workflow runner started", resumed=bool(resumed_checkpoint))

        _print_header()
        if resumed_checkpoint:
            print(f"Loaded persisted checkpoint: {resumed_checkpoint.get('label', '[unknown]')}")
            print("")

        initial_message = _attach_live_proof_state(
            _startup_user_message(
                resumed_checkpoint,
                live_state=live_state,
                autonomy_state=autonomy_state,
            ),
            live_state,
        )
        _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="busy")
        _record_queue_assignment(live_state, phase="startup")
        _prepare_queue_assignment_state(autonomy_state, live_state)
        _set_runtime_active_skill(_effective_skill_name(live_state))
        effective_reasoning = _apply_managed_reasoning_policy(agent, live_state, autonomy_state)
        _record_managed_reasoning_policy(
            live_state,
            autonomy_state,
            effective_reasoning,
            phase="startup",
        )
        _prepare_managed_turn_state(agent, autonomy_state)
        result = _run_managed_conversation(
            agent,
            on_interrupt=lambda: _persist_live_status(
                history,
                compaction_state,
                checkpoint_state,
                live_state,
                phase="paused",
            ),
            user_message=initial_message,
            system_message=system_prompt,
            conversation_history=history,
            persist_user_message="[epflemma-native startup workflow request]",
        )
        previous_history = history[:]
        history = result["messages"]
        checkpoint_state = _journal_status()
        live_state = _build_live_proof_state(history, checkpoint_state)
        live_state = _promote_live_state_to_verified(live_state)
        _record_turn_activity(previous_history, history, phase="startup")
        _persist_live_status(history, compaction_state, checkpoint_state, live_state)
        if result.get("interrupted") and not _is_step_boundary_interrupt(result):
            _record_activity("startup-interrupted", "Startup agent turn interrupted by user")
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="paused")
        else:
            history, compaction_state, checkpoint_state, live_state = _drive_autonomous_followups(
                agent,
                system_prompt,
                history,
                compaction_state,
                checkpoint_state,
                autonomy_state,
            )
        if not _native_interactive_enabled():
            if _live_state_is_verified(live_state):
                _terminate_descendant_agents(agent)
                _terminate_other_agents(agent)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="exited")
                _record_agent_activity(agent, "runner-exit", "Managed workflow runner exited after verified completion")
                return 0
            return _run_background_control_loop(
                agent,
                system_prompt,
                history,
                compaction_state,
                checkpoint_state,
                live_state,
                autonomy_state,
            )
        _print_interactive_mode_header(live_state)

        while True:
            try:
                raw = input("\nprover-agent> ")
            except EOFError:
                print("")
                if _is_autonomous_workflow() and history:
                    _write_workflow_checkpoint(
                        history,
                        agent,
                        label="pre-exit checkpoint",
                        trigger="pre-exit",
                        force_filesystem_checkpoint=True,
                    )
                _terminate_descendant_agents(agent)
                _terminate_other_agents(agent)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="exited")
                _record_agent_activity(agent, "runner-exit", "Managed workflow runner exited via EOF")
                return 0
            except KeyboardInterrupt:
                print("\nInterrupted. Use /exit to leave prover-agent mode.")
                continue

            text = raw.strip()
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                if _is_autonomous_workflow() and history:
                    live_state = _build_live_proof_state(history, checkpoint_state)
                    live_state = _promote_live_state_to_verified(live_state)
                    _write_workflow_checkpoint(
                        history,
                        agent,
                        label="pre-exit checkpoint",
                        trigger="pre-exit",
                        force_filesystem_checkpoint=True,
                        live_state=live_state,
                    )
                _terminate_descendant_agents(agent)
                _terminate_other_agents(agent)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="exited")
                _record_agent_activity(agent, "runner-exit", "Managed workflow runner exited by command")
                _print_header()
                return 0
            if text == "/help":
                _print_runner_help()
                continue
            if text == "/status" or text.startswith("/status "):
                parts = text.split()
                if len(parts) > 1:
                    recent_limit = 5
                    if len(parts) > 2:
                        try:
                            recent_limit = max(1, int(parts[2]))
                        except ValueError:
                            print("Usage: /status [agent-id] [recent-events]")
                            continue
                    _print_agent_detail(parts[1], recent_limit=recent_limit)
                    continue
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state)
                for line in _history_status_lines(history, compaction_state, checkpoint_state, live_state):
                    print(line)
                continue
            if text == "/swarm" or text.startswith("/swarm "):
                parts = text.split()
                if len(parts) == 1:
                    _print_swarm_overview()
                    continue
                recent_limit = 5
                if len(parts) > 2:
                    try:
                        recent_limit = max(1, int(parts[2]))
                    except ValueError:
                        print("Usage: /swarm [agent-id] [recent-events]")
                        continue
                _print_agent_detail(parts[1], recent_limit=recent_limit)
                continue
            if text == "/proof-state":
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state)
                _print_live_proof_state(live_state)
                continue
            if text == "/diagnostics":
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state)
                _print_live_proof_state(live_state, section="diagnostics")
                continue
            if text == "/goals":
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state)
                _print_live_proof_state(live_state, section="goals")
                continue
            if text == "/history":
                _print_history(_all_checkpoint_entries_latest_first())
                continue
            if text.startswith("/checkpoint"):
                note = text[len("/checkpoint"):].strip()
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                entry = _write_workflow_checkpoint(
                    history,
                    agent,
                    label=note or "manual checkpoint",
                    trigger="manual",
                    note=note,
                    force_filesystem_checkpoint=True,
                    live_state=live_state,
                )
                checkpoint_state = _journal_status()
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="checkpointed")
                print(f"Saved workflow checkpoint {entry['checkpoint_id']} ({entry['label']}).")
                continue
            if text.startswith("/resume-plan"):
                parts = text.split(maxsplit=1)
                ref = parts[1].strip() if len(parts) > 1 else ""
                entry = _resolve_checkpoint_ref(ref)
                if entry is None:
                    print("Checkpoint not found. Use /history to inspect available checkpoints.")
                    continue
                history = _resume_plan_from_checkpoint(entry)
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _record_activity("resume", f"Loaded workflow plan from {entry['label']}")
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="resumed")
                print(f"Loaded workflow plan from checkpoint {entry['label']}.")
                continue
            if text.startswith("/rollback"):
                parts = text.split(maxsplit=1)
                ref = parts[1].strip() if len(parts) > 1 else ""
                if not ref:
                    print("Usage: /rollback <N>")
                    continue
                entry = _resolve_checkpoint_ref(ref)
                if entry is None:
                    print("Checkpoint not found. Use /history to inspect available checkpoints.")
                    continue
                history, message = _rollback_to_checkpoint(agent, entry)
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                post = _write_workflow_checkpoint(
                    history,
                    agent,
                    label="post-rollback checkpoint",
                    trigger="rollback",
                    note=f"Rolled back to {entry.get('label', entry.get('checkpoint_id', 'checkpoint'))}",
                    force_filesystem_checkpoint=True,
                    live_state=live_state,
                )
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _record_activity("rollback", message, checkpoint_label=str(entry.get("label", "") or "checkpoint"))
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="resumed")
                print(message)
                print(f"Recorded {post['label']} ({post['checkpoint_id']}).")
                _print_interactive_mode_header(live_state)
                continue
            if text == "/compact":
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _maybe_checkpoint_before_compaction(history, agent, live_state=live_state)
                history, compaction_state = _auto_compact_history(history, agent, force=True)
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state(history, checkpoint_state)
                live_state = _promote_live_state_to_verified(live_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="compacted" if compaction_state["compacted"] else "in-progress")
                if compaction_state["compacted"]:
                    print(
                        "Compacted the managed session history "
                        f"(~{compaction_state['rough_tokens_before']} -> ~{compaction_state['rough_tokens_after']} tokens)."
                    )
                else:
                    print("Managed session compaction skipped: not enough history to compact yet.")
                _print_interactive_mode_header(live_state)
                continue

            live_state = _build_live_proof_state(history, checkpoint_state)
            live_state = _promote_live_state_to_verified(live_state)
            _maybe_checkpoint_before_compaction(history, agent, live_state=live_state)
            history, compaction_state = _auto_compact_history(history, agent)
            previous_history = history[:]
            live_state = _build_live_proof_state(history, checkpoint_state)
            live_state = _promote_live_state_to_verified(live_state)
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="busy")
            augmented_text = _attach_live_proof_state(text, live_state)
            _set_runtime_active_skill(_effective_skill_name(live_state))
            effective_reasoning = _apply_managed_reasoning_policy(agent, live_state, autonomy_state)
            _record_managed_reasoning_policy(
                live_state,
                autonomy_state,
                effective_reasoning,
                phase="interactive",
            )
            _prepare_queue_assignment_state(autonomy_state, live_state)
            _prepare_managed_turn_state(agent, autonomy_state)
            result = _run_managed_conversation(
                agent,
                on_interrupt=lambda: _persist_live_status(
                    history,
                    compaction_state,
                    checkpoint_state,
                    live_state,
                    phase="paused",
                ),
                user_message=augmented_text,
                system_message=system_prompt,
                conversation_history=history,
                persist_user_message=text,
            )
            history = result["messages"]
            live_state = _build_live_proof_state(history, checkpoint_state)
            live_state = _promote_live_state_to_verified(live_state)
            _record_turn_activity(previous_history, history, phase="interactive")
            _maybe_write_milestone_checkpoint(previous_history, history, agent, autonomy_state, live_state=live_state)
            checkpoint_state = _journal_status()
            live_state = _build_live_proof_state(history, checkpoint_state)
            live_state = _promote_live_state_to_verified(live_state)
            _persist_live_status(history, compaction_state, checkpoint_state, live_state)
            if result.get("interrupted") and not _is_step_boundary_interrupt(result):
                _record_activity("interactive-interrupted", "Interactive agent turn interrupted by user")
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="paused")
            else:
                history, compaction_state, checkpoint_state, live_state = _drive_autonomous_followups(
                    agent,
                    system_prompt,
                    history,
                    compaction_state,
                    checkpoint_state,
                    autonomy_state,
                )
            _print_interactive_mode_header(live_state)
    finally:
        owner_id = str(getattr(agent, "session_id", "") or "")
        if owner_id:
            release_all_file_locks(owner_id=owner_id)


if __name__ == "__main__":
    raise SystemExit(main())
