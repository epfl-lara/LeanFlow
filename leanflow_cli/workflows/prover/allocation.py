"""Wait for parallel allocations without discarding queued mathematical results."""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, wait
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.models import Node
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def wait_for_capacity(runtime: ProverRuntime, role: str, node: Node | None, prompt: str) -> None:
    """Reconcile completed workers before calling a temporarily reserved budget exhausted.

    Only the owning controller waits, outside its lock. A prover requesting a
    nested research job must never wait on its own future. Results remain queued
    for normal independent verification and DAG integration in arrival order.
    """
    if threading.get_ident() != runtime.controller_thread:
        return
    resumed = (
        runtime.resume_role_jobs.get((role, prompt))
        if role in {"orchestrator", "review", "research"}
        else (runtime.resume_negation_jobs if role == "negation" else runtime.resume_jobs).get(
            node.id if node else ""
        )
    )
    minimum = max(1, resumed["api_budget"] - resumed["api_calls"]) if resumed else 1
    while True:
        with runtime.lock:
            runtime._ensure_active()
            if runtime.config.total_api_calls - runtime.consumed - runtime.reserved >= minimum:
                return
            outstanding = {
                future: job for future, job in runtime.pending.items() if not job.get("accounted")
            }
        if not outstanding:
            return
        runtime._messages()
        done, _ = wait(outstanding, timeout=0.5, return_when=FIRST_COMPLETED)
        for future in done:
            runtime._finish_job(outstanding[future], future.result())
