"""Keep unfinished research obligations under orchestrator control until a real limit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.models import Node
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def restore_legacy_stops(runtime: ProverRuntime) -> None:
    """Reopen retired stop decisions on explicit resume without replaying valid actions."""
    if runtime.config.mode != "research":
        return
    for node in runtime.dag.nodes:
        if node.status != "blocked" or node.candidate:
            continue
        journal = runtime.state.get("recovery_in_flight", {}).get(node.id)
        if journal is not None and not (
            journal.get("stage") == "done"
            and (journal.get("decision") or {}).get("action") == "stop"
        ):
            continue
        latest = next(
            (
                item
                for item in reversed(runtime.state.get("recovery_decisions", []))
                if item.get("node_id") == node.id
                and item.get("node_revision", node.revision) == node.revision
            ),
            None,
        )
        if journal is None and (latest is None or latest.get("action") != "stop"):
            continue
        previous = (journal or {}).get("decision") or latest or {}
        correction = {
            "code": "retired_stop_decision",
            "rejected_action": "stop",
            "message": "The previous stop decision left this obligation unfinished. Choose an actionable recovery; stop is no longer permitted.",
            "previous_rationale": str(previous.get("rationale", "")),
        }
        if journal is None:
            runtime._enqueue_recovery(
                node,
                {"notes": node.notes, "progress": False, "recovery_correction": correction},
            )
        else:
            journal.update(stage="decide", charged=False, decision=None)
            journal.pop("decision_result", None)
            journal["report"] = {
                **dict(journal.get("report") or {}),
                "recovery_correction": correction,
            }
            runtime._persist()


def recover_idle_obligation(runtime: ProverRuntime) -> bool:
    """Give one stranded live obligation to the orchestrator before the scheduler exits.

    Only inspect dependencies of unfinished targets, dependency-first. Checked
    nodes and retained candidates are never turned into fresh prover attempts.
    Recovery exhaustion waits until active work is drained, preserving already
    authorized independent jobs instead of cancelling them early.
    """
    from leanflow_cli.workflows.prover.runtime import BudgetExhausted, InfrastructureFailure

    index = runtime.dag.by_id()
    if all(index[root].status == "proved" for root in runtime.dag.roots):
        return False
    runtime._ensure_active()
    if runtime.consumed >= runtime.config.total_api_calls:
        raise BudgetExhausted("The campaign used its total call budget.")
    used = int(runtime.state["metrics"]["decompositions"])
    if used >= runtime.config.max_decompositions:
        raise BudgetExhausted(
            f"Campaign recovery budget exhausted ({used}/{runtime.config.max_decompositions}).",
            code="campaign_recoveries",
        )
    ordered: list[Node] = []
    seen: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        node = index[node_id]
        if node.status == "proved":
            return
        for dep in node.dependencies:
            visit(dep)
        ordered.append(node)

    for root in runtime.dag.roots:
        visit(root)
    for node in ordered:
        if node.status in {"proved", "false"} or node.candidate:
            continue
        report: dict[str, Any] = {
            "status": "scheduler_recovery",
            "progress": False,
            "attempts": node.attempts,
            "notes": node.notes,
            "scheduler": {
                "node_status": node.status,
                "dependencies": {dep: index[dep].status for dep in node.dependencies},
                "message": "No prover is active and the obligation is unfinished. Choose a concrete recovery action.",
            },
        }
        runtime._recover(node, report)
        return True
    raise InfrastructureFailure(
        "The research scheduler has unresolved targets but no recoverable obligation; retained candidates and certified refutations were preserved.",
        status="scheduler_error",
    )
