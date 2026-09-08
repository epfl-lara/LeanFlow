"""Explain terminal limits without confusing job exhaustion with campaign capacity."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def job_stop_reason(job: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    """Describe a single allocation's stop without declaring the campaign exhausted."""
    if isinstance(result.get("stop_reason"), dict):
        return dict(result["stop_reason"])
    status = result.get("status")
    if status not in {"budget_exhausted", "timeout", "context_limit"}:
        return None
    calls = status == "budget_exhausted"
    return {
        "code": "job_api_calls" if calls else "job_" + str(status),
        "scope": "job",
        "job_id": job["id"],
        "node_id": job.get("node_id", ""),
        "message": (
            "This job used its call allocation."
            if calls
            else "This job reached its " + str(status) + " limit."
        ),
        "used": job.get("api_calls") if calls else None,
        "limit": job.get("api_budget") if calls else None,
        "next_step": "The controller evaluates saved progress for a bounded restart or branch replanning; this does not exhaust the campaign by itself.",
    }


def campaign_stop_reason(runtime: ProverRuntime, status: str) -> dict[str, Any] | None:
    """Return the exact stopping scope and preserved obligations for the final report."""
    if status == "completed":
        return None
    metrics = runtime.state["metrics"]
    used = metrics.get("api_calls", 0)
    if status not in {"budget_exhausted", "blocked", "timeout"}:
        code, scope, message, used, limit = (
            status,
            "campaign",
            str(runtime.state.get("error") or status.replace("_", " ")),
            None,
            None,
        )
    elif runtime._elapsed() >= runtime.config.wall_time_s:
        code, scope, message, used, limit = (
            "campaign_wall_time",
            "campaign",
            "The campaign reached its total active-time limit.",
            round(runtime._elapsed(), 3),
            runtime.config.wall_time_s,
        )
    elif used >= runtime.config.total_api_calls:
        code, scope, message, limit = (
            "campaign_api_calls",
            "campaign",
            "The campaign used its total call budget.",
            runtime.config.total_api_calls,
        )
    elif status in {"budget_exhausted", "blocked"}:
        # Every unproved obligation is blocked -- its recovery budget is spent or
        # the orchestrator's replan was declined -- while the total call budget
        # may still have room. Preserve that distinction in the UI.
        code, scope, message, used, limit = (
            "no_runnable_obligations",
            "scheduler",
            "No runnable obligation remains: every unproved node is blocked (recovery budget spent or replanning declined) while the total call budget is not exhausted.",
            None,
            None,
        )
    else:
        code, scope, message, used, limit = (
            status,
            "campaign",
            str(runtime.state.get("error") or status.replace("_", " ")),
            None,
            None,
        )
    return {
        "code": code,
        "scope": scope,
        "message": message,
        "used": used,
        "limit": limit,
        "remaining_api_calls": max(0, runtime.config.total_api_calls - metrics.get("api_calls", 0)),
        "unresolved_nodes": [node.id for node in runtime.dag.nodes if node.status != "proved"],
        "next_step": runtime.state.get("next_step")
        or (
            "Inspect the saved node reports and plan. Resume preserves spent calls and completed proofs; an exhausted allocation is not reset."
            if scope == "scheduler"
            else "Inspect the recorded cause and saved progress. Resume preserves consumed calls and active time."
        ),
    }
