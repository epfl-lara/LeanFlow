"""Account for campaign-wide native sessions separately for each agent role."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.config import ProverConfig
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


class RoleSessions(TypedDict):
    """Expose started sessions and unstarted reservations without double counting."""

    used: int
    reserved: int
    limit: int | None


ROLE_GROUPS = ("prover", "orchestrator", "negation")


def role_group(role: str) -> str:
    """Charge planning, review, and resource research to the orchestrator allowance."""
    return "orchestrator" if role in {"orchestrator", "review", "research"} else role


def session_metrics(jobs: list[dict[str, Any]], config: ProverConfig) -> dict[str, RoleSessions]:
    """Derive durable session spending and live reservations from the job journal.

    The legacy api_calls field records native invocations in version-two runs.
    Resuming a job extends its cumulative count; it does not create free capacity.
    """
    metrics: dict[str, RoleSessions] = {
        group: {"used": 0, "reserved": 0, "limit": getattr(config, group + "_sessions")}
        for group in ROLE_GROUPS
    }
    for job in jobs:
        group = role_group(str(job.get("role", "")))
        if group not in metrics:
            continue
        calls = int(job.get("api_calls", 0) or 0)
        metrics[group]["used"] += calls
        if job.get("status") == "running" and not job.get("accounted"):
            started = max(0, calls - int(job.get("previous_api_calls", 0)))
            metrics[group]["reserved"] += max(0, int(job.get("reserved_calls", 0)) - started)
    return metrics


def has_capacity(runtime: ProverRuntime, role: str) -> bool:
    """Return whether one more native session fits the requested role's allowance."""
    metrics = session_metrics(runtime.state["jobs"], runtime.config).get(role_group(role))
    return (
        metrics is None
        or metrics["limit"] is None
        or metrics["used"] + metrics["reserved"] < metrics["limit"]
    )


def require_capacity(runtime: ProverRuntime, role: str) -> None:
    """Reject exhausted role allowances before taking a global reservation.

    Call while holding the runtime lock, before publishing the reserved job.
    Unknown native launch outcomes remain charged by the existing durable ledger;
    proven startup failures and preparation failures retain their capacity.
    """
    from leanflow_cli.workflows.prover.runtime import BudgetExhausted

    group = role_group(role)
    metrics = session_metrics(runtime.state["jobs"], runtime.config).get(group)
    if metrics is None or metrics["limit"] is None:
        return
    if metrics["used"] + metrics["reserved"] >= metrics["limit"]:
        raise BudgetExhausted(
            f"{group.capitalize()} campaign session limit reached: "
            f"{metrics['used']} started, {metrics['reserved']} reserved, limit {metrics['limit']}.",
            code=group + "_sessions",
        )


def stop_details(runtime: ProverRuntime) -> RoleSessions | None:
    """Return the exhausted role's current accounting for the terminal explanation."""
    code = runtime.state.get("stop_code", "")
    if code not in {group + "_sessions" for group in ROLE_GROUPS}:
        return None
    return session_metrics(runtime.state["jobs"], runtime.config)[code.removesuffix("_sessions")]


def context_allowances(runtime: ProverRuntime) -> dict[str, dict[str, int | None]]:
    """Describe the allowance remaining after current reservations for agent guidance."""
    return {
        role: {
            "used": counts["used"],
            "reserved": counts["reserved"],
            "limit": counts["limit"],
            "remaining": (
                None
                if counts["limit"] is None
                else max(0, counts["limit"] - counts["used"] - counts["reserved"])
            ),
        }
        for role, counts in session_metrics(runtime.state["jobs"], runtime.config).items()
    }


def recovery_actions(runtime: ProverRuntime) -> dict[str, dict[str, Any]]:
    """Expose which recovery actions can start under the reserved role allowances.

    A fresh decomposition needs outline, proposal, and review sessions. An
    allowance that permits starting one does not promise enough for completion.
    """
    allowances = context_allowances(runtime)
    roles = {
        "retry": "prover",
        "continue": "prover",
        "negate": "negation",
        "decompose": "orchestrator",
    }
    return {
        action: {
            "role": role,
            "remaining_sessions": allowances[role]["remaining"],
            "can_start": allowances[role]["remaining"] != 0
            and not (
                action == "decompose"
                and getattr(runtime, "formalization_handoff", None) is not None
            ),
            "minimum_sessions_for_fresh_action": 3 if action == "decompose" else 1,
        }
        for action, role in roles.items()
    }
