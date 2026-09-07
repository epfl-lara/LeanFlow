"""Persist and run the requested two-lane, 72-cell Lean-IMO-Bench comparison."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.lean_imo_campaign.adoption import AdoptedProcess, process_identity
from scripts.lean_imo_campaign.artifacts import environment, freeze, prepare, save
from scripts.lean_imo_campaign.matrix import CONDITION_SETS, cells, next_cell
from scripts.lean_imo_campaign.recovery import requires_inspection, schedule_recovery
from scripts.lean_imo_campaign.runtime_versions import runtime_directory


def now() -> str:
    """Return an unambiguous UTC event timestamp."""
    return datetime.now(UTC).isoformat()


def report(directory: Path, campaign: dict[str, Any]) -> None:
    """Export all live metrics plus a compact comparable CSV and editor overview."""
    campaign["updated_at"] = now()
    save(directory / "campaign.json", campaign)
    keys = [
        "api_calls",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "cost_complete",
        "cost_source",
        "costed_api_calls",
        "elapsed_s",
        "plan_refinements",
        "decompositions",
    ]
    temporary = directory / "metrics.csv.tmp"
    with temporary.open("w") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "id",
                "problem",
                "condition",
                "status",
                "verified",
                "runtime_sha256",
                "stop_code",
                "stop_scope",
                "legacy_unverified_cost_usd",
                *keys,
            ],
        )
        writer.writeheader()
        for cell in campaign["cells"]:
            metrics = {key: cell["metrics"].get(key) for key in keys}
            legacy_cost = None
            if metrics.get("cost_source") not in {
                "provider_reported",
                "provider_estimated",
                "estimated",
                "mixed",
            }:
                # Keep historical guesses available for auditing, never as a
                # price comparable with newly source-labelled measurements.
                legacy_cost = metrics.get("cost_usd")
                metrics.update(cost_usd=None, cost_source="unavailable", cost_complete=False)
            writer.writerow(
                {
                    "id": cell["id"],
                    "problem": cell["problem"]["id"],
                    "condition": cell["condition"],
                    "status": cell["status"],
                    "verified": cell["verified"],
                    "runtime_sha256": cell.get("runtime_sha256", ""),
                    "stop_code": (cell.get("stop_reason") or {}).get("code", ""),
                    "stop_scope": (cell.get("stop_reason") or {}).get("scope", ""),
                    "legacy_unverified_cost_usd": legacy_cost,
                    **metrics,
                }
            )
    temporary.replace(directory / "metrics.csv")


def refresh(cell: dict[str, Any]) -> None:
    """Copy controller evidence without equating a job completion with a verified proof."""
    if not cell.get("project"):
        return
    path = Path(cell["project"]) / ".leanflow/workflow-state/prover" / cell["run_id"] / "state.json"
    if not path.exists():
        return
    try:
        state = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        cell["refresh_error"] = str(exc)
        return
    cell.pop("refresh_error", None)
    cell["controller_status"] = state.get("status")
    cell["phase"] = state.get("phase")
    cell["stop_reason"] = state.get("stop_reason")
    cell["metrics"] = state.get("metrics", {})
    cell["verification"] = state.get("verification")
    cell["verified"] = bool(
        state.get("status") == "completed"
        and state.get("terminal")
        and (state.get("verification") or {}).get("accepted") is True
    )
    cell["active_jobs"] = [
        {
            k: j.get(k)
            for k in ("id", "role", "kind", "node_id", "status", "api_calls", "api_budget")
        }
        for j in state.get("jobs", [])
        if j.get("status") in {"running", "queued"}
    ]
    cell["node_counts"] = {
        status: sum(n.get("status") == status for n in state.get("dag", {}).get("nodes", []))
        for status in (
            "proved",
            "candidate",
            "conditional",
            "running",
            "pending",
            "retry",
            "blocked",
            "failed",
            "false",
            "submitted",
            "verifying",
            "integrating",
        )
    }
    roots = set(state.get("dag", {}).get("roots", []))
    proved = {
        node.get("id")
        for node in state.get("dag", {}).get("nodes", [])
        if node.get("status") == "proved"
    }
    cell["root_proofs_ready"] = bool(roots) and roots <= proved
    if state.get("error"):
        cell["error"] = state["error"]


def launch(
    directory: Path, campaign: dict[str, Any], cell: dict[str, Any]
) -> subprocess.Popen[bytes]:
    """Persist admission before spawning exactly one independent campaign process."""
    snapshot = (
        runtime_directory(directory, cell)
        if cell.get("runtime_directory")
        else Path(campaign.get("default_runtime_directory", directory)).resolve()
    )
    identity = json.loads((snapshot / "provenance.json").read_text())
    cell.update(runtime_directory=str(snapshot), runtime_sha256=identity["runtime_sha256"])
    cell.update(
        status="preparing",
        started_at=now(),
        started_epoch=time.time(),
        run_id="bench-"
        + cell["id"].lower()
        + (f"-r{cell['recovery_attempts']}" if cell.get("resume_run_id") else ""),
    )
    report(directory, campaign)
    root = Path(cell["project"]) if cell.get("resume_run_id") else prepare(directory, cell)
    cell.pop("controller_status", None)
    cell.pop("error", None)
    cell["project"] = str(root)
    cell["status"] = "running"
    cell["started_epoch"] = time.time() - float(cell.get("metrics", {}).get("elapsed_s", 0))
    report(directory, campaign)
    bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('scripts.lean_imo_campaign.worker',run_name='__main__')"
    argv = [
        str(snapshot / "python-env/bin/python"),
        "-I",
        "-B",
        "-u",
        "-c",
        bootstrap,
        str(snapshot / "runtime"),
        str(directory),
        cell["id"],
    ]
    log_directory = directory / "cell-logs"
    log_directory.mkdir(exist_ok=True)
    cell["log"] = str(log_directory / f"{cell['run_id']}.log")
    with Path(cell["log"]).open("wb") as output:
        process = subprocess.Popen(
            argv,
            cwd=root,
            env=environment(snapshot),
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    cell["pid"] = process.pid
    try:
        cell["process_identity"] = process_identity(process.pid)
    except (OSError, RuntimeError) as exc:
        # The child already exists; keep monitoring it even if ps is unavailable.
        cell["process_identity_error"] = str(exc)
    report(directory, campaign)
    return process


def run(directory: Path, *, adopt_active: bool = False) -> None:
    """Advance completed lanes; pause dispatch on infrastructure errors without resetting cells."""
    with (directory / "runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        campaign = json.loads((directory / "campaign.json").read_text())
        # A crashed dispatcher must not spawn replacements while its children may survive.
        if not adopt_active and any(
            c["status"] in {"running", "preparing"} for c in campaign["cells"]
        ):
            raise ValueError(
                "Existing active admissions require reconciliation; no cell was restarted"
            )
        active: dict[int, tuple[dict[str, Any], subprocess.Popen[bytes] | AdoptedProcess]] = {}
        if adopt_active:
            for cell in campaign["cells"]:
                if cell["status"] == "preparing":
                    raise ValueError("Cannot adopt an unfinished preparation")
                if cell["status"] == "running":
                    lane = cell["lane"]
                    if lane not in (1, 2) or lane in active:
                        raise ValueError("Invalid active lane assignments")
                    refresh(cell)
                    active[lane] = (cell, AdoptedProcess(cell))
        campaign.update(status="running", runner_pid=os.getpid(), dispatcher_argv=sys.argv)
        dispatcher_snapshot = Path(__file__).resolve().parents[3]
        if (dispatcher_snapshot / "provenance.json").is_file():
            campaign["dispatcher_runtime_directory"] = str(dispatcher_snapshot)
            campaign["dispatcher_runtime_sha256"] = json.loads(
                (dispatcher_snapshot / "provenance.json").read_text()
            )["runtime_sha256"]
        paused = False

        def pause(_signum: int, _frame: Any) -> None:
            nonlocal paused
            paused = True

        signal.signal(signal.SIGTERM, pause)
        signal.signal(signal.SIGINT, pause)
        while True:
            paused = paused or (directory / "PAUSE_AFTER_ACTIVE").exists()
            for lane, (cell, process) in list(active.items()):
                refresh(cell)
                # The controller enforces eight active hours; this bounds a hung process too.
                if (
                    process.poll() is None
                    and time.time() > cell["started_epoch"] + cell["config"]["wall_time_s"] + 120
                ):
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    cell["watchdog_timeout"] = True
                code = process.poll()
                if code is None:
                    continue
                refresh(cell)
                if isinstance(process, AdoptedProcess):
                    code = 0 if cell["verified"] else 1
                cell.update(
                    returncode=code,
                    finished_at=now(),
                    status=cell.get("controller_status") or "environment_error",
                )
                if cell.get("watchdog_timeout"):
                    cell.update(status="watchdog_timeout", verified=False)
                if cell["status"] in {"running", "completed"} and not cell["verified"]:
                    cell["status"] = "unverified_exit"
                recovered = schedule_recovery(cell) if not paused else False
                if not recovered and requires_inspection(cell):
                    paused = True
                    campaign["pause_reason"] = (
                        f"Inspect {cell['id']}: {cell['status']}. Existing runs continue; new dispatch is paused."
                    )
                del active[lane]
                report(directory, campaign)
            if not paused:
                for lane in (1, 2):
                    if lane in active:
                        continue
                    candidate = next_cell(campaign["cells"], lane)
                    if candidate is not None:
                        if time.time() < candidate.get("not_before", 0):
                            continue
                        try:
                            active[lane] = (candidate, launch(directory, campaign, candidate))
                        except Exception as exc:
                            candidate.update(
                                status="environment_error", error=str(exc), finished_at=now()
                            )
                            campaign["pause_reason"] = str(exc)
                            paused = True
                            break
            campaign["status"] = (
                "draining"
                if paused and active
                else (
                    "paused"
                    if paused
                    else (
                        "running"
                        if active
                        else (
                            "waiting"
                            if any(c["status"] == "pending" for c in campaign["cells"])
                            else "completed"
                        )
                    )
                )
            )
            report(directory, campaign)
            if not active and campaign["status"] != "waiting":
                return
            time.sleep(5)


def main() -> None:
    """Create a frozen experiment or run its pending cells without silently recreating it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--adopt-active", action="store_true")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--conditions",
        choices=sorted(CONDITION_SETS),
        default="full",
        help="Which comparison arms to freeze (prepare only; default: full)",
    )
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.command == "run":
        run(directory, adopt_active=args.adopt_active)
        return
    from leanflow_cli.runtime.runtime_provider import resolve_runtime_provider

    directory.mkdir(parents=True, exist_ok=False)
    repo = args.repo.resolve()
    fixture = repo / "testdata/workflow_projects/LeanIMOBench"
    identity = freeze(repo, fixture, directory)
    manifest = json.loads((directory / "baseline/manifest.json").read_text())
    provider = resolve_runtime_provider(requested="openai-codex")
    conditions = CONDITION_SETS[args.conditions]
    campaign = {
        "version": 1,
        "name": f"Lean-IMO-Bench · LEAP-unsolved · {args.conditions} · 2 lanes",
        "condition_set": args.conditions,
        "conditions": [c._asdict() for c in conditions],
        "created_at": now(),
        "status": "prepared",
        "provider_base_url": provider["base_url"].rstrip("/"),
        "provenance": identity,
        "cells": cells([p for p in manifest["problems"] if p["leap_solved"] is False], conditions),
    }
    report(directory, campaign)
    print(directory, flush=True)


if __name__ == "__main__":
    main()
