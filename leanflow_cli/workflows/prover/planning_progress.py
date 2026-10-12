"""Checkpoint rejected planning attempts and stop repeated unchanged failures."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.planning import json_report

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


REPEATED_FAILURE_LIMIT = 3


def restore_legacy_progress(runtime: ProverRuntime, checkpoint: dict[str, Any]) -> None:
    """Skip only old attempts whose successor proposal was already allocated.

    Earlier checkpoints lack rejection cursors. A later proposal step proves
    that every earlier draft was rejected; the newest step itself may still be
    interrupted and must replay normally. Do not infer completion from phase or
    a stale proposal_status field.
    """
    if "progress" in checkpoint:
        return
    steps = checkpoint.get("steps", {})
    attempts = [
        int(step.removeprefix("proposal-"))
        for step in steps
        if step.startswith("proposal-") and step.removeprefix("proposal-").isdigit()
    ]
    current = max(attempts, default=0)
    if not current:
        return
    previous_id = steps.get(f"proposal-{current - 1}")
    previous = next((job for job in runtime.state["jobs"] if job["id"] == previous_id), None)
    if previous is None:
        return
    try:
        result = json.loads((Path(previous["workspace"]) / "result.json").read_text())
        if not isinstance(result, dict):
            return
    except (OSError, ValueError):
        return
    checkpoint["progress"] = {
        "next_attempt": current,
        "previous_proposal": json_report(str(result.get("final_response", ""))),
        "critique": str(runtime.state.get("proposal_critique", "")),
        "failure_counts": {},
    }
    runtime._persist()


def reject_attempt(
    runtime: ProverRuntime,
    checkpoint: dict[str, Any],
    attempt: int,
    proposal: dict[str, Any],
    *,
    stage: str,
    critique: str,
    finding: str,
    malformed: bool = False,
) -> None:
    """Persist a rejection once, then stop when its unchanged failure repeats.

    The full proposal participates in the key, so changed mathematical plans
    remain eligible even when their reviewer gives the same critique. Malformed
    report failures share a stage-local key: changing unparseable text does not
    establish that the report protocol has recovered.
    """
    progress = checkpoint.setdefault("progress", {})
    if attempt >= progress.get("next_attempt", 0):
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "stage": stage,
                    "proposal": None if malformed else proposal,
                    "critique": "malformed report" if malformed else critique,
                },
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        counts = progress.setdefault("failure_counts", {})
        repeats = counts[fingerprint] = int(counts.get(fingerprint, 0)) + 1
        progress.update(
            next_attempt=attempt + 1,
            previous_proposal=copy.deepcopy(proposal),
            critique=critique,
        )
        checkpoint.pop("materialization", None)
        if repeats >= REPEATED_FAILURE_LIMIT:
            progress["stalled"] = {
                "stage": stage,
                "attempt": attempt,
                "repeats": repeats,
                "critique": critique,
            }
        runtime.state.update(proposal_status="rejected", proposal_critique=critique)
        runtime.record_finding(finding, critique)
        runtime.store.event(
            "plan_rejected", {"reason": critique, "stage": stage, "attempt": attempt}
        )
        # Advancing the cursor, recording the finding, and setting any stall
        # marker share a checkpoint. Resume cannot charge the same rejection or
        # repeat an already failed materialization after this persist.
        runtime._persist()
    ensure_not_stalled(runtime, checkpoint)


def ensure_not_stalled(runtime: ProverRuntime, checkpoint: dict[str, Any]) -> None:
    """Require an explicit correction before resuming a repeatedly failed plan."""
    stalled = checkpoint.get("progress", {}).get("stalled")
    if not isinstance(stalled, dict):
        return
    from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

    runtime.state["next_step"] = (
        "Planning repeated an unchanged failure. Inspect the saved proposals and review or "
        "validation reports, correct the cause, and explicitly clear planning_request.progress.stalled "
        "before resuming. The checkpoint, rejected drafts, consumed calls, and verified proofs "
        "remain preserved; resuming without a correction does not start another model job."
    )
    runtime._persist()
    raise InfrastructureFailure(
        f"Planning stalled after {stalled['repeats']} repeated {stalled['stage']} failures: "
        + str(stalled["critique"]),
        status="planning_stalled",
    )
