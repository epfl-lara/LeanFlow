"""Expose exact-run prover state and durable user steering through the CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_TERMINAL_PHASES = frozenset(
    {
        "complete",
        "completed",
        "succeeded",
        "failed",
        "cancelled",
        "canceled",
        "stopped",
        "exhausted",
        "budget-exhausted",
        "disproved",
        "interrupted",
        "provider_error",
        "environment_error",
        "source_conflict",
        "verification_failed",
        "budget_exhausted",
        "context_limit",
        "error",
        "blocked",
    }
)
_MAX_STATE_BYTES = 16 * 1024 * 1024


def _identifier(value: str, label: str) -> str:
    """Reject identifiers that could escape the exact run directory."""
    if not _IDENTIFIER.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"Invalid prover {label}.")
    return value


def _run_directory(state_root: Path, run_id: str) -> Path:
    """Resolve the requested run without following symlinked state entries."""
    _identifier(run_id, "run id")
    current = state_root
    for part in ("prover", run_id):
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinked prover state is not supported.")
    return current


def _read_file(path: Path, limit: int) -> str:
    """Read bounded regular state text without following a leaf symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("Prover state must be a regular file.")
            contents = handle.read(limit + 1)
    except OSError as exc:
        raise ValueError(f"Cannot read prover state file {path.name}: {exc.strerror}") from exc
    if len(contents) > limit:
        raise ValueError(f"Prover state file {path.name} exceeds the size limit.")
    return contents.decode("utf-8")


def read_prover_state(state_root: Path, run_id: str) -> dict[str, Any] | None:
    """Return one exact run's snapshot, including its persisted proof plan."""
    directory = _run_directory(state_root, run_id)
    state_file = directory / "state.json"
    if not state_file.exists() and not state_file.is_symlink():
        return None
    try:
        payload = json.loads(_read_file(state_file, _MAX_STATE_BYTES))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("The requested prover snapshot is not valid JSON.") from exc
    if not isinstance(payload, dict) or payload.get("run_id") != run_id:
        raise ValueError("The prover snapshot does not match the requested run id.")
    plan_file = directory / "PLAN.md"
    if plan_file.exists() or plan_file.is_symlink():
        payload["plan_markdown"] = _read_file(plan_file, 512 * 1024)
        payload["plan_path"] = str(plan_file)
    return payload


def append_prover_message(
    state_root: Path, run_id: str, agent_id: str, message: str
) -> dict[str, Any]:
    """Append user guidance for the runtime to consume between decisions."""
    _identifier(agent_id, "agent id")
    if not message.strip() or len(message) > 8192 or "\0" in message:
        raise ValueError("A prover message must contain 1 to 8192 characters.")
    state = read_prover_state(state_root, run_id)
    if state is None:
        raise ValueError("The requested prover run was not found.")
    if (
        state.get("terminal")
        or str(state.get("phase", state.get("status", ""))).lower() in _TERMINAL_PHASES
    ):
        raise ValueError("This prover run has finished and cannot accept messages.")
    jobs = state.get("jobs", [])
    agent_ids = {
        str(job.get("agent_id", job.get("id", ""))) for job in jobs if isinstance(job, dict)
    }
    if agent_id != "orchestrator" and agent_id not in agent_ids:
        raise ValueError("The requested agent does not belong to this prover run.")
    if agent_id != "orchestrator":
        addressed = next(
            job
            for job in jobs
            if isinstance(job, dict) and str(job.get("agent_id", job.get("id", ""))) == agent_id
        )
        if addressed.get("status") not in {None, "", "running", "starting", "resume_pending"}:
            raise ValueError(
                "That prover job has finished. Send new guidance to the orchestrator or an active job."
            )
    record = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "agent_id": agent_id,
        "message": message.strip(),
        "status": "pending",
    }
    directory = _run_directory(state_root, run_id)
    inbox = directory / "inbox.jsonl"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(inbox, flags, 0o600)
        with os.fdopen(fd, "ab", buffering=0) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("The prover inbox must be a regular file.")
            # One write preserves independent CLI submissions in arrival order.
            handle.write((json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ValueError(f"Cannot append the prover message: {exc.strerror}") from exc
    return record


def register_prover_parsers(subparsers: Any) -> None:
    """Attach exact-run status and message commands to the runs parser."""
    status = subparsers.add_parser(
        "prover", help="Read one prover run's DAG, plan, jobs, and metrics"
    )
    status.add_argument("run_id")
    status.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    message = subparsers.add_parser("prover-message", help="Queue guidance for a prover agent")
    message.add_argument("run_id")
    message.add_argument("--agent", default="orchestrator")
    message.add_argument("--message", required=True)
    message.add_argument("--json", action="store_true", help=argparse.SUPPRESS)


def handle_prover(args: argparse.Namespace, state_root: Path) -> int:
    """Print an exact-run snapshot or the acknowledgement of queued guidance."""
    if args.runs_command == "prover-message":
        record = append_prover_message(state_root, args.run_id, args.agent, args.message)
        result: dict[str, Any] = {"version": 1, "success": True, "message_id": record["id"]}
    else:
        state = read_prover_state(state_root, args.run_id)
        result = {"version": 1, "found": state is not None, "prover": state}
    print(json.dumps(result, ensure_ascii=False))
    return 0
