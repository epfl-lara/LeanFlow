"""Persistent managed-workflow state for the LeanFlow shell."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import signal
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Iterator, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.filesystem import ensure_directory
from core.process_identity import (
    ProcessIdentity,
    current_process_identity,
    process_identity_details,
    process_identity_from_mapping,
    process_identity_matches,
)
from leanflow_cli.workflows.activity_preview import (  # noqa: F401
    _activity_preview_limit,
    _agent_event_preview,
    _agent_status_from_live_phase,
    _coerce_tool_arguments,
    _shorten_text,
    _summarize_requested_tools,
    _tool_call_preview,
    _tool_result_preview,
)
from leanflow_cli.workflows.workflow_activity_reader import (
    JsonlPathsFingerprint,
    iter_jsonl_dicts,
    jsonl_paths_fingerprint,
)
from leanflow_cli.workflows.workflow_activity_retention import (
    ActivityRetentionResult,
    activity_retention_catalog_path,
    compact_closed_activity,
    load_retained_agent_summaries,
    load_retained_recent_events,
    retained_source_names,
)
from leanflow_cli.workflows.workflow_json_io import (  # noqa: F401
    read_json_file,
    update_json_file,
    update_json_file_if_changed,
    write_json_file,
)
from leanflow_cli.workflows.workflow_outcome_retention import append_outcome_entry
from leanflow_cli.workflows.workflow_state_paths import (  # noqa: F401
    PROJECT_STATE_DIRNAME,
    _discover_project_root,
    _leanflow_home,
    _project_root_from_env,
    _project_state_root,
    workflow_state_root,
)

logger = logging.getLogger(__name__)


class WorkflowLiveStatusOwnerConflictError(RuntimeError):
    """Reject a live-status write while another verified owner is live."""

    def __init__(self, identity: ProcessIdentity, *, phase: str, run_id: str) -> None:
        self.identity = identity
        self.phase = str(phase or "")
        self.run_id = str(run_id or "")
        run_suffix = f" (run {self.run_id})" if self.run_id else ""
        super().__init__(
            "Cannot claim workflow live status while verified live owner "
            f"PID {identity.pid} remains in phase {self.phase or '[unknown]'}{run_suffix}."
        )


try:
    import fcntl  # POSIX advisory file locking
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

# Serializes activity/outcome/log appends so concurrent /swarm agents cannot interleave or
# lose JSON-lines: _APPEND_LOCK guards threads within this process; fcntl.flock guards across
# subprocesses. Best-effort — degrades to in-process-only if flock is unavailable.
_APPEND_LOCK = threading.Lock()
# Captured stdout/stderr belongs to the one top-level native runner that reset
# ``latest-run.log`` and selected the timestamped run id. Dispatch subprocesses
# write their own worker logs and never install the tee. Keep these two plain-
# text sinks on a process-local lock so a foreground verification print cannot
# queue behind cross-process JSONL activity flock contention.
_RUN_LOG_APPEND_LOCK = threading.Lock()
_RUN_LOG_RELEASED_OWNER_TOKENS: set[str] = set()
_APPEND_LATENCY_LOCK = threading.Lock()
_SLOW_APPEND_THRESHOLD_S = 1.0

# Cache only the compact event-derived agent reductions. Live phase/process
# overlays are recomputed on every call, and the small LRU prevents test/home
# switches from retaining historical project state indefinitely.
_AGENT_SUMMARY_CACHE_LOCK = threading.Lock()
_AGENT_SUMMARY_CACHE_MAX = 8
_AGENT_SUMMARY_CACHE: OrderedDict[
    tuple[str, int],
    tuple[JsonlPathsFingerprint, dict[str, dict[str, Any]]],
] = OrderedDict()


def _record_slow_append(
    path: Path,
    *,
    text_bytes: int,
    elapsed_s: float,
    local_lock_wait_s: float,
    cross_process_lock_wait_s: float,
    write_s: float,
) -> None:
    """Persist append-lock latency without re-entering the contested stream."""
    latency_path = workflow_state_root() / f"append-latency-pid{os.getpid()}.jsonl"
    record = {
        "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "process_id": os.getpid(),
        "path": str(path),
        "text_bytes": max(0, int(text_bytes)),
        "elapsed_s": round(max(0.0, elapsed_s), 3),
        "local_lock_wait_s": round(max(0.0, local_lock_wait_s), 3),
        "cross_process_lock_wait_s": round(max(0.0, cross_process_lock_wait_s), 3),
        "write_s": round(max(0.0, write_s), 3),
    }
    try:
        ensure_directory(latency_path.parent)
        with _APPEND_LATENCY_LOCK, latency_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
    except OSError:
        # Telemetry must never turn a durable workflow append into a failure.
        pass


def _locked_append(path: Path, text: str) -> None:
    """Append ``text`` and trace slow local, process, and write phases."""
    started = time.monotonic()
    local_lock_wait_s = 0.0
    cross_process_lock_wait_s = 0.0
    write_s = 0.0
    ensure_directory(path.parent)
    local_lock_started = time.monotonic()
    with _APPEND_LOCK:
        local_lock_wait_s = max(0.0, time.monotonic() - local_lock_started)
        with path.open("a", encoding="utf-8") as handle:
            if fcntl is not None:
                flock_started = time.monotonic()
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except OSError:
                    logger.debug(
                        "flock unavailable for %s; append not cross-process locked",
                        path,
                        exc_info=True,
                    )
                cross_process_lock_wait_s = max(0.0, time.monotonic() - flock_started)
            write_started = time.monotonic()
            handle.write(text)
            handle.flush()
            write_s = max(0.0, time.monotonic() - write_started)
    elapsed_s = max(0.0, time.monotonic() - started)
    if elapsed_s >= _SLOW_APPEND_THRESHOLD_S:
        _record_slow_append(
            path,
            text_bytes=len(text.encode("utf-8", errors="replace")),
            elapsed_s=elapsed_s,
            local_lock_wait_s=local_lock_wait_s,
            cross_process_lock_wait_s=cross_process_lock_wait_s,
            write_s=write_s,
        )


@contextlib.contextmanager
def _workflow_run_log_flock() -> Iterator[None]:
    """Serialize console-log ownership without sharing the activity lock."""
    lock_path = workflow_state_root() / ".run-log.lock"
    ensure_directory(lock_path.parent)
    with lock_path.open("a+b") as handle:
        locked = False
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                locked = True
            except OSError:
                logger.debug("run-log flock unavailable for %s", lock_path, exc_info=True)
        try:
            yield
        finally:
            if locked and fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _workflow_run_log_owner_path() -> Path:
    """Return the owner-token path for the shared latest-run console log."""
    return workflow_state_root() / ".latest-run.owner"


def _workflow_run_log_owner_token(run_id: str) -> str:
    """Return the process-specific token allowed to write ``latest-run.log``."""
    return json.dumps(
        {"process_id": os.getpid(), "run_id": str(run_id or "")},
        sort_keys=True,
    )


def _append_plain_text(path: Path, text: str) -> None:
    """Append and flush plain console text while the dedicated lock is held."""
    ensure_directory(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


WORKFLOW_TASK_LABELS = {
    "autoprove": "prove",
    "autoformalize": "formalize",
    "prove": "prove",
    "formalize": "formalize",
    "draft": "draft",
    "review": "review",
    "refactor": "refactor",
    "golf": "golf",
}
WORKFLOW_RUN_SCOPE_TOP_LEVEL = "top-level"
WORKFLOW_RUN_SCOPE_BACKGROUND = "background-session"


def ensure_workflow_state_root() -> Path:
    root = workflow_state_root()
    return ensure_directory(root)


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
    safe_agent_id = "".join(
        ch for ch in str(agent_id or "").strip() if ch.isalnum() or ch in {"-", "_"}
    )
    return workflow_agent_inbox_root() / f"{safe_agent_id or 'unknown'}.jsonl"


def workflow_outcomes_path() -> Path:
    return workflow_state_root() / "outcomes.jsonl"


def workflow_verified_patch_status_path() -> Path:
    return workflow_state_root() / "verified_patch_status.json"


def workflow_verified_patch_checkpoint_root() -> Path:
    return workflow_state_root() / "verified-patch-checkpoints"


def _attach_current_process_identity(payload: dict[str, Any]) -> None:
    """Add exact ownership fields when a payload describes this process."""
    try:
        process_id = int(payload.get("process_id", 0) or 0)
    except (TypeError, ValueError):
        return
    if process_id != os.getpid():
        return
    for key, value in process_identity_details(current_process_identity()).items():
        payload.setdefault(key, value)


def workflow_run_activity_path(run_id: str) -> Path:
    safe_run_id = "".join(
        ch for ch in str(run_id or "").strip() if ch.isalnum() or ch in {"-", "_"}
    )
    return workflow_run_activity_root() / f"{safe_run_id or 'unknown'}.jsonl"


def workflow_run_metadata_path(run_id: str) -> Path:
    safe_run_id = "".join(
        ch for ch in str(run_id or "").strip() if ch.isalnum() or ch in {"-", "_"}
    )
    return workflow_run_metadata_root() / f"{safe_run_id or 'unknown'}.json"


def workflow_agent_activity_path(agent_id: str, task_label: str = "") -> Path:
    safe_agent_id = "".join(
        ch for ch in str(agent_id or "").strip() if ch.isalnum() or ch in {"-", "_"}
    )
    safe_task = (
        "".join(
            ch for ch in str(task_label or "").strip() if ch.isalnum() or ch in {"-", "_"}
        ).strip()
        or "agent"
    )
    return workflow_agent_activity_root() / f"{safe_task}-{safe_agent_id or 'unknown'}.jsonl"


def _read_workflow_run_metadata(run_id: str) -> dict[str, Any]:
    return read_json_file(workflow_run_metadata_path(run_id))


def _workflow_run_scope_from_event(
    event_type: str, details: Mapping[str, Any] | None = None
) -> str:
    if event_type == "runner-start":
        return WORKFLOW_RUN_SCOPE_TOP_LEVEL
    normalized_details = details if isinstance(details, Mapping) else {}
    explicit = str(
        normalized_details.get("run_scope", "")
        or os.getenv("LEANFLOW_WORKFLOW_RUN_SCOPE", "")
        or ""
    ).strip()
    if explicit:
        return explicit
    # A top-level runner and its process-isolated workers intentionally share
    # one run stream. Infer ownership from the runner-start sidecar when no
    # caller supplied an explicit scope: events emitted by the owner process
    # stay top-level, while worker PIDs remain background sessions.
    metadata = _read_workflow_run_metadata(_workflow_run_id())
    try:
        owner_pid = int(metadata.get("process_id", 0) or 0)
        event_pid = int(normalized_details.get("process_id", 0) or os.getpid())
    except (TypeError, ValueError):
        owner_pid = event_pid = 0
    if (
        str(metadata.get("run_scope", "") or "") == WORKFLOW_RUN_SCOPE_TOP_LEVEL
        and owner_pid > 0
        and event_pid == owner_pid
    ):
        return WORKFLOW_RUN_SCOPE_TOP_LEVEL
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
    incoming_scope = str(run_scope or WORKFLOW_RUN_SCOPE_BACKGROUND)
    # Research workers intentionally append their events to the campaign run
    # stream.  They must not replace the top-level runner identity (especially
    # its owner PID) in the sidecar metadata used by latest-run/status lookup.
    preserve_top_level_identity = (
        str(payload.get("run_scope", "") or "") == WORKFLOW_RUN_SCOPE_TOP_LEVEL
        and incoming_scope != WORKFLOW_RUN_SCOPE_TOP_LEVEL
    )
    if not preserve_top_level_identity:
        payload["run_scope"] = incoming_scope
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
    current_run_id = str(os.getenv("LEANFLOW_WORKFLOW_RUN_ID", "") or "").strip()
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
    run_id = str(os.getenv("LEANFLOW_WORKFLOW_RUN_ID", "") or "").strip()
    if run_id:
        return run_id
    started = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    workflow_kind = str(os.getenv("LEANFLOW_NATIVE_WORKFLOW_KIND", ""))
    task = _workflow_task_label(
        workflow_kind,
        str(os.getenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "")) if workflow_kind else "",
        0,
    )
    safe_task = "".join(ch for ch in task if ch.isalnum() or ch in {"-", "_"}).strip() or "agent"
    run_id = f"{safe_task}-{started}-pid{os.getpid()}"
    os.environ["LEANFLOW_WORKFLOW_RUN_ID"] = run_id
    return run_id


def workflow_timestamped_run_log_path() -> Path:
    return workflow_runs_root() / f"{_workflow_run_id()}.log"


def load_workflow_live_status() -> dict[str, Any]:
    """Return live status after atomically rechecking any stale-owner rewrite."""
    path = workflow_live_status_path()
    payload = read_json_file(path)
    normalized, changed = _normalize_workflow_live_status_payload(payload)
    if not changed:
        return normalized

    def normalize_current(current: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        locked_normalized, locked_changed = _normalize_workflow_live_status_payload(current)
        if locked_changed:
            current.clear()
            current.update(locked_normalized)
        return locked_normalized, locked_changed

    return update_json_file_if_changed(path, normalize_current)


def save_workflow_live_status(payload: Mapping[str, Any]) -> None:
    """Replace live status without stealing a verified foreign ownership lease."""
    replacement = dict(payload)
    _attach_current_process_identity(replacement)
    writer_identity = current_process_identity()

    def mutate(current: dict[str, Any]) -> None:
        current_identity = process_identity_from_mapping(current)
        current_owner_is_live = current_identity.verifiable and process_identity_matches(
            current_identity
        )
        if current_owner_is_live and not _identity_is_current_process_owner(
            current_identity, writer_identity
        ):
            raise WorkflowLiveStatusOwnerConflictError(
                current_identity,
                phase=str(current.get("phase", "") or ""),
                run_id=str(current.get("run_id", "") or ""),
            )
        current.clear()
        current.update(replacement)

    update_json_file(workflow_live_status_path(), mutate)


_WORKFLOW_STARTUP_PHASES = frozenset({"starting", "reconciling"})


def _identity_is_current_process_owner(
    persisted: ProcessIdentity, current: ProcessIdentity
) -> bool:
    """Return whether a persisted verified identity describes this process."""
    if not persisted.verifiable or not current.verifiable:
        return False
    if persisted.pid != current.pid or persisted.token_sha256 != current.token_sha256:
        return False
    if persisted.process_group_id > 0 and persisted.process_group_id != current.process_group_id:
        return False
    return persisted.session_id <= 0 or persisted.session_id == current.session_id


def _startup_previous_owner_metadata(
    payload: Mapping[str, Any], identity: ProcessIdentity
) -> dict[str, Any]:
    """Return bounded audit metadata for an interrupted startup owner."""
    previous = {
        "phase": str(payload.get("phase", "") or "").strip().lower(),
        **process_identity_details(identity),
    }
    for key in ("run_id", "updated_at", "runtime_heartbeat_at"):
        value = str(payload.get(key, "") or "")
        if value:
            previous[key] = value
    return previous


def mark_workflow_live_status_startup(
    *, phase: str, metadata: Mapping[str, Any] | None = None
) -> None:
    """Claim the live-status pointer before expensive startup reconciliation.

    Preserve the previous mathematical snapshot until the native runner can
    rebuild it from Lean and plan state. Replace only runtime ownership and
    launch metadata, and mark those retained proof fields as pending
    reconciliation. Reject a distinct verified live owner; retain bounded
    audit metadata when taking over an interrupted or already-normalized
    startup owner.
    """
    normalized_phase = str(phase or "").strip().lower()
    if normalized_phase not in _WORKFLOW_STARTUP_PHASES:
        raise ValueError(f"Unsupported workflow startup phase: {phase!r}")
    heartbeat = datetime.now(UTC).replace(microsecond=0).isoformat()
    current_identity = current_process_identity()
    identity = process_identity_details(current_identity)
    launch_metadata = dict(metadata or {})

    def mutate(payload: dict[str, Any]) -> None:
        previous_identity = process_identity_from_mapping(payload)
        same_owner = _identity_is_current_process_owner(previous_identity, current_identity)
        previous_owner_is_live = previous_identity.verifiable and process_identity_matches(
            previous_identity
        )
        if previous_owner_is_live and not same_owner:
            raise WorkflowLiveStatusOwnerConflictError(
                previous_identity,
                phase=str(payload.get("phase", "") or ""),
                run_id=str(payload.get("run_id", "") or ""),
            )
        previous_phase = str(payload.get("phase", "") or "").strip().lower()
        interrupted_startup = (
            not same_owner
            and previous_identity.pid > 0
            and previous_phase in _WORKFLOW_STARTUP_PHASES
        )
        previous_owner_metadata = (
            _startup_previous_owner_metadata(payload, previous_identity)
            if interrupted_startup
            else None
        )
        retained_previous_owner = payload.get("startup_previous_owner")
        payload.update(launch_metadata)
        payload.update(identity)
        payload.update(
            {
                "version": 1,
                "phase": normalized_phase,
                "updated_at": heartbeat,
                "runtime_heartbeat_at": heartbeat,
                "startup_reconciliation_pending": True,
                "interrupt_source": "",
                "held_locks": 0,
            }
        )
        # These fields describe the previous process, not the retained Lean
        # snapshot. Never advertise an old terminal outcome for a new owner.
        payload.pop("exit_code", None)
        payload.pop("reason", None)
        for key in ("stale_snapshot", "stale_process_id", "stale_held_locks"):
            payload.pop(key, None)
        if previous_owner_metadata is not None:
            payload["startup_previous_owner"] = previous_owner_metadata
        elif not same_owner and isinstance(retained_previous_owner, Mapping):
            payload["startup_previous_owner"] = dict(retained_previous_owner)
        elif not same_owner:
            payload.pop("startup_previous_owner", None)

    update_json_file(workflow_live_status_path(), mutate)


def _refresh_workflow_live_queue_source(
    *,
    target_symbol: str,
    active_file: str,
    source_item: Mapping[str, Any],
    prefix: str,
    slice_text: str,
    process_id: int | None = None,
) -> bool:
    """Refresh source-derived queue fields in the owning live snapshot.

    Managed proof edits can insert helpers above the assigned theorem while a
    model turn remains open. Update only line/range/source fields atomically so
    heartbeat writes cannot keep advertising the pre-edit locations.
    """
    path = workflow_live_status_path()
    if not path.is_file():
        return False
    expected_target = str(target_symbol or "").strip().removeprefix("_root_.")
    expected_file = str(active_file or "").strip()
    if not expected_target or not expected_file:
        return False
    owner_pid = os.getpid() if process_id is None else int(process_id)
    root = _project_root_from_env()

    def resolve_file(value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() and root is not None:
            candidate = root / candidate
        return candidate.resolve()

    def symbols_match(left: str, right: str) -> bool:
        left_value = str(left or "").strip().removeprefix("_root_.")
        right_value = str(right or "").strip().removeprefix("_root_.")
        return bool(left_value and right_value) and (
            left_value == right_value
            or left_value.endswith(f".{right_value}")
            or right_value.endswith(f".{left_value}")
        )

    source_fields = {
        key: source_item[key]
        for key in ("label", "file", "kind", "line", "end_line")
        if key in source_item
    }

    def mutate(payload: dict[str, Any]) -> bool:
        try:
            snapshot_pid = int(payload.get("process_id", 0) or 0)
        except (TypeError, ValueError):
            snapshot_pid = 0
        if snapshot_pid != owner_pid or not symbols_match(
            str(payload.get("target_symbol", "") or ""), expected_target
        ):
            return False
        snapshot_file = str(payload.get("active_file", "") or "").strip()
        try:
            same_file = bool(snapshot_file) and resolve_file(snapshot_file) == resolve_file(
                expected_file
            )
        except (OSError, RuntimeError):
            same_file = snapshot_file == expected_file
        if not same_file:
            return False
        current_item = dict(payload.get("current_queue_item") or {})
        current_label = str(current_item.get("label", "") or expected_target)
        if not symbols_match(current_label, expected_target):
            return False
        current_item.update(source_fields)
        payload["current_queue_item"] = current_item
        preview: list[dict[str, Any]] = []
        for raw_item in list(payload.get("declaration_queue_preview") or []):
            item = dict(raw_item) if isinstance(raw_item, Mapping) else {}
            if symbols_match(str(item.get("label", "") or ""), expected_target):
                item.update(source_fields)
            preview.append(item)
        if preview:
            payload["declaration_queue_preview"] = preview
        payload["current_queue_item_prefix"] = str(prefix or "")
        payload["current_queue_item_slice"] = str(slice_text or "")
        heartbeat = datetime.now(UTC).replace(microsecond=0).isoformat()
        payload["updated_at"] = heartbeat
        payload["runtime_heartbeat_at"] = heartbeat
        return True

    return bool(update_json_file(path, mutate))


def touch_workflow_runtime_heartbeat(
    *, process_id: int | None = None, timestamp: str | None = None
) -> bool:
    """Advance only the live owner snapshot's runtime heartbeat.

    Return whether the owner PID matched. Deliberately preserve
    ``last_activity_*`` so silent liveness cannot masquerade as a new event.
    """
    path = workflow_live_status_path()
    if not path.is_file():
        return False
    owner_pid = os.getpid() if process_id is None else int(process_id)
    heartbeat = timestamp or datetime.now(UTC).replace(microsecond=0).isoformat()

    def mutate(payload: dict[str, Any]) -> bool:
        try:
            snapshot_pid = int(payload.get("process_id", 0) or 0)
        except (TypeError, ValueError):
            snapshot_pid = 0
        if snapshot_pid != owner_pid:
            return False
        payload["updated_at"] = heartbeat
        payload["runtime_heartbeat_at"] = heartbeat
        return True

    return bool(update_json_file(path, mutate))


def _touch_workflow_live_status_from_activity(
    *, timestamp: str, event_type: str, message: str
) -> None:
    """Refresh the owner runner's snapshot after durable live activity.

    Background processes share the project state directory, so only the
    process recorded as the live-status owner may advance its heartbeat.
    Lifecycle timestamps in ``summary.json.campaign`` intentionally remain
    unchanged; ``runtime_heartbeat_at`` is the liveness clock.
    """
    path = workflow_live_status_path()
    if not path.is_file():
        return
    owner_pid = os.getpid()

    def mutate(payload: dict[str, Any]) -> None:
        try:
            snapshot_pid = int(payload.get("process_id", 0) or 0)
        except (TypeError, ValueError):
            snapshot_pid = 0
        if snapshot_pid != owner_pid:
            return
        payload["updated_at"] = timestamp
        payload["runtime_heartbeat_at"] = timestamp
        payload["last_activity_type"] = str(event_type or "")
        payload["last_activity_message"] = str(message or "")[:500]

    update_json_file(path, mutate)


def append_workflow_activity(event_type: str, message: str, **details: Any) -> None:
    """Append a timestamped activity event to run and agent activity streams with workflow context.

    Enriches event details with environment variables for workflow kind, skill, prompt, and project root; persists run metadata. Events are locked and appended to both run-scoped and agent-scoped JSONL files.
    """
    ensure_workflow_state_root()
    normalized_details = dict(details)
    env_workflow_kind = str(os.getenv("LEANFLOW_NATIVE_WORKFLOW_KIND", ""))
    normalized_details.setdefault("workflow_kind", env_workflow_kind)
    normalized_details.setdefault(
        "workflow_command", str(os.getenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", ""))
    )
    normalized_details.setdefault(
        "effective_prompt",
        str(
            os.getenv("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", "")
            or os.getenv("LEANFLOW_NATIVE_USER_PROMPT", "")
            or os.getenv("LEANFLOW_NATIVE_EXPLICIT_GOAL", "")
        ),
    )
    normalized_details.setdefault(
        "active_skill",
        str(os.getenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "")) if env_workflow_kind else "",
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
        or os.getenv("LEANFLOW_WORKFLOW_PARENT_RUN_ID", "")
        or ""
    ).strip()
    normalized_details.setdefault("parent_run_id", parent_run_id)
    try:
        process_id = int(normalized_details.get("process_id", 0) or 0)
    except Exception:
        process_id = 0
    _attach_current_process_identity(normalized_details)
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
    _touch_workflow_live_status_from_activity(
        timestamp=timestamp,
        event_type=event_type,
        message=message,
    )


def append_workflow_outcome(kind: str, payload: Mapping[str, Any]) -> None:
    ensure_workflow_state_root()
    entry = {
        "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "kind": str(kind or "").strip() or "outcome",
        "workflow_kind": str(os.getenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "")),
        "workflow_command": str(os.getenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "")),
        "payload": dict(payload or {}),
    }
    path = workflow_outcomes_path()
    append_outcome_entry(path, entry, append=_locked_append)


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
    """Persist the latest apply_verified_patch status for resume/queue logic.

    Share the verified-patch sidecar lock with reconciliation.  A later patch
    must not race an older checkpoint's read-modify-write promotion and then be
    overwritten by that stale promotion.
    """
    status = dict(payload or {})
    status.setdefault("timestamp", datetime.now(UTC).replace(microsecond=0).isoformat())

    def mutate(current: dict[str, Any]) -> None:
        current.clear()
        current.update({"version": 1, "latest": status})

    update_json_file(workflow_verified_patch_status_path(), mutate)


def mark_verified_patch_queue_rejected(
    checkpoint_id: str,
    *,
    restored_source_sha256: str,
    message: str,
) -> bool:
    """Mark a matching verified patch as rolled back by the queue guard."""
    expected = str(checkpoint_id or "").strip()
    if not expected:
        return False

    def mutate(current: dict[str, Any]) -> bool:
        latest = current.get("latest")
        if (
            not isinstance(latest, Mapping)
            or str(latest.get("checkpoint_id", "") or "").strip() != expected
        ):
            return False
        status = dict(latest)
        status.update(
            {
                "success": False,
                "status": "queue_guard_rejected",
                "patch_applied_before_queue_guard": bool(status.get("patch_applied")),
                "patch_applied": False,
                "patch_retained": False,
                "queue_edit_accepted": False,
                "target_verified": False,
                "verified": False,
                "restored_source_revision_sha256": str(restored_source_sha256 or "").strip(),
                "message": str(message or "").strip(),
                "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
            }
        )
        current["latest"] = status
        return True

    return bool(update_json_file(workflow_verified_patch_status_path(), mutate))


def load_verified_patch_status() -> dict[str, Any]:
    payload = read_json_file(workflow_verified_patch_status_path())
    latest = payload.get("latest")
    return dict(latest) if isinstance(latest, Mapping) else {}


def _read_activity_file(path: Path | None) -> list[dict[str, Any]]:
    """Return one activity stream for compatibility with private callers."""
    return list(iter_jsonl_dicts([path] if path is not None else []))


def _workflow_activity_paths() -> tuple[Path, ...]:
    """Return unarchived activity paths in deterministic scan order."""
    root = workflow_run_activity_root()
    if root.is_dir():
        retained = retained_source_names(workflow_state_root())
        return tuple(path for path in sorted(root.glob("*.jsonl")) if path.name not in retained)
    latest = workflow_latest_run_activity_path()
    return (latest,) if latest is not None else ()


def _iter_all_workflow_activity() -> Iterator[dict[str, Any]]:
    """Yield all workflow activity while retaining only one JSONL record."""
    return iter_jsonl_dicts(_workflow_activity_paths())


def _cached_agent_summary_base(
    paths: tuple[Path, ...], activity_limit: int
) -> tuple[dict[str, dict[str, Any]] | None, JsonlPathsFingerprint]:
    """Return an unchanged event-derived summary base and its file fingerprint.

    Any append rebuilds the deterministic cross-file reduction. This preserves
    lifecycle ordering; bounded record ingestion keeps that cold rebuild from
    materializing legacy request payloads in memory.
    """
    fingerprint = jsonl_paths_fingerprint(
        (*paths, activity_retention_catalog_path(workflow_state_root()))
    )
    key = (str(workflow_state_root()), max(1, int(activity_limit)))
    with _AGENT_SUMMARY_CACHE_LOCK:
        cached = _AGENT_SUMMARY_CACHE.get(key)
        if cached is None or cached[0] != fingerprint:
            return None, fingerprint
        _AGENT_SUMMARY_CACHE.move_to_end(key)
        return cached[1], fingerprint


def _store_agent_summary_base(
    *,
    activity_limit: int,
    fingerprint: JsonlPathsFingerprint,
    by_agent: dict[str, dict[str, Any]],
) -> None:
    """Store one compact event-derived summary base in the bounded LRU."""
    key = (str(workflow_state_root()), max(1, int(activity_limit)))
    with _AGENT_SUMMARY_CACHE_LOCK:
        _AGENT_SUMMARY_CACHE[key] = (fingerprint, deepcopy(by_agent))
        _AGENT_SUMMARY_CACHE.move_to_end(key)
        while len(_AGENT_SUMMARY_CACHE) > _AGENT_SUMMARY_CACHE_MAX:
            _AGENT_SUMMARY_CACHE.popitem(last=False)


def _read_all_workflow_activity() -> list[dict[str, Any]]:
    """Materialize all activity for legacy private callers only."""
    return list(_iter_all_workflow_activity())


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


def _workflow_process_identity_is_live(
    payload: Mapping[str, Any], *, require_verified: bool = False
) -> bool:
    """Return whether a persisted workflow identity still owns its process.

    New records carry a launch-token fingerprint and therefore detect PID
    reuse. Legacy records remain readable for status display, but callers that
    can signal or enqueue control input require verified ownership and fail
    closed when the token is absent.
    """
    identity = process_identity_from_mapping(payload)
    if identity.verifiable:
        return process_identity_matches(identity)
    if require_verified:
        return False
    return _process_seems_alive(identity.pid)


def compact_closed_workflow_activity() -> ActivityRetentionResult:
    """Archive provably closed activity while preserving the current run hot."""
    return compact_closed_activity(
        workflow_state_root(),
        current_run_id=_workflow_run_id(),
        reduce_event=_reduce_workflow_agent_event,
        compact_event=_compact_workflow_activity_event,
        identity_is_live=_workflow_process_identity_is_live,
    )


def interrupt_workflow_process(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Interrupt one exactly revalidated workflow process or fail closed.

    A process group is eligible only when the runner is its isolated session
    leader. Interactive runners share the caller's terminal group and receive
    a PID-only signal so unrelated shell processes remain outside the boundary.
    """
    identity = process_identity_from_mapping(payload)
    if not identity.verifiable:
        return {
            "success": False,
            "error": "Process ownership identity is unavailable; refusing to signal.",
            "process_id": identity.pid,
        }
    if identity.pid == os.getpid():
        return {
            "success": False,
            "error": "Refusing to interrupt the current process.",
            "process_id": identity.pid,
        }
    if not process_identity_matches(identity):
        return {
            "success": False,
            "error": "Process ownership identity no longer matches; refusing to signal.",
            "process_id": identity.pid,
        }

    isolated_group = bool(
        identity.process_group_id == identity.pid and identity.session_id == identity.pid
    )
    try:
        if isolated_group and hasattr(os, "killpg"):
            os.killpg(identity.process_group_id, signal.SIGINT)
        else:
            os.kill(identity.pid, signal.SIGINT)
    except ProcessLookupError:
        return {
            "success": False,
            "error": "Process already exited.",
            "process_id": identity.pid,
        }
    except (PermissionError, OSError) as exc:
        return {
            "success": False,
            "error": str(exc),
            "process_id": identity.pid,
        }
    return {
        "success": True,
        "process_id": identity.pid,
        "process_group_id": identity.process_group_id if isolated_group else 0,
        "identity_verified": True,
    }


_LIVE_STATUS_TERMINAL_PHASES = {
    "completed",
    "dead",
    "exited",
    "failed",
    "interrupted",
    "stopped",
    "verified",
}


def _normalize_workflow_live_status_payload(
    payload: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], bool]:
    """Return a display-safe snapshot and whether its stale state must persist."""
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
    if process_id <= 0 or _workflow_process_identity_is_live(normalized):
        return normalized, changed

    stale_phase = str(normalized.get("phase", "") or "").strip().lower()
    stale_identity = process_identity_from_mapping(normalized)
    if (
        stale_phase in _WORKFLOW_STARTUP_PHASES
        and stale_identity.pid > 0
        and not isinstance(normalized.get("startup_previous_owner"), Mapping)
    ):
        normalized["startup_previous_owner"] = _startup_previous_owner_metadata(
            normalized,
            stale_identity,
        )
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

    if stale_phase not in _LIVE_STATUS_TERMINAL_PHASES:
        normalized["phase"] = "dead"
    return normalized, changed


def enqueue_workflow_agent_message(
    agent_ref: str, text: str, *, kind: str = "message"
) -> dict[str, Any]:
    """Enqueue a user message to a live workflow agent's inbox after validating process liveness.

    Rejects if agent process is dead or in terminal state. Appends message to agent inbox, records sequence number, and logs agent-input-queued activity.
    """
    agent_id = resolve_workflow_agent_id(agent_ref)
    if not agent_id:
        return {"success": False, "error": "Agent not found or ambiguous."}
    detail = workflow_agent_detail(agent_id, activity_limit=1)
    process_id = int(detail.get("process_id", 0) or 0)
    status = str(detail.get("status", "") or "")
    if process_id <= 0 or not _workflow_process_identity_is_live(detail, require_verified=True):
        return {
            "success": False,
            "error": "Agent process is no longer running.",
            "agent_id": agent_id,
        }
    if status in {"exited", "stopped", "interrupted"}:
        return {
            "success": False,
            "error": f"Agent is no longer accepting input ({status}).",
            "agent_id": agent_id,
        }
    message = str(text or "").strip()
    if not message:
        return {"success": False, "error": "Message is empty.", "agent_id": agent_id}
    path = workflow_agent_inbox_path(agent_id)
    ensure_directory(path.parent)
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
    events: deque[dict[str, Any]] = deque(maxlen=max(1, limit))
    events.extend(
        load_retained_recent_events(
            workflow_state_root(),
            limit=max(1, limit),
            agent_id=agent_id,
            event_types=event_types,
        )
    )
    for event in _iter_all_workflow_activity():
        if agent_id:
            details = event.get("details")
            if not isinstance(details, dict):
                continue
            if str(details.get("agent_session_id", "") or "") != agent_id:
                continue
        if event_types and str(event.get("type", "") or "") not in event_types:
            continue
        events.append(event)
    return list(events)


_TERMINAL_AGENT_STATUSES = {"completed", "exited", "stopped", "interrupted", "dead", "failed"}


_RETAINED_ACTIVITY_DETAIL_KEYS = {
    "agent_session_id",
    "parent_agent_session_id",
    "delegate_depth",
    "workflow_kind",
    "workflow_command",
    "active_skill",
    "project_root",
    "run_scope",
    "process_id",
    "process_group_id",
    "process_session_id",
    "process_token_sha256",
    "iteration",
    "api_calls",
    "completed",
    "interrupted",
    "status",
    "exit_code",
    "reason",
}


def _compact_workflow_activity_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded status/transcript tail record for the hot catalog."""
    details = event.get("details")
    details = details if isinstance(details, Mapping) else {}
    compact_details = {
        key: details[key] for key in _RETAINED_ACTIVITY_DETAIL_KEYS if key in details
    }
    compact_details["archived_preview"] = _agent_event_preview(event)
    return {
        key: event[key]
        for key in (
            "event_id",
            "timestamp",
            "type",
            "run_id",
            "agent_id",
            "task_label",
            "run_scope",
        )
        if key in event
    } | {
        "message": _shorten_text(event.get("message", ""), limit=500),
        "details": compact_details,
    }


def _reduce_workflow_agent_event(
    by_agent: dict[str, dict[str, Any]],
    event: Mapping[str, Any],
    metadata: Mapping[str, Any],
    activity_limit: int,
) -> None:
    """Fold one activity event into the status summary used by hot and cold state."""
    details = event.get("details")
    if not isinstance(details, Mapping):
        return
    agent_id = str(details.get("agent_session_id", "") or "")
    if not agent_id:
        return
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
            "process_group_id": 0,
            "process_session_id": 0,
            "process_token_sha256": "",
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
    summary["parent_agent_id"] = str(
        details.get("parent_agent_session_id", "") or summary["parent_agent_id"]
    )
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
    with contextlib.suppress(Exception):
        summary["delegate_depth"] = int(
            details.get("delegate_depth", summary["delegate_depth"]) or 0
        )
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
            if process_id != int(summary.get("process_id", 0) or 0):
                summary["process_group_id"] = 0
                summary["process_session_id"] = 0
                summary["process_token_sha256"] = ""
            summary["process_id"] = process_id
            for key in (
                "process_group_id",
                "process_session_id",
                "process_token_sha256",
            ):
                value = details.get(key)
                if value not in (None, ""):
                    summary[key] = value
    except Exception:
        pass
    timestamp = str(event.get("timestamp", "") or "")
    event_type = str(event.get("type", "") or "")
    event_preview = _agent_event_preview(event)
    summary["last_event_type"] = event_type
    summary["last_event_at"] = timestamp
    summary["last_message"] = event_preview
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
        with contextlib.suppress(Exception):
            summary["api_calls"] = max(
                int(details.get("api_calls", 0) or 0), int(summary["api_calls"] or 0)
            )
    elif event_type == "api-request":
        with contextlib.suppress(Exception):
            summary["api_calls"] = max(
                int(details.get("iteration", 0) or 0), int(summary["api_calls"] or 0)
            )
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
            "preview": event_preview,
        }
    )
    summary["_recent_activity"] = summary["_recent_activity"][-max(1, activity_limit) :]


def summarize_workflow_agents(*, activity_limit: int = 5) -> list[dict[str, Any]]:
    """Build live agent summaries from all activity events with status tracking, process checks, and live-phase correlation.

    Merges run metadata, extracts task labels and model details, tracks API and tool call counts, detects dead processes, and syncs active task status from live_status.json. Returns agents ordered by recency.
    """
    activity_paths = _workflow_activity_paths()
    cached_by_agent, activity_fingerprint = _cached_agent_summary_base(
        activity_paths,
        activity_limit,
    )
    cache_hit = cached_by_agent is not None
    by_agent = (
        cached_by_agent
        if cached_by_agent is not None
        else load_retained_agent_summaries(
            workflow_state_root(),
            activity_limit=activity_limit,
        )
    )
    run_metadata_cache: dict[str, dict[str, Any]] = {}
    events = () if cache_hit else iter_jsonl_dicts(activity_paths)
    for event in events:
        details = event.get("details")
        if not isinstance(details, dict):
            continue
        run_id = str(event.get("run_id", "") or "")
        if run_id:
            metadata = run_metadata_cache.get(run_id)
            if metadata is None:
                metadata = _read_workflow_run_metadata(run_id)
                run_metadata_cache[run_id] = metadata
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
                "process_group_id": 0,
                "process_session_id": 0,
                "process_token_sha256": "",
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
        summary["parent_agent_id"] = str(
            details.get("parent_agent_session_id", "") or summary["parent_agent_id"]
        )
        run_scope = str(details.get("run_scope", "") or metadata.get("run_scope", "") or "")
        if run_scope:
            summary["_run_scope"] = run_scope
        project_root = str(
            details.get("project_root", "") or metadata.get("project_root", "") or ""
        )
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
        with contextlib.suppress(Exception):
            summary["delegate_depth"] = int(
                details.get("delegate_depth", summary["delegate_depth"]) or 0
            )
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
                if process_id != int(summary.get("process_id", 0) or 0):
                    summary["process_group_id"] = 0
                    summary["process_session_id"] = 0
                    summary["process_token_sha256"] = ""
                summary["process_id"] = process_id
                for key in (
                    "process_group_id",
                    "process_session_id",
                    "process_token_sha256",
                ):
                    value = details.get(key)
                    if value not in (None, ""):
                        summary[key] = value
        except Exception:
            pass
        timestamp = str(event.get("timestamp", "") or "")
        event_type = str(event.get("type", "") or "")
        event_preview = _agent_event_preview(event)
        summary["last_event_type"] = event_type
        summary["last_event_at"] = timestamp
        summary["last_message"] = event_preview
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
            with contextlib.suppress(Exception):
                summary["api_calls"] = max(
                    int(details.get("api_calls", 0) or 0), int(summary["api_calls"] or 0)
                )
        elif event_type == "api-request":
            with contextlib.suppress(Exception):
                summary["api_calls"] = max(
                    int(details.get("iteration", 0) or 0), int(summary["api_calls"] or 0)
                )
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
                "preview": event_preview,
            }
        )
        summary["_recent_activity"] = summary["_recent_activity"][-max(1, activity_limit) :]

    if not cache_hit:
        _store_agent_summary_base(
            activity_limit=activity_limit,
            fingerprint=activity_fingerprint,
            by_agent=by_agent,
        )

    ordered = sorted(
        (deepcopy(summary) for summary in by_agent.values()),
        key=lambda item: (
            str(item.get("last_event_at", "") or ""),
            str(item.get("agent_id", "") or ""),
        ),
        reverse=True,
    )
    for summary in ordered:
        process_id = int(summary.get("process_id", 0) or 0)
        status = str(summary.get("status", "") or "")
        if (
            process_id > 0
            and status not in _TERMINAL_AGENT_STATUSES
            and not _workflow_process_identity_is_live(summary)
        ):
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
    live_process_alive = live_process_id > 0 and _workflow_process_identity_is_live(live_status)
    if live_phase:
        for summary in ordered:
            if int(summary.get("delegate_depth", 0) or 0) != 0:
                continue
            if live_task_label and str(summary.get("task_label", "") or "") != live_task_label:
                continue
            summary_process_id = int(summary.get("process_id", 0) or 0)
            if summary_process_id > 0 and not _workflow_process_identity_is_live(summary):
                continue
            if (
                live_process_id > 0
                and summary_process_id > 0
                and summary_process_id != live_process_id
            ):
                continue
            if live_process_id > 0 and not live_process_alive:
                continue
            live_token = str(live_status.get("process_token_sha256", "") or "")
            summary_token = str(summary.get("process_token_sha256", "") or "")
            if live_token and summary_token and live_token != summary_token:
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
    """Extract conversation-shaped transcript for an agent, mapping activity events to roles (user/assistant/tool-call/event).

    Filters to conversation events, previews tool I/O, and returns most recent turn limit with all supporting tool exchanges.
    """
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
        content = str(details.get("archived_preview", "") or event.get("message", "") or "")
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
        elif (
            event_type == "conversation-end"
            or event_type == "agent-awaiting-input"
            or event_type == "runner-exit"
        ):
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
    return transcript[-max(1, limit) :]


def workflow_agent_transcript_all(agent_id: str) -> list[dict[str, Any]]:
    return workflow_agent_transcript(agent_id, limit=10000)


def resolve_workflow_agent_id(agent_ref: str) -> str:
    ref = str(agent_ref or "").strip()
    if not ref:
        return ""
    summaries = summarize_workflow_agents(activity_limit=1)
    exact = [
        str(summary.get("agent_id", "") or "")
        for summary in summaries
        if str(summary.get("agent_id", "") or "") == ref
    ]
    if exact:
        return exact[0]
    prefix = [
        str(summary.get("agent_id", "") or "")
        for summary in summaries
        if str(summary.get("agent_id", "") or "").startswith(ref)
    ]
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
        return {
            "success": False,
            "error": "No process id recorded for this agent.",
            "agent_id": agent_id,
        }
    result = interrupt_workflow_process(detail)
    result["agent_id"] = agent_id
    return result


def terminate_workflow_agent_descendants(agent_ref: str) -> dict[str, Any]:
    """Terminate distinct descendant processes without signaling logical sessions.

    The activity graph contains both process-isolated workers and nested model
    sessions hosted by their parent's Python process. Traverse every logical
    edge so external grandchildren remain reachable, but skip session records
    sharing the root or caller PID and coalesce repeated verified identities.
    """
    agent_id = resolve_workflow_agent_id(agent_ref)
    if not agent_id:
        return {"success": False, "error": "Agent not found or ambiguous."}
    summaries = summarize_workflow_agents(activity_limit=1)
    by_agent = {
        str(summary.get("agent_id", "") or ""): summary
        for summary in summaries
        if str(summary.get("agent_id", "") or "")
    }
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
    skipped_same_process: list[str] = []
    coalesced_same_process: list[str] = []
    root_identity = process_identity_from_mapping(by_agent.get(agent_id, {}))
    caller_pid = os.getpid()
    targeted_identities: set[tuple[int, str]] = set()
    for child_id in descendants:
        child_identity = process_identity_from_mapping(by_agent.get(child_id, {}))
        if child_identity.pid > 0 and child_identity.pid in {root_identity.pid, caller_pid}:
            skipped_same_process.append(child_id)
            continue
        identity_key = (child_identity.pid, child_identity.token_sha256)
        if child_identity.verifiable and identity_key in targeted_identities:
            coalesced_same_process.append(child_id)
            continue
        if child_identity.verifiable:
            targeted_identities.add(identity_key)
        results.append(terminate_workflow_agent(child_id))

    success_count = sum(1 for item in results if item.get("success"))
    failed = [item for item in results if not item.get("success")]
    return {
        "success": not failed,
        "agent_id": agent_id,
        "terminated": [item.get("agent_id") for item in results if item.get("success")],
        "failed": failed,
        "count": success_count,
        "skipped_same_process": skipped_same_process,
        "coalesced_same_process": coalesced_same_process,
    }


def terminate_all_workflow_agents(
    *, exclude_agent_id: str = "", exclude_process_id: int = 0
) -> dict[str, Any]:
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
        if status in _TERMINAL_AGENT_STATUSES or not _workflow_process_identity_is_live(
            summary, require_verified=True
        ):
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
    """Queue graceful exit messages to all top-level (parent-less) workflow agents in a project with live processes.

    Excludes specified agent and process IDs; agents must be active and in the target project root to receive the exit signal.
    """
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
        if process_id <= 0 or not _workflow_process_identity_is_live(
            summary, require_verified=True
        ):
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
    """Forcefully terminate all non-terminal workflow agents in a project via SIGINT, excluding specified process and agent IDs.

    Only targets agents with matching project_root that are not already exited/stopped/completed and have live processes.
    """
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
        if status in _TERMINAL_AGENT_STATUSES or not _workflow_process_identity_is_live(
            summary, require_verified=True
        ):
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
    ensure_directory(path.parent)
    ensure_directory(workflow_runs_root())
    run_id = _workflow_run_id()
    os.environ["LEANFLOW_WORKFLOW_RUN_ID"] = run_id
    owner_path = _workflow_run_log_owner_path()
    with _RUN_LOG_APPEND_LOCK, _workflow_run_log_flock():
        path.write_text("", encoding="utf-8")
        workflow_timestamped_run_log_path().write_text("", encoding="utf-8")
        owner_token = _workflow_run_log_owner_token(run_id)
        owner_path.write_text(owner_token, encoding="utf-8")
        _RUN_LOG_RELEASED_OWNER_TOKENS.discard(owner_token)
    return path


def release_workflow_run_log_owner() -> bool:
    """Release only this process and run's latest-console ownership token.

    Timestamped run-log writes remain available during late interpreter
    cleanup, but this process cannot silently reacquire ``latest-run.log`` once
    finalization has released it. A concurrent runner's different token is
    preserved.
    """
    run_id = _workflow_run_id()
    owner_path = _workflow_run_log_owner_path()
    owner_token = _workflow_run_log_owner_token(run_id)
    with _RUN_LOG_APPEND_LOCK, _workflow_run_log_flock():
        _RUN_LOG_RELEASED_OWNER_TOKENS.add(owner_token)
        try:
            current_owner = owner_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        if current_owner != owner_token:
            return False
        owner_path.unlink(missing_ok=True)
        return True


def append_workflow_run_log(text: str) -> None:
    """Append owner console text under a dedicated cross-process lock."""
    if not text:
        return
    run_id = _workflow_run_id()
    path = workflow_run_log_path()
    ensure_directory(path.parent)
    timestamped_path = workflow_timestamped_run_log_path()
    ensure_directory(timestamped_path.parent)
    owner_path = _workflow_run_log_owner_path()
    owner_token = _workflow_run_log_owner_token(run_id)
    with _RUN_LOG_APPEND_LOCK, _workflow_run_log_flock():
        _append_plain_text(timestamped_path, text)
        try:
            current_owner = owner_path.read_text(encoding="utf-8")
        except OSError:
            current_owner = ""
        if not current_owner and owner_token not in _RUN_LOG_RELEASED_OWNER_TOKENS:
            owner_path.write_text(owner_token, encoding="utf-8")
            current_owner = owner_token
        if current_owner == owner_token:
            _append_plain_text(path, text)


def read_workflow_run_log(tail_lines: int = 120) -> str:
    """Return a bounded line tail without materializing the complete run log."""
    path = workflow_run_log_path()
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8") as handle:
            lines = deque(
                (line.rstrip("\r\n") for line in handle),
                maxlen=max(1, int(tail_lines)),
            )
    except (OSError, UnicodeDecodeError):
        logger.debug("Failed to read workflow run log %s", path, exc_info=True)
        return ""
    return "\n".join(lines)


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
