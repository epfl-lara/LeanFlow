"""Compile imported private project sources before warming isolated LeanProbe."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import operation_cancelled, remaining_seconds
from leanflow_cli.lean.lean_module_paths import _lean_imports_from_text
from leanflow_cli.workflows.prover import check_process
from leanflow_cli.workflows.prover.check_sandbox import _validated_paths


def _local_imports(project: Path, source: Path | None) -> list[str]:
    """Find scratch imports with sources in the private project, excluding dependencies."""
    if source is None:
        return []
    modules = _lean_imports_from_text(source.read_text(encoding="utf-8"))
    if not modules:
        return []
    suffixes = {module: Path(*module.split(".")).with_suffix(".lean").parts for module in modules}
    found: set[str] = set()
    for directory, names, files in os.walk(project, followlinks=False):
        parent = Path(directory)
        names[:] = [
            name
            for name in names
            if not name.startswith(".")
            and name not in {"node_modules", "__pycache__"}
            and not (parent / name).is_symlink()
        ]
        for name in files:
            path = parent / name
            if path.suffix != ".lean" or path.is_symlink():
                continue
            relative = path.relative_to(project).parts
            for module, suffix in suffixes.items():
                if len(relative) >= len(suffix) and relative[-len(suffix) :] == suffix:
                    found.add(module)
    return [module for module in modules if module in found]


def build_session_imports(
    *,
    project_root: Path,
    workspace: Path,
    source_file: Path | None,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    """Build only imported local modules into private outputs under the campaign deadline.

    Snapshots deliberately omit canonical project artifacts. Lake resolves the
    prerequisites from filtered source while dependencies remain read-only;
    ordinary LeanProbe checks receive no additional write capabilities.
    """
    try:
        if operation_cancelled() or (cancelled is not None and cancelled()):
            return {
                "success": False,
                "ok": False,
                "error_code": "check_cancelled",
                "error": "The controller cancelled private project setup.",
            }
        project, work = _validated_paths(project_root, workspace)
        modules = _local_imports(project, source_file)
        if not modules:
            return {"success": True, "modules": [], "skipped": True}
        build = project / ".lake/build"
        if (project / ".lake").is_symlink() or build.is_symlink():
            raise ValueError("Private project build outputs cannot be linked to another directory.")
        build.mkdir(parents=True, exist_ok=True)
        _validated_paths(project, build)
        result = check_process.isolated_command(
            project_root=project,
            workspace=work,
            argv=["lake", "build", *(f"+{module}:olean" for module in modules)],
            timeout_s=remaining_seconds(deadline),
            extra_writable_roots=(build,),
            cancelled=cancelled,
        )
        if result.get("success") is not True:
            result = {
                **result,
                "ok": False,
                "error_code": result.get("error_code") or "check_prerequisite_build_failed",
                "error": result.get("error")
                or "Private project prerequisites failed to compile: "
                + str(result.get("stderr") or result.get("stdout") or "Lake exited unsuccessfully"),
            }
        return {**result, "modules": modules, "skipped": False}
    except (OSError, ValueError) as exc:
        return {
            "success": False,
            "ok": False,
            "error_code": "check_setup_failed",
            "error": str(exc),
        }
