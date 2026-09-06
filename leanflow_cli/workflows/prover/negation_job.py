"""Run a bounded exact-negation attempt without editing the original declaration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.lean.lean_declarations import declaration_region
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.negation import NegationTask, prepare_negation
from leanflow_cli.workflows.prover.planning import json_report
from leanflow_cli.workflows.prover.source import (
    SourceDocument,
    extract_scratch_replacements,
    read_source,
    sorry_spans,
    write_source,
)

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def attempt_negation(runtime: ProverRuntime, node: Node) -> dict[str, Any]:
    """Return certified evidence only after the controller checks the exact closed negation."""
    task = prepare_negation(runtime.root, node)
    if task is None:
        return {
            "certified": False,
            "notes": "Exact negation context is unsupported; use a structural split.",
        }
    prompt = (
        runtime._prover_prompt(task.node)
        + "\nThis assignment proves the exact negation of the original claim. The original sorry theorem is never evidence."
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
    runtime._finish_job(job, result)
    runtime._assert_sources()
    report = json_report(str(result.get("final_response", "")))
    proof = report.get("proof")
    candidate_path = Path(job["workspace"]) / "candidate.txt"
    if not isinstance(proof, str) and candidate_path.is_file() and not candidate_path.is_symlink():
        proof = read_source(candidate_path)
    if not isinstance(proof, str):
        recovered = extract_scratch_replacements(task.source, read_source(scratch), task.node.holes)
        proof = recovered[0] if recovered else ""
    if not proof.strip() or sorry_spans(proof):
        return {
            "certified": False,
            "notes": str(report.get("notes", result.get("final_response", ""))),
        }
    checked_path = runtime.store.directory / "checks" / f"{task.node.id}.lean"
    checked_path.parent.mkdir(exist_ok=True)
    document = SourceDocument(node.file, task.source)
    write_source(checked_path, document.render({task.node.holes[0]: proof}))
    checked = runtime.verifier.check(task.node, checked_path)
    runtime._assert_sources()
    runtime.store.event("negation_checked", {"node_id": node.id, "result": checked})
    return {
        "certified": checked.get("accepted") is True,
        "notes": str(report.get("notes", "")),
        "evidence_path": str(checked_path),
        "verification": checked,
    }
