"""Run a bounded exact-negation attempt without editing the original declaration."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.lean.lean_declarations import declaration_region
from leanflow_cli.workflows.prover.check_failures import infrastructure_code
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.negation import NegationTask, prepare_negation
from leanflow_cli.workflows.prover.planning import json_report
from leanflow_cli.workflows.prover.source import (
    SourceDocument,
    extract_scratch_replacements,
    read_source,
    sorry_spans,
    validate_hole_replacement,
    write_source,
)

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def attempt_negation(
    runtime: ProverRuntime, node: Node, *, screen: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Return certified evidence only after the controller checks the exact closed negation."""
    task = prepare_negation(runtime.root, node)
    if task is None:
        return {
            "certified": False,
            "notes": "Exact negation context is unsupported; use a structural split.",
        }
    # A proof extracted on a previous attempt is cached only across the
    # verification window (set just before verifier.check, cleared just after).
    # Reusing it lets a crash DURING verification re-verify for free instead of
    # buying a whole new negation model allocation.
    proof_cache = runtime.state.setdefault("negation_proofs", {})
    cached_proof = proof_cache.get(node.id)
    if isinstance(cached_proof, str):
        # A recorded outcome from an interrupted attempt. A nonempty proof is
        # re-verified for free; an empty marker means a completed model attempt
        # produced no usable proof, so the model is NOT re-invoked on resume (a
        # completed negation job is not re-queued, so re-invoking would buy a
        # whole fresh allocation to redo work that already finished).
        if cached_proof.strip():
            return _certify_negation(
                runtime,
                node,
                task,
                cached_proof,
                {"notes": "reused a cached negation proof after an interrupted check"},
            )
        return {
            "certified": False,
            "notes": "a completed negation attempt produced no usable proof (recovered without rerunning the model)",
        }

    prompt = (
        runtime._prover_prompt(task.node)
        + "\nThis assignment proves the exact negation of the original claim. The original sorry theorem is never evidence."
    )
    if screen is not None:
        if screen.get("found") is True:
            prompt += (
                "\nAn empirical Plausible screen FOUND a counterexample; turn it into the witness:\n"
                + str(screen.get("detail", ""))[:2000]
            )
        elif screen.get("found") is False:
            prompt += (
                "\nAn empirical Plausible screen found NO counterexample in random testing; a "
                "refutation, if one exists, needs a structured witness rather than a small case."
            )
        else:
            prompt += (
                "\nThe empirical Plausible screen was inconclusive: "
                + str(screen.get("detail", ""))[:800]
            )
    job, context = runtime._new_job("negation", node=node, prompt=prompt)
    scratch = Path(job["scratch_path"])
    if job.get("resumed"):
        task = NegationTask(
            node=Node(**job["negation_assignment"]), source=runtime.scratch_before[job["id"]]
        )
    else:
        write_source(scratch, task.source)
        baseline = scratch.parent.parent / ".runtime" / scratch.parent.name / "source-before.lean"
        write_source(baseline, task.source)
        runtime.scratch_before[job["id"]] = task.source
        job["negation_assignment"] = task.node.to_dict()
        job["scratch_holes"] = task.node.holes
    context.update(
        assignment=task.node.to_dict(),
        scratch_holes=task.node.holes,
        scratch_declaration=str(
            (declaration_region(scratch, task.node.name) or {}).get("text", "")
        )[:16000],
    )
    runtime._persist()
    result = runtime._invoke(job, context, prompt)
    report = json_report(str(result.get("final_response", "")))
    proof = report.get("proof")
    candidate_path = Path(job["workspace"]) / "candidate.txt"
    if not isinstance(proof, str) and candidate_path.is_file() and not candidate_path.is_symlink():
        proof = read_source(candidate_path)
    if not isinstance(proof, str):
        recovered = extract_scratch_replacements(task.source, read_source(scratch), task.node.holes)
        proof = recovered[0] if recovered else ""
    usable = bool(proof.strip()) and not sorry_spans(proof)
    # Record the outcome (the proof, or "" as a no-proof marker) BEFORE finishing
    # the job, so finish_job's persist commits the terminal status and the
    # outcome together. A completed negation job is not re-queued for resume, so
    # otherwise a crash between finishing and certifying would re-invoke the
    # model from scratch. Skip an infrastructure failure: that job stays
    # resumable and must be retried, not recorded as a definitive no-proof.
    if str(result.get("status", "")) not in {
        "provider_error",
        "environment_error",
        "error",
        "source_conflict",
        "interrupted",
    }:
        runtime.state.setdefault("negation_proofs", {})[node.id] = proof if usable else ""
    runtime._finish_job(job, result)
    # A cancelled/interrupted session finishes WITHOUT raising (finish_job only
    # sets cancelled+stopping for status "interrupted"). Bail before the caller
    # consumes the outcome, so the negate stage does not persist a spurious
    # "unrefuted" advance (stage=decide, charged=False) that would make resume
    # re-charge and re-decide instead of continuing the still-resumable job.
    runtime._ensure_active()
    runtime._assert_sources()
    if not usable:
        return {
            "certified": False,
            "notes": str(report.get("notes", result.get("final_response", ""))),
        }
    return _certify_negation(runtime, node, task, proof, report)


def _certify_negation(
    runtime: ProverRuntime,
    node: Node,
    task: NegationTask,
    proof: str,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    """Kernel-check one negation proof. The cached proof is cleared by the recovery
    state machine (atomically with recording the outcome), not here."""
    try:
        validate_hole_replacement(proof)
    except ValueError as error:
        # A completed model attempt whose proof is unusable: record the no-proof
        # marker rather than clearing the cache, so a crash here does not let
        # resume re-buy a whole negation allocation to redo a finished attempt.
        # The recovery state machine clears the marker as it advances the stage.
        runtime.state.setdefault("negation_proofs", {})[node.id] = ""
        runtime._persist()
        return {"certified": False, "notes": "Rejected negation replacement: " + str(error)}
    checked_path = runtime.store.directory / "checks" / f"{task.node.id}.lean"
    checked_path.parent.mkdir(exist_ok=True)
    document = SourceDocument(node.file, task.source)
    write_source(checked_path, document.render({task.node.holes[0]: proof}))
    # Persist the proof across the check, so an interruption here replays the
    # verification rather than re-running the model.
    runtime.state.setdefault("negation_proofs", {})[node.id] = proof
    runtime._persist()
    checked = runtime.verifier.check(task.node, checked_path)
    runtime._assert_sources()
    if checked.get("accepted") is not True and infrastructure_code(checked):
        # The independent check could not COMPLETE (timeout/cancel/env) -- that is
        # not a refutation verdict. The proof stays cached (persisted just above),
        # so resume RE-VERIFIES it for free instead of the negate stage reading
        # this as an unrefuted negation, dropping the proof and re-deciding.
        from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

        runtime._ensure_active()
        raise InfrastructureFailure(
            "Negation verification environment unavailable: " + str(checked.get("error", checked)),
            status="environment_error",
        )
    # Do NOT clear the cached proof here: the recovery state machine clears it
    # atomically as it records this outcome and advances the node's stage. A
    # clear in a separate persist would let a crash between the two re-run the
    # whole negation model job on resume.
    runtime.store.event("negation_checked", {"node_id": node.id, "result": checked})
    return {
        "certified": checked.get("accepted") is True,
        "notes": str(report.get("notes", "")),
        "evidence_path": str(checked_path),
        "verification": checked,
    }
