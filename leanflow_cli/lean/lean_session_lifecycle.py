"""Preserve foreground LeanProbe reuse while admitting independent cold checks."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.project_resource_admission import ProjectLeanAdmission, ProjectLeanAdmissionRetained
from core.runtime_modes import dispatch_worker_enabled, low_memory_mode_enabled

_WARM_RETENTION_ENV = "LEANFLOW_LEAN_WARM_RETENTION"
_PROJECT_GENERATIONS: dict[Path, tuple[Any, str | None]] = {}


class LeanColdCheckEnvironmentChanged(RuntimeError):
    """Reject a cold result whose dependency inputs changed during execution."""


def forget_incremental_project_generations(project_root: Path | None = None) -> None:
    """Discard stamps only after the corresponding resident state is retired."""
    if project_root is None:
        _PROJECT_GENERATIONS.clear()
    else:
        _PROJECT_GENERATIONS.pop(project_root.resolve(), None)


def observe_incremental_project(project_root: Path, probe: Any) -> None:
    """Record dependency provenance once before this owned probe serves warm state.

    Repeated proof attempts reuse this stamp without rescanning imported modules.
    Cold boundaries compare it against current compiled and loader inputs.
    Source edits remain with the caller's source/prefix checks and do not retire
    unchanged imported environments. Unreadable inputs remain unconfirmed and
    must be retired at the next cold boundary.
    """
    from leanflow_cli.lean import lean_incremental

    if incremental_session_reclaim_required() or lean_incremental._PROBE is not probe:
        return
    root = project_root.resolve()
    previous = _PROJECT_GENERATIONS.get(root)
    if (
        previous is not None
        and previous[0] is probe
        and lean_incremental.incremental_sessions_active(root)
    ):
        return
    from leanflow_cli.workflows.prover.lean_environment import lean_loader_generation

    try:
        generation: str | None = lean_loader_generation(root)
    except Exception:
        generation = None
    _PROJECT_GENERATIONS[root] = (probe, generation)


@dataclass(frozen=True)
class IncrementalSessionPreparation:
    """Distinguish permission to start a cold checker from actual reclamation."""

    ready: bool
    reclaimed: bool


def incremental_session_reclaim_required() -> bool:
    """Resolve whether a cold check must reclaim this process's warm sessions.

    Foreground ``auto`` and ``retain`` reuse the declared normal-resource mode;
    they do not estimate available RAM. Low-memory and dispatch workers always
    reclaim, including when retention is requested explicitly. Unknown settings
    reclaim conservatively instead of disabling the memory protection.
    """
    if low_memory_mode_enabled() or dispatch_worker_enabled():
        return True
    configured = str(os.getenv(_WARM_RETENTION_ENV, "auto") or "auto").strip().lower()
    return configured not in {"auto", "retain"}


def prepare_incremental_sessions_for_cold_check(
    admission: ProjectLeanAdmission,
    *,
    close_failure_reason: str,
) -> IncrementalSessionPreparation:
    """Keep useful foreground sessions or reclaim them before independent Lean.

    A failed required close retains the admission slot and refuses another Lean
    process. Retention changes only process lifetime: the cold checker still
    owns a fresh process and its own source acceptance.
    """
    if not incremental_session_reclaim_required():
        return IncrementalSessionPreparation(ready=True, reclaimed=False)

    from leanflow_cli.lean.lean_incremental import close_incremental_sessions

    reclaimed = close_incremental_sessions()
    if not reclaimed:
        admission.retain_until_process_exit(close_failure_reason)
    return IncrementalSessionPreparation(ready=reclaimed, reclaimed=reclaimed)


def _invalidate_retained_sessions(admission: ProjectLeanAdmission, project_root: Path) -> None:
    """Refuse further Lean work when changed resident dependencies cannot close."""
    from leanflow_cli.lean.lean_incremental import close_incremental_project

    if close_incremental_project(project_root):
        return
    reason = "owned LeanProbe project close failed during dependency invalidation"
    admission.retain_until_process_exit(reason)
    raise ProjectLeanAdmissionRetained(str(project_root), reason)


@contextmanager
def retained_incremental_cold_check_guard(
    admission: ProjectLeanAdmission,
    project_root: Path,
    *,
    allow_build_updates: bool = False,
) -> Iterator[None]:
    """Check retained provenance before and after every independent cold check.

    Skip dependency scans without a resident probe or when the resource mode
    already requires reclamation. A scanner failure invalidates conservatively;
    failed invalidation keeps the admission slot and fails verification closed.
    A change before entry retires stale imports even when the cold command is a
    no-op build or exact-source check. Changes during execution retire retained
    state and reject that result so callers retry against stable dependencies.
    """
    from leanflow_cli.lean import lean_incremental
    from leanflow_cli.workflows.prover.lean_environment import lean_loader_generation

    if incremental_session_reclaim_required() or not lean_incremental.incremental_sessions_active(
        project_root
    ):
        yield
        return
    try:
        generation = lean_loader_generation(project_root)
    except Exception:
        _invalidate_retained_sessions(admission, project_root)
        yield
        return
    root = project_root.resolve()
    recorded = _PROJECT_GENERATIONS.get(root)
    if recorded is None or recorded[0] is not lean_incremental._PROBE or recorded[1] != generation:
        _invalidate_retained_sessions(admission, root)
    try:
        yield
    finally:
        try:
            changed = generation != lean_loader_generation(project_root)
        except Exception:
            changed = True
        if changed:
            _invalidate_retained_sessions(admission, project_root)
            if not allow_build_updates:
                raise LeanColdCheckEnvironmentChanged(
                    "Lean project dependencies changed during the independent check; run it again."
                )


@contextmanager
def retained_incremental_build_guard(
    admission: ProjectLeanAdmission, project_root: Path
) -> Iterator[None]:
    """Invalidate changes emitted by a Lake build without rejecting its own output."""
    with retained_incremental_cold_check_guard(admission, project_root, allow_build_updates=True):
        yield
