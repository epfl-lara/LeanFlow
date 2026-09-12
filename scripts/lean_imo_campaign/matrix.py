"""Define the fixed comparison and advance two independent problem lanes."""

from __future__ import annotations

from typing import Any, NamedTuple


class Budget(NamedTuple):
    """One arm's spending limits, stated in full so no arm inherits a default."""

    job_api_calls: int
    orchestrator_api_calls: int
    total_api_calls: int
    parallelism: int
    wall_time_s: int
    timeout_s: int


#: The limits every arm runs under. Named and shared rather than hardcoded in
#: configuration(), so an arm that needs different ceilings states them instead
#: of silently rewriting everyone else's.
WIDE = Budget(
    job_api_calls=200,
    orchestrator_api_calls=50,
    total_api_calls=2000,
    parallelism=4,
    wall_time_s=28800,
    timeout_s=1200,
)


#: Calibrated for luna's provers by the first astra-planned run rather than
#: chosen. Across 87 observed prover passes no successful one ever exceeded 164
#: calls (p50 34, p90 111), while 37% ground to the old 200 cap and consumed 65%
#: of all prover spend, so 150 keeps essentially every productive pass and
#: returns the rest to replanning. The four proofs that arm produced needed
#: 687-3162 calls, so the original 2000 ceiling was well short of what luna needs
#: to finish a problem astra closes in ~175.
LUNA_CALIBRATED = Budget(
    job_api_calls=150,
    orchestrator_api_calls=50,
    total_api_calls=5000,
    parallelism=4,
    wall_time_s=28800,
    timeout_s=1200,
)


class Condition(NamedTuple):
    """One comparison arm. Empty efforts inherit the launch reasoning effort."""

    label: str
    model: str
    order: str
    prover_effort: str = ""
    orchestrator_effort: str = ""
    #: Empty means the planning roles share the prover's model. Set it to run
    #: planning and proving on different models.
    orchestrator_model: str = ""
    budget: Budget = WIDE
    provider: str = ""
    base_url: str = ""
    api_key_env: str = ""
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
        budget=LUNA_CALIBRATED,
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
        budget=LUNA_CALIBRATED,
    ),
)

RCP_FLASH_BUDGET = LUNA_CALIBRATED._replace(parallelism=1)


def rcp_flash_condition(label: str, model: str) -> tuple[Condition, ...]:
    """Keep one RCP prover per cell so two lanes use at most two key requests."""
    return (
        Condition(
            label=label,
            model=model,
            order="top-down",
            prover_effort="medium",
            orchestrator_effort="low",
            orchestrator_model="gpt-6-astra",
            budget=RCP_FLASH_BUDGET,
            provider="rcp",
            base_url="https://inference.rcp.epfl.ch/v1",
            api_key_env="RCP_API_KEY",
            orchestrator_provider="openai-codex",
        ),
    )


CONDITION_SETS = {
    "full": CONDITIONS,
    "top-down-split": TOP_DOWN_SPLIT_EFFORT,
    "luna-top-down-split": LUNA_TOP_DOWN_SPLIT_EFFORT,
    "astra-plan-luna-prove": ASTRA_PLAN_LUNA_PROVE,
    "astra-plan-luna-prove-bottom": ASTRA_PLAN_LUNA_PROVE_BOTTOM_UP,
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
    provider: str = "",
    base_url: str = "",
    api_key_env: str = "",
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
        base_url=base_url,
        api_key_env=api_key_env,
        orchestrator_provider=orchestrator_provider,
        reasoning_effort=prover_effort,
        orchestrator_reasoning_effort=orchestrator_effort,
        **budget._asdict(),
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
            "orchestrator_model": condition.orchestrator_model or condition.model,
            "order": condition.order,
            # The launch effort the native runtime starts at. Per-role efforts
            # live in "config" and win over this wherever both apply.
            "effort": condition.orchestrator_effort or "xhigh",
            "config": configuration(
                condition.model,
                condition.order,
                condition.prover_effort,
                condition.orchestrator_effort,
                condition.budget,
                condition.orchestrator_model,
                condition.provider,
                condition.base_url,
                condition.api_key_env,
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
