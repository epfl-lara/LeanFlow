"""Choose terminal verification and research-quiescence policy."""

from __future__ import annotations

_LOW_VALUE_FINAL_WARNING_FRAGMENTS = ("try 'simp' instead of 'simpa'",)


def focused_verification_mode(*, full_project: bool) -> str:
    """Return the canonical verification scope for one completion check."""
    return "project" if full_project else "file_exact"


def requires_project_verification(
    *,
    focused_ok: bool,
    project_sorry_count: int | None,
    declaration_scope: str,
    workflow_kind: str,
) -> bool:
    """Return whether completion also requires a whole-project Lake build."""
    if not focused_ok or project_sorry_count != 0:
        return False
    return declaration_scope != "file" or workflow_kind == "formalize"


def should_quiesce_research_after_target(
    *,
    target_gate_accepted: bool,
    declaration_scope: str,
    source_sorry_count: int | None,
) -> bool:
    """Return whether the accepted target completed a file-scoped proof queue."""
    return target_gate_accepted and declaration_scope == "file" and source_sorry_count == 0


def warning_cleanup_worth_model_turn(warning_summary: str) -> bool:
    """Return whether final warnings justify another source-editing model turn."""
    warnings = [line.strip().lower() for line in str(warning_summary or "").splitlines()]
    warnings = [line for line in warnings if line]
    if not warnings:
        return False
    return any(
        not any(fragment in warning for fragment in _LOW_VALUE_FINAL_WARNING_FRAGMENTS)
        for warning in warnings
    )
