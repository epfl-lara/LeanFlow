"""Reclaim process-scoped workflow artifacts during native finalization."""

from __future__ import annotations

import os
from pathlib import Path

from core.project_resource_admission import reclaim_process_foreground_waiters
from leanflow_cli.workflows.workflow_state import release_workflow_run_log_owner


def release_native_process_artifacts(project_root: str | os.PathLike[str]) -> None:
    """Release the exact runner's console token and unlocked admission markers.

    Both cleanup steps are attempted. Residual locked waiter markers fail the
    operation because they prove process-owned Lean work was not fully
    quiescent; artifacts owned by another PID or run are never removed.
    """
    failures: list[str] = []
    try:
        release_workflow_run_log_owner()
    except OSError as exc:
        failures.append(f"latest-run owner: {type(exc).__name__}: {str(exc)[:160]}")

    try:
        residual = reclaim_process_foreground_waiters(
            Path(project_root),
            process_id=os.getpid(),
        )
    except OSError as exc:
        failures.append(f"foreground waiters: {type(exc).__name__}: {str(exc)[:160]}")
    else:
        if residual:
            failures.append("locked foreground waiters remain: " + ", ".join(residual))

    if failures:
        raise RuntimeError("; ".join(failures))
