"""Freeze and recheck proof-only revisions before issuing preservation evidence."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import operation_cancelled, remaining_seconds
from leanflow_cli.formalization.dependency_snapshot import create_formalization_snapshot
from leanflow_cli.formalization.evidence import digest
from leanflow_cli.native.harness_artifacts import source_files
from leanflow_cli.runtime.storage import forget_workspace


def check_active(deadline: float | None, cancelled: Callable[[], bool] | None) -> None:
    """Reject expired or cancelled evidence work before it can publish success."""
    if operation_cancelled() or (cancelled is not None and cancelled()):
        raise ValueError("Proof preservation checking was cancelled")
    remaining = remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        raise ValueError("Proof preservation checking exceeded its deadline")


def inspect_frozen_proof(
    root: Path,
    modules: list[str],
    expected_files: dict[str, str],
    environment_id: str,
    allowed_axioms: tuple[str, ...],
    *,
    inspect: Callable[..., dict[str, Any]],
    environment: Callable[[Path], str],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    """Inspect independent immutable inputs and reject live or frozen input drift."""
    check_active(deadline, cancelled)
    before = source_files(root)
    if {
        path: digest(data) for path, data in before.items() if path.endswith(".lean")
    } != expected_files:
        raise ValueError("Proof revision source census differs from the reviewed project")
    if environment(root) != environment_id:
        raise ValueError("Proof preservation compiler/dependency environment changed")
    with tempfile.TemporaryDirectory(prefix="leanflow-proof-preservation-") as temporary:
        frozen = create_formalization_snapshot(
            root,
            Path(temporary).resolve() / "project",
            environment=environment,
            expected_environment=environment_id,
        )
        try:
            check_active(deadline, cancelled)
            if source_files(frozen) != before or source_files(root) != before:
                raise ValueError("Proof source changed while freezing independent evidence")
            if environment(frozen) != environment_id:
                raise ValueError("Frozen proof compiler/dependency environment changed")
            result = inspect(
                frozen,
                modules,
                frozen.parent / "checks",
                allowed_axioms,
                deadline=deadline,
                cancelled=cancelled,
            )
            check_active(deadline, cancelled)
            if source_files(frozen) != before or source_files(root) != before:
                raise ValueError("Proof source changed during independent inspection")
            if environment(root) != environment_id or environment(frozen) != environment_id:
                raise ValueError("Proof compiler/dependency environment changed during inspection")
            check_active(deadline, cancelled)
            return result
        finally:
            forget_workspace(frozen)
