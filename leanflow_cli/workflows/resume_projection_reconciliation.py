"""Repair provider-free workflow projections from durable proof authorities.

Resume can stop before Lean or provider startup while a usage-limit pause is
active.  This module keeps the cheap human/runtime projections truthful in
that path: conditional-helper bookkeeping follows the current graph/source
classifier, plan.md follows the durable queue assignment, and an old broad
patch result inherits a later exact theorem verification.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.workflows import (
    campaign_epoch,
    conditional_helper_progress,
    decomposition_provenance,
    plan_state,
)
from leanflow_cli.workflows.queue_models import VerificationScope, verification_from_mapping
from leanflow_cli.workflows.workflow_json_io import update_json_file
from leanflow_cli.workflows.workflow_state import (
    load_verified_patch_status,
    workflow_verified_patch_status_path,
)


@dataclass(frozen=True)
class ResumeProjectionReconciliation:
    """Summarize one provider-free projection repair pass."""

    conditional_deferred_node_ids: tuple[str, ...] = ()
    conditional_released_node_ids: tuple[str, ...] = ()
    plan_rendered: bool = False
    verified_patch_promoted: bool = False


def _provider_free_streak_floor(campaign: Mapping[str, Any]) -> int:
    """Return the conservative retained-route floor without activity I/O."""
    routes = [entry for entry in (campaign.get("epoch_routes") or []) if isinstance(entry, Mapping)]
    if routes:
        return len(routes)
    try:
        return max(0, int(campaign.get("no_progress_route_streak", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _canonical_file(value: Any, *, cwd: Any = "") -> str:
    """Return a stable absolute file identity without requiring existence."""
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        base = Path(str(cwd or "").strip()).expanduser() if str(cwd or "").strip() else Path.cwd()
        path = base / path
    try:
        return os.path.normcase(str(path.resolve(strict=False)))
    except OSError:
        return os.path.normcase(os.path.abspath(str(path)))


def _same_symbol(left: Any, right: Any) -> bool:
    """Return whether two exact declaration identities match.

    ``_root_.`` is Lean's explicit spelling of the root namespace and may be
    erased safely.  Arbitrary suffix matching is not an alias rule: ``B.foo``
    and ``A.B.foo`` can coexist as distinct declarations in the same file.
    """
    first = str(left or "").strip().removeprefix("_root_.")
    second = str(right or "").strip().removeprefix("_root_.")
    return bool(first and second) and first == second


def _accepted_exact_outcome(outcome: Mapping[str, Any], theorem_id: str) -> bool:
    """Return whether one solved outcome carries an exact accepted target gate."""
    if str(outcome.get("status", "") or "").strip().lower() != "solved":
        return False
    raw_verification = outcome.get("last_verification")
    verification = verification_from_mapping(
        raw_verification if isinstance(raw_verification, Mapping) else None
    )
    if (
        verification is None
        or verification.scope is not VerificationScope.TARGET
        or not verification.ok
        or verification.errors
        or verification.sorry_count
        or not _same_symbol(verification.target, theorem_id)
    ):
        return False
    if str(os.getenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "0") or "0").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    raw = dict(raw_verification or {})
    blockers = raw.get("axiom_profile_blockers")
    return bool(
        raw.get("axiom_profile_checked") is True
        and isinstance(blockers, (list, tuple))
        and not blockers
    )


def _matching_persisted_outcome(
    summary: Mapping[str, Any],
    *,
    theorem_id: str,
    active_file: str,
) -> dict[str, Any]:
    """Return the exact durable queue outcome for one patch target."""
    manager = summary.get("queue_manager_state")
    outcomes = manager.get("theorem_outcomes") if isinstance(manager, Mapping) else None
    if not isinstance(outcomes, Mapping):
        return {}
    wanted_file = _canonical_file(active_file)
    for raw in outcomes.values():
        if not isinstance(raw, Mapping):
            continue
        if not _same_symbol(raw.get("target_symbol", ""), theorem_id):
            continue
        if _canonical_file(raw.get("active_file", "")) != wanted_file:
            continue
        return dict(raw)
    return {}


def _matching_proved_node(
    blueprint: plan_state.Blueprint,
    *,
    theorem_id: str,
    active_file: str,
    current_source_sha256: str,
) -> plan_state.GraphNode | None:
    """Return a current-source graph proof with the exact patch identity."""
    wanted_file = _canonical_file(active_file)
    if not current_source_sha256:
        return None
    return next(
        (
            node
            for node in blueprint.nodes
            if node.status == "proved"
            and _same_symbol(node.name, theorem_id)
            and _canonical_file(node.file) == wanted_file
            and str(node.source_sha256 or "").strip().lower() == current_source_sha256
        ),
        None,
    )


def reconcile_verified_patch_status(
    *,
    blueprint: plan_state.Blueprint | None = None,
    summary: Mapping[str, Any] | None = None,
    exact_outcome: Mapping[str, Any] | None = None,
) -> bool:
    """Promote a broad patch result after later exact theorem proof authority.

    A current exact outcome is ordered after the patch by the caller.  Resume
    repair accepts the durable conjunction of a matching solved exact outcome
    and current-source graph-proved node; a current-source graph proof alone
    is also authoritative because ``proved`` is gate-only in plan state.
    """
    # ``blueprint`` and ``summary`` remain accepted for call-site compatibility,
    # but they are only pre-lock snapshots.  Promotion must reload both durable
    # authorities while holding the source/graph leases below.
    del blueprint, summary
    latest = load_verified_patch_status()
    if (
        not latest
        or latest.get("check_passed") is not True
        or latest.get("patch_applied") is not True
        or latest.get("target_verified") is True
        or latest.get("verified") is True
    ):
        return False
    theorem_id = str(latest.get("theorem_id", "") or "").strip()
    active_file = _canonical_file(latest.get("path", ""), cwd=latest.get("cwd", ""))
    if not theorem_id or not active_file:
        return False

    explicit = dict(exact_outcome or {})
    reconciled_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    expected_checkpoint = str(latest.get("checkpoint_id", "") or "")
    try:
        with decomposition_provenance.source_operation(
            Path(active_file),
            canonical=True,
        ) as operation:
            source_bytes = decomposition_provenance.read_source_bytes(operation)
            current_source_sha256 = hashlib.sha256(source_bytes).hexdigest()
            with plan_state.blueprint_commit_guard():
                durable_blueprint = plan_state.load_blueprint()
                durable_summary = plan_state.load_summary()
                explicit_matches = bool(
                    explicit
                    and _same_symbol(explicit.get("target_symbol", ""), theorem_id)
                    and _canonical_file(explicit.get("active_file", "")) == active_file
                    and _accepted_exact_outcome(explicit, theorem_id)
                )
                persisted = _matching_persisted_outcome(
                    durable_summary,
                    theorem_id=theorem_id,
                    active_file=active_file,
                )
                persisted_matches = bool(
                    persisted and _accepted_exact_outcome(persisted, theorem_id)
                )
                proved_node = _matching_proved_node(
                    durable_blueprint,
                    theorem_id=theorem_id,
                    active_file=active_file,
                    current_source_sha256=current_source_sha256,
                )
                if not (explicit_matches or proved_node is not None):
                    return False
                if not explicit_matches and persisted and not persisted_matches:
                    # A matching rejected/stale outcome outranks an older graph projection.
                    return False

                evidence = explicit if explicit_matches else persisted
                verification = dict(evidence.get("last_verification") or {})
                if explicit_matches:
                    verification_source = "exact_theorem_outcome"
                elif persisted_matches:
                    verification_source = "durable_exact_theorem_outcome"
                else:
                    verification_source = "durable_graph_proof"

                def mutate(payload: dict[str, Any]) -> bool:
                    current = payload.get("latest")
                    if not isinstance(current, Mapping):
                        return False
                    status = dict(current)
                    try:
                        source_unchanged = (
                            decomposition_provenance.read_source_bytes(operation) == source_bytes
                        )
                    except OSError:
                        source_unchanged = False
                    if (
                        not source_unchanged
                        or str(status.get("checkpoint_id", "") or "") != expected_checkpoint
                        or not _same_symbol(status.get("theorem_id", ""), theorem_id)
                        or _canonical_file(status.get("path", ""), cwd=status.get("cwd", ""))
                        != active_file
                        or status.get("target_verified") is True
                        or status.get("verified") is True
                    ):
                        return False
                    status.update(
                        {
                            "status": "verified",
                            "target_verified": True,
                            "verified": True,
                            "target_verified_at": reconciled_at,
                            "target_verification_source": verification_source,
                            "target_verification_scope": str(verification.get("scope", "") or ""),
                            "target_verification_tool": str(verification.get("tool", "") or ""),
                            "target_graph_node_id": (
                                proved_node.id if proved_node is not None else ""
                            ),
                            "message": (
                                "Patch applied, its broad check passed, and the later exact "
                                "theorem gate verified the patched target."
                            ),
                        }
                    )
                    payload["version"] = max(1, int(payload.get("version", 1) or 1))
                    payload["latest"] = status
                    return True

                promoted = bool(update_json_file(workflow_verified_patch_status_path(), mutate))
                if not promoted:
                    return False
                try:
                    source_still_unchanged = (
                        decomposition_provenance.read_source_bytes(operation) == source_bytes
                    )
                except OSError:
                    source_still_unchanged = False
                if source_still_unchanged:
                    return True

                # This can only be an uncooperative writer, because the source
                # operation remains leased. Restore the exact pre-promotion row
                # unless a newer patch status has already replaced it.
                def rollback(payload: dict[str, Any]) -> bool:
                    current = payload.get("latest")
                    if not isinstance(current, Mapping):
                        return False
                    if (
                        str(current.get("checkpoint_id", "") or "") != expected_checkpoint
                        or str(current.get("target_verified_at", "") or "") != reconciled_at
                    ):
                        return False
                    payload["version"] = max(1, int(payload.get("version", 1) or 1))
                    payload["latest"] = dict(latest)
                    return True

                update_json_file(workflow_verified_patch_status_path(), rollback)
                return False
    except (OSError, RuntimeError):
        return False


def reconcile_provider_free_resume_projections(
    autonomy_state: dict[str, Any],
) -> ResumeProjectionReconciliation:
    """Refresh durable projections without Lean, providers, or research jobs."""
    if not plan_state.plan_state_enabled():
        promoted = reconcile_verified_patch_status()
        return ResumeProjectionReconciliation(verified_patch_promoted=promoted)
    blueprint = plan_state.load_blueprint()
    assessments = conditional_helper_progress.assess_conditional_helpers(blueprint)
    summary = plan_state.load_summary()
    campaign = summary.get("campaign")
    raw_policy = (
        campaign.get("conditional_helper_progress") if isinstance(campaign, Mapping) else {}
    )
    policy = raw_policy if isinstance(raw_policy, Mapping) else {}
    raw_deferred = policy.get("deferred_node_ids")
    persisted_values = raw_deferred if isinstance(raw_deferred, (list, tuple, set)) else ()
    persisted_deferred = {
        str(node_id or "").strip() for node_id in persisted_values if str(node_id or "").strip()
    }
    try:
        policy_version = int(policy.get("version", 0) or 0)
    except (TypeError, ValueError):
        policy_version = 0
    current_deferred = set(assessments)
    conditional = campaign_epoch.ConditionalHelperProgressReconciliation()
    if (
        persisted_deferred != current_deferred
        or policy_version < campaign_epoch.CONDITIONAL_HELPER_PROGRESS_POLICY_VERSION
    ):
        conditional = campaign_epoch.reconcile_conditional_helper_progress(
            autonomy_state,
            deferred_node_ids=tuple(assessments),
            precomputed_streak_floor=_provider_free_streak_floor(
                campaign if isinstance(campaign, Mapping) else {}
            ),
        )
        summary = plan_state.load_summary()
    promoted = reconcile_verified_patch_status(
        blueprint=blueprint,
        summary=summary,
    )
    plan_state.save_plan_md(blueprint, summary)
    return ResumeProjectionReconciliation(
        conditional_deferred_node_ids=tuple(sorted(assessments)),
        conditional_released_node_ids=conditional.released_node_ids,
        plan_rendered=True,
        verified_patch_promoted=promoted,
    )
