"""Research, review, and materialize bounded proof plans for the owning controller."""

from __future__ import annotations

import copy
import json
import queue
import re
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.check_failures import infrastructure_code
from leanflow_cli.workflows.prover.models import Dag
from leanflow_cli.workflows.prover.planning import apply_proposal, json_report, planning_prompt
from leanflow_cli.workflows.prover.source import (
    SourceConflictError,
    SourceDocument,
    lean_code_mask,
    project_path,
    read_source,
)

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def _finalize_plan_checkpoint(
    runtime: ProverRuntime, checkpoint: dict[str, Any], applied: bool
) -> None:
    """Commit a plan checkpoint's terminal bookkeeping, atomically with its pop.

    Called just before the same persist that pops ``planning_request``, so a
    crash cannot land between the pop and the bookkeeping:

    - A recovery plan seeds ``checkpoint["owner"]`` with its node id; its
      outcome must live on that node's journal rather than the shared
      ``proposal_status``, which a later node's plan can overwrite before this
      node's journal is drained.
    - The initial design seeds ``checkpoint["design"]``; marking design complete
      here (not in a later separate step) means an interrupted-but-committed
      design cannot strand -- resume would otherwise skip both the checkpoint
      drain (marker unset) and the design guard (phase already "proving").
    """
    owner = checkpoint.get("owner")
    if owner:
        rec = runtime.state.get("recovery_in_flight", {}).get(owner)
        if isinstance(rec, dict):
            rec["applied"] = applied
    if checkpoint.get("design"):
        runtime.state["design_plan_complete"] = True


def research_plan(
    runtime: ProverRuntime,
    reason: str,
    *,
    affected: set[str] | None = None,
    refinement: bool = False,
) -> bool:
    """Run fresh planning and critique contexts, then materialize only reviewed proposals."""
    checkpoint = runtime.state.get("planning_request")
    if not isinstance(checkpoint, dict):
        checkpoint = {
            "reason": reason,
            "affected": sorted(affected) if affected is not None else None,
            "refinement": refinement,
            "previous_plan": runtime.state["plan_markdown"],
            "steps": {},
        }
        runtime.state["planning_request"] = checkpoint
    else:
        reason, refinement = checkpoint["reason"], checkpoint["refinement"]
        affected = set(checkpoint["affected"]) if checkpoint["affected"] is not None else None
    runtime._ensure_active()
    if checkpoint.get("accepted_proposal"):
        launch_research_requests(runtime, checkpoint["accepted_proposal"].get("research_jobs", []))
        _finalize_plan_checkpoint(runtime, checkpoint, True)
        runtime.state.pop("planning_request", None)
        runtime.state["phase"] = "proving"
        runtime._persist()
        return True
    previous_plan = checkpoint.setdefault("previous_plan", runtime.state["plan_markdown"])
    runtime.state["phase"] = "planning"
    runtime._persist()
    # Informal research and graph design receive independent model histories.
    outline = _planning_call(
        runtime,
        checkpoint,
        "outline",
        "orchestrator",
        "Develop an honest informal proof outline for the supplied roots. Research available "
        "mathematical sources and preserve prior findings. Do not search Lean lemmas or prove "
        'declarations. Return JSON {"plan":"outline, resources, uncertainties and next steps"}.\n'
        + reason,
    )
    outline_report = json_report(str(outline.get("final_response", "")))
    if isinstance(outline_report.get("plan"), str) and outline_report["plan"].strip():
        runtime.state["plan_markdown"] = outline_report["plan"]
        runtime._persist()
    critique = ""
    proposal: dict[str, Any] = {}
    # Every proposal/review remains separately capped and globally accounted.
    # Three rejected drafts do not consume the remaining campaign allowance.
    # The global call ceiling also bounds malformed zero-call session adapters.
    for attempt in range(runtime.config.total_api_calls):
        runtime._ensure_active()
        runtime._assert_sources()
        result = _planning_call(
            runtime,
            checkpoint,
            f"proposal-{attempt}",
            "orchestrator",
            planning_prompt(reason=reason + "\n" + critique),
            context_extra=(
                {"previous_proposal": proposal, "planning_critique": critique} if attempt else None
            ),
        )
        proposal = json_report(str(result.get("final_response", "")))
        try:
            updated, skeletons = apply_proposal(
                runtime.dag, proposal, max_nodes=runtime.config.max_nodes, affected=affected
            )
            if not proposal or not isinstance(proposal.get("plan"), str):
                raise ValueError("planning report must include a concrete plan")
        except ValueError as error:
            critique = str(error)
            runtime.state.update(proposal_status="rejected", proposal_critique=critique)
            runtime.store.event("plan_rejected", {"reason": critique})
            runtime._persist()
            continue
        runtime.state.update(
            phase="reviewing",
            proposed_dag=updated.to_dict(),
            proposed_plan=proposal["plan"],
            proposal_status="proposed",
            proposal_critique=critique,
        )
        runtime._persist()
        reviewed = _planning_call(
            runtime,
            checkpoint,
            f"review-{attempt}",
            "review",
            planning_prompt(reason=reason, review=True),
            context_extra={
                "previous_accepted_plan": previous_plan,
                "proposed_dag": updated.to_dict(),
                "proposed_plan": proposal["plan"],
                "proposed_change_kind": proposal.get(
                    "change_kind", "decomposition" if skeletons else "direction"
                ),
            },
        )
        review = json_report(str(reviewed.get("final_response", "")))
        if review.get("accepted") is not True:
            critique = str(review.get("critique", "review did not accept the graph"))
            runtime.state.update(proposal_status="rejected", proposal_critique=critique)
            runtime.store.event("plan_rejected", {"reason": critique})
            runtime._persist()
            continue
        changed_direction = not checkpoint.get("refinement_charged") and (
            refinement
            or (
                affected is not None
                and review.get(
                    "change_kind",
                    proposal.get("change_kind", "decomposition" if skeletons else "direction"),
                )
                == "direction"
            )
        )
        if (
            changed_direction
            and runtime.state["metrics"]["plan_refinements"] >= runtime.config.plan_refinements
        ):
            # The reviewer accepted this direction, but the controller drops it. Label the
            # draft rejected so persisted state and dashboards stop showing it as awaiting
            # review, and leave the phase at proving: the repair caller never resets it, and
            # a persisted "reviewing" phase would replan from scratch on resume.
            critique = (
                "Plan refinement budget exhausted: the reviewed proposal changes mathematical "
                f"direction after the plan refinement limit ({runtime.config.plan_refinements}) "
                "was reached; the previous plan is retained."
            )
            runtime.state["plan_markdown"] = previous_plan
            runtime.state.update(
                phase="proving", proposal_status="rejected", proposal_critique=critique
            )
            runtime.state.pop("planning_request", None)
            runtime.store.event("plan_refinement_budget_exhausted", {"reason": reason})
            runtime._persist()
            return False
        runtime.state["materialization_job_id"] = checkpoint["steps"].get(f"proposal-{attempt}", "")
        runtime.state.update(
            phase="validating",
            proposal_status="validating",
            proposal_critique=str(review.get("critique", "")),
        )
        runtime._persist()
        try:
            install_planned_libraries(runtime, proposal.get("libraries", []))
            materialize(runtime, updated, skeletons)
        except (ValueError, RuntimeError) as error:
            from leanflow_cli.workflows.prover.runtime import BudgetExhausted, InfrastructureFailure

            if isinstance(error, (BudgetExhausted, InfrastructureFailure)):
                raise
            if isinstance(error, SourceConflictError):
                raise InfrastructureFailure(str(error), status="source_conflict") from error
            critique = f"Independent skeleton gate rejected proposal: {error}"
            failed = next(
                (
                    op
                    for op in reversed(runtime.state.get("operations", []))
                    if op.get("status") == "failed" and op.get("file")
                ),
                None,
            )
            if failed is not None:
                for proposed_node in runtime.state.get("proposed_dag", {}).get("nodes", []):
                    if proposed_node["file"] == failed["file"]:
                        proposed_node.update(status="check_failed", notes=str(error))
            runtime.state.update(proposal_status="rejected", proposal_critique=critique)
            runtime.store.event("plan_rejected", {"reason": critique})
            runtime._persist()
            continue
        runtime.dag = updated
        if changed_direction:
            runtime.state["metrics"]["plan_refinements"] += 1
            checkpoint["refinement_charged"] = True
        runtime.state["plan_markdown"] = proposal["plan"]
        runtime.state["phase"] = "proving"
        runtime.state["proposal_status"] = "accepted"
        checkpoint["accepted_proposal"] = proposal
        runtime._persist()
        launch_research_requests(runtime, proposal.get("research_jobs", []))
        _finalize_plan_checkpoint(runtime, checkpoint, True)
        runtime.state.pop("planning_request", None)
        runtime._persist()
        return True
    runtime.state["plan_markdown"] += f"\n\nPlanning review did not converge: {critique}\n"
    _finalize_plan_checkpoint(runtime, checkpoint, False)
    runtime.state.pop("planning_request", None)
    runtime.state["phase"] = "proving"
    runtime._persist()
    return False


def _planning_call(
    runtime: ProverRuntime,
    checkpoint: dict[str, Any],
    step: str,
    role: str,
    prompt: str,
    *,
    context_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay finished planning reports or continue the exact interrupted allocation."""
    runtime._ensure_active()
    job_id = checkpoint["steps"].get(step)
    job = next((item for item in runtime.state["jobs"] if item["id"] == job_id), None)
    if job is not None and job.get("status") not in {
        "running",
        "resume_pending",
        "provider_error",
        "environment_error",
        "error",
        # A cancelled or source-conflicted step never finished cleanly; replaying
        # its partial result.json as a completed planning report would corrupt
        # the plan. Re-run it (or, with no budget left, fail loudly) instead.
        "interrupted",
        "source_conflict",
    }:
        result_path = Path(job["workspace"]) / "result.json"
        if result_path.is_file():
            return dict(json.loads(result_path.read_text()))
    job, context = runtime._new_job(role, prompt=prompt)
    checkpoint["steps"][step] = job["id"]
    context.update(context_extra or {})
    runtime._persist()
    result = runtime._invoke(job, context, prompt)
    runtime._finish_job(job, result)
    runtime._ensure_active()
    return result


def install_planned_libraries(runtime: ProverRuntime, entries: Any) -> None:
    """Apply only reviewed, immutable library requests and record exact configuration diffs."""
    if not entries:
        return
    if not runtime.config.allow_internet:
        raise ValueError("Internet access is disabled; use only already installed libraries")
    if not isinstance(entries, list):
        raise ValueError("planning libraries must be a list")
    from leanflow_cli.workflows.prover.libraries import install_libraries

    before = {
        name: (runtime.root / name).read_bytes()
        for name in ("lakefile.toml", "lakefile.lean", "lake-manifest.json", "lean-toolchain")
        if (runtime.root / name).is_file()
    }
    result = install_libraries(runtime.root, entries, timeout_s=runtime.config.timeout_s)
    runtime.store.event("libraries_installed", result)
    if result.get("accepted") is not True:
        if result.get("dependency_cache_may_have_changed"):
            from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

            raise InfrastructureFailure(
                "Dependency resolution failed after changing the package cache; restore the locked environment before resuming: "
                + str(result.get("error", result)),
                status="environment_error",
            )
        raise RuntimeError(f"library installation rejected: {result}")
    for name, original in before.items():
        path = runtime.root / name
        if path.read_bytes() == original:
            continue
        baseline = runtime.store.directory / "baselines" / name
        baseline.parent.mkdir(parents=True, exist_ok=True)
        if not baseline.exists():
            baseline.write_bytes(original)
        runtime.state["changes"].append(
            {
                "path": str(path),
                "baseline_path": str(baseline),
                "status": "modified",
                "agent_id": "orchestrator",
            }
        )
        if name in runtime.documents:
            runtime.documents[name].baseline = read_source(path)
    runtime.state.setdefault("libraries", []).append(result)
    from leanflow_cli.workflows.prover.check_process import close_check_workers

    close_check_workers(runtime.store.directory / "checks")
    runtime._assert_sources()
    runtime._persist()


def materialize(runtime: ProverRuntime, dag: Dag, skeletons: dict[str, str]) -> None:
    """Serialize the controller's helper and import source mutations."""
    with runtime.lock:
        previous_changes = copy.deepcopy(runtime.state["changes"])
        runtime.progress.call(
            "graph_validation",
            "Materializing and validating reviewed graph",
            lambda: _materialize(runtime, dag, skeletons, previous_changes),
            total=len(skeletons),
            timeout_s=runtime._remaining_verification_time(),
        )


def _materialize(
    runtime: ProverRuntime,
    dag: Dag,
    skeletons: dict[str, str],
    previous_changes: list[dict[str, Any]],
) -> None:
    """Compile reviewed helper skeletons before making them dependencies of user goals."""
    runtime._assert_sources()
    index = dag.by_id()
    documents = copy.deepcopy(runtime.documents)
    original_imports = sorted(
        {
            module
            for document in runtime.documents.values()
            if document.path in {item.file for item in dag.nodes if item.original}
            for module in re.findall(
                r"(?m)^\s*import\s+([\w.]+)", lean_code_mask(document.baseline)
            )
        }
    )
    original_modules = {node.module for node in dag.nodes if node.original}
    pending = set(skeletons)
    rewritten: dict[Path, bytes] = {}
    retired = [node for node in runtime.dag.nodes if node.id not in index and not node.original]
    from leanflow_cli.workflows.prover.source_transaction import (
        begin_materialization,
        materialized_write,
        recover_materialization,
    )

    journal = begin_materialization(runtime)
    try:
        if skeletons:
            from leanflow_cli.workflows.prover.libraries import ensure_helper_library

            for name in ("lakefile.toml", "lakefile.lean"):
                path = runtime.root / name
                if path.is_file():
                    rewritten[path] = path.read_bytes()
            with tempfile.TemporaryDirectory(prefix="leanflow-helper-config-") as temporary:
                preview = Path(temporary)
                for path, content in rewritten.items():
                    (preview / path.name).write_bytes(content)
                registered = ensure_helper_library(preview)
                for change in registered.get("changes", []):
                    preview_path = Path(change["path"])
                    original_path = runtime.root / preview_path.name
                    materialized_write(
                        runtime,
                        journal,
                        original_path,
                        preview_path.read_bytes(),
                        expected_before=rewritten[original_path],
                    )
                    change["path"] = str(original_path)
            if registered.get("accepted") is not True:
                raise RuntimeError(f"helper library registration failed: {registered}")
            for change in registered.get("changes", []):
                path = Path(change["path"])
                name = str(path.relative_to(runtime.root))
                baseline = runtime.store.directory / "baselines" / name
                baseline.parent.mkdir(parents=True, exist_ok=True)
                if not baseline.exists():
                    baseline.write_bytes(rewritten[path])
                runtime.state["changes"].append(
                    {
                        "path": str(path),
                        "baseline_path": str(baseline),
                        "status": "modified",
                        "agent_id": "orchestrator",
                        "job_id": runtime.state.get("materialization_job_id", ""),
                    }
                )
                if name in documents:
                    documents[name].baseline = read_source(path)
        while pending:
            ready = sorted(
                node_id
                for node_id in pending
                if not any(dep in pending for dep in index[node_id].dependencies)
            )
            if not ready:
                raise ValueError("helper module cycle")
            for node_id in ready:
                node = index[node_id]
                modules = sorted(
                    set(original_imports + [index[dep].module for dep in node.dependencies])
                )
                if node.module in modules or original_modules.intersection(modules):
                    raise ValueError("helper would import an original goal module or itself")
                document = SourceDocument(
                    node.file,
                    "\n".join(f"import {module}" for module in original_imports)
                    + "\n\n"
                    + skeletons[node_id]
                    + "\n",
                    # Dependency imports must remain removable when the graph changes.
                    # Represent them once, before both compilation and source checking.
                    imports=[module for module in modules if module not in original_imports],
                    generated=True,
                )
                path = project_path(runtime.root, node.file)
                if path.exists() or path.is_symlink():
                    raise ValueError(f"helper path already exists: {node.file}")
                path.parent.mkdir(parents=True, exist_ok=True)
                materialized_write(
                    runtime, journal, path, document.render().encode("utf-8"), require_absent=True
                )
                documents[node.file] = document
                pending.remove(node_id)
        needed_by_file: dict[str, set[str]] = {}
        for node in dag.nodes:
            needed_by_file.setdefault(node.file, set()).update(
                index[dep].module
                for dep in node.dependencies
                if index[dep].file != node.file and index[dep].module not in original_modules
            )
        # Replaced edges must remove old imports even when both helpers remain reachable.
        # Original imports stay protected in the baseline; only controller additions change.
        for relative, document in documents.items():
            document.imports = sorted(needed_by_file.get(relative, set()))
        for node in retired:
            document = documents.pop(node.file)
            path = runtime.root / node.file
            rewritten[path] = path.read_bytes()
            materialized_write(runtime, journal, path, None)
            for change in runtime.state["changes"]:
                if change["path"] == str(path):
                    change["status"] = "deleted"
            artifact = (
                runtime.root
                / ".lake"
                / "build"
                / "lib"
                / "lean"
                / Path(node.file).with_suffix(".olean")
            )
            if artifact.exists():
                rewritten[artifact] = artifact.read_bytes()
                materialized_write(runtime, journal, artifact, None)
        for relative, document in documents.items():
            if (
                relative in runtime.documents
                and document.render() != runtime.documents[relative].render()
            ):
                rewritten.setdefault(
                    runtime.root / relative, (runtime.root / relative).read_bytes()
                )
                materialized_write(
                    runtime, journal, runtime.root / relative, document.render().encode("utf-8")
                )
                runtime._change(document, "orchestrator")
        for node_id in skeletons:
            runtime._change(documents[index[node_id].file], "orchestrator")
        from leanflow_cli.workflows.prover.materialization_imports import compile_changed_helpers

        for change in runtime.state["changes"]:
            if change not in previous_changes:
                change.update(pending=True, staged_status=change["status"], status="staged")
        runtime.progress.publish()
        compile_changed_helpers(runtime, dag, documents, journal)
        if hasattr(runtime.verifier, "capture_signatures"):
            signatures = runtime.verifier.capture_signatures(
                dag, documents, runtime.store.directory / "checks" / "signatures", initialize=False
            )
            if signatures.get("accepted") is not True:
                if infrastructure_code(signatures):
                    from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

                    runtime._ensure_active()
                    raise InfrastructureFailure(
                        "Protected-type verification infrastructure failed: " + str(signatures),
                        status="environment_error",
                    )
                raise ValueError(
                    "Planned imports changed or failed to elaborate a protected declaration type: "
                    + str(signatures.get("error", signatures))
                )
        for document in documents.values():
            document.assert_current(runtime.root)
        runtime._ensure_active()
        runtime.documents = documents
        runtime._refresh_locations(dag)
        runtime.state.setdefault("retired_nodes", []).extend(node.to_dict() for node in retired)
        runtime.dag = dag
        from leanflow_cli.workflows.prover.source_transaction import commit_materialization

        for change in runtime.state["changes"]:
            if change.get("pending"):
                change.update(status=change.pop("staged_status", "modified"), pending=False)
        commit_materialization(runtime)
    except Exception:
        staged = copy.deepcopy(
            [change for change in runtime.state["changes"] if change.get("pending")]
        )
        try:
            recover_materialization(runtime, journal)
        except (ValueError, SourceConflictError) as conflict:
            from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

            raise InfrastructureFailure(str(conflict), status="source_conflict") from conflict
        for change in staged:
            change.update(status="rolled_back", pending=False)
        for change in staged:
            prior = next(
                (item for item in runtime.state["changes"] if item["path"] == change["path"]), None
            )
            if prior is None:
                runtime.state["changes"].append(change)
            else:
                prior["last_attempt"] = {
                    "status": "rolled_back",
                    "agent_id": change.get("agent_id", "orchestrator"),
                }
        runtime.progress.publish()
        raise


def launch_research_requests(runtime: ProverRuntime, requests: Any) -> None:
    """Execute a bounded set of resource questions with independent contexts."""
    if not isinstance(requests, list):
        return
    completed: queue.SimpleQueue[Future[dict[str, Any]]] = queue.SimpleQueue()
    pending: dict[Future[dict[str, Any]], tuple[dict[str, Any], str]] = {}
    capacity = max(1, runtime.config.parallelism - len(runtime.pending))
    checkpoint = runtime.state.get("planning_request", {})
    jobs_by_request = checkpoint.setdefault("research_jobs", {})
    with ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="leanflow-research") as pool:
        try:
            for index, request in enumerate(requests[: runtime.config.parallelism]):
                runtime._ensure_active()
                if not isinstance(request, dict) or not str(request.get("question", "")).strip():
                    continue
                question = str(request["question"])
                prompt = (
                    "Investigate this bounded question; save resources locally and return concrete evidence with source paths. Do not prove Lean declarations.\n"
                    + question
                )
                previous = next(
                    (
                        item
                        for item in runtime.state["jobs"]
                        if item["id"] == jobs_by_request.get(str(index))
                    ),
                    None,
                )
                if previous is not None and previous.get("research_recorded"):
                    continue
                result_path = (
                    Path(previous["workspace"]) / "result.json" if previous is not None else None
                )
                if (
                    previous is not None
                    and previous.get("accounted")
                    and previous.get("status")
                    not in {"resume_pending", "provider_error", "environment_error", "error"}
                    and result_path is not None
                    and result_path.is_file()
                ):
                    job = previous
                    future = Future[dict[str, Any]]()
                    future.set_result(dict(json.loads(result_path.read_text())))
                else:
                    job, context = runtime._new_job("research", prompt=prompt)
                    jobs_by_request[str(index)] = job["id"]
                    runtime._persist()
                    future = pool.submit(runtime._invoke, job, context, prompt)
                pending[future] = (job, question)
                future.add_done_callback(completed.put)
            while pending:
                runtime._messages()
                runtime._ensure_active()
                try:
                    future = completed.get(timeout=0.5)
                except queue.Empty:
                    continue
                job, question = pending.pop(future)
                result = future.result()
                runtime._finish_job(job, result)
                runtime.state["plan_markdown"] += (
                    "\n\n## Research result\n\n"
                    + question
                    + "\n\n"
                    + str(result.get("final_response", ""))[:16000]
                )
                job["research_recorded"] = True
                runtime._persist()
        except BaseException:
            runtime.cancelled.set()
            raise
        finally:
            for future, (job, _) in pending.items():
                try:
                    runtime._finish_job(job, future.result())
                except Exception as error:
                    runtime.store.event(
                        "research_cleanup_error", {"job_id": job["id"], "error": str(error)}
                    )
