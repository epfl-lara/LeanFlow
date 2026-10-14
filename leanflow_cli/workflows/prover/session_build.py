"""Compile imported private project sources before warming isolated LeanProbe."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import operation_cancelled, remaining_seconds
from leanflow_cli.formalization.publication import PublicationUnsupported
from leanflow_cli.lean.lean_module_paths import _lean_imports_from_text
from leanflow_cli.runtime.project_build_cache import ProjectBuildCache
from leanflow_cli.runtime.storage import (
    StoragePressure,
    StorageSafetyError,
    ensure_storage_capacity,
)
from leanflow_cli.workflows.prover import check_process
from leanflow_cli.workflows.prover.check_sandbox import _environment, _validated_paths


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
    cache_fresh_snapshot: bool = False,
    static_bound: bool = False,
) -> dict[str, Any]:
    """Build only imported local modules into private outputs under the campaign deadline.

    Snapshots deliberately omit canonical project artifacts. Lake resolves the
    prerequisites from filtered source while dependencies remain read-only;
    ordinary LeanProbe checks receive no additional write capabilities. Cache reuse
    is opt-in and limited to fresh snapshots before native agents receive access.
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
        if static_bound:
            from leanflow_cli.workflows.prover.scoped_imports import prepare_scoped_imports

            return prepare_scoped_imports(
                project_root=project,
                workspace=work,
                modules=modules,
                deadline=deadline,
                cancelled=cancelled,
            )
        build = project / ".lake/build"
        if (project / ".lake").is_symlink() or build.is_symlink():
            raise ValueError("Private project build outputs cannot be linked to another directory.")
        build.mkdir(parents=True, exist_ok=True)
        _validated_paths(project, build)
        ensure_storage_capacity(build)
        cache: ProjectBuildCache | None = None
        cache_metadata: dict[str, Any] = {
            "eligible": False,
            "hit": False,
            "published": False,
            "key": None,
            "reason": "Resumed or unspecified snapshots cannot publish project build artifacts",
        }
        if cache_fresh_snapshot:
            try:
                if any(build.iterdir()):
                    raise ValueError("Fresh snapshots must start without local compiled artifacts")
                with tempfile.TemporaryDirectory(prefix="leanflow-cache-identity-") as temporary:
                    environment = _environment(Path(temporary))
                    cache = ProjectBuildCache(project, modules, environment)
                cache_metadata = cache.metadata
                cache.restore(build)
            except (
                OSError,
                ValueError,
                StorageSafetyError,
                StoragePressure,
                subprocess.SubprocessError,
            ) as exc:
                cache_metadata["reason"] = str(exc)
                cache = None
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
        if result.get("success") is True and cache is not None and not cache.metadata["hit"]:
            try:
                cache.publish(build)
            except (
                OSError,
                ValueError,
                StorageSafetyError,
                StoragePressure,
                subprocess.SubprocessError,
            ) as exc:
                cache_metadata["reason"] = str(exc)
        return {
            **result,
            "modules": modules,
            "skipped": False,
            "artifact_cache": cache_metadata,
        }
    except InterruptedError as exc:
        return {"success": False, "ok": False, "error_code": "check_cancelled", "error": str(exc)}
    except TimeoutError as exc:
        return {
            "success": False,
            "ok": False,
            "timed_out": True,
            "error_code": "check_timeout",
            "error": str(exc),
        }
    except (OSError, ValueError, StoragePressure, PublicationUnsupported) as exc:
        return {
            "success": False,
            "ok": False,
            "error_code": "check_setup_failed",
            "error": str(exc),
        }
