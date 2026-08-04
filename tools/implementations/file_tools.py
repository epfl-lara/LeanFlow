#!/usr/bin/env python3
"""File Tools Module - LLM agent file manipulation tools."""

import errno
import json
import logging
import threading
import time
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from core.runtime_modes import scratch_only_dispatch_worker_enabled
from leanflow_cli.runtime.file_locks import ensure_file_lock
from tools.implementations.file_operations import ShellFileOperations
from tools.response import dumps, error
from tools.utilities.read_freshness import (
    check_freshness,
    clear_freshness,
    note_write,
    record_read,
)
from tools.utilities.repository_research_policy import clean_room_path_block_reason
from tools.utilities.workflow_artifact_guard import (
    diagnostic_workflow_file_access_enabled,
    is_managed_plan_path,
    managed_plan_read_view,
    workflow_log_read_error,
    workflow_machine_snapshot_read_error,
    workflow_plan_pagination_error,
    workflow_state_search_error,
)

logger = logging.getLogger(__name__)

_EXPECTED_WRITE_ERRNOS = {errno.EACCES, errno.EPERM, errno.EROFS}


def _is_expected_write_exception(exc: Exception) -> bool:
    """Return True for expected write denials that should not hit error logs."""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno in _EXPECTED_WRITE_ERRNOS:
        return True
    return False


_file_ops_lock = threading.Lock()
_file_ops_cache: dict = {}

# Track files read per task to detect re-read loops after context compression.
# Per task_id we store:
#   "last_key":     the key of the most recent read/search call (or None)
#   "consecutive":  how many times that exact call has been repeated in a row
#   "read_history": set of (path, offset, limit) tuples for get_read_files_summary
_read_tracker_lock = threading.Lock()
_read_tracker: dict = {}


def _clean_room_path_denial(path: str) -> str | None:
    """Return a serialized clean-room denial for one escaped file path."""
    reason = clean_room_path_block_reason(path)
    if not reason:
        return None
    return dumps(
        {
            "success": False,
            "status": "clean_room_path_denied",
            "path": path,
            "error": reason,
        }
    )


def _filter_clean_room_search_result(result: Any) -> int:
    """Remove protected sibling-task paths from one search result in place."""
    omitted = 0
    if hasattr(result, "matches"):
        kept_matches = []
        for match in result.matches:
            if clean_room_path_block_reason(str(getattr(match, "path", "") or "")):
                omitted += 1
            else:
                kept_matches.append(match)
        result.matches = kept_matches
    if hasattr(result, "files"):
        kept_files = []
        for candidate in result.files:
            if clean_room_path_block_reason(str(candidate or "")):
                omitted += 1
            else:
                kept_files.append(candidate)
        result.files = kept_files
    if hasattr(result, "counts") and isinstance(result.counts, dict):
        result.counts = {
            candidate: count
            for candidate, count in result.counts.items()
            if not clean_room_path_block_reason(str(candidate or ""))
        }
    if omitted and hasattr(result, "total_count"):
        result.total_count = max(0, int(result.total_count or 0) - omitted)
    return omitted


def _clean_room_patch_denial(mode: str, path: str | None, patch: str | None) -> str | None:
    """Return a denial when any patch source or destination escapes the project."""
    if mode == "replace":
        return _clean_room_path_denial(path) if path else None
    if mode != "patch" or not patch:
        return None

    from tools.utilities.patch_parser import OperationType, parse_v4a_patch

    operations, _parse_error = parse_v4a_patch(patch)
    for operation in operations or []:
        candidates = [operation.file_path]
        if operation.operation == OperationType.MOVE and operation.new_path:
            candidates.append(operation.new_path)
        for candidate in candidates:
            denial = _clean_room_path_denial(candidate)
            if denial:
                return denial
    return None


def _get_file_ops(task_id: str = "default") -> ShellFileOperations:
    """Get or create ShellFileOperations for a terminal environment.

    Respects the TERMINAL_ENV setting -- if the task_id doesn't have an
    environment yet, creates one using the configured backend (local, docker,
    modal, etc.) rather than always defaulting to local.

    Thread-safe: uses the same per-task creation locks as terminal_tool to
    prevent duplicate sandbox creation from concurrent tool calls.
    """
    import time

    from tools.implementations.terminal_tool import (
        _active_environments,
        _create_environment,
        _creation_locks,
        _creation_locks_lock,
        _env_lock,
        _get_env_config,
        _last_activity,
        _start_cleanup_thread,
    )

    # Fast path: check cache -- but also verify the underlying environment
    # is still alive (it may have been killed by the cleanup thread).
    with _file_ops_lock:
        cached = _file_ops_cache.get(task_id)
    if cached is not None:
        with _env_lock:
            if task_id in _active_environments:
                _last_activity[task_id] = time.time()
                return cached
            else:
                # Environment was cleaned up -- invalidate stale cache entry
                with _file_ops_lock:
                    _file_ops_cache.pop(task_id, None)

    # Need to ensure the environment exists before building file_ops.
    # Acquire per-task lock so only one thread creates the sandbox.
    with _creation_locks_lock:
        if task_id not in _creation_locks:
            _creation_locks[task_id] = threading.Lock()
        task_lock = _creation_locks[task_id]

    with task_lock:
        # Double-check: another thread may have created it while we waited
        with _env_lock:
            if task_id in _active_environments:
                _last_activity[task_id] = time.time()
                terminal_env = _active_environments[task_id]
            else:
                terminal_env = None

        if terminal_env is None:
            from tools.implementations.terminal_tool import _task_env_overrides

            config = _get_env_config()
            env_type = config["env_type"]
            overrides = _task_env_overrides.get(task_id, {})

            if env_type == "docker":
                image = overrides.get("docker_image") or config["docker_image"]
            elif env_type == "singularity":
                image = overrides.get("singularity_image") or config["singularity_image"]
            elif env_type == "modal":
                image = overrides.get("modal_image") or config["modal_image"]
            elif env_type == "daytona":
                image = overrides.get("daytona_image") or config["daytona_image"]
            else:
                image = ""

            cwd = overrides.get("cwd") or config["cwd"]
            logger.info("Creating new %s environment for task %s...", env_type, task_id[:8])

            container_config = None
            if env_type in ("docker", "singularity", "modal", "daytona"):
                container_config = {
                    "container_cpu": config.get("container_cpu", 1),
                    "container_memory": config.get("container_memory", 5120),
                    "container_disk": config.get("container_disk", 51200),
                    "container_persistent": config.get("container_persistent", True),
                    "docker_volumes": config.get("docker_volumes", []),
                }

            ssh_config = None
            if env_type == "ssh":
                ssh_config = {
                    "host": config.get("ssh_host", ""),
                    "user": config.get("ssh_user", ""),
                    "port": config.get("ssh_port", 22),
                    "key": config.get("ssh_key", ""),
                    "persistent": config.get("ssh_persistent", False),
                }

            local_config = None
            if env_type == "local":
                local_config = {
                    "persistent": config.get("local_persistent", False),
                }

            terminal_env = _create_environment(
                env_type=env_type,
                image=image,
                cwd=cwd,
                timeout=config["timeout"],
                ssh_config=ssh_config,
                container_config=container_config,
                local_config=local_config,
                task_id=task_id,
                host_cwd=config.get("host_cwd"),
            )

            with _env_lock:
                _active_environments[task_id] = terminal_env
                _last_activity[task_id] = time.time()

            _start_cleanup_thread()
            logger.info("%s environment ready for task %s", env_type, task_id[:8])

    # Build file_ops from the (guaranteed live) environment and cache it
    file_ops = ShellFileOperations(terminal_env)
    with _file_ops_lock:
        _file_ops_cache[task_id] = file_ops
    return file_ops


def clear_file_ops_cache(task_id: str = None):
    """Clear the file operations cache."""
    with _file_ops_lock:
        if task_id:
            _file_ops_cache.pop(task_id, None)
        else:
            _file_ops_cache.clear()


def read_file_tool(path: str, offset: int = 1, limit: int = 2000, task_id: str = "default") -> str:
    """Read a file with pagination and line numbers."""
    try:
        clean_room_denial = _clean_room_path_denial(path)
        if clean_room_denial:
            return clean_room_denial
        guard_error = workflow_log_read_error(path)
        if guard_error:
            return dumps({"error": guard_error, "path": path, "workflow_log_blocked": True})
        snapshot_error = workflow_machine_snapshot_read_error(path)
        if snapshot_error:
            return dumps(
                {
                    "error": snapshot_error,
                    "path": path,
                    "workflow_snapshot_blocked": True,
                }
            )
        plan_error = workflow_plan_pagination_error(path, offset)
        if plan_error:
            return dumps({"error": plan_error, "path": path, "workflow_plan_blocked": True})
        file_ops = _get_file_ops(task_id)
        result = file_ops.read_file(path, offset, limit)
        if is_managed_plan_path(path) and getattr(result, "error", None):
            # Plan regeneration is an atomic replace, but the first route can
            # advertise the path just before its initial materialization.
            # Bridge that bounded startup transition instead of telling the
            # model the managed plan disappeared.
            for _attempt in range(3):
                time.sleep(0.05)
                result = file_ops.read_file(path, offset, limit)
                if not getattr(result, "error", None):
                    break
        if result.content:
            result.content = managed_plan_read_view(path, redact_sensitive_text(result.content))
        plan_view_applied = (
            is_managed_plan_path(path) and not diagnostic_workflow_file_access_enabled()
        )
        if plan_view_applied and not getattr(result, "error", None):
            result.truncated = False
            result.hint = (
                "Read-only managed plan view; historical user Notes are excluded and model "
                "writes to plan.md are blocked. Do not paginate this file. Refresh the queue "
                "assignment and Lean diagnostics for current inventory and declaration truth."
            )
        result_dict = result.to_dict()
        if plan_view_applied and not getattr(result, "error", None):
            result_dict["managed_plan_view"] = True
            result_dict["historical_notes_excluded"] = True

        # D2 read-before-edit freshness: record the hash of the raw on-disk file
        # so a later patch can detect it editing stale content. We hash the full
        # raw file (not the paginated view) and tolerate read_raw being absent or
        # non-str (e.g. mocked) — freshness is best-effort, never blocks a read.
        if not getattr(result, "error", None):
            try:
                raw = file_ops.read_raw(path)
                if isinstance(raw, str):
                    record_read(task_id, path, raw)
            except Exception:
                pass

        # Track reads to detect *consecutive* re-read loops.
        # The counter resets whenever any other tool is called in between,
        # so only truly back-to-back identical reads trigger warnings/blocks.
        read_key = ("read", path, offset, limit)
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(
                task_id,
                {
                    "last_key": None,
                    "consecutive": 0,
                    "read_history": set(),
                },
            )
            task_data["read_history"].add((path, offset, limit))
            if task_data["last_key"] == read_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = read_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

        if count >= 4:
            # Hard block: stop returning content to break the loop
            return dumps(
                {
                    "error": (
                        f"BLOCKED: You have read this exact file region {count} times in a row. "
                        "The content has NOT changed. You already have this information. "
                        "STOP re-reading and proceed with your task."
                    ),
                    "path": path,
                    "already_read": count,
                }
            )
        elif count >= 3:
            result_dict["_warning"] = (
                f"You have read this exact file region {count} times consecutively. "
                "The content has not changed since your last read. Use the information you already have. "
                "If you are stuck in a loop, stop reading and proceed with writing or responding."
            )

        return dumps(result_dict)
    except Exception as e:
        return error(str(e))


def get_read_files_summary(task_id: str = "default") -> list:
    """Return a list of files read in this session for the given task.

    Used by context compression to preserve file-read history across
    compression boundaries.
    """
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id, {})
        read_history = task_data.get("read_history", set())
        seen_paths: dict = {}
        for path, offset, limit in read_history:
            if path not in seen_paths:
                seen_paths[path] = []
            seen_paths[path].append(f"lines {offset}-{offset + limit - 1}")
        return [{"path": p, "regions": regions} for p, regions in sorted(seen_paths.items())]


def clear_read_tracker(task_id: str = None):
    """Clear the read tracker.

    Call with a task_id to clear just that task, or without to clear all.
    Should be called when a session is destroyed to prevent memory leaks
    in long-running gateway processes.
    """
    with _read_tracker_lock:
        if task_id:
            _read_tracker.pop(task_id, None)
        else:
            _read_tracker.clear()
    # Keep freshness hashes in lockstep so neither outlives the session.
    clear_freshness(task_id)


def notify_other_tool_call(task_id: str = "default"):
    """Reset consecutive read/search counter for a task.

    Called by the tool dispatcher (model_tools.py) whenever a tool OTHER
    than read_file / search_files is executed.  This ensures we only warn
    or block on *truly consecutive* repeated reads — if the agent does
    anything else in between (write, patch, terminal, etc.) the counter
    resets and the next read is treated as fresh.
    """
    with _read_tracker_lock:
        task_data = _read_tracker.get(task_id)
        if task_data:
            task_data["last_key"] = None
            task_data["consecutive"] = 0


def _guard_file_lock(path: str, owner_id: str, purpose: str) -> dict | None:
    result = ensure_file_lock(path, owner_id=owner_id, purpose=purpose)
    if result.get("success"):
        return None
    return {
        "error": str(result.get("error", "file is locked")),
        "path": path,
        "lock": result.get("lock"),
    }


def write_file_tool(path: str, content: str, task_id: str = "default", owner_id: str = "") -> str:
    """Write content to a file."""
    clean_room_denial = _clean_room_path_denial(path)
    if clean_room_denial:
        return clean_room_denial
    if scratch_only_dispatch_worker_enabled():
        return dumps(
            {
                "success": False,
                "status": "scratch_only_write_denied",
                "path": path,
                "error": "Scratch-only research jobs cannot write project files.",
            }
        )
    if is_managed_plan_path(path) and not diagnostic_workflow_file_access_enabled():
        return dumps(
            {
                "success": False,
                "status": "managed_plan_write_denied",
                "path": path,
                "error": (
                    "Managed plan.md cannot be overwritten because that would erase hidden "
                    "historical Notes and machine-owned generated sections. The managed plan is "
                    "read-only to model file tools; use current queue/kernel state and structured "
                    "planner findings instead."
                ),
            }
        )
    try:
        if owner_id:
            conflict = _guard_file_lock(path, owner_id, "write_file")
            if conflict:
                return dumps(conflict)
        file_ops = _get_file_ops(task_id)
        result = file_ops.write_file(path, content)
        result_dict = result.to_dict()
        # Record the just-written content so a subsequent patch of this file isn't
        # hard-rejected as "stale" against the agent's own write (D2 freshness).
        if not result_dict.get("error"):
            note_write(task_id, path, content)
        return dumps(result_dict)
    except Exception as e:
        if _is_expected_write_exception(e):
            logger.debug("write_file expected denial: %s: %s", type(e).__name__, e)
        else:
            logger.error("write_file error: %s: %s", type(e).__name__, e, exc_info=True)
        return error(str(e))


def _freshness_guard(file_ops, path: str, task_id: str) -> tuple[str | None, str | None]:
    """Enforce the read-before-edit contract for a single-file edit.

    Returns (current_raw, warning):
      * Raises FreshnessError when the on-disk content changed since the agent
        last read it — a hard reject, because the edit is based on stale content.
      * Returns a soft warning string (not an error) when the file was never read.
    Best-effort: if the raw content is unavailable (e.g. mocked or unreadable),
    returns (None, None) and the edit proceeds unguarded, so existing flows that
    legitimately patch without reading are never broken.
    """
    try:
        raw = file_ops.read_raw(path)
    except Exception:
        return None, None
    if not isinstance(raw, str):
        return None, None

    verdict = check_freshness(task_id, path, raw)
    if verdict.status == "stale":
        raise _FreshnessError(verdict.message)
    if verdict.status == "never_read":
        return raw, verdict.message
    return raw, None


class _FreshnessError(Exception):
    """Raised when a patch would edit content that changed since the last read."""


def patch_tool(
    mode: str = "replace",
    path: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    patch: str = None,
    task_id: str = "default",
    owner_id: str = "",
    strict: bool = False,
) -> str:
    """Patch a file using replace mode or V4A patch format.

    `strict` makes the edit exact-or-fail (no fuzzy/whitespace relocation) — for
    high-risk edits where applying to a merely-similar region would be wrong.
    """
    clean_room_denial = _clean_room_patch_denial(mode, path, patch)
    if clean_room_denial:
        return clean_room_denial
    if scratch_only_dispatch_worker_enabled():
        return dumps(
            {
                "success": False,
                "status": "scratch_only_write_denied",
                "path": path or "",
                "error": "Scratch-only research jobs cannot patch project files.",
            }
        )
    if not diagnostic_workflow_file_access_enabled():
        if mode == "replace" and path and is_managed_plan_path(path):
            return dumps(
                {
                    "success": False,
                    "status": "managed_plan_patch_denied",
                    "path": path,
                    "error": (
                        "Managed plan.md is read-only to model file tools. Historical Notes "
                        "are user-owned and generated Strategy/Grounding state is persisted "
                        "by the workflow manager."
                    ),
                }
            )
        if mode == "patch" and patch:
            from tools.utilities.patch_parser import OperationType, parse_v4a_patch

            preflight_ops, _preflight_error = parse_v4a_patch(patch)
            for operation in preflight_ops or []:
                source_is_managed = is_managed_plan_path(operation.file_path)
                destination_is_managed = bool(
                    operation.operation == OperationType.MOVE
                    and operation.new_path
                    and is_managed_plan_path(operation.new_path)
                )
                if not source_is_managed and not destination_is_managed:
                    continue
                blocked_path = (
                    operation.new_path
                    if destination_is_managed and operation.new_path
                    else operation.file_path
                )
                return dumps(
                    {
                        "success": False,
                        "status": "managed_plan_operation_denied",
                        "path": blocked_path,
                        "error": (
                            "Managed plan.md is read-only to model file tools and cannot be "
                            "added, updated, deleted, or moved. Historical Notes are user-owned; "
                            "the workflow manager persists generated planning state."
                        ),
                    }
                )
    try:
        file_ops = _get_file_ops(task_id)
        freshness_warning: str | None = None

        if mode == "replace":
            if not path:
                return json.dumps({"error": "path required"})
            if old_string is None or new_string is None:
                return json.dumps({"error": "old_string and new_string required"})
            if owner_id:
                conflict = _guard_file_lock(path, owner_id, "patch")
                if conflict:
                    return dumps(conflict)
            # D2 read-before-edit: hard-reject a stale edit, soft-warn a never-read one.
            try:
                _raw, freshness_warning = _freshness_guard(file_ops, path, task_id)
            except _FreshnessError as fe:
                return dumps({"success": False, "error": str(fe), "path": path, "stale": True})
            if strict and freshness_warning:
                return dumps(
                    {
                        "success": False,
                        "status": "fresh_read_required",
                        "error": (
                            "Strict edits require a fresh read of the exact target file in this "
                            "tool session before patching."
                        ),
                        "path": path,
                    }
                )
            # Only forward `strict` when set, so the default call shape (path, old, new,
            # replace_all) — which callers and tests assert on — is unchanged.
            if strict:
                result = file_ops.patch_replace(
                    path, old_string, new_string, replace_all, strict=True
                )
            else:
                result = file_ops.patch_replace(path, old_string, new_string, replace_all)
            # Refresh the tracked hash to the just-written content so the agent's
            # own edit doesn't make a follow-up edit look stale.
            if getattr(result, "success", False):
                try:
                    new_raw = file_ops.read_raw(path)
                    if isinstance(new_raw, str):
                        note_write(task_id, path, new_raw)
                except Exception:
                    pass
        elif mode == "patch":
            if not patch:
                return json.dumps({"error": "patch content required"})
            # D2 read-before-edit for V4A: reject if any UPDATE target changed since last read,
            # soft-warn for never-read targets — mirroring replace mode, which the schema promises.
            from tools.utilities.patch_parser import OperationType, parse_v4a_patch

            ops, _parse_err = parse_v4a_patch(patch)
            update_paths = [
                op.file_path
                for op in (ops or [])
                if op.operation in (OperationType.UPDATE, OperationType.MOVE)
            ]
            warnings: list[str] = []
            try:
                for update_path in update_paths:
                    _raw, warning = _freshness_guard(file_ops, update_path, task_id)
                    if warning:
                        warnings.append(warning)
            except _FreshnessError as fe:
                return dumps({"success": False, "error": str(fe), "stale": True})
            if warnings and not freshness_warning:
                freshness_warning = " ".join(warnings)
            if strict and freshness_warning:
                return dumps(
                    {
                        "success": False,
                        "status": "fresh_read_required",
                        "error": (
                            "Strict edits require fresh reads of every updated target file in "
                            "this tool session before patching."
                        ),
                    }
                )
            result = file_ops.patch_v4a(patch, strict=True) if strict else file_ops.patch_v4a(patch)
            # Refresh tracked hashes for the files this patch just wrote.
            if getattr(result, "success", False):
                for update_path in update_paths:
                    try:
                        new_raw = file_ops.read_raw(update_path)
                        if isinstance(new_raw, str):
                            note_write(task_id, update_path, new_raw)
                    except Exception:
                        pass
        else:
            return json.dumps({"error": f"Unknown mode: {mode}"})

        result_dict = result.to_dict()
        # Surface the never-read nudge without changing the edit outcome.
        if freshness_warning and not result_dict.get("freshness_warning"):
            result_dict["freshness_warning"] = freshness_warning
        result_json = dumps(result_dict)
        # Hint when old_string not found — saves iterations where the agent
        # retries with stale content instead of re-reading the file.
        if result_dict.get("error") and "Could not find" in str(result_dict["error"]):
            result_json += "\n\n[Hint: old_string not found. Use read_file to verify the current content, or search_files to locate the text.]"
        return result_json
    except Exception as e:
        return error(str(e))


def search_tool(
    pattern: str,
    target: str = "content",
    path: str = ".",
    file_glob: str = None,
    limit: int = 50,
    offset: int = 0,
    output_mode: str = "content",
    context: int = 0,
    task_id: str = "default",
) -> str:
    """Search for content or files."""
    try:
        clean_room_denial = _clean_room_path_denial(path)
        if clean_room_denial:
            return clean_room_denial
        guard_error = workflow_state_search_error(path)
        if guard_error:
            return dumps(
                {
                    "error": guard_error,
                    "path": path,
                    "workflow_state_blocked": True,
                }
            )
        # Track searches to detect *consecutive* repeated search loops.
        search_key = ("search", pattern, target, str(path), file_glob or "")
        with _read_tracker_lock:
            task_data = _read_tracker.setdefault(
                task_id,
                {
                    "last_key": None,
                    "consecutive": 0,
                    "read_history": set(),
                },
            )
            if task_data["last_key"] == search_key:
                task_data["consecutive"] += 1
            else:
                task_data["last_key"] = search_key
                task_data["consecutive"] = 1
            count = task_data["consecutive"]

        if count >= 4:
            return dumps(
                {
                    "error": (
                        f"BLOCKED: You have run this exact search {count} times in a row. "
                        "The results have NOT changed. You already have this information. "
                        "STOP re-searching and proceed with your task."
                    ),
                    "pattern": pattern,
                    "already_searched": count,
                }
            )

        file_ops = _get_file_ops(task_id)
        result = file_ops.search(
            pattern=pattern,
            path=path,
            target=target,
            file_glob=file_glob,
            limit=limit,
            offset=offset,
            output_mode=output_mode,
            context=context,
        )
        clean_room_omitted = _filter_clean_room_search_result(result)
        if hasattr(result, "matches"):
            for m in result.matches:
                if hasattr(m, "content") and m.content:
                    m.content = redact_sensitive_text(m.content)
        result_dict = result.to_dict()
        if clean_room_omitted:
            result_dict["clean_room_omitted_results"] = clean_room_omitted

        if count >= 3:
            result_dict["_warning"] = (
                f"You have run this exact search {count} times consecutively. "
                "The results have not changed. Use the information you already have."
            )

        result_json = dumps(result_dict)
        # Hint when results were truncated — explicit next offset is clearer
        # than relying on the model to infer it from total_count vs match count.
        if result_dict.get("truncated"):
            next_offset = offset + limit
            result_json += f"\n\n[Hint: Results truncated. Use offset={next_offset} to see more, or narrow with a more specific pattern or file_glob.]"
        return result_json
    except Exception as e:
        return error(str(e))


FILE_TOOLS = [
    {"name": "read_file", "function": read_file_tool},
    {"name": "write_file", "function": write_file_tool},
    {"name": "patch", "function": patch_tool},
    {"name": "search_files", "function": search_tool},
]


def get_file_tools():
    """Get the list of file tool definitions."""
    return FILE_TOOLS


# ---------------------------------------------------------------------------
# Schemas + Registry
# ---------------------------------------------------------------------------
from tools.registry import registry


def _check_file_reqs():
    """Lazy wrapper to avoid circular import with tools/__init__.py."""
    from tools import check_file_requirements

    return check_file_requirements()


READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Suggests similar filenames if not found. Use offset and limit for large files. Managed workflow logs are blocked to prevent recursive self-ingestion; campaign findings arrive through structured workflow context. NOTE: Cannot read images or binary files.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read (absolute, relative, or ~/path)",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (1-indexed, default: 1)",
                "default": 1,
                "minimum": 1,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read (default: 2000, max: 2000)",
                "default": 2000,
                "maximum": 2000,
            },
        },
        "required": ["path"],
    },
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for targeted edits.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to write (will be created if it doesn't exist, overwritten if it does)",
            },
            "content": {"type": "string", "description": "Complete content to write to the file"},
        },
        "required": ["path", "content"],
    },
}

PATCH_SCHEMA = {
    "name": "patch",
    "description": "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. Uses fuzzy matching (8 strategies) so minor whitespace/indentation differences won't break it. Returns a unified diff and reports which strategy matched ('matched_via') and its confidence ('similarity'); a low similarity means the match was approximate, so verify the diff. Auto-runs syntax checks after editing. Editing a file whose on-disk content changed since you last read it is rejected — re-read first.\n\nReplace mode (default): find a unique string and replace it.\nPatch mode: apply V4A multi-file patches for bulk changes.",
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["replace", "patch"],
                "description": "Edit mode: 'replace' for targeted find-and-replace, 'patch' for V4A multi-file patches",
                "default": "replace",
            },
            "path": {
                "type": "string",
                "description": "File path to edit (required for 'replace' mode)",
            },
            "old_string": {
                "type": "string",
                "description": "Text to find in the file (required for 'replace' mode). Must be unique in the file unless replace_all=true. Include enough surrounding context to ensure uniqueness.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text (required for 'replace' mode). Can be empty string to delete the matched text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences instead of requiring a unique match (default: false)",
                "default": False,
            },
            "patch": {
                "type": "string",
                "description": "V4A format patch content (required for 'patch' mode). Format:\n*** Begin Patch\n*** Update File: path/to/file\n@@ context hint @@\n context line\n-removed line\n+added line\n*** End Patch",
            },
            "strict": {
                "type": "boolean",
                "description": "Require an exact match — disable whitespace/fuzzy relocation. Use for high-risk edits where applying to a similar-but-wrong region would be damaging (default: false).",
                "default": False,
            },
        },
        "required": ["mode"],
    },
}

SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": "Search file contents or find files by name. Use this instead of grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents. Repository-wide searches exclude .leanflow managed state and logs to prevent recursive self-ingestion.\n\nContent search (target='content'): Regex search inside files. Output modes: full matches with line numbers, file paths only, or match counts.\n\nFile search (target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls — results sorted by modification time.",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern for content search, or glob pattern (e.g., '*.py') for file search",
            },
            "target": {
                "type": "string",
                "enum": ["content", "files"],
                "description": "'content' searches inside file contents, 'files' searches for files by name",
                "default": "content",
            },
            "path": {
                "type": "string",
                "description": "Directory or file to search in (default: current working directory)",
                "default": ".",
            },
            "file_glob": {
                "type": "string",
                "description": "Filter files by pattern in grep mode (e.g., '*.py' to only search Python files)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 50)",
                "default": 50,
            },
            "offset": {
                "type": "integer",
                "description": "Skip first N results for pagination (default: 0)",
                "default": 0,
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_only", "count"],
                "description": "Output format for grep mode: 'content' shows matching lines with line numbers, 'files_only' lists file paths, 'count' shows match counts per file",
                "default": "content",
            },
            "context": {
                "type": "integer",
                "description": "Number of context lines before and after each match (grep mode only)",
                "default": 0,
            },
        },
        "required": ["pattern"],
    },
}


def _handle_read_file(args, **kw):
    tid = kw.get("task_id") or "default"
    return read_file_tool(
        path=args.get("path", ""),
        offset=args.get("offset", 1),
        limit=args.get("limit", 500),
        task_id=tid,
    )


def _handle_write_file(args, **kw):
    tid = kw.get("task_id") or "default"
    owner = str(kw.get("owner_id", "") or "")
    return write_file_tool(
        path=args.get("path", ""), content=args.get("content", ""), task_id=tid, owner_id=owner
    )


def _handle_patch(args, **kw):
    tid = kw.get("task_id") or "default"
    owner = str(kw.get("owner_id", "") or "")
    return patch_tool(
        mode=args.get("mode", "replace"),
        path=args.get("path"),
        old_string=args.get("old_string"),
        new_string=args.get("new_string"),
        replace_all=args.get("replace_all", False),
        patch=args.get("patch"),
        task_id=tid,
        owner_id=owner,
        strict=args.get("strict", False),
    )


def _handle_search_files(args, **kw):
    tid = kw.get("task_id") or "default"
    target_map = {"grep": "content", "find": "files"}
    raw_target = args.get("target", "content")
    target = target_map.get(raw_target, raw_target)
    return search_tool(
        pattern=args.get("pattern", ""),
        target=target,
        path=args.get("path", "."),
        file_glob=args.get("file_glob"),
        limit=args.get("limit", 50),
        offset=args.get("offset", 0),
        output_mode=args.get("output_mode", "content"),
        context=args.get("context", 0),
        task_id=tid,
    )


registry.register(
    name="read_file",
    toolset="file",
    schema=READ_FILE_SCHEMA,
    handler=_handle_read_file,
    check_fn=_check_file_reqs,
    emoji="📖",
)
registry.register(
    name="write_file",
    toolset="file",
    schema=WRITE_FILE_SCHEMA,
    handler=_handle_write_file,
    check_fn=_check_file_reqs,
    emoji="✍️",
)
registry.register(
    name="patch",
    toolset="file",
    schema=PATCH_SCHEMA,
    handler=_handle_patch,
    check_fn=_check_file_reqs,
    emoji="🔧",
)
registry.register(
    name="search_files",
    toolset="file",
    schema=SEARCH_FILES_SCHEMA,
    handler=_handle_search_files,
    check_fn=_check_file_reqs,
    emoji="🔎",
)
