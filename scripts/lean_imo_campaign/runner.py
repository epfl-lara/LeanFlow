"""Persist and run the requested two-lane, 72-cell Lean-IMO-Bench comparison."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.lean_imo_campaign.artifacts import environment, freeze, prepare, save
from scripts.lean_imo_campaign.matrix import cells, next_cell
from scripts.lean_imo_campaign.recovery import schedule_recovery


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
        "elapsed_s",
        "plan_refinements",
        "decompositions",
    ]
    temporary = directory / "metrics.csv.tmp"
    with temporary.open("w") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["id", "problem", "condition", "status", "verified", *keys]
        )
        writer.writeheader()
        for cell in campaign["cells"]:
            writer.writerow(
                {
                    "id": cell["id"],
                    "problem": cell["problem"]["id"],
                    "condition": cell["condition"],
                    "status": cell["status"],
                    "verified": cell["verified"],
                    **{key: cell["metrics"].get(key) for key in keys},
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
        for status in ("proved", "conditional", "running", "pending", "failed")
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
        str(directory / "python-env/bin/python"),
        "-I",
        "-B",
        "-u",
        "-c",
        bootstrap,
        str(directory / "runtime"),
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
            env=environment(directory),
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    cell["pid"] = process.pid
    report(directory, campaign)
    return process


def run(directory: Path) -> None:
    """Advance completed lanes; pause dispatch on infrastructure errors without resetting cells."""
    with (directory / "runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        campaign = json.loads((directory / "campaign.json").read_text())
        # A crashed dispatcher must not spawn replacements while its children may survive.
        if any(c["status"] in {"running", "preparing"} for c in campaign["cells"]):
            raise ValueError(
                "Existing active admissions require reconciliation; no cell was restarted"
            )
        active: dict[int, tuple[dict[str, Any], subprocess.Popen[bytes]]] = {}
        campaign.update(status="running", runner_pid=os.getpid())
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
                if process.poll() is None and time.time() > cell["started_epoch"] + 28800 + 120:
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
                if not recovered and cell["status"] in {
                    "environment_error",
                    "error",
                    "provider_error",
                    "infrastructure_error",
                    "source_conflict",
                    "unverified_exit",
                    "watchdog_timeout",
                    "interrupted",
                }:
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
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.command == "run":
        run(directory)
        return
    from leanflow_cli.runtime.runtime_provider import resolve_runtime_provider

    directory.mkdir(parents=True, exist_ok=False)
    repo = args.repo.resolve()
    fixture = repo / "testdata/workflow_projects/LeanIMOBench"
    identity = freeze(repo, fixture, directory)
    manifest = json.loads((directory / "baseline/manifest.json").read_text())
    provider = resolve_runtime_provider(requested="openai-codex")
    campaign = {
        "version": 1,
        "name": "Lean-IMO-Bench · LEAP-unsolved · 2 lanes",
        "created_at": now(),
        "status": "prepared",
        "provider_base_url": provider["base_url"].rstrip("/"),
        "provenance": identity,
        "cells": cells([p for p in manifest["problems"] if p["leap_solved"] is False]),
    }
    report(directory, campaign)
    print(directory, flush=True)


if __name__ == "__main__":
    main()
