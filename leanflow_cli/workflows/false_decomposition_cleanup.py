"""Retract campaign-created false helpers and reopen their original parents.

Only an authoritative sublemma negation plus exact decomposer provenance may
enter this path.  Cleanup is a durable source-first transaction: persist the
full compare-and-swap plan, surgically remove the owned helper and restore the
pre-edit parent declaration, invalidate exact same-revision unresolved
decomposer obligations that depend on the false helper, retire owned structural
edges, then archive (rather than discard) the valid negation evidence. When the
graph contains the exact proved promotion witness, the false helper remains as
its audit tombstone. Interrupted transactions replay idempotently. Ambiguous
pre-edit ownership is quarantined; post-edit source or graph drift remains
pending for safe replay.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _strip_lean_comments_and_strings,
)
from leanflow_cli.workflows import (
    decomposition_provenance,
    false_cleanup_transaction_registry,
    negation_transaction_registry,
    plan_state,
)
from leanflow_cli.workflows.workflow_json_io import update_json_file, update_json_file_if_changed
from leanflow_cli.workflows.workflow_state import (
    append_workflow_activity,
    append_workflow_outcome,
)

_TRANSACTION_CAP = 50
_CLEANUP_CAP = 50
_QUARANTINE_CAP = 50
_LEGACY_EVIDENCE_QUARANTINE_REASON = (
    "helper graph node has evidence edges that cleanup must preserve"
)
_MULTIPLE_VERIFIED_EVIDENCE_QUARANTINE_REASON = (
    "evidence edge is not the unique proved promotion proof declaration"
)
_NO_WORK_LEGACY_MIGRATION_QUARANTINE_REASON = (
    "committed cleanup dependent migration: " + _MULTIPLE_VERIFIED_EVIDENCE_QUARANTINE_REASON
)
_SIGNATURE_MISMATCH_REASON = "current false helper signature hash differs from promotion evidence"
_LEGACY_SIGNATURE_MISMATCH_REASON = (
    "current false helper signature hash differs from promoted evidence"
)
_SIGNATURE_MISMATCH_REASONS = frozenset(
    {_SIGNATURE_MISMATCH_REASON, _LEGACY_SIGNATURE_MISMATCH_REASON}
)
_EVIDENCE_TOMBSTONE_BASIS_MARKER = "-with-evidence-tombstone:"
_TRANSACTION_V1_IDENTITY_FIELDS = (
    "version",
    "file",
    "graph_file",
    "helper",
    "parent",
    "helper_node_id",
    "parent_node_id",
    "promotion_id",
    "provenance_id",
    "source_hash_kind",
    "source_before_sha256",
    "source_after_sha256",
    "helper_declaration_sha256",
    "helper_signature_sha256",
    "parent_current_declaration_sha256",
    "parent_signature_sha256",
    "parent_restored_declaration_sha256",
    "ownership_basis",
    "promotion_evidence_sha256",
)
_TRANSACTION_V2_IDENTITY_FIELDS = (
    *_TRANSACTION_V1_IDENTITY_FIELDS,
    "invalidated_dependents_sha256",
)
_TRANSACTION_V3_IDENTITY_FIELDS = (
    *_TRANSACTION_V2_IDENTITY_FIELDS,
    "migration_from_transaction_id",
)
_DEPENDENT_INVALIDATION_FIELDS = frozenset(
    {"node_id", "name", "file", "source_sha256", "declaration_sha256"}
)
_V3_DEPENDENT_INVALIDATION_FIELDS = frozenset({*_DEPENDENT_INVALIDATION_FIELDS, "source_kind"})
_UNRESOLVED_DECOMPOSER_STATUSES = frozenset({"stated", "conjectured"})

# Lean names may be qualified and may contain primes.  Matching a complete
# lexical name after comments and strings are removed avoids treating a doc
# comment, string literal, or prefix identifier as a proof dependency.
_LEAN_IDENTIFIER_RE = re.compile(
    r"(?<![\w'])((?:_root_\.)?(?!\d)\w[\w']*(?:\.(?!\d)\w[\w']*)*)(?![\w'])",
    flags=re.UNICODE,
)


@dataclass(frozen=True)
class CleanupReconciliation:
    """Report false-decomposition cleanup and recovery outcomes."""

    pending: int = 0
    cleaned: int = 0
    quarantined: int = 0
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class _GraphCleanupShape:
    """Describe graph edges an authenticated false-helper cleanup may change."""

    preserved_evidence: tuple[plan_state.GraphEdge, ...] = ()
    invalidated_dependents: tuple[plan_state.GraphNode, ...] = ()
    removable_structural: tuple[plan_state.GraphEdge, ...] = ()


class CleanupTransactionCapacityError(ValueError):
    """Reject a cleanup write that cannot retain every live transaction."""


class _RetryablePromotionValidation(RuntimeError):
    """Keep a cleanup candidate live after transient kernel infrastructure failure."""


def _audit_cleanup_transactions(
    raw_registry: object,
) -> false_cleanup_transaction_registry.FalseCleanupRegistryAudit:
    """Audit every cleanup transaction without filtering unresolved evidence."""
    return false_cleanup_transaction_registry.audit_false_cleanup_transaction_registry(
        raw_registry,
        terminal_history_cap=_TRANSACTION_CAP,
    )


def _audit_cleanup_quarantines(
    raw_registry: object,
) -> false_cleanup_transaction_registry.FalseCleanupRegistryAudit:
    """Audit every cleanup quarantine without filtering unresolved evidence."""
    return false_cleanup_transaction_registry.audit_false_cleanup_quarantine_registry(
        raw_registry,
        terminal_history_cap=_QUARANTINE_CAP,
    )


def _retained_cleanup_transactions(records: object) -> object:
    """Retain every unresolved cleanup plus bounded authenticated history."""
    audit = _audit_cleanup_transactions(records)
    live = sum(record.disposition == "live" for record in audit.records)
    if live > _TRANSACTION_CAP:
        raise CleanupTransactionCapacityError(
            f"more than {_TRANSACTION_CAP} false-decomposition cleanups remain pending"
        )
    return audit.retained_registry


def _retained_negation_transactions(records: object) -> object:
    """Retain all ambiguous/live evidence plus bounded authenticated history."""
    return negation_transaction_registry.audit_negation_transaction_registry(
        records,
        terminal_history_cap=_TRANSACTION_CAP,
    ).retained_registry


def _retained_cleanup_quarantines(records: object) -> object:
    """Retain unresolved quarantine evidence and bounded resolutions."""
    return _audit_cleanup_quarantines(records).retained_registry


def _audit_active_promotions(
    raw_registry: object,
) -> negation_transaction_registry.NegationPromotionRegistryAudit:
    """Audit active negation authority without normalizing legacy evidence."""
    return negation_transaction_registry.audit_negation_promotions(raw_registry)


def _active_promotion_records_for_mutation(
    summary: Mapping[str, Any],
    *,
    promotion_id: str,
    allow_reconcilable: bool = False,
) -> tuple[list[object], int | None]:
    """Return a lossless promotion ledger and one safe mutation target."""
    raw = summary.get("negation_promotions")
    if raw is None:
        return [], None
    audit = _audit_active_promotions(raw)
    if not isinstance(raw, list):
        raise RuntimeError("negation-promotion registry is not a list")
    target = str(promotion_id or "").strip()
    matches = audit.matching_indexes(target) if target else ()
    if len(matches) > 1:
        raise RuntimeError("negation-promotion target is duplicated")
    index = (
        audit.unique_selectable_index(target)
        if allow_reconcilable
        else audit.unique_authenticated_index(target)
    )
    if matches and index is None:
        raise RuntimeError("negation-promotion target is unauthenticated")
    return list(raw), index


def _bridge_revalidated_promotion(
    original: Mapping[str, Any],
    authoritative: Mapping[str, Any],
    *,
    legacy_evidence_quarantine_id: str = "",
) -> dict[str, Any]:
    """Seal leased legacy authority and its commit before cleanup can edit source."""
    upgraded = dict(authoritative)
    upgraded_audit = _audit_active_promotions([upgraded])
    if upgraded_audit.active != 1 or not upgraded_audit.ok:
        reason = upgraded_audit.reasons[0] if upgraded_audit.reasons else "unknown evidence"
        raise RuntimeError(f"fresh promotion is not fully authenticated: {reason}")
    original_id = _promotion_id(original)
    upgraded_id = _promotion_id(upgraded)
    if not original_id or not upgraded_id:
        raise RuntimeError("promotion bridge lacks a durable identity")
    now = _now_iso()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        raw_quarantines = summary.get("false_decomposition_cleanup_quarantine")
        required_quarantine_index: int | None = None
        if legacy_evidence_quarantine_id:
            matching_quarantines = _live_legacy_evidence_quarantines(summary, original)
            exact_quarantines = [
                index
                for index, quarantine_id in matching_quarantines
                if quarantine_id == legacy_evidence_quarantine_id
            ]
            if len(matching_quarantines) != 1 or len(exact_quarantines) != 1:
                raise RuntimeError("promotion bridge evidence quarantine changed during upgrade")
            required_quarantine_index = exact_quarantines[0]
            if not isinstance(raw_quarantines, list):
                raise RuntimeError("promotion bridge evidence quarantine registry disappeared")
        records, record_index = _active_promotion_records_for_mutation(
            summary,
            promotion_id=original_id,
            allow_reconcilable=True,
        )
        if record_index is None and upgraded_id != original_id:
            records, record_index = _active_promotion_records_for_mutation(
                summary,
                promotion_id=upgraded_id,
            )
        if record_index is None:
            raise RuntimeError("promotion authority changed during leased upgrade")
        records[record_index] = upgraded
        summary["negation_promotions"] = records

        raw_transactions = summary.get("negation_promotion_transactions")
        if raw_transactions is None:
            transactions: list[object] = []
            transaction_audit = negation_transaction_registry.audit_negation_transaction_registry(
                transactions
            )
        elif isinstance(raw_transactions, list):
            transactions = list(raw_transactions)
            transaction_audit = negation_transaction_registry.audit_negation_transaction_registry(
                raw_transactions,
                terminal_history_cap=_TRANSACTION_CAP,
            )
        else:
            raise RuntimeError("negation-promotion transaction registry is not a list")
        transaction_indexes: list[int] = []
        for index, raw in enumerate(transactions):
            if not isinstance(raw, Mapping):
                continue
            nested = raw.get("promotion")
            nested_id = (
                str(nested.get("promotion_id", "") or "").strip()
                if isinstance(nested, Mapping)
                else ""
            )
            if {str(raw.get("transaction_id", "") or "").strip(), nested_id} & {
                original_id,
                upgraded_id,
            }:
                transaction_indexes.append(index)
        if len(transaction_indexes) > 1:
            raise RuntimeError("promotion bridge transaction target is duplicated")
        prepared_at = str(upgraded.get("promoted_at", "") or now)
        committed_at = now
        if transaction_indexes:
            transaction_index = transaction_indexes[0]
            record_audit = transaction_audit.records[transaction_index]
            raw_transaction = transactions[transaction_index]
            if (
                record_audit.disposition != "terminal"
                or not isinstance(raw_transaction, Mapping)
                or str(raw_transaction.get("state", "") or "") != "committed"
            ):
                raise RuntimeError("promotion bridge found unauthenticated transaction evidence")
            prepared_at = str(raw_transaction.get("prepared_at", "") or prepared_at)
            committed_at = str(raw_transaction.get("committed_at", "") or committed_at)
        committed_transaction = {
            "transaction_id": upgraded_id,
            "state": "committed",
            "prepared_at": prepared_at,
            "committed_at": committed_at,
            "promotion": upgraded,
        }
        candidate_audit = negation_transaction_registry.audit_negation_transaction_registry(
            [committed_transaction]
        )
        if not candidate_audit.records or candidate_audit.records[0].disposition != "terminal":
            reason = candidate_audit.records[0].reason if candidate_audit.records else ""
            raise RuntimeError(
                "promotion bridge commit is unauthenticated" + (f": {reason}" if reason else "")
            )
        if transaction_indexes:
            transactions[transaction_indexes[0]] = committed_transaction
        else:
            transactions.append(committed_transaction)
        summary["negation_promotion_transactions"] = _retained_negation_transactions(transactions)

        if required_quarantine_index is not None:
            assert isinstance(raw_quarantines, list)
            quarantines: list[object] = list(raw_quarantines)
            raw_quarantine = quarantines[required_quarantine_index]
            assert isinstance(raw_quarantine, Mapping)
            migrated = _quarantine_entry(
                upgraded,
                reason=_LEGACY_EVIDENCE_QUARANTINE_REASON,
                provenance_id=str(raw_quarantine.get("provenance_id", "") or ""),
                quarantined_at=now,
            )
            if migrated["quarantine_id"] == raw_quarantine.get("quarantine_id"):
                quarantines[required_quarantine_index] = migrated
            else:
                quarantines[required_quarantine_index] = {
                    **dict(raw_quarantine),
                    "state": "resolved",
                    "resolved_at": now,
                    "resolution_reason": (
                        "fresh validation upgraded the exact promotion authority and migrated "
                        "its evidence-tombstone retry"
                    ),
                }
                quarantines.append(migrated)
            summary["false_decomposition_cleanup_quarantine"] = _retained_cleanup_quarantines(
                quarantines
            )
        return upgraded

    return dict(update_json_file(plan_state.plan_state_paths().summary_json, mutate))


def _cleanup_transaction_records_for_mutation(
    summary: Mapping[str, Any],
    *,
    transaction_id: str,
) -> tuple[list[object], int | None]:
    """Return a lossless transaction registry plus one authenticated target."""
    raw = summary.get("false_decomposition_cleanup_transactions")
    if raw is None:
        return [], None
    audit = _audit_cleanup_transactions(raw)
    if not isinstance(raw, list):
        raise RuntimeError("false-cleanup transaction registry is not a list")
    indexes = [
        index
        for index, item in enumerate(raw)
        if isinstance(item, Mapping)
        and str(item.get("transaction_id", "") or "").strip() == transaction_id
    ]
    if len(indexes) > 1:
        raise RuntimeError("false-cleanup transaction target is duplicated")
    index = indexes[0] if indexes else None
    if index is not None and audit.records[index].disposition == "ambiguous":
        raise RuntimeError("false-cleanup transaction target is unauthenticated")
    return list(raw), index


def _cleanup_quarantine_records_for_mutation(
    summary: Mapping[str, Any],
    *,
    quarantine_id: str,
) -> tuple[list[object], int | None]:
    """Return a lossless quarantine registry plus one authenticated target."""
    raw = summary.get("false_decomposition_cleanup_quarantine")
    if raw is None:
        return [], None
    audit = _audit_cleanup_quarantines(raw)
    if not isinstance(raw, list):
        raise RuntimeError("false-cleanup quarantine registry is not a list")
    indexes = [
        index
        for index, item in enumerate(raw)
        if isinstance(item, Mapping)
        and str(item.get("quarantine_id", "") or "").strip() == quarantine_id
    ]
    if len(indexes) > 1:
        raise RuntimeError("false-cleanup quarantine target is duplicated")
    index = indexes[0] if indexes else None
    if index is not None and audit.records[index].disposition == "ambiguous":
        raise RuntimeError("false-cleanup quarantine target is unauthenticated")
    return list(raw), index


def _authorized_retry_to_pending(
    transaction: Mapping[str, Any],
    *,
    authorized_at: str = "",
    authorization_reason: str = "",
) -> dict[str, Any]:
    """Restore one exact quarantined plan to replayable pending state.

    Retry authorization changes no sealed source or graph identity.  It removes
    the contradictory quarantine state while retaining the operator's explicit
    authorization as durable provenance on the eventual commit.
    """
    pending = dict(transaction)
    pending["state"] = "pending"
    pending.pop("quarantined_at", None)
    pending.pop("reason", None)
    if authorized_at:
        pending["manual_retry_authorized_at"] = authorized_at
    if authorization_reason:
        pending["manual_retry_reason"] = authorization_reason
    audit = _audit_cleanup_transactions([pending])
    if (
        not audit.records
        or audit.records[0].disposition != "live"
        or audit.records[0].state != "pending"
    ):
        reason = audit.records[0].reason if audit.records else "missing record"
        raise RuntimeError(f"authorized cleanup retry is unauthenticated: {reason}")
    return pending


def _prospective_cleanup_transaction(
    records: list[object],
    *,
    candidate_index: int,
    transaction_id: str,
    promotion_id: str,
) -> None:
    """Reject a would-be registry unless the exact pending target stays unique."""
    audit = _audit_cleanup_transactions(records)
    if candidate_index >= len(audit.records):
        raise RuntimeError("prospective false-cleanup transaction disappeared")
    candidate = audit.records[candidate_index]
    if (
        candidate.record_id != transaction_id
        or candidate.promotion_id != promotion_id
        or candidate.disposition != "live"
        or candidate.state != "pending"
    ):
        raise RuntimeError(
            "prospective false-cleanup transaction is ambiguous"
            + (f": {candidate.reason}" if candidate.reason else "")
        )
    matching_promotions = [
        record
        for record in audit.records
        if record.promotion_id and record.promotion_id == promotion_id
    ]
    if len(matching_promotions) != 1:
        raise RuntimeError("false-cleanup promotion already has another transaction")


def _cleanup_reconciliation_state(*, cleaned: int = 0) -> CleanupReconciliation:
    """Return final live cleanup ambiguity from the durable summary."""
    if not plan_state.plan_state_enabled():
        return CleanupReconciliation(cleaned=cleaned)
    summary = plan_state.load_summary()
    transaction_audit = _audit_cleanup_transactions(
        summary.get("false_decomposition_cleanup_transactions")
    )
    quarantine_audit = _audit_cleanup_quarantines(
        summary.get("false_decomposition_cleanup_quarantine")
    )
    transaction_quarantined = sum(
        record.disposition == "live" and record.state == "quarantined"
        for record in transaction_audit.records
    )
    pending = transaction_audit.pending - transaction_quarantined
    durable_reasons: list[str] = []
    raw_transactions = summary.get("false_decomposition_cleanup_transactions")
    if isinstance(raw_transactions, list):
        for record in transaction_audit.records:
            if record.disposition != "live":
                continue
            raw = raw_transactions[record.index]
            if not isinstance(raw, Mapping):
                continue
            reason = str(
                raw.get("last_reconciliation_reason", "") or raw.get("reason", "") or ""
            ).strip()
            if reason:
                durable_reasons.append(reason)
    raw_quarantines = summary.get("false_decomposition_cleanup_quarantine")
    if isinstance(raw_quarantines, list):
        for record in quarantine_audit.records:
            if record.disposition != "live":
                continue
            raw = raw_quarantines[record.index]
            if isinstance(raw, Mapping):
                reason = str(raw.get("reason", "") or "").strip()
                if reason:
                    durable_reasons.append(reason)
    reasons = [*durable_reasons, *transaction_audit.reasons, *quarantine_audit.reasons]
    if transaction_audit.pending > _TRANSACTION_CAP:
        reasons.insert(
            0,
            f"cleanup transaction capacity exceeded: {transaction_audit.pending} live records "
            f"for a capacity of {_TRANSACTION_CAP}",
        )
    if quarantine_audit.pending > _QUARANTINE_CAP:
        reasons.insert(
            0,
            f"cleanup quarantine capacity exceeded: {quarantine_audit.pending} live records "
            f"for a history capacity of {_QUARANTINE_CAP}",
        )
    return CleanupReconciliation(
        pending=pending,
        cleaned=cleaned,
        quarantined=transaction_quarantined + quarantine_audit.pending,
        reasons=tuple(dict.fromkeys(reason for reason in reasons if reason))[:20],
    )


def cleanup_reconciliation_state() -> CleanupReconciliation:
    """Return the current durable cleanup pause state without replaying it."""
    return _cleanup_reconciliation_state()


def committed_cleanup_records() -> tuple[dict[str, Any], ...]:
    """Return durable cleanup effects that queue state must reconcile."""
    if not plan_state.plan_state_enabled():
        return ()
    records = plan_state.load_summary().get("false_decomposition_cleanup_transactions")
    audit = _audit_cleanup_transactions(records)
    if not isinstance(records, list):
        return ()
    return tuple(
        dict(records[record.index])
        for record in audit.records
        if record.disposition == "terminal" and isinstance(records[record.index], Mapping)
    )


def _cleanup_transaction_hook(stage: str) -> None:
    """Expose deterministic crash boundaries for transaction tests."""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(content: bytes) -> str:
    """Return the digest of exact source bytes without newline normalization."""
    return hashlib.sha256(content).hexdigest()


def _promotion_id(promotion: Mapping[str, Any]) -> str:
    """Return the durable promotion id required for exact archival."""
    return str(promotion.get("promotion_id", "") or "").strip()


def _normalized_absolute_path(value: str) -> Path | None:
    """Return one lexical absolute path without resolving its stored identity."""
    raw = str(value or "").strip()
    path = Path(raw).expanduser()
    if (
        not raw
        or not path.is_absolute()
        or os.path.normpath(str(path)) != str(path)
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        return None
    return path


def _promotion_source_path(
    promotion: Mapping[str, Any], *, project_root: Path
) -> tuple[Path | None, str]:
    """Resolve one immutable promotion path and reject disagreeing aliases."""
    theorem = str(promotion.get("theorem", "") or "").strip()
    operation_path = str(promotion.get("operation_path", "") or "").strip()
    aliases = {
        key: str(promotion.get(key, "") or "").strip()
        for key in ("file", "canonical_file")
        if str(promotion.get(key, "") or "").strip()
    }
    key = str(promotion.get("key", "") or "").strip()
    keyed_file = ""
    if key:
        suffix = f"::{theorem}" if theorem else ""
        if not suffix or not key.endswith(suffix):
            return None, "promotion key does not match its theorem identity"
        keyed_file = key[: -len(suffix)]

    if operation_path:
        durable = _normalized_absolute_path(operation_path)
        if durable is None:
            return None, "promotion operation_path is not a normalized absolute identity"
        for alias_name, alias in aliases.items():
            if alias != operation_path:
                return None, f"promotion {alias_name} differs from immutable operation_path"
        if keyed_file and keyed_file != operation_path:
            return None, "promotion key differs from immutable operation_path"
        return durable, ""

    # Legacy evidence predates operation_path. Resolve each old alias exactly
    # once and require all of them to identify the same canonical source.
    legacy_aliases = [*aliases.values(), *([keyed_file] if keyed_file else [])]
    if not legacy_aliases:
        return None, "promotion lacks a durable source identity"
    canonical = {
        decomposition_provenance.canonical_file(alias, project_root) for alias in legacy_aliases
    }
    if len(canonical) != 1:
        return None, "legacy promotion source aliases resolve to different files"
    path = _normalized_absolute_path(next(iter(canonical)))
    if path is None:
        return None, "legacy promotion source identity is not canonical"
    return path, ""


def _graph_file_reaches_source(
    graph_file: str, *, project_root: Path, source_identity: str
) -> bool:
    """Return whether one graph label resolves to the pinned source identity."""
    candidate = Path(str(graph_file or "").strip()).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        return str(candidate.resolve(strict=True)) == source_identity
    except (OSError, RuntimeError):
        return False


def _promotion_graph_file(
    promotion: Mapping[str, Any],
    *,
    project_root: Path,
    source_identity: str,
) -> tuple[str, str]:
    """Return the bound graph label while authenticating its source mapping."""
    theorem = str(promotion.get("theorem", "") or "").strip()
    node_id = str(promotion.get("node_id", "") or "").strip()
    bound_fields = (
        "operation_path",
        "graph_node_name",
        "graph_node_file",
        "graph_identity_sha256",
    )
    present = [bool(str(promotion.get(field, "") or "").strip()) for field in bound_fields]
    if any(present) and not all(present):
        return "", "promotion graph/source binding is incomplete"
    if all(present):
        operation_path = str(promotion.get("operation_path", "") or "")
        graph_name = str(promotion.get("graph_node_name", "") or "")
        graph_file = str(promotion.get("graph_node_file", "") or "")
        if operation_path != source_identity:
            return "", "promotion graph binding names another source operation"
        if graph_name != theorem:
            return "", "promotion graph node name differs from its theorem"
        if not _graph_file_reaches_source(
            graph_file,
            project_root=project_root,
            source_identity=source_identity,
        ):
            return "", "promotion graph node file does not resolve to its leased source"
        payload = {
            "theorem": theorem,
            "operation_path": operation_path,
            "node_id": node_id,
            "graph_node_name": graph_name,
            "graph_node_file": graph_file,
            "is_main_goal": bool(promotion.get("is_main_goal")),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if str(promotion.get("graph_identity_sha256", "") or "") != expected_hash:
            return "", "promotion graph identity hash does not match its binding"
        if node_id != plan_state.node_id_for(theorem, graph_file):
            return "", "promotion helper graph node id differs from bound graph identity"
        return graph_file, ""

    # Legacy evidence had no separate graph label. Its node identity was based
    # on the canonical source path recorded by the old graph writer.
    if node_id != plan_state.node_id_for(theorem, source_identity):
        return "", "legacy promotion helper graph node id differs from stable identity"
    return source_identity, ""


def _transaction_fingerprint(transaction: Mapping[str, Any]) -> str:
    """Hash every immutable source/graph field used by cleanup replay."""
    identity = {
        field: transaction.get(field)
        for field in _transaction_identity_fields(transaction.get("version"))
    }
    serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _transaction_identity_fields(version: object) -> tuple[str, ...]:
    """Return immutable fields for one supported cleanup transaction version."""
    return (
        _TRANSACTION_V3_IDENTITY_FIELDS
        if version == 3 and not isinstance(version, bool)
        else (
            _TRANSACTION_V2_IDENTITY_FIELDS
            if version == 2 and not isinstance(version, bool)
            else _TRANSACTION_V1_IDENTITY_FIELDS
        )
    )


def _sha256_json(value: object) -> str:
    """Hash one JSON-compatible payload with the transaction wire encoding."""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(serialized)


def _dependent_invalidation_records(
    transaction: Mapping[str, Any],
) -> tuple[dict[str, str], ...]:
    """Return authenticated dependent records from a version-2 transaction."""
    if transaction.get("version") not in {2, 3} or isinstance(transaction.get("version"), bool):
        return ()
    raw = transaction.get("invalidated_dependents")
    if not isinstance(raw, list):
        return ()
    return tuple(dict(item) for item in raw if isinstance(item, Mapping))


def _expected_dependent_records(
    transaction: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Index sealed dependent invalidations by their stable graph identity."""
    return {
        str(record.get("node_id", "") or ""): record
        for record in _dependent_invalidation_records(transaction)
    }


def _dependent_graph_replay_state(
    blueprint: plan_state.Blueprint,
    expected: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str]:
    """Classify sealed dependent nodes as wholly live or wholly retired."""
    if not expected:
        return "absent", ""
    live: list[plan_state.GraphNode] = []
    missing: list[str] = []
    for node_id, record in expected.items():
        matches = [node for node in blueprint.nodes if node.id == node_id]
        if not matches:
            missing.append(node_id)
            continue
        if len(matches) != 1 or not _dependent_matches_sealed_record(matches[0], record):
            return "", "sealed false-dependent graph identity changed during cleanup"
        live.append(matches[0])
    if live and missing:
        return "", "false-dependent graph cleanup is only partially applied"
    if missing:
        retired_ids = set(expected)
        if any(retired_ids & {edge.source, edge.target} for edge in blueprint.edges):
            return "", "retired false-dependent graph id regained an edge"
        return "retired", ""
    return "live", ""


def _dependent_shape_policy(
    transaction: Mapping[str, Any],
    expected: Mapping[str, Mapping[str, Any]],
) -> tuple[str, bool, bool, bool, bool, str]:
    """Return revision/traversal policy sealed by one cleanup transaction."""
    if transaction.get("version") != 3:
        return "", True, False, False, False, ""
    revisions = {
        str(record.get("source_sha256", "") or "")
        for record in expected.values()
        if str(record.get("source_kind", "") or "") == "source_obligation"
    }
    if len(revisions) != 1 or not next(iter(revisions), ""):
        return (
            "",
            False,
            False,
            False,
            False,
            "legacy dependent migration revision is ambiguous",
        )
    return next(iter(revisions)), True, False, True, True, ""


def _promotion_evidence_sha256(promotion: Mapping[str, Any]) -> str:
    """Hash the complete nested promotion evidence bound into a cleanup plan."""
    serialized = json.dumps(
        dict(promotion), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _transaction_promotion(transaction: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return nested promotion evidence only when it remains a mapping."""
    promotion = transaction.get("promotion")
    return promotion if isinstance(promotion, Mapping) else {}


def _transaction_requires_evidence_tombstone(transaction: Mapping[str, Any]) -> bool:
    """Return whether a sealed legacy retry requires its exact graph witness."""
    return bool(_transaction_evidence_quarantine_id(transaction))


def _transaction_evidence_quarantine_id(transaction: Mapping[str, Any]) -> str:
    """Return the legacy quarantine identity sealed into an evidence retry."""
    basis = str(transaction.get("ownership_basis", "") or "")
    _base, marker, quarantine_id = basis.partition(_EVIDENCE_TOMBSTONE_BASIS_MARKER)
    return quarantine_id if marker else ""


def _transaction_base_ownership_basis(transaction: Mapping[str, Any]) -> str:
    """Return the graph/provenance ownership authority without retry policy."""
    basis = str(transaction.get("ownership_basis", "") or "decomposer-graph")
    return basis.partition(_EVIDENCE_TOMBSTONE_BASIS_MARKER)[0]


def _seal_transaction(transaction: Mapping[str, Any]) -> dict[str, Any]:
    """Attach the immutable fingerprint used as the cleanup transaction id."""
    sealed = dict(transaction)
    promotion = sealed.get("promotion")
    if not isinstance(promotion, Mapping):
        raise ValueError("cleanup transaction cannot seal absent promotion evidence")
    sealed["promotion_evidence_sha256"] = _promotion_evidence_sha256(promotion)
    if sealed.get("version") in {2, 3} and not isinstance(sealed.get("version"), bool):
        dependents = sealed.get("invalidated_dependents")
        if not isinstance(dependents, list):
            raise ValueError("cleanup transaction cannot seal malformed dependent invalidations")
        sealed["invalidated_dependents_sha256"] = _sha256_json(dependents)
    fingerprint = _transaction_fingerprint(sealed)
    sealed["immutable_fingerprint"] = fingerprint
    sealed["transaction_id"] = fingerprint
    return sealed


def _transaction_integrity_reason(transaction: Mapping[str, Any]) -> str:
    """Return why a pending replay record is not its originally sealed plan."""
    version = transaction.get("version")
    if version not in {1, 2, 3} or isinstance(version, bool):
        return "cleanup transaction version is invalid"
    if version == 1 and {
        "invalidated_dependents",
        "invalidated_dependents_sha256",
        "migration_from_transaction_id",
    } & set(transaction):
        return "version-1 cleanup transaction carries version-2 dependent evidence"
    if version == 2 and "migration_from_transaction_id" in transaction:
        return "version-2 cleanup transaction carries a legacy-migration identity"
    for field in _transaction_identity_fields(version):
        value = transaction.get(field)
        if field == "version":
            continue
        if not isinstance(value, str) or not value.strip():
            return f"cleanup transaction immutable field {field} is missing or malformed"
    if version in {2, 3}:
        raw_dependents = transaction.get("invalidated_dependents")
        if not isinstance(raw_dependents, list):
            return "cleanup transaction dependent invalidations are not a list"
        if version == 3 and not raw_dependents:
            return "legacy dependent migration has no sealed invalidations"
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        graph_file = str(transaction.get("graph_file", "") or "")
        for raw in raw_dependents:
            expected_fields = (
                _V3_DEPENDENT_INVALIDATION_FIELDS
                if version == 3
                else _DEPENDENT_INVALIDATION_FIELDS
            )
            if not isinstance(raw, Mapping) or set(raw) != expected_fields:
                return "cleanup transaction dependent invalidation is malformed"
            if any(
                not isinstance(raw.get(field), str) or not str(raw.get(field)).strip()
                for field in raw
                if field != "source_sha256"
            ):
                return "cleanup transaction dependent invalidation lacks exact identity evidence"
            node_id = str(raw.get("node_id", "") or "")
            name = str(raw.get("name", "") or "")
            if (
                str(raw.get("file", "") or "") != graph_file
                or node_id != plan_state.node_id_for(name, graph_file)
                or node_id in seen_ids
                or name in seen_names
            ):
                return "cleanup transaction dependent graph identity is ambiguous"
            if any(
                re.fullmatch(r"[0-9a-f]{64}", str(raw.get(field, "") or "")) is None
                for field in ("declaration_sha256",)
            ):
                return "cleanup transaction dependent source identity is malformed"
            if (
                version == 2
                and re.fullmatch(r"[0-9a-f]{64}", str(raw.get("source_sha256", "") or "")) is None
            ):
                return "cleanup transaction dependent source identity is malformed"
            if version == 3:
                source_kind = str(raw.get("source_kind", "") or "")
                source_sha256 = str(raw.get("source_sha256", "") or "")
                if source_kind == "source_obligation":
                    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
                        return "cleanup transaction dependent source identity is malformed"
                elif source_kind == "graph_artifact":
                    if source_sha256:
                        return "cleanup transaction graph artifact claims a source revision"
                else:
                    return "cleanup transaction dependent source kind is invalid"
            seen_ids.add(node_id)
            seen_names.add(name)
        if _sha256_json(raw_dependents) != str(
            transaction.get("invalidated_dependents_sha256", "") or ""
        ):
            return "cleanup transaction dependent invalidations differ from their sealed hash"
    if (
        version == 3
        and re.fullmatch(
            r"[0-9a-f]{64}", str(transaction.get("migration_from_transaction_id", "") or "")
        )
        is None
    ):
        return "cleanup transaction legacy-migration identity is malformed"
    path = _normalized_absolute_path(str(transaction.get("file", "") or ""))
    if path is None:
        return "cleanup transaction file is not a normalized absolute identity"
    expected = _transaction_fingerprint(transaction)
    if str(transaction.get("immutable_fingerprint", "") or "") != expected:
        return "cleanup transaction immutable fingerprint does not match its payload"
    if str(transaction.get("transaction_id", "") or "") != expected:
        return "cleanup transaction id does not match its immutable fingerprint"
    promotion = transaction.get("promotion")
    if not isinstance(promotion, Mapping):
        return "cleanup transaction lost its promotion evidence"
    if _promotion_evidence_sha256(promotion) != str(
        transaction.get("promotion_evidence_sha256", "") or ""
    ):
        return "cleanup transaction promotion evidence differs from its sealed plan"
    if _promotion_id(promotion) != str(transaction.get("promotion_id", "") or ""):
        return "cleanup transaction promotion identity differs from its sealed plan"
    if str(promotion.get("theorem", "") or "") != str(transaction.get("helper", "") or ""):
        return "cleanup transaction promotion theorem differs from its sealed helper"
    if str(promotion.get("node_id", "") or "") != str(transaction.get("helper_node_id", "") or ""):
        return "cleanup transaction promotion graph node differs from its sealed helper"
    if str(promotion.get("declaration_signature_sha256", "") or "") != str(
        transaction.get("helper_signature_sha256", "") or ""
    ):
        return "cleanup transaction promotion signature differs from its sealed helper"
    if str(promotion.get("graph_identity_sha256", "") or ""):
        if str(promotion.get("operation_path", "") or "") != str(transaction.get("file", "") or ""):
            return "cleanup transaction promotion operation differs from its sealed source"
        if str(promotion.get("graph_node_name", "") or "") != str(
            transaction.get("helper", "") or ""
        ):
            return "cleanup transaction promotion graph name differs from its sealed helper"
        if str(promotion.get("graph_node_file", "") or "") != str(
            transaction.get("graph_file", "") or ""
        ):
            return "cleanup transaction promotion graph file differs from its sealed graph"

    source_after = transaction.get("source_after")
    if not isinstance(source_after, str) or not source_after:
        return "cleanup transaction lost its exact post-edit source payload"
    if _sha256_bytes(source_after.encode("utf-8")) != str(
        transaction.get("source_after_sha256", "") or ""
    ):
        return "cleanup transaction post-edit source differs from its sealed hash"
    restored_parent = transaction.get("parent_restored_declaration")
    if not isinstance(restored_parent, str) or not restored_parent:
        return "cleanup transaction lost its exact restored parent payload"
    if _sha256_text(restored_parent) != str(
        transaction.get("parent_restored_declaration_sha256", "") or ""
    ):
        return "cleanup transaction restored parent differs from its sealed hash"
    parent_name = str(transaction.get("parent", "") or "")
    restored = decomposition_provenance.declaration_slice(restored_parent, parent_name)
    if restored is None:
        return "cleanup transaction restored parent payload is not one exact declaration"
    if restored.signature_sha256 != str(transaction.get("parent_signature_sha256", "") or ""):
        return "cleanup transaction restored parent signature differs from its sealed plan"
    if _graph_statement(restored_parent, parent_name) != str(
        transaction.get("parent_restored_statement", "") or ""
    ):
        return "cleanup transaction restored graph statement differs from its source payload"
    return ""


def _record_event(event: str, message: str, **details: Any) -> None:
    """Best-effort journal, activity, and outcome emission for cleanup state."""
    with contextlib.suppress(Exception):
        plan_state.append_journal_event({"event": event, **details})
    with contextlib.suppress(Exception):
        append_workflow_activity(event, message, **details)
    with contextlib.suppress(Exception):
        append_workflow_outcome(event, details)


def _quarantine_entry(
    promotion: Mapping[str, Any],
    *,
    reason: str,
    provenance_id: str = "",
    quarantined_at: str = "",
) -> dict[str, Any]:
    """Build the authenticated quarantine envelope for one cleanup decision."""
    promotion_record = dict(promotion)
    promotion_id = _promotion_id(promotion_record)
    recorded_at = quarantined_at or _now_iso()
    identity = hashlib.sha256(f"{promotion_id}\0{provenance_id}\0{reason}".encode()).hexdigest()
    return {
        "quarantine_id": identity,
        "state": "quarantined",
        "quarantined_at": recorded_at,
        "reason": reason,
        "promotion": promotion_record,
        "provenance_id": provenance_id,
    }


def _live_legacy_evidence_quarantines(
    summary: Mapping[str, Any], promotion: Mapping[str, Any]
) -> tuple[tuple[int, str], ...]:
    """Return exact authenticated live evidence quarantines for one promotion."""
    raw_quarantines = summary.get("false_decomposition_cleanup_quarantine")
    audit = _audit_cleanup_quarantines(raw_quarantines)
    if not isinstance(raw_quarantines, list):
        return ()
    matches: list[tuple[int, str]] = []
    for record in audit.records:
        if record.disposition != "live":
            continue
        raw = raw_quarantines[record.index]
        if not isinstance(raw, Mapping):
            continue
        nested = raw.get("promotion")
        if (
            raw.get("reason") == _LEGACY_EVIDENCE_QUARANTINE_REASON
            and isinstance(nested, Mapping)
            and dict(nested) == dict(promotion)
        ):
            matches.append((record.index, record.record_id))
    return tuple(matches)


def _exact_live_legacy_evidence_quarantine_id(
    promotion: Mapping[str, Any],
) -> tuple[str, str]:
    """Resolve one exact live legacy evidence exception for a promotion."""
    matches = _live_legacy_evidence_quarantines(plan_state.load_summary(), promotion)
    if len(matches) != 1 or not matches[0][1]:
        return "", "legacy evidence cleanup quarantine is missing or ambiguous"
    return matches[0][1], ""


def _transaction_evidence_quarantine_index(
    transaction: Mapping[str, Any], summary: Mapping[str, Any]
) -> tuple[int | None, str]:
    """Authenticate the one live legacy quarantine sealed into a cleanup plan."""
    quarantine_id = _transaction_evidence_quarantine_id(transaction)
    if not quarantine_id:
        return None, ""
    if re.fullmatch(r"[0-9a-f]{64}", quarantine_id) is None:
        return None, "cleanup transaction evidence quarantine identity is malformed"
    matches = _live_legacy_evidence_quarantines(summary, _transaction_promotion(transaction))
    exact = [index for index, record_id in matches if record_id == quarantine_id]
    if len(matches) != 1 or len(exact) != 1:
        return None, "cleanup transaction evidence quarantine is missing or ambiguous"
    return exact[0], ""


def _quarantine_candidate(
    promotion: Mapping[str, Any], *, reason: str, provenance_id: str = ""
) -> None:
    """Persist a deduplicated fail-closed cleanup decision without source edits."""
    promotion_record = dict(promotion)
    promotion_id = _promotion_id(promotion_record)
    entry = _quarantine_entry(
        promotion_record,
        reason=reason,
        provenance_id=provenance_id,
    )
    identity = str(entry["quarantine_id"])

    def mutate(summary: dict[str, Any]) -> None:
        records, index = _cleanup_quarantine_records_for_mutation(
            summary,
            quarantine_id=identity,
        )
        if index is None:
            records.append(entry)
        else:
            records[index] = entry
        summary["false_decomposition_cleanup_quarantine"] = _retained_cleanup_quarantines(records)

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    _record_event(
        "false-decomposition-cleanup-quarantined",
        f"Quarantined false-helper cleanup for {promotion_record.get('theorem', '[unknown]')}",
        theorem=str(promotion_record.get("theorem", "") or ""),
        file=str(promotion_record.get("file", "") or ""),
        promotion_id=promotion_id,
        provenance_id=provenance_id,
        reason=reason,
    )


def authorize_cleanup_quarantine_retry(quarantine_id: str, *, reason: str) -> bool:
    """Atomically authorize and restore one exact quarantined cleanup plan."""
    target = str(quarantine_id or "").strip()
    explanation = str(reason or "").strip()
    if not target or not explanation or not plan_state.plan_state_enabled():
        return False

    def mutate(summary: dict[str, Any]) -> bool:
        quarantine, quarantine_index = _cleanup_quarantine_records_for_mutation(
            summary,
            quarantine_id=target,
        )
        if quarantine_index is None:
            return False
        raw_selected = quarantine[quarantine_index]
        if (
            not isinstance(raw_selected, Mapping)
            or str(raw_selected.get("state", "") or "") != "quarantined"
        ):
            return False
        authorized_at = _now_iso()
        selected = {
            **dict(raw_selected),
            "state": "resolved",
            "resolved_at": authorized_at,
            "resolution_reason": explanation,
        }

        promotion = selected.get("promotion")
        promotion_id = (
            str(promotion.get("promotion_id", "") or "") if isinstance(promotion, Mapping) else ""
        )
        provenance_id = str(selected.get("provenance_id", "") or "")
        raw_transactions = summary.get("false_decomposition_cleanup_transactions")
        transaction_audit = _audit_cleanup_transactions(raw_transactions)
        if raw_transactions is None:
            transactions: list[object] = []
        elif isinstance(raw_transactions, list):
            transactions = list(raw_transactions)
        else:
            raise RuntimeError("false-cleanup transaction registry is not a list")
        matching_indexes: list[int] = []
        for index, item in enumerate(transactions):
            if not isinstance(item, Mapping):
                continue
            same_promotion = (
                promotion_id and str(item.get("promotion_id", "") or "") == promotion_id
            )
            same_provenance = (
                provenance_id and str(item.get("provenance_id", "") or "") == provenance_id
            )
            exact_identity = bool(promotion_id or provenance_id) and (
                (not promotion_id or same_promotion) and (not provenance_id or same_provenance)
            )
            if str(item.get("state", "") or "") == "quarantined" and exact_identity:
                matching_indexes.append(index)
        if len(matching_indexes) > 1 or any(
            transaction_audit.records[index].disposition == "ambiguous"
            for index in matching_indexes
        ):
            raise RuntimeError("cleanup retry transaction target is ambiguous")
        if matching_indexes:
            index = matching_indexes[0]
            item = transactions[index]
            assert isinstance(item, Mapping)
            pending_retry = _authorized_retry_to_pending(
                item,
                authorized_at=authorized_at,
                authorization_reason=explanation,
            )
            transactions[index] = pending_retry
            _prospective_cleanup_transaction(
                transactions,
                candidate_index=index,
                transaction_id=str(pending_retry.get("transaction_id", "") or ""),
                promotion_id=str(pending_retry.get("promotion_id", "") or ""),
            )
        quarantine[quarantine_index] = selected
        summary["false_decomposition_cleanup_quarantine"] = _retained_cleanup_quarantines(
            quarantine
        )
        summary["false_decomposition_cleanup_transactions"] = _retained_cleanup_transactions(
            transactions
        )
        return True

    return bool(update_json_file(plan_state.plan_state_paths().summary_json, mutate))


def _identifier_names_helper(identifier: str, helper_name: str) -> bool:
    """Return whether one complete Lean identifier can resolve to ``helper_name``."""
    candidate = str(identifier or "").removeprefix("_root_.")
    helper = str(helper_name or "").removeprefix("_root_.")
    if not candidate or not helper:
        return False
    return (
        candidate == helper or candidate.endswith(f".{helper}") or helper.endswith(f".{candidate}")
    )


def _text_references_helper(text: str, helper_name: str) -> bool:
    """Return whether Lean code contains a token-level reference to a helper."""
    sanitized = _strip_lean_comments_and_strings(str(text or ""))
    return any(
        _identifier_names_helper(match.group(1), helper_name)
        for match in _LEAN_IDENTIFIER_RE.finditer(sanitized)
    )


def _proof_references_helper(declaration: str, helper_name: str) -> bool:
    """Return whether the declaration proof, excluding its name, uses a helper."""
    _separator, present, proof = str(declaration or "").partition(":=")
    return bool(present) and _text_references_helper(proof, helper_name)


def _source_has_external_reference(
    source: str,
    *,
    helper_name: str,
    parent_name: str,
    retired_names: tuple[str, ...] = (),
) -> bool:
    """Return whether source outside the owned helper/parent uses the helper."""
    spans: list[tuple[int, int]] = []
    for declaration_name in (helper_name, parent_name, *retired_names):
        declaration = decomposition_provenance.declaration_slice(source, declaration_name)
        if declaration is not None:
            spans.append((declaration.metadata_start, declaration.end))
    outside = source
    for start, end in sorted(spans, reverse=True):
        outside = outside[:start] + ("\n" * outside[start:end].count("\n")) + outside[end:]
    return _text_references_helper(outside, helper_name)


def _graph_helper(
    promotion: Mapping[str, Any], *, source_identity: str, project_root: Path
) -> tuple[plan_state.Blueprint, plan_state.GraphNode | None, str, str]:
    """Resolve a unique promoted helper node without fallback identity guesses."""
    blueprint = plan_state.load_blueprint()
    node_id = str(promotion.get("node_id", "") or "").strip()
    theorem = str(promotion.get("theorem", "") or "").strip()
    if not node_id:
        return blueprint, None, "", "promotion does not record an exact helper graph node id"
    graph_file, graph_reason = _promotion_graph_file(
        promotion,
        project_root=project_root,
        source_identity=source_identity,
    )
    if not graph_file:
        return blueprint, None, "", graph_reason
    id_matches = [node for node in blueprint.nodes if node.id == node_id]
    identity_matches = [
        node for node in blueprint.nodes if node.name == theorem and node.file == graph_file
    ]
    if len(id_matches) != 1 or len(identity_matches) != 1:
        return (
            blueprint,
            None,
            graph_file,
            "dependency graph helper identity is missing or duplicated",
        )
    if id_matches[0] != identity_matches[0]:
        return (
            blueprint,
            None,
            graph_file,
            "promotion helper graph node id resolves to another declaration",
        )
    source_aliases = [
        node
        for node in blueprint.nodes
        if node.name == theorem
        and _graph_file_reaches_source(
            node.file,
            project_root=project_root,
            source_identity=source_identity,
        )
    ]
    if len(source_aliases) != 1 or source_aliases[0] != id_matches[0]:
        return (
            blueprint,
            None,
            graph_file,
            "dependency graph helper has ambiguous file aliases for one source",
        )
    return blueprint, id_matches[0], graph_file, ""


def _unique_parent_node(
    blueprint: plan_state.Blueprint,
    *,
    parent_name: str,
    graph_file: str,
    expected_id: str = "",
    project_root: Path | None = None,
    source_identity: str = "",
) -> tuple[plan_state.GraphNode | None, str]:
    """Resolve one exact parent graph identity and reject aliases or duplicates."""
    stable_id = plan_state.node_id_for(parent_name, graph_file)
    if expected_id and expected_id != stable_id:
        return None, "cleanup parent graph node id differs from stable identity"
    id_matches = [node for node in blueprint.nodes if node.id == stable_id]
    identity_matches = [
        node for node in blueprint.nodes if node.name == parent_name and node.file == graph_file
    ]
    if len(id_matches) != 1 or len(identity_matches) != 1:
        return None, "dependency graph parent identity is missing or duplicated"
    if id_matches[0] != identity_matches[0]:
        return None, "cleanup parent graph node id resolves to another declaration"
    if project_root is not None and source_identity:
        source_aliases = [
            node
            for node in blueprint.nodes
            if node.name == parent_name
            and _graph_file_reaches_source(
                node.file,
                project_root=project_root,
                source_identity=source_identity,
            )
        ]
        if len(source_aliases) != 1 or source_aliases[0] != id_matches[0]:
            return None, "dependency graph parent has ambiguous file aliases for one source"
    return id_matches[0], ""


def _source_matches_graph_node(source_text: str, node: plan_state.GraphNode) -> bool:
    """Return whether source contains one exact declaration represented by a graph node."""
    matches = [
        entry
        for entry in _declaration_line_index_from_text(source_text)
        if str(entry.get("name", "") or "").strip() == node.name
    ]
    declaration = decomposition_provenance.declaration_slice(source_text, node.name)
    return (
        len(matches) == 1
        and declaration is not None
        and declaration.text.strip() == node.statement.strip()
    )


def _dependent_matches_sealed_record(
    node: plan_state.GraphNode,
    record: Mapping[str, Any],
) -> bool:
    """Return whether one live graph node is the exact sealed invalidation target."""
    source_kind = str(record.get("source_kind", "source_obligation") or "")
    source_matches = (
        node.source_sha256 == str(record.get("source_sha256", "") or "")
        if source_kind == "source_obligation"
        else source_kind == "graph_artifact" and not node.source_sha256
    )
    return bool(
        node.id == str(record.get("node_id", "") or "")
        and node.name == str(record.get("name", "") or "")
        and node.file == str(record.get("file", "") or "")
        and source_matches
        and _sha256_text(node.statement) == str(record.get("declaration_sha256", "") or "")
    )


def _dependent_invalidation_closure(
    blueprint: plan_state.Blueprint,
    *,
    helper: plan_state.GraphNode,
    parent: plan_state.GraphNode,
    promoted_source_revision: str,
    source_text: str,
    expected_records: Mapping[str, Mapping[str, Any]] | None = None,
    transitive: bool = True,
    detach_incoming_dependents: bool = False,
    allow_graph_only_dependents: bool = False,
    require_parent_split: bool = False,
) -> tuple[tuple[plan_state.GraphNode, ...], tuple[plan_state.GraphEdge, ...], str]:
    """Return exact unresolved decomposer nodes made impossible by a false helper.

    The closure follows incoming ``depends_on`` edges. Only same-revision,
    same-file pending obligations with an unresolved source declaration are removable.
    This includes untouched ``stated`` stubs and queued ``conjectured`` stubs;
    active, reviewed, and verified states remain outside cleanup authority.
    A legacy migration may additionally retire source-less planner artifacts
    that belong to the same parent. Verified, externally owned, or
    evidence-bearing dependents make cleanup pause instead of deleting source
    beyond its authority.
    """
    invalidated: dict[str, plan_state.GraphNode] = {}
    frontier = {helper.id}
    while frontier:
        target_ids = set(frontier)
        frontier.clear()
        for edge in blueprint.edges:
            if edge.kind != "depends_on" or edge.target not in target_ids:
                continue
            if edge.source in {helper.id, parent.id, *invalidated}:
                continue
            matches = [node for node in blueprint.nodes if node.id == edge.source]
            if len(matches) != 1:
                return (), (), "dependent false-helper graph identity is missing or duplicated"
            dependent = matches[0]
            aliases = [
                node
                for node in blueprint.nodes
                if node.name == dependent.name and node.file == helper.file
            ]
            record = (expected_records or {}).get(dependent.id)
            source_matches = not source_text or _source_matches_graph_node(source_text, dependent)
            sealed_match = record is not None and _dependent_matches_sealed_record(
                dependent, record
            )
            record_kind = (
                str(record.get("source_kind", "source_obligation") or "")
                if record is not None
                else ""
            )
            unresolved_statement = bool(
                re.search(
                    r"\b(?:sorry|admit)\b",
                    _strip_lean_comments_and_strings(dependent.statement),
                )
            )
            source_owned = bool(
                dependent.generated_by == "decomposer"
                and promoted_source_revision
                and dependent.source_sha256 == promoted_source_revision
                and (
                    (record is None and source_matches)
                    or (record_kind == "source_obligation" and sealed_match)
                )
            )
            graph_only_owned = bool(
                allow_graph_only_dependents
                and dependent.generated_by in {"decomposer", "planner"}
                and not dependent.source_sha256
                and source_text
                and decomposition_provenance.declaration_slice(source_text, dependent.name) is None
                and (record is None or (record_kind == "graph_artifact" and sealed_match))
            )
            parent_split = plan_state.GraphEdge(
                source=dependent.id,
                target=parent.id,
                kind="split_of",
            )
            outgoing_splits = [
                candidate
                for candidate in blueprint.edges
                if candidate.kind == "split_of" and candidate.source == dependent.id
            ]
            if (
                len(aliases) != 1
                or aliases[0] != dependent
                or len([node for node in blueprint.nodes if node.id == dependent.id]) != 1
                or dependent.id != plan_state.node_id_for(dependent.name, helper.file)
                or dependent.file != helper.file
                or dependent.status not in _UNRESOLVED_DECOMPOSER_STATUSES
                or not (source_owned or graph_only_owned)
                or not unresolved_statement
                or blueprint.edges.count(edge) != 1
                or (
                    require_parent_split
                    and (
                        outgoing_splits != [parent_split]
                        or blueprint.edges.count(parent_split) != 1
                    )
                )
            ):
                return (), (), "another graph node depends on the false helper"
            invalidated[dependent.id] = dependent
            if transitive:
                frontier.add(dependent.id)

    invalidated_ids = set(invalidated)
    removable: list[plan_state.GraphEdge] = []
    for edge in blueprint.edges:
        incident_ids = {edge.source, edge.target} & invalidated_ids
        if not incident_ids:
            continue
        if blueprint.edges.count(edge) != 1:
            return (), (), "false-dependent structural edge is duplicated"
        if edge.kind == "evidence":
            return (), (), "false-dependent graph node has evidence that cleanup must preserve"
        if edge.kind == "depends_on":
            incoming_external = edge.target in invalidated_ids and edge.source not in {
                *invalidated_ids,
                parent.id,
            }
            if incoming_external:
                if not detach_incoming_dependents:
                    return (), (), "another graph node depends on a false-dependent obligation"
                external = [node for node in blueprint.nodes if node.id == edge.source]
                if len(external) != 1 or external[0].status in {"audited", "proved"}:
                    return (
                        (),
                        (),
                        "verified or ambiguous graph authority depends on the false obligation",
                    )
        elif edge.kind == "split_of":
            if edge.source in invalidated_ids and edge.target not in {
                *invalidated_ids,
                helper.id,
                parent.id,
            }:
                return (), (), "false-dependent obligation belongs to another graph parent"
            if edge.target in invalidated_ids and edge.source not in invalidated_ids:
                return (), (), "false-dependent obligation has an unowned nested decomposition"
        else:
            return (), (), "false-dependent obligation has an unsupported structural edge"
        removable.append(edge)

    if expected_records is not None and set(expected_records) != invalidated_ids:
        return (), (), "sealed false-dependent invalidation set changed before cleanup"
    ordered = tuple(sorted(invalidated.values(), key=lambda node: (node.name, node.id)))
    return ordered, tuple(removable), ""


def _graph_cleanup_shape(
    blueprint: plan_state.Blueprint,
    *,
    helper: plan_state.GraphNode,
    parent: plan_state.GraphNode,
    promotion: Mapping[str, Any],
    source_text: str = "",
    expected_dependents: Mapping[str, Mapping[str, Any]] | None = None,
    dependent_source_revision: str = "",
    transitive_dependents: bool = True,
    detach_incoming_dependents: bool = False,
    allow_graph_only_dependents: bool = False,
    require_parent_split: bool = False,
    evidence_source_revision: str = "",
) -> tuple[_GraphCleanupShape | None, str]:
    """Classify exact structural ownership and promotion-bound evidence.

    The one proof declaration sealed into the source-negation promotion may
    remain connected to a false helper as forensic evidence. Other verified
    prover-edit findings from the exact promoted source revision are
    non-authoritative evidence and may share that tombstone. Exact unresolved
    same-revision decomposer nodes depending on the false helper form a sealed
    invalidation closure; they do not remain active tombstones. Nested helper
    decompositions are removable only as complete, same-file decomposer-owned
    edge pairs. Every other incident edge remains an ambiguity.
    """
    incident = tuple(edge for edge in blueprint.edges if helper.id in {edge.source, edge.target})
    proof_declaration = str(promotion.get("proof_declaration", "") or "").strip()
    promoted_source_revision = str(promotion.get("source_revision_sha256", "") or "").strip()
    preserved_evidence: list[plan_state.GraphEdge] = []
    structural: list[plan_state.GraphEdge] = []
    promotion_evidence_count = 0
    evidence_edge_count = sum(edge.kind == "evidence" for edge in incident)
    evidence_revision = evidence_source_revision or promoted_source_revision

    def source_matches(node: plan_state.GraphNode) -> bool:
        return not source_text or _source_matches_graph_node(source_text, node)

    for edge in incident:
        if edge.kind != "evidence":
            structural.append(edge)
            continue
        if not proof_declaration or edge.target != helper.id or edge.source == helper.id:
            return None, "helper graph node has unrelated evidence edges that cleanup must preserve"
        evidence_nodes = [node for node in blueprint.nodes if node.id == edge.source]
        if len(evidence_nodes) != 1:
            return None, "promotion evidence graph identity is missing or duplicated"
        evidence_node = evidence_nodes[0]
        expected_evidence_id = plan_state.node_id_for(evidence_node.name, helper.file)
        evidence_aliases = [
            node
            for node in blueprint.nodes
            if node.name == evidence_node.name and node.file == helper.file
        ]
        if (
            len(evidence_aliases) != 1
            or evidence_aliases[0] != evidence_node
            or evidence_node.id != expected_evidence_id
            or evidence_node.file != helper.file
            or evidence_node.status != "proved"
        ):
            return None, "evidence edge is not the unique proved promotion proof declaration"
        if incident.count(edge) != 1:
            return None, "promotion evidence edge is duplicated"
        if evidence_node.name == proof_declaration:
            binding_is_current = bool(
                evidence_revision
                and evidence_node.source_sha256 == evidence_revision
                and evidence_node.statement.strip()
                and source_matches(evidence_node)
                and not re.search(
                    r"\b(?:sorry|admit)\b",
                    _strip_lean_comments_and_strings(evidence_node.statement),
                )
            )
            legacy_binding_is_absent = bool(
                not evidence_node.source_sha256 and not evidence_node.statement.strip()
            )
            # A single old promotion edge predates graph source snapshots and
            # remains safe because the promotion itself is freshly rechecked.
            # Multiple evidence edges use the new, stricter source-bound shape.
            if not binding_is_current and (evidence_edge_count > 1 or not legacy_binding_is_absent):
                return (
                    None,
                    "evidence edge is not the unique proved promotion proof declaration",
                )
            promotion_evidence_count += 1
        elif (
            evidence_node.generated_by != "prover-edit"
            or not evidence_revision
            or evidence_node.source_sha256 != evidence_revision
            or not evidence_node.statement.strip()
            or not source_matches(evidence_node)
            or re.search(
                r"\b(?:sorry|admit)\b",
                _strip_lean_comments_and_strings(evidence_node.statement),
            )
        ):
            return None, "helper graph node has unrelated evidence edges that cleanup must preserve"
        preserved_evidence.append(edge)

    if preserved_evidence and promotion_evidence_count != 1:
        return None, "evidence edge is not the unique proved promotion proof declaration"

    parent_edges = {
        plan_state.GraphEdge(source=parent.id, target=helper.id, kind="depends_on"),
        plan_state.GraphEdge(source=helper.id, target=parent.id, kind="split_of"),
    }
    unclassified = [edge for edge in structural if edge not in parent_edges]
    parent_edge_counts = {edge: structural.count(edge) for edge in parent_edges}
    for edge, count in parent_edge_counts.items():
        if count > 1:
            return None, "owned parent/helper structural edge is duplicated"
    if sum(parent_edge_counts.values()) == 1:
        return None, "parent/helper dependency is not one complete owned edge pair"

    invalidated_dependents, invalidated_edges, invalidation_reason = (
        _dependent_invalidation_closure(
            blueprint,
            helper=helper,
            parent=parent,
            promoted_source_revision=(dependent_source_revision or promoted_source_revision),
            source_text=source_text,
            expected_records=expected_dependents,
            transitive=transitive_dependents,
            detach_incoming_dependents=detach_incoming_dependents,
            allow_graph_only_dependents=allow_graph_only_dependents,
            require_parent_split=require_parent_split,
        )
    )
    if invalidation_reason:
        return None, invalidation_reason
    invalidated_root_edges = {
        edge
        for edge in unclassified
        if edge.kind == "depends_on"
        and edge.target == helper.id
        and any(node.id == edge.source for node in invalidated_dependents)
    }
    nested_structural = [edge for edge in unclassified if edge not in invalidated_root_edges]

    child_ids = {
        edge.target if edge.source == helper.id else edge.source for edge in nested_structural
    }
    removable = [edge for edge in structural if edge in parent_edges]
    for child_id in sorted(child_ids):
        child_matches = [node for node in blueprint.nodes if node.id == child_id]
        if len(child_matches) != 1:
            return None, "nested helper graph identity is missing or duplicated"
        child = child_matches[0]
        child_aliases = [
            node for node in blueprint.nodes if node.name == child.name and node.file == helper.file
        ]
        if (
            len(child_aliases) != 1
            or child_aliases[0] != child
            or child.id != plan_state.node_id_for(child.name, helper.file)
            or child.file != helper.file
            or child.generated_by != "decomposer"
        ):
            return None, "another graph node depends on the false helper"
        pair = (
            plan_state.GraphEdge(source=helper.id, target=child.id, kind="depends_on"),
            plan_state.GraphEdge(source=child.id, target=helper.id, kind="split_of"),
        )
        if any(structural.count(expected) != 1 for expected in pair):
            return None, "nested helper dependency is not one complete owned edge pair"
        child_incident = [
            edge for edge in nested_structural if child.id in {edge.source, edge.target}
        ]
        if set(child_incident) != set(pair) or len(child_incident) != len(pair):
            return None, "another graph node depends on the false helper"
        removable.extend(pair)

    if len(removable) + len(invalidated_root_edges) != len(structural):
        return None, "another graph node depends on the false helper"
    removable.extend(edge for edge in invalidated_edges if edge not in removable)
    return (
        _GraphCleanupShape(
            preserved_evidence=tuple(preserved_evidence),
            invalidated_dependents=invalidated_dependents,
            removable_structural=tuple(removable),
        ),
        "",
    )


def _graph_dependencies_are_owned(
    blueprint: plan_state.Blueprint,
    *,
    helper: plan_state.GraphNode,
    parent: plan_state.GraphNode,
    promotion: Mapping[str, Any],
) -> tuple[bool, str]:
    """Return whether every incident edge has exact cleanup authority."""
    shape, reason = _graph_cleanup_shape(
        blueprint,
        helper=helper,
        parent=parent,
        promotion=promotion,
    )
    return shape is not None, reason


def _graph_statement(declaration: str, name: str) -> str:
    """Return the normalized proposition stored in a dependency-graph node."""
    parsed = decomposition_provenance.declaration_slice(declaration, name)
    if parsed is None:
        return ""
    statement = re.sub(
        r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?(?:theorem|lemma)\s+\S+",
        "",
        parsed.signature,
    )
    return " ".join(statement.split())


def _build_source_transaction(
    promotion: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    current_source: str,
    file_identity: str,
    invalidated_dependents: tuple[plan_state.GraphNode, ...] = (),
) -> tuple[dict[str, Any] | None, str]:
    """Build an exact surgical source plan after all ownership checks pass."""
    helper_name = str(promotion.get("theorem", "") or "").strip()
    parent_name = str(provenance.get("parent", "") or "").strip()
    helper = decomposition_provenance.declaration_slice(current_source, helper_name)
    parent = decomposition_provenance.declaration_slice(current_source, parent_name)
    if helper is None:
        return None, "current false helper declaration is absent"
    if parent is None:
        return None, "current decomposition parent declaration is absent"
    promoted_signature = str(promotion.get("declaration_signature_sha256", "") or "").strip()
    helper_full_signature = decomposition_provenance.full_declaration_signature_sha256(helper.text)
    if not promoted_signature or helper_full_signature != promoted_signature:
        return None, _SIGNATURE_MISMATCH_REASON
    provenance_parent_signature = str(provenance.get("parent_signature_sha256", "") or "").strip()
    if parent.signature_sha256 != provenance_parent_signature:
        return None, "current parent statement differs from the durable pre-edit signature"
    restored_parent = str(provenance.get("parent_before_declaration", "") or "")
    if _sha256_text(restored_parent) != str(
        provenance.get("parent_before_declaration_sha256", "") or ""
    ):
        return None, "durable pre-edit parent declaration hash is invalid"
    restored = decomposition_provenance.declaration_slice(restored_parent, parent_name)
    if restored is None or restored.signature_sha256 != parent.signature_sha256:
        return None, "durable pre-edit parent declaration has a different statement"
    if decomposition_provenance.full_declaration_signature_sha256(
        restored.text
    ) != decomposition_provenance.full_declaration_signature_sha256(parent.text):
        return None, "current parent statement differs from the durable pre-edit declaration"
    if not re.search(r"\b(?:sorry|admit)\b", _strip_lean_comments_and_strings(restored_parent)):
        return None, "durable pre-edit parent does not reopen an unresolved declaration"
    parent_references_helper = _proof_references_helper(parent.text, helper_name)
    parent_is_unchanged_insertion_target = parent.declaration_sha256 == restored.declaration_sha256
    if not parent_references_helper and not parent_is_unchanged_insertion_target:
        return None, "current parent proof no longer references the false helper"
    dependent_slices: list[tuple[plan_state.GraphNode, Any]] = []
    for dependent in invalidated_dependents:
        declaration = decomposition_provenance.declaration_slice(current_source, dependent.name)
        if (
            declaration is None
            or declaration.text.strip() != dependent.statement.strip()
            or not re.search(
                r"\b(?:sorry|admit)\b",
                _strip_lean_comments_and_strings(declaration.text),
            )
        ):
            return None, "false-dependent source obligation changed before cleanup"
        dependent_slices.append((dependent, declaration))
    retired_names = tuple(dependent.name for dependent in invalidated_dependents)
    if _source_has_external_reference(
        current_source,
        helper_name=helper_name,
        parent_name=parent_name,
        retired_names=retired_names,
    ):
        return None, "another same-file command references the false helper"
    for dependent in invalidated_dependents:
        if _source_has_external_reference(
            current_source,
            helper_name=dependent.name,
            parent_name=parent_name,
            retired_names=(helper_name, *retired_names),
        ):
            return None, "another same-file command references a false-dependent obligation"

    # Remove metadata attached to the helper as part of the declaration.  Keep
    # the parent's current documentation/attributes and replace only its exact
    # theorem/lemma command with the durable pre-edit command.
    removals = [
        (helper.metadata_start, helper.end, ""),
        *(
            (declaration.metadata_start, declaration.end, "")
            for _node, declaration in dependent_slices
        ),
    ]
    replacements = [(parent.start, parent.end, restored_parent)]
    spans = sorted([*removals, *replacements], key=lambda item: item[0], reverse=True)
    for previous, following in zip(spans, spans[1:], strict=False):
        if following[1] > previous[0]:
            return None, "cleanup source spans overlap unexpectedly"
    after_source = current_source
    for start, end, replacement_text in spans:
        if start < 0 or end < start or end > len(after_source):
            return None, "cleanup source span is invalid"
        after_source = after_source[:start] + replacement_text + after_source[end:]
    if decomposition_provenance.declaration_slice(after_source, helper_name) is not None:
        return None, "false helper remains after planned surgical removal"
    if any(
        decomposition_provenance.declaration_slice(after_source, name) is not None
        for name in retired_names
    ):
        return None, "false-dependent obligation remains after planned surgical removal"
    reopened_parent = decomposition_provenance.declaration_slice(after_source, parent_name)
    if reopened_parent is None or reopened_parent.declaration_sha256 != restored.declaration_sha256:
        return None, "parent restoration plan does not reproduce the durable declaration"

    dependent_records = [
        {
            "node_id": node.id,
            "name": node.name,
            "file": node.file,
            "source_sha256": node.source_sha256,
            "declaration_sha256": declaration.declaration_sha256,
        }
        for node, declaration in dependent_slices
    ]
    prepared = {
        "version": 2,
        "state": "pending",
        "prepared_at": _now_iso(),
        "file": file_identity,
        "helper": helper_name,
        "parent": parent_name,
        "helper_node_id": str(promotion.get("node_id", "") or ""),
        "promotion_id": _promotion_id(promotion),
        "promotion": dict(promotion),
        "provenance_id": str(provenance.get("transaction_id", "") or ""),
        "source_hash_kind": "sha256-raw-utf8-bytes",
        "source_before_sha256": _sha256_bytes(current_source.encode("utf-8")),
        "source_after_sha256": _sha256_bytes(after_source.encode("utf-8")),
        "source_after": after_source,
        "helper_declaration_sha256": helper.declaration_sha256,
        "helper_signature_sha256": helper_full_signature,
        "parent_current_declaration_sha256": parent.declaration_sha256,
        "parent_signature_sha256": parent.signature_sha256,
        "parent_restored_declaration_sha256": restored.declaration_sha256,
        "parent_restored_declaration": restored_parent,
        "parent_restored_statement": _graph_statement(restored_parent, parent_name),
        "invalidated_dependents": dependent_records,
    }
    return prepared, ""


def _begin_transaction(transaction: Mapping[str, Any]) -> dict[str, Any]:
    """Persist one unique compare-and-swap plan before source mutation."""
    stored = dict(transaction)
    transaction_id = str(stored.get("transaction_id", "") or "")
    promotion_id = str(stored.get("promotion_id", "") or "")
    stored_audit = _audit_cleanup_transactions([stored])
    if (
        not stored_audit.records
        or stored_audit.records[0].disposition != "live"
        or stored_audit.records[0].state != "pending"
    ):
        reason = stored_audit.records[0].reason if stored_audit.records else "missing record"
        raise RuntimeError(f"new false-cleanup transaction is unauthenticated: {reason}")

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        records, index = _cleanup_transaction_records_for_mutation(
            summary,
            transaction_id=transaction_id,
        )
        promotion_indexes = [
            item_index
            for item_index, item in enumerate(records)
            if isinstance(item, Mapping)
            and str(item.get("promotion_id", "") or "").strip() == promotion_id
        ]
        if len(promotion_indexes) > 1:
            raise RuntimeError("false-cleanup promotion transaction target is duplicated")

        candidate_index: int | None = None
        if index is not None:
            raw_existing = records[index]
            assert isinstance(raw_existing, Mapping)
            existing = dict(raw_existing)
            state = str(existing.get("state", "") or "")
            if state == "manual-retry-authorized":
                existing = _authorized_retry_to_pending(existing)
                records[index] = existing
                candidate_index = index
            elif state == "pending":
                candidate_index = index
        elif promotion_indexes:
            retry_index = promotion_indexes[0]
            raw_retry = records[retry_index]
            migration_from = str(stored.get("migration_from_transaction_id", "") or "").strip()
            retry_audit = _audit_cleanup_transactions(records)
            migrates_committed_v1 = bool(
                stored.get("version") == 3
                and isinstance(raw_retry, Mapping)
                and raw_retry.get("version") == 1
                and str(raw_retry.get("state", "") or "") == "committed"
                and str(raw_retry.get("transaction_id", "") or "") == migration_from
                and retry_audit.records[retry_index].disposition == "terminal"
                and dict(raw_retry.get("promotion") or {}) == dict(stored.get("promotion") or {})
            )
            if migrates_committed_v1:
                # Replace the exact authenticated legacy commit before source
                # mutation. The v3 transaction carries its predecessor's id,
                # so a crash leaves one replayable authority, never two.
                existing = stored
                records[retry_index] = stored
                candidate_index = retry_index
            else:
                # An explicitly authorized retry owns its exact old replay plan.
                # Never append a rebuilt transaction with the same promotion id.
                if (
                    not isinstance(raw_retry, Mapping)
                    or str(raw_retry.get("state", "") or "") != "manual-retry-authorized"
                ):
                    raise RuntimeError("false-cleanup promotion already has another transaction")
                existing = _authorized_retry_to_pending(raw_retry)
                records[retry_index] = existing
                candidate_index = retry_index
        else:
            records.append(stored)
            existing = stored
            candidate_index = len(records) - 1

        if candidate_index is not None:
            _prospective_cleanup_transaction(
                records,
                candidate_index=candidate_index,
                transaction_id=str(existing.get("transaction_id", "") or ""),
                promotion_id=str(existing.get("promotion_id", "") or ""),
            )
        summary["false_decomposition_cleanup_transactions"] = _retained_cleanup_transactions(
            records
        )
        return existing

    return dict(update_json_file(plan_state.plan_state_paths().summary_json, mutate))


def _apply_graph_cleanup(
    transaction: Mapping[str, Any],
    *,
    file_identity: str,
    current_source: str,
    project_root: Path,
) -> tuple[bool, str]:
    """Retire exact owned graph state while preserving later safe graph edits."""
    _quarantine_index, quarantine_reason = _transaction_evidence_quarantine_index(
        transaction, plan_state.load_summary()
    )
    if quarantine_reason:
        return False, quarantine_reason
    blueprint = plan_state.load_blueprint()
    helper_id = str(transaction.get("helper_node_id", "") or "").strip()
    helper_name = str(transaction.get("helper", "") or "").strip()
    parent_name = str(transaction.get("parent", "") or "").strip()
    graph_file = str(transaction.get("graph_file", "") or "").strip()
    if str(transaction.get("file", "") or "") != file_identity:
        return False, "cleanup transaction file differs from its pinned source identity"
    if not _graph_file_reaches_source(
        graph_file,
        project_root=project_root,
        source_identity=file_identity,
    ):
        return False, "cleanup graph file no longer resolves to its pinned source identity"
    expected_helper_id = plan_state.node_id_for(helper_name, graph_file)
    if not helper_id or helper_id != expected_helper_id:
        return False, "cleanup helper graph node id differs from stable identity"
    helper_id_matches = [node for node in blueprint.nodes if node.id == helper_id]
    helper_identity_matches = [
        node for node in blueprint.nodes if node.name == helper_name and node.file == graph_file
    ]
    helper_source_aliases = [
        node
        for node in blueprint.nodes
        if node.name == helper_name
        and _graph_file_reaches_source(
            node.file,
            project_root=project_root,
            source_identity=file_identity,
        )
    ]
    if len(helper_source_aliases) > 1:
        return False, "dependency graph helper has ambiguous file aliases for one source"
    if len(helper_id_matches) > 1 or len(helper_identity_matches) > 1:
        return False, "dependency graph helper identity became duplicated"
    if (
        helper_id_matches
        and helper_identity_matches
        and helper_id_matches[0] != helper_identity_matches[0]
    ):
        return False, "cleanup helper graph node id was reassigned to another declaration"
    if bool(helper_id_matches) != bool(helper_identity_matches):
        return False, "cleanup helper graph identity is internally inconsistent"
    helper = helper_id_matches[0] if helper_id_matches else None
    expected_dependents = _expected_dependent_records(transaction)
    dependent_state, dependent_state_reason = _dependent_graph_replay_state(
        blueprint, expected_dependents
    )
    if dependent_state_reason:
        return False, dependent_state_reason
    (
        dependent_source_revision,
        transitive_dependents,
        detach_incoming_dependents,
        allow_graph_only_dependents,
        require_parent_split,
        dependent_policy_reason,
    ) = _dependent_shape_policy(transaction, expected_dependents)
    if dependent_policy_reason:
        return False, dependent_policy_reason
    parent, parent_reason = _unique_parent_node(
        blueprint,
        parent_name=parent_name,
        graph_file=graph_file,
        expected_id=str(transaction.get("parent_node_id", "") or ""),
        project_root=project_root,
        source_identity=file_identity,
    )
    if parent is None:
        return False, parent_reason
    shape = _GraphCleanupShape()
    if helper is not None:
        if helper.status != "false":
            return False, "helper graph node lost authoritative false status before graph cleanup"
        ownership_basis = _transaction_base_ownership_basis(transaction)
        if ownership_basis == "decomposer-graph" and helper.generated_by != "decomposer":
            return False, "helper graph ownership changed before graph cleanup"
        graph_shape, dependency_reason = _graph_cleanup_shape(
            blueprint,
            helper=helper,
            parent=parent,
            promotion=_transaction_promotion(transaction),
            source_text=current_source,
            expected_dependents=(expected_dependents if dependent_state == "live" else None),
            dependent_source_revision=dependent_source_revision,
            transitive_dependents=transitive_dependents,
            detach_incoming_dependents=detach_incoming_dependents,
            allow_graph_only_dependents=allow_graph_only_dependents,
            require_parent_split=require_parent_split,
            evidence_source_revision=(
                dependent_source_revision if transaction.get("version") == 3 else ""
            ),
        )
        if graph_shape is None:
            return False, dependency_reason
        if (
            _transaction_requires_evidence_tombstone(transaction)
            and not graph_shape.preserved_evidence
        ):
            return False, "legacy evidence cleanup no longer has its promotion-bound tombstone"
        if transaction.get("version") == 1 and graph_shape.invalidated_dependents:
            return False, "legacy cleanup transaction lacks sealed false-dependent invalidations"
        if dependent_state == "retired" and graph_shape.invalidated_dependents:
            return False, "retired false-dependent graph nodes reappeared during cleanup"
        shape = graph_shape
    elif dependent_state == "live":
        return False, "false helper retired before its dependent graph obligations"
    elif any(helper_id in {edge.source, edge.target} for edge in blueprint.edges):
        return False, "retired helper graph id still has edges that cleanup must preserve"
    elif _transaction_requires_evidence_tombstone(transaction):
        return False, "legacy evidence cleanup lost its required false-node tombstone"

    preserve_tombstone = bool(shape.preserved_evidence)
    invalidated_ids = set(expected_dependents)
    nodes = (
        blueprint.nodes
        if preserve_tombstone
        else tuple(node for node in blueprint.nodes if node.id != helper_id)
    )
    nodes = tuple(node for node in nodes if node.id not in invalidated_ids)
    # Remove only exact decomposer structural ownership. The source-negation
    # proof edge and its false target remain as an audit tombstone; conjectured
    # dependents sealed into the transaction leave the active graph entirely.
    edges = tuple(edge for edge in blueprint.edges if edge not in shape.removable_structural)
    restored_declaration = str(transaction.get("parent_restored_declaration", "") or "")
    restored_statement = str(transaction.get("parent_restored_statement", "") or "")
    if not restored_statement:
        restored_statement = _graph_statement(restored_declaration, parent_name)
    current_parent = decomposition_provenance.declaration_slice(current_source, parent_name)
    if current_parent is None:
        return False, "cleanup parent declaration disappeared after source persistence"
    if current_parent.declaration_sha256 == str(
        transaction.get("parent_restored_declaration_sha256", "") or ""
    ):
        replacement = replace(
            parent,
            statement=restored_statement,
            source_sha256=_sha256_text(current_source),
            status="stated",
            owner="",
        )
        nodes = tuple(replacement if node.id == parent.id else node for node in nodes)
    updated = replace(blueprint, nodes=nodes, edges=edges)
    if updated != blueprint:
        try:
            plan_state.save_blueprint(updated)
        except plan_state.PlanStateRevisionConflict:
            return False, "dependency graph changed while cleanup was being reconciled"
        preserved = [edge.to_mapping() for edge in shape.preserved_evidence]
        removed = [edge.to_mapping() for edge in shape.removable_structural]
        if preserve_tombstone:
            _record_event(
                "false-helper-negation-evidence-preserved",
                f"Preserved false-helper audit evidence for {helper_name}",
                helper=helper_name,
                helper_node_id=helper_id,
                file=graph_file,
                proof_declaration=str(
                    transaction.get("promotion", {}).get("proof_declaration", "")
                    if isinstance(transaction.get("promotion"), Mapping)
                    else ""
                ),
                preserved_evidence_edges=preserved,
                invalidated_dependent_nodes=sorted(invalidated_ids),
                removed_structural_edges=removed,
            )
        if invalidated_ids:
            _record_event(
                "false-dependent-obligations-invalidated",
                f"Retired {len(invalidated_ids)} obligation(s) depending on {helper_name}",
                helper=helper_name,
                helper_node_id=helper_id,
                file=graph_file,
                invalidated_dependent_nodes=sorted(invalidated_ids),
                removed_structural_edges=removed,
            )
    return True, ""


def _graph_reflects_cleanup(
    transaction: Mapping[str, Any],
    *,
    file_identity: str,
    current_source: str,
    project_root: Path,
) -> tuple[bool, str]:
    """Verify helper retirement or its exact evidence tombstone before commit."""
    _quarantine_index, quarantine_reason = _transaction_evidence_quarantine_index(
        transaction, plan_state.load_summary()
    )
    if quarantine_reason:
        return False, quarantine_reason
    blueprint = plan_state.load_blueprint()
    expected_dependents = _expected_dependent_records(transaction)
    dependent_state, dependent_state_reason = _dependent_graph_replay_state(
        blueprint, expected_dependents
    )
    if dependent_state_reason:
        return False, dependent_state_reason
    if expected_dependents and dependent_state != "retired":
        return False, "false-dependent graph obligations remain before cleanup commit"
    evidence_source_revision = ""
    if transaction.get("version") == 3:
        (
            evidence_source_revision,
            _transitive_dependents,
            _detach_incoming_dependents,
            _allow_graph_only_dependents,
            _require_parent_split,
            dependent_policy_reason,
        ) = _dependent_shape_policy(transaction, expected_dependents)
        if dependent_policy_reason:
            return False, dependent_policy_reason
    helper_id = str(transaction.get("helper_node_id", "") or "").strip()
    helper_name = str(transaction.get("helper", "") or "").strip()
    parent_name = str(transaction.get("parent", "") or "").strip()
    graph_file = str(transaction.get("graph_file", "") or "").strip()
    if str(transaction.get("file", "") or "") != file_identity:
        return False, "cleanup transaction file differs from its pinned source identity"
    if not _graph_file_reaches_source(
        graph_file,
        project_root=project_root,
        source_identity=file_identity,
    ):
        return False, "cleanup graph file no longer resolves to its pinned source identity"
    if not helper_id or helper_id != plan_state.node_id_for(helper_name, graph_file):
        return False, "cleanup helper graph node id differs from stable identity"
    parent, parent_reason = _unique_parent_node(
        blueprint,
        parent_name=parent_name,
        graph_file=graph_file,
        expected_id=str(transaction.get("parent_node_id", "") or ""),
        project_root=project_root,
        source_identity=file_identity,
    )
    if parent is None:
        return False, parent_reason
    helper_matches = [node for node in blueprint.nodes if node.id == helper_id]
    helper_aliases = [
        node
        for node in blueprint.nodes
        if node.name == helper_name
        and _graph_file_reaches_source(
            node.file,
            project_root=project_root,
            source_identity=file_identity,
        )
    ]
    if helper_matches or helper_aliases:
        if len(helper_matches) != 1 or len(helper_aliases) != 1:
            return False, "negation audit tombstone identity is missing or duplicated"
        helper = helper_matches[0]
        if helper != helper_aliases[0] or helper.status != "false":
            return False, "negation audit tombstone lost authoritative false identity"
        if (
            _transaction_base_ownership_basis(transaction) == "decomposer-graph"
            and helper.generated_by != "decomposer"
        ):
            return False, "negation audit tombstone lost authoritative graph ownership"
        graph_shape, graph_reason = _graph_cleanup_shape(
            blueprint,
            helper=helper,
            parent=parent,
            promotion=_transaction_promotion(transaction),
            source_text=current_source,
            evidence_source_revision=evidence_source_revision,
        )
        if graph_shape is None:
            return False, graph_reason
        if not graph_shape.preserved_evidence:
            return False, "false helper graph node reappeared without audit tombstone edges"
        if graph_shape.invalidated_dependents:
            return False, "false-dependent graph obligation reappeared before cleanup commit"
        if graph_shape.removable_structural:
            return False, "false helper structural edges reappeared before cleanup commit"
    elif any(helper_id in {edge.source, edge.target} for edge in blueprint.edges):
        return False, "retired helper graph id regained edges before cleanup commit"
    elif _transaction_requires_evidence_tombstone(transaction):
        return False, "legacy evidence cleanup lost its required false-node tombstone"
    current_parent = decomposition_provenance.declaration_slice(current_source, parent_name)
    if current_parent is None:
        return False, "cleanup parent declaration disappeared before cleanup commit"
    if current_parent.declaration_sha256 == str(
        transaction.get("parent_restored_declaration_sha256", "") or ""
    ):
        restored_statement = str(transaction.get("parent_restored_statement", "") or "")
        if not restored_statement:
            restored_statement = _graph_statement(
                str(transaction.get("parent_restored_declaration", "") or ""),
                parent_name,
            )
        expected_parent = replace(
            parent,
            statement=restored_statement,
            source_sha256=_sha256_text(current_source),
            status="stated",
            owner="",
        )
        if parent != expected_parent:
            return False, "cleanup parent graph state changed before cleanup commit"
    return True, ""


def _finalize_committed_dependent_migration(
    transaction: Mapping[str, Any],
) -> None:
    """Commit a dependent-only upgrade of one authenticated legacy cleanup."""
    transaction_id = str(transaction.get("transaction_id", "") or "")
    committed_at = _now_iso()
    committed = {
        **dict(transaction),
        "state": "committed",
        "committed_at": committed_at,
    }
    committed.pop("source_after", None)

    def mutate(summary: dict[str, Any]) -> None:
        transactions, index = _cleanup_transaction_records_for_mutation(
            summary,
            transaction_id=transaction_id,
        )
        if index is None:
            raise RuntimeError("legacy dependent migration disappeared before commit")
        raw = transactions[index]
        if (
            not isinstance(raw, Mapping)
            or raw.get("version") != 3
            or str(raw.get("state", "") or "") != "pending"
            or str(raw.get("migration_from_transaction_id", "") or "")
            != str(transaction.get("migration_from_transaction_id", "") or "")
        ):
            raise RuntimeError("legacy dependent migration cannot commit from its durable state")
        transactions.pop(index)
        transactions.append(committed)
        summary["false_decomposition_cleanup_transactions"] = _retained_cleanup_transactions(
            transactions
        )

        raw_cleanups = summary.get("false_decomposition_cleanups")
        if raw_cleanups is None:
            cleanups: list[object] = []
        elif isinstance(raw_cleanups, list):
            cleanups = list(raw_cleanups)
        else:
            raise RuntimeError("false-cleanup archive registry is not a list")
        cleanups.append(committed)
        summary["false_decomposition_cleanups"] = cleanups[-_CLEANUP_CAP:]

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    _record_event(
        "false-dependent-cleanup-migrated",
        f"Retired legacy obligations depending on {transaction.get('helper', '[unknown]')}",
        transaction_id=transaction_id,
        migration_from_transaction_id=str(
            transaction.get("migration_from_transaction_id", "") or ""
        ),
        helper=str(transaction.get("helper", "") or ""),
        parent=str(transaction.get("parent", "") or ""),
        file=str(transaction.get("file", "") or ""),
        invalidated_dependents=[
            record.get("name", "") for record in _dependent_invalidation_records(transaction)
        ],
    )


def _finalize_transaction(transaction: Mapping[str, Any]) -> None:
    """Archive valid negation evidence and commit the cleanup summary atomically."""
    if transaction.get("version") == 3:
        _finalize_committed_dependent_migration(transaction)
        return
    transaction_id = str(transaction.get("transaction_id", "") or "")
    promotion_id = str(transaction.get("promotion_id", "") or "")
    committed_at = _now_iso()
    committed = {
        **dict(transaction),
        "state": "committed",
        "committed_at": committed_at,
    }
    resolved_legacy_evidence_quarantine = False
    # The complete post-edit source made crash replay possible, but retaining it
    # forever in summary.json would duplicate a potentially large source file.
    committed.pop("source_after", None)

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal resolved_legacy_evidence_quarantine
        raw_promotion_transactions = summary.get("negation_promotion_transactions")
        promotion_transaction_audit = (
            negation_transaction_registry.audit_negation_transaction_registry(
                raw_promotion_transactions,
                terminal_history_cap=_TRANSACTION_CAP,
            )
        )
        if not isinstance(raw_promotion_transactions, list):
            raise RuntimeError(
                "false cleanup cannot consume an ambiguous negation transaction registry"
            )
        matching_indexes: list[int] = []
        for index, raw in enumerate(raw_promotion_transactions):
            if not isinstance(raw, Mapping):
                continue
            raw_promotion = raw.get("promotion")
            raw_promotion_id = (
                str(raw_promotion.get("promotion_id", "") or "").strip()
                if isinstance(raw_promotion, Mapping)
                else ""
            )
            if promotion_id in {
                str(raw.get("transaction_id", "") or "").strip(),
                raw_promotion_id,
            }:
                matching_indexes.append(index)
        if len(matching_indexes) != 1:
            raise RuntimeError(
                "false cleanup requires one unique authenticated negation transaction"
            )
        match_index = matching_indexes[0]
        match_audit = promotion_transaction_audit.records[match_index]
        matched_raw = raw_promotion_transactions[match_index]
        if (
            match_audit.disposition != "terminal"
            or not isinstance(matched_raw, Mapping)
            or str(matched_raw.get("state", "") or "") != "committed"
        ):
            raise RuntimeError("false cleanup cannot consume unauthenticated negation authority")
        matched_promotion = matched_raw.get("promotion")
        cleanup_promotion = transaction.get("promotion")
        if (
            not isinstance(matched_promotion, Mapping)
            or not isinstance(cleanup_promotion, Mapping)
            or dict(matched_promotion) != dict(cleanup_promotion)
        ):
            raise RuntimeError("false cleanup promotion evidence changed before commit")
        evidence_quarantine_index, evidence_quarantine_reason = (
            _transaction_evidence_quarantine_index(transaction, summary)
        )
        if evidence_quarantine_reason:
            raise RuntimeError(evidence_quarantine_reason)
        transactions, cleanup_index = _cleanup_transaction_records_for_mutation(
            summary,
            transaction_id=transaction_id,
        )
        if cleanup_index is None:
            raise RuntimeError("durable false-cleanup transaction disappeared before commit")
        raw_cleanup = transactions[cleanup_index]
        if (
            not isinstance(raw_cleanup, Mapping)
            or str(raw_cleanup.get("state", "") or "") != "pending"
        ):
            raise RuntimeError("false-cleanup transaction cannot commit from its durable state")
        # Move the just-committed record to the tail before terminal-history
        # retention.  A long-lived pending record may sit before fifty newer
        # commits and must not disappear in the same update that commits it.
        transactions.pop(cleanup_index)
        transactions.append(committed)
        summary["false_decomposition_cleanup_transactions"] = _retained_cleanup_transactions(
            transactions
        )
        # Every remaining promotion is live mathematical authority until its
        # own cleanup or quarantine commits. Never apply a history cap here.
        promotion_records, promotion_index = _active_promotion_records_for_mutation(
            summary,
            promotion_id=promotion_id,
        )
        if promotion_index is None:
            raise RuntimeError("active negation promotion disappeared before cleanup commit")
        raw_active_promotion = promotion_records[promotion_index]
        if not isinstance(raw_active_promotion, Mapping) or dict(raw_active_promotion) != dict(
            cleanup_promotion
        ):
            raise RuntimeError("active negation promotion changed before cleanup commit")
        promotion_records.pop(promotion_index)
        summary["negation_promotions"] = promotion_records
        promotion_transactions: list[object] = list(raw_promotion_transactions)
        consumed_promotion_transaction = {
            **dict(matched_raw),
            "state": "consumed-by-false-decomposition-cleanup",
            "cleanup_transaction_id": transaction_id,
        }
        promotion_transactions.pop(match_index)
        promotion_transactions.append(consumed_promotion_transaction)
        summary["negation_promotion_transactions"] = _retained_negation_transactions(
            promotion_transactions
        )
        raw_cleanups = summary.get("false_decomposition_cleanups")
        if raw_cleanups is None:
            cleanups: list[object] = []
        elif isinstance(raw_cleanups, list):
            cleanups = [
                item
                for item in raw_cleanups
                if not (
                    isinstance(item, Mapping)
                    and str(item.get("transaction_id", "") or "") == transaction_id
                )
            ]
        else:
            raise RuntimeError("false-cleanup archive registry is not a list")
        cleanups.append(committed)
        summary["false_decomposition_cleanups"] = cleanups[-_CLEANUP_CAP:]

        raw_quarantines = summary.get("false_decomposition_cleanup_quarantine")
        if evidence_quarantine_index is not None:
            if not isinstance(raw_quarantines, list):
                raise RuntimeError("cleanup transaction evidence quarantine registry disappeared")
            quarantines: list[object] = list(raw_quarantines)
            raw_quarantine = quarantines[evidence_quarantine_index]
            if not isinstance(raw_quarantine, Mapping):
                raise RuntimeError("cleanup transaction evidence quarantine payload disappeared")
            quarantines[evidence_quarantine_index] = {
                **dict(raw_quarantine),
                "state": "resolved",
                "resolved_at": committed_at,
                "resolution_reason": (
                    "exact promotion-bound negation evidence was preserved as a false-node "
                    "audit tombstone"
                ),
            }
            resolved_legacy_evidence_quarantine = True
            summary["false_decomposition_cleanup_quarantine"] = _retained_cleanup_quarantines(
                quarantines
            )

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    _record_event(
        "false-decomposition-cleaned",
        f"Retracted false campaign helper {transaction.get('helper', '[unknown]')} and reopened its parent",
        transaction_id=transaction_id,
        promotion_id=promotion_id,
        provenance_id=str(transaction.get("provenance_id", "") or ""),
        helper=str(transaction.get("helper", "") or ""),
        parent=str(transaction.get("parent", "") or ""),
        file=str(transaction.get("file", "") or ""),
        preserved_negation_evidence=True,
    )
    if resolved_legacy_evidence_quarantine:
        _record_event(
            "false-decomposition-cleanup-quarantine-auto-resolved",
            f"Resolved obsolete evidence quarantine for {transaction.get('helper', '[unknown]')}",
            transaction_id=transaction_id,
            promotion_id=promotion_id,
            helper=str(transaction.get("helper", "") or ""),
            resolution="authenticated_negation_evidence_tombstone",
        )


def _mark_transaction_quarantined(transaction: Mapping[str, Any], reason: str) -> None:
    """Atomically stop replay and persist its exact quarantine decision."""
    transaction_id = str(transaction.get("transaction_id", "") or "")
    quarantined_at = _now_iso()
    promotion = dict(transaction.get("promotion") or {})
    provenance_id = str(transaction.get("provenance_id", "") or "")
    quarantine_entry = _quarantine_entry(
        promotion,
        reason=reason,
        provenance_id=provenance_id,
        quarantined_at=quarantined_at,
    )

    def mutate(summary: dict[str, Any]) -> None:
        transactions, index = _cleanup_transaction_records_for_mutation(
            summary,
            transaction_id=transaction_id,
        )
        if index is None:
            raise RuntimeError("false-cleanup transaction disappeared before quarantine")
        raw = transactions[index]
        assert isinstance(raw, Mapping)
        transactions[index] = {
            **dict(raw),
            "state": "quarantined",
            "quarantined_at": quarantined_at,
            "reason": reason,
        }
        summary["false_decomposition_cleanup_transactions"] = _retained_cleanup_transactions(
            transactions
        )
        quarantines, quarantine_index = _cleanup_quarantine_records_for_mutation(
            summary,
            quarantine_id=str(quarantine_entry["quarantine_id"]),
        )
        if quarantine_index is None:
            quarantines.append(quarantine_entry)
        else:
            quarantines[quarantine_index] = quarantine_entry
        summary["false_decomposition_cleanup_quarantine"] = _retained_cleanup_quarantines(
            quarantines
        )

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    _record_event(
        "false-decomposition-cleanup-quarantined",
        f"Quarantined false-helper cleanup for {promotion.get('theorem', '[unknown]')}",
        theorem=str(promotion.get("theorem", "") or ""),
        file=str(promotion.get("file", "") or ""),
        promotion_id=_promotion_id(promotion),
        provenance_id=provenance_id,
        reason=reason,
    )


def _retain_pending_transaction(transaction: Mapping[str, Any], reason: str) -> None:
    """Keep a post-source ambiguity resumable without overwriting later edits."""
    transaction_id = str(transaction.get("transaction_id", "") or "")

    def mutate(summary: dict[str, Any]) -> None:
        transactions, index = _cleanup_transaction_records_for_mutation(
            summary,
            transaction_id=transaction_id,
        )
        if index is None:
            raise RuntimeError("false-cleanup transaction disappeared during reconciliation")
        raw = transactions[index]
        assert isinstance(raw, Mapping)
        transactions[index] = {
            **dict(raw),
            "state": "pending",
            "last_reconciliation_at": _now_iso(),
            "last_reconciliation_reason": reason,
        }
        summary["false_decomposition_cleanup_transactions"] = _retained_cleanup_transactions(
            transactions
        )

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    _record_event(
        "false-decomposition-cleanup-reconciliation-pending",
        f"Preserved cleanup transaction for {transaction.get('helper', '[unknown]')}",
        transaction_id=transaction_id,
        helper=str(transaction.get("helper", "") or ""),
        parent=str(transaction.get("parent", "") or ""),
        file=str(transaction.get("file", "") or ""),
        reason=reason,
    )


def _source_still_reflects_cleanup(
    current_source: str,
    transaction: Mapping[str, Any],
) -> tuple[bool, str]:
    """Recognize cleanup followed by safe user edits without reverting them."""
    helper_name = str(transaction.get("helper", "") or "").strip()
    parent_name = str(transaction.get("parent", "") or "").strip()
    invalidated_names = tuple(
        str(record.get("name", "") or "") for record in _dependent_invalidation_records(transaction)
    )
    if decomposition_provenance.declaration_slice(current_source, helper_name) is not None:
        return False, "false helper is present in the later source revision"
    parent = decomposition_provenance.declaration_slice(current_source, parent_name)
    if parent is None:
        return False, "cleanup parent is absent from the later source revision"
    if parent.signature_sha256 != str(transaction.get("parent_signature_sha256", "") or ""):
        return False, "cleanup parent statement changed in the later source revision"
    if _proof_references_helper(parent.text, helper_name):
        return False, "cleanup parent again references the removed false helper"
    if _source_has_external_reference(
        current_source,
        helper_name=helper_name,
        parent_name=parent_name,
        retired_names=invalidated_names,
    ):
        return False, "later same-file source references the removed false helper"
    for invalidated_name in invalidated_names:
        if decomposition_provenance.declaration_slice(current_source, invalidated_name) is not None:
            return False, "false-dependent obligation is present in the later source revision"
        if _source_has_external_reference(
            current_source,
            helper_name=invalidated_name,
            parent_name=parent_name,
            retired_names=(helper_name, *invalidated_names),
        ):
            return False, "later same-file source references a false-dependent obligation"
    return True, ""


def _transaction_graph_is_safe(
    transaction: Mapping[str, Any],
    *,
    file_identity: str,
    current_source: str,
    project_root: Path,
) -> tuple[bool, str]:
    """Revalidate exact graph ownership immediately before source deletion."""
    _quarantine_index, quarantine_reason = _transaction_evidence_quarantine_index(
        transaction, plan_state.load_summary()
    )
    if quarantine_reason:
        return False, quarantine_reason
    blueprint = plan_state.load_blueprint()
    expected_dependents = _expected_dependent_records(transaction)
    dependent_state, dependent_state_reason = _dependent_graph_replay_state(
        blueprint, expected_dependents
    )
    if dependent_state_reason:
        return False, dependent_state_reason
    if expected_dependents and dependent_state != "live":
        return False, "false-dependent graph obligations changed before source cleanup"
    (
        dependent_source_revision,
        transitive_dependents,
        detach_incoming_dependents,
        allow_graph_only_dependents,
        require_parent_split,
        dependent_policy_reason,
    ) = _dependent_shape_policy(transaction, expected_dependents)
    if dependent_policy_reason:
        return False, dependent_policy_reason
    helper_id = str(transaction.get("helper_node_id", "") or "").strip()
    helper_name = str(transaction.get("helper", "") or "").strip()
    parent_name = str(transaction.get("parent", "") or "").strip()
    graph_file = str(transaction.get("graph_file", "") or "").strip()
    if str(transaction.get("file", "") or "") != file_identity:
        return False, "cleanup transaction file differs from its pinned source identity"
    if not _graph_file_reaches_source(
        graph_file,
        project_root=project_root,
        source_identity=file_identity,
    ):
        return False, "cleanup graph file no longer resolves to its pinned source identity"
    if helper_id != plan_state.node_id_for(helper_name, graph_file):
        return False, "cleanup helper graph id is not the stable declaration identity"
    helper_matches = [
        node
        for node in blueprint.nodes
        if node.id == helper_id and node.name == helper_name and node.file == graph_file
    ]
    if len(helper_matches) != 1:
        return False, "dependency graph helper identity changed before source cleanup"
    # Transactions created before the explicit field existed were admitted
    # only through decomposer-owned graph state.
    ownership_basis = _transaction_base_ownership_basis(transaction)
    helper = helper_matches[0]
    if helper.status != "false":
        return False, "helper graph node lost authoritative false status before source cleanup"
    if ownership_basis == "decomposer-graph" and helper.generated_by != "decomposer":
        return False, "helper graph ownership changed before source cleanup"
    if len([node for node in blueprint.nodes if node.id == helper_id]) != 1:
        return False, "dependency graph helper id became duplicated before source cleanup"
    if (
        len(
            [
                node
                for node in blueprint.nodes
                if node.name == helper_name
                and _graph_file_reaches_source(
                    node.file,
                    project_root=project_root,
                    source_identity=file_identity,
                )
            ]
        )
        != 1
    ):
        return False, "dependency graph helper identity became duplicated before source cleanup"
    parent, parent_reason = _unique_parent_node(
        blueprint,
        parent_name=parent_name,
        graph_file=graph_file,
        expected_id=str(transaction.get("parent_node_id", "") or ""),
        project_root=project_root,
        source_identity=file_identity,
    )
    if parent is None:
        return False, parent_reason
    graph_shape, graph_reason = _graph_cleanup_shape(
        blueprint,
        helper=helper,
        parent=parent,
        promotion=_transaction_promotion(transaction),
        source_text=current_source,
        expected_dependents=expected_dependents or None,
        dependent_source_revision=dependent_source_revision,
        transitive_dependents=transitive_dependents,
        detach_incoming_dependents=detach_incoming_dependents,
        allow_graph_only_dependents=allow_graph_only_dependents,
        require_parent_split=require_parent_split,
        evidence_source_revision=(
            dependent_source_revision if transaction.get("version") == 3 else ""
        ),
    )
    if graph_shape is None:
        return False, graph_reason
    if transaction.get("version") == 1 and graph_shape.invalidated_dependents:
        return False, "legacy cleanup transaction lacks sealed false-dependent invalidations"
    if _transaction_requires_evidence_tombstone(transaction) and not graph_shape.preserved_evidence:
        return False, "legacy evidence cleanup no longer has its promotion-bound tombstone"
    return True, ""


def _execute_transaction(transaction: Mapping[str, Any], *, project_root: Path) -> tuple[str, str]:
    """Replay one transaction, preserving post-source ambiguity for later repair."""
    integrity_reason = _transaction_integrity_reason(transaction)
    if integrity_reason:
        _mark_transaction_quarantined(transaction, integrity_reason)
        return "quarantined", integrity_reason
    path = Path(str(transaction.get("file", "") or ""))
    try:
        with decomposition_provenance.source_operation(path, canonical=True) as operation:
            return _execute_transaction_under_lease(
                transaction,
                project_root=project_root,
                operation=operation,
            )
    except (OSError, UnicodeDecodeError) as exc:
        reason = f"cleanup source unavailable during replay: {str(exc)[:160]}"
        _mark_transaction_quarantined(transaction, reason)
        return "quarantined", reason


def _transaction_parent_statement_is_safe(
    current_source: str,
    transaction: Mapping[str, Any],
) -> bool:
    """Return whether replay would restore the same complete parent statement.

    Version-1 transactions retain the historical prefix signature for durable
    compatibility. Reconstruct both full statements from their exact source
    payloads immediately before CAS so an older pending plan cannot overwrite
    a dependent-let parent whose suffix changed after that prefix.
    """
    parent_name = str(transaction.get("parent", "") or "").strip()
    restored_text = str(transaction.get("parent_restored_declaration", "") or "")
    current_parent = decomposition_provenance.declaration_slice(current_source, parent_name)
    restored_parent = decomposition_provenance.declaration_slice(restored_text, parent_name)
    if current_parent is None or restored_parent is None:
        return False
    return decomposition_provenance.full_declaration_signature_sha256(
        current_parent.text
    ) == decomposition_provenance.full_declaration_signature_sha256(restored_parent.text)


def _execute_transaction_under_lease(
    transaction: Mapping[str, Any],
    *,
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
) -> tuple[str, str]:
    """Replay one cleanup while holding its pinned source lifecycle lease."""
    integrity_reason = _transaction_integrity_reason(transaction)
    if integrity_reason:
        _mark_transaction_quarantined(transaction, integrity_reason)
        return "quarantined", integrity_reason
    file_identity = str(operation.path)
    current_bytes = decomposition_provenance.read_source_bytes(operation)
    current_source = current_bytes.decode("utf-8")
    current_hash = _sha256_bytes(current_bytes)
    before_hash = str(transaction.get("source_before_sha256", "") or "")
    after_hash = str(transaction.get("source_after_sha256", "") or "")
    if current_hash == before_hash:
        after_source = str(transaction.get("source_after", "") or "")
        after_bytes = after_source.encode("utf-8")
        if not after_source or _sha256_bytes(after_bytes) != after_hash:
            reason = "cleanup transaction lost its exact post-edit source payload"
            _mark_transaction_quarantined(transaction, reason)
            return "quarantined", reason
        if not _transaction_parent_statement_is_safe(current_source, transaction):
            reason = "cleanup parent full statement differs from its sealed restoration payload"
            _mark_transaction_quarantined(transaction, reason)
            return "quarantined", reason
        graph_safe, graph_reason = _transaction_graph_is_safe(
            transaction,
            file_identity=file_identity,
            current_source=current_source,
            project_root=project_root,
        )
        if not graph_safe:
            _retain_pending_transaction(transaction, graph_reason)
            return "pending", graph_reason
        swapped = decomposition_provenance.compare_and_swap_source(
            operation.path,
            expected_bytes=current_bytes,
            replacement_bytes=after_bytes,
            operation=operation,
        )
        if not swapped:
            try:
                current_bytes = decomposition_provenance.read_source_bytes(operation)
                current_source = current_bytes.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                reason = f"cleanup source unavailable after compare-and-swap: {str(exc)[:160]}"
                _retain_pending_transaction(transaction, reason)
                return "pending", reason
        else:
            current_bytes = after_bytes
            current_source = after_source
    if _sha256_bytes(current_bytes) != after_hash:
        reconciled, reconcile_reason = _source_still_reflects_cleanup(current_source, transaction)
        if not reconciled:
            reason = f"source changed around false-helper cleanup: {reconcile_reason}"
            _retain_pending_transaction(transaction, reason)
            return "pending", reason
    _cleanup_transaction_hook("source-persisted")
    try:
        graph_source_bytes = decomposition_provenance.read_source_bytes(operation)
    except OSError as exc:
        reason = f"cleanup source identity changed before graph cleanup: {str(exc)[:160]}"
        _mark_transaction_quarantined(transaction, reason)
        return "quarantined", reason
    if graph_source_bytes != current_bytes:
        reason = "cleanup source changed before graph cleanup"
        _mark_transaction_quarantined(transaction, reason)
        return "quarantined", reason
    graph_ok, graph_reason = _apply_graph_cleanup(
        transaction,
        file_identity=file_identity,
        current_source=current_source,
        project_root=project_root,
    )
    if not graph_ok:
        _retain_pending_transaction(transaction, graph_reason)
        return "pending", graph_reason
    try:
        after_graph_bytes = decomposition_provenance.read_source_bytes(operation)
    except OSError as exc:
        reason = f"cleanup source identity changed after graph cleanup: {str(exc)[:160]}"
        _mark_transaction_quarantined(transaction, reason)
        return "quarantined", reason
    if after_graph_bytes != current_bytes:
        reason = "cleanup source changed during graph cleanup"
        _mark_transaction_quarantined(transaction, reason)
        return "quarantined", reason
    _cleanup_transaction_hook("graph-persisted")
    # The graph save above released its writer lock. Reacquire that cooperative
    # lease, re-read both authorities, and keep it through the terminal summary
    # update so another graph writer cannot enter after validation but before
    # the promotion/replay evidence is archived.
    with plan_state.blueprint_commit_guard():
        try:
            finalize_source_bytes = decomposition_provenance.read_source_bytes(operation)
        except OSError as exc:
            reason = f"cleanup source identity changed before finalize: {str(exc)[:160]}"
            _mark_transaction_quarantined(transaction, reason)
            return "quarantined", reason
        if finalize_source_bytes != current_bytes:
            reason = "cleanup source changed before finalize"
            _mark_transaction_quarantined(transaction, reason)
            return "quarantined", reason
        graph_final, graph_final_reason = _graph_reflects_cleanup(
            transaction,
            file_identity=file_identity,
            current_source=current_source,
            project_root=project_root,
        )
        if not graph_final:
            _retain_pending_transaction(transaction, graph_final_reason)
            return "pending", graph_final_reason
        _finalize_transaction(transaction)
    _cleanup_transaction_hook("committed")
    return "cleaned", ""


def _restore_authorized_retry_transactions() -> None:
    """Migrate authenticated legacy retry state back to exact pending plans."""

    def mutate(summary: dict[str, Any]) -> tuple[None, bool]:
        raw = summary.get("false_decomposition_cleanup_transactions")
        if raw is None:
            return None, False
        if not isinstance(raw, list):
            return None, False
        audit = _audit_cleanup_transactions(raw)
        records: list[object] = list(raw)
        changed = False
        for record in audit.records:
            if record.disposition != "live" or record.state != "manual-retry-authorized":
                continue
            selected = records[record.index]
            if not isinstance(selected, Mapping):
                continue
            pending = _authorized_retry_to_pending(selected)
            records[record.index] = pending
            _prospective_cleanup_transaction(
                records,
                candidate_index=record.index,
                transaction_id=str(pending.get("transaction_id", "") or ""),
                promotion_id=str(pending.get("promotion_id", "") or ""),
            )
            changed = True
        if changed:
            summary["false_decomposition_cleanup_transactions"] = _retained_cleanup_transactions(
                records
            )
        return None, changed

    update_json_file_if_changed(plan_state.plan_state_paths().summary_json, mutate)


def _legacy_cleanup_is_archived(summary: Mapping[str, Any], transaction: Mapping[str, Any]) -> bool:
    """Return whether the historical cleanup archive confirms one exact v1 commit."""
    transaction_id = str(transaction.get("transaction_id", "") or "")
    raw = summary.get("false_decomposition_cleanups")
    matches = (
        [
            item
            for item in raw
            if isinstance(item, Mapping)
            and str(item.get("transaction_id", "") or "") == transaction_id
        ]
        if isinstance(raw, list)
        else []
    )
    return len(matches) == 1 and dict(matches[0]) == dict(transaction)


def _legacy_migration_has_structural_work(
    blueprint: plan_state.Blueprint,
    *,
    helper_node_id: str,
) -> bool:
    """Return whether a committed cleanup tombstone still touches structural graph state."""
    return any(
        edge.kind != "evidence" and helper_node_id in {edge.source, edge.target}
        for edge in blueprint.edges
    )


def _resolve_no_work_legacy_migration_quarantine(
    transaction: Mapping[str, Any],
) -> bool:
    """Resolve the exact obsolete quarantine emitted for an evidence-only tombstone."""
    promotion = _transaction_promotion(transaction)
    provenance_id = str(transaction.get("provenance_id", "") or "")
    expected = _quarantine_entry(
        promotion,
        reason=_NO_WORK_LEGACY_MIGRATION_QUARANTINE_REASON,
        provenance_id=provenance_id,
    )
    quarantine_id = str(expected["quarantine_id"])

    def mutate(summary: dict[str, Any]) -> tuple[bool, bool]:
        records, index = _cleanup_quarantine_records_for_mutation(
            summary,
            quarantine_id=quarantine_id,
        )
        if index is None:
            return False, False
        selected = records[index]
        audit = _audit_cleanup_quarantines(records)
        nested = selected.get("promotion") if isinstance(selected, Mapping) else None
        if (
            not isinstance(selected, Mapping)
            or audit.records[index].disposition != "live"
            or audit.records[index].state != "quarantined"
            or selected.get("reason") != _NO_WORK_LEGACY_MIGRATION_QUARANTINE_REASON
            or str(selected.get("provenance_id", "") or "") != provenance_id
            or not isinstance(nested, Mapping)
            or dict(nested) != dict(promotion)
        ):
            return False, False
        records[index] = {
            **dict(selected),
            "state": "resolved",
            "resolved_at": _now_iso(),
            "resolution_reason": (
                "authenticated committed cleanup has no remaining structural dependent "
                "migration work"
            ),
        }
        summary["false_decomposition_cleanup_quarantine"] = _retained_cleanup_quarantines(records)
        return True, True

    try:
        resolved = bool(
            update_json_file_if_changed(plan_state.plan_state_paths().summary_json, mutate)
        )
    except RuntimeError:
        return False
    if resolved:
        _record_event(
            "false-dependent-cleanup-no-work-quarantine-resolved",
            "Resolved obsolete dependent-migration quarantine for an evidence-only tombstone",
            quarantine_id=quarantine_id,
            transaction_id=str(transaction.get("transaction_id", "") or ""),
            helper=str(transaction.get("helper", "") or ""),
        )
    return resolved


def _build_committed_dependent_migration(
    transaction: Mapping[str, Any],
    *,
    current_source: str,
    file_identity: str,
    graph_shape: _GraphCleanupShape,
) -> tuple[dict[str, Any] | None, str]:
    """Build a dependent-only CAS plan from one authenticated committed v1 cleanup."""
    if transaction.get("version") != 1 or str(transaction.get("state", "") or "") != "committed":
        return None, "legacy dependent migration requires one committed version-1 cleanup"
    helper_name = str(transaction.get("helper", "") or "").strip()
    parent_name = str(transaction.get("parent", "") or "").strip()
    if decomposition_provenance.declaration_slice(current_source, helper_name) is not None:
        return None, "legacy cleanup helper reappeared in current source"
    parent = decomposition_provenance.declaration_slice(current_source, parent_name)
    if parent is None or parent.signature_sha256 != str(
        transaction.get("parent_signature_sha256", "") or ""
    ):
        return None, "legacy cleanup parent statement changed before dependent migration"
    invalidated = graph_shape.invalidated_dependents
    if not invalidated:
        return None, "legacy cleanup has no false-dependent obligations to migrate"
    invalidated_ids = {node.id for node in invalidated}
    if any(
        not ({edge.source, edge.target} & invalidated_ids)
        for edge in graph_shape.removable_structural
    ):
        return None, "legacy cleanup structural ownership reappeared before migration"

    dependent_slices: list[tuple[plan_state.GraphNode, Any]] = []
    graph_artifacts: list[plan_state.GraphNode] = []
    for dependent in invalidated:
        declaration = decomposition_provenance.declaration_slice(current_source, dependent.name)
        if declaration is not None:
            if (
                dependent.generated_by != "decomposer"
                or dependent.source_sha256 != _sha256_text(current_source)
                or declaration.text.strip() != dependent.statement.strip()
                or not re.search(
                    r"\b(?:sorry|admit)\b",
                    _strip_lean_comments_and_strings(declaration.text),
                )
            ):
                return None, "legacy false-dependent source obligation changed before migration"
            dependent_slices.append((dependent, declaration))
            continue
        if (
            dependent.source_sha256
            or dependent.generated_by not in {"decomposer", "planner"}
            or not re.search(
                r"\b(?:sorry|admit)\b",
                _strip_lean_comments_and_strings(dependent.statement),
            )
        ):
            return None, "legacy false-dependent graph artifact changed before migration"
        graph_artifacts.append(dependent)
    retired_names = tuple(node.name for node in invalidated)
    for dependent in invalidated:
        if _source_has_external_reference(
            current_source,
            helper_name=dependent.name,
            parent_name=parent_name,
            retired_names=(helper_name, *retired_names),
        ):
            return None, "another source command references a legacy false-dependent obligation"

    spans = sorted(
        [
            (declaration.metadata_start, declaration.end, "")
            for _node, declaration in dependent_slices
        ],
        key=lambda item: item[0],
        reverse=True,
    )
    for previous, following in zip(spans, spans[1:], strict=False):
        if following[1] > previous[0]:
            return None, "legacy dependent cleanup source spans overlap"
    after_source = current_source
    for start, end, replacement_text in spans:
        if start < 0 or end < start or end > len(after_source):
            return None, "legacy dependent cleanup source span is invalid"
        after_source = after_source[:start] + replacement_text + after_source[end:]
    if any(
        decomposition_provenance.declaration_slice(after_source, node.name) is not None
        for node, _declaration in dependent_slices
    ):
        return None, "legacy false-dependent obligation remains after planned migration"

    dependent_records = [
        {
            "node_id": node.id,
            "name": node.name,
            "file": node.file,
            "source_sha256": node.source_sha256,
            "declaration_sha256": declaration.declaration_sha256,
            "source_kind": "source_obligation",
        }
        for node, declaration in dependent_slices
    ]
    dependent_records.extend(
        {
            "node_id": node.id,
            "name": node.name,
            "file": node.file,
            "source_sha256": "",
            "declaration_sha256": _sha256_text(node.statement),
            "source_kind": "graph_artifact",
        }
        for node in graph_artifacts
    )
    dependent_records.sort(key=lambda record: (record["name"], record["node_id"]))

    prepared = dict(transaction)
    for field in (
        "committed_at",
        "immutable_fingerprint",
        "last_reconciliation_at",
        "last_reconciliation_reason",
        "manual_retry_authorized_at",
        "manual_retry_reason",
        "promotion_evidence_sha256",
        "quarantined_at",
        "reason",
        "transaction_id",
    ):
        prepared.pop(field, None)
    prepared.update(
        {
            "version": 3,
            "state": "pending",
            "prepared_at": _now_iso(),
            "file": file_identity,
            "source_before_sha256": _sha256_bytes(current_source.encode("utf-8")),
            "source_after_sha256": _sha256_bytes(after_source.encode("utf-8")),
            "source_after": after_source,
            "parent_current_declaration_sha256": parent.declaration_sha256,
            "ownership_basis": _transaction_base_ownership_basis(transaction),
            "migration_from_transaction_id": str(transaction.get("transaction_id", "") or ""),
            "invalidated_dependents": dependent_records,
        }
    )
    return _seal_transaction(prepared), ""


def _prepare_committed_dependent_migrations(*, project_root: Path) -> int:
    """Upgrade exact stale v1 cleanup tombstones into replayable v3 transactions."""
    summary = plan_state.load_summary()
    raw_transactions = summary.get("false_decomposition_cleanup_transactions")
    audit = _audit_cleanup_transactions(raw_transactions)
    if not isinstance(raw_transactions, list) or audit.pending:
        return 0
    candidates = [
        dict(raw_transactions[record.index])
        for record in audit.records
        if record.disposition == "terminal"
        and isinstance(raw_transactions[record.index], Mapping)
        and raw_transactions[record.index].get("version") == 1
        and str(raw_transactions[record.index].get("state", "") or "") == "committed"
        and _transaction_base_ownership_basis(raw_transactions[record.index]) == "decomposer-graph"
    ]
    prepared = 0
    for transaction in candidates:
        promotion = _transaction_promotion(transaction)
        provenance_id = str(transaction.get("provenance_id", "") or "")
        helper_node_id = str(transaction.get("helper_node_id", "") or "")
        discovery_blueprint = plan_state.load_blueprint()
        if not _legacy_migration_has_structural_work(
            discovery_blueprint,
            helper_node_id=helper_node_id,
        ):
            # An evidence-only tombstone has no source or graph branch left to
            # migrate. Do not reopen historical evidence validation merely
            # because startup discovered an authenticated committed-v1 row.
            if _legacy_cleanup_is_archived(summary, transaction):
                _resolve_no_work_legacy_migration_quarantine(transaction)
            continue
        if not _legacy_cleanup_is_archived(summary, transaction):
            _quarantine_candidate(
                promotion,
                reason="committed cleanup dependent migration lacks an exact archive witness",
                provenance_id=provenance_id,
            )
            continue
        path = Path(str(transaction.get("file", "") or ""))
        try:
            with decomposition_provenance.source_operation(path, canonical=True) as operation:
                file_identity = str(operation.path)
                current_source = decomposition_provenance.read_source_bytes(operation).decode(
                    "utf-8"
                )
                with plan_state.blueprint_commit_guard():
                    blueprint, helper, graph_file, helper_reason = _graph_helper(
                        promotion,
                        source_identity=file_identity,
                        project_root=project_root,
                    )
                    if helper is None:
                        # A fully retired helper has no stale dependent edge to migrate.
                        if any(
                            helper_node_id in {edge.source, edge.target} for edge in blueprint.edges
                        ):
                            _quarantine_candidate(
                                promotion,
                                reason=("committed cleanup dependent migration: " + helper_reason),
                                provenance_id=provenance_id,
                            )
                        continue
                    if helper.status != "false" or helper.generated_by != "decomposer":
                        _quarantine_candidate(
                            promotion,
                            reason=(
                                "committed cleanup dependent migration lost its exact false "
                                "decomposer tombstone"
                            ),
                            provenance_id=provenance_id,
                        )
                        continue
                    parent, parent_reason = _unique_parent_node(
                        blueprint,
                        parent_name=str(transaction.get("parent", "") or ""),
                        graph_file=graph_file,
                        expected_id=str(transaction.get("parent_node_id", "") or ""),
                        project_root=project_root,
                        source_identity=file_identity,
                    )
                    if parent is None:
                        _quarantine_candidate(
                            promotion,
                            reason=("committed cleanup dependent migration: " + parent_reason),
                            provenance_id=provenance_id,
                        )
                        continue
                    shape, shape_reason = _graph_cleanup_shape(
                        blueprint,
                        helper=helper,
                        parent=parent,
                        promotion=promotion,
                        source_text=current_source,
                        dependent_source_revision=_sha256_text(current_source),
                        transitive_dependents=True,
                        detach_incoming_dependents=False,
                        allow_graph_only_dependents=True,
                        require_parent_split=True,
                        evidence_source_revision=_sha256_text(current_source),
                    )
                    if shape is None:
                        _quarantine_candidate(
                            promotion,
                            reason=("committed cleanup dependent migration: " + shape_reason),
                            provenance_id=provenance_id,
                        )
                        continue
                    if not shape.invalidated_dependents:
                        continue
                    migration, migration_reason = _build_committed_dependent_migration(
                        transaction,
                        current_source=current_source,
                        file_identity=file_identity,
                        graph_shape=shape,
                    )
                    if migration is None:
                        _quarantine_candidate(
                            promotion,
                            reason=("committed cleanup dependent migration: " + migration_reason),
                            provenance_id=provenance_id,
                        )
                        continue
                    _begin_transaction(migration)
                    prepared += 1
        except (OSError, RuntimeError, UnicodeDecodeError) as exc:
            _quarantine_candidate(
                promotion,
                reason=(
                    "committed cleanup dependent migration source/graph drift: " f"{str(exc)[:160]}"
                ),
                provenance_id=provenance_id,
            )
    return prepared


def recover_cleanup_transactions(*, cwd: str = "") -> CleanupReconciliation:
    """Replay every pending cleanup transaction before considering new evidence."""
    if not plan_state.plan_state_enabled():
        return CleanupReconciliation()
    project_root = Path(cwd or ".").expanduser().resolve()
    _restore_authorized_retry_transactions()
    _prepare_committed_dependent_migrations(project_root=project_root)
    raw_transactions = plan_state.load_summary().get("false_decomposition_cleanup_transactions")
    audit = _audit_cleanup_transactions(raw_transactions)
    pending = (
        [
            dict(raw_transactions[record.index])
            for record in audit.records
            if record.disposition == "live"
            and record.state == "pending"
            and isinstance(raw_transactions[record.index], Mapping)
        ]
        if isinstance(raw_transactions, list)
        else []
    )
    if audit.pending > _TRANSACTION_CAP:
        # A legacy or externally modified summary may already exceed the new
        # live-record invariant. Do not mutate or partially replay it; native
        # startup consumes this final state as a resumable operational pause.
        return _cleanup_reconciliation_state()
    cleaned = 0
    for transaction in pending:
        outcome, _reason = _execute_transaction(transaction, project_root=project_root)
        if outcome == "cleaned":
            cleaned += 1
    return _cleanup_reconciliation_state(cleaned=cleaned)


def _reconcile_candidate_under_lease(
    promotion: Mapping[str, Any],
    *,
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
    validate_promotion: Callable[[Mapping[str, Any]], Any] | None,
    legacy_evidence_quarantine_id: str = "",
) -> bool:
    """Prepare and execute one cleanup from a pinned promotion source identity."""
    file_identity = str(operation.path)
    try:
        current_bytes = decomposition_provenance.read_source_bytes(operation)
        current_source = current_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _quarantine_candidate(promotion, reason=f"cleanup source unavailable: {str(exc)[:160]}")
        return False
    blueprint, helper_node, graph_file, helper_graph_reason = _graph_helper(
        promotion,
        source_identity=file_identity,
        project_root=project_root,
    )
    # Reject the exact graph identity before provenance recovery can migrate or
    # upsert any legacy ownership record from this source.
    if helper_node is None:
        _quarantine_candidate(promotion, reason=helper_graph_reason)
        return False
    if helper_node.status != "false":
        _quarantine_candidate(
            promotion,
            reason="promoted helper does not retain authoritative false graph status",
        )
        return False
    provenance, provenance_reason = decomposition_provenance.resolve_helper_provenance(
        helper_name=str(promotion.get("theorem", "") or ""),
        file_label=file_identity,
        promotion_signature_sha256=str(promotion.get("declaration_signature_sha256", "") or ""),
        current_source=current_source,
        cwd=str(project_root),
    )
    try:
        if decomposition_provenance.read_source_bytes(operation) != current_bytes:
            raise OSError("source changed during provenance recovery")
    except OSError as exc:
        _quarantine_candidate(
            promotion,
            reason=f"cleanup source identity changed during provenance recovery: {str(exc)[:160]}",
        )
        return False
    provenance_is_exact = (
        provenance is not None and str(provenance.get("state", "") or "") == "committed"
    )
    graph_is_decomposer_owned = helper_node.generated_by == "decomposer"
    if bool(promotion.get("is_main_goal")) and not (
        provenance_is_exact or graph_is_decomposer_owned
    ):
        return False
    if not graph_is_decomposer_owned and not (
        bool(promotion.get("is_main_goal")) and provenance_is_exact
    ):
        _quarantine_candidate(
            promotion,
            reason="promoted helper is not a decomposer-owned graph node",
        )
        return False
    if provenance is None:
        _quarantine_candidate(promotion, reason=provenance_reason)
        return False
    provenance_id = str(provenance.get("transaction_id", "") or "")
    parent_name = str(provenance.get("parent", "") or "").strip()
    parent_node, parent_graph_reason = _unique_parent_node(
        blueprint,
        parent_name=parent_name,
        graph_file=graph_file,
        project_root=project_root,
        source_identity=file_identity,
    )
    if parent_node is None:
        _quarantine_candidate(
            promotion,
            reason=parent_graph_reason,
            provenance_id=provenance_id,
        )
        return False
    graph_shape, dependency_reason = _graph_cleanup_shape(
        blueprint,
        helper=helper_node,
        parent=parent_node,
        promotion=promotion,
        source_text=current_source,
    )
    if graph_shape is None:
        _quarantine_candidate(
            promotion,
            reason=dependency_reason,
            provenance_id=provenance_id,
        )
        return False
    if legacy_evidence_quarantine_id and not graph_shape.preserved_evidence:
        # Keep the historical quarantine live. Its one safe automatic migration
        # is specifically the newly recognized promotion-bound audit edge.
        return False
    if legacy_evidence_quarantine_id:
        current_quarantine_id, _quarantine_reason = _exact_live_legacy_evidence_quarantine_id(
            promotion
        )
        if current_quarantine_id != legacy_evidence_quarantine_id:
            return False
    if validate_promotion is not None:
        fresh = dict(promotion)
        fresh.pop("promotion_id", None)
        fresh["source_revision_sha256"] = _sha256_bytes(current_bytes)
        validation = validate_promotion(fresh)
        if not bool(getattr(validation, "ok", False)):
            reason = str(
                getattr(validation, "reason", "fresh negation validation failed")
                or "fresh negation validation failed"
            )
            if bool(getattr(validation, "retryable", False)):
                failure_kind = str(
                    getattr(validation, "failure_kind", "infrastructure_unavailable")
                    or "infrastructure_unavailable"
                )
                raise _RetryablePromotionValidation(f"{failure_kind}: {reason}")
            _quarantine_candidate(
                promotion,
                reason=f"fresh negation evidence failed: {reason}",
                provenance_id=provenance_id,
            )
            return False
        try:
            if decomposition_provenance.read_source_bytes(operation) != current_bytes:
                raise OSError("source changed during fresh negation validation")
        except OSError as exc:
            _quarantine_candidate(
                promotion,
                reason=f"cleanup source identity changed during validation: {str(exc)[:160]}",
                provenance_id=provenance_id,
            )
            return False
        if bool(getattr(validation, "is_main_goal", False)):
            # Legacy records could carry a stale ``is_main_goal`` bit, so
            # cleanup still uses exact provenance to discover old helpers.
            # A fresh promotion revalidation, however, is the classification
            # authority. Never let later mutable graph/provenance ownership
            # turn an authenticated requested root into deletable source.
            return False
        promotion_id = _promotion_id(promotion)
        promotion_audit = _audit_active_promotions(
            plan_state.load_summary().get("negation_promotions")
        )
        matching_indexes = promotion_audit.matching_indexes(promotion_id)
        if len(matching_indexes) != 1:
            _quarantine_candidate(
                promotion,
                reason="fresh negation evidence has ambiguous durable promotion authority",
                provenance_id=provenance_id,
            )
            return False
        durable_record = promotion_audit.records[matching_indexes[0]]
        if durable_record.disposition == "reconcilable":
            authoritative = getattr(validation, "evidence", None)
            if not isinstance(authoritative, Mapping):
                _quarantine_candidate(
                    promotion,
                    reason="legacy promotion revalidation did not return sealable evidence",
                    provenance_id=provenance_id,
                )
                return False
            try:
                promotion = _bridge_revalidated_promotion(
                    promotion,
                    authoritative,
                    legacy_evidence_quarantine_id=legacy_evidence_quarantine_id,
                )
            except RuntimeError as exc:
                _quarantine_candidate(
                    promotion,
                    reason=f"legacy promotion bridge failed: {str(exc)[:160]}",
                    provenance_id=provenance_id,
                )
                return False
            try:
                if decomposition_provenance.read_source_bytes(operation) != current_bytes:
                    raise OSError("source changed during legacy promotion bridge")
            except OSError as exc:
                _quarantine_candidate(
                    promotion,
                    reason=(
                        "cleanup source identity changed during promotion bridge: "
                        f"{str(exc)[:160]}"
                    ),
                    provenance_id=provenance_id,
                )
                return False
        elif durable_record.disposition != "active":
            _quarantine_candidate(
                promotion,
                reason="fresh negation evidence does not have active durable authority",
                provenance_id=provenance_id,
            )
            return False
    if legacy_evidence_quarantine_id:
        current_quarantine_id, _quarantine_reason = _exact_live_legacy_evidence_quarantine_id(
            promotion
        )
        if not current_quarantine_id:
            return False
        legacy_evidence_quarantine_id = current_quarantine_id
    transaction, reason = _build_source_transaction(
        promotion,
        provenance,
        current_source=current_source,
        file_identity=file_identity,
        invalidated_dependents=graph_shape.invalidated_dependents,
    )
    if transaction is None:
        _quarantine_candidate(promotion, reason=reason, provenance_id=provenance_id)
        return False
    transaction["helper_node_id"] = helper_node.id
    transaction["parent_node_id"] = parent_node.id
    transaction["graph_file"] = graph_file
    ownership_basis = "decomposer-graph" if graph_is_decomposer_owned else "committed-provenance"
    if legacy_evidence_quarantine_id:
        ownership_basis += _EVIDENCE_TOMBSTONE_BASIS_MARKER + legacy_evidence_quarantine_id
    transaction["ownership_basis"] = ownership_basis
    transaction = _seal_transaction(transaction)
    try:
        transaction = _begin_transaction(transaction)
    except CleanupTransactionCapacityError as exc:
        _quarantine_candidate(
            promotion,
            reason=f"cleanup transaction capacity exhausted: {exc}",
            provenance_id=provenance_id,
        )
        return False
    if str(transaction.get("state", "") or "") != "pending":
        return False
    _cleanup_transaction_hook("pending-persisted")
    try:
        outcome, _reason = _execute_transaction_under_lease(
            transaction,
            project_root=project_root,
            operation=operation,
        )
    except (OSError, UnicodeDecodeError) as exc:
        _mark_transaction_quarantined(
            transaction,
            f"cleanup source identity changed before replay: {str(exc)[:160]}",
        )
        return False
    return outcome == "cleaned"


def _recoverable_dependent_let_signature_quarantine(
    promotion: Mapping[str, Any],
    *,
    project_root: Path,
) -> dict[str, Any] | None:
    """Return exact committed provenance proving the legacy parser mismatch.

    Automatic retry is intentionally narrower than ordinary cleanup admission:
    the source must still be the promoted revision, its full statement must
    match promotion evidence while its historical prefix hash differs, and a
    committed decomposer transaction must contain the exact inserted
    declaration from which that full identity can be reconstructed.
    """
    path, _reason = _promotion_source_path(promotion, project_root=project_root)
    if path is None:
        return None
    promoted_signature = str(promotion.get("declaration_signature_sha256", "") or "").strip()
    promoted_revision = str(promotion.get("source_revision_sha256", "") or "").strip()
    helper_name = str(promotion.get("theorem", "") or "").strip()
    if not promoted_signature or not promoted_revision or not helper_name:
        return None
    try:
        with decomposition_provenance.source_operation(path, canonical=True) as operation:
            current_bytes = decomposition_provenance.read_source_bytes(operation)
            if _sha256_bytes(current_bytes) != promoted_revision:
                return None
            current_source = current_bytes.decode("utf-8")
            helper = decomposition_provenance.declaration_slice(current_source, helper_name)
            if (
                helper is None
                or helper.signature_sha256 == promoted_signature
                or decomposition_provenance.full_declaration_signature_sha256(helper.text)
                != promoted_signature
            ):
                return None
            provenance, _provenance_reason = decomposition_provenance.resolve_helper_provenance(
                helper_name=helper_name,
                file_label=str(operation.path),
                promotion_signature_sha256=promoted_signature,
                current_source=current_source,
                cwd=str(project_root),
            )
            if decomposition_provenance.read_source_bytes(operation) != current_bytes:
                return None
    except (OSError, UnicodeDecodeError):
        return None
    if provenance is None or str(provenance.get("state", "") or "") != "committed":
        return None
    return dict(provenance)


def _resolve_authenticated_parser_mismatch_quarantine(
    *,
    quarantine_id: str,
    promotion: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> bool:
    """Resolve only one exact historical parser quarantine row.

    This migration deliberately leaves every cleanup transaction untouched.
    A subsequent ordinary reconciliation must build and validate a fresh plan;
    an unrelated quarantined transaction for the promotion remains blocking.
    """
    target = str(quarantine_id or "").strip()
    promotion_id = _promotion_id(promotion)
    provenance_id = str(provenance.get("transaction_id", "") or "").strip()
    if not target or not promotion_id or not provenance_id:
        return False

    def mutate(summary: dict[str, Any]) -> bool:
        records, target_index = _cleanup_quarantine_records_for_mutation(
            summary,
            quarantine_id=target,
        )
        if target_index is None:
            return False
        audit = _audit_cleanup_quarantines(records)
        live_parser_indexes: list[int] = []
        for audited in audit.records:
            if audited.disposition == "terminal":
                continue
            raw = records[audited.index]
            if not isinstance(raw, Mapping):
                continue
            nested = raw.get("promotion")
            if (
                isinstance(raw.get("reason"), str)
                and raw.get("reason") in _SIGNATURE_MISMATCH_REASONS
                and isinstance(nested, Mapping)
                and _promotion_id(nested) == promotion_id
            ):
                live_parser_indexes.append(audited.index)
        if live_parser_indexes != [target_index]:
            return False

        selected = records[target_index]
        if not isinstance(selected, Mapping):
            return False
        selected_audit = audit.records[target_index]
        nested_promotion = selected.get("promotion")
        recorded_provenance_id = str(selected.get("provenance_id", "") or "")
        reason = str(selected.get("reason", "") or "")
        if (
            selected_audit.disposition != "live"
            or selected_audit.state != "quarantined"
            or not isinstance(nested_promotion, Mapping)
            or dict(nested_promotion) != dict(promotion)
            or (recorded_provenance_id and recorded_provenance_id != provenance_id)
            or (not recorded_provenance_id and reason != _LEGACY_SIGNATURE_MISMATCH_REASON)
        ):
            return False

        active_records, active_index = _active_promotion_records_for_mutation(
            summary,
            promotion_id=promotion_id,
        )
        if active_index is None:
            return False
        active_record = active_records[active_index]
        if not isinstance(active_record, Mapping) or dict(active_record) != dict(promotion):
            return False
        raw_provenance = summary.get("decomposition_provenance")
        if not isinstance(raw_provenance, list):
            return False
        provenance_matches = [
            item
            for item in raw_provenance
            if isinstance(item, Mapping)
            and str(item.get("transaction_id", "") or "") == provenance_id
        ]
        if len(provenance_matches) != 1 or dict(provenance_matches[0]) != dict(provenance):
            return False
        if str(provenance_matches[0].get("state", "") or "") != "committed":
            return False

        resolved_at = _now_iso()
        records[target_index] = {
            **dict(selected),
            "state": "resolved",
            "resolved_at": resolved_at,
            "resolution_reason": (
                "automatically reconciled the historical dependent-let signature parser "
                "identity against exact promotion and insertion evidence"
            ),
        }
        summary["false_decomposition_cleanup_quarantine"] = _retained_cleanup_quarantines(records)
        return True

    try:
        return bool(update_json_file(plan_state.plan_state_paths().summary_json, mutate))
    except RuntimeError:
        # Automatic migration is optional. Ambiguous/duplicate durable
        # authority must keep the exact quarantine live and pause resumably.
        return False


def _recoverable_multiple_evidence_quarantine(
    promotion: Mapping[str, Any],
    *,
    project_root: Path,
    provenance_id: str,
) -> dict[str, Any] | None:
    """Return provenance for the exact live multi-evidence classifier upgrade.

    The obsolete classifier rejected a valid promotion as soon as a second
    kernel-gated prover finding pointed at the same false helper. Admit only an
    unchanged promoted source, its exact committed insertion record, one
    promotion-bound proof edge, and the newly authenticated graph shape.
    """
    expected_provenance_id = str(provenance_id or "").strip()
    promoted_revision = str(promotion.get("source_revision_sha256", "") or "").strip()
    helper_name = str(promotion.get("theorem", "") or "").strip()
    if not expected_provenance_id or not promoted_revision or not helper_name:
        return None
    path, _path_reason = _promotion_source_path(promotion, project_root=project_root)
    if path is None:
        return None
    try:
        with decomposition_provenance.source_operation(path, canonical=True) as operation:
            current_bytes = decomposition_provenance.read_source_bytes(operation)
            if _sha256_bytes(current_bytes) != promoted_revision:
                return None
            current_source = current_bytes.decode("utf-8")
            provenance, _provenance_reason = decomposition_provenance.resolve_helper_provenance(
                helper_name=helper_name,
                file_label=str(operation.path),
                promotion_signature_sha256=str(
                    promotion.get("declaration_signature_sha256", "") or ""
                ),
                current_source=current_source,
                cwd=str(project_root),
            )
            if decomposition_provenance.read_source_bytes(operation) != current_bytes:
                return None
            blueprint, helper, graph_file, _helper_reason = _graph_helper(
                promotion,
                source_identity=str(operation.path),
                project_root=project_root,
            )
    except (OSError, UnicodeDecodeError):
        return None
    if (
        provenance is None
        or str(provenance.get("state", "") or "") != "committed"
        or str(provenance.get("transaction_id", "") or "") != expected_provenance_id
        or helper is None
        or helper.status != "false"
        or helper.generated_by != "decomposer"
    ):
        return None
    parent, _parent_reason = _unique_parent_node(
        blueprint,
        parent_name=str(provenance.get("parent", "") or ""),
        graph_file=graph_file,
        project_root=project_root,
        source_identity=str(path),
    )
    if parent is None:
        return None
    shape, _shape_reason = _graph_cleanup_shape(
        blueprint,
        helper=helper,
        parent=parent,
        promotion=promotion,
        source_text=current_source,
    )
    if shape is None or len(shape.preserved_evidence) < 2:
        return None
    return dict(provenance)


def _resolve_authenticated_multiple_evidence_quarantine(
    *,
    quarantine_id: str,
    promotion: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> bool:
    """Resolve one exact obsolete multi-evidence classifier quarantine row."""
    target = str(quarantine_id or "").strip()
    promotion_id = _promotion_id(promotion)
    provenance_id = str(provenance.get("transaction_id", "") or "").strip()
    if not target or not promotion_id or not provenance_id:
        return False

    def mutate(summary: dict[str, Any]) -> bool:
        records, target_index = _cleanup_quarantine_records_for_mutation(
            summary,
            quarantine_id=target,
        )
        if target_index is None:
            return False
        audit = _audit_cleanup_quarantines(records)
        matching_indexes: list[int] = []
        for audited in audit.records:
            if audited.disposition == "terminal":
                continue
            raw = records[audited.index]
            if not isinstance(raw, Mapping):
                continue
            nested = raw.get("promotion")
            if (
                raw.get("reason") == _MULTIPLE_VERIFIED_EVIDENCE_QUARANTINE_REASON
                and isinstance(nested, Mapping)
                and _promotion_id(nested) == promotion_id
            ):
                matching_indexes.append(audited.index)
        if matching_indexes != [target_index]:
            return False

        selected = records[target_index]
        if not isinstance(selected, Mapping):
            return False
        selected_audit = audit.records[target_index]
        nested_promotion = selected.get("promotion")
        if (
            selected_audit.disposition != "live"
            or selected_audit.state != "quarantined"
            or not isinstance(nested_promotion, Mapping)
            or dict(nested_promotion) != dict(promotion)
            or str(selected.get("provenance_id", "") or "") != provenance_id
        ):
            return False

        active_records, active_index = _active_promotion_records_for_mutation(
            summary,
            promotion_id=promotion_id,
        )
        if active_index is None:
            return False
        active_record = active_records[active_index]
        if not isinstance(active_record, Mapping) or dict(active_record) != dict(promotion):
            return False
        raw_provenance = summary.get("decomposition_provenance")
        if not isinstance(raw_provenance, list):
            return False
        provenance_matches = [
            item
            for item in raw_provenance
            if isinstance(item, Mapping)
            and str(item.get("transaction_id", "") or "") == provenance_id
        ]
        if (
            len(provenance_matches) != 1
            or dict(provenance_matches[0]) != dict(provenance)
            or str(provenance_matches[0].get("state", "") or "") != "committed"
        ):
            return False

        resolved_at = _now_iso()
        records[target_index] = {
            **dict(selected),
            "state": "resolved",
            "resolved_at": resolved_at,
            "resolution_reason": (
                "automatically reconciled verified same-revision prover evidence while "
                "preserving the promotion-bound false-helper audit tombstone"
            ),
        }
        summary["false_decomposition_cleanup_quarantine"] = _retained_cleanup_quarantines(records)
        return True

    try:
        return bool(update_json_file(plan_state.plan_state_paths().summary_json, mutate))
    except RuntimeError:
        return False


def reconcile_false_decompositions(
    promotions: list[dict[str, Any]],
    *,
    cwd: str = "",
    validate_promotion: Callable[[Mapping[str, Any]], Any] | None = None,
) -> CleanupReconciliation:
    """Clean exact campaign-created false sublemmas and quarantine ambiguity."""
    if not plan_state.plan_state_enabled():
        return CleanupReconciliation()
    project_root = Path(cwd or ".").expanduser().resolve()
    recovered = recover_cleanup_transactions(cwd=str(project_root))
    if recovered.pending > _TRANSACTION_CAP:
        return recovered
    cleaned = recovered.cleaned
    retryable_pending = 0
    retryable_reasons: list[str] = []
    summary = plan_state.load_summary()
    active_promotions = {
        str(item.get("promotion_id", "") or ""): dict(item)
        for item in (summary.get("negation_promotions") or [])
        if isinstance(item, Mapping) and str(item.get("promotion_id", "") or "")
    }
    active_ids = set(active_promotions)
    raw_cleanup_transactions = summary.get("false_decomposition_cleanup_transactions")
    cleanup_audit = _audit_cleanup_transactions(raw_cleanup_transactions)
    blocked_promotion_ids: set[str] = set()
    if isinstance(raw_cleanup_transactions, list):
        blocked_promotion_ids.update(
            record.promotion_id
            for record in cleanup_audit.records
            if record.disposition == "live"
            and record.state in {"pending", "quarantined"}
            and record.promotion_id
        )
    raw_quarantines = summary.get("false_decomposition_cleanup_quarantine")
    quarantine_audit = _audit_cleanup_quarantines(raw_quarantines)
    legacy_evidence_retry_quarantine_ids: dict[str, list[str]] = {}
    for record in quarantine_audit.records:
        if record.disposition == "ambiguous":
            # Duplicate, forged, or otherwise unauthenticated quarantine rows
            # are still negative authority for any safely extracted target.
            # Never let registry ambiguity make that promotion auto-cleanable.
            if record.promotion_id:
                blocked_promotion_ids.add(record.promotion_id)
            continue
        if record.disposition != "live":
            continue
        if not isinstance(raw_quarantines, list):
            continue
        item = raw_quarantines[record.index]
        if not isinstance(item, Mapping):
            continue
        item_promotion = item.get("promotion")
        if isinstance(item_promotion, Mapping):
            item_promotion_id = str(item_promotion.get("promotion_id", "") or "")
            active_promotion = active_promotions.get(item_promotion_id)
            if (
                item.get("reason") == _LEGACY_EVIDENCE_QUARANTINE_REASON
                and active_promotion is not None
                and dict(item_promotion) == active_promotion
            ):
                legacy_evidence_retry_quarantine_ids.setdefault(item_promotion_id, []).append(
                    record.record_id
                )
            elif (
                item.get("reason") in _SIGNATURE_MISMATCH_REASONS
                and active_promotion is not None
                and dict(item_promotion) == active_promotion
            ):
                exact_provenance = _recoverable_dependent_let_signature_quarantine(
                    active_promotion,
                    project_root=project_root,
                )
                reconciled = exact_provenance is not None and (
                    _resolve_authenticated_parser_mismatch_quarantine(
                        quarantine_id=record.record_id,
                        promotion=active_promotion,
                        provenance=exact_provenance,
                    )
                )
                if reconciled:
                    _record_event(
                        "false-decomposition-cleanup-quarantine-reconciled",
                        "Reconciled dependent-let signature quarantine",
                        theorem=str(active_promotion.get("theorem", "") or ""),
                        promotion_id=item_promotion_id,
                        quarantine_id=record.record_id,
                        reason="authenticated dependent-let parser migration",
                    )
                else:
                    blocked_promotion_ids.add(item_promotion_id)
            elif (
                item.get("reason") == _MULTIPLE_VERIFIED_EVIDENCE_QUARANTINE_REASON
                and active_promotion is not None
                and dict(item_promotion) == active_promotion
            ):
                exact_provenance = _recoverable_multiple_evidence_quarantine(
                    active_promotion,
                    project_root=project_root,
                    provenance_id=str(item.get("provenance_id", "") or ""),
                )
                reconciled = exact_provenance is not None and (
                    _resolve_authenticated_multiple_evidence_quarantine(
                        quarantine_id=record.record_id,
                        promotion=active_promotion,
                        provenance=exact_provenance,
                    )
                )
                if reconciled:
                    _record_event(
                        "false-decomposition-cleanup-quarantine-reconciled",
                        "Reconciled verified multi-evidence cleanup quarantine",
                        theorem=str(active_promotion.get("theorem", "") or ""),
                        promotion_id=item_promotion_id,
                        quarantine_id=record.record_id,
                        reason="authenticated same-revision prover evidence",
                    )
                else:
                    blocked_promotion_ids.add(item_promotion_id)
            else:
                blocked_promotion_ids.add(item_promotion_id)
    for promotion_id, quarantine_ids in legacy_evidence_retry_quarantine_ids.items():
        # More than one live quarantine for the same authority is ambiguous;
        # only the unique historical classifier row may auto-migrate.
        if len(quarantine_ids) != 1 or not quarantine_ids[0]:
            blocked_promotion_ids.add(promotion_id)
    unique_legacy_evidence_retries = {
        promotion_id: quarantine_ids[0]
        for promotion_id, quarantine_ids in legacy_evidence_retry_quarantine_ids.items()
        if len(quarantine_ids) == 1
        and quarantine_ids[0]
        and promotion_id not in blocked_promotion_ids
    }
    for promotion in promotions:
        promotion_id = _promotion_id(promotion)
        if promotion_id and promotion_id not in active_ids:
            continue
        if promotion_id and promotion_id in blocked_promotion_ids:
            continue
        path, path_reason = _promotion_source_path(promotion, project_root=project_root)
        if path is None:
            _quarantine_candidate(promotion, reason=path_reason)
            continue
        try:
            with decomposition_provenance.source_operation(path, canonical=True) as operation:
                cleaned += int(
                    _reconcile_candidate_under_lease(
                        promotion,
                        project_root=project_root,
                        operation=operation,
                        validate_promotion=validate_promotion,
                        legacy_evidence_quarantine_id=unique_legacy_evidence_retries.get(
                            promotion_id, ""
                        ),
                    )
                )
        except _RetryablePromotionValidation as exc:
            retryable_pending += 1
            retryable_reasons.append(f"fresh negation evidence awaits retry: {str(exc)[:200]}")
        except (OSError, UnicodeDecodeError) as exc:
            _quarantine_candidate(
                promotion,
                reason=f"cleanup source unavailable: {str(exc)[:160]}",
            )
    state = _cleanup_reconciliation_state(cleaned=cleaned)
    if not retryable_pending:
        return state
    return replace(
        state,
        pending=state.pending + retryable_pending,
        reasons=tuple(dict.fromkeys([*retryable_reasons, *state.reasons]))[:20],
    )
