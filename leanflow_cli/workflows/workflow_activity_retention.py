"""Archive closed workflow activity while keeping status state hot and bounded.

Closed run and mirrored agent JSONL streams move to checksum-addressed gzip
evidence under ``activity/archive``.  A crash-atomic catalog retains one
replaceable evidence entry per run, while status summaries and recent tails
remain streamable in per-run JSONL shards. Interrupted migrations converge
without double-counting lifecycle or tool-call state. Normal status readers
consume only the small catalog, status shards, and uncompressed live streams;
gzip archives remain a cold operator/audit surface.
"""

from __future__ import annotations

import contextlib
import errno
import gzip
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from leanflow_cli.workflows.activity_preview import _shorten_text
from leanflow_cli.workflows.workflow_json_io import read_json_file, write_json_file

logger = logging.getLogger(__name__)

try:  # POSIX advisory locking; degrades to in-process-only elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX (Windows)
    fcntl = None  # type: ignore[assignment]

RETENTION_VERSION = 1
RETENTION_LAYOUT = "sharded-jsonl-v2"
DURABLE_AGENT_ACTIVITY_LIMIT = 64
DURABLE_RECENT_EVENT_LIMIT = 128
MAX_SUMMARY_RECORD_BYTES = 64 * 1024
# One retained summary can contain up to 64 source-record-derived activity
# previews. Keep the reader bounded without silently dropping a valid worst-case
# summary assembled from individually bounded source records.
MAX_AGENT_SUMMARY_BYTES = (DURABLE_AGENT_ACTIVITY_LIMIT + 1) * MAX_SUMMARY_RECORD_BYTES
MAX_AUDIT_CATALOG_BYTES = 64 * 1024 * 1024
MAX_AUDIT_RECORD_BYTES = 8 * 1024 * 1024
MAX_AUDIT_ISSUE_SAMPLES = 32
_COPY_CHUNK_BYTES = 64 * 1024
_RETENTION_LOCK = threading.Lock()

AgentSummaries = dict[str, dict[str, Any]]
ReduceEvent = Callable[
    [AgentSummaries, Mapping[str, Any], Mapping[str, Any], int],
    None,
]
CompactEvent = Callable[[Mapping[str, Any]], dict[str, Any]]
IdentityIsLive = Callable[[Mapping[str, Any]], bool]

_STATUS_TRANSITION_EVENTS = {
    "conversation-end",
    "agent-input-queued",
    "agent-resume",
    "agent-awaiting-input",
    "runner-exit",
}
_SUMMARY_METADATA_FIELDS = (
    "parent_agent_id",
    "_run_scope",
    "project_root",
    "task_label",
    "workflow_kind",
    "workflow_command",
    "active_skill",
    "model",
    "provider",
    "base_url",
)
_PROCESS_IDENTITY_FIELDS = (
    "process_group_id",
    "process_session_id",
    "process_token_sha256",
)
_CANONICAL_RUN_ID = re.compile(
    r"^[A-Za-z0-9_-]+-(?P<started>[0-9]{8}T[0-9]{6}Z)-pid(?P<pid>[1-9][0-9]*)$"
)
_MAX_CANONICAL_PID = 2_147_483_647


@dataclass(frozen=True)
class ActivityRetentionResult:
    """Describe one bounded retention pass for logging and tests."""

    archived_runs: tuple[str, ...] = ()
    archived_agent_streams: tuple[str, ...] = ()
    skipped_live_runs: tuple[str, ...] = ()
    skipped_unproven_runs: tuple[str, ...] = ()
    reclaimed_bytes: int = 0


@dataclass(frozen=True)
class RetainedRunAuditIssue:
    """Describe one sampled reason a strict retained-run audit is incomplete."""

    code: str
    run_id: str = ""
    path: str = ""
    detail: str = ""


@dataclass(frozen=True)
class RetainedRunAuditResult:
    """Report whether every cataloged retained run was verified and scanned."""

    complete: bool
    catalog_status: str
    catalog_runs: int = 0
    verified_runs: int = 0
    verified_events: int = 0
    matched_events: int = 0
    issue_counts: tuple[tuple[str, int], ...] = ()
    issue_samples: tuple[RetainedRunAuditIssue, ...] = ()

    def issue_count(self, code: str) -> int:
        """Return the number of audit failures recorded under ``code``."""
        return dict(self.issue_counts).get(str(code), 0)


@dataclass
class _RetainedRunAuditAccumulator:
    """Accumulate fixed-cardinality audit counters and bounded issue samples."""

    catalog_status: str = "verified"
    catalog_runs: int = 0
    verified_runs: int = 0
    verified_events: int = 0
    matched_events: int = 0
    issue_counts: dict[str, int] = field(default_factory=dict)
    issue_samples: list[RetainedRunAuditIssue] = field(default_factory=list)

    def add_issue(
        self,
        code: str,
        *,
        run_id: str = "",
        path: Path | None = None,
        detail: str = "",
    ) -> None:
        """Record one issue without retaining an unbounded per-run error list."""
        normalized = str(code or "audit_error")
        self.issue_counts[normalized] = self.issue_counts.get(normalized, 0) + 1
        if len(self.issue_samples) >= MAX_AUDIT_ISSUE_SAMPLES:
            return
        self.issue_samples.append(
            RetainedRunAuditIssue(
                code=normalized,
                run_id=str(run_id or ""),
                path=str(path or ""),
                detail=_shorten_text(detail, limit=300),
            )
        )

    def result(self) -> RetainedRunAuditResult:
        """Freeze the accumulated state into the public strict-audit result."""
        complete = bool(
            self.catalog_status == "verified"
            and not self.issue_counts
            and self.verified_runs == self.catalog_runs
        )
        return RetainedRunAuditResult(
            complete=complete,
            catalog_status=self.catalog_status,
            catalog_runs=self.catalog_runs,
            verified_runs=self.verified_runs,
            verified_events=self.verified_events,
            matched_events=self.matched_events,
            issue_counts=tuple(sorted(self.issue_counts.items())),
            issue_samples=tuple(self.issue_samples),
        )


@dataclass(frozen=True)
class _RetainedArchiveDescriptor:
    """Hold validated catalog identity for one immutable retained run archive."""

    run_id: str
    path: Path
    archive_sha256: str
    archive_bytes: int
    source_sha256: str
    source_bytes: int
    valid_events: int
    skipped_records: int
    hot_source: _RetainedHotSource | None


@dataclass(frozen=True)
class _RetainedCatalogSnapshot:
    """Pin the catalog file generation parsed by one strict audit."""

    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str


@dataclass(frozen=True)
class _RetainedHotSource:
    """Pin one cataloged hot-source identity for end-of-audit revalidation."""

    device: int
    inode: int
    size: int
    modified_ns: int
    sha256: str


@dataclass
class _ArchiveStats:
    """Carry streaming archive measurements and parsed status state."""

    temp_path: Path
    source_sha256: str
    source_bytes: int
    valid_events: int
    skipped_records: int
    agent_summaries: AgentSummaries
    recent_events: list[dict[str, Any]]
    identities: list[dict[str, Any]]
    saw_runner_exit: bool
    agent_stream_names: list[str]
    run_ids: list[str]
    source_mtime_ns: int


@dataclass
class _AgentFlags:
    """Retain merge semantics that a final per-run snapshot alone cannot express."""

    has_conversation_start: bool = False
    has_status_transition: bool = False
    delegate_depth_touched: bool = False
    process_touched: bool = False
    last_process_id: int = 0
    process_force_clear: bool = False
    process_identity_fields: set[str] | None = None

    def __post_init__(self) -> None:
        if self.process_identity_fields is None:
            self.process_identity_fields = set()


def activity_retention_catalog_path(state_root: Path) -> Path:
    """Return the crash-atomic historical activity catalog path."""
    return state_root / "activity" / "historical-summary.json"


def _run_archive_root(state_root: Path) -> Path:
    return state_root / "activity" / "archive" / "runs"


def _agent_archive_root(state_root: Path) -> Path:
    return state_root / "activity" / "archive" / "agents"


def _run_status_root(state_root: Path) -> Path:
    return state_root / "activity" / "historical-runs"


def _hot_run_root(state_root: Path) -> Path:
    return state_root / "activity" / "runs"


def _run_metadata_root(state_root: Path) -> Path:
    return state_root / "activity" / "run-metadata"


def _hot_agent_root(state_root: Path) -> Path:
    return state_root / "activity" / "agents"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _safe_component(value: str, *, fallback: str) -> str:
    safe = "".join(
        character
        for character in str(value or "").strip()
        if character.isalnum() or character in {"-", "_"}
    )
    return safe or fallback


def _empty_catalog() -> dict[str, Any]:
    return {
        "version": RETENTION_VERSION,
        "layout": RETENTION_LAYOUT,
        "updated_at": "",
        "runs": {},
        "agent_streams": {},
    }


def _load_catalog(state_root: Path) -> dict[str, Any]:
    payload = read_json_file(activity_retention_catalog_path(state_root))
    if not payload:
        return _empty_catalog()
    try:
        version = int(payload.get("version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    if version != RETENTION_VERSION:
        raise RuntimeError(
            "Unsupported workflow activity retention catalog version "
            f"{version!r} at {activity_retention_catalog_path(state_root)}"
        )
    runs = payload.get("runs")
    streams = payload.get("agent_streams")
    if not isinstance(runs, dict) or not isinstance(streams, dict):
        raise RuntimeError(
            "Malformed workflow activity retention catalog at "
            f"{activity_retention_catalog_path(state_root)}"
        )
    layout = str(payload.get("layout", "") or "")
    if layout not in {"", RETENTION_LAYOUT}:
        raise RuntimeError(
            "Unsupported workflow activity retention catalog layout "
            f"{layout!r} at {activity_retention_catalog_path(state_root)}"
        )
    return dict(payload)


def _load_catalog_for_status(state_root: Path) -> dict[str, Any]:
    """Return catalog state or fail open to live JSONLs for status views."""
    try:
        return _load_catalog(state_root)
    except Exception as exc:
        logger.warning(
            "Ignoring unreadable workflow activity retention catalog %s: %s",
            activity_retention_catalog_path(state_root),
            exc,
        )
        return _empty_catalog()


def _write_catalog(state_root: Path, catalog: dict[str, Any]) -> None:
    catalog["version"] = RETENTION_VERSION
    catalog["layout"] = RETENTION_LAYOUT
    catalog["updated_at"] = _utc_now()
    write_json_file(activity_retention_catalog_path(state_root), catalog)


def _load_catalog_for_audit(
    state_root: Path,
    audit: _RetainedRunAuditAccumulator,
) -> tuple[dict[str, Any], _RetainedCatalogSnapshot] | None:
    """Read one bounded catalog snapshot and classify every failure explicitly."""
    path = activity_retention_catalog_path(state_root)
    try:
        with _open_directory_chain(state_root, ("activity",)) as directory:
            with _open_regular_file_at(directory, path.name) as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                initial = os.fstat(handle.fileno())
                size = int(initial.st_size)
                if size > MAX_AUDIT_CATALOG_BYTES:
                    audit.catalog_status = "oversized"
                    audit.add_issue(
                        "catalog_oversized",
                        path=path,
                        detail=f"{size} bytes exceeds {MAX_AUDIT_CATALOG_BYTES}",
                    )
                    return None
                encoded = handle.read(MAX_AUDIT_CATALOG_BYTES + 1)
                final = os.fstat(handle.fileno())
    except FileNotFoundError:
        audit.catalog_status = "missing"
        audit.add_issue("catalog_missing", path=path)
        return None
    except OSError as exc:
        audit.catalog_status = "unreadable"
        audit.add_issue("catalog_unreadable", path=path, detail=str(exc))
        return None

    if not encoded.strip():
        audit.catalog_status = "malformed"
        audit.add_issue("catalog_malformed", path=path, detail="empty catalog")
        return None
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        audit.catalog_status = "malformed"
        audit.add_issue("catalog_malformed", path=path, detail=str(exc))
        return None
    if not isinstance(payload, dict):
        audit.catalog_status = "malformed"
        audit.add_issue(
            "catalog_malformed",
            path=path,
            detail=f"expected object, got {type(payload).__name__}",
        )
        return None
    if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns) != (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    ) or len(encoded) != size:
        audit.catalog_status = "unreadable"
        audit.add_issue(
            "catalog_changed",
            path=path,
            detail="catalog identity changed while being read",
        )
        return None
    try:
        version = int(payload.get("version", 0) or 0)
    except (TypeError, ValueError):
        version = 0
    layout = str(payload.get("layout", "") or "")
    runs = payload.get("runs")
    streams = payload.get("agent_streams")
    if version != RETENTION_VERSION or layout not in {"", RETENTION_LAYOUT}:
        audit.catalog_status = "malformed"
        audit.add_issue(
            "catalog_malformed",
            path=path,
            detail=f"unsupported version/layout {version!r}/{layout!r}",
        )
        return None
    if not isinstance(runs, dict) or not isinstance(streams, dict):
        audit.catalog_status = "malformed"
        audit.add_issue(
            "catalog_malformed",
            path=path,
            detail="runs and agent_streams must be objects",
        )
        return None
    snapshot = _RetainedCatalogSnapshot(
        device=int(final.st_dev),
        inode=int(final.st_ino),
        size=len(encoded),
        modified_ns=int(final.st_mtime_ns),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )
    return payload, snapshot


@contextlib.contextmanager
def _catalog_lock(state_root: Path) -> Iterator[None]:
    """Serialize migration and catalog replacement across startup processes."""
    lock_path = state_root / "activity" / ".retention.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _RETENTION_LOCK, lock_path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def _open_directory_chain(state_root: Path, parts: tuple[str, ...]) -> Iterator[int]:
    """Open a pinned directory chain while rejecting symlinked components."""
    root = state_root.expanduser().resolve(strict=True)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(root, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise NotADirectoryError(str(root))
        for component in parts:
            child_flags = flags
            if hasattr(os, "O_NOFOLLOW"):
                child_flags |= os.O_NOFOLLOW
            child = os.open(component, child_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise NotADirectoryError(component)
        yield descriptor
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


@contextlib.contextmanager
def _open_regular_file_at(directory: int, name: str) -> Iterator[BinaryIO]:
    """Open one nonblocking, non-symlink regular file relative to a pinned root."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=directory)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"retained evidence is not a regular file: {name}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            yield handle
    finally:
        with contextlib.suppress(OSError):
            os.close(descriptor)


@contextlib.contextmanager
def _open_retained_archive(state_root: Path, name: str) -> Iterator[BinaryIO]:
    """Open one archive through pinned, no-follow evidence directories."""
    with _open_directory_chain(state_root, ("activity", "archive", "runs")) as directory:
        with _open_regular_file_at(directory, name) as handle:
            yield handle


@contextlib.contextmanager
def _locked_source(path: Path) -> Iterator[BinaryIO]:
    """Hold the same advisory lock used by appenders while archiving a source."""
    with path.open("rb") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle


def _retention_fault(_stage: str, _path: Path) -> None:
    """Expose deterministic crash boundaries to tests without production behavior."""


def _status_shard_fault(_stage: str, _path: Path) -> None:
    """Expose deterministic status-read races to tests without production behavior."""


def _retained_audit_fault(_stage: str, _path: Path) -> None:
    """Expose deterministic cold-audit races to tests without production behavior."""


def _fsync_directory(path: Path) -> None:
    """Persist a completed rename when the platform supports directory fsync."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> tuple[str, int]:
    """Hash one file in bounded chunks and return its byte size."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _run_summary_path(state_root: Path, run_id: str) -> Path:
    safe_run_id = _safe_component(run_id, fallback="unknown")
    return _run_status_root(state_root) / f"{safe_run_id}.agents.jsonl"


def _run_recent_path(state_root: Path, run_id: str) -> Path:
    safe_run_id = _safe_component(run_id, fallback="unknown")
    return _run_status_root(state_root) / f"{safe_run_id}.recent.jsonl"


def _atomic_jsonl_write(
    path: Path,
    records: Iterator[Mapping[str, Any]],
) -> tuple[str, int]:
    """Write JSONL records crash-atomically while retaining only one record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.retention-",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for record in records:
                encoded = (
                    json.dumps(
                        dict(record),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )
                handle.write(encoded)
                digest.update(encoded)
                size += len(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    return digest.hexdigest(), size


def _write_run_status_shards(
    state_root: Path,
    run_id: str,
    *,
    agent_summaries: Mapping[str, Any],
    recent_events: list[Any],
) -> dict[str, Any]:
    """Persist one run's mergeable summaries and global tail as streaming shards."""
    summary_path = _run_summary_path(state_root, run_id)
    recent_path = _run_recent_path(state_root, run_id)
    summary_sha256, summary_bytes = _atomic_jsonl_write(
        summary_path,
        (
            agent_summaries[agent_id]
            for agent_id in sorted(agent_summaries)
            if isinstance(agent_summaries[agent_id], Mapping)
        ),
    )
    recent_sha256, recent_bytes = _atomic_jsonl_write(
        recent_path,
        (event for event in recent_events if isinstance(event, Mapping)),
    )
    return {
        "summary_path": _relative_path(summary_path, state_root),
        "summary_sha256": summary_sha256,
        "summary_bytes": summary_bytes,
        "summary_agents": len(agent_summaries),
        "recent_path": _relative_path(recent_path, state_root),
        "recent_sha256": recent_sha256,
        "recent_bytes": recent_bytes,
        "recent_event_count": len(recent_events),
    }


def _gzip_payload_sha256(path: Path) -> tuple[str, int]:
    """Hash the uncompressed evidence payload without materializing it."""
    digest = hashlib.sha256()
    size = 0
    try:
        with gzip.open(path, "rb") as handle:
            while True:
                chunk = handle.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except (OSError, EOFError):
        return "", 0
    return digest.hexdigest(), size


def _parse_record(record: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(record)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _copy_records(
    source: BinaryIO,
    destination: Any,
    *,
    on_event: Callable[[dict[str, Any]], None],
) -> tuple[str, int, int, int]:
    """Copy raw JSONL bytes and feed bounded valid records to ``on_event``."""
    digest = hashlib.sha256()
    source_bytes = 0
    valid_events = 0
    skipped_records = 0
    record = bytearray()
    oversized = False

    while True:
        chunk = source.readline(_COPY_CHUNK_BYTES)
        if not chunk:
            break
        destination.write(chunk)
        digest.update(chunk)
        source_bytes += len(chunk)
        if not oversized:
            if len(record) + len(chunk) <= MAX_SUMMARY_RECORD_BYTES:
                record.extend(chunk)
            else:
                record.clear()
                oversized = True
        if not chunk.endswith(b"\n"):
            continue
        if oversized:
            skipped_records += 1
        else:
            payload = _parse_record(bytes(record))
            if payload is None:
                skipped_records += 1
            else:
                on_event(payload)
                valid_events += 1
        record.clear()
        oversized = False

    if record or oversized:
        if oversized:
            skipped_records += 1
        else:
            payload = _parse_record(bytes(record))
            if payload is None:
                skipped_records += 1
            else:
                on_event(payload)
                valid_events += 1
    return digest.hexdigest(), source_bytes, valid_events, skipped_records


def _metadata_for_run(state_root: Path, run_id: str) -> dict[str, Any]:
    safe_run_id = _safe_component(run_id, fallback="unknown")
    return read_json_file(_run_metadata_root(state_root) / f"{safe_run_id}.json")


def _identity_key(payload: Mapping[str, Any]) -> tuple[int, int, int, str]:
    def integer(key: str) -> int:
        try:
            return int(payload.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    return (
        integer("process_id"),
        integer("process_group_id"),
        integer("process_session_id"),
        str(payload.get("process_token_sha256", "") or ""),
    )


def _record_identity(
    identities: dict[tuple[int, int, int, str], dict[str, Any]],
    payload: Mapping[str, Any],
) -> None:
    key = _identity_key(payload)
    if key[0] > 0:
        identities[key] = {
            "process_id": key[0],
            "process_group_id": key[1],
            "process_session_id": key[2],
            "process_token_sha256": key[3],
        }


def _canonical_filename_process_identity(source_path: Path) -> dict[str, Any]:
    """Return the one-sided liveness fallback encoded by a canonical run name.

    Legacy standalone telemetry did not persist an identity or ``runner-exit``.
    Its generated run name still records the writer PID. A dead PID proves that
    writer has gone away; a live or reused PID remains conservatively hot. Do
    not infer ownership from user-defined or malformed run names.
    """
    if source_path.suffix != ".jsonl":
        return {}
    match = _CANONICAL_RUN_ID.fullmatch(source_path.stem)
    if match is None:
        return {}
    try:
        datetime.strptime(match.group("started"), "%Y%m%dT%H%M%SZ")
        process_id = int(match.group("pid"))
    except (TypeError, ValueError):
        return {}
    if not 0 < process_id <= _MAX_CANONICAL_PID:
        return {}
    return {"process_id": process_id}


def _agent_stream_name(event: Mapping[str, Any]) -> str:
    details = event.get("details")
    details = details if isinstance(details, Mapping) else {}
    agent_id = str(details.get("agent_session_id", "") or event.get("agent_id", "") or "").strip()
    if not agent_id:
        return ""
    task_label = str(event.get("task_label", "") or "agent")
    safe_task = _safe_component(task_label, fallback="agent")
    safe_agent = _safe_component(agent_id, fallback="unknown")
    return f"{safe_task}-{safe_agent}.jsonl"


def _update_agent_flags(
    flags_by_agent: dict[str, _AgentFlags],
    event: Mapping[str, Any],
) -> None:
    details = event.get("details")
    if not isinstance(details, Mapping):
        return
    agent_id = str(details.get("agent_session_id", "") or "")
    if not agent_id:
        return
    flags = flags_by_agent.setdefault(agent_id, _AgentFlags())
    event_type = str(event.get("type", "") or "")
    if event_type == "conversation-start":
        flags.has_conversation_start = True
    if event_type in _STATUS_TRANSITION_EVENTS:
        flags.has_status_transition = True
    if "delegate_depth" in details:
        flags.delegate_depth_touched = True
    try:
        process_id = int(details.get("process_id", 0) or 0)
    except (TypeError, ValueError):
        process_id = 0
    if process_id <= 0:
        return
    flags.process_touched = True
    if flags.last_process_id > 0 and flags.last_process_id != process_id:
        flags.process_force_clear = True
        assert flags.process_identity_fields is not None
        flags.process_identity_fields.clear()
    flags.last_process_id = process_id
    assert flags.process_identity_fields is not None
    for key in _PROCESS_IDENTITY_FIELDS:
        if details.get(key) not in (None, ""):
            flags.process_identity_fields.add(key)


def _annotate_agent_summaries(
    summaries: AgentSummaries,
    flags_by_agent: Mapping[str, _AgentFlags],
) -> None:
    for agent_id, summary in summaries.items():
        flags = flags_by_agent.get(agent_id, _AgentFlags())
        summary["_retention_has_conversation_start"] = flags.has_conversation_start
        summary["_retention_has_status_transition"] = flags.has_status_transition
        summary["_retention_delegate_depth_touched"] = flags.delegate_depth_touched
        summary["_retention_process_touched"] = flags.process_touched
        summary["_retention_process_force_clear"] = flags.process_force_clear
        summary["_retention_process_identity_fields"] = sorted(flags.process_identity_fields or ())


def _archive_temp_path(archive_path: Path) -> tuple[int, Path]:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=str(archive_path.parent),
        prefix=f".{archive_path.name}.retention-",
        suffix=".tmp",
    )
    return descriptor, Path(name)


def _scan_run_to_archive(
    source_path: Path,
    archive_path: Path,
    *,
    source: BinaryIO,
    state_root: Path,
    reduce_event: ReduceEvent,
    compact_event: CompactEvent,
) -> _ArchiveStats:
    """Stream one run to a temporary gzip while deriving bounded status state."""
    summaries: AgentSummaries = {}
    flags_by_agent: dict[str, _AgentFlags] = {}
    recent_events: deque[dict[str, Any]] = deque(maxlen=DURABLE_RECENT_EVENT_LIMIT)
    identities: dict[tuple[int, int, int, str], dict[str, Any]] = {}
    agent_stream_names: set[str] = set()
    run_ids: set[str] = set()
    metadata_cache: dict[str, dict[str, Any]] = {}
    saw_runner_exit = False
    source_stat = source_path.stat()
    default_metadata = _metadata_for_run(state_root, source_path.stem)
    _record_identity(identities, default_metadata)
    descriptor, temp_path = _archive_temp_path(archive_path)

    def consume(event: dict[str, Any]) -> None:
        nonlocal saw_runner_exit
        details = event.get("details")
        if isinstance(details, Mapping):
            _record_identity(identities, details)
        run_id = str(event.get("run_id", "") or source_path.stem)
        run_ids.add(run_id)
        metadata = metadata_cache.get(run_id)
        if metadata is None:
            metadata = _metadata_for_run(state_root, run_id)
            metadata_cache[run_id] = metadata
            _record_identity(identities, metadata)
        reduce_event(summaries, event, metadata, DURABLE_AGENT_ACTIVITY_LIMIT)
        _update_agent_flags(flags_by_agent, event)
        compacted = compact_event(event)
        if compacted:
            recent_events.append(compacted)
        stream_name = _agent_stream_name(event)
        if stream_name:
            agent_stream_names.add(stream_name)
        if str(event.get("type", "") or "") == "runner-exit":
            saw_runner_exit = True

    try:
        with os.fdopen(descriptor, "wb") as raw_destination:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=6,
                fileobj=raw_destination,
                mtime=0,
            ) as destination:
                digest, size, events, skipped = _copy_records(
                    source,
                    destination,
                    on_event=consume,
                )
            raw_destination.flush()
            os.fsync(raw_destination.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    if not saw_runner_exit and not identities:
        _record_identity(
            identities,
            _canonical_filename_process_identity(source_path),
        )
    _annotate_agent_summaries(summaries, flags_by_agent)
    return _ArchiveStats(
        temp_path=temp_path,
        source_sha256=digest,
        source_bytes=size,
        valid_events=events,
        skipped_records=skipped,
        agent_summaries=summaries,
        recent_events=list(recent_events),
        identities=list(identities.values()),
        saw_runner_exit=saw_runner_exit,
        agent_stream_names=sorted(agent_stream_names),
        run_ids=sorted(run_ids),
        source_mtime_ns=int(source_stat.st_mtime_ns),
    )


def _scan_agent_to_archive(
    source_path: Path,
    archive_path: Path,
    *,
    source: BinaryIO,
) -> _ArchiveStats:
    """Stream one mirrored agent file while deriving only closure evidence."""
    identities: dict[tuple[int, int, int, str], dict[str, Any]] = {}
    run_ids: set[str] = set()
    source_stat = source_path.stat()
    descriptor, temp_path = _archive_temp_path(archive_path)

    def consume(event: dict[str, Any]) -> None:
        details = event.get("details")
        if isinstance(details, Mapping):
            _record_identity(identities, details)
        run_id = str(event.get("run_id", "") or "").strip()
        if run_id:
            run_ids.add(run_id)

    try:
        with os.fdopen(descriptor, "wb") as raw_destination:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=6,
                fileobj=raw_destination,
                mtime=0,
            ) as destination:
                digest, size, events, skipped = _copy_records(
                    source,
                    destination,
                    on_event=consume,
                )
            raw_destination.flush()
            os.fsync(raw_destination.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise
    return _ArchiveStats(
        temp_path=temp_path,
        source_sha256=digest,
        source_bytes=size,
        valid_events=events,
        skipped_records=skipped,
        agent_summaries={},
        recent_events=[],
        identities=list(identities.values()),
        saw_runner_exit=False,
        agent_stream_names=[],
        run_ids=sorted(run_ids),
        source_mtime_ns=int(source_stat.st_mtime_ns),
    )


def _commit_or_reuse_archive(stats: _ArchiveStats, archive_path: Path) -> tuple[str, int]:
    """Install a complete archive or reuse an exact orphan from a prior crash."""
    reuse_existing = False
    if archive_path.is_file():
        payload_sha256, payload_bytes = _gzip_payload_sha256(archive_path)
        reuse_existing = bool(
            payload_sha256 == stats.source_sha256 and payload_bytes == stats.source_bytes
        )
    if reuse_existing:
        with contextlib.suppress(OSError):
            stats.temp_path.unlink()
    else:
        os.replace(stats.temp_path, archive_path)
        _fsync_directory(archive_path.parent)
    return _sha256_file(archive_path)


def _entry_archive_path(state_root: Path, entry: Mapping[str, Any]) -> Path:
    relative = str(entry.get("archive_path", "") or "").strip()
    return state_root / relative if relative else Path()


def _catalog_entry_is_authoritative(
    state_root: Path,
    entry: Mapping[str, Any],
) -> bool:
    """Prefer a changed post-crash hot source over its stale catalog snapshot."""
    source_name = str(entry.get("source_name", "") or "").strip()
    if not source_name:
        return False
    source_path = _hot_run_root(state_root) / source_name
    try:
        stat = source_path.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        expected_bytes = int(entry.get("source_bytes", -1))
        expected_mtime_ns = int(entry.get("source_mtime_ns", -1))
    except (TypeError, ValueError):
        return False
    return bool(int(stat.st_size) == expected_bytes and int(stat.st_mtime_ns) == expected_mtime_ns)


def _entry_matches_source(
    state_root: Path,
    source_path: Path,
    entry: Mapping[str, Any],
) -> bool:
    """Verify both sides of a cataloged transaction before pending unlink."""
    archive_path = _entry_archive_path(state_root, entry)
    if not archive_path.is_file():
        return False
    try:
        source_sha256, source_bytes = _sha256_file(source_path)
        archive_sha256, archive_bytes = _sha256_file(archive_path)
    except OSError:
        return False
    matches = bool(
        source_sha256 == str(entry.get("source_sha256", "") or "")
        and source_bytes == int(entry.get("source_bytes", 0) or 0)
        and archive_sha256 == str(entry.get("archive_sha256", "") or "")
        and archive_bytes == int(entry.get("archive_bytes", 0) or 0)
    )
    if not matches:
        return False
    for prefix in ("summary", "recent"):
        relative = str(entry.get(f"{prefix}_path", "") or "").strip()
        if not relative:
            return False
        artifact = state_root / relative
        if not artifact.is_file():
            return False
        try:
            digest, size = _sha256_file(artifact)
        except OSError:
            return False
        if digest != str(entry.get(f"{prefix}_sha256", "") or "") or size != int(
            entry.get(f"{prefix}_bytes", 0) or 0
        ):
            return False
    return True


def _relative_path(path: Path, state_root: Path) -> str:
    try:
        return str(path.relative_to(state_root))
    except ValueError:  # pragma: no cover - defensive for injected test paths
        return str(path)


def _run_entry(
    stats: _ArchiveStats,
    archive_path: Path,
    *,
    state_root: Path,
    archive_sha256: str,
    archive_bytes: int,
    status_shards: Mapping[str, Any],
) -> dict[str, Any]:
    entry = {
        "source_name": f"{archive_path.name.removesuffix('.gz')}",
        "source_sha256": stats.source_sha256,
        "source_bytes": stats.source_bytes,
        "source_mtime_ns": stats.source_mtime_ns,
        "archive_path": _relative_path(archive_path, state_root),
        "archive_sha256": archive_sha256,
        "archive_bytes": archive_bytes,
        "valid_events": stats.valid_events,
        "skipped_records": stats.skipped_records,
        "run_ids": stats.run_ids,
        "agent_stream_names": stats.agent_stream_names,
        "archived_at": _utc_now(),
    }
    entry.update(dict(status_shards))
    return entry


def _upgrade_monolithic_catalog(state_root: Path, catalog: dict[str, Any]) -> None:
    """Move legacy nested summaries into shards before the next status read."""
    if str(catalog.get("layout", "") or "") == RETENTION_LAYOUT:
        return
    runs = catalog.get("runs")
    if not isinstance(runs, dict):
        raise RuntimeError("Workflow activity retention runs catalog is not an object")
    for run_id, raw_entry in list(runs.items()):
        if not isinstance(raw_entry, Mapping):
            continue
        entry = dict(raw_entry)
        raw_summaries = entry.get("agent_summaries")
        agent_summaries = raw_summaries if isinstance(raw_summaries, Mapping) else {}
        raw_recent = entry.get("recent_events")
        recent_events = raw_recent if isinstance(raw_recent, list) else []
        entry.update(
            _write_run_status_shards(
                state_root,
                str(run_id),
                agent_summaries=agent_summaries,
                recent_events=recent_events,
            )
        )
        entry.pop("agent_summaries", None)
        entry.pop("recent_events", None)
        entry.pop("recent_agent_events", None)
        runs[str(run_id)] = entry
    _write_catalog(state_root, catalog)


def _agent_entry(
    stats: _ArchiveStats,
    source_name: str,
    archive_path: Path,
    *,
    state_root: Path,
    archive_sha256: str,
    archive_bytes: int,
) -> dict[str, Any]:
    return {
        "source_name": source_name,
        "source_sha256": stats.source_sha256,
        "source_bytes": stats.source_bytes,
        "source_mtime_ns": stats.source_mtime_ns,
        "archive_path": _relative_path(archive_path, state_root),
        "archive_sha256": archive_sha256,
        "archive_bytes": archive_bytes,
        "valid_events": stats.valid_events,
        "skipped_records": stats.skipped_records,
        "run_ids": stats.run_ids,
        "archived_at": _utc_now(),
    }


def _scan_disposition(stats: _ArchiveStats, identity_is_live: IdentityIsLive) -> str:
    """Classify one scan as closed, live, or lacking closure evidence."""
    if not stats.identities:
        return "closed" if stats.saw_runner_exit else "unproven"
    if any(identity_is_live(identity) for identity in stats.identities):
        return "live"
    return "closed"


def _cleanup_orphan_temps(state_root: Path) -> None:
    for root in (
        _run_archive_root(state_root),
        _agent_archive_root(state_root),
        _run_status_root(state_root),
    ):
        if not root.is_dir():
            continue
        for path in root.glob("*.retention-*.tmp"):
            with contextlib.suppress(OSError):
                path.unlink()


def _archive_runs(
    state_root: Path,
    catalog: dict[str, Any],
    *,
    current_run_id: str,
    reduce_event: ReduceEvent,
    compact_event: CompactEvent,
    identity_is_live: IdentityIsLive,
) -> tuple[list[str], list[str], list[str], int]:
    runs = catalog.setdefault("runs", {})
    if not isinstance(runs, dict):
        raise RuntimeError("Workflow activity retention runs catalog is not an object")
    archived: list[str] = []
    skipped_live: list[str] = []
    skipped_unproven: list[str] = []
    reclaimed = 0
    hot_root = _hot_run_root(state_root)
    if not hot_root.is_dir():
        return archived, skipped_live, skipped_unproven, reclaimed

    for source_path in sorted(hot_root.glob("*.jsonl")):
        run_id = source_path.stem
        if run_id == current_run_id:
            continue
        with _locked_source(source_path) as source:
            existing = runs.get(run_id)
            if isinstance(existing, Mapping) and _entry_matches_source(
                state_root, source_path, existing
            ):
                source_bytes = int(existing.get("source_bytes", 0) or 0)
                source_path.unlink()
                reclaimed += source_bytes
                archived.append(run_id)
                continue

            archive_path = _run_archive_root(state_root) / f"{source_path.name}.gz"
            stats = _scan_run_to_archive(
                source_path,
                archive_path,
                source=source,
                state_root=state_root,
                reduce_event=reduce_event,
                compact_event=compact_event,
            )
            disposition = _scan_disposition(stats, identity_is_live)
            if disposition != "closed":
                with contextlib.suppress(OSError):
                    stats.temp_path.unlink()
                if disposition == "live":
                    skipped_live.append(run_id)
                else:
                    skipped_unproven.append(run_id)
                continue
            archive_sha256, archive_bytes = _commit_or_reuse_archive(stats, archive_path)
            _retention_fault("run-archive-committed", archive_path)
            status_shards = _write_run_status_shards(
                state_root,
                run_id,
                agent_summaries=stats.agent_summaries,
                recent_events=stats.recent_events,
            )
            runs[run_id] = _run_entry(
                stats,
                archive_path,
                state_root=state_root,
                archive_sha256=archive_sha256,
                archive_bytes=archive_bytes,
                status_shards=status_shards,
            )
            _write_catalog(state_root, catalog)
            _retention_fault("run-catalog-committed", source_path)
            source_path.unlink()
            reclaimed += stats.source_bytes
            archived.append(run_id)
    return archived, skipped_live, skipped_unproven, reclaimed


def _archive_agent_streams(
    state_root: Path,
    catalog: dict[str, Any],
    *,
    identity_is_live: IdentityIsLive,
) -> tuple[list[str], int]:
    runs = catalog.get("runs")
    run_entries = runs if isinstance(runs, Mapping) else {}
    compacted_run_ids = {str(run_id) for run_id in run_entries}
    candidate_names = {
        str(name)
        for entry in run_entries.values()
        if isinstance(entry, Mapping)
        for name in list(entry.get("agent_stream_names") or [])
        if str(name)
    }
    streams = catalog.setdefault("agent_streams", {})
    if not isinstance(streams, dict):
        raise RuntimeError("Workflow activity retention agent catalog is not an object")
    archived: list[str] = []
    reclaimed = 0
    hot_root = _hot_agent_root(state_root)
    if not hot_root.is_dir():
        return archived, reclaimed

    for source_path in sorted(hot_root.glob("*.jsonl")):
        source_name = source_path.name
        if source_name not in candidate_names and source_name not in streams:
            continue
        with _locked_source(source_path) as source:
            existing = streams.get(source_name)
            if isinstance(existing, Mapping) and _entry_matches_source(
                state_root, source_path, existing
            ):
                source_bytes = int(existing.get("source_bytes", 0) or 0)
                source_path.unlink()
                reclaimed += source_bytes
                archived.append(source_name)
                continue

            archive_path = _agent_archive_root(state_root) / f"{source_name}.gz"
            stats = _scan_agent_to_archive(
                source_path,
                archive_path,
                source=source,
            )
            eligible = bool(stats.run_ids) and set(stats.run_ids).issubset(compacted_run_ids)
            if eligible:
                eligible = not any(identity_is_live(identity) for identity in stats.identities)
            if not eligible:
                with contextlib.suppress(OSError):
                    stats.temp_path.unlink()
                continue
            archive_sha256, archive_bytes = _commit_or_reuse_archive(stats, archive_path)
            _retention_fault("agent-archive-committed", archive_path)
            streams[source_name] = _agent_entry(
                stats,
                source_name,
                archive_path,
                state_root=state_root,
                archive_sha256=archive_sha256,
                archive_bytes=archive_bytes,
            )
            _write_catalog(state_root, catalog)
            _retention_fault("agent-catalog-committed", source_path)
            source_path.unlink()
            reclaimed += stats.source_bytes
            archived.append(source_name)
    return archived, reclaimed


def compact_closed_activity(
    state_root: Path,
    *,
    current_run_id: str,
    reduce_event: ReduceEvent,
    compact_event: CompactEvent,
    identity_is_live: IdentityIsLive,
) -> ActivityRetentionResult:
    """Archive all provably closed non-current activity streams transactionally.

    Copy and summarize under source flocks, commit gzip evidence before its
    catalog entry, and remove a hot source only after the catalog is durable.
    Per-run summaries are replaceable, so a writer that appended after an
    interrupted transaction causes a checksum mismatch and a clean rebuild.
    """
    normalized_current = str(current_run_id or "").strip()
    if not normalized_current:
        return ActivityRetentionResult()
    state_root.mkdir(parents=True, exist_ok=True)
    with _catalog_lock(state_root):
        _cleanup_orphan_temps(state_root)
        catalog = _load_catalog(state_root)
        _upgrade_monolithic_catalog(state_root, catalog)
        archived_runs, skipped_live, skipped_unproven, reclaimed_runs = _archive_runs(
            state_root,
            catalog,
            current_run_id=normalized_current,
            reduce_event=reduce_event,
            compact_event=compact_event,
            identity_is_live=identity_is_live,
        )
        archived_agents, reclaimed_agents = _archive_agent_streams(
            state_root,
            catalog,
            identity_is_live=identity_is_live,
        )
    return ActivityRetentionResult(
        archived_runs=tuple(archived_runs),
        archived_agent_streams=tuple(archived_agents),
        skipped_live_runs=tuple(skipped_live),
        skipped_unproven_runs=tuple(skipped_unproven),
        reclaimed_bytes=reclaimed_runs + reclaimed_agents,
    )


def retained_source_names(state_root: Path) -> set[str]:
    """Return hot run names already represented by durable catalog entries."""
    catalog = _load_catalog_for_status(state_root)
    runs = catalog.get("runs")
    if not isinstance(runs, Mapping):
        return set()
    return {
        str(entry.get("source_name", "") or "")
        for entry in runs.values()
        if isinstance(entry, Mapping)
        and _catalog_entry_is_authoritative(state_root, entry)
        and str(entry.get("source_name", "") or "")
    }


def _valid_audit_sha256(value: Any) -> str:
    """Return one normalized SHA-256 digest or an empty invalid marker."""
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        return ""
    return normalized


def _audit_integer(entry: Mapping[str, Any], key: str) -> int | None:
    """Return one nonnegative catalog integer without coercing invalid evidence."""
    try:
        value = int(entry.get(key, -1))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _retained_hot_source_snapshot(
    state_root: Path,
    source_name: str,
) -> _RetainedHotSource | None:
    """Return one securely read hot-source identity, or ``None`` when absent."""
    try:
        with _open_directory_chain(state_root, ("activity", "runs")) as directory:
            with _open_regular_file_at(directory, source_name) as source:
                if fcntl is not None:
                    fcntl.flock(source.fileno(), fcntl.LOCK_SH)
                metadata = os.fstat(source.fileno())
                digest = hashlib.sha256()
                size = 0
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
    except FileNotFoundError:
        return None
    return _RetainedHotSource(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        size=size,
        modified_ns=int(metadata.st_mtime_ns),
        sha256=digest.hexdigest(),
    )


def _revalidate_retained_hot_source(
    state_root: Path,
    descriptor: _RetainedArchiveDescriptor,
    audit: _RetainedRunAuditAccumulator,
) -> None:
    """Fail a strict audit when its hot-source authority changed mid-scan."""
    source_name = f"{descriptor.run_id}.jsonl"
    try:
        current = _retained_hot_source_snapshot(state_root, source_name)
    except OSError as exc:
        audit.add_issue(
            "run_non_authoritative",
            run_id=descriptor.run_id,
            path=_hot_run_root(state_root) / source_name,
            detail=f"hot source could not be revalidated: {exc}",
        )
        return
    if current != descriptor.hot_source:
        audit.add_issue(
            "run_non_authoritative",
            run_id=descriptor.run_id,
            path=_hot_run_root(state_root) / source_name,
            detail="hot source identity changed during retained evidence audit",
        )


def _revalidate_audit_catalog(
    state_root: Path,
    expected: _RetainedCatalogSnapshot,
    audit: _RetainedRunAuditAccumulator,
) -> None:
    """Fail a strict audit when the parsed catalog generation was replaced."""
    path = activity_retention_catalog_path(state_root)
    try:
        with _open_directory_chain(state_root, ("activity",)) as directory:
            with _open_regular_file_at(directory, path.name) as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                metadata = os.fstat(handle.fileno())
                digest = hashlib.sha256()
                size = 0
                while chunk := handle.read(_COPY_CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
                final = os.fstat(handle.fileno())
    except OSError as exc:
        audit.add_issue("catalog_changed", path=path, detail=str(exc))
        return
    current = _RetainedCatalogSnapshot(
        device=int(final.st_dev),
        inode=int(final.st_ino),
        size=size,
        modified_ns=int(final.st_mtime_ns),
        sha256=digest.hexdigest(),
    )
    read_identity = (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )
    final_identity = (
        int(final.st_dev),
        int(final.st_ino),
        int(final.st_size),
        int(final.st_mtime_ns),
    )
    if read_identity != final_identity or size != int(final.st_size) or current != expected:
        audit.add_issue(
            "catalog_changed",
            path=path,
            detail="catalog identity changed during retained evidence audit",
        )


def _retained_archive_descriptor(
    state_root: Path,
    run_id: str,
    raw_entry: Any,
    audit: _RetainedRunAuditAccumulator,
) -> _RetainedArchiveDescriptor | None:
    """Validate one run entry, including its hot-source authority boundary."""
    if not isinstance(raw_entry, Mapping):
        audit.add_issue("run_entry_malformed", run_id=run_id, detail="entry is not an object")
        return None
    safe_run_id = _safe_component(run_id, fallback="")
    source_name = str(raw_entry.get("source_name", "") or "").strip()
    if not run_id or safe_run_id != run_id or source_name != f"{run_id}.jsonl":
        audit.add_issue(
            "run_entry_malformed",
            run_id=run_id,
            detail=f"unsafe or mismatched source_name {source_name!r}",
        )
        return None

    integer_keys = (
        "source_bytes",
        "source_mtime_ns",
        "archive_bytes",
        "valid_events",
        "skipped_records",
    )
    integers = {key: _audit_integer(raw_entry, key) for key in integer_keys}
    archive_sha256 = _valid_audit_sha256(raw_entry.get("archive_sha256"))
    source_sha256 = _valid_audit_sha256(raw_entry.get("source_sha256"))
    if any(value is None for value in integers.values()) or not archive_sha256 or not source_sha256:
        audit.add_issue(
            "run_entry_malformed",
            run_id=run_id,
            detail="invalid checksum or numeric identity metadata",
        )
        return None
    source_bytes = integers["source_bytes"]
    source_mtime_ns = integers["source_mtime_ns"]
    archive_bytes = integers["archive_bytes"]
    valid_events = integers["valid_events"]
    skipped_records = integers["skipped_records"]
    assert source_bytes is not None
    assert source_mtime_ns is not None
    assert archive_bytes is not None
    assert valid_events is not None
    assert skipped_records is not None

    relative_text = str(raw_entry.get("archive_path", "") or "").strip()
    relative = Path(relative_text)
    expected_parts = ("activity", "archive", "runs", f"{source_name}.gz")
    archive_path = state_root.joinpath(*expected_parts)
    if not relative_text or relative.is_absolute() or relative.parts != expected_parts:
        audit.add_issue(
            "archive_path_malformed",
            run_id=run_id,
            path=archive_path,
            detail=relative_text,
        )
        return None

    try:
        hot_source = _retained_hot_source_snapshot(state_root, source_name)
    except OSError as exc:
        audit.add_issue(
            "run_non_authoritative",
            run_id=run_id,
            path=_hot_run_root(state_root) / source_name,
            detail=f"hot source could not be verified: {exc}",
        )
        return None
    authoritative = bool(
        hot_source is None
        or (
            hot_source.size == source_bytes
            and hot_source.modified_ns == source_mtime_ns
            and hot_source.sha256 == source_sha256
        )
    )
    if not authoritative:
        audit.add_issue(
            "run_non_authoritative",
            run_id=run_id,
            path=_hot_run_root(state_root) / source_name,
            detail="hot source changed after the catalog transaction",
        )
        return None

    return _RetainedArchiveDescriptor(
        run_id=run_id,
        path=archive_path,
        archive_sha256=archive_sha256,
        archive_bytes=archive_bytes,
        source_sha256=source_sha256,
        source_bytes=source_bytes,
        valid_events=valid_events,
        skipped_records=skipped_records,
        hot_source=hot_source,
    )


def _discard_audit_record_tail(handle: Any) -> None:
    """Discard an oversized retained-run record in bounded chunks."""
    while True:
        chunk = handle.readline(_COPY_CHUNK_BYTES)
        if not chunk or chunk.endswith(b"\n"):
            return


def _audit_retained_archive(
    state_root: Path,
    descriptor: _RetainedArchiveDescriptor,
    *,
    selected_types: set[str],
    on_event: Callable[[dict[str, Any]], None] | None,
    audit: _RetainedRunAuditAccumulator,
) -> None:
    """Verify and replay one archive through the same locked open file identity."""
    issue_total_before = sum(audit.issue_counts.values())
    archive_name = f"{descriptor.run_id}.jsonl.gz"
    try:
        archive_context = _open_retained_archive(state_root, archive_name)
        raw = archive_context.__enter__()
    except FileNotFoundError:
        audit.add_issue(
            "archive_missing",
            run_id=descriptor.run_id,
            path=descriptor.path,
        )
        return
    except OSError as exc:
        issue_code = (
            "archive_path_malformed"
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}
            else "archive_unreadable"
        )
        audit.add_issue(
            issue_code,
            run_id=descriptor.run_id,
            path=descriptor.path,
            detail=str(exc),
        )
        return

    try:
        try:
            if fcntl is not None:
                fcntl.flock(raw.fileno(), fcntl.LOCK_SH)
            archive_digest = hashlib.sha256()
            archive_bytes = 0
            while chunk := raw.read(_COPY_CHUNK_BYTES):
                archive_digest.update(chunk)
                archive_bytes += len(chunk)
        except OSError as exc:
            audit.add_issue(
                "archive_unreadable",
                run_id=descriptor.run_id,
                path=descriptor.path,
                detail=str(exc),
            )
            return
        if (
            archive_bytes != descriptor.archive_bytes
            or archive_digest.hexdigest() != descriptor.archive_sha256
        ):
            audit.add_issue(
                "archive_checksum_mismatch",
                run_id=descriptor.run_id,
                path=descriptor.path,
                detail=f"cataloged {descriptor.archive_bytes} bytes; read {archive_bytes}",
            )
            return

        try:
            raw.seek(0)
            source_digest = hashlib.sha256()
            source_bytes = 0
            with gzip.GzipFile(fileobj=raw, mode="rb") as decoded:
                while chunk := decoded.read(_COPY_CHUNK_BYTES):
                    source_digest.update(chunk)
                    source_bytes += len(chunk)
        except (OSError, EOFError) as exc:
            audit.add_issue(
                "archive_malformed",
                run_id=descriptor.run_id,
                path=descriptor.path,
                detail=str(exc),
            )
            return
        if (
            source_bytes != descriptor.source_bytes
            or source_digest.hexdigest() != descriptor.source_sha256
        ):
            audit.add_issue(
                "source_checksum_mismatch",
                run_id=descriptor.run_id,
                path=descriptor.path,
                detail=f"cataloged {descriptor.source_bytes} bytes; decoded {source_bytes}",
            )
            return

        valid_events = 0
        catalog_valid_events = 0
        catalog_skipped_records = 0
        matching_events = 0
        try:
            raw.seek(0)
            with gzip.GzipFile(fileobj=raw, mode="rb") as decoded:
                while True:
                    record = decoded.readline(MAX_AUDIT_RECORD_BYTES + 1)
                    if not record:
                        break
                    if len(record) > MAX_AUDIT_RECORD_BYTES:
                        catalog_skipped_records += 1
                        audit.add_issue(
                            "record_oversized",
                            run_id=descriptor.run_id,
                            path=descriptor.path,
                        )
                        if not record.endswith(b"\n"):
                            _discard_audit_record_tail(decoded)
                        continue
                    payload = _parse_record(record)
                    if payload is None:
                        audit.add_issue(
                            "record_malformed",
                            run_id=descriptor.run_id,
                            path=descriptor.path,
                        )
                        catalog_skipped_records += 1
                        continue
                    valid_events += 1
                    if len(record) > MAX_SUMMARY_RECORD_BYTES:
                        catalog_skipped_records += 1
                    else:
                        catalog_valid_events += 1
                    if not selected_types or str(payload.get("type", "") or "") in selected_types:
                        matching_events += 1
        except (OSError, EOFError) as exc:
            audit.add_issue(
                "archive_malformed",
                run_id=descriptor.run_id,
                path=descriptor.path,
                detail=str(exc),
            )
            return
        if (
            catalog_valid_events != descriptor.valid_events
            or catalog_skipped_records != descriptor.skipped_records
        ):
            audit.add_issue(
                "record_count_mismatch",
                run_id=descriptor.run_id,
                path=descriptor.path,
                detail=(
                    f"valid/skipped catalog={descriptor.valid_events}/{descriptor.skipped_records} "
                    f"scan={catalog_valid_events}/{catalog_skipped_records}"
                ),
            )
        if sum(audit.issue_counts.values()) != issue_total_before:
            return

        _retained_audit_fault("archive-verified", descriptor.path)
        if on_event is not None:
            try:
                raw.seek(0)
                with gzip.GzipFile(fileobj=raw, mode="rb") as decoded:
                    while True:
                        record = decoded.readline(MAX_AUDIT_RECORD_BYTES + 1)
                        if not record:
                            break
                        payload = _parse_record(record)
                        if payload is None:
                            audit.add_issue(
                                "archive_malformed",
                                run_id=descriptor.run_id,
                                path=descriptor.path,
                                detail="verified record changed before replay",
                            )
                            return
                        if (
                            not selected_types
                            or str(payload.get("type", "") or "") in selected_types
                        ):
                            on_event(payload)
            except (OSError, EOFError) as exc:
                audit.add_issue(
                    "archive_malformed",
                    run_id=descriptor.run_id,
                    path=descriptor.path,
                    detail=f"verified archive could not be replayed: {exc}",
                )
                return
        audit.verified_runs += 1
        audit.verified_events += valid_events
        audit.matched_events += matching_events
    finally:
        archive_context.__exit__(None, None, None)


def audit_retained_run_events(
    state_root: Path,
    *,
    event_types: set[str] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> RetainedRunAuditResult:
    """Strictly audit all retained runs and optionally consume matching events.

    Unlike the compatibility iterator below, this surface reports missing,
    non-authoritative, malformed, oversized, and checksum-invalid evidence.
    Each callback is delayed until its archive is completely validated and is
    replayed through the same open inode. Callbacks are provisional across the
    catalog: callers must use candidates only when ``result.complete`` is true.
    The catalog, record buffers, counters, and issue samples are all bounded.
    """
    audit = _RetainedRunAuditAccumulator()
    loaded = _load_catalog_for_audit(state_root, audit)
    if loaded is None:
        return audit.result()
    catalog, catalog_snapshot = loaded
    runs = catalog["runs"]
    assert isinstance(runs, dict)
    audit.catalog_runs = len(runs)
    selected_types = {str(item) for item in (event_types or set()) if str(item)}
    seen_sources: set[str] = set()
    seen_archives: set[Path] = set()
    descriptors: list[_RetainedArchiveDescriptor] = []
    for raw_run_id, raw_entry in sorted(runs.items(), key=lambda item: str(item[0])):
        run_id = str(raw_run_id or "")
        descriptor = _retained_archive_descriptor(state_root, run_id, raw_entry, audit)
        if descriptor is None:
            continue
        source_name = f"{descriptor.run_id}.jsonl"
        if source_name in seen_sources or descriptor.path in seen_archives:
            audit.add_issue(
                "run_entry_malformed",
                run_id=run_id,
                path=descriptor.path,
                detail="duplicate retained-run source or archive identity",
            )
            continue
        seen_sources.add(source_name)
        seen_archives.add(descriptor.path)
        descriptors.append(descriptor)
        _audit_retained_archive(
            state_root,
            descriptor,
            selected_types=selected_types,
            on_event=on_event,
            audit=audit,
        )
    for descriptor in descriptors:
        _revalidate_retained_hot_source(state_root, descriptor, audit)
    _revalidate_audit_catalog(state_root, catalog_snapshot, audit)
    return audit.result()


def iter_retained_run_events(
    state_root: Path,
    *,
    event_types: set[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield cold run events only after verifying both cataloged checksums.

    This is an explicit audit/recovery surface, not a status-reader path.  It
    makes two bounded passes over each immutable gzip payload: the first
    verifies the compressed archive and its uncompressed source identity, and
    the second parses bounded JSONL records from the same open inode.  Unsafe
    paths, malformed catalogs, checksum mismatches, and oversized records fail
    closed by yielding no evidence from the affected archive.
    """
    try:
        catalog = _load_catalog(state_root)
    except Exception:
        return
    runs = catalog.get("runs")
    if not isinstance(runs, Mapping):
        return
    selected_types = {str(item) for item in (event_types or set()) if str(item)}
    archive_root = _run_archive_root(state_root).resolve()
    for raw_entry in runs.values():
        if not isinstance(raw_entry, Mapping):
            continue
        if not _catalog_entry_is_authoritative(state_root, raw_entry):
            continue
        relative = Path(str(raw_entry.get("archive_path", "") or ""))
        if not str(relative) or relative.is_absolute():
            continue
        try:
            archive_path = (state_root / relative).resolve()
            archive_path.relative_to(archive_root)
            expected_archive_bytes = int(raw_entry.get("archive_bytes", -1))
            expected_source_bytes = int(raw_entry.get("source_bytes", -1))
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if archive_path.suffixes[-2:] != [".jsonl", ".gz"]:
            continue
        expected_archive_sha256 = str(raw_entry.get("archive_sha256", "") or "")
        expected_source_sha256 = str(raw_entry.get("source_sha256", "") or "")
        try:
            with archive_path.open("rb") as raw:
                if fcntl is not None:
                    fcntl.flock(raw.fileno(), fcntl.LOCK_SH)
                archive_digest = hashlib.sha256()
                archive_bytes = 0
                while chunk := raw.read(_COPY_CHUNK_BYTES):
                    archive_digest.update(chunk)
                    archive_bytes += len(chunk)
                if (
                    archive_bytes != expected_archive_bytes
                    or archive_digest.hexdigest() != expected_archive_sha256
                ):
                    continue

                raw.seek(0)
                source_digest = hashlib.sha256()
                source_bytes = 0
                with gzip.GzipFile(fileobj=raw, mode="rb") as decoded:
                    while chunk := decoded.read(_COPY_CHUNK_BYTES):
                        source_digest.update(chunk)
                        source_bytes += len(chunk)
                if (
                    source_bytes != expected_source_bytes
                    or source_digest.hexdigest() != expected_source_sha256
                ):
                    continue

                raw.seek(0)
                with gzip.GzipFile(fileobj=raw, mode="rb") as decoded:
                    while True:
                        record = decoded.readline(MAX_SUMMARY_RECORD_BYTES + 1)
                        if not record:
                            break
                        if len(record) > MAX_SUMMARY_RECORD_BYTES:
                            while record and not record.endswith(b"\n"):
                                record = decoded.readline(_COPY_CHUNK_BYTES)
                            continue
                        payload = _parse_record(record)
                        if payload is not None and (
                            not selected_types
                            or str(payload.get("type", "") or "") in selected_types
                        ):
                            yield payload
        except (OSError, EOFError):
            continue


def _merge_summary(
    by_agent: AgentSummaries,
    segment: Mapping[str, Any],
    *,
    activity_limit: int,
) -> None:
    agent_id = str(segment.get("agent_id", "") or "")
    if not agent_id:
        return
    recent_limit = max(1, int(activity_limit))
    existing = by_agent.get(agent_id)
    if existing is None:
        copied = dict(segment)
        copied["_recent_activity"] = list(copied.get("_recent_activity") or [])[-recent_limit:]
        by_agent[agent_id] = copied
        return

    base_had_start = bool(str(existing.get("started_at", "") or ""))
    for key in _SUMMARY_METADATA_FIELDS:
        value = segment.get(key)
        if value not in (None, ""):
            existing[key] = value
    if bool(segment.get("_retention_delegate_depth_touched")):
        existing["delegate_depth"] = int(segment.get("delegate_depth", 0) or 0)

    if bool(segment.get("_retention_process_touched")):
        segment_pid = int(segment.get("process_id", 0) or 0)
        existing_pid = int(existing.get("process_id", 0) or 0)
        force_clear = bool(segment.get("_retention_process_force_clear"))
        if force_clear or segment_pid != existing_pid:
            for key in _PROCESS_IDENTITY_FIELDS:
                existing[key] = 0 if key != "process_token_sha256" else ""
        existing["process_id"] = segment_pid
        touched_fields = {
            str(key) for key in list(segment.get("_retention_process_identity_fields") or [])
        }
        for key in _PROCESS_IDENTITY_FIELDS:
            if key in touched_fields:
                existing[key] = segment.get(key, "" if key == "process_token_sha256" else 0)

    segment_start = str(segment.get("started_at", "") or "")
    if not base_had_start and segment_start:
        existing["started_at"] = segment_start
    has_nonstart_transition = bool(segment.get("_retention_has_status_transition"))
    has_start = bool(segment.get("_retention_has_conversation_start"))
    if has_nonstart_transition or (has_start and not base_had_start):
        existing["status"] = str(segment.get("status", "") or existing.get("status", ""))
        existing["finished_at"] = str(segment.get("finished_at", "") or "")

    try:
        existing["api_calls"] = max(
            int(existing.get("api_calls", 0) or 0),
            int(segment.get("api_calls", 0) or 0),
        )
    except (TypeError, ValueError):
        pass
    try:
        existing["tool_calls"] = int(existing.get("tool_calls", 0) or 0) + int(
            segment.get("tool_calls", 0) or 0
        )
    except (TypeError, ValueError):
        pass
    for key in ("last_event_type", "last_event_at", "last_message"):
        value = segment.get(key)
        if value not in (None, ""):
            existing[key] = value
    recent = list(existing.get("_recent_activity") or [])
    recent.extend(list(segment.get("_recent_activity") or []))
    existing["_recent_activity"] = recent[-recent_limit:]


def _authoritative_run_entries(
    state_root: Path,
    catalog: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Return cataloged runs whose hot source has not changed after commit."""
    runs = catalog.get("runs")
    if not isinstance(runs, Mapping):
        return []
    return sorted(
        (
            entry
            for entry in runs.values()
            if isinstance(entry, Mapping) and _catalog_entry_is_authoritative(state_root, entry)
        ),
        key=lambda entry: str(entry.get("source_name", "") or ""),
    )


def _status_shard_descriptor(
    state_root: Path,
    entry: Mapping[str, Any],
    *,
    prefix: str,
) -> tuple[Path, str, int] | None:
    """Return confined shard identity metadata without opening the payload."""
    relative_text = str(entry.get(f"{prefix}_path", "") or "").strip()
    relative = Path(relative_text)
    if not relative_text or relative.is_absolute():
        return None
    suffix = "agents" if prefix == "summary" else "recent"
    try:
        root = _run_status_root(state_root).resolve()
        path = (state_root / relative).resolve()
        path.relative_to(root)
        expected_bytes = int(entry.get(f"{prefix}_bytes", -1))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not path.name.endswith(f".{suffix}.jsonl") or expected_bytes < 0:
        return None
    expected_sha256 = str(entry.get(f"{prefix}_sha256", "") or "")
    if not expected_sha256:
        return None
    return path, expected_sha256, expected_bytes


def _discard_status_record_tail(handle: BinaryIO) -> None:
    """Discard one oversized status record without accumulating its tail."""
    while True:
        chunk = handle.readline(_COPY_CHUNK_BYTES)
        if not chunk or chunk.endswith(b"\n"):
            return


def _iter_verified_status_records(
    state_root: Path,
    entry: Mapping[str, Any],
    *,
    prefix: str,
    max_record_bytes: int = MAX_SUMMARY_RECORD_BYTES,
) -> Iterator[dict[str, Any]]:
    """Verify and parse one status shard through the same open file identity.

    Retention replaces stable shard paths atomically. Holding one descriptor
    across checksum verification and parsing prevents a concurrent replacement
    from mixing an old catalog entry with unverified new shard contents.
    """
    descriptor = _status_shard_descriptor(state_root, entry, prefix=prefix)
    if descriptor is None:
        return
    path, expected_sha256, expected_bytes = descriptor
    record_limit = max(1, int(max_record_bytes))
    try:
        with path.open("rb") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            digest = hashlib.sha256()
            actual_bytes = 0
            while chunk := handle.read(_COPY_CHUNK_BYTES):
                digest.update(chunk)
                actual_bytes += len(chunk)
            if actual_bytes != expected_bytes or digest.hexdigest() != expected_sha256:
                logger.warning("Ignoring corrupt workflow activity status shard %s", path)
                return

            _status_shard_fault("verified", path)
            handle.seek(0)
            while True:
                record = handle.readline(record_limit + 1)
                if not record:
                    break
                if len(record) > record_limit:
                    if not record.endswith(b"\n"):
                        _discard_status_record_tail(handle)
                    continue
                try:
                    payload = json.loads(record)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    yield payload
    except OSError:
        return


def _iter_sharded_summaries(
    state_root: Path,
    entries: list[Mapping[str, Any]],
) -> Iterator[tuple[Mapping[str, Any], dict[str, Any]]]:
    """Yield one verified retained agent summary at a time."""
    for entry in entries:
        for summary in _iter_verified_status_records(
            state_root,
            entry,
            prefix="summary",
            max_record_bytes=MAX_AGENT_SUMMARY_BYTES,
        ):
            yield entry, summary


def _entry_run_id(entry: Mapping[str, Any]) -> str:
    """Return the primary run identifier represented by one catalog entry."""
    run_ids = entry.get("run_ids")
    if isinstance(run_ids, list):
        for run_id in run_ids:
            normalized = str(run_id or "").strip()
            if normalized:
                return normalized
    return str(entry.get("source_name", "") or "").removesuffix(".jsonl")


def _summary_activity_event(
    entry: Mapping[str, Any],
    summary: Mapping[str, Any],
    activity: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild a compact agent-tail event from its retained summary activity."""
    agent_id = str(summary.get("agent_id", "") or "")
    return {
        "timestamp": str(activity.get("timestamp", "") or ""),
        "type": str(activity.get("type", "") or ""),
        "run_id": _entry_run_id(entry),
        "agent_id": agent_id,
        "task_label": str(summary.get("task_label", "") or ""),
        "run_scope": str(summary.get("_run_scope", "") or ""),
        "message": _shorten_text(activity.get("message", ""), limit=500),
        "details": {
            "agent_session_id": agent_id,
            "archived_preview": str(activity.get("preview", "") or ""),
        },
    }


def load_retained_agent_summaries(
    state_root: Path,
    *,
    activity_limit: int,
) -> AgentSummaries:
    """Stream merged archived-run summaries without opening cold evidence."""
    catalog = _load_catalog_for_status(state_root)
    entries = _authoritative_run_entries(state_root, catalog)
    by_agent: AgentSummaries = {}
    if str(catalog.get("layout", "") or "") == RETENTION_LAYOUT:
        for _entry, summary in _iter_sharded_summaries(state_root, entries):
            _merge_summary(by_agent, summary, activity_limit=activity_limit)
    else:
        for entry in entries:
            summaries = entry.get("agent_summaries")
            if not isinstance(summaries, Mapping):
                continue
            for summary in summaries.values():
                if isinstance(summary, Mapping):
                    _merge_summary(by_agent, summary, activity_limit=activity_limit)
    for summary in by_agent.values():
        for key in tuple(summary):
            if key.startswith("_retention_"):
                summary.pop(key, None)
    return by_agent


def load_retained_recent_events(
    state_root: Path,
    *,
    limit: int,
    agent_id: str | None = None,
    event_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Stream a bounded recent tail from retained status shards only."""
    selected_limit = max(1, int(limit))
    catalog = _load_catalog_for_status(state_root)
    entries = _authoritative_run_entries(state_root, catalog)
    retained: deque[dict[str, Any]] = deque(maxlen=selected_limit)
    sharded = str(catalog.get("layout", "") or "") == RETENTION_LAYOUT
    if sharded and agent_id:
        for entry, summary in _iter_sharded_summaries(state_root, entries):
            if str(summary.get("agent_id", "") or "") != agent_id:
                continue
            activity_items = summary.get("_recent_activity")
            if not isinstance(activity_items, list):
                continue
            for activity in activity_items:
                if not isinstance(activity, Mapping):
                    continue
                if event_types and str(activity.get("type", "") or "") not in event_types:
                    continue
                retained.append(_summary_activity_event(entry, summary, activity))
        return list(retained)

    if sharded:
        for entry in entries:
            for event in _iter_verified_status_records(
                state_root,
                entry,
                prefix="recent",
            ):
                if event_types and str(event.get("type", "") or "") not in event_types:
                    continue
                retained.append(event)
        return list(retained)

    for entry in entries:
        if agent_id:
            by_agent = entry.get("recent_agent_events")
            values = by_agent.get(agent_id, []) if isinstance(by_agent, Mapping) else []
            if not values:
                summaries = entry.get("agent_summaries")
                legacy_summary = summaries.get(agent_id) if isinstance(summaries, Mapping) else None
                activity_items = (
                    legacy_summary.get("_recent_activity")
                    if isinstance(legacy_summary, Mapping)
                    else []
                )
                values = (
                    [
                        _summary_activity_event(entry, legacy_summary, activity)
                        for activity in activity_items
                        if isinstance(activity, Mapping)
                    ]
                    if isinstance(activity_items, list) and isinstance(legacy_summary, Mapping)
                    else []
                )
        else:
            values = entry.get("recent_events") or []
        if not isinstance(values, list):
            continue
        for event in values:
            if not isinstance(event, dict):
                continue
            if event_types and str(event.get("type", "") or "") not in event_types:
                continue
            retained.append(dict(event))
    return list(retained)
