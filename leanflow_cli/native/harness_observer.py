"""Publish native workflow ownership and compact progress to existing shell status."""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from core.campaign_limits import CampaignLimits
from leanflow_cli.workflows.workflow_json_io import WorkflowStateLockTimeout
from leanflow_cli.workflows.workflow_state import (
    append_workflow_activity,
    save_workflow_live_status,
)

_CHECK_FAILURE_SUMMARIES = {
    "check_busy": "Another Lean check still owns this workspace.",
    "check_cancelled": "This Lean check was cancelled.",
    "check_capture_failed": "Lean check output capture was interrupted.",
    "check_environment_changed": "The Lean environment changed during this check.",
    "check_request_too_large": "The Lean check request exceeded its size limit.",
    "check_response_too_large": "Lean check feedback exceeded its size limit.",
    "check_setup_failed": "The Lean checker could not complete this request.",
    "check_timeout": "The Lean check exceeded its time limit.",
    "isolated_lean_check_failed": "The isolated Lean checker could not complete this check.",
    "isolation_unavailable": "The required Lean check isolation is unavailable.",
}


def _check_receipt(receipt: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap one completed operation envelope without inspecting arbitrary tool content."""
    nested = receipt.get("result")
    if isinstance(nested, Mapping) and (
        receipt.get("status") == "completed" or receipt.get("success") is True
    ):
        return nested
    return receipt


class NativeObserver:
    """Expose one CLI-owned native run without copying prompts or tool transcripts.

    Construction has no global side effects. Call ``start`` only in the owning CLI
    process: the existing activity APIs route through its workflow environment.
    """

    def __init__(self, root: Path, run_id: str, kind: str, backend: str) -> None:
        self.root = root.resolve()
        self.run_id = run_id
        self.kind = kind
        self.backend = backend
        self._lock = threading.RLock()
        self._started = False
        self._finished = False
        self._phase = "starting"
        self._metrics: dict[str, Any] = {}
        self._secrets = tuple(
            value
            for name, value in os.environ.items()
            if value and re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", name)
        )

    def _text(self, value: Any, limit: int = 500) -> str:
        """Redact configured credentials before shortening a public scalar."""
        text = str(value or "")
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return redact_sensitive_text(text, force=True)[:limit]

    def _activity(self, kind: str, message: str, **details: Any) -> None:
        """Supply explicit context so activity enrichment cannot copy user prompts."""
        try:
            append_workflow_activity(
                kind,
                self._text(message),
                lock_timeout_s=0.0,
                process_id=os.getpid(),
                agent_session_id="native-" + self.run_id,
                project_root=str(self.root),
                workflow_kind=self.kind,
                workflow_command="/" + self.kind,
                effective_prompt="",
                active_skill="",
                run_scope="top-level",
                provider=self.backend,
                **details,
            )
        except WorkflowStateLockTimeout:
            pass

    @contextmanager
    def _display_lock(self) -> Iterator[bool]:
        """Skip display work while another callback owns its bounded publication."""
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    def start(self, state: Mapping[str, Any]) -> None:
        """Claim live process ownership before announcing the native run."""
        with self._display_lock() as acquired:
            if not acquired:
                return
            if self._started:
                return
            os.environ.update(
                LEANFLOW_PROJECT_ROOT=str(self.root),
                LEANFLOW_WORKFLOW_RUN_ID=self.run_id,
                LEANFLOW_NATIVE_WORKFLOW_KIND=self.kind,
            )
            self.publish(state, phase="starting")
            self._started = True
            self._activity("runner-start", f"Native {self.kind} workflow started", status="running")

    def publish(
        self,
        state: Mapping[str, Any],
        budget: CampaignLimits | None = None,
        phase: str | None = None,
    ) -> None:
        """Publish scalar progress while preserving unknown native usage as unknown."""
        with self._display_lock() as acquired:
            if not acquired:
                return
            if self._finished:
                return
            if phase is not None:
                self._phase = phase
            if budget is not None:
                if not budget.lock.acquire(blocking=False):
                    return
                try:
                    calls = [
                        call
                        for call in budget.state["invocations"].values()
                        if call["status"] != "not_started"
                    ]
                    known = all(call.get("usage") is not None for call in calls)
                    reported = sum(
                        sum(
                            (call.get("usage") or {}).get(key, 0)
                            for key in ("input_tokens", "output_tokens")
                        )
                        for call in calls
                    )
                    self._metrics = {
                        "agent_calls": len(calls),
                        "running_calls": sum(
                            call["status"] in {"reserved", "running"} for call in calls
                        ),
                        "reported_tokens": reported if known else None,
                        "known_reported_tokens": reported,
                        "usage_known": known,
                        "elapsed_s": round(budget.elapsed(), 3),
                        "limits": dict(budget.limits),
                    }
                finally:
                    budget.lock.release()
            timestamp = datetime.now(UTC).isoformat()
            try:
                self._save_status(state, timestamp)
            except WorkflowStateLockTimeout:
                pass

    def _save_status(self, state: Mapping[str, Any], timestamp: str) -> None:
        """Publish through a nonblocking status sidecar attempt."""
        save_workflow_live_status(
            {
                "run_id": self.run_id,
                "project_root": str(self.root),
                "process_id": os.getpid(),
                "updated_at": timestamp,
                "runtime_heartbeat_at": timestamp,
                "workflow_kind": self.kind,
                "workflow_command": "/" + self.kind,
                "provider": self.backend,
                "phase": self._phase,
                "status": self._text(state.get("status", "running")),
                "terminal": bool(state.get("terminal", False)),
                "native_session_id": self._text(state.get("session_id", "")),
                "native_state_path": str(
                    self.root / ".leanflow/native-workflows" / self.run_id / "state.json"
                ),
                "native_metrics": dict(self._metrics),
                "current_blocker": self._text(state.get("error", ""), 1000),
            },
            lock_timeout_s=0.0,
        )

    def event(self, kind: str, detail: Mapping[str, Any]) -> None:
        """Append bounded receipts and allowlisted failure summaries, keeping output private."""
        with self._display_lock() as acquired:
            if not acquired:
                return
            if not self._started or self._finished:
                return
            if kind == "agent-settings":
                self._activity(
                    kind,
                    "Native agent reported model settings",
                    **{
                        key: self._text(detail[key])
                        for key in (
                            "invocation_id",
                            "session_id",
                            "observed_at",
                            "model",
                            "model_source",
                            "reasoning_effort",
                            "reasoning_source",
                            "requested_model",
                            "requested_reasoning_effort",
                        )
                        if isinstance(detail.get(key), str)
                    },
                )
                return
            receipt = detail.get("result")
            summary = dict(receipt) if isinstance(receipt, Mapping) else {}
            summary.update(detail)
            compact: dict[str, Any] = {
                key: self._text(summary[key], 200)
                for key in ("operation_id", "tool", "status", "phase")
                if isinstance(summary.get(key), str)
            }
            compact.update(
                {
                    key: summary[key]
                    for key in ("accepted", "success", "completed", "total")
                    if isinstance(summary.get(key), (bool, int))
                }
            )
            tool = compact.get("tool", "operation")
            if kind == "tool-result" and summary.get("tool") == "start_lean_check":
                check = _check_receipt(receipt) if isinstance(receipt, Mapping) else detail
                if check.get("success") is False or check.get("status") == "failed":
                    # Transport completion does not make the nested Lean check successful.
                    compact["success"] = False
                    code = check.get("error_code")
                    failure_summary = (
                        _CHECK_FAILURE_SUMMARIES.get(code) if isinstance(code, str) else None
                    )
                    if failure_summary is not None:
                        compact["error_code"] = self._text(code, 80)
                    compact["failure_summary"] = self._text(
                        failure_summary
                        or "The Lean check failed; details remain in the private tool log.",
                        200,
                    )
            message = {
                "tool-start": f"Native tool started: {tool}",
                "tool-result": f"Native tool finished: {tool}",
            }.get(kind, f"Native workflow event: {self._text(kind, 80)}")
            self._activity(self._text(kind, 80), message, **compact)

    def finish(self, state: Mapping[str, Any]) -> None:
        """Publish terminal ownership and the exit event consumed by run discovery."""
        with self._display_lock() as acquired:
            if not acquired:
                return
            if not self._started or self._finished:
                return
            terminal = dict(state)
            status = str(terminal.get("status", "error"))
            if status in {"running", "starting"}:
                status = "error"
                terminal["error"] = "Native controller exited without a terminal result"
            terminal.update(status=status, terminal=True)
            # Existing readers recognize exited as terminal even after the PID dies.
            terminal_phase = (
                "completed" if status in {"completed", "completed_with_ambiguities"} else "exited"
            )
            self.publish(terminal, phase=terminal_phase)
            code = (
                0
                if status in {"completed", "draft_ready", "completed_with_ambiguities"}
                else 130 if status == "cancelled" else 1
            )
            self._activity(
                "runner-exit",
                f"Native {self.kind} workflow finished: {status}",
                status=status,
                phase=terminal_phase,
                exit_code=code,
            )
            self._finished = True
