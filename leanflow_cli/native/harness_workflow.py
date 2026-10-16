"""Run source formalization through native harnesses and independent acceptance."""

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
from agent.runtime.harness.models import NativeCleanupUnverified, NativeLaunchFailure
from agent.runtime.harness.network_policy import restricted_research
from core.campaign_limits import CampaignLimits, LimitReached
from core.utils import atomic_json_write
from leanflow_cli.formalization.records import current_selection_token, save_receipt
from leanflow_cli.formalization.stage_errors import RepreparationRequired
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.native.harness_context import prepare_context
from leanflow_cli.native.harness_observer import NativeObserver
from leanflow_cli.runtime.project_operations import project_activity
from leanflow_cli.runtime.storage import StoragePressure
from leanflow_cli.runtime.workflow_resources import WorkflowResourceFailure, WorkflowResourceWindow
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
    kind = environment.get("LEANFLOW_NATIVE_WORKFLOW_KIND", "")
    if kind != "formalize":
        raise ValueError(
            "The native formalization controller accepts only formalize; use the dedicated prover for prove"
        )
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

        completed_draft = None
        if resume:
            from leanflow_cli.formalization.source_draft_recovery import completed_draft_state

            draft_intent = environment.get("LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY", "false")
            if draft_intent not in {"true", "false"}:
                raise ValueError("Invalid explicit source-draft-only intent")
            completed_draft = completed_draft_state(
                root,
                environment,
                explicit=draft_intent == "true",
                backend=backend,
            )
        if completed_draft is None:
            recover_pending_publication(root)
            assert_no_pending_publication(root)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "controller.lock").open("a") as owner:
            try:
                fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("This native workflow already has an active controller") from error
            if completed_draft is not None:
                from leanflow_cli.formalization.source_draft_recovery import recover_completed_draft

                return recover_completed_draft(root, directory, completed_draft, environment)
            return _run_controller(
                environment, root, backend, directory, run_id, resume, observe=env is None
            )


def _run_controller(
    environment: dict[str, str],
    root: Path,
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
    source_draft_intent = environment.get("LEANFLOW_FORMALIZATION_SOURCE_DRAFT_ONLY", "false")
    if source_draft_intent not in {"true", "false"}:
        raise ValueError("Invalid explicit source-draft-only intent")
    explicit_source_draft = source_draft_intent == "true"
    preview_layout: dict[str, Any] | None = None
    if resume:
        from leanflow_cli.formalization.source_draft_recovery import (
            completed_draft_state,
            recover_completed_draft,
        )

        completed = completed_draft_state(
            root, environment, explicit=explicit_source_draft, backend=backend
        )
        if completed is not None:
            return recover_completed_draft(root, directory, completed, environment)
    if not document_id:
        raise ValueError("Formalization requires a frozen document inventory; restart the launch")
    from leanflow_cli.formalization.intake import (
        author_blocking_diagnostics,
        draft_source_guidance,
        draft_source_rejection,
        preflight_dependencies,
    )
    from leanflow_cli.formalization.inventory_authority import complete_inventory
    from leanflow_cli.formalization.records import assert_author_selection, load_document

    selected_document = load_document(root, document_id)
    assert_author_selection(selected_document, run_id, current_selection_token(root, document_id))
    selected_inventory = complete_inventory(selected_document)
    blockers = [
        row["message"]
        for row in author_blocking_diagnostics(
            selected_inventory, source_draft_only=explicit_source_draft
        )
    ]
    if blockers:
        raise ValueError("Repair source inventory before formalization: " + "; ".join(blockers))
    source_draft_guidance = draft_source_guidance(
        selected_inventory, source_draft_only=explicit_source_draft
    )
    source_draft_blocker = draft_source_rejection(
        selected_inventory, source_draft_only=explicit_source_draft
    )
    if source_draft_blocker is not None:
        from leanflow_cli.formalization.source_draft import assert_unaccepted_draft

        assert_unaccepted_draft(selected_document)
    requested_revision = environment.get("LEANFLOW_FORMALIZATION_REVISION", "")
    if requested_revision != selected_document["revision_id"] and not (
        requested_revision == selected_document.get("author_base_revision")
        and selected_document.get("author_run_id") == run_id
    ):
        raise ValueError("Source inventory changed; restart from its selected revision")
    state_path = directory / "state.json"
    if resume:
        state = json.loads(state_path.read_text())
        if state.get("native_cleanup_unverified"):
            raise ValueError(
                "Prior native process closure is unverified; retain its private inputs and start a separate campaign"
            )
        restricted = restricted or state.get("research_policy") == "restricted-v1"
        if (
            state.get("version") != 2
            or state.get("backend") != backend
            or state.get("workflow") != "formalize"
        ):
            raise ValueError(
                "Start a new native run when changing backends or migrating legacy runs"
            )
        if bool(state.get("source_draft_intent", False)) != explicit_source_draft:
            raise ValueError("Start a new campaign when changing source-draft-only intent")
        if bool(state.get("source_draft_only", False)) != (source_draft_blocker is not None):
            raise ValueError("Effective source-draft mode changed; start a new campaign")
        if restricted and state.get("research_policy") != "restricted-v1":
            raise ValueError(
                "Start a new run for clean-room testing; this run contains earlier online or unclassified context."
            )
        baseline = {path: bytes.fromhex(data) for path, data in state["baseline"].items()}
        if source_files(root) != baseline:
            raise ValueError("Canonical sources changed; start a new workflow")
        if not state.get("formalization_selection_token") or state[
            "formalization_selection_token"
        ] != current_selection_token(root, document_id):
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
            "workflow": "formalize",
            "baseline": {path: data.hex() for path, data in baseline.items()},
            "status": "running",
            "source_draft_intent": explicit_source_draft,
        }
        if document_id:
            from leanflow_cli.formalization.publish import portable_baseline

            state["formalization_baseline"] = {
                name: data.hex() for name, data in portable_baseline(root, document_id).items()
            }
            state["formalization_selection_token"] = current_selection_token(
                root, document_id, initialize=True
            )
    state.update(
        status="running",
        terminal=False,
        research_policy="restricted-v1" if restricted else "online-v1",
    )
    if source_draft_blocker is not None:
        state["formalization_document_id"] = document_id
        state["formalization_base_revision"] = selected_document["revision_id"]
        state["source_draft_only"] = True
        state["source_diagnostics"] = source_draft_blocker["source_diagnostics"]
    else:
        state["source_draft_only"] = False
    if restricted:
        environment = {**environment, "LEANFLOW_PROVER_ALLOW_INTERNET": "false"}
    atomic_json_write(state_path, state)
    from leanflow_cli.native.harness_formalization_stages import (
        campaign_budget_limits,
        campaign_budget_path,
    )

    limits_path = campaign_budget_path(root, environment, directory)
    budget = CampaignLimits(limits_path, campaign_budget_limits(limits_path, environment))
    cancelled = threading.Event()
    from leanflow_cli.workflows.prover.check_process import owned_checker_identities

    resources = WorkflowResourceWindow(
        environment, cancelled, already_owned=owned_checker_identities(directory)
    )
    explicit_cancel = threading.Event()
    terminal_submission = threading.Event()

    def execution_stopped() -> bool:
        """Interrupt paid native work when external drift cannot be corrected by the author."""
        return cancelled.is_set() or terminal_submission.is_set()

    storage_admission = StorageAdmission(root)
    storage_error = ""
    finished = threading.Event()
    observing = threading.Event()
    observer = NativeObserver(root, run_id, "formalize", backend) if observe else None
    previous_signals: dict[int, Any] = {}

    def stop_signal(*_: Any) -> None:
        """Distinguish user stop from budget, storage and status cancellation for local retention."""
        explicit_cancel.set()
        state["explicit_cancel_requested"] = True
        cancelled.set()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signals[signum] = signal.signal(signum, stop_signal)

    def heartbeat() -> None:
        nonlocal storage_error
        while not finished.wait(1):
            state["workflow_resources"] = resources.evidence()
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
        resources.start()
        if observer is not None:
            observer.start(state)
            observing.set()
        storage_admission.check(force=True)
        # Dependency census can be expensive. Install campaign monitoring and
        # cancellation before paying that read-only preparation cost.
        checked_layout = preflight_dependencies(root, document_id)
        if source_draft_blocker is not None:
            from leanflow_cli.formalization.lean_layout import generation_layout

            preview_layout = generation_layout(checked_layout, selected_document)
            if not preview_layout.get("owned_prefix") or not preview_layout.get("aggregator"):
                raise ValueError("Source draft requires an allocated private module subtree")
            state["source_draft_layout"] = preview_layout
            atomic_json_write(state_path, state)
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
        context, prompt = prepare_context(
            root=root, snapshot=snapshot, scratch=scratch, environment=environment, allowed=allowed
        )
        if source_draft_guidance:
            context["source_draft_guidance"] = source_draft_guidance
            if source_draft_guidance not in prompt:
                prompt += "\n" + source_draft_guidance
        source_prompt = prompt
        if source_draft_blocker is None:
            from leanflow_cli.native.harness_formalization_stages import stage_assignment

            context, prompt = stage_assignment(root, environment, context, source_prompt)
        elif source_draft_blocker is not None:
            context["formalization_stage"] = {
                "phase": "source_draft",
                "base_revision_id": selected_document["revision_id"],
                "representative_source_ids": selected_document.get("slice_source_ids", []),
                "publication_authority": False,
                "proof_authority": False,
            }
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
                resources.check(force=True)
                if terminal_submission.is_set():
                    return dict(last_validation)
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
                if source_draft_blocker is not None:
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
                elif source_draft_blocker is None:
                    from leanflow_cli.formalization.proposal import check_proposal
                    from leanflow_cli.formalization.review import review_configuration

                    checked = check_proposal(
                        **arguments,
                        document_id=document_id,
                        base_revision_id=environment["LEANFLOW_FORMALIZATION_REVISION"],
                        selection_token=state["formalization_selection_token"],
                        author_run_id=run_id,
                        review_policy=review_configuration(
                            backend,
                            environment.get("LEANFLOW_NATIVE_MODEL", ""),
                            environment.get("LEANFLOW_NATIVE_REASONING_EFFORT", ""),
                            environment,
                        ),
                    )
                resources.check(force=True)
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
                if checked.get("error_code") == "needs_reprepare":
                    terminal_submission.set()
                elif source_draft_blocker is None:
                    try:
                        stage_assignment(root, environment, context, source_prompt)
                    except RepreparationRequired as error:
                        checked = {
                            "accepted": False,
                            "status": "check_error",
                            "error_code": error.error_code,
                            "error": str(error),
                        }
                        receipt.clear()
                        last_validation.clear()
                        last_validation.update(checked)
                        atomic_json_write(directory / "accepted.json", checked)
                        terminal_submission.set()
                    else:
                        bridge.context = dict(context)
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
            cancelled=execution_stopped,
            allow_internet=not restricted,
        )
        from leanflow_cli.native.harness_source_draft import (
            closed_draft_bridge,
            drain_author_bridge,
        )

        with closed_draft_bridge(
            NativeBridge(
                toolset=tools,
                context=context,
                callbacks={"submit_files": submit},
                directory=directory / "bridge",
                on_event=observer.event if observer is not None else None,
            ),
            state,
            state_path,
            draft_only=source_draft_blocker is not None,
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
                resources.check(force=True)
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
                    if source_draft_blocker is not None:
                        from leanflow_cli.native.harness_source_draft import observe_draft_terminal

                        observe_draft_terminal(backend, event, state, state_path)
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

                result = None
                try:
                    state.update(
                        native_cleanup_unverified=True, native_process_cleanup_verified=False
                    )
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
                                cancelled=execution_stopped,
                                mcp_config=mcp_path,
                                log_path=directory / f"{invocation}.jsonl",
                                isolation=isolation,
                                executable=executable,
                                on_started=on_started,
                            ),
                            on_event=capture,
                        )
                    except NativeLaunchFailure as error:
                        result = error.result
                    state["native_process_cleanup_verified"] = result.cleanup_verified is True
                    if source_draft_blocker is not None and (
                        result.status == "completed" or state.get("source_draft_terminal_observed")
                    ):
                        from leanflow_cli.native.harness_source_draft import draft_reported_blocker

                        state.update(
                            source_draft_native_closed=result.cleanup_verified is True,
                            source_draft_quiescent=False,
                            native_status=result.status,
                            final_response=result.final_response,
                            session_id=result.session_id or state.get("session_id", ""),
                            usage_total=(
                                result.usage_total
                                if result.usage_total is not None
                                else state.get("usage_total")
                            ),
                        )
                        if result.status == "completed":
                            state.update(
                                source_draft_completed=True,
                                status="blocked",
                                error=draft_reported_blocker(
                                    result.final_response, source_draft_blocker["error"]
                                ),
                            )
                        atomic_json_write(state_path, state)
                        terminal_submission.set()
                    if result.cleanup_verified is not True:
                        state["native_cleanup_unverified"] = True
                        if result.status == "completed":
                            raise NativeCleanupUnverified(
                                "Successful native output lacks verified process closure; retain invocation inputs"
                            )
                except NativeCleanupUnverified as error:
                    state["native_cleanup_unverified"] = True
                    known = result if result is not None else error.result
                    budget.finish(
                        invocation, known.usage if known is not None else None, started=started
                    )
                    raise
                except BaseException:
                    budget.finish(invocation, None, started=started)
                    raise
                budget.finish(invocation, result.usage, started=started)
                resources.check(force=True)
                drain_author_bridge(
                    bridge, state, state_path, draft_only=source_draft_blocker is not None
                )
                state.update(
                    session_id=result.session_id or state.get("session_id", ""),
                    usage_total=(
                        result.usage_total
                        if result.usage_total is not None
                        else state.get("usage_total")
                    ),
                    final_response=result.final_response,
                    status="running" if result.status == "completed" else result.status,
                    native_status=result.status,
                    error=result.error,
                )
                if source_draft_blocker is not None and result.status == "completed":
                    state["source_draft_completed"] = True
                    state.update(
                        status="blocked",
                        error=draft_reported_blocker(
                            result.final_response, source_draft_blocker["error"]
                        ),
                    )
                atomic_json_write(state_path, state)
                if last_validation.get("error_code") == "needs_reprepare":
                    state.update(
                        status="needs_reprepare",
                        error=last_validation.get(
                            "error", "Check source and project preparation again"
                        ),
                    )
                    return state
                remaining = budget.remaining()
                if remaining is not None and remaining <= 0:
                    raise LimitReached("Global campaign time limit reached")
                if cancelled.is_set():
                    state["status"] = "cancelled"
                    return state
                if result.status != "completed":
                    return state
                if source_draft_blocker is not None:
                    return state
                final_checked = bool(receipt)
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
                if last_validation.get("status") == "check_error":
                    state.update(
                        status={
                            "check_cancelled": "cancelled",
                            "check_timeout": "deadline_exceeded",
                            "needs_reprepare": "needs_reprepare",
                        }.get(str(last_validation.get("error_code", "")), "blocked"),
                        error=last_validation.get(
                            "error", "Formalization checking could not finish"
                        ),
                    )
                    return state
                if receipt.get("accepted"):
                    from leanflow_cli.formalization.review import review_revision

                    if observer is not None:
                        observer.publish(state, budget, phase="source-review")
                    resources.check(force=True)
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
                    resources.check(force=True)
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
                    if (
                        record.get("workflow_protocol") == 2
                        and record.get("proposal_phase") == "representative_slice"
                    ):
                        from leanflow_cli.formalization.slice_gate import save_reviewed_slice

                        slice_receipt = save_reviewed_slice(root, receipt, review)
                        save_receipt(
                            record,
                            {
                                "schema_version": 1,
                                "document_id": document_id,
                                "revision_id": record["revision_id"],
                                "status": "slice_reviewed",
                                "checks": record["checks"],
                                "reviews": review.get("reviews", []),
                                "handoff": None,
                                "slice_id": slice_receipt["slice_id"],
                            },
                        )
                        state.update(
                            formalization_phase="complete",
                            representative_slice=slice_receipt["slice_id"],
                        )
                        atomic_json_write(state_path, state)
                        receipt.clear()
                        last_validation.clear()
                        atomic_json_write(directory / "accepted.json", {"accepted": False})
                        context, prompt = stage_assignment(
                            root, environment, context, source_prompt
                        )
                        bridge.context = dict(context)
                        initial_prompt = prompt
                        prompt += "\nIndependent representative-slice review passed. Expand the remaining source while preserving the accepted slice."
                        continue
                    from leanflow_cli.formalization.publication_io import PublicationCancelled
                    from leanflow_cli.formalization.publish import publish_checked
                    from leanflow_cli.formalization.records import DocumentLockCancelled

                    resources.check(force=True)
                    try:
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
                            deadline=deadline,
                        )
                    except (PublicationCancelled, DocumentLockCancelled) as error:
                        state.update(
                            status=(
                                "deadline_exceeded"
                                if deadline is not None and time.monotonic() >= deadline
                                else "cancelled"
                            ),
                            error=str(error),
                        )
                        return state
                    state.update(
                        status=(
                            "draft_ready"
                            if publication["status"] == "published"
                            else "accepted_staged"
                        ),
                        formalization_revision=receipt["revision_id"],
                        publication=publication,
                        promoted_files=(
                            list(receipt["files"]) if publication["status"] == "published" else []
                        ),
                    )
                    return state
                # The native process has ended, so continuation is another charged invocation.
                feedback = (
                    "Continue the existing assignment. Independent verification rejected the source proposal. Repair these errors, then use submit_files: "
                    + json.dumps(last_validation)
                )
                context, prompt = stage_assignment(root, environment, context, source_prompt)
                bridge.context = dict(context)
                initial_prompt = prompt
                prompt += "\n" + feedback
            state["status"] = "cancelled"
            return state
    except WorkflowResourceFailure as error:
        state.update(status="resource_error", error=str(error), error_code=error.code)
        return state
    except RepreparationRequired as error:
        state.update(status="needs_reprepare", error=str(error))
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
        finished.set()
        ticker.join()
        if (
            state.get("source_draft_completed") or state.get("source_draft_terminal_observed")
        ) and source_draft_blocker is not None:
            from leanflow_cli.formalization.source_draft_recovery import retain_completed_draft

            retain_completed_draft(
                root, directory, state, environment, cancelled=explicit_cancel.is_set
            )
        cancelled.set()
        try:
            resources.close()
        except NativeCleanupUnverified as error:
            state.update(native_cleanup_unverified=True, status="error", error=str(error))
        state["workflow_resources"] = resources.evidence()
        if resources.failure is not None:
            state.update(
                status="resource_error",
                error=str(resources.failure),
                error_code=resources.failure.code,
            )
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
                if state.get("native_cleanup_unverified") or (
                    (
                        state.get("source_draft_terminal_observed")
                        or state.get("source_draft_completed")
                    )
                    and not state.get("source_draft_quiescent")
                ):
                    state["cache_cleanup_error"] = (
                        "Author process closure is unverified; retain its private dependency views and pins"
                    )
                    atomic_json_write(state_path, state)
                else:
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
