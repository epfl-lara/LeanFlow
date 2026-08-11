"""Audit and retain false-decomposition cleanup and dependent-invalidations evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from leanflow_cli.workflows import decomposition_provenance, plan_state

TERMINAL_TRANSACTION_STATES = frozenset({"committed"})
LIVE_TRANSACTION_STATES = frozenset({"pending", "quarantined", "manual-retry-authorized"})
TERMINAL_QUARANTINE_STATES = frozenset({"resolved"})
LIVE_QUARANTINE_STATES = frozenset({"quarantined"})
DEFAULT_TERMINAL_HISTORY_CAP = 50

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
_TRANSACTION_FIELDS = frozenset(
    {
        *_TRANSACTION_V3_IDENTITY_FIELDS,
        "state",
        "prepared_at",
        "promotion",
        "source_after",
        "parent_restored_declaration",
        "parent_restored_statement",
        "immutable_fingerprint",
        "transaction_id",
        "last_reconciliation_at",
        "last_reconciliation_reason",
        "committed_at",
        "quarantined_at",
        "reason",
        "manual_retry_authorized_at",
        "manual_retry_reason",
        "invalidated_dependents",
        "migration_from_transaction_id",
    }
)
_TRANSACTION_REQUIRED_FIELDS = frozenset(
    {
        *_TRANSACTION_V1_IDENTITY_FIELDS,
        "state",
        "prepared_at",
        "promotion",
        "parent_restored_declaration",
        "parent_restored_statement",
        "immutable_fingerprint",
        "transaction_id",
    }
)
_TRANSACTION_SHA256_FIELDS = (
    "promotion_id",
    "provenance_id",
    "source_before_sha256",
    "source_after_sha256",
    "helper_declaration_sha256",
    "helper_signature_sha256",
    "parent_current_declaration_sha256",
    "parent_signature_sha256",
    "parent_restored_declaration_sha256",
    "promotion_evidence_sha256",
    "immutable_fingerprint",
    "transaction_id",
)
_QUARANTINE_FIELDS = frozenset(
    {
        "quarantine_id",
        "state",
        "quarantined_at",
        "reason",
        "promotion",
        "provenance_id",
        "resolved_at",
        "resolution_reason",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class FalseCleanupRecordAudit:
    """Classify one raw cleanup registry element without changing it."""

    index: int
    disposition: str
    state: str = ""
    record_id: str = ""
    promotion_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class FalseCleanupRegistryAudit:
    """Report terminal history and every unresolved cleanup registry element."""

    ok: bool
    retained_registry: object
    records: tuple[FalseCleanupRecordAudit, ...] = ()
    pending: int = 0
    ambiguous: int = 0
    terminal: int = 0
    reasons: tuple[str, ...] = ()


def _nonempty_string(value: object) -> str:
    """Return an exact non-empty string without coercing durable evidence."""
    return value.strip() if isinstance(value, str) else ""


def _valid_sha256(value: object) -> bool:
    """Return whether a value is one lowercase SHA-256 digest."""
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _sha256_text(value: str) -> str:
    """Hash exact UTF-8 text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    """Hash one JSON-compatible payload with the writer's canonical encoding."""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(serialized)


def _lexical_absolute_file(value: object) -> str:
    """Return one normalized absolute path without consulting live filesystem state."""
    raw = _nonempty_string(value)
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        return ""
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        return ""
    return str(path)


def _transaction_fingerprint(record: Mapping[object, object]) -> str:
    """Hash the immutable cleanup fields using the production writer format."""
    identity = {
        field: record.get(field) for field in _transaction_identity_fields(record.get("version"))
    }
    return _sha256_json(identity)


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


def _graph_identity_sha256(promotion: Mapping[object, object]) -> str:
    """Hash the optional current graph binding carried by a promotion."""
    payload = {
        "theorem": promotion.get("theorem"),
        "operation_path": promotion.get("operation_path"),
        "node_id": promotion.get("node_id"),
        "graph_node_name": promotion.get("graph_node_name"),
        "graph_node_file": promotion.get("graph_node_file"),
        "is_main_goal": promotion.get("is_main_goal"),
    }
    return _sha256_json(payload)


def _graph_statement(declaration: str, name: str) -> str:
    """Return the normalized proposition sealed into the parent graph record."""
    parsed = decomposition_provenance.declaration_slice(declaration, name)
    if parsed is None:
        return ""
    statement = re.sub(
        r"^\s*(?:@\[[^\]]*\]\s*)?(?:private\s+)?(?:theorem|lemma)\s+\S+",
        "",
        parsed.signature,
    )
    return " ".join(statement.split())


def _promotion_integrity_reason(
    raw: object,
    *,
    record: Mapping[object, object],
) -> tuple[str, str]:
    """Authenticate the nested promotion fields consumed by cleanup replay."""
    if not isinstance(raw, Mapping):
        return "cleanup promotion is not a mapping", ""
    promotion_id = _nonempty_string(raw.get("promotion_id"))
    if not _valid_sha256(promotion_id):
        return "cleanup promotion lacks a valid identity", promotion_id
    if promotion_id != record.get("promotion_id"):
        return "cleanup promotion identity differs from its sealed plan", promotion_id
    helper = _nonempty_string(record.get("helper"))
    helper_node_id = _nonempty_string(record.get("helper_node_id"))
    helper_signature = _nonempty_string(record.get("helper_signature_sha256"))
    if raw.get("theorem") != helper:
        return "cleanup promotion theorem differs from its sealed helper", promotion_id
    if raw.get("node_id") != helper_node_id:
        return "cleanup promotion graph node differs from its sealed helper", promotion_id
    if raw.get("declaration_signature_sha256") != helper_signature:
        return "cleanup promotion signature differs from its sealed helper", promotion_id
    try:
        evidence_hash = _sha256_json(dict(raw))
    except (TypeError, ValueError):
        return "cleanup promotion evidence is not JSON serializable", promotion_id
    if evidence_hash != record.get("promotion_evidence_sha256"):
        return "cleanup promotion evidence differs from its sealed plan", promotion_id

    binding_fields = (
        "operation_path",
        "graph_node_name",
        "graph_node_file",
        "graph_identity_sha256",
    )
    present = [bool(_nonempty_string(raw.get(field))) for field in binding_fields]
    if any(present) and not all(present):
        return "cleanup promotion graph binding is incomplete", promotion_id
    graph_file = _nonempty_string(record.get("graph_file"))
    if all(present):
        if type(raw.get("is_main_goal")) is not bool:
            return "cleanup promotion graph classification is malformed", promotion_id
        if raw.get("operation_path") != record.get("file"):
            return "cleanup promotion operation differs from its sealed source", promotion_id
        if raw.get("graph_node_name") != helper:
            return "cleanup promotion graph name differs from its sealed helper", promotion_id
        if raw.get("graph_node_file") != graph_file:
            return "cleanup promotion graph file differs from its sealed graph", promotion_id
        if not _valid_sha256(raw.get("graph_identity_sha256")):
            return "cleanup promotion graph seal is malformed", promotion_id
        try:
            expected_graph_identity = _graph_identity_sha256(raw)
        except (TypeError, ValueError):
            return "cleanup promotion graph binding is not JSON serializable", promotion_id
        if raw.get("graph_identity_sha256") != expected_graph_identity:
            return "cleanup promotion graph seal is forged", promotion_id
    return "", promotion_id


def _paired_optional_strings(record: Mapping[object, object], first: str, second: str) -> bool:
    """Return whether two optional provenance fields are absent or both exact strings."""
    present = (first in record, second in record)
    if present == (False, False):
        return True
    return present == (True, True) and bool(
        _nonempty_string(record.get(first)) and _nonempty_string(record.get(second))
    )


def _state_integrity_reason(record: Mapping[object, object], state: str) -> str:
    """Validate source payload and provenance required by one durable state."""
    if not _paired_optional_strings(record, "last_reconciliation_at", "last_reconciliation_reason"):
        return "cleanup transaction has incomplete reconciliation provenance"
    if not _paired_optional_strings(record, "manual_retry_authorized_at", "manual_retry_reason"):
        return "cleanup transaction has incomplete manual-retry authorization provenance"
    forbidden: tuple[str, ...]
    if state == "pending":
        forbidden = (
            "committed_at",
            "quarantined_at",
            "reason",
        )
    elif state == "committed":
        if not _nonempty_string(record.get("committed_at")):
            return "committed cleanup transaction lacks commit provenance"
        if "source_after" in record:
            return "committed cleanup transaction retains replay source payload"
        forbidden = (
            "quarantined_at",
            "reason",
        )
    elif state == "quarantined":
        if not _nonempty_string(record.get("quarantined_at")) or not _nonempty_string(
            record.get("reason")
        ):
            return "quarantined cleanup transaction lacks quarantine provenance"
        forbidden = ("committed_at",)
    elif state == "manual-retry-authorized":
        required = (
            "quarantined_at",
            "reason",
            "manual_retry_authorized_at",
            "manual_retry_reason",
        )
        if any(not _nonempty_string(record.get(field)) for field in required):
            return "manual cleanup retry lacks quarantine or authorization provenance"
        forbidden = ("committed_at",)
    else:
        return "cleanup transaction has unknown state"
    if any(field in record for field in forbidden):
        return "cleanup transaction has contradictory state provenance"
    if state != "committed":
        source_after = record.get("source_after")
        if not isinstance(source_after, str) or not source_after:
            return "live cleanup transaction lacks its exact replay source payload"
        if _sha256_text(source_after) != record.get("source_after_sha256"):
            return "cleanup transaction replay source differs from its sealed hash"
    return ""


def _classify_transaction(raw: object, index: int) -> FalseCleanupRecordAudit:
    """Classify one cleanup transaction as live, terminal, or ambiguous."""
    if not isinstance(raw, Mapping):
        return FalseCleanupRecordAudit(
            index, "ambiguous", reason="registry element is not a mapping"
        )
    record_id = _nonempty_string(raw.get("transaction_id"))
    promotion_id = _nonempty_string(raw.get("promotion_id"))
    state = _nonempty_string(raw.get("state"))
    unknown_fields = sorted(set(raw) - _TRANSACTION_FIELDS, key=str)
    if unknown_fields:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            f"record has unknown fields: {', '.join(map(str, unknown_fields))}",
        )
    version = raw.get("version")
    required_fields = set(_TRANSACTION_REQUIRED_FIELDS)
    if version in {2, 3} and not isinstance(version, bool):
        required_fields.update({"invalidated_dependents", "invalidated_dependents_sha256"})
    missing_fields = sorted(required_fields - set(raw))
    if missing_fields:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            f"record lacks required fields: {', '.join(missing_fields)}",
        )
    if state not in LIVE_TRANSACTION_STATES | TERMINAL_TRANSACTION_STATES:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "record has missing or unknown state",
        )
    if version not in {1, 2, 3} or isinstance(version, bool):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup transaction version is invalid",
        )
    if version == 1 and {
        "invalidated_dependents",
        "invalidated_dependents_sha256",
        "migration_from_transaction_id",
    } & set(raw):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "version-1 cleanup transaction carries version-2 dependent evidence",
        )
    if version == 2 and "migration_from_transaction_id" in raw:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "version-2 cleanup transaction carries a legacy-migration identity",
        )
    required_strings = tuple(
        field for field in _transaction_identity_fields(version) if field != "version"
    )
    required_strings += (
        "prepared_at",
        "parent_restored_declaration",
        "parent_restored_statement",
        "immutable_fingerprint",
        "transaction_id",
    )
    if any(not _nonempty_string(raw.get(field)) for field in required_strings):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup transaction lacks required identity evidence",
        )
    sha256_fields = list(_TRANSACTION_SHA256_FIELDS)
    if version in {2, 3}:
        sha256_fields.append("invalidated_dependents_sha256")
    if any(not _valid_sha256(raw.get(field)) for field in sha256_fields):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup transaction contains a malformed SHA-256 identity",
        )
    if not _lexical_absolute_file(raw.get("file")):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup transaction source identity is not canonical",
        )
    if raw.get("source_hash_kind") != "sha256-raw-utf8-bytes":
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup transaction has an unknown source hash kind",
        )
    if version in {2, 3}:
        raw_dependents = raw.get("invalidated_dependents")
        if not isinstance(raw_dependents, list):
            return FalseCleanupRecordAudit(
                index,
                "ambiguous",
                state,
                record_id,
                promotion_id,
                "cleanup transaction dependent invalidations are not a list",
            )
        if version == 3 and not raw_dependents:
            return FalseCleanupRecordAudit(
                index,
                "ambiguous",
                state,
                record_id,
                promotion_id,
                "legacy dependent migration has no sealed invalidations",
            )
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        graph_file = _nonempty_string(raw.get("graph_file"))
        for dependent in raw_dependents:
            expected_fields = (
                _V3_DEPENDENT_INVALIDATION_FIELDS
                if version == 3
                else _DEPENDENT_INVALIDATION_FIELDS
            )
            if not isinstance(dependent, Mapping) or set(dependent) != expected_fields:
                return FalseCleanupRecordAudit(
                    index,
                    "ambiguous",
                    state,
                    record_id,
                    promotion_id,
                    "cleanup transaction dependent invalidation is malformed",
                )
            node_id = _nonempty_string(dependent.get("node_id"))
            name = _nonempty_string(dependent.get("name"))
            source_kind = _nonempty_string(dependent.get("source_kind"))
            source_sha256 = dependent.get("source_sha256")
            source_identity_valid = (
                _valid_sha256(source_sha256)
                if version == 2 or source_kind == "source_obligation"
                else source_kind == "graph_artifact" and source_sha256 == ""
            )
            if (
                not node_id
                or not name
                or _nonempty_string(dependent.get("file")) != graph_file
                or node_id != plan_state.node_id_for(name, graph_file)
                or node_id in seen_ids
                or name in seen_names
                or not source_identity_valid
                or not _valid_sha256(dependent.get("declaration_sha256"))
            ):
                return FalseCleanupRecordAudit(
                    index,
                    "ambiguous",
                    state,
                    record_id,
                    promotion_id,
                    "cleanup transaction dependent graph identity is ambiguous",
                )
            seen_ids.add(node_id)
            seen_names.add(name)
        try:
            dependents_hash = _sha256_json(raw_dependents)
        except (TypeError, ValueError):
            dependents_hash = ""
        if dependents_hash != raw.get("invalidated_dependents_sha256"):
            return FalseCleanupRecordAudit(
                index,
                "ambiguous",
                state,
                record_id,
                promotion_id,
                "cleanup transaction dependent invalidations differ from their sealed hash",
            )
    if version == 3 and not _valid_sha256(raw.get("migration_from_transaction_id")):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup transaction legacy-migration identity is malformed",
        )
    ownership_basis = raw.get("ownership_basis")
    plain_ownership_basis = ownership_basis in {"decomposer-graph", "committed-provenance"}
    evidence_ownership_basis = (
        isinstance(ownership_basis, str)
        and re.fullmatch(
            r"(?:decomposer-graph|committed-provenance)-with-evidence-tombstone:[0-9a-f]{64}",
            ownership_basis,
        )
        is not None
    )
    if not plain_ownership_basis and not evidence_ownership_basis:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup transaction has an unknown ownership basis",
        )
    helper = _nonempty_string(raw.get("helper"))
    parent = _nonempty_string(raw.get("parent"))
    graph_file = _nonempty_string(raw.get("graph_file"))
    if raw.get("helper_node_id") != plan_state.node_id_for(helper, graph_file) or raw.get(
        "parent_node_id"
    ) != plan_state.node_id_for(parent, graph_file):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup transaction graph node identities are non-deterministic",
        )
    try:
        expected_fingerprint = _transaction_fingerprint(raw)
    except (TypeError, ValueError):
        expected_fingerprint = ""
    if (
        not expected_fingerprint
        or raw.get("immutable_fingerprint") != expected_fingerprint
        or raw.get("transaction_id") != expected_fingerprint
    ):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup transaction immutable fingerprint is forged",
        )
    promotion_reason, nested_promotion_id = _promotion_integrity_reason(
        raw.get("promotion"), record=raw
    )
    promotion_id = nested_promotion_id or promotion_id
    if promotion_reason:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            promotion_reason,
        )
    restored = raw.get("parent_restored_declaration")
    assert isinstance(restored, str)
    if _sha256_text(restored) != raw.get("parent_restored_declaration_sha256"):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup restored parent differs from its sealed hash",
        )
    restored_slice = decomposition_provenance.declaration_slice(restored, parent)
    if restored_slice is None:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup restored parent is not one exact declaration",
        )
    if restored_slice.signature_sha256 != raw.get("parent_signature_sha256"):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup restored parent signature differs from its sealed plan",
        )
    if _graph_statement(restored, parent) != raw.get("parent_restored_statement"):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup restored graph statement differs from its source payload",
        )
    state_reason = _state_integrity_reason(raw, state)
    if state_reason:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            state_reason,
        )
    disposition = "terminal" if state in TERMINAL_TRANSACTION_STATES else "live"
    return FalseCleanupRecordAudit(index, disposition, state, record_id, promotion_id)


def _audit_registry(
    raw_registry: object,
    *,
    classify: Callable[[object, int], FalseCleanupRecordAudit],
    registry_label: str,
    terminal_history_cap: int,
    duplicate_promotion_ids: bool,
) -> FalseCleanupRegistryAudit:
    """Retain every nonterminal record and cap authenticated terminal history."""
    if raw_registry is None:
        return FalseCleanupRegistryAudit(True, raw_registry)
    if not isinstance(raw_registry, list):
        reason = f"{registry_label} registry is not a list"
        return FalseCleanupRegistryAudit(
            False,
            raw_registry,
            records=(FalseCleanupRecordAudit(0, "ambiguous", reason=reason),),
            pending=1,
            ambiguous=1,
            reasons=(reason,),
        )
    initial = tuple(classify(raw, index) for index, raw in enumerate(raw_registry))
    id_counts = Counter(record.record_id for record in initial if record.record_id)
    promotion_counts = Counter(record.promotion_id for record in initial if record.promotion_id)
    audited: list[FalseCleanupRecordAudit] = []
    for record in initial:
        duplicate_reasons: list[str] = []
        if record.record_id and id_counts[record.record_id] > 1:
            duplicate_reasons.append("record identity is duplicated")
        if (
            duplicate_promotion_ids
            and record.promotion_id
            and promotion_counts[record.promotion_id] > 1
        ):
            duplicate_reasons.append("promotion identity is duplicated")
        if duplicate_reasons:
            reason = "; ".join([part for part in (record.reason, *duplicate_reasons) if part])
            record = replace(record, disposition="ambiguous", reason=reason)
        audited.append(record)
    cap = max(0, int(terminal_history_cap))
    terminal_indexes = [record.index for record in audited if record.disposition == "terminal"]
    retained_terminal = set(terminal_indexes[-cap:] if cap else ())
    retained = [
        raw
        for index, raw in enumerate(raw_registry)
        if audited[index].disposition != "terminal" or index in retained_terminal
    ]
    unresolved = [record for record in audited if record.disposition != "terminal"]
    ambiguous = [record for record in audited if record.disposition == "ambiguous"]
    reasons = tuple(
        (
            f"ambiguous {registry_label} {record.record_id or f'index-{record.index}'}: "
            f"{record.reason}"
            if record.disposition == "ambiguous"
            else f"live {registry_label} {record.record_id} ({record.state})"
        )
        for record in unresolved
    )
    return FalseCleanupRegistryAudit(
        not unresolved,
        retained,
        records=tuple(audited),
        pending=len(unresolved),
        ambiguous=len(ambiguous),
        terminal=len(audited) - len(unresolved),
        reasons=reasons,
    )


def audit_false_cleanup_transaction_registry(
    raw_registry: object,
    *,
    terminal_history_cap: int = DEFAULT_TERMINAL_HISTORY_CAP,
) -> FalseCleanupRegistryAudit:
    """Audit false-cleanup transactions without filtering unresolved evidence."""
    return _audit_registry(
        raw_registry,
        classify=_classify_transaction,
        registry_label="false-decomposition cleanup transaction",
        terminal_history_cap=terminal_history_cap,
        duplicate_promotion_ids=True,
    )


def _classify_quarantine(raw: object, index: int) -> FalseCleanupRecordAudit:
    """Classify one cleanup quarantine as unresolved, resolved, or ambiguous."""
    if not isinstance(raw, Mapping):
        return FalseCleanupRecordAudit(
            index, "ambiguous", reason="registry element is not a mapping"
        )
    record_id = _nonempty_string(raw.get("quarantine_id"))
    state = _nonempty_string(raw.get("state"))
    promotion = raw.get("promotion")
    promotion_id = (
        _nonempty_string(promotion.get("promotion_id")) if isinstance(promotion, Mapping) else ""
    )
    unknown_fields = sorted(set(raw) - _QUARANTINE_FIELDS, key=str)
    if unknown_fields:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            f"record has unknown fields: {', '.join(map(str, unknown_fields))}",
        )
    required = {
        "quarantine_id",
        "state",
        "quarantined_at",
        "reason",
        "promotion",
        "provenance_id",
    }
    missing = sorted(required - set(raw))
    if missing:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            f"record lacks required fields: {', '.join(missing)}",
        )
    if state not in LIVE_QUARANTINE_STATES | TERMINAL_QUARANTINE_STATES:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "record has missing or unknown state",
        )
    if not _valid_sha256(record_id):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup quarantine lacks a valid identity",
        )
    if not isinstance(promotion, Mapping):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup quarantine promotion is not a mapping",
        )
    raw_promotion_id = promotion.get("promotion_id", "")
    if not isinstance(raw_promotion_id, str):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup quarantine promotion identity is not a string",
        )
    provenance_id = raw.get("provenance_id")
    reason = raw.get("reason")
    if not isinstance(provenance_id, str) or not _nonempty_string(reason):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup quarantine lacks exact reason or provenance evidence",
        )
    if not _nonempty_string(raw.get("quarantined_at")):
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup quarantine lacks quarantine provenance",
        )
    expected_id = hashlib.sha256(
        f"{raw_promotion_id.strip()}\0{provenance_id}\0{reason}".encode()
    ).hexdigest()
    if record_id != expected_id:
        return FalseCleanupRecordAudit(
            index,
            "ambiguous",
            state,
            record_id,
            promotion_id,
            "cleanup quarantine identity does not match its evidence",
        )
    if state == "quarantined":
        if "resolved_at" in raw or "resolution_reason" in raw:
            return FalseCleanupRecordAudit(
                index,
                "ambiguous",
                state,
                record_id,
                promotion_id,
                "unresolved cleanup quarantine contains resolution provenance",
            )
        disposition = "live"
    else:
        if not _nonempty_string(raw.get("resolved_at")) or not _nonempty_string(
            raw.get("resolution_reason")
        ):
            return FalseCleanupRecordAudit(
                index,
                "ambiguous",
                state,
                record_id,
                promotion_id,
                "resolved cleanup quarantine lacks resolution provenance",
            )
        disposition = "terminal"
    return FalseCleanupRecordAudit(index, disposition, state, record_id, promotion_id)


def audit_false_cleanup_quarantine_registry(
    raw_registry: object,
    *,
    terminal_history_cap: int = DEFAULT_TERMINAL_HISTORY_CAP,
) -> FalseCleanupRegistryAudit:
    """Audit cleanup quarantine evidence and cap only authenticated resolutions."""
    return _audit_registry(
        raw_registry,
        classify=_classify_quarantine,
        registry_label="false-decomposition cleanup quarantine",
        terminal_history_cap=terminal_history_cap,
        duplicate_promotion_ids=False,
    )
