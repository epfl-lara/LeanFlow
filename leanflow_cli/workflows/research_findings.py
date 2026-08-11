"""Select and render consumed background findings for research-mode prompts."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from leanflow_cli.workflows import (
    dispatch_ledger_compaction,
    research_delivery_gate,
    research_route_context,
)
from leanflow_cli.workflows.dispatch_models import SOURCE_REVISION_INPUT_KEY, LedgerEntry
from leanflow_cli.workflows.dispatch_service import (
    CHECKED_HELPER_STATUS,
    CHECKED_HELPERS_KEY,
    CHECKED_REPLACEMENT_TOOL,
    enforce_checked_replacement_contract,
)
from leanflow_cli.workflows.plan_state import Blueprint
from leanflow_cli.workflows.workflow_json_io import (
    read_json_file,
    update_json_file,
    update_json_file_if_changed,
)
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

DEFAULT_FINDING_LIMIT = 3
DEFAULT_PROMPT_CAP = 24_000
FOREGROUND_BATCH_FINDING_CAP = 3
FOREGROUND_PROMPT_HARD_CAP = 64_000
FOREGROUND_PAYLOAD_CAP = FOREGROUND_PROMPT_HARD_CAP - 1_000
DELIVERY_BACKLOG_CAP = 32
# A split child needs a bounded sample of its ancestors' evidence, not the
# ancestors' entire history. Keep one foreground batch visible while reserving
# the rest of the bounded window for research produced for the exact child.
INHERITED_DELIVERY_BACKLOG_CAP = FOREGROUND_BATCH_FINDING_CAP
# Delivered materializations are only a prompt cache over the lossless ledger.
# Keep their nominal history window aligned with the maximum delivery backlog;
# correctness-owned undelivered and quarantined findings may still exceed it.
DURABLE_FINDING_HISTORY_CAP = DELIVERY_BACKLOG_CAP
DELIVERY_STATE_KEY = "research_delivery_state"
DELIVERY_MARKER_KEYS = ("research_findings_delivered", "orchestrator_jobs_seen")
DELIVERY_MARKER_CAP = 5_000
DELIVERY_RECEIPTS_FILENAME = "research-delivery-receipts.json"
DELIVERY_RECEIPTS_VERSION = 2
DELIVERY_RECEIPT_ARCHIVE_KEY = "research_findings_delivered_archive"
CHUNK_TRANSFERS_KEY = "research_finding_chunk_transfers"
CHUNK_TRANSFER_CAP = 64
PENDING_FOREGROUND_KEY = "_research_findings_pending_foreground"
PENDING_FOREGROUND_CAP = 64
PENDING_FOREGROUND_TARGET_CAP = 2
OVERSIZED_DEFERRED_KEY = "_research_oversized_findings_deferred"
FINDING_MIGRATION_KEY = "research_finding_migration"
FINDING_ARCHIVE_VERSION = 3
FINDING_SUBSTANCE_VERSION = 2
DELIVERY_TOKEN_PREFIX = "leanflow-research-delivery:"
DELIVERY_TRANSFER_PREFIX = "leanflow-research-transfer:"

_EMPTY_FINDING_TEXT_RE = re.compile(
    r"^(?:none|n/?a|empty|done|complete(?:d)?|ok|success|"
    r"no\s+(?:new\s+|substantive\s+)?(?:finding|result|evidence|progress)s?)"
    r"[.!\s]*$",
    flags=re.IGNORECASE,
)
_ADMINISTRATIVE_FINDING_KEYS = frozenset(
    {
        "status",
        "reported_status",
        "checked_helper_status",
        "checked_replacement_status",
        "contract_violation",
        "parent_recheck_required",
    }
)
_CHECKED_HELPER_ROUTE_DISPOSITIONS = frozenset({"advance_current_route", "evidence_only"})


@dataclass(frozen=True)
class RelevantFindingsIndex:
    """Hold findings normalized from one authenticated summary snapshot.

    Building the index hydrates and integrity-checks every compact dispatch
    archive exactly once.  Callers may then project the same immutable
    snapshot onto several graph targets without reopening the archive ledger
    for every target.
    """

    findings: tuple[dict[str, Any], ...] = ()


_EVIDENCE_ONLY_RAW_KEYS = frozenset(
    {
        "active_file",
        "archive_result_sha256",
        "archetype",
        "campaign_id",
        "consumed_at",
        "semantic_novelty",
        "target_symbol",
    }
)
_EVIDENCE_ONLY_SAFE_KEYS = frozenset(
    {
        "check_scope",
        "parent_recheck_required",
        "verification_caveat",
        "verification_note",
    }
)
_EVIDENCE_ONLY_KEY_PARTS = (
    "blocker",
    "counterexample",
    "countermodel",
    "dead_end",
    "failure",
    "issue",
    "limitation",
    "missing",
    "non_coverage",
    "noncoverage",
    "non_exhaust",
    "nonexhaust",
    "obstruction",
    "risk",
    "surviv",
    "unsupported",
    "unresolved",
)
_EVIDENCE_ONLY_ACTION_KEY_PARTS = (
    "candidate",
    "code",
    "construction",
    "delta",
    "dependency",
    "discharge",
    "edit",
    "factorization",
    "helper",
    "identity",
    "insert",
    "name",
    "next_action",
    "outline",
    "proof",
    "recommend",
    "replacement",
    "route",
    "statement",
    "target",
)
_EVIDENCE_ONLY_ACTION_TEXT_RE = re.compile(
    r"(?:"
    r"\b(?:by_cases|exact|refine|apply|simpa|rw)\b"
    r"|\b(?:can|could|should|would|next)\b.{0,80}"
    r"\b(?:add|construct|cover|define|discharge|eliminate|handle|implement|insert|invoke|prove|remove|retry)\b"
    r"|\b(?:[A-Za-z_][A-Za-z0-9_']*|\d+)\s*%\s*\d+\s*=\s*\d+\b"
    r"|\btarget\s+integration\b"
    r")",
    flags=re.IGNORECASE | re.DOTALL,
)
_PARTIAL_COVERAGE_MIN_FAILED_SHAPES = 2
_PARTIAL_COVERAGE_FINGERPRINT_PREFIXES = ("congruence:", "witness:")
_PARTIAL_COVERAGE_UNIT_RE = re.compile(
    r"\b(?:only|merely|just)\b.{0,120}\b"
    r"(?:branch|case|class|family|residue|singleton|subcase|witness)\b",
    flags=re.IGNORECASE | re.DOTALL,
)
_PARTIAL_COVERAGE_SCOPE_RE = re.compile(
    r"(?:"
    r"\b[A-Za-z_][A-Za-z0-9_']*\s*(?:%\s*\d+\s*=|≡)\s*\d+\b"
    r"|\b(?:congruence|modular|residue|singleton|strict\s+subcase|witness)\b"
    r".{0,100}\b(?:branch|case|class|family|condition|dispatch|hypothesis)\b"
    r")",
    flags=re.IGNORECASE | re.DOTALL,
)
_PARTIAL_COVERAGE_GAP_RE = re.compile(
    r"(?:"
    r"\bdoes\s+not\s+(?:claim|close|establish|prove|show|solve|supply|complete|resolve|cover)\b"
    r".{0,180}\b(?:completion|complement|coverage|exhaustive|full|remaining|residual|target|terminal|universal)\b"
    r"|\bno\s+(?:proof[- ]?)?completion\s+(?:is\s+)?claim(?:ed)?\b"
    r"|\bnot\s+(?:a|the)\s+proof[- ]completion\s+claim\b"
    r"|\b(?:non[- ]?exhaustive|remaining\s+(?:branch|case|class|family|residue|target))\b"
    r"|\b(?:global|residual|remaining|terminal)\s+"
    r"(?:complement|coverage|dispatch|goal|obligation|target)\b"
    r".{0,120}\b(?:remain(?:s|ed)?|unresolved|open|not\s+closed)\b"
    r")",
    flags=re.IGNORECASE | re.DOTALL,
)
_EVIDENCE_ONLY_SEMANTIC_KEYS = frozenset(
    {
        "classification",
        "has_checked_helper",
        "malformed",
        "progress_anchor_eligible",
        "progress_anchor_reason",
        "subsumed_by_job_ids",
        "version",
    }
)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _summary_path():
    return workflow_state_root() / "summary.json"


def _stable_payload_sha256(value: Any) -> str:
    """Return a deterministic digest for JSON-owned workflow evidence."""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _streaming_payload_sha256(value: Any) -> str:
    """Return a stable digest without materializing a second full JSON payload."""
    digest = sha256()
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def _marker_values(value: Any) -> set[str]:
    """Return non-empty persisted delivery markers from one JSON value."""
    return set(_marker_sequence(value))


def _marker_sequence(value: Any) -> tuple[str, ...]:
    """Return persisted markers in deterministic oldest-to-newest order.

    A repeated marker moves to its last position. This makes the sequence a
    compact recency log while retaining exact set semantics for callers that
    only need membership.
    """
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    ordered: dict[str, None] = {}
    for item in value:
        if not isinstance(item, str) or not item:
            continue
        ordered.pop(item, None)
        ordered[item] = None
    return tuple(ordered)


def _bounded_marker_append(
    existing: Any,
    incoming: Any,
) -> list[str]:
    """Append markers and retain the newest entries at the configured cap.

    Sorting a marker set before slicing makes lexical order masquerade as
    recency and can discard a newly acknowledged marker immediately. Preserve
    persisted order instead, moving every incoming duplicate to the newest
    edge before evicting old hot-history entries.
    """
    ordered = _ordered_marker_append(existing, incoming)
    cap = max(0, int(DELIVERY_MARKER_CAP))
    if not cap:
        return []
    return ordered[-cap:]


def _ordered_marker_append(existing: Any, incoming: Any) -> list[str]:
    """Append and deduplicate marker histories without bounding them."""
    ordered = dict.fromkeys(_marker_sequence(existing))
    for marker in _marker_sequence(incoming):
        ordered.pop(marker, None)
        ordered[marker] = None
    return list(ordered)


def _delivery_receipts_path(campaign_id: str = "") -> Path:
    """Return the campaign-isolated write-ahead receipt path.

    The empty-id form names the legacy single-campaign file and exists only
    for migration. New writes use the full campaign-id digest so overlapping
    campaigns cannot clear or overwrite each other's acknowledgements.
    """
    normalized_campaign = str(campaign_id or "")
    if not normalized_campaign:
        return workflow_state_root() / DELIVERY_RECEIPTS_FILENAME
    digest = sha256(normalized_campaign.encode("utf-8")).hexdigest()
    return workflow_state_root() / f"research-delivery-receipts-{digest}.json"


def _read_delivery_receipt_payload(campaign_id: str) -> dict[str, Any]:
    """Read a campaign receipt, falling back to its matching legacy payload."""
    normalized_campaign = str(campaign_id or "")
    if not normalized_campaign:
        return {}
    payload = read_json_file(_delivery_receipts_path(normalized_campaign))
    if payload:
        return payload
    legacy = read_json_file(_delivery_receipts_path())
    if str(legacy.get("campaign_id", "") or "") == normalized_campaign:
        return legacy
    return {}


def _summary_campaign_conflicts(summary: Mapping[str, Any], campaign_id: str) -> bool:
    """Return whether top-level durable campaign identity rejects this writer."""
    campaign = summary.get("campaign")
    if not isinstance(campaign, Mapping):
        return False
    durable_id = str(campaign.get("campaign_id", "") or "")
    return bool(durable_id and durable_id != str(campaign_id or ""))


def _same_campaign_summary_delivery_sequence(
    campaign_id: str,
    summary: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return the summary hot window only when both campaign authorities agree."""
    normalized_campaign = str(campaign_id or "")
    if not normalized_campaign:
        return ()
    state = dict(summary if summary is not None else read_json_file(_summary_path()))
    if _summary_campaign_conflicts(state, normalized_campaign):
        return ()
    persisted = dict(state.get(DELIVERY_STATE_KEY) or {})
    if str(persisted.get("campaign_id", "") or "") != normalized_campaign:
        return ()
    return _marker_sequence(persisted.get("research_findings_delivered"))


def _delivery_receipt_markers(
    campaign_id: str,
    payload: Mapping[str, Any] | None = None,
) -> set[str]:
    """Return acknowledged pair markers from the campaign receipt sidecar."""
    normalized_campaign = str(campaign_id or "")
    if not normalized_campaign:
        return set()
    state = dict(
        payload if payload is not None else _read_delivery_receipt_payload(normalized_campaign)
    )
    if str(state.get("campaign_id", "") or "") != normalized_campaign:
        return set()
    return _marker_values(state.get("research_findings_delivered")) | _marker_values(
        state.get(DELIVERY_RECEIPT_ARCHIVE_KEY)
    )


def _delivery_receipt_sequence(
    campaign_id: str,
    payload: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return acknowledged pairs in receipt commit order for one campaign."""
    normalized_campaign = str(campaign_id or "")
    if not normalized_campaign:
        return ()
    state = dict(
        payload if payload is not None else _read_delivery_receipt_payload(normalized_campaign)
    )
    if str(state.get("campaign_id", "") or "") != normalized_campaign:
        return ()
    archived = _marker_sequence(state.get(DELIVERY_RECEIPT_ARCHIVE_KEY))
    if archived:
        return archived
    return _marker_sequence(state.get("research_findings_delivered"))


def _delivery_receipt_hot_sequence(
    campaign_id: str,
    payload: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return the bounded receipt window mirrored into process and summary state."""
    normalized_campaign = str(campaign_id or "")
    if not normalized_campaign:
        return ()
    state = dict(
        payload if payload is not None else _read_delivery_receipt_payload(normalized_campaign)
    )
    if str(state.get("campaign_id", "") or "") != normalized_campaign:
        return ()
    return _marker_sequence(state.get("research_findings_delivered"))


def _persist_delivery_receipts(
    *,
    campaign_id: str,
    markers: Sequence[str],
    seed_markers: Sequence[str] = (),
) -> tuple[str, ...]:
    """Write acknowledged foreground pairs before updating shared summary state.

    ``summary.json`` has many cooperating writers. This isolated receipt is a
    write-ahead authority: if a stale summary snapshot or an in-process state
    replacement regresses its mirrored marker list, the next foreground tick
    can still recover the acknowledgement without showing the same finding to
    the prover again.
    """
    normalized_campaign = str(campaign_id or "")
    incoming = _marker_sequence(markers)
    summary_seed = _marker_sequence(seed_markers)
    if not normalized_campaign or (not incoming and not summary_seed):
        return ()
    payload_seed = _read_delivery_receipt_payload(normalized_campaign)

    def mutate(payload: dict[str, Any]) -> tuple[str, ...]:
        if not payload and payload_seed:
            payload.update(payload_seed)
        existing_campaign = str(payload.get("campaign_id", "") or "")
        if existing_campaign and existing_campaign != normalized_campaign:
            raise RuntimeError("campaign receipt identity mismatch")
        if not existing_campaign:
            payload.update(
                {
                    "version": DELIVERY_RECEIPTS_VERSION,
                    "campaign_id": normalized_campaign,
                    "research_findings_delivered": [],
                    DELIVERY_RECEIPT_ARCHIVE_KEY: [],
                }
            )
        # Copy the summary hot window into exact cold storage before adding a
        # new acknowledgement. Existing sidecar markers come afterward in the
        # hot ordering because they may be crash receipts not mirrored into
        # the older summary snapshot yet.
        archived = _ordered_marker_append(
            payload.get(DELIVERY_RECEIPT_ARCHIVE_KEY),
            summary_seed,
        )
        archived = _ordered_marker_append(
            archived,
            payload.get("research_findings_delivered"),
        )
        archived = _ordered_marker_append(archived, incoming)
        stored = _bounded_marker_append(
            summary_seed,
            payload.get("research_findings_delivered"),
        )
        stored = _bounded_marker_append(stored, incoming)
        payload["version"] = DELIVERY_RECEIPTS_VERSION
        payload["campaign_id"] = normalized_campaign
        payload["research_findings_delivered"] = stored
        # The dispatch ledger can rematerialize a finding long after its hot
        # marker ages out. Keep an exact cold pair tombstone for the lifetime
        # of the campaign; hot eviction is safe only after this same atomic
        # sidecar transaction records the pair here.
        payload[DELIVERY_RECEIPT_ARCHIVE_KEY] = archived
        payload["updated_at"] = _now_iso()
        return tuple(stored)

    return tuple(update_json_file(_delivery_receipts_path(normalized_campaign), mutate))


def _chunk_transfer_records(value: Any) -> dict[str, dict[str, Any]]:
    """Return validated prefix receipts for oversized foreground transfers."""
    if not isinstance(value, Mapping):
        return {}
    records: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_value, Mapping):
            continue
        transfer_id = str(raw_key or raw_value.get("transfer_id", "") or "")
        payload_sha256 = str(raw_value.get("payload_sha256", "") or "")
        markers = sorted(_marker_values(raw_value.get("markers")))
        try:
            chunk_count = int(raw_value.get("chunk_count", 0) or 0)
            next_index = int(raw_value.get("next_index", 0) or 0)
            novelty_version = int(raw_value.get("semantic_novelty_version", 0) or 0)
        except (TypeError, ValueError):
            continue
        if (
            not transfer_id.startswith(DELIVERY_TRANSFER_PREFIX)
            or len(payload_sha256) != 64
            or not markers
            or chunk_count < 2
            or not 0 <= next_index < chunk_count
            or novelty_version != research_route_context.SEMANTIC_NOVELTY_VERSION
        ):
            continue
        records[transfer_id] = {
            "transfer_id": transfer_id,
            "target_symbol": str(raw_value.get("target_symbol", "") or ""),
            "markers": markers,
            "payload_sha256": payload_sha256,
            "chunk_count": chunk_count,
            "next_index": next_index,
            "semantic_novelty_version": novelty_version,
            "yield_once": bool(raw_value.get("yield_once")),
            "updated_at": str(raw_value.get("updated_at", "") or ""),
        }
    return records


def _merge_chunk_transfer_records(
    local: Any,
    persisted: Any,
) -> dict[str, dict[str, Any]]:
    """Merge durable transfer receipts without moving a prefix backwards."""
    merged = _chunk_transfer_records(local)
    for transfer_id, durable in _chunk_transfer_records(persisted).items():
        current = merged.get(transfer_id)
        if current is None or int(durable["next_index"]) >= int(current["next_index"]):
            merged[transfer_id] = durable
    ordered = sorted(
        merged.values(),
        key=lambda record: (str(record.get("updated_at", "")), str(record["transfer_id"])),
    )[-CHUNK_TRANSFER_CAP:]
    return {str(record["transfer_id"]): record for record in ordered}


def _prune_persisted_chunk_transfers(
    *,
    campaign_id: str,
    transfer_ids: Sequence[str],
) -> None:
    """Remove completed transfer prefixes left behind by a stale summary."""
    completed = {str(item) for item in transfer_ids if isinstance(item, str) and item}
    if not campaign_id or not completed:
        return

    def mutate(summary: dict[str, Any]) -> None:
        if _summary_campaign_conflicts(summary, campaign_id):
            return
        persisted = dict(summary.get(DELIVERY_STATE_KEY) or {})
        if str(persisted.get("campaign_id", "") or "") != campaign_id:
            return
        transfers = _chunk_transfer_records(persisted.get(CHUNK_TRANSFERS_KEY))
        for transfer_id in completed:
            transfers.pop(transfer_id, None)
        if transfers:
            persisted[CHUNK_TRANSFERS_KEY] = transfers
        else:
            persisted.pop(CHUNK_TRANSFERS_KEY, None)
        persisted["updated_at"] = _now_iso()
        summary[DELIVERY_STATE_KEY] = persisted

    update_json_file(_summary_path(), mutate)


def hydrate_delivery_markers(
    autonomy_state: dict[str, Any],
    summary: Mapping[str, Any] | None = None,
) -> bool:
    """Restore campaign-scoped finding delivery markers after state replacement."""
    campaign_id = str(autonomy_state.get("campaign_id", "") or "")
    if not campaign_id:
        return False
    summary_state = dict(summary if summary is not None else read_json_file(_summary_path()))
    persisted = dict(summary_state.get(DELIVERY_STATE_KEY) or {})
    persisted_matches = not _summary_campaign_conflicts(summary_state, campaign_id) and (
        str(persisted.get("campaign_id", "") or "") == campaign_id
    )
    summary_hot = _same_campaign_summary_delivery_sequence(campaign_id, summary_state)
    receipt_payload = _read_delivery_receipt_payload(campaign_id)
    receipts = _delivery_receipt_markers(campaign_id, receipt_payload)
    changed = False
    if summary_hot and not set(summary_hot).issubset(receipts):
        # Upgrade pre-sidecar acknowledgements before a later hot-window write
        # can evict them. The campaign checks above prevent cross-run seeding.
        _persist_delivery_receipts(
            campaign_id=campaign_id,
            markers=(),
            seed_markers=summary_hot,
        )
        receipt_payload = _read_delivery_receipt_payload(campaign_id)
        receipts = _delivery_receipt_markers(campaign_id, receipt_payload)
        changed = True
    receipt_sequence = _delivery_receipt_sequence(campaign_id, receipt_payload)
    receipt_hot_sequence = _delivery_receipt_hot_sequence(campaign_id, receipt_payload)
    for key in DELIVERY_MARKER_KEYS:
        durable = _marker_sequence(persisted.get(key)) if persisted_matches else ()
        merged = _bounded_marker_append(autonomy_state.get(key), durable)
        if key == "research_findings_delivered":
            merged = _bounded_marker_append(merged, receipt_sequence)
        if tuple(merged) != _marker_sequence(autonomy_state.get(key)):
            changed = True
        if merged:
            autonomy_state[key] = merged
    transfers = _merge_chunk_transfer_records(
        autonomy_state.get(CHUNK_TRANSFERS_KEY),
        persisted.get(CHUNK_TRANSFERS_KEY) if persisted_matches else None,
    )
    all_delivered = receipts | (
        _marker_values(persisted.get("research_findings_delivered")) if persisted_matches else set()
    )
    completed_transfer_ids = [
        transfer_id
        for transfer_id, record in transfers.items()
        if _marker_values(record.get("markers")).issubset(all_delivered)
    ]
    for transfer_id in completed_transfer_ids:
        transfers.pop(transfer_id, None)
    if completed_transfer_ids:
        _prune_persisted_chunk_transfers(
            campaign_id=campaign_id,
            transfer_ids=completed_transfer_ids,
        )
        changed = True
    if transfers != _chunk_transfer_records(autonomy_state.get(CHUNK_TRANSFERS_KEY)):
        changed = True
    if transfers:
        autonomy_state[CHUNK_TRANSFERS_KEY] = transfers
    else:
        autonomy_state.pop(CHUNK_TRANSFERS_KEY, None)
    delivered = all_delivered | _marker_values(autonomy_state.get("research_findings_delivered"))
    if delivered:
        records = _pending_foreground_records(autonomy_state)
        retained = [
            record
            for record in records
            if not _marker_values(record.get("markers")).issubset(delivered)
        ]
        if len(retained) != len(records):
            changed = True
        if retained:
            autonomy_state[PENDING_FOREGROUND_KEY] = retained
        else:
            autonomy_state.pop(PENDING_FOREGROUND_KEY, None)
    mirrored = _marker_values(persisted.get("research_findings_delivered"))
    receipt_hot = set(receipt_hot_sequence)
    if receipt_hot and (not persisted_matches or not receipt_hot.issubset(mirrored)):
        # Repair the shared summary mirror after a stale writer/state snapshot
        # regressed it. The isolated write-ahead receipt remains authoritative.
        persist_delivery_markers(
            autonomy_state,
            key="research_findings_delivered",
            markers=receipt_hot_sequence,
        )
        changed = True
    return changed


def persist_delivery_markers(
    autonomy_state: dict[str, Any],
    *,
    key: str,
    markers: Sequence[str],
) -> tuple[str, ...]:
    """Merge and durably checkpoint one campaign-scoped marker collection."""
    if key not in DELIVERY_MARKER_KEYS:
        raise ValueError(f"unsupported research delivery marker key: {key}")
    campaign_id = str(autonomy_state.get("campaign_id", "") or "")
    incoming = _marker_sequence(markers)
    receipt_sequence: tuple[str, ...] = ()
    if key == "research_findings_delivered" and campaign_id and incoming:
        summary_hot = _same_campaign_summary_delivery_sequence(campaign_id)
        receipt_sequence = _persist_delivery_receipts(
            campaign_id=campaign_id,
            markers=incoming,
            seed_markers=summary_hot,
        )
    current = _bounded_marker_append(autonomy_state.get(key), receipt_sequence)
    current = _bounded_marker_append(current, incoming)
    if current:
        autonomy_state[key] = current
    if not campaign_id:
        return tuple(autonomy_state.get(key) or ())

    def mutate(summary: dict[str, Any]) -> tuple[str, ...]:
        if _summary_campaign_conflicts(summary, campaign_id):
            return tuple(current)
        persisted = dict(summary.get(DELIVERY_STATE_KEY) or {})
        if str(persisted.get("campaign_id", "") or "") != campaign_id:
            persisted = {"campaign_id": campaign_id}
        bounded = _bounded_marker_append(persisted.get(key), current)
        bounded = _bounded_marker_append(bounded, incoming)
        persisted[key] = bounded
        persisted["updated_at"] = _now_iso()
        summary[DELIVERY_STATE_KEY] = persisted
        if key == "research_findings_delivered":
            compact_durable_findings(summary)
        return tuple(bounded)

    stored = update_json_file(_summary_path(), mutate)
    autonomy_state[key] = _bounded_marker_append(stored, incoming)
    return stored


def _persist_chunk_transfer(
    autonomy_state: dict[str, Any],
    record: Mapping[str, Any],
) -> None:
    """Checkpoint the next required chunk for one incomplete transfer."""
    transfer_id = str(record.get("transfer_id", "") or "")
    normalized = _chunk_transfer_records({transfer_id: record}).get(transfer_id)
    if normalized is None:
        raise ValueError("invalid oversized research transfer receipt")
    transfers = _merge_chunk_transfer_records(
        autonomy_state.get(CHUNK_TRANSFERS_KEY),
        {transfer_id: normalized},
    )
    campaign_id = str(autonomy_state.get("campaign_id", "") or "")
    if not campaign_id:
        autonomy_state[CHUNK_TRANSFERS_KEY] = transfers
        return

    def mutate(summary: dict[str, Any]) -> None:
        if _summary_campaign_conflicts(summary, campaign_id):
            return
        persisted = dict(summary.get(DELIVERY_STATE_KEY) or {})
        if str(persisted.get("campaign_id", "") or "") != campaign_id:
            persisted = {"campaign_id": campaign_id}
        persisted[CHUNK_TRANSFERS_KEY] = _merge_chunk_transfer_records(
            persisted.get(CHUNK_TRANSFERS_KEY),
            {transfer_id: normalized},
        )
        persisted["updated_at"] = _now_iso()
        summary[DELIVERY_STATE_KEY] = persisted

    update_json_file(_summary_path(), mutate)
    autonomy_state[CHUNK_TRANSFERS_KEY] = transfers


def _finish_chunk_transfer(
    autonomy_state: dict[str, Any],
    *,
    transfer_id: str,
    markers: Sequence[str],
) -> tuple[str, ...]:
    """Atomically persist a complete transfer marker and remove its receipt."""
    completed = _marker_sequence(markers)
    campaign_id = str(autonomy_state.get("campaign_id", "") or "")
    receipt_sequence: tuple[str, ...] = ()
    if campaign_id and completed:
        # Commit the acknowledgement to the isolated authority before the
        # shared summary transaction. A crash or stale summary writer after
        # the final chunk must not resurrect the completed transfer.
        receipt_sequence = _persist_delivery_receipts(
            campaign_id=campaign_id,
            markers=completed,
            seed_markers=_same_campaign_summary_delivery_sequence(campaign_id),
        )
    current = _bounded_marker_append(
        autonomy_state.get("research_findings_delivered"),
        receipt_sequence,
    )
    bounded = _bounded_marker_append(current, completed)
    if campaign_id:

        def mutate(summary: dict[str, Any]) -> tuple[str, ...]:
            if _summary_campaign_conflicts(summary, campaign_id):
                return tuple(bounded)
            persisted = dict(summary.get(DELIVERY_STATE_KEY) or {})
            if str(persisted.get("campaign_id", "") or "") != campaign_id:
                persisted = {"campaign_id": campaign_id}
            stored = _bounded_marker_append(
                persisted.get("research_findings_delivered"),
                bounded,
            )
            stored = _bounded_marker_append(stored, completed)
            persisted["research_findings_delivered"] = stored
            transfers = _chunk_transfer_records(persisted.get(CHUNK_TRANSFERS_KEY))
            transfers.pop(transfer_id, None)
            if transfers:
                persisted[CHUNK_TRANSFERS_KEY] = transfers
            else:
                persisted.pop(CHUNK_TRANSFERS_KEY, None)
            persisted["updated_at"] = _now_iso()
            summary[DELIVERY_STATE_KEY] = persisted
            compact_durable_findings(summary)
            return tuple(stored)

        stored = update_json_file(_summary_path(), mutate)
        autonomy_state["research_findings_delivered"] = list(stored)
    elif bounded:
        autonomy_state["research_findings_delivered"] = bounded
    transfers = _chunk_transfer_records(autonomy_state.get(CHUNK_TRANSFERS_KEY))
    transfers.pop(transfer_id, None)
    if transfers:
        autonomy_state[CHUNK_TRANSFERS_KEY] = transfers
    else:
        autonomy_state.pop(CHUNK_TRANSFERS_KEY, None)
    return completed


def _same_file(left: str, right: str) -> bool:
    """Return whether two persisted paths identify the same file spelling."""
    if not left or not right:
        return left == right
    project_root = str(os.getenv("LEANFLOW_PROJECT_ROOT", "") or os.getcwd())

    def canonical(path: str) -> str:
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(project_root, expanded)
        return os.path.realpath(expanded)

    return canonical(left) == canonical(right)


def related_target_symbols(
    blueprint: Blueprint | None,
    *,
    target_symbol: str,
    active_file: str,
) -> tuple[str, ...]:
    """Return the active target followed by its same-file split ancestors.

    Decomposition edges point from the generated child to its parent target.
    Findings attached to that parent remain mathematically relevant to the
    generated helper, while findings for unrelated declarations in the same
    source file do not.
    """
    target = str(target_symbol or "").strip()
    if not target:
        return ()
    related = [target]
    if blueprint is None:
        return tuple(related)
    nodes_by_id = {node.id: node for node in blueprint.nodes}
    pending = [
        node.id
        for node in blueprint.nodes
        if node.name == target and (not active_file or _same_file(node.file, active_file))
    ]
    seen_ids = set(pending)
    seen_names = {target}
    split_parents: dict[str, list[str]] = {}
    for edge in blueprint.edges:
        if edge.kind == "split_of":
            split_parents.setdefault(edge.source, []).append(edge.target)
    while pending:
        child_id = pending.pop(0)
        for parent_id in split_parents.get(child_id, ()):
            if parent_id in seen_ids:
                continue
            seen_ids.add(parent_id)
            parent = nodes_by_id.get(parent_id)
            if parent is None or (active_file and not _same_file(parent.file, active_file)):
                continue
            pending.append(parent_id)
            if parent.name and parent.name not in seen_names:
                seen_names.add(parent.name)
                related.append(parent.name)
    return tuple(related)


def delivery_key(job_id: str, target_symbol: str) -> str:
    """Return the stable one-shot key for one finding and queue target."""
    return json.dumps(
        [str(job_id or ""), str(target_symbol or "")],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def durable_delivery_markers(summary: Mapping[str, Any] | None) -> set[str]:
    """Return the durable foreground-delivery markers in one summary."""
    state = dict(summary or {})
    persisted = dict(state.get(DELIVERY_STATE_KEY) or {})
    delivery_campaign_id = str(persisted.get("campaign_id", "") or "")
    campaign = state.get("campaign")
    top_campaign_id = (
        str(campaign.get("campaign_id", "") or "") if isinstance(campaign, Mapping) else ""
    )
    campaign_id = top_campaign_id or delivery_campaign_id
    hot = (
        _marker_values(persisted.get("research_findings_delivered"))
        if not top_campaign_id or delivery_campaign_id == top_campaign_id
        else set()
    )
    return hot | (_delivery_receipt_markers(campaign_id) if campaign_id else set())


def _campaign_id_from_spec(spec: Mapping[str, Any]) -> str:
    """Recover a campaign id from one exact parent-owned dispatch spec."""
    inputs = dict(spec.get("inputs") or {})
    explicit = str(inputs.get("campaign_id", "") or "").strip()
    if explicit:
        return explicit
    job_id = str(spec.get("job_id", "") or "").strip()
    requester_role = str(spec.get("requester_role", "") or "").strip()
    parent_job_id = str(spec.get("parent_job_id", "") or "").strip()
    parent_suffix = f".{requester_role}" if requester_role else ""
    if (
        parent_suffix
        and parent_job_id.endswith(parent_suffix)
        and job_id.startswith(parent_job_id + ".")
    ):
        return parent_job_id[: -len(parent_suffix)]
    marker = f".{requester_role}." if requester_role else ""
    if marker and marker in job_id:
        return job_id.rsplit(marker, 1)[0]
    return ""


def _finding_artifact_paths(
    result: Mapping[str, Any],
    deliverable: Mapping[str, Any],
) -> list[str]:
    """Return explicit artifacts or structured scratch files as a fallback."""
    raw = result.get("artifact_paths") or deliverable.get("files_modified") or []
    if isinstance(raw, str):
        candidates: Sequence[Any] = (raw,)
    elif isinstance(raw, Sequence):
        candidates = raw
    else:
        candidates = ()
    paths: list[str] = []
    for value in candidates:
        path = str(value or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _finding_record_base(
    entry: LedgerEntry,
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], LedgerEntry]:
    """Build normalized finding evidence without semantic-history traversal."""
    deliverable = dict(result.get("deliverable") or {})
    if entry.spec.archetype == "deep_search" or entry.spec.deliverable == "findings_report":
        deliverable = enforce_checked_replacement_contract(
            deliverable,
            expected_target_symbol=str(entry.spec.inputs.get("target_symbol", "") or ""),
        )
    normalized_result = dict(result)
    normalized_result["deliverable"] = deliverable
    normalized_entry = replace(entry, result=normalized_result)
    return (
        {
            "job_id": entry.spec.job_id,
            "campaign_id": str(entry.spec.inputs.get("campaign_id", "") or "")
            or _campaign_id_from_spec(entry.spec.to_mapping()),
            "archetype": entry.spec.archetype,
            "objective": entry.spec.objective,
            "target_symbol": str(entry.spec.inputs.get("target_symbol", "") or ""),
            "active_file": str(entry.spec.inputs.get("active_file", "") or ""),
            "source_revision_sha256": str(
                entry.spec.inputs.get(SOURCE_REVISION_INPUT_KEY, "") or ""
            ),
            "deliverable": deliverable,
            "artifact_paths": _finding_artifact_paths(result, deliverable),
            "plan_delta": list(result.get("plan_delta") or []),
            "archive_result_sha256": _stable_payload_sha256(dict(result)),
        },
        normalized_entry,
    )


def _classify_finding_novelty(
    entry: LedgerEntry,
    entries: Sequence[LedgerEntry],
    *,
    semantic_evidence_cache: (
        MutableMapping[int, tuple[LedgerEntry, research_route_context.SemanticEvidence]] | None
    ) = None,
    semantic_novelty_cache: MutableMapping[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Classify one normalized finding once within a migration transaction."""
    cache_key = (entry.spec.job_id, _stable_payload_sha256(dict(entry.result)))
    if semantic_novelty_cache is not None:
        cached = semantic_novelty_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
    novelty = research_route_context.classify_semantic_novelty(
        entry,
        entries,
        evidence_cache=semantic_evidence_cache,
    )
    if semantic_novelty_cache is not None:
        semantic_novelty_cache[cache_key] = dict(novelty)
    return novelty


def build_finding_record(
    entry: LedgerEntry,
    result: Mapping[str, Any],
    *,
    entries: Sequence[LedgerEntry] = (),
    semantic_evidence_cache: (
        MutableMapping[int, tuple[LedgerEntry, research_route_context.SemanticEvidence]] | None
    ) = None,
    semantic_novelty_cache: MutableMapping[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the canonical durable finding for one terminal ledger result.

    Normal portfolio harvesting and startup recovery share this builder so
    checked-replacement downgrades, assignment provenance, and semantic
    novelty cannot diverge across the crash boundary.
    """
    base, normalized_entry = _finding_record_base(entry, result)
    classification_entries = tuple(
        normalized_entry if candidate.spec.job_id == entry.spec.job_id else candidate
        for candidate in (entries or (entry,))
    )
    return {
        **base,
        "semantic_novelty": _classify_finding_novelty(
            normalized_entry,
            classification_entries,
            semantic_evidence_cache=semantic_evidence_cache,
            semantic_novelty_cache=semantic_novelty_cache,
        ),
        "consumed_at": _now_iso(),
    }


_MATERIALIZED_EVIDENCE_KEYS = (
    "job_id",
    "campaign_id",
    "archetype",
    "objective",
    "target_symbol",
    "active_file",
    "source_revision_sha256",
    "deliverable",
    "artifact_paths",
    "plan_delta",
)


def _materialized_evidence_sha256(finding: Mapping[str, Any]) -> str:
    """Return the digest of evidence duplicated outside the dispatch ledger."""
    return _stable_payload_sha256({key: finding.get(key) for key in _MATERIALIZED_EVIDENCE_KEYS})


def _archive_record(
    entry: LedgerEntry,
    *,
    status: str,
    materialized_for_target: str = "",
    reason: str = "",
    substantive: bool | None = None,
) -> dict[str, Any]:
    """Build one lightweight pointer into the lossless dispatch-ledger payload."""
    canonical, _normalized_entry = _finding_record_base(entry, entry.result)
    record: dict[str, Any] = {
        "job_id": entry.spec.job_id,
        "campaign_id": str(canonical.get("campaign_id", "") or ""),
        "target_symbol": str(canonical.get("target_symbol", "") or ""),
        "active_file": str(canonical.get("active_file", "") or ""),
        "result_sha256": str(canonical.get("archive_result_sha256", "") or ""),
        "materialized_evidence_sha256": _materialized_evidence_sha256(canonical),
        "status": str(status or "archived_available"),
    }
    if substantive is not None:
        record["substantive"] = substantive
        record["substance_version"] = FINDING_SUBSTANCE_VERSION
    if materialized_for_target:
        record["materialized_for_target"] = materialized_for_target
    if reason:
        record["reason"] = reason
    return record


def _archived_substantive_decision(
    record: Mapping[str, Any] | None,
    *,
    result_sha256: str,
) -> bool | None:
    """Return a reusable versioned substance decision for an unchanged result."""
    if not isinstance(record, Mapping):
        return None
    try:
        version = int(record.get("substance_version", 0) or 0)
    except (TypeError, ValueError):
        return None
    substantive = record.get("substantive")
    if (
        version != FINDING_SUBSTANCE_VERSION
        or str(record.get("result_sha256", "") or "") != result_sha256
        or not isinstance(substantive, bool)
    ):
        return None
    return substantive


def _set_archive_record(
    records: dict[str, dict[str, Any]],
    job_id: str,
    record: Mapping[str, Any],
    *,
    now: str,
) -> bool:
    """Replace one archive record only when its durable meaning changed."""
    normalized = dict(record)
    previous = dict(records.get(job_id) or {})
    previous.pop("updated_at", None)
    if previous == normalized:
        return False
    normalized["updated_at"] = now
    records[job_id] = normalized
    return True


_MIGRATION_REPORT_ONLY_KEYS = frozenset(
    {
        "last_report",
        "reconstructed",
        "reconstructed_job_ids",
        "updated_at",
    }
)


def _migration_state_semantics(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return durable migration state without observation-only timestamps.

    ``last_report`` and the reconstructed aliases describe the most recent
    invocation, not archive authority. Record timestamps likewise document
    when a semantic transition happened; the record fields themselves define
    that transition.
    """
    state = {
        key: item
        for key, item in dict(value or {}).items()
        if key not in _MIGRATION_REPORT_ONLY_KEYS
    }
    raw_records = state.get("records")
    if isinstance(raw_records, Mapping):
        state["records"] = {
            str(job_id): (
                {key: item for key, item in dict(record).items() if key != "updated_at"}
                if isinstance(record, Mapping)
                else record
            )
            for job_id, record in raw_records.items()
        }
    return state


def _finding_matches_archived_entry(
    finding: Mapping[str, Any],
    entry: LedgerEntry,
    *,
    entries: Sequence[LedgerEntry],
    semantic_evidence_cache: (
        MutableMapping[int, tuple[LedgerEntry, research_route_context.SemanticEvidence]] | None
    ) = None,
    semantic_novelty_cache: MutableMapping[tuple[str, str], dict[str, Any]] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate and canonically upgrade one materialized ledger copy.

    Checked-replacement policy can become stricter after a finding was first
    harvested. Reapplying the current deterministic contract to the stored
    deliverable distinguishes that expected normalization drift from an actual
    payload mismatch; every other evidence field must still match the exact
    consumed ledger result.
    """
    base, normalized_entry = _finding_record_base(entry, entry.result)
    raw_stored_novelty = finding.get("semantic_novelty")
    stored_novelty = dict(raw_stored_novelty) if isinstance(raw_stored_novelty, Mapping) else {}
    consumed_at = str(finding.get("consumed_at", "") or "") or _now_iso()
    canonical = {
        **base,
        "semantic_novelty": stored_novelty,
        "consumed_at": consumed_at,
    }
    stored_result_sha256 = str(finding.get("archive_result_sha256", "") or "")
    canonical_result_sha256 = str(canonical.get("archive_result_sha256", "") or "")
    if stored_result_sha256 and stored_result_sha256 != canonical_result_sha256:
        return False, "archive_result_hash_mismatch", canonical
    normalized = dict(finding)
    if entry.spec.archetype == "deep_search" or entry.spec.deliverable == "findings_report":
        raw_deliverable = normalized.get("deliverable")
        deliverable = dict(raw_deliverable) if isinstance(raw_deliverable, Mapping) else {}
        normalized["deliverable"] = enforce_checked_replacement_contract(
            deliverable,
            expected_target_symbol=str(entry.spec.inputs.get("target_symbol", "") or ""),
        )
    _canonicalize_pre_compaction_objective(
        normalized,
        entry=entry,
        canonical_objective=str(canonical.get("objective", "") or ""),
    )
    if _materialized_evidence_sha256(normalized) != _materialized_evidence_sha256(canonical):
        return False, "materialized_evidence_hash_mismatch", canonical
    try:
        novelty_version = int(stored_novelty.get("version", 0) or 0)
    except (TypeError, ValueError):
        novelty_version = 0
    if novelty_version != research_route_context.SEMANTIC_NOVELTY_VERSION:
        classification_entries = tuple(
            normalized_entry if candidate.spec.job_id == entry.spec.job_id else candidate
            for candidate in (entries or (entry,))
        )
        canonical["semantic_novelty"] = _classify_finding_novelty(
            normalized_entry,
            classification_entries,
            semantic_evidence_cache=semantic_evidence_cache,
            semantic_novelty_cache=semantic_novelty_cache,
        )
    return True, "", canonical


def _canonicalize_pre_compaction_objective(
    finding: dict[str, Any],
    *,
    entry: LedgerEntry,
    canonical_objective: str,
) -> None:
    """Canonicalize an authenticated objective copied before ledger compaction.

    Terminal-ledger compaction removes rendered route history after a finding
    may already have materialized it. Trust that older copy only when the
    parent-owned digest authenticates its exact bytes and stripping its route
    context produces the ledger's compact objective. Any disagreement remains
    visible to the ordinary materialized-evidence quarantine gate.
    """
    stored_objective = finding.get("objective")
    expected_digest = entry.spec.inputs.get(dispatch_ledger_compaction.OBJECTIVE_SHA256_INPUT_KEY)
    if (
        not isinstance(stored_objective, str)
        or stored_objective == canonical_objective
        or not isinstance(expected_digest, str)
        or sha256(stored_objective.encode("utf-8")).hexdigest() != expected_digest
        or research_route_context.semantic_worker_objective(stored_objective) != canonical_objective
    ):
        return
    finding["objective"] = canonical_objective


def _payload_has_substance(value: Any, *, key: str = "") -> bool:
    """Return whether a deliverable value carries non-administrative evidence."""
    if key in _ADMINISTRATIVE_FINDING_KEYS:
        return False
    if isinstance(value, Mapping):
        return any(
            _payload_has_substance(item, key=str(item_key))
            for item_key, item in value.items()
            if str(item_key) != research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_payload_has_substance(item) for item in value)
    if isinstance(value, str):
        text = " ".join(value.split())
        return bool(text) and _EMPTY_FINDING_TEXT_RE.fullmatch(text) is None
    return value is not None and value is not False


def _migration_entry_is_substantive(
    entry: LedgerEntry,
    *,
    entries: Sequence[LedgerEntry],
    semantic_evidence_cache: (
        MutableMapping[int, tuple[LedgerEntry, research_route_context.SemanticEvidence]] | None
    ) = None,
) -> bool:
    """Return whether a consumed result is safe to resurrect as evidence."""
    raw = entry.result.get("deliverable")
    if not isinstance(raw, Mapping):
        return False
    deliverable = research_route_context.strip_parent_route_context(raw)
    if not deliverable or not _payload_has_substance(deliverable):
        return False
    return not research_route_context.semantic_result_is_operational_error(
        entry,
        evidence_cache=semantic_evidence_cache,
    )


def recover_finding_provenance(summary: dict[str, Any]) -> int:
    """Fill missing finding assignment provenance from the exact ledger spec.

    Recovery is deliberately job-id exact. Ambiguous or missing legacy ledger
    records remain unacknowledged instead of being guessed into a target.
    """
    specs: dict[str, dict[str, Any]] = {}
    for raw in dispatch_ledger_compaction.hydrate_dispatch_ledger(
        summary.get("dispatch_ledger") or [],
        state_root=workflow_state_root(),
    ):
        raw_spec = raw.get("spec")
        if not isinstance(raw_spec, Mapping):
            continue
        spec = dict(raw_spec)
        job_id = str(spec.get("job_id", "") or "")
        if job_id:
            specs[job_id] = spec
    recovered = 0
    findings: list[dict[str, Any]] = []
    for raw in summary.get("research_findings") or []:
        if not isinstance(raw, Mapping):
            continue
        finding = dict(raw)
        spec = specs.get(str(finding.get("job_id", "") or ""), {})
        inputs = dict(spec.get("inputs") or {})
        additions = {
            "target_symbol": str(inputs.get("target_symbol", "") or ""),
            "active_file": str(inputs.get("active_file", "") or ""),
            "campaign_id": _campaign_id_from_spec(spec),
            "source_revision_sha256": str(inputs.get(SOURCE_REVISION_INPUT_KEY, "") or ""),
        }
        for key, value in additions.items():
            if value and not str(finding.get(key, "") or ""):
                finding[key] = value
                recovered += 1
        findings.append(finding)
    summary["research_findings"] = findings
    return recovered


def _finding_delivered_to_origin(
    finding: Mapping[str, Any],
    delivered: set[str],
) -> bool:
    """Return whether one finding reached the target that produced it."""
    target_symbol = str(finding.get("target_symbol", "") or "")
    if not target_symbol:
        # Missing legacy provenance is not evidence of delivery. Retain it.
        return False
    return was_delivered(
        finding,
        target_symbol=target_symbol,
        delivered=delivered,
    )


def compact_durable_findings(
    summary: dict[str, Any],
    *,
    protected_job_ids: Iterable[str] = (),
) -> int:
    """Retain undelivered/quarantined findings plus newest delivered history.

    The history cap applies only to acknowledged prompt-cache copies. An
    unacknowledged or quarantined finding is correctness state and may exceed
    it; the portfolio launch gate separately bounds the undelivered portion
    while the foreground provider is unavailable.
    """
    recover_finding_provenance(summary)
    raw_findings = summary.get("research_findings") or []
    findings = [dict(item) for item in raw_findings if isinstance(item, Mapping)]
    if not findings:
        summary["research_findings"] = []
        return 0
    delivered = durable_delivery_markers(summary)
    protected = {
        str(job_id) for job_id in protected_job_ids if isinstance(job_id, str) and job_id
    } | _quarantined_archive_job_ids(summary)
    undelivered_indices: set[int] = set()
    for index, finding in enumerate(findings):
        # Pair-scoped markers are the sole acknowledgement authority. Do not
        # collapse them into a global boolean: a child delivery must not hide
        # an origin obligation if that parent is later reopened.
        finding.pop("delivery_acknowledged", None)
        if str(finding.get("job_id", "") or "") in protected or not (
            _finding_delivered_to_origin(finding, delivered)
        ):
            undelivered_indices.add(index)
    delivered_indices = [
        index for index in range(len(findings)) if index not in undelivered_indices
    ]
    retained_delivered = set(delivered_indices[-DURABLE_FINDING_HISTORY_CAP:])
    summary["research_findings"] = [
        finding
        for index, finding in enumerate(findings)
        if index in undelivered_indices or index in retained_delivered
    ]
    return len(undelivered_indices)


def migrate_consumed_findings_for_assignment(
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    blueprint: Blueprint | None = None,
) -> dict[str, Any]:
    """Page archived ledger evidence into the active target's delivery cache.

    The consumed dispatch ledger is the lossless payload authority;
    ``research_findings`` is only a target-scoped materialization. One atomic
    summary transaction defers inactive unacknowledged copies when their exact
    ledger payload is intact, then fills the active target and its same-file
    split ancestors up to the delivery cap. Pair-scoped markers remain the
    only acknowledgement authority.
    """
    normalized_campaign = str(campaign_id or "").strip()
    normalized_target = str(target_symbol or "").strip()
    normalized_file = str(active_file or "").strip()
    empty_report: dict[str, Any] = {
        "campaign_id": normalized_campaign,
        "target_symbol": normalized_target,
        "active_file": normalized_file,
        "related_target_symbols": [],
        "materialized": 0,
        "materialized_job_ids": [],
        "dematerialized": 0,
        "dematerialized_job_ids": [],
        "deferred_capacity": 0,
        "quarantined": 0,
        "active_delivery_backlog": 0,
        "archive_records": 0,
        "archive_updates": 0,
        "state_changed": False,
        # Compatibility aliases retained for existing activity consumers.
        "reconstructed": 0,
        "reconstructed_job_ids": [],
        "already_present": 0,
        "already_delivered": 0,
        "skipped_non_substantive": 0,
        "invalid_ledger_records": 0,
    }
    if not normalized_campaign or not normalized_target or not normalized_file:
        return empty_report

    def mutate(summary: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        report: dict[str, Any] = dict(empty_report)
        now = _now_iso()
        original_findings_sha256 = _streaming_payload_sha256(summary.get("research_findings"))
        related_targets = set(
            related_target_symbols(
                blueprint,
                target_symbol=normalized_target,
                active_file=normalized_file,
            )
        )
        report["related_target_symbols"] = sorted(related_targets)
        entries: list[LedgerEntry] = []
        malformed_records: list[tuple[str, Mapping[str, Any]]] = []
        raw_ledger = summary.get("dispatch_ledger") or []
        hydrated_ledger = dispatch_ledger_compaction.hydrate_dispatch_ledger(
            raw_ledger,
            state_root=workflow_state_root(),
        )
        report["invalid_ledger_records"] += sum(
            1 for raw in raw_ledger if not isinstance(raw, Mapping)
        )
        for index, raw in enumerate(hydrated_ledger):
            try:
                entries.append(LedgerEntry.from_mapping(raw))
            except (TypeError, ValueError):
                report["invalid_ledger_records"] += 1
                malformed_records.append((f"invalid-ledger-{index:06d}", raw))

        entries_by_id: dict[str, LedgerEntry] = {}
        duplicate_job_ids: set[str] = set()
        for entry in entries:
            job_id = entry.spec.job_id
            if not job_id:
                report["invalid_ledger_records"] += 1
                continue
            if job_id in entries_by_id:
                duplicate_job_ids.add(job_id)
                continue
            entries_by_id[job_id] = entry

        prior_index = dict(summary.get(FINDING_MIGRATION_KEY) or {})
        prior_records = prior_index.get("records")
        records = {
            str(job_id): dict(raw)
            for job_id, raw in (prior_records.items() if isinstance(prior_records, Mapping) else ())
            if str(job_id) and isinstance(raw, Mapping)
        }
        prior_record_semantics = {
            job_id: {key: value for key, value in record.items() if key != "updated_at"}
            for job_id, record in records.items()
        }

        def entry_campaign(entry: LedgerEntry) -> str:
            inputs = dict(entry.spec.inputs or {})
            return str(inputs.get("campaign_id", "") or "").strip() or (
                _campaign_id_from_spec(entry.spec.to_mapping())
            )

        semantic_evidence_cache: dict[
            int, tuple[LedgerEntry, research_route_context.SemanticEvidence]
        ] = {}
        semantic_novelty_cache: dict[tuple[str, str], dict[str, Any]] = {}
        substantive_by_job_id: dict[str, bool] = {}

        def archive_record(
            entry: LedgerEntry,
            *,
            status: str,
            materialized_for_target: str = "",
            reason: str = "",
        ) -> dict[str, Any]:
            """Build an archive record carrying this scan's substance decision."""
            return _archive_record(
                entry,
                status=status,
                materialized_for_target=materialized_for_target,
                reason=reason,
                substantive=substantive_by_job_id.get(entry.spec.job_id),
            )

        campaign_entries: list[LedgerEntry] = []
        substantive_job_ids: set[str] = set()
        for entry in entries:
            job_id = entry.spec.job_id
            if (
                not job_id
                or entry_campaign(entry) != normalized_campaign
                or not entry.consumed
                or entry.state != "done"
            ):
                continue
            campaign_entries.append(entry)
            inputs = dict(entry.spec.inputs or {})
            target = str(inputs.get("target_symbol", "") or "")
            source_file = str(inputs.get("active_file", "") or "")
            status = "archived_available"
            reason = ""
            if job_id in duplicate_job_ids:
                status = "quarantined_duplicate_ledger"
                reason = "multiple dispatch-ledger rows share this job id"
            elif not target or not source_file:
                status = "quarantined_missing_provenance"
                reason = "consumed ledger result lacks exact target/file provenance"
            else:
                result_sha256 = _stable_payload_sha256(dict(entry.result))
                substantive = _archived_substantive_decision(
                    records.get(job_id),
                    result_sha256=result_sha256,
                )
                if substantive is None:
                    substantive = _migration_entry_is_substantive(
                        entry,
                        entries=entries,
                        semantic_evidence_cache=semantic_evidence_cache,
                    )
                substantive_by_job_id[job_id] = substantive
                if substantive:
                    substantive_job_ids.add(job_id)
                else:
                    status = "archived_non_substantive"
                    reason = "ledger result has no mathematical evidence"
                    report["skipped_non_substantive"] += 1
            _set_archive_record(
                records,
                job_id,
                archive_record(entry, status=status, reason=reason),
                now=now,
            )

        for synthetic_id, malformed_raw in malformed_records:
            _set_archive_record(
                records,
                synthetic_id,
                {
                    "job_id": synthetic_id,
                    "campaign_id": normalized_campaign,
                    "result_sha256": _stable_payload_sha256(dict(malformed_raw)),
                    "status": "quarantined_malformed_ledger",
                    "reason": "dispatch-ledger row could not be decoded",
                },
                now=now,
            )

        recover_finding_provenance(summary)
        findings = [
            dict(item)
            for item in (summary.get("research_findings") or [])
            if isinstance(item, Mapping)
        ]
        delivered = durable_delivery_markers(summary)
        retained: list[dict[str, Any]] = []
        dematerialized: list[str] = []
        quarantined_job_ids: set[str] = set()
        active_due_kept = 0
        inherited_due_kept = 0
        for index, finding in enumerate(findings):
            job_id = str(finding.get("job_id", "") or "")
            finding_campaign = str(finding.get("campaign_id", "") or "")
            if finding_campaign != normalized_campaign:
                retained.append(finding)
                continue
            matched_entry = entries_by_id.get(job_id)
            quarantine_reason = ""
            canonical_finding: dict[str, Any] | None = None
            if not job_id:
                quarantine_reason = "materialized finding lacks a job id"
            elif matched_entry is None:
                quarantine_reason = "no exact dispatch-ledger result exists"
            elif job_id in duplicate_job_ids:
                quarantine_reason = "dispatch-ledger job id is ambiguous"
            elif (
                not matched_entry.consumed
                or matched_entry.state != "done"
                or entry_campaign(matched_entry) != normalized_campaign
            ):
                quarantine_reason = "dispatch-ledger result is not consumed done evidence"
            else:
                matches, mismatch_reason, canonical_finding = _finding_matches_archived_entry(
                    finding,
                    matched_entry,
                    entries=entries,
                    semantic_evidence_cache=semantic_evidence_cache,
                    semantic_novelty_cache=semantic_novelty_cache,
                )
                if not matches:
                    quarantine_reason = mismatch_reason

            if quarantine_reason:
                quarantine_key = job_id or f"invalid-finding-{index:06d}"
                quarantined_job_ids.add(quarantine_key)
                if matched_entry is not None:
                    record = archive_record(
                        matched_entry,
                        status=f"quarantined_{quarantine_reason}",
                        reason=quarantine_reason.replace("_", " "),
                    )
                else:
                    record = {
                        "job_id": quarantine_key,
                        "campaign_id": normalized_campaign,
                        "target_symbol": str(finding.get("target_symbol", "") or ""),
                        "active_file": str(finding.get("active_file", "") or ""),
                        "materialized_evidence_sha256": _materialized_evidence_sha256(finding),
                        "status": "quarantined_missing_ledger",
                        "reason": quarantine_reason,
                    }
                _set_archive_record(records, quarantine_key, record, now=now)
                retained.append(finding)
                continue

            assert matched_entry is not None
            assert canonical_finding is not None
            finding = canonical_finding
            finding_target = str(finding.get("target_symbol", "") or "")
            finding_file = str(finding.get("active_file", "") or "")
            active_related = finding_target in related_targets and _same_file(
                finding_file,
                normalized_file,
            )
            delivered_to_active = active_related and was_delivered(
                finding,
                target_symbol=normalized_target,
                delivered=delivered,
            )
            if not active_related and not _finding_delivered_to_origin(finding, delivered):
                dematerialized.append(job_id)
                _set_archive_record(
                    records,
                    job_id,
                    archive_record(
                        matched_entry,
                        status="deferred_inactive",
                        reason="origin is not due to the active delivery target",
                    ),
                    now=now,
                )
                continue

            inherited = active_related and finding_target != normalized_target
            if inherited and delivered_to_active:
                # The pair-scoped receipt and lossless ledger are sufficient.
                # Keeping this duplicate materialized until the parent reopens
                # would let a long split campaign grow the prompt cache again.
                dematerialized.append(job_id)
                _set_archive_record(
                    records,
                    job_id,
                    archive_record(
                        matched_entry,
                        status="archived_delivered_current",
                        materialized_for_target=normalized_target,
                    ),
                    now=now,
                )
                continue

            delivery_due = active_related and not delivered_to_active
            inherited_window_full = bool(
                inherited and inherited_due_kept >= INHERITED_DELIVERY_BACKLOG_CAP
            )
            if delivery_due and (active_due_kept >= DELIVERY_BACKLOG_CAP or inherited_window_full):
                dematerialized.append(job_id)
                _set_archive_record(
                    records,
                    job_id,
                    archive_record(
                        matched_entry,
                        status="deferred_capacity",
                        materialized_for_target=normalized_target,
                        reason=(
                            "inherited delivery window is at capacity"
                            if inherited_window_full
                            else "active delivery backlog is at capacity"
                        ),
                    ),
                    now=now,
                )
                continue
            if delivery_due:
                active_due_kept += 1
                if inherited:
                    inherited_due_kept += 1

            status = "materialized_history"
            materialized_for = ""
            if active_related:
                materialized_for = normalized_target
                status = (
                    "materialized_delivered_current"
                    if was_delivered(
                        finding,
                        target_symbol=normalized_target,
                        delivered=delivered,
                    )
                    else "materialized_current"
                )
            _set_archive_record(
                records,
                job_id,
                archive_record(
                    matched_entry,
                    status=status,
                    materialized_for_target=materialized_for,
                ),
                now=now,
            )
            retained.append(finding)

        findings = retained
        existing_job_ids = {str(finding.get("job_id", "") or "") for finding in findings}
        preexisting_active_count = sum(
            1
            for finding in findings
            if str(finding.get("campaign_id", "") or "") == normalized_campaign
            and str(finding.get("target_symbol", "") or "") in related_targets
            and _same_file(str(finding.get("active_file", "") or ""), normalized_file)
        )
        active_due = sum(
            1
            for finding in findings
            if str(finding.get("campaign_id", "") or "") == normalized_campaign
            and str(finding.get("target_symbol", "") or "") in related_targets
            and _same_file(str(finding.get("active_file", "") or ""), normalized_file)
            and str(finding.get("job_id", "") or "") not in quarantined_job_ids
            and not was_delivered(
                finding,
                target_symbol=normalized_target,
                delivered=delivered,
            )
        )
        candidates: list[LedgerEntry] = []
        for entry in campaign_entries:
            job_id = entry.spec.job_id
            inputs = dict(entry.spec.inputs or {})
            origin_target = str(inputs.get("target_symbol", "") or "")
            origin_file = str(inputs.get("active_file", "") or "")
            if (
                job_id in existing_job_ids
                or job_id not in substantive_job_ids
                or job_id in duplicate_job_ids
                or origin_target not in related_targets
                or not _same_file(origin_file, normalized_file)
            ):
                continue
            if was_delivered(
                {"job_id": job_id, "target_symbol": origin_target},
                target_symbol=normalized_target,
                delivered=delivered,
            ):
                report["already_delivered"] += 1
                _set_archive_record(
                    records,
                    job_id,
                    archive_record(
                        entry,
                        status="archived_delivered_current",
                        materialized_for_target=normalized_target,
                    ),
                    now=now,
                )
                continue
            candidates.append(entry)

        candidates.sort(
            key=lambda candidate: (
                (
                    0
                    if str(candidate.spec.inputs.get("target_symbol", "") or "")
                    == normalized_target
                    else 1
                ),
                candidate.created_at or candidate.finished_at or "",
                candidate.finished_at or "",
                candidate.spec.job_id,
            )
        )
        materialized: list[str] = []
        deferred_capacity = 0
        inherited_due = sum(
            1
            for finding in findings
            if str(finding.get("campaign_id", "") or "") == normalized_campaign
            and str(finding.get("target_symbol", "") or "") in related_targets
            and str(finding.get("target_symbol", "") or "") != normalized_target
            and _same_file(str(finding.get("active_file", "") or ""), normalized_file)
            and str(finding.get("job_id", "") or "") not in quarantined_job_ids
            and not was_delivered(
                finding,
                target_symbol=normalized_target,
                delivered=delivered,
            )
        )
        for entry in candidates:
            job_id = entry.spec.job_id
            origin_target = str(entry.spec.inputs.get("target_symbol", "") or "")
            inherited = origin_target != normalized_target
            inherited_window_full = bool(
                inherited and inherited_due >= INHERITED_DELIVERY_BACKLOG_CAP
            )
            if active_due >= DELIVERY_BACKLOG_CAP or inherited_window_full:
                deferred_capacity += 1
                _set_archive_record(
                    records,
                    job_id,
                    archive_record(
                        entry,
                        status="deferred_capacity",
                        materialized_for_target=normalized_target,
                        reason=(
                            "inherited delivery window is at capacity"
                            if inherited_window_full
                            else "active delivery backlog is at capacity"
                        ),
                    ),
                    now=now,
                )
                continue
            finding = build_finding_record(
                entry,
                entry.result,
                entries=entries,
                semantic_evidence_cache=semantic_evidence_cache,
                semantic_novelty_cache=semantic_novelty_cache,
            )
            findings.append(finding)
            existing_job_ids.add(job_id)
            materialized.append(job_id)
            active_due += 1
            if inherited:
                inherited_due += 1
            _set_archive_record(
                records,
                job_id,
                archive_record(
                    entry,
                    status="materialized_current",
                    materialized_for_target=normalized_target,
                ),
                now=now,
            )

        summary["research_findings"] = findings
        compact_durable_findings(summary, protected_job_ids=quarantined_job_ids)
        report["materialized"] = len(materialized)
        report["materialized_job_ids"] = materialized
        report["reconstructed"] = len(materialized)
        report["reconstructed_job_ids"] = materialized
        report["dematerialized"] = len(dematerialized)
        report["dematerialized_job_ids"] = dematerialized
        report["deferred_capacity"] = deferred_capacity
        report["quarantined"] = len(quarantined_job_ids) + len(malformed_records)
        report["active_delivery_backlog"] = active_due
        report["archive_records"] = len(records)
        final_record_semantics = {
            job_id: {key: value for key, value in record.items() if key != "updated_at"}
            for job_id, record in records.items()
        }
        report["archive_updates"] = sum(
            1
            for job_id in prior_record_semantics.keys() | final_record_semantics.keys()
            if prior_record_semantics.get(job_id) != final_record_semantics.get(job_id)
        )
        report["already_present"] = preexisting_active_count
        next_migration = {
            "version": FINDING_ARCHIVE_VERSION,
            "campaign_id": normalized_campaign,
            "active_target_symbol": normalized_target,
            "active_file": normalized_file,
            "related_target_symbols": sorted(related_targets),
            "active_delivery_backlog": report["active_delivery_backlog"],
            "reconstructed": report["reconstructed"],
            "reconstructed_job_ids": report["reconstructed_job_ids"],
            "records": records,
            "last_report": {
                key: value
                for key, value in report.items()
                if key not in {"campaign_id", "target_symbol", "active_file"}
            },
            "updated_at": now,
        }
        findings_changed = (
            _streaming_payload_sha256(summary.get("research_findings")) != original_findings_sha256
        )
        migration_changed = _migration_state_semantics(prior_index) != _migration_state_semantics(
            next_migration
        )
        state_changed = findings_changed or migration_changed
        report["state_changed"] = state_changed
        next_migration["last_report"] = {
            key: value
            for key, value in report.items()
            if key not in {"campaign_id", "target_symbol", "active_file"}
        }
        if state_changed:
            summary[FINDING_MIGRATION_KEY] = next_migration
        return report, state_changed

    return dict(update_json_file_if_changed(_summary_path(), mutate))


def delivery_backlog_count(
    summary: Mapping[str, Any] | None,
    *,
    target_symbol: str,
    active_file: str,
) -> int:
    """Return undelivered durable findings for one exact research assignment."""
    state = dict(summary or {})
    recover_finding_provenance(state)
    delivered = durable_delivery_markers(state)
    count = 0
    for raw in state.get("research_findings") or []:
        if not isinstance(raw, Mapping):
            continue
        finding = dict(raw)
        finding_target = str(finding.get("target_symbol", "") or "")
        finding_file = str(finding.get("active_file", "") or "")
        if target_symbol and finding_target != target_symbol:
            continue
        if active_file and not _same_file(finding_file, active_file):
            continue
        if not _finding_delivered_to_origin(finding, delivered):
            count += 1
    return count


def _quarantined_archive_job_ids(summary: Mapping[str, Any]) -> set[str]:
    """Return materialized job ids withheld by archive-integrity checks."""
    migration = dict(summary.get(FINDING_MIGRATION_KEY) or {})
    raw_records = migration.get("records")
    if not isinstance(raw_records, Mapping):
        return set()
    return {
        str(job_id)
        for job_id, raw in raw_records.items()
        if isinstance(raw, Mapping) and str(raw.get("status", "") or "").startswith("quarantined_")
    }


def active_delivery_backlog_count(
    summary: Mapping[str, Any] | None,
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    blueprint: Blueprint | None = None,
) -> int:
    """Return evidence currently due to one foreground delivery target.

    The cap is campaign-owned but the foreground consumer is target-scoped.
    Inactive origin obligations stay as ledger archive pointers and therefore
    cannot occupy all capacity while a different theorem is being proved.
    """
    counts = active_delivery_backlog_counts(
        summary,
        campaign_id=campaign_id,
        target_symbol=target_symbol,
        active_file=active_file,
        blueprint=blueprint,
    )
    return counts["total"]


def active_delivery_backlog_counts(
    summary: Mapping[str, Any] | None,
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    blueprint: Blueprint | None = None,
) -> dict[str, int]:
    """Return exact-target and inherited undelivered cache counts."""
    state = dict(summary or {})
    recover_finding_provenance(state)
    delivered = durable_delivery_markers(state)
    quarantined = _quarantined_archive_job_ids(state)
    normalized_campaign = str(campaign_id or "").strip()
    normalized_target = str(target_symbol or "").strip()
    normalized_file = str(active_file or "").strip()
    related_targets = set(
        related_target_symbols(
            blueprint,
            target_symbol=normalized_target,
            active_file=normalized_file,
        )
    )
    exact = 0
    inherited = 0
    for raw in state.get("research_findings") or []:
        if not isinstance(raw, Mapping):
            continue
        finding = dict(raw)
        job_id = str(finding.get("job_id", "") or "")
        finding_campaign = str(finding.get("campaign_id", "") or "").strip()
        belongs = (
            not normalized_campaign
            or finding_campaign == normalized_campaign
            or (not finding_campaign and job_id.startswith(normalized_campaign + "."))
        )
        if not belongs or job_id in quarantined:
            continue
        if str(finding.get("target_symbol", "") or "") not in related_targets:
            continue
        if normalized_file and not _same_file(
            str(finding.get("active_file", "") or ""),
            normalized_file,
        ):
            continue
        if not was_delivered(
            finding,
            target_symbol=normalized_target,
            delivered=delivered,
        ):
            if str(finding.get("target_symbol", "") or "") == normalized_target:
                exact += 1
            else:
                inherited += 1
    return {"exact": exact, "inherited": inherited, "total": exact + inherited}


def campaign_delivery_backlog_count(
    summary: Mapping[str, Any] | None,
    *,
    campaign_id: str,
    target_symbol: str = "",
    active_file: str = "",
    blueprint: Blueprint | None = None,
) -> int:
    """Return campaign-owned evidence due to the active foreground target.

    Callers that lack an active assignment retain the legacy global count for
    diagnostics. Production portfolio maintenance always supplies the target
    and file, making the campaign cap safe across scope transitions.
    """
    if target_symbol and active_file:
        return active_delivery_backlog_count(
            summary,
            campaign_id=campaign_id,
            target_symbol=target_symbol,
            active_file=active_file,
            blueprint=blueprint,
        )
    state = dict(summary or {})
    recover_finding_provenance(state)
    delivered = durable_delivery_markers(state)
    normalized_campaign = str(campaign_id or "").strip()
    count = 0
    for raw in state.get("research_findings") or []:
        if not isinstance(raw, Mapping):
            continue
        finding = dict(raw)
        finding_campaign = str(finding.get("campaign_id", "") or "").strip()
        job_id = str(finding.get("job_id", "") or "")
        belongs = (
            not normalized_campaign
            or finding_campaign == normalized_campaign
            or (not finding_campaign and job_id.startswith(normalized_campaign + "."))
        )
        if belongs and not _finding_delivered_to_origin(finding, delivered):
            count += 1
    return count


def _pending_foreground_records(autonomy_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return well-formed process-local foreground delivery records.

    Admission is bounded in ``stage_foreground_delivery``. Never slice this
    collection while reading it: doing so would silently discard evidence
    whose durable acknowledgement has not happened yet.
    """
    raw = autonomy_state.get(PENDING_FOREGROUND_KEY)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    records: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        record = dict(item)
        try:
            novelty_version = int(record.get("semantic_novelty_version", 0) or 0)
        except (TypeError, ValueError):
            continue
        if novelty_version != research_route_context.SEMANTIC_NOVELTY_VERSION:
            # Old prompts may embed a finding that the current deterministic
            # classifier now treats as evidence-only. Drop process-local
            # staging; the durable ledger remains available for canonical
            # reclassification and safe redelivery.
            continue
        token = str(record.get("token", "") or "")
        prompt = str(record.get("prompt", "") or "")
        markers = sorted(_marker_values(record.get("markers")))
        if not token.startswith(DELIVERY_TOKEN_PREFIX) or not prompt or not markers:
            continue
        record["token"] = token
        record["prompt"] = prompt
        record["markers"] = markers
        record["target_symbol"] = str(record.get("target_symbol", "") or "")
        record["semantic_novelty_version"] = novelty_version
        transfer_id = str(record.get("transfer_id", "") or "")
        if transfer_id.startswith(DELIVERY_TRANSFER_PREFIX):
            try:
                chunk_index = int(record.get("chunk_index", -1))
                chunk_count = int(record.get("chunk_count", 0))
            except (TypeError, ValueError):
                continue
            if not 0 <= chunk_index < chunk_count:
                continue
            record["transfer_id"] = transfer_id
            record["chunk_index"] = chunk_index
            record["chunk_count"] = chunk_count
            record["payload_sha256"] = str(record.get("payload_sha256", "") or "")
        records.append(record)
    return records


def pending_foreground_markers(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
) -> set[str]:
    """Return in-flight finding markers for one foreground queue target."""
    return {
        marker
        for record in _pending_foreground_records(autonomy_state)
        if str(record.get("target_symbol", "") or "") == target_symbol
        for marker in record["markers"]
    }


def retain_foreground_target(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
) -> int:
    """De-stage records from assignments that are no longer foreground.

    This never acknowledges or deletes the durable finding. If the queue
    later returns to the old target, normal fresh-finding selection reconstructs
    the handoff. Explicit target cleanup keeps a long decomposition campaign
    from accumulating two process-local prompt copies per visited theorem.
    """
    records = _pending_foreground_records(autonomy_state)
    kept = [
        record
        for record in records
        if str(record.get("target_symbol", "") or "") == str(target_symbol or "")
    ]
    removed = len(records) - len(kept)
    if kept:
        autonomy_state[PENDING_FOREGROUND_KEY] = kept
    else:
        autonomy_state.pop(PENDING_FOREGROUND_KEY, None)
    return removed


def _delivery_token(identity: str) -> str:
    """Return a stable tagged-prompt token for one delivery identity."""
    return DELIVERY_TOKEN_PREFIX + sha256(identity.encode("utf-8")).hexdigest()


def _chunk_transfer_id(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    markers: Sequence[str],
    payload_sha256: str,
) -> str:
    """Return the deterministic identity for one exact oversized payload."""
    identity = json.dumps(
        [
            str(autonomy_state.get("campaign_id", "") or ""),
            str(target_symbol or ""),
            *sorted(_marker_values(markers)),
            payload_sha256,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return DELIVERY_TRANSFER_PREFIX + sha256(identity.encode("utf-8")).hexdigest()


def _render_delivery_chunk(
    *,
    transfer_id: str,
    payload_sha256: str,
    chunk_index: int,
    chunk_count: int,
    segment: str,
) -> str:
    """Render one self-describing exact JSON-string segment."""
    token = _delivery_token(f"{transfer_id}:{chunk_index}")
    body = {
        "protocol": "leanflow-research-finding-chunks-v1",
        "transfer_id": transfer_id,
        "payload_sha256": payload_sha256,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "segment_sha256": sha256(segment.encode("utf-8")).hexdigest(),
        "segment_encoding": "json-string",
        "segment": segment,
        "instruction": (
            "Retain this decoded segment exactly. After receiving every chunk, concatenate "
            "segments by zero-based chunk_index and consume the reconstructed research prompt. "
            "Do not omit or rewrite checked_replacements or unchecked_replacements."
        ),
    }
    rendered = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"[LEANFLOW RESEARCH DELIVERY TOKEN: {token}]\n{rendered}"


def _split_delivery_prompt(
    prompt: str,
    *,
    transfer_id: str,
    payload_sha256: str,
) -> tuple[str, ...]:
    """Split one prompt into deterministic chunks under the hard prompt cap."""
    text = str(prompt)
    if not text:
        return ()
    segments: list[str] = []
    start = 0
    # chunk_count can never exceed the number of characters. Using that value
    # while sizing reserves at least as many index digits as the final header.
    sizing_index = max(2, len(text))
    while start < len(text):
        low = start + 1
        high = len(text)
        accepted = start
        while low <= high:
            middle = (low + high) // 2
            candidate = text[start:middle]
            rendered = _render_delivery_chunk(
                transfer_id=transfer_id,
                payload_sha256=payload_sha256,
                chunk_index=sizing_index,
                chunk_count=sizing_index,
                segment=candidate,
            )
            if len(rendered) <= FOREGROUND_PROMPT_HARD_CAP:
                accepted = middle
                low = middle + 1
            else:
                high = middle - 1
        if accepted == start:
            raise ValueError("research delivery chunk envelope exceeds the hard prompt cap")
        segments.append(text[start:accepted])
        start = accepted
    chunk_count = len(segments)
    chunks = tuple(
        _render_delivery_chunk(
            transfer_id=transfer_id,
            payload_sha256=payload_sha256,
            chunk_index=index,
            chunk_count=chunk_count,
            segment=segment,
        )
        for index, segment in enumerate(segments)
    )
    if chunk_count < 2 or any(len(chunk) > FOREGROUND_PROMPT_HARD_CAP for chunk in chunks):
        raise ValueError("oversized research delivery did not produce bounded chunks")
    return chunks


def stage_foreground_delivery(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    markers: Sequence[str],
    prompt: str,
) -> str:
    """Stage one unacknowledged foreground handoff and return its tagged prompt.

    The record intentionally remains process-local. A crash loses the staging
    record but not the durable finding, so a resumed process reconstructs and
    redelivers it. Only ``acknowledge_foreground_deliveries`` checkpoints the
    ordinary delivered marker after a completed foreground transcript proves
    that the model responded after seeing the tag.
    """
    bounded_markers = sorted(_marker_values(markers))
    text = str(prompt or "")
    if not bounded_markers or not text.strip():
        return ""
    records = _pending_foreground_records(autonomy_state)
    target_records = [
        record
        for record in records
        if str(record.get("target_symbol", "") or "") == str(target_symbol or "")
    ]
    if (
        len(records) >= PENDING_FOREGROUND_CAP
        or len(target_records) >= PENDING_FOREGROUND_TARGET_CAP
    ):
        # Refuse admission instead of evicting an older unacknowledged record.
        # The finding remains durable and is retried after an acknowledgement
        # or process restart frees process-local capacity.
        return ""
    digest_payload = json.dumps(
        [str(autonomy_state.get("campaign_id", "") or ""), *bounded_markers],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    ordinary_token = _delivery_token(digest_payload)
    tagged = f"[LEANFLOW RESEARCH DELIVERY TOKEN: {ordinary_token}]\n{text}"
    record: dict[str, Any]
    if len(tagged) <= FOREGROUND_PROMPT_HARD_CAP:
        if any(str(item.get("token", "") or "") == ordinary_token for item in records):
            return ""
        record = {
            "token": ordinary_token,
            "target_symbol": str(target_symbol or ""),
            "markers": bounded_markers,
            "prompt": tagged,
            "semantic_novelty_version": research_route_context.SEMANTIC_NOVELTY_VERSION,
        }
    else:
        payload_sha256 = sha256(text.encode("utf-8")).hexdigest()
        transfer_id = _chunk_transfer_id(
            autonomy_state,
            target_symbol=target_symbol,
            markers=bounded_markers,
            payload_sha256=payload_sha256,
        )
        chunks = _split_delivery_prompt(
            text,
            transfer_id=transfer_id,
            payload_sha256=payload_sha256,
        )
        transfers = _chunk_transfer_records(autonomy_state.get(CHUNK_TRANSFERS_KEY))
        transfer = transfers.get(transfer_id)
        if (
            transfer is None
            or str(transfer.get("payload_sha256", "") or "") != payload_sha256
            or int(transfer.get("chunk_count", 0) or 0) != len(chunks)
        ):
            transfer = {
                "transfer_id": transfer_id,
                "target_symbol": str(target_symbol or ""),
                "markers": bounded_markers,
                "payload_sha256": payload_sha256,
                "chunk_count": len(chunks),
                "next_index": 0,
                "semantic_novelty_version": research_route_context.SEMANTIC_NOVELTY_VERSION,
                "yield_once": False,
                "updated_at": _now_iso(),
            }
            transfers[transfer_id] = transfer
            autonomy_state[CHUNK_TRANSFERS_KEY] = transfers
        chunk_index = int(transfer.get("next_index", 0) or 0)
        chunk_prompt = chunks[chunk_index]
        token_line = chunk_prompt.splitlines()[0]
        token = token_line.removeprefix("[LEANFLOW RESEARCH DELIVERY TOKEN: ").removesuffix("]")
        if any(str(item.get("token", "") or "") == token for item in records):
            return ""
        tagged = chunk_prompt
        record = {
            "token": token,
            "target_symbol": str(target_symbol or ""),
            "markers": bounded_markers,
            "prompt": tagged,
            "transfer_id": transfer_id,
            "payload_sha256": payload_sha256,
            "chunk_index": chunk_index,
            "chunk_count": len(chunks),
            "semantic_novelty_version": research_route_context.SEMANTIC_NOVELTY_VERSION,
        }
    records.append(record)
    autonomy_state[PENDING_FOREGROUND_KEY] = records
    return tagged


def attach_pending_foreground_prompts(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    user_message: str,
    conversation_history: Sequence[Mapping[str, Any]],
) -> str:
    """Reattach any staged target prompt lost to compaction or epoch rollover."""
    retain_foreground_target(autonomy_state, target_symbol=target_symbol)
    text = str(user_message or "")
    visible = "\n".join(
        [text]
        + [
            str(message.get("content", "") or "")
            for message in conversation_history
            if isinstance(message, Mapping)
        ]
    )
    missing = [
        str(record["prompt"])
        for record in _pending_foreground_records(autonomy_state)
        if str(record.get("target_symbol", "") or "") == target_symbol
        and str(record["token"]) not in visible
        and len(str(record["prompt"])) <= FOREGROUND_PROMPT_HARD_CAP
    ][:1]
    return "\n\n".join(part for part in (text, *missing) if part)


def _content_text(value: Any) -> str:
    """Return searchable text from a provider message content value."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value or "")


def _acknowledge_chunk_record(
    autonomy_state: dict[str, Any],
    record: Mapping[str, Any],
) -> tuple[tuple[str, ...], bool]:
    """Advance one ordered chunk receipt without prematurely marking a finding."""
    transfer_id = str(record.get("transfer_id", "") or "")
    transfers = _chunk_transfer_records(autonomy_state.get(CHUNK_TRANSFERS_KEY))
    transfer = transfers.get(transfer_id)
    if transfer is None:
        return (), False
    chunk_index = int(record.get("chunk_index", -1))
    chunk_count = int(record.get("chunk_count", 0))
    if (
        str(transfer.get("payload_sha256", "") or "") != str(record.get("payload_sha256", "") or "")
        or int(transfer.get("chunk_count", 0)) != chunk_count
    ):
        return (), False
    next_index = int(transfer.get("next_index", 0))
    if chunk_index < next_index:
        # A crash can leave an already-checkpointed prompt in local staging.
        return (), True
    if chunk_index != next_index:
        return (), False
    markers = sorted(_marker_values(record.get("markers")))
    if chunk_index + 1 == chunk_count:
        return (
            _finish_chunk_transfer(
                autonomy_state,
                transfer_id=transfer_id,
                markers=markers,
            ),
            True,
        )
    advanced = dict(transfer)
    advanced["next_index"] = chunk_index + 1
    advanced["yield_once"] = True
    advanced["updated_at"] = _now_iso()
    _persist_chunk_transfer(autonomy_state, advanced)
    return (), True


def acknowledge_foreground_deliveries(
    autonomy_state: dict[str, Any],
    messages: Sequence[Mapping[str, Any]],
    *,
    api_user_message: str | None = None,
) -> tuple[str, ...]:
    """Durably acknowledge tagged findings followed by a model response.

    Merely constructing a prompt or appending it to a tool result is not an
    acknowledgement. The transcript must contain a later assistant message,
    proving that a foreground provider turn consumed the preceding context.
    ``api_user_message`` restores the current API-only prompt for this scan
    when transcript persistence replaced it with a clean history message.
    Processing any token reopens foreground selection, including a non-final
    chunk whose completed-marker result is intentionally empty.
    """
    records = _pending_foreground_records(autonomy_state)
    if not records:
        return ()
    by_token = {str(record["token"]): record for record in records}
    current_user_index = -1
    if api_user_message is not None:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if isinstance(message, Mapping) and str(message.get("role", "") or "") == "user":
                current_user_index = index
                break
    observed: set[str] = set()
    acknowledged: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            continue
        if str(message.get("role", "") or "") == "assistant":
            acknowledged.update(observed)
        content = _content_text(
            api_user_message if index == current_user_index else message.get("content", "")
        )
        observed.update(token for token in by_token if token in content)
    if not acknowledged:
        return ()
    ordinary_tokens = {
        token for token in acknowledged if not str(by_token[token].get("transfer_id", "") or "")
    }
    ordinary_markers = sorted(
        {
            marker
            for token in ordinary_tokens
            for marker in by_token[token].get("markers", [])
            if isinstance(marker, str) and marker
        }
    )
    if ordinary_markers:
        persist_delivery_markers(
            autonomy_state,
            key="research_findings_delivered",
            markers=ordinary_markers,
        )
    completed_markers = set(ordinary_markers)
    processed_tokens = set(ordinary_tokens)
    for record in records:
        token = str(record.get("token", "") or "")
        if token not in acknowledged or token in ordinary_tokens:
            continue
        delivered, processed = _acknowledge_chunk_record(autonomy_state, record)
        completed_markers.update(delivered)
        if processed:
            processed_tokens.add(token)
    autonomy_state[PENDING_FOREGROUND_KEY] = [
        record for record in records if str(record["token"]) not in processed_tokens
    ]
    if not autonomy_state[PENDING_FOREGROUND_KEY]:
        autonomy_state.pop(PENDING_FOREGROUND_KEY, None)
    if processed_tokens:
        research_delivery_gate.request_current_assignment_scan(autonomy_state)
    return tuple(sorted(completed_markers))


def was_delivered(
    finding: Mapping[str, Any],
    *,
    target_symbol: str,
    delivered: set[str],
) -> bool:
    """Return whether a finding was handed to this exact queue target.

    Old checkpoints stored only a bare job id. Treat that legacy marker as an
    exact-target delivery by comparing the finding's original target, so a
    subsequently generated split descendant can still inherit the evidence.
    """
    job_id = str(finding.get("job_id", "") or "")
    if not job_id:
        return True
    if delivery_key(job_id, target_symbol) in delivered:
        return True
    finding_target = str(finding.get("target_symbol", "") or "")
    return job_id in delivered and finding_target == target_symbol


def build_relevant_findings_index(
    summary: Mapping[str, Any] | None,
) -> RelevantFindingsIndex:
    """Normalize one summary snapshot after authenticating its ledger once."""
    state = dict(summary or {})
    ledger_specs: dict[str, dict[str, Any]] = {}
    ledger_entries: list[LedgerEntry] = []
    ledger_entries_by_id: dict[str, LedgerEntry] = {}
    for raw_entry in dispatch_ledger_compaction.hydrate_dispatch_ledger(
        state.get("dispatch_ledger") or [],
        state_root=workflow_state_root(),
    ):
        raw_spec = raw_entry.get("spec")
        if not isinstance(raw_spec, Mapping):
            continue
        spec = dict(raw_spec)
        job_id = str(spec.get("job_id", "") or "")
        if job_id:
            ledger_specs[job_id] = spec
        try:
            ledger_entry = LedgerEntry.from_mapping(raw_entry)
        except (TypeError, ValueError):
            continue
        ledger_entries.append(ledger_entry)
        if job_id and job_id not in ledger_entries_by_id:
            ledger_entries_by_id[job_id] = ledger_entry
    quarantined = _quarantined_archive_job_ids(state)
    normalized: list[dict[str, Any]] = []
    for raw in state.get("research_findings") or []:
        if not isinstance(raw, Mapping):
            continue
        finding = dict(raw)
        job_id = str(finding.get("job_id", "") or "")
        novelty = finding.get("semantic_novelty")
        try:
            novelty_version = (
                int(novelty.get("version", 0) or 0) if isinstance(novelty, Mapping) else 0
            )
        except (TypeError, ValueError):
            novelty_version = 0
        if (
            isinstance(novelty, Mapping)
            and novelty_version != research_route_context.SEMANTIC_NOVELTY_VERSION
        ):
            matched_ledger_entry = ledger_entries_by_id.get(job_id)
            if matched_ledger_entry is not None:
                matches, _reason, canonical = _finding_matches_archived_entry(
                    finding,
                    matched_ledger_entry,
                    entries=ledger_entries,
                )
                if matches:
                    finding = canonical
                else:
                    finding["semantic_novelty"] = {
                        "version": research_route_context.SEMANTIC_NOVELTY_VERSION,
                        "classification": "stale_unreclassified",
                        "progress_anchor_eligible": False,
                        "progress_anchor_reason": "stale_semantic_novelty_version",
                    }
            else:
                finding["semantic_novelty"] = {
                    "version": research_route_context.SEMANTIC_NOVELTY_VERSION,
                    "classification": "stale_unreclassified",
                    "progress_anchor_eligible": False,
                    "progress_anchor_reason": "stale_semantic_novelty_version",
                }
        if job_id in quarantined:
            continue
        spec = ledger_specs.get(job_id, {})
        inputs = dict(spec.get("inputs") or {})
        finding_target = str(
            finding.get("target_symbol", "") or inputs.get("target_symbol", "") or ""
        )
        finding_file = str(finding.get("active_file", "") or inputs.get("active_file", "") or "")
        finding["target_symbol"] = finding_target
        finding["active_file"] = finding_file
        normalized.append(finding)
    return RelevantFindingsIndex(findings=tuple(normalized))


def relevant_findings(
    summary: Mapping[str, Any] | None,
    *,
    target_symbol: str,
    active_file: str,
    blueprint: Blueprint | None = None,
    limit: int | None = DEFAULT_FINDING_LIMIT,
    index: RelevantFindingsIndex | None = None,
) -> tuple[dict[str, Any], ...]:
    """Return recent findings for an assignment or its split ancestors.

    ``index`` may be shared by several target projections from the exact same
    summary snapshot.  Omitting it retains the standalone fail-loud archive
    authentication behavior.
    """
    related_targets = set(
        related_target_symbols(
            blueprint,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    )
    prepared = index or build_relevant_findings_index(summary)
    selected: list[dict[str, Any]] = []
    for raw in prepared.findings:
        finding = dict(raw)
        finding_target = str(finding.get("target_symbol", "") or "")
        finding_file = str(finding.get("active_file", "") or "")
        if target_symbol and finding_target not in related_targets:
            continue
        if active_file and not _same_file(finding_file, active_file):
            continue
        selected.append(finding)
    exact = [
        finding
        for finding in selected
        if str(finding.get("target_symbol", "") or "") == target_symbol
    ]
    inherited = [
        finding
        for finding in selected
        if str(finding.get("target_symbol", "") or "") != target_symbol
    ]
    if limit is None:
        return tuple([*exact, *inherited])
    bounded_limit = max(1, int(limit or DEFAULT_FINDING_LIMIT))
    exact_tail = exact[-bounded_limit:]
    remaining = bounded_limit - len(exact_tail)
    inherited_tail = inherited[-remaining:] if remaining else []
    return tuple([*exact_tail, *inherited_tail])


def _exact_target_checked_replacement(finding: Mapping[str, Any]) -> bool:
    """Return whether the worker supplied a contract-valid exact target replacement."""
    target_symbol = str(finding.get("target_symbol", "") or "")
    if not target_symbol:
        return False
    deliverable = enforce_checked_replacement_contract(
        dict(finding.get("deliverable") or {}),
        expected_target_symbol=target_symbol,
    )
    return bool(deliverable.get("checked_replacements"))


def _canonical_checked_helpers(
    deliverable: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Return parent-captured helper artifacts with intact exact source.

    Dispatch strips model-authored ``checked_helpers`` and recreates the key
    from observed tool traffic. Revalidating its stable schema and source hash
    here prevents legacy or manually constructed findings from acquiring the
    same foreground authority merely by using the reserved key name.
    """
    if (
        deliverable.get("checked_helper_status") != CHECKED_HELPER_STATUS
        or deliverable.get("parent_recheck_required") is not True
    ):
        return ()
    raw_helpers = deliverable.get(CHECKED_HELPERS_KEY)
    if not isinstance(raw_helpers, Sequence) or isinstance(raw_helpers, (str, bytes, bytearray)):
        return ()
    helpers: list[dict[str, Any]] = []
    for raw_helper in raw_helpers:
        if not isinstance(raw_helper, Mapping):
            continue
        helper = dict(raw_helper)
        declaration = helper.get("declaration")
        worker_check = helper.get("worker_check")
        if (
            not isinstance(declaration, str)
            or not declaration.strip()
            or str(helper.get("declaration_sha256", "") or "")
            != sha256(declaration.encode("utf-8")).hexdigest()
            or not str(helper.get("anchor_target_symbol", "") or "").strip()
            or not str(helper.get("active_file", "") or "").strip()
            or helper.get("parent_recheck_required") is not True
            or not isinstance(worker_check, Mapping)
        ):
            continue
        raw_declarations = worker_check.get("replacement_declarations")
        if (
            worker_check.get("tool") != CHECKED_REPLACEMENT_TOOL
            or worker_check.get("action") != "check_helper"
            or worker_check.get("valid_without_sorry") is not True
            or worker_check.get("has_errors") is not False
            or worker_check.get("has_sorry") is not False
            or worker_check.get("verification_scope") != "helper_candidate"
            or worker_check.get("replacement_matches_target") is not False
            or not isinstance(raw_declarations, Sequence)
            or isinstance(raw_declarations, (str, bytes, bytearray))
            or not any(isinstance(value, str) and value.strip() for value in raw_declarations)
        ):
            continue
        helpers.append(helper)
    return tuple(helpers)


def canonical_checked_helpers(
    finding: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Return schema-valid worker helpers captured by the parent process.

    These artifacts remain parent-recheckable rather than kernel authority.
    Exposing the canonical projection lets deterministic consumers correlate
    one artifact with independently parent-verified graph truth without
    duplicating the dispatch schema or trusting model-authored prose.
    """
    deliverable = finding.get("deliverable")
    if not isinstance(deliverable, Mapping):
        return ()
    return _canonical_checked_helpers(deliverable)


def _has_canonical_checked_helper(finding: Mapping[str, Any]) -> bool:
    """Return whether one finding carries parent-observed helper source."""
    return bool(canonical_checked_helpers(finding))


def _declared_finite_evidence_without_target_completion(
    finding: Mapping[str, Any],
) -> bool:
    """Return whether a worker explicitly reported only finite evidence.

    This guard intentionally survives missing or stale semantic-novelty
    metadata. A checked fixed-instance declaration is still useful evidence,
    but it cannot acquire helper-integration priority unless it is an exact
    contract-valid replacement for the assigned target.
    """
    deliverable = finding.get("deliverable")
    return bool(
        isinstance(deliverable, Mapping)
        and not _exact_target_checked_replacement(finding)
        and research_route_context.explicitly_declared_finite_evidence_result(deliverable)
    )


def _partial_coverage_without_completion(finding: Mapping[str, Any]) -> bool:
    """Return whether an incomplete result cannot define a foreground route.

    Explicit partial/incomplete/noncompletion status is sufficient regardless
    of route age. A finite leaf without that status is quarantined after repeated
    rejected proof shapes. This policy does not rewrite archived mathematical
    novelty, and an exact checked replacement for the assigned target remains
    actionable for parent-side rechecking.
    """
    if _exact_target_checked_replacement(finding) or _has_canonical_checked_helper(finding):
        return False
    deliverable = dict(finding.get("deliverable") or {})
    if research_route_context.explicitly_nonclosing_result(deliverable):
        return True
    novelty = finding.get("semantic_novelty")
    if not isinstance(novelty, Mapping):
        return False
    parent_context = deliverable.pop(
        research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY,
        None,
    )
    if not isinstance(parent_context, Mapping):
        return False
    failed_shapes = parent_context.get("recent_failed_proof_shapes")
    if not isinstance(failed_shapes, Sequence) or isinstance(
        failed_shapes, (str, bytes, bytearray)
    ):
        return False
    failed_count = sum(1 for item in failed_shapes if isinstance(item, Mapping))
    if failed_count < _PARTIAL_COVERAGE_MIN_FAILED_SHAPES:
        return False
    descriptive_text = json.dumps(
        deliverable,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    fingerprints = novelty.get("fingerprints") or novelty.get("novel_fingerprints") or ()
    fingerprint_scope = bool(
        isinstance(fingerprints, Sequence)
        and not isinstance(fingerprints, (str, bytes, bytearray))
        and any(
            str(fingerprint).startswith(_PARTIAL_COVERAGE_FINGERPRINT_PREFIXES)
            for fingerprint in fingerprints
        )
    )
    finite_scope = fingerprint_scope or bool(_PARTIAL_COVERAGE_SCOPE_RE.search(descriptive_text))
    return bool(
        finite_scope
        and (_PARTIAL_COVERAGE_UNIT_RE.search(descriptive_text) or fingerprint_scope)
        and _PARTIAL_COVERAGE_GAP_RE.search(descriptive_text)
    )


def foreground_use_reason(finding: Mapping[str, Any]) -> str:
    """Return the deterministic reason governing foreground actionability."""
    expected_revision = str(finding.get("source_revision_sha256", "") or "").strip()
    active_file = str(finding.get("active_file", "") or "").strip()
    if expected_revision and active_file:
        try:
            current_revision = sha256(Path(active_file).read_bytes()).hexdigest()
        except OSError:
            current_revision = ""
        if current_revision and current_revision != expected_revision:
            return "stale_active_file_revision"
    novelty = finding.get("semantic_novelty")
    if isinstance(novelty, Mapping):
        try:
            version = int(novelty.get("version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        if version and version != research_route_context.SEMANTIC_NOVELTY_VERSION:
            return "stale_semantic_novelty_version"
    if isinstance(novelty, Mapping) and novelty.get("progress_anchor_eligible") is False:
        return str(novelty.get("progress_anchor_reason", "") or "semantic_progress_ineligible")
    deliverable = finding.get("deliverable")
    if isinstance(deliverable, Mapping) and canonical_checked_helpers(finding):
        disposition = str(deliverable.get("checked_helper_route_disposition", "") or "").strip()
        if disposition and disposition not in _CHECKED_HELPER_ROUTE_DISPOSITIONS:
            return "malformed_checked_helper_route_disposition"
        if disposition == "evidence_only":
            return "checked_helper_declared_evidence_only"
        # Older worker reports predate the structured disposition. Preserve
        # their ordinary behavior except for an explicit, unambiguous statement
        # that the captured helper does not advance the assigned target.
        interpretation = str(deliverable.get("interpretation", "") or "").casefold()
        if not disposition and any(
            phrase in interpretation
            for phrase in (
                "does not advance",
                "doesn't advance",
                "does not contribute to",
                "not needed by the current route",
            )
        ):
            return "legacy_checked_helper_explicitly_nonadvancing"
    if _declared_finite_evidence_without_target_completion(finding):
        return "declared_finite_evidence_only"
    if _partial_coverage_without_completion(finding):
        return "partial_coverage_without_completion"
    return "actionable_research_finding"


def foreground_use_role(finding: Mapping[str, Any]) -> str:
    """Return whether one finding may define the next foreground proof action.

    Semantic novelty is deterministic parent-owned metadata. A result already
    classified as duplicate, subsumed, malformed, or otherwise ineligible is
    still useful as route history and negative evidence, but presenting its
    candidate as actionable invites the exact repetition the classifier was
    designed to prevent. Legacy findings without this metadata retain their
    historical actionable behavior.
    """
    use_reason = foreground_use_reason(finding)
    if use_reason in {
        "stale_active_file_revision",
        "checked_helper_declared_evidence_only",
        "legacy_checked_helper_explicitly_nonadvancing",
        "malformed_checked_helper_route_disposition",
    }:
        return "evidence_only"
    novelty = finding.get("semantic_novelty")
    if isinstance(novelty, Mapping):
        try:
            version = int(novelty.get("version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        if version and version != research_route_context.SEMANTIC_NOVELTY_VERSION:
            return "evidence_only"
    if isinstance(novelty, Mapping) and novelty.get("progress_anchor_eligible") is False:
        return "evidence_only"
    if _declared_finite_evidence_without_target_completion(finding):
        return "evidence_only"
    if _partial_coverage_without_completion(finding):
        return "evidence_only"
    return "actionable"


def has_actionable_exact_candidate(finding: Mapping[str, Any]) -> bool:
    """Return whether foreground may recheck exact target or helper source."""
    if foreground_use_role(finding) != "actionable":
        return False
    return bool(
        _exact_target_checked_replacement(finding) or _has_canonical_checked_helper(finding)
    )


def has_actionable_exact_candidate_for_target(
    finding: Mapping[str, Any],
    *,
    target_symbol: str,
) -> bool:
    """Return whether checked source belongs to the exact foreground target.

    Split descendants inherit parent findings for negative knowledge, but a
    checked parent replacement is not a replacement for the child.  Keep the
    priority rule exact-scope so inherited evidence cannot preempt the child's
    own proof turn.
    """
    target = str(target_symbol or "").strip()
    return bool(
        target
        and str(finding.get("target_symbol", "") or "").strip() == target
        and has_actionable_exact_candidate(finding)
    )


def pending_checked_target_replacement(
    autonomy_state: Mapping[str, Any],
    summary: Mapping[str, Any] | None,
    *,
    target_symbol: str,
    active_file: str,
    blueprint: Blueprint | None = None,
) -> bool:
    """Return whether a staged exact target replacement awaits one prover turn.

    A delivery token is the authority that the exact source will be present in
    the next provider prompt.  Merely finding checked code in the archive is
    insufficient because an unstaged result may be oversized or outside the
    current delivery window.
    """
    target = str(target_symbol or "").strip()
    if not target:
        return False
    pending = pending_foreground_markers(
        autonomy_state,
        target_symbol=target,
    )
    if not pending:
        return False
    for finding in relevant_findings(
        summary,
        target_symbol=target,
        active_file=str(active_file or "").strip(),
        blueprint=blueprint,
        limit=None,
    ):
        if (
            str(finding.get("target_symbol", "") or "").strip() == target
            and foreground_use_role(finding) == "actionable"
            and _exact_target_checked_replacement(finding)
            and delivery_key(str(finding.get("job_id", "") or ""), target) in pending
        ):
            return True
    return False


def _sanitize_negative_action_text(value: Any) -> Any:
    """Remove implementation clauses embedded inside one negative-evidence field."""
    if not isinstance(value, str):
        return value
    clauses = re.split(r"(?<=[.!?;])\s+", value)
    retained = [
        clause.strip()
        for clause in clauses
        if clause.strip() and not _EVIDENCE_ONLY_ACTION_TEXT_RE.search(clause)
    ]
    return " ".join(retained)


def negative_evidence_lines(
    finding: Mapping[str, Any],
    *,
    cap: int = 8,
) -> tuple[str, ...]:
    """Return bounded explicit dead-route evidence from one research finding.

    Route labels are retained only when they are descriptive rather than Lean
    action text. This lets later turns avoid a spent mathematical direction
    without turning an evidence-only result back into an implementation plan.
    """
    deliverable = finding.get("deliverable")
    if not isinstance(deliverable, Mapping):
        return ()
    raw_dead_ends = deliverable.get("dead_ends") or []
    if isinstance(raw_dead_ends, (str, bytes, bytearray)) or not isinstance(
        raw_dead_ends, Sequence
    ):
        raw_dead_ends = [raw_dead_ends]
    lines: list[str] = []
    for raw in raw_dead_ends:
        route = ""
        reason = ""
        if isinstance(raw, Mapping):
            route = str(_sanitize_negative_action_text(raw.get("route", "")) or "").strip()
            reason = str(_sanitize_negative_action_text(raw.get("reason", "")) or "").strip()
        else:
            reason = str(_sanitize_negative_action_text(raw) or "").strip()
        text = f"{route}: {reason}" if route and reason else route or reason
        text = " ".join(text.split())[:500].strip()
        if text and text not in lines:
            lines.append(text)
        if len(lines) >= max(1, int(cap)):
            break
    return tuple(lines)


def _evidence_only_projection(
    value: Any,
    *,
    negative_context: bool = False,
    action_context: bool = False,
) -> Any:
    """Return only explicit negative or route-excluding evidence from a payload."""
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.strip().casefold()
            negative_key = normalized in _EVIDENCE_ONLY_SAFE_KEYS or any(
                marker in normalized for marker in _EVIDENCE_ONLY_KEY_PARTS
            )
            action_key = any(marker in normalized for marker in _EVIDENCE_ONLY_ACTION_KEY_PARTS)
            if action_key and not negative_key:
                continue
            if isinstance(item, Mapping) or (
                isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray))
            ):
                nested = _evidence_only_projection(
                    item,
                    negative_context=negative_context or negative_key,
                    action_context=action_context or action_key,
                )
            elif (negative_context or negative_key) and (negative_key or not action_key):
                # Negative wrappers are worker-authored too. Sanitize every retained
                # string so a field such as ``counterexample_evidence.new_test``
                # cannot smuggle a fresh congruence route into an evidence-only prompt.
                nested = _sanitize_negative_action_text(item)
            else:
                nested = None
            if nested not in ({}, [], (), None, ""):
                projected[key] = nested
        return projected
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        projected_items: list[Any] = []
        for item in value:
            projected_item: Any
            if isinstance(item, Mapping) or (
                isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray))
            ):
                projected_item = _evidence_only_projection(
                    item,
                    negative_context=negative_context,
                    action_context=action_context,
                )
            else:
                projected_item = _sanitize_negative_action_text(item) if negative_context else None
            if projected_item not in ({}, [], (), None, ""):
                projected_items.append(projected_item)
        return projected_items
    return None


def _evidence_only_raw_metadata(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return parent-owned finding metadata safe for an evidence-only prompt."""
    metadata = {key: raw[key] for key in _EVIDENCE_ONLY_RAW_KEYS if key in raw}
    novelty = raw.get("semantic_novelty")
    if isinstance(novelty, Mapping):
        safe_novelty = {key: novelty[key] for key in _EVIDENCE_ONLY_SEMANTIC_KEYS if key in novelty}
        suppressed = {
            key: value for key, value in novelty.items() if key not in _EVIDENCE_ONLY_SEMANTIC_KEYS
        }
        if suppressed:
            safe_novelty["suppressed_semantic_fields"] = sorted(suppressed)
            safe_novelty["suppressed_semantic_sha256"] = _stable_payload_sha256(suppressed)
        metadata["semantic_novelty"] = safe_novelty
    return metadata


def _has_exact_replacement(finding: Mapping[str, Any]) -> bool:
    """Return whether an actionable finding carries exact checked Lean source."""
    if foreground_use_role(finding) == "evidence_only":
        return False
    deliverable = enforce_checked_replacement_contract(
        dict(finding.get("deliverable") or {}),
        expected_target_symbol=str(finding.get("target_symbol", "") or ""),
    )
    return bool(
        list(deliverable.get("checked_replacements") or [])
        or list(deliverable.get("unchecked_replacements") or [])
        or _canonical_checked_helpers(deliverable)
    )


def _active_chunk_transfer(
    autonomy_state: Mapping[str, Any] | None,
    *,
    marker: str,
) -> dict[str, Any] | None:
    """Return the active oversized transfer carrying one finding marker."""
    if autonomy_state is None:
        return None
    for transfer in _chunk_transfer_records(autonomy_state.get(CHUNK_TRANSFERS_KEY)).values():
        if marker in _marker_values(transfer.get("markers")):
            return transfer
    return None


def _consume_oversized_yield(
    autonomy_state: dict[str, Any] | None,
    *,
    marker: str,
    transfer: Mapping[str, Any] | None,
) -> None:
    """Record one fairness yield before returning to an oversized finding."""
    if autonomy_state is None:
        return
    if transfer is not None:
        if not bool(transfer.get("yield_once")):
            return
        updated = dict(transfer)
        updated["yield_once"] = False
        updated["updated_at"] = _now_iso()
        _persist_chunk_transfer(autonomy_state, updated)
        return
    deferred = _marker_values(autonomy_state.get(OVERSIZED_DEFERRED_KEY))
    deferred.add(marker)
    autonomy_state[OVERSIZED_DEFERRED_KEY] = sorted(deferred)[-CHUNK_TRANSFER_CAP:]


def foreground_delivery_batch(
    findings: Sequence[Mapping[str, Any]],
    *,
    finding_limit: int = FOREGROUND_BATCH_FINDING_CAP,
    max_chars: int = FOREGROUND_PAYLOAD_CAP,
    autonomy_state: dict[str, Any] | None = None,
    target_symbol: str = "",
) -> tuple[tuple[dict[str, Any], ...], str]:
    """Build one bounded, evidence-complete foreground batch.

    Actionable checked source for the exact target outranks generic FIFO work
    and travels alone.  This prevents a bounded batch from advancing the
    completion watermark while leaving a ready proof candidate stranded behind
    older prose. Generic findings retain FIFO order and are admitted only while
    their complete JSON fits the hard research-prompt budget. Any oversized
    finding yields one batch slot to later evidence, then travels through the
    ordered chunk protocol in ``stage_foreground_delivery``.
    """
    candidates = [dict(finding) for finding in findings if isinstance(finding, Mapping)]
    if not candidates:
        return (), ""
    exact_target_candidates = [
        finding
        for finding in candidates
        if has_actionable_exact_candidate_for_target(
            finding,
            target_symbol=target_symbol,
        )
    ]
    if exact_target_candidates:
        selected = exact_target_candidates[0]
        candidates = [
            selected,
            *(finding for finding in candidates if finding is not selected),
        ]
    cap = max(1000, int(max_chars or FOREGROUND_PROMPT_HARD_CAP))
    limit = max(1, int(finding_limit or FOREGROUND_BATCH_FINDING_CAP))
    batch: list[dict[str, Any]] = []
    deferred_oversized: tuple[dict[str, Any], str, str, dict[str, Any] | None] | None = None
    for finding in candidates:
        has_exact = _has_exact_replacement(finding)
        if batch and (has_exact or _has_exact_replacement(batch[0])):
            break
        proposed = [*batch, finding]
        rendered = prompt_payload(proposed, max_chars=2**31 - 1)
        if len(rendered) > cap:
            if not batch:
                finding_rendered = prompt_payload([finding], max_chars=2**31 - 1)
                marker = delivery_key(
                    str(finding.get("job_id", "") or ""),
                    str(target_symbol or finding.get("target_symbol", "") or ""),
                )
                transfer = _active_chunk_transfer(autonomy_state, marker=marker)
                deferred = _marker_values((autonomy_state or {}).get(OVERSIZED_DEFERRED_KEY))
                should_yield = bool(transfer and transfer.get("yield_once")) or (
                    transfer is None and marker not in deferred
                )
                if not should_yield:
                    return (finding,), finding_rendered
                if deferred_oversized is None:
                    deferred_oversized = (finding, finding_rendered, marker, transfer)
                continue
            break
        batch = proposed
        if has_exact or len(batch) >= limit:
            break
    if batch:
        if deferred_oversized is not None:
            _, _, marker, transfer = deferred_oversized
            _consume_oversized_yield(
                autonomy_state,
                marker=marker,
                transfer=transfer,
            )
        return tuple(batch), prompt_payload(batch, max_chars=2**31 - 1)
    if deferred_oversized is not None:
        finding, rendered, marker, transfer = deferred_oversized
        _consume_oversized_yield(
            autonomy_state,
            marker=marker,
            transfer=transfer,
        )
        return (finding,), rendered
    return (), ""


def prompt_payload(
    findings: Sequence[Mapping[str, Any]],
    *,
    max_chars: int = DEFAULT_PROMPT_CAP,
) -> str:
    """Render findings with exact checked replacements ahead of bounded prose."""
    payload: list[dict[str, Any]] = []
    for finding in findings:
        raw = dict(finding)
        retired_routes = negative_evidence_lines(raw)
        use_role = foreground_use_role(raw)
        raw.pop("foreground_use_role", None)
        raw.pop("foreground_use_policy", None)
        deliverable = enforce_checked_replacement_contract(dict(raw.pop("deliverable", {}) or {}))
        original_deliverable = dict(deliverable)
        parent_context = deliverable.pop(
            research_route_context.PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY,
            None,
        )
        checked = list(deliverable.pop("checked_replacements", []) or [])
        unchecked = list(deliverable.pop("unchecked_replacements", []) or [])
        checked_helpers = list(_canonical_checked_helpers(deliverable))
        deliverable.pop(CHECKED_HELPERS_KEY, None)
        candidate_payload = [*checked, *unchecked, *checked_helpers]
        actionable = use_role == "actionable"
        item: dict[str, Any] = {
            "job_id": str(raw.pop("job_id", "") or ""),
            "checked_replacements": checked if actionable else [],
        }
        if checked_helpers:
            item[CHECKED_HELPERS_KEY] = checked_helpers if actionable else []
        if isinstance(parent_context, Mapping):
            item["parent_route_context_sha256"] = str(parent_context.get("sha256", "") or "")
        if not actionable:
            item["foreground_use_role"] = "evidence_only"
            item["foreground_use_reason"] = foreground_use_reason(finding)
            item["foreground_use_policy"] = (
                "EVIDENCE_ONLY: preserve negative facts, obstructions, and route exclusions, "
                "but do not implement, retry, or prioritize any candidate, helper, or proof "
                "shape from this item. Choose a materially distinct route."
            )
            if candidate_payload:
                item["suppressed_candidate_count"] = len(candidate_payload)
                item["suppressed_candidate_sha256"] = _stable_payload_sha256(candidate_payload)
            item["suppressed_deliverable_sha256"] = _stable_payload_sha256(original_deliverable)
            objective = str(raw.get("objective", "") or "")
            if objective:
                item["suppressed_objective_sha256"] = sha256(objective.encode("utf-8")).hexdigest()
            suppressed_raw = {
                key: value for key, value in raw.items() if key not in _EVIDENCE_ONLY_RAW_KEYS
            }
            if suppressed_raw:
                item["suppressed_raw_fields"] = sorted(suppressed_raw)
                item["suppressed_raw_sha256"] = _stable_payload_sha256(suppressed_raw)
            negative_evidence = _evidence_only_projection(deliverable)
            if negative_evidence in ({}, [], (), None, ""):
                negative_evidence = {
                    "notice": (
                        "No new negative evidence was retained beyond the semantic novelty "
                        "metadata; all worker-authored action content was suppressed."
                    )
                }
            deliverable = {"negative_evidence": negative_evidence}
            if retired_routes:
                deliverable["retired_routes"] = list(retired_routes)
            raw = _evidence_only_raw_metadata(raw)
        elif checked:
            item["foreground_use_role"] = "actionable"
            item["checked_replacement_policy"] = (
                "Prioritize the exact checked_replacements candidate, but treat worker_check "
                "as advisory. The parent must rerun lean_incremental_check(check_target) "
                "against the current declaration before editing or accepting it."
            )
        if checked_helpers and actionable:
            item["foreground_use_role"] = "actionable"
            item["checked_helper_policy"] = (
                "PARTIAL HELPER, NOT TARGET CLOSURE: preserve this exact declaration and rerun "
                "lean_incremental_check(action=check_helper) against the current active file "
                "before inserting or relying on it. After parent recheck, integrate it once as "
                "a verified sub-result, but continue proving the unresolved target and residual "
                "coverage; the helper alone is never a proof-completion verdict."
            )
        if not checked and deliverable.get("checked_replacement_status") == (
            "incomplete_unverified"
        ):
            item["checked_replacement_policy"] = (
                "Do not treat this candidate as verified: the worker omitted or violated "
                "the exact checked_replacements contract."
            )
        if unchecked and actionable:
            # Keep full candidate text for diagnosis, but separate it from the
            # authoritative-looking checked list and label it unverified.
            item["unchecked_replacements"] = unchecked
        item.update(raw)
        item["deliverable"] = deliverable
        payload.append(item)

    rendered = json.dumps(payload, ensure_ascii=False)
    cap = max(1000, int(max_chars or DEFAULT_PROMPT_CAP))
    if len(rendered) <= cap:
        return rendered

    if any(item.get("checked_replacements") or item.get(CHECKED_HELPERS_KEY) for item in payload):
        # Exact proof text is correctness data. Drop/bound prose first; if the
        # exact candidates alone exceed the nominal prompt cap, exceed it
        # explicitly instead of silently cutting a proof.
        prioritized: list[dict[str, Any]] = []
        generic_summaries: list[str] = []
        for item in payload:
            deliverable = dict(item.get("deliverable") or {})
            priority = {
                "job_id": str(item.get("job_id", "") or ""),
                "checked_replacements": list(item.get("checked_replacements") or []),
                CHECKED_HELPERS_KEY: list(item.get(CHECKED_HELPERS_KEY) or []),
                "checked_replacement_policy": str(item.get("checked_replacement_policy", "") or ""),
                "checked_helper_policy": str(item.get("checked_helper_policy", "") or ""),
                "foreground_use_role": str(item.get("foreground_use_role", "") or ""),
                "foreground_use_policy": str(item.get("foreground_use_policy", "") or ""),
                "archetype": str(item.get("archetype", "") or ""),
                "objective": str(item.get("objective", "") or "")[:500],
            }
            if item.get("unchecked_replacements"):
                priority["unchecked_replacements"] = list(item["unchecked_replacements"])
            if deliverable.get("checked_replacement_status"):
                priority["checked_replacement_status"] = deliverable["checked_replacement_status"]
            if deliverable.get("checked_helper_status"):
                priority["checked_helper_status"] = deliverable["checked_helper_status"]
            prioritized.append(priority)
            generic_summaries.append(json.dumps(deliverable, ensure_ascii=False, sort_keys=True))

        priority_rendered = json.dumps(prioritized, ensure_ascii=False)
        if len(priority_rendered) > cap:
            for item in prioritized:
                if item.get("checked_replacements"):
                    item["prompt_cap_exceeded_for_exact_replacements"] = True
                if item.get(CHECKED_HELPERS_KEY):
                    item["prompt_cap_exceeded_for_exact_checked_helpers"] = True
            return json.dumps(prioritized, ensure_ascii=False)

        remaining = max(0, cap - len(priority_rendered) - 200 * len(prioritized))
        per_finding = remaining // max(1, len(prioritized))
        for item, summary in zip(prioritized, generic_summaries, strict=True):
            if per_finding:
                item["deliverable_summary"] = summary[:per_finding]
            item["truncated"] = True
        bounded_rendered = json.dumps(prioritized, ensure_ascii=False)
        if len(bounded_rendered) <= cap:
            return bounded_rendered
        # Escaping overhead can exceed the estimate. The priority-only payload
        # has already been measured under cap, so fall back to it intact.
        for item in prioritized:
            item.pop("deliverable_summary", None)
        return json.dumps(prioritized, ensure_ascii=False)

    per_finding = max(400, cap // max(1, len(payload)) - 200)
    bounded = []
    for finding in payload:
        deliverable_text = json.dumps(
            finding.get("deliverable") or {}, ensure_ascii=False, sort_keys=True
        )
        bounded.append(
            {
                "job_id": str(finding.get("job_id", "") or ""),
                "archetype": str(finding.get("archetype", "") or ""),
                "objective": str(finding.get("objective", "") or "")[:500],
                "foreground_use_role": str(finding.get("foreground_use_role", "") or ""),
                "foreground_use_policy": str(finding.get("foreground_use_policy", "") or ""),
                "deliverable_summary": deliverable_text[:per_finding],
                "truncated": True,
            }
        )
    return json.dumps(bounded, ensure_ascii=False, sort_keys=True)
