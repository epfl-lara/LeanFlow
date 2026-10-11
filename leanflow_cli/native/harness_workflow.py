"""Run non-prover workflows through native harnesses and independent source acceptance."""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent.runtime.harness import HarnessRequest, build_runtime_isolation, run_harness
from agent.runtime.harness.network_policy import network_policy_error
from core.campaign_limits import CampaignLimits, LimitReached
from core.utils import atomic_json_write
from leanflow_cli.native.harness_artifacts import promote, source_files, verify_proposal
from leanflow_cli.native.harness_context import prepare_context
from leanflow_cli.native.harness_observer import NativeObserver
from leanflow_cli.workflows.prover.native_bridge import NativeBridge
from leanflow_cli.workflows.prover.native_snapshot import create_snapshot
from leanflow_cli.workflows.prover.session_tools import SessionTools


def run_workflow(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Drive one resumable native workflow with persistent global accounting."""
    environment = dict(os.environ if env is None else env)
    root = Path(environment.get("LEANFLOW_PROJECT_ROOT", os.getcwd())).resolve()
    kind = environment.get("LEANFLOW_NATIVE_WORKFLOW_KIND", "review")
    backend = environment.get("LEANFLOW_NATIVE_PROVIDER", "codex")
    policy_error = network_policy_error(environment)
    if policy_error:
        raise ValueError(policy_error)
    resume = environment.get("LEANFLOW_NATIVE_RESUME_RUN_ID", "")
    run_id = resume or environment.get("LEANFLOW_WORKFLOW_RUN_ID") or uuid.uuid4().hex[:16]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid native workflow run ID")
    directory = root / ".leanflow" / "native-workflows" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "controller.lock").open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("This native workflow already has an active controller") from error
        return _run_controller(
            environment, root, kind, backend, directory, run_id, resume, observe=env is None
        )


def _run_controller(
    environment: dict[str, str],
    root: Path,
    kind: str,
    backend: str,
    directory: Path,
    run_id: str,
    resume: str,
    *,
    observe: bool,
) -> dict[str, Any]:
    """Hold exclusive campaign ownership through native execution and final cleanup."""
    state_path = directory / "state.json"
    if resume:
        state = json.loads(state_path.read_text())
        if (
            state.get("version") != 2
            or state.get("backend") != backend
            or state.get("workflow") != kind
        ):
            raise ValueError(
                "Start a new native run when changing backends or migrating legacy runs"
            )
        baseline = {path: bytes.fromhex(data) for path, data in state["baseline"].items()}
        if source_files(root) != baseline:
            raise ValueError("Canonical sources changed; start a new workflow")
    else:
        if state_path.exists():
            raise ValueError(
                "This native run ID already exists; resume it explicitly or start a new run"
            )
        baseline = source_files(root)
        state = {
            "version": 2,
            "run_id": run_id,
            "backend": backend,
            "workflow": kind,
            "baseline": {path: data.hex() for path, data in baseline.items()},
            "status": "running",
        }
    state.update(status="running", terminal=False)
    atomic_json_write(state_path, state)
    budget = CampaignLimits(
        directory / "limits.json",
        {
            name: environment.get("LEANFLOW_PROVER_" + name.upper())
            for name in ("agent_calls", "tokens", "wall_time_s")
        },
    )
    cancelled = threading.Event()
    finished = threading.Event()
    observing = threading.Event()
    observer = NativeObserver(root, run_id, kind, backend) if observe else None
    previous_signals: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signals[signum] = signal.signal(signum, lambda *_: cancelled.set())

    def heartbeat() -> None:
        while not finished.wait(1):
            budget.persist()
            if observer is not None and observing.is_set():
                try:
                    observer.publish(state, budget)
                except Exception as error:
                    state["error"] = f"Live status ownership failed: {error}"
                    cancelled.set()
            remaining = budget.remaining()
            if remaining is not None and remaining <= 0:
                cancelled.set()

    ticker = threading.Thread(target=heartbeat, daemon=True)
    ticker.start()
    snapshot = directory / "project"
    try:
        if observer is not None:
            observer.start(state)
            observing.set()
        if not snapshot.exists():
            create_snapshot(root, snapshot)
        scratch = snapshot / ".leanflow-job"
        scratch.mkdir(exist_ok=True)
        active = environment.get("LEANFLOW_NATIVE_ACTIVE_FILE", "")
        allowed = tuple(
            value.strip()
            for value in environment.get(
                "LEANFLOW_NATIVE_ALLOWED_AXIOMS", "propext,Classical.choice,Quot.sound"
            ).split(",")
            if value.strip()
        )
        remaining = budget.remaining()
        deadline = None if remaining is None else time.monotonic() + remaining
        context, prompt, source_context = prepare_context(
            root=root, snapshot=snapshot, scratch=scratch, environment=environment, allowed=allowed
        )
        receipt: dict[str, Any] = {}
        validation_lock = threading.Lock()
        last_validation: dict[str, Any] = {}

        def submit(response: str) -> dict[str, Any]:
            """Freeze one proposal at a time and retain only independently accepted bytes."""
            with validation_lock:
                if observer is not None:
                    observer.publish(state, budget, phase="verifying")
                checked = verify_proposal(
                    root=root,
                    snapshot=snapshot,
                    baseline=baseline,
                    directory=directory / "checks",
                    workflow=kind,
                    active_file=active,
                    allowed_axioms=allowed,
                    deadline=deadline,
                    cancelled=cancelled.is_set,
                )
                last_validation.clear()
                last_validation.update(
                    {
                        key: value
                        for key, value in checked.items()
                        if key not in {"files", "profiles"}
                    }
                )
                if checked.get("accepted"):
                    receipt.clear()
                    receipt.update(checked)
                    atomic_json_write(directory / "accepted.json", checked)
                return {
                    key: value for key, value in checked.items() if key not in {"files", "profiles"}
                }

        tools = SessionTools(
            role="prover",
            project_root=snapshot,
            workspace=scratch,
            context=context,
            timeout_s=None,
            deadline=deadline,
            cancelled=cancelled.is_set,
        )
        with NativeBridge(
            toolset=tools,
            context=context,
            callbacks={"submit_files": submit},
            directory=directory / "bridge",
            on_event=observer.event if observer is not None else None,
        ) as bridge:
            mcp_path = scratch / "mcp.json"
            atomic_json_write(mcp_path, bridge.mcp_config())
            executable = ""
            reads = [Path(__file__).resolve().parents[2]]
            if backend == "lemex":
                from leanflow_cli.runtime.lemex_install import (
                    managed_distribution_root,
                    resolve_lemex_executable,
                )

                executable = str(resolve_lemex_executable() or "lemex")
                distribution = managed_distribution_root()
                if distribution is not None:
                    reads.append(distribution)
            isolation = build_runtime_isolation(
                backend, snapshot, env=environment, readable_roots=reads, denied_roots=(root,)
            )
            while not cancelled.is_set():
                if observer is not None:
                    observer.publish(state, budget, phase="agent")
                invocation = budget.reserve()
                started = False

                def on_started() -> None:
                    nonlocal started
                    started = True
                    budget.started_call(invocation)

                def capture(event: dict[str, Any]) -> None:
                    """Persist native session identity before a crash can lose continuation."""
                    session = (
                        event.get("session_id")
                        if backend == "claude"
                        else (
                            event.get("thread_id")
                            if event.get("type") == "thread.started"
                            else None
                        )
                    )
                    if isinstance(session, str) and session:
                        state["session_id"] = session
                        atomic_json_write(state_path, state)

                try:
                    result = run_harness(
                        HarnessRequest(
                            backend=backend,
                            prompt=prompt,
                            cwd=snapshot,
                            model=environment.get("LEANFLOW_NATIVE_MODEL", ""),
                            effort=environment.get("LEANFLOW_NATIVE_REASONING_EFFORT", ""),
                            session_id=str(state.get("session_id", "")),
                            previous_usage=state.get("usage_total"),
                            env=environment,
                            deadline=deadline,
                            cancelled=cancelled.is_set,
                            mcp_config=mcp_path,
                            log_path=directory / f"{invocation}.jsonl",
                            isolation=isolation,
                            executable=executable,
                            on_started=on_started,
                        ),
                        on_event=capture,
                    )
                except BaseException:
                    budget.finish(invocation, None, started=started)
                    raise
                budget.finish(invocation, result.usage, started=started)
                bridge.drain()
                state.update(
                    session_id=result.session_id,
                    usage_total=result.usage_total,
                    final_response=result.final_response,
                    status="running" if result.status == "completed" else result.status,
                    native_status=result.status,
                    error=result.error,
                )
                atomic_json_write(state_path, state)
                remaining = budget.remaining()
                if remaining is not None and remaining <= 0:
                    raise LimitReached("Global campaign time limit reached")
                if cancelled.is_set():
                    state["status"] = "cancelled"
                    return state
                if result.status != "completed":
                    return state
                if kind == "review":
                    if source_files(snapshot) != baseline:
                        state.update(
                            status="blocked",
                            error="Review changed source files; changes were not published",
                        )
                        return state
                    state["status"] = "completed"
                    return state
                if not receipt:
                    try:
                        reported = json.loads(result.final_response)
                    except (ValueError, TypeError):
                        reported = None
                    if isinstance(reported, dict) and reported.get("status") == "blocked":
                        state.update(
                            status="blocked", error=str(reported.get("reason", "External blocker"))
                        )
                        return state
                    submit(result.final_response)
                if receipt.get("accepted"):
                    if kind == "formalize":
                        from leanflow_cli.native.harness_formalization import review_formalization

                        if observer is not None:
                            observer.publish(state, budget, phase="source-review")
                        review = review_formalization(
                            receipt=receipt,
                            source_context=source_context,
                            backend=backend,
                            model=environment.get("LEANFLOW_NATIVE_MODEL", ""),
                            effort=environment.get("LEANFLOW_NATIVE_REASONING_EFFORT", ""),
                            environment=environment,
                            directory=directory / "source-review",
                            budget=budget,
                            deadline=deadline,
                            cancelled=cancelled.is_set,
                        )
                        atomic_json_write(directory / "source-review.json", review)
                        state["source_review"] = review
                        if review.get("accepted") is not True:
                            if review.get("status") not in {None, "completed"}:
                                state.update(
                                    status=(
                                        review.get("status")
                                        if review.get("status")
                                        in {"limit_reached", "cancelled", "deadline_exceeded"}
                                        else "blocked"
                                    ),
                                    error=str(
                                        review.get(
                                            "error",
                                            review.get("feedback", "Source reviewer failed"),
                                        )
                                    ),
                                )
                                return state
                            receipt.clear()
                            prompt = (
                                "Independent source review rejected the draft. Repair the source mapping and statements, then submit_files again. Review feedback: "
                                + json.dumps(review)
                            )
                            continue
                    state["promoted_files"] = promote(
                        root, baseline, receipt, deadline=deadline, cancelled=cancelled.is_set
                    )
                    state["status"] = (
                        "draft_ready" if kind in {"draft", "formalize"} else "completed"
                    )
                    return state
                # The native process has ended, so continuation is another charged invocation.
                prompt = (
                    "Continue the existing assignment. Independent verification rejected the source proposal. Repair these errors, then use submit_files: "
                    + json.dumps(last_validation)
                )
            state["status"] = "cancelled"
            return state
    except InterruptedError as error:
        state.update(status="cancelled", error=str(error))
        return state
    except LimitReached as error:
        state.update(status="limit_reached", error=str(error))
        return state
    except Exception as error:
        state.update(status="error", error=str(error))
        return state
    finally:
        cancelled.set()
        finished.set()
        ticker.join()
        try:
            budget.persist()
            state["terminal"] = True
            atomic_json_write(state_path, state)
            if observer is not None and observing.is_set():
                observer.publish(state, budget)
                observer.finish(state)
        finally:
            # Storage/status failures must not leak controller process state.
            for previous_signum, handler in previous_signals.items():
                signal.signal(previous_signum, handler)


def main() -> int:
    """Report an authoritative native workflow outcome to the shell and status history."""
    from leanflow_cli.workflows.workflow_state import append_workflow_outcome

    try:
        result = run_workflow()
    except Exception as error:
        result = {"status": "error", "error": str(error)}
    public = {key: value for key, value in result.items() if key != "baseline"}
    append_workflow_outcome("native-workflow", public)
    print(json.dumps(public, ensure_ascii=False))
    return (
        0
        if result.get("status") in {"completed", "draft_ready"}
        else 130 if result.get("status") == "cancelled" else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
