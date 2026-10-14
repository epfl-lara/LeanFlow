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
from agent.runtime.harness.network_policy import restricted_research
from core.campaign_limits import CampaignLimits, LimitReached
from core.utils import atomic_json_write
from leanflow_cli.formalization.records import current_selection_token, save_receipt
from leanflow_cli.native.harness_artifacts import promote, source_files, verify_proposal
from leanflow_cli.native.harness_context import prepare_context
from leanflow_cli.native.harness_observer import NativeObserver
from leanflow_cli.runtime.project_operations import project_activity
from leanflow_cli.runtime.storage import StoragePressure
from leanflow_cli.workflows.prover.native_bridge import NativeBridge
from leanflow_cli.workflows.prover.native_snapshot import (
    prepare_snapshot,
    release_snapshot_dependencies,
)
from leanflow_cli.workflows.prover.session_tools import SessionTools
from leanflow_cli.workflows.prover.storage_admission import StorageAdmission


def run_workflow(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Drive one resumable native workflow with persistent global accounting."""
    environment = dict(os.environ if env is None else env)
    root = Path(environment.get("LEANFLOW_PROJECT_ROOT", os.getcwd())).resolve()
    kind = environment.get("LEANFLOW_NATIVE_WORKFLOW_KIND", "review")
    backend = environment.get("LEANFLOW_NATIVE_PROVIDER", "codex")
    resume = environment.get("LEANFLOW_NATIVE_RESUME_RUN_ID", "")
    run_id = resume or environment.get("LEANFLOW_WORKFLOW_RUN_ID") or uuid.uuid4().hex[:16]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid native workflow run ID")
    directory = root / ".leanflow" / "native-workflows" / run_id
    with project_activity(root):
        from leanflow_cli.formalization.publication import (
            assert_no_pending_publication,
            recover_pending_publication,
        )

        recover_pending_publication(root)
        assert_no_pending_publication(root)
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
    restricted = restricted_research(environment)
    document_id = environment.get("LEANFLOW_FORMALIZATION_DOCUMENT_ID", "")
    source_draft_guidance = ""
    source_draft_blocker: dict[str, Any] | None = None
    if kind == "formalize" and not document_id:
        raise ValueError("Formalization requires a frozen document inventory; restart the launch")
    if kind == "formalize":
        from leanflow_cli.formalization.intake import (
            author_blocking_diagnostics,
            draft_source_guidance,
            draft_source_rejection,
            preflight_dependencies,
        )
        from leanflow_cli.formalization.records import load_document

        selected_document = load_document(root, document_id)
        blockers = [
            row["message"] for row in author_blocking_diagnostics(selected_document["inventory"])
        ]
        if blockers:
            raise ValueError("Repair source inventory before formalization: " + "; ".join(blockers))
        source_draft_guidance = draft_source_guidance(selected_document["inventory"])
        source_draft_blocker = draft_source_rejection(selected_document["inventory"])
        requested_revision = environment.get("LEANFLOW_FORMALIZATION_REVISION", "")
        if requested_revision not in {
            selected_document["revision_id"],
            selected_document.get("author_base_revision"),
        }:
            raise ValueError("Source inventory changed; restart from its selected revision")
        preflight_dependencies(root, document_id)
    state_path = directory / "state.json"
    if resume:
        state = json.loads(state_path.read_text())
        restricted = restricted or state.get("research_policy") == "restricted-v1"
        if (
            state.get("version") != 2
            or state.get("backend") != backend
            or state.get("workflow") != kind
        ):
            raise ValueError(
                "Start a new native run when changing backends or migrating legacy runs"
            )
        if restricted and state.get("research_policy") != "restricted-v1":
            raise ValueError(
                "Start a new run for clean-room testing; this run contains earlier online or unclassified context."
            )
        baseline = {path: bytes.fromhex(data) for path, data in state["baseline"].items()}
        if source_files(root) != baseline:
            raise ValueError("Canonical sources changed; start a new workflow")
        if kind == "formalize" and (
            not state.get("formalization_selection_token")
            or state["formalization_selection_token"] != current_selection_token(root, document_id)
        ):
            raise ValueError("Formalization selection changed; restart from its selected revision")
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
        if document_id:
            from leanflow_cli.formalization.publish import portable_baseline

            state["formalization_baseline"] = {
                name: data.hex() for name, data in portable_baseline(root, document_id).items()
            }
            if kind == "formalize":
                state["formalization_selection_token"] = current_selection_token(
                    root, document_id, initialize=True
                )
    state.update(
        status="running",
        terminal=False,
        research_policy="restricted-v1" if restricted else "online-v1",
    )
    if source_draft_blocker is not None:
        state["source_draft_only"] = True
        state["source_diagnostics"] = source_draft_blocker["source_diagnostics"]
    if restricted:
        environment = {**environment, "LEANFLOW_PROVER_ALLOW_INTERNET": "false"}
    atomic_json_write(state_path, state)
    budget = CampaignLimits(
        directory / "limits.json",
        {
            name: environment.get("LEANFLOW_PROVER_" + name.upper())
            for name in ("agent_calls", "tokens", "wall_time_s")
        },
    )
    cancelled = threading.Event()
    storage_admission = StorageAdmission(root)
    storage_error = ""
    finished = threading.Event()
    observing = threading.Event()
    observer = NativeObserver(root, run_id, kind, backend) if observe else None
    previous_signals: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signals[signum] = signal.signal(signum, lambda *_: cancelled.set())

    def heartbeat() -> None:
        nonlocal storage_error
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
            try:
                storage_admission.check()
            except StoragePressure as error:
                storage_error = str(error)
                cancelled.set()
                return

    ticker = threading.Thread(target=heartbeat, daemon=True)
    ticker.start()
    snapshot = directory / "project"
    try:
        if observer is not None:
            observer.start(state)
            observing.set()
        storage_admission.check(force=True)
        prepare_snapshot(root, snapshot)
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
        context, prompt, _source_context = prepare_context(
            root=root, snapshot=snapshot, scratch=scratch, environment=environment, allowed=allowed
        )
        if source_draft_guidance:
            context["source_draft_guidance"] = source_draft_guidance
            if source_draft_guidance not in prompt:
                prompt += "\n" + source_draft_guidance
        initial_prompt = prompt
        receipt: dict[str, Any] = {}
        validation_lock = threading.Lock()
        last_validation: dict[str, Any] = {}
        correction_rounds = int(state.get("formalization_correction_rounds", 0))
        correction_limit = int(environment.get("LEANFLOW_FORMALIZATION_CORRECTION_ROUNDS", "2"))
        if not 0 <= correction_limit <= 8:
            raise ValueError("Formalization correction rounds must be between 0 and 8")

        def submit(response: str) -> dict[str, Any]:
            """Freeze one proposal at a time and retain only independently accepted bytes."""
            with validation_lock:
                receipt.clear()
                atomic_json_write(directory / "accepted.json", {"accepted": False})
                if observer is not None:
                    observer.publish(state, budget, phase="verifying")
                arguments: dict[str, Any] = dict(
                    root=root,
                    snapshot=snapshot,
                    baseline=baseline,
                    directory=directory / "checks",
                    allowed_axioms=allowed,
                    deadline=deadline,
                    cancelled=cancelled.is_set,
                )
                if kind == "formalize" and source_draft_blocker is not None:
                    checked = dict(source_draft_blocker)
                    if (
                        current_selection_token(root, document_id)
                        != state["formalization_selection_token"]
                    ):
                        checked = {
                            "accepted": False,
                            "error_code": "selection_changed",
                            "error": "Source inventory selection changed; restart from its selected revision",
                        }
                elif kind == "formalize":
                    from leanflow_cli.formalization.proposal import check_proposal
                    from leanflow_cli.formalization.review import review_configuration

                    checked = check_proposal(
                        **arguments,
                        document_id=document_id,
                        base_revision_id=environment["LEANFLOW_FORMALIZATION_REVISION"],
                        selection_token=state["formalization_selection_token"],
                        review_policy=review_configuration(
                            backend,
                            environment.get("LEANFLOW_NATIVE_MODEL", ""),
                            environment.get("LEANFLOW_NATIVE_REASONING_EFFORT", ""),
                            environment,
                        ),
                    )
                else:
                    checked = verify_proposal(**arguments, workflow=kind, active_file=active)
                last_validation.clear()
                last_validation.update(
                    {
                        key: value
                        for key, value in checked.items()
                        if key not in {"files", "profiles", "record", "frozen_root"}
                    }
                )
                if checked.get("accepted"):
                    receipt.clear()
                    receipt.update(checked)
                    atomic_json_write(directory / "accepted.json", checked)
                return {
                    key: value
                    for key, value in checked.items()
                    if key not in {"files", "profiles", "record", "frozen_root"}
                }

        tools = SessionTools(
            role="prover",
            project_root=snapshot,
            workspace=scratch,
            context=context,
            timeout_s=None,
            deadline=deadline,
            cancelled=cancelled.is_set,
            allow_internet=not restricted,
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
                storage_admission.check(force=True)
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
                    if event.get("type") == "harness.settings":
                        settings = {key: value for key, value in event.items() if key != "type"}
                        state.setdefault("model_settings", []).append(settings)
                        atomic_json_write(state_path, state)
                        if observer is not None:
                            observer.event("agent-settings", settings)
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
                            restricted_tools=restricted,
                            prompt=(
                                "External research is disabled. Use only the LeanFlow bridge tools. "
                                "Call get_assignment to recover the full task. Use start_command and "
                                "poll_operation for local commands and changes; native file edits are read-only.\n"
                                if restricted
                                else ""
                            )
                            + (
                                initial_prompt + "\n\nCONTROLLER FEEDBACK\n" + prompt
                                if restricted and prompt != initial_prompt
                                else prompt
                            ),
                            cwd=snapshot,
                            model=environment.get("LEANFLOW_NATIVE_MODEL", ""),
                            effort=environment.get("LEANFLOW_NATIVE_REASONING_EFFORT", ""),
                            session_id="" if restricted else str(state.get("session_id", "")),
                            previous_usage=None if restricted else state.get("usage_total"),
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
                final_checked = kind == "formalize" and bool(receipt)
                if final_checked:
                    # The native process can edit after submit_files. Its final
                    # response cannot select earlier accepted bytes implicitly.
                    submit(result.final_response)
                if not receipt and last_validation.get("status") != "check_error":
                    try:
                        reported = json.loads(result.final_response)
                    except (ValueError, TypeError):
                        reported = None
                    if isinstance(reported, dict) and reported.get("status") == "blocked":
                        state.update(
                            status="blocked", error=str(reported.get("reason", "External blocker"))
                        )
                        return state
                    if not final_checked:
                        submit(result.final_response)
                if last_validation.get("error_code") == "selection_changed":
                    state.update(status="blocked", error=last_validation["error"])
                    return state
                if last_validation.get("status") == "source_blocked":
                    state.update(status="blocked", error=last_validation["error"])
                    return state
                if kind == "formalize" and last_validation.get("status") == "check_error":
                    state.update(
                        status={
                            "check_cancelled": "cancelled",
                            "check_timeout": "deadline_exceeded",
                        }.get(str(last_validation.get("error_code", "")), "blocked"),
                        error=last_validation.get(
                            "error", "Formalization checking could not finish"
                        ),
                    )
                    return state
                if receipt.get("accepted"):
                    if kind == "formalize":
                        from leanflow_cli.formalization.review import review_revision

                        if observer is not None:
                            observer.publish(state, budget, phase="source-review")
                        review = review_revision(
                            receipt["record"],
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
                        record = receipt["record"]
                        save_receipt(
                            record,
                            {
                                "schema_version": 1,
                                "document_id": document_id,
                                "revision_id": record["revision_id"],
                                "status": (
                                    "review_passed"
                                    if review.get("accepted")
                                    else (
                                        "review_rejected"
                                        if review.get("status") == "completed"
                                        else "review_error"
                                    )
                                ),
                                "error": review.get("error", review.get("feedback", "")),
                                "review_status": review.get("status"),
                                "checks": record["checks"],
                                "reviews": review.get("reviews", []),
                                "handoff": None,
                            },
                        )
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
                            if correction_rounds >= correction_limit or (
                                bool(review.get("reviews"))
                                and all(
                                    row.get("cache_reused")
                                    for row in review["reviews"]
                                    if not row.get("accepted")
                                )
                            ):
                                state.update(
                                    status="blocked",
                                    error="Source review needs correction; unchanged rejections are not rerolled. "
                                    + str(review.get("feedback", "")),
                                )
                                return state
                            correction_rounds += 1
                            state["formalization_correction_rounds"] = correction_rounds
                            atomic_json_write(state_path, state)
                            receipt.clear()
                            prompt = (
                                "Independent source review rejected the draft. Repair the source mapping and statements, then submit_files again. Review feedback: "
                                + json.dumps(review)
                            )
                            continue
                        from leanflow_cli.formalization.publish import publish_checked

                        publication = publish_checked(
                            root,
                            baseline,
                            {
                                name: bytes.fromhex(value)
                                for name, value in state.get("formalization_baseline", {}).items()
                            },
                            receipt,
                            review,
                            directory / "publication",
                            cancelled=cancelled.is_set,
                        )
                        state.update(
                            status=(
                                "draft_ready"
                                if publication["status"] == "published"
                                else "accepted_staged"
                            ),
                            formalization_revision=receipt["revision_id"],
                            publication=publication,
                            promoted_files=(
                                list(receipt["files"])
                                if publication["status"] == "published"
                                else []
                            ),
                        )
                        return state
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
    except StoragePressure as error:
        storage_error = str(error)
        return state
    except Exception as error:
        state.update(status="error", error=str(error))
        return state
    finally:
        cancelled.set()
        finished.set()
        ticker.join()
        if storage_error:
            state.update(
                status="storage_pressure",
                error=storage_error,
                next_step="Free disk space or adjust storage.min_free_gib, then resume. Proofs, checkpoints and consumed calls are preserved.",
            )
        try:
            budget.persist()
            state["terminal"] = True
            atomic_json_write(state_path, state)
            # Retain continuation files; only rebuildable dependency views expire.
            try:
                release_snapshot_dependencies(snapshot)
            except (OSError, ValueError) as error:
                state["cache_cleanup_error"] = str(error)
                atomic_json_write(state_path, state)
            if observer is not None and observing.is_set():
                observer.publish(state, budget)
                observer.finish(state)
        finally:
            # Storage/status failures must not leak controller process state.
            for previous_signum, handler in previous_signals.items():
                signal.signal(previous_signum, handler)


def main() -> int:
    """Pin managed runtime files throughout the detached native controller."""
    from leanflow_cli.runtime.runtime_install import running_runtime_lease

    with running_runtime_lease():
        return _main()


def _main() -> int:
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
