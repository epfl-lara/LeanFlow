"""Recover submissions that older controllers misclassified during infrastructure outages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.check_failures import infrastructure_code

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def recover_environment_candidates(runtime: ProverRuntime) -> None:
    """Retain the latest completed submission after an infrastructure-only gate failure.

    A later empty retry must not hide an earlier proof. Saved acceptance is never
    reused: these candidates must pass the current independent verifier again.
    Keep every admission ledger and attempt count, including wasted retries.
    """
    if runtime.state.get("status") not in {"environment_error", "interrupted"}:
        return
    path = runtime.store.directory / "events.jsonl"
    if not path.is_file():
        return
    latest: dict[str, dict[str, Any]] = {}
    with path.open() as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "candidate_checked":
                latest[str(event.get("node_id", ""))] = event
    eligible = {
        node_id
        for node_id, event in latest.items()
        if event.get("accepted") is False and infrastructure_code(event.get("result"))
    }
    if runtime.state.get("status") == "interrupted":
        # Older promotion loops cleared the candidate before entering the
        # verifier. KeyboardInterrupt ends that operation without a verdict or
        # error string, leaving the completed job as its only durable copy.
        checks = {
            op.get("node_id"): op
            for op in runtime.state.get("operations", [])
            if op.get("kind") == "submission_check"
        }
        eligible.update(
            node_id
            for node_id, op in checks.items()
            if op.get("status") == "failed"
            and not op.get("error")
            and isinstance(op.get("started_at"), str)
            and latest.get(node_id, {}).get("time", "") < op["started_at"]
        )
    for job in reversed(runtime.state.get("jobs", [])):
        node = runtime.dag.by_id().get(job.get("node_id", ""))
        if (
            node is None
            or node.id not in eligible
            or node.candidate
            or node.status == "proved"
            or job.get("role") != "prover"
            or job.get("status") != "completed"
            or not job.get("accounted")
            or job.get("node_revision") != node.revision
        ):
            continue
        result_path = Path(job.get("result_path", ""))
        if not result_path.is_file():
            continue
        runtime._retain_candidate(job, json.loads(result_path.read_text()))
        if node.candidate:
            runtime.store.event(
                "candidate_recovered",
                {"node_id": node.id, "job_id": job["id"], "reason": "infrastructure_failure"},
            )
