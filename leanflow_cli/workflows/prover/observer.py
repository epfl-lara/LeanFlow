"""Bridge prover state into the existing CLI run history and process ownership."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent.accounting.redact import redact_sensitive_text
from leanflow_cli.workflows.prover.event_preview import event_message, preview_details
from leanflow_cli.workflows.workflow_state import (
    append_workflow_activity,
    append_workflow_run_log,
    reset_workflow_run_log,
    save_workflow_live_status,
)


def exit_code(status: str) -> int:
    """Keep verified success distinct from exhaustion, interruption, and failure."""
    if status == "completed":
        return 0
    if status == "disproved":
        return 3
    if status == "interrupted":
        return 130
    if status in {"blocked", "budget_exhausted", "stopped", "context_limit", "source_conflict"}:
        return 2
    return 1


class RunObserver:
    """Publish compact events while full transcripts stay in each job's own log."""

    def __init__(self, root: Path, run_id: str) -> None:
        self.root = root
        self.run_id = run_id
        os.environ["LEANFLOW_PROJECT_ROOT"] = str(root)
        os.environ["LEANFLOW_WORKFLOW_RUN_ID"] = run_id
        os.environ["LEANFLOW_NATIVE_WORKFLOW_KIND"] = "prove"

    def start(self, state: Mapping[str, Any], *, resumed: bool = False) -> None:
        """Claim observable process ownership before expensive model or Lean startup."""
        reset_workflow_run_log()
        self.publish(state)
        append_workflow_activity(
            "runner-start",
            "Bounded prover controller started",
            process_id=os.getpid(),
            agent_session_id="orchestrator",
            active_skill="lean-bounded-prover",
            resumed=resumed,
            mode=state.get("mode"),
            workflow_kind="prove",
            run_scope="top-level",
        )
        append_workflow_run_log(f"LeanFlow {state.get('mode', 'standard')} prover: {self.run_id}\n")

    def publish(self, state: Mapping[str, Any]) -> None:
        """Project live scalar status without substituting another run's artifacts."""
        dag = state.get("dag") or {}
        nodes = dag.get("nodes", []) if isinstance(dag, dict) else []
        running: Mapping[str, Any] = next(
            (node for node in nodes if node.get("status") == "running"), {}
        )
        remaining = sum(node.get("status") != "proved" for node in nodes)
        save_workflow_live_status(
            {
                "run_id": self.run_id,
                "project_root": str(self.root),
                "process_id": os.getpid(),
                "workflow_kind": "prove",
                "workflow_command": os.getenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove"),
                "active_skill": "lean-bounded-prover",
                "phase": state.get("phase", "starting"),
                "status": state.get("status", "running"),
                "terminal": bool(state.get("terminal")),
                "target_symbol": running.get("name", ""),
                "active_file": running.get("file", ""),
                "active_file_label": running.get("file", ""),
                "sorry_count": remaining,
                "proof_solved": state.get("status") == "completed",
                "prover_mode": state.get("mode"),
                "prover_state_path": str(
                    self.root / ".leanflow/workflow-state/prover" / self.run_id / "state.json"
                ),
                "prover_metrics": state.get("metrics", {}),
                "current_blocker": state.get("error", ""),
            }
        )

    def event(self, kind: str, details: Mapping[str, Any]) -> None:
        """Emit bounded progress events with the job identity used by editor filters.

        The row message says what happened; bounded previews let an editor expand
        the row immediately; ``evidence_id`` resolves the complete record from the
        job's own log through ``leanflow runs event`` without copying transcripts
        into the shared stream.
        """
        keys = (
            "job_id",
            "node_id",
            "evidence_id",
            "role",
            "api_calls",
            "api_budget",
            "model",
            "input_tokens",
            "output_tokens",
            "status",
            "accepted",
            "conditional",
            "final_report_only",
            "tool",
            "error",
        )
        compact = {key: details[key] for key in keys if key in details}
        preview = preview_details(kind, details)
        message = event_message(kind, details, preview)
        append_workflow_activity(
            kind,
            redact_sensitive_text(message[:2000]),
            process_id=os.getpid(),
            agent_session_id=str(details.get("job_id") or "orchestrator"),
            active_skill="lean-bounded-prover",
            **{**preview, **compact},
        )
        if kind in {"job_finished", "candidate_checked", "plan_rejected", "api-error"}:
            append_workflow_run_log(redact_sensitive_text(f"{kind}: {compact}\n"))

    def finish(self, state: Mapping[str, Any]) -> None:
        """Publish terminal state and the exact exit event used by run discovery."""
        self.publish(state)
        code = exit_code(str(state.get("status", "error")))
        append_workflow_activity(
            "runner-exit",
            f"Prover finished: {state.get('status')}",
            process_id=os.getpid(),
            agent_session_id="orchestrator",
            exit_code=code,
            phase=state.get("phase"),
            status=state.get("status"),
            active_skill="lean-bounded-prover",
            prover_metrics=state.get("metrics", {}),
            run_scope="top-level",
        )
        append_workflow_run_log(f"Prover finished: {state.get('status')} (exit {code})\n")
