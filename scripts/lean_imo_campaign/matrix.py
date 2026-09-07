"""Define the fixed comparison and advance two independent problem lanes."""

from __future__ import annotations

from typing import Any, NamedTuple


class Condition(NamedTuple):
    """One comparison arm. Empty efforts inherit the launch reasoning effort."""

    label: str
    model: str
    order: str
    prover_effort: str = ""
    orchestrator_effort: str = ""


#: The original four-arm comparison: two models x two search orders, every role
#: at the launch effort.
CONDITIONS = (
    Condition("astra-bottom", "gpt-6-astra", "bottom-up"),
    Condition("astra-top", "gpt-6-astra", "top-down"),
    Condition("terra-bottom", "gpt-5.6-terra", "bottom-up"),
    Condition("terra-top", "gpt-5.6-terra", "top-down"),
)

#: Single arm: top-down only, one model, but the planning/review/research roles
#: reason at xhigh while the prover and negation passes run at low. Isolates
#: "does expensive planning plus a cheap prover work?" from model choice.
TOP_DOWN_SPLIT_EFFORT = (Condition("astra-top-split", "gpt-6-astra", "top-down", "low", "xhigh"),)

CONDITION_SETS = {
    "full": CONDITIONS,
    "top-down-split": TOP_DOWN_SPLIT_EFFORT,
}


def configuration(
    model: str,
    order: str,
    prover_effort: str = "",
    orchestrator_effort: str = "",
) -> dict[str, Any]:
    """Return every prover setting explicitly, avoiding mutable home profile defaults."""
    from leanflow_cli.workflows.prover.config import ProverConfig

    return ProverConfig(
        mode="research",
        search_order=order,
        model=model,
        orchestrator_model=model,
        reasoning_effort=prover_effort,
        orchestrator_reasoning_effort=orchestrator_effort,
        parallelism=4,
        job_api_calls=200,
        orchestrator_api_calls=50,
        total_api_calls=2000,
        wall_time_s=28800,
        timeout_s=1200,
        context_tokens=64000,
        orchestrator_context_tokens=96000,
        max_restarts=3,
        plan_refinements=16,
        max_nodes=128,
        max_decompositions=32,
        compression=True,
        orchestrator_compression=True,
        fill_definitions=False,
        allow_internet=False,
    ).to_mapping()


def cells(
    problems: list[dict[str, Any]],
    conditions: tuple[Condition, ...] = CONDITIONS,
) -> list[dict[str, Any]]:
    """Create one fresh cell per condition for each problem, in manifest order."""
    return [
        {
            "id": f"{problem['id']}-{condition.label}",
            "problem": problem,
            "condition": condition.label,
            "model": condition.model,
            "order": condition.order,
            # The launch effort the native runtime starts at. Per-role efforts
            # live in "config" and win over this wherever both apply.
            "effort": condition.orchestrator_effort or "xhigh",
            "config": configuration(
                condition.model,
                condition.order,
                condition.prover_effort,
                condition.orchestrator_effort,
            ),
            "status": "pending",
            "lane": None,
            "metrics": {},
            "verified": False,
        }
        for problem in problems
        for condition in conditions
    ]


def next_cell(rows: list[dict[str, Any]], lane: int) -> dict[str, Any] | None:
    """Finish a lane's assigned problem before atomically claiming another problem."""
    if any(row["lane"] == lane and row["status"] in {"preparing", "running"} for row in rows):
        return None
    assigned = [row for row in rows if row["lane"] == lane and row["status"] == "pending"]
    if assigned:
        return assigned[0]
    unclaimed = next((row for row in rows if row["lane"] is None), None)
    if unclaimed is None:
        return None
    problem = unclaimed["problem"]["id"]
    for row in rows:
        if row["problem"]["id"] == problem:
            row["lane"] = lane
    return unclaimed
