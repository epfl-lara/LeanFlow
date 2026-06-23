#!/usr/bin/env python3
"""Local managed Lean workflow runner for the epflemma-native backend."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.auxiliary_client import call_llm
from agent.context_compressor import ContextCompressor
from agent.model_metadata import estimate_messages_tokens_rough
from epflemma_cli.config import load_config
from epflemma_cli.file_locks import list_file_locks, release_all_file_locks
from epflemma_cli.lean_incremental import lean_incremental_check
from epflemma_cli.lean_services import (
    actionable_diagnostic_line_numbers,
    diagnostic_items,
    diagnostics_indicate_actionable_failure,
    lean_inspect,
    lean_verify,
    probe_capabilities,
    recent_empty_search_streak,
    route_workflow_step,
)
from epflemma_cli.lean_workflow_specs import specs_for_skill
from epflemma_cli.queue_manager import (
    Classification,
    ManagerCheck,
    PrepareState,
    QueueItem,
    TheoremKey,
    TheoremQueueManager,
    classify_check,
    verification_from_mapping,
    verification_to_mapping,
)
from epflemma_cli.skill_core import load_skill
from epflemma_cli.verification_providers import (
    AUTOFORMALIZER_VERIFICATION_TASK,
    BLUEPRINT_VERIFICATION_TASK,
    is_command_verification_provider,
    is_local_verification_provider,
    resolve_verification_provider,
    run_command_verification_review,
    run_model_verification_review,
)
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
PROJECT_SCAN_SKIP_DIRS = {".artifacts", ".git", ".lake", ".epflemma", ".opengauss", ".gauss", "build"}
WORKFLOW_STEP_BOUNDARY_INTERRUPT = "[epflemma-native workflow step boundary]"
STARTUP_SKILL_CONTRACT_MAX_CHARS = 7000
MANAGER_WARNING_RETRY_LIMIT = 1
MANAGER_HARD_RETRY_LIMIT = 2
MANAGER_POST_EDIT_HARD_RETRY_LIMIT = 40
SEARCH_PROGRESS_REPEAT_NUDGE_LIMIT = 3
SEARCH_PROGRESS_TOTAL_NUDGE_LIMIT = 14
FAILED_ATTEMPT_ESCALATION_NUDGE_LIMIT = 20
FAILED_ATTEMPT_ESCALATION_NUDGE_INTERVAL = 8
ACTIVE_AGENT_STATUSES = {"active"}
LIVE_AGENT_STATUSES = {"active", "blocked", "paused", "queued"}
DEAD_AGENT_STATUSES = {"dead"}
PROOF_DECLARATION_KINDS = {"theorem", "lemma", "example"}
CONSTRUCTION_DECLARATION_KINDS = {"def", "instance", "class", "structure"}
_QUEUE_MANAGER_STATE_KEYS = TheoremQueueManager.OWNED_AUTONOMY_KEYS

# Final-sweep warning-cleanup state. Lives directly on autonomy_state because
# it is workflow-loop scoped, not per-theorem. The flag persists across
# checkpoint resume so a crashed run cannot accidentally re-trigger the
# one-shot cleanup window. The baseline carries the file's pre-cleanup
# content so the runner can restore it if the cleanup attempt introduces a
# hard failure (we promised "warning-tolerant" — never ship worse).
_FINAL_SWEEP_AUTONOMY_KEYS = frozenset(
    {
        "final_sweep_cleanup_attempted",
        "final_sweep_cleanup_turn_started",
        "final_sweep_cleanup_outcome_recorded",
        "final_sweep_baseline",
        "final_sweep_warning_summary",
    }
)


# Leaf modules extracted from native_runner (refactor Phase 2) are re-exported here for
# backwards compatibility — these names are referenced throughout this module and by tests.
# lean_parsing.py holds the pure Lean source-text/declaration parsers (plus
# LEAN_DECLARATION_PREAMBLE_RE); native_config.py holds the pure env/config readers;
# native_state.py holds the module-level mutable de-dup caches (mutated in place, so the names
# below share the very same cache objects) plus the _cache_once helper that maintains them;
# queue_edit_guard.py holds the pure queue-edit-guard helpers (guard key, protected-declaration
# inventory/diff, and source-text restoration) used by the single-queue-item edit guard;
# formalization_document_runner.py holds the cleanly-pure /formalize document-formalization
# helpers (workflow-phase predicates and the blueprint-manifest text parsers).
from epflemma_cli.formalization_document_runner import (  # noqa: E402
    _BLUEPRINT_UNRESOLVED_FIDELITY_RE,
    _autoformalizer_advisory_review_due,
    _blueprint_block_missing,
    _blueprint_bullet_block,
    _blueprint_bullet_value,
    _blueprint_checklist_item_checked,
    _blueprint_fidelity_field,
    _blueprint_fidelity_field_unresolved,
    _blueprint_first_bullet_value,
    _blueprint_import_plan_section,
    _blueprint_source_inventory_entries,
    _blueprint_value_missing,
    _document_formalization_blueprint_checklist_issues,
    _document_formalization_blueprint_waiting_for_review,
    _document_formalization_handoff_blocked_state,
    _document_formalization_has_draft_sorries,
    _document_formalization_manifest_blocks,
    _document_formalization_manifest_labels,
    _document_formalization_needs_blueprint_plan,
    _document_formalization_organization_phase_active,
    _document_formalization_organization_phase_needed,
    _document_formalization_planner_phase,
    _document_formalization_ready_for_prover_handoff,
    _document_formalization_requested,
    _document_formalization_review_prompt,
    _document_formalization_waiting_for_independent_review,
)
from epflemma_cli.lean_parsing import (  # noqa: E402
    LEAN_DECLARATION_PREAMBLE_RE,
    _declaration_entries_by_name_from_text,
    _declaration_line_index_from_text,
    _declaration_matches_target,
    _declaration_names_from_text,
    _declaration_stable_key,
    _extract_target_symbol,
    _find_assignment_marker_for_statement,
    _strip_lean_comments_and_strings,
    _text_has_any_completed_theorem_or_lemma,
    _text_has_sorry,
    _text_has_theorem_or_lemma,
    _text_has_theorem_or_lemma_without_sorry,
    _text_self_approves_document_formalization_blueprint,
    _trim_declaration_region_end,
)
from epflemma_cli.manager_verification import (  # noqa: E402
    MANAGER_INCREMENTAL_CHECK_TIMEOUT_DEFAULT_S,
    MANAGER_INCREMENTAL_PREPARE_TIMEOUT_DEFAULT_S,
    _last_verification_record,
    _manager_feedback_retry_key,
    _manager_incremental_check_timeout_s,
    _manager_incremental_prepare_timeout_s,
    _verification_outcome,
    _verification_review_system_prompt,
    _verification_task_has_aux_overrides,
)
from epflemma_cli.native_config import (  # noqa: E402
    _managed_home,
    _project_root,
    _read_int_env,
    _read_native_env,
    _read_text_env,
    _utc_now_isoformat,
    _workflow_kind,
)
from epflemma_cli.native_state import (  # noqa: E402
    _MANAGER_VERIFICATION_LOG_CACHE,
    _MANAGER_VERIFICATION_LOG_CACHE_LIMIT,
    _MANAGER_VERIFICATION_LOG_CACHE_ORDER,
    _VERIFICATION_ADVISORY_CACHE,
    _VERIFICATION_ADVISORY_CACHE_LIMIT,
    _VERIFICATION_ADVISORY_CACHE_ORDER,
    _VERIFICATION_ADVISORY_RESULT_CACHE,
    _VERIFICATION_DECISION_LOG_CACHE,
    _VERIFICATION_DECISION_LOG_CACHE_LIMIT,
    _VERIFICATION_DECISION_LOG_CACHE_ORDER,
    _cache_once,
)
from epflemma_cli.native_utils import (  # noqa: E402
    _bounded_verifier_response,
    _collect_message_text,
    _diagnostic_counts_from_messages,
    _extract_blocker_summary,
    _extract_diagnostic_line_numbers,
    _extract_diagnostics_summary,
    _extract_json_payload,
    _extract_next_steps,
    _extract_recent_build_status,
    _format_declaration_queue,
    _format_diagnostic_for_model,
    _format_turns_for_snapshot,
    _logging_config,
    _message_text,
    _normalize_blocker_summary,
    _positive_int_config,
    _relative_file_label,
    _relative_project_file_label,
    _single_line,
)
from epflemma_cli.project_prove_manager import (  # noqa: E402
    PROJECT_PROVE_MANAGER_CANDIDATE_LIMIT,
    PROJECT_PROVE_MANAGER_DECL_CONTEXT_MAX_CHARS,
    PROJECT_PROVE_MANAGER_FULL_FILE_MAX_CHARS,
    PROJECT_PROVE_MANAGER_HINT_CONTEXT_MAX_CHARS,
    PROJECT_PROVE_MANAGER_PENDING_DECL_LIMIT,
    PROJECT_PROVE_MANAGER_SELECTED_FILE_MAX_CHARS,
    _guard_project_prove_llm_order,
    _lean_import_modules,
    _module_name_for_project_path,
    _ordered_labels_from_llm_payload,
    _project_prove_bounded_excerpt,
    _project_prove_declaration_context,
    _project_prove_declaration_difficulty,
    _project_prove_dependency_graph,
    _project_prove_fallback_order,
    _project_prove_file_context_excerpt,
    _project_prove_file_difficulty,
    _project_prove_file_hint_count,
    _project_prove_header_excerpt,
    _project_prove_hint_excerpt,
    _project_prove_label_list,
    _project_prove_manager_active,
    _project_prove_manager_summary,
    _project_prove_priority_bucket,
    _project_prove_transitive_paths,
    _project_prove_worked_example_count,
)
from epflemma_cli.queue_edit_guard import (  # noqa: E402
    _queue_edit_assigned_statement_signature,
    _queue_edit_changed_protected_declarations,
    _queue_edit_guard_key,
    _queue_edit_initial_declaration_keys,
    _queue_edit_protected_declarations,
    _queue_edit_statement_signature,
    _restore_assigned_declaration_against_before_text,
    _restore_changed_protected_declarations,
)


def _workflow_display_name(workflow_kind: str | None = None) -> str:
    return str(workflow_kind or _workflow_kind() or "")


def _native_interactive_enabled() -> bool:
    raw = _read_native_env("INTERACTIVE", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _stdin_is_interactive() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _verified_workflow_should_exit_without_prompt(live_state: Mapping[str, Any]) -> bool:
    return _live_state_is_verified(live_state) and not _stdin_is_interactive()


def _interactive_prompt_loop_allowed() -> bool:
    """Whether main() may enter the blocking ``input()`` prompt loop.

    Only when stdin is a real TTY. A headless run (no TTY — e.g. ``epflemma workflow prove``
    launched from a script, pipe, or background process) has no human to answer the prompt, so
    blocking on ``input()`` would hang the process forever. Such runs must exit cleanly instead.
    """
    return _stdin_is_interactive()


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


def _additional_skill_names() -> list[str]:
    raw = (
        _read_native_env("ADDITIONAL_SKILLS", "")
        or _read_text_env("OPENGAUSS_NATIVE_ADDITIONAL_SKILLS", "")
    )
    values: list[str] = []
    for part in re.split(rf"[{re.escape(os.pathsep)}\n]+", str(raw or "")):
        normalized = part.strip()
        if normalized and normalized not in values:
            values.append(normalized)
    return values


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


def _autonomous_max_cycles() -> int:
    """Absolute ceiling on autonomous continuation cycles — a safety backstop that guarantees the
    autonomous loop terminates even if the "stalled"/"verified" stop conditions never fire. Set
    generously so it does not cut off legitimate long proofs; override via AUTONOMOUS_MAX_CYCLES.
    """
    raw = _read_native_env("AUTONOMOUS_MAX_CYCLES", "120")
    try:
        return max(8, int(raw))
    except ValueError:
        return 120


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
    except KeyboardInterrupt:
        raise
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
    resolved_phase = _workflow_phase(live_state, explicit=phase, compaction_state=compaction_state)
    if resolved_phase == "exited":
        owner_id = _runner_owner_id()
        if owner_id:
            release_all_file_locks(owner_id=owner_id)
    payload = {
        "version": 1,
        "updated_at": _utc_now_isoformat(),
        "phase": resolved_phase,
        "workflow_kind": _workflow_kind(),
        "workflow_command": _read_native_env("WORKFLOW_COMMAND", "[unset]"),
        "effective_prompt": _read_native_env("EFFECTIVE_PROMPT", _read_native_env("USER_PROMPT", _read_native_env("EXPLICIT_GOAL", ""))),
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
        "build_status": str(live_state.get("build_status", "") or ""),
        "last_verification": dict(live_state.get("last_verification", {}) or {}),
        "proof_solved": bool(live_state.get("proof_solved", False)),
        "warning_cleanup_status": str(live_state.get("warning_cleanup_status", "") or ""),
        "warning_cleanup_attempted": bool(live_state.get("warning_cleanup_attempted", False)),
        "warning_cleanup_verified": bool(live_state.get("warning_cleanup_verified", False)),
        "warning_cleanup_skipped": bool(live_state.get("warning_cleanup_skipped", False)),
        "warning_cleanup_blocked": bool(live_state.get("warning_cleanup_blocked", False)),
        "warning_cleanup_warning_count": int(live_state.get("warning_cleanup_warning_count", 0) or 0),
        "warning_cleanup_warning_summary": str(live_state.get("warning_cleanup_warning_summary", "") or ""),
        "warning_cleanup_diagnostics": str(live_state.get("warning_cleanup_diagnostics", "") or ""),
        "warning_cleanup": dict(live_state.get("warning_cleanup", {}) or {}),
        "proof_state_message": str(live_state.get("message", "") or ""),
        "sorry_count": live_state.get("sorry_count"),
        "project_sorry_count": live_state.get("project_sorry_count"),
        "project_prove_manager": bool(live_state.get("project_prove_manager", False)),
        "project_prove_file_queue": list(live_state.get("project_prove_file_queue", []) or []),
        "project_prove_completed_files": list(live_state.get("project_prove_completed_files", []) or []),
        "project_prove_plan_source": str(live_state.get("project_prove_plan_source", "") or ""),
        "project_prove_plan_reason": str(live_state.get("project_prove_plan_reason", "") or ""),
        "document_formalization_handoff": dict(live_state.get("document_formalization_handoff", {}) or {}),
        "document_formalization_proof_sorry_count": int(live_state.get("document_formalization_proof_sorry_count", 0) or 0),
        "document_formalization_construction_sorry_count": int(live_state.get("document_formalization_construction_sorry_count", 0) or 0),
        "formalization_document": _read_text_env("EPFLEMMA_FORMALIZATION_DOCUMENT_RELATIVE", ""),
        "formalization_document_kind": _read_text_env("EPFLEMMA_FORMALIZATION_DOCUMENT_KIND", ""),
        "formalization_request_kind": _read_text_env("EPFLEMMA_FORMALIZATION_REQUEST_KIND", ""),
        "formalization_request": _read_text_env("EPFLEMMA_FORMALIZATION_REQUEST_RELATIVE", ""),
        "formalization_selected_source_document": _read_text_env("EPFLEMMA_FORMALIZATION_SELECTED_SOURCE", ""),
        "formalization_context": _read_text_env("EPFLEMMA_FORMALIZATION_CONTEXT", ""),
        "formalization_blueprint": _read_text_env("EPFLEMMA_FORMALIZATION_BLUEPRINT", ""),
        "formalization_extracted_blueprint_path": _read_text_env("EPFLEMMA_FORMALIZATION_BLUEPRINT", ""),
        "formalization_target_file": _read_text_env("EPFLEMMA_FORMALIZATION_TARGET_FILE", ""),
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
        effective_prompt=_read_native_env("EFFECTIVE_PROMPT", _read_native_env("USER_PROMPT", _read_native_env("EXPLICIT_GOAL", ""))),
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
    if _document_formalization_handoff_blocked_state(live_state):
        return
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


def _queue_item_mappings_from_live_state(live_state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    current = dict(live_state or {})
    raw_queue = current.get("declaration_queue")
    if not isinstance(raw_queue, list):
        raw_queue = current.get("declaration_queue_preview")
    return [dict(item) for item in list(raw_queue or []) if isinstance(item, Mapping)]


def _queue_manager_from_state(
    autonomy_state: Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None = None,
) -> TheoremQueueManager:
    mgr = TheoremQueueManager.from_autonomy_state(dict(autonomy_state or {}))
    current = dict(live_state or {})
    active_file = str(current.get("active_file", "") or current.get("active_file_label", "") or "").strip()
    if active_file:
        mgr.set_active_file(active_file)
    queue_items = _queue_item_mappings_from_live_state(live_state)
    if queue_items:
        mgr.replace_queue(queue_items)
    return mgr


def _flush_queue_manager(autonomy_state: Mapping[str, Any] | None, mgr: TheoremQueueManager) -> None:
    if not isinstance(autonomy_state, dict):
        return
    serialized = mgr.to_autonomy_state()
    for key in _QUEUE_MANAGER_STATE_KEYS:
        autonomy_state.pop(key, None)
    autonomy_state.update(serialized)
    if _queue_invariant_checks_enabled():
        mgr.check_invariants()


def _queue_key(target_symbol: str, active_file: str) -> TheoremKey:
    return TheoremKey.make(target_symbol, active_file)


def _scoped_failed_attempt_entries(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> list[dict[str, Any]]:
    key = _queue_key(target_symbol, active_file)
    if not key.is_valid():
        return []
    mgr = _queue_manager_from_state(autonomy_state)
    entries = list(mgr.attempt_entries_for(key))
    if entries:
        return entries
    # Backward-compatible read path for checkpoints that stored relative file
    # labels while callers now pass absolute active-file paths.
    attempts = [dict(item) for item in dict(autonomy_state or {}).get("failed_attempts", []) if isinstance(item, Mapping)]
    return [
        attempt
        for attempt in attempts
        if str(attempt.get("target_symbol", "") or "").strip() == key.target_symbol
        and _same_active_file(str(attempt.get("active_file", "") or ""), active_file)
    ]


def _failed_attempt_count_for_theorem(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> int:
    key = _queue_key(target_symbol, active_file)
    if not key.is_valid():
        return 0
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
    if _queue_needs_final_file_sweep(current):
        return {"enabled": True, "effort": "high"}

    target_symbol, active_file = _queue_assignment_identity(current)
    if target_symbol and active_file:
        return {"enabled": True, "effort": "high"}

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
    threshold = _failed_attempt_reasoning_threshold()
    if (
        isinstance(autonomy_state, dict)
        and enabled
        and effort == "high"
        and failed_attempt_count >= threshold
    ):
        key = _queue_key(target_symbol, active_file)
        mgr = _queue_manager_from_state(autonomy_state)
        previous = mgr.remembered_reasoning_effort_for(key)
        if previous != "high":
            mgr.remember_reasoning_effort_for(key, "high")
            _flush_queue_manager(autonomy_state, mgr)
            _record_activity(
                "managed-reasoning-escalated",
                f"Reasoning effort escalated for {target_symbol}: {previous} -> high",
                target_symbol=target_symbol,
                active_file=active_file,
                failed_attempt_count=failed_attempt_count,
                threshold=threshold,
            )
            print(f"⬆️ Reasoning effort: {previous} → high (failed-attempt threshold reached).")


def _tool_result_counts_as_theorem_feedback(function_name: str, args: Mapping[str, Any] | None = None) -> bool:
    if function_name in {"lean_verify", "lean_incremental_check", "apply_verified_patch"}:
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
    agent._managed_step_boundary_recorded_attempt = False
    agent._managed_step_boundary_closed = False


def _disable_generic_lean_statement_guard_for_native_runner() -> None:
    # Native managed workflows have a contextual queue guard below. The generic
    # file-tool guard is intentionally broader and blocks formalization drafting.
    os.environ["EPFLEMMA_ALLOW_LEAN_STATEMENT_EDITS"] = "1"


def _agent_interrupted(agent: Any) -> bool:
    value = getattr(agent, "is_interrupted", False)
    if callable(value):
        try:
            return bool(value())
        except TypeError:
            return False
    return bool(value)


def _request_step_boundary_interrupt(agent: Any) -> None:
    try:
        setattr(agent, "_suppress_next_interrupt_log", True)
    except Exception:
        pass
    agent.interrupt(WORKFLOW_STEP_BOUNDARY_INTERRUPT)


def _print_queue_step_separator(target_symbol: str, *, accepted: bool = True) -> None:
    label = str(target_symbol or "[unknown]").strip()
    status = "verified" if accepted else "needs manager feedback"
    line = "=" * 72
    print("")
    print(line)
    print(f"Queue step boundary: {label} {status}")
    print(line)


def _manager_verify_queue_file(active_file: str) -> dict[str, Any]:
    path = str(active_file or "").strip()
    if not path:
        return {}
    try:
        result = lean_verify(target=path, cwd=_project_root(), mode="file_exact")
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}
    return {
        "ok": bool(result.ok),
        "mode": result.mode,
        "command": result.command,
        "target": result.target,
        "output": _single_line(result.output, 500),
    }


def _manager_incremental_check_queue_item(active_file: str, target_symbol: str) -> dict[str, Any]:
    path = str(active_file or "").strip()
    target = str(target_symbol or "").strip()
    if not path or not target:
        return {"ok": False, "error": "active file and target declaration are required"}
    try:
        result = lean_incremental_check(
            action="check_target",
            file_path=path,
            theorem_id=target,
            cwd=_project_root(),
            include_tactics=False,
            timeout_s=_manager_incremental_check_timeout_s(),
        )
    except Exception as exc:
        return {
            "ok": False,
            "backend": "lean_interact",
            "error": str(exc)[:500],
            "incremental": {"success": False, "error": str(exc)[:500]},
        }
    output = str(result.get("output", "") or result.get("error", "") or "")
    # Forward the structured `messages` list alongside the flattened text so
    # downstream consumers (notably `_declaration_diagnostic_feedback_reason`)
    # can locate per-line warnings/errors. The flattened `output` field is
    # capped + single-lined and lacks the `<file>:<line>:<col>:` prefix that
    # `diagnostic_items()`'s text parser depends on, so without this the
    # cleanup-feedback helper silently returns "" for `lean_interact`-style
    # warnings and the per-theorem warning-cleanup opportunity never fires.
    structured_messages = list(result.get("messages") or [])
    return {
        "ok": bool(result.get("ok", False)),
        "mode": "incremental_target",
        "backend": str(result.get("backend", "lean_interact") or "lean_interact"),
        "command": str(result.get("command", "lean_interact check_target") or "lean_interact check_target"),
        "target": str(result.get("target", target) or target),
        "output": _single_line(output, 500),
        "messages": structured_messages,
        "incremental": result,
    }


def _manager_prepare_incremental_queue_item(active_file: str, target_symbol: str) -> dict[str, Any]:
    path = str(active_file or "").strip()
    target = str(target_symbol or "").strip()
    if not path or not target:
        return {"success": False, "ok": False, "error": "active file and target declaration are required"}
    try:
        result = lean_incremental_check(
            action="prepare_file",
            file_path=path,
            theorem_id=target,
            cwd=_project_root(),
            include_tactics=False,
            timeout_s=_manager_incremental_prepare_timeout_s(),
        )
    except Exception as exc:
        return {
            "success": False,
            "ok": False,
            "backend": "lean_interact",
            "error": str(exc)[:500],
        }
    output = str(result.get("output", "") or result.get("error", "") or "")
    return {
        "success": bool(result.get("success", False)),
        "ok": bool(result.get("ok", False)),
        "backend": str(result.get("backend", "lean_interact") or "lean_interact"),
        "action": "prepare_file",
        "target": str(result.get("target", target) or target),
        "elapsed_s": result.get("elapsed_s", 0),
        "output": _single_line(output, 500),
        "cache": dict(result.get("cache") or {}),
        "error": str(result.get("error", "") or ""),
    }


def _manager_check_queue_item(active_file: str, target_symbol: str) -> tuple[dict[str, Any], str]:
    if target_symbol and active_file:
        manager_verification = _manager_incremental_check_queue_item(active_file, target_symbol)
        incremental_payload = dict(manager_verification.get("incremental") or {})
        if incremental_payload.get("success", False):
            return manager_verification, "lean_incremental_check"
    return _manager_verify_queue_file(active_file), "lean_verify"


def _verification_record_from_check(
    active_file: str,
    target_symbol: str,
    manager_check: Mapping[str, Any],
    manager_tool: str,
    *,
    full_project: bool = False,
) -> dict[str, Any]:
    check = dict(manager_check or {})
    incremental = dict(check.get("incremental") or {})
    output = str(check.get("output", "") or check.get("error", "") or incremental.get("output", "") or incremental.get("error", "") or "")
    errors, warnings, sorry_count = _diagnostic_counts_from_messages(
        output=output,
        messages=incremental.get("messages"),
    )
    if bool(incremental.get("has_errors")) and errors == 0:
        errors = 1
    if bool(incremental.get("has_sorry")) and sorry_count == 0:
        sorry_count = 1
    cache = dict(incremental.get("cache") or check.get("cache") or {})
    if "cache_hit" in cache:
        cache_label = "warm" if bool(cache.get("cache_hit")) else "cold"
    elif cache:
        cache_label = "warm"
    else:
        cache_label = ""
    elapsed = incremental.get("elapsed_s", check.get("elapsed_s", 0))
    try:
        elapsed_s = float(elapsed or 0.0)
    except (TypeError, ValueError):
        elapsed_s = 0.0
    if manager_tool == "lean_incremental_check" or str(check.get("mode", "")) == "incremental_target":
        scope = f"target:{target_symbol or check.get('target', '') or '[unknown]'}"
        tool = "lean_incremental_check"
    elif full_project:
        scope = "project"
        tool = "lean_verify"
    else:
        scope = "file"
        tool = str(manager_tool or "lean_verify")
    summary = str(check.get("command", "") or "").strip()
    if output:
        summary = f"{summary}: {_single_line(output, 220)}" if summary else _single_line(output, 220)
    return {
        "scope": scope,
        "ok": bool(check.get("ok", False)),
        "tool": tool,
        "target": str(target_symbol or check.get("target", "") or ""),
        "active_file": str(active_file or check.get("target", "") or ""),
        "cache": cache_label,
        "elapsed_s": round(elapsed_s, 3),
        "errors": errors,
        "warnings": warnings,
        "sorry": sorry_count,
        "summary": summary,
        "command": str(check.get("command", "") or ""),
    }


def _active_file_warning_summary(live_state: Mapping[str, Any] | None) -> tuple[int, str]:
    """Count style/linter warnings on the active file from the latest live state.

    Reads the structured diagnostics already attached to ``live_state`` by the
    most recent ``lean_inspect`` refresh. Returns ``(count, summary)`` where
    ``summary`` is a short multi-line string suitable for prompts and logs.
    The check is intentionally cheap — no extra Lean process is spawned here;
    we trust the live-state refresh that the workflow loop just performed.
    """
    diagnostics_text = str((live_state or {}).get("diagnostics", "") or "")
    if not diagnostics_text:
        return 0, ""
    items = diagnostic_items(diagnostics_text)
    warnings = [
        item
        for item in items
        if str(item.get("severity", "") or "").strip().lower() == "warning"
    ]
    if not warnings:
        return 0, ""
    summary_lines = []
    for item in warnings[:6]:
        line = item.get("line") if isinstance(item.get("line"), int) else None
        message = _single_line(str(item.get("message", "") or ""), 160)
        prefix = f"line {line}: " if line else ""
        summary_lines.append(f"- {prefix}{message}".rstrip())
    if len(warnings) > 6:
        summary_lines.append(f"- ...and {len(warnings) - 6} more warning(s)")
    return len(warnings), "\n".join(summary_lines)


def _capture_final_sweep_baseline(
    autonomy_state: Mapping[str, Any] | None,
    active_file: str,
) -> bool:
    """Snapshot the active file's content into autonomy_state for restore-on-fail.

    Returns False if we cannot read the file; in that case the caller should
    skip the cleanup attempt entirely (we cannot promise warning-tolerant
    rollback without a baseline).
    """
    if not isinstance(autonomy_state, dict):
        return False
    try:
        content = Path(active_file).read_text(encoding="utf-8")
    except Exception:
        return False
    autonomy_state["final_sweep_baseline"] = {
        "active_file": str(Path(active_file).resolve()),
        "content": content,
    }
    return True


def _restore_final_sweep_baseline(
    autonomy_state: Mapping[str, Any] | None,
    active_file: str,
) -> bool:
    """Rewrite the active file from the captured baseline. Returns True on success."""
    if not isinstance(autonomy_state, dict):
        return False
    baseline = dict(autonomy_state.get("final_sweep_baseline") or {})
    content = baseline.get("content")
    baseline_file = str(baseline.get("active_file", "") or "")
    if not isinstance(content, str) or not baseline_file:
        return False
    try:
        if Path(baseline_file).resolve() != Path(active_file).resolve():
            return False
        Path(active_file).write_text(content, encoding="utf-8")
    except Exception:
        return False
    return True


def _final_sweep_warning_cleanup_due(
    autonomy_state: Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None,
) -> tuple[bool, int, str]:
    """Spec ([:672](docs/product-reference.md:672)): grant exactly one whole-file
    warning-cleanup opportunity when the queue is empty and only warnings
    remain. Returns ``(due, warning_count, warning_summary)``."""
    if not isinstance(autonomy_state, dict):
        return False, 0, ""
    if bool(autonomy_state.get("final_sweep_cleanup_attempted")):
        return False, 0, ""
    current = dict(live_state or {})
    if str(current.get("declaration_scope", "") or "") != "file":
        return False, 0, ""
    if int(current.get("declaration_queue_total", 0) or 0) != 0:
        return False, 0, ""
    if not str(current.get("active_file", "") or "").strip():
        return False, 0, ""
    count, summary = _active_file_warning_summary(current)
    if count <= 0:
        return False, 0, ""
    return True, count, summary


def _with_warning_cleanup_state(
    live_state: Mapping[str, Any] | None,
    *,
    status: str,
    proof_solved: bool,
    warning_count: int = 0,
    warning_summary: str = "",
    diagnostics: str = "",
    attempted: bool | None = None,
    verified: bool | None = None,
) -> dict[str, Any]:
    """Attach the shell-visible post-prove warning-cleanup state machine."""
    normalized = dict(live_state or {})
    normalized["proof_solved"] = bool(proof_solved)
    normalized["warning_cleanup_status"] = str(status or "unknown")
    normalized["warning_cleanup_attempted"] = bool(attempted) if attempted is not None else status in {
        "pending",
        "verified",
        "blocked",
    }
    normalized["warning_cleanup_verified"] = bool(verified) if verified is not None else status == "verified"
    normalized["warning_cleanup_skipped"] = status == "skipped"
    normalized["warning_cleanup_blocked"] = status == "blocked"
    normalized["warning_cleanup_warning_count"] = int(warning_count or 0)
    normalized["warning_cleanup_warning_summary"] = str(warning_summary or "")
    normalized["warning_cleanup_diagnostics"] = str(diagnostics or "")
    normalized["warning_cleanup"] = {
        "status": normalized["warning_cleanup_status"],
        "proof_solved": normalized["proof_solved"],
        "attempted": normalized["warning_cleanup_attempted"],
        "verified": normalized["warning_cleanup_verified"],
        "skipped": normalized["warning_cleanup_skipped"],
        "blocked": normalized["warning_cleanup_blocked"],
        "warning_count": normalized["warning_cleanup_warning_count"],
        "warning_summary": normalized["warning_cleanup_warning_summary"],
        "diagnostics": normalized["warning_cleanup_diagnostics"],
    }
    return normalized


def _record_final_sweep_cleanup_outcome_once(
    autonomy_state: Mapping[str, Any] | None,
    *,
    status: str,
    active_file: str,
    warning_count: int = 0,
    diagnostics: str = "",
) -> None:
    if not isinstance(autonomy_state, dict):
        return
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"verified", "accepted", "skipped", "blocked"}:
        return
    signature = json.dumps(
        {
            "status": normalized_status,
            "active_file": str(active_file or ""),
            "warning_count": int(warning_count or 0),
            "diagnostics": _single_line(diagnostics, 220),
        },
        sort_keys=True,
    )
    if str(autonomy_state.get("final_sweep_cleanup_outcome_recorded", "") or "") == signature:
        return
    autonomy_state["final_sweep_cleanup_outcome_recorded"] = signature
    messages = {
        "verified": "Final-sweep warning cleanup verified",
        "accepted": "Final-sweep warning cleanup accepted with remaining warnings",
        "skipped": "Final-sweep warning cleanup skipped",
        "blocked": "Final-sweep warning cleanup blocked",
    }
    _record_activity(
        f"final-sweep-warning-cleanup-{normalized_status}",
        messages[normalized_status],
        active_file=active_file,
        warning_count=int(warning_count or 0),
        diagnostics=_single_line(diagnostics, 520),
    )


def _store_last_verification(
    autonomy_state: Mapping[str, Any] | None,
    record: Mapping[str, Any] | None,
) -> None:
    if not isinstance(autonomy_state, dict) or not isinstance(record, Mapping):
        return
    parsed = verification_from_mapping(record)
    if parsed is None:
        return
    mgr = _queue_manager_from_state(autonomy_state)
    mgr.record_verification(parsed)
    _flush_queue_manager(autonomy_state, mgr)


def _verification_status_text(record: Mapping[str, Any] | None) -> str:
    record = dict(record or {})
    if not record:
        return ""
    status = "passed" if bool(record.get("ok")) else "failed"
    scope = str(record.get("scope", "") or "verification")
    tool = str(record.get("tool", "") or "manager")
    detail_parts = [f"{scope} {status}", f"tool: {tool}"]
    if record.get("cache"):
        detail_parts.append(f"cache: {record.get('cache')}")
    if record.get("elapsed_s") not in (None, "", 0, 0.0):
        detail_parts.append(f"elapsed: {record.get('elapsed_s')}s")
    counts = []
    for key, label in (("errors", "errors"), ("warnings", "warnings"), ("sorry", "sorry")):
        if record.get(key) not in (None, ""):
            counts.append(f"{label}: {int(record.get(key) or 0)}")
    if counts:
        detail_parts.append(", ".join(counts))
    summary = str(record.get("summary", "") or "").strip()
    if summary:
        detail_parts.append(_single_line(summary, 220))
    return " | ".join(detail_parts)


def _recent_verification_status(
    autonomy_state: Mapping[str, Any] | None = None,
    live_state: Mapping[str, Any] | None = None,
) -> str:
    return _verification_status_text(_last_verification_record(autonomy_state, live_state))


def _record_manager_verification(
    autonomy_state: Mapping[str, Any] | None,
    active_file: str,
    target_symbol: str,
    manager_check: Mapping[str, Any],
    manager_tool: str,
    *,
    full_project: bool = False,
    log: bool = True,
) -> dict[str, Any]:
    record = _verification_record_from_check(
        active_file,
        target_symbol,
        manager_check,
        manager_tool,
        full_project=full_project,
    )
    _store_last_verification(autonomy_state, record)
    if log:
        _log_manager_verification(
            active_file,
            full_project=full_project,
            ok=bool(record.get("ok")),
            build_status=_verification_status_text(record),
            scope=str(record.get("scope", "") or ""),
            target_symbol=target_symbol,
            tool=str(record.get("tool", "") or manager_tool),
            cache=str(record.get("cache", "") or ""),
            elapsed_s=record.get("elapsed_s"),
            errors=int(record.get("errors", 0) or 0),
            warnings=int(record.get("warnings", 0) or 0),
            sorry_count=int(record.get("sorry", 0) or 0),
        )
    return record


def _json_tool_result_payload(result: str) -> dict[str, Any]:
    try:
        payload = json.loads(str(result or ""))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _disable_agent_tool_schema(agent: Any, tool_name: str) -> None:
    name = str(tool_name or "").strip()
    if not name:
        return
    tools = list(getattr(agent, "tools", []) or [])
    filtered = [
        tool
        for tool in tools
        if str(dict(tool.get("function", {}) or {}).get("name", "") or "") != name
    ]
    if len(filtered) != len(tools):
        agent.tools = filtered
    valid = set(getattr(agent, "valid_tool_names", set()) or set())
    if name in valid:
        valid.remove(name)
        agent.valid_tool_names = valid


def _record_disabled_tool_this_run(
    autonomy_state: Mapping[str, Any] | None,
    tool_name: str,
    reason: str = "",
) -> None:
    if not isinstance(autonomy_state, dict):
        return
    name = str(tool_name or "").strip()
    if not name:
        return
    mgr = _queue_manager_from_state(autonomy_state)
    mgr.disable_tool(name, _single_line(reason, 240))
    _flush_queue_manager(autonomy_state, mgr)


def _disabled_tools_summary(autonomy_state: Mapping[str, Any] | None) -> list[str]:
    entries = []
    for entry in list(dict(autonomy_state or {}).get("disabled_tools_this_run") or []):
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name", "") or "").strip()
        if not name:
            continue
        reason = str(entry.get("reason", "") or "").strip()
        entries.append(f"{name} ({reason})" if reason else name)
    return entries


def _sync_disabled_tools_from_result(agent: Any, function_name: str, result: str) -> None:
    payload = _json_tool_result_payload(result)
    if not payload:
        return
    reasons = [str(reason) for reason in list(payload.get("degraded_reasons") or []) if str(reason).strip()]
    joined = " ".join(reasons).lower()
    tool_to_disable = ""
    if function_name == "lean_auto_try" and "lean automation try disabled for this run" in joined:
        tool_to_disable = "lean_auto_try"
    if not tool_to_disable:
        return
    autonomy_state = getattr(agent, "_managed_autonomy_state", None)
    reason = next((reason for reason in reasons if "disabled for this run" in reason.lower()), reasons[0] if reasons else "")
    _record_disabled_tool_this_run(autonomy_state if isinstance(autonomy_state, dict) else None, tool_to_disable, reason)
    _disable_agent_tool_schema(agent, tool_to_disable)
    _record_activity(
        "managed-tool-disabled",
        f"Disabled {tool_to_disable} for the rest of this run",
        tool=tool_to_disable,
        reason=reason,
    )


def _latest_assistant_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role", "") or "") == "assistant":
            return str(message.get("content", "") or "").strip()
    return ""


def _final_report_claims_queue_success(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    negative_patterns = (
        r"\bstill\s+(?:blocked|failing|fails|has errors?)\b",
        r"\bnot\s+(?:solved|verified|complete|done)\b",
        r"\b(?:cannot|can't|could not|unable to)\s+(?:prove|solve|verify|finish)\b",
        r"\bverification\s+(?:failed|fails)\b",
    )
    if any(re.search(pattern, lowered) for pattern in negative_patterns):
        return False
    success_patterns = (
        r"\b(?:solved|verified|complete|completed|done)\b",
        r"\b(?:passes|succeeds|succeeded)\b",
        r"\bfile verification succeeded\b",
        r"\blake env lean\b.*\b(?:passes|succeeds|succeeded)\b",
    )
    return any(re.search(pattern, lowered) for pattern in success_patterns)


def _manager_final_report_feedback(
    target_symbol: str,
    active_file: str,
    manager_check: Mapping[str, Any],
) -> str:
    file_check_ok = bool(manager_check.get("file_check_ok", manager_check.get("ok")))
    status = "passed" if file_check_ok else "failed"
    command = str(manager_check.get("command", "") or "manager file verification")
    output = str(manager_check.get("output", "") or manager_check.get("error", "") or "").strip()
    blocker_kind = str(manager_check.get("feedback_kind", "") or "").strip()
    lines = [
        "[EPFLEMMA-NATIVE MANAGER REVIEW]",
        "The agent reported this queue item as solved, so the manager ran deterministic verification.",
        f"- declaration: {target_symbol or '[unknown]'}",
        f"- file: {active_file or '[unknown]'}",
        f"- manager file check: {status}",
        f"- check: `{command}`",
    ]
    if blocker_kind:
        lines.append(f"- blocker kind: {blocker_kind}")
    if output and blocker_kind != "warning":
        lines.append(f"- feedback: {_single_line(output, 700)}")
    elif output:
        lines.append(
            "- file check output: "
            + _single_line(output, 700)
            + " [not the blocking reason unless it points at the assigned declaration]"
        )
    if manager_check.get("local_cleanup_reason"):
        lines.append(f"- local cleanup: {_single_line(manager_check.get('local_cleanup_reason'), 700)}")
    if blocker_kind == "warning":
        retry_count = int(manager_check.get("feedback_retry_count", 0) or 0)
        retry_limit = int(manager_check.get("feedback_retry_limit", MANAGER_WARNING_RETRY_LIMIT) or MANAGER_WARNING_RETRY_LIMIT)
        lines.append(
            f"- retry policy: warning-only cleanup gets {retry_limit} focused manager cleanup opportunity; "
            f"this is opportunity {retry_count + 1}."
        )
    if manager_check.get("ok"):
        lines.append("- next step: accept this report and refresh the queue.")
    elif blocker_kind == "warning":
        lines.append(
            "- next step: fix the warning(s) in the assigned declaration context; "
            "helper declarations created for this theorem may be adjusted, but do not solve unrelated future "
            "queue items just because their `sorry` warnings appear in file output."
        )
    elif blocker_kind == "sorry":
        lines.append(
            "- next step: continue the same theorem; the assigned declaration still contains `sorry`, "
            "so solve it or report a concrete blocker."
        )
    elif blocker_kind == "error":
        lines.append(
            "- next step: continue the same theorem; fix the assigned declaration's Lean error(s) "
            "before reporting success again."
        )
    else:
        lines.append("- next step: continue the same theorem; fix the returned manager feedback before reporting success again.")
    return "\n".join(lines)


def _manager_feedback_retry_count(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    kind: str,
) -> int:
    key = _queue_key(target_symbol, active_file)
    if not key.is_valid():
        return 0
    return _queue_manager_from_state(autonomy_state).retry_count_for(key, kind)


def _increment_manager_feedback_retry(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    kind: str,
    signature: str = "",
) -> int:
    if not isinstance(autonomy_state, dict):
        return 0
    key = _queue_key(target_symbol, active_file)
    mgr = _queue_manager_from_state(autonomy_state)
    count = mgr.consume_retry_once_for(key, kind=kind, signature=signature)
    _flush_queue_manager(autonomy_state, mgr)
    return count


def _clear_manager_feedback_retries(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> None:
    if not isinstance(autonomy_state, dict):
        return
    mgr = _queue_manager_from_state(autonomy_state)
    mgr.clear_retries_for(_queue_key(target_symbol, active_file))
    _flush_queue_manager(autonomy_state, mgr)


def _manager_feedback_retry_signature(
    kind: str,
    manager_check: Mapping[str, Any] | None,
) -> str:
    check = dict(manager_check or {})
    basis = {
        "kind": str(kind or ""),
        "local_cleanup_reason": _single_line(str(check.get("local_cleanup_reason", "") or ""), 300),
        "output": _single_line(str(check.get("output", "") or check.get("error", "") or ""), 500),
        "target": str(check.get("target", "") or ""),
        "command": str(check.get("command", "") or ""),
    }
    return json.dumps(basis, sort_keys=True, ensure_ascii=False)


def _clear_all_manager_feedback_retries_except(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> None:
    if not isinstance(autonomy_state, dict):
        return
    mgr = _queue_manager_from_state(autonomy_state)
    mgr.clear_all_retries_except(_queue_key(target_symbol, active_file))
    _flush_queue_manager(autonomy_state, mgr)


def _line_in_declaration(entry: Mapping[str, Any] | None, line: Any) -> bool:
    if not isinstance(line, int) or line <= 0 or not entry:
        return False
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or start)
    return bool(start > 0 and start <= line <= max(start, end))


def _manager_check_for_feedback_kind(
    active_file: str,
    target_symbol: str,
    manager_check: Mapping[str, Any],
) -> ManagerCheck:
    entry = _find_declaration_entry(active_file, target_symbol)
    output = str(manager_check.get("output", "") or manager_check.get("error", "") or "")
    parsed = diagnostic_items(output)
    manager_verification_failed = (
        ("ok" in manager_check or "file_check_ok" in manager_check)
        and not bool(manager_check.get("ok"))
        and not bool(manager_check.get("file_check_ok"))
    )
    has_assigned_sorry = bool(entry and entry.get("has_sorry"))
    has_assigned_error = False
    has_assigned_warning = False
    has_future_evidence = False
    has_assigned_open_goals = _goals_still_open(str(manager_check.get("goals", "") or ""))

    local_cleanup = str(manager_check.get("local_cleanup_reason", "") or "").strip()
    lowered_cleanup = local_cleanup.lower()
    if lowered_cleanup:
        if "sorry" in lowered_cleanup:
            has_assigned_sorry = True
        elif any(token in lowered_cleanup for token in ("error", "unsolved", "failed")):
            has_assigned_error = True
        else:
            has_assigned_warning = True

    for item in parsed:
        severity = str(item.get("severity", "") or "").strip().lower()
        message = str(item.get("message", "") or "").strip().lower()
        line = item.get("line")
        if _line_in_declaration(entry, line):
            if severity == "error":
                has_assigned_error = True
            if "sorry" in message:
                has_assigned_sorry = True
            if severity == "warning" and not manager_verification_failed:
                has_assigned_warning = True
        elif entry and (severity in {"error", "warning"} or "sorry" in message):
            has_future_evidence = True

    if not entry and any(str(item.get("severity", "") or "").strip().lower() == "error" for item in parsed):
        has_assigned_error = True

    lowered_output = output.lower()
    if (manager_verification_failed or not entry) and any(
        token in lowered_output for token in ("error:", "unsolved goals", "type mismatch", "failed to synthesize")
    ):
        has_assigned_error = True

    return ManagerCheck(
        has_assigned_sorry=has_assigned_sorry,
        has_assigned_error=has_assigned_error,
        has_assigned_open_goals=has_assigned_open_goals,
        has_assigned_warning=has_assigned_warning,
        has_future_evidence=has_future_evidence,
        verification_failed=manager_verification_failed,
        raw_messages=(output,),
    )


def _manager_feedback_kind(
    active_file: str,
    target_symbol: str,
    manager_check: Mapping[str, Any],
) -> str:
    """Legacy string adapter for manager feedback.

    `Classification.FUTURE_ONLY` and `Classification.ACCEPT` both map to
    `""` here for backward compatibility. Consumers that need to distinguish
    those branches must call `_manager_check_for_feedback_kind` and
    `classify_check` directly.
    """
    check = _manager_check_for_feedback_kind(active_file, target_symbol, manager_check)
    classification = classify_check(check)
    if classification is Classification.HARD_BLOCKER:
        if check.has_assigned_sorry:
            return "sorry"
        return "error"
    if classification is Classification.WARNING_ONCE:
        return "warning"
    return ""


def _manager_retry_exhausted_message(
    *,
    target_symbol: str,
    active_file: str,
    kind: str,
    retry_limit: int,
    restore_result: Mapping[str, Any],
    manager_check: Mapping[str, Any],
) -> str:
    output = str(manager_check.get("output", "") or manager_check.get("error", "") or "").strip()
    restore_line = (
        "restored current declaration to the baseline `sorry` slice"
        if restore_result.get("restored")
        else f"baseline restore skipped: {restore_result.get('reason', 'not needed')}"
    )
    lines = [
        "[EPFLEMMA-NATIVE MANAGER RETRY LIMIT REACHED]",
        "",
        f"- declaration: {target_symbol or '[unknown]'}",
        f"- file: {active_file or '[unknown]'}",
        f"- blocker kind: {kind or 'unknown'}",
        f"- manager retries used: {retry_limit}",
        f"- safe-state action: {restore_line}",
    ]
    if output:
        lines.append(f"- last manager feedback: {_single_line(output, 700)}")
    lines.append(
        "- next action: continue this same queue item from the recorded failed-attempt state; "
        "do not claim it is solved until manager verification clears it."
    )
    return "\n".join(lines).strip()


def _review_agent_final_report(
    result: Mapping[str, Any],
    autonomy_state: Mapping[str, Any],
) -> dict[str, Any]:
    updated = dict(result)
    if not _single_queue_item_turn_enabled():
        return updated
    if updated.get("interrupted") or not updated.get("completed"):
        return updated
    assignment = dict(autonomy_state.get("current_queue_assignment") or {})
    target_symbol = str(assignment.get("target_symbol", "") or "").strip()
    active_file = str(assignment.get("active_file", "") or "").strip()
    if not target_symbol or not active_file:
        return updated
    messages = list(updated.get("messages") or [])
    final_text = str(updated.get("final_response", "") or "").strip() or _latest_assistant_content(messages)
    if not _final_report_claims_queue_success(final_text):
        return updated

    manager_check, manager_tool = _manager_check_queue_item(active_file, target_symbol)
    file_check_ok = bool(manager_check.get("ok"))
    manager_check = dict(manager_check)
    manager_check["file_check_ok"] = file_check_ok
    manager_check["manager_tool"] = manager_tool
    verification_record = _record_manager_verification(
        autonomy_state,
        active_file,
        target_symbol,
        manager_check,
        manager_tool,
    )
    manager_check["last_verification"] = verification_record
    if bool(manager_check.get("ok")):
        # Keep final-report cleanup policy aligned with post-patch checks:
        # only the targeted manager check can grant the focused cleanup turn.
        cleanup_reason = _declaration_diagnostic_feedback_reason(
            active_file,
            target_symbol,
            str(manager_check.get("output", "") or ""),
            str(manager_check.get("error", "") or ""),
            structured_items=manager_check.get("messages") or (),
        )
        if cleanup_reason:
            manager_check["ok"] = False
            manager_check["local_cleanup_reason"] = cleanup_reason
            manager_check["diagnostics"] = _single_line(
                str(manager_check.get("output", "") or manager_check.get("error", "") or ""),
                700,
            )
    feedback_kind = _manager_feedback_kind(active_file, target_symbol, manager_check)
    if feedback_kind:
        manager_check["feedback_kind"] = feedback_kind
    retry_count = 0
    retry_limit = 0
    if not bool(manager_check.get("ok")):
        if feedback_kind == "warning":
            retry_limit = MANAGER_WARNING_RETRY_LIMIT
        elif feedback_kind in {"error", "sorry"}:
            retry_limit = MANAGER_HARD_RETRY_LIMIT
        if retry_limit:
            retry_count = _manager_feedback_retry_count(
                autonomy_state,
                target_symbol=target_symbol,
                active_file=active_file,
                kind=feedback_kind,
            )
            manager_check["feedback_retry_count"] = retry_count
            manager_check["feedback_retry_limit"] = retry_limit
            if feedback_kind == "warning" and retry_count >= retry_limit:
                manager_check["ok"] = True
                manager_check["accepted_after_warning_retry_limit"] = True
                manager_check["acceptance_note"] = (
                    "accepted after one warning-only cleanup opportunity; remaining warnings are not allowed to stall the queue"
                )
            elif feedback_kind in {"error", "sorry"} and retry_count >= retry_limit:
                restore_result = _restore_queue_assignment_to_baseline_sorry(autonomy_state, {})
                if restore_result.get("restored"):
                    restore_result = dict(restore_result)
                    restore_result["reason"] = (
                        "reverted current declaration to its baseline `sorry` slice after manager retry exhaustion"
                    )
                manager_check["retry_exhausted"] = True
                manager_check["restore"] = restore_result
                messages.append(
                    {
                        "role": "user",
                        "content": _manager_retry_exhausted_message(
                            target_symbol=target_symbol,
                            active_file=active_file,
                            kind=feedback_kind,
                            retry_limit=retry_limit,
                            restore_result=restore_result,
                            manager_check=manager_check,
                        ),
                    }
                )
                updated["messages"] = messages
                updated["completed"] = False
                updated["exit_reason"] = "manager_retry_exhausted"
                updated["error"] = "Manager retry limit reached for unresolved theorem feedback"
    ok = bool(manager_check.get("ok"))
    if ok:
        _clear_manager_feedback_retries(
            autonomy_state,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    file_label = _relative_file_label(active_file) or active_file
    _record_activity(
        "manager-final-report-review",
        f"Manager reviewed agent final report for {target_symbol}: {'accepted' if ok else 'rejected'}",
        target_symbol=target_symbol,
        active_file=active_file,
        active_file_label=file_label,
        manager_verification=manager_check,
        accepted=ok,
    )
    print("")
    print(f"🔎 Manager review of agent final report for {target_symbol}: {'accepted' if ok else 'needs work'}")
    if manager_check.get("command"):
        print(f"   check: {manager_check.get('command')}")
    detail = str(manager_check.get("output", "") or manager_check.get("error", "") or "").strip()
    if detail:
        print(f"   result: {_single_line(detail, 520)}")
    if feedback_kind:
        print(f"   blocker kind: {feedback_kind}")
    if manager_check.get("local_cleanup_reason"):
        print(f"   cleanup: {_single_line(manager_check.get('local_cleanup_reason'), 520)}")
    if ok:
        if manager_check.get("accepted_after_warning_retry_limit"):
            print("   note: warning-only cleanup opportunity already used; allowing the queue to advance.")
        print(f"✅ Workflow step verified for {target_symbol}; refreshing Lean state and selecting the next target...")
        _print_queue_step_separator(target_symbol)
    elif manager_check.get("retry_exhausted"):
        print(
            f"⚠️  Manager retry limit reached for {target_symbol}; "
            "restored safe state when possible and recorded this as unresolved."
        )
        _print_queue_step_separator(target_symbol, accepted=False)
    else:
        if retry_limit:
            _increment_manager_feedback_retry(
                autonomy_state,
                target_symbol=target_symbol,
                active_file=active_file,
                kind=feedback_kind,
                signature=_manager_feedback_retry_signature(feedback_kind, manager_check),
            )
        print(f"↻ Agent reported {target_symbol} as solved, but manager verification still failed; continuing this queue item.")
        _print_queue_step_separator(target_symbol, accepted=False)
        messages.append(
            {
                "role": "user",
                "content": _manager_final_report_feedback(target_symbol, active_file, manager_check),
            }
        )
        updated["messages"] = messages
    updated["manager_final_report_review"] = manager_check
    return updated


def _managed_tool_result_succeeded(result: str) -> bool:
    text = str(result or "").strip()
    if not text:
        return True
    try:
        payload = json.loads(text)
    except Exception:
        return True
    if not isinstance(payload, Mapping):
        return True
    if "success" in payload:
        return bool(payload.get("success"))
    if "error" in payload and payload.get("error"):
        return False
    if "ok" in payload:
        return bool(payload.get("ok"))
    return True


def _search_progress_assignment(agent: Any) -> tuple[str, str]:
    autonomy_state = getattr(agent, "_managed_autonomy_state", {}) or {}
    assignment = dict(dict(autonomy_state or {}).get("current_queue_assignment") or {})
    target_symbol = str(assignment.get("target_symbol", "") or "").strip()
    active_file = str(assignment.get("active_file", "") or "").strip()
    return target_symbol, active_file


def _normalized_search_query(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _append_post_tool_result_message(agent: Any, message: str) -> None:
    text = str(message or "").strip()
    if not text:
        return
    previous = str(getattr(agent, "_post_tool_result_appendix", "") or "").strip()
    try:
        setattr(agent, "_post_tool_result_appendix", f"{previous}\n\n{text}".strip() if previous else text)
    except Exception:
        pass


FORMALIZATION_HANDOFF_FEEDBACK_TOOLS = {
    "patch",
    "write_file",
    "apply_verified_patch",
    "lean_verify",
}


def _formalization_lean_edit_paths(function_name: str, args: Mapping[str, Any] | None) -> list[Path]:
    if _workflow_kind() != "formalize" or not _document_formalization_requested():
        return []
    root = Path(_project_root()).expanduser().resolve()
    target_path = _document_formalization_target_path()
    target_dir = None
    if target_path is not None:
        try:
            target_dir = target_path.resolve().parent
        except Exception:
            target_dir = None
    paths: list[Path] = []
    for path in _tool_edit_paths(function_name, args):
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except Exception:
            continue
        if resolved.suffix != ".lean":
            continue
        if target_path is not None:
            try:
                if resolved == target_path.resolve():
                    if resolved not in paths:
                        paths.append(resolved)
                    continue
            except Exception:
                pass
        if target_dir is not None:
            try:
                resolved.relative_to(target_dir)
                if resolved not in paths:
                    paths.append(resolved)
                continue
            except Exception:
                pass
        generated_paths = {item.resolve() for item in _formalization_generated_lean_paths(str(target_path or ""))}
        if resolved in generated_paths:
            if resolved not in paths:
                paths.append(resolved)
    return paths


def _formalization_raw_lean_check_paths(edit_paths: Sequence[Path]) -> list[Path]:
    target_path = _document_formalization_target_path()
    paths: list[Path] = []

    def _add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.resolve()
        except Exception:
            return
        if resolved.suffix == ".lean" and resolved.is_file() and resolved not in paths:
            paths.append(resolved)

    _add(target_path)
    for path in edit_paths:
        _add(path)
    return paths


def _check_formalization_raw_lean_edit_result(
    agent: Any,
    function_name: str,
    args: Mapping[str, Any] | None,
) -> bool:
    if function_name not in {"patch", "write_file"}:
        return False
    edit_paths = _formalization_lean_edit_paths(function_name, args)
    if not edit_paths:
        return False
    autonomy_state = getattr(agent, "_managed_autonomy_state", None)
    checked: list[dict[str, Any]] = []
    failed: tuple[Path, dict[str, Any], dict[str, Any]] | None = None
    for path in _formalization_raw_lean_check_paths(edit_paths):
        manager_check = _manager_verify_queue_file(str(path))
        record = _record_manager_verification(
            autonomy_state if isinstance(autonomy_state, dict) else None,
            str(path),
            "",
            manager_check,
            "lean_verify",
        )
        label = _relative_file_label(str(path)) or str(path)
        checked.append({"path": label, "ok": bool(record.get("ok", False)), "summary": record.get("summary", "")})
        if not bool(record.get("ok", False)) and failed is None:
            failed = (path, manager_check, record)
            break

    _record_activity(
        "formalization-lean-edit-checked",
        (
            "Generated Lean edit passed immediate verification"
            if failed is None
            else "Generated Lean edit failed immediate verification"
        ),
        tool=function_name,
        checked=checked,
    )
    if failed is None:
        if not bool(getattr(agent, "quiet_mode", False)):
            labels = ", ".join(str(item.get("path", "")) for item in checked if item.get("path"))
            print(f"\n✅ Formalization Lean edit verified{f' ({labels})' if labels else ''}.")
        return True

    path, manager_check, record = failed
    output = str(manager_check.get("output", "") or manager_check.get("error", "") or "").strip()
    lines = [
        "[EPFLEMMA FORMALIZATION LEAN CHECK FAILED]",
        "- status: a raw generated Lean edit failed immediate file verification",
        f"- file: {_relative_file_label(str(path)) or str(path)}",
        f"- verification: {_verification_status_text(record) or 'failed'}",
        "- next action: fix the generated Lean elaboration before updating blueprint status, requesting review, or reporting completion",
    ]
    if output:
        lines.append(f"- feedback: {_single_line(output, 700)}")
    _append_post_tool_result_message(agent, "\n".join(lines))
    if not bool(getattr(agent, "quiet_mode", False)):
        print(
            "\n↻ Formalization Lean edit failed immediate verification; "
            "feeding Lean diagnostics back into the drafting turn."
        )
    return True


def _formalization_handoff_feedback_text(live_state: Mapping[str, Any] | None) -> str:
    if not _document_formalization_handoff_blocked_state(live_state):
        return ""
    handoff = dict((live_state or {}).get("document_formalization_handoff", {}) or {})
    issues = [_single_line(issue, 360) for issue in handoff.get("issues", []) or [] if str(issue or "").strip()]
    if not issues:
        summary = _single_line(str(handoff.get("summary", "") or ""), 360)
        issues = [summary] if summary else []
    if not issues:
        return ""
    lines = [
        "[EPFLEMMA FORMALIZATION VERIFIER BLOCK]",
        "The formalization verifier blocked handoff. Treat these findings as the next required correction task, not as advisory log text.",
        "- do not mark the blueprint proof-ready",
        "- do not report formalization as ready",
        "- do not start theorem proving or fill theorem/lemma/example proofs",
        "- edit the Lean statements, blueprint coverage/scope fields, or source-aware doc comments to address the findings",
        "- if the only remaining blocker is independent source/statement approval, stop and report that instead of self-approving it",
        "",
        "Verifier findings to address now:",
    ]
    lines.extend(f"- {issue}" for issue in issues[:8])
    if len(issues) > 8:
        lines.append(f"- plus {len(issues) - 8} more verifier finding(s) in workflow status")
    return "\n".join(lines)


def _maybe_append_formalization_handoff_feedback(
    agent: Any,
    *,
    function_name: str,
    live_state: Mapping[str, Any] | None = None,
) -> None:
    if function_name not in FORMALIZATION_HANDOFF_FEEDBACK_TOOLS:
        return
    if _workflow_kind() != "formalize" or not _document_formalization_requested():
        return
    current = dict(live_state or {})
    if not current:
        autonomy_state = getattr(agent, "_managed_autonomy_state", {}) or {}
        try:
            current = _build_live_proof_state_compat(
                list(getattr(agent, "_session_messages", []) or []),
                autonomy_state=autonomy_state if isinstance(autonomy_state, dict) else None,
            )
        except Exception:
            current = {}
    message = _formalization_handoff_feedback_text(current)
    if not message:
        return
    _append_post_tool_result_message(agent, message)
    handoff = dict(current.get("document_formalization_handoff", {}) or {})
    _record_activity(
        "formalization-verifier-feedback-injected",
        "Injected formalization verifier BLOCK findings into the next model turn",
        tool=function_name,
        active_file=str(current.get("active_file_label", "") or current.get("active_file", "") or ""),
        issues=[_single_line(issue, 240) for issue in handoff.get("issues", []) or []][:8],
    )
    if not bool(getattr(agent, "quiet_mode", False)):
        print("\n↻ Formalization verifier BLOCK; feeding findings back into the drafting turn.")


def _reset_search_progress(agent: Any) -> None:
    autonomy_state = getattr(agent, "_managed_autonomy_state", None)
    if isinstance(autonomy_state, dict):
        autonomy_state.pop("search_progress", None)


def _note_non_search_tool_progress(agent: Any, function_name: str) -> None:
    reset_tools = {
        "patch",
        "write_file",
        "apply_verified_patch",
        "lean_incremental_check",
        "lean_verify",
        "lean_multi_attempt",
        "terminal",
    }
    if function_name in reset_tools:
        _reset_search_progress(agent)
        return
    if function_name not in {"lean_proof_context", "lean_auto_search", "lean_reasoning_help", "lean_inspect"}:
        return
    autonomy_state = getattr(agent, "_managed_autonomy_state", None)
    if not isinstance(autonomy_state, dict):
        return
    tracker = dict(autonomy_state.get("search_progress") or {})
    if not tracker:
        return
    used_tools = dict(tracker.get("used_tools") or {})
    used_tools[function_name] = int(used_tools.get(function_name, 0) or 0) + 1
    tracker["used_tools"] = used_tools
    autonomy_state["search_progress"] = tracker


def _track_search_progress(agent: Any, args: Mapping[str, Any] | None, result: str) -> None:
    autonomy_state = getattr(agent, "_managed_autonomy_state", None)
    if not isinstance(autonomy_state, dict):
        return
    target_symbol, active_file = _search_progress_assignment(agent)
    if not target_symbol or not active_file:
        return
    payload = _json_tool_result_payload(result)
    query = str(payload.get("query", "") or dict(args or {}).get("query", "") or dict(args or {}).get("q", "") or "")
    normalized_query = _normalized_search_query(query)
    if not normalized_query:
        return
    tracker = dict(autonomy_state.get("search_progress") or {})
    if (
        str(tracker.get("target_symbol", "") or "") != target_symbol
        or str(tracker.get("active_file", "") or "") != active_file
    ):
        tracker = {
            "target_symbol": target_symbol,
            "active_file": active_file,
            "search_count": 0,
            "same_query_streak": 0,
            "last_query": "",
            "unique_queries": [],
            "used_tools": {},
        }
    search_count = int(tracker.get("search_count", 0) or 0) + 1
    same_query_streak = (
        int(tracker.get("same_query_streak", 0) or 0) + 1
        if str(tracker.get("last_query", "") or "") == normalized_query
        else 1
    )
    unique_queries = [str(item) for item in list(tracker.get("unique_queries") or []) if str(item)]
    if normalized_query not in unique_queries:
        unique_queries.append(normalized_query)
    results = payload.get("results")
    result_count = len(results) if isinstance(results, list) else 0
    tracker.update(
        {
            "search_count": search_count,
            "same_query_streak": same_query_streak,
            "last_query": normalized_query,
            "last_query_display": query[:240],
            "unique_queries": unique_queries[-8:],
            "last_result_count": result_count,
        }
    )
    autonomy_state["search_progress"] = tracker

    nudge_reason = ""
    if same_query_streak >= SEARCH_PROGRESS_REPEAT_NUDGE_LIMIT:
        nudge_reason = f"same lean_search query repeated {same_query_streak} times"
    elif search_count >= SEARCH_PROGRESS_TOTAL_NUDGE_LIMIT:
        nudge_reason = f"{search_count} lean_search calls on this declaration since the last edit/check"
    if not nudge_reason:
        return
    if int(tracker.get("last_nudged_search_count", 0) or 0) == search_count:
        return
    tracker["last_nudged_search_count"] = search_count
    autonomy_state["search_progress"] = tracker
    used_tools = dict(tracker.get("used_tools") or {})
    context_hint = (
        "- `lean_proof_context` has already been used; prefer a concrete proof draft/check, `lean_decompose_helpers` for a sublemma split, or `lean_reasoning_help`."
        if int(used_tools.get("lean_proof_context", 0) or 0) > 0
        else "- if you still need context, call `lean_proof_context` once; if the proof needs intermediate invariants, use `lean_decompose_helpers`; otherwise draft and check a proof."
    )
    _append_post_tool_result_message(
        agent,
        "\n".join(
            [
                "[EPFLEMMA-NATIVE SEARCH PROGRESS NUDGE]",
                f"- declaration: {target_symbol}",
                f"- file: {_relative_file_label(active_file) or active_file}",
                f"- observed: {nudge_reason}",
                f"- latest query: {query[:240] or '[unknown]'}",
                f"- latest result count: {result_count}",
                "- search providers are responding; this is a route-progress nudge, not a search outage.",
                "- do not call `lean_search` again in this turn unless the query strategy materially changes.",
                context_hint,
                "- next useful action should be a concrete proof edit, `lean_incremental_check(check_target)` on a draft, `lean_multi_attempt`, `lean_decompose_helpers` for a helper-lemma split, or `lean_reasoning_help`.",
            ]
        ),
    )
    _record_activity(
        "search-progress-nudge",
        f"Repeated search-only progress nudge for {target_symbol}",
        target_symbol=target_symbol,
        active_file=active_file,
        search_count=search_count,
        same_query_streak=same_query_streak,
        latest_query=query,
        result_count=result_count,
    )


def _should_emit_failed_attempt_escalation_nudge(attempt_number: int) -> bool:
    if attempt_number < FAILED_ATTEMPT_ESCALATION_NUDGE_LIMIT:
        return False
    interval = max(1, FAILED_ATTEMPT_ESCALATION_NUDGE_INTERVAL)
    return (attempt_number - FAILED_ATTEMPT_ESCALATION_NUDGE_LIMIT) % interval == 0


def _terminal_command_may_edit(command: str) -> bool:
    text = str(command or "")
    if not text.strip():
        return False
    patterns = (
        r"\bsed\s+(?:-[A-Za-z]*i|[^;&|]*\s-i\b)",
        r"\bperl\s+-[A-Za-z]*i\b",
        r"\bpython(?:3)?\b.*\b(?:write_text|open\s*\(|Path\s*\().*(?:write|append|unlink)",
        r"\b(?:rm|mv|cp|touch|chmod|chown|truncate)\b",
        r"\btee\b",
        r"(?:^|\s)(?:\d)?>>?(?!&)",
    )
    return any(re.search(pattern, text, flags=re.DOTALL) for pattern in patterns)


def _queue_edit_snapshot_required(function_name: str, args: Mapping[str, Any] | None) -> bool:
    if function_name in {"patch", "write_file", "apply_verified_patch"}:
        return True
    if function_name == "terminal":
        return _terminal_command_may_edit(str(dict(args or {}).get("command", "") or ""))
    return False


def _resolve_project_path(raw_path: str) -> Path | None:
    path_text = str(raw_path or "").strip()
    if not path_text:
        return None
    try:
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = Path(_project_root()) / path
        return path.resolve()
    except Exception:
        return None


def _document_formalization_target_path() -> Path | None:
    if not _document_formalization_requested():
        return None
    return _resolve_project_path(_read_text_env("EPFLEMMA_FORMALIZATION_TARGET_FILE", ""))


def _tool_edit_paths(function_name: str, args: Mapping[str, Any] | None) -> list[Path]:
    data = dict(args or {})
    raw_paths: list[str] = []
    if function_name in {"write_file", "apply_verified_patch"}:
        raw_paths.append(str(data.get("path", "") or ""))
    elif function_name == "patch":
        mode = str(data.get("mode", "replace") or "replace")
        if mode == "replace":
            raw_paths.append(str(data.get("path", "") or ""))
        elif mode == "patch":
            patch_text = str(data.get("patch", "") or "")
            raw_paths.extend(
                match.group(1).strip()
                for match in re.finditer(
                    r"^\*\*\* (?:Add|Update|Delete) File: (.+)$",
                    patch_text,
                    flags=re.MULTILINE,
                )
            )
    resolved = [_resolve_project_path(raw) for raw in raw_paths if raw]
    return [path for path in resolved if path is not None]


def _tool_proposed_edit_text(function_name: str, args: Mapping[str, Any] | None) -> str:
    data = dict(args or {})
    if function_name == "write_file":
        return str(data.get("content", "") or "")
    if function_name == "patch":
        mode = str(data.get("mode", "replace") or "replace")
        if mode == "replace":
            return str(data.get("new_string", "") or "")
        if mode == "patch":
            return str(data.get("patch", "") or "")
    if function_name == "apply_verified_patch":
        return str(data.get("patch", "") or "")
    return ""


def _tool_edit_removes_sorry(function_name: str, args: Mapping[str, Any] | None) -> bool:
    data = dict(args or {})
    if function_name in {"patch", "apply_verified_patch"}:
        old_text = str(data.get("old_string", "") or "")
        new_text = str(data.get("new_string", "") or data.get("patch", "") or "")
        return _text_has_sorry(old_text) and not _text_has_sorry(new_text)
    return False


def _document_formalization_pre_tool_guard(
    agent: Any,
    function_name: str,
    args: Mapping[str, Any] | None,
) -> str | None:
    if function_name not in {"patch", "write_file", "apply_verified_patch"}:
        return None
    target_path = _document_formalization_target_path()
    if target_path is None:
        return None
    formalization_lean_paths = _formalization_lean_edit_paths(function_name, args)
    blueprint = _read_text_env("EPFLEMMA_FORMALIZATION_BLUEPRINT", "").strip()
    blueprint_path = _resolve_project_path(blueprint) if blueprint else None
    try:
        touches_blueprint = bool(
            blueprint_path
            and any(path.resolve() == blueprint_path for path in _tool_edit_paths(function_name, args))
        )
    except Exception:
        touches_blueprint = False
    proposed = _tool_proposed_edit_text(function_name, args)
    if touches_blueprint and _text_self_approves_document_formalization_blueprint(proposed):
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Document formalization blueprint approval must be written by the independent "
                    "statement/source verifier pass, not by the drafting formalizer. Leave "
                    "`Statement verification status` entries pending and keep review/proof-ready "
                    "checklist items unchecked until a verifier PASS stamps approval."
                ),
                "blueprint": blueprint,
                "next_required_step": "independent_statement_source_verification",
            },
            ensure_ascii=False,
        )
    try:
        touches_target = any(path.resolve() == target_path for path in _tool_edit_paths(function_name, args))
    except Exception:
        touches_target = False
    if not touches_target and not formalization_lean_paths:
        return None
    if _document_formalization_needs_blueprint_plan():
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Document formalization must update the planner blueprint before editing the target Lean file. "
                    "Replace the preflight `_pending_` entries with planned declaration names, dependencies, split "
                    "lemmas, statement-fidelity reviews, and source proof/prover notes, then retry the Lean draft. "
                    "The Lean draft must also carry compact source proof/prover notes in theorem/lemma doc comments."
                ),
                "target": _relative_project_file_label(target_path),
                "blueprint": blueprint,
            },
            ensure_ascii=False,
        )
    proposed = _tool_proposed_edit_text(function_name, args)
    if (
        proposed
        and _document_formalization_planner_phase(agent)
        and (
            (
                _document_formalization_needs_planner_draft(str(target_path))
                and _text_has_theorem_or_lemma_without_sorry(proposed)
            )
            or (
                not _document_formalization_needs_planner_draft(str(target_path))
                and _text_has_any_completed_theorem_or_lemma(proposed)
                and not _text_has_sorry(proposed)
            )
            or _tool_edit_removes_sorry(function_name, args)
        )
    ):
        return json.dumps(
            {
                "success": False,
                "error": (
                    "The document formalization planner draft must leave theorem/lemma proofs as `sorry`. "
                    "Put source proof strategy and prover hints in the nearby Blueprint.md and in compact Lean "
                    "doc comments above source theorem/lemma declarations, draft stable statements with `by sorry`, "
                    "then request independent statement/source verification. A later prove workflow must be "
                    "started explicitly by the user."
                ),
                "target": _relative_project_file_label(target_path),
                "blueprint": blueprint,
            },
            ensure_ascii=False,
        )
    return None


def _managed_pre_tool_call(agent: Any, function_name: str, args: Mapping[str, Any] | None) -> str | None:
    formalization_guard = _document_formalization_pre_tool_guard(agent, function_name, args)
    if formalization_guard:
        return formalization_guard
    if not _single_queue_item_turn_enabled() or _agent_interrupted(agent):
        return None
    if not _queue_edit_snapshot_required(function_name, args):
        return None
    autonomy_state = getattr(agent, "_managed_autonomy_state", {}) or {}
    assignment = dict(autonomy_state.get("current_queue_assignment") or {})
    target_symbol = str(assignment.get("target_symbol", "") or "").strip()
    active_file = str(assignment.get("active_file", "") or "").strip()
    if not target_symbol or not active_file:
        return None
    if function_name == "terminal":
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Managed theorem queues do not allow terminal-based file edits. "
                    "Use `patch` for Lean edits; helper lemmas for the assigned theorem are allowed, "
                    "but pre-existing future queue declarations must not be edited."
                ),
            },
            ensure_ascii=False,
        )
    try:
        before_text = Path(active_file).read_text(encoding="utf-8")
    except Exception:
        return None
    entry = _find_declaration_entry(active_file, target_symbol)
    if not entry:
        return None
    guard_key = _queue_edit_guard_key(target_symbol, active_file)
    guard_state = dict(getattr(agent, "_managed_queue_edit_guard_state", {}) or {})
    if guard_state.get("key") != guard_key:
        assigned_statement_signature = ""
        if _queue_edit_protect_assigned_statement(agent, before_text, target_symbol, active_file):
            assigned_statement_signature = _queue_edit_assigned_statement_signature(before_text, target_symbol)
        guard_state = {
            "key": guard_key,
            "target_symbol": target_symbol,
            "active_file": active_file,
            "assigned_statement_signature": assigned_statement_signature,
            "protected_declarations": _queue_edit_protected_declarations(before_text, target_symbol),
        }
        setattr(agent, "_managed_queue_edit_guard_state", guard_state)
    setattr(
        agent,
        "_managed_queue_edit_snapshot",
        {
            "target_symbol": target_symbol,
            "active_file": active_file,
            "before_text": before_text,
            "start": int(entry.get("line", 0) or 0),
            "end": int(entry.get("end_line", 0) or 0),
            "guard_key": guard_key,
            "assigned_statement_signature": str(guard_state.get("assigned_statement_signature", "") or ""),
            "protected_declarations": guard_state.get("protected_declarations") or (),
        },
    )
    return None


def _document_formalization_source_declaration_names() -> set[str]:
    manifest_blocks = _document_formalization_manifest_blocks()
    if not manifest_blocks:
        return set()
    blueprint_path = _read_text_env("EPFLEMMA_FORMALIZATION_BLUEPRINT", "").strip()
    if not blueprint_path:
        return set()
    try:
        blueprint_text = Path(blueprint_path).read_text(encoding="utf-8")
    except Exception:
        return set()
    entries = _blueprint_source_inventory_entries(blueprint_text)
    names: set[str] = set()
    for block in manifest_blocks:
        entry = entries.get(str(block.get("label", "") or "").strip(), "")
        if not entry:
            continue
        planned = _blueprint_first_bullet_value(
            entry,
            (
                "Planned Lean declarations",
                "Planned Lean declaration",
                "Formal names",
                "Formal name",
                "Lean declarations",
                "Lean declaration",
            ),
        )
        names.update(_lean_decl_names_from_planned_value(planned))
    return names


def _queue_edit_protect_assigned_statement(
    agent: Any,
    before_text: str,
    target_symbol: str,
    active_file: str,
) -> bool:
    target = str(target_symbol or "").strip()
    short_target = target.split(".")[-1]
    if not target:
        return False
    if _document_formalization_requested():
        source_names = _document_formalization_source_declaration_names()
        return bool(source_names) and (target in source_names or short_target in source_names)
    for entry in _declaration_line_index_from_text(before_text):
        if not _declaration_matches_target(entry, target):
            continue
        key = _declaration_stable_key(entry)
        if key is None:
            return False
        return key in _queue_edit_initial_declaration_keys(agent, active_file, before_text)
    return False


def _restore_out_of_scope_queue_edit(agent: Any, function_name: str) -> str:
    if function_name not in {"patch", "write_file", "apply_verified_patch"}:
        return ""
    snapshot = dict(getattr(agent, "_managed_queue_edit_snapshot", {}) or {})
    try:
        delattr(agent, "_managed_queue_edit_snapshot")
    except Exception:
        pass
    active_file = str(snapshot.get("active_file", "") or "")
    target_symbol = str(snapshot.get("target_symbol", "") or "")
    before_text = str(snapshot.get("before_text", "") or "")
    start = int(snapshot.get("start", 0) or 0)
    end = int(snapshot.get("end", 0) or 0)
    if "protected_declarations" in snapshot:
        protected_declarations = list(snapshot.get("protected_declarations") or ())
    else:
        protected_declarations = _queue_edit_protected_declarations(before_text, target_symbol)
    if not active_file or not target_symbol or not before_text or start <= 0 or end < start:
        return ""
    path = Path(active_file)
    try:
        current_text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    if current_text == before_text:
        return ""
    current_entry = _find_declaration_entry(active_file, target_symbol)
    if not current_entry:
        try:
            path.write_text(before_text, encoding="utf-8")
        except Exception:
            return ""
        return (
            "[EPFLEMMA-NATIVE QUEUE EDIT GUARD]\n"
            f"The `{function_name}` edit removed or obscured the assigned declaration `{target_symbol}`. "
            "The manager restored the file to its pre-tool state. Keep the assigned declaration present; "
            "helper lemmas may be added without removing or renaming it."
        )
    current_slice = str(current_entry.get("text", "") or "").strip()
    if not current_slice:
        return ""
    assigned_statement_signature = str(snapshot.get("assigned_statement_signature", "") or "")
    if assigned_statement_signature:
        current_signature = _queue_edit_statement_signature(current_entry)
        if current_signature != assigned_statement_signature:
            try:
                path.write_text(before_text, encoding="utf-8")
            except Exception:
                return ""
            return (
                "[EPFLEMMA-NATIVE QUEUE STATEMENT GUARD]\n"
                f"The `{function_name}` edit changed the protected source statement for `{target_symbol}`. "
                "The manager restored the file to its pre-tool state. During the prover queue, keep the "
                "assigned original/source theorem statement fixed; helper lemmas created by the model may "
                "be added and revised."
            )
    changed_protected = _queue_edit_changed_protected_declarations(protected_declarations, current_text)
    if not changed_protected:
        return ""
    restored_text = _restore_changed_protected_declarations(current_text, changed_protected)
    restore_reason = "changed protected declarations outside the assigned declaration"
    if restored_text is None:
        restored_text = _restore_assigned_declaration_against_before_text(
            before_text,
            current_slice,
            start=start,
            end=end,
        )
        restore_reason = "removed or obscured protected declarations outside the assigned declaration"
    if restored_text == current_text:
        return ""
    try:
        path.write_text(restored_text, encoding="utf-8")
    except Exception:
        return ""
    changed_names = ", ".join(
        str(dict(item.get("protected") or {}).get("name", "") or "")
        for item in changed_protected[:4]
        if str(dict(item.get("protected") or {}).get("name", "") or "").strip()
    )
    detail = f" ({changed_names})" if changed_names else ""
    return (
        "[EPFLEMMA-NATIVE QUEUE EDIT GUARD]\n"
        f"The `{function_name}` edit {restore_reason}{detail} while solving `{target_symbol}`. "
        "The manager preserved the current assigned declaration body and restored those protected declarations. "
        "Adding and iterating on new helper lemmas for this theorem is allowed; do not edit pre-existing "
        "future queue items in this theorem turn."
    )


def _finish_queue_step_boundary(
    agent: Any,
    *,
    pending_target: str,
    pending_file: str,
    verification_tool: str,
    manager_verification: Mapping[str, Any] | None = None,
) -> None:
    live_state: dict[str, Any] = {}
    item: dict[str, Any] = {}
    still_blocked = False
    refresh_error = ""
    attempt_recorded = False
    should_yield = True
    manager_check = dict(manager_verification or {})
    cleanup_feedback_reason = ""
    feedback_kind = ""
    warning_retry_accepted = False
    hard_retry_exhausted = False
    hard_retry_limit = 0
    hard_retry_count = 0
    attempt_number = 0
    restore_result: dict[str, Any] = {}
    manager_feedback_reason = ""
    verification_base_tool = str(verification_tool or "").split("+", 1)[0]
    post_edit_verification = verification_base_tool in {"patch", "write_file", "apply_verified_patch"}
    try:
        autonomy_state = getattr(agent, "_managed_autonomy_state", None)
        if manager_check:
            verification_record = _record_manager_verification(
                autonomy_state if isinstance(autonomy_state, dict) else None,
                pending_file,
                pending_target,
                manager_check,
                "lean_incremental_check"
                if str(manager_check.get("mode", "") or "") == "incremental_target"
                else verification_tool,
            )
            manager_feedback_reason = (
                str(manager_check.get("output", "") or manager_check.get("error", "") or "").strip()
                or _verification_status_text(verification_record)
            )
        live_state = _build_live_proof_state_compat(
            list(getattr(agent, "_session_messages", []) or []),
            autonomy_state=autonomy_state if isinstance(autonomy_state, dict) else None,
        )
        item = dict(live_state.get("current_queue_item") or {})
        same_assignment = _queue_assignment_transition(
            {
                "current_queue_assignment": {
                    "target_symbol": pending_target,
                    "active_file": pending_file,
                }
            },
            live_state,
        ) is None and bool(item)
        # Spec: the target-level check is the authoritative gate for queue
        # progress. Only consider warnings reported by the manager check
        # itself; do not let file-wide `lean_inspect` style warnings (which
        # the targeted check intentionally ignores) override its verdict.
        # Otherwise the printed `🔎 Manager verification ... warnings: 0`
        # line and the runner's actual decision contradict each other.
        # Pass structured `messages` so the helper can locate
        # `lean_incremental_check`-style warnings, which lack the
        # `<file>:<line>:<col>:` prefix the text fallback regex needs.
        cleanup_feedback_reason = _declaration_diagnostic_feedback_reason(
            pending_file,
            pending_target,
            str(manager_check.get("output", "") or ""),
            str(manager_check.get("error", "") or ""),
            structured_items=manager_check.get("messages") or (),
        )
        if cleanup_feedback_reason:
            feedback_kind = _manager_feedback_kind(
                pending_file,
                pending_target,
                {**manager_check, "local_cleanup_reason": cleanup_feedback_reason},
            )
        if not feedback_kind and same_assignment:
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
                entry = _find_declaration_entry(pending_file, pending_target)
                feedback_kind = "sorry" if entry and entry.get("has_sorry") else "error"
        elif feedback_kind in {"error", "sorry"}:
            still_blocked = True
        elif feedback_kind == "warning":
            retry_count = _manager_feedback_retry_count(
                getattr(agent, "_managed_autonomy_state", {}) or {},
                target_symbol=pending_target,
                active_file=pending_file,
                kind=feedback_kind,
            )
            manager_check["feedback_kind"] = feedback_kind
            manager_check["feedback_retry_count"] = retry_count
            manager_check["feedback_retry_limit"] = MANAGER_WARNING_RETRY_LIMIT
            if retry_count >= MANAGER_WARNING_RETRY_LIMIT:
                warning_retry_accepted = True
                manager_check["accepted_after_warning_retry_limit"] = True
                manager_check["acceptance_note"] = (
                    "accepted after one warning-only cleanup opportunity; unrelated warnings cannot stall the theorem queue"
                )
                cleanup_feedback_reason = ""
                feedback_kind = ""
                if isinstance(autonomy_state, dict):
                    _clear_manager_feedback_retries(
                        autonomy_state,
                        target_symbol=pending_target,
                        active_file=pending_file,
                    )
            else:
                if isinstance(autonomy_state, dict):
                    _increment_manager_feedback_retry(
                        autonomy_state,
                        target_symbol=pending_target,
                        active_file=pending_file,
                        kind=feedback_kind,
                        signature=_manager_feedback_retry_signature(feedback_kind, manager_check),
                    )
        if still_blocked:
            cleanup_feedback_reason = ""
        if still_blocked:
            if feedback_kind in {"error", "sorry"} and post_edit_verification and isinstance(autonomy_state, dict):
                hard_retry_limit = MANAGER_POST_EDIT_HARD_RETRY_LIMIT
                hard_retry_count = _manager_feedback_retry_count(
                    autonomy_state,
                    target_symbol=pending_target,
                    active_file=pending_file,
                    kind=feedback_kind,
                )
                manager_check["feedback_kind"] = feedback_kind
                manager_check["feedback_retry_count"] = hard_retry_count
                manager_check["feedback_retry_limit"] = hard_retry_limit
                if hard_retry_count >= hard_retry_limit:
                    restore_result = _restore_queue_assignment_to_baseline_sorry(autonomy_state, live_state)
                    if restore_result.get("restored"):
                        restore_result = dict(restore_result)
                        restore_result["reason"] = (
                            "reverted current declaration to its baseline `sorry` slice after manager retry exhaustion"
                        )
                    manager_check["retry_exhausted"] = True
                    manager_check["restore"] = restore_result
                    hard_retry_exhausted = True
                    still_blocked = False
                else:
                    hard_retry_count = _increment_manager_feedback_retry(
                        autonomy_state,
                        target_symbol=pending_target,
                        active_file=pending_file,
                        kind=feedback_kind,
                        signature=_manager_feedback_retry_signature(feedback_kind, manager_check),
                    )
                    manager_check["feedback_retry_count"] = hard_retry_count
            if hard_retry_exhausted:
                cleanup_feedback_reason = ""
        if still_blocked:
            if isinstance(autonomy_state, dict):
                _remember_failed_attempt(
                    autonomy_state,
                    live_state,
                    cycle_number=int(autonomy_state.get("current_cycle", 0) or 0),
                    reason=manager_feedback_reason,
                )
                attempt_recorded = True
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
                    verification_tool=verification_tool,
                )
    except Exception as exc:
        refresh_error = str(exc)[:500]
    finally:
        continue_same_turn = bool(still_blocked or cleanup_feedback_reason)
        should_yield = bool(refresh_error or not continue_same_turn)
        try:
            setattr(agent, "_managed_step_boundary_recorded_attempt", attempt_recorded)
        except Exception:
            pass
        _record_activity(
            (
                "queue-theorem-feedback"
                if still_blocked
                else "queue-theorem-cleanup-feedback"
                if cleanup_feedback_reason
                else "queue-theorem-retry-exhausted"
                if hard_retry_exhausted
                else "queue-step-boundary"
            ),
            (
                f"Continuing same theorem after failed verification feedback for {pending_target}"
                if still_blocked
                else f"Continuing same theorem for local warning cleanup on {pending_target}"
                if cleanup_feedback_reason
                else f"Manager retry limit reached for {pending_target}"
                if hard_retry_exhausted
                else f"Yielding after verification feedback for {pending_target}"
            ),
            queue_item=item,
            target_symbol=pending_target,
            active_file=pending_file,
            reasons=list(item.get("reasons", []) or []),
            verification_tool=verification_tool,
            manager_verification=manager_check,
            still_blocked=still_blocked,
            cleanup_feedback_reason=cleanup_feedback_reason,
            feedback_kind=feedback_kind,
            warning_retry_accepted=warning_retry_accepted,
            hard_retry_exhausted=hard_retry_exhausted,
            hard_retry_count=hard_retry_count,
            hard_retry_limit=hard_retry_limit,
            restore=restore_result,
            yielded=should_yield,
            refresh_error=refresh_error,
        )
        if continue_same_turn:
            feedback_lines = [
                "[EPFLEMMA-NATIVE THEOREM FEEDBACK]",
                f"- declaration: {pending_target}",
                (
                    "- status: still blocked; continue the same theorem turn"
                    if still_blocked
                    else "- status: proof cleared, but this assigned declaration still has warning-only cleanup; fix only this declaration"
                ),
                f"- verification tool: {verification_tool}",
            ]
            if cleanup_feedback_reason:
                feedback_lines.append(f"- local cleanup: {cleanup_feedback_reason}")
                feedback_lines.append(
                    "- cleanup opportunity: this consumes the assigned declaration's one focused warning-cleanup opportunity; if the next manager check still sees only warnings here, the queue advances"
                )
                feedback_lines.append(
                    "- expected effort: attempt at least one safe edit before bailing — low-risk fixes include "
                    "removing a `try { ... }`/`<;> try { ... }` whose tactic is reported as never executed, "
                    "deleting an `all_goals X` reported as doing nothing, dropping a `<;>` reported as "
                    "unnecessary sequencing, or removing a tactic the linter says is unused. Do NOT touch "
                    "the theorem statement, the proof's overall structure, or any line not flagged."
                )
                feedback_lines.append(
                    "- bail clause: if you have read the assigned declaration and identified that no safe "
                    "cleanup remains, you may emit a final report — the warnings will be accepted and the "
                    "queue advances. The bail clause requires that you actually inspected the declaration first."
                )
                feedback_lines.append(
                    "- queue boundary: do not edit future queued declarations; stop after this cleanup or after the manager cleanup opportunity"
                )
            if manager_check:
                feedback_lines.append(
                    f"- manager file check: {'ok' if manager_check.get('ok') else 'failed'}"
                    + (f" via `{manager_check.get('command')}`" if manager_check.get("command") else "")
                )
                output = str(manager_check.get("output", "") or manager_check.get("error", "") or "").strip()
                if output:
                    feedback_lines.append(f"- feedback: {_single_line(output, 500)}")
            if still_blocked and _should_emit_failed_attempt_escalation_nudge(attempt_number):
                feedback_lines.extend(
                    [
                        "",
                        "[EPFLEMMA-NATIVE FAILED ATTEMPT NUDGE]",
                        f"- observed: {attempt_number} verified failed edits/checks on this same declaration",
                        "- manager checks are working; this is a proof-strategy escalation nudge, not a backend failure",
                        "- avoid another broad rewrite of the same proof shape unless you can state the concrete new invariant it fixes",
                        "- next useful action should be `lean_incremental_check(action=feedback, include_tactics=true)` for local goal state, `lean_multi_attempt` for small tactic variants, `lean_decompose_helpers` when the proof needs sublemmas/intermediate invariants, or `lean_reasoning_help` for an external proof plan",
                    ]
                )
                _record_activity(
                    "failed-attempt-escalation-nudge",
                    f"Escalation nudge after {attempt_number} failed attempts for {pending_target}",
                    target_symbol=pending_target,
                    active_file=pending_file,
                    attempt=attempt_number,
                    verification_tool=verification_tool,
                )
            try:
                setattr(agent, "_post_tool_result_appendix", "\n".join(feedback_lines))
            except Exception:
                pass
            if not bool(getattr(agent, "quiet_mode", False)):
                if cleanup_feedback_reason and not still_blocked:
                    retry_count = int(manager_check.get("feedback_retry_count", 0) or 0)
                    retry_limit = int(
                        manager_check.get("feedback_retry_limit", MANAGER_WARNING_RETRY_LIMIT)
                        or MANAGER_WARNING_RETRY_LIMIT
                    )
                    used = max(1, retry_count + 1)
                    print(
                        f"\n🟡 Cleanup feedback for {pending_target}: "
                        f"{_single_line(cleanup_feedback_reason, 220)}"
                    )
                    print(
                        f"   focused warning-cleanup opportunity granted "
                        f"({used}/{retry_limit}); agent gets one more edit on this declaration"
                    )
                elif still_blocked:
                    print(
                        f"\n↻ Verification did not clear {pending_target}; "
                        "feeding manager note back into the same theorem turn..."
                    )
            agent._managed_pending_theorem_feedback = None
            return

        if not bool(getattr(agent, "quiet_mode", False)):
            if refresh_error:
                print(
                    f"\n↻ Verification feedback received for {pending_target}; "
                    "refreshing workflow state before continuing..."
                )
            elif still_blocked:
                print(
                    f"\n↻ Verification did not clear {pending_target}; "
                    "refreshing Lean state before retrying..."
                )
            elif warning_retry_accepted:
                print(
                    f"\n✅ Workflow step verified for {pending_target}; "
                    "warning-only cleanup opportunity already used, selecting the next target..."
                )
                _print_queue_step_separator(pending_target)
            elif hard_retry_exhausted:
                print(
                    f"\n⚠️  Manager retry limit reached for {pending_target}; "
                    "restored safe state when possible and yielding this theorem turn."
                )
                _print_queue_step_separator(pending_target, accepted=False)
            elif manager_check and not bool(manager_check.get("ok")):
                print(
                    f"\n↻ Queue item cleared for {pending_target}; "
                    "file verification still has remaining blockers, refreshing Lean state..."
                )
            else:
                print(
                    f"\n✅ Workflow step verified for {pending_target}; "
                    "refreshing Lean state and selecting the next target..."
                )
                _print_queue_step_separator(pending_target)
        agent._managed_pending_theorem_feedback = None
        try:
            delattr(agent, "_post_tool_result_appendix")
        except Exception:
            pass
        try:
            setattr(agent, "_managed_step_boundary_closed", True)
        except Exception:
            pass
        _request_step_boundary_interrupt(agent)


def _handle_managed_tool_result(
    agent: Any,
    function_name: str,
    args: Mapping[str, Any] | None,
    _result: str,
) -> None:
    _sync_disabled_tools_from_result(agent, function_name, _result)
    if not _single_queue_item_turn_enabled() or _agent_interrupted(agent):
        return
    if function_name == "lean_search":
        _track_search_progress(agent, args, _result)
    else:
        _note_non_search_tool_progress(agent, function_name)

    if (
        function_name == "lean_verify"
        and _workflow_kind() == "formalize"
        and _document_formalization_requested()
    ):
        payload = _json_tool_result_payload(_result)
        if payload:
            autonomy_state = getattr(agent, "_managed_autonomy_state", None)
            target_path = _document_formalization_target_path()
            active_file = str(target_path or _read_native_env("ACTIVE_FILE", "") or payload.get("target", "") or "")
            mode = str(dict(args or {}).get("mode", "") or payload.get("mode", "") or "").strip().lower()
            full_project = mode == "project"
            record = _record_manager_verification(
                autonomy_state if isinstance(autonomy_state, dict) else None,
                active_file,
                "",
                payload,
                "lean_verify",
                full_project=full_project,
                log=False,
            )
            record["scope"] = "project" if full_project else str(record.get("scope", "") or "file")
            record["summary"] = str(payload.get("command", "") or payload.get("output", "") or record.get("summary", ""))
            _store_last_verification(autonomy_state if isinstance(autonomy_state, dict) else None, record)
        _maybe_append_formalization_handoff_feedback(agent, function_name=function_name)

    if function_name == "apply_verified_patch":
        managed_autonomy = getattr(agent, "_managed_autonomy_state", {}) or {}
        baseline = dict(managed_autonomy).get("current_queue_assignment", {})
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
            live_state = _build_live_proof_state_compat(
                list(getattr(agent, "_session_messages", []) or []),
                autonomy_state=managed_autonomy if isinstance(managed_autonomy, dict) else None,
            )
            live_target, live_file = _queue_assignment_identity(live_state)
            target_symbol = target_symbol or live_target
            active_file = active_file or live_file
            _maybe_append_formalization_handoff_feedback(
                agent,
                function_name=function_name,
                live_state=live_state,
            )
        if target_symbol and active_file:
            agent._managed_pending_theorem_feedback = {
                "target_symbol": target_symbol,
                "active_file": active_file,
            }
        else:
            _maybe_append_formalization_handoff_feedback(agent, function_name=function_name)

    if function_name in {"patch", "write_file"}:
        if not _managed_tool_result_succeeded(_result):
            return
        if _check_formalization_raw_lean_edit_result(agent, function_name, args):
            _maybe_append_formalization_handoff_feedback(agent, function_name=function_name)
            return
        managed_autonomy = getattr(agent, "_managed_autonomy_state", {}) or {}
        baseline = dict(managed_autonomy).get("current_queue_assignment", {})
        target_symbol = str(dict(baseline or {}).get("target_symbol", "") or "").strip()
        active_file = str(dict(baseline or {}).get("active_file", "") or "").strip()
        live_state_for_feedback: Mapping[str, Any] | None = None
        if not target_symbol or not active_file:
            live_state_for_feedback = _build_live_proof_state_compat(
                list(getattr(agent, "_session_messages", []) or []),
                autonomy_state=managed_autonomy if isinstance(managed_autonomy, dict) else None,
            )
            target_symbol, active_file = _queue_assignment_identity(live_state_for_feedback)
        if target_symbol and active_file:
            agent._managed_pending_theorem_feedback = {
                "target_symbol": target_symbol,
                "active_file": active_file,
            }
            manager_verification, manager_tool = _manager_check_queue_item(active_file, target_symbol)
            verification_tool = f"{function_name}+{manager_tool}"
            _finish_queue_step_boundary(
                agent,
                pending_target=target_symbol,
                pending_file=active_file,
                verification_tool=verification_tool,
                manager_verification=manager_verification,
            )
        _maybe_append_formalization_handoff_feedback(
            agent,
            function_name=function_name,
            live_state=live_state_for_feedback,
        )
        return

    pending = dict(getattr(agent, "_managed_pending_theorem_feedback", None) or {})
    pending_target = str(pending.get("target_symbol", "") or "").strip()
    pending_file = str(pending.get("active_file", "") or "").strip()
    if not _tool_result_counts_as_theorem_feedback(function_name, args):
        return
    if (not pending_target or not pending_file) and not bool(getattr(agent, "_managed_step_boundary_closed", False)):
        baseline = dict(getattr(agent, "_managed_autonomy_state", {}) or {}).get("current_queue_assignment", {})
        pending_target = str(dict(baseline or {}).get("target_symbol", "") or "").strip()
        pending_file = str(dict(baseline or {}).get("active_file", "") or "").strip()
    if not pending_target or not pending_file:
        return

    manager_verification: dict[str, Any] | None = None
    if function_name == "lean_incremental_check":
        payload = _json_tool_result_payload(_result)
        if str(payload.get("action", "") or "") in {"check_target", "feedback"}:
            manager_verification = payload
    elif function_name == "lean_verify":
        payload = _json_tool_result_payload(_result)
        if payload:
            manager_verification = payload

    _finish_queue_step_boundary(
        agent,
        pending_target=pending_target,
        pending_file=pending_file,
        verification_tool=function_name,
        manager_verification=manager_verification,
    )
    _maybe_append_formalization_handoff_feedback(agent, function_name=function_name)


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
    _MANAGER_VERIFICATION_LOG_CACHE.clear()
    _MANAGER_VERIFICATION_LOG_CACHE_ORDER.clear()
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
            "Load the native proving contract from the active skill/spec, begin with `lean_capabilities` and `lean_inspect`, use `lean_search` before guessing; the live queue, route decision, and verification gate below are the state for this turn.",
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
            "Load the native formalization contract from the active skill/spec, begin with `lean_capabilities` and `lean_inspect`, and use `lean_search` before redrafting blindly; the live queue, route decision, and verification gate below are the state for this turn.",
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
    document_block = _formalization_document_startup_block()
    if workflow_kind == "formalize" and document_block:
        guidance += f"\n\n{document_block}"
    if _swarm_enabled():
        agent_count = _parallel_agents()
        guidance += (
            "\n\n"
            f"User-approved swarm mode is enabled for this workflow ({agent_count} agents total).\n"
            "You may use delegate_task because the user explicitly requested multi-agent execution.\n"
            "If you delegate, assign concrete Lean goals, avoid duplicate file ownership, and keep one verifier path focused on final compilation."
        )
    return guidance


def _formalization_document_startup_block() -> str:
    document = _read_text_env("EPFLEMMA_FORMALIZATION_DOCUMENT_RELATIVE", "").strip()
    if not document:
        return ""
    kind = _read_text_env("EPFLEMMA_FORMALIZATION_DOCUMENT_KIND", "").strip() or "document"
    request_kind = _read_text_env("EPFLEMMA_FORMALIZATION_REQUEST_KIND", "").strip()
    request_relative = _read_text_env("EPFLEMMA_FORMALIZATION_REQUEST_RELATIVE", "").strip()
    target = _read_text_env("EPFLEMMA_FORMALIZATION_TARGET_FILE", "").strip()
    context = _read_text_env("EPFLEMMA_FORMALIZATION_CONTEXT", "").strip()
    blueprint = _read_text_env("EPFLEMMA_FORMALIZATION_BLUEPRINT", "").strip()
    lines = [
        "Document formalization source:",
        f"- document: {document}",
        f"- kind: {kind}",
    ]
    if request_relative and (request_relative != document or request_kind == "directory"):
        label = request_kind or "request"
        lines.append(f"- original input: {request_relative} ({label})")
    if target:
        lines.append(f"- target Lean file: {target}")
    if context:
        lines.append(f"- planner context: {context}")
    if blueprint:
        lines.append(f"- planner blueprint: {blueprint}")
    lines.extend(
        [
            "",
            "Start in planner mode: read the document context, inspect the source document, create/update the blueprint, "
            "draft well-scoped Lean declarations with `sorry` proofs for nontrivial theorem/lemma statements, "
            "put compact source proof/prover notes in Lean doc comments above source theorem/lemma declarations, "
            "then stop at the statement/source verification gate. Do not prove theorem/lemma skeletons in the formalizer. "
            "After a review workflow approves or corrects the blueprint and statements, the normal proof queue can eliminate the resulting `sorry` placeholders.",
        ]
    )
    return "\n".join(lines)


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


def _snapshot_metadata() -> dict[str, Any]:
    return {
        "workflow_kind": _workflow_kind(),
        "workflow_command": _read_native_env("WORKFLOW_COMMAND", "[unset]"),
        "effective_prompt": _read_native_env("EFFECTIVE_PROMPT", _read_native_env("USER_PROMPT", _read_native_env("EXPLICIT_GOAL", ""))),
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
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name for name in dirnames
                if name not in PROJECT_SCAN_SKIP_DIRS
            ]
            base = Path(dirpath)
            for filename in filenames:
                if filename.endswith(".lean"):
                    paths.append(base / filename)
    except OSError:
        return []
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


def _workflow_command_has_explicit_lean_file() -> bool:
    return bool(_extract_active_files(_read_native_env("WORKFLOW_COMMAND")))


def _project_prove_manager_requested() -> bool:
    return _workflow_kind() == "prove" and not _workflow_command_has_explicit_lean_file()


def _set_project_prove_manager_active(value: bool) -> None:
    return None


def _set_native_active_file(file_label: str) -> None:
    normalized = str(file_label or "").strip()
    os.environ["EPFLEMMA_NATIVE_ACTIVE_FILE"] = normalized


def _formalization_manifest_payload() -> dict[str, Any]:
    manifest = _read_text_env("EPFLEMMA_FORMALIZATION_MANIFEST", "").strip()
    if not manifest:
        return {}
    try:
        payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _formalization_generated_lean_paths(active_file: str = "") -> list[Path]:
    root = Path(_project_root()).expanduser().resolve()
    paths: list[Path] = []

    def _add(candidate: str | os.PathLike[str] | None) -> None:
        raw = str(candidate or "").strip()
        if not raw:
            return
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except Exception:
            return
        if resolved.suffix == ".lean" and resolved.is_file() and resolved not in paths:
            paths.append(resolved)

    _add(active_file)
    payload = _formalization_manifest_payload()
    _add(payload.get("target_lean_relative"))
    _add(payload.get("target_lean_path"))

    directory_seeds = [path.parent for path in paths if path.name == "Main.lean"]
    for seed in list(directory_seeds):
        if not seed.is_dir():
            continue
        for path in sorted(seed.rglob("*.lean"), key=lambda item: str(item.relative_to(seed)).lower()):
            _add(path)
        parent_module_file = seed.with_suffix(".lean")
        _add(parent_module_file)

    return paths


def _topologically_order_project_paths(paths: Sequence[Path], root: Path) -> list[Path]:
    unique_paths: list[Path] = []
    for path in paths:
        try:
            resolved = path.resolve()
        except Exception:
            continue
        if resolved not in unique_paths:
            unique_paths.append(resolved)
    if len(unique_paths) <= 1:
        return unique_paths
    module_to_path = {
        module: path
        for path in unique_paths
        for module in [_module_name_for_project_path(path, root)]
        if module
    }
    imports_by_path, _imported_by_path, _modules = _project_prove_dependency_graph(unique_paths, module_to_path)
    path_set = {path.resolve() for path in unique_paths}
    ordered: list[Path] = []
    visiting: set[Path] = set()
    visited: set[Path] = set()

    def _visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in visited:
            return
        if resolved in visiting:
            return
        visiting.add(resolved)
        for dependency in sorted(imports_by_path.get(resolved, set()) & path_set, key=lambda item: str(item)):
            _visit(dependency)
        visiting.discard(resolved)
        visited.add(resolved)
        ordered.append(resolved)

    for path in sorted(unique_paths, key=lambda item: str(item)):
        _visit(path)
    return ordered


def _formalization_generated_prove_scope(active_file: str = "") -> list[str]:
    root = Path(_project_root()).expanduser().resolve()
    paths = _topologically_order_project_paths(_formalization_generated_lean_paths(active_file), root)
    labels: list[str] = []
    for path in paths:
        label = _relative_project_file_label(path, root)
        if label and label not in labels:
            labels.append(label)
    return labels


def _prove_file_scope_ordered_paths(project_root: str | os.PathLike[str] | None = None) -> list[Path]:
    raw = (
        _read_text_env("EPFLEMMA_PROVE_FILE_SCOPE", "")
        or _read_text_env("OPENGAUSS_PROVE_FILE_SCOPE", "")
        or _read_text_env("GAUSS_PROVE_FILE_SCOPE", "")
    ).strip()
    if not raw:
        return []
    root = Path(project_root or _project_root()).expanduser().resolve()
    parts: list[str] = []
    parsed = _extract_json_payload(raw)
    if isinstance(parsed, list):
        parts = [str(item or "") for item in parsed]
    else:
        parts = [part for part in re.split(rf"[{re.escape(os.pathsep)}\n]+", raw) if part.strip()]
    scope: list[Path] = []
    for part in parts:
        value = str(part or "").strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except Exception:
            continue
        if resolved.suffix == ".lean" and resolved.is_file() and resolved not in scope:
            scope.append(resolved)
    return scope


def _prove_file_scope_paths(project_root: str | os.PathLike[str] | None = None) -> set[Path]:
    return set(_prove_file_scope_ordered_paths(project_root))


def _collect_project_prove_file_candidates(project_root: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    root = Path(project_root or _project_root())
    if not root.is_dir():
        return []
    lean_files = _project_lean_files(str(root))
    scope_order = _prove_file_scope_ordered_paths(root)
    if scope_order:
        project_lean_files = {path.resolve() for path in lean_files}
        lean_files = [path for path in scope_order if path.resolve() in project_lean_files]
    module_to_path = {
        module: path.resolve()
        for path in lean_files
        for module in [_module_name_for_project_path(path, root)]
        if module
    }
    imports_by_path, imported_by_path, import_modules_by_path = _project_prove_dependency_graph(lean_files, module_to_path)
    sorry_files: list[Path] = []
    for path in lean_files:
        count = _count_sorries(str(path))
        if isinstance(count, int) and count > 0:
            sorry_files.append(path.resolve())

    sorry_path_set = {path.resolve() for path in sorry_files}

    candidates: list[dict[str, Any]] = []
    for path in sorry_files:
        resolved = path.resolve()
        try:
            text = path.read_text(encoding="utf-8")
            line_count = len(text.splitlines())
        except Exception:
            text = ""
            line_count = 0
        declarations = _declaration_line_index(str(path))
        difficulty = _project_prove_file_difficulty(declarations, text)
        direct_imports = set(imports_by_path.get(resolved, set()))
        direct_imported_by = set(imported_by_path.get(resolved, set()))
        transitive_imports = _project_prove_transitive_paths(resolved, imports_by_path)
        transitive_imported_by = _project_prove_transitive_paths(resolved, imported_by_path)
        candidate_imports = direct_imports & sorry_path_set
        candidate_imported_by = direct_imported_by & sorry_path_set
        candidate_upstream = transitive_imports & sorry_path_set
        candidate_downstream = transitive_imported_by & sorry_path_set
        candidates.append(
            {
                "label": _relative_project_file_label(path, root),
                "path": str(path),
                "module_name": _module_name_for_project_path(path, root),
                "sorry_count": int(_count_sorries(str(path)) or 0),
                "line_count": line_count,
                "declaration_count": len(declarations),
                "import_count": int(len(import_modules_by_path.get(resolved, []) or [])),
                "project_import_count": int(len(direct_imports)),
                "imported_by_count": int(len(direct_imported_by)),
                "project_downstream_count": int(len(transitive_imported_by)),
                "candidate_import_count": int(len(candidate_imports)),
                "candidate_imports": _project_prove_label_list(candidate_imports, root),
                "candidate_imported_by_count": int(len(candidate_imported_by)),
                "candidate_imported_by": _project_prove_label_list(candidate_imported_by, root),
                "candidate_upstream_count": int(len(candidate_upstream)),
                "candidate_downstream_count": int(len(candidate_downstream)),
                "candidate_downstream": _project_prove_label_list(candidate_downstream, root),
                "project_imports": _project_prove_label_list(direct_imports, root),
                "project_imported_by": _project_prove_label_list(direct_imported_by, root),
                **difficulty,
            }
        )
    return candidates


def _llm_prioritize_project_prove_files(candidates: Sequence[Mapping[str, Any]]) -> tuple[list[str], str, str]:
    # NOTE: intentionally NOT extracted to project_prove_manager — the test suite monkeypatches
    # ``native_runner.call_llm`` to drive this ranking, so the ``call_llm`` lookup must resolve in
    # the native_runner namespace. It still calls the extracted pure rankers via the re-export shim.
    fallback = _project_prove_fallback_order(candidates)
    if not candidates:
        return [], "fallback", "no candidate files"
    valid_labels = {str(item.get("label", "") or "") for item in candidates}
    summaries = [
        {
            "label": str(item.get("label", "") or ""),
            "sorry_count": int(item.get("sorry_count", 0) or 0),
            "line_count": int(item.get("line_count", 0) or 0),
            "declaration_count": int(item.get("declaration_count", 0) or 0),
            "imported_by_count": int(item.get("imported_by_count", 0) or 0),
            "import_count": int(item.get("import_count", 0) or 0),
            "module_name": str(item.get("module_name", "") or ""),
            "dependency": {
                "candidate_downstream_count": int(item.get("candidate_downstream_count", 0) or 0),
                "candidate_downstream": list(item.get("candidate_downstream", []) or []),
                "candidate_import_count": int(item.get("candidate_import_count", 0) or 0),
                "candidate_imports": list(item.get("candidate_imports", []) or []),
                "candidate_imported_by_count": int(item.get("candidate_imported_by_count", 0) or 0),
                "candidate_imported_by": list(item.get("candidate_imported_by", []) or []),
                "project_downstream_count": int(item.get("project_downstream_count", 0) or 0),
                "project_imported_by": list(item.get("project_imported_by", []) or []),
                "project_imports": list(item.get("project_imports", []) or []),
            },
            "difficulty_score": int(item.get("difficulty_score", 0) or 0),
            "first_pending_difficulty_score": int(item.get("first_pending_difficulty_score", 0) or 0),
            "hint_count": int(item.get("hint_count", 0) or 0),
            "worked_example_count": int(item.get("worked_example_count", 0) or 0),
            "pending_declarations": list(item.get("pending_declarations", []) or [])[:5],
            "file_context": {
                "kind": str(item.get("file_context_kind", "") or ""),
                "excerpt": str(item.get("file_context_excerpt", "") or ""),
            },
        }
        for item in list(candidates)[:PROJECT_PROVE_MANAGER_CANDIDATE_LIMIT]
    ]
    prompt = (
        "Rank Lean files for an EPFLemma `/prove` project run.\n\n"
        "Priority policy:\n"
        "1. Prefer candidate files that other candidate files transitively depend on. "
        "Use `dependency.candidate_downstream_count` and `dependency.candidate_downstream` for this.\n"
        "2. Prefer files with fewer unresolved candidate-file dependencies of their own. "
        "Use `dependency.candidate_import_count` and `dependency.candidate_imports`.\n"
        "3. Use project-wide import data as secondary dependency evidence, because top-level aggregator files may import many peers.\n"
        "4. Prefer easier files, especially lower `difficulty_score` and lower `first_pending_difficulty_score`.\n"
        "5. Read the provided file excerpt and pending declaration contexts. Full source is included for small files; large files include selected headers, hints, and pending theorem excerpts.\n"
        "6. Treat files with local hints, checked lemmas, worked examples, and simple first pending declarations as easier.\n"
        "7. Treat competition-style names such as Putnam, IMO, AIME, AMC, and deep number theory as harder unless the excerpt shows a simple proof path.\n"
        "8. Prefer fewer `sorry`s and shorter files only when dependency and difficulty are similar.\n\n"
        "Return only JSON in this shape: {\"files\": [\"relative/File.lean\", ...], \"reason\": \"short reason\"}.\n"
        "Use only labels from the candidate list.\n\n"
        f"Candidates:\n{json.dumps(summaries, ensure_ascii=False)}"
    )
    try:
        response = call_llm(
            task="prove_manager",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2000,
            timeout=20.0,
        )
        content = response.choices[0].message.content
        payload = _extract_json_payload(content if isinstance(content, str) else str(content or ""))
        labels = _ordered_labels_from_llm_payload(payload, valid_labels)
        for label in fallback:
            if label not in labels:
                labels.append(label)
        labels = _guard_project_prove_llm_order(labels, candidates)
        if labels:
            reason = str(payload.get("reason", "") if isinstance(payload, Mapping) else "").strip()
            return labels, "llm", reason or "LLM-ranked by dependency, theorem difficulty, and length"
    except Exception as exc:
        return fallback, "fallback", f"LLM ranking unavailable: {type(exc).__name__}: {exc}"
    return fallback, "fallback", "LLM ranking returned no usable file order"


def _refresh_project_prove_file_queue(autonomy_state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = _collect_project_prove_file_candidates(_project_root())
    candidate_by_label = {str(item.get("label", "") or ""): dict(item) for item in candidates}
    existing_queue = [
        str(label or "")
        for label in autonomy_state.get("project_prove_file_queue", [])
        if str(label or "") in candidate_by_label
    ]
    planned_new_queue = not existing_queue
    if existing_queue:
        ordered_labels = existing_queue
        fallback = _project_prove_fallback_order(candidates)
        for label in fallback:
            if label not in ordered_labels:
                ordered_labels.append(label)
        source = str(autonomy_state.get("project_prove_plan_source", "") or "existing")
        reason = str(autonomy_state.get("project_prove_plan_reason", "") or "kept existing file queue")
    else:
        scope_order = [
            _relative_project_file_label(path, _project_root())
            for path in _prove_file_scope_ordered_paths(_project_root())
        ]
        scoped_labels = [label for label in scope_order if label in candidate_by_label]
        if scoped_labels:
            ordered_labels = scoped_labels
            source = "formalization-scope"
            reason = "kept generated formalization files in import order"
        else:
            ordered_labels, source, reason = _llm_prioritize_project_prove_files(candidates)
    ordered_candidates = [candidate_by_label[label] for label in ordered_labels if label in candidate_by_label]
    autonomy_state["project_prove_file_queue"] = [str(item["label"]) for item in ordered_candidates]
    autonomy_state["project_prove_file_candidates"] = ordered_candidates[:PROJECT_PROVE_MANAGER_CANDIDATE_LIMIT]
    autonomy_state["project_prove_plan_source"] = source
    autonomy_state["project_prove_plan_reason"] = reason
    if planned_new_queue and ordered_candidates:
        _record_activity(
            "project-prove-file-queue-planned",
            f"Project prove manager planned {len(ordered_candidates)} file(s)",
            total_candidates=len(candidates),
            candidate_limit=PROJECT_PROVE_MANAGER_CANDIDATE_LIMIT,
            ordered_files=[str(item.get("label", "") or "") for item in ordered_candidates],
            candidates=ordered_candidates[:PROJECT_PROVE_MANAGER_CANDIDATE_LIMIT],
            plan_source=source,
            plan_reason=reason,
        )
    return ordered_candidates


def _assign_project_prove_file(
    autonomy_state: dict[str, Any],
    candidate: Mapping[str, Any],
    *,
    phase: str,
) -> bool:
    label = str(candidate.get("label", "") or "").strip()
    path = str(candidate.get("path", "") or "").strip()
    if not label or not path:
        return False
    _set_native_active_file(label)
    _set_project_prove_manager_active(True)
    autonomy_state["project_prove_manager_enabled"] = True
    autonomy_state["project_prove_active_file"] = label
    autonomy_state["project_prove_active_file_path"] = path
    autonomy_state.pop("final_file_sweep_announcement", None)
    for key in _FINAL_SWEEP_AUTONOMY_KEYS:
        autonomy_state.pop(key, None)
    queue = [str(value or "") for value in autonomy_state.get("project_prove_file_queue", [])]
    _record_activity(
        "project-prove-file-assigned",
        f"Project prove manager assigned {label}",
        phase=phase,
        active_file=label,
        active_file_path=path,
        remaining_files=queue[:PROJECT_PROVE_MANAGER_CANDIDATE_LIMIT],
        plan_source=str(autonomy_state.get("project_prove_plan_source", "") or ""),
        plan_reason=str(autonomy_state.get("project_prove_plan_reason", "") or ""),
    )
    print("")
    print(f"Project prove manager assigned file: {label}")
    return True


def _ensure_project_prove_manager_started(
    autonomy_state: dict[str, Any],
    *,
    phase: str,
) -> bool:
    if not _project_prove_manager_requested():
        return False
    autonomy_state["project_prove_manager_enabled"] = True
    _set_project_prove_manager_active(True)
    if _read_native_env("ACTIVE_FILE", "").strip():
        return False
    candidates = _refresh_project_prove_file_queue(autonomy_state)
    if not candidates:
        _record_activity(
            "project-prove-file-queue-empty",
            "Project prove manager found no files with sorry placeholders",
            phase=phase,
        )
        return False
    return _assign_project_prove_file(autonomy_state, candidates[0], phase=phase)


def _advance_project_prove_manager_if_needed(
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None,
    *,
    phase: str,
) -> bool:
    if not _project_prove_manager_active(autonomy_state):
        return False
    current = dict(live_state or {})
    if not _live_state_is_verified(current):
        return False
    active_label = _display_file_label(current) or str(autonomy_state.get("project_prove_active_file", "") or "")
    if active_label:
        completed = [str(value or "") for value in autonomy_state.get("project_prove_completed_files", [])]
        if active_label not in completed:
            completed.append(active_label)
        autonomy_state["project_prove_completed_files"] = completed
    candidates = _refresh_project_prove_file_queue(autonomy_state)
    if not candidates:
        autonomy_state["project_prove_file_queue"] = []
        _record_activity(
            "project-prove-file-queue-complete",
            "Project prove manager has no remaining files with sorry placeholders",
            phase=phase,
            completed_files=list(autonomy_state.get("project_prove_completed_files", []) or []),
        )
        return False
    for candidate in candidates:
        label = str(candidate.get("label", "") or "")
        if label and label != active_label:
            return _assign_project_prove_file(autonomy_state, candidate, phase=phase)
    return False


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
        content = path.read_text(encoding="utf-8")
    except Exception:
        return []
    return _declaration_line_index_from_text(content)


def _find_declaration_entry(active_file: str, label: str) -> dict[str, Any] | None:
    wanted = str(label or "").strip()
    if not active_file or not wanted:
        return None
    for entry in _declaration_line_index(active_file):
        if str(entry.get("name", "") or "").strip() == wanted:
            return entry
    return None


def _declaration_prefix_text(active_file: str, label: str, *, max_lines: int = 200) -> str:
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


def _queue_diagnostic_items(text: str) -> list[dict[str, Any]]:
    return [
        item
        for item in diagnostic_items(text)
        if str(item.get("severity", "") or "").strip().lower() == "error"
    ]


def _queue_diagnostic_line_numbers(text: str) -> list[int]:
    items = diagnostic_items(text)
    if items:
        values: list[int] = []
        for item in _queue_diagnostic_items(text):
            line = item.get("line")
            if isinstance(line, int) and line > 0 and line not in values:
                values.append(line)
        return values
    return _extract_diagnostic_line_numbers(text)


def _diagnostics_indicate_queue_blocker(text: str) -> bool:
    items = diagnostic_items(text)
    if items:
        return bool(_queue_diagnostic_items(text))
    lowered = (text or "").lower()
    cleared_tokens = (
        "no errors found",
        "no errors",
        "without errors",
    )
    if any(token in lowered for token in cleared_tokens):
        for token in cleared_tokens:
            lowered = lowered.replace(token, "")
    blocker_patterns = (
        r"\berror\b",
        r"\berrors\b",
        r"\bunsolved\b",
        r"\bfailed\b",
        r"\btype mismatch\b",
        r"\bunknown option\b",
    )
    return any(re.search(pattern, lowered) for pattern in blocker_patterns)


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


def _diagnostic_reason_for_entry(entry: Mapping[str, Any], diagnostic_lines: list[int]) -> str:
    if not diagnostic_lines:
        return ""
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or start)
    if start <= 0:
        return ""
    for line_number in diagnostic_lines:
        if start <= int(line_number) <= max(start, end):
            return f"diagnostic near line {line_number}"
    return ""


def _declaration_diagnostic_feedback_reason(
    active_file: str,
    label: str,
    *texts: str,
    structured_items: Sequence[Mapping[str, Any]] = (),
) -> str:
    entry = _find_declaration_entry(active_file, label)
    if not entry:
        return ""
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or start)
    if start <= 0:
        return ""

    def _structured_diagnostic_line(diagnostic: Mapping[str, Any]) -> int | None:
        for key in ("line", "start_line", "file_line"):
            value = diagnostic.get(key)
            if isinstance(value, int):
                return value
        for key in ("file_start", "start"):
            value = diagnostic.get(key)
            if isinstance(value, Mapping):
                line = value.get("line")
                if isinstance(line, int):
                    return line
        return None

    # Prefer the manager_check's structured messages when available. The text
    # fallbacks below only catch diagnostics that come in `<file>:<line>:<col>:`
    # form (lake / lean_inspect output); `lean_incremental_check` returns
    # warnings as plain `warning: ...` lines that the regex cannot locate, so
    # the structured path is the only way to honour the spec's per-theorem
    # warning-cleanup opportunity for warnings the targeted check surfaced.
    for diagnostic in structured_items or ():
        if not isinstance(diagnostic, Mapping):
            continue
        line = _structured_diagnostic_line(diagnostic)
        if not (isinstance(line, int) and start <= line <= max(start, end)):
            continue
        severity = str(diagnostic.get("severity", "") or "diagnostic").strip().lower()
        if severity not in {"warning", "error"}:
            continue
        message = _single_line(str(diagnostic.get("message", "") or ""), 180)
        return (
            f"{severity} near line {line}: {message}"
            if message
            else f"{severity} near line {line}"
        )
    for text in texts:
        if not text:
            continue
        parsed_items = diagnostic_items(text)
        for diagnostic in parsed_items:
            line = diagnostic.get("line")
            if isinstance(line, int) and start <= line <= max(start, end):
                severity = str(diagnostic.get("severity", "") or "diagnostic").strip().lower()
                if severity not in {"warning", "error"}:
                    continue
                message = _single_line(str(diagnostic.get("message", "") or ""), 180)
                return (
                    f"{severity} near line {line}: {message}"
                    if message
                    else f"{severity} near line {line}"
                )
        if not parsed_items:
            lowered_text = text.lower()
            if re.search(r":\d+:\d+:\s*info:", lowered_text) and not re.search(
                r":\d+:\d+:\s*(?:warning|error):",
                lowered_text,
            ):
                continue
            reason = _diagnostic_reason_for_entry(entry, _extract_diagnostic_line_numbers(text))
            if reason:
                return reason
    return ""


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
    diagnostics_active = _diagnostics_indicate_queue_blocker(issue_text)
    diagnostic_lines = _queue_diagnostic_line_numbers(issue_text)
    parsed_diagnostic_items = _queue_diagnostic_items(issue_text)
    diagnostic_match_text = issue_text
    if parsed_diagnostic_items:
        diagnostic_match_text = "\n".join(
            str(item.get("message", "") or "") for item in parsed_diagnostic_items
        )

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
            if diagnostics_active and not diagnostic_lines and _declaration_name_safe_for_diagnostic_match(name):
                if re.search(rf"\b{re.escape(name)}\b", diagnostic_match_text or ""):
                    reasons.append("referenced in diagnostics")
            diagnostic_reason = _diagnostic_reason_for_entry(entry, diagnostic_lines) if diagnostics_active else ""
            if diagnostic_reason:
                reasons.append(diagnostic_reason)
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
        if not queue and diagnostics_active and diagnostic_lines:
            fallback = _nearest_declaration_name(active_file, next(iter(diagnostic_lines), None))
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


def _queue_horizon_summary(
    *,
    declaration_scope: str,
    queue_needs_final_file_sweep: bool,
    current_queue_item: Mapping[str, Any] | None,
    declaration_queue_summary: str,
    declaration_queue_total: int = 0,
) -> str:
    if declaration_scope != "file" or queue_needs_final_file_sweep:
        return declaration_queue_summary or "[none]"
    item = dict(current_queue_item or {})
    if not item:
        return "[none]"
    label = str(item.get("label", "") or "[unnamed]")
    reasons = ", ".join(str(reason) for reason in item.get("reasons", []) or [] if str(reason).strip()) or "pending"
    hidden_count = max(0, int(declaration_queue_total or 0) - 1)
    lines = [
        f"- assigned declaration: {label} - {reasons}",
        (
            f"- future queue items: hidden until the manager assigns them ({hidden_count} pending)"
            if hidden_count
            else "- future queue items: no further queue items pending (0 pending)"
        ),
    ]
    return "\n".join(lines)


def _diagnostics_for_queue_horizon(
    *,
    active_file: str,
    target_symbol: str,
    diagnostics: str,
    declaration_scope: str,
    queue_needs_final_file_sweep: bool,
) -> str:
    text = str(diagnostics or "").strip()
    if declaration_scope != "file" or queue_needs_final_file_sweep or not target_symbol:
        return text or "unavailable"
    entry = _find_declaration_entry(active_file, target_symbol)
    parsed = diagnostic_items(text)
    if parsed and entry:
        scoped = [item for item in parsed if _line_in_declaration(entry, item.get("line"))]
        if scoped:
            lines = [_format_diagnostic_for_model(item) for item in scoped[:12]]
            hidden = len(scoped) - len(lines)
            if hidden > 0:
                lines.append(f"- ... plus {hidden} more diagnostic(s) in the assigned declaration")
            return "\n".join(lines)
        return (
            "No diagnostics in the assigned declaration. "
            "Diagnostics from future queue items are hidden until the manager assigns them."
        )
    lowered = text.lower()
    if not text or "no errors found" in lowered or "no diagnostics" in lowered or "no errors" in lowered:
        return text or "No diagnostics in the assigned declaration."
    return (
        "Diagnostics could not be scoped reliably for the assigned declaration. "
        "Use `lean_inspect` on the assigned declaration before editing."
    )


def _proof_status_lines_for_queue_horizon(
    *,
    active_file: str,
    target_symbol: str,
    declaration_scope: str,
    queue_needs_final_file_sweep: bool,
    sorry_count: Any,
    project_sorry_count: Any,
    project_sorry_files: list[str],
) -> list[str]:
    if declaration_scope == "file" and target_symbol and not queue_needs_final_file_sweep:
        entry = _find_declaration_entry(active_file, target_symbol)
        if entry:
            has_sorry = "yes" if entry.get("has_sorry") else "no"
        else:
            has_sorry = "[unknown]"
        return [
            f"assigned declaration has sorry: {has_sorry}",
            "future declaration sorry counts: hidden until manager assignment",
        ]
    return [
        f"sorry count: {sorry_count if sorry_count is not None else '[unknown]'}",
        f"project sorry count: {project_sorry_count if project_sorry_count is not None else '[unknown]'}",
        (
            "project files with sorry: " + ", ".join(project_sorry_files)
            if project_sorry_files
            else "project files with sorry: [none]"
        ),
    ]


def _queue_item_has_diagnostic_reason(item: Mapping[str, Any]) -> bool:
    reasons = " ".join(str(reason or "") for reason in item.get("reasons", []) or []).lower()
    return bool(
        "diagnostic" in reasons
        or "error" in reasons
        or "unsolved" in reasons
        or "type mismatch" in reasons
        or "failed" in reasons
    )


def _queue_item_has_sorry_reason(item: Mapping[str, Any]) -> bool:
    return any(str(reason or "").strip().lower() == "contains sorry" for reason in item.get("reasons", []) or [])


def _queue_item_has_error_diagnostic(item: Mapping[str, Any], active_file: str, diagnostics: str) -> bool:
    label = str(item.get("label", "") or "").strip()
    entry = _find_declaration_entry(active_file, label)
    if not entry:
        return False
    for diagnostic in diagnostic_items(diagnostics):
        if str(diagnostic.get("severity", "") or "").strip().lower() != "error":
            continue
        if _line_in_declaration(entry, diagnostic.get("line")):
            return True
    return False


def _inspection_queue_item_is_queue_blocker(item: Mapping[str, Any], active_file: str, diagnostics: str) -> bool:
    if _queue_item_has_sorry_reason(item):
        return True
    if _queue_item_has_error_diagnostic(item, active_file, diagnostics):
        return True
    reasons = " ".join(str(reason or "") for reason in item.get("reasons", []) or []).lower()
    return bool(
        "error" in reasons
        or "unsolved" in reasons
        or "type mismatch" in reasons
        or "failed" in reasons
    )


def _current_queue_item(queue: list[dict[str, Any]], active_file: str) -> dict[str, Any] | None:
    if not queue or not active_file:
        return None
    mgr = TheoremQueueManager()
    mgr.set_active_file(active_file)
    mgr.replace_queue(queue)
    selected = mgr.select_next(is_present_in_file=lambda label: bool(_find_declaration_entry(active_file, label)))
    if selected is None:
        return None
    for item in queue:
        if str(item.get("label", "") or "").strip() == selected.label:
            return dict(item)
    return {"label": selected.label, "reasons": list(selected.reasons)}


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
    if _document_formalization_handoff_blocked_state(current) or _document_formalization_ready_for_prover_handoff(current):
        mgr = _queue_manager_from_state(autonomy_state, current)
        mgr.clear_assignment()
        _flush_queue_manager(autonomy_state, mgr)
        autonomy_state.pop("current_queue_assignment", None)
        _assert_queue_invariants(autonomy_state, live_state, event="prepare-formalization-gate")
        return
    item = dict(current.get("current_queue_item") or {})
    label = str(item.get("label", "") or current.get("target_symbol", "") or "").strip()
    active_file = str(current.get("active_file", "") or current.get("active_file_label", "") or "").strip()
    slice_text = str(current.get("current_queue_item_slice", "") or "").strip()
    mgr = _queue_manager_from_state(autonomy_state, current)
    if not label or not active_file:
        mgr.clear_assignment()
        _flush_queue_manager(autonomy_state, mgr)
        _assert_queue_invariants(autonomy_state, live_state, event="prepare-assignment")
        return

    previous = mgr.current
    previous_prepare = previous.prepare if previous is not None else None
    same_assignment = bool(
        previous is not None
        and previous.key.target_symbol == label
        and _same_active_file(previous.key.active_file, active_file)
    )
    if not same_assignment:
        _record_activity(
            "queue-manager-assigned",
            f"Queue manager assigned {label}",
            target_symbol=label,
            active_file=active_file,
            active_file_label=str(current.get("active_file_label", "") or ""),
        )
        print(f"Queue manager assigned {label}")
    if same_assignment and previous_prepare and previous_prepare.success:
        prepare = previous_prepare
    else:
        prepare_dict = _manager_prepare_incremental_queue_item(active_file, label)
        prepare = PrepareState.from_mapping(prepare_dict)
        _record_activity(
            "manager-incremental-warmup",
            (
                f"LeanInteract warmup succeeded for {label}"
                if prepare_dict.get("success")
                else f"LeanInteract warmup unavailable for {label}"
            ),
            target_symbol=label,
            active_file=active_file,
            ok=bool(prepare_dict.get("ok")),
            success=bool(prepare_dict.get("success")),
            elapsed_s=prepare_dict.get("elapsed_s", 0),
            cache=dict(prepare_dict.get("cache") or {}),
            error=str(prepare_dict.get("error", "") or ""),
        )
    mgr.assign(QueueItem.from_mapping({**item, "label": label}), active_file=active_file, slice_text=slice_text, prepare=prepare)
    _flush_queue_manager(autonomy_state, mgr)
    _assert_queue_invariants(autonomy_state, live_state, event="prepare-assignment")


def _queue_invariant_checks_enabled() -> bool:
    return _read_text_env("EPFLEMMA_QUEUE_INVARIANT_CHECKS", "").strip().lower() in {"1", "true", "yes", "on"}


def _assert_queue_invariants(
    autonomy_state: Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None,
    *,
    event: str = "",
) -> None:
    if not _queue_invariant_checks_enabled():
        return
    autonomy = dict(autonomy_state or {})
    assignment = dict(autonomy.get("current_queue_assignment") or {})
    target = str(assignment.get("target_symbol", "") or "").strip()
    active_file = str(assignment.get("active_file", "") or "").strip()
    if not target or not active_file:
        return
    current_key = _manager_feedback_retry_key(target, active_file)
    retries = dict(autonomy.get("manager_feedback_retries") or {})
    stale_keys = [key for key in retries if str(key) != current_key]
    if stale_keys:
        raise AssertionError(f"queue invariant failed after {event}: stale feedback retries {stale_keys!r}")

    def _slice_body(value: str) -> str:
        text = str(value or "").strip()
        if text.startswith("Assigned declaration slice") and ":\n" in text:
            return text.partition(":\n")[2].strip()
        return text

    expected_slice = _slice_body(str(assignment.get("slice", "") or ""))
    current_slice = _slice_body(_declaration_slice_text(active_file, target))
    if expected_slice and current_slice and expected_slice != current_slice:
        raise AssertionError(f"queue invariant failed after {event}: assignment slice drifted for {target}")
    entry = _find_declaration_entry(active_file, target)
    diagnostics = str(dict(live_state or {}).get("diagnostics", "") or "")
    if entry and diagnostics:
        leaked = [
            item.get("line")
            for item in diagnostic_items(diagnostics)
            if isinstance(item.get("line"), int) and not _line_in_declaration(entry, item.get("line"))
        ]
        scoped = [
            item.get("line")
            for item in diagnostic_items(_diagnostics_for_queue_horizon(
                active_file=active_file,
                target_symbol=target,
                diagnostics=diagnostics,
                declaration_scope="file",
                queue_needs_final_file_sweep=False,
            ))
            if isinstance(item.get("line"), int)
        ]
        if any(not _line_in_declaration(entry, line) for line in scoped):
            raise AssertionError(
                f"queue invariant failed after {event}: horizon diagnostics leaked future lines {leaked!r}"
            )
    record = dict(autonomy.get("last_verification") or {})
    if record and str(record.get("scope", "") or "").startswith("target:"):
        expected_scope = f"target:{target}"
        if str(record.get("scope", "") or "") != expected_scope:
            raise AssertionError(
                f"queue invariant failed after {event}: last verification scope does not match current target"
            )


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
    mgr = _queue_manager_from_state(autonomy_state)
    previous = mgr.current.key if mgr.current is not None else None
    previous_target = previous.target_symbol if previous is not None else ""
    previous_file = str(baseline.get("active_file", "") or "").strip() or (previous.active_file if previous is not None else "")
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
    mgr = _queue_manager_from_state(autonomy_state)
    mgr.clear_attempts_for(_queue_key(target_symbol, active_file))
    _flush_queue_manager(autonomy_state, mgr)


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
    mgr = _queue_manager_from_state(autonomy_state, current)
    prepare = mgr.current.prepare if mgr.current is not None else PrepareState(success=False)
    mgr.assign(
        QueueItem.from_mapping({"label": target_symbol}),
        active_file=active_file,
        slice_text=str(current.get("current_queue_item_slice", "") or "").strip(),
        prepare=prepare,
    )
    _flush_queue_manager(autonomy_state, mgr)


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
        "- do not start solving unrelated future queue items",
        "- future queued `sorry` warnings are queue state only; do not edit those declarations in this turn",
        "- if the assigned declaration verifies and only unrelated queued declarations remain, stop and let the manager hand off the next item",
        "- after a meaningful edit, stop and let the manager re-check the queue",
        "- for this file-scoped theorem turn, use `lean_incremental_check(check_target)` as the fast queue-step acceptance check; `lean_verify(mode=file_exact)` is reserved for final Lake sweeps, fallback, or explicit canonical verification",
        "- use Lean tools for normal verification so the manager can classify the assigned declaration; terminal-based Lake checks are emergency/manual fallback only",
    ]
    verification_hint = _queue_item_verification_hint(active_file)
    if verification_hint:
        parts.extend(["", "Verification for this queue item:", verification_hint])
    warmup = dict(dict(autonomy_state or {}).get("current_queue_assignment", {}) or {}).get("incremental_prepare")
    if isinstance(warmup, Mapping):
        status = "ready" if warmup.get("success") else "unavailable"
        detail = str(warmup.get("error", "") or warmup.get("output", "") or "").strip()
        line = f"- LeanInteract warmup: {status}"
        if warmup.get("elapsed_s") not in (None, ""):
            line += f" ({warmup.get('elapsed_s')}s)"
        if detail and not warmup.get("success"):
            line += f"; {detail[:180]}"
        parts.extend(["", "Incremental verifier state:", line])
    prefix_text = str(live_state.get("current_queue_item_prefix", "") or "").strip()
    if prefix_text:
        parts.extend(["", prefix_text])
    slice_text = str(live_state.get("current_queue_item_slice", "") or "").strip()
    if slice_text and not prefix_text:
        parts.extend(["", slice_text])
    failed = _recent_failed_attempts_summary(dict(autonomy_state or {}), live_state)
    if failed:
        parts.extend(["", failed])
    disabled_tools = _disabled_tools_summary(autonomy_state)
    if disabled_tools:
        parts.extend(["", "Disabled this run:", f"- {', '.join(disabled_tools)}"])
    if search_hints:
        parts.extend(["", "Search hints:", f"- {', '.join(search_hints[:4])}"])
    if bool(live_state.get("search_exhausted")):
        parts.extend(
            [
                "",
                "Search exhaustion:",
                "- repeated search attempts have already failed for this theorem",
                "- do not call `lean_search` again in this turn unless you are changing the query strategy materially",
                "- your next move should be an edit, `lean_verify`, or a concrete blocker report",
            ]
        )
    parts.extend(["", "Task:", f"Repair `{label}` from its current state."])
    return "\n".join(parts)


def _remember_failed_attempt(
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None,
    *,
    cycle_number: int,
    refresh_baseline: bool = True,
    reason: str = "",
) -> None:
    if not live_state:
        return
    item = dict(live_state.get("current_queue_item") or {})
    assignment = dict(autonomy_state.get("current_queue_assignment") or {})
    assignment_target = str(assignment.get("target_symbol", "") or "").strip()
    assignment_file = str(assignment.get("active_file", "") or "").strip()
    target_symbol = str(assignment_target or item.get("label", "") or live_state.get("target_symbol", "") or "").strip()
    active_file = str(assignment_file or live_state.get("active_file", "") or live_state.get("active_file_label", "") or "").strip()
    if not target_symbol or not active_file:
        return
    failure_reason = str(
        reason
        or live_state.get("blocker_summary", "")
        or live_state.get("diagnostics", "")
        or live_state.get("goals", "")
        or live_state.get("build_status", "")
        or ""
    ).strip()
    if not failure_reason:
        return
    record_live_state = dict(live_state)
    record_item = dict(record_live_state.get("current_queue_item") or {})
    live_target = str(record_item.get("label", "") or record_live_state.get("target_symbol", "") or "").strip()
    live_file = str(record_live_state.get("active_file", "") or record_live_state.get("active_file_label", "") or "").strip()
    if live_target != target_symbol or not _same_active_file(live_file, active_file):
        current_slice = _declaration_slice_text(active_file, target_symbol) or str(assignment.get("slice", "") or "")
        record_item = {"label": target_symbol}
        record_live_state["current_queue_item"] = record_item
        record_live_state["target_symbol"] = target_symbol
        record_live_state["active_file"] = active_file
        record_live_state["current_queue_item_slice"] = current_slice
    proof_shape = _attempt_proof_shape_from_delta(autonomy_state, record_live_state)
    mgr = _queue_manager_from_state(autonomy_state)
    attempt = mgr.record_attempt_for(
        _queue_key(target_symbol, active_file),
        cycle=cycle_number,
        proof_shape=proof_shape,
        reason=_single_line(failure_reason, 240),
    )
    if attempt is None:
        return
    _flush_queue_manager(autonomy_state, mgr)
    if refresh_baseline:
        _refresh_failed_attempt_baseline(autonomy_state, record_live_state)
    entry = {
        "attempt": attempt.attempt,
        "cycle": attempt.cycle,
        "target_symbol": attempt.key.target_symbol,
        "active_file": attempt.key.active_file,
        "proof_shape": attempt.proof_shape,
        "reason": attempt.reason,
    }
    _record_activity(
        "manager-failed-attempt",
        f"Manager recorded failed attempt {entry['attempt']} for {target_symbol}",
        target_symbol=target_symbol,
        active_file=active_file,
        attempt=entry["attempt"],
        cycle=cycle_number,
        reason=entry["reason"],
    )
    print("")
    print(f"🔁 Manager feedback (attempt {entry['attempt']} on {target_symbol}):")
    print(f"   blocker: {_single_line(reason, 220)}")


def _record_theorem_outcome(autonomy_state: dict[str, Any], outcome: Mapping[str, Any]) -> None:
    target_symbol = str(outcome.get("target_symbol", "") or "").strip()
    active_file = str(outcome.get("active_file", "") or "").strip()
    if not target_symbol or not active_file:
        return
    mgr = _queue_manager_from_state(autonomy_state)
    mgr.record_outcome_for(
        _queue_key(target_symbol, active_file),
        status=str(outcome.get("status", "") or "unknown"),
        note=str(outcome.get("note", "") or ""),
        build_status=str(outcome.get("build_status", "") or ""),
        verification=verification_from_mapping(dict(outcome.get("last_verification") or {})),
    )
    _flush_queue_manager(autonomy_state, mgr)


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

    mgr = _queue_manager_from_state(autonomy_state)
    key = _queue_key(target_symbol, active_file)
    assignment = mgr.current
    slice_text = ""
    if assignment is not None and assignment.key == key:
        slice_text = str(assignment.slice or "").strip()
    if not slice_text:
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
    mgr.record_attempt_for(
        key,
        cycle=0,
        proof_shape=proof_shape or "[no attempted proof shape recorded]",
        reason=reason,
    )
    _flush_queue_manager(autonomy_state, mgr)


def _has_unresolved_theorem_outcomes(autonomy_state: Mapping[str, Any]) -> bool:
    mgr = _queue_manager_from_state(autonomy_state)
    for value in mgr.outcomes.values():
        status = str(value.status or "").strip().lower()
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
        "build_status": _single_line(_recent_verification_status(autonomy_state, live_state), 220),
        "last_verification": _last_verification_record(autonomy_state, live_state),
    }


def _workflow_transition_snapshot(
    compaction_state: Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None,
) -> str:
    snapshot_text = str((compaction_state or {}).get("snapshot_text", "") or "").strip()
    if snapshot_text:
        return snapshot_text
    current = dict(live_state or {})
    queue_horizon = _queue_horizon_summary(
        declaration_scope=str(current.get("declaration_scope", "") or _declaration_queue_scope()),
        queue_needs_final_file_sweep=bool(current.get("queue_needs_final_file_sweep")),
        current_queue_item=dict(current.get("current_queue_item", {}) or {}),
        declaration_queue_summary=str(current.get("declaration_queue_summary", "") or "[none]"),
        declaration_queue_total=int(current.get("declaration_queue_total", 0) or 0),
    )
    return "\n".join(
        [
            MANAGED_SNAPSHOT_PREFIX,
            "",
            f"Workflow: {_workflow_kind()}",
            f"Active file: {_display_file_label(current) or '[unknown]'}",
            f"Active file path: {str(current.get('active_file', '') or '[unknown]')}",
            "Current queue horizon:",
            queue_horizon,
            "",
            "Latest manager verification:",
            _verification_status_text(dict(current.get("last_verification") or {})) or "no recent manager verification",
            "",
            "Current blocker:",
            str(current.get("current_blocker", "") or "[none]"),
        ]
    ).strip()


@dataclass(frozen=True)
class HandoffView:
    previous_target: str
    previous_file: str
    previous_status: str
    previous_note: str
    current_target: str
    current_file_label: str
    current_file: str
    queue_horizon: str
    pending_count: int
    previous_attempts: tuple[dict[str, Any], ...]
    last_verification: dict[str, Any]
    disabled_tools: tuple[str, ...]
    reasoning_effort: str

    def render(self) -> str:
        lines = [
            "[EPFLEMMA-NATIVE THEOREM TRANSITION HANDOFF]",
            "",
            "Previous theorem outcome:",
            f"- declaration: {self.previous_target or '[unknown]'}",
            f"- file: {self.previous_file or '[unknown]'}",
            f"- final status: {self.previous_status or 'unknown'}",
            f"- note: {self.previous_note or '[none]'}",
            "",
            "Current queue focus:",
            f"- declaration: {self.current_target or '[unknown]'}",
            f"- file: {self.current_file_label}",
            f"- exact tool path: {self.current_file or '[unknown]'}",
            f"- pending count: {self.pending_count} pending",
            f"- reasoning effort: {self.reasoning_effort or 'high'}",
            "",
            "Queue horizon:",
            self.queue_horizon,
            "",
            "Latest manager verification:",
            _verification_status_text(self.last_verification) or "no recent manager verification",
        ]
        if self.previous_attempts:
            lines.extend(["", "Previous attempts:"])
            for attempt in self.previous_attempts[-3:]:
                attempt_number = str(attempt.get("attempt", "") or "?")
                reason = _single_line(str(attempt.get("reason", "") or "[no reason recorded]"), 220)
                lines.append(f"- attempt {attempt_number}: {reason}")
        if self.disabled_tools:
            lines.extend(["", "Disabled this run:", f"- {', '.join(self.disabled_tools)}"])
        return "\n".join(lines).strip()


def _handoff_pending_count(
    mgr: TheoremQueueManager,
    live_state: Mapping[str, Any],
    current_target: str,
) -> int:
    if _queue_item_mappings_from_live_state(live_state):
        return mgr.pending_count
    summary = str(live_state.get("declaration_queue_summary", "") or "")
    labels = [
        match.group(1).strip()
        for match in re.finditer(r"^\s*-\s+([^\s\[]+)", summary, flags=re.MULTILINE)
        if match.group(1).strip()
    ]
    if labels:
        return sum(1 for label in labels if label != current_target)
    total = int(live_state.get("declaration_queue_total", 0) or 0)
    return max(0, total - (1 if current_target else 0))


def _theorem_transition_handoff_message(
    outcome: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
    autonomy_state: Mapping[str, Any] | None = None,
) -> str:
    current_target, current_file = _queue_assignment_identity(live_state)
    current = dict(live_state or {})
    mgr = _queue_manager_from_state(autonomy_state, current)
    current_item = dict(current.get("current_queue_item") or {})
    view_mgr = mgr
    if current_target and current_file:
        try:
            view_mgr = mgr.peek_assignment(
                QueueItem.from_mapping({**current_item, "label": current_target}),
                active_file=current_file,
                slice_text=str(current.get("current_queue_item_slice", "") or ""),
                prepare=PrepareState(success=False),
            )
        except Exception:
            pass
    current_file_label = _display_file_label(current) or current_file or "[unknown]"
    queue_horizon = _queue_horizon_summary(
        declaration_scope=str(current.get("declaration_scope", "") or _declaration_queue_scope()),
        queue_needs_final_file_sweep=bool(current.get("queue_needs_final_file_sweep")),
        current_queue_item=dict(current.get("current_queue_item", {}) or {}),
        declaration_queue_summary=str(current.get("declaration_queue_summary", "") or "[none]"),
        declaration_queue_total=int(current.get("declaration_queue_total", 0) or 0),
    )
    previous_target = str(outcome.get("target_symbol", "") or "").strip()
    previous_file = str(outcome.get("active_file", "") or "").strip()
    attempts = tuple(mgr.attempt_entries_for(_queue_key(previous_target, previous_file)))
    last_verification = (
        verification_to_mapping(mgr.last_verification)
        or dict(outcome.get("last_verification") or {})
    )
    disabled_tools = tuple(_disabled_tools_summary(view_mgr.to_autonomy_state()))
    return HandoffView(
        previous_target=previous_target or "[unknown]",
        previous_file=previous_file or "[unknown]",
        previous_status=str(outcome.get("status", "") or "unknown"),
        previous_note=str(outcome.get("note", "") or "[none]"),
        current_target=current_target or "[unknown]",
        current_file_label=current_file_label,
        current_file=current_file or "[unknown]",
        queue_horizon=queue_horizon,
        pending_count=_handoff_pending_count(view_mgr, current, current_target),
        previous_attempts=attempts,
        last_verification=last_verification,
        disabled_tools=disabled_tools,
        reasoning_effort=view_mgr.reasoning_effort_for_current(),
    ).render()


def _theorem_transition_active_skill_message(live_state: Mapping[str, Any] | None) -> str:
    if not _single_queue_item_turn_enabled():
        return ""
    skill_contract = _startup_active_skill_contract(_effective_skill_name(live_state))
    additional_skill_contracts = _startup_additional_skill_contracts(_effective_skill_name(live_state))
    combined_skill_contract = "\n\n".join(part for part in (skill_contract, additional_skill_contracts) if part)
    if not combined_skill_contract:
        return ""
    return "\n".join(
        [
            "[EPFLEMMA-NATIVE THEOREM TRANSITION ACTIVE SKILL]",
            "",
            "The active and supplemental skill contracts are preserved after clearing theorem-local context.",
            "",
            combined_skill_contract,
        ]
    ).strip()


def _rebuild_history_for_theorem_transition(
    history: list[dict[str, Any]],
    compaction_state: Mapping[str, Any] | None,
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, str]] | tuple[None, None]:
    if _document_formalization_handoff_blocked_state(live_state):
        return None, None
    if _document_formalization_ready_for_prover_handoff(live_state):
        return None, None
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
    ]
    skill_message = _theorem_transition_active_skill_message(live_state)
    if skill_message:
        rebuilt_history.append({"role": "assistant", "content": skill_message})
    rebuilt_history.append({"role": "assistant", "content": _theorem_transition_handoff_message(outcome, live_state, autonomy_state)})
    autonomy_state["last_theorem_outcome"] = outcome
    autonomy_state["continuation_blocked_runs"] = 0
    autonomy_state["continuation_stable_cycles"] = 0
    autonomy_state["continuation_live_state_signature"] = None
    return rebuilt_history, transition


def _transition_handoff_from_history(history: list[dict[str, Any]]) -> str:
    for message in history:
        content = message.get("content")
        if isinstance(content, str) and content.startswith("[EPFLEMMA-NATIVE THEOREM TRANSITION HANDOFF]"):
            return content.strip()
    return ""


def _print_theorem_transition_handoff(history: list[dict[str, Any]]) -> None:
    handoff = _transition_handoff_from_history(history)
    if not handoff:
        return
    print("")
    print("Queue handoff for next model turn:")
    for line in handoff.splitlines():
        print(f"  {line}" if line else "")


def _queue_needs_final_file_sweep(live_state: Mapping[str, Any] | None) -> bool:
    current = dict(live_state or {})
    if _document_formalization_waiting_for_independent_review(current):
        return False
    if _document_formalization_ready_for_prover_handoff(current):
        return False
    if _document_formalization_has_draft_sorries(current):
        return False
    return (
        _single_queue_item_turn_enabled()
        and str(current.get("declaration_scope", "") or "") == "file"
        and bool(str(current.get("active_file", "") or "").strip())
        and int(current.get("declaration_queue_total", 0) or 0) == 0
        and not _live_state_is_verified(current)
    )


def _maybe_announce_final_file_sweep_state(
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None,
) -> None:
    current = dict(live_state or {})
    active_file = str(current.get("active_file", "") or "").strip()
    queue_empty = (
        _single_queue_item_turn_enabled()
        and str(current.get("declaration_scope", "") or "") == "file"
        and bool(active_file)
        and int(current.get("declaration_queue_total", 0) or 0) == 0
    )
    if not queue_empty:
        autonomy_state.pop("final_file_sweep_announcement", None)
        return

    if _document_formalization_waiting_for_independent_review(current):
        announcement = f"deferred-review:{active_file}"
        if autonomy_state.get("final_file_sweep_announcement") == announcement:
            return
        autonomy_state["final_file_sweep_announcement"] = announcement
        autonomy_state["continuation_blocked_runs"] = 0
        autonomy_state["continuation_stable_cycles"] = 0
        blocker = str(current.get("current_blocker", "") or "").strip()
        print("")
        print(
            "Document formalization review gate: declaration queue is empty; waiting for independent "
            "statement/source verification before proof cleanup."
        )
        _record_activity(
            "final-file-sweep-deferred-for-document-review",
            "Declaration queue empty but document formalization is waiting for statement/source verification",
            active_file=active_file,
            blocker=blocker,
        )
        return

    if _document_formalization_ready_for_prover_handoff(current):
        announcement = f"deferred-prover-handoff:{active_file}"
        if autonomy_state.get("final_file_sweep_announcement") == announcement:
            return
        autonomy_state["final_file_sweep_announcement"] = announcement
        autonomy_state["continuation_blocked_runs"] = 0
        autonomy_state["continuation_stable_cycles"] = 0
        print("")
        print(
            "Document formalization handoff: declaration queue is empty and statement/source review "
            "passed; stopping before proof cleanup."
        )
        _record_activity(
            "final-file-sweep-deferred-for-prover-handoff",
            "Declaration queue empty and document formalization is ready for /prove",
            active_file=active_file,
            sorry_count=current.get("sorry_count"),
        )
        return

    if _document_formalization_has_draft_sorries(current):
        announcement = f"deferred-document-formalization:{active_file}"
        if autonomy_state.get("final_file_sweep_announcement") == announcement:
            return
        autonomy_state["final_file_sweep_announcement"] = announcement
        autonomy_state["continuation_blocked_runs"] = 0
        autonomy_state["continuation_stable_cycles"] = 0
        print("")
        print(
            "Document formalization handoff: declaration queue is empty but draft declarations still "
            "contain `sorry`; staying in planner/review handoff instead of proof cleanup."
        )
        _record_activity(
            "final-file-sweep-deferred-for-document-formalization",
            "Declaration queue empty but document formalization draft still has sorry declarations",
            active_file=active_file,
            sorry_count=current.get("sorry_count"),
        )
        return

    if _live_state_is_verified(current):
        announcement = "skipped-clean"
        if autonomy_state.get("final_file_sweep_announcement") == announcement:
            return
        autonomy_state["final_file_sweep_announcement"] = announcement
        print("")
        print(
            "Final verification sweep: declaration queue is empty and file verification is already clean; "
            "no further sweep needed."
        )
        _record_activity(
            "final-file-sweep-skipped",
            "Declaration queue empty and file verification already clean",
            active_file=active_file,
        )
        return

    if _queue_needs_final_file_sweep(current):
        announcement = f"started:{active_file}"
        if autonomy_state.get("final_file_sweep_announcement") == announcement:
            return
        autonomy_state["final_file_sweep_announcement"] = announcement
        autonomy_state["continuation_blocked_runs"] = 0
        autonomy_state["continuation_stable_cycles"] = 0
        print("")
        print(
            "Final verification sweep: declaration queue is empty but file verification still has blockers; "
            "starting file-level cleanup."
        )
        _record_activity(
            "final-file-sweep-started",
            "Declaration queue empty but file verification still has blockers",
            active_file=active_file,
            blocker=str(current.get("current_blocker", "") or current.get("diagnostics", "") or ""),
        )


def _document_formalization_review_signature(live_state: Mapping[str, Any] | None) -> str:
    current = dict(live_state or {})
    handoff = dict(current.get("document_formalization_handoff", {}) or {})
    active_file = str(current.get("active_file", "") or "")
    blueprint_path = _read_text_env("EPFLEMMA_FORMALIZATION_BLUEPRINT", "").strip()

    def _content_hash(path: str) -> str:
        if not path:
            return ""
        try:
            return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
        except Exception:
            return ""

    generated_hashes: list[tuple[str, str]] = []
    for path in _formalization_generated_lean_paths(active_file):
        label = _relative_file_label(str(path)) or str(path)
        generated_hashes.append((label, _content_hash(str(path))))
    payload = {
        "active_file": str(current.get("active_file_label", "") or current.get("active_file", "") or ""),
        "blueprint": blueprint_path,
        "blueprint_hash": _content_hash(blueprint_path),
        "generated_lean_hashes": generated_hashes,
        "source": _read_text_env("EPFLEMMA_FORMALIZATION_DOCUMENT_RELATIVE", "").strip(),
        "issues": [str(issue or "") for issue in handoff.get("issues", []) or []],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _document_formalization_review_due(
    live_state: Mapping[str, Any] | None,
    autonomy_state: Mapping[str, Any] | None,
) -> bool:
    if not _document_formalization_waiting_for_independent_review(live_state):
        return False
    signature = _document_formalization_review_signature(live_state)
    previous = str((autonomy_state or {}).get("document_formalization_review_signature", "") or "")
    return bool(signature) and signature != previous


def _stamp_blueprint_statement_review_approved(
    *,
    provider: str,
    active_file: str = "",
) -> bool:
    blueprint_path = _read_text_env("EPFLEMMA_FORMALIZATION_BLUEPRINT", "").strip()
    if not blueprint_path:
        return False
    path = Path(blueprint_path)
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return False
    stamp = f"approved by {provider or 'configured'} verifier"
    status_re = re.compile(
        r"^(?P<prefix>\s*-\s*(?:Statement verification status|Statement/source verification|Source verification status|Verification status)\s*:\s*)(?P<value>.*)$",
        flags=re.MULTILINE | re.IGNORECASE,
    )

    changed = False

    def _replace_status(match: re.Match[str]) -> str:
        nonlocal changed
        value = str(match.group("value") or "").strip()
        if re.search(r"\b(approved|verified|reviewed|accepted)\b", value, flags=re.IGNORECASE):
            return match.group(0)
        changed = True
        return f"{match.group('prefix')}{stamp}"

    updated = status_re.sub(_replace_status, text)
    checklist_re = re.compile(
        r"^(?P<prefix>\s*-\s*)\[(?P<checked>[ xX])\](?P<suffix>\s*Run independent statement/source verification review and apply corrections\.\s*)$",
        flags=re.MULTILINE | re.IGNORECASE,
    )
    statement_match_re = re.compile(
        r"^(?P<prefix>\s*-\s*)\[(?P<checked>[ xX])\](?P<suffix>\s*Verify drafted Lean statements match the source document\.\s*)$",
        flags=re.MULTILINE | re.IGNORECASE,
    )

    def _replace_checklist(match: re.Match[str]) -> str:
        nonlocal changed
        if str(match.group("checked") or "").lower() == "x":
            return match.group(0)
        changed = True
        return f"{match.group('prefix')}[x]{match.group('suffix')}"

    updated = statement_match_re.sub(_replace_checklist, updated)
    updated = checklist_re.sub(_replace_checklist, updated)
    proof_ready_re = re.compile(
        r"^(?P<prefix>\s*-\s*)\[(?P<checked>[ xX])\](?P<suffix>\s*(?:Hand stable (?:theorem/lemma/example )?`sorry` declarations to the managed prover queue|Mark stable theorem/lemma/example `sorry` declarations ready for a user-started prove workflow)\.\s*(?:\(Only check after independent review approves every source entry\.?\)\s*)?)$",
        flags=re.MULTILINE | re.IGNORECASE,
    )
    updated = proof_ready_re.sub(_replace_checklist, updated)
    status_line_re = re.compile(
        r"^(?P<prefix>\s*-\s*Status\s*:\s*)(?P<value>planner draft in progress|draft in progress|pending review|ready for review)\s*$",
        flags=re.MULTILINE | re.IGNORECASE,
    )

    def _replace_status_line(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return f"{match.group('prefix')}statement/source review approved; ready for user-started prove workflow"

    updated = status_line_re.sub(_replace_status_line, updated)
    if not changed or updated == text:
        return False
    try:
        path.write_text(updated, encoding="utf-8")
    except Exception:
        return False
    _record_activity(
        "formalization-review-approval-stamped",
        "Stamped blueprint statement/source review approval after configured verifier PASS",
        provider=provider,
        blueprint=_relative_file_label(str(path)) or str(path),
        active_file=active_file,
    )
    return True


def _record_verifier_decision(
    *,
    task: str,
    provider: str,
    configured_provider: str,
    mode: str,
    ok: bool,
    summary: str,
    active_file: str = "",
    issues: Sequence[str] | None = None,
) -> None:
    issue_list = [str(issue or "") for issue in (issues or [])]
    payload = {
        "task": task,
        "provider": provider,
        "configured_provider": configured_provider,
        "mode": mode,
        "ok": bool(ok),
        "summary": _single_line(summary, 520),
        "active_file": _relative_file_label(active_file) or active_file,
        "issues": issue_list[:10],
    }
    signature = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if not _cache_once(
        signature,
        cache=_VERIFICATION_DECISION_LOG_CACHE,
        order=_VERIFICATION_DECISION_LOG_CACHE_ORDER,
        limit=_VERIFICATION_DECISION_LOG_CACHE_LIMIT,
    ):
        return
    _record_activity(
        "verification-decision",
        f"{task.replace('_', ' ').title()} verifier decision {'passed' if ok else 'blocked'}",
        **payload,
    )


def _verification_review_result_payload(result: Any) -> dict[str, Any]:
    return {
        "task": str(getattr(result, "task", "") or ""),
        "provider": str(getattr(result, "provider", "") or ""),
        "mode": str(getattr(result, "mode", "") or ""),
        "status": str(getattr(result, "status", "") or ""),
        "command": list(getattr(result, "command", []) or []),
        "exit_status": getattr(result, "exit_status", None),
        "response": _bounded_verifier_response(str(getattr(result, "response", "") or "")),
        "response_chars": int(getattr(result, "response_chars", 0) or 0),
        "max_response_chars": int(getattr(result, "max_response_chars", 0) or 0),
        "truncated": bool(getattr(result, "truncated", False)),
        "timed_out": bool(getattr(result, "timed_out", False)),
        "model": str(getattr(result, "model", "") or ""),
        "error": str(getattr(result, "error", "") or ""),
    }


def _verification_review_decision(payload: Mapping[str, Any] | None) -> str:
    response = str((payload or {}).get("response", "") or "").strip()
    if not response:
        return ""
    parsed = _extract_json_payload(response)
    if isinstance(parsed, Mapping):
        for key in ("decision", "status", "result"):
            value = str(parsed.get(key, "") or "").strip().upper()
            if value in {"PASS", "BLOCK"}:
                return value
    match = re.search(
        r"^\s*(?:[#>*_`\-]+\s*)?(?:Decision\s*[:=-]\s*)?\**(PASS|BLOCK)\**\b",
        response,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(r"\bDecision\s*[:=-]\s*(PASS|BLOCK)\b", response, flags=re.IGNORECASE)
    return match.group(1).upper() if match else ""


def _verification_review_findings(payload: Mapping[str, Any] | None, *, limit: int = 5) -> list[str]:
    response = str((payload or {}).get("response", "") or "").strip()
    if not response:
        return []
    parsed = _extract_json_payload(response)
    findings: list[str] = []
    if isinstance(parsed, Mapping):
        raw_findings = parsed.get("findings") or parsed.get("issues") or parsed.get("blockers") or []
        if isinstance(raw_findings, list):
            for item in raw_findings:
                text = _single_line(item, 240)
                if text:
                    findings.append(text)
                    if len(findings) >= limit:
                        return findings
    for line in response.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^(PASS|BLOCK)\b", stripped, flags=re.IGNORECASE):
            continue
        bullet = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)$", stripped)
        if bullet:
            finding = _single_line(bullet.group(1), 240)
            if finding:
                findings.append(finding)
        elif "block" in stripped.lower() or "missing" in stripped.lower() or "fix" in stripped.lower():
            findings.append(_single_line(stripped, 240))
        if len(findings) >= limit:
            break
    if findings:
        return findings
    return [_single_line(response, 240)] if response else []


def _print_verification_review_summary(payload: Mapping[str, Any]) -> None:
    provider = str(payload.get("provider", "") or "verifier")
    task = str(payload.get("task", "") or "verification").replace("_", " ")
    decision = _verification_review_decision(payload) or str(payload.get("status", "") or "reviewed")
    findings = _verification_review_findings(payload, limit=3)
    print(f"{task.title()} verifier feedback ({provider}): {decision}")
    for finding in findings:
        print(f"- {finding}")


def _autoformalizer_advisory_block_issues(payload: Mapping[str, Any] | None) -> list[str]:
    if not payload:
        return []
    if _verification_review_decision(payload) != "BLOCK":
        return []
    findings = _verification_review_findings(payload, limit=4)
    if not findings:
        findings = ["configured verifier returned BLOCK without detailed findings"]
    return [f"configured autoformalizer verifier returned BLOCK: {finding}" for finding in findings]


def _run_advisory_verification_review(
    *,
    task: str,
    provider: str,
    prompt: str,
    active_file: str = "",
) -> dict[str, Any]:
    configured_provider = resolve_verification_provider(task, explicit=provider)
    if is_local_verification_provider(configured_provider):
        _record_verifier_decision(
            task=task,
            provider="local",
            configured_provider=configured_provider,
            mode="local",
            ok=False,
            summary="configured local verifier uses deterministic checks only; no advisory review was run",
            active_file=active_file,
            issues=[],
        )
        return {
            "task": task,
            "provider": "local",
            "mode": "local",
            "status": "local-only",
            "response": "",
        }
    if is_command_verification_provider(configured_provider):
        result = run_command_verification_review(
            provider=configured_provider,
            task=task,
            prompt=prompt,
            cwd=_project_root(),
            timeout_s=1200,
        )
    else:
        result = run_model_verification_review(
            provider=configured_provider,
            task=task,
            prompt=prompt,
            system_prompt=_verification_review_system_prompt(task),
            timeout_s=1200,
        )
    payload = _verification_review_result_payload(result)
    _record_activity(
        "verification-advisory-review",
        f"{task.replace('_', ' ').title()} advisory review finished",
        active_file=active_file,
        **payload,
    )
    _print_verification_review_summary(payload)
    return payload


def _run_configured_blueprint_verification(
    parent_agent: Any,
    system_prompt: str,
    live_state: Mapping[str, Any],
    autonomy_state: dict[str, Any],
) -> dict[str, Any]:
    provider = resolve_verification_provider(BLUEPRINT_VERIFICATION_TASK)
    if (
        provider in {"main", "auto"}
        and not _verification_task_has_aux_overrides(BLUEPRINT_VERIFICATION_TASK)
    ):
        return _run_document_formalization_review_agent(parent_agent, system_prompt, live_state, autonomy_state)

    signature = _document_formalization_review_signature(live_state)
    autonomy_state["document_formalization_review_signature"] = signature
    autonomy_state["document_formalization_review_attempted"] = True
    autonomy_state["document_formalization_review_provider"] = provider
    prompt = _attach_live_proof_state(_document_formalization_review_prompt(dict(live_state)), live_state)
    active_file = str(live_state.get("active_file_label", "") or live_state.get("active_file", "") or "")
    _record_agent_activity(
        parent_agent,
        "formalization-review-start",
        "Starting configured document formalization statement/source review",
        active_file=active_file,
        provider=provider,
        blocker=str(live_state.get("current_blocker", "") or ""),
    )
    result = _run_advisory_verification_review(
        task=BLUEPRINT_VERIFICATION_TASK,
        provider=provider,
        prompt=prompt,
        active_file=active_file,
    )
    autonomy_state["document_formalization_review_result"] = result
    approval_stamped = False
    decision = _verification_review_decision(result)
    if decision == "PASS":
        approval_stamped = _stamp_blueprint_statement_review_approved(
            provider=provider,
            active_file=active_file,
        )
        if approval_stamped:
            autonomy_state.pop("document_formalization_review_signature", None)
    elif decision == "BLOCK":
        findings = _verification_review_findings(result, limit=8)
        lines = [
            "[EPFLEMMA FORMALIZATION STATEMENT REVIEW BLOCK]",
            "The independent statement/source verifier returned BLOCK. Continue formalization and address these findings before stopping again.",
            "- do not self-approve statement verification statuses",
            "- do not mark the proof-ready checklist item",
            "- update Lean statements, companion declarations, source coverage, scope-change records, or doc-comment proof nudges as needed",
            "",
            "Verifier findings:",
        ]
        lines.extend(f"- {finding}" for finding in findings) if findings else lines.append("- verifier returned BLOCK without detailed findings")
        autonomy_state["document_formalization_review_feedback_message"] = "\n".join(lines)
        autonomy_state["document_formalization_review_feedback_pending"] = True
    _record_agent_activity(
        parent_agent,
        "formalization-review-complete",
        "Configured document formalization statement/source review completed",
        active_file=active_file,
        provider=provider,
        status=str(result.get("status", "") or ""),
        mode=str(result.get("mode", "") or ""),
        decision=decision,
        approval_stamped=approval_stamped,
    )
    return {"messages": [], "interrupted": False, "verification_review": result}


def _autoformalizer_verification_prompt(
    *,
    active_file: str,
    ok: bool,
    summary: str,
    issues: Sequence[str],
    blueprint_text: str,
    target_text: str,
) -> str:
    issue_lines = "\n".join(f"- {issue}" for issue in issues) or "- [none]"
    blueprint_excerpt = _bounded_verifier_response(str(blueprint_text or ""), 10000)
    target_excerpt = _bounded_verifier_response(str(target_text or ""), 10000)
    return (
        "Review this EPFLemma document autoformalization handoff decision.\n\n"
        "The deterministic local verifier and Lean kernel checks remain authoritative. "
        "Your job is to identify source-fidelity, complete-proof-attachment, doc-comment nudge, blueprint, import, or Lean-readiness issues "
        "that the formalizer should fix; do not claim proof verification unless Lean did it.\n\n"
        "Important: the target Lean file may be an aggregator that imports generated sibling modules. "
        "Judge declarations and imports across the generated Lean excerpt, not only the entry file body.\n\n"
        "Start your response with exactly `PASS` or `BLOCK` on its own line. "
        "Use `BLOCK` if proof launch should not start until the formalizer corrects the draft. "
        "Then include `Findings:` and `Correction steps:` bullets.\n\n"
        f"- target Lean file: {_relative_file_label(active_file) or active_file or '[missing]'}\n"
        f"- deterministic local decision: {'passed' if ok else 'blocked'}\n"
        f"- deterministic summary: {summary or '[none]'}\n\n"
        "Deterministic issues:\n"
        f"{issue_lines}\n\n"
        "Blueprint excerpt:\n"
        f"```markdown\n{blueprint_excerpt}\n```\n\n"
        "Generated Lean excerpt:\n"
        f"```lean\n{target_excerpt}\n```\n\n"
        "Return concise, actionable feedback. Verifier agents are read-only reviewers; drafting agents apply corrections."
    )


def _maybe_run_autoformalizer_advisory_review(
    *,
    active_file: str,
    ok: bool,
    summary: str,
    issues: Sequence[str],
    blueprint_text: str,
    target_text: str,
) -> dict[str, Any] | None:
    provider = resolve_verification_provider(AUTOFORMALIZER_VERIFICATION_TASK)
    if is_local_verification_provider(provider):
        return None
    payload = {
        "task": AUTOFORMALIZER_VERIFICATION_TASK,
        "provider": provider,
        "active_file": _relative_file_label(active_file) or active_file,
        "ok": bool(ok),
        "summary": _single_line(summary, 400),
        "issues": [str(issue or "") for issue in issues[:10]],
        "blueprint_len": len(str(blueprint_text or "")),
        "target_len": len(str(target_text or "")),
    }
    signature = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if signature in _VERIFICATION_ADVISORY_RESULT_CACHE:
        return dict(_VERIFICATION_ADVISORY_RESULT_CACHE[signature])
    if not _cache_once(
        signature,
        cache=_VERIFICATION_ADVISORY_CACHE,
        order=_VERIFICATION_ADVISORY_CACHE_ORDER,
        limit=_VERIFICATION_ADVISORY_CACHE_LIMIT,
    ):
        return None
    prompt = _autoformalizer_verification_prompt(
        active_file=active_file,
        ok=ok,
        summary=summary,
        issues=issues,
        blueprint_text=blueprint_text,
        target_text=target_text,
    )
    result = _run_advisory_verification_review(
        task=AUTOFORMALIZER_VERIFICATION_TASK,
        provider=provider,
        prompt=prompt,
        active_file=active_file,
    )
    _VERIFICATION_ADVISORY_RESULT_CACHE[signature] = dict(result)
    return result


def _run_document_formalization_review_agent(
    parent_agent: Any,
    system_prompt: str,
    live_state: Mapping[str, Any],
    autonomy_state: dict[str, Any],
) -> dict[str, Any]:
    global _CURRENT_AGENT_ACTIVITY_DETAILS

    signature = _document_formalization_review_signature(live_state)
    autonomy_state["document_formalization_review_signature"] = signature
    autonomy_state["document_formalization_review_attempted"] = True
    _record_agent_activity(
        parent_agent,
        "formalization-review-start",
        "Starting independent document formalization statement/source review",
        active_file=str(live_state.get("active_file_label", "") or live_state.get("active_file", "") or ""),
        blocker=str(live_state.get("current_blocker", "") or ""),
    )
    parent_details = dict(_CURRENT_AGENT_ACTIVITY_DETAILS)
    parent_owner = _read_native_env("RUNNER_OWNER", "")
    reviewer = _build_agent()
    reviewer._parent_session_id = str(getattr(parent_agent, "session_id", "") or "")
    reviewer._delegate_depth = int(getattr(parent_agent, "_delegate_depth", 0) or 0) + 1
    reviewer._managed_autonomy_state = autonomy_state
    reviewer._managed_queue_edit_guard_state = {}
    reviewer._managed_initial_declaration_keys_by_file = {}
    reviewer._managed_step_boundary_closed = False

    _CURRENT_AGENT_ACTIVITY_DETAILS = _agent_activity_details(reviewer)
    try:
        result = _run_managed_conversation(
            reviewer,
            user_message=_attach_live_proof_state(_document_formalization_review_prompt(dict(live_state)), live_state),
            system_message=system_prompt,
            conversation_history=[],
            persist_user_message="[epflemma-native independent formalization statement/source review]",
        )
        _record_turn_activity([], list(result.get("messages", []) or []), phase="formalization-review")
        _record_agent_activity(
            reviewer,
            "formalization-review-complete",
            "Independent document formalization statement/source review completed",
            interrupted=bool(result.get("interrupted")),
        )
        return result
    finally:
        _CURRENT_AGENT_ACTIVITY_DETAILS = parent_details
        if parent_owner:
            os.environ["EPFLEMMA_NATIVE_RUNNER_OWNER"] = parent_owner


def _maybe_run_document_formalization_review_agent(
    agent: Any,
    system_prompt: str,
    live_state: Mapping[str, Any],
    autonomy_state: dict[str, Any],
) -> bool:
    if not _document_formalization_review_due(live_state, autonomy_state):
        return False
    _run_configured_blueprint_verification(agent, system_prompt, live_state, autonomy_state)
    return True


def _final_file_sweep_block(live_state: Mapping[str, Any]) -> str:
    active_file = str(live_state.get("active_file", "") or live_state.get("active_file_label", "") or "[unknown]")
    active_file_label = _display_file_label(live_state) or active_file
    blocker = str(live_state.get("current_blocker", "") or live_state.get("diagnostics", "") or "unknown remaining issue").strip()
    verification_hint = _queue_item_verification_hint(str(live_state.get("active_file", "") or ""))
    warning_cleanup_pending = bool(live_state.get("final_sweep_warning_cleanup_pending"))
    warning_count = int(live_state.get("final_sweep_warning_count", 0) or 0)
    warning_summary = str(live_state.get("final_sweep_warning_summary", "") or "").strip()
    if warning_cleanup_pending:
        # Warning-only cleanup mode: file already passes verification; this is
        # the spec's one focused whole-file warning-cleanup window. Bound to a
        # single attempt — if the model breaks the file the runner restores
        # the baseline and accepts the original warning-only state.
        lines = [
            "Queue status:",
            "- declaration queue is empty",
            f"- file: {active_file_label}",
            f"- exact tool path: {active_file}",
            "- file verification: passing (lake build clean; only style warnings remain)",
            (
                f"- final file verification: {verification_hint}"
                if verification_hint
                else "- final file verification: [unknown]"
            ),
            "",
            "Final file sweep — warning cleanup (1/1 opportunity):",
            f"- {warning_count} warning(s) remain in `{active_file_label}`",
        ]
        if warning_summary:
            lines.append("- detected warnings (first few shown):")
            lines.extend(f"  {line}" for line in warning_summary.splitlines()[:6])
        lines.extend(
            [
                "- this is your one focused whole-file warning-cleanup opportunity",
                (
                    "- expected effort: read the file with `read_file`, then make at least one safe edit "
                    "through `apply_verified_patch` before bailing. Low-risk fixes include: replacing "
                    "deprecated `push_neg` with `push Not`; removing a `try { ... }` or `<;> try { ... }` "
                    "whose tactic is reported as never executed; deleting an `all_goals X`/`<;> X` reported "
                    "as doing nothing; renaming an unused parameter to `_` (or dropping it from the proof "
                    "body if it's a `have`); removing a `simp`/`linarith`/`omega` reported as unused."
                ),
                "- safety: edit only lines flagged by the linter. Do NOT touch theorem statements, proof "
                "structure, or any unflagged tactic. The runner will restore the file to its pre-cleanup "
                "content if your edit causes a hard verification failure.",
                "- after one focused edit, stop and let the manager re-verify; the manager runs `lean_verify` "
                "automatically.",
                (
                    "- bail clause: if you have read the file and identified that no safe cleanup remains, "
                    "emit a final report. The warnings will be accepted as-is and the file marked verified. "
                    "The bail clause requires that you actually inspected the file first — emitting a final "
                    "report without reading the file is not the intended use of this opportunity."
                ),
            ]
        )
        return "\n".join(lines)
    return "\n".join(
        [
            "Queue status:",
            "- declaration queue is empty",
            f"- file: {active_file_label}",
            f"- exact tool path: {active_file}",
            f"- current blocker: {blocker}",
            (
                f"- final file verification: {verification_hint}"
                if verification_hint
                else "- final file verification: [unknown]"
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
    entry = _find_declaration_entry(current_file, current_target)
    item_reasons = " ".join(str(reason or "") for reason in item.get("reasons", []) or [])
    local_cleanup_reason = ""
    if "contains sorry" in item_reasons.lower():
        local_cleanup_reason = "contains sorry"
    check = {
        "ok": not _goals_still_open(goals),
        "file_check_ok": False,
        "output": diagnostics,
        "goals": goals,
        "local_cleanup_reason": local_cleanup_reason,
    }
    feedback_kind = _manager_feedback_kind(current_file, current_target, check)
    return bool(
        feedback_kind in {"error", "sorry"}
        or (entry and entry.get("has_sorry"))
        or _goals_still_open(goals)
    )


def _result_exhausted_api_steps(result: Mapping[str, Any], agent: Any | None = None) -> bool:
    if not isinstance(result, Mapping):
        return False
    if bool(result.get("interrupted")) and not _is_step_boundary_interrupt(result):
        return False
    reason = str(result.get("exit_reason", "") or "").strip()
    if reason in {"max_iterations", "iteration_budget_exhausted"}:
        return True
    if bool(result.get("completed")):
        return False
    try:
        api_calls = int(result.get("api_calls", 0) or 0)
    except (TypeError, ValueError):
        api_calls = 0
    try:
        max_turns = int(getattr(agent, "max_iterations", 0) or 0)
    except (TypeError, ValueError):
        max_turns = 0
    return bool(max_turns > 0 and api_calls >= max_turns)


def _queue_assignment_slice_body(slice_text: str) -> str:
    raw = str(slice_text or "").strip()
    if not raw:
        return ""
    _, separator, body = raw.partition(":\n")
    candidate = body if separator else raw
    if "-- [truncated declaration slice]" in candidate:
        return ""
    return candidate.strip()


def _failed_attempt_comment_lines(
    current_text: str,
    *,
    target_symbol: str,
    max_lines: int = 120,
    max_chars: int = 20_000,
) -> list[str]:
    text = str(current_text or "").strip()
    if not text:
        return []
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
        truncated = True
    attempt_lines = text.splitlines()
    if len(attempt_lines) > max_lines:
        attempt_lines = attempt_lines[:max_lines]
        truncated = True
    lines = [
        "-- EPFLemma failed attempt preserved after API step budget exhaustion.",
        f"-- Declaration: {target_symbol or '[unknown]'}",
        "-- The active proof was restored to the baseline `sorry` body below.",
        "-- Failed attempt:",
    ]
    for line in attempt_lines:
        lines.append(f"-- {line}" if line else "--")
    if truncated:
        lines.append("-- [truncated failed attempt]")
    return lines


def _restore_queue_assignment_to_baseline_sorry(
    autonomy_state: Mapping[str, Any],
    live_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    assignment = dict(autonomy_state.get("current_queue_assignment") or {})
    target_symbol = str(assignment.get("target_symbol", "") or "").strip()
    active_file = str(assignment.get("active_file", "") or "").strip()
    baseline_body = _queue_assignment_slice_body(str(assignment.get("slice", "") or ""))
    if not target_symbol or not active_file:
        return {"restored": False, "reason": "missing queue assignment"}
    if not baseline_body:
        return {"restored": False, "reason": "missing untruncated baseline declaration slice"}
    if not re.search(r"\bsorry\b", _strip_lean_comments_and_strings(baseline_body)):
        return {"restored": False, "reason": "baseline declaration slice does not contain sorry"}
    entry = _find_declaration_entry(active_file, target_symbol)
    if not entry:
        return {"restored": False, "reason": "current declaration entry not found"}
    current_text = str(entry.get("text", "") or "").strip()
    if current_text == baseline_body:
        return {"restored": False, "reason": "current declaration is already at baseline sorry"}
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or 0)
    if start <= 0 or end < start:
        return {"restored": False, "reason": "invalid declaration range"}
    path = Path(active_file)
    try:
        original_text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"restored": False, "reason": f"could not read active file: {exc}"}
    original_lines = original_text.splitlines()
    replacement_lines = (
        _failed_attempt_comment_lines(current_text, target_symbol=target_symbol)
        + baseline_body.splitlines()
    )
    new_lines = original_lines[: start - 1] + replacement_lines + original_lines[end:]
    new_text = "\n".join(new_lines)
    if original_text.endswith("\n"):
        new_text += "\n"
    try:
        path.write_text(new_text, encoding="utf-8")
    except Exception as exc:
        return {"restored": False, "reason": f"could not restore baseline sorry: {exc}"}
    return {
        "restored": True,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "line": start,
        "end_line": end,
        "reason": "reverted current declaration to its baseline `sorry` slice after API step budget exhaustion",
    }


def _api_step_budget_handoff_message(
    *,
    target_symbol: str,
    active_file: str,
    api_calls: int,
    max_turns: int,
    attempt_recorded: bool,
    restore_result: Mapping[str, Any],
) -> str:
    restored = bool(restore_result.get("restored"))
    restore_line = (
        "restored the current declaration to its baseline `sorry` slice"
        if restored
        else f"no file restore was applied ({restore_result.get('reason', 'not needed')})"
    )
    return "\n".join(
        [
            "[EPFLEMMA-NATIVE API STEP BUDGET EXHAUSTED]",
            "",
            f"- declaration: {target_symbol or '[unknown]'}",
            f"- file: {active_file or '[unknown]'}",
            f"- API steps used: {api_calls}/{max_turns}" if max_turns else f"- API steps used: {api_calls}",
            "- manager action: recorded this as a failed focused attempt" if attempt_recorded else "- manager action: no failed attempt was recorded",
            f"- safe-state action: {restore_line}",
            "- next action: continue this same queue item from the recorded failed-attempt state; do not claim the theorem is solved until file verification clears it.",
        ]
    ).strip()


def _handle_api_step_budget_exhaustion(
    agent: Any,
    result: Mapping[str, Any],
    history: list[dict[str, Any]],
    autonomy_state: dict[str, Any],
    live_state: Mapping[str, Any] | None,
    *,
    cycle: int = 0,
    phase: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    if not _single_queue_item_turn_enabled() or not _result_exhausted_api_steps(result, agent):
        return history, dict(live_state or {}), False
    if not _same_queue_assignment_still_blocked(autonomy_state, live_state):
        return history, dict(live_state or {}), False

    assignment = dict(autonomy_state.get("current_queue_assignment") or {})
    target_symbol = str(assignment.get("target_symbol", "") or "").strip()
    active_file = str(assignment.get("active_file", "") or "").strip()
    try:
        api_calls = int(result.get("api_calls", 0) or 0)
    except (TypeError, ValueError):
        api_calls = 0
    try:
        max_turns = int(getattr(agent, "max_iterations", 0) or 0)
    except (TypeError, ValueError):
        max_turns = 0

    original_assignment = dict(assignment)
    if original_assignment:
        mgr = _queue_manager_from_state(autonomy_state)
        mgr.assign(
            QueueItem.from_mapping({"label": target_symbol}),
            active_file=active_file,
            slice_text=str(original_assignment.get("slice", "") or ""),
            prepare=PrepareState.from_mapping(original_assignment.get("incremental_prepare")),
        )
        _flush_queue_manager(autonomy_state, mgr)
    _remember_failed_attempt(autonomy_state, live_state, cycle_number=cycle, refresh_baseline=False)
    restore_result = _restore_queue_assignment_to_baseline_sorry(autonomy_state, live_state)
    if restore_result.get("restored"):
        manager_check = _manager_verify_queue_file(active_file)
        restore_result = dict(restore_result)
        restore_result["manager_verification"] = manager_check

    message = _api_step_budget_handoff_message(
        target_symbol=target_symbol,
        active_file=active_file,
        api_calls=api_calls,
        max_turns=max_turns,
        attempt_recorded=True,
        restore_result=restore_result,
    )
    updated_history = list(history or []) + [{"role": "user", "content": message}]
    updated_live_state = _build_live_proof_state(updated_history, _journal_status())
    updated_live_state = _promote_live_state_to_verified_compat(updated_live_state, autonomy_state)

    print("")
    print(
        f"⚠️  API step budget exhausted while working on {target_symbol}; "
        "recorded a failed attempt and refreshed the queue state."
    )
    if restore_result.get("restored"):
        print("↻ Restored the declaration to its baseline `sorry` slice before continuing.")
    else:
        print(f"↻ Baseline restore skipped: {restore_result.get('reason', 'not needed')}.")
    _record_activity(
        "api-step-budget-exhausted",
        f"API step budget exhausted for {target_symbol}",
        phase=phase,
        cycle=cycle,
        target_symbol=target_symbol,
        active_file=active_file,
        api_calls=api_calls,
        max_turns=max_turns,
        restore=restore_result,
    )
    return updated_history, updated_live_state, True


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
    autonomy_state: Mapping[str, Any] | None = None,
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
    last_verification = _last_verification_record(autonomy_state)
    build_status = _verification_status_text(last_verification)
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
                if not _inspection_queue_item_is_queue_blocker(merged, active_file, diagnostics):
                    continue
                if not merged.get("search_hints"):
                    merged["search_hints"] = [label, str(merged.get("kind", "") or "").strip()]
                if not merged.get("verification_gate"):
                    merged["verification_gate"] = _canonical_file_verification_command(active_file)
                if not merged.get("blocker_signature"):
                    merged["blocker_signature"] = f"{label or 'queue'}:{merged.get('line', '?')}"
                enriched_queue.append(merged)
        declaration_queue = enriched_queue
    declaration_queue = _filter_document_formalization_proof_queue(declaration_queue)
    document_formalization_proof_sorry_count = (
        _document_formalization_generated_proof_sorry_count(active_file)
        if _document_formalization_requested() and active_file
        else 0
    )
    document_handoff = _document_formalization_handoff_verification(
        active_file,
        diagnostics=diagnostics,
        sorry_count=sorry_count if isinstance(sorry_count, int) else None,
        last_verification=last_verification,
    )
    document_review_pending = _document_formalization_blueprint_waiting_for_review()
    if _document_formalization_requested() and document_review_pending and bool(document_handoff.get("ok")):
        document_handoff = {
            "ok": False,
            "issues": ["statement/source verification is pending independent review"],
            "summary": "document formalization handoff verifier blocked queue: statement/source verification is pending independent review",
        }
    document_handoff_blocked = _document_formalization_requested() and (
        not bool(document_handoff.get("ok")) or document_review_pending
    )
    if document_handoff_blocked:
        declaration_queue = []
    current_queue_item = _current_queue_item(declaration_queue, active_file)
    current_queue_label = str((current_queue_item or {}).get("label", "") or "").strip()
    queue_needs_final_file_sweep = (
        declaration_scope == "file"
        and bool(active_file)
        and not declaration_queue
        and not document_handoff_blocked
        and not document_review_pending
        and not (
            _workflow_kind() == "formalize"
            and _document_formalization_requested()
            and isinstance(sorry_count, int)
            and sorry_count > 0
        )
    )
    if declaration_scope == "file" and current_queue_label:
        target_symbol = current_queue_label
    elif queue_needs_final_file_sweep:
        target_symbol = ""
    current_queue_prefix = _declaration_prefix_text(active_file, current_queue_label) if current_queue_label else ""
    current_queue_slice = _declaration_slice_text(active_file, current_queue_label) if current_queue_label else ""
    declaration_queue_summary = _format_declaration_queue(declaration_queue)
    if document_handoff_blocked:
        declaration_queue_summary = str(document_handoff.get("summary", "") or declaration_queue_summary)
    current_blocker = blocker_summary or ", ".join((current_queue_item or {}).get("reasons", []) or [])
    if document_handoff_blocked:
        current_blocker = str(document_handoff.get("summary", "") or current_blocker)
        blocker_summary = current_blocker
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
        "last_verification": last_verification,
        "declaration_scope": declaration_scope,
        "declaration_queue_total": len(declaration_queue),
        "declaration_queue": list(declaration_queue),
        "declaration_queue_preview": list(declaration_queue[:8]),
        "declaration_queue_summary": declaration_queue_summary,
        "current_queue_item": dict(current_queue_item or {}),
        "current_queue_item_prefix": current_queue_prefix,
        "current_queue_item_slice": current_queue_slice,
        "current_blocker": current_blocker,
        "queue_needs_final_file_sweep": queue_needs_final_file_sweep,
        "sorry_count": sorry_count,
        "document_formalization_proof_sorry_count": document_formalization_proof_sorry_count,
        "project_sorry_count": project_sorry_count,
        "project_sorry_files": list(project_sorry_files),
        "blocker_summary": blocker_summary,
        "verification_hint": verification_hint,
        "capability_report": capability_report,
        "recent_empty_search_streak": empty_search_streak,
        "search_exhausted": search_exhausted,
        "document_formalization_handoff": dict(document_handoff),
    }
    route_decision = route_workflow_step(
        _workflow_kind(),
        provisional_state,
        configured_skill=_base_active_skill(),
        cwd=_project_root(),
    ).to_dict()
    if document_handoff_blocked:
        route_decision.update(
            {
                "skill_name": "lean-formalization",
                "route_action": "planner-review-gate",
                "recommended_worker": "",
                "reason": str(document_handoff.get("summary", "") or "document formalization handoff is blocked"),
            }
        )
    if current_queue_item and route_decision.get("recommended_worker"):
        current_queue_item = dict(current_queue_item)
        current_queue_item.setdefault(
            "recommended_worker",
            str(route_decision.get("recommended_worker", "") or ""),
        )
    degraded_summary = ", ".join(capability_report.get("degraded_reasons", []) or []) or "[none]"
    route_summary = str(route_decision.get("reason", "") or "[none]")
    route_action = str(route_decision.get("route_action", "") or "[none]")
    model_diagnostics = _diagnostics_for_queue_horizon(
        active_file=active_file,
        target_symbol=target_symbol,
        diagnostics=diagnostics,
        declaration_scope=declaration_scope,
        queue_needs_final_file_sweep=queue_needs_final_file_sweep,
    )
    model_queue_summary = _queue_horizon_summary(
        declaration_scope=declaration_scope,
        queue_needs_final_file_sweep=queue_needs_final_file_sweep,
        current_queue_item=current_queue_item,
        declaration_queue_summary=declaration_queue_summary,
        declaration_queue_total=len(declaration_queue),
    )
    model_proof_status = _proof_status_lines_for_queue_horizon(
        active_file=active_file,
        target_symbol=target_symbol,
        declaration_scope=declaration_scope,
        queue_needs_final_file_sweep=queue_needs_final_file_sweep,
        sorry_count=sorry_count,
        project_sorry_count=project_sorry_count,
        project_sorry_files=list(project_sorry_files),
    )
    project_prove_summary = _project_prove_manager_summary(autonomy_state)
    body = "\n".join(
        (
            [
                LIVE_PROOF_STATE_PREFIX,
                "",
                f"Workflow: {_workflow_kind()}",
                f"Active file: {active_file_label or '[unknown]'}",
                f"Active file path: {active_file or '[unknown]'}",
                f"Target theorem: {target_symbol or ('[full-file verification sweep]' if queue_needs_final_file_sweep else '[unknown]')}",
                "",
                "Diagnostics:",
                model_diagnostics,
                "",
                "Goals:",
                goals,
                "",
                "Build:",
                build_status or "no recent manager verification",
                "",
                "Queue horizon:",
                model_queue_summary,
                "",
                "Document formalization handoff verifier:",
                str(document_handoff.get("summary", "") or "[not active]"),
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
            ]
            + (["Project manager:", project_prove_summary, ""] if project_prove_summary else [])
            + [
                "Capabilities:",
                f"degraded reasons: {degraded_summary}",
                "",
                "Proof status:",
                *model_proof_status,
            ]
        )
    ).strip()
    live_state = {
        "active_file": active_file,
        "active_file_label": active_file_label,
        "target_symbol": target_symbol,
        "diagnostics": diagnostics,
        "goals": goals,
        "build_status": build_status,
        "last_verification": last_verification,
        "declaration_scope": declaration_scope,
        "declaration_queue_total": len(declaration_queue),
        "declaration_queue": list(declaration_queue),
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
        "document_formalization_handoff": dict(document_handoff),
        "project_prove_manager": _project_prove_manager_active(autonomy_state),
        "project_prove_file_queue": list(dict(autonomy_state or {}).get("project_prove_file_queue", []) or []),
        "project_prove_completed_files": list(dict(autonomy_state or {}).get("project_prove_completed_files", []) or []),
        "project_prove_plan_source": str(dict(autonomy_state or {}).get("project_prove_plan_source", "") or ""),
        "project_prove_plan_reason": str(dict(autonomy_state or {}).get("project_prove_plan_reason", "") or ""),
        "message": body,
    }
    if _workflow_kind() in AUTONOMOUS_WORKFLOW_KINDS:
        live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
        live_scope = str(live_state.get("declaration_scope", "") or declaration_scope)
        live_target = str(live_state.get("target_symbol", "") or "")
        live_active_file = str(live_state.get("active_file", "") or "")
        live_final_sweep = bool(live_state.get("queue_needs_final_file_sweep"))
        live_diagnostics = _diagnostics_for_queue_horizon(
            active_file=live_active_file,
            target_symbol=live_target,
            diagnostics=str(live_state.get("diagnostics", "") or ""),
            declaration_scope=live_scope,
            queue_needs_final_file_sweep=live_final_sweep,
        )
        live_queue_summary = _queue_horizon_summary(
            declaration_scope=live_scope,
            queue_needs_final_file_sweep=live_final_sweep,
            current_queue_item=dict(live_state.get("current_queue_item", {}) or {}),
            declaration_queue_summary=str(live_state.get("declaration_queue_summary", "") or "[none]"),
            declaration_queue_total=int(live_state.get("declaration_queue_total", 0) or 0),
        )
        live_proof_status = _proof_status_lines_for_queue_horizon(
            active_file=live_active_file,
            target_symbol=live_target,
            declaration_scope=live_scope,
            queue_needs_final_file_sweep=live_final_sweep,
            sorry_count=live_state.get("sorry_count"),
            project_sorry_count=live_state.get("project_sorry_count"),
            project_sorry_files=list(live_state.get("project_sorry_files", []) or []),
        )
        live_project_prove_summary = _project_prove_manager_summary(autonomy_state)
        live_document_handoff = dict(live_state.get("document_formalization_handoff", {}) or {})
        live_state["message"] = "\n".join(
            (
                [
                    LIVE_PROOF_STATE_PREFIX,
                    "",
                    f"Workflow: {_workflow_kind()}",
                    f"Active file: {live_state.get('active_file_label') or '[unknown]'}",
                    f"Active file path: {live_state.get('active_file') or '[unknown]'}",
                    f"Target theorem: {live_state.get('target_symbol') or '[unknown]'}",
                    "",
                    "Diagnostics:",
                    live_diagnostics,
                    "",
                    "Goals:",
                    str(live_state.get("goals", "") or "unavailable"),
                    "",
                    "Build:",
                    str(live_state.get("build_status", "") or "no recent manager verification"),
                    "",
                    "Queue horizon:",
                    live_queue_summary,
                    "",
                    "Document formalization handoff verifier:",
                    str(live_document_handoff.get("summary", "") or "[not active]"),
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
                ]
                + (["Project manager:", live_project_prove_summary, ""] if live_project_prove_summary else [])
                + [
                    "Capabilities:",
                    "degraded reasons: "
                    + (
                        ", ".join(dict(live_state.get("capability_report", {}) or {}).get("degraded_reasons", []) or [])
                        or "[none]"
                    ),
                    "",
                    "Proof status:",
                    *live_proof_status,
                ]
            )
        ).strip()
    return live_state


def _build_live_proof_state_compat(
    history: list[dict[str, Any]],
    checkpoint_state: Mapping[str, Any] | None = None,
    autonomy_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return _build_live_proof_state(history, checkpoint_state, autonomy_state)
    except TypeError:
        return _build_live_proof_state(history, checkpoint_state)


def _attach_live_proof_state(user_message: str, live_state: Mapping[str, Any]) -> str:
    block = str(live_state.get("message", "") or "").strip()
    supplemental = _startup_additional_skill_contracts(_effective_skill_name(live_state))
    parts = [str(user_message or "").strip()]
    if block:
        parts.append(block)
    if supplemental:
        parts.append(supplemental)
    return "\n\n".join(part for part in parts if part).strip()


def _diagnostics_indicate_failure(diagnostics: str) -> bool:
    return diagnostics_indicate_actionable_failure(diagnostics)


def _diagnostics_indicate_hard_failure(diagnostics: str) -> bool:
    items = diagnostic_items(diagnostics)
    if items:
        return any(
            str(item.get("severity", "") or "").strip().lower() == "error"
            for item in items
        )
    lowered = (diagnostics or "").lower()
    for token in ("no errors found", "no errors", "without errors"):
        lowered = lowered.replace(token, "")
    hard_patterns = (
        r"\berror\b",
        r"\berrors\b",
        r"\bunsolved\b",
        r"\btype mismatch\b",
        r"\bunknown constant\b",
        r"\bfailed to synthesize\b",
        r"\bdeterministic timeout\b",
        r"\bmaximum number of heartbeats\b",
        r"\btactic execution\b",
    )
    return any(re.search(pattern, lowered) for pattern in hard_patterns)


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
    declaration_scope = str(live_state.get("declaration_scope", "") or "project")
    sorry_count = live_state.get("sorry_count")
    project_sorry_count = live_state.get("project_sorry_count")
    verification_ok = live_state.get("verification_ok")
    verification_outcome = _verification_outcome(live_state=live_state)
    verification_passed = verification_outcome == "ok"
    declaration_queue_total = int(live_state.get("declaration_queue_total", 0) or 0)

    if not active_file:
        return False
    if _document_formalization_needs_planner_draft(active_file):
        return False
    if _document_formalization_needs_blueprint_plan():
        return False
    document_handoff = _document_formalization_handoff_verification(
        active_file,
        diagnostics=diagnostics,
        sorry_count=sorry_count if isinstance(sorry_count, int) else None,
        last_verification=_last_verification_record(live_state=live_state),
        completion=True,
    )
    if not bool(document_handoff.get("ok")):
        return False
    # The final-sweep warning-cleanup gate granted a one-shot cleanup turn;
    # the file is NOT fully verified until that turn runs (or is bypassed
    # by the no-regression path on the next promote pass). Without this
    # check the project-prove manager treats the file as done, advances to
    # the next file, and pops the cleanup state via `_assign_project_prove_file`
    # — so the cleanup conversation never gets to drive the model. That's
    # exactly why warnings persist across multi-file project workflows.
    if bool(live_state.get("final_sweep_warning_cleanup_pending")):
        return False
    if _document_formalization_requested() and _document_formalization_generated_proof_sorry_count(active_file) > 0:
        return False
    if isinstance(sorry_count, int) and sorry_count > 0:
        return False
    if declaration_scope != "file" and isinstance(project_sorry_count, int) and project_sorry_count > 0:
        return False
    warning_only_final_file = (
        declaration_scope == "file"
        and declaration_queue_total == 0
        and verification_passed
        and not _diagnostics_indicate_hard_failure(diagnostics)
    )
    if _diagnostics_indicate_failure(diagnostics) and not warning_only_final_file:
        return False
    if verification_outcome == "failed":
        return False
    if verification_outcome == "unverified" and "reported errors" in build_status:
        return False
    if _goals_still_open(goals):
        return False
    return verification_passed


def _document_formalization_needs_planner_draft(active_file: str) -> bool:
    if not _document_formalization_requested() or not active_file:
        return False
    generated_paths = _formalization_generated_lean_paths(active_file)
    if generated_paths:
        for path in generated_paths:
            try:
                generated_text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            if re.search(LEAN_DECLARATION_PREAMBLE_RE, generated_text, flags=re.MULTILINE):
                return False
    try:
        text = Path(active_file).read_text(encoding="utf-8")
    except Exception:
        return False
    has_declaration = bool(re.search(LEAN_DECLARATION_PREAMBLE_RE, text, flags=re.MULTILINE))
    if has_declaration:
        return False
    non_import_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("--") and not line.lstrip().startswith("import ")
    ]
    if not non_import_lines:
        return True
    return "EPFLemma formalization target scaffold" in text or "EPFLemma created this file as the active formalization target" in text


def _lean_imports_from_text(text: str) -> list[str]:
    imports: list[str] = []
    for match in re.finditer(
        r"^\s*import\s+([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)\b",
        _strip_lean_comments_and_strings(str(text or "")),
        flags=re.MULTILINE,
    ):
        module = match.group(1).strip()
        if module and module not in imports:
            imports.append(module)
    return imports


def _lean_imports_from_file(path: Path) -> list[str]:
    try:
        return _lean_imports_from_text(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _valid_lean_module_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return all(re.match(r"^[A-Za-z_][A-Za-z0-9_']*$", part) for part in text.split("."))


def _blueprint_import_plan_imports(text: str) -> list[str]:
    section = _blueprint_import_plan_section(text)
    if not section:
        return []
    imports: list[str] = []

    def _add(module: str) -> None:
        normalized = str(module or "").strip()
        if normalized.endswith(".lean") or "/" in normalized:
            return
        if _valid_lean_module_name(normalized) and normalized not in imports:
            imports.append(normalized)

    for match in re.finditer(
        r"^\s*import\s+([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)\b",
        section,
        flags=re.MULTILINE,
    ):
        _add(match.group(1))
    in_non_direct_subsection = False
    for line in section.splitlines():
        lowered = line.lower()
        if re.match(r"^\s*#{1,6}\s+", line) or re.match(r"^\s*(?:direct|suggested|search|notes?)\s*:", lowered):
            in_non_direct_subsection = any(token in lowered for token in ("suggest", "search", "candidate", "transitive", "note"))
            continue
        if any(token in lowered for token in ("suggested search", "search module", "candidate module", "transitive", "not required", "prover may")):
            continue
        if in_non_direct_subsection:
            continue
        if not line.lstrip().startswith("-"):
            continue
        code_span = re.search(r"`([^`]+)`", line)
        if code_span:
            _add(code_span.group(1))
            continue
        bullet = re.search(
            r"-\s*([A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*)\b",
            line,
        )
        if bullet:
            _add(bullet.group(1))
    return imports


def _lean_decl_names_from_planned_value(value: str) -> list[str]:
    names: list[str] = []

    def _add(raw: str) -> None:
        candidate = str(raw or "").strip()
        candidate = candidate.split(":", 1)[0].strip()
        candidate = candidate.split(" ", 1)[0].strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_'.]*$", candidate) and candidate not in names:
            names.append(candidate)

    for span in re.findall(r"`([^`]+)`", str(value or "")):
        _add(span)
    if not names:
        for chunk in re.split(r"[,;]|\band\b", str(value or "")):
            _add(chunk)
    return names


def _formalization_generated_lean_text(
    active_file: str,
    *,
    active_text: str = "",
) -> str:
    chunks: list[str] = []
    active_path = Path(active_file).expanduser() if active_file else None
    try:
        active_resolved = active_path.resolve() if active_path is not None else None
    except Exception:
        active_resolved = active_path
    for path in _formalization_generated_lean_paths(active_file):
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        try:
            text = (
                str(active_text or "")
                if active_resolved is not None and resolved == active_resolved and active_text
                else path.read_text(encoding="utf-8")
            )
        except Exception:
            continue
        label = _relative_file_label(str(path)) or str(path)
        chunks.append(f"/- EPFLemma generated file: {label} -/\n{text}")
    if chunks:
        return "\n\n".join(chunks)
    return str(active_text or "")


def _formalization_generated_imports(active_file: str, *, active_text: str = "") -> list[str]:
    imports: list[str] = []
    active_path = Path(active_file).expanduser() if active_file else None
    try:
        active_resolved = active_path.resolve() if active_path is not None else None
    except Exception:
        active_resolved = active_path
    for path in _formalization_generated_lean_paths(active_file):
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        try:
            text = (
                str(active_text or "")
                if active_resolved is not None and resolved == active_resolved and active_text
                else path.read_text(encoding="utf-8")
            )
        except Exception:
            continue
        for module in _lean_imports_from_text(text):
            if module not in imports:
                imports.append(module)
    if not imports and active_text:
        imports = _lean_imports_from_text(active_text)
    return imports


def _formalization_generated_module_names(active_file: str) -> set[str]:
    root = Path(_project_root()).expanduser().resolve()
    modules: set[str] = set()
    for path in _formalization_generated_lean_paths(active_file):
        module = _module_name_for_project_path(path, root)
        if module:
            modules.add(module)
    return modules


def _document_formalization_construction_sorry_issues(active_file: str, target_text: str = "") -> list[str]:
    issues: list[str] = []
    scanned_paths = _formalization_generated_lean_paths(active_file)
    if not scanned_paths and active_file:
        scanned_paths = [Path(active_file)]
    for path in scanned_paths:
        try:
            text = (
                str(target_text or "")
                if active_file and path.resolve() == Path(active_file).expanduser().resolve() and target_text
                else path.read_text(encoding="utf-8")
            )
        except Exception:
            continue
        for entry in _declaration_line_index_from_text(text):
            kind = str(entry.get("kind", "") or "").strip().lower()
            if kind not in CONSTRUCTION_DECLARATION_KINDS or not entry.get("has_sorry"):
                continue
            name = str(entry.get("name", "") or "").strip() or f"[{kind} at line {entry.get('line', '?')}]"
            line = int(entry.get("line", 0) or 0)
            label = _relative_file_label(str(path))
            location = f"{label}:{line}" if line > 0 else label
            issues.append(
                f"construction gap before proof handoff: `{name}` is a {kind} declaration with `sorry` at {location}; "
                "finish the construction or rewrite it as an explicit theorem/lemma proof obligation before `/prove`"
            )
    return issues


def _document_formalization_generated_proof_sorry_count(active_file: str = "") -> int:
    total = 0
    for path in _formalization_generated_lean_paths(active_file):
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for entry in _declaration_line_index_from_text(text):
            if str(entry.get("kind", "") or "").strip().lower() in PROOF_DECLARATION_KINDS and entry.get("has_sorry"):
                total += 1
    return total


def _filter_document_formalization_proof_queue(queue: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not (_workflow_kind() == "formalize" and _document_formalization_requested()):
        return [dict(item) for item in queue if isinstance(item, Mapping)]
    filtered: list[dict[str, Any]] = []
    for item in queue:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind", "") or "").strip().lower()
        if kind in PROOF_DECLARATION_KINDS:
            filtered.append(dict(item))
    return filtered


def _lean_declaration_preceding_comment_window(
    target_text: str,
    entry: Mapping[str, Any],
    *,
    max_lines: int = 16,
) -> str:
    try:
        line = int(entry.get("line", 0) or 0)
    except Exception:
        line = 0
    if line <= 1:
        return ""
    lines = str(target_text or "").splitlines()
    start = max(0, line - max_lines - 1)
    end = max(0, min(line - 1, len(lines)))
    return "\n".join(lines[start:end]).strip()


def _lean_comment_has_source_proof_notes(comment: str) -> bool:
    lowered = str(comment or "").lower()
    required_markers = (
        "source proof",
        "proof sketch",
        "proof strategy",
        "prover notes",
        "paper proof",
    )
    return any(marker in lowered for marker in required_markers)


def _document_formalization_blueprint_inventory_issues(
    blueprint_text: str,
    target_text: str,
) -> list[str]:
    issues: list[str] = []
    entries = _blueprint_source_inventory_entries(blueprint_text)
    target_entries = _declaration_entries_by_name_from_text(target_text)
    target_decl_names = set(target_entries)
    for block in _document_formalization_manifest_blocks():
        label = block["label"]
        kind = block["kind"]
        requires_proof_notes = bool(block.get("has_proof")) or kind in {"theorem", "lemma", "proposition", "corollary"}
        entry = entries.get(label, "")
        if not entry:
            issues.append(f"blueprint is missing source inventory entry `{label}`")
            continue
        locator = _blueprint_first_bullet_value(
            entry,
            ("Source locator", "Source location", "Source line/page", "Source lines", "Source page"),
        )
        if _blueprint_value_missing(locator):
            issues.append(f"blueprint entry `{label}` is missing a concrete source locator")
        elif _document_formalization_requested():
            source_relative = _read_text_env("EPFLEMMA_FORMALIZATION_DOCUMENT_RELATIVE", "").strip()
            if source_relative and source_relative not in locator:
                issues.append(
                    f"blueprint entry `{label}` source locator should include `{source_relative}` so the prover can reopen the source"
                )
        planned = _blueprint_first_bullet_value(
            entry,
            (
                "Planned Lean declarations",
                "Planned Lean declaration",
                "Formal names",
                "Formal name",
                "Lean declarations",
                "Lean declaration",
            ),
        )
        planned_block = (
            _blueprint_bullet_block(entry, "Planned Lean declarations")
            or _blueprint_bullet_block(entry, "Planned Lean declaration")
            or _blueprint_bullet_block(entry, "Lean declarations")
            or _blueprint_bullet_block(entry, "Lean declaration")
        )
        if _blueprint_value_missing(planned):
            planned = planned_block
        elif planned_block and planned_block not in planned:
            planned = f"{planned}\n{planned_block}"
        if _blueprint_value_missing(planned):
            issues.append(f"blueprint entry `{label}` has no concrete planned Lean declarations")
            planned_names: list[str] = []
        else:
            planned_names = _lean_decl_names_from_planned_value(planned)
            if not planned_names:
                issues.append(f"blueprint entry `{label}` planned declarations are not parseable")
            else:
                missing = [name for name in planned_names if name not in target_decl_names]
                if missing:
                    issues.append(
                        f"blueprint entry `{label}` names declarations missing from generated Lean files: "
                        + ", ".join(f"`{name}`" for name in missing)
                    )

        review = _blueprint_bullet_block(entry, "Formal statement review") or _blueprint_bullet_value(entry, "Formal statement review")
        if _blueprint_block_missing(review):
            issues.append(f"blueprint entry `{label}` is missing a statement-fidelity review")
        source_qualifiers = _blueprint_fidelity_field(entry, ("Source qualifiers", "Source qualifier", "Fidelity axes"))
        if _blueprint_fidelity_field_unresolved(source_qualifiers):
            issues.append(f"blueprint entry `{label}` is missing resolved source qualifiers / fidelity axes")
        lean_coverage = _blueprint_fidelity_field(entry, ("Lean coverage", "Formal coverage", "Lean statement coverage"))
        if _blueprint_fidelity_field_unresolved(lean_coverage):
            issues.append(f"blueprint entry `{label}` is missing resolved Lean coverage for the source qualifiers")
        scope_changes = _blueprint_fidelity_field(entry, ("Scope changes", "Intentional scope changes", "Scope change"))
        if _blueprint_fidelity_field_unresolved(scope_changes):
            issues.append(f"blueprint entry `{label}` must explicitly record scope changes, or `none`")

        verification = _blueprint_first_bullet_value(
            entry,
            (
                "Statement verification status",
                "Statement/source verification",
                "Source verification status",
                "Verification status",
            ),
        )
        if _blueprint_value_missing(verification):
            issues.append(
                f"blueprint entry `{label}` is missing statement/source verification approval; "
                "run the review workflow to check and correct the planned Lean statements before proving"
            )
        elif not re.search(r"\b(approved|verified|reviewed|accepted)\b", verification, flags=re.IGNORECASE):
            issues.append(
                f"blueprint entry `{label}` statement/source verification is not approved"
            )

        notes = _blueprint_first_bullet_value(entry, ("Source proof / prover notes", "Proof strategy", "Prover notes"))
        if _blueprint_value_missing(notes):
            issues.append(f"blueprint entry `{label}` is missing source proof/prover notes")
        if requires_proof_notes and notes.lower() in {"none", "none needed", "n/a"}:
            issues.append(f"blueprint entry `{label}` needs prover notes for its {kind}")
        if requires_proof_notes:
            for name in planned_names:
                target_entry = target_entries.get(name, {})
                decl_kind = str(target_entry.get("kind", "") or "").strip().lower()
                if decl_kind not in {"theorem", "lemma", "example"}:
                    continue
                comment = _lean_declaration_preceding_comment_window(target_text, target_entry)
                if not _lean_comment_has_source_proof_notes(comment):
                    issues.append(
                        f"Lean doc comment above `{name}` is missing source proof/prover notes"
                    )
    return issues


def _root_module_file_for_module(module_name: str) -> tuple[str, Path] | None:
    if not module_name or "." not in module_name:
        return None
    root_module = module_name.split(".", 1)[0]
    if not _valid_lean_module_name(root_module):
        return None
    return root_module, Path(_project_root()) / f"{root_module}.lean"


def _module_file_for_module(module_name: str) -> Path | None:
    if not _valid_lean_module_name(module_name):
        return None
    return Path(_project_root()) / Path(*module_name.split(".")).with_suffix(".lean")


def _document_formalization_handoff_verification(
    active_file: str,
    *,
    diagnostics: str | None = None,
    sorry_count: int | None = None,
    last_verification: Mapping[str, Any] | None = None,
    completion: bool = False,
) -> dict[str, Any]:
    if not _document_formalization_requested() or not active_file:
        return {"ok": True, "issues": [], "summary": "document formalization not active"}

    issues: list[str] = []
    blueprint_local_issues: list[str] = []
    active_path = Path(active_file).expanduser()
    try:
        active_path = active_path.resolve()
    except Exception:
        pass

    if _document_formalization_needs_planner_draft(str(active_path)):
        issues.append("planner has not drafted Lean declarations in the target file")
    if _document_formalization_needs_blueprint_plan():
        issue = "blueprint still contains preflight placeholders or `_pending_` entries"
        issues.append(issue)
        blueprint_local_issues.append(issue)

    blueprint_path = _read_text_env("EPFLEMMA_FORMALIZATION_BLUEPRINT", "").strip()
    blueprint_text = ""
    if blueprint_path:
        try:
            blueprint_text = Path(blueprint_path).read_text(encoding="utf-8")
        except Exception:
            issue = "blueprint file could not be read"
            issues.append(issue)
            blueprint_local_issues.append(issue)
    else:
        issue = "blueprint file is not configured"
        issues.append(issue)
        blueprint_local_issues.append(issue)

    target_text = ""
    try:
        target_text = active_path.read_text(encoding="utf-8")
    except Exception:
        issues.append("target Lean file could not be read")
    generated_text = _formalization_generated_lean_text(str(active_path), active_text=target_text)
    construction_issues = _document_formalization_construction_sorry_issues(str(active_path), target_text)
    issues.extend(construction_issues)

    diagnostic_text = str(diagnostics or "").strip()
    if diagnostic_text and _diagnostics_indicate_hard_failure(diagnostic_text):
        issues.append(
            "target Lean file has hard diagnostics before prover handoff: "
            + _single_line(diagnostic_text, 260)
        )
    verification_record = dict(last_verification or {})
    project_verification_issue = ""
    if not (
        bool(verification_record.get("ok"))
        and str(verification_record.get("scope", "") or "").strip().lower() == "project"
        and str(verification_record.get("tool", "") or "").strip() == "lean_verify"
    ):
        project_verification_issue = (
            "project-level Lean verification has not passed; run `lean_verify(mode=project)` "
            "after the generated formalization files and root imports are in place"
        )

    target_imports = _formalization_generated_imports(str(active_path), active_text=target_text)
    generated_modules = _formalization_generated_module_names(str(active_path))
    module_name = _module_name_for_file(str(active_path))
    root_info = _root_module_file_for_module(module_name)
    if root_info:
        root_module, root_file = root_info
        if root_module in target_imports:
            issues.append(
                f"target module `{module_name}` still imports root module `{root_module}`; "
                "replace the scaffold import with direct dependencies before project-wide inclusion"
            )
        elif not root_file.is_file():
            issues.append(
                f"root module file `{root_file.name}` is missing, so plain `lake build` will not include `{module_name}`"
            )
        else:
            root_imports = _lean_imports_from_file(root_file)
            root_imports_target = module_name in root_imports
            parent_module = module_name.rsplit(".", 1)[0] if "." in module_name else ""
            parent_file = _module_file_for_module(parent_module) if parent_module else None
            root_imports_parent = bool(parent_module and parent_module in root_imports and parent_file)
            parent_imports_target = bool(
                root_imports_parent
                and parent_file is not None
                and parent_file.is_file()
                and module_name in _lean_imports_from_file(parent_file)
            )
            if root_imports_parent and parent_file is not None and not parent_file.is_file():
                issues.append(
                    f"root module `{root_module}` imports parent module `{parent_module}`, "
                    f"but parent module file `{parent_file.name}` is missing"
                )
            elif root_imports_parent and not parent_imports_target:
                issues.append(
                    f"parent module `{parent_module}` does not import `{module_name}`, so plain `lake build` can skip it"
                )
            elif not root_imports_target and not parent_imports_target:
                expected = f"`{module_name}`"
                if parent_module:
                    expected += f" or parent module `{parent_module}`"
                issues.append(
                    f"root module `{root_module}` does not import {expected}, so plain `lake build` can skip it"
                )

    if blueprint_text:
        checklist_issues = _document_formalization_blueprint_checklist_issues(blueprint_text)
        blueprint_local_issues.extend(checklist_issues)
        issues.extend(checklist_issues)

        inventory_issues = _document_formalization_blueprint_inventory_issues(blueprint_text, generated_text)
        blueprint_local_issues.extend(inventory_issues)
        issues.extend(inventory_issues)

        planned_imports = _blueprint_import_plan_imports(blueprint_text)
        if planned_imports:
            missing_from_target = [module for module in planned_imports if module not in target_imports]
            missing_from_plan = [
                module for module in target_imports
                if module not in planned_imports and module not in generated_modules
            ]
            if missing_from_target:
                issues.append(
                    "blueprint import plan mentions modules not imported by generated Lean files: "
                    + ", ".join(f"`{module}`" for module in missing_from_target)
                )
            if missing_from_plan:
                issues.append(
                    "generated Lean files import external modules missing from the blueprint import plan: "
                    + ", ".join(f"`{module}`" for module in missing_from_plan)
                )

        if project_verification_issue:
            issues.append(project_verification_issue)

        if completion and isinstance(sorry_count, int) and sorry_count == 0:
            if re.search(r"^\s*-\s*Status:\s*active formalization\s*$", blueprint_text, flags=re.MULTILINE | re.IGNORECASE):
                issues.append("blueprint status is still `active formalization` after proofs are complete")
            handoff_match = re.search(
                r"^\s*-\s*\[(?P<checked>[ xX])\]\s*(?:Hand stable (?:theorem/lemma/example )?`sorry` declarations to the managed prover queue|Mark stable theorem/lemma/example `sorry` declarations ready for a user-started prove workflow)\.",
                blueprint_text,
                flags=re.MULTILINE,
            )
            if handoff_match and handoff_match.group("checked") == " ":
                issues.append("blueprint proof-ready handoff checklist item is still unchecked")

    local_ok = not issues
    local_summary = (
        "document formalization handoff verifier passed"
        if local_ok
        else "document formalization handoff verifier blocked queue: " + "; ".join(issues[:5])
    )
    if len(issues) > 5:
        local_summary += f"; plus {len(issues) - 5} more issue(s)"
    advisory_payload = (
        _maybe_run_autoformalizer_advisory_review(
            active_file=str(active_path),
            ok=local_ok,
            summary=local_summary,
            issues=issues,
            blueprint_text=blueprint_text,
            target_text=generated_text,
        )
        if _autoformalizer_advisory_review_due(local_ok=local_ok, issues=issues, completion=completion)
        else None
    )
    advisory_block_issues = _autoformalizer_advisory_block_issues(advisory_payload)
    if advisory_block_issues:
        issues.extend(advisory_block_issues)

    ok = not issues
    summary = (
        "document formalization handoff verifier passed"
        if ok
        else "document formalization handoff verifier blocked queue: " + "; ".join(issues[:5])
    )
    if len(issues) > 5:
        summary += f"; plus {len(issues) - 5} more issue(s)"
    blueprint_summary = (
        "blueprint verifier passed"
        if not blueprint_local_issues
        else "blueprint verifier blocked queue: " + "; ".join(blueprint_local_issues[:5])
    )
    if len(blueprint_local_issues) > 5:
        blueprint_summary += f"; plus {len(blueprint_local_issues) - 5} more issue(s)"
    _record_verifier_decision(
        task=BLUEPRINT_VERIFICATION_TASK,
        provider="local",
        configured_provider=resolve_verification_provider(BLUEPRINT_VERIFICATION_TASK),
        mode="local",
        ok=not blueprint_local_issues,
        summary=blueprint_summary,
        active_file=str(active_path),
        issues=blueprint_local_issues,
    )
    _record_verifier_decision(
        task=AUTOFORMALIZER_VERIFICATION_TASK,
        provider="local",
        configured_provider=resolve_verification_provider(AUTOFORMALIZER_VERIFICATION_TASK),
        mode="local",
        ok=local_ok,
        summary=local_summary,
        active_file=str(active_path),
        issues=issues[: len(issues) - len(advisory_block_issues)] if advisory_block_issues else issues,
    )
    if advisory_payload:
        decision = _verification_review_decision(advisory_payload)
        advisory_findings = _verification_review_findings(advisory_payload, limit=6)
        if decision == "PASS" and local_ok:
            _stamp_blueprint_statement_review_approved(
                provider=str(advisory_payload.get("provider", "") or "configured"),
                active_file=str(active_path),
            )
        advisory_summary = (
            f"configured autoformalizer verifier returned {decision}"
            if decision
            else "configured autoformalizer verifier returned no explicit PASS/BLOCK decision"
        )
        if advisory_findings:
            advisory_summary += ": " + "; ".join(advisory_findings[:3])
        _record_verifier_decision(
            task=AUTOFORMALIZER_VERIFICATION_TASK,
            provider=str(advisory_payload.get("provider", "") or "configured"),
            configured_provider=resolve_verification_provider(AUTOFORMALIZER_VERIFICATION_TASK),
            mode=str(advisory_payload.get("mode", "") or "configured"),
            ok=decision != "BLOCK",
            summary=advisory_summary,
            active_file=str(active_path),
            issues=advisory_findings,
        )
    return {"ok": ok, "issues": issues, "summary": summary}


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
        "- queue-step acceptance tool: `lean_incremental_check(action=check_target)` on the active file and assigned declaration\n"
        f"- final Lake sweep command when requested or at queue end: `{command}`\n"
        "- use `lean_inspect` for iteration; when the proof is ready, verify the assigned declaration with `lean_incremental_check`\n"
        "- if the active file still reports errors, treat those errors as blockers before moving to future `sorry` items\n"
        "- future queued `sorry` warnings do not belong to this theorem turn; stop after this assigned declaration is clean\n"
        "- a declaration disappearing from the pending queue is not enough by itself when the file gate is still failing\n"
        "- do not treat `lake build`, `grep`, `head`, or truncated output as proof that this theorem-sized repair is clean"
    )


def _recommended_verification_command(active_file: str) -> str:
    relative_label = _relative_file_label(active_file)
    if _workflow_kind() == "formalize" and _document_formalization_requested() and active_file:
        return (
            f"`lean_inspect` on {relative_label}, then `lean_verify(mode=project)` for final draft readiness; "
            "use module/file verification only for intermediate iteration; "
            "use the document formalization handoff verifier for blueprint/source-review readiness, "
            "not terminal Lake checks"
        )
    if _single_queue_item_turn_enabled() and active_file:
        return (
            f"`lean_inspect` on {relative_label}, then `lean_incremental_check(check_target)` "
            "for this file-scoped queue step"
        )
    module_name = _module_name_for_file(active_file)
    if module_name:
        return "`lean_inspect` first, then `lean_verify(mode=module)` when the file is close to clean"
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


def _log_manager_verification(
    active_file: str,
    *,
    full_project: bool,
    ok: bool,
    build_status: str,
    scope: str = "",
    target_symbol: str = "",
    tool: str = "",
    cache: str = "",
    elapsed_s: Any = None,
    errors: int | None = None,
    warnings: int | None = None,
    sorry_count: int | None = None,
) -> None:
    scope = str(scope or ("project" if full_project else "file"))
    status = "passed" if ok else "failed"
    file_label = _relative_file_label(active_file) or active_file or "[unknown]"
    detail = _single_line(build_status, 520) or "[no output]"
    signature = (scope, file_label, bool(ok), detail)
    if signature in _MANAGER_VERIFICATION_LOG_CACHE:
        return
    if len(_MANAGER_VERIFICATION_LOG_CACHE_ORDER) >= _MANAGER_VERIFICATION_LOG_CACHE_LIMIT:
        old = _MANAGER_VERIFICATION_LOG_CACHE_ORDER.popleft()
        _MANAGER_VERIFICATION_LOG_CACHE.discard(old)
    _MANAGER_VERIFICATION_LOG_CACHE_ORDER.append(signature)
    _MANAGER_VERIFICATION_LOG_CACHE.add(signature)
    target_label = str(target_symbol or "").strip()
    display_scope = f"target {target_label}" if scope.startswith("target:") and target_label else scope
    details = {
        "active_file": active_file,
        "active_file_label": file_label,
        "full_project": full_project,
        "verification_ok": bool(ok),
        "build_status": build_status,
    }
    if scope != ("project" if full_project else "file"):
        details["scope"] = scope
    if target_label:
        details["target_symbol"] = target_label
    if tool:
        details["tool"] = tool
    if cache:
        details["cache"] = cache
    if elapsed_s not in (None, "", 0, 0.0):
        details["elapsed_s"] = elapsed_s
    if errors is not None:
        details["errors"] = errors
    if warnings is not None:
        details["warnings"] = warnings
    if sorry_count is not None:
        details["sorry_count"] = sorry_count
    _record_activity(
        "manager-verification",
        f"Manager verification ({display_scope}) {status}",
        **details,
    )
    print("")
    print(f"🔎 Manager verification ({display_scope}): {status}")
    print(f"   file: {file_label}")
    if scope.startswith("target:"):
        metrics = []
        if tool:
            metrics.append(f"tool: {tool}")
        if cache:
            metrics.append(f"cache: {cache}")
        if elapsed_s not in (None, "", 0, 0.0):
            metrics.append(f"elapsed: {elapsed_s}s")
        if errors is not None:
            metrics.append(f"errors: {errors}")
        if warnings is not None:
            metrics.append(f"warnings: {warnings}")
        if sorry_count is not None:
            metrics.append(f"sorry: {sorry_count}")
        if metrics:
            print(f"   {'  '.join(metrics)}")
        if detail and detail != "[no output]":
            print(f"   check: {detail}")
    else:
        print(f"   check: {detail}")


def _promote_live_state_to_verified(
    live_state: Mapping[str, Any] | None,
    autonomy_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = dict(live_state or {})
    if not normalized or not normalized.get("active_file"):
        return normalized
    normalized["verification_ok"] = False
    active_file = str(normalized.get("active_file", "") or "")
    if _document_formalization_needs_planner_draft(active_file):
        normalized["blocker_summary"] = "document formalization planner has not drafted Lean declarations yet"
        normalized["build_status"] = normalized.get("build_status") or "waiting for document formalization draft"
        return normalized
    if _document_formalization_needs_blueprint_plan():
        normalized["blocker_summary"] = "document formalization blueprint has not been updated from the preflight placeholder"
        normalized["build_status"] = normalized.get("build_status") or "waiting for document formalization blueprint plan"
        return normalized
    document_handoff = _document_formalization_handoff_verification(
        active_file,
        diagnostics=str(normalized.get("diagnostics", "") or ""),
        sorry_count=normalized.get("sorry_count") if isinstance(normalized.get("sorry_count"), int) else None,
        last_verification=_last_verification_record(autonomy_state, normalized),
        completion=True,
    )
    normalized["document_formalization_handoff"] = dict(document_handoff)
    if not bool(document_handoff.get("ok")):
        normalized["blocker_summary"] = str(document_handoff.get("summary", "") or "document formalization handoff blocked")
        normalized["build_status"] = normalized.get("build_status") or "waiting for document formalization handoff verifier"
        return normalized
    declaration_scope = str(normalized.get("declaration_scope", "") or _declaration_queue_scope())
    diagnostics = str(normalized.get("diagnostics", "") or "")
    declaration_queue_total = int(normalized.get("declaration_queue_total", 0) or 0)
    warning_only_final_file = (
        declaration_scope == "file"
        and declaration_queue_total == 0
        and not _diagnostics_indicate_hard_failure(diagnostics)
    )
    if _diagnostics_indicate_failure(diagnostics) and not warning_only_final_file:
        return normalized
    if _goals_still_open(str(normalized.get("goals", "") or "")):
        return normalized
    sorry_count = normalized.get("sorry_count")
    if isinstance(sorry_count, int) and sorry_count > 0:
        return normalized

    project_sorry_count, project_sorry_files = _count_project_sorries(_project_root())
    normalized["project_sorry_count"] = project_sorry_count
    normalized["project_sorry_files"] = project_sorry_files
    ok, build_status = _run_explicit_verification_build(active_file, full_project=False)
    record = _record_manager_verification(
        autonomy_state,
        active_file,
        "",
        {"ok": ok, "command": build_status, "output": build_status},
        "lean_verify",
        log=False,
    )
    record["scope"] = "file"
    record["summary"] = build_status
    _store_last_verification(autonomy_state, record)
    _log_manager_verification(active_file, full_project=False, ok=ok, build_status=build_status)
    verification_ok = bool(ok)
    needs_full_project_build = (
        verification_ok
        and isinstance(project_sorry_count, int)
        and project_sorry_count == 0
        and bool(_module_name_for_file(active_file))
    )
    if needs_full_project_build:
        verification_ok, build_status = _run_explicit_verification_build(active_file, full_project=True)
        record = _record_manager_verification(
            autonomy_state,
            active_file,
            "",
            {"ok": verification_ok, "command": build_status, "output": build_status},
            "lean_verify",
            full_project=True,
            log=False,
        )
        record["scope"] = "project"
        record["summary"] = build_status
        _store_last_verification(autonomy_state, record)
        _log_manager_verification(
            active_file,
            full_project=True,
            ok=verification_ok,
            build_status=build_status,
        )
    normalized["build_status"] = build_status
    proof_solved_for_cleanup = (
        bool(verification_ok)
        and declaration_scope == "file"
        and declaration_queue_total == 0
        and not _goals_still_open(str(normalized.get("goals", "") or ""))
        and not (isinstance(sorry_count, int) and sorry_count > 0)
    )

    # Spec line 672: the final file sweep is the canonical place for the
    # worker to clean whole-file residual warnings. Lake build does not see
    # style linter warnings, so a passing lake build alone leaves them
    # forever. We grant exactly one focused cleanup cycle here when warnings
    # remain and the one-shot flag is unset; on the second pass the flag is
    # set, so we either accept (clean / warnings-only) or restore baseline
    # (model introduced a hard issue) and accept the original warning-only
    # state. "Wouldn't overkill it" — never loops, never stalls.
    final_sweep_cleanup_already_attempted = bool(
        isinstance(autonomy_state, dict)
        and autonomy_state.get("final_sweep_cleanup_attempted")
    )
    final_sweep_cleanup_turn_started = bool(
        isinstance(autonomy_state, dict)
        and autonomy_state.get("final_sweep_cleanup_turn_started")
    )
    final_sweep_baseline = dict(autonomy_state.get("final_sweep_baseline") or {}) if isinstance(autonomy_state, dict) else {}
    final_sweep_baseline_captured = final_sweep_baseline.get("content") is not None
    if (
        verification_ok
        and not final_sweep_cleanup_already_attempted
        and declaration_scope == "file"
        and declaration_queue_total == 0
    ):
        warning_count, warning_summary = _active_file_warning_summary(normalized)
        if warning_count > 0 and isinstance(autonomy_state, dict):
            if _capture_final_sweep_baseline(autonomy_state, active_file):
                autonomy_state["final_sweep_cleanup_attempted"] = True
                autonomy_state["final_sweep_warning_summary"] = warning_summary
                normalized = _with_warning_cleanup_state(
                    normalized,
                    status="pending",
                    proof_solved=True,
                    warning_count=warning_count,
                    warning_summary=warning_summary,
                    diagnostics=diagnostics,
                    attempted=True,
                    verified=False,
                )
                normalized["final_sweep_warning_cleanup_pending"] = True
                normalized["final_sweep_warning_count"] = warning_count
                normalized["final_sweep_warning_summary"] = warning_summary
                # Suppress verified status so the workflow loop runs one more
                # cycle with the cleanup-pending state visible to the prompt.
                verification_ok = False
                normalized["queue_needs_final_file_sweep"] = True
                normalized["blocker_summary"] = (
                    f"final-sweep warning cleanup pending: {warning_count} warning(s) remain"
                )
                _record_activity(
                    "final-sweep-warning-cleanup-granted",
                    f"Granted one focused whole-file warning cleanup ({warning_count} warning(s))",
                    active_file=active_file,
                    warning_count=warning_count,
                )
                print("")
                print(
                    f"🟢 Final file sweep — warning cleanup opportunity granted (1/1): "
                    f"{warning_count} warning(s) remain on the active file"
                )
                if warning_summary:
                    for line in warning_summary.splitlines()[:3]:
                        print(f"   {line}")
                normalized["last_verification"] = _last_verification_record(autonomy_state, normalized)
                normalized["verification_ok"] = False
                return normalized
            normalized = _with_warning_cleanup_state(
                normalized,
                status="skipped",
                proof_solved=True,
                warning_count=warning_count,
                warning_summary=warning_summary,
                diagnostics="warning cleanup skipped because the active file baseline could not be captured",
                attempted=False,
                verified=False,
            )
            _record_activity(
                "final-sweep-warning-cleanup-skipped",
                "Skipped final-sweep warning cleanup because the active file baseline could not be captured",
                active_file=active_file,
                warning_count=warning_count,
            )

    if (
        final_sweep_cleanup_already_attempted
        and final_sweep_baseline_captured
        and not final_sweep_cleanup_turn_started
        and declaration_scope == "file"
        and declaration_queue_total == 0
        and isinstance(autonomy_state, dict)
    ):
        # The cleanup window was granted on a previous promote pass, but the
        # model has not yet received that cleanup turn. Keep the workflow open
        # instead of interpreting "granted" as "already attempted".
        warning_count, warning_summary = _active_file_warning_summary(normalized)
        if warning_count <= 0:
            autonomy_state.pop("final_sweep_baseline", None)
            normalized = _with_warning_cleanup_state(
                normalized,
                status="verified",
                proof_solved=True,
                warning_count=0,
                warning_summary="",
                diagnostics="warning cleanup verified; no warnings remain",
                attempted=True,
                verified=True,
            )
        else:
            warning_summary = warning_summary or str(autonomy_state.get("final_sweep_warning_summary", "") or "")
            normalized = _with_warning_cleanup_state(
                normalized,
                status="pending",
                proof_solved=True,
                warning_count=warning_count,
                warning_summary=warning_summary,
                diagnostics=diagnostics,
                attempted=True,
                verified=False,
            )
            normalized["final_sweep_warning_cleanup_pending"] = True
            normalized["final_sweep_warning_count"] = warning_count
            normalized["final_sweep_warning_summary"] = warning_summary
            normalized["queue_needs_final_file_sweep"] = True
            normalized["blocker_summary"] = (
                f"final-sweep warning cleanup pending: {warning_count} warning(s) remain"
            )
            normalized["last_verification"] = _last_verification_record(autonomy_state, normalized)
            normalized["verification_ok"] = False
            return normalized

    if (
        final_sweep_cleanup_already_attempted
        and final_sweep_cleanup_turn_started
        and declaration_scope == "file"
        and declaration_queue_total == 0
        and isinstance(autonomy_state, dict)
        and dict(autonomy_state.get("final_sweep_baseline") or {}).get("content") is not None
    ):
        # Cleanup turn happened. If lake/lean is now unhappy, the model broke
        # the file trying to clean style warnings — restore to the captured
        # baseline and proceed as warning-tolerant accept.
        cleanup_blocked_diagnostics = ""
        if not verification_ok:
            restored = _restore_final_sweep_baseline(autonomy_state, active_file)
            if restored:
                cleanup_blocked_diagnostics = build_status
                _record_activity(
                    "final-sweep-warning-cleanup-restored",
                    "Restored active file from final-sweep baseline after cleanup attempt regressed",
                    active_file=active_file,
                )
                print("")
                print(
                    "↩️  Final file sweep cleanup attempt regressed verification; "
                    "restored active file to pre-cleanup baseline and accepting warning-only state."
                )
                ok, build_status = _run_explicit_verification_build(active_file, full_project=False)
                verification_ok = bool(ok)
                normalized["build_status"] = build_status
            else:
                cleanup_blocked_diagnostics = build_status
        # One-shot complete; drop the heavy baseline payload from autonomy_state.
        autonomy_state.pop("final_sweep_baseline", None)
        autonomy_state.pop("final_sweep_cleanup_turn_started", None)
        if cleanup_blocked_diagnostics:
            normalized = _with_warning_cleanup_state(
                normalized,
                status="blocked",
                proof_solved=bool(verification_ok),
                warning_count=_active_file_warning_summary(normalized)[0],
                warning_summary=_active_file_warning_summary(normalized)[1],
                diagnostics=cleanup_blocked_diagnostics,
                attempted=True,
                verified=False,
            )
        elif verification_ok:
            remaining_warning_count, remaining_warning_summary = _active_file_warning_summary(normalized)
            cleanup_status = "verified" if remaining_warning_count == 0 else "accepted"
            normalized = _with_warning_cleanup_state(
                normalized,
                status=cleanup_status,
                proof_solved=True,
                warning_count=remaining_warning_count,
                warning_summary=remaining_warning_summary,
                diagnostics=(
                    "warning cleanup verified; no warnings remain"
                    if remaining_warning_count == 0
                    else "warning cleanup accepted; remaining warnings accepted after one cleanup pass"
                ),
                attempted=True,
                verified=True,
            )
    elif final_sweep_cleanup_already_attempted and proof_solved_for_cleanup:
        remaining_warning_count, remaining_warning_summary = _active_file_warning_summary(normalized)
        cleanup_status = "verified" if remaining_warning_count == 0 else "accepted"
        normalized = _with_warning_cleanup_state(
            normalized,
            status=cleanup_status,
            proof_solved=True,
            warning_count=remaining_warning_count,
            warning_summary=remaining_warning_summary,
            diagnostics=(
                "warning cleanup verified; no warnings remain"
                if remaining_warning_count == 0
                else "warning cleanup accepted; remaining warnings accepted after one cleanup pass"
            ),
            attempted=True,
            verified=True,
        )
    elif proof_solved_for_cleanup and declaration_scope == "file" and declaration_queue_total == 0:
        warning_count, warning_summary = _active_file_warning_summary(normalized)
        if warning_count <= 0:
            normalized = _with_warning_cleanup_state(
                normalized,
                status="skipped",
                proof_solved=True,
                warning_count=0,
                warning_summary="",
                diagnostics="warning cleanup skipped because the solved file has no warnings",
                attempted=False,
                verified=False,
            )

    normalized["last_verification"] = _last_verification_record(autonomy_state, normalized)
    normalized["verification_ok"] = bool(verification_ok)
    cleanup_status = str(normalized.get("warning_cleanup_status", "") or "").strip().lower()
    if cleanup_status in {"verified", "accepted", "skipped", "blocked"}:
        _record_final_sweep_cleanup_outcome_once(
            autonomy_state,
            status=cleanup_status,
            active_file=active_file,
            warning_count=int(normalized.get("warning_cleanup_warning_count", 0) or 0),
            diagnostics=str(normalized.get("warning_cleanup_diagnostics", "") or ""),
        )
    if verification_ok:
        normalized["queue_needs_final_file_sweep"] = False
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


def _promote_live_state_to_verified_compat(
    live_state: Mapping[str, Any] | None,
    autonomy_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return _promote_live_state_to_verified(live_state, autonomy_state)
    except TypeError:
        return _promote_live_state_to_verified(live_state)


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
    except (KeyboardInterrupt, InterruptedError):
        return _fallback_checkpoint_summary(history, label=label, trigger=trigger, note=note, live_state=live_state)
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
    max_turns_raw = _read_text_env("AGENT_MAX_TURNS", "200")
    try:
        max_turns = max(1, int(max_turns_raw))
    except ValueError:
        max_turns = 200

    if not model:
        raise SystemExit("epflemma-native: EPFLEMMA_NATIVE_MODEL is not configured")
    if not base_url or not api_key:
        raise SystemExit("epflemma-native: provider credentials are incomplete")

    toolset_name = _read_native_env("TOOLSET", "epflemma-native") or "epflemma-native"
    logging_cfg = _logging_config()
    agent_cfg = _agent_config()
    configured_reasoning_effort = str(agent_cfg.get("reasoning_effort", "auto") or "auto")
    runtime_reasoning_effort = _read_native_env("REASONING_EFFORT")
    if runtime_reasoning_effort and configured_reasoning_effort.strip().lower() == "auto":
        configured_reasoning_effort = runtime_reasoning_effort
    reasoning_cfg = _parse_managed_reasoning_config(configured_reasoning_effort)
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
    project_root = _project_root()
    managed_tool_task_id = f"epflemma-native-{getattr(agent, 'session_id', '') or os.getpid()}"
    agent._managed_tool_task_id = managed_tool_task_id
    os.environ["TERMINAL_CWD"] = project_root
    try:
        from tools.terminal_tool import register_task_env_overrides

        register_task_env_overrides(managed_tool_task_id, {"cwd": project_root})
    except Exception:
        pass
    agent._managed_base_reasoning_config = dict(reasoning_cfg or {}) if reasoning_cfg else None
    _disable_generic_lean_statement_guard_for_native_runner()

    def _pre_tool_call_callback(function_name: str, _args: Mapping[str, Any]) -> str | None:
        return _managed_pre_tool_call(agent, function_name, _args)

    def _post_tool_result_callback(function_name: str, _args: Mapping[str, Any], _result: str) -> None:
        guard_feedback = _restore_out_of_scope_queue_edit(agent, function_name)
        _handle_managed_tool_result(agent, function_name, _args, _result)
        if guard_feedback:
            previous_appendix = str(getattr(agent, "_post_tool_result_appendix", "") or "").strip()
            setattr(
                agent,
                "_post_tool_result_appendix",
                f"{previous_appendix}\n\n{guard_feedback}".strip() if previous_appendix else guard_feedback,
            )

    agent.pre_tool_call_callback = _pre_tool_call_callback
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
    formalization_document = _read_text_env("EPFLEMMA_FORMALIZATION_DOCUMENT_RELATIVE", "").strip()
    if formalization_document:
        print(f"Document: {formalization_document}")
        target = _read_text_env("EPFLEMMA_FORMALIZATION_TARGET_FILE", "").strip()
        if target:
            print(f"Formalization target: {target}")
    print(f"Run log: {_workflow_state_root() / 'latest-run.log'}")
    print("")
    print("Commands: /help, /status, /status <agent> [N], /swarm [agent] [N], /proof-state, /diagnostics, /goals, /history, /checkpoint [note], /rollback <N>, /resume-plan [N], /compact, /exit, Ctrl+C")
    print("Inspect later from the shell with /workflow activity or /workflow log 120.")
    print("")


def _interactive_mode_label(live_state: Mapping[str, Any] | None = None) -> str:
    workflow_kind = _workflow_kind()
    if workflow_kind in {"formalize", "autoformalize"}:
        return "formalizer-agent"
    if workflow_kind in {"prove", "autoprove"}:
        return "prover-agent"
    return "prover-agent"


def _print_interactive_mode_header(live_state: Mapping[str, Any] | None = None) -> None:
    live_state = dict(live_state or {})
    phase = str(live_state.get("build_status", "") or "managed session")
    active_file = str(live_state.get("active_file_label", "") or "[unknown file]")
    theorem = str(live_state.get("target_symbol", "") or "[unknown target]")
    mode_label = _interactive_mode_label(live_state)
    print("")
    print("─" * 78)
    print(f"{mode_label} mode  ·  {phase}")
    print(f"file: {active_file}  ·  target: {theorem}")
    print("commands: /status  /status <agent> [N]  /swarm [agent] [N]  /proof-state  /diagnostics  /goals  /history  /compact  /exit  Ctrl+C")
    print("─" * 78)


def _run_managed_conversation(
    agent: AIAgent,
    *,
    on_interrupt: Callable[[], None] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    managed_task_id = str(getattr(agent, "_managed_tool_task_id", "") or "").strip()
    if managed_task_id and not kwargs.get("task_id"):
        kwargs["task_id"] = managed_task_id
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
            print(f"Returned to {_interactive_mode_label()} mode after interrupt.")
            return result
        error_type = type(error).__name__
        error_text = str(error).strip() or repr(error)
        summary = f"{error_type}: {_single_line(error_text, 240)}"
        messages = list(getattr(agent, "_session_messages", []) or kwargs.get("conversation_history") or [])
        print("")
        print(f"⚠️  Managed workflow stopped after provider/API error: {summary}")
        return {
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "failed": True,
            "partial": True,
            "error": summary,
            "final_response": f"Managed workflow stopped after provider/API error: {summary}",
        }

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
        print(f"Returned to {_interactive_mode_label()} mode after interrupt.")
        return result
    if not isinstance(result, dict):
        raise RuntimeError("Managed conversation did not return a result payload")

    if result.get("interrupted") and not _is_step_boundary_interrupt(result):
        print(f"Returned to {_interactive_mode_label()} mode after interrupt.")
    return result


def _managed_conversation_failed(result: Mapping[str, Any] | None) -> bool:
    payload = dict(result or {})
    return bool(payload.get("failed") or payload.get("error"))


def _record_managed_conversation_failure(result: Mapping[str, Any], *, phase: str) -> None:
    error = _single_line(str(result.get("error", "") or "managed conversation failed"), 520)
    _record_activity(
        "managed-conversation-failed",
        f"Managed conversation failed during {phase}: {error}",
        error=error,
        phase=phase,
    )
    print("")
    print(f"⚠️  Managed workflow paused after {phase} failure: {error}")


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
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
                _maybe_checkpoint_before_compaction(history, agent, live_state=live_state)
                history, compaction_state = _auto_compact_history(history, agent)
                previous_history = history[:]
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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
                result = _review_agent_final_report(result, autonomy_state)
                history = result["messages"]
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
                history, live_state, _ = _handle_api_step_budget_exhaustion(
                    agent,
                    result,
                    history,
                    autonomy_state,
                    live_state,
                    phase="background",
                )
                checkpoint_state = _journal_status()
                _record_turn_activity(previous_history, history, phase="interactive")
                _maybe_write_milestone_checkpoint(previous_history, history, agent, autonomy_state, live_state=live_state)
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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


def _startup_skill_contract(skill_name: str, *, heading: str = "EPFLEMMA ACTIVE SKILL") -> str:
    name = str(skill_name or "").strip()
    if not name:
        return ""
    payload = load_skill(name, _project_root())
    if not payload:
        return ""
    content = str(payload.get("content", "") or "").strip()
    if not content:
        return ""
    if len(content) > STARTUP_SKILL_CONTRACT_MAX_CHARS:
        content = (
            content[:STARTUP_SKILL_CONTRACT_MAX_CHARS].rstrip()
            + "\n\n[epflemma-native truncated active skill contract; "
            "use `skill_view` for the full skill/spec payload if needed.]"
        )
    linked_files = payload.get("linked_files") or {}
    linked_summary_lines: list[str] = []
    if linked_files:
        linked_summary_lines.append(
            "Linked skill files are not expanded in startup; use `skill_view` if they become relevant."
        )
    try:
        spec_ids = [record.spec_id for record in specs_for_skill(name)]
    except Exception:
        spec_ids = []
    if spec_ids:
        linked_summary_lines.append(
            "Linked workflow specs available through `skill_view`: " + ", ".join(spec_ids) + "."
        )
    parts = [
        f"[{heading}: {payload.get('name') or name} ({payload.get('source') or 'unknown'})]",
        "",
        content,
    ]
    if linked_summary_lines:
        parts.extend(["", "\n".join(linked_summary_lines)])
    return "\n".join(parts).strip()


def _startup_active_skill_contract(skill_name: str) -> str:
    return _startup_skill_contract(skill_name, heading="EPFLEMMA ACTIVE SKILL")


def _startup_additional_skill_contracts(active_skill: str = "") -> str:
    active = str(active_skill or "").strip()
    blocks: list[str] = []
    for name in _additional_skill_names():
        if active and name == active:
            continue
        block = _startup_skill_contract(name, heading="EPFLEMMA SUPPLEMENTAL SKILL")
        if block:
            blocks.append(block)
    if not blocks:
        return ""
    return "\n\n".join(
        [
            "[EPFLEMMA SUPPLEMENTAL SKILLS]",
            "These supplemental skills are reattached on managed continuations and after context compaction.",
            "",
            "\n\n".join(blocks),
        ]
    ).strip()


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
    skill_contract = _startup_active_skill_contract(selected_skill)
    additional_skill_contracts = _startup_additional_skill_contracts(selected_skill)
    combined_skill_contract = "\n\n".join(part for part in (skill_contract, additional_skill_contracts) if part)
    skill_block = f"\n\n{combined_skill_contract}" if combined_skill_contract else ""
    explicit_goal = _read_native_env("EFFECTIVE_PROMPT", _read_native_env("USER_PROMPT", _read_native_env("EXPLICIT_GOAL", "")))
    goal_block = f"\n\nUser prompt: {explicit_goal}" if explicit_goal else ""
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
        route_block = f"\n\n{chr(10).join(route_lines)}"
    queue_block = ""
    if _single_queue_item_turn_enabled():
        # Mirror the continuation-prompt conditional: when the queue is empty
        # but the file still needs the final whole-file sweep (e.g. on resume
        # after the cleanup gate granted a warning-cleanup window), the
        # startup prompt must surface the warning-cleanup wording. Otherwise
        # the model resumes, sees `action: final-sweep` in the route line,
        # observes the file is clean, and bails without ever reading the
        # cleanup invitation — which is what happened on the IMOMath2 resume.
        if _queue_needs_final_file_sweep(live_state):
            queue_block = f"\n\n{_final_file_sweep_block(dict(live_state or {}))}"
        else:
            queue_text = _queue_assignment_block(dict(live_state or {}), autonomy_state)
            if queue_text:
                queue_block = f"\n\n{queue_text}"
    organization_block = ""
    if _document_formalization_organization_phase_active(live_state, autonomy_state):
        organization_block = f"\n\n{_document_formalization_organization_prompt(dict(live_state or {}))}"
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
            return f"{resume_text}\n\n{startup_prompt}{goal_block}{route_block}{queue_block}{organization_block}{swarm_block}{skill_block}"
        if workflow_command:
            return f"{resume_text}\n\n{_workflow_startup_guidance(workflow_kind, workflow_command)}{goal_block}{route_block}{queue_block}{organization_block}{swarm_block}{skill_block}"
        return f"{resume_text}{skill_block}"
    if startup_prompt:
        return f"{startup_prompt}{goal_block}{route_block}{queue_block}{organization_block}{swarm_block}{skill_block}"
    if workflow_command:
        return f"{_workflow_startup_guidance(workflow_kind, workflow_command)}{goal_block}{route_block}{queue_block}{organization_block}{swarm_block}{skill_block}"
    return f"Begin the requested managed Lean workflow now.{skill_block}"


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


def _record_formalization_manual_prove_handoff(
    live_state: Mapping[str, Any] | None,
    autonomy_state: dict[str, Any],
) -> None:
    if autonomy_state.get("formalization_manual_prove_handoff_recorded"):
        return
    autonomy_state["formalization_manual_prove_handoff_recorded"] = True
    current = dict(live_state or {})
    active_file = str(current.get("active_file", "") or current.get("active_file_label", "") or "").strip()
    scope = _formalization_generated_prove_scope(active_file)
    active_label = str(current.get("active_file_label", "") or _relative_file_label(active_file) or active_file or "").strip()
    target = active_label or (scope[0] if scope else "")
    suggested_command = f"epflemma workflow prove {target}".strip() if target else "epflemma workflow prove"

    _record_activity(
        "formalizer-ended",
        "Formalizer ended after source/statement review; prove must be started explicitly by the user",
        active_file=active_label,
        proof_obligations=int(current.get("document_formalization_proof_sorry_count", 0) or 0),
        prove_scope=scope,
    )
    _record_activity(
        "prove-workflow-manual-start-required",
        "Proof workflow was not started automatically; review generated formalization, then run prove explicitly",
        suggested_command=suggested_command,
        prove_scope=scope,
    )
    print("")
    print("Formalizer ended: document source/statement review passed.")
    print("Prove workflow not started automatically. Review the generated formalization, then run:")
    print(f"  {suggested_command}")
    if scope:
        print("Prove scope: " + ", ".join(scope))


def _document_formalization_organization_prompt(live_state: Mapping[str, Any]) -> str:
    current = dict(live_state or {})
    active_file = str(current.get("active_file_label", "") or current.get("active_file", "") or "").strip()
    blueprint = _read_text_env("EPFLEMMA_FORMALIZATION_BLUEPRINT", "").strip()
    source = _read_text_env("EPFLEMMA_FORMALIZATION_DOCUMENT_RELATIVE", "").strip()
    generated_scope = _formalization_generated_prove_scope(str(current.get("active_file", "") or ""))
    generated_lines = "\n".join(f"- `{item}`" for item in generated_scope) or "- [none detected]"
    proof_obligations = int(current.get("document_formalization_proof_sorry_count", 0) or 0)
    return (
        "[EPFLEMMA FORMALIZATION FINAL ORGANIZATION PASS]\n\n"
        "Statement/source verification has passed. Before the formalizer exits, do exactly one focused "
        "organization pass over the generated formalization.\n\n"
        f"- source document: `{source or '[missing]'}`\n"
        f"- planner blueprint: `{blueprint or '[missing]'}`\n"
        f"- current entry file: `{active_file or '[missing]'}`\n"
        f"- proof obligations: {proof_obligations}\n"
        "- generated Lean scope:\n"
        f"{generated_lines}\n\n"
        "Task:\n"
        "1. Read the blueprint and generated Lean files directly.\n"
        "2. Decide whether the generated project should stay single-file or be split. Use a multi-file layout "
        "when declarations exceed 12, proof obligations exceed 8, or the source naturally separates "
        "definitions/constructions/main results. If staying single-file is better, record that decision in "
        "`## Generated File Layout`.\n"
        "3. If splitting, keep `Main.lean` as an aggregator and move content into clear modules such as "
        "`Basic.lean`, `Constructions.lean`, and `Theorems.lean` only when that improves import order. "
        "Update parent/root imports so `lake build` still checks the generated formalization.\n"
        "4. Update `Blueprint.md` so `## Generated File Layout`, `## Import Plan`, planned declaration names, "
        "source locators, proof notes, and source mappings still match the Lean files. Do not lose any "
        "blueprint source entry or verifier approval stamp.\n"
        "5. Do not prove theorem/lemma/example `sorry`s, do not self-approve new statement review statuses, "
        "and do not change mathematical statements except for namespace/import adjustments needed by a split.\n"
        "6. Run `lean_verify(mode=project)` once after the organization decision or split. If it fails, fix the "
        "organization/import issue and rerun the project check.\n\n"
        "Stop after reporting the layout decision and the project verification result."
    )


def _autonomous_stop_reason(
    history: list[dict[str, Any]],
    live_state: Mapping[str, Any] | None,
    autonomy_state: dict[str, Any],
) -> str:
    if _document_formalization_ready_for_prover_handoff(live_state):
        if _document_formalization_organization_phase_needed(live_state, autonomy_state):
            autonomy_state["document_formalization_organization_turn_started"] = True
            autonomy_state["continuation_blocked_runs"] = 0
            autonomy_state["continuation_stable_cycles"] = 0
            _record_activity(
                "formalization-organization-pass-started",
                "Statement/source review passed; starting final generated-file organization pass",
                active_file=str(
                    (live_state or {}).get("active_file_label", "")
                    or (live_state or {}).get("active_file", "")
                    or ""
                ),
                proof_obligations=(live_state or {}).get("document_formalization_proof_sorry_count"),
            )
            return "continue"
        if _document_formalization_organization_phase_active(live_state, autonomy_state):
            autonomy_state["document_formalization_organization_completed"] = True
            _record_activity(
                "formalization-organization-pass-completed",
                "Final generated-file organization pass completed; formalizer may exit before proof workflow",
                active_file=str(
                    (live_state or {}).get("active_file_label", "")
                    or (live_state or {}).get("active_file", "")
                    or ""
                ),
                proof_obligations=(live_state or {}).get("document_formalization_proof_sorry_count"),
            )
        autonomy_state["continuation_blocked_runs"] = 0
        autonomy_state["continuation_stable_cycles"] = 0
        if not autonomy_state.get("document_formalization_prover_handoff_recorded"):
            autonomy_state["document_formalization_prover_handoff_recorded"] = True
            _record_activity(
                "formalization-prover-handoff-ready",
                "Document formalization statement/source review passed; ready for /prove",
                active_file=str((live_state or {}).get("active_file_label", "") or (live_state or {}).get("active_file", "") or ""),
                sorry_count=(live_state or {}).get("sorry_count"),
                proof_obligations=(live_state or {}).get("document_formalization_proof_sorry_count"),
            )
        return "formalization-prover-handoff-ready"

    if _document_formalization_waiting_for_independent_review(live_state):
        autonomy_state["continuation_blocked_runs"] = 0
        autonomy_state["continuation_stable_cycles"] = 0
        if autonomy_state.pop("document_formalization_review_feedback_pending", False):
            return "continue"
        return "blocked"

    # The stall signature detects a no-progress autonomous loop (same state N cycles in a row ->
    # "stalled" -> stop). build_status carries a volatile "elapsed: <wall-clock>s" token (added by
    # _verification_status_text) that changes every verification, which would reset stable_cycles
    # every cycle and make the safety net never trip — letting the loop spin forever when the file
    # is effectively done but _live_state_is_verified flaps. Strip that volatile token so a truly
    # unchanging state is recognized as stalled.
    stable_build_status = re.sub(
        r"\s*\|?\s*elapsed:\s*[0-9.]+s",
        "",
        str((live_state or {}).get("build_status", "") or ""),
    )
    signature = (
        str((live_state or {}).get("active_file_label", "") or ""),
        str((live_state or {}).get("target_symbol", "") or ""),
        str((live_state or {}).get("diagnostics", "") or ""),
        str((live_state or {}).get("goals", "") or ""),
        stable_build_status,
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
    document_handoff = dict(live_state.get("document_formalization_handoff", {}) or {})
    document_handoff_blocked = _document_formalization_requested() and not bool(document_handoff.get("ok", True))
    if _runner_lean_prompt_enabled():
        if declaration_scope == "file":
            verification_gate = str(
                live_state.get("verification_hint", "")
                or "`lean_inspect` on the active file, then the LeanInteract queue-step gate or final Lake verification gate"
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
        if document_handoff_blocked:
            prompt += (
                "\n\nDocument formalization gate: keep this as planner/review work. "
                "Use `lean_inspect` and `lean_verify`, update the blueprint and source-aware doc comments, "
                "and do not fill theorem/lemma `sorry` proofs. If the only remaining blocker is independent "
                "statement/source approval, report that blocker instead of self-approving it."
            )
    else:
        if declaration_scope == "file":
            verification_lines = (
                "- explicit successful `lean_incremental_check(check_target)` for queue steps, or file verification for final/fallback checks\n"
                "- no errors that prevent checking the active file\n"
                "- no warnings in the assigned declaration, except after the manager's focused warning-cleanup opportunity is exhausted\n"
                "- no open goals for the active work\n"
                "- no remaining `sorry` in the assigned declaration\n"
                "- future queued declarations may still have `sorry`; treat those as queue state, not as permission to edit them now\n\n"
            )
            conclusion = (
                "make the next strongest move for the assigned declaration, and stop after the file check "
                "clears that declaration so the manager can choose the next queue item."
            )
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
        if document_handoff_blocked:
            prompt += (
                "\n\nDocument formalization gate: keep this as planner/review work. "
                "Use `lean_inspect` and `lean_verify` for draft readiness, update the blueprint and source-aware "
                "doc comments, and do not fill theorem/lemma `sorry` proofs. If the only remaining blocker is "
                "independent statement/source approval, report that blocker instead of self-approving it."
            )
    route_decision = dict(live_state.get("route_decision", {}) or {})
    if not route_decision:
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
    if _document_formalization_organization_phase_active(live_state, autonomy_state):
        prompt += f"\n\n{_document_formalization_organization_prompt(live_state)}"
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
        live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
        return history, compaction_state, checkpoint_state, live_state

    cycle = 1
    while True:
        autonomy_state["current_cycle"] = cycle
        # Hard backstop: even if the per-cycle "stalled" signature fails to stabilize (e.g. a
        # volatile field keeps it from tripping) or _live_state_is_verified flaps, the autonomous
        # loop must terminate. Without this the runner can spin continuation cycles indefinitely.
        if cycle > _autonomous_max_cycles():
            checkpoint_state = _journal_status()
            live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
            live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
            _record_activity(
                "autonomy-stop",
                f"Autonomous workflow hit the hard cycle ceiling ({_autonomous_max_cycles()} cycles); "
                "stopping to avoid a runaway loop",
                cycle=cycle,
            )
            ceiling_phase = "verified" if _live_state_is_verified(live_state) else "stalled"
            _persist_live_status(
                history, compaction_state, checkpoint_state, live_state, phase=ceiling_phase
            )
            return history, compaction_state, checkpoint_state, live_state
        checkpoint_state = _journal_status()
        live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
        live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
        if _advance_project_prove_manager_if_needed(autonomy_state, live_state, phase="autonomous"):
            checkpoint_state = _journal_status()
            live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
            live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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
                f"Rebuilt theorem handoff for {transition['current_target']}",
                previous_theorem=transition["previous_target"],
                current_theorem=transition["current_target"],
                previous_status=str(previous_outcome.get("status", "") or "unknown"),
                previous_note=str(previous_outcome.get("note", "") or ""),
            )
            _print_theorem_transition_handoff(history)
        if _maybe_run_document_formalization_review_agent(agent, system_prompt, live_state, autonomy_state):
            review_feedback = str(autonomy_state.pop("document_formalization_review_feedback_message", "") or "").strip()
            if review_feedback:
                history.append({"role": "user", "content": review_feedback})
                _record_activity(
                    "formalization-review-feedback-queued",
                    "Queued independent statement/source verifier BLOCK findings for the next drafting turn",
                    active_file=str(live_state.get("active_file_label", "") or live_state.get("active_file", "") or ""),
                )
            checkpoint_state = _journal_status()
            live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
            live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="verifying")
            continue
        _maybe_announce_final_file_sweep_state(autonomy_state, live_state)
        _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="verifying")
        stop_reason = _autonomous_stop_reason(history, live_state, autonomy_state)
        if stop_reason != "continue":
            _record_activity("autonomy-stop", f"Autonomous workflow stop reason: {stop_reason}", cycle=cycle)
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase=stop_reason)
            if stop_reason == "formalization-prover-handoff-ready":
                _record_formalization_manual_prove_handoff(live_state, autonomy_state)
            return history, compaction_state, checkpoint_state, live_state

        _maybe_checkpoint_before_compaction(history, agent, live_state=live_state)
        history, compaction_state = _auto_compact_history(history, agent)
        checkpoint_state = _journal_status()
        live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
        previous_history = history[:]
        _record_activity("autonomous-followup", f"Autonomous continuation #{cycle}", cycle=cycle)
        _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="busy")
        _record_queue_assignment(live_state, cycle=cycle, phase="autonomous")
        _prepare_queue_assignment_state(autonomy_state, live_state)
        if bool(live_state.get("final_sweep_warning_cleanup_pending")) and not bool(
            autonomy_state.get("final_sweep_cleanup_turn_started")
        ):
            autonomy_state["final_sweep_cleanup_turn_started"] = True
            _record_activity(
                "final-sweep-warning-cleanup-started",
                "Started final-sweep warning cleanup model turn",
                active_file=str(live_state.get("active_file", "") or ""),
                warning_count=int(live_state.get("final_sweep_warning_count", 0) or 0),
            )
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
        if _managed_conversation_failed(result):
            history = list(result.get("messages") or history)
            checkpoint_state = _journal_status()
            live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
            _record_managed_conversation_failure(result, phase=f"autonomous continuation #{cycle}")
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="failed")
            return history, compaction_state, checkpoint_state, live_state
        result = _review_agent_final_report(result, autonomy_state)
        history = result["messages"]
        checkpoint_state = _journal_status()
        live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
        live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
        history, live_state, budget_recorded_attempt = _handle_api_step_budget_exhaustion(
            agent,
            result,
            history,
            autonomy_state,
            live_state,
            cycle=cycle,
            phase="autonomous",
        )
        checkpoint_state = _journal_status()
        boundary_recorded_attempt = bool(getattr(agent, "_managed_step_boundary_recorded_attempt", False))
        if boundary_recorded_attempt:
            try:
                setattr(agent, "_managed_step_boundary_recorded_attempt", False)
            except Exception:
                pass
        if (
            not boundary_recorded_attempt
            and not budget_recorded_attempt
            and _same_queue_assignment_still_blocked(autonomy_state, live_state)
        ):
            _remember_failed_attempt(autonomy_state, live_state, cycle_number=cycle)
        _record_turn_activity(previous_history, history, phase="autonomous")
        _maybe_write_milestone_checkpoint(previous_history, history, agent, autonomy_state, live_state=live_state)
        _persist_live_status(history, compaction_state, checkpoint_state, live_state)
        if result.get("interrupted") and not _is_step_boundary_interrupt(result):
            _record_activity("autonomy-interrupted", f"Autonomous continuation #{cycle} interrupted by user", cycle=cycle)
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="paused")
            return history, compaction_state, checkpoint_state, live_state
        cycle += 1


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
        _ensure_project_prove_manager_started(autonomy_state, phase="startup")
        live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
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
        if _managed_conversation_failed(result):
            history = list(result.get("messages") or history)
            checkpoint_state = _journal_status()
            live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
            _record_managed_conversation_failure(result, phase="startup")
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="failed")
            _record_agent_activity(agent, "runner-exit", "Managed workflow runner exited after provider/API failure")
            return 1
        result = _review_agent_final_report(result, autonomy_state)
        previous_history = history[:]
        history = result["messages"]
        checkpoint_state = _journal_status()
        live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
        live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
        history, live_state, _ = _handle_api_step_budget_exhaustion(
            agent,
            result,
            history,
            autonomy_state,
            live_state,
            phase="startup",
        )
        checkpoint_state = _journal_status()
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
        if _verified_workflow_should_exit_without_prompt(live_state):
            _terminate_descendant_agents(agent)
            _terminate_other_agents(agent)
            _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="exited")
            _record_agent_activity(agent, "runner-exit", "Managed workflow runner exited after verified completion (non-interactive)")
            return 0
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
        if not _interactive_prompt_loop_allowed():
            # Headless run (stdin is not a TTY): there is no human to answer the prompt, so we
            # must NOT block on input(). The autonomous followups have already run; write the
            # pre-exit checkpoint (mirroring the EOFError path below so resumability is preserved),
            # persist the final state, and exit cleanly instead of hanging on a prompt forever.
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
            exit_phase = "exited" if _live_state_is_verified(live_state) else "paused"
            _persist_live_status(
                history, compaction_state, checkpoint_state, live_state, phase=exit_phase
            )
            _record_agent_activity(
                agent,
                "runner-exit",
                "Managed workflow runner exited without interactive prompt (stdin not a TTY)",
            )
            return 0
        _print_interactive_mode_header(live_state)

        while True:
            mode_label = _interactive_mode_label(live_state)
            try:
                raw = input(f"\n{mode_label}> ")
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
                print(f"\nInterrupted. Use /exit to leave {mode_label} mode.")
                continue

            text = raw.strip()
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                if _is_autonomous_workflow() and history:
                    live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                    live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state)
                _print_live_proof_state(live_state)
                continue
            if text == "/diagnostics":
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state)
                _print_live_proof_state(live_state, section="diagnostics")
                continue
            if text == "/goals":
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
                _persist_live_status(history, compaction_state, checkpoint_state, live_state)
                _print_live_proof_state(live_state, section="goals")
                continue
            if text == "/history":
                _print_history(_all_checkpoint_entries_latest_first())
                continue
            if text.startswith("/checkpoint"):
                note = text[len("/checkpoint"):].strip()
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
                _record_activity("rollback", message, checkpoint_label=str(entry.get("label", "") or "checkpoint"))
                _persist_live_status(history, compaction_state, checkpoint_state, live_state, phase="resumed")
                print(message)
                print(f"Recorded {post['label']} ({post['checkpoint_id']}).")
                _print_interactive_mode_header(live_state)
                continue
            if text == "/compact":
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
                _maybe_checkpoint_before_compaction(history, agent, live_state=live_state)
                history, compaction_state = _auto_compact_history(history, agent, force=True)
                checkpoint_state = _journal_status()
                live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
                live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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

            live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
            live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
            _maybe_checkpoint_before_compaction(history, agent, live_state=live_state)
            history, compaction_state = _auto_compact_history(history, agent)
            previous_history = history[:]
            live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
            live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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
            result = _review_agent_final_report(result, autonomy_state)
            history = result["messages"]
            live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
            live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
            history, live_state, _ = _handle_api_step_budget_exhaustion(
                agent,
                result,
                history,
                autonomy_state,
                live_state,
                phase="interactive",
            )
            checkpoint_state = _journal_status()
            _record_turn_activity(previous_history, history, phase="interactive")
            _maybe_write_milestone_checkpoint(previous_history, history, agent, autonomy_state, live_state=live_state)
            checkpoint_state = _journal_status()
            live_state = _build_live_proof_state_compat(history, checkpoint_state, autonomy_state)
            live_state = _promote_live_state_to_verified_compat(live_state, autonomy_state)
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
