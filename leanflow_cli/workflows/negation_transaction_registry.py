"""Audit and retain durable negation-promotion authority registries."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from leanflow_cli.workflows import plan_state

TERMINAL_TRANSACTION_STATES = frozenset(
    {"committed", "quarantined", "consumed-by-false-decomposition-cleanup"}
)
LIVE_TRANSACTION_STATES = frozenset({"pending"})
DEFAULT_TERMINAL_HISTORY_CAP = 50
DEFAULT_QUARANTINE_HISTORY_CAP = 50

_TRANSACTION_FIELDS = frozenset(
    {
        "transaction_id",
        "state",
        "prepared_at",
        "promotion",
        "committed_at",
        "reason",
        "quarantined_at",
        "quarantine_pending_reason",
        "cleanup_transaction_id",
    }
)
_PROMOTION_FIELDS = frozenset(
    {
        "key",
        "theorem",
        "file",
        "canonical_file",
        "operation_path",
        "source_revision_sha256",
        "declaration_signature_sha256",
        "negation_name",
        "negation_prop",
        "proof_tactic",
        "proof_declaration",
        "axioms",
        "promotion_kind",
        "promoted_at",
        "node_id",
        "graph_node_name",
        "graph_node_file",
        "graph_identity_sha256",
        "classification_identity_sha256",
        "is_main_goal",
        "classification_basis",
        "scope_root_campaign_id",
        "scope_root_identity_sha256",
        "scope_root_theorem",
        "scope_root_file",
        "scope_root_node_id",
        "graph_before_statuses",
        "graph_after_statuses",
        "graph_changed_node_identities",
        "graph_before_revision",
        "graph_expected_revision",
        "rollback_plan_sha256",
        "promotion_id",
    }
)
_PROMOTION_KINDS = frozenset({"scratch_negation", "source_negation"})
_CLASSIFICATION_BASES = frozenset({"requested_scope_manifest", "decomposition_helper"})
_PROMOTION_REQUIRED_FIELDS = _PROMOTION_FIELDS - {"proof_declaration", "promotion_kind"}
_LEGACY_PROMOTION_FIELDS = frozenset(
    {
        "key",
        "theorem",
        "file",
        "canonical_file",
        "source_revision_sha256",
        "declaration_signature_sha256",
        "negation_name",
        "negation_prop",
        "proof_tactic",
        "proof_declaration",
        "axioms",
        "promotion_kind",
        "promoted_at",
        "node_id",
        "is_main_goal",
        "promotion_id",
    }
)
_LEGACY_PROMOTION_REQUIRED_FIELDS = _LEGACY_PROMOTION_FIELDS - {
    "proof_declaration",
    "promotion_kind",
}
_QUARANTINE_ENVELOPE_FIELDS = frozenset({"reason", "quarantined_at", "transaction_id"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TransactionRecordAudit:
    """Classify one raw registry element without changing its evidence."""

    index: int
    disposition: str
    transaction_id: str = ""
    promotion_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class NegationTransactionRegistryAudit:
    """Report terminal history and every live or ambiguous registry element."""

    ok: bool
    retained_registry: object
    records: tuple[TransactionRecordAudit, ...] = ()
    pending: int = 0
    ambiguous: int = 0
    terminal: int = 0
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromotionRecordAudit:
    """Classify one active or quarantined promotion without copying it."""

    index: int
    disposition: str
    promotion_id: str = ""
    transaction_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class NegationPromotionRegistryAudit:
    """Report lossless promotion retention and safe mutation targets."""

    ok: bool
    retained_registry: object
    records: tuple[PromotionRecordAudit, ...] = ()
    retained_indexes: tuple[int, ...] = ()
    active: int = 0
    reconcilable: int = 0
    ambiguous: int = 0
    terminal: int = 0
    reasons: tuple[str, ...] = ()

    @property
    def authenticated_indexes(self) -> tuple[int, ...]:
        """Return original indexes carrying authenticated authority/history."""
        return tuple(
            record.index for record in self.records if record.disposition in {"active", "terminal"}
        )

    @property
    def selectable_indexes(self) -> tuple[int, ...]:
        """Return exact indexes safe to target after the required validation."""
        return tuple(
            record.index
            for record in self.records
            if record.disposition in {"active", "reconcilable", "terminal"}
        )

    @property
    def unresolved(self) -> int:
        """Return records that block terminal truth until reconciled."""
        return self.reconcilable + self.ambiguous

    def matching_indexes(self, promotion_id: str) -> tuple[int, ...]:
        """Return every raw index claiming one promotion identity."""
        target = str(promotion_id or "").strip()
        if not target:
            return ()
        return tuple(record.index for record in self.records if record.promotion_id == target)

    def unique_authenticated_index(self, promotion_id: str) -> int | None:
        """Return one sealed target index, rejecting ambiguity and legacy state."""
        matches = [
            record.index
            for record in self.records
            if record.promotion_id == str(promotion_id or "").strip()
            and record.disposition in {"active", "terminal"}
        ]
        return matches[0] if len(matches) == 1 else None

    def unique_selectable_index(self, promotion_id: str) -> int | None:
        """Return one sealed or legacy-upgrade target without normalizing it."""
        matches = [
            record.index
            for record in self.records
            if record.promotion_id == str(promotion_id or "").strip()
            and record.disposition in {"active", "reconcilable", "terminal"}
        ]
        return matches[0] if len(matches) == 1 else None


def _sha256_json(payload: object) -> str:
    """Hash one JSON-compatible identity with canonical separators."""
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _nonempty_string(value: object) -> str:
    """Return an exact non-empty string, rejecting coercible impostors."""
    return value.strip() if isinstance(value, str) else ""


def _valid_sha256(value: object) -> bool:
    """Return whether a value is one lowercase SHA-256 digest."""
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _lexical_absolute_file(value: object) -> str:
    """Return a normalized absolute path without resolving filesystem state."""
    raw = _nonempty_string(value)
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        return ""
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        return ""
    return str(path)


def _normalized_statuses(raw: object) -> dict[str, str] | None:
    """Return an exact string status map or reject its ambiguous shape."""
    if not isinstance(raw, Mapping):
        return None
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in raw.items()):
        return None
    return {key: raw[key] for key in sorted(raw)}


def _graph_identity_payload(promotion: Mapping[str, Any]) -> dict[str, object]:
    """Build the graph binding protected by a promotion seal."""
    return {
        "theorem": promotion.get("theorem"),
        "operation_path": promotion.get("operation_path"),
        "node_id": promotion.get("node_id"),
        "graph_node_name": promotion.get("graph_node_name"),
        "graph_node_file": promotion.get("graph_node_file"),
        "is_main_goal": promotion.get("is_main_goal"),
    }


def _classification_identity_payload(promotion: Mapping[str, Any]) -> dict[str, object]:
    """Build the graph and requested-scope classification seal payload."""
    return {
        **_graph_identity_payload(promotion),
        "classification_basis": promotion.get("classification_basis"),
        "scope_root_campaign_id": promotion.get("scope_root_campaign_id"),
        "scope_root_identity_sha256": promotion.get("scope_root_identity_sha256"),
        "scope_root_theorem": promotion.get("scope_root_theorem"),
        "scope_root_file": promotion.get("scope_root_file"),
        "scope_root_node_id": promotion.get("scope_root_node_id"),
    }


def _rollback_plan_payload(promotion: Mapping[str, Any]) -> dict[str, object]:
    """Build every graph field consumed by interrupted-transaction rollback."""
    return {
        "node_id": promotion.get("node_id"),
        "graph_node_name": promotion.get("graph_node_name"),
        "graph_node_file": promotion.get("graph_node_file"),
        "graph_identity_sha256": promotion.get("graph_identity_sha256"),
        "classification_identity_sha256": promotion.get("classification_identity_sha256"),
        "is_main_goal": promotion.get("is_main_goal"),
        "classification_basis": promotion.get("classification_basis"),
        "scope_root_campaign_id": promotion.get("scope_root_campaign_id"),
        "scope_root_identity_sha256": promotion.get("scope_root_identity_sha256"),
        "scope_root_theorem": promotion.get("scope_root_theorem"),
        "scope_root_file": promotion.get("scope_root_file"),
        "scope_root_node_id": promotion.get("scope_root_node_id"),
        "graph_before_statuses": _normalized_statuses(promotion.get("graph_before_statuses")),
        "graph_after_statuses": _normalized_statuses(promotion.get("graph_after_statuses")),
        "graph_changed_node_identities": promotion.get("graph_changed_node_identities"),
        "graph_before_revision": promotion.get("graph_before_revision"),
        "graph_expected_revision": promotion.get("graph_expected_revision"),
    }


def _promotion_identity_payload(promotion: Mapping[str, Any]) -> dict[str, object] | None:
    """Build the canonical promotion identity without consulting live files."""
    operation_path = _lexical_absolute_file(promotion.get("operation_path"))
    statuses_before = _normalized_statuses(promotion.get("graph_before_statuses"))
    statuses_after = _normalized_statuses(promotion.get("graph_after_statuses"))
    raw_axioms = promotion.get("axioms")
    if (
        not operation_path
        or statuses_before is None
        or statuses_after is None
        or not isinstance(raw_axioms, list)
        or not all(isinstance(axiom, str) for axiom in raw_axioms)
    ):
        return None
    return {
        "file": operation_path,
        "theorem": str(promotion.get("theorem", "")).strip(),
        "source_revision_sha256": str(promotion.get("source_revision_sha256", "")).strip(),
        "declaration_signature_sha256": str(
            promotion.get("declaration_signature_sha256", "")
        ).strip(),
        "negation_prop": str(promotion.get("negation_prop", "")).strip(),
        "proof_tactic": str(promotion.get("proof_tactic", "")).strip(),
        "proof_declaration": str(promotion.get("proof_declaration", "")).strip(),
        "promotion_kind": str(
            promotion.get("promotion_kind", "scratch_negation") or "scratch_negation"
        ).strip(),
        "axioms": sorted({axiom.strip() for axiom in raw_axioms if axiom.strip()}),
        "axioms_recorded": True,
        "operation_path": operation_path,
        "node_id": promotion.get("node_id"),
        "graph_node_name": promotion.get("graph_node_name"),
        "graph_node_file": promotion.get("graph_node_file"),
        "graph_identity_sha256": promotion.get("graph_identity_sha256"),
        "classification_identity_sha256": promotion.get("classification_identity_sha256"),
        "is_main_goal": promotion.get("is_main_goal"),
        "is_main_goal_recorded": True,
        "classification_basis": promotion.get("classification_basis"),
        "scope_root_campaign_id": promotion.get("scope_root_campaign_id"),
        "scope_root_identity_sha256": promotion.get("scope_root_identity_sha256"),
        "scope_root_theorem": promotion.get("scope_root_theorem"),
        "scope_root_file": promotion.get("scope_root_file"),
        "scope_root_node_id": promotion.get("scope_root_node_id"),
        "graph_before_statuses": statuses_before,
        "graph_after_statuses": statuses_after,
        "graph_changed_node_identities": promotion.get("graph_changed_node_identities"),
        "graph_before_revision": promotion.get("graph_before_revision"),
        "graph_expected_revision": promotion.get("graph_expected_revision"),
        "rollback_plan_sha256": promotion.get("rollback_plan_sha256"),
    }


def _promotion_integrity_reason(raw: object, transaction_id: str) -> tuple[str, str]:
    """Authenticate the nested promotion consumed by replay and terminal history."""
    if not isinstance(raw, Mapping):
        return "transaction promotion is not a mapping", ""
    promotion = raw
    unknown_fields = sorted(set(promotion) - _PROMOTION_FIELDS, key=str)
    if unknown_fields:
        return (
            f"transaction promotion has unknown fields: {', '.join(map(str, unknown_fields))}",
            "",
        )
    missing_fields = sorted(_PROMOTION_REQUIRED_FIELDS - set(promotion))
    if missing_fields:
        return (
            f"transaction promotion lacks required fields: {', '.join(missing_fields)}",
            "",
        )
    promotion_id = _nonempty_string(promotion.get("promotion_id"))
    if not _valid_sha256(promotion_id) or promotion_id != transaction_id:
        return "transaction and promotion identities do not match", promotion_id
    required_strings = (
        "theorem",
        "operation_path",
        "node_id",
        "graph_node_name",
        "graph_node_file",
        "classification_basis",
        "source_revision_sha256",
        "declaration_signature_sha256",
        "negation_prop",
        "proof_tactic",
        "key",
        "file",
        "canonical_file",
        "negation_name",
        "promoted_at",
    )
    if any(not _nonempty_string(promotion.get(field)) for field in required_strings):
        return "transaction promotion lacks required identity fields", promotion_id
    operation_path = _lexical_absolute_file(promotion.get("operation_path"))
    if not operation_path:
        return "transaction promotion source identity is not canonical", promotion_id
    theorem = _nonempty_string(promotion.get("theorem"))
    if (
        promotion.get("file") != operation_path
        or promotion.get("canonical_file") != operation_path
        or promotion.get("key") != f"{operation_path}::{theorem}"
    ):
        return "transaction promotion source aliases are contradictory", promotion_id
    if promotion.get("graph_node_name") != theorem:
        return "transaction promotion theorem and graph identities differ", promotion_id
    if promotion.get("node_id") != plan_state.node_id_for(
        theorem, str(promotion.get("graph_node_file"))
    ):
        return "transaction promotion graph node identity is non-deterministic", promotion_id
    for field in (
        "source_revision_sha256",
        "declaration_signature_sha256",
        "graph_identity_sha256",
        "classification_identity_sha256",
        "rollback_plan_sha256",
    ):
        if not _valid_sha256(promotion.get(field)):
            return f"transaction promotion has invalid {field}", promotion_id
    if type(promotion.get("is_main_goal")) is not bool:
        return "transaction promotion has invalid main-goal classification", promotion_id
    kind = promotion.get("promotion_kind", "scratch_negation")
    if not isinstance(kind, str) or kind not in _PROMOTION_KINDS:
        return "transaction promotion has unknown promotion kind", promotion_id
    proof_declaration = promotion.get("proof_declaration", "")
    if not isinstance(proof_declaration, str) or (
        kind == "source_negation" and not proof_declaration.strip()
    ):
        return "source promotion lacks exact proof-declaration evidence", promotion_id
    axioms = promotion.get("axioms")
    if (
        not isinstance(axioms, list)
        or not all(
            isinstance(axiom, str) and axiom == axiom.strip() and bool(axiom) for axiom in axioms
        )
        or len(axioms) != len(set(axioms))
    ):
        return "transaction promotion has malformed axiom evidence", promotion_id
    basis = promotion.get("classification_basis")
    if not isinstance(basis, str) or basis not in _CLASSIFICATION_BASES:
        return "transaction promotion has unknown classification kind", promotion_id
    is_main_goal = promotion.get("is_main_goal") is True
    scope_fields = (
        "scope_root_campaign_id",
        "scope_root_identity_sha256",
        "scope_root_theorem",
        "scope_root_file",
        "scope_root_node_id",
    )
    if is_main_goal != (basis == "requested_scope_manifest"):
        return "transaction promotion classification is contradictory", promotion_id
    if is_main_goal and any(not _nonempty_string(promotion.get(field)) for field in scope_fields):
        return "main promotion lacks requested-root binding", promotion_id
    if is_main_goal and (
        not _valid_sha256(promotion.get("scope_root_identity_sha256"))
        or promotion.get("scope_root_theorem") != theorem
        or promotion.get("scope_root_file") != promotion.get("graph_node_file")
        or promotion.get("scope_root_node_id") != promotion.get("node_id")
    ):
        return "main promotion requested-root identity is contradictory", promotion_id
    if not is_main_goal and any(promotion.get(field) != "" for field in scope_fields):
        return "helper promotion forges requested-root binding", promotion_id
    before = _normalized_statuses(promotion.get("graph_before_statuses"))
    after = _normalized_statuses(promotion.get("graph_after_statuses"))
    identities = promotion.get("graph_changed_node_identities")
    empty_transition = before == {} and after == {}
    if (
        before is None
        or after is None
        or set(before) != set(after)
        or any(status not in plan_state.NODE_STATUSES for status in before.values())
        or (
            not empty_transition
            and (
                after.get(str(promotion.get("node_id"))) != "false"
                or any(
                    status != "conjectured"
                    for node_id, status in after.items()
                    if node_id != str(promotion.get("node_id"))
                )
            )
        )
    ):
        return "transaction promotion has malformed graph status plan", promotion_id
    if not isinstance(identities, Mapping) or set(identities) != set(before):
        return "transaction promotion has malformed graph identity plan", promotion_id
    if not all(
        isinstance(identity, Mapping)
        and _nonempty_string(identity.get("name"))
        and _nonempty_string(identity.get("file"))
        and set(identity) == {"name", "file"}
        for identity in identities.values()
    ):
        return "transaction promotion has ambiguous graph node identities", promotion_id
    if not empty_transition:
        target_identity = identities.get(str(promotion.get("node_id")))
        if not isinstance(target_identity, Mapping) or (
            target_identity.get("name") != promotion.get("graph_node_name")
            or target_identity.get("file") != promotion.get("graph_node_file")
        ):
            return "transaction promotion target identity is contradictory", promotion_id
    before_revision = promotion.get("graph_before_revision")
    expected_revision = promotion.get("graph_expected_revision")
    if (
        not isinstance(before_revision, int)
        or isinstance(before_revision, bool)
        or not isinstance(expected_revision, int)
        or isinstance(expected_revision, bool)
        or before_revision < 0
        or expected_revision < 0
        or expected_revision != before_revision + (1 if before else 0)
    ):
        return "transaction promotion has invalid graph revisions", promotion_id
    try:
        if promotion.get("graph_identity_sha256") != _sha256_json(
            _graph_identity_payload(promotion)
        ):
            return "transaction promotion graph seal is forged", promotion_id
        if promotion.get("classification_identity_sha256") != _sha256_json(
            _classification_identity_payload(promotion)
        ):
            return "transaction promotion classification seal is forged", promotion_id
        if promotion.get("rollback_plan_sha256") != _sha256_json(_rollback_plan_payload(promotion)):
            return "transaction promotion rollback seal is forged", promotion_id
        identity = _promotion_identity_payload(promotion)
        if identity is None or promotion_id != _sha256_json(identity):
            return "transaction promotion identity seal is forged", promotion_id
    except (TypeError, ValueError):
        return "transaction promotion seal payload is not serializable", promotion_id
    return "", promotion_id


def _standalone_promotion_integrity_reason(raw: object) -> tuple[str, str]:
    """Authenticate one fully sealed promotion outside a transaction envelope."""
    if not isinstance(raw, Mapping):
        return "promotion registry element is not a mapping", ""
    promotion_id = _nonempty_string(raw.get("promotion_id"))
    if not _valid_sha256(promotion_id):
        return "promotion lacks a valid promotion identity", promotion_id
    reason, validated_id = _promotion_integrity_reason(raw, promotion_id)
    return reason.replace("transaction promotion", "promotion"), validated_id


def _legacy_promotion_integrity_reason(raw: object) -> tuple[str, str]:
    """Recognize one exact pre-graph promotion for leased startup upgrade.

    Legacy evidence is selectable only for the existing source/graph
    revalidation path. It is never authenticated terminal authority itself.
    """
    if not isinstance(raw, Mapping):
        return "legacy promotion registry element is not a mapping", ""
    unknown_fields = sorted(set(raw) - _LEGACY_PROMOTION_FIELDS, key=str)
    if unknown_fields:
        return (
            f"legacy promotion has unknown fields: {', '.join(map(str, unknown_fields))}",
            _nonempty_string(raw.get("promotion_id")),
        )
    missing_fields = sorted(_LEGACY_PROMOTION_REQUIRED_FIELDS - set(raw))
    if missing_fields:
        return (
            f"legacy promotion lacks required fields: {', '.join(missing_fields)}",
            _nonempty_string(raw.get("promotion_id")),
        )
    promotion_id = _nonempty_string(raw.get("promotion_id"))
    if not _valid_sha256(promotion_id):
        return "legacy promotion lacks a valid promotion identity", promotion_id
    theorem = _nonempty_string(raw.get("theorem"))
    operation_path = _lexical_absolute_file(raw.get("canonical_file"))
    if not theorem or not operation_path:
        return "legacy promotion source identity is not canonical", promotion_id
    if raw.get("file") != operation_path or raw.get("key") != f"{operation_path}::{theorem}":
        return "legacy promotion source aliases are contradictory", promotion_id
    required_strings = (
        "negation_name",
        "negation_prop",
        "proof_tactic",
        "promoted_at",
        "node_id",
    )
    if any(not _nonempty_string(raw.get(field)) for field in required_strings):
        return "legacy promotion lacks required evidence fields", promotion_id
    for field in ("source_revision_sha256", "declaration_signature_sha256"):
        if not _valid_sha256(raw.get(field)):
            return f"legacy promotion has invalid {field}", promotion_id
    if type(raw.get("is_main_goal")) is not bool:
        return "legacy promotion has invalid main-goal classification", promotion_id
    kind = raw.get("promotion_kind", "scratch_negation")
    if not isinstance(kind, str) or kind not in _PROMOTION_KINDS:
        return "legacy promotion has unknown promotion kind", promotion_id
    proof_declaration = raw.get("proof_declaration", "")
    if not isinstance(proof_declaration, str) or (
        kind == "source_negation" and not proof_declaration.strip()
    ):
        return "legacy source promotion lacks exact proof-declaration evidence", promotion_id
    axioms = raw.get("axioms")
    if (
        not isinstance(axioms, list)
        or not all(
            isinstance(axiom, str) and axiom == axiom.strip() and bool(axiom) for axiom in axioms
        )
        or len(axioms) != len(set(axioms))
    ):
        return "legacy promotion has malformed axiom evidence", promotion_id
    return "", promotion_id


def _classify_active_promotion(raw: object, index: int) -> PromotionRecordAudit:
    """Classify one live promotion as sealed, upgradeable legacy, or ambiguous."""
    reason, promotion_id = _standalone_promotion_integrity_reason(raw)
    if not reason:
        return PromotionRecordAudit(index, "active", promotion_id=promotion_id)
    legacy_reason, legacy_id = _legacy_promotion_integrity_reason(raw)
    if not legacy_reason:
        return PromotionRecordAudit(
            index,
            "reconcilable",
            promotion_id=legacy_id,
            reason="legacy promotion requires leased source/graph upgrade",
        )
    return PromotionRecordAudit(
        index,
        "ambiguous",
        promotion_id=promotion_id or legacy_id,
        reason=reason,
    )


def _quarantine_integrity_reason(raw: object) -> tuple[str, str, str]:
    """Authenticate one flattened terminal promotion-quarantine envelope."""
    if not isinstance(raw, Mapping):
        return "promotion quarantine element is not a mapping", "", ""
    allowed_fields = _PROMOTION_FIELDS | _QUARANTINE_ENVELOPE_FIELDS
    unknown_fields = sorted(set(raw) - allowed_fields, key=str)
    if unknown_fields:
        return (
            f"promotion quarantine has unknown fields: {', '.join(map(str, unknown_fields))}",
            _nonempty_string(raw.get("promotion_id")),
            _nonempty_string(raw.get("transaction_id")),
        )
    missing_envelope = sorted(_QUARANTINE_ENVELOPE_FIELDS - set(raw))
    if missing_envelope:
        return (
            f"promotion quarantine lacks envelope fields: {', '.join(missing_envelope)}",
            _nonempty_string(raw.get("promotion_id")),
            _nonempty_string(raw.get("transaction_id")),
        )
    promotion = {key: raw[key] for key in raw if key in _PROMOTION_FIELDS}
    reason, promotion_id = _standalone_promotion_integrity_reason(promotion)
    transaction_id = _nonempty_string(raw.get("transaction_id"))
    if reason:
        return reason, promotion_id, transaction_id
    if not _valid_sha256(transaction_id) or transaction_id != promotion_id:
        return (
            "promotion quarantine transaction identity is contradictory",
            promotion_id,
            transaction_id,
        )
    if not _nonempty_string(raw.get("reason")) or not _nonempty_string(raw.get("quarantined_at")):
        return "promotion quarantine lacks terminal provenance", promotion_id, transaction_id
    return "", promotion_id, transaction_id


def _classify_quarantined_promotion(raw: object, index: int) -> PromotionRecordAudit:
    """Classify one terminal promotion quarantine without rewriting its payload."""
    reason, promotion_id, transaction_id = _quarantine_integrity_reason(raw)
    return PromotionRecordAudit(
        index,
        "ambiguous" if reason else "terminal",
        promotion_id=promotion_id,
        transaction_id=transaction_id,
        reason=reason,
    )


def _mark_duplicate_promotions(
    records: tuple[PromotionRecordAudit, ...],
    *,
    include_transactions: bool,
) -> tuple[PromotionRecordAudit, ...]:
    """Turn every duplicated promotion or envelope identity into ambiguity."""
    promotion_counts = Counter(record.promotion_id for record in records if record.promotion_id)
    transaction_counts = Counter(
        record.transaction_id for record in records if record.transaction_id
    )
    audited: list[PromotionRecordAudit] = []
    for record in records:
        duplicate_reason = ""
        if record.promotion_id and promotion_counts[record.promotion_id] > 1:
            duplicate_reason = "promotion identity is duplicated"
        if (
            include_transactions
            and record.transaction_id
            and transaction_counts[record.transaction_id] > 1
        ):
            duplicate_reason = "promotion quarantine transaction identity is duplicated"
        audited.append(
            replace(record, disposition="ambiguous", reason=duplicate_reason)
            if duplicate_reason
            else record
        )
    return tuple(audited)


def _promotion_registry_reasons(
    records: tuple[PromotionRecordAudit, ...],
    *,
    registry_label: str,
) -> tuple[str, ...]:
    """Build deterministic blockers for legacy and ambiguous promotion evidence."""
    reasons: list[str] = []
    for record in records:
        identity = record.promotion_id or f"index-{record.index}"
        if record.disposition == "reconcilable":
            reasons.append(f"reconcilable {registry_label} {identity}: {record.reason}")
        elif record.disposition == "ambiguous":
            reasons.append(
                f"ambiguous {registry_label} {identity}: {record.reason or 'unknown evidence'}"
            )
    return tuple(reasons)


def audit_negation_promotions(raw_registry: object) -> NegationPromotionRegistryAudit:
    """Audit every active promotion without capping or normalizing authority.

    Fully sealed unique records are authenticated active authority. Exact
    pre-graph legacy records remain selectable for leased startup upgrade, but
    they block terminal truth until that upgrade commits. Every other shape is
    ambiguous and retained exactly.
    """
    if raw_registry is None:
        return NegationPromotionRegistryAudit(True, raw_registry)
    if not isinstance(raw_registry, list):
        reason = "negation promotion registry is not a list"
        return NegationPromotionRegistryAudit(
            False,
            raw_registry,
            records=(PromotionRecordAudit(0, "ambiguous", reason=reason),),
            ambiguous=1,
            reasons=(reason,),
        )
    records = _mark_duplicate_promotions(
        tuple(_classify_active_promotion(raw, index) for index, raw in enumerate(raw_registry)),
        include_transactions=False,
    )
    active = sum(record.disposition == "active" for record in records)
    reconcilable = sum(record.disposition == "reconcilable" for record in records)
    ambiguous = sum(record.disposition == "ambiguous" for record in records)
    reasons = _promotion_registry_reasons(records, registry_label="negation promotion")
    return NegationPromotionRegistryAudit(
        not reconcilable and not ambiguous,
        raw_registry,
        records=records,
        retained_indexes=tuple(range(len(raw_registry))),
        active=active,
        reconcilable=reconcilable,
        ambiguous=ambiguous,
        reasons=reasons,
    )


def audit_negation_promotion_quarantine(
    raw_registry: object,
    *,
    terminal_history_cap: int = DEFAULT_QUARANTINE_HISTORY_CAP,
) -> NegationPromotionRegistryAudit:
    """Audit terminal quarantine history and cap only authenticated records."""
    if raw_registry is None:
        return NegationPromotionRegistryAudit(True, raw_registry)
    if not isinstance(raw_registry, list):
        reason = "negation promotion quarantine is not a list"
        return NegationPromotionRegistryAudit(
            False,
            raw_registry,
            records=(PromotionRecordAudit(0, "ambiguous", reason=reason),),
            ambiguous=1,
            reasons=(reason,),
        )
    records = _mark_duplicate_promotions(
        tuple(
            _classify_quarantined_promotion(raw, index) for index, raw in enumerate(raw_registry)
        ),
        include_transactions=True,
    )
    cap = max(0, int(terminal_history_cap))
    terminal_indexes = [record.index for record in records if record.disposition == "terminal"]
    retained_terminal = set(terminal_indexes[-cap:] if cap else ())
    retained_indexes = tuple(
        index
        for index in range(len(raw_registry))
        if records[index].disposition != "terminal" or index in retained_terminal
    )
    retained: object = (
        raw_registry
        if len(retained_indexes) == len(raw_registry)
        else [raw_registry[index] for index in retained_indexes]
    )
    terminal = sum(record.disposition == "terminal" for record in records)
    ambiguous = sum(record.disposition == "ambiguous" for record in records)
    reasons = _promotion_registry_reasons(records, registry_label="promotion quarantine")
    return NegationPromotionRegistryAudit(
        not ambiguous,
        retained,
        records=records,
        retained_indexes=retained_indexes,
        ambiguous=ambiguous,
        terminal=terminal,
        reasons=reasons,
    )


def _state_integrity_reason(record: Mapping[str, Any], state: str) -> str:
    """Validate fields proving that one recognized state actually committed."""
    prepared_at = _nonempty_string(record.get("prepared_at"))
    if state == "pending":
        if not prepared_at:
            return "pending transaction lacks preparation provenance"
        if any(
            field in record
            for field in ("committed_at", "quarantined_at", "cleanup_transaction_id")
        ):
            return "pending transaction contains terminal-only fields"
        return ""
    if state == "committed":
        if not prepared_at or not _nonempty_string(record.get("committed_at")):
            return "committed transaction lacks commit provenance"
        if "quarantined_at" in record or "cleanup_transaction_id" in record:
            return "committed transaction has contradictory terminal fields"
        return ""
    if state == "quarantined":
        if not _nonempty_string(record.get("quarantined_at")) or not _nonempty_string(
            record.get("reason")
        ):
            return "quarantined transaction lacks quarantine provenance"
        if "cleanup_transaction_id" in record:
            return "quarantined transaction has contradictory cleanup provenance"
        return ""
    if state == "consumed-by-false-decomposition-cleanup":
        if (
            not prepared_at
            or not _nonempty_string(record.get("committed_at"))
            or not _nonempty_string(record.get("cleanup_transaction_id"))
        ):
            return "consumed transaction lacks commit and cleanup provenance"
        if "quarantined_at" in record:
            return "consumed transaction has contradictory quarantine provenance"
        return ""
    return "transaction has unknown state"


def _classify_record(raw: object, index: int) -> TransactionRecordAudit:
    """Classify one raw item as live, authenticated terminal, or ambiguous."""
    if not isinstance(raw, Mapping):
        return TransactionRecordAudit(
            index, "ambiguous", reason="registry element is not a mapping"
        )
    unknown_fields = sorted(set(raw) - _TRANSACTION_FIELDS, key=str)
    if unknown_fields:
        return TransactionRecordAudit(
            index,
            "ambiguous",
            reason=f"record has unknown fields: {', '.join(map(str, unknown_fields))}",
        )
    transaction_id = _nonempty_string(raw.get("transaction_id"))
    if not _valid_sha256(transaction_id):
        return TransactionRecordAudit(
            index,
            "ambiguous",
            transaction_id=transaction_id,
            reason="record lacks a valid transaction identity",
        )
    state = _nonempty_string(raw.get("state"))
    if state not in LIVE_TRANSACTION_STATES | TERMINAL_TRANSACTION_STATES:
        return TransactionRecordAudit(
            index,
            "ambiguous",
            transaction_id=transaction_id,
            reason="record has missing or unknown state",
        )
    promotion_reason, promotion_id = _promotion_integrity_reason(
        raw.get("promotion"), transaction_id
    )
    if promotion_reason:
        return TransactionRecordAudit(
            index,
            "ambiguous",
            transaction_id=transaction_id,
            promotion_id=promotion_id,
            reason=promotion_reason,
        )
    state_reason = _state_integrity_reason(raw, state)
    if state_reason:
        return TransactionRecordAudit(
            index,
            "ambiguous",
            transaction_id=transaction_id,
            promotion_id=promotion_id,
            reason=state_reason,
        )
    disposition = "live" if state in LIVE_TRANSACTION_STATES else "terminal"
    return TransactionRecordAudit(
        index,
        disposition,
        transaction_id=transaction_id,
        promotion_id=promotion_id,
    )


def audit_negation_transaction_registry(
    raw_registry: object,
    *,
    terminal_history_cap: int = DEFAULT_TERMINAL_HISTORY_CAP,
) -> NegationTransactionRegistryAudit:
    """Classify every raw item and retain all live or ambiguous evidence.

    ``None`` is the compatible absent-registry representation. Any other
    non-list shape is itself unresolved evidence and is returned unchanged.
    Only authenticated terminal records participate in bounded history.
    """
    if raw_registry is None:
        return NegationTransactionRegistryAudit(True, raw_registry)
    if not isinstance(raw_registry, list):
        reason = "negation-promotion transaction registry is not a list"
        return NegationTransactionRegistryAudit(
            False,
            raw_registry,
            records=(TransactionRecordAudit(0, "ambiguous", reason=reason),),
            pending=1,
            ambiguous=1,
            reasons=(reason,),
        )
    cap = max(0, int(terminal_history_cap))
    records = tuple(_classify_record(raw, index) for index, raw in enumerate(raw_registry))
    transaction_counts = Counter(
        record.transaction_id for record in records if record.transaction_id
    )
    promotion_counts = Counter(record.promotion_id for record in records if record.promotion_id)
    audited: list[TransactionRecordAudit] = []
    for record in records:
        duplicate_reason = ""
        if record.transaction_id and transaction_counts[record.transaction_id] > 1:
            duplicate_reason = "transaction identity is duplicated"
        if record.promotion_id and promotion_counts[record.promotion_id] > 1:
            duplicate_reason = "promotion identity is duplicated"
        audited.append(
            replace(record, disposition="ambiguous", reason=duplicate_reason)
            if duplicate_reason
            else record
        )
    terminal_indexes = [record.index for record in audited if record.disposition == "terminal"]
    retained_terminal = set(terminal_indexes[-cap:] if cap else ())
    retained = [
        raw
        for index, raw in enumerate(raw_registry)
        if audited[index].disposition != "terminal" or index in retained_terminal
    ]
    pending_records = [record for record in audited if record.disposition != "terminal"]
    ambiguous_records = [record for record in audited if record.disposition == "ambiguous"]
    reasons = tuple(
        (
            f"ambiguous negation-promotion transaction {record.transaction_id or f'index-{record.index}'}: "
            f"{record.reason}"
            if record.disposition == "ambiguous"
            else f"live negation-promotion transaction {record.transaction_id}"
        )
        for record in pending_records
    )
    return NegationTransactionRegistryAudit(
        not pending_records,
        retained,
        records=tuple(audited),
        pending=len(pending_records),
        ambiguous=len(ambiguous_records),
        terminal=len(audited) - len(pending_records),
        reasons=reasons,
    )
