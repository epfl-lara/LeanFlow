"""Apply compiled draft policy while retaining existing kernel type protections."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.deadlines import remaining_seconds
from leanflow_cli.formalization.lean_policy import inspect_draft
from leanflow_cli.workflows.prover.type_profile import compiled_type_profiles


def inspect_statement_draft(
    *,
    root: Path,
    frozen: Path,
    baseline: dict[str, bytes],
    changes: dict[str, bytes],
    workspace: Path,
    allowed_axioms: tuple[str, ...],
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
) -> dict[str, Any]:
    """Inspect all changed modules and independently protect original declaration types."""
    from leanflow_cli.lean.lean_incremental import _segment_file

    result = inspect_draft(
        frozen,
        list(changes),
        workspace,
        allowed_axioms,
        deadline=deadline,
        cancelled=cancelled,
        require_closed_inputs=False,
    )
    if not result.get("accepted"):
        return result
    modules = {relative: module for module, relative in result["modules"].items()}
    for relative, content in changes.items():
        if relative not in baseline:
            continue
        original = baseline[relative].decode()
        _, segments = _segment_file(original)
        names = list(dict.fromkeys(str(segment.name) for segment in segments if segment.name))
        if not names:
            continue
        # Retain the legacy dependency-sensitive kernel type comparison. The
        # compiled registry above, rather than this source index, governs all
        # new declaration policy and permits import-only aggregators.
        profiles = [
            compiled_type_profiles(
                root=project,
                source=source,
                names=names,
                workspace=workspace,
                timeout_s=remaining_seconds(deadline),
                module=modules[relative],
                cancelled=cancelled,
            )
            for project, source in ((root, original), (frozen, content.decode()))
        ]
        if any(not profile.get("accepted") for profile in profiles) or any(
            profiles[0]["profiles"][name]["sha256"] != profiles[1]["profiles"][name]["sha256"]
            for name in names
        ):
            return {
                "accepted": False,
                "error": f"Original kernel declaration types changed or could not be verified in {relative}",
            }
    return result
