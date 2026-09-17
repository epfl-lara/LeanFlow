"""Hand stalled attempts to durable orchestrator recovery without replaying failures."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.recovery_reports import recent_tool_evidence

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.models import Node
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def stalled_job(runtime: ProverRuntime, node: Node) -> dict[str, Any] | None:
    """Return a stalled latest attempt at this revision, never an older superseded one."""
    latest = next(
        (
            job
            for job in reversed(runtime.state.get("jobs", []))
            if job.get("node_id") == node.id
            and job.get("node_revision") == node.revision
            and job.get("role") in {"prover", "negation"}
        ),
        None,
    )
    return latest if latest is not None and latest.get("status") == "response_stalled" else None


def response_failure_report(
    runtime: ProverRuntime, node: Node, report: Mapping[str, Any]
) -> dict[str, Any]:
    """Attach saved failure evidence and mark admission for the caller's atomic persist.

    Never include raw reasoning. The orchestrator receives structured failure
    metadata, bounded written notes, and paths to the retained scratch and history.
    """
    enriched = dict(report)
    job = stalled_job(runtime, node)
    if job is None:
        return enriched
    workspace = Path(str(job.get("workspace", "")))
    plan = workspace / "PLAN_job.md"
    failure: dict[str, Any] = {
        "job_id": job["id"],
        "role": job["role"],
        "node_revision": node.revision,
        "status": "response_stalled",
        "api_calls": job.get("api_calls", 0),
        "stop_reason": dict(job.get("stop_reason") or {}),
        "workspace": str(workspace),
        "scratch_path": str(job.get("scratch_path", "")),
        "result_path": str(job.get("result_path", "")),
        "artifacts": list(job.get("artifacts") or []),
        "plan_path": str(plan),
        "recent_tool_results": recent_tool_evidence(workspace),
    }
    if plan.is_file() and not plan.is_symlink():
        try:
            with plan.open("rb") as stream:
                stream.seek(max(0, plan.stat().st_size - 12000))
                failure["plan_tail"] = stream.read(12000).decode("utf-8", errors="replace")
        except OSError:
            pass
    enriched.update(job_id=job["id"], response_failure=failure)
    enriched.setdefault("status", "response_stalled")
    enriched.setdefault("stop_reason", failure["stop_reason"])
    enriched.setdefault("api_calls", job.get("api_calls", 0))
    enriched.setdefault("notes", node.notes)
    job["response_recovery_enqueued"] = True
    job["result_processed"] = True
    return enriched


def restore_stalled_recoveries(runtime: ProverRuntime) -> None:
    """Migrate old terminal blocks once, preserving checked candidates and journal stages."""
    if runtime.config.mode != "research":
        return
    for node in runtime.dag.nodes:
        if node.status in {"proved", "false"} or node.candidate:
            continue
        job = stalled_job(runtime, node)
        if job is None or job.get("response_recovery_enqueued"):
            continue
        if node.id in runtime.state.get("recovery_in_flight", {}):
            # A crash may predate the migration marker while an existing journal
            # already owns a decision, a plan, or cached negation verification.
            job["response_recovery_enqueued"] = True
            job["result_processed"] = True
            continue
        runtime._enqueue_recovery(node, {"attempts": node.attempts, "progress": False})


def record_recovery_guidance(
    runtime: ProverRuntime, node: Node, decision: Mapping[str, Any], report: Mapping[str, Any]
) -> None:
    """Replace next-attempt guidance in the same transaction as the chosen decision."""
    guidance = runtime.state.setdefault("prover_recovery_guidance", {})
    if decision["action"] not in {"retry", "continue", "negate"}:
        guidance.pop(node.id, None)
        return
    guidance[node.id] = {
        "node_revision": node.revision,
        "action": decision["action"],
        "instructions": str(decision.get("instructions", "")),
        "source_job_id": str(report.get("job_id", "")),
        "rationale": str(decision.get("rationale", "")),
        "failure": {
            "job_id": str(report.get("job_id", "")),
            "status": str(
                report.get("response_failure", {}).get("status", report.get("status", ""))
            ),
            "stop_reason": dict(
                report.get("response_failure", {}).get("stop_reason", report.get("stop_reason", {}))
                or {}
            ),
        },
        "decision_index": len(runtime.state.get("recovery_decisions", [])) - 1,
    }


def attach_recovery_guidance(
    runtime: ProverRuntime, job: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Snapshot applicable instructions into the exact bounded worker assignment.

    Keep instructions separate from truncatable notes, and preserve the chosen
    snapshot when the same job resumes after later controller updates.
    """
    allowed = {"prover": {"retry", "continue"}, "negation": {"negate"}}.get(job["role"], set())
    if not allowed:
        return context
    with runtime.lock:
        guidance = job.get("recovery_guidance")
        if guidance is None:
            guidance = runtime.state.get("prover_recovery_guidance", {}).get(job["node_id"])
        if (
            not isinstance(guidance, dict)
            or guidance.get("node_revision") != job["node_revision"]
            or guidance.get("action") not in allowed
        ):
            return context
        if "recovery_guidance" not in job:
            job["recovery_guidance"] = copy.deepcopy(guidance)
            runtime._persist()
        return {**context, "recovery_guidance": copy.deepcopy(guidance)}
