"""Gate native proof sessions on LeanProbe acceptance in their actual private workspace."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import remaining_seconds
from core.utils import atomic_json_write
from leanflow_cli.workflows.prover import check_process
from leanflow_cli.workflows.prover.session_build import build_session_imports
from leanflow_cli.workflows.prover.session_tools import SessionTools


def check_session_setup(
    toolset: SessionTools,
    runtime_dir: Path,
    emit: Callable[[str, dict[str, Any]], None],
) -> dict[str, Any]:
    """Check the real sandbox, retain diagnostics, and keep the checker worker ready."""
    scratch_file = toolset.workspace / "Scratch.lean"
    details = {
        "project_root": str(toolset.project_root),
        "workspace": str(toolset.workspace),
        "checker": "LeanProbe",
    }
    emit("job-setup-start", details)
    source_file = scratch_file if scratch_file.is_file() else None
    prerequisites = build_session_imports(
        project_root=toolset.project_root,
        workspace=toolset.workspace,
        source_file=source_file,
        deadline=toolset.deadline,
        cancelled=toolset.cancelled,
    )
    result = prerequisites
    if prerequisites.get("success") is True:
        result = check_process.preflight_check(
            project_root=toolset.project_root,
            workspace=toolset.workspace,
            source_file=source_file,
            timeout_s=remaining_seconds(toolset.deadline),
            cancelled=toolset.cancelled,
        )
    evidence = {
        **result,
        **details,
        "prerequisite_build": prerequisites,
        "accepted": result.get("success") is True and result.get("ok") is True,
    }
    atomic_json_write(runtime_dir / "lean-setup.json", evidence)
    emit("job-setup-result", evidence)
    return evidence
