"""Let the orchestrator choose how a blocked obligation recovers.

After a prover job fails to close a node, the orchestrator reads the prover's
report and picks exactly one action: retry the node as it stands, attempt a
refutation, or decompose the branch. Every decision spends one unit of the
campaign-wide recovery budget; there is deliberately no per-node cap.

A refutation runs an empirical Plausible screen first -- a few Lean calls and
no API calls -- and only then a bounded proof of the exact negation. The
screen is advisory: only a kernel-accepted negation ever marks a node false.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.negation import negation_context
from leanflow_cli.workflows.prover.planning import json_report

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.models import Node
    from leanflow_cli.workflows.prover.runtime import ProverRuntime

ACTIONS: tuple[str, ...] = ("retry", "negate", "decompose")
#: When the orchestrator's reply carries no usable action, replanning is the
#: safest generic move: it neither burns a refutation budget nor repeats a
#: failed attempt unchanged.
FALLBACK_ACTION = "decompose"
#: Plausible itself is quick; elaborating a Mathlib-heavy prefix is the cost.
SCREEN_TIMEOUT_S = 600.0


def parse_decision(text: str) -> dict[str, Any]:
    """Read the orchestrator's JSON decision, falling back rather than guessing."""
    report = json_report(text)
    action = str(report.get("action", "")).strip().lower()
    rationale = str(report.get("rationale", "")).strip()
    if action in ACTIONS:
        return {"action": action, "rationale": rationale, "fallback": False}
    return {
        "action": FALLBACK_ACTION,
        "rationale": rationale or "the orchestrator returned no usable action",
        "fallback": True,
        "raw": text[:500],
    }


def parse_screen(result: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a Plausible run into found / not found / inconclusive.

    Plausible never closes the goal, so the ephemeral check "fails" either way;
    the verdict lives in its printed ``output`` (lean_ephemeral_source_check
    leaves ``messages`` empty). ``found`` is True with a counterexample, False when
    random testing exhausted its samples, and None when Lean could not even run
    the test (no Testable instance, a timeout, an unrelated error).
    """
    messages = result.get("messages") or []
    blob = "\n".join(
        text
        for text in [str(result.get("output") or "")]
        + [str(item.get("message", "")) for item in messages if isinstance(item, Mapping)]
        if text
    )
    if "Found a counter-example" in blob:
        return {"found": True, "detail": blob[-2000:]}
    if "Unable to find a counter-example" in blob:
        return {"found": False, "detail": blob[-2000:]}
    detail = blob or str(result.get("error") or "no Plausible verdict")
    return {"found": None, "detail": detail[-2000:]}


def empirical_screen(
    runtime: ProverRuntime, node: Node, *, timeout_s: float | None = None
) -> dict[str, Any]:
    """Ask Plausible for a counterexample to the node's closed proposition."""
    from leanflow_cli.lean.lean_ephemeral import lean_ephemeral_source_check

    limit = int(timeout_s or min(float(runtime.config.timeout_s), SCREEN_TIMEOUT_S))
    try:
        context = negation_context(runtime.root, node)
        if context is None:
            return {
                "found": None,
                "detail": "statement context unsupported for an exact negation",
            }
        scratch = (
            context.prefix
            + "set_option autoImplicit false in\nexample : "
            + context.goal.prop
            + " := by\n  plausible\n"
        )
        result = lean_ephemeral_source_check(scratch, cwd=runtime.root, timeout_s=limit)
        screen = parse_screen(result)
    except Exception as exc:  # noqa: BLE001 - advisory: no screen failure may end recovery
        return {"found": None, "detail": f"screen failed: {exc}"[:2000]}
    with contextlib.suppress(Exception):
        runtime.store.event("negation_screen", {"node_id": node.id, **screen})
    return screen


#: Session statuses that mean the turn did not complete normally and must stay
#: resumable rather than being recorded as a finished outcome.
_INFRA_FAILURE_STATUSES = frozenset(
    {"provider_error", "environment_error", "error", "source_conflict", "interrupted"}
)


def recovery_decision(
    runtime: ProverRuntime, node: Node, report: Mapping[str, Any]
) -> dict[str, Any]:
    """One bounded orchestrator turn: choose retry, negate, or decompose.

    The reply is cached into the node's recovery journal atomically with
    finishing the job, so a crash between the turn completing and the decide
    stage recording it REPLAYS the same decision on resume instead of buying a
    second orchestrator allocation (a finished job is not re-queued for resume).
    An infrastructure failure is never cached: that job stays resumable and its
    turn is continued.
    """
    metrics = runtime.state["metrics"]
    history = [
        item
        for item in runtime.state.get("recovery_decisions", [])
        if item.get("node_id") == node.id
    ]
    remaining_recoveries = runtime.config.max_decompositions - int(metrics.get("decompositions", 0))
    prior = (
        "\n".join(f"  - {item['action']}: {item.get('rationale', '')}" for item in history)
        or "  (none)"
    )
    negation = report.get("last_negation")
    prompt = (
        "You are the LeanFlow research orchestrator. The prover has just failed to close ONE "
        "obligation. Read its report and choose exactly one recovery action. Do not prove anything "
        "yourself.\n\n"
        "Actions:\n"
        "- retry: run the prover again on this exact statement with the current plan. Choose it "
        "when the report shows a concrete near-miss that another focused pass can finish.\n"
        f"- negate: an empirical Plausible screen, then a bounded attempt "
        f"({runtime.config.negation_api_calls} calls) to PROVE the exact negation. Choose it when "
        "you genuinely suspect the obligation is false or a witness looks reachable.\n"
        "- decompose: replan this branch -- split it into helper obligations or change direction. "
        "Choose it when the statement is true but too large to close directly.\n\n"
        f"Every decision spends one unit of the campaign recovery budget: {remaining_recoveries} of "
        f"{runtime.config.max_decompositions} remain. A prover retry costs up to "
        f"{runtime.config.job_api_calls} calls; a refutation up to "
        f"{runtime.config.negation_api_calls}.\n\n"
        f"Obligation: {node.name}\n{node.statement[:1500]}\n\n"
        f"Prover attempts so far: {node.attempts}. "
        f"Last attempt made new partial progress: {bool(report.get('progress'))}. "
        f"Refutations attempted: {int(report.get('negations_attempted', 0))}.\n"
        + (
            f"Last refutation: screen={negation.get('screen')} notes={negation.get('notes', '')}\n"
            if isinstance(negation, Mapping)
            else ""
        )
        + f"Prior recovery decisions for this obligation:\n{prior}\n\n"
        f"Prover report:\n{str(report.get('notes', node.notes))[-4000:]}\n\n"
        'Respond with JSON only: {"action": "retry" | "negate" | "decompose", "rationale": "..."}'
    )
    rec = runtime.state.get("recovery_in_flight", {}).get(node.id)
    if isinstance(rec, dict) and isinstance(rec.get("decision_result"), str):
        # A completed-but-unrecorded turn from before a crash: replay it.
        return parse_decision(rec["decision_result"])
    if not isinstance(rec, dict):
        result = runtime._run_role(
            "orchestrator", prompt, node=node, context_extra={"recovery_decision": node.id}
        )
        return parse_decision(str(result.get("final_response", "")))
    # Same lifecycle as run_role, but cache the reply into the journal BEFORE
    # finishing the job so the terminal status and the decision persist together.
    job, context = runtime._new_job("orchestrator", node=node, prompt=prompt)
    context.update({"recovery_decision": node.id})
    result = runtime._invoke(job, context, prompt)
    if str(result.get("status", "")) not in _INFRA_FAILURE_STATUSES:
        rec["decision_result"] = str(result.get("final_response", ""))
    runtime._finish_job(job, result)
    runtime._ensure_active()
    return parse_decision(str(result.get("final_response", "")))
