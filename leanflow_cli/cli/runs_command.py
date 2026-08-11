"""``leanflow runs`` — read managed-workflow run state as JSON.

The workflow-state directory is an implementation detail with locking rules,
retention compaction, and per-run/per-agent stream splitting. Anything outside
the runtime that wants to observe a run — the VS Code extension, the evaluation
harness, a notebook — should read it through this command rather than globbing
the directory, so the on-disk layout stays free to change.

Every subcommand emits JSON by default; ``--pretty`` renders a terminal table
instead for interactive use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from leanflow_cli.cli import run_history as _run_history

_RUN_ID_PREFIXES_TO_STRIP = _run_history._RUN_ID_PREFIXES_TO_STRIP

#: The variable the state-path resolver consults to locate the active project.
_PROJECT_ROOT_ENV = "LEANFLOW_PROJECT_ROOT"


def register_runs_parser(subparsers: Any) -> None:
    """Attach the ``runs`` command tree to the top-level CLI parser."""
    runs_parser = subparsers.add_parser(
        "runs", help="Inspect managed workflow runs and their recorded state"
    )
    runs_parser.add_argument(
        "--project",
        default="",
        help="Project root to read state from (defaults to discovery from the cwd)",
    )
    runs_sub = runs_parser.add_subparsers(dest="runs_command")

    list_parser = runs_sub.add_parser("list", help="List recorded runs, newest first")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--pretty", action="store_true")
    list_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    status_parser = runs_sub.add_parser("status", help="Show the live status snapshot")
    status_parser.add_argument("--pretty", action="store_true")
    status_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    events_parser = runs_sub.add_parser("events", help="Read a run's activity events")
    events_parser.add_argument("run_id", nargs="?", default="")
    events_parser.add_argument("--limit", type=int, default=200)
    events_parser.add_argument("--since", default="", help="Only events after this event_id")
    events_parser.add_argument(
        "--type", default="", dest="event_types", help="Comma-separated event types to keep"
    )
    events_parser.add_argument("--pretty", action="store_true")
    events_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    journal_parser = runs_sub.add_parser("journal", help="Read proof-graph journal records")
    journal_parser.add_argument("--limit", type=int, default=200)
    journal_parser.add_argument("--pretty", action="store_true")
    journal_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    outcomes_parser = runs_sub.add_parser("outcomes", help="Read recorded workflow outcomes")
    outcomes_parser.add_argument("--limit", type=int, default=200)
    outcomes_parser.add_argument("--pretty", action="store_true")
    outcomes_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    log_parser = runs_sub.add_parser("log", help="Tail the human-readable run log")
    log_parser.add_argument("run_id", nargs="?", default="")
    log_parser.add_argument("--tail", type=int, default=200)
    log_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    stop_parser = runs_sub.add_parser(
        "stop", help="Interrupt the verified live owner of one exact run"
    )
    stop_parser.add_argument("run_id")
    stop_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    types_parser = runs_sub.add_parser("types", help="Count activity event types across runs")
    types_parser.add_argument("--pretty", action="store_true")
    types_parser.add_argument("--run-id", default="", dest="run_id")
    types_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    metrics_parser = runs_sub.add_parser(
        "metrics",
        help="Aggregate one run's complete recorded stream, with provenance",
    )
    metrics_parser.add_argument("run_id", nargs="?", default="")
    metrics_parser.add_argument("--pretty", action="store_true")
    metrics_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    metrics_parser.add_argument(
        "--no-provenance",
        action="store_true",
        help="Omit sealed provenance fields and their completeness requirement",
    )

    provenance_parser = runs_sub.add_parser(
        "provenance", help="Show what is needed to reproduce runs in this project"
    )
    provenance_parser.add_argument("--pretty", action="store_true")
    provenance_parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


@contextmanager
def _resolved_state_root(args: argparse.Namespace) -> Iterator[Path]:
    """Yield the workflow-state root, honouring an explicit --project.

    ``workflow_state_root`` and the readers built on it resolve the project from
    ``LEANFLOW_PROJECT_ROOT``, so an explicit ``--project`` is applied by setting
    it. The change is scoped and restored: this module is imported by callers
    that outlive one command, and leaving the variable set would silently
    redirect their state reads to a directory they never asked for.
    """
    explicit = str(getattr(args, "project", "") or "").strip()
    if not explicit:
        from leanflow_cli.workflows.workflow_state import workflow_state_root

        yield workflow_state_root()
        return

    root = Path(explicit).expanduser().resolve()
    previous = os.environ.get(_PROJECT_ROOT_ENV)
    os.environ[_PROJECT_ROOT_ENV] = str(root)
    try:
        from leanflow_cli.workflows.workflow_state import workflow_state_root

        yield workflow_state_root()
    finally:
        if previous is None:
            os.environ.pop(_PROJECT_ROOT_ENV, None)
        else:
            os.environ[_PROJECT_ROOT_ENV] = previous


_read_jsonl = _run_history._read_jsonl
_iter_run_files = _run_history._iter_run_files
_iter_run_result_files = _run_history._iter_run_result_files
_run_metadata = _run_history._run_metadata
_coerce_exit_code = _run_history._coerce_exit_code
_terminal_status = _run_history._terminal_status
_summarize_run = _run_history._summarize_run
_run_result_with_status = _run_history._run_result_with_status
_run_result = _run_history._run_result
_summarize_final_result = _run_history._summarize_final_result
_archive_audit_payload = _run_history._archive_audit_payload
_summarize_retained_run = _run_history._summarize_retained_run
_retained_run_summaries = _run_history._retained_run_summaries
_retained_artifacts_present = _run_history._retained_artifacts_present


def _status_with_inferred_run_id(status: dict[str, Any], state_root: Path) -> dict[str, Any]:
    """Bind a legacy live snapshot to one exact process-identity-matched stream.

    Current runners persist ``run_id`` directly. Runners started before that
    contract landed can still be observed safely when exactly one hot stream
    contains the same pid and process-token fingerprint. Ambiguous or weaker
    evidence stays unbound rather than attributing another run's analytics.
    """
    if str(status.get("run_id", "") or "").strip():
        return status
    try:
        process_id = int(status.get("process_id", 0) or 0)
    except (TypeError, ValueError):
        return status
    token_sha256 = str(status.get("process_token_sha256", "") or "").strip()
    if process_id <= 0 or len(token_sha256) != hashlib.sha256().digest_size * 2:
        return status

    matches: list[str] = []
    for path in _iter_run_files(state_root):
        # Compatibility is needed only for runners already in flight when the
        # direct run-id field shipped. Identity-bearing provider/runner events
        # are frequent, so bound the legacy scan instead of rereading an
        # unbounded multi-hour stream on every status refresh.
        for event in reversed(_read_jsonl(path, limit=2_000)):
            details = event.get("details")
            if not isinstance(details, dict):
                continue
            try:
                event_pid = int(details.get("process_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            event_token = str(details.get("process_token_sha256", "") or "").strip()
            if event_pid == process_id and event_token == token_sha256:
                matches.append(path.stem)
                break
    if len(matches) != 1:
        return status
    return {**status, "run_id": matches[0]}


def _handle_list(args: argparse.Namespace, state_root: Path) -> int:
    limit = max(1, int(getattr(args, "limit", 50) or 50))
    runs = [_summarize_run(state_root, path) for path in _iter_run_files(state_root)]
    hot_run_ids = {str(run.get("run_id", "") or "") for run in runs}
    invalid_final_results: list[str] = []
    for path in _iter_run_result_files(state_root):
        if path.stem in hot_run_ids:
            continue
        summary = _summarize_final_result(state_root, path)
        if summary is not None:
            runs.append(summary)
        else:
            invalid_final_results.append(path.stem)
    known_run_ids = {str(run.get("run_id", "") or "") for run in runs}
    retained, archive_audit = _retained_run_summaries(state_root)
    runs.extend(run for run in retained if run["run_id"] not in known_run_ids)
    runs.sort(key=lambda row: row["updated_at"], reverse=True)
    total_count = len(runs)
    truncated = total_count > limit
    runs = runs[:limit]
    completeness_issues = [
        f"retained-archive:{code}"
        for code, count in dict(archive_audit.get("issue_counts") or {}).items()
        if count
    ]
    completeness_issues.extend(
        f"final-snapshot-invalid:{run_id}" for run_id in invalid_final_results
    )
    for run in runs:
        run_id = str(run.get("run_id", "") or "")
        if run.get("stream_integrity_complete") is not True:
            completeness_issues.append(f"hot-stream-invalid:{run_id}")
        final_integrity = str(run.get("final_snapshot_integrity", "") or "")
        if final_integrity not in {"integrity-verified", "final-snapshot-missing"}:
            completeness_issues.append(f"final-snapshot-invalid:{run_id}:{final_integrity}")
    if truncated:
        completeness_issues.append(f"history-truncated-by-limit:{total_count - limit}")
    completeness_issues = sorted(set(completeness_issues))
    payload = {
        "version": 2,
        "state_root": str(state_root),
        "complete": bool(archive_audit.get("complete") and not completeness_issues),
        "completeness_issues": completeness_issues,
        "archive_audit": archive_audit,
        "limit": limit,
        "total_count": total_count,
        "truncated": truncated,
        "count": len(runs),
        "runs": runs,
    }
    if not getattr(args, "pretty", False):
        _print_json(payload)
        return 0
    console = Console()
    if not runs:
        console.print(f"[dim]No recorded runs under {state_root}[/dim]")
        return 0
    table = Table(title="Managed workflow runs", title_justify="left", header_style="bold")
    table.add_column("Run")
    table.add_column("Kind")
    table.add_column("Command", overflow="fold")
    table.add_column("Events", justify="right")
    table.add_column("Updated")
    for run in runs:
        table.add_row(
            run["label"],
            run["workflow_kind"] or "—",
            run["workflow_command"] or "—",
            str(run["event_count"]),
            run["updated_at"] or "—",
        )
    console.print(table)
    return 0


def _handle_status(args: argparse.Namespace, state_root: Path) -> int:
    from leanflow_cli.workflows.workflow_state import load_workflow_live_status

    payload = _status_with_inferred_run_id(load_workflow_live_status() or {}, state_root)
    if not getattr(args, "pretty", False):
        _print_json({"version": 1, "state_root": str(state_root), "status": payload})
        return 0
    console = Console()
    if not payload:
        console.print("[dim]No live status recorded.[/dim]")
        return 0
    table = Table(title="Live status", title_justify="left", header_style="bold")
    table.add_column("Field")
    table.add_column("Value", overflow="fold")
    for key in (
        "phase",
        "workflow_kind",
        "workflow_command",
        "active_file_label",
        "target_symbol",
        "provider",
        "model",
        "sorry_count",
        "project_sorry_count",
        "proof_solved",
        "current_blocker",
        "last_activity_type",
        "last_activity_message",
        "updated_at",
        "runtime_heartbeat_at",
        "process_id",
    ):
        if key in payload:
            table.add_row(key, str(payload[key]))
    console.print(table)
    return 0


def _resolve_run_path(state_root: Path, run_id: str) -> Path | None:
    from leanflow_cli.cli.run_metrics import validate_run_id

    if run_id:
        run_id = validate_run_id(run_id)
        candidate = state_root / "activity" / "runs" / f"{run_id}.jsonl"
        return candidate if candidate.is_file() else None
    paths = list(_iter_run_files(state_root))
    if not paths:
        return None
    return max(paths, key=lambda path: path.stat().st_mtime)


def _handle_events(args: argparse.Namespace, state_root: Path) -> int:
    run_id = str(getattr(args, "run_id", "") or "").strip()
    path = _resolve_run_path(state_root, run_id)
    if path is None:
        _print_json({"version": 1, "run_id": run_id, "count": 0, "events": []})
        return 0
    limit = max(1, int(getattr(args, "limit", 200) or 200))
    events = _read_jsonl(path, limit=0)

    since = str(getattr(args, "since", "") or "").strip()
    if since:
        # Incremental polling: return only what the caller has not seen. An
        # unknown cursor means the stream was rotated, so send the tail instead
        # of nothing, which would silently stall a follower.
        index = next(
            (i for i, event in enumerate(events) if str(event.get("event_id", "")) == since),
            None,
        )
        if index is not None:
            events = events[index + 1 :]

    type_filter = {
        part.strip()
        for part in str(getattr(args, "event_types", "") or "").split(",")
        if part.strip()
    }
    if type_filter:
        events = [event for event in events if str(event.get("type", "")) in type_filter]
    events = events[-limit:]

    payload = {
        "version": 1,
        "run_id": path.stem,
        "count": len(events),
        "cursor": str(events[-1].get("event_id", "")) if events else since,
        "events": events,
    }
    if not getattr(args, "pretty", False):
        _print_json(payload)
        return 0
    console = Console()
    for event in events:
        console.print(
            f"[dim]{event.get('timestamp', '')}[/dim] "
            f"[cyan]{event.get('type', '')}[/cyan] {event.get('message', '')}"
        )
    return 0


def _handle_journal(args: argparse.Namespace, state_root: Path) -> int:
    limit = max(1, int(getattr(args, "limit", 200) or 200))
    records = _read_jsonl(state_root / "journal.jsonl", limit=limit)
    payload = {"version": 1, "count": len(records), "records": records}
    if not getattr(args, "pretty", False):
        _print_json(payload)
        return 0
    console = Console()
    for record in records:
        console.print(
            f"[dim]{record.get('ts', '')}[/dim] [cyan]{record.get('event', '')}[/cyan] "
            f"{record.get('name', '')} {record.get('why', '')}"
        )
    return 0


def _handle_outcomes(args: argparse.Namespace, state_root: Path) -> int:
    limit = max(1, int(getattr(args, "limit", 200) or 200))
    records = _read_jsonl(state_root / "outcomes.jsonl", limit=limit)
    payload = {"version": 1, "count": len(records), "outcomes": records}
    if not getattr(args, "pretty", False):
        _print_json(payload)
        return 0
    console = Console()
    for record in records:
        console.print(
            f"[dim]{record.get('timestamp', '')}[/dim] [cyan]{record.get('kind', '')}[/cyan] "
            f"{json.dumps(record.get('payload', {}), sort_keys=True)[:160]}"
        )
    return 0


def _handle_log(args: argparse.Namespace, state_root: Path) -> int:
    tail = max(1, int(getattr(args, "tail", 200) or 200))
    run_id = str(getattr(args, "run_id", "") or "").strip()
    if run_id:
        from leanflow_cli.cli.run_metrics import validate_run_id

        run_id = validate_run_id(run_id)
        path = state_root / "runs" / f"{run_id}.log"
        if not path.is_file():
            _print_json(
                {
                    "version": 1,
                    "success": False,
                    "run_id": run_id,
                    "error": "No run-specific log is recorded for this run.",
                }
            )
            return 1
        try:
            from collections import deque

            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = deque((line.rstrip("\r\n") for line in handle), maxlen=tail)
        except OSError as exc:
            _print_json(
                {
                    "version": 1,
                    "success": False,
                    "run_id": run_id,
                    "error": str(exc),
                }
            )
            return 1
        print("\n".join(lines) or "[run log is empty]")
        return 0

    from leanflow_cli.workflows.workflow_state import read_workflow_run_log

    print(read_workflow_run_log(tail_lines=tail) or "[no workflow run log recorded yet]")
    return 0


def _handle_stop(args: argparse.Namespace, state_root: Path) -> int:
    """Interrupt only the verified live-status owner for the requested run."""
    from leanflow_cli.cli.run_metrics import validate_run_id
    from leanflow_cli.workflows.workflow_state import (
        interrupt_workflow_process,
        load_workflow_live_status,
    )

    run_id = validate_run_id(str(getattr(args, "run_id", "") or ""))
    status = _status_with_inferred_run_id(load_workflow_live_status(), state_root)
    current_run_id = str(status.get("run_id", "") or "")
    if current_run_id != run_id:
        _print_json(
            {
                "version": 1,
                "success": False,
                "stopped": False,
                "run_id": run_id,
                "current_run_id": current_run_id,
                "reason": "The requested run is not the project's verified live owner.",
            }
        )
        return 1
    result = interrupt_workflow_process(status)
    success = bool(result.get("success"))
    _print_json(
        {
            "version": 1,
            "success": success,
            "stopped": success,
            "run_id": run_id,
            "reason": (
                "Interrupt sent to the verified run owner."
                if success
                else str(result.get("error", "") or "Run owner could not be interrupted.")
            ),
            "process_id": result.get("process_id", status.get("process_id", 0)),
            "process_group_id": result.get("process_group_id", 0),
            "identity_verified": bool(result.get("identity_verified", False)),
        }
    )
    return 0 if success else 1


def _handle_types(args: argparse.Namespace, state_root: Path) -> int:
    """Count event types, which is what a log viewer needs to build its filter."""
    from collections import Counter

    run_id = str(getattr(args, "run_id", "") or "").strip()
    paths = [_resolve_run_path(state_root, run_id)] if run_id else list(_iter_run_files(state_root))
    counter: Counter[str] = Counter()
    for path in paths:
        if path is None:
            continue
        for event in _read_jsonl(path, limit=0):
            counter[str(event.get("type", "") or "unknown")] += 1
    ranked: list[tuple[str, int]] = counter.most_common()
    payload = {
        "version": 1,
        "total": sum(counter.values()),
        "types": [{"type": name, "count": count} for name, count in ranked],
    }
    if not getattr(args, "pretty", False):
        _print_json(payload)
        return 0
    console = Console()
    table = Table(title="Activity event types", title_justify="left", header_style="bold")
    table.add_column("Type")
    table.add_column("Count", justify="right")
    for name, count in ranked:
        table.add_row(name, str(count))
    console.print(table)
    return 0


def _project_root_for(args: argparse.Namespace, state_root: Path) -> Path:
    """Return the project root implied by the resolved state root."""
    explicit = str(getattr(args, "project", "") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    # <project>/.leanflow/workflow-state -> <project>
    return state_root.parent.parent


def _handle_metrics(args: argparse.Namespace, state_root: Path) -> int:
    from leanflow_cli.cli.run_metrics import collect_run_metrics

    payload = collect_run_metrics(
        state_root,
        run_id=str(getattr(args, "run_id", "") or ""),
        project_root=(
            None if getattr(args, "no_provenance", False) else _project_root_for(args, state_root)
        ),
    )
    exact = bool(dict(payload.get("scope") or {}).get("exact"))
    if not getattr(args, "pretty", False):
        _print_json(payload)
        return 0 if exact else 1

    console = Console()
    run = payload["run"]
    console.print(f"[bold]{run['run_id'] or '[no run recorded]'}[/bold]")
    console.print(f"  {run['workflow_command'] or '—'}")
    console.print(
        f"  events {payload['events']['total']}"
        f" · tool calls {payload['activity']['tool_calls']}"
        f" · api requests {payload['activity']['api_requests']}"
    )
    usage = payload["usage"]
    if usage["output_tokens"] is not None:
        console.print(
            f"  tokens in {usage['input_tokens']} / out {usage['output_tokens']}"
            f" · ${usage['cost_usd'] if usage['cost_usd'] is not None else '—'}"
        )
    if payload["failures"]:
        table = Table(title="Failures", title_justify="left", header_style="bold")
        table.add_column("Kind")
        table.add_column("Count", justify="right")
        for kind, count in payload["failures"].items():
            table.add_row(kind, str(count))
        console.print(table)
    if payload["declarations"]:
        table = Table(title="Declarations", title_justify="left", header_style="bold")
        table.add_column("Declaration")
        table.add_column("File", overflow="fold")
        table.add_column("Status")
        table.add_column("Attempts", justify="right")
        for row in payload["declarations"]:
            table.add_row(row["name"], row["file"], row["status"], str(row["attempts"]))
        console.print(table)
    return 0 if exact else 1


def _handle_provenance(args: argparse.Namespace, state_root: Path) -> int:
    from leanflow_cli.cli.run_metrics import collect_provenance

    payload = collect_provenance(_project_root_for(args, state_root))
    complete = payload.get("provenance_complete") is True
    if not getattr(args, "pretty", False):
        _print_json(payload)
        return 0 if complete else 1
    console = Console()
    for key, value in payload.items():
        if isinstance(value, dict):
            console.print(f"  {key}:")
            for name, rev in value.items():
                console.print(f"    {name:<18} {rev}")
        else:
            console.print(f"  {key:<20} {value}")
    return 0 if complete else 1


def handle_runs(args: argparse.Namespace) -> int:
    """Dispatch one ``leanflow runs`` invocation."""
    command = str(getattr(args, "runs_command", "") or "list")
    handlers = {
        "list": _handle_list,
        "status": _handle_status,
        "events": _handle_events,
        "journal": _handle_journal,
        "outcomes": _handle_outcomes,
        "log": _handle_log,
        "stop": _handle_stop,
        "types": _handle_types,
        "metrics": _handle_metrics,
        "provenance": _handle_provenance,
    }
    handler = handlers.get(command)
    if handler is None:
        print(f"Unknown runs command: {command}", file=sys.stderr)
        return 1
    try:
        with _resolved_state_root(args) as state_root:
            return handler(args, state_root)
    except ValueError as exc:
        _print_json({"version": 1, "success": False, "error": str(exc)})
        return 2
