"""Remove legacy project artifacts written by scratch-only research jobs.

The production dispatcher now gives scratch-only delegates a read/check-only
toolset, but older campaigns may retain project-root Lean files and shared
``apply_verified_patch`` metadata.  This migration deletes only artifacts with
deterministic dispatch, activity, checkpoint, filesystem, and Git provenance.
Ambiguous files are preserved.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.dispatch_models import SCRATCH_ISOLATION_VERSION
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file

MIGRATION_KEY = "scratch_artifact_cleanup_v1"
_TERMINAL_JOB_STATES = frozenset({"done", "failed", "stuck", "killed"})
_OLD_AXIOM_HARNESS_RE = re.compile(r"tmp[a-z0-9_]{8}\.lean\Z")
_ADD_FILE_RE = re.compile(r"^\*\*\* Add File:\s*(.+?)\s*$", re.MULTILINE)
_AXIOM_QUERY_RE = re.compile(r"^\s*#print\s+axioms\s+(\S+)\s*$")
_FILE_TIME_TOLERANCE = timedelta(seconds=3)
_MAX_HARNESS_BYTES = 16 * 1024 * 1024
_PREFIX_LINES = 128
_MIN_PREFIX_LINES = 20


@dataclass(frozen=True)
class _ScratchJob:
    """Hold the dispatch provenance needed by the migration."""

    job_id: str
    process_id: int
    started_at: datetime
    finished_at: datetime
    active_file: Path | None


@dataclass(frozen=True)
class _PatchCandidate:
    """Associate an add-file checkpoint with its resolved project artifact."""

    path: Path
    checkpoint_path: Path
    checkpoint_id: str
    checkpoint_created_at: datetime


@dataclass(frozen=True)
class _ToolEvent:
    """Represent one scratch-job tool event relevant to cleanup."""

    job: _ScratchJob
    timestamp: datetime
    arguments: dict[str, Any]


def _utc_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp and normalize it to UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _resolve_path(value: Any, *, root: Path, cwd: Any = "") -> Path | None:
    """Resolve one state-file path without requiring it to exist."""
    text = str(value or "").strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        base_text = str(cwd or "").strip()
        base = Path(base_text).expanduser() if base_text else root
        if not base.is_absolute():
            base = root / base
        candidate = base / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return None


def _inside_project_artifact(path: Path, *, root: Path) -> bool:
    """Return whether ``path`` is a non-state descendant of the project."""
    if path == root or not path.is_relative_to(root):
        return False
    state_root = root / ".leanflow"
    return path != state_root and not path.is_relative_to(state_root)


def _ledger_entries(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return legacy list- or mapping-shaped dispatch ledger entries."""
    raw = payload.get("dispatch_ledger")
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, Mapping)]
    if isinstance(raw, Mapping):
        return [entry for entry in raw.values() if isinstance(entry, Mapping)]
    return []


def _scratch_only_spec(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return a ledger spec only when it declares the scratch-only contract."""
    spec = raw.get("spec")
    if not isinstance(spec, Mapping):
        return None
    scope = spec.get("scope")
    if not isinstance(scope, Mapping) or scope.get("scratch_only") is not True:
        return None
    return spec


def _legacy_scratch_spec(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return scratch specs that predate process and toolset write isolation."""
    spec = _scratch_only_spec(raw)
    if spec is None:
        return None
    scope = spec.get("scope")
    assert isinstance(scope, Mapping)
    try:
        isolation_version = int(scope.get("isolation_version", 0) or 0)
    except (TypeError, ValueError):
        isolation_version = 0
    return spec if isolation_version < SCRATCH_ISOLATION_VERSION else None


def _unfinished_scratch_job_count(payload: Mapping[str, Any]) -> int:
    """Count scratch jobs whose terminal lifetime has not been persisted yet."""
    return sum(
        1
        for raw in _ledger_entries(payload)
        if _legacy_scratch_spec(raw) is not None
        and (
            str(raw.get("state", "") or "") not in _TERMINAL_JOB_STATES
            or _utc_datetime(raw.get("finished_at")) is None
        )
    )


def _scratch_jobs(payload: Mapping[str, Any], *, root: Path) -> list[_ScratchJob]:
    """Load terminal scratch-only jobs with a bounded PID time window."""
    jobs: list[_ScratchJob] = []
    for raw in _ledger_entries(payload):
        spec = _legacy_scratch_spec(raw)
        if spec is None:
            continue
        if str(raw.get("state", "") or "") not in _TERMINAL_JOB_STATES:
            continue
        try:
            process_id = int(raw.get("process_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        started_at = _utc_datetime(raw.get("started_at"))
        finished_at = _utc_datetime(raw.get("finished_at"))
        if process_id <= 0 or started_at is None or finished_at is None:
            continue
        inputs = spec.get("inputs")
        active_file = None
        if isinstance(inputs, Mapping):
            active_file = _resolve_path(inputs.get("active_file"), root=root)
        jobs.append(
            _ScratchJob(
                job_id=str(spec.get("job_id", "") or ""),
                process_id=process_id,
                started_at=started_at,
                finished_at=finished_at,
                active_file=active_file,
            )
        )
    return jobs


def _matching_job(
    jobs_by_pid: Mapping[int, list[_ScratchJob]], *, process_id: int, timestamp: datetime
) -> _ScratchJob | None:
    """Return the scratch job whose PID and lifetime contain an event."""
    for job in jobs_by_pid.get(process_id, []):
        if (
            job.started_at - _FILE_TIME_TOLERANCE
            <= timestamp
            <= (job.finished_at + _FILE_TIME_TOLERANCE)
        ):
            return job
    return None


def _checkpoint_add_target(payload: Mapping[str, Any], *, root: Path) -> Path | None:
    """Return the exact target of a zero-byte ``Add File`` checkpoint."""
    try:
        before_bytes = int(payload.get("before_bytes", -1))
    except (TypeError, ValueError):
        return None
    if before_bytes != 0:
        return None
    patch = str(payload.get("patch", "") or "")
    matches = _ADD_FILE_RE.findall(patch)
    if len(matches) != 1:
        return None
    checkpoint_target = _resolve_path(matches[0], root=root, cwd=payload.get("cwd"))
    recorded_target = _resolve_path(payload.get("file_path"), root=root, cwd=payload.get("cwd"))
    if checkpoint_target is None or checkpoint_target != recorded_target:
        return None
    return checkpoint_target


def _patch_candidates(state_root: Path, *, root: Path) -> tuple[list[_PatchCandidate], bool]:
    """Load add-file candidates and report whether every checkpoint was readable."""
    candidates_by_path: dict[Path, _PatchCandidate] = {}
    complete = True
    checkpoint_root = state_root / "verified-patch-checkpoints"
    if not checkpoint_root.is_dir():
        return [], complete
    try:
        checkpoint_paths = sorted(checkpoint_root.glob("*.json"))
    except OSError:
        return [], False
    for checkpoint_path in checkpoint_paths:
        try:
            payload = read_json_file(checkpoint_path)
        except (OSError, RuntimeError):
            complete = False
            continue
        target = _checkpoint_add_target(payload, root=root)
        created_at = _utc_datetime(payload.get("created_at"))
        if target is None or created_at is None or not _inside_project_artifact(target, root=root):
            continue
        candidate = _PatchCandidate(
            path=target,
            checkpoint_path=checkpoint_path,
            checkpoint_id=str(payload.get("checkpoint_id", "") or checkpoint_path.stem),
            checkpoint_created_at=created_at,
        )
        previous = candidates_by_path.get(target)
        if previous is None or candidate.checkpoint_created_at > previous.checkpoint_created_at:
            candidates_by_path[target] = candidate
    return list(candidates_by_path.values()), complete


def _applied_checkpoint_id(details: Mapping[str, Any]) -> str:
    """Return the checkpoint id when an apply-patch result confirms a write."""
    if details.get("is_error") is True:
        return ""
    raw_result = details.get("result")
    if isinstance(raw_result, Mapping):
        result = raw_result
    elif isinstance(raw_result, str):
        try:
            decoded = json.loads(raw_result)
        except json.JSONDecodeError:
            return ""
        if not isinstance(decoded, Mapping):
            return ""
        result = decoded
    else:
        return ""
    if result.get("patch_applied") is not True:
        return ""
    return str(result.get("checkpoint_id", "") or "").strip()


def _scan_activity(
    activity_root: Path,
    *,
    root: Path,
    jobs: list[_ScratchJob],
    candidates: list[_PatchCandidate],
) -> tuple[dict[Path, _ToolEvent], list[_ToolEvent], int, bool]:
    """Stream activity once for attributed add-file and old axiom-harness events."""
    jobs_by_pid: dict[int, list[_ScratchJob]] = {}
    for job in jobs:
        jobs_by_pid.setdefault(job.process_id, []).append(job)
    candidate_names = {candidate.path.name for candidate in candidates}
    candidates_by_path = {candidate.path: candidate for candidate in candidates}
    patch_events: dict[Path, _ToolEvent] = {}
    axiom_events: list[_ToolEvent] = []
    files_scanned = 0
    complete = True
    if not activity_root.is_dir():
        return patch_events, axiom_events, files_scanned, complete
    try:
        activity_paths = sorted(activity_root.glob("*.jsonl"))
    except OSError:
        return patch_events, axiom_events, files_scanned, False
    for activity_path in activity_paths:
        files_scanned += 1
        try:
            handle = activity_path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            complete = False
            continue
        with handle:
            try:
                for line in handle:
                    if '"type": "tool-' not in line:
                        continue
                    is_patch = '"tool": "apply_verified_patch"' in line
                    is_axiom = '"tool": "lean_axioms"' in line
                    if not is_patch and not is_axiom:
                        continue
                    if (
                        is_patch
                        and candidate_names
                        and not any(name in line for name in candidate_names)
                    ):
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    details = event.get("details")
                    timestamp = _utc_datetime(event.get("timestamp"))
                    if not isinstance(details, Mapping) or timestamp is None:
                        continue
                    try:
                        process_id = int(details.get("process_id", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    matched_job = _matching_job(
                        jobs_by_pid, process_id=process_id, timestamp=timestamp
                    )
                    arguments = details.get("arguments")
                    if matched_job is None or not isinstance(arguments, Mapping):
                        continue
                    tool = str(details.get("tool", "") or "")
                    event_type = str(event.get("type", "") or "")
                    if tool == "apply_verified_patch" and event_type == "tool-result":
                        path = _resolve_path(
                            arguments.get("path"), root=root, cwd=arguments.get("cwd")
                        )
                        patch = str(arguments.get("patch", "") or "")
                        applied_checkpoint_id = _applied_checkpoint_id(details)
                        candidate = candidates_by_path.get(path) if path is not None else None
                        if (
                            path is not None
                            and candidate is not None
                            and _ADD_FILE_RE.search(patch)
                            and applied_checkpoint_id == candidate.checkpoint_id
                        ):
                            patch_events[path] = _ToolEvent(
                                job=matched_job,
                                timestamp=timestamp,
                                arguments=dict(arguments),
                            )
                    elif tool == "lean_axioms" and event_type == "tool-call":
                        axiom_events.append(
                            _ToolEvent(
                                job=matched_job,
                                timestamp=timestamp,
                                arguments=dict(arguments),
                            )
                        )
            except OSError:
                complete = False
    return patch_events, axiom_events, files_scanned, complete


def _git_untracked(path: Path, *, root: Path) -> bool:
    """Return whether Git confirms that ``path`` is not in the index."""
    try:
        relative = path.relative_to(root)
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", str(relative)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return completed.returncode == 1


def _regular_untracked(path: Path, *, root: Path) -> os.stat_result | None:
    """Return lstat data only for a regular, non-symlinked, untracked file."""
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        return None
    if not _git_untracked(path, root=root):
        return None
    return file_stat


def _safe_patch_orphan(
    candidate: _PatchCandidate,
    event: _ToolEvent,
    *,
    root: Path,
    active_files: set[Path],
) -> bool:
    """Return whether a patch-created artifact is safe to unlink now."""
    if candidate.path in active_files:
        return False
    if not (
        event.job.started_at - _FILE_TIME_TOLERANCE
        <= candidate.checkpoint_created_at
        <= event.job.finished_at + _FILE_TIME_TOLERANCE
    ):
        return False
    if candidate.path.is_symlink():
        return False
    if not candidate.path.exists():
        return True
    file_stat = _regular_untracked(candidate.path, root=root)
    if file_stat is None:
        return False
    modified_at = datetime.fromtimestamp(file_stat.st_mtime, tz=UTC)
    return (
        event.timestamp - _FILE_TIME_TOLERANCE
        <= modified_at
        <= (event.job.finished_at + _FILE_TIME_TOLERANCE)
    )


def _matching_checkpoint_paths(
    checkpoint_root: Path, *, artifact: Path, root: Path
) -> tuple[list[Path], bool]:
    """Return every readable checkpoint that belongs to one artifact."""
    matches: list[Path] = []
    complete = True
    if not checkpoint_root.is_dir():
        return matches, complete
    try:
        paths = sorted(checkpoint_root.glob("*.json"))
    except OSError:
        return matches, False
    for path in paths:
        try:
            payload = read_json_file(path)
        except (OSError, RuntimeError):
            complete = False
            continue
        target = _resolve_path(payload.get("file_path"), root=root, cwd=payload.get("cwd"))
        if target == artifact:
            matches.append(path)
    return matches, complete


def _harness_matches_source(path: Path, event: _ToolEvent, *, root: Path) -> bool:
    """Match an old axiom harness to its tool target and source prefix."""
    source = _resolve_path(
        event.arguments.get("file_path"), root=root, cwd=event.arguments.get("cwd")
    )
    target = str(event.arguments.get("target", "") or "").strip()
    if source is None or not _inside_project_artifact(source, root=root) or not target:
        return False
    try:
        if path.stat().st_size > _MAX_HARNESS_BYTES:
            return False
        harness_lines = path.read_text(encoding="utf-8").splitlines()
        source_lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    queries: list[tuple[int, str]] = []
    for index, line in enumerate(harness_lines):
        matched = _AXIOM_QUERY_RE.fullmatch(line)
        if matched:
            queries.append((index, matched.group(1)))
    if len(queries) != 1:
        return False
    query_index, query_target = queries[0]
    if query_target not in {target, target.rsplit(".", 1)[-1]}:
        return False
    without_query = harness_lines[:query_index] + harness_lines[query_index + 1 :]
    prefix_length = min(_PREFIX_LINES, len(without_query), len(source_lines))
    if prefix_length < _MIN_PREFIX_LINES:
        return False
    return without_query[:prefix_length] == source_lines[:prefix_length]


def _old_axiom_harnesses(
    root: Path,
    *,
    events: list[_ToolEvent],
    active_files: set[Path],
) -> list[Path]:
    """Return exact legacy project-root axiom harnesses safe to delete."""
    matches: list[Path] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return matches
    for path in children:
        if not _OLD_AXIOM_HARNESS_RE.fullmatch(path.name) or path in active_files:
            continue
        file_stat = _regular_untracked(path, root=root)
        if file_stat is None:
            continue
        modified_at = datetime.fromtimestamp(file_stat.st_mtime, tz=UTC)
        for event in events:
            if abs(modified_at - event.timestamp) > _FILE_TIME_TOLERANCE:
                continue
            if _harness_matches_source(path, event, root=root):
                matches.append(path)
                break
    return matches


def _status_path(payload: Mapping[str, Any], *, root: Path) -> Path | None:
    """Resolve the artifact named by ``verified_patch_status.latest``."""
    latest = payload.get("latest")
    if not isinstance(latest, Mapping):
        return None
    return _resolve_path(latest.get("path"), root=root, cwd=latest.get("cwd"))


def _result_template(status: str) -> dict[str, Any]:
    """Build the stable migration result shape."""
    return {
        "status": status,
        "artifacts_removed": 0,
        "patch_artifacts_removed": 0,
        "axiom_harnesses_removed": 0,
        "checkpoints_removed": 0,
        "verified_patch_status_cleared": 0,
        "artifacts_preserved": 0,
        "activity_files_scanned": 0,
        "unfinished_scratch_jobs": 0,
        "removed_paths": [],
    }


def cleanup_legacy_scratch_artifacts(*, cwd: str | Path) -> dict[str, Any]:
    """Remove only legacy scratch artifacts with complete deterministic provenance."""
    root = Path(cwd).expanduser().resolve()
    state_root = root / ".leanflow" / "workflow-state"
    summary_path = state_root / "summary.json"
    if not summary_path.is_file():
        return _result_template("not_applicable")
    summary = read_json_file(summary_path)
    migrations = summary.get("maintenance_migrations")
    if isinstance(migrations, Mapping) and MIGRATION_KEY in migrations:
        return _result_template("already_completed")
    jobs = _scratch_jobs(summary, root=root)
    unfinished_jobs = _unfinished_scratch_job_count(summary)
    if not jobs:
        result = _result_template("deferred" if unfinished_jobs else "not_applicable")
        result["unfinished_scratch_jobs"] = unfinished_jobs
        return result

    result = _result_template("completed")
    result["unfinished_scratch_jobs"] = unfinished_jobs
    active_files = {job.active_file for job in jobs if job.active_file is not None}
    candidates, checkpoints_complete = _patch_candidates(state_root, root=root)
    patch_events, axiom_events, files_scanned, activity_complete = _scan_activity(
        state_root / "activity" / "agents",
        root=root,
        jobs=jobs,
        candidates=candidates,
    )
    result["activity_files_scanned"] = files_scanned
    cleanup_paths: set[Path] = set()
    patch_cleanup_paths: set[Path] = set()
    for candidate in candidates:
        event = patch_events.get(candidate.path)
        if event is None or not _safe_patch_orphan(
            candidate, event, root=root, active_files=active_files
        ):
            result["artifacts_preserved"] += 1
            continue
        cleanup_paths.add(candidate.path)
        patch_cleanup_paths.add(candidate.path)
    harness_paths = _old_axiom_harnesses(root, events=axiom_events, active_files=active_files)
    cleanup_paths.update(harness_paths)

    errors = False
    removed_or_absent: set[Path] = set()
    for path in sorted(cleanup_paths):
        try:
            if path.exists():
                path.unlink()
                result["artifacts_removed"] += 1
                result["removed_paths"].append(str(path))
            removed_or_absent.add(path)
        except OSError:
            errors = True
            result["artifacts_preserved"] += 1
    result["patch_artifacts_removed"] = sum(
        1 for path in patch_cleanup_paths if path in removed_or_absent
    )
    result["axiom_harnesses_removed"] = sum(
        1 for path in harness_paths if path in removed_or_absent
    )

    checkpoint_root = state_root / "verified-patch-checkpoints"
    metadata_paths = patch_cleanup_paths.intersection(removed_or_absent)
    for artifact in sorted(metadata_paths):
        matches, complete = _matching_checkpoint_paths(
            checkpoint_root, artifact=artifact, root=root
        )
        checkpoints_complete = checkpoints_complete and complete
        for checkpoint_path in matches:
            try:
                checkpoint_path.unlink()
                result["checkpoints_removed"] += 1
            except OSError:
                errors = True

    status_path = state_root / "verified_patch_status.json"
    if status_path.is_file() and metadata_paths:
        try:
            status = read_json_file(status_path)
            latest_path = _status_path(status, root=root)
            if latest_path in metadata_paths:

                def clear_latest(payload: dict[str, Any]) -> None:
                    payload.setdefault("version", 1)
                    payload["latest"] = {}

                update_json_file(status_path, clear_latest)
                result["verified_patch_status_cleared"] = 1
        except (OSError, RuntimeError):
            errors = True

    if errors or not checkpoints_complete or not activity_complete or unfinished_jobs:
        result["status"] = "incomplete"
        return result

    completed_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    def record_marker(payload: dict[str, Any]) -> None:
        raw_migrations = payload.get("maintenance_migrations")
        stored = dict(raw_migrations) if isinstance(raw_migrations, Mapping) else {}
        stored[MIGRATION_KEY] = {
            "completed_at": completed_at,
            "artifacts_removed": result["artifacts_removed"],
            "checkpoints_removed": result["checkpoints_removed"],
            "verified_patch_status_cleared": result["verified_patch_status_cleared"],
            "artifacts_preserved": result["artifacts_preserved"],
        }
        payload["maintenance_migrations"] = stored

    update_json_file(summary_path, record_marker)
    return result
