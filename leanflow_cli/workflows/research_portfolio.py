"""Maintain a bounded background research portfolio for an unresolved proof."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent.providers.isolated_auxiliary import sanitize_auxiliary_error
from core.provider_availability import normalize_provider_retry_after
from leanflow_cli.workflows import (
    campaign_epoch,
    dispatch_ledger_compaction,
    plan_state,
    research_findings,
    research_obstruction_dominance,
    research_route_context,
)
from leanflow_cli.workflows import (
    dispatch_service as dispatch_runtime,
)
from leanflow_cli.workflows.dispatch_models import (
    ASSIGNMENT_REVISION_INPUT_KEY,
    MATHEMATICAL_DELTA_SIGNATURE_INPUT_KEY,
    SCRATCH_ISOLATION_VERSION,
    SOURCE_REVISION_INPUT_KEY,
    JobBudget,
    JobSpec,
    LedgerEntry,
    is_ancestor,
)
from leanflow_cli.workflows.dispatch_service import (
    DispatchService,
)
from leanflow_cli.workflows.workflow_json_io import (
    read_json_file,
    update_json_file,
    update_json_file_if_changed,
)
from leanflow_cli.workflows.workflow_state import (
    append_workflow_activity,
    read_workflow_activity,
)
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

try:  # POSIX diagnostic idempotency; in-process locking remains the fallback.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

FINDINGS_CAP = research_findings.DURABLE_FINDING_HISTORY_CAP
DEFAULT_JOB_API_STEPS = 40
DEFAULT_JOB_WALL_CLOCK_S = 1800
ROUTE_ANCHOR_SUMMARY_CAP = 12000
DELIVERY_BACKPRESSURE_STATE_KEY = "research_delivery_backpressure"
PENDING_REPLACEMENT_STATE_KEY = "research_portfolio_pending_replacement"
PENDING_REPLACEMENT_STATE_VERSION = 1
FAILURE_BACKOFF_STATE_KEY = "research_portfolio_failure_backoff"
FAILURE_BACKOFF_STATE_VERSION = 2
# A failed isolated worker is operational evidence, not a reason to spin up an
# identical provider call on every parent heartbeat.  The cap remains finite:
# a recovered provider is retried automatically, while consecutive empty
# failures progressively reduce pressure on it.
FAILURE_BACKOFF_DELAYS_S = (15, 60, 300, 900)
PROCESS_RELEASE_ACTIVITY_SCAN_LIMIT = 10000

_EVIDENCE_DERIVED_BASE_ROUTES = frozenset({"evidence-to-helper"})
_SEMANTIC_LANE_COOLDOWN_REASONS = frozenset(
    {
        "declared_finite_evidence_only",
        "explicit_nonclosing_result",
        "partial_coverage_without_completion",
        "repeated_mechanism_without_material_coverage",
        "saturated_finite_branch_family",
    }
)
_ROTATION_LANE_COOLDOWN_REASONS: dict[str, frozenset[str]] = {
    # An inconclusive feasibility/decomposition turn is still a completed
    # portfolio direction. Rotate instead of rerunning the same archetype
    # merely because it did not produce a classifier-specific fingerprint.
    "negation_probe": frozenset(
        {
            "duplicate_mathematical_semantics",
            "no_classified_mathematical_semantics",
            "subsumed_mathematical_semantics",
        }
    ),
    "decomposition": frozenset(
        {
            "duplicate_mathematical_semantics",
            "no_classified_mathematical_semantics",
            "subsumed_mathematical_semantics",
        }
    ),
}

# The native runner's main-thread heartbeat and the foreground conversation's
# post-tool callback can both request maintenance. Keep the complete
# poll-consume-refill transaction single-flight within one parent process; the
# dispatch service's file locks remain the cross-process authority.
_MAINTENANCE_LOCK = threading.RLock()
_PROCESS_RELEASE_REPORT_LOCKS_GUARD = threading.Lock()
_PROCESS_RELEASE_REPORT_LOCKS: dict[str, threading.RLock] = {}
_SHUTDOWN_CAMPAIGNS: set[tuple[str, str]] = set()

_VOLATILE_ROUTE_COUNTER_RE = re.compile(
    r"\b(?:generation|attempt(?:_count)?)\s*(?:[:=#]\s*)?\d+\b",
    flags=re.IGNORECASE,
)
_VOLATILE_DELTA_SET_RE = re.compile(
    r"\b(?:prior\s+route-set|active\s+portfolio-set)\s+[0-9a-f]{8,64}\b",
    flags=re.IGNORECASE,
)
_OPERATIONAL_FAILURE_TEXT_RE = re.compile(
    r"\b(?:api|backend|connection|infrastructure|provider|server|service)\b.{0,48}"
    r"\b(?:error|fail(?:ed|ure)?|overload(?:ed)?|rate[- ]?limit(?:ed)?|timed?[- ]?out|"
    r"timeout|unavailable)\b|"
    r"\b(?:connection reset|empty (?:result|response)|malformed response|no result|"
    r"rate[- ]?limit(?:ed)?|timed?[- ]?out|timeout)\b",
    flags=re.IGNORECASE,
)
_FAILURE_ADMINISTRATIVE_KEYS = frozenset(
    {
        "api_calls",
        "error",
        "error_detail",
        "errors",
        "exception",
        "failure_reason",
        "issues",
        "issues_encountered",
        "message",
        "notes",
        "reported_status",
        "status",
    }
)
_ROUTE_FOCUSES: dict[str, tuple[tuple[str, str], ...]] = {
    "deep_search": (
        ("formal-library-grounding", "search mathlib and nearby formal proofs for reusable lemmas"),
        ("informal-proof-blueprint", "research the informal theorem and extract a proof blueprint"),
        (
            "alternate-formulation",
            "find an alternate formulation or invariant not used by prior attempts",
        ),
    ),
    "empirical": (
        (
            "small-case-invariant",
            "test small cases and identify the strongest plausible invariant",
        ),
        (
            "boundary-counterexample-probe",
            "probe boundary cases and assumptions for counterexamples",
        ),
        (
            "evidence-to-helper",
            "translate computational evidence into candidate helper lemmas",
        ),
    ),
    "negation_probe": (
        (
            "bounded-formal-negation",
            "attempt a bounded formal negation and report only kernel-backed evidence",
        ),
        (
            "exact-statement-feasibility",
            "recheck feasibility under the exact current statement and imports",
        ),
    ),
    "decomposition": (
        (
            "source-backed-subgoal-cut",
            "derive a small source-backed helper cut whose children are each strictly easier",
        ),
        (
            "dependency-chain",
            "propose an acyclic source-backed dependency chain from helpers to the exact target",
        ),
        (
            "alternate-decomposition",
            "propose a materially different source-backed split avoiding prior proof shapes",
        ),
    ),
}
_PORTFOLIO_ARCHETYPES = frozenset(_ROUTE_FOCUSES)
_ACTIVE_DELTA_ROTATION_FOCUSES: dict[str, str] = {
    "deep_search": (
        "derive a parametric identity or general library-backed reduction that applies beyond "
        "every banked finite instance; do not run another finite witness search"
    ),
    "empirical": (
        "use bounded computation to derive or test a cross-instance invariant or parametric "
        "construction outside every banked finite scope; do not return another isolated fixed "
        "or bounded instance as progress, and do not repeat a listed witness or the active "
        "parametric search"
    ),
    "negation_probe": (
        "test one exact unresolved universal premise or method boundary not already banked, "
        "without treating a finite obstruction as target negation"
    ),
    "decomposition": (
        "derive an acyclic parametric subgoal cut outside active search and finite-instance lanes, "
        "with each child strictly easier than the exact target"
    ),
}


def _utc_now() -> datetime:
    """Return the injectable wall clock used by durable retry decisions."""
    return datetime.now(UTC)


def _now_iso() -> str:
    return _utc_now().replace(microsecond=0).isoformat()


def _record_finding(
    entry: LedgerEntry,
    result: Mapping[str, Any],
    *,
    entries: Sequence[LedgerEntry] = (),
    delivery_target_symbol: str = "",
    delivery_active_file: str = "",
    blueprint: plan_state.Blueprint | None = None,
) -> bool:
    """Archive one job result and materialize it only within active capacity.

    The dispatch ledger already owns the lossless payload. This transaction
    checkpoints a lightweight archive pointer before ledger consumption and
    appends a prompt-facing copy only when it is due to the active target and
    the 32-record delivery window has room.
    """
    finding = research_findings.build_finding_record(
        entry,
        result,
        entries=entries,
    )

    def mutate(summary: dict[str, Any]) -> dict[str, object]:
        findings = [
            dict(item)
            for item in (summary.get("research_findings") or [])
            if isinstance(item, Mapping)
        ]
        if any(str(item.get("job_id", "") or "") == entry.spec.job_id for item in findings):
            return {
                "archive_changed": False,
                "materialized": False,
                "status": "already_materialized",
                "reason": "",
            }

        migration = dict(summary.get(research_findings.FINDING_MIGRATION_KEY) or {})
        raw_records = migration.get("records")
        records = {
            str(job_id): dict(raw)
            for job_id, raw in (raw_records.items() if isinstance(raw_records, Mapping) else ())
            if str(job_id) and isinstance(raw, Mapping)
        }
        current_target = str(delivery_target_symbol or "")
        current_file = str(delivery_active_file or "")
        related_targets = set(
            research_findings.related_target_symbols(
                blueprint,
                target_symbol=current_target,
                active_file=current_file,
            )
        )
        origin_target = str(finding.get("target_symbol", "") or "")
        origin_file = str(finding.get("active_file", "") or "")
        due_to_active = bool(
            current_target
            and current_file
            and origin_target in related_targets
            and research_findings._same_file(origin_file, current_file)
        )
        substantive = research_findings._migration_entry_is_substantive(
            entry,
            entries=entries or (entry,),
        )
        delivered = research_findings.durable_delivery_markers(summary)
        already_delivered = due_to_active and research_findings.was_delivered(
            finding,
            target_symbol=current_target,
            delivered=delivered,
        )
        backlog_counts = research_findings.active_delivery_backlog_counts(
            summary,
            campaign_id=str(finding.get("campaign_id", "") or ""),
            target_symbol=current_target,
            active_file=current_file,
            blueprint=blueprint,
        )
        inherited = bool(origin_target and origin_target != current_target)
        inherited_window_full = bool(
            inherited
            and backlog_counts["inherited"] >= research_findings.INHERITED_DELIVERY_BACKLOG_CAP
        )
        materialize = bool(
            substantive
            and due_to_active
            and not already_delivered
            and backlog_counts["total"] < research_findings.DELIVERY_BACKLOG_CAP
            and not inherited_window_full
        )
        status = "materialized_current" if materialize else "deferred_inactive"
        reason = ""
        if not substantive:
            status = "archived_non_substantive"
            reason = "ledger result has no mathematical evidence"
        elif already_delivered:
            status = "archived_delivered_current"
        elif due_to_active and not materialize:
            status = "deferred_capacity"
            reason = (
                "inherited delivery window is at capacity"
                if inherited_window_full
                else "active delivery backlog is at capacity"
            )

        now = _now_iso()
        archive_changed = research_findings._set_archive_record(
            records,
            entry.spec.job_id,
            research_findings._archive_record(
                entry,
                status=status,
                materialized_for_target=current_target if due_to_active else "",
                reason=reason,
            ),
            now=now,
        )
        migration.update(
            {
                "version": research_findings.FINDING_ARCHIVE_VERSION,
                "campaign_id": str(finding.get("campaign_id", "") or ""),
                "active_target_symbol": current_target,
                "active_file": current_file,
                "related_target_symbols": sorted(related_targets),
                "records": records,
                "updated_at": now,
            }
        )
        summary[research_findings.FINDING_MIGRATION_KEY] = migration
        if materialize:
            findings.append(finding)
            summary["research_findings"] = findings
            research_findings.compact_durable_findings(summary)
        return {
            "archive_changed": archive_changed,
            "materialized": materialize,
            "status": status,
            "reason": reason,
        }

    outcome = update_json_file(workflow_state_root() / "summary.json", mutate)
    if not isinstance(outcome, Mapping):
        return False
    recorded = bool(outcome.get("materialized"))
    if recorded:
        append_workflow_activity(
            "research-finding",
            f"Recorded {entry.spec.archetype} findings from {entry.spec.job_id}",
            **finding,
        )
    elif str(outcome.get("status", "") or "") not in {"", "already_materialized"}:
        append_workflow_activity(
            "research-finding-archived",
            f"Archived {entry.spec.archetype} findings from {entry.spec.job_id}",
            archive_event_key=(
                "research-finding-archived:"
                f"{str(finding.get('campaign_id', '') or '')}:{entry.spec.job_id}"
            ),
            job_id=entry.spec.job_id,
            campaign_id=str(finding.get("campaign_id", "") or ""),
            archetype=entry.spec.archetype,
            target_symbol=str(finding.get("target_symbol", "") or ""),
            active_file=str(finding.get("active_file", "") or ""),
            archive_status=str(outcome.get("status", "") or ""),
            archive_reason=str(outcome.get("reason", "") or ""),
        )
    return recorded


def _route_focuses(archetype: str) -> tuple[tuple[str, str], ...]:
    """Return the finite grounding routes for one research archetype."""
    return _ROUTE_FOCUSES.get(
        archetype,
        (("distinct-proof-direction", "find a distinct proof direction"),),
    )


def _focus(archetype: str, generation: int) -> str:
    """Return the legacy generation-indexed focus for direct spec construction."""
    values = _route_focuses(archetype)
    return values[(generation - 1) % len(values)][1]


def _normalized_route_objective(objective: str) -> str:
    """Remove volatile counters and whitespace from a route objective."""
    without_counters = _VOLATILE_ROUTE_COUNTER_RE.sub("[counter]", str(objective or ""))
    return " ".join(without_counters.casefold().split())


def _stable_route_signature(
    *,
    archetype: str,
    target_symbol: str,
    active_file: str,
    objective: str,
) -> str:
    """Return a durable semantic signature scoped to one exact assignment."""
    normalized_file = os.path.realpath(active_file) if active_file else ""
    payload = "\x1f".join(
        (
            str(archetype or "").strip().casefold(),
            str(target_symbol or "").strip(),
            normalized_file,
            _normalized_route_objective(objective),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _mathematical_delta_signature(
    *,
    target_symbol: str,
    active_file: str,
    focus: str,
) -> str:
    """Return an archetype- and generation-independent research-delta identity."""
    normalized_file = os.path.realpath(active_file) if active_file else ""
    normalized_focus = _VOLATILE_DELTA_SET_RE.sub("[route-set]", str(focus or ""))
    payload = "\x1f".join(
        (
            str(target_symbol or "").strip(),
            normalized_file,
            _normalized_route_objective(normalized_focus),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _job_objective(
    *,
    target_symbol: str,
    active_file: str,
    generation: int,
    focus: str,
) -> str:
    """Build the worker-facing objective while keeping counters observational."""
    return (
        f"Research unresolved Lean target {target_symbol or '[project scope]'} in "
        f"{active_file or '[project]'}; generation {generation}: {focus}. "
        "Return compact structured findings, not a claim of proof completion."
    )


def _job_spec(
    service: DispatchService,
    *,
    archetype: str,
    generation: int,
    target_symbol: str,
    active_file: str,
    attempt_count: int,
    route_key: str = "",
    route_focus: str = "",
    route_anchor_job_id: str = "",
    route_anchor_entry: LedgerEntry | None = None,
    route_context: Mapping[str, Any] | None = None,
    campaign_epoch_number: int = 0,
    assignment_revision: str = "",
    source_revision: str = "",
    forbidden_delta_signatures: frozenset[str] = frozenset(),
    universal_obstruction: research_obstruction_dominance.UniversalObstruction | None = None,
) -> JobSpec:
    if route_anchor_entry is not None:
        actual_anchor_id = route_anchor_entry.spec.job_id
        if route_anchor_job_id and route_anchor_job_id != actual_anchor_id:
            raise ValueError(
                f"route anchor mismatch: {route_anchor_job_id!r} != {actual_anchor_id!r}"
            )
        route_anchor_job_id = actual_anchor_id
    job_id = service.mint_job_id(archetype, role="orchestrator")
    if not route_key or not route_focus:
        choices = _route_focuses(archetype)
        default_route_key, default_focus = choices[(generation - 1) % len(choices)]
        route_key = route_key or default_route_key
        route_focus = route_focus or default_focus
    route_objective = _job_objective(
        target_symbol=target_symbol,
        active_file=active_file,
        generation=generation,
        focus=route_focus,
    )
    if universal_obstruction is not None and (
        research_obstruction_dominance.dominated_finite_instance_objective(
            archetype=archetype,
            route_key=route_key,
            objective=route_objective,
        )
    ):
        raise ValueError(
            "research finite-instance objective is dominated by parent-verified "
            f"universal obstruction {universal_obstruction.node_id}"
        )
    route_signature = _stable_route_signature(
        archetype=archetype,
        target_symbol=target_symbol,
        active_file=active_file,
        objective=route_objective,
    )
    delta_signature = _mathematical_delta_signature(
        target_symbol=target_symbol,
        active_file=active_file,
        focus=route_focus,
    )
    if delta_signature in forbidden_delta_signatures:
        raise ValueError(
            "research mathematical delta duplicates an active exact-assignment worker: "
            f"{delta_signature}"
        )
    scoped_route_context = (
        research_route_context.route_context_for_assignment(
            route_context,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        if route_context is not None
        else None
    )
    repeated_fact_job_id = (
        research_route_context.consumed_fact_objective_conflict(
            route_objective,
            scoped_route_context,
        )
        if scoped_route_context is not None and route_anchor_entry is None
        else ""
    )
    if repeated_fact_job_id:
        raise ValueError(
            "research objective repeats a consumed exact-target finite fact from "
            f"{repeated_fact_job_id}"
        )
    objective = (
        research_route_context.objective_with_route_context(route_objective, scoped_route_context)
        if scoped_route_context is not None
        else route_objective
    )
    deliverable = {
        "negation_probe": "probe_verdict",
        "empirical": "experiment_result",
        "decomposition": "decomposition_report",
    }.get(archetype, "findings_report")
    toolsets = {
        "deep_search": ("web-research", "lean-research"),
        "empirical": ("lean-research", "empirical-compute"),
        "negation_probe": ("lean-research",),
        "decomposition": ("web-research", "lean-research"),
    }.get(archetype, ("lean-research",))
    if _route_requires_anchor(route_key) and route_anchor_entry is None:
        raise ValueError(f"evidence-derived route {route_key!r} requires its source ledger entry")
    route_mode = "evidence_synthesis" if route_anchor_entry is not None else "grounding"
    inputs: dict[str, Any] = {
        "campaign_id": service.root_job_id,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "attempt_count": attempt_count,
        "generation": generation,
        "route_key": route_key,
        "route_signature": route_signature,
        MATHEMATICAL_DELTA_SIGNATURE_INPUT_KEY: delta_signature,
        "route_anchor_job_id": route_anchor_job_id,
        "route_mode": route_mode,
    }
    if campaign_epoch_number > 0:
        inputs["campaign_epoch"] = int(campaign_epoch_number)
    if assignment_revision:
        inputs[ASSIGNMENT_REVISION_INPUT_KEY] = str(assignment_revision)
    if source_revision:
        inputs[SOURCE_REVISION_INPUT_KEY] = str(source_revision)
    if scoped_route_context is not None:
        inputs[research_route_context.ROUTE_CONTEXT_INPUT_KEY] = scoped_route_context
        inputs[research_route_context.ROUTE_CONTEXT_SHA256_INPUT_KEY] = (
            research_route_context.route_context_sha256(scoped_route_context)
        )
    if route_anchor_entry is not None:
        summary, finding_sha256, truncated = _route_anchor_finding_summary(route_anchor_entry)
        provenance = _route_anchor_provenance(
            route_anchor_entry,
            finding_sha256=finding_sha256,
        )
        inputs.update(
            {
                "route_anchor_provenance": provenance,
                "route_anchor_finding_summary": summary,
                "route_anchor_finding_sha256": finding_sha256,
                "route_anchor_finding_truncated": truncated,
                "route_anchor_consumption_key": _route_anchor_consumption_key(
                    route_key,
                    route_anchor_entry,
                    finding_sha256=finding_sha256,
                ),
            }
        )
    return JobSpec(
        job_id=job_id,
        archetype=archetype,
        requester_role="orchestrator",
        objective=objective,
        budget=JobBudget(
            api_steps=DEFAULT_JOB_API_STEPS,
            wall_clock_s=DEFAULT_JOB_WALL_CLOCK_S,
        ),
        deliverable=deliverable,
        inputs=inputs,
        toolsets=toolsets,
        scope={
            "scratch_only": True,
            "isolation_version": SCRATCH_ISOLATION_VERSION,
        },
        parent_job_id=job_id.rpartition(".")[0],
        report_to="Research Findings",
    )


def _desired_archetypes(
    attempt_count: int,
    workers: int,
    *,
    universal_obstruction: bool = False,
) -> list[str]:
    """Return the ordered lane pool available at the current proof depth."""
    if universal_obstruction:
        # Promotion/replanning is the first remaining obligation. A separate
        # decomposition lane preserves proof-shape exploration if promotion
        # needs a bridge or a corrected statement, while deep search remains
        # useful at larger capacities. More finite samples add no information.
        return ["negation_probe", "decomposition", "deep_search"][:workers]
    desired = ["deep_search"]
    if attempt_count >= 2:
        desired.extend(["empirical", "negation_probe", "decomposition"])
    return desired[:workers]


def _assignment_revision(
    blueprint: plan_state.Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> str:
    """Return a proof-declaration revision without hashing unrelated file edits."""
    node = blueprint.node_by_id(plan_state.node_id_for(target_symbol, active_file))
    if node is None or not str(node.statement or "").strip():
        return ""
    return hashlib.sha256(str(node.statement).encode("utf-8")).hexdigest()


def _source_revision(active_file: str) -> str:
    """Return the current active-file digest for stale-result suppression."""
    try:
        return hashlib.sha256(Path(active_file).read_bytes()).hexdigest()
    except OSError:
        return ""


def _job_matches_assignment(
    entry: LedgerEntry,
    *,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether an open research job still serves the live assignment."""
    inputs = dict(entry.spec.inputs)
    job_target = str(inputs.get("target_symbol", "") or "")
    job_file = str(inputs.get("active_file", "") or "")
    if job_target != str(target_symbol or ""):
        return False
    if not job_file or not active_file:
        return job_file == str(active_file or "")
    return os.path.realpath(job_file) == os.path.realpath(active_file)


def _dominated_open_portfolio_jobs(
    entries: Sequence[LedgerEntry],
    *,
    target_symbol: str,
    active_file: str,
    obstruction: research_obstruction_dominance.UniversalObstruction | None,
) -> tuple[LedgerEntry, ...]:
    """Return open finite-instance workers subsumed by universal evidence."""
    if obstruction is None:
        return ()
    return tuple(
        entry
        for entry in entries
        if not entry.is_terminal()
        and _job_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        and research_obstruction_dominance.dominated_finite_instance_objective(
            archetype=entry.spec.archetype,
            route_key=str(entry.spec.inputs.get("route_key", "") or ""),
            objective=entry.spec.objective,
        )
    )


def _reconcile_delta_reservation_winner(
    service: DispatchService,
    conflict: dispatch_runtime.MathematicalDeltaReservationConflict,
    *,
    target_symbol: str,
    active_file: str,
) -> list[LedgerEntry]:
    """Adopt and reconcile the exact open job that won a proposal race."""
    entries = service.entries()
    winner = next(
        (entry for entry in entries if entry.spec.job_id == conflict.winning_job_id),
        None,
    )
    winner_inputs = dict(winner.spec.inputs or {}) if winner is not None else {}
    if (
        winner is None
        or winner.is_terminal()
        or not _job_matches_assignment(
            winner,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        or str(winner_inputs.get(MATHEMATICAL_DELTA_SIGNATURE_INPUT_KEY, "") or "").strip()
        != conflict.delta_signature
    ):
        raise conflict

    # Proposal and process launch are separate durable transactions. The
    # losing parent may observe the winner before its proposer launches it, so
    # use the idempotent per-job launch path to close that gap. A concurrent
    # launcher returns the already-reserved/running winner unchanged.
    try:
        service.deploy_async(winner.spec.job_id)
    except RuntimeError:
        # A very fast worker may become terminal between validation and the
        # launch reservation. Preserve that completed verdict, but propagate
        # every operational launch failure while the reservation remains open.
        current = next(
            (entry for entry in service.entries() if entry.spec.job_id == conflict.winning_job_id),
            None,
        )
        if current is None or not current.is_terminal():
            raise
    return service.entries()


def _semantic_lane_cooldown_record(
    entry: LedgerEntry,
    *,
    semantic_entries: Sequence[LedgerEntry],
) -> dict[str, Any]:
    """Return the deterministic semantic cooldown caused by one lane result."""
    novelty = research_route_context.classify_semantic_novelty(entry, semantic_entries)
    finding = {
        "target_symbol": str(entry.spec.inputs.get("target_symbol", "") or ""),
        "active_file": str(entry.spec.inputs.get("active_file", "") or ""),
        "deliverable": dict(entry.result.get("deliverable") or {}),
        "semantic_novelty": novelty,
    }
    reason = research_findings.foreground_use_reason(finding)
    lane_reasons = _ROTATION_LANE_COOLDOWN_REASONS.get(
        entry.spec.archetype,
        frozenset(),
    )
    if reason not in _SEMANTIC_LANE_COOLDOWN_REASONS and reason not in lane_reasons:
        return {}
    return {
        "job_id": entry.spec.job_id,
        "classification": str(novelty.get("classification", "") or ""),
        "reason": reason,
    }


def _assignment_graph_node_ids(
    blueprint: plan_state.Blueprint,
    *,
    target_symbol: str,
    active_file: str,
) -> set[str]:
    """Return the structural dependency subtree for one exact assignment."""
    target_id = plan_state.node_id_for(target_symbol, active_file)
    if blueprint.node_by_id(target_id) is None:
        return set()
    children: dict[str, set[str]] = {}
    for edge in blueprint.edges:
        if edge.kind == "depends_on":
            children.setdefault(edge.source, set()).add(edge.target)
        elif edge.kind == "split_of":
            children.setdefault(edge.target, set()).add(edge.source)
    related = {target_id}
    pending = [target_id]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child in related:
                continue
            related.add(child)
            pending.append(child)
    return related


def _assignment_progress_after(
    entry: LedgerEntry,
    *,
    campaign: Mapping[str, Any],
    blueprint: plan_state.Blueprint,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether later verified graph progress reopens this exact lane."""
    raw_progress = campaign.get("last_verified_graph_progress")
    if not isinstance(raw_progress, Mapping):
        return False
    progress_at = _parse_utc_timestamp(raw_progress.get("recorded_at"))
    terminal_at = _parse_utc_timestamp(entry.finished_at or entry.created_at)
    if progress_at is None or terminal_at is None or progress_at <= terminal_at:
        return False
    progress_node_ids = {
        str(node_id or "").strip()
        for node_id in (raw_progress.get("node_ids") or [])
        if str(node_id or "").strip()
    }
    if not progress_node_ids:
        return False
    return bool(
        progress_node_ids.intersection(
            _assignment_graph_node_ids(
                blueprint,
                target_symbol=target_symbol,
                active_file=active_file,
            )
        )
    )


def _semantic_completion_order_key(entry: LedgerEntry) -> tuple[int, datetime, str]:
    """Return a stable order key for one mathematically completed lane result.

    Valid completion times always outrank missing or malformed legacy values.
    The ledger job ID is unique and breaks equal-time or timestamp-less ties
    independently of ledger insertion order.
    """
    finished_at = _parse_utc_timestamp(entry.finished_at)
    return (
        int(finished_at is not None),
        finished_at or datetime.min.replace(tzinfo=UTC),
        entry.spec.job_id,
    )


def _job_campaign_epoch(entry: LedgerEntry) -> int:
    """Return the positive campaign epoch recorded by one research job."""
    try:
        epoch = int(dict(entry.spec.inputs or {}).get("campaign_epoch", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, epoch)


def _semantic_lane_cooldowns(
    entries: Sequence[LedgerEntry],
    *,
    target_symbol: str,
    active_file: str,
    blueprint: plan_state.Blueprint | None = None,
    campaign: Mapping[str, Any] | None = None,
    assignment_revision: str = "",
) -> dict[str, dict[str, Any]]:
    """Return exact-assignment lanes that must rotate after semantic saturation.

    The latest mathematically completed (``done``) result owns the lane verdict.
    Operational failure and cleanup rows cannot erase it. A later target-declaration
    revision or verified node in the target's structural dependency subtree reopens
    the lane; unrelated targets and file edits cannot do so.
    """
    assignment_entries = [
        entry
        for entry in entries
        if _job_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        and (
            not assignment_revision
            or str(
                dict(entry.spec.inputs or {}).get(ASSIGNMENT_REVISION_INPUT_KEY, "") or ""
            ).strip()
            == assignment_revision
        )
    ]
    latest_by_archetype: dict[str, LedgerEntry] = {}
    for entry in assignment_entries:
        # Operational cleanup verdicts carry no mathematical lane semantics.
        # In particular, epoch shutdown writes ``killed`` after the latest
        # substantive result; letting that empty row win would erase a durable
        # saturation cooldown and restart the spent route after every resume.
        if entry.state != "done":
            continue
        previous = latest_by_archetype.get(entry.spec.archetype)
        if previous is None or _semantic_completion_order_key(
            entry
        ) > _semantic_completion_order_key(previous):
            latest_by_archetype[entry.spec.archetype] = entry

    result: dict[str, dict[str, Any]] = {}
    current_blueprint = blueprint or plan_state.Blueprint()
    current_campaign = campaign or {}
    for archetype, entry in latest_by_archetype.items():
        record = _semantic_lane_cooldown_record(
            entry,
            semantic_entries=assignment_entries,
        )
        if not record:
            continue
        prior_revision = str(
            dict(entry.spec.inputs or {}).get(ASSIGNMENT_REVISION_INPUT_KEY, "") or ""
        )
        if assignment_revision and assignment_revision != prior_revision:
            continue
        if _assignment_progress_after(
            entry,
            campaign=current_campaign,
            blueprint=current_blueprint,
            target_symbol=target_symbol,
            active_file=active_file,
        ):
            continue
        record["campaign_epoch"] = _job_campaign_epoch(entry)
        result[archetype] = record
    return result


def _older_epoch_cooldown_relaxation(
    *,
    archetype: str,
    record: Mapping[str, Any],
    current_epoch: int,
) -> dict[str, Any] | None:
    """Return observable metadata when a prior epoch's cooldown may refill a vacant slot."""
    try:
        producing_epoch = int(record.get("campaign_epoch", 0) or 0)
    except (TypeError, ValueError):
        return None
    if producing_epoch <= 0 or current_epoch <= producing_epoch:
        return None
    return {
        "archetype": archetype,
        "job_id": str(record.get("job_id", "") or ""),
        "classification": str(record.get("classification", "") or ""),
        "reason": str(record.get("reason", "") or ""),
        "producing_epoch": producing_epoch,
        "current_epoch": current_epoch,
    }


def _failure_scope_key(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    archetype: str,
) -> str:
    """Return the durable identity of one assignment/archetype circuit."""
    payload = {
        "campaign_id": str(campaign_id or "campaign"),
        "target_symbol": str(target_symbol or ""),
        "active_file": os.path.realpath(active_file) if active_file else "",
        "archetype": str(archetype or ""),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _parse_utc_timestamp(value: Any) -> datetime | None:
    """Parse one persisted ISO timestamp into an aware UTC datetime."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _mathematical_payload_present(value: Any, *, key: str = "") -> bool:
    """Return whether a failed result contains more than operational metadata."""
    normalized_key = str(key or "").strip().casefold()
    if normalized_key == research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY:
        return False
    if normalized_key in _FAILURE_ADMINISTRATIVE_KEYS:
        return False
    if isinstance(value, Mapping):
        return any(
            _mathematical_payload_present(item, key=str(item_key))
            for item_key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_mathematical_payload_present(item) for item in value)
    if isinstance(value, str):
        text = " ".join(value.split())
        return bool(text) and _OPERATIONAL_FAILURE_TEXT_RE.search(text) is None
    return value is not None and value is not False


def _failure_has_mathematical_payload(entry: LedgerEntry) -> bool:
    """Return whether a failed worker preserved concrete mathematical evidence."""
    if entry.result.get("artifact_paths") or entry.result.get("plan_delta"):
        return True
    evidence = research_route_context.semantic_evidence(entry)
    if evidence.fingerprints or evidence.has_checked_helper:
        return True
    raw_deliverable = entry.result.get("deliverable")
    deliverable = research_route_context.strip_parent_route_context(
        raw_deliverable if isinstance(raw_deliverable, Mapping) else None
    )
    if _mathematical_payload_present(deliverable):
        return True
    # Some older workers returned a summary beside rather than inside the
    # deliverable. Preserve that evidence unless it is plainly operational.
    return _mathematical_payload_present(entry.result.get("summary"), key="summary_payload")


def _is_empty_worker_failure(entry: LedgerEntry) -> bool:
    """Return whether one terminal worker failure should cool its exact lane."""
    if normalize_provider_retry_after(entry.result.get("provider_retry_after")):
        # An account-wide reset is an admission pause, not evidence that this
        # mathematical lane or proof shape failed.
        return False
    return entry.state in {"failed", "stuck"} and not _failure_has_mathematical_payload(entry)


def _active_provider_usage_limit(
    entries: Sequence[LedgerEntry],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    now_epoch: float,
) -> dict[str, Any]:
    """Return the latest active reset for this exact campaign assignment."""
    latest: dict[str, Any] = {}
    normalized_campaign = str(campaign_id or "").strip()
    for entry in entries:
        if not entry.is_terminal() or not _job_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        ):
            continue
        if normalized_campaign and not is_ancestor(normalized_campaign, entry.spec.job_id):
            continue
        retry_after = normalize_provider_retry_after(
            entry.result.get("provider_retry_after"),
            now_epoch=now_epoch,
        )
        if not retry_after or now_epoch >= float(retry_after["unavailable_until_epoch"]):
            continue
        if not latest or int(retry_after["unavailable_until_epoch"]) > int(
            latest["unavailable_until_epoch"]
        ):
            latest = retry_after
    return latest


def _active_campaign_provider_usage_limit(
    *,
    campaign_id: str,
    now_epoch: float,
) -> dict[str, Any]:
    """Return the active provider reset published by this exact campaign."""
    try:
        campaign = campaign_epoch.campaign_snapshot()
    except Exception:
        return {}
    if str(campaign.get("campaign_id", "") or "").strip() != str(campaign_id or "").strip():
        return {}
    retry_after = normalize_provider_retry_after(
        campaign.get(campaign_epoch.PROVIDER_USAGE_LIMIT_PAUSE_FIELD),
        now_epoch=now_epoch,
    )
    if not retry_after or now_epoch >= float(retry_after["unavailable_until_epoch"]):
        return {}
    return retry_after


def _later_provider_usage_limit(
    first: Mapping[str, Any] | None,
    second: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the reset with the later absolute provider deadline."""
    candidates = [dict(value) for value in (first, second) if value]
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda value: int(value.get("unavailable_until_epoch", 0) or 0),
    )


def _summary_allows_provider_launch(
    summary: Mapping[str, Any],
    *,
    campaign_id: str,
    now_epoch: float,
) -> bool:
    """Return whether one locked campaign snapshot permits process launch."""
    raw_campaign = summary.get("campaign")
    campaign = dict(raw_campaign) if isinstance(raw_campaign, Mapping) else {}
    if str(campaign.get("campaign_id", "") or "").strip() != str(campaign_id or "").strip():
        return True
    raw_pause = campaign.get(campaign_epoch.PROVIDER_USAGE_LIMIT_PAUSE_FIELD)
    if raw_pause is None:
        return True
    retry_after = normalize_provider_retry_after(raw_pause, now_epoch=now_epoch)
    if not retry_after:
        # Corrupt provider-owned admission state must not silently open a new
        # process lane; startup reconciliation will surface the manual pause.
        return False
    return now_epoch >= float(retry_after["unavailable_until_epoch"])


def _worker_failure_reason(entry: LedgerEntry) -> str:
    """Return a bounded observable reason for one empty worker failure."""
    raw_deliverable = entry.result.get("deliverable")
    deliverable = dict(raw_deliverable) if isinstance(raw_deliverable, Mapping) else {}
    candidates = (
        entry.result.get("error"),
        entry.result.get("error_detail"),
        deliverable.get("error"),
        deliverable.get("error_detail"),
        entry.notes,
    )
    detail = next(
        (sanitize_auxiliary_error(item, limit=400) for item in candidates if str(item).strip()),
        "",
    )
    status = sanitize_auxiliary_error(
        entry.result.get("status", "") or entry.state,
        limit=100,
    )
    if detail:
        return f"{status}: {detail}"[:500]
    return f"{status}: worker returned no mathematical deliverable"[:500]


def _unseen_terminal_entries(
    entries: Sequence[LedgerEntry],
    *,
    last_terminal_job_id: str,
) -> list[LedgerEntry]:
    """Return append-only terminal rows after one persisted ledger marker."""
    terminal = [entry for entry in entries if entry.is_terminal()]
    marker = str(last_terminal_job_id or "")
    if not marker:
        return terminal
    for index, entry in enumerate(terminal):
        if entry.spec.job_id == marker:
            return terminal[index + 1 :]
    # A missing marker means the ledger was externally compacted or replaced.
    # Preserve the existing circuit instead of recounting old failures.
    return []


def _reconcile_failure_backoff(
    entries: Sequence[LedgerEntry],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    archetypes: Sequence[str],
    now: datetime,
) -> dict[str, Any]:
    """Persist and return assignment-local empty-worker retry circuits.

    Ledger job ids are append-only markers, so every terminal result affects a
    circuit once even across runner restarts. Successful results and failed
    results with concrete mathematical payload reset the consecutive-empty
    streak; only empty ``failed``/``stuck`` rows schedule a cooldown. A scope
    first discovered with terminal history is hydrated silently: those rows
    predate the circuit's observation baseline and must not replay activity
    transitions on every cold rebuild.
    """
    normalized_now = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    now_iso = normalized_now.replace(microsecond=0).isoformat()
    normalized_campaign = str(campaign_id or "campaign")
    normalized_target = str(target_symbol or "")
    normalized_file = os.path.realpath(active_file) if active_file else ""
    desired = tuple(dict.fromkeys(str(item) for item in archetypes if str(item)))

    assignment_entries = [
        entry
        for entry in entries
        if entry.spec.archetype in desired
        and _job_matches_assignment(
            entry,
            target_symbol=normalized_target,
            active_file=normalized_file,
        )
    ]

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        raw_state = summary.get(FAILURE_BACKOFF_STATE_KEY)
        state = dict(raw_state) if isinstance(raw_state, Mapping) else {}
        raw_scopes = state.get("scopes")
        scopes = {
            str(key): dict(value)
            for key, value in (raw_scopes.items() if isinstance(raw_scopes, Mapping) else ())
            if str(key) and isinstance(value, Mapping)
        }
        transitions: list[dict[str, Any]] = []
        state_changed = False

        for archetype in desired:
            scope_key = _failure_scope_key(
                campaign_id=normalized_campaign,
                target_symbol=normalized_target,
                active_file=normalized_file,
                archetype=archetype,
            )
            matching = [
                entry
                for entry in assignment_entries
                if entry.spec.archetype == archetype and entry.is_terminal()
            ]
            existing = scopes.get(scope_key)
            record = dict(existing or {})
            # Persist an observation baseline before this scope launches its
            # first worker. If the baseline itself was lost or this is a
            # pre-circuit campaign, reconstruct the current circuit from raw
            # terminal history without announcing each historical transition.
            # A later terminal row then has an existing baseline and emits once.
            history_hydration = existing is None or not (
                str(record.get("initialized_at", "") or "")
                or str(record.get("last_terminal_job_id", "") or "")
            )
            if history_hydration:
                # This state is only a derived cache. Rebuild it from the raw
                # ledger instead of compounding a partial legacy record whose
                # terminal watermark is absent.
                record = {"initialized_at": now_iso}
                state_changed = True
            unseen = _unseen_terminal_entries(
                matching,
                last_terminal_job_id=str(record.get("last_terminal_job_id", "") or ""),
            )
            for entry in unseen:
                previous_failures = max(0, int(record.get("consecutive_failures", 0) or 0))
                record["last_terminal_job_id"] = entry.spec.job_id
                record["last_terminal_state"] = entry.state
                record["last_terminal_at"] = str(entry.finished_at or now_iso)
                record["updated_at"] = now_iso
                state_changed = True
                if _is_empty_worker_failure(entry):
                    consecutive = previous_failures + 1
                    delay_s = FAILURE_BACKOFF_DELAYS_S[
                        min(consecutive - 1, len(FAILURE_BACKOFF_DELAYS_S) - 1)
                    ]
                    failed_at = _parse_utc_timestamp(entry.finished_at) or normalized_now
                    next_retry = (failed_at + timedelta(seconds=delay_s)).replace(microsecond=0)
                    reason = _worker_failure_reason(entry)
                    record.update(
                        {
                            "consecutive_failures": consecutive,
                            "delay_seconds": delay_s,
                            "last_failure_job_id": entry.spec.job_id,
                            "last_failure_at": failed_at.replace(microsecond=0).isoformat(),
                            "last_failure_reason": reason,
                            "next_retry_at": next_retry.isoformat(),
                        }
                    )
                    if not history_hydration:
                        transitions.append(
                            {
                                "kind": "backoff",
                                "scope_key": scope_key,
                                "archetype": archetype,
                                "job_id": entry.spec.job_id,
                                "consecutive_failures": consecutive,
                                "delay_seconds": delay_s,
                                "next_retry_at": next_retry.isoformat(),
                                "reason": reason,
                            }
                        )
                elif entry.state == "done" or entry.state in {"failed", "stuck"}:
                    if previous_failures and not history_hydration:
                        transitions.append(
                            {
                                "kind": "cleared",
                                "scope_key": scope_key,
                                "archetype": archetype,
                                "job_id": entry.spec.job_id,
                                "previous_failures": previous_failures,
                                "reason": (
                                    "completed result"
                                    if entry.state == "done"
                                    else "terminal result preserved mathematical evidence"
                                ),
                            }
                        )
                    record.update(
                        {
                            "consecutive_failures": 0,
                            "delay_seconds": 0,
                            "next_retry_at": "",
                        }
                    )

            record.update(
                {
                    "campaign_id": normalized_campaign,
                    "target_symbol": normalized_target,
                    "active_file": normalized_file,
                    "archetype": archetype,
                }
            )
            scopes[scope_key] = record

        blocked: dict[str, dict[str, Any]] = {}
        retry_eligible: dict[str, dict[str, Any]] = {}
        for archetype in desired:
            scope_key = _failure_scope_key(
                campaign_id=normalized_campaign,
                target_symbol=normalized_target,
                active_file=normalized_file,
                archetype=archetype,
            )
            record = dict(scopes.get(scope_key) or {})
            consecutive = max(0, int(record.get("consecutive_failures", 0) or 0))
            retry_at = _parse_utc_timestamp(record.get("next_retry_at"))
            if consecutive <= 0 or retry_at is None:
                continue
            if normalized_now < retry_at:
                blocked[archetype] = record
            else:
                retry_eligible[archetype] = record

        if state_changed:
            state.update(
                {
                    "version": FAILURE_BACKOFF_STATE_VERSION,
                    "scopes": scopes,
                    "updated_at": now_iso,
                }
            )
            summary[FAILURE_BACKOFF_STATE_KEY] = state
        return {
            "blocked": blocked,
            "retry_eligible": retry_eligible,
            "transitions": transitions,
        }

    return dict(update_json_file(workflow_state_root() / "summary.json", mutate) or {})


def _emit_failure_backoff_transitions(
    transitions: Sequence[Mapping[str, Any]],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
) -> None:
    """Emit one activity event per newly observed circuit transition."""
    for raw in transitions:
        transition = dict(raw)
        kind = str(transition.pop("kind", "") or "")
        archetype = str(transition.get("archetype", "") or "research")
        if kind == "backoff":
            append_workflow_activity(
                "research-portfolio-failure-backoff",
                f"Cooling down the {archetype} lane after an empty worker failure",
                campaign_id=campaign_id,
                target_symbol=target_symbol,
                active_file=active_file,
                **transition,
            )
        elif kind == "cleared":
            append_workflow_activity(
                "research-portfolio-failure-backoff-cleared",
                f"Cleared the {archetype} lane failure circuit after a substantive result",
                campaign_id=campaign_id,
                target_symbol=target_symbol,
                active_file=active_file,
                **transition,
            )


def _entry_route_signature(entry: LedgerEntry) -> str:
    """Return a stored signature or derive one for a pre-signature ledger entry."""
    inputs = dict(entry.spec.inputs)
    stored = str(inputs.get("route_signature", "") or "").strip()
    if stored:
        return stored
    return _stable_route_signature(
        archetype=entry.spec.archetype,
        target_symbol=str(inputs.get("target_symbol", "") or ""),
        active_file=str(inputs.get("active_file", "") or ""),
        objective=entry.spec.objective,
    )


def _route_family(route_key: str) -> str:
    """Return the stable family used to dedupe one evidence consumption."""
    normalized = str(route_key or "").strip()
    for prefix in ("handoff-synthesis-after:", "refresh-after:"):
        if normalized.startswith(prefix):
            return prefix.removesuffix(":")
    return normalized


def _route_requires_anchor(route_key: str) -> bool:
    """Return whether a route derives its work from one prior finding."""
    family = _route_family(route_key)
    return family in _EVIDENCE_DERIVED_BASE_ROUTES or family in {
        "handoff-synthesis-after",
        "refresh-after",
    }


def _route_anchor_finding_summary(entry: LedgerEntry) -> tuple[str, str, bool]:
    """Return bounded canonical source-finding JSON, its digest, and truncation flag."""
    raw_deliverable = entry.result.get("deliverable")
    source = {
        "status": str(entry.result.get("status", "") or entry.state),
        "deliverable": research_route_context.strip_parent_route_context(
            raw_deliverable if isinstance(raw_deliverable, Mapping) else None
        ),
        "artifact_paths": list(entry.result.get("artifact_paths") or []),
        "plan_delta": list(entry.result.get("plan_delta") or []),
    }
    canonical = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if len(canonical) <= ROUTE_ANCHOR_SUMMARY_CAP:
        return canonical, digest, False
    prefix_limit = max(500, ROUTE_ANCHOR_SUMMARY_CAP - 500)
    bounded = json.dumps(
        {
            "exact_source_prefix": canonical[:prefix_limit],
            "source_sha256": digest,
            "truncated": True,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    while len(bounded) > ROUTE_ANCHOR_SUMMARY_CAP and prefix_limit > 500:
        prefix_limit -= min(500, prefix_limit - 500)
        bounded = json.dumps(
            {
                "exact_source_prefix": canonical[:prefix_limit],
                "source_sha256": digest,
                "truncated": True,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return bounded, digest, True


def _route_anchor_provenance(
    entry: LedgerEntry,
    *,
    finding_sha256: str,
) -> dict[str, Any]:
    """Return exact assignment and route identity for one source finding."""
    inputs = dict(entry.spec.inputs or {})
    return {
        "job_id": entry.spec.job_id,
        "archetype": entry.spec.archetype,
        "target_symbol": str(inputs.get("target_symbol", "") or ""),
        "active_file": str(inputs.get("active_file", "") or ""),
        "route_key": str(inputs.get("route_key", "") or ""),
        "route_signature": _entry_route_signature(entry),
        "finding_sha256": finding_sha256,
    }


def _route_anchor_consumption_key(
    route_key: str,
    entry: LedgerEntry,
    *,
    finding_sha256: str,
) -> str:
    """Return a stable once-only key for route-family/source-finding use."""
    payload = "\x1f".join(
        (
            _route_family(route_key),
            entry.spec.job_id,
            finding_sha256,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _prior_route_excerpt(entry: LedgerEntry, *, limit: int = 480) -> str:
    """Return a compact durable boundary for a novelty-refresh route."""
    raw_deliverable = entry.result.get("deliverable")
    deliverable = research_route_context.strip_parent_route_context(
        raw_deliverable if isinstance(raw_deliverable, Mapping) else None
    )
    if deliverable:
        raw = json.dumps(deliverable, ensure_ascii=False, sort_keys=True, default=str)
    elif entry.notes:
        raw = entry.notes
    else:
        raw = f"no structured deliverable; terminal state {entry.state}"
    compact = " ".join(str(raw).split())
    bounded = max(80, int(limit))
    return compact[:bounded] + ("..." if len(compact) > bounded else "")


def _route_boundary_handoff(entry: LedgerEntry) -> dict[str, Any]:
    """Return a provenance-matched route-boundary handoff, if present."""
    deliverable = dict(entry.result.get("deliverable") or {})
    if str(deliverable.get("status", "") or "") != "interrupted_with_evidence":
        return {}
    raw_boundary = deliverable.get("route_boundary")
    if not isinstance(raw_boundary, Mapping):
        return {}
    boundary = dict(raw_boundary)
    raw_provenance = boundary.get("provenance")
    provenance = dict(raw_provenance) if isinstance(raw_provenance, Mapping) else {}
    inputs = dict(entry.spec.inputs or {})
    expected = {
        "job_id": entry.spec.job_id,
        "target_symbol": str(inputs.get("target_symbol", "") or ""),
        "active_file": str(inputs.get("active_file", "") or ""),
        "route_key": str(inputs.get("route_key", "") or ""),
        "route_signature": _entry_route_signature(entry),
    }
    if any(str(provenance.get(key, "") or "") != value for key, value in expected.items()):
        return {}
    evidence = boundary.get("evidence")
    if not isinstance(evidence, list) or not any(
        isinstance(item, Mapping) and str(item.get("result_excerpt", "") or "").strip()
        for item in evidence
    ):
        return {}
    return boundary


def _boundary_synthesis_excerpt(boundary: Mapping[str, Any], *, limit: int = 9000) -> str:
    """Render every selected observation into one bounded synthesis handoff."""
    lines: list[str] = []
    for index, raw_item in enumerate(boundary.get("evidence") or [], start=1):
        if not isinstance(raw_item, Mapping):
            continue
        tool = str(raw_item.get("tool", "") or "unknown")
        arguments = " ".join(str(raw_item.get("arguments", "") or "").split())[:240]
        result = " ".join(str(raw_item.get("result_excerpt", "") or "").split())[:700]
        if not result:
            continue
        lines.append(f"{index}. {tool}({arguments}) => {result}")
    reasoning = [
        " ".join(str(item).split())[:400]
        for item in (boundary.get("reasoning") or [])
        if str(item).strip()
    ]
    if reasoning:
        lines.append("Prior reasoning: " + " | ".join(reasoning))
    compact = "\n".join(lines)
    bounded = max(500, int(limit))
    return compact[:bounded] + ("..." if len(compact) > bounded else "")


def _has_substantive_route_evidence(entry: LedgerEntry) -> bool:
    """Return whether a worker produced evidence worth anchoring a refresh to."""
    novelty = research_route_context.classify_semantic_novelty(
        entry,
        [entry],
    )
    return bool(novelty.get("progress_anchor_eligible", False))


def _is_progress_route_evidence(
    entry: LedgerEntry,
    *,
    semantic_entries: Sequence[LedgerEntry],
) -> bool:
    """Return whether parent-derived semantics permit a progress-producing refresh."""
    evidence = research_route_context.semantic_evidence(entry)
    if research_route_context.semantic_anchor_superseded(entry, semantic_entries):
        return False
    novelty = research_route_context.classify_semantic_novelty(entry, semantic_entries)
    if not bool(novelty.get("progress_anchor_eligible", False)):
        return False
    # Checked constructive helpers belong in the foreground integration queue,
    # not a recursive audit cascade. One narrow exception is a checked,
    # non-finite obstruction with no target replacement: it changes which proof
    # family is viable, so one anchored worker may investigate the concrete
    # dependency it exposed. Route-signature deduplication limits that exact
    # finding to one ``refresh-after`` route.
    checked_obstruction = bool(
        evidence.has_checked_helper
        and evidence.obstructions
        and not evidence.witnesses
        and not evidence.congruences
        and not bool(novelty.get("checked_target_replacement", False))
    )
    if evidence.has_checked_helper and not checked_obstruction:
        return False
    # Mathematical novelty is durable knowledge, but an explicitly
    # non-closing finite leaf after repeated failed integrations cannot fuel a
    # recursive evidence-to-helper/audit cascade. Foreground actionability is
    # the stricter parent-owned policy for that distinction.
    finding = {
        "target_symbol": str(entry.spec.inputs.get("target_symbol", "") or ""),
        "active_file": str(entry.spec.inputs.get("active_file", "") or ""),
        "deliverable": dict(entry.result.get("deliverable") or {}),
        "semantic_novelty": novelty,
    }
    return research_findings.foreground_use_role(finding) == "actionable"


def _anchor_already_consumed(
    entries: Sequence[LedgerEntry],
    *,
    route_key: str,
    anchor: LedgerEntry,
) -> bool:
    """Return whether this route family already consumed the exact finding."""
    _summary, finding_sha256, _truncated = _route_anchor_finding_summary(anchor)
    expected_key = _route_anchor_consumption_key(
        route_key,
        anchor,
        finding_sha256=finding_sha256,
    )
    family = _route_family(route_key)
    for entry in entries:
        inputs = dict(entry.spec.inputs or {})
        stored_key = str(inputs.get("route_anchor_consumption_key", "") or "")
        if stored_key and stored_key == expected_key:
            return True
        # Resume-safe compatibility for an anchored spec written before the
        # explicit consumption key existed.
        if (
            str(inputs.get("route_anchor_job_id", "") or "") == anchor.spec.job_id
            and _route_family(str(inputs.get("route_key", "") or "")) == family
        ):
            return True
    return False


def _latest_unconsumed_anchor(
    entries: Sequence[LedgerEntry],
    *,
    route_key: str,
    semantic_entries: Sequence[LedgerEntry] | None = None,
) -> LedgerEntry | None:
    """Return the newest substantive source not yet used by this route family."""
    family = _route_family(route_key)
    semantic_history = semantic_entries if semantic_entries is not None else entries
    for entry in reversed(entries):
        source_route = str(entry.spec.inputs.get("route_key", "") or "")
        if _route_family(source_route) == family:
            continue
        if not entry.is_terminal() or not _has_substantive_route_evidence(entry):
            continue
        if _anchor_already_consumed(entries, route_key=route_key, anchor=entry):
            continue
        evidence = research_route_context.semantic_evidence(entry)
        if family == "evidence-to-helper" and evidence.has_checked_helper:
            continue
        if not _is_progress_route_evidence(entry, semantic_entries=semantic_history):
            continue
        return entry
    return None


def _anchored_followup_delivery_plan(
    entries: Sequence[LedgerEntry],
    findings: Sequence[Mapping[str, Any]],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any]:
    """Plan foreground delivery around exact evidence-synthesis reservations.

    An ``evidence-to-helper`` worker consumes one exact source finding. While
    that worker is active, or while its substantive result still awaits parent
    harvesting, reserve the source so the foreground does not independently
    perform the same synthesis. Once the follow-up finding is materialized,
    prioritize it and couple the source receipt to that delivery. Failed,
    killed, and non-substantive follow-ups never suppress their source.
    """
    finding_by_id = {
        str(finding.get("job_id", "") or ""): dict(finding)
        for finding in findings
        if isinstance(finding, Mapping) and str(finding.get("job_id", "") or "")
    }
    finding_ids = set(finding_by_id)
    if not finding_ids:
        return {
            "deferred_source_job_ids": [],
            "priority_followup_job_ids": [],
            "source_receipts_by_followup": {},
        }

    normalized_campaign = str(campaign_id or "").strip()
    by_source: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        inputs = dict(entry.spec.inputs or {})
        source_job_id = str(inputs.get("route_anchor_job_id", "") or "").strip()
        if source_job_id not in finding_ids:
            continue
        if str(inputs.get("route_mode", "") or "") != "evidence_synthesis":
            continue
        if _route_family(str(inputs.get("route_key", "") or "")) != "evidence-to-helper":
            continue
        consumption_key = str(inputs.get("route_anchor_consumption_key", "") or "").strip()
        provenance = inputs.get("route_anchor_provenance")
        provenance_map = dict(provenance) if isinstance(provenance, Mapping) else {}
        if not consumption_key or str(provenance_map.get("job_id", "") or "") != source_job_id:
            continue
        if str(provenance_map.get("target_symbol", "") or "") != str(target_symbol or ""):
            continue
        provenance_file = str(provenance_map.get("active_file", "") or "")
        if not provenance_file or os.path.realpath(provenance_file) != os.path.realpath(
            active_file
        ):
            continue
        if not _job_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        ):
            continue
        entry_campaign = str(inputs.get("campaign_id", "") or "").strip()
        if normalized_campaign and entry_campaign and entry_campaign != normalized_campaign:
            continue
        if (
            normalized_campaign
            and not entry_campaign
            and not is_ancestor(
                normalized_campaign,
                entry.spec.job_id,
            )
        ):
            continue
        by_source.setdefault(source_job_id, []).append(entry)

    deferred: set[str] = set()
    priority: list[str] = []
    source_receipts: dict[str, list[str]] = {}
    for source_job_id, followups in by_source.items():
        if any(not entry.is_terminal() for entry in followups):
            deferred.add(source_job_id)
            continue

        substantive_done: list[LedgerEntry] = []
        for entry in followups:
            if entry.state != "done":
                continue
            try:
                candidate = finding_by_id.get(entry.spec.job_id)
                if candidate is None:
                    candidate = research_findings.build_finding_record(
                        entry,
                        entry.result,
                        entries=entries,
                    )
                substantive = research_findings.has_actionable_exact_candidate(
                    candidate,
                )
            except Exception:
                substantive = False
            if substantive:
                substantive_done.append(entry)
        if not substantive_done:
            continue
        if any(not entry.consumed for entry in substantive_done):
            deferred.add(source_job_id)
            continue

        materialized = next(
            (entry for entry in reversed(substantive_done) if entry.spec.job_id in finding_ids),
            None,
        )
        if materialized is None:
            # Do not create a delivery-capacity deadlock. If harvesting did not
            # materialize the follow-up, keep the source available.
            continue
        followup_job_id = materialized.spec.job_id
        if followup_job_id not in priority:
            priority.append(followup_job_id)
        source_receipts.setdefault(followup_job_id, []).append(source_job_id)

    return {
        "deferred_source_job_ids": sorted(deferred),
        "priority_followup_job_ids": priority,
        "source_receipts_by_followup": {
            job_id: sorted(set(source_ids)) for job_id, source_ids in source_receipts.items()
        },
    }


def prepare_anchored_foreground_findings(
    findings: Sequence[Mapping[str, Any]],
    *,
    summary: Mapping[str, Any],
    campaign_id: str,
    target_symbol: str,
    active_file: str,
) -> tuple[dict[str, Any], ...]:
    """Reserve anchored sources and prioritize their substantive follow-ups.

    The summary's ledger snapshot is parent-owned and already loaded by the
    foreground handoff. Reading it keeps this projection free of dispatch
    writes and avoids a second filesystem transaction inside a tool callback.
    """
    entries: list[LedgerEntry] = []
    for raw in dispatch_ledger_compaction.hydrate_dispatch_ledger(
        summary.get("dispatch_ledger") or [],
        state_root=workflow_state_root(),
    ):
        try:
            entries.append(LedgerEntry.from_mapping(raw))
        except (TypeError, ValueError):
            continue
    prepared = [dict(finding) for finding in findings if isinstance(finding, Mapping)]
    plan = _anchored_followup_delivery_plan(
        entries,
        prepared,
        campaign_id=str(campaign_id or "").strip(),
        target_symbol=target_symbol,
        active_file=active_file,
    )
    deferred = {
        str(job_id) for job_id in (plan.get("deferred_source_job_ids") or []) if str(job_id)
    }
    source_receipts = {
        str(job_id): tuple(str(source_id) for source_id in source_ids if str(source_id))
        for job_id, source_ids in dict(plan.get("source_receipts_by_followup") or {}).items()
        if str(job_id)
        and isinstance(source_ids, Sequence)
        and not isinstance(source_ids, (str, bytes))
    }
    superseded = {source_id for source_ids in source_receipts.values() for source_id in source_ids}
    entry_by_id = {entry.spec.job_id: entry for entry in entries}
    filtered: list[dict[str, Any]] = []
    for finding in prepared:
        job_id = str(finding.get("job_id", "") or "")
        if job_id in deferred or job_id in superseded:
            continue
        source_ids = source_receipts.get(job_id, ())
        if source_ids:
            followup_entry = entry_by_id.get(job_id)
            inputs = dict(followup_entry.spec.inputs or {}) if followup_entry else {}
            finding["route_anchor_delivery"] = {
                "source_job_id": source_ids[0],
                "source_job_ids": list(source_ids),
                "route_anchor_consumption_key": str(
                    inputs.get("route_anchor_consumption_key", "") or ""
                ),
                "route_anchor_provenance": dict(inputs.get("route_anchor_provenance") or {}),
                "policy": (
                    "This actionable schema-valid exact follow-up consumes the source finding. "
                    "Do not independently rerun that source synthesis; parent-check and integrate "
                    "this follow-up, then continue a distinct unresolved route."
                ),
            }
        filtered.append(finding)

    priority = {
        str(job_id): index
        for index, job_id in enumerate(plan.get("priority_followup_job_ids") or [])
        if str(job_id)
    }
    return tuple(
        finding
        for _index, finding in sorted(
            enumerate(filtered),
            key=lambda item: (
                0 if str(item[1].get("job_id", "") or "") in priority else 1,
                priority.get(str(item[1].get("job_id", "") or ""), item[0]),
                item[0],
            ),
        )
    )


def foreground_delivery_job_ids(finding: Mapping[str, Any]) -> tuple[str, ...]:
    """Return one rendered finding id plus any exact source receipt it subsumes."""
    job_id = str(finding.get("job_id", "") or "").strip()
    anchor = finding.get("route_anchor_delivery")
    anchor_map = dict(anchor) if isinstance(anchor, Mapping) else {}
    source_ids = anchor_map.get("source_job_ids") or [anchor_map.get("source_job_id", "")]
    values = [job_id]
    if isinstance(source_ids, Sequence) and not isinstance(source_ids, (str, bytes)):
        values.extend(str(source_id).strip() for source_id in source_ids if str(source_id).strip())
    return tuple(dict.fromkeys(value for value in values if value))


def persist_foreground_negative_evidence(
    findings: Sequence[Mapping[str, Any]],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
) -> tuple[str, ...]:
    """Persist delivered research dead ends for the exact foreground assignment."""
    target = str(target_symbol or "").strip()
    source_file = str(active_file or "").strip()
    if not target or not source_file:
        return ()
    persisted: list[str] = []
    fallback_time = _now_iso()
    for index, finding in enumerate(findings):
        evidence = research_findings.negative_evidence_lines(finding)
        if not evidence:
            continue
        job_id = str(finding.get("job_id", "") or "").strip() or f"finding-{index}"
        checkpoint_seed = "\0".join((str(campaign_id or "").strip(), job_id, target, source_file))
        checkpoint_id = (
            "research-negative-" + hashlib.sha256(checkpoint_seed.encode("utf-8")).hexdigest()[:20]
        )
        created_at = str(
            finding.get("consumed_at", "")
            or finding.get("completed_at", "")
            or finding.get("created_at", "")
            or fallback_time
        )
        if plan_state.record_checkpoint_advisory(
            checkpoint_id=checkpoint_id,
            created_at=created_at,
            target_symbol=target,
            active_file=source_file,
            negative_evidence=list(evidence),
        ):
            persisted.append(job_id)
    return tuple(persisted)


def _select_distinct_route(
    entries: Sequence[LedgerEntry],
    *,
    archetype: str,
    generation: int,
    target_symbol: str,
    active_file: str,
    forbidden_delta_signatures: frozenset[str] = frozenset(),
    universal_obstruction: research_obstruction_dominance.UniversalObstruction | None = None,
) -> tuple[str, str, str]:
    """Select an unused assignment-local route and a durable refresh anchor.

    Once the finite grounding routes are spent, the newest terminal job becomes
    an evidence boundary: its concrete result is summarized and the replacement
    must investigate only a dependency or assumption that result left open.
    """
    assignment_entries = [
        entry
        for entry in entries
        if _job_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    ]
    matching = [entry for entry in assignment_entries if entry.spec.archetype == archetype]
    used_signatures = {_entry_route_signature(entry) for entry in matching}

    def delta_available(focus: str) -> bool:
        return (
            _mathematical_delta_signature(
                target_symbol=target_symbol,
                active_file=active_file,
                focus=focus,
            )
            not in forbidden_delta_signatures
        )

    obstruction_routes = {
        "negation_probe": (
            "universal-obstruction-promotion",
            (
                "use parent-kernel-verified exact universal obstruction helper "
                f"{universal_obstruction.name if universal_obstruction else '[missing]'} to derive "
                "and directly check the exact closed-negation bridge required by authoritative "
                "source promotion; do not test another finite instance"
            ),
        ),
        "decomposition": (
            "universal-obstruction-replan",
            (
                "prepare a source-backed replan for the invalid target/decomposition around "
                f"verified obstruction {universal_obstruction.name if universal_obstruction else '[missing]'}; "
                "preserve the obstruction and investigate one materially different proof shape "
                "if authoritative promotion still needs repair"
            ),
        ),
    }
    obstruction_route = obstruction_routes.get(archetype) if universal_obstruction else None
    if obstruction_route is not None:
        route_key, focus = obstruction_route
        objective = _job_objective(
            target_symbol=target_symbol,
            active_file=active_file,
            generation=generation,
            focus=focus,
        )
        signature = _stable_route_signature(
            archetype=archetype,
            target_symbol=target_symbol,
            active_file=active_file,
            objective=objective,
        )
        if signature not in used_signatures and delta_available(focus):
            return route_key, focus, ""

    # A hard search boundary is an explicit synthesis checkpoint, not a failed
    # route. Consume its bounded evidence through one non-search proof-shaping
    # turn before launching another generic grounding route.
    prior_boundary_synthesis = any(
        str(dict(entry.spec.inputs or {}).get("route_key", "") or "").startswith(
            "handoff-synthesis-after:"
        )
        for entry in matching
    )
    boundary_anchors: list[tuple[LedgerEntry, dict[str, Any]]] = []
    if not prior_boundary_synthesis:
        for candidate in matching:
            candidate_boundary = _route_boundary_handoff(candidate)
            if candidate_boundary and _is_progress_route_evidence(
                candidate,
                semantic_entries=assignment_entries,
            ):
                boundary_anchors.append((candidate, candidate_boundary))
    if boundary_anchors:
        # One synthesis turn receives every currently preserved boundary. Once
        # that lane has started, later refills must rotate instead of creating
        # one synthesis worker per boundary or recursively synthesizing a
        # capped synthesis worker.
        boundary_anchor, _ = boundary_anchors[-1]
        preserved_handoffs = "\n\n".join(
            (
                f"Boundary job {candidate.spec.job_id}:\n"
                f"{_boundary_synthesis_excerpt(candidate_boundary)}"
            )
            for candidate, candidate_boundary in boundary_anchors
        )
        focus = (
            f"synthesize preserved evidence from {len(boundary_anchors)} route-boundary job(s), "
            f"anchored by {boundary_anchor.spec.job_id}, without broad web/library search; derive one "
            "concrete formula, helper lemma, or proof shape and run a direct check before any "
            "new retrieval. Preserved handoffs:\n"
            f"{preserved_handoffs}"
        )
        route_key = f"handoff-synthesis-after:{boundary_anchor.spec.job_id}"
        objective = _job_objective(
            target_symbol=target_symbol,
            active_file=active_file,
            generation=generation,
            focus=focus,
        )
        signature = _stable_route_signature(
            archetype=archetype,
            target_symbol=target_symbol,
            active_file=active_file,
            objective=objective,
        )
        if signature not in used_signatures and delta_available(focus):
            return route_key, focus, boundary_anchor.spec.job_id

    for route_key, base_focus in _route_focuses(archetype):
        focus = base_focus
        route_anchor_job_id = ""
        if route_key in _EVIDENCE_DERIVED_BASE_ROUTES:
            evidence_anchor = _latest_unconsumed_anchor(
                matching,
                route_key=route_key,
                semantic_entries=assignment_entries,
            )
            if evidence_anchor is None:
                continue
            _summary, finding_sha256, _truncated = _route_anchor_finding_summary(evidence_anchor)
            route_anchor_job_id = evidence_anchor.spec.job_id
            focus = (
                f"{base_focus} using the already-gathered exact finding from source job "
                f"{route_anchor_job_id} (finding {finding_sha256[:16]}); do not rerun its "
                "sweep or rediscover its witnesses"
            )
        objective = _job_objective(
            target_symbol=target_symbol,
            active_file=active_file,
            generation=generation,
            focus=focus,
        )
        signature = _stable_route_signature(
            archetype=archetype,
            target_symbol=target_symbol,
            active_file=active_file,
            objective=objective,
        )
        if signature not in used_signatures and delta_available(focus):
            return route_key, focus, route_anchor_job_id

    anchor = next(
        (
            entry
            for entry in reversed(matching)
            if _has_substantive_route_evidence(entry)
            and _is_progress_route_evidence(
                entry,
                semantic_entries=assignment_entries,
            )
        ),
        None,
    )
    if anchor is not None:
        anchor_signature = _entry_route_signature(anchor)
        focus = (
            f"audit prior job {anchor.spec.job_id} (route {anchor_signature}) whose result was: "
            f"{_prior_route_excerpt(anchor)}. Isolate one concrete unresolved dependency or "
            "unsupported assumption that result did not settle, then investigate only that delta "
            "with a materially different proof shape"
        )
        route_key = f"refresh-after:{anchor.spec.job_id}"
        objective = _job_objective(
            target_symbol=target_symbol,
            active_file=active_file,
            generation=generation,
            focus=focus,
        )
        signature = _stable_route_signature(
            archetype=archetype,
            target_symbol=target_symbol,
            active_file=active_file,
            objective=objective,
        )
        if signature not in used_signatures and delta_available(focus):
            return route_key, focus, anchor.spec.job_id

    # A malformed legacy ledger can theoretically make the newest anchor
    # collide. Bind the fallback to the complete prior-signature set, so a
    # successful replacement extends the set and the next refresh differs.
    history_digest = hashlib.sha256("\n".join(sorted(used_signatures)).encode("utf-8")).hexdigest()[
        :16
    ]
    lane_focus = _ACTIVE_DELTA_ROTATION_FOCUSES.get(
        archetype,
        "check a concrete unresolved dependency outside all active mathematical lanes",
    )
    focus = f"outside prior route-set {history_digest}, {lane_focus}"
    if not delta_available(focus):
        active_delta_digest = hashlib.sha256(
            "\n".join(sorted(forbidden_delta_signatures)).encode("utf-8")
        ).hexdigest()[:16]
        focus = f"outside active portfolio-set {active_delta_digest}, {lane_focus}"
    return f"history-refresh:{history_digest}", focus, ""


def _persist_delivery_backpressure_state(
    summary: Mapping[str, Any],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    active: bool,
    backlog: int,
) -> bool:
    """Persist active-delivery-target backpressure transitions for observability."""
    normalized_campaign = str(campaign_id or "campaign")
    normalized_target = str(target_symbol or "")
    normalized_file = str(active_file or "")
    current = dict(summary.get(DELIVERY_BACKPRESSURE_STATE_KEY) or {})
    if active:
        if (
            current.get("active") is True
            and str(current.get("campaign_id", "") or "") == normalized_campaign
            and str(current.get("target_symbol", "") or "") == normalized_target
            and str(current.get("active_file", "") or "") == normalized_file
            and int(current.get("backlog", 0) or 0) == int(backlog)
        ):
            return False
    elif not (
        current.get("active") is True
        and str(current.get("campaign_id", "") or "") == normalized_campaign
    ):
        return False

    def mutate(payload: dict[str, Any]) -> bool:
        # Make any exact-ledger legacy provenance durable in the same
        # transition write; a restarted campaign then computes the same bound.
        research_findings.recover_finding_provenance(payload)
        persisted = dict(payload.get(DELIVERY_BACKPRESSURE_STATE_KEY) or {})
        if active:
            desired = {
                "active": True,
                "campaign_id": normalized_campaign,
                "scope": "active_delivery_target",
                "target_symbol": normalized_target,
                "active_file": normalized_file,
                "backlog": int(backlog),
                "cap": research_findings.DELIVERY_BACKLOG_CAP,
            }
            if all(persisted.get(key) == value for key, value in desired.items()):
                return False
            desired["updated_at"] = _now_iso()
            payload[DELIVERY_BACKPRESSURE_STATE_KEY] = desired
            return True
        if (
            persisted.get("active") is True
            and str(persisted.get("campaign_id", "") or "") == normalized_campaign
        ):
            payload.pop(DELIVERY_BACKPRESSURE_STATE_KEY, None)
            return True
        return False

    return bool(update_json_file(workflow_state_root() / "summary.json", mutate))


def _normalized_replacement_assignment(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
) -> tuple[str, str, str]:
    """Return the stable assignment identity used by replacement intents."""
    return (
        str(campaign_id or "campaign"),
        str(target_symbol or ""),
        os.path.realpath(active_file) if active_file else "",
    )


def _replacement_intent_matches(
    intent: Mapping[str, Any],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether one pending intent belongs to the exact active assignment."""
    normalized_campaign, normalized_target, normalized_file = _normalized_replacement_assignment(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    try:
        version = int(intent.get("version", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        intent.get("pending") is True
        and version == PENDING_REPLACEMENT_STATE_VERSION
        and str(intent.get("campaign_id", "") or "") == normalized_campaign
        and str(intent.get("target_symbol", "") or "") == normalized_target
        and str(intent.get("active_file", "") or "") == normalized_file
    )


def _pending_replacement_intent(
    summary: Mapping[str, Any],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any]:
    """Return the durable replacement intent for the exact active assignment."""
    raw = summary.get(PENDING_REPLACEMENT_STATE_KEY)
    if not isinstance(raw, Mapping) or not _replacement_intent_matches(
        raw,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    ):
        return {}
    return dict(raw)


def _persist_pending_replacement_intent(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    attempt_count: int,
    workers: int,
    requested_slots: int,
    reason: str,
    trigger_job_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Upsert or clear one crash-durable replacement obligation.

    The assignment-scoped record survives planner reservations and campaign
    epoch rollover. Repeated parent heartbeats are write-free when the
    obligation is unchanged, and clearing only touches the exact assignment.
    """
    normalized_campaign, normalized_target, normalized_file = _normalized_replacement_assignment(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    normalized_workers = max(0, int(workers))
    normalized_slots = min(
        normalized_workers,
        max(0, int(requested_slots)),
    )
    normalized_triggers = [
        job_id for job_id in dict.fromkeys(str(item or "") for item in trigger_job_ids) if job_id
    ][-16:]
    identity_payload = json.dumps(
        [normalized_campaign, normalized_target, normalized_file],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    intent_id = "replacement-" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()[:16]

    def mutate(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        raw = payload.get(PENDING_REPLACEMENT_STATE_KEY)
        current = dict(raw) if isinstance(raw, Mapping) else {}
        current_matches = _replacement_intent_matches(
            current,
            campaign_id=normalized_campaign,
            target_symbol=normalized_target,
            active_file=normalized_file,
        )
        if normalized_slots <= 0:
            if not current_matches:
                stale_same_campaign = bool(
                    current.get("pending") is True
                    and str(current.get("campaign_id", "") or "") == normalized_campaign
                )
                if stale_same_campaign:
                    payload.pop(PENDING_REPLACEMENT_STATE_KEY, None)
                return (
                    {
                        "changed": stale_same_campaign,
                        "cleared": False,
                        "created": False,
                        "intent": {},
                        "stale_cleared": stale_same_campaign,
                    },
                    stale_same_campaign,
                )
            payload.pop(PENDING_REPLACEMENT_STATE_KEY, None)
            return (
                {
                    "changed": True,
                    "cleared": True,
                    "created": False,
                    "intent": current,
                },
                True,
            )

        prior_triggers = (
            [str(item or "") for item in (current.get("trigger_job_ids") or [])]
            if current_matches
            else []
        )
        merged_triggers = [
            job_id for job_id in dict.fromkeys([*prior_triggers, *normalized_triggers]) if job_id
        ][-16:]
        now = _now_iso()
        desired: dict[str, Any] = {
            "version": PENDING_REPLACEMENT_STATE_VERSION,
            "pending": True,
            "intent_id": intent_id,
            "campaign_id": normalized_campaign,
            "target_symbol": normalized_target,
            "active_file": normalized_file,
            "attempt_count": max(0, int(attempt_count)),
            "workers": normalized_workers,
            "requested_slots": normalized_slots,
            "reason": str(reason or "capacity_temporarily_unavailable"),
            "trigger_job_ids": merged_triggers,
            "created_at": str(current.get("created_at", "") or now) if current_matches else now,
        }
        if current_matches and all(current.get(key) == value for key, value in desired.items()):
            return (
                {
                    "changed": False,
                    "cleared": False,
                    "created": False,
                    "intent": current,
                },
                False,
            )
        desired["updated_at"] = now
        payload[PENDING_REPLACEMENT_STATE_KEY] = desired
        return (
            {
                "changed": True,
                "cleared": False,
                "created": not current_matches,
                "intent": desired,
            },
            True,
        )

    outcome = update_json_file_if_changed(workflow_state_root() / "summary.json", mutate)
    return dict(outcome) if isinstance(outcome, Mapping) else {}


def _replacement_pending_status(intent: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact public status projection for one replacement intent."""
    if not intent.get("pending"):
        return {}
    return {
        "replacement_pending": True,
        "replacement_intent_id": str(intent.get("intent_id", "") or ""),
        "replacement_slots": max(0, int(intent.get("requested_slots", 0) or 0)),
    }


def _defer_portfolio_replacement(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    attempt_count: int,
    workers: int,
    requested_slots: int,
    reason: str,
    trigger_job_ids: Sequence[str],
    active_job_ids: Sequence[str],
) -> dict[str, Any]:
    """Persist a vacancy and emit one transition event for a new obligation."""
    outcome = _persist_pending_replacement_intent(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=attempt_count,
        workers=workers,
        requested_slots=requested_slots,
        reason=reason,
        trigger_job_ids=trigger_job_ids,
    )
    intent = dict(outcome.get("intent") or {})
    if outcome.get("created"):
        with contextlib.suppress(Exception):
            append_workflow_activity(
                "research-portfolio-replacement-deferred",
                "Queued a durable background-research replacement obligation",
                campaign_id=campaign_id,
                target_symbol=target_symbol,
                active_file=active_file,
                replacement_intent_id=str(intent.get("intent_id", "") or ""),
                replacement_slots=max(0, int(intent.get("requested_slots", 0) or 0)),
                reason=str(intent.get("reason", "") or ""),
                trigger_job_ids=list(intent.get("trigger_job_ids") or []),
                active_jobs=list(active_job_ids),
            )
    return _replacement_pending_status(intent)


def _complete_portfolio_replacement(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    attempt_count: int,
    workers: int,
    launched_job_ids: Sequence[str],
    active_job_ids: Sequence[str],
) -> bool:
    """Clear an exact fulfilled obligation and report the transition once."""
    outcome = _persist_pending_replacement_intent(
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        attempt_count=attempt_count,
        workers=workers,
        requested_slots=0,
        reason="fulfilled",
    )
    if not outcome.get("cleared"):
        return False
    intent = dict(outcome.get("intent") or {})
    with contextlib.suppress(Exception):
        append_workflow_activity(
            "research-portfolio-replacement-fulfilled",
            "Fulfilled the durable background-research replacement obligation",
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            replacement_intent_id=str(intent.get("intent_id", "") or ""),
            launched=list(launched_job_ids),
            active_jobs=list(active_job_ids),
        )
    return True


def _entry_predates_epoch_refresh(entry: LedgerEntry, refresh: Mapping[str, Any]) -> bool:
    """Return whether one open worker belongs to an epoch before the refresh."""
    try:
        entry_epoch = int(dict(entry.spec.inputs).get("campaign_epoch", 0) or 0)
    except (TypeError, ValueError):
        entry_epoch = 0
    try:
        new_epoch = max(1, int(refresh.get("new_epoch", 1) or 1))
    except (TypeError, ValueError):
        new_epoch = 1
    # Jobs created before epoch tagging have epoch zero and are conservatively
    # part of the spent portfolio. New-epoch jobs are never retired by replay.
    return entry_epoch < new_epoch


def _entry_survives_semantic_epoch_refresh(
    entry: LedgerEntry,
    refresh: Mapping[str, Any],
) -> bool:
    """Carry one freshly launched worker across a short semantic rollover.

    A foreground turn can exhaust its local route portfolio within minutes of
    launching background research. Preserve workers from exactly the epoch
    being closed so they get one additional epoch to publish a finding; an
    older worker is still retired on the next rollover.
    """
    if str(refresh.get("reason", "") or "") != campaign_epoch.SEMANTIC_PORTFOLIO_ROLLOVER_REASON:
        return False
    try:
        entry_epoch = int(dict(entry.spec.inputs).get("campaign_epoch", 0) or 0)
        previous_epoch = int(refresh.get("previous_epoch", 0) or 0)
        new_epoch = int(refresh.get("new_epoch", 0) or 0)
    except (TypeError, ValueError):
        return False
    return previous_epoch > 0 and new_epoch == previous_epoch + 1 and entry_epoch == previous_epoch


def _job_matches_refresh_assignment(entry: LedgerEntry, refresh: Mapping[str, Any]) -> bool:
    """Return whether a worker belongs to the refresh's recorded assignment."""
    target_symbol = str(refresh.get("target_symbol", "") or "")
    active_file = str(refresh.get("active_file", "") or "")
    if not target_symbol and not active_file:
        # An interrupted startup can roll before queue identity is available.
        # In that case every pre-refresh worker in this campaign is stale.
        return True
    return _job_matches_assignment(
        entry,
        target_symbol=target_symbol,
        active_file=active_file,
    )


def _terminal_killed_process_released(
    entry: LedgerEntry,
    *,
    service: DispatchService,
) -> bool:
    """Prove one terminal-killed worker no longer owns capacity.

    Legacy PID-only rows receive a durable release tombstone only after the
    dispatch layer proves process exit or exact command mismatch. Modern exact
    identities retain the existing synchronous termination boundary.
    """
    if entry.state != "killed":
        return True
    if entry.launch_nonce or entry.process_identity().verifiable:
        # Modern workers carry an exact launch identity. The legacy release
        # authority is only a fallback after direct exact-identity retirement.
        try:
            if dispatch_runtime._terminate_dispatch_process_and_wait(entry):
                return True
        except Exception:
            pass
        # A long-dead worker's PID, process group, and session may all be
        # reused together. If the launch token no longer matches, persist
        # exact argv/spec mismatch evidence instead of pinning the campaign.
        try:
            release = service.release_legacy_killed_process_capacity(entry)
        except Exception:
            release = {}
        if release.get("released"):
            with contextlib.suppress(Exception):
                _report_terminal_process_release(entry, service=service, release=release)
            return True
        return False
    try:
        release = service.release_legacy_killed_process_capacity(entry)
    except Exception:
        release = {}
    if release.get("released"):
        try:
            _report_terminal_process_release(entry, service=service, release=release)
        except Exception:
            # Observability cannot reclaim mathematically unrelated actor
            # capacity. The durable unreported marker retries next tick.
            pass
        return True
    try:
        return dispatch_runtime._terminate_dispatch_process_and_wait(entry)
    except Exception:
        return False


def terminal_killed_process_released(
    entry: LedgerEntry,
    *,
    service: DispatchService,
) -> bool:
    """Prove that one terminal-killed worker no longer owns capacity.

    Expose the portfolio's durable legacy-release and exact-identity authority
    to terminal shutdown code so every quiescence path applies the same PID
    reuse-safe boundary.
    """
    return _terminal_killed_process_released(entry, service=service)


def _release_activity_scan(report_key: str) -> tuple[bool, str]:
    """Return whether the activity scan succeeded and a matching timestamp."""
    try:
        events = read_workflow_activity(
            limit=PROCESS_RELEASE_ACTIVITY_SCAN_LIMIT,
            event_types={"research-portfolio-capacity-released"},
        )
    except Exception:
        return False, ""
    for event in reversed(events):
        details = event.get("details")
        if not isinstance(details, Mapping):
            continue
        if str(details.get("release_report_key", "") or "") == report_key:
            return True, str(event.get("timestamp", "") or "")
    return True, ""


@contextlib.contextmanager
def _release_activity_lock(report_key: str) -> Iterator[None]:
    """Serialize one deterministic diagnostic scan-and-append window."""
    digest = hashlib.sha256(report_key.encode("utf-8")).hexdigest()[:32]
    path = workflow_state_root() / "dispatch-jobs" / f"release-report-{digest}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    local_key = str(path.resolve(strict=False))
    with _PROCESS_RELEASE_REPORT_LOCKS_GUARD:
        local_lock = _PROCESS_RELEASE_REPORT_LOCKS.setdefault(
            local_key,
            threading.RLock(),
        )
    with local_lock, path.open("a+b") as handle:
        cross_process = False
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                cross_process = True
            except OSError:
                pass
        try:
            yield
        finally:
            if cross_process and fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _report_terminal_process_release(
    entry: LedgerEntry,
    *,
    service: DispatchService,
    release: Mapping[str, Any],
) -> None:
    """Persist one retryable, idempotently keyed release diagnostic.

    Release truth is committed before observability. Activity failures never
    reclaim the freed actor slot; the unreported ledger marker retries on the
    next portfolio tick. Scanning the deterministic key closes a crash between
    a successful append and the reported-marker transaction.
    """
    if str(release.get("reported_at", "") or ""):
        return
    report_key = str(release.get("report_key", "") or "").strip()
    reason = str(release.get("reason", "") or "").strip()
    if not report_key or not reason:
        return
    with _release_activity_lock(report_key):
        scanned, persisted_at = _release_activity_scan(report_key)
        if not scanned:
            return
        if not persisted_at:
            try:
                append_workflow_activity(
                    "research-portfolio-capacity-released",
                    (
                        "Released terminal legacy dispatch capacity after authoritative "
                        "process evidence"
                    ),
                    release_report_key=report_key,
                    job_id=entry.spec.job_id,
                    archetype=entry.spec.archetype,
                    target_symbol=str(entry.spec.inputs.get("target_symbol", "") or ""),
                    active_file=str(entry.spec.inputs.get("active_file", "") or ""),
                    release_reason=reason,
                    release_evidence_sha256=str(release.get("evidence_sha256", "") or ""),
                    observed_process_started_at=str(release.get("observed_started_at", "") or ""),
                    released_at=str(release.get("released_at", "") or ""),
                    finished_at=str(release.get("finished_at", "") or entry.finished_at),
                    process_id=int(release.get("process_id", 0) or entry.process_id),
                )
            except Exception:
                return
            persisted_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    try:
        service.mark_process_release_reported(
            job_id=entry.spec.job_id,
            report_key=report_key,
            reported_at=persisted_at,
        )
    except Exception:
        # A later tick finds the deterministic activity key and repairs the
        # durable acknowledgement without appending a duplicate event.
        pass


def _reconcile_epoch_worker_refresh(
    service: DispatchService,
    *,
    campaign_id: str,
    refresh: Mapping[str, Any],
    complete_durable: bool,
) -> tuple[list[str], bool, list[str]]:
    """Harvest results, retire spent workers, and complete one refresh token.

    The operation is replay-safe. A crash after only some kills leaves the
    durable token pending; the next maintenance pass resumes from the ledger.
    Successful artifacts are harvested before and during every kill, so a
    worker that wins the completion race remains ``done`` and consumable.
    """
    token = str(refresh.get("token", "") or "")
    service.recover_completed_artifacts()
    for entry in service.entries():
        if entry.state == "running":
            service.poll(entry.spec.job_id)

    def should_retire(entry: LedgerEntry) -> bool:
        """Return whether this refresh owns retirement of one open worker."""
        return (
            _entry_predates_epoch_refresh(entry, refresh)
            and _job_matches_refresh_assignment(entry, refresh)
            and not _entry_survives_semantic_epoch_refresh(entry, refresh)
        )

    carried = [
        entry.spec.job_id
        for entry in service.open_jobs()
        if _entry_predates_epoch_refresh(entry, refresh)
        and _job_matches_refresh_assignment(entry, refresh)
        and _entry_survives_semantic_epoch_refresh(entry, refresh)
    ]
    killed: list[str] = []
    reconciliation_failed = False
    for entry in service.open_jobs():
        if not should_retire(entry):
            continue
        try:
            outcome = service.kill(
                entry.spec.job_id,
                requester_job_id=campaign_id or service.root_job_id,
            )
        except (Exception, KeyboardInterrupt):
            reconciliation_failed = True
            continue
        if outcome.get("killed") or outcome.get("state") == "killed":
            killed.append(entry.spec.job_id)

    terminal_process_survivors = [
        entry.spec.job_id
        for entry in service.entries()
        if should_retire(entry) and not _terminal_killed_process_released(entry, service=service)
    ]

    # A result can publish while the cancellation boundary is being reaped.
    # Recover it before deciding whether any pre-refresh worker is still open.
    service.recover_completed_artifacts()
    remaining = [entry.spec.job_id for entry in service.open_jobs() if should_retire(entry)]
    completed = not reconciliation_failed and not remaining and not terminal_process_survivors
    if completed and complete_durable:
        completed = campaign_epoch.complete_worker_refresh(
            refresh_token=token,
            killed_job_ids=killed,
        ) or not campaign_epoch.pending_worker_refresh(campaign_id=campaign_id)
    return killed, completed, carried


def _maintain_portfolio_once(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    attempt_count: int,
    workers: int,
    refill: bool,
) -> dict[str, Any]:
    """Run one serialized portfolio reconciliation transaction.

    Reconciliation still runs when background capacity is zero so a resumed
    ``--no-parallel`` campaign cannot retain dead jobs from an earlier runner.
    Any surviving open worker is stopped to enforce the zero-capacity profile.
    """
    capacity = max(0, workers)
    service = DispatchService(
        root_job_id=campaign_id or "campaign",
        cap=max(1, capacity),
        async_launch_admission=lambda summary: _summary_allows_provider_launch(
            summary,
            campaign_id=campaign_id,
            now_epoch=_utc_now().timestamp(),
        ),
    )
    service.recover_completed_artifacts()

    for entry in service.entries():
        # A launch reservation is already active capacity and must be resumed
        # before the portfolio decides whether to fill another lane.
        if entry.state in {"deployed", "running"}:
            service.poll(entry.spec.job_id)

    epoch_refresh = campaign_epoch.pending_worker_refresh(campaign_id=campaign_id)
    epoch_refresh_killed: list[str] = []
    epoch_refresh_carried: list[str] = []
    epoch_refresh_completed = True
    if epoch_refresh:
        (
            epoch_refresh_killed,
            epoch_refresh_completed,
            epoch_refresh_carried,
        ) = _reconcile_epoch_worker_refresh(
            service,
            campaign_id=campaign_id,
            refresh=epoch_refresh,
            complete_durable=True,
        )

    retired: list[str] = []
    for entry in service.entries():
        if entry.is_terminal() or _job_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        ):
            continue
        outcome = service.kill(
            entry.spec.job_id,
            requester_job_id=campaign_id or service.root_job_id,
        )
        if outcome.get("killed"):
            retired.append(entry.spec.job_id)

    consumed: list[str] = []
    blueprint = plan_state.load_blueprint()
    universal_obstruction = research_obstruction_dominance.exact_target_universal_obstruction(
        blueprint,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    retired_dominated: list[str] = []
    for entry in _dominated_open_portfolio_jobs(
        service.entries(),
        target_symbol=target_symbol,
        active_file=active_file,
        obstruction=universal_obstruction,
    ):
        outcome = service.kill(
            entry.spec.job_id,
            requester_job_id=campaign_id or service.root_job_id,
        )
        if outcome.get("killed") or outcome.get("state") == "killed":
            retired_dominated.append(entry.spec.job_id)
    if retired_dominated and universal_obstruction is not None:
        service.recover_completed_artifacts()
        append_workflow_activity(
            "research-portfolio-dominance",
            "Retired finite-instance research dominated by a verified universal obstruction",
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            obstruction_node_id=universal_obstruction.node_id,
            obstruction_name=universal_obstruction.name,
            retired_jobs=retired_dominated,
            replacement_lane_order=["negation_probe", "decomposition", "deep_search"],
        )
    entries_before_consumption = service.entries()
    for entry in entries_before_consumption:
        if entry.state != "done" or entry.consumed:
            continue
        # Persist the deliverable first. If either write fails, the unconsumed
        # ledger entry remains a durable retry signal; finding insertion is
        # job-idempotent for a crash between these two commits.
        _record_finding(
            entry,
            entry.result,
            entries=entries_before_consumption,
            delivery_target_symbol=target_symbol,
            delivery_active_file=active_file,
            blueprint=blueprint,
        )
        service.consume(entry.spec.job_id)
        consumed.append(entry.spec.job_id)

    entries = service.entries()
    terminal_process_survivors = {
        entry.spec.job_id
        for entry in entries
        if not _terminal_killed_process_released(entry, service=service)
    }
    active = [
        entry
        for entry in entries
        if not entry.is_terminal() or entry.spec.job_id in terminal_process_survivors
    ]
    provider_now_epoch = _utc_now().timestamp()
    provider_retry_after = _later_provider_usage_limit(
        _active_provider_usage_limit(
            entries,
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            now_epoch=provider_now_epoch,
        ),
        _active_campaign_provider_usage_limit(
            campaign_id=campaign_id,
            now_epoch=provider_now_epoch,
        ),
    )
    launched: list[str] = []
    primary_desired = _desired_archetypes(
        attempt_count,
        capacity,
        universal_obstruction=universal_obstruction is not None,
    )
    target_count = len(primary_desired)
    replacement_slots = max(0, target_count - len(active))
    replacement_trigger_job_ids = [
        entry.spec.job_id
        for entry in entries
        if entry.is_terminal()
        and _job_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    ][-16:]
    if capacity == 0:
        for entry in active:
            service.kill(
                entry.spec.job_id,
                requester_job_id=campaign_id or service.root_job_id,
            )
        entries = service.entries()
        terminal_process_survivors = {
            entry.spec.job_id
            for entry in entries
            if not _terminal_killed_process_released(entry, service=service)
        }
        active = [
            entry
            for entry in entries
            if not entry.is_terminal() or entry.spec.job_id in terminal_process_survivors
        ]
        # Disabling background workers intentionally cancels only the process
        # replacement obligation; sequential research routing remains live.
        _persist_pending_replacement_intent(
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=attempt_count,
            workers=capacity,
            requested_slots=0,
            reason="background_capacity_disabled",
        )
        status: dict[str, Any] = {
            "active": len(active),
            "active_jobs": [entry.spec.job_id for entry in active],
            "launched": launched,
            "consumed": consumed,
        }
        if active:
            status["cleanup_pending"] = True
            status["still_active"] = [entry.spec.job_id for entry in active]
        if epoch_refresh_killed:
            status["epoch_refresh_killed"] = epoch_refresh_killed
        if epoch_refresh_carried:
            status["epoch_refresh_carried"] = epoch_refresh_carried
        if provider_retry_after:
            status.update(
                {
                    "provider_unavailable": True,
                    "provider_retry_after": provider_retry_after,
                }
            )
        return status
    if provider_retry_after:
        replacement_status = _defer_portfolio_replacement(
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=attempt_count,
            workers=capacity,
            requested_slots=replacement_slots,
            reason="provider_usage_limit",
            trigger_job_ids=replacement_trigger_job_ids,
            active_job_ids=[entry.spec.job_id for entry in active],
        )
        return {
            "active": len(active),
            "active_jobs": [entry.spec.job_id for entry in active],
            "launched": launched,
            "consumed": consumed,
            "refill_deferred": True,
            "provider_unavailable": True,
            "provider_retry_after": provider_retry_after,
            **replacement_status,
        }
    if epoch_refresh and not epoch_refresh_completed:
        replacement_status = _defer_portfolio_replacement(
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=attempt_count,
            workers=capacity,
            requested_slots=replacement_slots,
            reason="epoch_refresh_pending",
            trigger_job_ids=replacement_trigger_job_ids,
            active_job_ids=[entry.spec.job_id for entry in active],
        )
        return {
            "active": len(active),
            "active_jobs": [entry.spec.job_id for entry in active],
            "launched": launched,
            "consumed": consumed,
            "epoch_refresh_pending": True,
            "epoch_refresh_killed": epoch_refresh_killed,
            "epoch_refresh_carried": epoch_refresh_carried,
            **replacement_status,
        }
    if not refill:
        # A foreground planner wave shares the same actor capacity as process
        # research. Reap and consume completed work, but leave the first freed
        # slot vacant so the short-lived planner cannot be perpetually beaten.
        # Persist the vacancy in the same maintenance transaction so planner
        # release or an intervening epoch rollover cannot lose the refill.
        replacement_status = _defer_portfolio_replacement(
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=attempt_count,
            workers=capacity,
            requested_slots=replacement_slots,
            reason="planner_capacity_reserved",
            trigger_job_ids=replacement_trigger_job_ids,
            active_job_ids=[entry.spec.job_id for entry in active],
        )
        return {
            "active": len(active),
            "active_jobs": [entry.spec.job_id for entry in active],
            "launched": launched,
            "consumed": consumed,
            "refill_deferred": True,
            **replacement_status,
        }
    summary = read_json_file(workflow_state_root() / "summary.json")
    delivery_backlog = research_findings.campaign_delivery_backlog_count(
        summary,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        blueprint=blueprint,
    )
    if delivery_backlog >= research_findings.DELIVERY_BACKLOG_CAP:
        existing_replacement = _pending_replacement_intent(
            summary,
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        replacement_status = (
            _defer_portfolio_replacement(
                campaign_id=campaign_id,
                target_symbol=target_symbol,
                active_file=active_file,
                attempt_count=attempt_count,
                workers=capacity,
                requested_slots=replacement_slots,
                reason="delivery_backpressure",
                trigger_job_ids=replacement_trigger_job_ids,
                active_job_ids=[entry.spec.job_id for entry in active],
            )
            if replacement_trigger_job_ids or existing_replacement
            else {}
        )
        backpressure_changed = _persist_delivery_backpressure_state(
            summary,
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            active=True,
            backlog=delivery_backlog,
        )
        if backpressure_changed or consumed or retired:
            append_workflow_activity(
                "research-portfolio-backpressure",
                "Paused active-scope research refill until foreground evidence delivery catches up",
                campaign_id=campaign_id,
                delivery_backlog_scope="active_delivery_target",
                target_symbol=target_symbol,
                active_file=active_file,
                active_jobs=[entry.spec.job_id for entry in active],
                consumed=consumed,
                retired_stale=retired,
                delivery_backlog=delivery_backlog,
                delivery_backlog_cap=research_findings.DELIVERY_BACKLOG_CAP,
            )
        return {
            "active": len(active),
            "active_jobs": [entry.spec.job_id for entry in active],
            "launched": launched,
            "consumed": consumed,
            "delivery_backpressure": True,
            "delivery_backlog": delivery_backlog,
            **replacement_status,
        }
    backpressure_cleared = _persist_delivery_backpressure_state(
        summary,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        active=False,
        backlog=delivery_backlog,
    )
    if backpressure_cleared:
        append_workflow_activity(
            "research-portfolio-backpressure-cleared",
            "Resumed active-scope research refill after foreground evidence delivery",
            campaign_id=campaign_id,
            delivery_backlog_scope="active_delivery_target",
            target_symbol=target_symbol,
            active_file=active_file,
            delivery_backlog=delivery_backlog,
            delivery_backlog_cap=research_findings.DELIVERY_BACKLOG_CAP,
        )
    campaign_payload = dict(summary.get("campaign") or {})
    assignment_revision = _assignment_revision(
        blueprint,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    source_revision = _source_revision(active_file)
    semantic_cooldowns = _semantic_lane_cooldowns(
        entries,
        target_symbol=target_symbol,
        active_file=active_file,
        blueprint=blueprint,
        campaign=campaign_payload,
        assignment_revision=assignment_revision,
    )
    desired = list(primary_desired)
    if set(primary_desired).intersection(semantic_cooldowns):
        # Semantic exhaustion must rotate proof shape, so expose the remaining
        # archetypes as replacement candidates without increasing capacity.
        # Operational backoff alone keeps its historical pressure-reduction
        # semantics and does not expand the portfolio.
        desired.extend(
            archetype
            for archetype in _ROUTE_FOCUSES
            if archetype not in desired
            and not (universal_obstruction is not None and archetype == "empirical")
        )
    failure_circuit = _reconcile_failure_backoff(
        entries,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        archetypes=desired,
        now=_utc_now(),
    )
    _emit_failure_backoff_transitions(
        list(failure_circuit.get("transitions") or []),
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    blocked = {
        str(archetype): dict(record)
        for archetype, record in dict(failure_circuit.get("blocked") or {}).items()
        if isinstance(record, Mapping)
    }
    retry_eligible = {
        str(archetype): dict(record)
        for archetype, record in dict(failure_circuit.get("retry_eligible") or {}).items()
        if isinstance(record, Mapping)
    }
    try:
        campaign_epoch_number = max(0, int(campaign_payload.get("epoch", 0) or 0))
    except (TypeError, ValueError):
        campaign_epoch_number = 0
    retried_archetypes: list[str] = []
    semantic_cooldown_relaxations: list[dict[str, Any]] = []
    provider_launch_deferred = False
    while len(active) < target_count:
        active_archetypes = {entry.spec.archetype for entry in active}
        archetype = next(
            (
                candidate
                for candidate in desired
                if candidate not in active_archetypes
                and candidate not in blocked
                and candidate not in semantic_cooldowns
            ),
            "",
        )
        relaxation: dict[str, Any] | None = None
        if not archetype:
            # A semantic cooldown remains authoritative throughout the epoch that produced it.
            # At a later epoch boundary, however, an all-lane cooldown must not leave configured
            # research capacity permanently empty. Reopen only enough older-epoch lanes to fill
            # otherwise-vacant slots; the history-wide route selector below still requires a
            # distinct objective and signature.
            for candidate in desired:
                if candidate in active_archetypes or candidate in blocked:
                    continue
                record = semantic_cooldowns.get(candidate)
                if record is None:
                    continue
                relaxation = _older_epoch_cooldown_relaxation(
                    archetype=candidate,
                    record=record,
                    current_epoch=campaign_epoch_number,
                )
                if relaxation is not None:
                    archetype = candidate
                    break
        if not archetype:
            break
        active_delta_signatures = frozenset(
            str(
                dict(entry.spec.inputs or {}).get(
                    MATHEMATICAL_DELTA_SIGNATURE_INPUT_KEY,
                    "",
                )
                or ""
            )
            for entry in active
            if _job_matches_assignment(
                entry,
                target_symbol=target_symbol,
                active_file=active_file,
            )
            and str(
                dict(entry.spec.inputs or {}).get(
                    MATHEMATICAL_DELTA_SIGNATURE_INPUT_KEY,
                    "",
                )
                or ""
            )
        )
        generation = 1 + sum(1 for entry in entries if entry.spec.archetype == archetype)
        route_key, route_focus, route_anchor_job_id = _select_distinct_route(
            entries,
            archetype=archetype,
            generation=generation,
            target_symbol=target_symbol,
            active_file=active_file,
            forbidden_delta_signatures=active_delta_signatures,
            universal_obstruction=universal_obstruction,
        )
        route_anchor_entry = next(
            (entry for entry in entries if entry.spec.job_id == route_anchor_job_id),
            None,
        )
        if route_anchor_job_id and route_anchor_entry is None:
            raise RuntimeError(f"missing research route anchor {route_anchor_job_id!r}")
        spec = _job_spec(
            service,
            archetype=archetype,
            generation=generation,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=attempt_count,
            route_key=route_key,
            route_focus=route_focus,
            route_anchor_job_id=route_anchor_job_id,
            route_anchor_entry=route_anchor_entry,
            route_context=research_route_context.build_route_context(
                entries,
                target_symbol=target_symbol,
                active_file=active_file,
                assignment_revision=assignment_revision,
            ),
            campaign_epoch_number=campaign_epoch_number,
            assignment_revision=assignment_revision,
            source_revision=source_revision,
            forbidden_delta_signatures=active_delta_signatures,
            universal_obstruction=universal_obstruction,
        )
        provider_now_epoch = _utc_now().timestamp()
        provider_retry_after = _later_provider_usage_limit(
            provider_retry_after,
            _active_campaign_provider_usage_limit(
                campaign_id=campaign_id,
                now_epoch=provider_now_epoch,
            ),
        )
        if provider_retry_after:
            # The foreground model thread can publish an account reset while
            # this parent heartbeat is doing route selection. Recheck at the
            # last safe point before spawning another provider process.
            break
        try:
            service.propose(spec)
        except dispatch_runtime.MathematicalDeltaReservationConflict as conflict:
            entries = _reconcile_delta_reservation_winner(
                service,
                conflict,
                target_symbol=target_symbol,
                active_file=active_file,
            )
            terminal_process_survivors = {
                entry.spec.job_id
                for entry in entries
                if not _terminal_killed_process_released(entry, service=service)
            }
            active = [
                entry
                for entry in entries
                if not entry.is_terminal() or entry.spec.job_id in terminal_process_survivors
            ]
            continue
        provider_now_epoch = _utc_now().timestamp()
        provider_retry_after = _later_provider_usage_limit(
            provider_retry_after,
            _active_campaign_provider_usage_limit(
                campaign_id=campaign_id,
                now_epoch=provider_now_epoch,
            ),
        )
        if provider_retry_after:
            service.kill(
                spec.job_id,
                requester_job_id=campaign_id or service.root_job_id,
            )
            entries = service.entries()
            active = [
                entry
                for entry in entries
                if not entry.is_terminal()
                or not _terminal_killed_process_released(entry, service=service)
            ]
            break
        try:
            running = service.deploy_async(spec.job_id)
        except dispatch_runtime.DispatchLaunchAdmissionDeferred:
            # The provider-pause summary write won the same lock used for the
            # async launch reservation. Retire the still-proposed row so it
            # cannot occupy capacity on resume.
            service.kill(
                spec.job_id,
                requester_job_id=campaign_id or service.root_job_id,
            )
            entries = service.entries()
            active = [
                entry
                for entry in entries
                if not entry.is_terminal()
                or not _terminal_killed_process_released(entry, service=service)
            ]
            provider_retry_after = _later_provider_usage_limit(
                provider_retry_after,
                _active_campaign_provider_usage_limit(
                    campaign_id=campaign_id,
                    now_epoch=_utc_now().timestamp(),
                ),
            )
            provider_launch_deferred = True
            break
        entries.append(running)
        active.append(running)
        launched.append(spec.job_id)
        if relaxation is not None:
            semantic_cooldown_relaxations.append(relaxation)
        if archetype in retry_eligible:
            retried_archetypes.append(archetype)

    replacement_slots = max(0, target_count - len(active))
    if provider_retry_after or provider_launch_deferred:
        replacement_status = _defer_portfolio_replacement(
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=attempt_count,
            workers=capacity,
            requested_slots=replacement_slots,
            reason="provider_usage_limit",
            trigger_job_ids=replacement_trigger_job_ids,
            active_job_ids=[entry.spec.job_id for entry in active],
        )
        status = {
            "active": len(active),
            "active_jobs": [entry.spec.job_id for entry in active],
            "launched": launched,
            "consumed": consumed,
            "refill_deferred": True,
            "provider_unavailable": True,
            **replacement_status,
        }
        if provider_retry_after:
            status["provider_retry_after"] = provider_retry_after
        return status
    if replacement_slots:
        replacement_status = _defer_portfolio_replacement(
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=attempt_count,
            workers=capacity,
            requested_slots=replacement_slots,
            reason="route_temporarily_unavailable",
            trigger_job_ids=replacement_trigger_job_ids,
            active_job_ids=[entry.spec.job_id for entry in active],
        )
    else:
        _complete_portfolio_replacement(
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=attempt_count,
            workers=capacity,
            launched_job_ids=launched,
            active_job_ids=[entry.spec.job_id for entry in active],
        )
        replacement_status = {}

    if launched or consumed or retired:
        append_workflow_activity(
            "research-portfolio",
            "Maintained the relentless background research portfolio",
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            active_jobs=[entry.spec.job_id for entry in active],
            launched=launched,
            consumed=consumed,
            retired_stale=retired,
            retired_dominated=retired_dominated,
            failure_backoff_retries=retried_archetypes,
            semantic_lane_cooldowns=semantic_cooldowns,
            **(
                {"semantic_lane_cooldown_relaxations": semantic_cooldown_relaxations}
                if semantic_cooldown_relaxations
                else {}
            ),
        )
    status = {
        "active": len(active),
        "active_jobs": [entry.spec.job_id for entry in active],
        "launched": launched,
        "consumed": consumed,
        **replacement_status,
    }
    if universal_obstruction is not None:
        status["universal_obstruction_dominance"] = {
            "node_id": universal_obstruction.node_id,
            "name": universal_obstruction.name,
            "retired_jobs": retired_dominated,
            "suppressed_archetypes": ["empirical"],
            "replacement_lane_order": ["negation_probe", "decomposition", "deep_search"],
        }
    if blocked:
        status["failure_backoff"] = {
            archetype: {
                "consecutive_failures": max(
                    0,
                    int(record.get("consecutive_failures", 0) or 0),
                ),
                "delay_seconds": max(0, int(record.get("delay_seconds", 0) or 0)),
                "next_retry_at": str(record.get("next_retry_at", "") or ""),
                "last_failure_job_id": str(record.get("last_failure_job_id", "") or ""),
                "reason": str(record.get("last_failure_reason", "") or ""),
            }
            for archetype, record in sorted(blocked.items())
        }
    if semantic_cooldowns:
        status["semantic_lane_cooldowns"] = semantic_cooldowns
    if semantic_cooldown_relaxations:
        status["semantic_lane_cooldown_relaxations"] = semantic_cooldown_relaxations
    if retried_archetypes:
        status["failure_backoff_retries"] = retried_archetypes
    if epoch_refresh_killed:
        status["epoch_refresh_killed"] = epoch_refresh_killed
    if epoch_refresh_carried:
        status["epoch_refresh_carried"] = epoch_refresh_carried
    return status


def maintain_portfolio(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    attempt_count: int,
    workers: int,
    refill: bool = True,
) -> dict[str, Any]:
    """Reconcile and consume the parent-owned research portfolio.

    Serialize the whole lifecycle so a recurring parent heartbeat may overlap
    a foreground post-tool callback without double-consuming a deliverable or
    overfilling worker capacity. ``refill=False`` reserves newly freed actor
    capacity for synchronous foreground control-plane work.
    """
    with _MAINTENANCE_LOCK:
        shutdown_key = (
            str(workflow_state_root().resolve()),
            str(campaign_id or "campaign"),
        )
        if shutdown_key in _SHUTDOWN_CAMPAIGNS:
            return {
                "active": 0,
                "active_jobs": [],
                "launched": [],
                "consumed": [],
                "shutdown": True,
            }
        return _maintain_portfolio_once(
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            attempt_count=attempt_count,
            workers=workers,
            refill=refill,
        )


def _owned_nonce_bound_portfolio_entry(
    entry: LedgerEntry,
    *,
    campaign_id: str,
) -> bool:
    """Return whether one ledger row is an authorized portfolio launch.

    Process termination is fail-closed on both authoritative lineage and the
    redundant campaign payload. A modern launch nonce is required so a stale
    or legacy row can never be mistaken for the exact raced replacement.
    """
    normalized_campaign = str(campaign_id or "").strip()
    inputs = dict(entry.spec.inputs or {})
    return bool(
        normalized_campaign
        and entry.launch_nonce
        and is_ancestor(normalized_campaign, entry.spec.job_id)
        and str(inputs.get("campaign_id", "") or "").strip() == normalized_campaign
        and entry.spec.requester_role == "orchestrator"
        and entry.spec.archetype in _PORTFOLIO_ARCHETYPES
    )


def reserve_planner_actor_slot(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    workers: int,
) -> dict[str, Any]:
    """Release the minimum process-job capacity needed by a pending planner wave.

    Ordinary reservation stops *new* portfolio launches, but a plan route can
    arrive while every configured actor slot is already held by a long-running
    process worker. Reconcile completed results first, then preempt only the
    newest replaceable process job(s) needed to leave one slot. ``DispatchService``
    proves exact process exit and harvests successful or worker-checked incremental
    evidence before publishing a kill verdict. Foreground prover/control work is
    never addressed by this function.
    """
    capacity = max(0, int(workers or 0))
    result: dict[str, Any] = {
        "capacity": capacity,
        "active_before": [],
        "active_after": [],
        "requested": [],
        "released": [],
        "killed": [],
        "completed": [],
        "still_active": [],
        "slot_reserved": capacity == 0,
        "foreground_untouched": True,
    }
    if capacity == 0:
        return result

    normalized_campaign = str(campaign_id or "campaign")
    with _MAINTENANCE_LOCK:
        service = DispatchService(root_job_id=normalized_campaign)
        service.recover_completed_artifacts()
        service.reconcile()
        service.recover_completed_artifacts()

        def owned_preemptible_entry(entry: LedgerEntry) -> bool:
            """Return whether this campaign may safely retire one portfolio actor."""
            if not _owned_nonce_bound_portfolio_entry(
                entry,
                campaign_id=normalized_campaign,
            ):
                return False
            if entry.state == "deployed":
                # The shared ownership predicate proves this modern launch can
                # be fenced before it enters its backend.
                return True
            if entry.state == "running":
                return entry.process_identity().verifiable
            return False

        def active_entries() -> list[LedgerEntry]:
            entries = service.entries()
            terminal_process_survivors = {
                entry.spec.job_id
                for entry in entries
                if not _terminal_killed_process_released(entry, service=service)
            }
            return [
                entry
                for entry in entries
                if entry.state in {"deployed", "running"}
                or entry.spec.job_id in terminal_process_survivors
            ]

        active = active_entries()
        result["active_before"] = [entry.spec.job_id for entry in active]
        while len(active) >= capacity:
            replaceable = [entry for entry in active if owned_preemptible_entry(entry)]
            required_releases = len(active) - capacity + 1
            # Do not sacrifice useful portfolio work when protected actors
            # already make a planner slot impossible. Re-evaluate before every
            # kill because completion/cancellation races can change the count.
            if len(replaceable) < required_releases:
                break
            stale = [
                entry
                for entry in replaceable
                if not _job_matches_assignment(
                    entry,
                    target_symbol=target_symbol,
                    active_file=active_file,
                )
            ]
            pool = stale or replaceable
            deployed = [entry for entry in pool if entry.state == "deployed"]
            # Ledger order is creation order. Prefer an unstarted launch, then
            # the newest running worker so older accumulated research survives.
            candidate = (deployed or pool)[-1]
            job_id = candidate.spec.job_id
            result["requested"].append(job_id)
            try:
                outcome = service.kill(
                    job_id,
                    requester_job_id=normalized_campaign or service.root_job_id,
                )
            except Exception:
                result["still_active"].append(job_id)
                break
            service.recover_completed_artifacts()
            current = next(
                (entry for entry in service.entries() if entry.spec.job_id == job_id),
                None,
            )
            released = current is None or (
                current.is_terminal()
                and _terminal_killed_process_released(current, service=service)
            )
            if not released:
                result["still_active"].append(job_id)
                break
            result["released"].append(job_id)
            if bool(outcome.get("killed")) or (current is not None and current.state == "killed"):
                result["killed"].append(job_id)
            elif current is not None and current.state == "done":
                result["completed"].append(job_id)
            active = active_entries()

        result["active_after"] = [entry.spec.job_id for entry in active]
        result["slot_reserved"] = len(active) < capacity
        if result["released"]:
            with contextlib.suppress(Exception):
                append_workflow_activity(
                    "research-portfolio-planner-preemption",
                    "Released process-isolated research capacity for a pending planner route",
                    campaign_id=normalized_campaign,
                    target_symbol=target_symbol,
                    active_file=active_file,
                    **result,
                )
        return result


def rollback_replacement_launches(
    *,
    campaign_id: str,
    job_ids: Sequence[str],
) -> dict[str, list[str]]:
    """Retire replacements launched concurrently with a new plan reservation.

    Prove that each replacement's exact process identity has exited before
    publishing a terminal kill verdict. A terminal ledger row alone is not
    release evidence because legacy runs may have persisted ``killed`` before
    an exact exit proof. Completed workers remain available for normal finding
    harvest, and older research work is never preempted.
    """
    requested = list(dict.fromkeys(str(job_id or "").strip() for job_id in job_ids if job_id))
    if not requested:
        return {"requested": [], "released": [], "killed": [], "still_active": []}
    normalized_campaign = str(campaign_id or "campaign")
    with _MAINTENANCE_LOCK:
        service = DispatchService(root_job_id=normalized_campaign)
        service.recover_completed_artifacts()
        entries = {entry.spec.job_id: entry for entry in service.entries()}
        exit_confirmed: set[str] = set()
        killed: list[str] = []
        for job_id in requested:
            entry = entries.get(job_id)
            if entry is None:
                exit_confirmed.add(job_id)
                continue
            if not _owned_nonce_bound_portfolio_entry(
                entry,
                campaign_id=normalized_campaign,
            ):
                # Job IDs are observational input until the ledger proves the
                # exact current-campaign portfolio authority. Never signal a
                # foreign campaign, foreground prover, or legacy un-fenced
                # launch merely because a caller supplied its identifier.
                continue
            try:
                process_exited = dispatch_runtime._terminate_dispatch_process_and_wait(entry)
            except Exception:
                process_exited = False
            if not process_exited:
                # Keep the non-terminal row authoritative when retirement
                # cannot prove the exact PID/session boundary is gone. This
                # prevents the next portfolio tick from reusing live capacity.
                continue
            exit_confirmed.add(job_id)
            if entry.is_terminal():
                continue
            try:
                outcome = service.kill(
                    job_id,
                    requester_job_id=normalized_campaign or service.root_job_id,
                )
            except Exception:
                # Continue through the exact launch set. A failed retirement is
                # reported in ``still_active`` after authoritative reconciliation
                # instead of hiding releases already completed for sibling jobs.
                continue
            if outcome.get("killed") or str(outcome.get("state", "") or "") == "killed":
                killed.append(job_id)
        service.recover_completed_artifacts()
        reconciled = {entry.spec.job_id: entry for entry in service.entries()}
        released = [
            job_id
            for job_id in requested
            if job_id in exit_confirmed
            and (job_id not in reconciled or reconciled[job_id].is_terminal())
        ]
        still_active = [job_id for job_id in requested if job_id not in released]
        if released:
            # Activity is observational. Once the dispatch ledger proves that
            # capacity was released, a logging failure must not make the caller
            # retry and misclassify the successful rollback as still active.
            with contextlib.suppress(Exception):
                append_workflow_activity(
                    "research-portfolio-planner-reservation",
                    "Released replacement research launch(es) for a pending planner route",
                    campaign_id=normalized_campaign,
                    requested=requested,
                    released=released,
                    killed=killed,
                    still_active=still_active,
                )
        return {
            "requested": requested,
            "released": released,
            "killed": killed,
            "still_active": still_active,
        }


def refresh_portfolio_for_epoch(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    previous_epoch: int,
    new_epoch: int,
    reason: str,
    refresh_token: str = "",
    refill: bool = True,
) -> list[str]:
    """Retire old-epoch workers and honor any durable replacement obligation.

    Reconcile completed artifacts before killing anything: a worker that
    finished at the epoch boundary remains a deliverable, while genuinely
    open workers cannot carry the spent route portfolio into the fresh
    context. Unlike campaign shutdown, this normally leaves refill enabled. A
    caller that owes foreground construction may set ``refill=False`` to
    retire the spent epoch without launching replacement research. A vacancy
    recorded while planner capacity was reserved is otherwise fulfilled
    immediately after a successful refresh, rather than waiting for a later
    cadence tick.
    """
    normalized_campaign = str(campaign_id or "campaign")
    with _MAINTENANCE_LOCK:
        shutdown_key = (str(workflow_state_root().resolve()), normalized_campaign)
        if shutdown_key in _SHUTDOWN_CAMPAIGNS:
            return []
        service = DispatchService(root_job_id=normalized_campaign)
        durable_refresh = campaign_epoch.pending_worker_refresh(campaign_id=normalized_campaign)
        if (
            durable_refresh
            and refresh_token
            and str(durable_refresh.get("token", "") or "") != str(refresh_token)
        ):
            # A newer rollover superseded this caller. Leave its token for the
            # next maintenance pass instead of clearing the wrong generation.
            return []
        refresh = durable_refresh or {
            "pending": True,
            "token": str(refresh_token or ""),
            "previous_epoch": int(previous_epoch),
            "new_epoch": int(new_epoch),
            "reason": str(reason or ""),
            "target_symbol": str(target_symbol or ""),
            "active_file": str(active_file or ""),
        }
        killed, completed, carried = _reconcile_epoch_worker_refresh(
            service,
            campaign_id=normalized_campaign,
            refresh=refresh,
            complete_durable=bool(durable_refresh),
        )
        refill_status: dict[str, Any] = {}
        pending_intent = _pending_replacement_intent(
            read_json_file(workflow_state_root() / "summary.json"),
            campaign_id=normalized_campaign,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        if completed and pending_intent and refill:
            refill_status = _maintain_portfolio_once(
                campaign_id=normalized_campaign,
                target_symbol=target_symbol,
                active_file=active_file,
                attempt_count=max(0, int(pending_intent.get("attempt_count", 0) or 0)),
                workers=max(0, int(pending_intent.get("workers", 0) or 0)),
                refill=True,
            )
        refill_launched = list(refill_status.get("launched") or [])
        refill_pending = bool(refill_status.get("replacement_pending"))
        if refill_launched and refill_pending:
            disposition = (
                f"immediately launched {len(refill_launched)} distinct replacement route(s); "
                "the remaining durable vacancy stays pending"
            )
        elif refill_launched:
            disposition = (
                f"immediately launched {len(refill_launched)} distinct replacement route(s)"
            )
        elif pending_intent and not refill:
            disposition = "replacement research remains deferred until construction progress"
        elif pending_intent and refill_pending:
            disposition = "the durable replacement obligation remains pending"
        else:
            disposition = "the next maintenance tick may refill distinct routes"
        append_workflow_activity(
            "research-portfolio-epoch-refresh",
            (
                f"Retired {len(killed)} old-epoch research worker(s); "
                f"carried {len(carried)} fresh worker(s) across one rollover; "
                f"{disposition}"
            ),
            campaign_id=normalized_campaign,
            target_symbol=target_symbol,
            active_file=active_file,
            previous_epoch=previous_epoch,
            new_epoch=new_epoch,
            reason=reason,
            killed=killed,
            carried=carried,
            refresh_token=str(refresh.get("token", "") or ""),
            completed=completed,
            replacement_intent_id=str(pending_intent.get("intent_id", "") or ""),
            replacement_refill_launched=refill_launched,
            replacement_pending=refill_pending,
            refill_allowed=refill,
        )
        return killed


def _shutdown_portfolio_once(*, campaign_id: str, reason: str) -> list[str]:
    """Kill every open research worker for one exiting campaign."""
    service = DispatchService(root_job_id=campaign_id or "campaign")
    killed: list[str] = []
    for entry in service.open_jobs():
        try:
            outcome = service.kill(
                entry.spec.job_id,
                requester_job_id=campaign_id or service.root_job_id,
            )
        except (Exception, KeyboardInterrupt):
            # Exit reconciliation is best-effort per worker: one broken or
            # interrupted process tree must not strand every later worker.
            continue
        if outcome.get("killed") or outcome.get("state") == "killed":
            killed.append(entry.spec.job_id)
    if killed:
        append_workflow_activity(
            "research-portfolio-shutdown",
            f"Stopped {len(killed)} research worker(s): {reason}",
            campaign_id=campaign_id,
            reason=reason,
            killed=killed,
        )
    return killed


def shutdown_portfolio(*, campaign_id: str, reason: str) -> list[str]:
    """Atomically close the portfolio against late heartbeat refills."""
    normalized_campaign = str(campaign_id or "campaign")
    with _MAINTENANCE_LOCK:
        _SHUTDOWN_CAMPAIGNS.add((str(workflow_state_root().resolve()), normalized_campaign))
        return _shutdown_portfolio_once(
            campaign_id=normalized_campaign,
            reason=reason,
        )
