"""Resolve one activity event to the complete model output recorded for it.

The shared activity stream stays compact: a bounded prover controller projects
one row per request, response, and tool result. The complete assistant message,
tool arguments, and tool result live in that job's private ``events.jsonl`` and
in the run's controller event log. This reader joins a row to its full record
by the ``evidence_id`` both sides carry, and falls back to a bounded kind/time
match for runs recorded before that id existed.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from leanflow_cli.cli.prover_status import _identifier, _run_directory
from leanflow_cli.cli.run_metrics import validate_run_id

#: Longest string retained inside one returned record; model outputs are far
#: shorter, and a truncated tool payload still names its artifact path.
MAX_EVIDENCE_STRING_CHARS = 200_000
#: Upper bound on bytes scanned per log so a corrupt or runaway file cannot
#: stall an editor request.
MAX_SCAN_BYTES = 512 * 1024 * 1024
#: A best-effort match must land near the row's own second-resolution stamp.
HEURISTIC_WINDOW_SECONDS = 300.0
_API_KINDS = {"api-request", "api-response", "api-error", "context-compacted"}


def _iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from one regular log file without following a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return
        consumed = 0
        for raw in handle:
            consumed += len(raw)
            if consumed > MAX_SCAN_BYTES:
                return
            line = raw.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                yield parsed


def _timestamp(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth > 24:
        return "<omitted: excessive nesting>"
    if isinstance(value, str):
        if len(value) <= MAX_EVIDENCE_STRING_CHARS:
            return value
        return value[:MAX_EVIDENCE_STRING_CHARS] + f"… <truncated chars={len(value)}>"
    if isinstance(value, list):
        return [_bounded(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): _bounded(item, depth + 1) for key, item in value.items()}
    return value


def find_activity_event(state_root: Path, run_id: str, event_id: str) -> dict[str, Any] | None:
    """Return the exact activity row for ``event_id`` from the run's hot stream."""
    path = state_root / "activity" / "runs" / f"{run_id}.jsonl"
    for record in _iter_records(path):
        if str(record.get("event_id", "") or "") != event_id:
            continue
        if str(record.get("run_id", "") or run_id) != run_id:
            continue
        return record
    return None


def _candidate(source: str, record: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a job-log or controller-log record into one comparable shape."""
    if source == "job-log":
        details = record.get("details")
        kind = str(record.get("type", "") or "")
        stamp = record.get("timestamp", "")
        details = details if isinstance(details, dict) else {}
        evidence_id = str(record.get("evidence_id", "") or details.get("evidence_id", "") or "")
    else:
        kind = str(record.get("event", "") or "")
        stamp = record.get("time", "")
        details = {key: item for key, item in record.items() if key not in {"time", "event"}}
        evidence_id = str(record.get("evidence_id", "") or "")
    if not kind:
        return None
    return {
        "kind": kind,
        "timestamp": str(stamp or ""),
        "details": details,
        "evidence_id": evidence_id,
    }


def _select(
    candidates: list[dict[str, Any]], event: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """Pick the record for ``event``: by shared id, else by kind, count, tool, and time."""
    details = event.get("details")
    details = details if isinstance(details, dict) else {}
    wanted = str(details.get("evidence_id", "") or "")
    if wanted:
        for candidate in candidates:
            if candidate["evidence_id"] == wanted:
                return candidate, "evidence_id"
    kind = str(event.get("type", "") or "")
    same_kind = [candidate for candidate in candidates if candidate["kind"] == kind]
    api_calls = details.get("api_calls")
    if kind in _API_KINDS and isinstance(api_calls, int) and not isinstance(api_calls, bool):
        exact = [c for c in same_kind if c["details"].get("api_calls") == api_calls]
        if len(exact) == 1:
            return exact[0], "heuristic"
        same_kind = exact or same_kind
    tool = str(details.get("tool", "") or "")
    if tool:
        same_kind = [c for c in same_kind if str(c["details"].get("tool", "") or "") == tool]
    if not same_kind:
        return None, ""
    at = _timestamp(event.get("timestamp"))
    if at is None:
        return (same_kind[0], "heuristic") if len(same_kind) == 1 else (None, "")
    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate in same_kind:
        stamp = _timestamp(candidate["timestamp"])
        if stamp is None:
            continue
        delta = abs(stamp - at)
        if delta <= HEURISTIC_WINDOW_SECONDS:
            scored.append((delta, candidate))
    if not scored:
        return None, ""
    scored.sort(key=lambda item: item[0])
    return scored[0][1], "heuristic"


def _evidence_sources(state_root: Path, run_id: str, job_id: str) -> list[tuple[str, Path]]:
    """List candidate logs, the job's own transcript first, never through symlinks."""
    try:
        directory = _run_directory(state_root, run_id)
    except ValueError:
        return []
    sources: list[tuple[str, Path]] = []
    if job_id:
        try:
            _identifier(job_id, "job id")
        except ValueError:
            pass
        else:
            sources.append(("job-log", directory / "jobs" / job_id / "events.jsonl"))
    sources.append(("run-events", directory / "events.jsonl"))
    return sources


def read_event_detail(state_root: Path, run_id: str, event_id: str) -> dict[str, Any]:
    """Return one activity row with the full recorded output it summarizes."""
    run_id = validate_run_id(run_id)
    event_id = _identifier(event_id, "event id")
    event = find_activity_event(state_root, run_id, event_id)
    payload: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "event_id": event_id,
        "found": event is not None,
        "event": event,
        "evidence": {"source": "none", "path": "", "match": "", "record": None},
    }
    if event is None:
        return payload
    details = event.get("details")
    details = details if isinstance(details, dict) else {}
    job_id = str(details.get("job_id", "") or "")
    for source, path in _evidence_sources(state_root, run_id, job_id):
        candidates = [
            candidate
            for candidate in (_candidate(source, record) for record in _iter_records(path))
            if candidate is not None
        ]
        selected, match = _select(candidates, event)
        if selected is None:
            continue
        payload["evidence"] = {
            "source": source,
            "path": str(path),
            "match": match,
            "record": _bounded(selected),
        }
        break
    return payload
