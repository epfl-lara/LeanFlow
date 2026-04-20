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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.auxiliary_client import call_llm
from agent.context_compressor import ContextCompressor
from agent.model_metadata import estimate_messages_tokens_rough
from model_tools import handle_function_call
from epflemma_cli.file_locks import list_file_locks, release_all_file_locks
from epflemma_cli.skill_core import build_skill_prompt
from epflemma_cli.workflow_state import (
    append_workflow_activity,
    append_workflow_run_log,
    read_workflow_agent_inbox,
    reset_workflow_run_log,
    save_workflow_live_status,
    summarize_workflow_agents,
    terminate_workflow_agent_descendants,
    workflow_agent_detail,
)
from run_agent import AIAgent
from tools.mcp_tool import discover_mcp_tools
from tools.registry import registry

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
AUTONOMOUS_WORKFLOW_KINDS = {"autoprove", "autoformalize"}
PROJECT_SCAN_SKIP_DIRS = {".git", ".lake", ".epflemma", ".opengauss", ".gauss", "build"}


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
    kind = str(workflow_kind or _workflow_kind() or "")
    if kind == "autoprove":
        return "prove"
    if kind == "autoformalize":
        return "formalize"
    return kind


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


def _swarm_enabled() -> bool:
    return _parallel_agents() > 1 and _read_native_env("USER_APPROVED_SWARM", "0") == "1"


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
    return _read_native_env("ACTIVE_SKILL", "").strip()


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
        "active_skill": _active_skill(),
        "parallel_agents": _parallel_agents(),
        "active_file": str(live_state.get("active_file", "") or ""),
        "active_file_label": str(live_state.get("active_file_label", "") or "[unknown]"),
        "target_symbol": str(live_state.get("target_symbol", "") or "[unknown]"),
        "diagnostics": str(live_state.get("diagnostics", "") or "unavailable"),
        "goals": str(live_state.get("goals", "") or "unavailable"),
        "build_status": str(live_state.get("build_status", "") or "unknown"),
        "proof_state_message": str(live_state.get("message", "") or ""),
        "sorry_count": live_state.get("sorry_count"),
        "project_sorry_count": live_state.get("project_sorry_count"),
        "checkpoint_count": int(checkpoint_state.get("count", 0) or 0),
        "latest_checkpoint_label": str(current_checkpoint.get("label", "") or "[none]"),
        "latest_filesystem_checkpoint": str(current_checkpoint.get("linked_filesystem_checkpoint", "") or "[none]"),
        "last_compaction_reason": str((compaction_state or {}).get("reason", "[none]") or "[none]"),
        "snapshot_present": bool((compaction_state or {}).get("snapshot_text")),
        "held_locks": _held_lock_count(_runner_owner_id()),
    }
    save_workflow_live_status(payload)


def _record_activity(event_type: str, message: str, **details: Any) -> None:
    append_workflow_activity(
        event_type,
        message,
        workflow_kind=_workflow_kind(),
        workflow_command=_read_native_env("WORKFLOW_COMMAND", "[unset]"),
        active_skill=_active_skill(),
        **details,
    )


def _agent_activity_details(agent: Any) -> dict[str, Any]:
    return {
        "agent_session_id": str(getattr(agent, "session_id", "") or ""),
        "parent_agent_session_id": str(getattr(agent, "_parent_session_id", "") or ""),
        "delegate_depth": int(getattr(agent, "_delegate_depth", 0) or 0),
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


def _single_line(text: Any, limit: int = 220) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


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
    if name == "_thinking":
        _record_activity("assistant-plan", _single_line(preview, 280))
        return
    arguments = dict(args or {})
    _record_activity(
        "tool-start",
        _single_line(preview or name, 280),
        tool=name,
        args_preview=_single_line(json.dumps(arguments, ensure_ascii=False), 320) if arguments else "",
    )


def _step_callback(iteration: int, previous_tools: list[str]) -> None:
    label = f"API call #{iteration}"
    if previous_tools:
        label += f" after {', '.join(previous_tools[:4])}"
    _record_activity(
        "api-call",
        label,
        iteration=iteration,
        previous_tools=list(previous_tools or []),
    )


def _held_lock_count(owner_id: str) -> int:
    if not owner_id:
        return 0
    return len([lock for lock in list_file_locks() if str(lock.get("owner_id", "") or "") == owner_id])


def _workflow_startup_guidance(workflow_kind: str, workflow_command: str) -> str:
    workflow_kind = workflow_kind.strip().lower()
    guidance_map = {
        "prove": (
            "guided proof session",
            "Locate the target declaration or file, inspect goals with `lean-lsp`, make the smallest proof edit that compiles, and verify the result before finishing.",
        ),
        "review": (
            "proof review session",
            "Inspect the current proof state, identify correctness or style issues, and propose or apply targeted fixes only when the review clearly justifies them.",
        ),
        "checkpoint": (
            "proof checkpoint session",
            "Summarize the current proof state, stabilize the file so it builds cleanly, and leave a clear handoff for the next proving step.",
        ),
        "refactor": (
            "proof refactor session",
            "Improve the proof structure without changing theorem meaning, then re-check diagnostics and rebuild to avoid regressions.",
        ),
        "golf": (
            "proof golfing session",
            "Shorten or simplify the proof while preserving readability enough for future maintenance, then verify that the reduced proof still compiles.",
        ),
        "draft": (
            "declaration drafting session",
            "Create or refine Lean declaration skeletons, imports, and signatures so the target is ready for proving work.",
        ),
        "autoprove": (
            "autonomous proving session",
            "Drive the proving loop end-to-end, use Lean diagnostics and proof goals aggressively, and continue iterating until the target is verified and the project has no remaining build errors or `sorry`, or a concrete blocker remains. Avoid repeated `lake env lean <file>` checks; prefer lean-lsp for iteration and a focused `lake build <Module>` or final `lake build` near milestones.",
        ),
        "formalize": (
            "interactive formalization session",
            "Translate the requested mathematics into Lean declarations step by step, check the generated code frequently, and keep the user-oriented structure readable.",
        ),
        "autoformalize": (
            "autonomous formalization session",
            "Handle drafting plus proving as one workflow, iterating on declarations and proofs until the formalization is verified and the project has no remaining build errors or `sorry`, or a concrete blocker remains. Avoid repeated `lake env lean <file>` checks; prefer lean-lsp for iteration and a focused `lake build <Module>` or final `lake build` near milestones.",
        ),
    }
    label, detail = guidance_map.get(
        workflow_kind,
        (
            "managed Lean workflow session",
            "Use the staged Lean tools to complete the requested workflow and verify your work before finishing.",
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
    try:
        discover_mcp_tools()
    except Exception:
        pass

    discovered = {"diagnostics": "", "goals": ""}
    for tool_name in registry.get_all_tool_names():
        lowered = tool_name.lower()
        if "lean" not in lowered:
            continue
        if not discovered["diagnostics"] and any(token in lowered for token in ("diagnostic", "message")):
            discovered["diagnostics"] = tool_name
        if not discovered["goals"] and any(token in lowered for token in ("goal", "proof")):
            discovered["goals"] = tool_name
    return discovered


def _tool_parameter_names(tool_name: str) -> set[str]:
    entry = registry._tools.get(tool_name)  # type: ignore[attr-defined]
    if entry is None:
        return set()
    schema = getattr(entry, "schema", {}) or {}
    parameters = schema.get("parameters", {}) if isinstance(schema, Mapping) else {}
    properties = parameters.get("properties", {}) if isinstance(parameters, Mapping) else {}
    if isinstance(properties, Mapping):
        return {str(key) for key in properties.keys()}
    return set()


def _invoke_json_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    accepted = _tool_parameter_names(tool_name)
    if accepted:
        filtered = {key: value for key, value in arguments.items() if key in accepted and value not in (None, "")}
    else:
        filtered = {key: value for key, value in arguments.items() if value not in (None, "")}
    raw = handle_function_call(tool_name, filtered)
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass
    return {"raw": raw}


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
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen[:8]


def _extract_target_symbol(text: str) -> str:
    patterns = [
        r"\btheorem\s+([A-Za-z_][A-Za-z0-9_']*)",
        r"\blemma\s+([A-Za-z_][A-Za-z0-9_']*)",
        r"\bdef\s+([A-Za-z_][A-Za-z0-9_']*)",
    ]
    combined = f"{_read_native_env('WORKFLOW_COMMAND')} {text}"
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
        if any(token in lowered_line for token in blocker_tokens):
            return line[:280]
    return lines[-1][:280] if lines else ""


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
    return _extract_target_symbol(_collect_message_text(history[-16:]))


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


def _query_live_diagnostics(active_file: str) -> str:
    if not active_file:
        return "No active Lean file identified."
    tool_names = _discover_lean_mcp_tool_names()
    diagnostics_tool = tool_names.get("diagnostics", "")
    if not diagnostics_tool:
        return "lean-lsp diagnostics tool unavailable."
    payload = _invoke_json_tool(
        diagnostics_tool,
        {
            "file_path": active_file,
            "path": active_file,
        },
    )
    return _summarize_tool_payload(payload)


def _query_live_goals(active_file: str, target_symbol: str) -> str:
    if not active_file:
        return "No active Lean file identified."
    tool_names = _discover_lean_mcp_tool_names()
    goals_tool = tool_names.get("goals", "")
    if not goals_tool:
        return "lean-lsp goals tool unavailable."
    line = _find_symbol_line(active_file, target_symbol) if target_symbol else None
    payload = _invoke_json_tool(
        goals_tool,
        {
            "file_path": active_file,
            "path": active_file,
            "line": line or 1,
        },
    )
    return _summarize_tool_payload(payload)


def _build_live_proof_state(
    history: list[dict[str, Any]],
    checkpoint_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    active_file = _resolve_active_file(history, checkpoint_state)
    target_symbol = _resolve_target_symbol(history, checkpoint_state)
    diagnostics = _query_live_diagnostics(active_file)
    goals = _query_live_goals(active_file, target_symbol)
    sorry_count = _count_sorries(active_file)
    project_sorry_count, project_sorry_files = _count_project_sorries(_project_root())
    build_status = _extract_recent_build_status(history)
    blocker_summary = _extract_blocker_summary(_collect_message_text(history[-10:]))
    active_file_label = ""
    verification_hint = _recommended_verification_command(active_file)
    if active_file:
        try:
            active_file_label = str(Path(active_file).resolve().relative_to(Path(_project_root()).resolve()))
        except Exception:
            active_file_label = active_file
    body = "\n".join(
        [
            LIVE_PROOF_STATE_PREFIX,
            "",
            f"Workflow: {_workflow_kind()}",
            f"Active file: {active_file_label or '[unknown]'}",
            f"Target theorem: {target_symbol or '[unknown]'}",
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
            "Recommended verification path:",
            verification_hint or "lean-lsp diagnostics/goals first, then `lake build` when close to clean",
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
        "sorry_count": sorry_count,
        "project_sorry_count": project_sorry_count,
        "project_sorry_files": list(project_sorry_files),
        "blocker_summary": blocker_summary,
        "verification_hint": verification_hint,
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
                "Recommended verification path:",
                str(live_state.get("verification_hint", "") or "lean-lsp diagnostics/goals first, then `lake build` when close to clean"),
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
        r"\bsorry\b",
        r"\bunsolved\b",
        r"\bfailed\b",
        r"declaration uses sorry",
    )
    return any(re.search(pattern, lowered) for pattern in failure_patterns)


def _goals_still_open(goals: str) -> bool:
    lowered = (goals or "").lower()
    if not lowered or "unavailable" in lowered:
        return False
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
    diagnostics = str(live_state.get("diagnostics", "") or "")
    goals = str(live_state.get("goals", "") or "")
    build_status = str(live_state.get("build_status", "") or "")
    sorry_count = live_state.get("sorry_count")
    project_sorry_count = live_state.get("project_sorry_count")

    if isinstance(sorry_count, int) and sorry_count > 0:
        return False
    if isinstance(project_sorry_count, int) and project_sorry_count > 0:
        return False
    if _diagnostics_indicate_failure(diagnostics):
        return False
    if build_status == "build reported errors":
        return False
    if build_status == "lake build succeeded":
        return True
    if _goals_still_open(goals):
        return False
    return False


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


def _recommended_verification_command(active_file: str) -> str:
    module_name = _module_name_for_file(active_file)
    if module_name:
        return f"lake build {module_name}"
    relative_label = ""
    try:
        relative_label = str(Path(active_file).resolve().relative_to(Path(_project_root()).resolve()))
    except Exception:
        relative_label = active_file
    return f"lean-lsp diagnostics/goals on {relative_label}, then final `lake env lean {relative_label}` when close to clean"


def _run_explicit_verification_build(active_file: str = "", *, full_project: bool = False) -> tuple[bool, str]:
    module_name = _module_name_for_file(active_file)
    build_cmd = ["lake", "build"]
    build_label = "lake build"
    if not full_project and module_name:
        build_cmd = ["lake", "build", module_name]
        build_label = f"lake build {module_name}"
    elif not full_project and active_file:
        try:
            relative_file = str(Path(active_file).resolve().relative_to(Path(_project_root()).resolve()))
        except Exception:
            relative_file = active_file
        build_cmd = ["lake", "env", "lean", relative_file]
        build_label = f"lake env lean {relative_file}"
    try:
        result = subprocess.run(
            build_cmd,
            cwd=_project_root(),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return False, f"build invocation failed: {type(exc).__name__}: {exc}"

    if result.returncode == 0:
        return True, f"{build_label} succeeded"

    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    detail = stderr or stdout or f"exit {result.returncode}"
    return False, f"{build_label} reported errors: {detail[:280]}"


def _promote_live_state_to_verified(live_state: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = dict(live_state or {})
    if not normalized or not normalized.get("active_file"):
        return normalized
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
    needs_full_project_build = isinstance(project_sorry_count, int) and project_sorry_count == 0
    ok, build_status = _run_explicit_verification_build(
        str(normalized.get("active_file", "") or ""),
        full_project=needs_full_project_build,
    )
    normalized["build_status"] = build_status
    if isinstance(project_sorry_count, int) and project_sorry_count > 0:
        normalized["blocker_summary"] = (
            f"project still contains {project_sorry_count} sorry placeholder(s): "
            + ", ".join(project_sorry_files[:4])
        )
    elif not ok:
        normalized["blocker_summary"] = build_status
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
    max_turns_raw = _read_text_env("AGENT_MAX_TURNS", "90")
    try:
        max_turns = max(1, int(max_turns_raw))
    except ValueError:
        max_turns = 90

    if not model:
        raise SystemExit("epflemma-native: EPFLEMMA_NATIVE_MODEL is not configured")
    if not base_url or not api_key:
        raise SystemExit("epflemma-native: provider credentials are incomplete")

    toolset_name = _read_native_env("TOOLSET", "epflemma-native") or "epflemma-native"
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
    )
    owner_id = str(getattr(agent, "session_id", "") or "")
    if owner_id:
        os.environ["EPFLEMMA_NATIVE_RUNNER_OWNER"] = owner_id
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
    if not isinstance(result, dict):
        raise RuntimeError("Managed conversation did not return a result payload")

    if result.get("interrupted"):
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
    active_agents = sum(1 for agent in agents if str(agent.get("status", "") or "") == "active")
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
        f"Agents: {len(agents)} total / {active_agents} active",
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
            augmented_text = _attach_live_proof_state(text, live_state)
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
            if result.get("interrupted"):
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
        if workflow_kind == "autoprove":
            return "verified proof milestone", "verified-progress"
        if workflow_kind == "autoformalize":
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

    if mutated and any(token in lowered for token in ("lake build", "lean-lsp", "diagnostic", "typecheck")):
        if workflow_kind == "autoformalize":
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


def _startup_user_message(resumed_checkpoint: Mapping[str, Any] | None = None) -> str:
    startup_prompt = _read_native_env("STARTUP_PROMPT")
    workflow_command = _read_native_env("WORKFLOW_COMMAND")
    workflow_kind = _workflow_kind()
    skill_prompt = build_skill_prompt(_active_skill(), _project_root()) if _active_skill() else ""
    explicit_goal = _read_native_env("EXPLICIT_GOAL", "")
    goal_block = f"\n\nUser goal: {explicit_goal}" if explicit_goal else ""
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
            body = f"{resume_text}\n\n{startup_prompt}{goal_block}{swarm_block}"
            return f"{body}\n\n{skill_prompt}".strip() if skill_prompt else body
        if workflow_command:
            body = f"{resume_text}\n\n{_workflow_startup_guidance(workflow_kind, workflow_command)}{goal_block}{swarm_block}"
            return f"{body}\n\n{skill_prompt}".strip() if skill_prompt else body
        return resume_text
    if startup_prompt:
        body = f"{startup_prompt}{goal_block}{swarm_block}"
        return f"{body}\n\n{skill_prompt}".strip() if skill_prompt else body
    if workflow_command:
        body = f"{_workflow_startup_guidance(workflow_kind, workflow_command)}{goal_block}{swarm_block}"
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

    sections = [
        "You are the epflemma-native managed Lean workflow backend.",
        "Work inside the active Lean project only.",
        "Treat `/lean4:*` entries as workflow labels and instructions, not shell commands.",
        "Prefer Lean/LSP-first workflows and use the staged `lean-lsp` MCP server for navigation, diagnostics, and proof goals.",
        "Do not repeatedly call `lake env lean <file>` as an iteration loop. It is too slow on large imports. Use lean-lsp diagnostics/goals for most cycles, then a focused `lake build <Module>` or final `lake build` only when the file looks close to clean.",
        "Use tools aggressively, keep changes reproducible, and explain blockers clearly when a proof or formalization fails.",
        "A proof is not verified merely because `sorry` disappeared or style warnings remain. Treat a workflow as verified only after an explicit successful Lean build plus clean diagnostics and no remaining proof goals.",
        "For autonomous workflows, use project-wide verification: do not stop while the Lean project still contains build errors or any remaining `sorry` placeholders outside dependencies.",
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


def _autonomous_continuation_prompt(live_state: Mapping[str, Any], cycle_number: int) -> str:
    prompt = (
        "Continue the autonomous workflow. Do not stop yet unless the workflow is truly verified or "
        "you have a concrete blocker that still remains after another attempt.\n\n"
        "Verification requires all of the following:\n"
        "- explicit successful `lake build`\n"
        "- clean Lean diagnostics\n"
        "- no open goals\n"
        "- no remaining `sorry` in the active file\n"
        "- no remaining `sorry` anywhere else in the project outside dependencies\n\n"
        f"This is autonomous continuation cycle {cycle_number}. Use the refreshed live proof state below, "
        "make the next strongest move, and re-check the whole project before concluding."
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
        checkpoint_state = _journal_status()
        live_state = _build_live_proof_state(history, checkpoint_state)
        live_state = _promote_live_state_to_verified(live_state)
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
        augmented_text = _attach_live_proof_state(
            _autonomous_continuation_prompt(live_state, cycle),
            live_state,
        )
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
        _record_turn_activity(previous_history, history, phase="autonomous")
        _maybe_write_milestone_checkpoint(previous_history, history, agent, autonomy_state, live_state=live_state)
        _persist_live_status(history, compaction_state, checkpoint_state, live_state)
        if result.get("interrupted"):
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
        _record_activity("runner-start", "Managed workflow runner started", resumed=bool(resumed_checkpoint))

        _print_header()
        if resumed_checkpoint:
            print(f"Loaded persisted checkpoint: {resumed_checkpoint.get('label', '[unknown]')}")
            print("")

        initial_message = _attach_live_proof_state(_startup_user_message(resumed_checkpoint), live_state)
        _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="busy")
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
        if result.get("interrupted"):
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
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="exited")
                _record_activity("runner-exit", "Managed workflow runner exited via EOF")
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
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="exited")
                _record_activity("runner-exit", "Managed workflow runner exited by command")
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
            if result.get("interrupted"):
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
