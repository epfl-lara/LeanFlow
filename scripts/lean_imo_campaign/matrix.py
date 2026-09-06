"""Define the fixed comparison and advance two independent problem lanes."""

from __future__ import annotations

from typing import Any

CONDITIONS = (
    ("astra-bottom", "gpt-6-astra", "bottom-up"),
    ("astra-top", "gpt-6-astra", "top-down"),
    ("terra-bottom", "gpt-5.6-terra", "bottom-up"),
    ("terra-top", "gpt-5.6-terra", "top-down"),
)


def configuration(model: str, order: str) -> dict[str, Any]:
    """Return every prover setting explicitly, avoiding mutable home profile defaults."""
    from leanflow_cli.workflows.prover.config import ProverConfig

    return ProverConfig(
        mode="research",
        search_order=order,
        model=model,
        orchestrator_model=model,
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


def cells(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create four fresh conditions for each unsolved problem in manifest order."""
    return [
        {
            "id": f"{problem['id']}-{label}",
            "problem": problem,
            "condition": label,
            "model": model,
            "order": order,
            "effort": "xhigh",
            "config": configuration(model, order),
            "status": "pending",
            "lane": None,
            "metrics": {},
            "verified": False,
        }
        for problem in problems
        for label, model, order in CONDITIONS
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
