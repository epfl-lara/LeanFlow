"""Reconstruct complete run history from hot, final, and retained evidence.

This leaf validates per-run stream and final-result integrity, audits retained
archives, and produces stable observer summaries without owning argparse or
terminal rendering.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

_RUN_ID_PREFIXES_TO_STRIP = (
    "prove-",
    "formalize-",
    "review-",
    "refactor-",
    "golf-",
    "draft-",
)


def _read_jsonl(path: Path, *, limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` trailing records from one JSONL file."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
    except OSError:
        return []
    return records[-limit:] if limit > 0 else records


def _iter_run_files(state_root: Path) -> Iterator[Path]:
    from leanflow_cli.cli.run_metrics import validate_run_id

    runs_dir = state_root / "activity" / "runs"
    if runs_dir.is_dir():
        for path in sorted(runs_dir.glob("*.jsonl")):
            try:
                validate_run_id(path.stem)
            except ValueError:
                continue
            yield path


def _iter_run_result_files(state_root: Path) -> Iterator[Path]:
    """Yield immutable final-result snapshots, including archived runs."""
    from leanflow_cli.cli.run_metrics import validate_run_id

    root = state_root / "activity" / "run-results"
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            try:
                validate_run_id(path.stem)
            except ValueError:
                continue
            yield path


def _run_metadata(state_root: Path, run_id: str) -> dict[str, Any]:
    from leanflow_cli.cli.run_metrics import validate_run_id

    run_id = validate_run_id(run_id)
    path = state_root / "activity" / "run-metadata" / f"{run_id}.json"
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_exit_code(value: Any) -> int | None:
    """Return an integer exit code without turning missing evidence into success."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _terminal_status(*, terminal: bool, exit_code: int | None) -> str:
    """Return a stable observer-facing state from exact terminal evidence."""
    if not terminal:
        return "running"
    from leanflow_cli.cli.run_metrics import terminal_status_for_exit_code

    return terminal_status_for_exit_code(exit_code)


def _summarize_run(state_root: Path, path: Path) -> dict[str, Any]:
    """Summarize one run stream without loading the whole file into memory twice."""
    run_id = path.stem
    first: dict[str, Any] | None = None
    last: dict[str, Any] | None = None
    count = 0
    stream_digest = hashlib.sha256()
    stream_integrity_complete = True
    try:
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            stream_integrity_complete = False
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (UnicodeError, json.JSONDecodeError):
                stream_integrity_complete = False
                continue
            if not isinstance(event, dict) or str(event.get("run_id", "") or "") != run_id:
                stream_integrity_complete = False
                continue
            count += 1
            stream_digest.update(
                json.dumps(
                    event,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            )
            stream_digest.update(b"\n")
            if first is None:
                first = event
            last = event
    except OSError:
        stream_integrity_complete = False
    metadata = _run_metadata(state_root, run_id)
    result, result_integrity = _run_result_with_status(state_root, run_id)
    result_stream = result.get("stream") if result else None
    if not (
        isinstance(result_stream, Mapping)
        and _coerce_exit_code(result_stream.get("event_count")) == count
        and str(result_stream.get("events_sha256", "") or "") == stream_digest.hexdigest()
        and str(result_stream.get("terminal_event_id", "") or "")
        == str((last or {}).get("event_id", "") or "")
    ):
        if result:
            result_integrity = "final-snapshot-stream-mismatch"
        result = {}
    details = (last or {}).get("details")
    details = details if isinstance(details, Mapping) else {}
    outcome = result.get("outcome")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    runner_exit_seen = str((last or {}).get("type", "") or "") == "runner-exit"
    terminal = runner_exit_seen or bool(result)
    event_exit_code = _coerce_exit_code(details.get("exit_code")) if runner_exit_seen else None
    exit_code = event_exit_code
    if exit_code is None and result:
        exit_code = _coerce_exit_code(outcome.get("exit_code"))
    terminal_phase = ""
    if terminal:
        terminal_phase = str(outcome.get("phase", "") or details.get("phase", "") or "exited")
    label = run_id
    for prefix in _RUN_ID_PREFIXES_TO_STRIP:
        if label.startswith(prefix):
            label = label[len(prefix) :]
            break
    return {
        "run_id": run_id,
        "label": label,
        "event_count": count,
        "started_at": str((first or {}).get("timestamp", "") or ""),
        "updated_at": str((last or {}).get("timestamp", "") or ""),
        "last_event_type": str((last or {}).get("type", "") or ""),
        "last_message": str((last or {}).get("message", "") or ""),
        "workflow_kind": str(
            metadata.get("workflow_kind") or details.get("workflow_kind", "") or ""
        ),
        "workflow_command": str(
            metadata.get("workflow_command") or details.get("workflow_command", "") or ""
        ),
        "active_skill": str(metadata.get("active_skill") or details.get("active_skill", "") or ""),
        "project_root": str(metadata.get("project_root") or details.get("project_root", "") or ""),
        "parent_run_id": str(metadata.get("parent_run_id", "") or ""),
        "run_scope": str(metadata.get("run_scope", "") or ""),
        "process_id": metadata.get("process_id", 0),
        "path": str(path),
        "stream_source": "hot",
        "stream_integrity_complete": stream_integrity_complete,
        "terminal": terminal,
        "exit_code": exit_code,
        "terminal_phase": terminal_phase,
        "terminal_status": _terminal_status(terminal=terminal, exit_code=exit_code),
        "final_snapshot_recorded": bool(result),
        "final_snapshot_integrity": result_integrity,
        "finalized_at": str(result.get("finalized_at", "") or ""),
    }


def _run_result_with_status(state_root: Path, run_id: str) -> tuple[dict[str, Any], str]:
    """Return one identity-checked result and its corruption-detection status."""
    from leanflow_cli.cli.run_metrics import read_integrity_checked_run_result

    return read_integrity_checked_run_result(state_root, run_id)


def _run_result(state_root: Path, run_id: str) -> dict[str, Any]:
    """Return one identity-checked immutable final-result snapshot."""
    result, _status = _run_result_with_status(state_root, run_id)
    return result


def _summarize_final_result(state_root: Path, path: Path) -> dict[str, Any] | None:
    """Summarize an archived run from its immutable final-result envelope."""
    from leanflow_cli.cli.run_metrics import validate_run_id

    try:
        run_id = validate_run_id(path.stem)
    except ValueError:
        return None
    result = _run_result(state_root, run_id)
    if not result:
        return None
    metadata = _run_metadata(state_root, run_id)
    run = result.get("run")
    run = run if isinstance(run, Mapping) else {}
    stream = result.get("stream")
    stream = stream if isinstance(stream, Mapping) else {}
    outcome = result.get("outcome")
    outcome = outcome if isinstance(outcome, Mapping) else {}
    exit_code = _coerce_exit_code(outcome.get("exit_code"))
    terminal_phase = str(outcome.get("phase", "") or "exited")
    label = run_id
    for prefix in _RUN_ID_PREFIXES_TO_STRIP:
        if label.startswith(prefix):
            label = label[len(prefix) :]
            break
    return {
        "run_id": run_id,
        "label": label,
        "event_count": int(stream.get("event_count", 0) or 0),
        "started_at": str(run.get("started_at", "") or ""),
        "updated_at": str(run.get("updated_at", "") or result.get("finalized_at", "") or ""),
        "last_event_type": "runner-exit",
        "last_message": str(outcome.get("reason", "") or ""),
        "workflow_kind": str(metadata.get("workflow_kind") or run.get("workflow_kind", "") or ""),
        "workflow_command": str(
            metadata.get("workflow_command") or run.get("workflow_command", "") or ""
        ),
        "active_skill": str(metadata.get("active_skill") or run.get("active_skill", "") or ""),
        "project_root": str(metadata.get("project_root", "") or ""),
        "parent_run_id": str(metadata.get("parent_run_id", "") or ""),
        "run_scope": str(metadata.get("run_scope", "") or ""),
        "process_id": 0,
        "path": str(path),
        "stream_source": "final-result",
        "stream_integrity_complete": True,
        "terminal": True,
        "exit_code": exit_code,
        "terminal_phase": terminal_phase,
        "terminal_status": _terminal_status(terminal=True, exit_code=exit_code),
        "final_snapshot_recorded": True,
        "final_snapshot_integrity": "integrity-verified",
        "finalized_at": str(result.get("finalized_at", "") or ""),
    }


def _archive_audit_payload(result: Any) -> dict[str, Any]:
    """Return the bounded strict-retention audit contract as JSON."""
    return {
        "complete": bool(result.complete),
        "catalog_status": str(result.catalog_status),
        "catalog_runs": int(result.catalog_runs),
        "verified_runs": int(result.verified_runs),
        "verified_events": int(result.verified_events),
        "matched_events": int(result.matched_events),
        "issue_counts": dict(result.issue_counts),
        "issue_samples": [
            {
                "code": item.code,
                "run_id": item.run_id,
                "path": item.path,
                "detail": item.detail,
            }
            for item in result.issue_samples
        ],
    }


def _summarize_retained_run(
    state_root: Path, run_id: str, events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Summarize one strictly verified retained stream, including crashed runs."""
    metadata = _run_metadata(state_root, run_id)
    first = events[0] if events else {}
    last = events[-1] if events else {}
    event_details = [
        event.get("details") for event in events if isinstance(event.get("details"), Mapping)
    ]

    def first_detail(name: str) -> str:
        for details in event_details:
            assert isinstance(details, Mapping)
            value = str(details.get(name, "") or "")
            if value:
                return value
        return ""

    last_details = last.get("details")
    last_details = last_details if isinstance(last_details, Mapping) else {}
    terminal = str(last.get("type", "") or "") == "runner-exit"
    exit_code = _coerce_exit_code(last_details.get("exit_code")) if terminal else None
    label = run_id
    for prefix in _RUN_ID_PREFIXES_TO_STRIP:
        if label.startswith(prefix):
            label = label[len(prefix) :]
            break
    return {
        "run_id": run_id,
        "label": label,
        "event_count": len(events),
        "started_at": str(first.get("timestamp", "") or ""),
        "updated_at": str(last.get("timestamp", "") or ""),
        "last_event_type": str(last.get("type", "") or ""),
        "last_message": str(last.get("message", "") or ""),
        "workflow_kind": str(metadata.get("workflow_kind") or first_detail("workflow_kind")),
        "workflow_command": str(
            metadata.get("workflow_command") or first_detail("workflow_command")
        ),
        "active_skill": str(metadata.get("active_skill") or first_detail("active_skill")),
        "project_root": str(metadata.get("project_root") or first_detail("project_root")),
        "parent_run_id": str(metadata.get("parent_run_id") or first_detail("parent_run_id")),
        "run_scope": str(metadata.get("run_scope") or first_detail("run_scope")),
        "process_id": metadata.get("process_id", 0),
        "path": "",
        "stream_source": "retained",
        "stream_integrity_complete": True,
        "terminal": terminal,
        "exit_code": exit_code,
        "terminal_phase": (str(last_details.get("phase", "") or "exited") if terminal else ""),
        "terminal_status": (
            _terminal_status(terminal=True, exit_code=exit_code) if terminal else "unknown"
        ),
        "final_snapshot_recorded": False,
        "final_snapshot_integrity": "final-snapshot-missing",
        "finalized_at": "",
    }


def _retained_run_summaries(state_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return retained summaries only after the whole archive catalog verifies."""
    from leanflow_cli.cli.run_metrics import validate_run_id
    from leanflow_cli.workflows.workflow_activity_retention import audit_retained_run_events

    by_run: dict[str, list[dict[str, Any]]] = {}

    def consume(event: dict[str, Any]) -> None:
        run_id = str(event.get("run_id", "") or "")
        try:
            validate_run_id(run_id)
        except ValueError:
            return
        by_run.setdefault(run_id, []).append(event)

    result = audit_retained_run_events(state_root, on_event=consume)
    audit = _archive_audit_payload(result)
    if result.catalog_status == "missing" and not _retained_artifacts_present(state_root):
        # A brand-new project has no retention epoch yet. Treat that state as
        # complete-empty only while no archive, shard, or retention temp file
        # exists; a missing catalog beside any such evidence remains a loss.
        audit = {
            **audit,
            "complete": True,
            "catalog_status": "complete-empty",
            "catalog_runs": 0,
            "verified_runs": 0,
            "verified_events": 0,
            "matched_events": 0,
            "issue_counts": {},
            "issue_samples": [],
        }
        return [], audit
    if not result.complete:
        return [], audit
    return [
        _summarize_retained_run(state_root, run_id, events)
        for run_id, events in sorted(by_run.items())
        if events
    ], audit


def _retained_artifacts_present(state_root: Path) -> bool:
    """Return whether a missing retention catalog has evidence beside it."""
    activity_root = state_root / "activity"
    roots = (activity_root / "archive", activity_root / "historical-runs")
    for root in roots:
        if not root.exists():
            continue
        try:
            if any(path.is_file() or path.is_symlink() for path in root.rglob("*")):
                return True
        except OSError:
            return True
    try:
        return any(
            path.is_file() or path.is_symlink()
            for path in activity_root.glob("*historical-summary*.tmp")
        )
    except OSError:
        return True
