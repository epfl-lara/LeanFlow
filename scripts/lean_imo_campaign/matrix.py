"""Define the fixed comparison and advance two independent problem lanes."""

from __future__ import annotations

from typing import Any, NamedTuple


class Budget(NamedTuple):
    """Set optional global limits for one complete cell, including resumed calls."""

    parallelism: int = 4
    agent_calls: int | None = None
    tokens: int | None = None
    wall_time_s: float | None = None


WIDE = Budget()


class Condition(NamedTuple):
    """Define explicit model and effort comparisons; empty efforts use native defaults."""

    label: str
    model: str
    order: str
    prover_effort: str = ""
    orchestrator_effort: str = ""
    #: Empty means the planning roles share the prover's model. Set it to run
    #: planning and proving on different models.
    orchestrator_model: str = ""
    budget: Budget = WIDE
    provider: str = "codex"
    orchestrator_provider: str = ""


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

#: The same split-effort question asked of gpt-5.6-luna, at the astra budget so
#: the two arms differ only by model and prover effort. The prover runs at medium
#: rather than low because luna is the smaller model: holding the effort label
#: fixed across models would compare two different things, so the arm holds the
#: *role* fixed -- cheap prover, expensive planner.
LUNA_TOP_DOWN_SPLIT_EFFORT = (
    Condition("luna-top-split", "gpt-5.6-luna", "top-down", "medium", "xhigh"),
)

#: Planning and proving on different models. astra planned every one of the
#: eighteen proofs the top-down split arm completed, while luna's planning
#: livelocked on two cells and talked itself out of a true statement on a third;
#: luna's provers, meanwhile, closed nodes whenever a plan reached them. This
#: arm buys astra's planning for luna's proving and measures whether that is
#: where the difference lives.
ASTRA_PLAN_LUNA_PROVE = (
    Condition(
        label="astra-plan-luna-prove",
        model="gpt-5.6-luna",
        order="top-down",
        prover_effort="medium",
        orchestrator_effort="medium",
        orchestrator_model="gpt-6-astra",
        budget=WIDE,
    ),
)

#: The same configuration asked bottom-up, so search order is the only thing that
#: differs from the arm that proved PB-Basic-025/026/028/029 top-down.
ASTRA_PLAN_LUNA_PROVE_BOTTOM_UP = (
    Condition(
        label="astra-plan-luna-prove-bottom",
        model="gpt-5.6-luna",
        order="bottom-up",
        prover_effort="medium",
        orchestrator_effort="medium",
        orchestrator_model="gpt-6-astra",
        budget=WIDE,
    ),
)

ASTRA_LOW_LUNA_HIGH_TOP = (
    ASTRA_PLAN_LUNA_PROVE[0]._replace(
        label="astra-low-luna-high-top",
        prover_effort="high",
        orchestrator_effort="low",
    ),
)

RCP_FLASH_BUDGET = WIDE._replace(parallelism=1)


def rcp_flash_condition(label: str, model: str) -> tuple[Condition, ...]:
    """Run external models through Lemex with one prover invocation per cell at a time."""
    return (
        Condition(
            label=label,
            model=model,
            order="top-down",
            prover_effort="medium",
            orchestrator_effort="low",
            orchestrator_model="gpt-6-astra",
            budget=RCP_FLASH_BUDGET,
            provider="lemex",
            orchestrator_provider="codex",
        ),
    )


CONDITION_SETS = {
    "kimi-glm-flash-top": (
        Condition(
            label="kimi-glm-flash-top",
            model="zai-org/GLM-5.3-Flash",
            order="top-down",
            prover_effort="high",
            orchestrator_effort="high",
            orchestrator_model="moonshotai/Kimi-K2.7-Code",
            budget=WIDE,
            provider="lemex",
        ),
    ),
    "full": CONDITIONS,
    "top-down-split": TOP_DOWN_SPLIT_EFFORT,
    "luna-top-down-split": LUNA_TOP_DOWN_SPLIT_EFFORT,
    "astra-plan-luna-prove": ASTRA_PLAN_LUNA_PROVE,
    "astra-plan-luna-prove-bottom": ASTRA_PLAN_LUNA_PROVE_BOTTOM_UP,
    "astra-low-luna-high-top": ASTRA_LOW_LUNA_HIGH_TOP,
    "astra-low-glm-flash-top": rcp_flash_condition(
        "astra-low-glm-flash-top", "zai-org/GLM-5.3-Flash"
    ),
    "astra-low-deepseek-flash-top": rcp_flash_condition(
        "astra-low-deepseek-flash-top", "deepseek-ai/DeepSeek-V4-Flash-0731"
    ),
}


def configuration(
    model: str,
    order: str,
    prover_effort: str = "",
    orchestrator_effort: str = "",
    budget: Budget = WIDE,
    orchestrator_model: str = "",
    provider: str = "codex",
    orchestrator_provider: str = "",
) -> dict[str, Any]:
    """Return every prover setting explicitly, avoiding mutable home profile defaults."""
    from leanflow_cli.workflows.prover.config import ProverConfig

    return ProverConfig(
        mode="research",
        search_order=order,
        model=model,
        orchestrator_model=orchestrator_model or model,
        provider=provider,
        orchestrator_provider=orchestrator_provider,
        reasoning_effort=prover_effort,
        orchestrator_reasoning_effort=orchestrator_effort,
        **budget._asdict(),
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
            "orchestrator_model": condition.orchestrator_model or condition.model,
            "order": condition.order,
            # The launch effort the native runtime starts at. Per-role efforts
            # live in "config" and win over this wherever both apply.
            "effort": condition.orchestrator_effort,
            "config": configuration(
                condition.model,
                condition.order,
                condition.prover_effort,
                condition.orchestrator_effort,
                condition.budget,
                condition.orchestrator_model,
                condition.provider,
                condition.orchestrator_provider,
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
