"""Select and validate graph declarations whose acceptance evidence was lost on resume."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.workflows.plan_state import Blueprint, DeclTruth

_RECOVERABLE_STATUSES = frozenset({"stated", "audited", "conjectured", "proving", "blocked"})
RESUME_GATE_RECOVERY_OUTCOME_NOTE = (
    "resume reconciliation rebuilt exact target and axiom gate evidence"
)


@dataclass(frozen=True)
class ResumeGraphCandidate:
    """Identify one on-disk graph declaration eligible for an exact resume gate."""

    node_id: str
    target_symbol: str
    active_file: str


def outcome_restores_resume_gate(outcome: Mapping[str, Any]) -> bool:
    """Return whether one solved outcome restores proof truth from before startup."""
    return (
        str(outcome.get("status", "") or "").strip().lower() == "solved"
        and str(outcome.get("note", "") or "").strip() == RESUME_GATE_RECOVERY_OUTCOME_NOTE
    )


def legacy_startup_reset_node_ids(
    campaign: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Return last-progress nodes proven to come from a legacy resume reset.

    Older checkpoints may not persist the recovery outcome note. Correlate only
    the campaign's exact last-progress timestamp with a route-reset event whose
    same run previously emitted a resume-gate recovery for that node. This is a
    one-time migration signal, not a general inference from clean source text.
    """
    last_progress = campaign.get("last_verified_graph_progress")
    if not isinstance(last_progress, Mapping):
        return ()
    recorded_at = str(last_progress.get("recorded_at", "") or "").strip()
    last_node_ids = {
        str(node_id or "").strip()
        for node_id in (last_progress.get("node_ids") or [])
        if str(node_id or "").strip()
    }
    if not recorded_at or not last_node_ids:
        return ()

    campaign_id = str(campaign.get("campaign_id", "") or "").strip()
    try:
        epoch = int(campaign.get("epoch", 1) or 1)
    except (TypeError, ValueError):
        epoch = 1
    recovered: set[str] = set()
    for reset in events:
        if str(reset.get("type", "") or "") != "campaign-route-streak-reset":
            continue
        if str(reset.get("timestamp", "") or "").strip() != recorded_at:
            continue
        details = reset.get("details")
        reset_details = dict(details) if isinstance(details, Mapping) else {}
        if not campaign_id or str(reset_details.get("campaign_id", "") or "") != campaign_id:
            continue
        try:
            reset_epoch = int(reset_details.get("epoch", epoch) or epoch)
        except (TypeError, ValueError):
            continue
        if reset_epoch != epoch:
            continue
        reset_nodes = {
            str(node_id or "").strip()
            for node_id in (reset_details.get("node_ids") or [])
            if str(node_id or "").strip()
        } & last_node_ids
        if not reset_nodes:
            continue
        run_id = str(reset.get("run_id", "") or "").strip()
        if not run_id:
            continue
        reset_at = str(reset.get("timestamp", "") or "").strip()
        for recovery in events:
            if str(recovery.get("type", "") or "") != "plan-graph-resume-gate-recovered":
                continue
            if str(recovery.get("run_id", "") or "").strip() != run_id:
                continue
            recovered_at = str(recovery.get("timestamp", "") or "").strip()
            if not recovered_at or recovered_at > reset_at:
                continue
            recovery_details = recovery.get("details")
            recovery_node_id = str(
                (
                    recovery_details.get("node_id", "")
                    if isinstance(recovery_details, Mapping)
                    else ""
                )
                or ""
            ).strip()
            if recovery_node_id in reset_nodes:
                recovered.add(recovery_node_id)
    return tuple(sorted(recovered))


def _same_target(left: str, right: str) -> bool:
    """Return whether two possibly qualified Lean names identify one declaration."""
    normalized_left = str(left or "").strip().removeprefix("_root_.")
    normalized_right = str(right or "").strip().removeprefix("_root_.")
    return bool(normalized_left and normalized_right) and (
        normalized_left == normalized_right
        or normalized_left.endswith(f".{normalized_right}")
        or normalized_right.endswith(f".{normalized_left}")
    )


def _same_file(left: str, right: str) -> bool:
    """Return whether absolute, relative, or suffix file labels identify one file."""
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    try:
        if Path(left_text).expanduser().resolve() == Path(right_text).expanduser().resolve():
            return True
        left_parts = Path(left_text).parts
        right_parts = Path(right_text).parts
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(left_parts and right_parts) and (
        (len(left_parts) >= len(right_parts) and left_parts[-len(right_parts) :] == right_parts)
        or (len(right_parts) >= len(left_parts) and right_parts[-len(left_parts) :] == left_parts)
    )


def resume_graph_candidates(
    blueprint: Blueprint,
    truth: Mapping[tuple[str, str], DeclTruth],
    *,
    active_file: str = "",
    target_symbol: str = "",
) -> tuple[ResumeGraphCandidate, ...]:
    """Return ready sorry-free declarations that still lack proved status.

    Surface parsing is only a cheap eligibility filter.  It never promotes a
    node: every returned declaration still requires an exact target gate and
    transitive axiom inspection in the runner.  ``false`` and ``parked`` are
    excluded because mathematical disproof and fidelity approval outrank
    proof-surface recovery.

    When a durable queue assignment exists, restrict recovery to that node and
    its explicit transitive dependencies.  Resume recovery is only a repair
    path for lost gate evidence; unrelated or downstream declarations must not
    delay restoration of the foreground theorem.  With no assignment, retain
    the campaign-wide recovery used for legacy checkpoints.
    """
    normalized_file = str(active_file or "").strip()
    normalized_target = str(target_symbol or "").strip()
    allowed_node_ids: set[str] | None = None
    if normalized_file and normalized_target:
        assignment = next(
            (
                node
                for node in blueprint.nodes
                if _same_file(node.file, normalized_file)
                and _same_target(node.name, normalized_target)
            ),
            None,
        )
        allowed_node_ids = {assignment.id} if assignment is not None else set()
        if assignment is not None:
            dependencies: dict[str, list[str]] = {}
            for edge in blueprint.edges:
                if edge.kind == "depends_on":
                    dependencies.setdefault(edge.source, []).append(edge.target)
            pending = list(dependencies.get(assignment.id, ()))
            while pending:
                dependency_id = pending.pop()
                if dependency_id in allowed_node_ids:
                    continue
                allowed_node_ids.add(dependency_id)
                pending.extend(dependencies.get(dependency_id, ()))

    candidates: list[ResumeGraphCandidate] = []
    for node in blueprint.nodes:
        if allowed_node_ids is not None and node.id not in allowed_node_ids:
            continue
        if node.status not in _RECOVERABLE_STATUSES or not node.file or not node.name:
            continue
        if blueprint.has_invalid_dependency(node.id):
            continue
        declaration = truth.get((node.file, node.name))
        if (
            declaration is None
            or not declaration.present
            or declaration.has_sorry
            or declaration.has_error_diag
        ):
            continue
        candidates.append(
            ResumeGraphCandidate(
                node_id=node.id,
                target_symbol=node.name,
                active_file=node.file,
            )
        )
    return tuple(candidates)


def exact_resume_gate_accepts(
    manager_check: Mapping[str, Any],
    verification: Mapping[str, Any],
    target_symbol: str,
) -> bool:
    """Return whether fresh evidence authorizes resume-time graph promotion.

    Fail closed unless the incremental backend itself completed, checked the
    requested declaration, reported no errors or sorry, and the separate
    transitive axiom gate completed with no blockers.  A file/module/project
    verification is deliberately never accepted here because sibling sorry
    declarations are legal during a partially solved campaign.
    """
    if not exact_resume_target_gate_accepts(manager_check, verification, target_symbol):
        return False
    record = dict(verification or {})
    blockers = record.get("axiom_profile_blockers")
    return (
        record.get("axiom_profile_checked") is True
        and isinstance(blockers, Sequence)
        and not isinstance(blockers, (str, bytes))
        and not blockers
    )


def exact_resume_target_gate_accepts(
    manager_check: Mapping[str, Any],
    verification: Mapping[str, Any],
    target_symbol: str,
) -> bool:
    """Return whether exact elaboration is clean before transitive axiom inspection."""
    check = dict(manager_check or {})
    incremental = dict(check.get("incremental") or {})
    if not incremental.get("success") or not check.get("ok"):
        return False
    checked_target = str(check.get("target", "") or incremental.get("target", "") or "")
    if not _same_target(checked_target, target_symbol):
        return False

    record = dict(verification or {})
    scope = str(record.get("scope", "") or "")
    record_target = str(record.get("target", "") or "")
    if not scope.startswith("target:") or not _same_target(record_target, target_symbol):
        return False
    try:
        errors = int(record.get("errors", 0) or 0)
        sorry_count = int(record.get("sorry", record.get("sorry_count", 0)) or 0)
    except (TypeError, ValueError):
        return False
    return bool(record.get("ok")) and errors == 0 and sorry_count == 0
