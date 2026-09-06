"""Manage fixed-budget job workspaces, session accounting, and durable handoffs."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.utils import atomic_json_write
from leanflow_cli.lean.lean_declarations import declaration_region
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.runtime import BudgetExhausted, InfrastructureFailure
from leanflow_cli.workflows.prover.source import (
    declaration_source,
    extract_scratch_replacements,
    read_source,
    sorry_spans,
    write_source,
)
from leanflow_cli.workflows.prover.store import now

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def new_job(
    runtime: ProverRuntime, role: str, *, node: Node | None = None, prompt: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reserve a complete job allocation before allowing model work to start."""
    resumed_jobs = runtime.resume_negation_jobs if role == "negation" else runtime.resume_jobs
    if role in {"prover", "negation"} and node is not None and node.id in resumed_jobs:
        job = resumed_jobs[node.id]
        remaining = job["api_budget"] - job["api_calls"]
        job["reserved_calls"] = runtime._reserve(remaining)
        if job["reserved_calls"] != remaining:
            runtime.reserved -= job["reserved_calls"]
            raise BudgetExhausted("remaining campaign allocation cannot resume this job")
        job["previous_api_calls"] = job["api_calls"]
        for field in ("input_tokens", "output_tokens", "cost_usd"):
            job["previous_" + field] = job.get(field)
        job["resumed"] = True
        job["status"] = "running"
        workspace = Path(job["workspace"])
        baseline = workspace.parent / ".runtime" / workspace.name / "source-before.lean"
        if not baseline.is_file():
            runtime.reserved -= job["reserved_calls"]
            job["status"] = "interrupted"
            raise RuntimeError(
                "resumed scratch baseline is missing; preserved job cannot be safely resumed"
            )
        runtime.scratch_before[job["id"]] = read_source(baseline)
        context = runtime._context(node)
        context.update(
            job_id=job["id"],
            scratch_file=job["scratch_path"],
            candidate_path=str(workspace / "candidate.txt"),
            plan_path=str(workspace / "PLAN_job.md"),
            original_file=node.file,
            scratch_holes=job.get("scratch_holes", []),
            scratch_declaration=str(
                (declaration_region(Path(job["scratch_path"]), node.name) or {}).get("text", "")
            )[:16000],
        )
        resumed_jobs.pop(node.id)
        runtime._persist()
        return job, context
    budget = runtime._reserve(
        runtime.config.job_api_calls
        if role in {"prover", "negation"}
        else runtime.config.orchestrator_api_calls
    )
    job_id = f"{role}_{len(runtime.state['jobs']) + 1:05d}"
    workspace = runtime.store.directory / "jobs" / job_id
    try:
        workspace.mkdir(parents=True, exist_ok=False)
    except OSError:
        runtime.reserved -= budget
        raise
    log_path = workspace / "events.jsonl"
    context = runtime._context(node)
    context.update(job_id=job_id, candidate_path=str(workspace / "candidate.txt"))
    job = {
        "id": job_id,
        "agent_id": job_id,
        "node_id": node.id if node else "",
        "role": role,
        "status": "running",
        "api_calls": 0,
        "api_budget": budget,
        "reserved_calls": budget,
        "previous_api_calls": 0,
        "log_path": str(log_path),
        "scratch_path": "",
        "workspace": str(workspace),
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": None,
        "purpose": prompt,
        "node_revision": node.revision if node else 0,
        "started_at": now(),
    }
    if node:
        scratch = workspace / "Scratch.lean"
        before = declaration_source(runtime.documents[node.file], node)
        write_source(scratch, before)
        runtime.scratch_before[job_id] = before
        baseline = workspace.parent / ".runtime" / workspace.name / "source-before.lean"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        write_source(baseline, before)
        context["scratch_file"] = str(scratch)
        context["original_file"] = node.file
        context["plan_path"] = str(workspace / "PLAN_job.md")
        job["scratch_path"] = str(scratch)
        # Earlier proven holes no longer exist in rendered scratch; remap the target indices.
        open_holes = [
            i
            for i in range(len(sorry_spans(runtime.documents[node.file].baseline)))
            if str(i) not in runtime.documents[node.file].replacements
        ]
        context["scratch_holes"] = [open_holes.index(i) for i in node.holes]
        job["scratch_holes"] = context["scratch_holes"]
        previous = next(
            (
                item
                for item in reversed(runtime.state["jobs"])
                if item.get("node_id") == node.id
                and item.get("role") == "prover"
                and item.get("node_revision") == node.revision
            ),
            None,
        )
        if previous:
            prior_workspace = Path(previous["workspace"])
            prior_baseline = (
                prior_workspace.parent / ".runtime" / prior_workspace.name / "source-before.lean"
            )
            prior_scratch = Path(previous["scratch_path"])
            if (
                prior_baseline.is_file()
                and prior_scratch.is_file()
                and not prior_scratch.is_symlink()
            ):
                partial = extract_scratch_replacements(
                    read_source(prior_baseline),
                    read_source(prior_scratch),
                    list(previous.get("scratch_holes", [])),
                )
                if partial and len(partial) == len(node.holes):
                    write_source(
                        scratch,
                        declaration_source(runtime.documents[node.file], node, candidate=partial),
                    )
            prior_plan = prior_workspace / "PLAN_job.md"
            if prior_plan.is_file() and not prior_plan.is_symlink():
                write_source(workspace / "PLAN_job.md", read_source(prior_plan))
            context["previous_job"] = {
                "id": previous["id"],
                "workspace": str(prior_workspace),
                "report_path": previous.get("result_path", ""),
            }
    if node:
        context["scratch_declaration"] = str(
            (declaration_region(Path(job["scratch_path"]), node.name) or {}).get("text", "")
        )[:16000]
    runtime.state["jobs"].append(job)
    runtime._persist()
    return job, context


def session_event(
    runtime: ProverRuntime, job: dict[str, Any], kind: str, details: dict[str, Any]
) -> None:
    """Publish live session usage without allowing worker writes to PLAN or DAG."""
    with runtime.lock:
        if isinstance(details.get("api_calls"), int):
            job["api_calls"] = max(
                int(job.get("api_calls", 0)), min(job["api_budget"], details["api_calls"])
            )
        for field in ("input_tokens", "output_tokens"):
            if isinstance(details.get(field), int):
                job[field] = max(
                    int(job.get(field, 0)),
                    int(job.get("previous_" + field, 0) or 0) + details[field],
                )
        runtime.store.event(kind, {"job_id": job["id"], "node_id": job["node_id"], **details})
        runtime._refresh_metrics()
        snapshot = {**runtime.state, "dag": runtime.dag.to_dict(), "updated_at": now()}
        atomic_json_write(runtime.store.directory / "state.json", snapshot)
        if runtime.cancelled.is_set():
            raise RuntimeError("prover controller stopped the job")


def invoke(
    runtime: ProverRuntime, job: dict[str, Any], context: dict[str, Any], prompt: str
) -> dict[str, Any]:
    """Execute one isolated session and retain its typed result even on infrastructure failure."""
    try:
        settings = runtime.config.to_mapping(job["role"])
        settings["_cancelled"] = runtime.cancelled.is_set
        if job["role"] == "prover":
            settings["_research_job"] = lambda question: research_job(runtime, job, question)
            settings["_candidate_feedback"] = lambda response: candidate_feedback(
                runtime, job, response
            )
        settings["wall_time_s"] = max(1.0, runtime.config.wall_time_s - runtime._elapsed())
        return runtime.session(
            role=job["role"],
            prompt=prompt,
            project_root=runtime.root,
            workspace=Path(job["workspace"]),
            config=settings,
            api_budget=job["api_budget"],
            log_path=Path(job["log_path"]),
            context=context,
            on_event=lambda kind, details: runtime._session_event(job, kind, details),
        )
    except Exception as error:
        return {
            "status": error.status if isinstance(error, InfrastructureFailure) else "error",
            "final_response": str(error),
            "api_calls": job.get("api_calls", 0),
        }

    finally:
        if hasattr(runtime.verifier, "close"):
            runtime.verifier.close(runtime.store.directory / "checks" / job["id"])


def candidate_feedback(
    runtime: ProverRuntime, job: dict[str, Any], response: str
) -> dict[str, Any]:
    """Reject malformed or invalid submissions inside their existing request allocation."""
    with runtime.lock:
        node = runtime.dag.by_id().get(job["node_id"])
        if node is None or node.revision != job["node_revision"]:
            return {
                "accepted": False,
                "error": "The assigned DAG node changed; preserve progress and stop this stale assignment.",
            }
        candidate, _ = runtime._candidate(job, {"final_response": response}, node)
        if not candidate:
            return {
                "accepted": False,
                "error": f"Submit exactly {len(node.holes)} nonempty literal-sorry replacements as JSON proof/proofs, or edit only the assigned scratch holes. Submitted replacements may not contain sorry, admit, axioms, unsafe, or native_decide.",
            }
        runtime._assert_sources()
        checked_node = copy.deepcopy(node)
        unresolved = [
            dep for dep in node.dependencies if runtime.dag.by_id()[dep].status != "proved"
        ]
        source = declaration_source(runtime.documents[node.file], node, candidate=candidate)
    workspace = runtime.store.directory / "checks" / job["id"]
    workspace.mkdir(parents=True, exist_ok=True)
    file = workspace / "Submission.lean"
    write_source(file, source)
    result = runtime.verifier.check(checked_node, file, skeleton=bool(unresolved))
    if result.get("error_code") in {
        "isolation_unavailable",
        "check_setup_failed",
        "lean_interact_start_failed",
        "lean_probe_unavailable",
        "local_repl_missing",
    }:
        raise InfrastructureFailure(
            "Lean verification environment unavailable: " + str(result.get("error", result)),
            status="environment_error",
        )
    with runtime.lock:
        runtime._assert_sources()
        runtime.store.event(
            "submission_checked",
            {
                "job_id": job["id"],
                "node_id": node.id,
                "accepted": bool(result.get("accepted")),
                "conditional": bool(unresolved),
                "result": result,
            },
        )
    return {
        "accepted": result.get("accepted") is True,
        "conditional": bool(unresolved),
        "feedback": json.dumps(result, default=str)[-16000:],
    }


def finish_job(runtime: ProverRuntime, job: dict[str, Any], result: dict[str, Any]) -> None:
    """Reconcile reserved requests using the session's actual monotonic request count."""
    with runtime.lock:
        calls = max(int(job.get("api_calls", 0)), int(result.get("api_calls", 0) or 0))
        if calls > job["api_budget"]:
            raise RuntimeError("session exceeded its reserved API allocation")
        runtime.reserved -= job.get("reserved_calls", job["api_budget"])
        runtime.consumed += calls - int(job.get("previous_api_calls", 0))
        job.update(status=str(result.get("status", "error")), api_calls=calls, finished_at=now())
        for field in ("input_tokens", "output_tokens", "cost_usd", "report_path", "artifacts"):
            if field in result:
                if field in {"input_tokens", "output_tokens"}:
                    job[field] = int(job.get("previous_" + field, 0) or 0) + int(result[field] or 0)
                elif field == "cost_usd" and result[field] is not None:
                    job[field] = float(job.get("previous_cost_usd", 0) or 0) + float(result[field])
                else:
                    job[field] = result[field]
        report_path = Path(job["workspace"]) / "result.json"
        atomic_json_write(report_path, result)
        job["result_path"] = str(report_path)
        runtime.store.event(
            "job_finished", {"job_id": job["id"], "status": job["status"], "api_calls": calls}
        )
        runtime._persist()
        if (
            result.get("status") in {"provider_error", "environment_error", "error"}
            and not runtime.cancelled.is_set()
        ):
            node = runtime.dag.by_id().get(job["node_id"])
            if node is not None and job["role"] == "prover":
                node.status = "retry"
            raise InfrastructureFailure(
                str(
                    result.get("error")
                    or result.get("final_response")
                    or "Provider session failed; its work and remaining budget were preserved"
                ),
                status=(
                    "environment_error"
                    if result.get("status") == "environment_error"
                    else "provider_error"
                ),
            )


def run_role(
    runtime: ProverRuntime,
    role: str,
    prompt: str,
    *,
    node: Node | None = None,
    context_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job, context = runtime._new_job(role, node=node, prompt=prompt)
    context.update(context_extra or {})
    result = runtime._invoke(job, context, prompt)
    runtime._finish_job(job, result)
    return result


def research_job(runtime: ProverRuntime, parent: dict[str, Any], question: str) -> dict[str, Any]:
    """Resolve one concrete resource or computation question as a separately bounded job."""
    prompt = (
        "Investigate this specific external resource or computation uncertainty. Save evidence locally and return a concise report with paths. Do not advise about Lean proof strategy or solve declarations.\n"
        + question
    )
    job, context = runtime._new_job("research", prompt=prompt)
    job["parent_job_id"] = parent["id"]
    context["requester"] = {"job_id": parent["id"], "node_id": parent["node_id"]}
    result = runtime._invoke(job, context, prompt)
    try:
        runtime._finish_job(job, result)
    except InfrastructureFailure:
        pass
    return {
        "job_id": job["id"],
        "status": result.get("status"),
        "report": str(result.get("final_response", ""))[:16000],
        "error": result.get("error", ""),
        "artifacts": result.get("artifacts", []),
    }


def prover_prompt(runtime: ProverRuntime, node: Node) -> str:
    return (
        "Prove the assigned Lean declaration. Work persistently within this fixed API allocation; "
        "continue concrete proof attempts rather than repeating search or requesting advice. "
        "Write your informal approach and failed attempts to the supplied private plan_path. "
        "Use scratch_file for LeanProbe experiments. Original source, PLAN and DAG are read-only. "
        "You may develop local have lemmas inside the assigned proof. Do not introduce axioms, "
        "admit, sorry, native_decide, or unsafe proof shortcuts in your submitted proof. "
        "Use only acyclic planned dependencies; an unresolved dependency makes a candidate untrusted "
        "until the controller rechecks it after all prerequisites are proved. "
        f"The declaration has {len(node.holes)} literal sorry hole(s). Return JSON "
        '{"proof":"replacement text for the single literal sorry", "notes":"concrete progress and blockers"}; '
        'for multiple holes use {"proofs":["replacement in source order", ...], "notes":"..."}. '
        "Alternatively write candidate_path (one-hole proof) or edit only the assigned scratch holes. "
        "Replace the literal sorry, not the complete declaration. In a tactic block use tactics; "
        "in a term position use an appropriate term or by-block. A local decomposition stays within "
        "this same budget; it does not launch a new advisory session. If unsolved, return honest "
        "notes, preserved partial work, and whether a specific additional attempt is promising."
    )
