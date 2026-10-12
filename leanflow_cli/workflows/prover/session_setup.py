"""Gate native proof sessions on LeanProbe acceptance in their actual private workspace."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import remaining_seconds
from core.utils import atomic_json_write
from leanflow_cli.workflows.prover import check_process
from leanflow_cli.workflows.prover.session_tools import SessionTools


def check_session_setup(
    toolset: SessionTools,
    runtime_dir: Path,
    emit: Callable[[str, dict[str, Any]], None],
) -> dict[str, Any]:
    """Check the real worker sandbox, retain diagnostics, and leave a successful REPL warm."""
    scratch_file = toolset.workspace / "Scratch.lean"
    details = {
        "project_root": str(toolset.project_root),
        "workspace": str(toolset.workspace),
        "checker": "LeanProbe",
    }
    emit("job-setup-start", details)
    result = check_process.preflight_check(
        project_root=toolset.project_root,
        workspace=toolset.workspace,
        source_file=scratch_file if scratch_file.is_file() else None,
        timeout_s=remaining_seconds(toolset.deadline),
        cancelled=toolset.cancelled,
    )
    evidence = {
        **result,
        **details,
        "accepted": result.get("success") is True and result.get("ok") is True,
    }
    atomic_json_write(runtime_dir / "lean-setup.json", evidence)
    emit("job-setup-result", evidence)
    return evidence
