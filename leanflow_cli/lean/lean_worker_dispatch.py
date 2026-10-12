"""Prepare deterministic Lean worker plans and reject removed direct-model delegation."""

from __future__ import annotations

from typing import Any

from leanflow_cli.lean.lean_models import LeanWorkerRequest, LeanWorkerResult
from leanflow_cli.lean.lean_workflow_specs import get_lean_spec
from leanflow_cli.runtime.file_locks import acquire_file_lock as _acquire_file_lock
from leanflow_cli.workflows.plan_state import artifact_context_block
from leanflow_cli.workflows.workflow_state import append_workflow_outcome


def _worker_prompt(worker: str, request: LeanWorkerRequest) -> str:
    record = get_lean_spec(worker)
    title = record.title if record else worker
    summary = record.summary if record else worker
    parts = [
        f"Native Lean worker: {title}",
        summary,
        "",
        f"Goal: {request.goal}",
    ]
    if request.file_path:
        parts.append(f"File: {request.file_path}")
    if request.line:
        parts.append(f"Line: {request.line}")
    if request.context:
        parts.extend(["", "Context:", request.context])
    if record:
        parts.extend(
            [
                "",
                "Worker contract:",
                f"- route action(s): {', '.join(record.route_actions) or '[none]'}",
                f"- tools: {', '.join(record.tools) or '[none]'}",
            ]
        )
    # The worker prompt is also the delegate context, so this block covers
    # dispatched workers and delegate_task children.
    plan_context = artifact_context_block()
    if plan_context:
        parts.extend(["", "Plan artifacts:", plan_context])
    return "\n".join(parts).strip()


def dispatch_worker(
    request: LeanWorkerRequest,
    *,
    parent_agent: Any = None,
    owner_id: str = "",
) -> LeanWorkerResult:
    """Build a worker plan or report that execution requires a native workflow.

    Preserve optional file reservation and outcome recording for planning callers.
    The retired in-process delegation path never constructs a model client.
    """
    worker = request.worker.strip()
    lock_result: dict[str, Any] | None = None
    if request.use_file_lock and request.file_path and owner_id:
        lock_result = _acquire_file_lock(
            request.file_path,
            owner_id=owner_id,
            purpose=f"lean-worker:{worker}",
            ttl_seconds=1800,
            force=False,
        )
        if not lock_result.get("success"):
            result = LeanWorkerResult(
                worker=worker,
                mode="plan",
                dispatched=False,
                summary=f"File lock unavailable for {request.file_path}: {lock_result.get('error', 'unknown error')}",
                lock=lock_result,
            )
            append_workflow_outcome("lean-worker", result.to_dict())
            return result

    prompt = _worker_prompt(worker, request)
    if not request.allow_delegation or parent_agent is None:
        result = LeanWorkerResult(
            worker=worker,
            mode="plan",
            dispatched=False,
            summary=prompt,
            lock=lock_result,
        )
        append_workflow_outcome("lean-worker", result.to_dict())
        return result

    from agent.providers.auxiliary_client import RETIRED_PROVIDER_MESSAGE

    result = LeanWorkerResult(
        worker=worker,
        mode="native_required",
        dispatched=False,
        summary=RETIRED_PROVIDER_MESSAGE,
        result={"error": RETIRED_PROVIDER_MESSAGE},
        lock=lock_result,
    )
    append_workflow_outcome("lean-worker", result.to_dict())
    return result
