"""Research, review, and materialize bounded proof plans for the owning controller."""

from __future__ import annotations

import contextlib
import copy
import queue
import re
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

from leanflow_cli.workflows.prover.models import Dag
from leanflow_cli.workflows.prover.planning import apply_proposal, json_report, planning_prompt
from leanflow_cli.workflows.prover.source import (
    SourceDocument,
    lean_code_mask,
    project_path,
    read_source,
    write_source,
)

if TYPE_CHECKING:
    from leanflow_cli.workflows.prover.runtime import ProverRuntime


def research_plan(
    runtime: ProverRuntime,
    reason: str,
    *,
    affected: set[str] | None = None,
    refinement: bool = False,
) -> bool:
    """Run fresh planning and critique contexts, then materialize only reviewed proposals."""
    previous_plan = runtime.state["plan_markdown"]
    if refinement:
        if runtime.state["metrics"]["plan_refinements"] >= runtime.config.plan_refinements:
            return False
        runtime.state["metrics"]["plan_refinements"] += 1
    runtime.state["phase"] = "planning"
    # Informal research and graph design receive independent model histories.
    outline = runtime._run_role(
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
    for _ in range(3):
        runtime._assert_sources()
        result = runtime._run_role("orchestrator", planning_prompt(reason=reason + "\n" + critique))
        proposal = json_report(str(result.get("final_response", "")))
        try:
            updated, skeletons = apply_proposal(
                runtime.dag, proposal, max_nodes=runtime.config.max_nodes, affected=affected
            )
            if not proposal or not isinstance(proposal.get("plan"), str):
                raise ValueError("planning report must include a concrete plan")
        except ValueError as error:
            critique = str(error)
            runtime.store.event("plan_rejected", {"reason": critique})
            continue
        runtime.state["phase"] = "reviewing"
        reviewed = runtime._run_role(
            "review",
            planning_prompt(reason=reason, review=True),
            context_extra={
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
            continue
        changed_direction = (
            affected is not None
            and not refinement
            and review.get(
                "change_kind",
                proposal.get("change_kind", "decomposition" if skeletons else "direction"),
            )
            == "direction"
        )
        if (
            changed_direction
            and runtime.state["metrics"]["plan_refinements"] >= runtime.config.plan_refinements
        ):
            runtime.state["plan_markdown"] = previous_plan
            runtime.store.event("plan_refinement_budget_exhausted", {"reason": reason})
            runtime._persist()
            return False
        try:
            install_planned_libraries(runtime, proposal.get("libraries", []))
            materialize(runtime, updated, skeletons)
        except (ValueError, RuntimeError) as error:
            from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

            if isinstance(error, InfrastructureFailure):
                raise
            critique = f"Independent skeleton gate rejected proposal: {error}"
            runtime.store.event("plan_rejected", {"reason": critique})
            continue
        runtime.dag = updated
        if changed_direction:
            runtime.state["metrics"]["plan_refinements"] += 1
        runtime.state["plan_markdown"] = proposal["plan"]
        runtime.state["phase"] = "proving"
        runtime._persist()
        launch_research_requests(runtime, proposal.get("research_jobs", []))
        return True
    runtime.state["plan_markdown"] += f"\n\nPlanning review did not converge: {critique}\n"
    runtime._persist()
    return False


def install_planned_libraries(runtime: ProverRuntime, entries: Any) -> None:
    """Apply only reviewed, immutable library requests and record exact configuration diffs."""
    if not entries:
        return
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
        _materialize(runtime, dag, skeletons)


def _materialize(runtime: ProverRuntime, dag: Dag, skeletons: dict[str, str]) -> None:
    """Compile reviewed helper skeletons before making them dependencies of user goals."""
    runtime._assert_sources()
    index = dag.by_id()
    previous_documents = runtime.documents
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
    created: list[Path] = []
    rewritten: dict[Path, bytes] = {}
    prior_changes = copy.deepcopy(runtime.state["changes"])
    retired = [node for node in runtime.dag.nodes if node.id not in index and not node.original]
    retired_modules = {node.module for node in retired}
    try:
        if skeletons:
            from leanflow_cli.workflows.prover.libraries import ensure_helper_library

            for name in ("lakefile.toml", "lakefile.lean"):
                path = runtime.root / name
                if path.is_file():
                    rewritten[path] = path.read_bytes()
            registered = ensure_helper_library(runtime.root)
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
                    "\n".join(f"import {module}" for module in modules)
                    + "\n\n"
                    + skeletons[node_id]
                    + "\n",
                    generated=True,
                )
                path = project_path(runtime.root, node.file)
                if path.exists() or path.is_symlink():
                    raise ValueError(f"helper path already exists: {node.file}")
                path.parent.mkdir(parents=True, exist_ok=True)
                write_source(path, document.render())
                created.append(path)
                check = runtime.verifier.compile_module(node.file)
                if not check.get("accepted"):
                    raise RuntimeError(str(check))
                documents[node.file] = document
                pending.remove(node_id)
        for node in dag.nodes:
            needed = [
                index[dep].module for dep in node.dependencies if index[dep].file != node.file
            ]
            # Existing originals may refer to each other already; only controller-owned helpers add imports.
            for module in needed:
                if module not in original_modules and module not in documents[node.file].imports:
                    documents[node.file].imports.append(module)
        for node in retired:
            document = documents.pop(node.file)
            path = runtime.root / node.file
            rewritten[path] = path.read_bytes()
            path.unlink()
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
                artifact.unlink()
        for document in documents.values():
            document.imports = [
                module for module in document.imports if module not in retired_modules
            ]
        for relative, document in documents.items():
            if (
                relative in runtime.documents
                and document.render() != runtime.documents[relative].render()
            ):
                rewritten.setdefault(
                    runtime.root / relative, (runtime.root / relative).read_bytes()
                )
                write_source(runtime.root / relative, document.render())
                runtime._change(document, "orchestrator")
        for node_id in skeletons:
            runtime._change(documents[index[node_id].file], "orchestrator")
        runtime.documents = documents
        runtime._refresh_locations(dag)
        runtime.state.setdefault("retired_nodes", []).extend(node.to_dict() for node in retired)
    except Exception:
        runtime.documents = previous_documents
        for path, content in rewritten.items():
            path.write_bytes(content)
        runtime.state["changes"] = prior_changes
        for path in created:
            with contextlib.suppress(OSError):
                path.unlink()
            artifact = (
                runtime.root
                / ".lake"
                / "build"
                / "lib"
                / "lean"
                / path.relative_to(runtime.root).with_suffix(".olean")
            )
            with contextlib.suppress(OSError):
                artifact.unlink()
        raise


def launch_research_requests(runtime: ProverRuntime, requests: Any) -> None:
    """Execute a bounded set of resource questions with independent contexts."""
    if not isinstance(requests, list):
        return
    completed: queue.SimpleQueue[Future[dict[str, Any]]] = queue.SimpleQueue()
    pending: dict[Future[dict[str, Any]], tuple[dict[str, Any], str]] = {}
    capacity = max(1, runtime.config.parallelism - len(runtime.pending))
    with ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="leanflow-research") as pool:
        try:
            for request in requests[: runtime.config.parallelism]:
                if not isinstance(request, dict) or not str(request.get("question", "")).strip():
                    continue
                question = str(request["question"])
                prompt = (
                    "Investigate this bounded question; save resources locally and return concrete evidence with source paths. Do not prove Lean declarations.\n"
                    + question
                )
                job, context = runtime._new_job("research", prompt=prompt)
                future = pool.submit(runtime._invoke, job, context, prompt)
                pending[future] = (job, question)
                future.add_done_callback(completed.put)
            while pending:
                runtime._messages()
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
                runtime._persist()
        except BaseException:
            runtime.cancelled.set()
            raise
        finally:
            for future, (job, _) in pending.items():
                runtime._finish_job(job, future.result())
