"""Persist relentless proving campaigns and roll fresh model-context epochs."""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.provider_availability import normalize_provider_retry_after
from leanflow_cli.workflows import research_semantic_identity
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file
from leanflow_cli.workflows.workflow_state import append_workflow_activity, read_workflow_activity
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

CAMPAIGN_HISTORY_CAP = 100
ROUTE_EPOCH_LIMIT = 4
ROUTE_NO_PROGRESS_ROLLOVER_REASON = "route-no-graph-progress"
SEMANTIC_PORTFOLIO_ROLLOVER_REASON = "semantic-route-portfolio-exhausted"
ROUTE_PORTFOLIO_ROLLOVER_REASON = "route-portfolio-exhausted"
NO_PROGRESS_ROLLOVER_REASONS = frozenset(
    {
        ROUTE_NO_PROGRESS_ROLLOVER_REASON,
        SEMANTIC_PORTFOLIO_ROLLOVER_REASON,
        ROUTE_PORTFOLIO_ROLLOVER_REASON,
    }
)
_EPOCH_CYCLES_STATE_KEY = "campaign_epoch_cycles"
PROVIDER_TURN_NONCE_STATE_KEY = "campaign_provider_turn_nonce"
EPOCH_ROUTES_STATE_KEY = "campaign_epoch_routes"
EPOCH_ROUTE_REFRESH_STATE_KEY = "campaign_epoch_route_refresh"
EPOCH_ROUTE_SELECTION_STATE_KEY = "campaign_epoch_route_selection"
INFLIGHT_ROUTE_STATE_KEY = "campaign_inflight_route"
EPOCH_WORKER_REFRESH_STATE_KEY = "campaign_epoch_worker_refresh"
EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY = "campaign_epoch_negation_refresh_retries"
PLANNER_CAPACITY_RESERVATION_STATE_KEY = "campaign_planner_capacity_reservation"
PLANNER_CAPACITY_RESERVATION_FIELD = "planner_capacity_reservation"
PLANNER_CAPACITY_RESERVATION_VERSION = 1
PLANNER_TERMINAL_OBSTACLE_STATE_KEY = "planner_terminal_obstacle"
PLANNER_TERMINAL_OBSTACLE_FIELD = "planner_terminal_obstacle"
INFLIGHT_ROUTE_FIELD = "inflight_route"
INFLIGHT_ROUTE_VERSION = 1
_CAMPAIGN_HYDRATED_PROCESS_KEY = "_campaign_hydrated_process_nonce"
_PROCESS_HYDRATION_NONCE = uuid.uuid4().hex
EPOCH_ROUTE_HISTORY_CAP = 16
SEMANTIC_ROUTE_LEGACY_BACKFILL_CAP = 64
SEMANTIC_ROUTE_HISTORY_FIELD = "no_progress_semantic_routes"
SEMANTIC_ROUTE_HISTORY_STATE_KEY = "campaign_no_progress_semantic_routes"
EPOCH_REFRESH_ALLOWED_ROUTES = ("decompose", "negate", "plan")
MECHANISM_LEDGER_VERSION = 1
MECHANISM_ROUTE_PROGRESS_POLICY_VERSION = 2
CONDITIONAL_HELPER_PROGRESS_POLICY_VERSION = 1
FINITE_BRANCH_PROGRESS_POLICY_VERSION = 3
RESUME_GRAPH_PROGRESS_POLICY_VERSION = 1
EPOCH_ROUTE_REPLAY_POLICY_VERSION = 1
NEGATION_PROMOTION_ROOT_REGISTRATION_OPEN_FIELD = "negation_promotion_root_registration_open"
PROVIDER_USAGE_LIMIT_PAUSE_FIELD = "provider_usage_limit_pause"
PROVIDER_USAGE_LIMIT_PAUSE_VERSION = 1
PROVIDER_USAGE_LIMIT_PAUSE_OWNER = "provider_usage_limit"


class CampaignRootProviderBlocked(RuntimeError):
    """Stop a provider turn whose immutable requested scope is not sealed."""


def _semantic_route_records(value: Any) -> list[dict[str, Any]]:
    """Return valid route records from untrusted persisted state."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(entry) for entry in value if isinstance(entry, Mapping)]


def _legacy_semantic_route_history(campaign: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Reconstruct recent no-progress semantics for pre-ledger campaigns."""
    records: list[dict[str, Any]] = []
    history = [
        dict(entry) for entry in (campaign.get("epoch_history") or []) if isinstance(entry, Mapping)
    ]
    for epoch in history[-2:]:
        records.extend(_semantic_route_records(epoch.get("route_portfolio")))
    records.extend(_semantic_route_records(campaign.get("epoch_routes")))
    return records[-SEMANTIC_ROUTE_LEGACY_BACKFILL_CAP:]


def _enabled_env(name: str) -> bool:
    """Return whether one LeanFlow feature flag is explicitly enabled."""
    raw = str(os.getenv(name, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _authoritative_root_registration_enabled() -> bool:
    """Return whether a new native campaign can promote terminal negations."""
    workflow_kind = str(os.getenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "") or "").strip().lower()
    return (
        workflow_kind in {"prove", "autoprove"}
        and _enabled_env("LEANFLOW_PLAN_STATE")
        and _enabled_env("LEANFLOW_NEGATION_PROBE")
    )


@dataclass(frozen=True)
class MechanismProgressResult:
    """Report one atomic mechanism-ledger and route-streak transaction."""

    progressed_node_ids: tuple[str, ...] = ()
    repeated_records: tuple[dict[str, Any], ...] = ()
    first_records: tuple[dict[str, Any], ...] = ()
    previous_streak: int = 0


@dataclass(frozen=True)
class ConditionalHelperProgressReconciliation:
    """Report one durable conditional-helper accounting reconciliation."""

    newly_deferred_node_ids: tuple[str, ...] = ()
    released_node_ids: tuple[str, ...] = ()
    removed_ledger_node_ids: tuple[str, ...] = ()
    previous_streak: int = 0
    repaired_streak: int = 0


@dataclass(frozen=True)
class FiniteBranchProgressReconciliation:
    """Report one legacy saturated finite-branch accounting repair."""

    false_reset_node_ids: tuple[str, ...] = ()
    removed_ledger_node_ids: tuple[str, ...] = ()
    retained_last_progress_node_ids: tuple[str, ...] = ()
    previous_streak: int = 0
    reconstructed_streak: int = 0
    repaired_streak: int = 0
    false_reset_predates_epoch_routes: bool = False
    rollover_required: bool = False
    changed: bool = False


@dataclass(frozen=True)
class ResumeGraphProgressReconciliation:
    """Report one repair of startup-restored truth miscounted as live progress."""

    recovered_node_ids: tuple[str, ...] = ()
    removed_last_progress_node_ids: tuple[str, ...] = ()
    retained_last_progress_node_ids: tuple[str, ...] = ()
    previous_streak: int = 0
    reconstructed_streak: int = 0
    repaired_streak: int = 0
    rollover_required: bool = False
    changed: bool = False


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _summary_path():
    return workflow_state_root() / "summary.json"


def _nonnegative_int(value: Any, default: int = 0) -> int:
    """Return a persisted counter as a non-negative integer."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _positive_int(value: Any, default: int) -> int:
    """Return a persisted limit as a positive integer."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _canonical_route_file(value: Any) -> str:
    """Return a stable comparison key for one persisted route file."""
    text = str(value or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(text))))


def _planner_terminal_obstacle(value: Any) -> dict[str, str]:
    """Return one valid exact-target terminal planner obstacle."""
    raw = value if isinstance(value, Mapping) else {}
    payload = {
        "target_symbol": str(raw.get("target_symbol", "") or "").strip(),
        "active_file": str(raw.get("active_file", "") or "").strip(),
        "target_signature_sha256": str(raw.get("target_signature_sha256", "") or "").strip(),
        "target_declaration_sha256": str(raw.get("target_declaration_sha256", "") or "").strip(),
        "outcome": str(raw.get("outcome", "") or "").strip(),
        "reason": str(raw.get("reason", "") or "").strip(),
    }
    if not all(
        (
            payload["target_symbol"],
            payload["active_file"],
            payload["target_signature_sha256"],
            payload["outcome"],
            payload["reason"],
        )
    ):
        return {}
    return payload


def _inflight_route_from_campaign(campaign: Mapping[str, Any]) -> dict[str, Any]:
    """Return one valid route selected but not yet observably completed."""
    raw = campaign.get(INFLIGHT_ROUTE_FIELD)
    if not isinstance(raw, Mapping) or not bool(raw.get("pending")):
        return {}
    campaign_id = str(campaign.get("campaign_id", "") or "").strip()
    epoch = _positive_int(campaign.get("epoch", 1), 1)
    token = str(raw.get("token", "") or "").strip()
    route = str(raw.get("route", "") or "").strip().lower()
    target_symbol = str(raw.get("target_symbol", "") or "").strip()
    active_file = str(raw.get("active_file", "") or "").strip()
    if (
        _nonnegative_int(raw.get("version", 0)) != INFLIGHT_ROUTE_VERSION
        or not campaign_id
        or str(raw.get("campaign_id", "") or "").strip() != campaign_id
        or _nonnegative_int(raw.get("epoch", 0)) != epoch
        or not token
        or not route
        or not target_symbol
        or not active_file
    ):
        return {}
    return {
        "version": INFLIGHT_ROUTE_VERSION,
        "pending": True,
        "token": token,
        "campaign_id": campaign_id,
        "epoch": epoch,
        "route": route,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "trigger": str(raw.get("trigger", "") or ""),
        "reason": str(raw.get("reason", "") or ""),
        "source": str(raw.get("source", "") or ""),
        "target": (dict(raw.get("target") or {}) if isinstance(raw.get("target"), Mapping) else {}),
        "selected_at": str(raw.get("selected_at", "") or ""),
    }


def _inflight_route_matches_scope(
    route: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether one pending route belongs to the exact queue assignment."""
    return str(route.get("target_symbol", "") or "").strip() == str(
        target_symbol or ""
    ).strip() and _canonical_route_file(route.get("active_file", "")) == _canonical_route_file(
        active_file
    )


def _planner_capacity_reservation_from_campaign(
    campaign: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one valid pending planner-capacity reservation."""
    raw = campaign.get(PLANNER_CAPACITY_RESERVATION_FIELD)
    if not isinstance(raw, Mapping) or not bool(raw.get("pending")):
        return {}
    campaign_id = str(campaign.get("campaign_id", "") or "").strip()
    epoch = _positive_int(campaign.get("epoch", 1), 1)
    token = str(raw.get("token", "") or "").strip()
    target_symbol = str(raw.get("target_symbol", "") or "").strip()
    active_file = str(raw.get("active_file", "") or "").strip()
    if (
        _nonnegative_int(raw.get("version", 0)) != PLANNER_CAPACITY_RESERVATION_VERSION
        or not campaign_id
        or str(raw.get("campaign_id", "") or "").strip() != campaign_id
        or _nonnegative_int(raw.get("epoch", 0)) != epoch
        or str(raw.get("route", "") or "").strip().lower() != "plan"
        or not token
        or not target_symbol
        or not active_file
    ):
        return {}
    return {
        "version": PLANNER_CAPACITY_RESERVATION_VERSION,
        "pending": True,
        "token": token,
        "campaign_id": campaign_id,
        "epoch": epoch,
        "route": "plan",
        "target_symbol": target_symbol,
        "active_file": active_file,
        "reason": str(raw.get("reason", "") or ""),
        "requested_at": str(raw.get("requested_at", "") or ""),
    }


def _reservation_matches_scope(
    reservation: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether one planner reservation owns the exact assignment."""
    return str(reservation.get("target_symbol", "") or "").strip() == str(
        target_symbol or ""
    ).strip() and _canonical_route_file(
        reservation.get("active_file", "")
    ) == _canonical_route_file(
        active_file
    )


def _hydrate_planner_capacity_reservation(
    autonomy_state: dict[str, Any],
    campaign: Mapping[str, Any],
) -> dict[str, Any]:
    """Hydrate one crash-durable pending plan route into process state."""
    reservation = _planner_capacity_reservation_from_campaign(campaign)
    if not reservation:
        autonomy_state.pop(PLANNER_CAPACITY_RESERVATION_STATE_KEY, None)
        return {}
    autonomy_state[PLANNER_CAPACITY_RESERVATION_STATE_KEY] = dict(reservation)
    requested = autonomy_state.get("prover_requested_route")
    if not isinstance(requested, Mapping):
        autonomy_state["prover_requested_route"] = {
            "route": "plan",
            "target_symbol": reservation["target_symbol"],
            "active_file": reservation["active_file"],
            "reason": reservation["reason"] or "planner capacity reserved",
        }
    return reservation


def _pending_refresh_selection(refresh: Mapping[str, Any]) -> dict[str, Any]:
    """Return a valid unstarted route selection from one refresh record."""
    selection = refresh.get("pending_selection")
    if not bool(refresh.get("required")) or not isinstance(selection, Mapping):
        return {}
    token = str(refresh.get("token", "") or "")
    epoch = _positive_int(refresh.get("new_epoch", 0), 1)
    route = str(selection.get("route", "") or "").strip().lower()
    if (
        not token
        or route not in EPOCH_REFRESH_ALLOWED_ROUTES
        or str(selection.get("token", "") or "") != token
        or _positive_int(selection.get("epoch", 0), 1) != epoch
    ):
        return {}
    return {
        "token": token,
        "epoch": epoch,
        "route": route,
        "target_symbol": str(selection.get("target_symbol", "") or "").strip(),
        "active_file": str(selection.get("active_file", "") or "").strip(),
        "reason": str(selection.get("reason", "") or ""),
        "source": str(selection.get("source", "") or ""),
        "target": (
            dict(selection.get("target") or {})
            if isinstance(selection.get("target"), Mapping)
            else {}
        ),
        "selected_at": str(selection.get("selected_at", "") or ""),
    }


def _negation_refresh_retry_entries(value: Any) -> list[dict[str, Any]]:
    """Return valid durable inconclusive-negation retry markers."""
    return [
        dict(entry)
        for entry in (value if isinstance(value, list) else [])
        if isinstance(entry, Mapping) and str(entry.get("evidence_key", "") or "").strip()
    ]


def negation_refresh_retry_consumed(
    autonomy_state: Mapping[str, Any], *, evidence_key: str
) -> bool:
    """Return whether unchanged negation evidence already received its epoch retry."""
    normalized = str(evidence_key or "").strip()
    if not normalized:
        return True
    return any(
        str(entry.get("evidence_key", "") or "").strip() == normalized
        for entry in _negation_refresh_retry_entries(
            autonomy_state.get(EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY)
        )
    )


def _selection_matches_scope(
    selection: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether one pending selection owns the exact active scope."""
    return str(selection.get("target_symbol", "") or "").strip() == str(
        target_symbol or ""
    ).strip() and _canonical_route_file(selection.get("active_file", "")) == _canonical_route_file(
        active_file
    )


def _parse_activity_time(value: Any) -> datetime | None:
    """Return one persisted UTC timestamp, or None when malformed."""
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


def _route_replay_reconciliation_events() -> tuple[dict[str, Any], ...]:
    """Load bounded durable evidence needed by the legacy replay migration."""
    try:
        return tuple(
            read_workflow_activity(
                limit=5000,
                event_types={
                    "campaign-epoch-route-started",
                    "managed-conversation-failed",
                    "orchestrator-route",
                    "provider-retry-exhausted",
                    "runner-exit",
                },
            )
        )
    except Exception:
        return ()


def _event_details(event: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return one activity event's structured detail mapping."""
    details = event.get("details")
    return details if isinstance(details, Mapping) else {}


def _matching_scope_entry_event(
    events: Sequence[Mapping[str, Any]],
    *,
    route_entry: Mapping[str, Any],
    expected_routes_used: int,
) -> bool:
    """Return whether activity proves this old route was a scope-entry replay."""
    decided_at = _parse_activity_time(route_entry.get("decided_at"))
    if decided_at is None:
        return False
    route = str(route_entry.get("route", "") or "").strip().lower()
    target_symbol = str(route_entry.get("target_symbol", "") or "").strip()
    active_file = _canonical_route_file(route_entry.get("active_file", ""))
    for event in events:
        if str(event.get("type", "") or "") != "orchestrator-route":
            continue
        event_at = _parse_activity_time(event.get("timestamp"))
        details = _event_details(event)
        if event_at is None or abs((event_at - decided_at).total_seconds()) > 2:
            continue
        if (
            str(details.get("trigger", "") or "").strip().lower() != "scope-entry"
            or str(details.get("route", "") or "").strip().lower() != route
            or str(details.get("target_symbol", "") or "").strip() != target_symbol
            or _canonical_route_file(details.get("active_file", "")) != active_file
        ):
            continue
        recorded_routes_used = _nonnegative_int(details.get("routes_used", 0))
        if recorded_routes_used and recorded_routes_used != expected_routes_used:
            continue
        return True
    return False


def _provider_pause_between(
    events: Sequence[Mapping[str, Any]],
    *,
    after: datetime,
    before: datetime,
) -> bool:
    """Return whether activity proves a provider pause between two routes."""
    failures_by_run: set[str] = set()
    for event in events:
        event_at = _parse_activity_time(event.get("timestamp"))
        event_type = str(event.get("type", "") or "")
        run_id = str(event.get("run_id", "") or "")
        if event_at is None or not (after < event_at < before) or not run_id:
            continue
        if event_type in {"managed-conversation-failed", "provider-retry-exhausted"}:
            failures_by_run.add(run_id)
    if not failures_by_run:
        return False
    for event in events:
        if str(event.get("type", "") or "") != "runner-exit":
            continue
        event_at = _parse_activity_time(event.get("timestamp"))
        run_id = str(event.get("run_id", "") or "")
        details = _event_details(event)
        reason = " ".join(
            (
                str(details.get("reason", "") or ""),
                str(event.get("message", "") or ""),
            )
        ).lower()
        if (
            event_at is not None
            and after < event_at < before
            and run_id in failures_by_run
            and _nonnegative_int(details.get("exit_code", 0)) == 2
            and any(marker in reason for marker in ("provider", "api failure", "infrastructure"))
        ):
            return True
    return False


def _same_route_assignment(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Return whether two route records have the same action and exact scope."""
    return (
        str(left.get("route", "") or "").strip().lower()
        == str(right.get("route", "") or "").strip().lower()
        and str(left.get("target_symbol", "") or "").strip()
        == str(right.get("target_symbol", "") or "").strip()
        and _canonical_route_file(left.get("active_file", ""))
        == _canonical_route_file(right.get("active_file", ""))
    )


def _reconcile_legacy_epoch_route_replays(
    campaign: dict[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Remove only old scope-entry routes proven to replay after provider pause."""
    if (
        _nonnegative_int(campaign.get("epoch_route_replay_policy_version", 0))
        >= EPOCH_ROUTE_REPLAY_POLICY_VERSION
    ):
        return {}
    campaign["epoch_route_replay_policy_version"] = EPOCH_ROUTE_REPLAY_POLICY_VERSION
    refresh = campaign.get("epoch_route_refresh")
    routes = [
        dict(entry) for entry in (campaign.get("epoch_routes") or []) if isinstance(entry, Mapping)
    ]
    if (
        not isinstance(refresh, Mapping)
        or len(routes) < 2
        or len(routes) >= EPOCH_ROUTE_HISTORY_CAP
        or _nonnegative_int(campaign.get("no_progress_route_streak", 0)) != len(routes)
        or _positive_int(refresh.get("new_epoch", 0), 1)
        != _positive_int(campaign.get("epoch", 1), 1)
    ):
        return {}
    requested_at = _parse_activity_time(refresh.get("requested_at"))
    first_at = _parse_activity_time(routes[0].get("decided_at"))
    started_at = _parse_activity_time(refresh.get("started_at"))
    if requested_at is None or first_at is None or first_at < requested_at:
        return {}
    if started_at is not None and first_at >= started_at:
        return {}
    first_trigger = str(routes[0].get("trigger", "") or "").strip().lower()
    selected_route = str(refresh.get("selected_route", "") or "").strip().lower()
    if first_trigger not in {"", "scope-entry"} or (
        selected_route and selected_route != str(routes[0].get("route", "") or "").strip().lower()
    ):
        return {}
    last_progress = campaign.get("last_verified_graph_progress")
    if isinstance(last_progress, Mapping):
        progressed_at = _parse_activity_time(last_progress.get("recorded_at"))
        if progressed_at is not None and progressed_at >= requested_at:
            return {}

    kept = [routes[0]]
    removed: list[dict[str, Any]] = []
    previous_attempt_at = first_at
    prefix_open = True
    for index, entry in enumerate(routes[1:], start=1):
        entry_at = _parse_activity_time(entry.get("decided_at"))
        within_refresh = entry_at is not None and (started_at is None or entry_at < started_at)
        if (
            prefix_open
            and within_refresh
            and entry_at is not None
            and _same_route_assignment(routes[0], entry)
            and _matching_scope_entry_event(
                events,
                route_entry=entry,
                expected_routes_used=index + 1,
            )
            and _provider_pause_between(
                events,
                after=previous_attempt_at,
                before=entry_at,
            )
        ):
            removed.append(entry)
            previous_attempt_at = entry_at
            continue
        prefix_open = False
        kept.append(entry)
    if not removed:
        return {}

    previous_streak = _nonnegative_int(campaign.get("no_progress_route_streak", 0))
    repaired_streak = max(0, previous_streak - len(removed))
    reconciled_at = _now_iso()
    campaign["epoch_routes"] = kept
    campaign["no_progress_route_streak"] = repaired_streak
    campaign["last_route_decision"] = dict(kept[-1])
    reconciliation = {
        "version": EPOCH_ROUTE_REPLAY_POLICY_VERSION,
        "reason": "legacy-infrastructure-scope-entry-replay",
        "previous_streak": previous_streak,
        "repaired_streak": repaired_streak,
        "removed_decisions": [str(entry.get("decided_at", "") or "") for entry in removed],
        "refresh_token": str(refresh.get("token", "") or ""),
        "reconciled_at": reconciled_at,
    }
    campaign["epoch_route_replay_reconciliation"] = reconciliation
    campaign["updated_at"] = reconciled_at
    return reconciliation


def _last_progress_is_repeated_mechanism(campaign: Mapping[str, Any]) -> bool:
    """Return whether legacy state last reset for an already-seen mechanism."""
    last_progress = campaign.get("last_verified_graph_progress")
    if (
        not isinstance(last_progress, Mapping)
        or str(last_progress.get("accounting", "") or "") != "parent-scoped-proof-mechanism"
    ):
        return False
    node_ids = {
        str(value or "").strip()
        for value in (last_progress.get("node_ids") or [])
        if str(value or "").strip()
    }
    if not node_ids:
        return False
    ledger = campaign.get("verified_mechanisms")
    raw_entries = ledger.get("entries") if isinstance(ledger, Mapping) else None
    entries = raw_entries.values() if isinstance(raw_entries, Mapping) else ()
    repeated: set[str] = set()
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            continue
        first_node_id = str(raw_entry.get("first_node_id", "") or "").strip()
        for value in raw_entry.get("seen_node_ids") or []:
            node_id = str(value or "").strip()
            if node_id and node_id != first_node_id:
                repeated.add(node_id)
    return node_ids.issubset(repeated)


def _reconcile_mechanism_progress_policy(campaign: dict[str, Any]) -> None:
    """Repair a legacy route-streak reset caused only by mechanism repetition."""
    if (
        _nonnegative_int(campaign.get("mechanism_route_progress_policy_version", 0))
        >= MECHANISM_ROUTE_PROGRESS_POLICY_VERSION
    ):
        return
    campaign["mechanism_route_progress_policy_version"] = MECHANISM_ROUTE_PROGRESS_POLICY_VERSION
    if not _last_progress_is_repeated_mechanism(campaign):
        return
    routes = [
        dict(entry) for entry in (campaign.get("epoch_routes") or []) if isinstance(entry, Mapping)
    ]
    previous = _nonnegative_int(campaign.get("no_progress_route_streak", 0))
    repaired = max(previous, len(routes))
    campaign["no_progress_route_streak"] = repaired
    campaign["mechanism_progress_policy_reconciliation"] = {
        "version": MECHANISM_ROUTE_PROGRESS_POLICY_VERSION,
        "reason": "legacy-repeated-mechanism-reset-ignored",
        "previous_streak": previous,
        "repaired_streak": repaired,
        "reconciled_at": _now_iso(),
    }


def ensure_campaign(
    autonomy_state: dict[str, Any],
    *,
    force_reload: bool = False,
) -> dict[str, Any]:
    """Return and persist the current campaign identity and epoch.

    ``force_reload`` bypasses the process-hydrated fast path after an in-process
    durability repair, ensuring the same runner immediately consumes repaired
    counters instead of waiting for a process restart.
    """
    cached_id = str(autonomy_state.get("campaign_id", "") or "")
    summary_snapshot: dict[str, Any] | None = (
        read_json_file(_summary_path()) if force_reload else None
    )
    if (
        not force_reload
        and cached_id
        and autonomy_state.get(_CAMPAIGN_HYDRATED_PROCESS_KEY) != (_PROCESS_HYDRATION_NONCE)
    ):
        # Checkpoint restoration may pre-populate campaign_id before this new
        # process has read the newer durable summary. Reconcile once per process
        # so a planner reservation written after the checkpoint cannot disappear
        # through the historical cached-id fast path.
        candidate = read_json_file(_summary_path())
        durable = candidate.get("campaign")
        if isinstance(durable, Mapping) and str(durable.get("campaign_id", "") or "") == cached_id:
            summary_snapshot = candidate
        else:
            autonomy_state[_CAMPAIGN_HYDRATED_PROCESS_KEY] = _PROCESS_HYDRATION_NONCE
    if cached_id and summary_snapshot is None:
        return {
            "campaign_id": cached_id,
            "epoch": int(autonomy_state.get("campaign_epoch", 1) or 1),
            "status": str(autonomy_state.get("campaign_status", "running") or "running"),
            "epoch_cycles": _nonnegative_int(autonomy_state.get(_EPOCH_CYCLES_STATE_KEY, 0)),
            "provider_turn_nonce": _nonnegative_int(
                autonomy_state.get(PROVIDER_TURN_NONCE_STATE_KEY, 0)
            ),
            "no_progress_route_streak": _nonnegative_int(
                autonomy_state.get("orchestrator_routes_used", 0)
            ),
        }

    if summary_snapshot is None:
        summary_snapshot = read_json_file(_summary_path())
    snapshot_campaign = summary_snapshot.get("campaign")
    needs_route_replay_reconciliation = (
        isinstance(snapshot_campaign, Mapping)
        and bool(snapshot_campaign.get("campaign_id"))
        and _nonnegative_int(snapshot_campaign.get("epoch_route_replay_policy_version", 0))
        < EPOCH_ROUTE_REPLAY_POLICY_VERSION
    )
    replay_events = (
        _route_replay_reconciliation_events() if needs_route_replay_reconciliation else ()
    )
    replay_reconciliation: dict[str, Any] = {}
    provider_pause_reconciliation: dict[str, Any] = {}
    now_epoch = time.time()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        existing = dict(summary.get("campaign") or {})
        campaign_id = str(existing.get("campaign_id", "") or "")
        if not campaign_id:
            run_id = str(os.getenv("LEANFLOW_WORKFLOW_RUN_ID", "") or "").strip()
            campaign_id = run_id or f"campaign-{uuid.uuid4().hex[:12]}"
            existing = {
                "campaign_id": campaign_id,
                "epoch": 1,
                "status": "running",
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                "epoch_history": [],
                "epoch_cycles": _nonnegative_int(autonomy_state.get(_EPOCH_CYCLES_STATE_KEY, 0)),
                "provider_turn_nonce": _nonnegative_int(
                    autonomy_state.get(PROVIDER_TURN_NONCE_STATE_KEY, 0)
                ),
                "no_progress_route_streak": _nonnegative_int(
                    autonomy_state.get("orchestrator_routes_used", 0)
                ),
                "no_progress_route_limit": ROUTE_EPOCH_LIMIT,
                SEMANTIC_ROUTE_HISTORY_FIELD: _semantic_route_records(
                    autonomy_state.get(SEMANTIC_ROUTE_HISTORY_STATE_KEY)
                ),
                EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY: [],
                "mechanism_route_progress_policy_version": (
                    MECHANISM_ROUTE_PROGRESS_POLICY_VERSION
                ),
                "epoch_route_replay_policy_version": EPOCH_ROUTE_REPLAY_POLICY_VERSION,
                "finite_branch_progress_policy_version": (FINITE_BRANCH_PROGRESS_POLICY_VERSION),
                "resume_graph_progress_policy_version": (RESUME_GRAPH_PROGRESS_POLICY_VERSION),
            }
            if _authoritative_root_registration_enabled():
                # Creation and the open handshake are one summary write. A
                # crash can therefore leave either a legacy/no-authority
                # campaign or a provider-blocking fresh campaign, never a
                # fresh authoritative campaign whose scope origin is unknown.
                existing[NEGATION_PROMOTION_ROOT_REGISTRATION_OPEN_FIELD] = True
        else:
            _reconcile_mechanism_progress_policy(existing)
            replay_reconciliation.update(
                _reconcile_legacy_epoch_route_replays(existing, replay_events)
            )
            # A fresh runner normally resumes an operational pause. A provider
            # usage-limit pause is the exception: its exact reset epoch owns
            # provider and portfolio admission until it expires.
            status = str(existing.get("status", "") or "")
            raw_provider_pause = existing.get(PROVIDER_USAGE_LIMIT_PAUSE_FIELD)
            provider_pause = normalize_provider_retry_after(
                raw_provider_pause,
                now_epoch=now_epoch,
            )
            if status in {"verified", "disproved"}:
                existing.pop(PROVIDER_USAGE_LIMIT_PAUSE_FIELD, None)
            elif raw_provider_pause is not None and not provider_pause:
                existing["status"] = "paused_infrastructure"
                existing["status_reason"] = (
                    "provider usage-limit pause metadata is malformed; manual resume "
                    "reconciliation is required"
                )
                provider_pause_reconciliation.update(
                    {"active": True, "malformed": True, "metadata": {}}
                )
            elif provider_pause and now_epoch < float(provider_pause["unavailable_until_epoch"]):
                existing["status"] = "paused_infrastructure"
                existing["status_reason"] = (
                    "provider usage limit active until epoch "
                    f"{provider_pause['unavailable_until_epoch']}"
                )
                provider_pause_reconciliation.update(
                    {"active": True, "malformed": False, "metadata": provider_pause}
                )
            else:
                if provider_pause:
                    prior_status = (
                        str(raw_provider_pause.get("prior_campaign_status", "") or "")
                        if isinstance(raw_provider_pause, Mapping)
                        else ""
                    )
                    prior_reason = (
                        str(raw_provider_pause.get("prior_campaign_status_reason", "") or "")
                        if isinstance(raw_provider_pause, Mapping)
                        else ""
                    )
                    existing.pop(PROVIDER_USAGE_LIMIT_PAUSE_FIELD, None)
                    provider_pause_reconciliation.update(
                        {
                            "recovered": True,
                            "metadata": provider_pause,
                            "restored_pause": prior_status == "paused_infrastructure",
                        }
                    )
                    if prior_status == "paused_infrastructure":
                        existing["status"] = prior_status
                        if prior_reason:
                            existing["status_reason"] = prior_reason
                        else:
                            existing.pop("status_reason", None)
                    else:
                        existing["status"] = "running"
                        existing.pop("status_reason", None)
                else:
                    existing["status"] = "running"
                    existing.pop("status_reason", None)
            existing["updated_at"] = _now_iso()
            existing.setdefault(
                "epoch_cycles",
                _nonnegative_int(autonomy_state.get(_EPOCH_CYCLES_STATE_KEY, 0)),
            )
            # Do not backfill a missing durable nonce while resuming. Presence
            # of this key is part of the fresh-campaign root-authority origin
            # proof; synthesizing it here could launder a legacy or partially
            # written campaign into terminal-disproof authority. Marker-absent
            # legacy campaigns may create their first nonce atomically when a
            # provider turn is actually reserved, without gaining a marker.
            existing.setdefault(
                "no_progress_route_streak",
                _nonnegative_int(autonomy_state.get("orchestrator_routes_used", 0)),
            )
            existing.setdefault("no_progress_route_limit", ROUTE_EPOCH_LIMIT)
            existing.setdefault(
                SEMANTIC_ROUTE_HISTORY_FIELD,
                _legacy_semantic_route_history(existing),
            )
            existing.setdefault(EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY, [])
            if (
                PLANNER_CAPACITY_RESERVATION_FIELD in existing
                and not _planner_capacity_reservation_from_campaign(existing)
            ):
                # A reservation belongs to one exact campaign epoch. Retire a
                # stale or malformed row during the same startup transaction
                # that resumes the campaign, before portfolio maintenance.
                existing.pop(PLANNER_CAPACITY_RESERVATION_FIELD, None)
            if INFLIGHT_ROUTE_FIELD in existing and not _inflight_route_from_campaign(existing):
                # A pending route is useful only for its exact campaign epoch.
                # Malformed or cross-epoch work must never be replayed.
                existing.pop(INFLIGHT_ROUTE_FIELD, None)
        summary["campaign"] = existing
        return existing

    campaign = update_json_file(_summary_path(), mutate)
    if replay_reconciliation:
        try:
            append_workflow_activity(
                "campaign-route-replay-reconciled",
                "Removed legacy scope-entry route replay after a provider pause",
                campaign_id=str(campaign.get("campaign_id", "") or ""),
                epoch=int(campaign.get("epoch", 1) or 1),
                **replay_reconciliation,
            )
        except Exception:
            pass
    if provider_pause_reconciliation.get("recovered"):
        try:
            append_workflow_activity(
                "provider-usage-limit-recovered",
                (
                    "Provider usage-limit reset elapsed; an unrelated infrastructure "
                    "pause remains active"
                    if provider_pause_reconciliation.get("restored_pause")
                    else "Provider usage-limit reset elapsed; resumed campaign admission"
                ),
                campaign_id=str(campaign.get("campaign_id", "") or ""),
                **dict(provider_pause_reconciliation.get("metadata") or {}),
            )
        except Exception:
            pass
    autonomy_state["campaign_id"] = str(campaign.get("campaign_id", ""))
    autonomy_state[_CAMPAIGN_HYDRATED_PROCESS_KEY] = _PROCESS_HYDRATION_NONCE
    autonomy_state["campaign_epoch"] = int(campaign.get("epoch", 1) or 1)
    autonomy_state["campaign_status"] = str(campaign.get("status", "running") or "running")
    active_provider_pause = normalize_provider_retry_after(
        campaign.get(PROVIDER_USAGE_LIMIT_PAUSE_FIELD),
        now_epoch=now_epoch,
    )
    if active_provider_pause and str(campaign.get("status", "") or "") == ("paused_infrastructure"):
        autonomy_state.update(
            {
                "operational_pause": "paused_infrastructure",
                "infrastructure_pause_reason": str(
                    campaign.get("status_reason", "") or "provider usage limit remains active"
                ),
                "provider_retry_after": active_provider_pause,
                "provider_pause_owner": PROVIDER_USAGE_LIMIT_PAUSE_OWNER,
            }
        )
    elif provider_pause_reconciliation.get("active") and provider_pause_reconciliation.get(
        "malformed"
    ):
        autonomy_state.update(
            {
                "operational_pause": "paused_infrastructure",
                "infrastructure_pause_reason": str(
                    campaign.get("status_reason", "")
                    or "provider usage-limit pause metadata is malformed"
                ),
                "provider_retry_after": {},
                "provider_pause_owner": PROVIDER_USAGE_LIMIT_PAUSE_OWNER,
            }
        )
    elif provider_pause_reconciliation.get("restored_pause"):
        autonomy_state.update(
            {
                "operational_pause": "paused_infrastructure",
                "infrastructure_pause_reason": str(
                    campaign.get("status_reason", "")
                    or "an unrelated infrastructure pause remains active"
                ),
            }
        )
        autonomy_state.pop("provider_retry_after", None)
        autonomy_state.pop("provider_pause_owner", None)
    elif autonomy_state.get("provider_pause_owner") == PROVIDER_USAGE_LIMIT_PAUSE_OWNER:
        autonomy_state.pop("operational_pause", None)
        autonomy_state.pop("infrastructure_pause_reason", None)
        autonomy_state.pop("provider_retry_after", None)
        autonomy_state.pop("provider_pause_owner", None)
    autonomy_state[_EPOCH_CYCLES_STATE_KEY] = _nonnegative_int(campaign.get("epoch_cycles", 0))
    autonomy_state[PROVIDER_TURN_NONCE_STATE_KEY] = _nonnegative_int(
        campaign.get("provider_turn_nonce", 0)
    )
    route_streak = _nonnegative_int(campaign.get("no_progress_route_streak", 0))
    route_limit = _positive_int(
        campaign.get("no_progress_route_limit", ROUTE_EPOCH_LIMIT), ROUTE_EPOCH_LIMIT
    )
    autonomy_state["orchestrator_routes_used"] = route_streak
    raw_epoch_routes = campaign.get("epoch_routes")
    autonomy_state[EPOCH_ROUTES_STATE_KEY] = [
        dict(entry)
        for entry in (raw_epoch_routes if isinstance(raw_epoch_routes, list) else [])
        if isinstance(entry, Mapping)
    ][-EPOCH_ROUTE_HISTORY_CAP:]
    autonomy_state[SEMANTIC_ROUTE_HISTORY_STATE_KEY] = _semantic_route_records(
        campaign.get(SEMANTIC_ROUTE_HISTORY_FIELD)
    )
    autonomy_state[EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY] = _negation_refresh_retry_entries(
        campaign.get(EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY)
    )
    raw_refresh = campaign.get("epoch_route_refresh")
    if isinstance(raw_refresh, Mapping) and bool(raw_refresh.get("required")):
        autonomy_state[EPOCH_ROUTE_REFRESH_STATE_KEY] = dict(raw_refresh)
        pending_selection = _pending_refresh_selection(raw_refresh)
        if pending_selection:
            autonomy_state[EPOCH_ROUTE_SELECTION_STATE_KEY] = pending_selection
        else:
            autonomy_state.pop(EPOCH_ROUTE_SELECTION_STATE_KEY, None)
    else:
        autonomy_state.pop(EPOCH_ROUTE_REFRESH_STATE_KEY, None)
        autonomy_state.pop(EPOCH_ROUTE_SELECTION_STATE_KEY, None)
    raw_worker_refresh = campaign.get("epoch_worker_refresh")
    if isinstance(raw_worker_refresh, Mapping) and bool(raw_worker_refresh.get("pending")):
        autonomy_state[EPOCH_WORKER_REFRESH_STATE_KEY] = dict(raw_worker_refresh)
    else:
        autonomy_state.pop(EPOCH_WORKER_REFRESH_STATE_KEY, None)
    inflight_route = _inflight_route_from_campaign(campaign)
    if inflight_route:
        autonomy_state[INFLIGHT_ROUTE_STATE_KEY] = inflight_route
    else:
        autonomy_state.pop(INFLIGHT_ROUTE_STATE_KEY, None)
    planner_obstacle = _planner_terminal_obstacle(campaign.get(PLANNER_TERMINAL_OBSTACLE_FIELD))
    if planner_obstacle:
        autonomy_state[PLANNER_TERMINAL_OBSTACLE_STATE_KEY] = planner_obstacle
    else:
        autonomy_state.pop(PLANNER_TERMINAL_OBSTACLE_STATE_KEY, None)
    _hydrate_planner_capacity_reservation(autonomy_state, campaign)
    if route_streak >= route_limit:
        request_rollover(autonomy_state, ROUTE_NO_PROGRESS_ROLLOVER_REASON)
    return dict(campaign)


def rehydrate_campaign(autonomy_state: dict[str, Any]) -> dict[str, Any]:
    """Force one same-process reload from the durable campaign summary."""
    return ensure_campaign(autonomy_state, force_reload=True)


def record_planner_terminal_obstacle(
    autonomy_state: dict[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, str]:
    """Persist an exact-target planner cooldown across process restarts."""
    obstacle = _planner_terminal_obstacle(payload)
    if not obstacle:
        raise ValueError("planner terminal obstacle requires complete exact-target evidence")
    campaign = ensure_campaign(autonomy_state)

    def mutate(summary: dict[str, Any]) -> dict[str, str]:
        current = dict(summary.get("campaign") or campaign)
        current[PLANNER_TERMINAL_OBSTACLE_FIELD] = obstacle
        current["updated_at"] = _now_iso()
        summary["campaign"] = current
        return obstacle

    persisted = dict(update_json_file(_summary_path(), mutate) or {})
    autonomy_state[PLANNER_TERMINAL_OBSTACLE_STATE_KEY] = persisted
    return persisted


def clear_planner_terminal_obstacle(
    autonomy_state: dict[str, Any],
    expected: Mapping[str, Any],
) -> bool:
    """Clear one exact persisted planner cooldown without racing a newer row."""
    obstacle = _planner_terminal_obstacle(expected)
    if not obstacle:
        return False
    campaign = ensure_campaign(autonomy_state)

    def mutate(summary: dict[str, Any]) -> bool:
        current = dict(summary.get("campaign") or campaign)
        persisted = _planner_terminal_obstacle(current.get(PLANNER_TERMINAL_OBSTACLE_FIELD))
        if persisted != obstacle:
            return False
        current.pop(PLANNER_TERMINAL_OBSTACLE_FIELD, None)
        current["updated_at"] = _now_iso()
        summary["campaign"] = current
        return True

    cleared = bool(update_json_file(_summary_path(), mutate))
    if cleared:
        local = _planner_terminal_obstacle(autonomy_state.get(PLANNER_TERMINAL_OBSTACLE_STATE_KEY))
        if local == obstacle:
            autonomy_state.pop(PLANNER_TERMINAL_OBSTACLE_STATE_KEY, None)
    return cleared


def reserve_planner_capacity(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    reason: str,
) -> dict[str, Any]:
    """Persist and hydrate one exact pending plan-capacity reservation."""
    normalized_target = str(target_symbol or "").strip()
    normalized_file = str(active_file or "").strip()
    if not normalized_target or not normalized_file:
        raise ValueError("planner capacity reservation requires an exact target and file")
    campaign = ensure_campaign(autonomy_state)
    campaign_id = str(campaign.get("campaign_id", "") or "").strip()
    epoch = _positive_int(campaign.get("epoch", 1), 1)
    requested_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        if (
            str(current.get("campaign_id", "") or "").strip() != campaign_id
            or _positive_int(current.get("epoch", 1), 1) != epoch
        ):
            raise RuntimeError("campaign changed while reserving planner capacity")
        existing = _planner_capacity_reservation_from_campaign(current)
        if existing and _reservation_matches_scope(
            existing,
            target_symbol=normalized_target,
            active_file=normalized_file,
        ):
            return existing
        reservation = {
            "version": PLANNER_CAPACITY_RESERVATION_VERSION,
            "pending": True,
            "token": uuid.uuid4().hex,
            "campaign_id": campaign_id,
            "epoch": epoch,
            "route": "plan",
            "target_symbol": normalized_target,
            "active_file": normalized_file,
            "reason": str(reason or "planner capacity reserved"),
            "requested_at": requested_at,
        }
        current[PLANNER_CAPACITY_RESERVATION_FIELD] = reservation
        current["updated_at"] = requested_at
        summary["campaign"] = current
        return reservation

    reservation = dict(update_json_file(_summary_path(), mutate) or {})
    return _hydrate_planner_capacity_reservation(
        autonomy_state,
        {
            **dict(campaign),
            PLANNER_CAPACITY_RESERVATION_FIELD: reservation,
        },
    )


def clear_planner_capacity_reservation(
    autonomy_state: dict[str, Any],
    *,
    reservation_token: str = "",
) -> bool:
    """Clear one token-matched durable planner reservation."""
    campaign = ensure_campaign(autonomy_state)
    expected_token = str(reservation_token or "").strip()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        raw = current.get(PLANNER_CAPACITY_RESERVATION_FIELD)
        existing = dict(raw) if isinstance(raw, Mapping) else {}
        token = str(existing.get("token", "") or "").strip()
        if not existing:
            return {
                "cleared": bool(expected_token),
                "token": expected_token,
                "already_absent": True,
            }
        if expected_token and token != expected_token:
            return {"cleared": False, "token": token}
        current.pop(PLANNER_CAPACITY_RESERVATION_FIELD, None)
        current["updated_at"] = _now_iso()
        summary["campaign"] = current
        return {"cleared": True, "token": token}

    result = dict(update_json_file(_summary_path(), mutate) or {})
    if not bool(result.get("cleared")):
        return False
    cleared_token = str(result.get("token", "") or "")
    local = autonomy_state.get(PLANNER_CAPACITY_RESERVATION_STATE_KEY)
    if not isinstance(local, Mapping) or str(local.get("token", "") or "") == cleared_token:
        autonomy_state.pop(PLANNER_CAPACITY_RESERVATION_STATE_KEY, None)
    requested = autonomy_state.get("prover_requested_route")
    requested_matches = bool(
        isinstance(requested, Mapping)
        and (
            str(requested.get("capacity_reservation_token", "") or "") == cleared_token
            or (
                isinstance(local, Mapping)
                and str(requested.get("route", "") or "").strip().lower() == "plan"
                and _reservation_matches_scope(
                    local,
                    target_symbol=str(requested.get("target_symbol", "") or ""),
                    active_file=str(requested.get("active_file", "") or ""),
                )
            )
        )
    )
    if requested_matches:
        autonomy_state.pop("prover_requested_route", None)
    return True


def reconcile_planner_capacity_reservation(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any]:
    """Keep only the pending planner reservation for the current assignment."""
    ensure_campaign(autonomy_state)
    raw = autonomy_state.get(PLANNER_CAPACITY_RESERVATION_STATE_KEY)
    reservation = dict(raw) if isinstance(raw, Mapping) else {}
    if not reservation:
        return {}
    if (
        str(reservation.get("campaign_id", "") or "")
        == str(autonomy_state.get("campaign_id", "") or "")
        and _nonnegative_int(reservation.get("epoch", 0))
        == _positive_int(autonomy_state.get("campaign_epoch", 1), 1)
        and _reservation_matches_scope(
            reservation,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    ):
        return reservation
    clear_planner_capacity_reservation(
        autonomy_state,
        reservation_token=str(reservation.get("token", "") or ""),
    )
    return {}


def reusable_epoch_route_selection(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any]:
    """Return the exact unstarted fresh-epoch route reserved for this scope."""
    refresh = autonomy_state.get(EPOCH_ROUTE_REFRESH_STATE_KEY)
    if not isinstance(refresh, Mapping):
        return {}
    selection = _pending_refresh_selection(refresh)
    if not selection or not _selection_matches_scope(
        selection,
        target_symbol=target_symbol,
        active_file=active_file,
    ):
        return {}
    return selection


def replay_sources_superseded_by_requested_route(
    *,
    requested_route: str,
    inflight_route: Mapping[str, Any] | None,
    epoch_selection: Mapping[str, Any] | None,
    authenticated_negate: bool = False,
) -> tuple[str, ...]:
    """Return stale replay sources displaced by one explicit prover request.

    An exact requested route is the newest foreground strategy decision. A
    different unfinished route must not silently replace it merely because its
    durable marker is older. Authenticated negation evidence remains stronger
    than strategy ordering, and a replay matching the request is ordinary
    crash recovery rather than stale work.
    """
    requested = str(requested_route or "").strip().lower()
    if requested not in EPOCH_REFRESH_ALLOWED_ROUTES:
        return ()
    superseded: list[str] = []
    for source, raw_route in (
        (INFLIGHT_ROUTE_STATE_KEY, inflight_route),
        (EPOCH_ROUTE_SELECTION_STATE_KEY, epoch_selection),
    ):
        route = (
            str(raw_route.get("route", "") or "").strip().lower()
            if isinstance(raw_route, Mapping)
            else ""
        )
        if not route or route == requested or (route == "negate" and authenticated_negate):
            continue
        superseded.append(source)
    return tuple(superseded)


def pending_inflight_route(autonomy_state: dict[str, Any]) -> dict[str, Any]:
    """Return the crash-durable route awaiting observable completion."""
    campaign = ensure_campaign(autonomy_state)
    local = autonomy_state.get(INFLIGHT_ROUTE_STATE_KEY)
    route = _inflight_route_from_campaign(
        {
            **campaign,
            INFLIGHT_ROUTE_FIELD: dict(local) if isinstance(local, Mapping) else {},
        }
    )
    if route:
        autonomy_state[INFLIGHT_ROUTE_STATE_KEY] = route
    else:
        autonomy_state.pop(INFLIGHT_ROUTE_STATE_KEY, None)
    return route


def complete_inflight_route(
    autonomy_state: dict[str, Any],
    *,
    token: str,
    outcome: str,
    dropped_reason: str = "",
) -> bool:
    """Retire one exact pending route after completion or stale-scope rejection."""
    expected_token = str(token or "").strip()
    if not expected_token:
        return False
    campaign = ensure_campaign(autonomy_state)
    completed_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        route = _inflight_route_from_campaign(current)
        if not route or str(route.get("token", "") or "") != expected_token:
            return {"completed": False}
        current.pop(INFLIGHT_ROUTE_FIELD, None)
        current["updated_at"] = completed_at
        summary["campaign"] = current
        return {"completed": True, "route": route}

    payload = dict(update_json_file(_summary_path(), mutate) or {})
    if not bool(payload.get("completed")):
        return False
    route = dict(payload.get("route") or {})
    local = autonomy_state.get(INFLIGHT_ROUTE_STATE_KEY)
    if not isinstance(local, Mapping) or str(local.get("token", "") or "") == expected_token:
        autonomy_state.pop(INFLIGHT_ROUTE_STATE_KEY, None)
    if dropped_reason:
        append_workflow_activity(
            "campaign-inflight-route-dropped",
            f"Dropped stale in-flight route {route.get('route', '')}",
            campaign_id=str(route.get("campaign_id", "") or ""),
            epoch=int(route.get("epoch", 1) or 1),
            route=str(route.get("route", "") or ""),
            target_symbol=str(route.get("target_symbol", "") or ""),
            active_file=str(route.get("active_file", "") or ""),
            reason=str(dropped_reason),
        )
    else:
        append_workflow_activity(
            "campaign-inflight-route-completed",
            f"Completed in-flight route {route.get('route', '')}",
            campaign_id=str(route.get("campaign_id", "") or ""),
            epoch=int(route.get("epoch", 1) or 1),
            route=str(route.get("route", "") or ""),
            target_symbol=str(route.get("target_symbol", "") or ""),
            active_file=str(route.get("active_file", "") or ""),
            outcome=str(outcome or "completed"),
        )
    return True


def reusable_inflight_route(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any]:
    """Return an unfinished route for this assignment and retire stale work."""
    route = pending_inflight_route(autonomy_state)
    if not route:
        return {}
    if _inflight_route_matches_scope(
        route,
        target_symbol=target_symbol,
        active_file=active_file,
    ):
        return route
    complete_inflight_route(
        autonomy_state,
        token=str(route.get("token", "") or ""),
        outcome="dropped",
        dropped_reason="assignment-mismatch",
    )
    return {}


def managed_cycle_count(autonomy_state: dict[str, Any]) -> int:
    """Return the durable number of managed prover turns in this epoch."""
    campaign = ensure_campaign(autonomy_state)
    cycles = max(
        _nonnegative_int(autonomy_state.get(_EPOCH_CYCLES_STATE_KEY, 0)),
        _nonnegative_int(campaign.get("epoch_cycles", 0)),
    )
    autonomy_state[_EPOCH_CYCLES_STATE_KEY] = cycles
    return cycles


def record_managed_cycle(autonomy_state: dict[str, Any]) -> int:
    """Reserve and persist the next managed prover turn in this epoch.

    Persist before the provider call so a process restart cannot erase an
    initiated turn and indefinitely defer the epoch's cycle boundary.
    """
    campaign = ensure_campaign(autonomy_state)
    local_cycles = _nonnegative_int(autonomy_state.get(_EPOCH_CYCLES_STATE_KEY, 0))
    recorded_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> int:
        current = dict(summary.get("campaign") or campaign)
        persisted_cycles = _nonnegative_int(current.get("epoch_cycles", 0))
        cycles = max(local_cycles, persisted_cycles) + 1
        current["epoch_cycles"] = cycles
        current["updated_at"] = recorded_at
        summary["campaign"] = current
        return cycles

    cycles = int(update_json_file(_summary_path(), mutate))
    autonomy_state[_EPOCH_CYCLES_STATE_KEY] = cycles
    return cycles


def reserve_provider_turn(autonomy_state: dict[str, Any]) -> dict[str, Any]:
    """Reserve one campaign-wide provider-turn identity before an API call.

    The nonce never resets at an epoch boundary and is committed before the
    provider request. Combined with campaign and epoch identity, it lets every
    kernel-rejection presentation from one turn deduplicate while a resumed or
    freshly rolled turn with the same local cycle number remains distinct.
    """
    campaign = ensure_campaign(autonomy_state)
    local_nonce = _nonnegative_int(autonomy_state.get(PROVIDER_TURN_NONCE_STATE_KEY, 0))
    reserved_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        # Validate the exact campaign snapshot while the summary write lock is
        # held. A separate preflight check would leave a gate-to-nonce race.
        from leanflow_cli.workflows import negation_promotion

        provider_allowed, blocked_reason = negation_promotion.campaign_root_provider_gate(current)
        if not provider_allowed:
            raise CampaignRootProviderBlocked(blocked_reason)
        persisted_nonce = _nonnegative_int(current.get("provider_turn_nonce", 0))
        nonce = max(local_nonce, persisted_nonce) + 1
        current["provider_turn_nonce"] = nonce
        current["updated_at"] = reserved_at
        summary["campaign"] = current
        return {
            "campaign_id": str(current.get("campaign_id", "") or ""),
            "epoch": int(current.get("epoch", 1) or 1),
            "nonce": nonce,
        }

    identity = dict(update_json_file(_summary_path(), mutate) or {})
    autonomy_state[PROVIDER_TURN_NONCE_STATE_KEY] = _nonnegative_int(identity.get("nonce", 0))
    return identity


def record_route_decision(
    autonomy_state: dict[str, Any],
    *,
    route: str,
    target_symbol: str = "",
    active_file: str = "",
    trigger: str = "",
    route_reason: str = "",
    route_source: str = "",
    route_target: Mapping[str, Any] | None = None,
    negation_refresh_evidence_key: str = "",
    reserve_inflight: bool = False,
    limit: int = ROUTE_EPOCH_LIMIT,
) -> int:
    """Persist one foreground route decision and request rollover at the limit.

    The counter is campaign-scoped rather than process- or theorem-scoped. It
    therefore survives a resumable process exit and only resets after
    kernel-gated graph progress or an actual epoch boundary. A scope-entry
    route selected for a pending epoch-refresh token is reserved atomically
    with this record. Replaying that exact token, scope, and route is
    idempotent until a managed turn successfully starts the route. An
    inconclusive-negation refresh marker is committed in the same write as
    its route selection so later epochs cannot reopen unchanged evidence.
    ``reserve_inflight`` also records the exact selected route in that atomic
    transaction, closing the crash window between charging and execution.
    """
    campaign = ensure_campaign(autonomy_state)
    route_limit = _positive_int(limit, ROUTE_EPOCH_LIMIT)
    local_streak = _nonnegative_int(autonomy_state.get("orchestrator_routes_used", 0))
    decided_at = _now_iso()
    reserve_exact_inflight = bool(
        reserve_inflight and str(target_symbol or "").strip() and str(active_file or "").strip()
    )
    inflight_token = uuid.uuid4().hex if reserve_exact_inflight else ""
    normalized_trigger = str(trigger or "").strip().lower()
    normalized_route = str(route or "unknown").strip().lower() or "unknown"
    normalized_negation_evidence = (
        str(negation_refresh_evidence_key or "").strip() if normalized_route == "negate" else ""
    )

    local_routes = [
        dict(entry)
        for entry in (autonomy_state.get(EPOCH_ROUTES_STATE_KEY) or [])
        if isinstance(entry, Mapping)
    ]
    local_semantic_routes = _semantic_route_records(
        autonomy_state.get(SEMANTIC_ROUTE_HISTORY_STATE_KEY)
    )
    normalized_target = dict(route_target or {})
    semantic_identity = research_semantic_identity.route_semantic_identity(
        route=normalized_route,
        target_symbol=target_symbol,
        active_file=active_file,
        reason=route_reason,
        target=normalized_target,
    )
    route_record: dict[str, Any] = {
        "route": normalized_route,
        "target_symbol": str(target_symbol or ""),
        "active_file": str(active_file or ""),
        "decided_at": decided_at,
        "semantic_route_key": semantic_identity.key,
        "semantic_route_family": semantic_identity.family,
        "semantic_target_hypothesis": semantic_identity.target_hypothesis,
    }
    if normalized_trigger:
        route_record["trigger"] = normalized_trigger
    if route_reason:
        route_record["reason"] = str(route_reason)[:2000]
    if route_source:
        route_record["source"] = str(route_source)[:200]
    if normalized_target:
        route_record["target"] = normalized_target
    if semantic_identity.proof_shapes:
        route_record["semantic_proof_shapes"] = list(semantic_identity.proof_shapes)
    semantic_record = {
        key: route_record[key]
        for key in (
            "route",
            "target_symbol",
            "active_file",
            "decided_at",
            "semantic_route_key",
            "semantic_route_family",
            "semantic_target_hypothesis",
            "semantic_proof_shapes",
        )
        if key in route_record
    }

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        existing_inflight = _inflight_route_from_campaign(current)
        if reserve_exact_inflight and existing_inflight:
            raise RuntimeError("cannot replace an unfinished orchestrator route")
        persisted_streak = _nonnegative_int(current.get("no_progress_route_streak", 0))
        persisted_routes = [
            dict(entry)
            for entry in (current.get("epoch_routes") or [])
            if isinstance(entry, Mapping)
        ]
        routes = persisted_routes if len(persisted_routes) >= len(local_routes) else local_routes
        if SEMANTIC_ROUTE_HISTORY_FIELD in current:
            persisted_semantic_routes = _semantic_route_records(
                current.get(SEMANTIC_ROUTE_HISTORY_FIELD)
            )
        else:
            persisted_semantic_routes = _legacy_semantic_route_history(current)
        semantic_routes = (
            persisted_semantic_routes
            if len(persisted_semantic_routes) >= len(local_semantic_routes)
            else local_semantic_routes
        )
        refresh = dict(current.get("epoch_route_refresh") or {})
        negation_retries = _negation_refresh_retry_entries(
            current.get(EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY)
        )
        pending_selection = _pending_refresh_selection(refresh)
        duplicate_refresh_selection = (
            normalized_trigger == "scope-entry"
            and bool(pending_selection)
            and str(pending_selection.get("route", "") or "") == normalized_route
            and _selection_matches_scope(
                pending_selection,
                target_symbol=target_symbol,
                active_file=active_file,
            )
        )
        if duplicate_refresh_selection:
            return {
                "streak": persisted_streak,
                "routes": routes,
                "semantic_routes": semantic_routes,
                "refresh": refresh,
                "negation_retries": negation_retries,
                "deduplicated": True,
            }

        streak = max(local_streak, persisted_streak) + 1
        routes = [*routes, route_record][-EPOCH_ROUTE_HISTORY_CAP:]
        existing_semantic_keys = {
            str(entry.get("semantic_route_key", "") or "").strip()
            for entry in semantic_routes
            if str(entry.get("semantic_route_key", "") or "").strip()
        }
        if (
            normalized_route in EPOCH_REFRESH_ALLOWED_ROUTES
            and semantic_identity.key not in existing_semantic_keys
        ):
            semantic_routes = [*semantic_routes, semantic_record]
        current["no_progress_route_streak"] = streak
        current["no_progress_route_limit"] = route_limit
        current["last_route_decision"] = route_record
        current["epoch_routes"] = routes
        current[SEMANTIC_ROUTE_HISTORY_FIELD] = semantic_routes
        if normalized_negation_evidence and not any(
            str(entry.get("evidence_key", "") or "").strip() == normalized_negation_evidence
            for entry in negation_retries
        ):
            negation_retries.append(
                {
                    "evidence_key": normalized_negation_evidence,
                    "target_symbol": str(target_symbol or "").strip(),
                    "active_file": str(active_file or "").strip(),
                    "epoch": _positive_int(current.get("epoch", 1), 1),
                    "recorded_at": decided_at,
                }
            )
        current[EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY] = negation_retries
        # Only executable strategy routes may own the fresh-epoch token.
        # Internal portfolio refresh is checkpointed as an in-flight control
        # action so it can request rollover and retire immediately without a
        # provider turn or an unstartable epoch-selection replay.
        reserved_refresh_selection = (
            normalized_route in EPOCH_REFRESH_ALLOWED_ROUTES
            and normalized_trigger == "scope-entry"
            and bool(refresh.get("required"))
            and str(refresh.get("token", "") or "")
            and _positive_int(refresh.get("new_epoch", 0), 1)
            == _positive_int(current.get("epoch", 1), 1)
        )
        if reserved_refresh_selection:
            refresh["pending_selection"] = {
                "token": str(refresh.get("token", "") or ""),
                "epoch": _positive_int(refresh.get("new_epoch", 0), 1),
                "route": normalized_route,
                "target_symbol": str(target_symbol or "").strip(),
                "active_file": str(active_file or "").strip(),
                "reason": str(route_reason or ""),
                "source": str(route_source or ""),
                "target": dict(route_target or {}),
                "selected_at": decided_at,
            }
            current["epoch_route_refresh"] = refresh
        elif reserve_exact_inflight:
            current[INFLIGHT_ROUTE_FIELD] = {
                "version": INFLIGHT_ROUTE_VERSION,
                "pending": True,
                "token": inflight_token,
                "campaign_id": str(current.get("campaign_id", "") or ""),
                "epoch": _positive_int(current.get("epoch", 1), 1),
                "route": normalized_route,
                "target_symbol": str(target_symbol or "").strip(),
                "active_file": str(active_file or "").strip(),
                "trigger": normalized_trigger,
                "reason": str(route_reason or ""),
                "source": str(route_source or ""),
                "target": dict(route_target or {}),
                "selected_at": decided_at,
            }
        current["updated_at"] = decided_at
        summary["campaign"] = current
        return {
            "streak": streak,
            "routes": routes,
            "semantic_routes": semantic_routes,
            "refresh": refresh,
            "inflight_route": (
                dict(current.get(INFLIGHT_ROUTE_FIELD) or {}) if reserve_exact_inflight else {}
            ),
            "negation_retries": negation_retries,
            "deduplicated": False,
        }

    payload = dict(update_json_file(_summary_path(), mutate) or {})
    streak = _nonnegative_int(payload.get("streak", 0))
    autonomy_state["orchestrator_routes_used"] = streak
    autonomy_state[EPOCH_ROUTES_STATE_KEY] = [
        dict(entry) for entry in (payload.get("routes") or []) if isinstance(entry, Mapping)
    ]
    autonomy_state[SEMANTIC_ROUTE_HISTORY_STATE_KEY] = _semantic_route_records(
        payload.get("semantic_routes")
    )
    autonomy_state[EPOCH_NEGATION_REFRESH_RETRIES_STATE_KEY] = _negation_refresh_retry_entries(
        payload.get("negation_retries")
    )
    refreshed = payload.get("refresh")
    if isinstance(refreshed, Mapping) and bool(refreshed.get("required")):
        autonomy_state[EPOCH_ROUTE_REFRESH_STATE_KEY] = dict(refreshed)
        pending_selection = _pending_refresh_selection(refreshed)
        if pending_selection:
            autonomy_state[EPOCH_ROUTE_SELECTION_STATE_KEY] = pending_selection
    inflight_route = payload.get("inflight_route")
    if isinstance(inflight_route, Mapping) and bool(inflight_route.get("pending")):
        autonomy_state[INFLIGHT_ROUTE_STATE_KEY] = dict(inflight_route)
    if streak >= route_limit:
        request_rollover(autonomy_state, ROUTE_NO_PROGRESS_ROLLOVER_REASON)
    return streak


def record_verified_graph_progress(
    autonomy_state: dict[str, Any],
    *,
    node_ids: Sequence[str],
) -> bool:
    """Reset the durable route streak after newly persisted verified nodes."""
    verified_nodes = sorted({str(node_id or "").strip() for node_id in node_ids if node_id})
    if not verified_nodes:
        return False
    campaign = ensure_campaign(autonomy_state)
    progressed_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> int:
        current = dict(summary.get("campaign") or campaign)
        previous = _nonnegative_int(current.get("no_progress_route_streak", 0))
        current["no_progress_route_streak"] = 0
        current[SEMANTIC_ROUTE_HISTORY_FIELD] = []
        current["last_verified_graph_progress"] = {
            "node_ids": verified_nodes,
            "recorded_at": progressed_at,
        }
        current["updated_at"] = progressed_at
        summary["campaign"] = current
        return previous

    previous = int(update_json_file(_summary_path(), mutate))
    autonomy_state["orchestrator_routes_used"] = 0
    autonomy_state[SEMANTIC_ROUTE_HISTORY_STATE_KEY] = []
    if str(autonomy_state.get("campaign_epoch_requested", "") or "") in (
        NO_PROGRESS_ROLLOVER_REASONS
    ):
        autonomy_state.pop("campaign_epoch_requested", None)
    if previous:
        append_workflow_activity(
            "campaign-route-streak-reset",
            f"Kernel-verified graph progress reset the no-progress route streak ({previous} -> 0)",
            campaign_id=str(campaign.get("campaign_id", "")),
            epoch=int(campaign.get("epoch", 1) or 1),
            previous_streak=previous,
            node_ids=verified_nodes,
        )
    return bool(previous)


def _mechanism_ledger_key(record: Mapping[str, Any]) -> str:
    """Return the parent-scoped key for one derived proof mechanism."""
    parent_id = str(record.get("parent_id", "") or "").strip()
    signature = str(record.get("mechanism_signature", "") or "").strip()
    return f"{parent_id}:{signature}" if parent_id and signature else ""


def record_verified_mechanism_progress(
    autonomy_state: dict[str, Any],
    *,
    historical_records: Sequence[Mapping[str, Any]] = (),
    candidate_records: Sequence[Mapping[str, Any]] = (),
    eligible_node_ids: Sequence[str] = (),
    forced_node_ids: Sequence[str] = (),
) -> MechanismProgressResult:
    """Persist helper mechanisms and reset only for a new parent-mechanism pair.

    Historical graph records are inserted before current candidates so a
    resumed campaign can classify repeated proof mechanisms accurately.
    Every eligible verified node remains a proved graph fact, but repeated use
    of the same mechanism under the same still-open parent cannot postpone a
    route-no-progress epoch rollover. Only the first eligible parent-mechanism
    pair resets the streak.
    ``forced_node_ids`` represents graph progress that outranks mechanism
    repetition, specifically parent closure or explicit exhaustive coverage.
    Every candidate remains a proved graph node regardless of this accounting.
    """
    historical = [dict(record) for record in historical_records]
    candidates = [dict(record) for record in candidate_records]
    eligible = {
        str(node_id or "").strip() for node_id in eligible_node_ids if str(node_id or "").strip()
    }
    forced = {
        str(node_id or "").strip() for node_id in forced_node_ids if str(node_id or "").strip()
    }
    if not historical and not candidates and not forced:
        return MechanismProgressResult()
    campaign = ensure_campaign(autonomy_state)
    recorded_at = _now_iso()

    def ordered(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            (dict(record) for record in records),
            key=lambda record: (
                str(record.get("node_id", "") or ""),
                str(record.get("parent_id", "") or ""),
                str(record.get("mechanism_signature", "") or ""),
            ),
        )

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        ledger = dict(current.get("verified_mechanisms") or {})
        raw_entries = ledger.get("entries")
        entries = {
            str(key): dict(value)
            for key, value in (
                dict(raw_entries).items() if isinstance(raw_entries, Mapping) else ()
            )
            if isinstance(value, Mapping)
        }

        def reserve(record: Mapping[str, Any], *, source: str) -> tuple[str, bool]:
            key = _mechanism_ledger_key(record)
            if not key:
                return "", False
            node_id = str(record.get("node_id", "") or "").strip()
            existing = dict(entries.get(key) or {})
            first_seen = not existing
            seen_node_ids = [
                str(value) for value in (existing.get("seen_node_ids") or []) if str(value).strip()
            ]
            if node_id and node_id not in seen_node_ids:
                seen_node_ids.append(node_id)
            if first_seen:
                existing = {
                    "signature_version": int(record.get("signature_version", 1) or 1),
                    "parent_id": str(record.get("parent_id", "") or ""),
                    "parent_name": str(record.get("parent_name", "") or ""),
                    "parent_file": str(record.get("parent_file", "") or ""),
                    "mechanism_signature": str(record.get("mechanism_signature", "") or ""),
                    "local_dependencies": list(record.get("local_dependencies") or []),
                    "local_dependency_ids": list(record.get("local_dependency_ids") or []),
                    "body_provenance_sha256": str(record.get("body_provenance_sha256", "") or ""),
                    "body_provenance_excerpt": str(record.get("body_provenance_excerpt", "") or "")[
                        :600
                    ],
                    "first_node_id": node_id,
                    "first_node_name": str(record.get("node_name", "") or ""),
                    "first_node_file": str(record.get("node_file", "") or ""),
                    "first_recorded_at": recorded_at,
                    "first_seen_source": source,
                }
            existing["seen_node_ids"] = seen_node_ids
            existing["seen_count"] = len(seen_node_ids)
            existing["last_node_id"] = node_id
            existing["last_node_name"] = str(record.get("node_name", "") or "")
            existing["last_recorded_at"] = recorded_at
            entries[key] = existing
            return key, first_seen

        for historical_record in ordered(historical):
            reserve(historical_record, source="graph-backfill")

        progressed: set[str] = set(forced)
        repeated: list[dict[str, Any]] = []
        first: list[dict[str, Any]] = []
        for candidate in ordered(candidates):
            node_id = str(candidate.get("node_id", "") or "").strip()
            key, first_seen = reserve(candidate, source="live-verification")
            if not key:
                continue
            annotated = {**candidate, "ledger_key": key}
            if first_seen:
                first.append(annotated)
            elif node_id not in forced:
                repeated.append(annotated)
            if node_id in eligible and first_seen:
                progressed.add(node_id)

        previous = _nonnegative_int(current.get("no_progress_route_streak", 0))
        if progressed:
            current["no_progress_route_streak"] = 0
            current[SEMANTIC_ROUTE_HISTORY_FIELD] = []
            current["last_verified_graph_progress"] = {
                "node_ids": sorted(progressed),
                "recorded_at": recorded_at,
                "accounting": "parent-scoped-proof-mechanism",
            }
        current["verified_mechanisms"] = {
            "version": MECHANISM_LEDGER_VERSION,
            "entries": entries,
        }
        current["updated_at"] = recorded_at
        summary["campaign"] = current
        return {
            "progressed_node_ids": sorted(progressed),
            "repeated_records": repeated,
            "first_records": first,
            "previous_streak": previous,
        }

    payload = dict(update_json_file(_summary_path(), mutate) or {})
    progressed_node_ids = tuple(str(value) for value in payload.get("progressed_node_ids") or [])
    repeated_records = tuple(
        dict(value) for value in payload.get("repeated_records") or [] if isinstance(value, Mapping)
    )
    first_records = tuple(
        dict(value) for value in payload.get("first_records") or [] if isinstance(value, Mapping)
    )
    previous = _nonnegative_int(payload.get("previous_streak", 0))
    if progressed_node_ids:
        autonomy_state["orchestrator_routes_used"] = 0
        autonomy_state[SEMANTIC_ROUTE_HISTORY_STATE_KEY] = []
        if str(autonomy_state.get("campaign_epoch_requested", "") or "") in (
            NO_PROGRESS_ROLLOVER_REASONS
        ):
            autonomy_state.pop("campaign_epoch_requested", None)
        if previous:
            append_workflow_activity(
                "campaign-route-streak-reset",
                (
                    "Kernel-verified graph progress reset the no-progress route "
                    f"streak ({previous} -> 0)"
                ),
                campaign_id=str(campaign.get("campaign_id", "")),
                epoch=int(campaign.get("epoch", 1) or 1),
                previous_streak=previous,
                node_ids=list(progressed_node_ids),
                accounting="parent-scoped-proof-mechanism",
            )
    return MechanismProgressResult(
        progressed_node_ids=progressed_node_ids,
        repeated_records=repeated_records,
        first_records=first_records,
        previous_streak=previous,
    )


def progress_route_streak_floor(
    campaign: Mapping[str, Any],
    excluded_node_ids: Sequence[str] | set[str],
) -> int:
    """Reconstruct a bounded route-streak floor while ignoring false resets.

    Current-epoch route decisions are durable and bounded.  Activity identifies
    the latest reset containing at least one non-excluded node; decisions after
    that point are the minimum true no-progress streak.  Missing activity fails
    conservatively to the whole retained epoch-route window.
    """
    excluded = {
        str(node_id or "").strip() for node_id in excluded_node_ids if str(node_id or "").strip()
    }
    routes = [
        dict(entry) for entry in (campaign.get("epoch_routes") or []) if isinstance(entry, Mapping)
    ]
    if not routes:
        return _nonnegative_int(campaign.get("no_progress_route_streak", 0))
    campaign_id = str(campaign.get("campaign_id", "") or "")
    epoch = _positive_int(campaign.get("epoch", 1), 1)
    try:
        events = read_workflow_activity(
            limit=5000,
            event_types={"campaign-route-streak-reset"},
        )
    except Exception:
        events = []
    latest_valid_reset: datetime | None = None
    for event in events:
        details = _event_details(event)
        event_campaign = str(details.get("campaign_id", "") or "")
        event_epoch = _positive_int(details.get("epoch", epoch), epoch)
        if (event_campaign and event_campaign != campaign_id) or event_epoch != epoch:
            continue
        node_ids = {
            str(value or "").strip()
            for value in (details.get("node_ids") or [])
            if str(value or "").strip()
        }
        if not node_ids or not (node_ids - excluded):
            continue
        event_at = _parse_activity_time(event.get("timestamp"))
        if event_at is not None and (latest_valid_reset is None or event_at > latest_valid_reset):
            latest_valid_reset = event_at
    if latest_valid_reset is None:
        return len(routes)
    return sum(
        1
        for route in routes
        if (decided_at := _parse_activity_time(route.get("decided_at"))) is not None
        and decided_at > latest_valid_reset
    )


def reconcile_conditional_helper_progress(
    autonomy_state: dict[str, Any],
    *,
    deferred_node_ids: Sequence[str],
    precomputed_streak_floor: int | None = None,
) -> ConditionalHelperProgressReconciliation:
    """Remove conditional bridges from campaign progress until their release.

    Kernel and graph ``proved`` statuses are intentionally preserved.  This
    transaction only removes deferred nodes from the proof-mechanism ledger,
    repairs a most-recent false route reset from bounded durable route history,
    and records which previously deferred nodes became eligible for ordinary
    progress accounting.  Provider-free startup may supply a conservative
    precomputed floor so this transaction does not scan activity history.
    """
    deferred = {
        str(node_id or "").strip() for node_id in deferred_node_ids if str(node_id or "").strip()
    }
    ensure_campaign(autonomy_state)
    campaign = campaign_snapshot()
    streak_floor = (
        progress_route_streak_floor(campaign, deferred)
        if precomputed_streak_floor is None
        else _nonnegative_int(precomputed_streak_floor)
    )
    reconciled_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        raw_policy = current.get("conditional_helper_progress")
        policy = dict(raw_policy) if isinstance(raw_policy, Mapping) else {}
        previous_deferred = {
            str(value or "").strip()
            for value in (policy.get("deferred_node_ids") or [])
            if str(value or "").strip()
        }
        newly_deferred = deferred - previous_deferred
        released = previous_deferred - deferred

        removed_ledger_nodes: set[str] = set()
        raw_ledger = current.get("verified_mechanisms")
        ledger = dict(raw_ledger) if isinstance(raw_ledger, Mapping) else {}
        raw_entries = ledger.get("entries")
        entries = {
            str(key): dict(value)
            for key, value in (
                dict(raw_entries).items() if isinstance(raw_entries, Mapping) else ()
            )
            if isinstance(value, Mapping)
        }
        retained_entries: dict[str, dict[str, Any]] = {}
        for key, entry in entries.items():
            seen = [
                str(value or "").strip()
                for value in (entry.get("seen_node_ids") or [])
                if str(value or "").strip()
            ]
            removed_ledger_nodes.update(set(seen) & deferred)
            kept = [node_id for node_id in seen if node_id not in deferred]
            if not kept:
                continue
            if str(entry.get("first_node_id", "") or "") not in kept:
                entry["first_node_id"] = kept[0]
                entry["first_node_name"] = ""
                entry["first_node_file"] = ""
                entry["first_seen_source"] = "conditional-helper-reconciliation"
            if str(entry.get("last_node_id", "") or "") not in kept:
                entry["last_node_id"] = kept[-1]
                entry["last_node_name"] = ""
            entry["seen_node_ids"] = kept
            entry["seen_count"] = len(kept)
            retained_entries[key] = entry
        if entries or retained_entries:
            current["verified_mechanisms"] = {
                "version": _positive_int(ledger.get("version", MECHANISM_LEDGER_VERSION), 1),
                "entries": retained_entries,
            }

        previous_streak = _nonnegative_int(current.get("no_progress_route_streak", 0))
        repaired_streak = previous_streak
        last_progress = current.get("last_verified_graph_progress")
        false_last_progress = False
        if isinstance(last_progress, Mapping):
            last_node_ids = [
                str(value or "").strip()
                for value in (last_progress.get("node_ids") or [])
                if str(value or "").strip()
            ]
            retained_last = [node_id for node_id in last_node_ids if node_id not in deferred]
            false_last_progress = bool(last_node_ids and len(retained_last) < len(last_node_ids))
            if false_last_progress:
                if retained_last:
                    current["last_verified_graph_progress"] = {
                        **dict(last_progress),
                        "node_ids": retained_last,
                    }
                else:
                    current.pop("last_verified_graph_progress", None)
                    repaired_streak = max(previous_streak, streak_floor)
                    current["no_progress_route_streak"] = repaired_streak

        policy = {
            "version": CONDITIONAL_HELPER_PROGRESS_POLICY_VERSION,
            "deferred_node_ids": sorted(deferred),
            "updated_at": reconciled_at,
        }
        if newly_deferred or released or removed_ledger_nodes or false_last_progress:
            policy["last_reconciliation"] = {
                "newly_deferred_node_ids": sorted(newly_deferred),
                "released_node_ids": sorted(released),
                "removed_ledger_node_ids": sorted(removed_ledger_nodes),
                "previous_streak": previous_streak,
                "repaired_streak": repaired_streak,
                "reconciled_at": reconciled_at,
            }
        current["conditional_helper_progress"] = policy
        current["updated_at"] = reconciled_at
        summary["campaign"] = current
        return {
            "newly_deferred_node_ids": sorted(newly_deferred),
            "released_node_ids": sorted(released),
            "removed_ledger_node_ids": sorted(removed_ledger_nodes),
            "previous_streak": previous_streak,
            "repaired_streak": repaired_streak,
            "changed": bool(
                newly_deferred or released or removed_ledger_nodes or false_last_progress
            ),
        }

    payload = dict(update_json_file(_summary_path(), mutate) or {})
    previous_streak = _nonnegative_int(payload.get("previous_streak", 0))
    repaired_streak = _nonnegative_int(payload.get("repaired_streak", previous_streak))
    autonomy_state["orchestrator_routes_used"] = repaired_streak
    route_limit = _positive_int(campaign.get("no_progress_route_limit", ROUTE_EPOCH_LIMIT), 1)
    if repaired_streak >= route_limit:
        request_rollover(autonomy_state, ROUTE_NO_PROGRESS_ROLLOVER_REASON)
    if bool(payload.get("changed")):
        append_workflow_activity(
            "campaign-conditional-helper-progress-reconciled",
            "Deferred conditional helper facts without changing their kernel-proved status",
            campaign_id=str(campaign.get("campaign_id", "") or ""),
            epoch=_positive_int(campaign.get("epoch", 1), 1),
            newly_deferred_node_ids=list(payload.get("newly_deferred_node_ids") or []),
            released_node_ids=list(payload.get("released_node_ids") or []),
            removed_ledger_node_ids=list(payload.get("removed_ledger_node_ids") or []),
            previous_streak=previous_streak,
            repaired_streak=repaired_streak,
        )
    return ConditionalHelperProgressReconciliation(
        newly_deferred_node_ids=tuple(
            str(value) for value in payload.get("newly_deferred_node_ids") or []
        ),
        released_node_ids=tuple(str(value) for value in payload.get("released_node_ids") or []),
        removed_ledger_node_ids=tuple(
            str(value) for value in payload.get("removed_ledger_node_ids") or []
        ),
        previous_streak=previous_streak,
        repaired_streak=repaired_streak,
    )


def reconcile_finite_branch_progress(
    autonomy_state: dict[str, Any],
    *,
    evidence_node_ids: Sequence[str],
) -> FiniteBranchProgressReconciliation:
    """Repair legacy route resets caused by saturated finite-branch helpers.

    The graph-aware caller supplies helpers reconstructed from durable proof
    promotion order. This transaction preserves their kernel-proved graph
    status, removes obsolete mechanism-ledger credit, and reconstructs the
    route streak from current-epoch routes plus genuine reset activity. A due
    rollover is requested at the configured limit instead of persisting an
    over-limit streak.
    """
    ordered_evidence = tuple(
        dict.fromkeys(
            str(node_id or "").strip()
            for node_id in evidence_node_ids
            if str(node_id or "").strip()
        )
    )
    evidence = set(ordered_evidence)
    ensure_campaign(autonomy_state)
    campaign = campaign_snapshot()
    if not evidence:
        current_streak = _nonnegative_int(campaign.get("no_progress_route_streak", 0))
        autonomy_state["orchestrator_routes_used"] = current_streak
        return FiniteBranchProgressReconciliation(
            previous_streak=current_streak,
            reconstructed_streak=current_streak,
            repaired_streak=current_streak,
        )
    streak_floor = progress_route_streak_floor(campaign, evidence)
    reconciled_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        previous_streak = _nonnegative_int(current.get("no_progress_route_streak", 0))
        route_limit = _positive_int(
            current.get("no_progress_route_limit", ROUTE_EPOCH_LIMIT),
            ROUTE_EPOCH_LIMIT,
        )
        if (
            _nonnegative_int(current.get("finite_branch_progress_policy_version", 0))
            >= FINITE_BRANCH_PROGRESS_POLICY_VERSION
        ):
            return {
                "previous_streak": previous_streak,
                "reconstructed_streak": previous_streak,
                "repaired_streak": previous_streak,
                "route_limit": route_limit,
                "changed": False,
            }

        removed_ledger_nodes: set[str] = set()
        raw_ledger = current.get("verified_mechanisms")
        ledger = dict(raw_ledger) if isinstance(raw_ledger, Mapping) else {}
        raw_entries = ledger.get("entries")
        entries = {
            str(key): dict(value)
            for key, value in (
                dict(raw_entries).items() if isinstance(raw_entries, Mapping) else ()
            )
            if isinstance(value, Mapping)
        }
        retained_entries: dict[str, dict[str, Any]] = {}
        for key, entry in entries.items():
            seen = [
                str(value or "").strip()
                for value in (entry.get("seen_node_ids") or [])
                if str(value or "").strip()
            ]
            removed_ledger_nodes.update(set(seen) & evidence)
            kept = [node_id for node_id in seen if node_id not in evidence]
            if not kept:
                continue
            if str(entry.get("first_node_id", "") or "") not in kept:
                entry["first_node_id"] = kept[0]
                entry["first_node_name"] = ""
                entry["first_node_file"] = ""
                entry["first_seen_source"] = "finite-branch-progress-reconciliation"
            if str(entry.get("last_node_id", "") or "") not in kept:
                entry["last_node_id"] = kept[-1]
                entry["last_node_name"] = ""
            entry["seen_node_ids"] = kept
            entry["seen_count"] = len(kept)
            retained_entries[key] = entry
        if removed_ledger_nodes:
            current["verified_mechanisms"] = {
                **ledger,
                "version": _positive_int(ledger.get("version", MECHANISM_LEDGER_VERSION), 1),
                "entries": retained_entries,
            }

        last_progress = current.get("last_verified_graph_progress")
        last_progress_mapping = dict(last_progress) if isinstance(last_progress, Mapping) else {}
        last_node_ids = [
            str(value or "").strip()
            for value in (last_progress_mapping.get("node_ids") or [])
            if str(value or "").strip()
        ]
        false_last = [node_id for node_id in last_node_ids if node_id in evidence]
        retained_last = [node_id for node_id in last_node_ids if node_id not in evidence]
        route_times = [
            decided_at
            for route in (current.get("epoch_routes") or [])
            if isinstance(route, Mapping)
            and (decided_at := _parse_activity_time(route.get("decided_at"))) is not None
        ]
        last_progress_at = _parse_activity_time(last_progress_mapping.get("recorded_at"))
        false_reset_predates_epoch_routes = bool(
            false_last
            and route_times
            and last_progress_at is not None
            and last_progress_at < min(route_times)
        )
        reconstructed_streak = previous_streak
        repaired_streak = previous_streak
        cleared_last_progress = False
        repaired_last_progress = False
        if false_last:
            if retained_last:
                current["last_verified_graph_progress"] = {
                    **last_progress_mapping,
                    "node_ids": retained_last,
                }
                repaired_last_progress = True
            else:
                current.pop("last_verified_graph_progress", None)
                cleared_last_progress = True
                if not false_reset_predates_epoch_routes:
                    reconstructed_streak = max(previous_streak, streak_floor)
                    repaired_streak = min(reconstructed_streak, route_limit)
                current["no_progress_route_streak"] = repaired_streak

        changed = bool(false_last or removed_ledger_nodes)
        current["finite_branch_progress_policy_version"] = FINITE_BRANCH_PROGRESS_POLICY_VERSION
        if changed:
            reconciliation = {
                "version": FINITE_BRANCH_PROGRESS_POLICY_VERSION,
                "reason": "legacy-saturated-finite-branch-reset-ignored",
                "false_reset_node_ids": list(ordered_evidence),
                "removed_ledger_node_ids": sorted(removed_ledger_nodes),
                "retained_last_progress_node_ids": retained_last,
                "previous_streak": previous_streak,
                "reconstructed_streak": reconstructed_streak,
                "repaired_streak": repaired_streak,
                "route_limit": route_limit,
                "last_progress_cleared": cleared_last_progress,
                "last_progress_repaired": repaired_last_progress,
                "false_reset_predates_epoch_routes": (false_reset_predates_epoch_routes),
                "rollover_required": repaired_streak >= route_limit,
                "reconciled_at": reconciled_at,
            }
            current["finite_branch_progress_reconciliation"] = reconciliation
        current["updated_at"] = reconciled_at
        summary["campaign"] = current
        return {
            "false_reset_node_ids": list(ordered_evidence),
            "removed_ledger_node_ids": sorted(removed_ledger_nodes),
            "retained_last_progress_node_ids": retained_last,
            "previous_streak": previous_streak,
            "reconstructed_streak": reconstructed_streak,
            "repaired_streak": repaired_streak,
            "route_limit": route_limit,
            "false_reset_predates_epoch_routes": (false_reset_predates_epoch_routes),
            "changed": changed,
        }

    payload = dict(update_json_file(_summary_path(), mutate) or {})
    previous_streak = _nonnegative_int(payload.get("previous_streak", 0))
    reconstructed_streak = _nonnegative_int(payload.get("reconstructed_streak", previous_streak))
    repaired_streak = _nonnegative_int(payload.get("repaired_streak", previous_streak))
    route_limit = _positive_int(payload.get("route_limit", ROUTE_EPOCH_LIMIT), ROUTE_EPOCH_LIMIT)
    autonomy_state["orchestrator_routes_used"] = repaired_streak
    rollover_required = repaired_streak >= route_limit
    if rollover_required:
        request_rollover(autonomy_state, ROUTE_NO_PROGRESS_ROLLOVER_REASON)
    return FiniteBranchProgressReconciliation(
        false_reset_node_ids=tuple(
            str(value) for value in payload.get("false_reset_node_ids") or []
        ),
        removed_ledger_node_ids=tuple(
            str(value) for value in payload.get("removed_ledger_node_ids") or []
        ),
        retained_last_progress_node_ids=tuple(
            str(value) for value in payload.get("retained_last_progress_node_ids") or []
        ),
        previous_streak=previous_streak,
        reconstructed_streak=reconstructed_streak,
        repaired_streak=repaired_streak,
        false_reset_predates_epoch_routes=bool(payload.get("false_reset_predates_epoch_routes")),
        rollover_required=rollover_required,
        changed=bool(payload.get("changed")),
    )


def _prior_repaired_streak_before(
    campaign: Mapping[str, Any],
    *,
    progress_at: datetime | None,
) -> int:
    """Return the strongest earlier policy repair overwritten by later progress."""
    candidates: list[Mapping[str, Any]] = []
    finite = campaign.get("finite_branch_progress_reconciliation")
    if isinstance(finite, Mapping):
        candidates.append(finite)
    mechanism = campaign.get("mechanism_progress_policy_reconciliation")
    if isinstance(mechanism, Mapping):
        candidates.append(mechanism)
    conditional = campaign.get("conditional_helper_progress")
    conditional_last = (
        conditional.get("last_reconciliation") if isinstance(conditional, Mapping) else None
    )
    if isinstance(conditional_last, Mapping):
        candidates.append(conditional_last)

    floor = 0
    for candidate in candidates:
        reconciled_at = _parse_activity_time(candidate.get("reconciled_at"))
        if progress_at is not None:
            if reconciled_at is None or reconciled_at > progress_at:
                continue
        floor = max(floor, _nonnegative_int(candidate.get("repaired_streak", 0)))
    return floor


def reconcile_resume_graph_progress(
    autonomy_state: dict[str, Any],
    *,
    recovered_node_ids: Sequence[str],
) -> ResumeGraphProgressReconciliation:
    """Keep resume-restored graph truth from masquerading as live progress.

    Exact resume gates restore proof authority for declarations completed before
    this process started. A legacy runner could promote the restored current
    assignment only after queue rotation, then clear an already-due route
    rollover as though the proof had just been produced. Apply this migration
    once per campaign and reconstruct any overwritten streak from durable route
    history plus earlier policy repairs.
    """
    recovered = tuple(
        dict.fromkeys(
            str(node_id or "").strip()
            for node_id in recovered_node_ids
            if str(node_id or "").strip()
        )
    )
    ensure_campaign(autonomy_state)
    campaign = campaign_snapshot()
    current_streak = _nonnegative_int(campaign.get("no_progress_route_streak", 0))
    if not recovered:
        if (
            _nonnegative_int(campaign.get("resume_graph_progress_policy_version", 0))
            < RESUME_GRAPH_PROGRESS_POLICY_VERSION
        ):

            def mark_current(summary: dict[str, Any]) -> None:
                current = dict(summary.get("campaign") or campaign)
                current["resume_graph_progress_policy_version"] = (
                    RESUME_GRAPH_PROGRESS_POLICY_VERSION
                )
                current["updated_at"] = _now_iso()
                summary["campaign"] = current

            update_json_file(_summary_path(), mark_current)
        autonomy_state["orchestrator_routes_used"] = current_streak
        return ResumeGraphProgressReconciliation(
            previous_streak=current_streak,
            reconstructed_streak=current_streak,
            repaired_streak=current_streak,
        )
    if (
        _nonnegative_int(campaign.get("resume_graph_progress_policy_version", 0))
        >= RESUME_GRAPH_PROGRESS_POLICY_VERSION
    ):
        autonomy_state["orchestrator_routes_used"] = current_streak
        return ResumeGraphProgressReconciliation(
            previous_streak=current_streak,
            reconstructed_streak=current_streak,
            repaired_streak=current_streak,
            rollover_required=current_streak
            >= _positive_int(
                campaign.get("no_progress_route_limit", ROUTE_EPOCH_LIMIT),
                ROUTE_EPOCH_LIMIT,
            ),
        )

    exclusions = set(recovered)
    finite = campaign.get("finite_branch_progress_reconciliation")
    if isinstance(finite, Mapping):
        exclusions.update(
            str(node_id or "").strip()
            for node_id in (finite.get("false_reset_node_ids") or [])
            if str(node_id or "").strip()
        )
    conditional = campaign.get("conditional_helper_progress")
    if isinstance(conditional, Mapping):
        exclusions.update(
            str(node_id or "").strip()
            for node_id in (conditional.get("deferred_node_ids") or [])
            if str(node_id or "").strip()
        )
    route_streak_floor = progress_route_streak_floor(campaign, exclusions)
    reconciled_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        previous_streak = _nonnegative_int(current.get("no_progress_route_streak", 0))
        route_limit = _positive_int(
            current.get("no_progress_route_limit", ROUTE_EPOCH_LIMIT),
            ROUTE_EPOCH_LIMIT,
        )
        if (
            _nonnegative_int(current.get("resume_graph_progress_policy_version", 0))
            >= RESUME_GRAPH_PROGRESS_POLICY_VERSION
        ):
            return {
                "previous_streak": previous_streak,
                "reconstructed_streak": previous_streak,
                "repaired_streak": previous_streak,
                "route_limit": route_limit,
                "changed": False,
            }

        current["resume_graph_progress_policy_version"] = RESUME_GRAPH_PROGRESS_POLICY_VERSION
        last_progress = current.get("last_verified_graph_progress")
        last_mapping = dict(last_progress) if isinstance(last_progress, Mapping) else {}
        last_node_ids = [
            str(node_id or "").strip()
            for node_id in (last_mapping.get("node_ids") or [])
            if str(node_id or "").strip()
        ]
        removed = [node_id for node_id in last_node_ids if node_id in recovered]
        retained = [node_id for node_id in last_node_ids if node_id not in recovered]
        reconstructed_streak = previous_streak
        repaired_streak = previous_streak
        if removed:
            if retained:
                current["last_verified_graph_progress"] = {
                    **last_mapping,
                    "node_ids": retained,
                }
            else:
                current.pop("last_verified_graph_progress", None)
                progress_at = _parse_activity_time(last_mapping.get("recorded_at"))
                reconstructed_streak = max(
                    previous_streak,
                    route_streak_floor,
                    _prior_repaired_streak_before(current, progress_at=progress_at),
                )
                repaired_streak = min(reconstructed_streak, route_limit)
                current["no_progress_route_streak"] = repaired_streak
            current["resume_graph_progress_reconciliation"] = {
                "version": RESUME_GRAPH_PROGRESS_POLICY_VERSION,
                "reason": "startup-restored-proof-is-not-live-progress",
                "recovered_node_ids": list(recovered),
                "removed_last_progress_node_ids": removed,
                "retained_last_progress_node_ids": retained,
                "previous_streak": previous_streak,
                "reconstructed_streak": reconstructed_streak,
                "repaired_streak": repaired_streak,
                "route_limit": route_limit,
                "rollover_required": repaired_streak >= route_limit,
                "reconciled_at": reconciled_at,
            }
        current["updated_at"] = reconciled_at
        summary["campaign"] = current
        return {
            "recovered_node_ids": list(recovered),
            "removed_last_progress_node_ids": removed,
            "retained_last_progress_node_ids": retained,
            "previous_streak": previous_streak,
            "reconstructed_streak": reconstructed_streak,
            "repaired_streak": repaired_streak,
            "route_limit": route_limit,
            "changed": bool(removed),
        }

    payload = dict(update_json_file(_summary_path(), mutate) or {})
    previous_streak = _nonnegative_int(payload.get("previous_streak", current_streak))
    reconstructed_streak = _nonnegative_int(payload.get("reconstructed_streak", previous_streak))
    repaired_streak = _nonnegative_int(payload.get("repaired_streak", previous_streak))
    route_limit = _positive_int(payload.get("route_limit", ROUTE_EPOCH_LIMIT), ROUTE_EPOCH_LIMIT)
    autonomy_state["orchestrator_routes_used"] = repaired_streak
    rollover_required = repaired_streak >= route_limit
    if rollover_required:
        request_rollover(autonomy_state, ROUTE_NO_PROGRESS_ROLLOVER_REASON)
    changed = bool(payload.get("changed"))
    if changed:
        append_workflow_activity(
            "campaign-resume-graph-progress-reconciled",
            "Removed startup-restored proof truth from live campaign progress accounting",
            campaign_id=str(campaign.get("campaign_id", "") or ""),
            epoch=_positive_int(campaign.get("epoch", 1), 1),
            recovered_node_ids=list(payload.get("recovered_node_ids") or []),
            removed_last_progress_node_ids=list(
                payload.get("removed_last_progress_node_ids") or []
            ),
            retained_last_progress_node_ids=list(
                payload.get("retained_last_progress_node_ids") or []
            ),
            previous_streak=previous_streak,
            reconstructed_streak=reconstructed_streak,
            repaired_streak=repaired_streak,
            rollover_required=rollover_required,
        )
    return ResumeGraphProgressReconciliation(
        recovered_node_ids=tuple(str(value) for value in payload.get("recovered_node_ids") or []),
        removed_last_progress_node_ids=tuple(
            str(value) for value in payload.get("removed_last_progress_node_ids") or []
        ),
        retained_last_progress_node_ids=tuple(
            str(value) for value in payload.get("retained_last_progress_node_ids") or []
        ),
        previous_streak=previous_streak,
        reconstructed_streak=reconstructed_streak,
        repaired_streak=repaired_streak,
        rollover_required=rollover_required,
        changed=changed,
    )


def request_rollover(autonomy_state: dict[str, Any], reason: str) -> None:
    """Request one epoch rollover without overwriting an earlier reason."""
    autonomy_state.setdefault("campaign_epoch_requested", str(reason or "strategy-refresh"))


def consume_rollover_request(autonomy_state: dict[str, Any]) -> str:
    """Return and clear the pending epoch-rollover reason."""
    return str(autonomy_state.pop("campaign_epoch_requested", "") or "")


def reset_compaction_state() -> dict[str, Any]:
    """Return the empty compaction status for a fresh epoch context."""
    return {
        "snapshot_text": "",
        "reason": "epoch-rollover",
        "rough_tokens_before": 0,
        "rough_tokens_after": 0,
        "pruned_messages": 0,
        "compacted": False,
        "snapshot_created": False,
    }


def roll_epoch(
    autonomy_state: dict[str, Any],
    *,
    reason: str,
    cycle: int,
    target_symbol: str = "",
    active_file: str = "",
    live_message: str = "",
    failed_attempts: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Persist an epoch boundary and return the fresh-context handoff."""
    campaign = ensure_campaign(autonomy_state)
    old_epoch = int(campaign.get("epoch", 1) or 1)
    new_epoch = old_epoch + 1
    route_streak = _nonnegative_int(autonomy_state.get("orchestrator_routes_used", 0))
    local_cycles = _nonnegative_int(autonomy_state.get(_EPOCH_CYCLES_STATE_KEY, 0))
    ended_at = _now_iso()
    attempts = [dict(entry) for entry in failed_attempts[-6:]]
    refresh_token = uuid.uuid4().hex

    def ending_scope(
        route_entries: Sequence[Mapping[str, Any]],
    ) -> tuple[str, str]:
        """Return the route-owned scope for a no-progress epoch boundary.

        Queue selection may advance before a resumed runner consumes a
        durable rollover request.  In that case the caller's target is the
        fresh epoch's active assignment, while the last persisted route still
        identifies the spent epoch that actually requested the rollover.
        """
        if reason != ROUTE_NO_PROGRESS_ROLLOVER_REASON:
            return target_symbol, active_file
        for entry in reversed(route_entries):
            route_target = str(entry.get("target_symbol", "") or "").strip()
            route_file = str(entry.get("active_file", "") or "").strip()
            if route_target or route_file:
                return route_target or target_symbol, route_file or active_file
        return target_symbol, active_file

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        prior_route_entries = [
            dict(entry)
            for entry in (current.get("epoch_routes") or [])
            if isinstance(entry, Mapping)
        ][-EPOCH_ROUTE_HISTORY_CAP:]
        prior_routes = [
            str(entry.get("route", "") or "")
            for entry in prior_route_entries
            if str(entry.get("route", "") or "")
        ]
        ended_target_symbol, ended_active_file = ending_scope(prior_route_entries)
        route_refresh = {
            "required": True,
            "token": refresh_token,
            "previous_epoch": old_epoch,
            "new_epoch": new_epoch,
            "reason": reason,
            "previous_routes": prior_routes,
            "previous_route_portfolio": prior_route_entries,
            "requested_at": ended_at,
        }
        worker_refresh = {
            "pending": True,
            "token": refresh_token,
            "previous_epoch": old_epoch,
            "new_epoch": new_epoch,
            "reason": reason,
            "target_symbol": target_symbol,
            "active_file": active_file,
            "requested_at": ended_at,
        }
        history = [
            dict(entry)
            for entry in (current.get("epoch_history") or [])
            if isinstance(entry, Mapping)
        ]
        persisted_cycles = _nonnegative_int(current.get("epoch_cycles", 0))
        ended_cycles = max(local_cycles, persisted_cycles, _nonnegative_int(cycle))
        history.append(
            {
                "epoch": old_epoch,
                "ended_at": ended_at,
                "reason": reason,
                "cycles": ended_cycles,
                "target_symbol": ended_target_symbol,
                "active_file": ended_active_file,
                "failed_attempt_count": len(attempts),
                "no_progress_route_streak": route_streak,
                "route_portfolio": prior_route_entries,
            }
        )
        current.update(
            {
                "epoch": new_epoch,
                "status": "running",
                "updated_at": ended_at,
                "epoch_history": history[-CAMPAIGN_HISTORY_CAP:],
                "epoch_cycles": 0,
                "no_progress_route_streak": 0,
                "epoch_routes": [],
                "epoch_route_refresh": route_refresh,
                "epoch_worker_refresh": worker_refresh,
            }
        )
        # A planner reservation is scoped to the spent epoch. The fresh epoch
        # receives its own route-selection contract after rollover.
        current.pop(PLANNER_CAPACITY_RESERVATION_FIELD, None)
        current.pop(INFLIGHT_ROUTE_FIELD, None)
        summary["campaign"] = current
        return current

    updated = update_json_file(_summary_path(), mutate)
    autonomy_state["campaign_epoch"] = new_epoch
    autonomy_state["campaign_status"] = "running"
    autonomy_state[_EPOCH_CYCLES_STATE_KEY] = 0
    autonomy_state["continuation_stable_cycles"] = 0
    autonomy_state["continuation_blocked_runs"] = 0
    autonomy_state["orchestrator_routes_used"] = 0
    autonomy_state[EPOCH_ROUTES_STATE_KEY] = []
    autonomy_state[EPOCH_ROUTE_REFRESH_STATE_KEY] = dict(updated.get("epoch_route_refresh") or {})
    autonomy_state[EPOCH_WORKER_REFRESH_STATE_KEY] = dict(updated.get("epoch_worker_refresh") or {})
    autonomy_state.pop("campaign_epoch_requested", None)
    autonomy_state.pop("orchestrator_scope_entered", None)
    autonomy_state.pop("orchestrator_current_route", None)
    autonomy_state.pop("_orchestrator_last_ctx", None)
    autonomy_state.pop(EPOCH_ROUTE_SELECTION_STATE_KEY, None)
    autonomy_state.pop(INFLIGHT_ROUTE_STATE_KEY, None)
    autonomy_state.pop(PLANNER_CAPACITY_RESERVATION_STATE_KEY, None)
    requested_route = autonomy_state.get("prover_requested_route")
    if isinstance(requested_route, Mapping) and (
        str(requested_route.get("route", "") or "").strip().lower() == "plan"
    ):
        autonomy_state.pop("prover_requested_route", None)
    autonomy_state.pop("orchestrator_cadence_cycle", None)
    autonomy_state.pop("manager_nudge_seen", None)

    ended_cycles = _nonnegative_int((updated.get("epoch_history") or [{}])[-1].get("cycles", 0))
    ended_epoch = dict((updated.get("epoch_history") or [{}])[-1])
    ended_target_symbol = str(ended_epoch.get("target_symbol", "") or "")
    ended_active_file = str(ended_epoch.get("active_file", "") or "")
    append_workflow_activity(
        "campaign-epoch-ended",
        f"Campaign epoch {old_epoch} ended: {reason}",
        campaign_id=str(updated.get("campaign_id", "")),
        epoch=old_epoch,
        reason=reason,
        cycle=ended_cycles,
        target_symbol=ended_target_symbol,
        active_file=ended_active_file,
    )
    append_workflow_activity(
        "campaign-epoch-started",
        f"Campaign epoch {new_epoch} started with a fresh model context",
        campaign_id=str(updated.get("campaign_id", "")),
        epoch=new_epoch,
        previous_reason=reason,
        target_symbol=target_symbol,
        active_file=active_file,
    )

    attempt_lines = [
        "- "
        + str(entry.get("proof_shape", "attempt") or "attempt")
        + ": "
        + str(entry.get("reason", "") or "no recorded reason")[:240]
        for entry in attempts
    ]
    return "\n".join(
        [
            "[LEANFLOW CAMPAIGN EPOCH HANDOFF]",
            f"- campaign: {updated.get('campaign_id', '')}",
            f"- fresh epoch: {new_epoch}",
            f"- rollover reason: {reason}",
            f"- active target: {target_symbol or '[scope]'}",
            f"- active file: {active_file or '[project]'}",
            "- mathematical status: unresolved; this rollover is not a stop or failure verdict",
            "- preserve all kernel-verified helpers and avoid the failed proof shapes below",
            *(attempt_lines or ["- failed proof shapes: [none recorded]"]),
            f"- latest live state: {str(live_message or '[refresh with lean_inspect]')[:1200]}",
            "- now inspect the refreshed Lean state and execute a distinct concrete route",
        ]
    )


def _refresh_route_is_distinct(refresh: Mapping[str, Any], route: str) -> bool:
    """Return whether one allowed route differs from the persisted prior portfolio."""
    normalized_route = str(route or "").strip().lower()
    if normalized_route not in EPOCH_REFRESH_ALLOWED_ROUTES:
        return False
    previous = [
        str(value or "").strip().lower()
        for value in (refresh.get("previous_routes") or [])
        if str(value or "").strip().lower() in EPOCH_REFRESH_ALLOWED_ROUTES
    ]
    unseen = [route for route in EPOCH_REFRESH_ALLOWED_ROUTES if route not in set(previous)]
    if unseen:
        return normalized_route in unseen
    return not previous or normalized_route != previous[-1]


def _refresh_accepts_started_route(
    refresh: Mapping[str, Any],
    route: str,
    *,
    target_symbol: str | None = None,
    active_file: str | None = None,
) -> bool:
    """Accept the exact persisted selection, otherwise apply legacy diversity.

    Orchestration selects from the routes that remain viable under current
    evidence. That set can be narrower than the static campaign vocabulary
    (for example, an attempted negation removes ``negate``). Once the selected
    route is durably reserved, recomputing diversity from the wider static set
    can reject valid work forever. A present selection is therefore
    authoritative only when its token-bound payload is valid and the exact
    route and optional scope match; malformed or mismatched selections fail
    closed. Refresh records created before durable selection support retain the
    static diversity check.
    """
    raw_selection = refresh.get("pending_selection")
    if isinstance(raw_selection, Mapping):
        selection = _pending_refresh_selection(refresh)
        if (
            not selection
            or str(selection.get("route", "") or "") != str(route or "").strip().lower()
        ):
            return False
        if target_symbol is not None and active_file is not None:
            return _selection_matches_scope(
                selection,
                target_symbol=target_symbol,
                active_file=active_file,
            )
        return True
    return _refresh_route_is_distinct(refresh, route)


def mark_epoch_refresh_started(
    autonomy_state: dict[str, Any],
    *,
    route: str,
    refresh_token: str,
    epoch: int,
    target_symbol: str | None = None,
    active_file: str | None = None,
) -> bool:
    """Complete the fresh-route obligation after observable strategy work.

    The caller must present the exact rollover token and epoch selected by the
    successful scope consult. This prevents a stale route from an earlier
    theorem or process context from consuming the durable obligation.
    """
    normalized_route = str(route or "").strip().lower()
    refresh = dict(autonomy_state.get(EPOCH_ROUTE_REFRESH_STATE_KEY) or {})
    if (
        not bool(refresh.get("required"))
        or str(refresh.get("token", "") or "") != str(refresh_token or "")
        or _positive_int(refresh.get("new_epoch", 0), 1) != _positive_int(epoch, 1)
        or not _refresh_accepts_started_route(
            refresh,
            normalized_route,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    ):
        return False
    campaign = ensure_campaign(autonomy_state)
    started_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> bool:
        current = dict(summary.get("campaign") or campaign)
        persisted = dict(current.get("epoch_route_refresh") or refresh)
        if (
            not bool(persisted.get("required"))
            or str(persisted.get("token", "") or "") != str(refresh_token or "")
            or _positive_int(persisted.get("new_epoch", 0), 1) != _positive_int(epoch, 1)
            or not _refresh_accepts_started_route(
                persisted,
                normalized_route,
                target_symbol=target_symbol,
                active_file=active_file,
            )
        ):
            return False
        persisted.update(
            {
                "required": False,
                "selected_route": normalized_route,
                "started_at": started_at,
            }
        )
        persisted.pop("pending_selection", None)
        current["epoch_route_refresh"] = persisted
        current["updated_at"] = started_at
        summary["campaign"] = current
        return True

    consumed = bool(update_json_file(_summary_path(), mutate))
    if not consumed:
        return False
    autonomy_state.pop(EPOCH_ROUTE_REFRESH_STATE_KEY, None)
    append_workflow_activity(
        "campaign-epoch-route-started",
        f"Fresh epoch started distinct route {normalized_route}",
        campaign_id=str(campaign.get("campaign_id", "")),
        epoch=int(campaign.get("epoch", 1) or 1),
        route=normalized_route,
        refresh_token=str(refresh_token or ""),
        previous_routes=list(refresh.get("previous_routes") or []),
    )
    return True


def supersede_epoch_refresh_selection(
    autonomy_state: dict[str, Any],
    *,
    refresh_token: str,
    epoch: int,
    target_symbol: str,
    active_file: str,
    reason: str,
) -> bool:
    """Retire an exact fresh-route selection displaced by newer source work.

    This is narrower than ordinary route completion: it records that the
    selected mechanical action never started and that a newer exact-scope
    source candidate now owns the foreground. Token, epoch, and assignment
    checks prevent unrelated resume state from clearing the obligation.
    """
    refresh = dict(autonomy_state.get(EPOCH_ROUTE_REFRESH_STATE_KEY) or {})
    selection = _pending_refresh_selection(refresh)
    if (
        not selection
        or str(selection.get("token", "") or "") != str(refresh_token or "")
        or _positive_int(selection.get("epoch", 0), 1) != _positive_int(epoch, 1)
        or not _selection_matches_scope(
            selection,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    ):
        return False
    campaign = ensure_campaign(autonomy_state)
    superseded_at = _now_iso()
    selected_route = str(selection.get("route", "") or "").strip().lower()

    def mutate(summary: dict[str, Any]) -> bool:
        current = dict(summary.get("campaign") or campaign)
        persisted = dict(current.get("epoch_route_refresh") or refresh)
        persisted_selection = _pending_refresh_selection(persisted)
        if (
            not persisted_selection
            or str(persisted_selection.get("token", "") or "") != str(refresh_token or "")
            or _positive_int(persisted_selection.get("epoch", 0), 1) != _positive_int(epoch, 1)
            or not _selection_matches_scope(
                persisted_selection,
                target_symbol=target_symbol,
                active_file=active_file,
            )
        ):
            return False
        persisted.update(
            {
                "required": False,
                "superseded_route": selected_route,
                "superseded_at": superseded_at,
                "superseded_reason": str(reason or "newer exact-scope source candidate"),
            }
        )
        persisted.pop("pending_selection", None)
        current["epoch_route_refresh"] = persisted
        current["updated_at"] = superseded_at
        summary["campaign"] = current
        return True

    superseded = bool(update_json_file(_summary_path(), mutate))
    if not superseded:
        return False
    autonomy_state.pop(EPOCH_ROUTE_REFRESH_STATE_KEY, None)
    autonomy_state.pop(EPOCH_ROUTE_SELECTION_STATE_KEY, None)
    append_workflow_activity(
        "campaign-epoch-route-superseded",
        f"Superseded fresh-epoch route {selected_route} with newer source work",
        campaign_id=str(campaign.get("campaign_id", "")),
        epoch=_positive_int(epoch, 1),
        route=selected_route,
        refresh_token=str(refresh_token or ""),
        target_symbol=str(target_symbol or ""),
        active_file=str(active_file or ""),
        reason=str(reason or ""),
    )
    return True


def pending_worker_refresh(*, campaign_id: str = "") -> dict[str, Any]:
    """Return the durable worker-refresh obligation for one live campaign."""
    campaign = campaign_snapshot()
    if campaign_id and str(campaign.get("campaign_id", "") or "") != str(campaign_id):
        return {}
    refresh = dict(campaign.get("epoch_worker_refresh") or {})
    return refresh if bool(refresh.get("pending")) else {}


def complete_worker_refresh(
    *,
    refresh_token: str,
    killed_job_ids: Sequence[str] = (),
) -> bool:
    """Clear one exact worker-refresh obligation after reconciliation succeeds."""
    completed_at = _now_iso()

    def mutate(summary: dict[str, Any]) -> bool:
        current = dict(summary.get("campaign") or {})
        refresh = dict(current.get("epoch_worker_refresh") or {})
        if (
            not bool(refresh.get("pending"))
            or not refresh_token
            or str(refresh.get("token", "") or "") != str(refresh_token)
        ):
            return False
        refresh.update(
            {
                "pending": False,
                "completed_at": completed_at,
                "killed_job_ids": sorted(
                    {str(job_id) for job_id in killed_job_ids if str(job_id).strip()}
                ),
            }
        )
        current["epoch_worker_refresh"] = refresh
        current["updated_at"] = completed_at
        summary["campaign"] = current
        return True

    return bool(update_json_file(_summary_path(), mutate))


def record_status(autonomy_state: dict[str, Any], status: str, *, reason: str = "") -> None:
    """Persist a campaign lifecycle status without changing mathematical truth."""
    campaign = ensure_campaign(autonomy_state)

    def mutate(summary: dict[str, Any]) -> None:
        current = dict(summary.get("campaign") or campaign)
        current["status"] = status
        current["updated_at"] = _now_iso()
        if status in {"verified", "disproved"}:
            current.pop(PLANNER_CAPACITY_RESERVATION_FIELD, None)
            current.pop(PROVIDER_USAGE_LIMIT_PAUSE_FIELD, None)
        if reason:
            current["status_reason"] = reason
        summary["campaign"] = current

    update_json_file(_summary_path(), mutate)
    autonomy_state["campaign_status"] = status
    if status in {"verified", "disproved"}:
        autonomy_state.pop(PLANNER_CAPACITY_RESERVATION_STATE_KEY, None)
        requested = autonomy_state.get("prover_requested_route")
        if isinstance(requested, Mapping) and (
            str(requested.get("route", "") or "").strip().lower() == "plan"
        ):
            autonomy_state.pop("prover_requested_route", None)


def record_provider_availability_probe_success(
    autonomy_state: dict[str, Any],
    *,
    provider: str,
) -> bool:
    """Resume a matching usage-limit pause after an authenticated provider call succeeds.

    Callers must invoke this only after the paused provider completed a fresh
    model turn. The transaction clears only the usage-limit-owned pause and
    restores any unrelated infrastructure pause that preceded it.
    """
    normalized_provider = str(provider or "").strip()
    if not normalized_provider:
        return False
    cleared: dict[str, Any] = {}

    def mutate(summary: dict[str, Any]) -> bool:
        current = dict(summary.get("campaign") or {})
        raw_pause = current.get(PROVIDER_USAGE_LIMIT_PAUSE_FIELD)
        if not isinstance(raw_pause, Mapping):
            return False
        paused_provider = str(raw_pause.get("provider", "") or "").strip()
        if paused_provider and paused_provider != normalized_provider:
            return False
        prior_status = str(raw_pause.get("prior_campaign_status", "") or "")
        prior_reason = str(raw_pause.get("prior_campaign_status_reason", "") or "")
        current.pop(PROVIDER_USAGE_LIMIT_PAUSE_FIELD, None)
        if prior_status == "paused_infrastructure":
            current["status"] = prior_status
            if prior_reason:
                current["status_reason"] = prior_reason
            else:
                current.pop("status_reason", None)
        else:
            current["status"] = "running"
            current.pop("status_reason", None)
        current["updated_at"] = _now_iso()
        summary["campaign"] = current
        cleared.update(
            {
                "campaign_id": str(current.get("campaign_id", "") or ""),
                "provider": normalized_provider,
                "restored_pause": prior_status == "paused_infrastructure",
            }
        )
        return True

    changed = bool(update_json_file(_summary_path(), mutate))
    if not changed:
        return False
    autonomy_state.pop("operational_pause", None)
    autonomy_state.pop("infrastructure_pause_reason", None)
    autonomy_state.pop("provider_retry_after", None)
    autonomy_state.pop("provider_pause_owner", None)
    autonomy_state["campaign_status"] = (
        "paused_infrastructure" if cleared.get("restored_pause") else "running"
    )
    with contextlib.suppress(Exception):
        append_workflow_activity(
            "provider-usage-limit-probe-recovered",
            "Fresh authenticated provider turn succeeded; resumed campaign admission",
            **cleared,
        )
    return True


def record_provider_usage_limit_pause(
    autonomy_state: dict[str, Any],
    retry_after: Mapping[str, Any],
    *,
    provider: str = "",
    base_url: str = "",
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Persist one exact reset-owned infrastructure pause.

    The campaign record is the cross-process authority. Only startup expiry
    reconciliation may clear it; ordinary resume logic cannot reinterpret an
    account-wide usage limit as a fresh mathematical turn.
    """
    now = float(time.time() if now_epoch is None else now_epoch)
    metadata = normalize_provider_retry_after(retry_after, now_epoch=now)
    if not metadata or now >= float(metadata["unavailable_until_epoch"]):
        raise ValueError("provider usage-limit pause requires an active bounded reset")
    # Read an already-started campaign without routing it through startup
    # resume reconciliation first. That reconciliation intentionally resumes
    # generic pauses, but this transaction must atomically remember any
    # unrelated infrastructure pause that preceded the provider reset.
    snapshot_campaign = read_json_file(_summary_path()).get("campaign")
    campaign = (
        dict(snapshot_campaign)
        if isinstance(snapshot_campaign, Mapping)
        and str(snapshot_campaign.get("campaign_id", "") or "").strip()
        else ensure_campaign(autonomy_state)
    )
    campaign_id = str(campaign.get("campaign_id", "") or "").strip()
    recorded_at = _now_iso()
    normalized_provider = str(provider or "unknown").strip()[:120] or "unknown"
    proposed_pause = {
        **metadata,
        "version": PROVIDER_USAGE_LIMIT_PAUSE_VERSION,
        "owner": PROVIDER_USAGE_LIMIT_PAUSE_OWNER,
        "provider": normalized_provider,
        "recorded_at": recorded_at,
    }
    # Base URL is intentionally not persisted. Provider identity is enough for
    # campaign admission, while omitting endpoint text keeps this operational
    # checkpoint on the same whitelisted-data footing as other activity state.
    _ = base_url

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        current = dict(summary.get("campaign") or campaign)
        if str(current.get("campaign_id", "") or "").strip() != campaign_id:
            raise RuntimeError("campaign changed while recording provider usage-limit pause")
        raw_existing_pause = current.get(PROVIDER_USAGE_LIMIT_PAUSE_FIELD)
        existing_pause = normalize_provider_retry_after(
            raw_existing_pause,
            now_epoch=now,
        )
        selected = dict(proposed_pause)
        if isinstance(raw_existing_pause, Mapping):
            # A later observation may extend the same provider outage. Carry
            # forward the pause that predated the first observation so reset
            # extension cannot erase unrelated infrastructure state.
            prior_status = str(raw_existing_pause.get("prior_campaign_status", "") or "")
            prior_reason = str(raw_existing_pause.get("prior_campaign_status_reason", "") or "")
            if prior_status:
                selected["prior_campaign_status"] = prior_status[:120]
            if prior_reason:
                selected["prior_campaign_status_reason"] = prior_reason[:1000]
        if (
            not existing_pause
            and raw_existing_pause is None
            and str(current.get("status", "") or "") == "paused_infrastructure"
        ):
            selected["prior_campaign_status"] = "paused_infrastructure"
            prior_reason = str(current.get("status_reason", "") or "")
            if prior_reason:
                selected["prior_campaign_status_reason"] = prior_reason[:1000]
        if existing_pause and int(existing_pause["unavailable_until_epoch"]) >= int(
            metadata["unavailable_until_epoch"]
        ):
            # Never shorten an account-wide reset because a slower worker
            # published an older observation after the foreground prover.
            selected = {
                **dict(raw_existing_pause or {}),
                **existing_pause,
                "version": PROVIDER_USAGE_LIMIT_PAUSE_VERSION,
                "owner": PROVIDER_USAGE_LIMIT_PAUSE_OWNER,
                "provider": str(
                    dict(raw_existing_pause or {}).get("provider", "") or normalized_provider
                )[:120],
                "recorded_at": str(
                    dict(raw_existing_pause or {}).get("recorded_at", "") or recorded_at
                ),
            }
        current[PROVIDER_USAGE_LIMIT_PAUSE_FIELD] = selected
        current["status"] = "paused_infrastructure"
        current["status_reason"] = (
            f"provider {selected['provider']} usage limit active until epoch "
            f"{selected['unavailable_until_epoch']}"
        )
        current["updated_at"] = recorded_at
        summary["campaign"] = current
        return selected

    persisted = dict(update_json_file(_summary_path(), mutate) or proposed_pause)
    persisted_metadata = normalize_provider_retry_after(persisted, now_epoch=now)
    persisted_provider = str(persisted.get("provider", "") or normalized_provider)
    reason = (
        f"provider {persisted_provider} usage limit active until epoch "
        f"{persisted_metadata['unavailable_until_epoch']}"
    )
    autonomy_state.update(
        {
            "campaign_id": campaign_id,
            "campaign_epoch": int(campaign.get("epoch", 1) or 1),
            "campaign_status": "paused_infrastructure",
            "operational_pause": "paused_infrastructure",
            "infrastructure_pause_reason": reason,
            "provider_retry_after": persisted_metadata,
            "provider_pause_owner": PROVIDER_USAGE_LIMIT_PAUSE_OWNER,
        }
    )
    append_workflow_activity(
        "provider-usage-limit-paused",
        "Paused campaign until the provider usage-limit reset",
        campaign_id=campaign_id,
        provider=persisted_provider,
        **persisted_metadata,
    )
    return persisted


def record_process_exit(
    autonomy_state: dict[str, Any],
    exit_code: int,
    *,
    verified: bool,
    reason: str = "",
) -> None:
    """Persist the process result independently from mathematical truth."""
    campaign = ensure_campaign(autonomy_state)

    def mutate(summary: dict[str, Any]) -> None:
        current = dict(summary.get("campaign") or campaign)
        current["last_exit_code"] = int(exit_code)
        current["last_exit_verified"] = bool(verified)
        current["last_exit_at"] = _now_iso()
        if exit_code == 0 and verified:
            current["status"] = "verified"
            current.pop(PLANNER_CAPACITY_RESERVATION_FIELD, None)
        elif exit_code == 3:
            current["status"] = "disproved"
            current.pop(PLANNER_CAPACITY_RESERVATION_FIELD, None)
        elif exit_code == 130:
            # A process signal pauses a resumable campaign; it does not cancel
            # the mathematical objective or discard the current epoch.
            current["status"] = "paused"
        elif exit_code == 2 and current.get("status") == "running":
            current["status"] = "paused"
        if reason:
            current["last_exit_reason"] = reason
        summary["campaign"] = current

    update_json_file(_summary_path(), mutate)
    if (exit_code == 0 and verified) or exit_code == 3:
        autonomy_state.pop(PLANNER_CAPACITY_RESERVATION_STATE_KEY, None)


def campaign_snapshot() -> dict[str, Any]:
    """Return the persisted campaign payload for status and tests."""
    return dict(read_json_file(_summary_path()).get("campaign") or {})
