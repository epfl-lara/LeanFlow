"""Native Lean worker dispatch (plan/delegate) for the Lean services layer.

Leaf module: builds a worker prompt from the workflow spec and either returns a plan or
delegates the task (with optional file lock), recording the outcome. Extracted verbatim from
lean_services.py and re-exported there; imports only the file_locks / lean_models /
lean_workflow_specs / workflow_state leaves (delegate_tool stays a lazy import), so no cycle.
"""

from __future__ import annotations

from typing import Any

from epflemma_cli.lean.lean_models import LeanWorkerRequest, LeanWorkerResult
from epflemma_cli.lean.lean_workflow_specs import get_lean_spec
from epflemma_cli.runtime.file_locks import acquire_file_lock as _acquire_file_lock
from epflemma_cli.workflows.workflow_state import append_workflow_outcome


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
    return "\n".join(parts).strip()


def dispatch_worker(
    request: LeanWorkerRequest,
    *,
    parent_agent: Any = None,
    owner_id: str = "",
) -> LeanWorkerResult:
    """Route a Lean worker request to either plan-mode return or full agent delegation, with optional file locking. Acquires a file lock if requested, builds a worker prompt from the request spec, and either returns the prompt for planning or delegates execution to an agent with terminal/file/skills/coordination toolsets; records outcome in workflow state."""
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

    from tools.implementations.delegate_tool import delegate_task

    delegated = delegate_task(
        goal=request.goal,
        context=prompt,
        toolsets=["terminal", "file", "skills", "coordination"],
        parent_agent=parent_agent,
        max_iterations=40,
    )
    result = LeanWorkerResult(
        worker=worker,
        mode="delegate",
        dispatched=True,
        summary=f"Delegated {worker}",
        result=delegated,
        lock=lock_result,
    )
    append_workflow_outcome("lean-worker", result.to_dict())
    return result
