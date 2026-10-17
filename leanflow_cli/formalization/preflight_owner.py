"""Publish a verified, cancellable CLI preparation owner before native-controller handoff."""

from __future__ import annotations

import os
import re
import secrets
import signal
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_value
from core.campaign_limits import AccountingError, CampaignLimits
from core.process_identity import (
    PROCESS_TOKEN_ENV,
    _process_token,
    current_process_identity,
    process_token_sha256,
)
from leanflow_cli.formalization.preparation_campaign import CampaignOwner
from leanflow_cli.runtime.workflow_resources import WorkflowResourceWindow
from leanflow_cli.workflows.workflow_json_io import WorkflowStateLockTimeout
from leanflow_cli.workflows.workflow_state import (
    mark_workflow_live_status_startup,
    publish_workflow_preflight_progress,
    release_workflow_startup_owner,
    workflow_live_status_path,
    workflow_run_activity_path,
)

_REEXEC = "leanflow_formalization_preflight_token"


class PreflightProgress:
    """Forward bounded metadata on one joined worker, leaving native callbacks free of file locks."""

    def __init__(
        self,
        run_id: str,
        cancelled: threading.Event,
        resources: WorkflowResourceWindow,
        *,
        enabled: bool,
        accounting_owner: CampaignOwner | None = None,
    ) -> None:
        self.run_id, self.cancelled, self.resources = run_id, cancelled, resources
        self.enabled = enabled
        self._accounting_owner = accounting_owner
        self.owner = current_process_identity()
        self.status_path = workflow_live_status_path()
        self.activity_path = workflow_run_activity_path(run_id)
        self._secrets = tuple(
            value
            for key, value in os.environ.items()
            if value and re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", key)
        )
        self._lock = threading.Lock()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None
        self._closing = False
        self._worker_finalized = False
        self._error: Exception | None = None
        self._budget: CampaignLimits | None = None
        self._phase, self._message = "starting", "Checking setup before formalization"
        self._sequence, self._published = 0, -1
        self._settings: dict[str, str] = {}
        self._settings_dirty = False
        self._failure: tuple[str, str] | None = None

    def start(self) -> None:
        """Checkpoint accounting periodically, publishing status only for verified live owners."""
        self._thread = threading.Thread(
            target=self._run, name="leanflow-preflight-progress", daemon=True
        )
        if self._accounting_owner is not None:
            self._accounting_owner.attach_worker()
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            if self._accounting_owner is not None:
                self._accounting_owner.close(worker=True)
            raise

    def bind_budget(self, budget: CampaignLimits) -> None:
        """Use the real shared campaign object without fabricating counts from native events."""
        with self._lock:
            if self._closing or self._worker_finalized or self._budget is not None:
                raise RuntimeError("Preflight accounting writer cannot be replaced or rebound")
            try:
                budget.begin_checkpoints()
            except BaseException as error:
                self.cancelled.set()
                budget.fail_accounting(error)
                raise
            self._budget = budget

    def bind_accounting_owner(self, owner: CampaignOwner) -> None:
        """Admit the existing checkpoint worker only after baseline/source checks pass."""
        with self._lock:
            if self._worker_finalized:
                raise RuntimeError("Preflight checkpoint worker already finalized")
            if self._closing or self._accounting_owner is not None:
                raise RuntimeError("Preflight accounting ownership cannot be replaced")
            if self._thread is not None:
                if not self._thread.is_alive():
                    raise RuntimeError("Preflight checkpoint worker already stopped")
                owner.attach_worker()
            self._accounting_owner = owner

    def phase(self, event: dict[str, Any]) -> None:
        """Record controller phase labels while ignoring source/output message text."""
        if event.get("type") == "output":
            return
        phase = event.get("phase")
        labels = {
            "checking_baseline": ("reconciling", "Checking existing setup and frozen source"),
            "dependency_plan": ("dependency_plan", "Estimating mathematical dependencies"),
            "project_setup": ("project_setup", "Preparing and checking project dependencies"),
            "ready": ("project_setup", "Project setup passed; handing off to translation"),
        }
        selected = labels.get(
            phase if isinstance(phase, str) else "",
            ("project_setup", "Preparing and checking project dependencies"),
        )
        with self._lock:
            if not self._closing:
                self._phase, self._message = selected
                self._sequence += 1

    def native_event(self, event: dict[str, Any]) -> None:
        """Retain only reported settings or generic tool activity, never prompts or arguments."""
        with self._lock:
            if self._closing:
                return
            if event.get("type") == "harness.settings":
                values = {
                    key: event[key]
                    for key in (
                        "model",
                        "reasoning_effort",
                        "requested_model",
                        "requested_reasoning_effort",
                    )
                    if isinstance(event.get(key), str) and event[key]
                }
                safe = redact_sensitive_value(values, exact_secrets=self._secrets)
                self._settings.update({key: value[:500] for key, value in safe.items()})
                self._settings_dirty = self._settings_dirty or bool(values)
            elif (
                isinstance(event.get("item"), dict) and event["item"].get("type") == "mcp_tool_call"
            ):
                self._message = "Dependency planner is reading frozen evidence"
            elif event.get("type") == "turn.started":
                self._message = "Native dependency planner started"
            else:
                return
            self._sequence += 1

    def failed(self, error: BaseException, *, code: str = "") -> None:
        """Expose a bounded redacted failure from this preparation, preserving specific classification."""
        with self._lock:
            if self._closing or self._failure is not None:
                return
            selected = (
                code
                or str(getattr(error, "code", ""))
                or ("preflight_cancelled" if self.cancelled.is_set() else "preflight_failed")
            )
            if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", selected) is None:
                selected = "preflight_failed"
            message = redact_sensitive_value(
                str(error) or type(error).__name__, exact_secrets=self._secrets
            )[:1000]
            self._failure = selected, message
            self._sequence += 1

    def _publish(self) -> None:
        with self._lock:
            budget, phase, message = self._budget, self._phase, self._message
            sequence, settings, settings_dirty = (
                self._sequence,
                dict(self._settings),
                self._settings_dirty,
            )
            failure = self._failure
        metrics: dict[str, Any] = {}
        if budget is not None:
            budget.persist()
        if not self.enabled:
            return
        if budget is not None:
            # Never hold a budget lock during status/activity file ownership.
            with budget.lock:
                calls = [
                    call
                    for call in budget.state["invocations"].values()
                    if call["status"] != "not_started"
                ]
                known = all(call.get("usage") is not None for call in calls)
                tokens = sum(
                    sum(
                        (call.get("usage") or {}).get(key, 0)
                        for key in ("input_tokens", "output_tokens")
                    )
                    for call in calls
                )
                metrics = {
                    "agent_calls": len(calls),
                    "running_calls": sum(
                        call["status"] in {"reserved", "running"} for call in calls
                    ),
                    "reported_tokens": tokens if known else None,
                    "known_reported_tokens": tokens,
                    "usage_known": known,
                    "elapsed_s": round(budget.elapsed(), 3),
                    "limits": dict(budget.limits),
                }
        activity = None
        if sequence != self._published:
            activity = {
                "type": "agent-settings" if settings_dirty else "preflight-progress",
                "message": (
                    "Native dependency planner reported model settings"
                    if settings_dirty
                    else message
                ),
                "details": {**settings, "phase": phase} if settings_dirty else {"phase": phase},
            }
            if failure is not None:
                activity = {
                    "type": "preflight-error",
                    "message": failure[1],
                    "details": {"phase": phase, "error_code": failure[0]},
                }
        try:
            published = publish_workflow_preflight_progress(
                self.run_id,
                self.owner,
                {
                    "phase": phase,
                    "native_metrics": metrics,
                    "preflight_resources": self.resources.evidence(),
                    # A reported error does not release the live owner: cleanup
                    # must finish before project setup can admit another launch.
                    "status": "running",
                    "preflight_error": failure[1] if failure is not None else "",
                    "preflight_error_code": failure[0] if failure is not None else "",
                },
                status_path=self.status_path,
                activity_path=self.activity_path,
                activity=activity,
            )
        except WorkflowStateLockTimeout:
            # Progress is best effort; the final bounded release below must
            # still succeed before this preparation can hand off ownership.
            return
        if not published:
            raise RuntimeError(
                "Formalization preflight ownership changed; stop this earlier launch"
            )
        with self._lock:
            self._published = sequence
            if self._settings == settings:
                self._settings_dirty = False

    def _run(self) -> None:
        try:
            self._publish()
            while not self._finished.wait(1):
                self._publish()
            self._publish()
        except Exception as error:
            self._error = error
            self.cancelled.set()
            if self._budget is not None:
                self._budget.fail_accounting(error)
        finally:
            with self._lock:
                self._worker_finalized = True
                owner = self._accounting_owner
            if owner is not None:
                owner.close(worker=True)

    def check(self) -> None:
        """Surface failed progress ownership instead of continuing under a stale live pointer."""
        if self._error is not None:
            raise AccountingError(
                "Formalization preflight progress could not be published safely"
            ) from self._error

    def finalize_accounting(self) -> None:
        """Clear the marker only after this parent and the exact worker finish budget effects."""
        if self._thread is not None and self._thread.is_alive():
            raise AccountingError("Preflight accounting worker remains active; retain its ledger")
        self.check()
        if self._budget is not None:
            self._budget.finish_checkpoints()

    def close(self) -> None:
        """Ignore late callbacks and join finitely before releasing ownership or changing environment."""
        with self._lock:
            self._closing = True
        self._finished.set()
        if self._thread is not None:
            self._thread.join(3)
            if self._thread.is_alive():
                self.cancelled.set()
                raise RuntimeError(
                    "Formalization preflight progress did not stop; retain its state"
                )
        self.check()


def inherited_process_token() -> bool:
    """Require external visibility instead of the same-process identity shortcut."""
    expected = os.environ.get(PROCESS_TOKEN_ENV, "")
    return bool(
        expected
        and process_token_sha256(_process_token(os.getpid())) == process_token_sha256(expected)
    )


def ensure_cli_preflight_token() -> None:
    """Re-exec an actual formalization CLI once with an initially inherited token."""
    run_id = os.environ.get("LEANFLOW_WORKFLOW_RUN_ID", "").strip()
    if run_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid formalization preflight run identity")
    if inherited_process_token():
        if not run_id:
            os.environ["LEANFLOW_WORKFLOW_RUN_ID"] = "formalize-cli-" + uuid.uuid4().hex[:16]
        return
    if _REEXEC in sys._xoptions:
        raise RuntimeError(
            "Formalization startup token is not externally observable after one re-exec; no preparation or model call started"
        )
    original = list(sys.orig_argv)
    if not original or not all(isinstance(value, str) for value in original):
        raise RuntimeError(
            "Cannot preserve the original Python CLI invocation for verified preparation startup"
        )
    environment = dict(os.environ)
    environment[PROCESS_TOKEN_ENV] = secrets.token_urlsafe(32)
    environment["LEANFLOW_WORKFLOW_RUN_ID"] = run_id or "formalize-cli-" + uuid.uuid4().hex[:16]
    # Preserve -I/-B, -m/-c and literal arguments; exec retains PID, descriptors,
    # session and native authentication while replacing only the initial env.
    try:
        os.execve(
            sys.executable, [sys.executable, "-X", _REEXEC + "=1", *original[1:]], environment
        )
    except OSError as error:
        raise RuntimeError(
            "Cannot restart formalization CLI with a verified startup token; no preparation or model call started"
        ) from error


@contextmanager
def preflight_owner(
    root: Path, environment: Mapping[str, str], *, accounting_owner: CampaignOwner | None = None
) -> Iterator[tuple[threading.Event, WorkflowResourceWindow, PreflightProgress]]:
    """Share preparation cancellation/resources and conditionally release only our live pointer."""
    cancelled = threading.Event()
    resources = WorkflowResourceWindow(environment, cancelled)
    keys = ("LEANFLOW_PROJECT_ROOT", "LEANFLOW_WORKFLOW_RUN_ID", "LEANFLOW_NATIVE_WORKFLOW_KIND")
    previous = {name: os.environ.get(name) for name in keys}
    run_id = str(
        environment.get("LEANFLOW_NATIVE_RESUME_RUN_ID")
        or environment.get("LEANFLOW_WORKFLOW_RUN_ID")
        or ""
    )
    if run_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("Invalid formalization preflight run identity")
    handlers: dict[int, Any] = {}
    claimed, successful, closed = False, False, False
    progress: PreflightProgress | None = None
    try:
        os.environ.update(
            LEANFLOW_PROJECT_ROOT=str(root), LEANFLOW_NATIVE_WORKFLOW_KIND="formalize"
        )
        if run_id:
            os.environ["LEANFLOW_WORKFLOW_RUN_ID"] = run_id
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                handlers[signum] = signal.signal(signum, lambda *_: cancelled.set())
        # Direct API callers keep their existing control contract; only an
        # externally verifiable inherited CLI token can publish Stop authority.
        if run_id and inherited_process_token():
            mark_workflow_live_status_startup(
                phase="starting",
                lock_timeout_s=1.0,
                metadata={
                    "run_id": run_id,
                    "project_root": str(root),
                    "workflow_kind": "formalize",
                    "workflow_command": "/formalize",
                    "status": "running",
                    "terminal": False,
                    "owner_role": "formalization-preflight",
                    "native_metrics": {},
                    "native_session_id": "",
                    "native_state_path": "",
                    "preflight_error": "",
                    "preflight_error_code": "",
                    "last_activity_type": "preflight-progress",
                    "last_activity_message": "Checking setup before formalization",
                    "last_activity_at": datetime.now(UTC).isoformat(),
                },
            )
            claimed = True
        progress = PreflightProgress(
            run_id, cancelled, resources, enabled=claimed, accounting_owner=accounting_owner
        )
        progress.start()
        resources.start()
        if cancelled.is_set():
            raise ValueError("Formalization preparation cancelled before admission")
        yield cancelled, resources, progress
        progress.check()
        resources.check(force=True)
        if cancelled.is_set():
            raise ValueError("Formalization preparation cancelled")
        successful = True
    except BaseException as error:
        if progress is not None:
            progress.failed(error)
        raise
    finally:
        progress_error: Exception | None = None
        if progress is not None:
            try:
                progress.close()
            except Exception as error:
                progress_error = error
        try:
            resources.close()
            closed = True
            if progress is not None and progress_error is None:
                try:
                    progress.finalize_accounting()
                except Exception as error:
                    progress_error = error
                    cancelled.set()
        finally:
            try:
                if claimed and (
                    progress is None or progress._thread is None or not progress._thread.is_alive()
                ):
                    released = release_workflow_startup_owner(
                        run_id,
                        outcome=(
                            "prepared"
                            if successful
                            and closed
                            and progress_error is None
                            and resources.failure is None
                            else (
                                "cancelled"
                                if cancelled.is_set() and closed and resources.failure is None
                                else "failed"
                            )
                        ),
                        metadata={"preflight_resources": resources.evidence()},
                        lock_timeout_s=1.0,
                    )
                    if successful and not released:
                        raise RuntimeError(
                            "Formalization preflight ownership changed before handoff"
                        )
            finally:
                for previous_signum, handler in handlers.items():
                    signal.signal(previous_signum, handler)
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        if progress_error is not None:
            raise progress_error
