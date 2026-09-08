"""Check exact negation submissions and return repair diagnostics within one job."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.check_failures import infrastructure_code
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.negation import NegationTask
from leanflow_cli.workflows.prover.session_context import tool_result_message
from leanflow_cli.workflows.prover.source import SourceDocument, write_source

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


SUBMISSION_CONTRACT = (
    "Negation submission format: the assigned literal sorry is inside an existing `:= by` "
    "tactic block. Submit only the tactics replacing that sorry, without another leading `by`, "
    "in candidate_path or JSON proof. To use a complete proof term, submit the explicit tactic "
    "`exact (<proof term>)`, for example `exact (by intro h; exact h)`. Preserve the surrounding "
    "scratch source. The controller checks those exact rendered bytes and returns any rejection "
    "for repair within this same allocation."
)


def rejection_notes(report: Mapping[str, Any], checked: Mapping[str, Any]) -> str:
    """Preserve model notes and nested checker diagnostics for the recovery decision."""
    parts = [str(report.get("notes", "")).strip()]

    def collect(value: Any) -> None:
        """Keep diagnostic text in nested compiler and inspector reports."""
        if isinstance(value, dict):
            for key in ("error", "message", "stderr", "stdout", "error_code"):
                text = value.get(key)
                if isinstance(text, str) and text.strip() and text.strip() not in parts:
                    parts.append(text.strip())
            for key in ("messages", "compile", "inspect", "kernel_profile"):
                collect(value.get(key))
        elif isinstance(value, list):
            for item in value:
                collect(item)

    if checked.get("accepted") is not True:
        collect(dict(checked))
        parts.append(
            "No certified refutation was obtained; this is not evidence that the claim is true."
        )
    return "\n".join(part for part in parts if part)[:12000]


def render_submission(task: NegationTask, proof: str) -> str:
    """Render one literal replacement without altering its term or tactic syntax."""
    return SourceDocument(task.node.file, task.source).render({task.node.holes[0]: proof})


def candidate_feedback(
    runtime: ProverRuntime, job: dict[str, Any], response: str
) -> dict[str, Any]:
    """Check the closed negation in its saved context before ending the session."""
    with runtime.lock:
        node = runtime.dag.by_id().get(job["node_id"])
        if node is None or node.revision != job["node_revision"]:
            return {
                "accepted": False,
                "error": "The assigned DAG node changed; preserve progress and stop this stale assignment.",
            }
        task = NegationTask(Node(**job["negation_assignment"]), runtime.scratch_before[job["id"]])
        candidate, report = runtime._candidate(job, {"final_response": response}, task.node)
        if not candidate:
            result = {
                "accepted": False,
                "error": str(
                    report.get("notes") or "Submit one nonempty, safe literal-sorry replacement."
                ),
            }
            job.pop("negation_pending_submission", None)
            job["negation_submission_feedback"] = result
            runtime._persist()
            return {**result, "submission_format": SUBMISSION_CONTRACT}
        runtime._assert_sources()
        source = render_submission(task, candidate[0])
        # This belongs to the still-running job, not the terminal proof cache.
        # Persist before checking so a final-call JSON candidate can be rechecked
        # after an interrupted verifier without buying another model allocation.
        job["negation_pending_submission"] = {
            "proof": candidate[0],
            "notes": str(report.get("notes", "")),
        }
        runtime._persist()
    workspace = runtime.store.directory / "checks" / job["id"]
    workspace.mkdir(parents=True, exist_ok=True)
    file = workspace / "Submission.lean"
    write_source(file, source)
    job["phase"] = "verifying"
    result = runtime.progress.call(
        "submission_check",
        "Checking submitted negation proof",
        lambda: runtime.verifier.check(task.node, file),
        job_id=job["id"],
        node_id=node.id,
        file=str(file),
        timeout_s=runtime.config.timeout_s,
    )
    job["phase"] = "submitted" if result.get("accepted") is True else "working"
    runtime.progress.publish()
    if result.get("accepted") is not True and infrastructure_code(result):
        from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

        runtime._ensure_active()
        raise InfrastructureFailure(
            "Negation verification environment unavailable: " + str(result.get("error", result)),
            status="environment_error",
        )
    with runtime.lock:
        runtime._assert_sources()
        job["negation_submission_feedback"] = result
        if result.get("accepted") is not True:
            job.pop("negation_pending_submission", None)
        runtime.store.event(
            "negation_submission_checked",
            {"job_id": job["id"], "node_id": node.id, "result": result},
        )
        runtime._persist()
    feedback, artifact = tool_result_message(result, Path(job["workspace"]))
    if artifact is not None:
        job.setdefault("artifacts", []).append(str(artifact))
    return {
        "accepted": result.get("accepted") is True,
        "feedback": feedback,
        "evidence_path": str(file),
        "submission_format": SUBMISSION_CONTRACT,
    }
