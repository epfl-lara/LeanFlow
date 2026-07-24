"""Keep one parent-side Lean verification gate foreground-atomic."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from os import PathLike

from core.project_resource_admission import (
    ProjectLeanAdmission,
    project_lean_verification_transaction,
)


@contextmanager
def parent_lean_verification_transaction(
    project_scope: str | PathLike[str],
) -> Iterator[ProjectLeanAdmission]:
    """Hold project admission from exact elaboration through axiom inspection."""
    with project_lean_verification_transaction(project_scope) as admission:
        yield admission
