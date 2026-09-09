"""Persist and run one frozen Lean-IMO-Bench comparison across its problem lanes."""

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


def lanes() -> tuple[int, ...]:
    """Lane ids to dispatch on, from LEANFLOW_CAMPAIGN_LANES (default 2).

    Lanes are pure scheduling: each cell still runs in its own project with its
    own frozen budget, and workers are always spawned from the cell's pinned
    runtime snapshot, so widening this changes throughput and machine load
    without touching any cell's configuration or comparability.
    """
    raw = os.environ.get("LEANFLOW_CAMPAIGN_LANES", "").strip()
    count = int(raw) if raw else 2
    if not 1 <= count <= 8:
        raise ValueError("LEANFLOW_CAMPAIGN_LANES must be between 1 and 8")
    return tuple(range(1, count + 1))


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


def resume_source(project: Path) -> str:
    """Name the run holding the most consumed calls, or "" when none is readable.

    A resume must continue the run that did the work. Directory age is the wrong
    signal: a relaunch that fails clones an OLD snapshot into a NEW directory, so
    the newest run is exactly the one that must not be resumed. Consumed calls
    only ever grow along a resume chain, so the highest count names the tip.
    """
    best, best_calls = "", -1
    runs = project / ".leanflow/workflow-state/prover"
    if not runs.is_dir():
        return ""
    for path in runs.iterdir():
        state = path / "state.json"
        if not path.is_dir() or path.name.endswith(".superseded") or not state.is_file():
            continue
        try:
            calls = int(json.loads(state.read_text()).get("metrics", {}).get("api_calls", 0))
        except (OSError, ValueError, TypeError):
            continue
        if calls > best_calls:
            best, best_calls = path.name, calls
    return best


def launch(
    directory: Path, campaign: dict[str, Any], cell: dict[str, Any]
) -> subprocess.Popen[bytes]:
    """Persist admission before spawning exactly one independent campaign process."""
    if cell.get("resume_run_id") and cell.get("project"):
        # A hand-requeued cell can name a run that later runs have moved past;
        # resuming it would roll proved work back to that snapshot and then stop
        # on a source conflict against the newer helpers on disk. Continue the
        # tip instead, and leave the correction visible in the cell record.
        tip = resume_source(Path(cell["project"]))
        if tip and tip != cell["resume_run_id"]:
            cell["resume_source_corrected_from"] = cell["resume_run_id"]
            cell["resume_run_id"] = tip
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
        # Lanes are a claim, not a preference: once a problem is claimed by lane
        # k every cell of it keeps lane=k for the campaign's life. The dispatch
        # loop only iterates `lanes()`, so narrowing LEANFLOW_CAMPAIGN_LANES
        # below a lane that already owns pending cells would strand them --
        # the campaign would sit in "waiting" forever, never dispatching them.
        # Widening is always safe; refuse to narrow below the highest claim.
        dispatch_lanes = set(lanes())
        stranded = sorted(
            {
                c["lane"]
                for c in campaign["cells"]
                if c.get("lane") is not None and c["lane"] not in dispatch_lanes
            }
        )
        if stranded:
            raise ValueError(
                f"LEANFLOW_CAMPAIGN_LANES={len(dispatch_lanes)} would strand cells already "
                f"claimed by lane(s) {stranded}; lanes may be widened but never narrowed "
                f"below the highest lane a cell has claimed."
            )
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
                    if lane not in lanes() or lane in active:
                        raise ValueError("Invalid active lane assignments")
                    refresh(cell)
                    active[lane] = (cell, AdoptedProcess(cell))
        campaign.update(
            status="running",
            runner_pid=os.getpid(),
            dispatcher_argv=sys.argv,
            dispatcher_lanes=list(lanes()),
        )
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
                for lane in lanes():
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
    parser.add_argument(
        "--problems",
        default="",
        help=(
            "Comma-separated problem IDs to run (prepare only; default: all 18 "
            "LEAP-unsolved). The fixture still freezes all 18 statements, so a "
            "subset campaign stays comparable with a full one."
        ),
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
    unsolved = [p for p in manifest["problems"] if p["leap_solved"] is False]
    selected = unsolved
    if args.problems:
        wanted = {name.strip() for name in args.problems.split(",") if name.strip()}
        unknown = sorted(wanted - {p["id"] for p in unsolved})
        if unknown:
            raise ValueError(f"not LEAP-unsolved problem ids: {unknown}")
        selected = [p for p in unsolved if p["id"] in wanted]
    campaign = {
        "version": 1,
        # Lanes are a run-time scheduling choice (LEANFLOW_CAMPAIGN_LANES),
        # not part of what was frozen, so the name must not claim a count.
        "name": f"Lean-IMO-Bench · LEAP-unsolved · {args.conditions}",
        "condition_set": args.conditions,
        "problem_subset": sorted(p["id"] for p in selected) if args.problems else None,
        "conditions": [c._asdict() for c in conditions],
        "created_at": now(),
        "status": "prepared",
        "provider_base_url": provider["base_url"].rstrip("/"),
        "provenance": identity,
        "cells": cells(selected, conditions),
    }
    report(directory, campaign)
    print(directory, flush=True)


if __name__ == "__main__":
    main()
