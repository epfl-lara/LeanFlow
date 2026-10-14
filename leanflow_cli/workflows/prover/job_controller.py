"""Manage isolated jobs, native invocation accounting, and verified handoffs."""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.utils import atomic_json_write
from leanflow_cli.lean.lean_declarations import declaration_region
from leanflow_cli.workflows.prover.check_failures import infrastructure_code
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.runtime import BudgetExhausted, InfrastructureFailure
from leanflow_cli.workflows.prover.session_errors import SessionInterrupted
from leanflow_cli.workflows.prover.source import (
    declaration_source,
    extract_scratch_replacements,
    read_source,
    sorry_spans,
    write_source,
)
from leanflow_cli.workflows.prover.store import now
from leanflow_cli.workflows.prover.submission_cache import submission_key
from leanflow_cli.workflows.prover.usage import merge_usage

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def new_job(
    runtime: ProverRuntime, role: str, *, node: Node | None = None, prompt: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prepare a session without leaking reservations if resumed context setup fails."""
    resumed_jobs: dict[Any, dict[str, Any]] = (
        runtime.resume_role_jobs
        if role in {"orchestrator", "review", "research"}
        else runtime.resume_negation_jobs if role == "negation" else runtime.resume_jobs
    )
    key = (
        (role, prompt)
        if role in {"orchestrator", "review", "research"}
        else node.id if node else ""
    )
    resumed = resumed_jobs.get(key)
    previous = dict(resumed) if resumed is not None else None
    reserved_before = runtime.reserved
    try:
        job, context = _prepare_job(runtime, role, node=node, prompt=prompt)
        from leanflow_cli.workflows.prover.role_limits import context_allowances

        context["role_sessions"] = context_allowances(runtime)
        return job, context
    except Exception:
        if resumed is not None and previous is not None:
            runtime.reserved = reserved_before
            resumed.clear()
            resumed.update(previous)
            resumed_jobs[key] = resumed
        raise


def _prepare_job(
    runtime: ProverRuntime, role: str, *, node: Node | None = None, prompt: str = ""
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reserve one native invocation before allowing model work to start."""
    from leanflow_cli.workflows.prover.role_limits import require_capacity

    require_capacity(runtime, role)
    if (
        role in {"orchestrator", "review", "research"}
        and (role, prompt) in runtime.resume_role_jobs
    ):
        job = runtime.resume_role_jobs[(role, prompt)]
        remaining = 1
        job["api_budget"] = job["api_calls"] + 1
        job["reserved_calls"] = runtime._reserve(remaining)
        if job["reserved_calls"] != remaining:
            runtime.reserved -= job["reserved_calls"]
            raise BudgetExhausted("remaining campaign allocation cannot resume this job")
        job.update(
            previous_api_calls=job["api_calls"], resumed=True, status="running", accounted=False
        )
        for field in (
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "cost_source",
            "costed_api_calls",
        ):
            job["previous_" + field] = job.get(field)
        context = runtime._context(node)
        context.update(
            job_id=job["id"], candidate_path=str(Path(job["workspace"]) / "candidate.txt")
        )
        runtime.resume_role_jobs.pop((role, prompt))
        runtime._persist()
        return job, context
    resumed_jobs = runtime.resume_negation_jobs if role == "negation" else runtime.resume_jobs
    if role in {"prover", "negation"} and node is not None and node.id in resumed_jobs:
        job = resumed_jobs[node.id]
        remaining = 1
        job["api_budget"] = job["api_calls"] + 1
        runtime._ensure_active()
        job["reserved_calls"] = runtime._reserve(remaining) if remaining else 0
        if job["reserved_calls"] != remaining:
            runtime.reserved -= job["reserved_calls"]
            raise BudgetExhausted("remaining campaign allocation cannot resume this job")
        job["previous_api_calls"] = job["api_calls"]
        for field in (
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "cost_source",
            "costed_api_calls",
        ):
            job["previous_" + field] = job.get(field)
        job["resumed"] = True
        job["accounted"] = False
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
    budget = runtime._reserve(1)
    role_settings = runtime.config.to_mapping(role)
    job_id = f"{role}_{len(runtime.state['jobs']) + 1:05d}"
    workspace = runtime.store.directory / "jobs" / job_id
    try:
        workspace.mkdir(parents=True, exist_ok=False)
    except OSError:
        runtime.reserved -= budget
        raise
    try:
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
            "updated_at": now(),
            "phase": "starting",
            "provider": role_settings.get("provider") or os.getenv("LEANFLOW_NATIVE_PROVIDER", ""),
            "reasoning_effort": (
                role_settings["reasoning_effort"]
                or os.getenv("LEANFLOW_NATIVE_REASONING_EFFORT", "")
            ),
            "model": role_settings["model"],
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
                    prior_workspace.parent
                    / ".runtime"
                    / prior_workspace.name
                    / "source-before.lean"
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
                            declaration_source(
                                runtime.documents[node.file], node, candidate=partial
                            ),
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
        # Persist zero admission before publishing or dispatching the job. A crash
        # during context/transport setup must not consume the entire allocation.
        ledger = workspace.parent / ".runtime" / workspace.name / "request-count.json"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(ledger, {"limit": budget, "used": 0})
    except Exception:
        runtime.reserved -= budget
        runtime.scratch_before.pop(job_id, None)
        shutil.rmtree(workspace)
        shutil.rmtree(workspace.parent / ".runtime" / workspace.name, ignore_errors=True)
        raise
    runtime.state["jobs"].append(job)
    runtime._persist()
    return job, context


def session_event(
    runtime: ProverRuntime, job: dict[str, Any], kind: str, details: dict[str, Any]
) -> None:
    """Publish live session usage without allowing worker writes to PLAN or DAG."""
    with runtime.progress.lock:
        if job.get("accounted") is True:
            return
        if isinstance(details.get("api_calls"), int):
            job["api_calls"] = max(
                int(job.get("api_calls", 0)), min(job["api_budget"], details["api_calls"])
            )
        merge_usage(job, details)
        runtime.store.event(kind, {"job_id": job["id"], "node_id": job["node_id"], **details})
        job["updated_at"] = now()
        if kind == "agent-settings":
            # Each native process has a distinct ID, including resumes of a job.
            # Keep earlier observations intact when the runtime changes models.
            job.setdefault("model_settings", []).append(dict(details))
        if kind == "api-request":
            for field in ("model", "provider", "reasoning_effort"):
                if isinstance(details.get(field), str) and details[field]:
                    job[field] = details[field]
                    if field != "model":
                        runtime.state[field] = details[field]
        if kind == "provider-wait":
            job["phase"] = "provider_queue"
            if not job.get("active_operation_id"):
                operation = runtime.progress.begin(
                    "provider_queue",
                    "Waiting for provider request slot",
                    job_id=job["id"],
                    node_id=job["node_id"],
                    timeout_s=details.get("timeout_s", runtime.config.wall_time_s),
                )
                job["active_operation_id"] = operation["id"]
        elif kind in {"api-request", "tool-start"}:
            previous_operation_id = job.pop("active_operation_id", "")
            previous_operation = next(
                (
                    op
                    for op in runtime.state.get("operations", [])
                    if op["id"] == previous_operation_id
                ),
                None,
            )
            if previous_operation is not None and previous_operation["status"] == "running":
                runtime.progress.end(
                    previous_operation,
                    failed=previous_operation["kind"] != "provider_queue",
                    error=(
                        "Operation ended without its expected completion event"
                        if previous_operation["kind"] != "provider_queue"
                        else ""
                    ),
                )
            job["phase"] = "model_request" if kind == "api-request" else "tool"
            operation = runtime.progress.begin(
                job["phase"],
                (
                    "Waiting for model response"
                    if kind == "api-request"
                    else "Running " + str(details.get("tool", "tool"))
                ),
                job_id=job["id"],
                node_id=job["node_id"],
                timeout_s=details.get("timeout_s", runtime.config.timeout_s),
            )
            job["active_operation_id"] = operation["id"]
        elif kind in {"api-response", "api-error", "tool-result", "job-session-end"}:
            operation_id = job.pop("active_operation_id", "")
            finished_operation = next(
                (op for op in runtime.state.get("operations", []) if op["id"] == operation_id), None
            )
            if finished_operation is not None:
                runtime.progress.end(
                    finished_operation,
                    failed=(
                        kind == "api-error"
                        or (
                            kind == "tool-result"
                            and isinstance(details.get("result"), dict)
                            and (
                                details["result"].get("success") is False
                                or details["result"].get("ok") is False
                            )
                        )
                        or (
                            kind == "job-session-end"
                            and details.get("status")
                            in {"error", "environment_error", "interrupted", "timeout"}
                        )
                    ),
                )
            job["phase"] = "working"
        runtime.progress.publish()
        # The final event must retain the typed outcome and its complete usage,
        # even when cancellation caused the job to finish.
        if runtime.cancelled.is_set() and kind != "job-session-end":
            raise SessionInterrupted("prover controller stopped the job")


def invoke(
    runtime: ProverRuntime, job: dict[str, Any], context: dict[str, Any], prompt: str
) -> dict[str, Any]:
    """Execute one isolated session and retain its typed result even on infrastructure failure."""
    from leanflow_cli.workflows.prover.response_recovery import attach_recovery_guidance

    try:
        context = attach_recovery_guidance(runtime, job, context)
        settings = runtime.config.to_mapping(job["role"])
        settings["_previous_usage"] = {
            field: job.get("previous_" + field)
            for field in (
                "input_tokens",
                "output_tokens",
                "cost_usd",
                "cost_source",
                "costed_api_calls",
            )
        }
        settings["_cancelled"] = runtime.cancelled.is_set
        settings["_bridge_registry"] = runtime.native_bridges
        if job["role"] == "prover":
            settings["_research_job"] = lambda question: research_job(runtime, job, question)
            submission_lock = threading.Lock()

            def submit(response: str) -> dict[str, Any]:
                with submission_lock:
                    return candidate_feedback(runtime, job, response)

            settings["_candidate_feedback"] = submit
        elif job["role"] == "negation":
            from leanflow_cli.workflows.prover.negation_submission import (
                candidate_feedback as negation_candidate_feedback,
            )

            submission_lock = threading.Lock()

            def submit_negation(response: str) -> dict[str, Any]:
                with submission_lock:
                    return negation_candidate_feedback(runtime, job, response)

            settings["_candidate_feedback"] = submit_negation
            pending_submission = job.get("negation_pending_submission")
            if job.get("resumed") and isinstance(pending_submission, dict):
                settings["_pending_submission_response"] = json.dumps(pending_submission)
        if context.get("planning_stage") in {"outline", "proposal", "review"}:
            from leanflow_cli.workflows.prover.planning_feedback import report_feedback

            affected = context.get("planning_affected")
            settings["_report_feedback"] = lambda response: report_feedback(
                response,
                stage=context["planning_stage"],
                dag=runtime.dag,
                max_nodes=runtime.config.max_nodes,
                affected=set(affected) if affected is not None else None,
            )
        settings["wall_time_s"] = runtime._remaining_verification_time()
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
        from agent.runtime.harness import BackendError
        from agent.runtime.harness.isolation import IsolationUnavailable
        from leanflow_cli.workflows.prover.native_snapshot import SnapshotProvenanceError

        if isinstance(error, SnapshotProvenanceError):
            return {
                "status": "environment_error",
                "fresh_workspace_required": True,
                "error_code": "snapshot_provenance_changed",
                "final_response": str(error),
                "api_calls": job.get("api_calls", 0),
            }

        return {
            "status": (
                error.status
                if isinstance(error, (InfrastructureFailure, SessionInterrupted))
                else (
                    "environment_error"
                    if isinstance(error, (BackendError, IsolationUnavailable))
                    else (
                        "source_conflict" if "protected source changed" in str(error) else "error"
                    )
                )
            ),
            "final_response": str(error),
            "api_calls": job.get("api_calls", 0),
        }


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
        from leanflow_cli.workflows.prover.planning import json_report
        from leanflow_cli.workflows.prover.submission_imports import (
            candidate_document,
            check_import_types,
            remember_imports,
        )

        try:
            remember_imports(runtime, node, job, json_report(response))
        except ValueError as error:
            return {"accepted": False, "error": str(error)}
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
        staged = candidate_document(runtime, node)
        import_check = check_import_types(
            runtime, node, staged, runtime.store.directory / "checks" / job["id"] / "imports"
        )
        if not import_check.get("accepted"):
            return {
                "accepted": False,
                "error": "Requested imports failed protected-type verification: "
                + str(import_check),
            }
        source = declaration_source(staged, node, candidate=candidate)
        checked_inputs = submission_key(runtime, node, source)
    workspace = runtime.store.directory / "checks" / job["id"]
    workspace.mkdir(parents=True, exist_ok=True)
    file = workspace / "Submission.lean"
    write_source(file, source)
    job["phase"] = "verifying"
    result = runtime.progress.call(
        "submission_check",
        "Checking submitted proof",
        lambda: runtime.verifier.check(checked_node, file, skeleton=bool(unresolved)),
        job_id=job["id"],
        node_id=node.id,
        file=str(file),
        timeout_s=runtime.config.timeout_s,
    )
    job["phase"] = "submitted" if result.get("accepted") is True else "working"
    runtime.progress.publish()
    if infrastructure_code(result):
        runtime._ensure_active()
        raise InfrastructureFailure(
            "Lean verification environment unavailable: " + str(result.get("error", result)),
            status="environment_error",
        )
    with runtime.lock:
        runtime._assert_sources()
        current_node = runtime.dag.by_id().get(checked_node.id)
        current_key = (
            submission_key(runtime, current_node, source) if current_node is not None else ""
        )
        if (
            current_node is None
            or current_node.revision != checked_node.revision
            or current_key != checked_inputs
        ):
            return {
                "accepted": False,
                "error": "The assignment or its dependencies changed during verification; submit against the current assignment.",
            }
        runtime.submission_cache.remember(
            current_node,
            checked_inputs,
            current_key,
            result,
            conditional=bool(unresolved),
        )
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
        "receipt": {
            "node_id": checked_node.id,
            "revision": checked_node.revision,
            "input_hash": checked_inputs,
        },
        "feedback": json.dumps(result, ensure_ascii=False, default=str),
    }


def finish_job(runtime: ProverRuntime, job: dict[str, Any], result: dict[str, Any]) -> None:
    """Reconcile reserved requests using the session's actual monotonic request count."""
    with runtime.lock, runtime.progress.lock:
        if job.get("accounted") is True:
            return
        calls = max(int(job.get("api_calls", 0)), int(result.get("api_calls", 0) or 0))
        if calls > job["api_budget"]:
            raise RuntimeError("session exceeded its reserved API allocation")
        runtime.reserved -= job.get("reserved_calls", job["api_budget"])
        runtime.consumed += calls - int(job.get("previous_api_calls", 0))
        job.update(
            status=str(result.get("status", "error")),
            api_calls=calls,
            finished_at=now(),
            accounted=True,
            phase=(
                "submitted"
                if job["role"] == "prover" and result.get("status") == "completed"
                else "finished"
            ),
            updated_at=now(),
        )
        if result.get("status") == "interrupted":
            runtime.cancelled.set()
            runtime.stopping = True
            runtime.state["stop_status"] = "interrupted"
        merge_usage(job, result)
        job["usage_known"] = result.get("usage_known", False)
        job["session_id"] = result.get("session_id", "")
        for field in ("report_path", "artifacts"):
            if field in result:
                job[field] = result[field]
        error_details = result.get("error_details")
        if isinstance(error_details, dict) and error_details:
            # This is the bounded, redacted record produced by session_errors.
            job["error_details"] = copy.deepcopy(error_details)
        else:
            job.pop("error_details", None)
        if (
            result.get("status") == "environment_error"
            and result.get("fresh_workspace_required") is True
            and result.get("error_code") == "snapshot_provenance_changed"
        ):
            job["fresh_workspace_required"] = True
            # No new provider ran in this unverifiable view. Keep its files but
            # do not promote or resume its saved scratch as the current job.
            job["result_processed"] = True
        report_path = Path(job["workspace"]) / "result.json"
        from leanflow_cli.workflows.prover.stop_reason import job_stop_reason

        job["stop_reason"] = job_stop_reason(job, result)
        atomic_json_write(report_path, result)
        job["result_path"] = str(report_path)
        runtime.store.event(
            "job_finished", {"job_id": job["id"], "status": job["status"], "api_calls": calls}
        )
        runtime._persist()
        if (
            result.get("status")
            in {"provider_error", "environment_error", "source_conflict", "error", "context_limit"}
            and not runtime.cancelled.is_set()
        ):
            node = runtime.dag.by_id().get(job["node_id"])
            if (
                node is not None
                and job["role"] == "prover"
                and node.revision == job["node_revision"]
                and node.status != "proved"
            ):
                node.status = "retry"
            if result.get("status") == "context_limit":
                # A smaller request/configuration is required. Cancel in-flight
                # siblings rather than spending calls while the controller stops.
                runtime.cancelled.set()
            raise InfrastructureFailure(
                str(
                    result.get("error")
                    or result.get("final_response")
                    or "Provider session failed; its work and remaining budget were preserved"
                ),
                status=(
                    str(result["status"])
                    if result.get("status")
                    in {"environment_error", "source_conflict", "context_limit", "error"}
                    else "provider_error"
                ),
                error_details=job.get("error_details"),
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
    if context.get("recovery_decision"):
        from leanflow_cli.workflows.prover.role_limits import recovery_actions

        context["recovery_actions"] = recovery_actions(runtime)
    result = runtime._invoke(job, context, prompt)
    runtime._finish_job(job, result)
    runtime._ensure_active()
    return result


def research_job(runtime: ProverRuntime, parent: dict[str, Any], question: str) -> dict[str, Any]:
    """Resolve one concrete resource or computation question as a separately bounded job."""
    from leanflow_cli.workflows.prover.resource_handoff import resource_inventory

    prompt = (
        "Investigate this specific external resource or computation uncertainty. Save evidence locally and return a concise report with paths. Do not advise about Lean proof strategy or solve declarations.\n"
        + question
    )
    try:
        job, context = runtime._new_job("research", prompt=prompt)
    except BudgetExhausted as error:
        runtime._ensure_active()
        return {
            "status": "unavailable",
            "error": str(error) + " Continue within the current prover session.",
            "stop_code": error.code,
        }
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
        "resources": resource_inventory(
            [
                child
                for child in runtime.state["jobs"]
                if child.get("parent_job_id") == parent["id"]
            ],
            project_root=runtime.root,
            run_directory=runtime.store.directory,
        ),
    }


def prover_prompt(runtime: ProverRuntime, node: Node) -> str:
    return (
        "Prove the assigned Lean declaration. Work persistently in this native agent session; "
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
        "this same conversation; it does not launch a new advisory session. If unsolved, return honest "
        "notes, preserved partial work, and whether a specific additional attempt is promising."
    )
