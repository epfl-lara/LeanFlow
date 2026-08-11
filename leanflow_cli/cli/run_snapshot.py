"""Seal immutable launch and final run-result artifacts.

This leaf owns artifact paths, write-once JSON creation, corruption-detection
digests, declaration validation, and post-quiescence final snapshot assembly.
Provenance collectors are injected by the stable run_metrics facade.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.cli import run_stream as _run_stream
from leanflow_cli.workflows.plan_state import NODE_STATUSES

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,191}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CLOSED_STATUSES = frozenset({"proved", "false", "split"})
_PROCESS_LAUNCH_COLLISIONS: set[tuple[str, str]] = set()

_read_hot_stream = _run_stream._read_hot_stream
_single_run_lifecycle = _run_stream._single_run_lifecycle
_events_sha256 = _run_stream._events_sha256
_aggregate_usage = _run_stream._aggregate_usage
_run_log_path = _run_stream._run_log_path
_journal_rejections = _run_stream._journal_rejections


def validate_run_id(value: str, *, allow_empty: bool = False) -> str:
    """Return one exact safe run id or raise without normalizing it."""
    run_id = str(value or "").strip()
    if not run_id and allow_empty:
        return ""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run id must contain only letters, digits, '-' and '_' (maximum 192 characters)"
        )
    return run_id


def run_result_snapshot_path(state_root: Path, run_id: str) -> Path:
    """Return the immutable final-result path for ``run_id``."""
    return state_root / "activity" / "run-results" / f"{validate_run_id(run_id)}.json"


def run_result_integrity_path(state_root: Path, run_id: str) -> Path:
    """Return the independently sealed final-result digest path for ``run_id``."""
    return state_root / "activity" / "run-result-integrity" / f"{validate_run_id(run_id)}.json"


def run_launch_snapshot_path(state_root: Path, run_id: str) -> Path:
    """Return the immutable launch-provenance path for ``run_id``."""
    return state_root / "activity" / "run-launch" / f"{validate_run_id(run_id)}.json"


def run_launch_collision_path(state_root: Path, run_id: str) -> Path:
    """Return the permanent evidence-collision marker for ``run_id``."""
    return state_root / "activity" / "run-launch-collisions" / f"{validate_run_id(run_id)}.json"


def _launch_collision_key(state_root: Path, run_id: str) -> tuple[str, str]:
    """Return the process-local fallback identity for a collision marker."""
    return str(state_root.expanduser().resolve()), validate_run_id(run_id)


def _parse_count(raw: Any) -> int | None:
    try:
        return int(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_exit_code(raw: Any) -> int | None:
    """Return an integer exit code without interpreting missing data as zero."""
    if raw is None or isinstance(raw, bool):
        return None
    return _parse_count(raw)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> bool:
    """Create one immutable JSON artifact without replacing prior evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n").encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return True


def _snapshot_sha256(payload: Mapping[str, Any]) -> str:
    """Return the canonical digest used to detect final-snapshot corruption."""
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return _sha256_bytes(encoded)


def _harden_snapshot_read_only(path: Path) -> None:
    """Remove write bits from a sealed artifact when the filesystem supports it."""
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):
        path.chmod(0o444)


def read_integrity_checked_run_result(state_root: Path, run_id: str) -> tuple[dict[str, Any], str]:
    """Return one run result only when its separately sealed digest matches.

    This detects accidental edits and storage corruption. It is not a signature
    and therefore does not claim protection from a hostile filesystem owner who
    can rewrite both the payload and its digest.
    """
    normalized = validate_run_id(run_id)
    snapshot = _read_json(run_result_snapshot_path(state_root, normalized))
    if not snapshot:
        return {}, "final-snapshot-missing"
    if str(snapshot.get("run_id", "") or "") != normalized:
        return {}, "final-snapshot-run-id-mismatch"
    integrity = _read_json(run_result_integrity_path(state_root, normalized))
    if not integrity:
        return {}, "final-snapshot-integrity-missing"
    if (
        _parse_count(integrity.get("version")) != 1
        or str(integrity.get("run_id", "") or "") != normalized
        or str(integrity.get("algorithm", "") or "") != "sha256"
    ):
        return {}, "final-snapshot-integrity-invalid"
    expected = str(integrity.get("payload_sha256", "") or "")
    if not _SHA256_RE.fullmatch(expected) or expected != _snapshot_sha256(snapshot):
        return {}, "final-snapshot-integrity-mismatch"
    return snapshot, "integrity-verified"


def capture_run_launch_snapshot(
    state_root: Path,
    *,
    run_id: str,
    project_root: Path,
    context: Mapping[str, Any] | None = None,
    launch_environment_collector: Callable[..., dict[str, Any]],
    provenance_collector: Callable[[Path], dict[str, Any]],
) -> bool:
    """Seal launch-time source and runtime provenance once for one run."""
    from leanflow_cli.workflow import _redacted_env_value

    normalized = validate_run_id(run_id)
    launch_environment = launch_environment_collector(project_root=project_root)
    safe_context = {
        str(key): (
            _redacted_env_value(f"LEANFLOW_LAUNCH_{key}", value)
            if isinstance(value, str)
            else value
        )
        for key, value in dict(context or {}).items()
    }
    payload = {
        "version": 1,
        "run_id": normalized,
        "captured_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "context": safe_context,
        "environment": launch_environment,
        "provenance": provenance_collector(project_root),
    }
    created = _write_json_exclusive(run_launch_snapshot_path(state_root, normalized), payload)
    if created:
        return True
    _PROCESS_LAUNCH_COLLISIONS.add(_launch_collision_key(state_root, normalized))
    _write_json_exclusive(
        run_launch_collision_path(state_root, normalized),
        {
            "version": 1,
            "run_id": normalized,
            "observed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "reason": "launch snapshot already existed for this run id",
        },
    )
    return False


def _declaration_rows(state_root: Path) -> list[dict[str, Any]]:
    """Snapshot per-declaration outcomes from the final dependency graph."""
    blueprint_path = state_root / "blueprint.json"
    blueprint = _read_json(blueprint_path)
    if blueprint_path.is_file() and not blueprint:
        raise RuntimeError("cannot seal an unreadable or invalid blueprint")
    raw_nodes = blueprint.get("nodes")
    if isinstance(raw_nodes, Mapping):
        nodes = [(str(key or ""), value) for key, value in raw_nodes.items()]
    elif isinstance(raw_nodes, list):
        nodes = [("", value) for value in raw_nodes]
    elif raw_nodes is not None:
        raise RuntimeError("cannot seal a blueprint whose nodes are not a collection")
    else:
        return []
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_declarations: set[tuple[str, str]] = set()
    for mapping_id, node in nodes:
        if not isinstance(node, Mapping):
            raise RuntimeError("cannot seal a blueprint containing a non-object node")
        node_id = str(node.get("id", "") or mapping_id).strip()
        name = str(node.get("name", "") or "").strip()
        file = str(node.get("file", "") or "").strip()
        status = str(node.get("status", "") or "")
        attempts = _parse_count(node.get("attempts", 0))
        api_steps = _parse_count(node.get("api_steps", 0))
        if not node_id or not name or not file:
            raise RuntimeError("cannot seal a declaration without nonempty id, file, and name")
        if node_id in seen_ids or (file, name) in seen_declarations:
            raise RuntimeError("cannot seal duplicate declaration identity")
        if status not in NODE_STATUSES:
            raise RuntimeError(
                f"cannot seal declaration {node_id!r} with invalid status {status!r}"
            )
        if attempts is None or attempts < 0 or api_steps is None or api_steps < 0:
            raise RuntimeError(f"cannot seal declaration {node_id!r} with invalid effort counters")
        seen_ids.add(node_id)
        seen_declarations.add((file, name))
        rows.append(
            {
                "id": node_id,
                "name": name,
                "file": file,
                "kind": str(node.get("kind", "") or ""),
                "status": status,
                "closed": status in _CLOSED_STATUSES,
                "proved": status == "proved",
                "attempts": attempts,
                "api_steps": api_steps,
                "notes": str(node.get("notes", "") or ""),
            }
        )
    rows.sort(key=lambda row: (row["file"], row["name"]))
    return rows


def finalize_run_snapshot(
    state_root: Path,
    *,
    run_id: str,
    project_root: Path,
    outcome: Mapping[str, Any],
    provenance_collector: Callable[[Path], dict[str, Any]],
) -> bool:
    """Seal final graph, outcome, usage, and provenance after quiescence."""
    normalized = validate_run_id(run_id)
    stream_path = state_root / "activity" / "runs" / f"{normalized}.jsonl"
    stream = _read_hot_stream(stream_path, normalized)
    if (
        not stream.integrity_complete
        or not stream.events
        or str(stream.events[-1].get("type", "") or "") != "runner-exit"
    ):
        raise RuntimeError("cannot seal run result before a complete runner-exit stream exists")
    if not _single_run_lifecycle(stream.events):
        raise RuntimeError("cannot seal a stream containing multiple run lifecycles")
    terminal_details = stream.events[-1].get("details")
    terminal_details = terminal_details if isinstance(terminal_details, Mapping) else {}
    terminal_exit_code = _parse_exit_code(terminal_details.get("exit_code"))
    outcome_exit_code = _parse_exit_code(outcome.get("exit_code"))
    if terminal_exit_code is None or outcome_exit_code is None:
        raise RuntimeError("cannot seal run result without exact terminal exit-code evidence")
    if terminal_exit_code != outcome_exit_code:
        raise RuntimeError("runner-exit and final outcome disagree on the exit code")
    declarations = _declaration_rows(state_root)
    status_counts = {status: 0 for status in NODE_STATUSES}
    for row in declarations:
        if row["status"] in status_counts:
            status_counts[row["status"]] += 1
    launch = _read_json(run_launch_snapshot_path(state_root, normalized))
    launch = launch if str(launch.get("run_id", "") or "") == normalized else {}
    timestamps = [
        str(event.get("timestamp", "") or "")
        for event in stream.events
        if str(event.get("timestamp", "") or "")
    ]
    payload = {
        "version": 1,
        "run_id": normalized,
        "finalized_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "stream": {
            "event_count": len(stream.events),
            "events_sha256": _events_sha256(stream.events),
            "terminal_event_id": str(stream.events[-1].get("event_id", "") or ""),
        },
        "run": {
            **_run_stream._event_run_metadata(stream.events),
            "started_at": timestamps[0] if timestamps else "",
            "updated_at": timestamps[-1] if timestamps else "",
        },
        "usage": _aggregate_usage(stream.events, _run_log_path(state_root, normalized)),
        "declarations": declarations,
        "declaration_status_counts": status_counts,
        "journal_rejections": dict(_journal_rejections(state_root, normalized).most_common()),
        "journal_rejections_scope": "explicit-run-id-only",
        "evidence": {
            "launch_snapshot_collision": (
                _launch_collision_key(state_root, normalized) in _PROCESS_LAUNCH_COLLISIONS
                or run_launch_collision_path(state_root, normalized).is_file()
            ),
        },
        "outcome": dict(outcome),
        "launch": launch,
        "final_provenance": provenance_collector(project_root),
    }
    snapshot_path = run_result_snapshot_path(state_root, normalized)
    if not _write_json_exclusive(snapshot_path, payload):
        return False
    integrity_path = run_result_integrity_path(state_root, normalized)
    integrity_created = _write_json_exclusive(
        integrity_path,
        {
            "version": 1,
            "run_id": normalized,
            "algorithm": "sha256",
            "payload_sha256": _snapshot_sha256(payload),
        },
    )
    _harden_snapshot_read_only(snapshot_path)
    if integrity_created:
        _harden_snapshot_read_only(integrity_path)
    return integrity_created
