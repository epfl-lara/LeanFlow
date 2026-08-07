"""Promote a fresh kernel-checked negation into authoritative graph falsity."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.lean import negation_probe
from leanflow_cli.lean.lean_declarations import declaration_region
from leanflow_cli.lean.lean_ephemeral import lean_ephemeral_source_check
from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _strip_lean_comments_and_strings,
    _trim_declaration_region_end,
    declaration_statement_text,
)
from leanflow_cli.workflows import (
    campaign_root_registry,
    decomposition_provenance,
    false_decomposition_cleanup,
    negation_revalidation_policy,
    negation_transaction_registry,
    plan_state,
    source_negation_batch,
    source_negation_harness,
)
from leanflow_cli.workflows.workflow_json_io import update_json_file
from leanflow_cli.workflows.workflow_state import append_workflow_activity

SOURCE_CANDIDATE_DECLARATION_MISSING = "source_candidate_declaration_missing"
SOURCE_CANDIDATE_KERNEL_INCOMPATIBLE = "source_candidate_kernel_incompatible"
SOURCE_CANDIDATE_AXIOMS_UNACCEPTABLE = "source_candidate_axioms_unacceptable"
SOURCE_CANDIDATE_INCOMPATIBLE_FAILURE_KINDS = frozenset(
    {
        SOURCE_CANDIDATE_DECLARATION_MISSING,
        SOURCE_CANDIDATE_KERNEL_INCOMPATIBLE,
        SOURCE_CANDIDATE_AXIOMS_UNACCEPTABLE,
    }
)


@dataclass(frozen=True)
class PromotionResult:
    """Describe an authoritative negation-promotion attempt."""

    ok: bool
    reason: str
    node_id: str = ""
    is_main_goal: bool = False
    evidence: dict[str, Any] | None = None
    already_promoted: bool = False
    failure_kind: str = ""
    retryable: bool = False
    scan_may_continue: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "node_id": self.node_id,
            "is_main_goal": self.is_main_goal,
            "evidence": dict(self.evidence or {}),
            "already_promoted": self.already_promoted,
            "failure_kind": self.failure_kind,
            "retryable": self.retryable,
            "scan_may_continue": self.scan_may_continue,
        }


def source_candidate_definitively_incompatible(result: PromotionResult) -> bool:
    """Return whether a fresh exact-source gate disproved this candidate only.

    Source availability, lease changes, target reconstruction, parser state,
    provider/runtime failures, axiom-audit uncertainty, and graph transaction
    failures are intentionally absent. Those failures must be retried without
    advancing the candidate scan.
    """
    return bool(
        not result.ok
        and not result.retryable
        and result.failure_kind in SOURCE_CANDIDATE_INCOMPATIBLE_FAILURE_KINDS
    )


@dataclass(frozen=True)
class PromotionReconciliation:
    """Report startup recovery and authoritative main-goal truth."""

    terminal_disproof: bool = False
    promotion: dict[str, Any] | None = None
    committed: int = 0
    quarantined: int = 0
    decompositions_cleaned: int = 0
    cleanup_pending: int = 0
    cleanup_quarantined: int = 0
    cleanup_reasons: tuple[str, ...] = ()
    promotion_pending: int = 0
    promotion_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CampaignRootRegistration:
    """Report immutable requested-root registry creation or validation."""

    ok: bool
    reason: str
    roots: tuple[dict[str, Any], ...] = ()


_TRANSACTION_CAP = 50
_QUARANTINE_CAP = 50
_FAILURE_DETAIL_CAP = 1200
_TERMINAL_TRANSACTION_STATES = frozenset(
    {"committed", "quarantined", "consumed-by-false-decomposition-cleanup"}
)
_CAMPAIGN_ROOTS_FIELD = campaign_root_registry.CAMPAIGN_ROOTS_FIELD
_CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD = (
    campaign_root_registry.CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD
)


class _SourceLeaseChanged(RuntimeError):
    """Report a source change detected while an authoritative lease is held."""


class _GraphTransactionChanged(RuntimeError):
    """Report graph drift between promotion mutation and summary finalization."""


def _audit_promotion_transactions(
    raw_registry: object,
) -> negation_transaction_registry.NegationTransactionRegistryAudit:
    """Audit every durable transaction element before reading or rewriting it."""
    return negation_transaction_registry.audit_negation_transaction_registry(
        raw_registry,
        terminal_history_cap=_TRANSACTION_CAP,
    )


def _retained_promotion_transactions(records: object) -> object:
    """Retain every live or ambiguous record and bounded authenticated history."""
    return _audit_promotion_transactions(records).retained_registry


def _audit_active_promotions(
    raw_registry: object,
) -> negation_transaction_registry.NegationPromotionRegistryAudit:
    """Audit live promotion authority without normalizing or capping it."""
    return negation_transaction_registry.audit_negation_promotions(raw_registry)


def _audit_promotion_quarantines(
    raw_registry: object,
) -> negation_transaction_registry.NegationPromotionRegistryAudit:
    """Audit promotion quarantine history and cap authenticated terminals only."""
    return negation_transaction_registry.audit_negation_promotion_quarantine(
        raw_registry,
        terminal_history_cap=_QUARANTINE_CAP,
    )


def _active_promotion_records_for_mutation(
    summary: Mapping[str, Any],
    *,
    promotion_id: str,
    allow_reconcilable: bool = False,
) -> tuple[list[object], int | None]:
    """Return a lossless active ledger plus one authenticated mutation target."""
    raw_registry = summary.get("negation_promotions")
    if raw_registry is None:
        return [], None
    audit = _audit_active_promotions(raw_registry)
    if not isinstance(raw_registry, list):
        raise _GraphTransactionChanged("negation-promotion registry is not a list")
    target = str(promotion_id or "").strip()
    matches = audit.matching_indexes(target) if target else ()
    if len(matches) > 1:
        raise _GraphTransactionChanged("negation-promotion target is duplicated")
    index = (
        audit.unique_selectable_index(target)
        if allow_reconcilable
        else audit.unique_authenticated_index(target)
    )
    if matches and index is None:
        raise _GraphTransactionChanged("negation-promotion target is unauthenticated")
    return list(raw_registry), index


def _promotion_quarantine_records_for_mutation(
    summary: Mapping[str, Any],
    *,
    promotion_id: str,
) -> tuple[list[object], int | None]:
    """Return lossless quarantine history plus one authenticated target."""
    raw_registry = summary.get("negation_promotion_quarantine")
    if raw_registry is None:
        return [], None
    audit = _audit_promotion_quarantines(raw_registry)
    if not isinstance(raw_registry, list):
        raise _GraphTransactionChanged("negation-promotion quarantine is not a list")
    target = str(promotion_id or "").strip()
    matches = audit.matching_indexes(target) if target else ()
    if len(matches) > 1:
        raise _GraphTransactionChanged("negation-promotion quarantine target is duplicated")
    index = audit.unique_authenticated_index(target)
    if matches and index is None:
        raise _GraphTransactionChanged("negation-promotion quarantine target is unauthenticated")
    return list(raw_registry), index


def _promotion_transaction_records_for_mutation(
    summary: Mapping[str, Any],
    *,
    transaction_id: str,
) -> list[object]:
    """Return a lossless registry snapshot after rejecting target ambiguity."""
    raw_registry = summary.get("negation_promotion_transactions")
    if raw_registry is None:
        return []
    audit = _audit_promotion_transactions(raw_registry)
    if not isinstance(raw_registry, list):
        raise _GraphTransactionChanged(
            "negation-promotion transaction registry has an ambiguous container"
        )
    target_indexes: list[int] = []
    for index, raw in enumerate(raw_registry):
        if not isinstance(raw, Mapping):
            continue
        nested = raw.get("promotion")
        nested_id = (
            str(nested.get("promotion_id", "") or "").strip() if isinstance(nested, Mapping) else ""
        )
        raw_id = str(raw.get("transaction_id", "") or "").strip()
        if transaction_id in {raw_id, nested_id}:
            target_indexes.append(index)
    target_records = [audit.records[index] for index in target_indexes]
    if len(target_records) > 1 or any(
        record.disposition == "ambiguous" for record in target_records
    ):
        detail = next(
            (record.reason for record in target_records if record.reason),
            "duplicate target identity",
        )
        raise _GraphTransactionChanged(
            "negation-promotion transaction target is ambiguous or duplicated: " + detail
        )
    # Preserve unrelated malformed evidence byte-for-byte in the JSON value;
    # terminal authority remains blocked until it is explicitly reconciled.
    return list(raw_registry)


def _quarantine_pending_records_for_mutation(
    summary: Mapping[str, Any],
    *,
    target_id: str,
    project_root: Path,
) -> tuple[list[object], int | None]:
    """Return the lossless unresolved-quarantine ledger and one safe target."""
    raw_registry = summary.get("negation_promotion_quarantine_pending")
    if raw_registry is None:
        return [], None
    if not isinstance(raw_registry, list):
        raise _GraphTransactionChanged(
            "negation-promotion quarantine-pending registry is not a list"
        )
    matching_indexes: list[int] = []
    for index, raw in enumerate(raw_registry):
        if not isinstance(raw, Mapping):
            continue
        if target_id in {
            str(raw.get("promotion_id", "") or "").strip(),
            str(raw.get("transaction_id", "") or "").strip(),
        }:
            matching_indexes.append(index)
    if len(matching_indexes) > 1:
        raise _GraphTransactionChanged("negation-promotion quarantine target is duplicated")
    match_index = matching_indexes[0] if matching_indexes else None
    if match_index is not None:
        matched = raw_registry[match_index]
        assert isinstance(matched, Mapping)
        promotion_id = str(matched.get("promotion_id", "") or "").strip()
        if (
            str(matched.get("state", "") or "") != "pending-graph-reconciliation"
            or not str(matched.get("reason", "") or "").strip()
            or not str(matched.get("updated_at", "") or "").strip()
            or str(matched.get("transaction_id", "") or "").strip() != promotion_id
            or not _promotion_identity_seals_are_authenticated(matched, project_root)
        ):
            raise _GraphTransactionChanged(
                "negation-promotion quarantine target is unauthenticated"
            )
    return list(raw_registry), match_index


def _promotion_transaction_hook(stage: str) -> None:
    """Expose deterministic crash boundaries for transaction tests."""


def _promotion_pending_state() -> tuple[int, tuple[str, ...]]:
    """Return unresolved cross-artifact promotion quarantine state."""

    summary = plan_state.load_summary()
    raw_quarantine = summary.get("negation_promotion_quarantine_pending")
    if raw_quarantine is None:
        quarantine_count = 0
        quarantine_reasons: list[str] = []
    elif isinstance(raw_quarantine, list):
        quarantine_count = len(raw_quarantine)
        quarantine_reasons = [
            (
                str(
                    item.get("reason", "") or "promotion quarantine requires reconciliation"
                ).strip()
                if isinstance(item, Mapping)
                else f"ambiguous promotion quarantine record index-{index}"
            )
            for index, item in enumerate(raw_quarantine)
        ]
    else:
        quarantine_count = 1
        quarantine_reasons = ["promotion quarantine registry is not a list"]
    transaction_audit = _audit_promotion_transactions(
        summary.get("negation_promotion_transactions")
    )
    promotion_audit = _audit_active_promotions(summary.get("negation_promotions"))
    quarantine_audit = _audit_promotion_quarantines(summary.get("negation_promotion_quarantine"))
    reasons = quarantine_reasons
    reasons.extend(transaction_audit.reasons)
    reasons.extend(promotion_audit.reasons)
    reasons.extend(quarantine_audit.reasons)
    return (
        quarantine_count
        + transaction_audit.pending
        + promotion_audit.unresolved
        + quarantine_audit.unresolved,
        tuple(dict.fromkeys(reasons)),
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scratch_messages(payload: Mapping[str, Any]) -> str:
    """Flatten structured Lean scratch messages for axiom parsing."""
    parts = [str(payload.get("error", "") or ""), str(payload.get("output", "") or "")]
    for message in payload.get("messages") or []:
        if isinstance(message, Mapping):
            parts.append(str(message.get("message", "") or ""))
        else:
            parts.append(str(message or ""))
    return "\n".join(part for part in parts if part)


def _authoritative_failure_detail(payload: Mapping[str, Any]) -> str:
    """Return the first bounded Lean error suitable for durable activity."""
    for message in payload.get("messages") or []:
        if not isinstance(message, Mapping):
            continue
        if str(message.get("severity", "") or "").strip().lower() != "error":
            continue
        detail = " ".join(str(message.get("message", "") or "").split())
        if detail:
            return detail[:_FAILURE_DETAIL_CAP]
    for field in ("output", "error"):
        for raw_line in str(payload.get(field, "") or "").splitlines():
            detail = " ".join(raw_line.split())
            if re.search(r"\berror(?:\([^)]*\))?:", detail):
                return detail[:_FAILURE_DETAIL_CAP]
    fallback = " ".join(str(payload.get("error", "") or "").split())
    return fallback[:_FAILURE_DETAIL_CAP]


def _structured_error_line(message: Mapping[str, Any]) -> int:
    """Return a one-based Lean diagnostic line from common structured shapes."""
    candidates: list[object] = [
        message.get("line"),
        message.get("line_number"),
        message.get("startLine"),
        message.get("start_line"),
    ]
    for field in ("location", "position", "start"):
        nested = message.get(field)
        if isinstance(nested, Mapping):
            candidates.extend(
                (
                    nested.get("line"),
                    nested.get("line_number"),
                    nested.get("startLine"),
                    nested.get("start_line"),
                )
            )
    for raw in candidates:
        try:
            line = int(str(raw or 0))
        except (TypeError, ValueError):
            continue
        if line > 0:
            return line
    return 0


def _lean_error_locations(payload: Mapping[str, Any]) -> tuple[tuple[int, ...], bool]:
    """Return known Lean error lines and whether any error lacks a location."""
    located: list[int] = []
    unlocated = False
    for raw_message in payload.get("messages") or []:
        if not isinstance(raw_message, Mapping):
            continue
        if str(raw_message.get("severity", "") or "").strip().lower() != "error":
            continue
        line = _structured_error_line(raw_message)
        if line:
            located.append(line)
        else:
            unlocated = True
    location_re = re.compile(
        r"\.lean:(\d+):\d+:\s*error(?:\[[^\]]+\])?:",
        flags=re.IGNORECASE,
    )
    error_re = re.compile(r"\berror(?:\[[^\]]+\])?:", flags=re.IGNORECASE)
    for field in ("output", "error"):
        for raw_line in str(payload.get(field, "") or "").splitlines():
            if not error_re.search(raw_line):
                continue
            match = location_re.search(raw_line)
            if match is None:
                unlocated = True
                continue
            located.append(int(match.group(1)))
    return tuple(located), unlocated


def _failure_confined_to_harness(
    payload: Mapping[str, Any],
    *,
    start_line: int,
    end_line: int,
) -> bool:
    """Return whether every located Lean error belongs to the inserted harness.

    Whole-source elaboration may expose an unrelated source error. Cache a
    candidate rejection only when diagnostics are complete, all errors have a
    location, and every location falls inside the freshly inserted alias.
    """
    if payload.get("retryable") or payload.get("output_truncated"):
        return False
    located, unlocated = _lean_error_locations(payload)
    return (
        bool(located) and not unlocated and all(start_line <= line <= end_line for line in located)
    )


def _failure_allows_candidate_scan_continuation(
    payload: Mapping[str, Any],
    *,
    start_line: int,
    end_line: int,
) -> bool:
    """Return whether bounded later-candidate checks are safe and useful.

    An exact-harness timeout is nonauthoritative but may be candidate-specific,
    so another candidate can be tried without advancing this candidate's
    cursor. For elaboration uncertainty, require every known error location to
    belong to the inserted harness. Project/source/admission failures and
    errors known to occur outside the harness abort the scan.
    """
    failure_kind = str(payload.get("failure_kind", "") or "").strip()
    if payload.get("output_truncated"):
        return False
    located, unlocated = _lean_error_locations(payload)
    if unlocated:
        return False
    if failure_kind == "infrastructure_timeout":
        return not located or all(start_line <= line <= end_line for line in located)
    if failure_kind != "lean_elaboration":
        return False
    return bool(located) and all(start_line <= line <= end_line for line in located)


def _printed_axioms(text: str, declaration_name: str) -> list[str] | None:
    """Return axioms printed for a possibly namespace-qualified declaration."""
    suffix = re.escape(str(declaration_name or "").strip())
    if not suffix:
        return None
    name_pattern = rf"(?:[^']+\.)?{suffix}"
    if re.search(rf"'{name_pattern}' does not depend on any axioms", text):
        return []
    match = re.search(rf"'{name_pattern}' depends on axioms: \[([^\]]*)\]", text)
    if not match:
        return None
    return [token.strip() for token in match.group(1).split(",") if token.strip()]


def _validated_source_candidate_statement(candidate_text: str) -> str | PromotionResult:
    """Parse one complete candidate statement before exact Lean verification.

    Statement syntax is not proof authority: ``P → False`` and reducible
    aliases may elaborate as the exact target negation. The exact harness owns
    that decision, including whether a proof depends on ``sorryAx``. This
    preflight therefore only requires a parseable declaration split; Lean
    syntax quotations and macros make raw placeholder-token scans unsound.
    """
    statement = declaration_statement_text(str(candidate_text or ""))
    if not statement:
        return PromotionResult(
            False,
            "source candidate statement could not be reconstructed",
            failure_kind="source_candidate_statement_uncertain",
            retryable=True,
            scan_may_continue=True,
        )
    return statement


def _last_source_declaration_insertion_index(
    source: str,
    lines: list[str],
    *,
    declaration_line: int,
) -> int:
    """Return an insertion index before trailing namespace endings."""
    sanitized_lines = _strip_lean_comments_and_strings(source).splitlines()
    insertion_index = len(lines)
    cursor = len(sanitized_lines) - 1
    declaration_index = max(0, declaration_line - 1)
    while cursor >= declaration_index:
        line = sanitized_lines[cursor].strip()
        if not line:
            cursor -= 1
            continue
        if re.fullmatch(r"end(?:\s+[A-Za-z0-9_'.\u00ab\u00bb]+)?", line):
            insertion_index = cursor
            cursor -= 1
            continue
        break
    return insertion_index


def _exact_source_declaration_region(
    source_path: Path,
    source: str,
    candidate: str,
) -> dict[str, Any] | None:
    """Return one declaration without the next declaration's docs or attributes."""
    broad_region = declaration_region(source_path, candidate)
    if not broad_region:
        return None
    start_line = int(broad_region.get("line", 0) or 0)
    entries = _declaration_line_index_from_text(source)
    entry_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if int(entry.get("line", 0) or 0) == start_line
        ),
        -1,
    )
    if entry_index < 0:
        return None
    lines = source.splitlines()
    entry = entries[entry_index]
    if entry_index + 1 < len(entries):
        insert_at = _trim_declaration_region_end(
            lines,
            start=start_line,
            next_start=int(entries[entry_index + 1].get("line", 0) or 0),
        )
    else:
        insert_at = _last_source_declaration_insertion_index(
            source,
            lines,
            declaration_line=start_line,
        )
    if insert_at < start_line or insert_at > len(lines):
        return None
    return {
        "kind": str(entry.get("kind", "") or ""),
        "name": str(entry.get("name", "") or ""),
        "line": start_line,
        "end_line": insert_at,
        "text": "\n".join(lines[start_line - 1 : insert_at]).strip(),
    }


def _canonical_file_identity(file_label: str, project_root: Path) -> str:
    """Return one real-path identity for relative paths and filesystem aliases."""
    path = Path(str(file_label or "").strip()).expanduser()
    if not path.is_absolute():
        path = project_root / path
    try:
        return str(path.resolve())
    except (OSError, RuntimeError):
        return str(path.absolute())


def _lexical_absolute_file(file_label: str) -> str:
    """Return an exact normalized absolute path without resolving its target."""
    raw = str(file_label or "").strip()
    path = Path(raw).expanduser()
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        return ""
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        return ""
    return str(path)


def _promotion_file_identity(promotion: Mapping[str, Any], project_root: Path) -> str:
    """Recover the canonical file identity from current and legacy records."""
    operation_path = str(promotion.get("operation_path", "") or "").strip()
    if operation_path:
        # New durable identities are lexical and must never be resolved again.
        return _lexical_absolute_file(operation_path)
    theorem = str(promotion.get("theorem", "") or "").strip()
    key = str(promotion.get("key", "") or "").strip()
    suffix = f"::{theorem}" if theorem else ""
    keyed_file = key[: -len(suffix)] if suffix and key.endswith(suffix) else ""
    candidate = str(
        promotion.get("canonical_file", "") or keyed_file or promotion.get("file", "") or ""
    ).strip()
    return _canonical_file_identity(candidate, project_root) if candidate else ""


def _normalized_statuses(raw: Any) -> dict[str, str]:
    """Return a deterministic status mapping or an empty invalid sentinel."""
    if not isinstance(raw, Mapping):
        return {}
    return {str(key): str(value) for key, value in sorted(raw.items())}


def _promotion_identity(promotion: Mapping[str, Any], project_root: Path) -> dict[str, Any]:
    """Build the stable mathematical/evidence identity of one promotion."""
    raw_axioms = promotion.get("axioms") or []
    if isinstance(raw_axioms, (str, bytes)):
        raw_axioms = [raw_axioms]
    return {
        "file": _promotion_file_identity(promotion, project_root),
        "theorem": str(promotion.get("theorem", "") or "").strip(),
        "source_revision_sha256": str(promotion.get("source_revision_sha256", "") or "").strip(),
        "declaration_signature_sha256": str(
            promotion.get("declaration_signature_sha256", "") or ""
        ).strip(),
        "negation_prop": str(promotion.get("negation_prop", "") or "").strip(),
        "proof_tactic": str(promotion.get("proof_tactic", "") or "").strip(),
        "proof_declaration": str(promotion.get("proof_declaration", "") or "").strip(),
        "promotion_kind": str(
            promotion.get("promotion_kind", "scratch_negation") or "scratch_negation"
        ).strip(),
        "axioms": sorted({str(axiom).strip() for axiom in raw_axioms if str(axiom).strip()}),
        "axioms_recorded": "axioms" in promotion,
        "operation_path": str(promotion.get("operation_path", "") or "").strip(),
        "node_id": str(promotion.get("node_id", "") or "").strip(),
        "graph_node_name": str(promotion.get("graph_node_name", "") or "").strip(),
        "graph_node_file": str(promotion.get("graph_node_file", "") or "").strip(),
        "graph_identity_sha256": str(promotion.get("graph_identity_sha256", "") or "").strip(),
        "classification_identity_sha256": str(
            promotion.get("classification_identity_sha256", "") or ""
        ).strip(),
        "is_main_goal": bool(promotion.get("is_main_goal")),
        "is_main_goal_recorded": "is_main_goal" in promotion,
        "classification_basis": str(promotion.get("classification_basis", "") or "").strip(),
        "scope_root_campaign_id": str(promotion.get("scope_root_campaign_id", "") or "").strip(),
        "scope_root_identity_sha256": str(
            promotion.get("scope_root_identity_sha256", "") or ""
        ).strip(),
        "scope_root_theorem": str(promotion.get("scope_root_theorem", "") or "").strip(),
        "scope_root_file": str(promotion.get("scope_root_file", "") or "").strip(),
        "scope_root_node_id": str(promotion.get("scope_root_node_id", "") or "").strip(),
        "graph_before_statuses": _normalized_statuses(promotion.get("graph_before_statuses")),
        "graph_after_statuses": _normalized_statuses(promotion.get("graph_after_statuses")),
        "graph_changed_node_identities": promotion.get("graph_changed_node_identities"),
        "graph_before_revision": promotion.get("graph_before_revision"),
        "graph_expected_revision": promotion.get("graph_expected_revision"),
        "rollback_plan_sha256": str(promotion.get("rollback_plan_sha256", "") or "").strip(),
    }


def _legacy_promotion_id(promotion: Mapping[str, Any], project_root: Path) -> str:
    """Return the pre-graph-binding promotion identifier for safe migration."""
    raw_axioms = promotion.get("axioms") or []
    if isinstance(raw_axioms, (str, bytes)):
        raw_axioms = [raw_axioms]
    identity = {
        "file": _promotion_file_identity(promotion, project_root),
        "theorem": str(promotion.get("theorem", "") or "").strip(),
        "source_revision_sha256": str(promotion.get("source_revision_sha256", "") or "").strip(),
        "declaration_signature_sha256": str(
            promotion.get("declaration_signature_sha256", "") or ""
        ).strip(),
        "negation_prop": str(promotion.get("negation_prop", "") or "").strip(),
        "proof_tactic": str(promotion.get("proof_tactic", "") or "").strip(),
        "proof_declaration": str(promotion.get("proof_declaration", "") or "").strip(),
        "promotion_kind": str(
            promotion.get("promotion_kind", "scratch_negation") or "scratch_negation"
        ).strip(),
        "axioms": sorted({str(axiom).strip() for axiom in raw_axioms if str(axiom).strip()}),
        "axioms_recorded": "axioms" in promotion,
    }
    serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(serialized.encode("utf-8"))


def _pre_scope_binding_promotion_id(promotion: Mapping[str, Any], project_root: Path) -> str:
    """Return the graph-bound identifier used before campaign-root binding."""
    identity = _promotion_identity(promotion, project_root)
    for field in (
        "classification_basis",
        "classification_identity_sha256",
        "scope_root_campaign_id",
        "scope_root_identity_sha256",
        "scope_root_theorem",
        "scope_root_file",
        "scope_root_node_id",
    ):
        identity.pop(field, None)
    serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(serialized.encode("utf-8"))


def _promotion_evidence_identity(
    promotion: Mapping[str, Any], project_root: Path
) -> dict[str, Any]:
    """Return exact mathematical/graph evidence independent of rollback epoch."""
    identity = _promotion_identity(promotion, project_root)
    for field in (
        "graph_before_statuses",
        "graph_after_statuses",
        "graph_changed_node_identities",
        "graph_before_revision",
        "graph_expected_revision",
        "rollback_plan_sha256",
    ):
        identity.pop(field, None)
    return identity


def _canonicalize_promotion_record(
    promotion: Mapping[str, Any], project_root: Path
) -> dict[str, Any]:
    """Attach a canonical file/key and deterministic identifier to a record."""
    stored = dict(promotion)
    identity = _promotion_identity(stored, project_root)
    canonical_file = str(identity["file"])
    theorem = str(identity["theorem"])
    stored["file"] = canonical_file
    stored["canonical_file"] = canonical_file
    if str(stored.get("operation_path", "") or "").strip():
        stored["operation_path"] = canonical_file
    stored["key"] = f"{canonical_file}::{theorem}"
    serialized = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stored["promotion_id"] = _sha256(serialized.encode("utf-8"))
    return stored


def _promotion_identity_is_complete(identity: Mapping[str, Any]) -> bool:
    """Return whether storage identity is complete enough for exact deduplication.

    This predicate is migration-only.  Mathematical authority separately
    requires the leased graph binding and therefore never trusts a legacy
    record merely because migration can collapse an exact duplicate.
    """
    return (
        identity.get("axioms_recorded") is True
        and identity.get("is_main_goal_recorded") is True
        and all(
            str(identity.get(field, "") or "").strip()
            for field in (
                "file",
                "theorem",
                "source_revision_sha256",
                "declaration_signature_sha256",
                "negation_prop",
                "proof_tactic",
                "node_id",
            )
        )
    )


def _promotion_axioms(promotion: Mapping[str, Any]) -> set[str] | None:
    """Return recorded axiom evidence, rejecting an absent or malformed result."""
    if "axioms" not in promotion:
        return None
    raw = promotion.get("axioms")
    if raw is None:
        return set()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, list):
        return None
    return {str(item).strip() for item in raw if str(item).strip()}


def _assert_source_unchanged(
    operation: decomposition_provenance.SourceOperation,
    expected: bytes,
    *,
    stage: str,
) -> None:
    """Fail closed when the leased source differs from its pinned snapshot."""
    try:
        current = decomposition_provenance.read_source_bytes(operation)
    except OSError as exc:
        raise _SourceLeaseChanged(f"source identity changed {stage}: {str(exc)[:200]}") from exc
    if current != expected:
        raise _SourceLeaseChanged(f"source revision changed {stage}")


def _graph_identity_payload(
    *,
    theorem: str,
    operation_path: str,
    node_id: str,
    node_name: str,
    node_file: str,
    is_main_goal: bool,
) -> dict[str, Any]:
    """Build the exact source/graph/classification identity for one promotion."""
    return {
        "theorem": str(theorem),
        "operation_path": str(operation_path),
        "node_id": str(node_id),
        "graph_node_name": str(node_name),
        "graph_node_file": str(node_file),
        "is_main_goal": bool(is_main_goal),
    }


def _graph_identity_sha256(payload: Mapping[str, Any]) -> str:
    """Hash one exact graph binding deterministically."""
    serialized = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return _sha256(serialized.encode("utf-8"))


def _campaign_root_entry_payload(root: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable identity fields of one requested campaign root."""
    return {
        "campaign_id": str(root.get("campaign_id", "") or ""),
        "theorem": str(root.get("theorem", "") or ""),
        "operation_path": str(root.get("operation_path", "") or ""),
        "node_id": str(root.get("node_id", "") or ""),
        "graph_node_name": str(root.get("graph_node_name", "") or ""),
        "graph_node_file": str(root.get("graph_node_file", "") or ""),
        "declaration_signature_sha256": str(root.get("declaration_signature_sha256", "") or ""),
        "initial_source_revision_sha256": str(root.get("initial_source_revision_sha256", "") or ""),
    }


def _seal_campaign_root_entry(root: Mapping[str, Any]) -> dict[str, Any]:
    """Seal one requested root against accidental reassignment."""
    stored = {**dict(root), **_campaign_root_entry_payload(root)}
    stored["root_identity_sha256"] = _graph_identity_sha256(_campaign_root_entry_payload(stored))
    return stored


def _campaign_root_entry_is_authenticated(root: Mapping[str, Any]) -> bool:
    """Return whether one requested-root entry is complete and sealed."""
    payload = _campaign_root_entry_payload(root)
    if not all(str(value or "").strip() for value in payload.values()):
        return False
    recorded = str(root.get("root_identity_sha256", "") or "").strip()
    return bool(recorded) and recorded == _graph_identity_sha256(payload)


def _campaign_root_registry_sha256(roots: Sequence[Mapping[str, Any]]) -> str:
    """Hash an ordered requested-root registry deterministically."""
    return campaign_root_registry.campaign_root_registry_sha256(roots)


def _validate_campaign_root_registry(
    campaign: object,
) -> campaign_root_registry.CampaignRootRegistryAudit:
    """Authenticate one registry and the fresh-campaign origin that created it.

    Registry hashes detect drift, but they do not establish that the scope was
    captured before a provider could create helper declarations.  Terminal
    authority therefore additionally requires the atomically-created open
    marker to be durably sealed and the new-campaign provider nonce key to be
    present.  Marker-absent legacy campaigns remain runnable through the
    provider gate, but this validator never grants them mathematical authority.
    """
    return campaign_root_registry.audit_campaign_root_registry(campaign)


def campaign_root_provider_gate(
    campaign: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Return whether campaign root registration permits a provider turn.

    Legacy campaigns have no registration marker and remain resumable, but
    they gain no main-disproof authority. New campaigns are gated until the
    immutable registry is complete and the scope-entry handshake is closed.

    Passing a campaign snapshot lets the provider-turn reservation validate
    the gate inside the same summary transaction that increments its nonce.
    """
    if campaign is None:
        loaded = plan_state.load_summary().get("campaign")
        campaign = loaded if isinstance(loaded, Mapping) else None
    if not isinstance(campaign, Mapping):
        return True, "legacy campaign has no requested-root gate"
    if _CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD not in campaign:
        return True, "legacy campaign has no requested-root gate"
    if campaign.get(_CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD) is True:
        return False, "requested campaign roots are not registered"
    validation = _validate_campaign_root_registry(campaign)
    return validation.ok, validation.reason


def _promotion_claims_manifest_main(promotion: Mapping[str, Any]) -> bool:
    """Return whether a record claims immutable requested-root authority."""
    return (
        bool(promotion.get("is_main_goal"))
        and str(promotion.get("classification_basis", "") or "") == "requested_scope_manifest"
    )


def _promotion_has_authenticated_campaign_root_binding(
    promotion: Mapping[str, Any],
    project_root: Path,
    *,
    campaign: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether durable scope authority forbids helper-source cleanup."""
    if not _promotion_claims_manifest_main(promotion):
        return False
    if campaign is None:
        loaded = plan_state.load_summary().get("campaign")
        campaign = loaded if isinstance(loaded, Mapping) else None
    if not isinstance(campaign, Mapping):
        return False
    validation = _validate_campaign_root_registry(campaign)
    if not validation.ok:
        return False
    campaign_id = validation.campaign_id
    stored_file = _promotion_file_identity(promotion, project_root)
    matches = [
        root
        for root in validation.roots
        if str(root.get("campaign_id", "") or "")
        == str(promotion.get("scope_root_campaign_id", "") or "")
        == campaign_id
        and str(root.get("root_identity_sha256", "") or "")
        == str(promotion.get("scope_root_identity_sha256", "") or "")
        and str(root.get("theorem", "") or "") == str(promotion.get("theorem", "") or "")
        and str(root.get("operation_path", "") or "") == stored_file
        and str(root.get("node_id", "") or "") == str(promotion.get("node_id", "") or "")
        and str(root.get("declaration_signature_sha256", "") or "")
        == str(promotion.get("declaration_signature_sha256", "") or "")
    ]
    return len(matches) == 1


def _promotion_identity_seals_are_authenticated(
    promotion: Mapping[str, Any], project_root: Path
) -> bool:
    """Authenticate the stored promotion id plus graph/classification seals."""
    recorded_id = str(promotion.get("promotion_id", "") or "").strip()
    if not recorded_id:
        return False
    canonical = _canonicalize_promotion_record(promotion, project_root)
    if str(canonical.get("promotion_id", "") or "") != recorded_id:
        return False
    payload = _graph_identity_payload(
        theorem=str(promotion.get("theorem", "") or ""),
        operation_path=str(promotion.get("operation_path", "") or ""),
        node_id=str(promotion.get("node_id", "") or ""),
        node_name=str(promotion.get("graph_node_name", "") or ""),
        node_file=str(promotion.get("graph_node_file", "") or ""),
        is_main_goal=bool(promotion.get("is_main_goal")),
    )
    if not all(str(value or "").strip() for key, value in payload.items() if key != "is_main_goal"):
        return False
    if str(promotion.get("graph_identity_sha256", "") or "") != _graph_identity_sha256(payload):
        return False
    classification_payload = {
        **payload,
        "classification_basis": str(promotion.get("classification_basis", "") or ""),
        "scope_root_campaign_id": str(promotion.get("scope_root_campaign_id", "") or ""),
        "scope_root_identity_sha256": str(promotion.get("scope_root_identity_sha256", "") or ""),
        "scope_root_theorem": str(promotion.get("scope_root_theorem", "") or ""),
        "scope_root_file": str(promotion.get("scope_root_file", "") or ""),
        "scope_root_node_id": str(promotion.get("scope_root_node_id", "") or ""),
    }
    if str(promotion.get("classification_identity_sha256", "") or "") != _graph_identity_sha256(
        classification_payload
    ):
        return False
    return _rollback_plan_is_authenticated(promotion)


def authoritative_runtime_main_promotion(
    autonomy_state: Mapping[str, Any],
    *,
    summary: Mapping[str, Any] | None = None,
    cwd: str = "",
) -> dict[str, Any] | None:
    """Return this run's revalidated main promotion, or fail closed.

    A raw durable promotion row is evidence awaiting startup revalidation, not
    a process-local terminal verdict.  Callers may render or route a disproof
    only after the native runtime set its terminal outcome with the exact
    promotion payload and while no promotion/cleanup ambiguity is active.
    """
    if str(autonomy_state.get("terminal_outcome", "") or "") != "disproved":
        return None
    if str(autonomy_state.get("operational_pause", "") or ""):
        return None
    for field in (
        "negation_promotion_pending",
        "false_cleanup_pending",
        "false_cleanup_quarantined",
    ):
        try:
            if int(autonomy_state.get(field, 0) or 0) > 0:
                return None
        except (TypeError, ValueError):
            return None
    payload = autonomy_state.get("negation_promotion")
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        return None
    if payload.get("is_main_goal") is not True:
        return None
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping) or not _promotion_claims_manifest_main(evidence):
        return None
    current_summary = summary if isinstance(summary, Mapping) else plan_state.load_summary()
    transaction_audit = _audit_promotion_transactions(
        current_summary.get("negation_promotion_transactions")
    )
    if not transaction_audit.ok:
        return None
    campaign = current_summary.get("campaign")
    if not isinstance(campaign, Mapping):
        return None
    project_root = Path(cwd or os.getenv("LEANFLOW_PROJECT_ROOT", "") or ".").expanduser().resolve()
    if not _promotion_has_authenticated_campaign_root_binding(
        evidence,
        project_root,
        campaign=campaign,
    ):
        return None
    if not _promotion_identity_seals_are_authenticated(evidence, project_root):
        return None
    ledger = current_summary.get("negation_promotions")
    promotion_audit = _audit_active_promotions(ledger)
    if not isinstance(ledger, list) or not promotion_audit.ok:
        return None
    promotion_id = str(evidence.get("promotion_id", "") or "").strip()
    promotion_index = promotion_audit.unique_authenticated_index(promotion_id)
    if promotion_index is None:
        return None
    raw_stored = ledger[promotion_index]
    if not isinstance(raw_stored, Mapping):
        return None
    stored = dict(raw_stored)
    if not _promotion_identity_seals_are_authenticated(stored, project_root):
        return None
    if _promotion_identity(stored, project_root) != _promotion_identity(evidence, project_root):
        return None
    raw_transactions = current_summary.get("negation_promotion_transactions")
    if not isinstance(raw_transactions, list):
        return None
    committed_matches = [
        record.index
        for record in transaction_audit.records
        if record.disposition == "terminal"
        and record.transaction_id == promotion_id
        and record.promotion_id == promotion_id
        and isinstance(raw_transactions[record.index], Mapping)
        and str(raw_transactions[record.index].get("state", "") or "") == "committed"
    ]
    if len(committed_matches) != 1:
        return None
    return dict(evidence)


def _rollback_plan_payload(promotion: Mapping[str, Any]) -> dict[str, Any]:
    """Build the complete graph rollback plan protected by its own seal."""
    return {
        "node_id": str(promotion.get("node_id", "") or ""),
        "graph_node_name": str(promotion.get("graph_node_name", "") or ""),
        "graph_node_file": str(promotion.get("graph_node_file", "") or ""),
        "graph_identity_sha256": str(promotion.get("graph_identity_sha256", "") or ""),
        "classification_identity_sha256": str(
            promotion.get("classification_identity_sha256", "") or ""
        ),
        "is_main_goal": bool(promotion.get("is_main_goal")),
        "classification_basis": str(promotion.get("classification_basis", "") or ""),
        "scope_root_campaign_id": str(promotion.get("scope_root_campaign_id", "") or ""),
        "scope_root_identity_sha256": str(promotion.get("scope_root_identity_sha256", "") or ""),
        "scope_root_theorem": str(promotion.get("scope_root_theorem", "") or ""),
        "scope_root_file": str(promotion.get("scope_root_file", "") or ""),
        "scope_root_node_id": str(promotion.get("scope_root_node_id", "") or ""),
        "graph_before_statuses": _normalized_statuses(promotion.get("graph_before_statuses")),
        "graph_after_statuses": _normalized_statuses(promotion.get("graph_after_statuses")),
        "graph_changed_node_identities": promotion.get("graph_changed_node_identities"),
        "graph_before_revision": promotion.get("graph_before_revision"),
        "graph_expected_revision": promotion.get("graph_expected_revision"),
    }


def _seal_rollback_plan(promotion: Mapping[str, Any]) -> dict[str, Any]:
    """Attach a deterministic seal over every rollback-consumed field."""
    stored = dict(promotion)
    payload = _rollback_plan_payload(stored)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    stored["rollback_plan_sha256"] = _sha256(serialized.encode("utf-8"))
    return stored


def _rollback_plan_is_authenticated(promotion: Mapping[str, Any]) -> bool:
    """Return whether all rollback fields match their durable seal."""
    recorded = str(promotion.get("rollback_plan_sha256", "") or "").strip()
    if not recorded:
        return False
    payload = _rollback_plan_payload(promotion)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if recorded != _sha256(serialized.encode("utf-8")):
        return False
    identities = promotion.get("graph_changed_node_identities")
    before = _normalized_statuses(promotion.get("graph_before_statuses"))
    if not isinstance(identities, Mapping) or set(map(str, identities)) != set(before):
        return False
    return all(
        isinstance(identity, Mapping)
        and str(identity.get("name", "") or "")
        and str(identity.get("file", "") or "")
        for identity in identities.values()
    )


def _rollback_plan_is_pre_scope_authenticated(promotion: Mapping[str, Any]) -> bool:
    """Return whether a legacy rollback seal authenticates its pre-scope fields."""
    recorded = str(promotion.get("rollback_plan_sha256", "") or "").strip()
    if not recorded:
        return False
    payload = _rollback_plan_payload(promotion)
    for field in (
        "classification_basis",
        "classification_identity_sha256",
        "scope_root_campaign_id",
        "scope_root_identity_sha256",
        "scope_root_theorem",
        "scope_root_file",
        "scope_root_node_id",
    ):
        payload.pop(field, None)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if recorded != _sha256(serialized.encode("utf-8")):
        return False
    identities = promotion.get("graph_changed_node_identities")
    before = _normalized_statuses(promotion.get("graph_before_statuses"))
    if not isinstance(identities, Mapping) or set(map(str, identities)) != set(before):
        return False
    return all(
        isinstance(identity, Mapping)
        and str(identity.get("name", "") or "")
        and str(identity.get("file", "") or "")
        for identity in identities.values()
    )


def _node_file_reaches_operation(
    node_file: str,
    *,
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
) -> bool:
    """Return whether a graph file label reaches the currently leased source."""
    candidate = Path(str(node_file or "").strip()).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    try:
        return candidate.resolve(strict=True) == operation.path
    except (OSError, RuntimeError):
        return False


def _unique_graph_node_for_new_promotion(
    blueprint: plan_state.Blueprint,
    *,
    theorem: str,
    file_label: str,
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
) -> plan_state.GraphNode | PromotionResult:
    """Resolve exactly one deterministic graph node for a fresh promotion."""
    candidate_ids = {
        plan_state.node_id_for(theorem, file_label),
        plan_state.node_id_for(theorem, str(operation.path)),
    }
    matches = [
        node
        for node in blueprint.nodes
        if node.name == theorem
        and _node_file_reaches_operation(
            node.file,
            project_root=project_root,
            operation=operation,
        )
        and (node.id in candidate_ids or node.id == plan_state.node_id_for(node.name, node.file))
    ]
    if len(matches) != 1:
        if matches:
            return PromotionResult(False, "dependency graph has ambiguous declaration identity")
        return PromotionResult(False, "dependency graph does not contain the probed declaration")
    node = matches[0]
    if node.id != plan_state.node_id_for(node.name, node.file):
        return PromotionResult(False, "dependency graph node identity is not deterministic")
    return node


def _is_decomposition_helper_under_lease(
    *,
    blueprint: plan_state.Blueprint,
    node: plan_state.GraphNode,
    theorem_id: str,
    promotion: Mapping[str, Any],
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
    source_bytes: bytes,
) -> bool:
    """Classify the exact graph declaration from graph and leased provenance."""
    if any(edge.kind == "split_of" and edge.source == node.id for edge in blueprint.edges):
        return True
    if node.generated_by == "decomposer":
        return True
    try:
        current_source = source_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False
    provenance, _reason = decomposition_provenance.resolve_helper_provenance(
        helper_name=theorem_id,
        file_label=str(operation.path),
        promotion_signature_sha256=str(promotion.get("declaration_signature_sha256", "") or ""),
        current_source=current_source,
        cwd=str(project_root),
    )
    return provenance is not None


def record_requested_campaign_roots(
    requested_roots: Sequence[Mapping[str, Any]],
    *,
    campaign_id: str,
    cwd: str = "",
) -> CampaignRootRegistration:
    """Persist the immutable full requested scope before the first provider turn.

    Call this once after deterministic queue construction has materialized its
    graph nodes, but before any provider turn or source edit.  Later queue
    assignments cannot extend or replace the registry.
    """
    project_root = Path(cwd or ".").expanduser().resolve()
    requested_campaign_id = str(campaign_id or "").strip()
    normalized: list[tuple[str, str]] = []
    for raw in requested_roots:
        theorem = str(raw.get("target_symbol", raw.get("theorem", "")) or "").strip()
        file_label = str(raw.get("active_file", raw.get("file", "")) or "").strip()
        if not theorem or not file_label:
            return CampaignRootRegistration(False, "requested root lacks theorem/file identity")
        source_path = Path(file_label).expanduser()
        if not source_path.is_absolute():
            source_path = project_root / source_path
        try:
            canonical_source = source_path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            return CampaignRootRegistration(
                False, f"requested root source is unavailable: {str(exc)[:200]}"
            )
        identity = (theorem, str(canonical_source))
        if identity in normalized:
            return CampaignRootRegistration(False, "requested root registry contains a duplicate")
        normalized.append(identity)
    if not requested_campaign_id:
        return CampaignRootRegistration(False, "campaign id is required")
    normalized.sort(key=lambda item: (item[1], item[0]))

    operations: list[tuple[decomposition_provenance.SourceOperation, bytes]] = []
    prepared: list[
        tuple[
            str, str, decomposition_provenance.SourceOperation, bytes, negation_probe.NegationGoal
        ]
    ] = []
    entries: list[dict[str, Any]] = []
    try:
        with contextlib.ExitStack() as stack:
            roots_by_file: dict[str, list[str]] = {}
            for theorem, file_label in normalized:
                roots_by_file.setdefault(file_label, []).append(theorem)
            for file_label in sorted(roots_by_file):
                operation = stack.enter_context(
                    decomposition_provenance.source_operation(Path(file_label), canonical=True)
                )
                source_bytes = decomposition_provenance.read_source_bytes(operation)
                operations.append((operation, source_bytes))
                for theorem in sorted(roots_by_file[file_label]):
                    goal = negation_probe.build_negation_goal(
                        str(operation.path), theorem, cwd=str(project_root)
                    )
                    _assert_source_unchanged(
                        operation,
                        source_bytes,
                        stage="after campaign-root declaration read",
                    )
                    if isinstance(goal, dict):
                        return CampaignRootRegistration(
                            False,
                            f"requested root declaration is unavailable: {goal.get('error', '')}",
                        )
                    prepared.append((theorem, file_label, operation, source_bytes, goal))

            with plan_state.blueprint_commit_guard():
                blueprint = plan_state.load_blueprint()
                semantic_identities: set[tuple[str, str, str]] = set()
                for theorem, file_label, operation, source_bytes, goal in prepared:
                    node_result = _unique_graph_node_for_new_promotion(
                        blueprint,
                        theorem=theorem,
                        file_label=file_label,
                        project_root=project_root,
                        operation=operation,
                    )
                    if isinstance(node_result, PromotionResult):
                        return CampaignRootRegistration(False, node_result.reason)
                    semantic_identity = (theorem, str(operation.path), node_result.id)
                    if semantic_identity in semantic_identities:
                        return CampaignRootRegistration(
                            False, "requested root registry contains a semantic alias duplicate"
                        )
                    semantic_identities.add(semantic_identity)
                    signature_sha256 = _sha256(goal.original.encode("utf-8"))
                    if _is_decomposition_helper_under_lease(
                        blueprint=blueprint,
                        node=node_result,
                        theorem_id=theorem,
                        promotion={"declaration_signature_sha256": signature_sha256},
                        project_root=project_root,
                        operation=operation,
                        source_bytes=source_bytes,
                    ):
                        return CampaignRootRegistration(
                            False, "requested scope contains a decomposition helper"
                        )
                    if node_result.generated_by not in {"", "human", "queue-sync"}:
                        return CampaignRootRegistration(
                            False, "requested root has non-root graph ownership"
                        )
                    entries.append(
                        _seal_campaign_root_entry(
                            {
                                "campaign_id": requested_campaign_id,
                                "theorem": theorem,
                                "operation_path": str(operation.path),
                                "node_id": node_result.id,
                                "graph_node_name": node_result.name,
                                "graph_node_file": node_result.file,
                                "declaration_signature_sha256": signature_sha256,
                                "initial_source_revision_sha256": _sha256(source_bytes),
                            }
                        )
                    )
                entries.sort(key=lambda item: (str(item["operation_path"]), str(item["theorem"])))
                registry_sha256 = _campaign_root_registry_sha256(entries)

                def mutate(summary: dict[str, Any]) -> dict[str, Any]:
                    campaign = dict(summary.get("campaign") or {})
                    if str(campaign.get("campaign_id", "") or "") != requested_campaign_id:
                        return {
                            "ok": False,
                            "created": False,
                            "reason": "campaign identity changed before root commit",
                        }
                    existing = campaign.get(_CAMPAIGN_ROOTS_FIELD)
                    if isinstance(existing, Mapping):
                        validation = _validate_campaign_root_registry(campaign)
                        if validation.ok and list(validation.roots) == entries:
                            return {
                                "ok": True,
                                "created": False,
                                "reason": "requested roots already recorded",
                            }
                        return {
                            "ok": False,
                            "created": False,
                            "reason": "requested campaign roots are immutable",
                        }
                    if campaign.get(_CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD) is not True:
                        return {
                            "ok": False,
                            "created": False,
                            "reason": "campaign root registration was not opened at scope entry",
                        }
                    provider_turn_nonce = campaign.get("provider_turn_nonce")
                    if type(provider_turn_nonce) is not int:
                        return {
                            "ok": False,
                            "created": False,
                            "reason": "legacy campaign cannot infer roots from its current queue",
                        }
                    if provider_turn_nonce != 0:
                        return {
                            "ok": False,
                            "created": False,
                            "reason": "requested roots must be recorded before the first provider turn",
                        }
                    campaign[_CAMPAIGN_ROOTS_FIELD] = {
                        "version": 1,
                        "campaign_id": requested_campaign_id,
                        "roots": entries,
                        "registry_sha256": registry_sha256,
                    }
                    campaign[_CAMPAIGN_ROOT_REGISTRATION_OPEN_FIELD] = False
                    summary["campaign"] = campaign
                    return {
                        "ok": True,
                        "created": True,
                        "reason": "requested campaign roots recorded",
                    }

                for operation, source_bytes in operations:
                    _assert_source_unchanged(
                        operation,
                        source_bytes,
                        stage="immediately before campaign-root registry commit",
                    )
                if plan_state.load_blueprint() != blueprint:
                    raise _GraphTransactionChanged(
                        "dependency graph changed before campaign-root registry commit"
                    )
                outcome = dict(update_json_file(plan_state.plan_state_paths().summary_json, mutate))
                if not bool(outcome.get("ok")):
                    return CampaignRootRegistration(False, str(outcome.get("reason", "") or ""))
                return CampaignRootRegistration(
                    True,
                    str(outcome.get("reason", "") or "requested campaign roots recorded"),
                    tuple(entries),
                )
    except (_SourceLeaseChanged, _GraphTransactionChanged, OSError, UnicodeDecodeError) as exc:
        return CampaignRootRegistration(
            False, f"requested root registration failed: {str(exc)[:200]}"
        )


def _node_file_matches_requested_scope(
    node_file: str,
    *,
    active_file: str,
    project_root: Path,
) -> bool:
    """Return whether a graph file is the exact newly requested scope file."""
    if not str(active_file or "").strip():
        return True
    node_path = Path(str(node_file or "").strip()).expanduser()
    scope_path = Path(str(active_file or "").strip()).expanduser()
    if not node_path.is_absolute():
        node_path = project_root / node_path
    if not scope_path.is_absolute():
        scope_path = project_root / scope_path
    try:
        return node_path.resolve(strict=True) == scope_path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def _authenticated_campaign_root(
    *,
    promotion: Mapping[str, Any],
    node: plan_state.GraphNode,
    operation: decomposition_provenance.SourceOperation,
) -> dict[str, Any] | PromotionResult:
    """Resolve the target only from the immutable pre-provider root registry."""
    summary = plan_state.load_summary()
    campaign = summary.get("campaign")
    if not isinstance(campaign, Mapping):
        return PromotionResult(False, "campaign has no durable requested-root registry")
    validation = _validate_campaign_root_registry(campaign)
    if not validation.ok:
        return PromotionResult(False, validation.reason)
    campaign_id = validation.campaign_id
    theorem = str(promotion.get("theorem", "") or "").strip()
    signature = str(promotion.get("declaration_signature_sha256", "") or "").strip()
    matches = [
        root
        for root in validation.roots
        if str(root.get("campaign_id", "") or "") == campaign_id
        and str(root.get("theorem", "") or "") == theorem
        and str(root.get("operation_path", "") or "") == str(operation.path)
        and str(root.get("node_id", "") or "") == node.id
    ]
    if len(matches) != 1:
        return PromotionResult(False, "promotion target is not an immutable requested root")
    root = matches[0]
    if (
        str(root.get("graph_node_name", "") or "") != node.name
        or str(root.get("graph_node_file", "") or "") != node.file
        or str(root.get("declaration_signature_sha256", "") or "") != signature
        or node.id != plan_state.node_id_for(node.name, node.file)
    ):
        return PromotionResult(False, "requested-root declaration identity changed")
    return dict(root)


def _classify_graph_node_under_lease(
    *,
    blueprint: plan_state.Blueprint,
    node: plan_state.GraphNode,
    theorem_id: str,
    promotion: Mapping[str, Any],
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
    source_bytes: bytes,
    requested_target_symbol: str,
    requested_active_file: str,
) -> dict[str, Any] | PromotionResult:
    """Classify a node from helper ownership or an authoritative campaign root."""
    # Scope-entry authority is immutable. A later planner/decomposer edge or
    # provenance record cannot demote a sealed requested root into a helper.
    root = _authenticated_campaign_root(
        promotion=promotion,
        node=node,
        operation=operation,
    )
    if not isinstance(root, PromotionResult):
        if requested_target_symbol and str(requested_target_symbol).strip() != str(root["theorem"]):
            return PromotionResult(False, "current assignment does not match the requested root")
        if requested_active_file and not _node_file_matches_requested_scope(
            str(root["graph_node_file"]),
            active_file=requested_active_file,
            project_root=project_root,
        ):
            return PromotionResult(
                False, "current assignment file does not match the requested root"
            )
        return {
            "is_main_goal": True,
            "classification_basis": "requested_scope_manifest",
            "scope_root_campaign_id": str(root["campaign_id"]),
            "scope_root_identity_sha256": str(root["root_identity_sha256"]),
            "scope_root_theorem": str(root["theorem"]),
            "scope_root_file": str(root["graph_node_file"]),
            "scope_root_node_id": str(root["node_id"]),
        }

    if _is_decomposition_helper_under_lease(
        blueprint=blueprint,
        node=node,
        theorem_id=theorem_id,
        promotion=promotion,
        project_root=project_root,
        operation=operation,
        source_bytes=source_bytes,
    ):
        return {
            "is_main_goal": False,
            "classification_basis": "decomposition_helper",
            "scope_root_campaign_id": "",
            "scope_root_identity_sha256": "",
            "scope_root_theorem": "",
            "scope_root_file": "",
            "scope_root_node_id": "",
        }
    # Current queue assignment and mutable topology cannot establish root
    # authority when the immutable registry did not match.
    return root


def _bind_graph_identity(
    promotion: Mapping[str, Any],
    *,
    blueprint: plan_state.Blueprint,
    node: plan_state.GraphNode,
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
    source_bytes: bytes,
    requested_target_symbol: str = "",
    requested_active_file: str = "",
) -> dict[str, Any] | PromotionResult:
    """Attach the exact graph node and current main/helper classification."""
    theorem = str(promotion.get("theorem", "") or "").strip()
    classification = _classify_graph_node_under_lease(
        blueprint=blueprint,
        node=node,
        theorem_id=theorem,
        promotion=promotion,
        project_root=project_root,
        operation=operation,
        source_bytes=source_bytes,
        requested_target_symbol=requested_target_symbol,
        requested_active_file=requested_active_file,
    )
    if isinstance(classification, PromotionResult):
        return classification
    is_main_goal = bool(classification["is_main_goal"])
    payload = _graph_identity_payload(
        theorem=theorem,
        operation_path=str(operation.path),
        node_id=node.id,
        node_name=node.name,
        node_file=node.file,
        is_main_goal=is_main_goal,
    )
    classification_payload = {
        **payload,
        "classification_basis": str(classification["classification_basis"]),
        "scope_root_campaign_id": str(classification["scope_root_campaign_id"]),
        "scope_root_identity_sha256": str(classification["scope_root_identity_sha256"]),
        "scope_root_theorem": str(classification["scope_root_theorem"]),
        "scope_root_file": str(classification["scope_root_file"]),
        "scope_root_node_id": str(classification["scope_root_node_id"]),
    }
    return {
        **dict(promotion),
        **payload,
        **classification,
        "file": str(operation.path),
        "canonical_file": str(operation.path),
        "graph_identity_sha256": _graph_identity_sha256(payload),
        "classification_identity_sha256": _graph_identity_sha256(classification_payload),
    }


def _validate_or_upgrade_graph_binding(
    promotion: Mapping[str, Any],
    *,
    blueprint: plan_state.Blueprint,
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
    source_bytes: bytes,
    requested_target_symbol: str = "",
    requested_active_file: str = "",
) -> dict[str, Any] | PromotionResult:
    """Prove an exact graph binding, upgrading only uniquely provable legacy evidence."""
    theorem = str(promotion.get("theorem", "") or "").strip()
    node_id = str(promotion.get("node_id", "") or "").strip()
    if not theorem or not node_id:
        return PromotionResult(False, "promotion lacks theorem/node graph identity")
    matches = [node for node in blueprint.nodes if node.id == node_id]
    if len(matches) != 1:
        return PromotionResult(False, "dependency graph node identity is missing or ambiguous")
    node = matches[0]
    if node.name != theorem:
        return PromotionResult(False, "dependency graph node name no longer matches promotion")
    if not _node_file_reaches_operation(
        node.file,
        project_root=project_root,
        operation=operation,
    ):
        return PromotionResult(False, "dependency graph node file no longer matches promotion")
    semantic_declaration_matches = [
        candidate
        for candidate in blueprint.nodes
        if candidate.name == node.name
        and _node_file_reaches_operation(
            candidate.file,
            project_root=project_root,
            operation=operation,
        )
    ]
    if len(semantic_declaration_matches) != 1:
        return PromotionResult(False, "dependency graph declaration identity is ambiguous")
    bound = _bind_graph_identity(
        promotion,
        blueprint=blueprint,
        node=node,
        project_root=project_root,
        operation=operation,
        source_bytes=source_bytes,
        requested_target_symbol=requested_target_symbol,
        requested_active_file=requested_active_file,
    )
    if isinstance(bound, PromotionResult):
        return bound
    expected_payload = _graph_identity_payload(
        theorem=theorem,
        operation_path=str(operation.path),
        node_id=node.id,
        node_name=node.name,
        node_file=node.file,
        is_main_goal=bool(bound["is_main_goal"]),
    )
    expected_hash = _graph_identity_sha256(expected_payload)
    expected_classification_hash = _graph_identity_sha256(
        {
            **expected_payload,
            "classification_basis": str(bound["classification_basis"]),
            "scope_root_campaign_id": str(bound["scope_root_campaign_id"]),
            "scope_root_identity_sha256": str(bound["scope_root_identity_sha256"]),
            "scope_root_theorem": str(bound["scope_root_theorem"]),
            "scope_root_file": str(bound["scope_root_file"]),
            "scope_root_node_id": str(bound["scope_root_node_id"]),
        }
    )
    recorded_fields = (
        "operation_path",
        "graph_node_name",
        "graph_node_file",
        "graph_identity_sha256",
        "classification_basis",
        "classification_identity_sha256",
    )
    has_binding = all(str(promotion.get(field, "") or "").strip() for field in recorded_fields)
    if has_binding:
        if str(promotion.get("operation_path", "") or "") != str(operation.path):
            return PromotionResult(False, "promotion source operation identity was reassigned")
        if str(promotion.get("graph_node_name", "") or "") != node.name:
            return PromotionResult(False, "promotion graph node name was reassigned")
        if str(promotion.get("graph_node_file", "") or "") != node.file:
            return PromotionResult(False, "promotion graph node file was reassigned")
        if bool(promotion.get("is_main_goal")) != bool(bound["is_main_goal"]):
            return PromotionResult(False, "promotion main/helper classification changed")
        for field in (
            "classification_basis",
            "scope_root_campaign_id",
            "scope_root_identity_sha256",
            "scope_root_theorem",
            "scope_root_file",
            "scope_root_node_id",
        ):
            if str(promotion.get(field, "") or "") != str(bound.get(field, "") or ""):
                return PromotionResult(False, "promotion campaign-root classification changed")
        if str(promotion.get("graph_identity_sha256", "") or "") != expected_hash:
            return PromotionResult(False, "promotion graph identity hash does not match")
        if (
            str(promotion.get("classification_identity_sha256", "") or "")
            != expected_classification_hash
        ):
            return PromotionResult(False, "promotion campaign-root identity hash does not match")
    else:
        # Legacy authority can be upgraded only when every old classifier agrees
        # with the uniquely reconstructed current graph/source identity.
        if "is_main_goal" not in promotion:
            return PromotionResult(False, "legacy promotion lacks main/helper classification")
        if bool(promotion.get("is_main_goal")) != bool(bound["is_main_goal"]):
            return PromotionResult(False, "legacy promotion main/helper classification is stale")
        if _rollback_plan_is_pre_scope_authenticated(promotion):
            bound = _seal_rollback_plan(bound)
    return bound


def _graph_restore_is_safe(
    promotion: Mapping[str, Any],
    *,
    blueprint: plan_state.Blueprint,
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
) -> bool:
    """Return whether rollback still targets the exact recorded declaration."""
    if not _rollback_plan_is_authenticated(promotion):
        return False
    node_id = str(promotion.get("node_id", "") or "").strip()
    theorem = str(promotion.get("theorem", "") or "").strip()
    matches = [node for node in blueprint.nodes if node.id == node_id]
    if len(matches) != 1 or matches[0].name != theorem:
        return False
    node = matches[0]
    recorded_name = str(promotion.get("graph_node_name", "") or "").strip()
    recorded_file = str(promotion.get("graph_node_file", "") or "").strip()
    if recorded_name and recorded_name != node.name:
        return False
    if recorded_file and recorded_file != node.file:
        return False
    return _node_file_reaches_operation(
        node.file,
        project_root=project_root,
        operation=operation,
    )


def _current_promotion_goal(
    promotion: Mapping[str, Any],
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
) -> tuple[bytes, negation_probe.NegationGoal] | PromotionResult:
    """Load the exact current declaration and compare its durable identity."""
    theorem = str(promotion.get("theorem", "") or "").strip()
    file_label = _promotion_file_identity(promotion, project_root)
    if not theorem or not file_label:
        return PromotionResult(False, "promotion lacks theorem/file identity")
    if str(operation.path) != file_label:
        return PromotionResult(False, "promotion source operation identity does not match its file")
    try:
        source_bytes = decomposition_provenance.read_source_bytes(operation)
    except OSError as exc:
        return PromotionResult(False, f"source unavailable: {str(exc)[:200]}")
    # Promotion authority is revision-scoped, not merely declaration-scoped:
    # unrelated same-file edits require a fresh rerun and a fresh transaction.
    if _sha256(source_bytes) != str(promotion.get("source_revision_sha256", "") or ""):
        return PromotionResult(False, "source revision changed after promotion")
    goal = negation_probe.build_negation_goal(str(operation.path), theorem, cwd=str(project_root))
    try:
        _assert_source_unchanged(operation, source_bytes, stage="after goal reconstruction")
    except _SourceLeaseChanged as exc:
        return PromotionResult(False, str(exc))
    if isinstance(goal, dict):
        return PromotionResult(
            False, f"current declaration cannot be negated: {goal.get('error', '')}"
        )
    if _sha256(goal.original.encode("utf-8")) != str(
        promotion.get("declaration_signature_sha256", "") or ""
    ):
        return PromotionResult(False, "declaration signature changed after promotion")
    if goal.prop != str(promotion.get("negation_prop", "") or ""):
        return PromotionResult(False, "reconstructed negation no longer matches promotion")
    return source_bytes, goal


def _run_authoritative_source_check(
    source: str,
    *,
    cwd: str,
    theorem: str,
) -> dict[str, Any]:
    """Elaborate one exact full-source harness with a cold-start deadline.

    The isolated checker still owns all mathematical authority.  This wrapper
    only separates its whole-module wall budget from the much smaller scratch
    probe budget and records enough timing to diagnose a resumable pause.
    """
    timeout_s = negation_revalidation_policy.source_promotion_timeout_s(
        probe_timeout_s=negation_probe.probe_timeout_s()
    )
    if os.getenv("LEANFLOW_WORKFLOW_RUN_ID", "").strip():
        with contextlib.suppress(Exception):
            append_workflow_activity(
                "negation-promotion-kernel-check-started",
                f"Started exact kernel revalidation for {theorem}",
                theorem=theorem,
                timeout_s=timeout_s,
            )
    started = time.monotonic()
    result = lean_ephemeral_source_check(source, cwd=cwd, timeout_s=timeout_s)
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    failure_detail = _authoritative_failure_detail(result) if not result.get("success") else ""
    if os.getenv("LEANFLOW_WORKFLOW_RUN_ID", "").strip():
        with contextlib.suppress(Exception):
            append_workflow_activity(
                "negation-promotion-kernel-check-completed",
                f"Completed exact kernel revalidation for {theorem}",
                theorem=theorem,
                timeout_s=timeout_s,
                elapsed_ms=elapsed_ms,
                success=bool(result.get("success")),
                timed_out=bool(result.get("timed_out")),
                retryable=bool(result.get("retryable")),
                failure_kind=str(result.get("failure_kind", "") or ""),
                failure_detail=failure_detail,
            )
    return {
        **result,
        "authoritative_timeout_s": timeout_s,
        "authoritative_elapsed_ms": elapsed_ms,
        "failure_detail": failure_detail,
    }


def _rerun_source_promotion(
    promotion: Mapping[str, Any],
    *,
    project_root: Path,
    source_path: Path,
    source_bytes: bytes,
    goal: negation_probe.NegationGoal,
) -> list[str] | PromotionResult:
    """Rerun one exact source-negation declaration and return printed axioms."""
    candidate = str(promotion.get("proof_declaration", "") or "").strip()
    if not candidate:
        return PromotionResult(False, "source promotion lacks proof declaration identity")
    try:
        source_text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return PromotionResult(False, f"source unavailable: {str(exc)[:200]}")
    region = _exact_source_declaration_region(source_path, source_text, candidate)
    if not region:
        return PromotionResult(
            False,
            "source negation declaration was not found",
            failure_kind=SOURCE_CANDIDATE_DECLARATION_MISSING,
        )
    candidate_text = str(region.get("text", "") or "")
    candidate_statement = _validated_source_candidate_statement(candidate_text)
    if isinstance(candidate_statement, PromotionResult):
        return candidate_statement
    lines = source_text.splitlines()
    insert_at = int(region.get("end_line", 0) or 0)
    if insert_at <= 0 or insert_at > len(lines):
        return PromotionResult(False, "source negation declaration range is invalid")
    alias = f"leanflowNegationPromotion_{_sha256((goal.name + candidate).encode())[:12]}"
    candidate_name = str(region.get("name", "") or candidate).strip()
    harness = source_negation_harness.build_source_negation_harness(
        alias=alias,
        negation_prop=goal.prop,
        candidate_name=candidate_name,
        recorded_proof_tactic=str(promotion.get("proof_tactic", "") or ""),
    )
    if harness is None:
        return PromotionResult(False, "source promotion proof tactic no longer matches declaration")
    # The candidate's declaration prefix contains its complete elaboration
    # context. Later commands cannot affect the candidate or the inserted alias,
    # but a slow or broken assigned theorem there can poison this compatibility
    # check and cause the same unrelated full-file failure to be replayed for
    # every support lemma.
    scratch_source = "\n".join([*lines[:insert_at], harness.declaration]) + "\n"
    rerun = _run_authoritative_source_check(
        scratch_source,
        cwd=str(project_root),
        theorem=str(promotion.get("theorem", "") or goal.name),
    )
    messages = list(rerun.get("messages") or [])
    if not rerun.get("success") or any(
        isinstance(message, Mapping)
        and str(message.get("severity", "") or "").strip().lower() == "error"
        for message in messages
    ):
        return PromotionResult(
            False,
            "fresh source rerun did not elaborate the exact negation",
            failure_kind=str(rerun.get("failure_kind", "lean_elaboration") or "lean_elaboration"),
            retryable=bool(rerun.get("retryable", False)),
        )
    axioms = _printed_axioms(_scratch_messages(rerun), alias)
    if axioms is None:
        return PromotionResult(False, "fresh source negation has no auditable axiom result")
    return axioms


def _revalidate_promotion_under_operation(
    promotion: Mapping[str, Any],
    *,
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
    require_graph_binding: bool = True,
    requested_target_symbol: str = "",
    requested_active_file: str = "",
) -> PromotionResult:
    """Rerun durable evidence while retaining one exact source lease."""
    canonical = _canonicalize_promotion_record(promotion, project_root)
    recorded_id = str(promotion.get("promotion_id", "") or "").strip()
    has_graph_binding = bool(str(promotion.get("graph_identity_sha256", "") or "").strip())
    has_scope_binding = bool(str(promotion.get("classification_basis", "") or "").strip())
    canonical_id = str(canonical.get("promotion_id", "") or "")
    if recorded_id:
        permitted_ids = {canonical_id}
        if not has_graph_binding:
            permitted_ids.add(_legacy_promotion_id(promotion, project_root))
        elif not has_scope_binding:
            permitted_ids.add(_pre_scope_binding_promotion_id(promotion, project_root))
        if recorded_id not in permitted_ids:
            return PromotionResult(False, "promotion identity hash does not match its evidence")
    recorded_axioms = _promotion_axioms(promotion)
    if recorded_axioms is None:
        return PromotionResult(False, "promotion lacks auditable axiom evidence")
    if not recorded_axioms <= negation_probe.STANDARD_AXIOMS:
        return PromotionResult(False, "promotion records non-standard axioms")
    current = _current_promotion_goal(promotion, project_root, operation)
    if isinstance(current, PromotionResult):
        return current
    source_bytes, goal = current
    kind = str(promotion.get("promotion_kind", "scratch_negation") or "scratch_negation")
    if kind == "source_negation":
        source_result = _rerun_source_promotion(
            promotion,
            project_root=project_root,
            source_path=operation.path,
            source_bytes=source_bytes,
            goal=goal,
        )
        try:
            _assert_source_unchanged(operation, source_bytes, stage="after source-negation rerun")
        except _SourceLeaseChanged as exc:
            return PromotionResult(False, str(exc))
        if isinstance(source_result, PromotionResult):
            return source_result
        rerun_axioms = set(source_result)
    else:
        tactic = str(promotion.get("proof_tactic", "") or "").strip()
        if not tactic or re.search(r"\b(?:sorry|admit|sorryAx)\b", tactic):
            return PromotionResult(False, "promotion proof tactic is absent or unsafe")
        rerun = negation_probe.run_negation_attempt(
            goal,
            file_path=str(operation.path),
            cwd=str(project_root),
            timeout_s=negation_probe.probe_timeout_s(),
            tactics=(tactic,),
        )
        try:
            _assert_source_unchanged(operation, source_bytes, stage="after negation rerun")
        except _SourceLeaseChanged as exc:
            return PromotionResult(False, str(exc))
        if rerun.get("verdict") != "negation_proved" or not rerun.get("axioms_ok"):
            return PromotionResult(False, "fresh Lean rerun did not re-prove the negation")
        rerun_axioms = {str(item) for item in (rerun.get("axioms") or []) if str(item)}
    if not rerun_axioms <= negation_probe.STANDARD_AXIOMS:
        return PromotionResult(False, "fresh negation depends on non-standard axioms")
    if rerun_axioms != recorded_axioms:
        return PromotionResult(False, "fresh negation axiom result changed after promotion")
    bound: dict[str, Any] = dict(promotion)
    if require_graph_binding:
        try:
            _assert_source_unchanged(operation, source_bytes, stage="before graph identity check")
        except _SourceLeaseChanged as exc:
            return PromotionResult(False, str(exc))
        blueprint = plan_state.load_blueprint()
        graph_result = _validate_or_upgrade_graph_binding(
            promotion,
            blueprint=blueprint,
            project_root=project_root,
            operation=operation,
            source_bytes=source_bytes,
            requested_target_symbol=requested_target_symbol,
            requested_active_file=requested_active_file,
        )
        try:
            _assert_source_unchanged(operation, source_bytes, stage="after graph identity check")
        except _SourceLeaseChanged as exc:
            return PromotionResult(False, str(exc))
        if isinstance(graph_result, PromotionResult):
            return graph_result
        bound = graph_result
        if not _rollback_plan_is_authenticated(bound):
            rollback_fields = (
                "graph_before_statuses",
                "graph_after_statuses",
                "graph_changed_node_identities",
                "graph_before_revision",
                "graph_expected_revision",
                "rollback_plan_sha256",
            )
            if has_graph_binding or any(field in promotion for field in rollback_fields):
                return PromotionResult(
                    False,
                    "promotion graph rollback evidence is incomplete or unauthenticated",
                )
            node = blueprint.node_by_id(str(bound.get("node_id", "") or ""))
            if node is None or node.status != "false":
                return PromotionResult(
                    False,
                    "legacy promotion graph does not retain authoritative false status",
                )
            # Pre-transaction promotion rows did not record the historical
            # graph write.  Leased recovery must not invent one.  Bind the
            # freshly proven evidence to an exact idempotent transition at the
            # current revision so subsequent cleanup can roll forward safely.
            bound = _seal_rollback_plan(
                {
                    **bound,
                    "graph_before_statuses": {},
                    "graph_after_statuses": {},
                    "graph_changed_node_identities": {},
                    "graph_before_revision": blueprint.revision,
                    "graph_expected_revision": blueprint.revision,
                }
            )
    canonical = _canonicalize_promotion_record(bound, project_root)
    return PromotionResult(
        True,
        "authoritative negation evidence is current",
        node_id=str(canonical.get("node_id", "") or ""),
        is_main_goal=bool(canonical.get("is_main_goal")),
        evidence=canonical,
    )


def revalidate_promotion(
    promotion: Mapping[str, Any],
    *,
    cwd: str = "",
    requested_target_symbol: str = "",
    requested_active_file: str = "",
) -> PromotionResult:
    """Rerun exact durable evidence under one pinned source lease."""
    project_root = Path(cwd or ".").expanduser().resolve()
    exact_file = _promotion_file_identity(promotion, project_root)
    if not exact_file:
        return PromotionResult(False, "promotion lacks a usable exact source identity")
    canonical = bool(str(promotion.get("operation_path", "") or "").strip())
    try:
        with decomposition_provenance.source_operation(
            Path(exact_file), canonical=canonical
        ) as operation:
            return _revalidate_promotion_under_operation(
                promotion,
                project_root=project_root,
                operation=operation,
                requested_target_symbol=requested_target_symbol,
                requested_active_file=requested_active_file,
            )
    except OSError as exc:
        return PromotionResult(False, f"source unavailable: {str(exc)[:200]}")


def _migrate_promotion_records(
    records: list[Any], project_root: Path
) -> tuple[list[Any], dict[str, int]]:
    """Canonicalize only a wholly authenticated, unambiguous registry.

    Duplicate or legacy rows are durable evidence requiring leased recovery.
    Storage migration must never collapse them before the strict registry audit
    has had a chance to block terminal authority.
    """
    if not _audit_active_promotions(records).ok:
        return list(records), {
            "records_before": len(records),
            "records_after": len(records),
            "records_canonicalized": 0,
            "duplicates_removed": 0,
        }
    migrated: list[Any] = []
    canonicalized = 0
    for raw in records:
        if not isinstance(raw, Mapping):
            migrated.append(raw)
            continue
        original = dict(raw)
        identity = _promotion_identity(original, project_root)
        if not str(identity.get("file", "") or "") or not str(identity.get("theorem", "") or ""):
            migrated.append(original)
            continue
        normalized = _canonicalize_promotion_record(original, project_root)
        if normalized != original:
            canonicalized += 1
        migrated.append(normalized)
    return migrated, {
        "records_before": len(records),
        "records_after": len(migrated),
        "records_canonicalized": canonicalized,
        "duplicates_removed": 0,
    }


def migrate_promotion_summary(*, cwd: str = "") -> dict[str, int]:
    """Canonicalize only uniquely authenticated promotions during startup.

    This is a storage migration only: it neither reruns Lean nor changes graph
    truth, and it emits no mathematical promotion events. Legacy, malformed,
    and duplicate evidence is retained byte-for-byte for leased recovery or an
    explicit reconciliation pause.
    """
    empty = {
        "records_before": 0,
        "records_after": 0,
        "records_canonicalized": 0,
        "duplicates_removed": 0,
    }
    if not plan_state.plan_state_enabled():
        return empty
    project_root = Path(cwd or ".").expanduser().resolve()
    summary = plan_state.load_summary()
    raw_records = summary.get("negation_promotions")
    if not isinstance(raw_records, list) or not raw_records:
        return empty
    # Never normalize a legacy or ambiguous authority container.  In
    # particular, canonicalization can make two distinct legacy rows look
    # identical and a subsequent deduplication would erase the evidence that
    # must block terminal truth.  Reconcilable rows are upgraded only under a
    # pinned source/graph lease during startup recovery.
    if not _audit_active_promotions(raw_records).ok:
        return {
            "records_before": len(raw_records),
            "records_after": len(raw_records),
            "records_canonicalized": 0,
            "duplicates_removed": 0,
        }
    preview, result = _migrate_promotion_records(list(raw_records), project_root)
    if preview == raw_records:
        return result

    def mutate(current: dict[str, Any]) -> dict[str, int]:
        current_records = current.get("negation_promotions")
        if not isinstance(current_records, list):
            return empty
        if not _audit_active_promotions(current_records).ok:
            return {
                "records_before": len(current_records),
                "records_after": len(current_records),
                "records_canonicalized": 0,
                "duplicates_removed": 0,
            }
        migrated, current_result = _migrate_promotion_records(list(current_records), project_root)
        current["negation_promotions"] = migrated
        return current_result

    return update_json_file(plan_state.plan_state_paths().summary_json, mutate)


def _changed_statuses(
    before: plan_state.Blueprint, after: plan_state.Blueprint
) -> tuple[dict[str, str], dict[str, str]]:
    """Return before/after node statuses changed by one false-subtree update."""
    before_by_id = {node.id: node.status for node in before.nodes}
    after_by_id = {node.id: node.status for node in after.nodes}
    changed_ids = {
        node_id for node_id, status in before_by_id.items() if after_by_id.get(node_id) != status
    }
    return (
        {node_id: before_by_id[node_id] for node_id in sorted(changed_ids)},
        {node_id: after_by_id[node_id] for node_id in sorted(changed_ids)},
    )


def _changed_node_identities(
    blueprint: plan_state.Blueprint, changed_ids: Mapping[str, Any]
) -> dict[str, dict[str, str]]:
    """Bind every rollback-touched node id to its exact graph declaration."""
    identities: dict[str, dict[str, str]] = {}
    for node_id in sorted(str(item) for item in changed_ids):
        node = blueprint.node_by_id(node_id)
        if node is not None:
            identities[node_id] = {"name": node.name, "file": node.file}
    return identities


def _begin_promotion_transaction(
    stored: Mapping[str, Any],
    *,
    project_root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Durably record full evidence before any authoritative graph mutation."""
    prepared_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    promotion = dict(stored)
    promotion.setdefault("promoted_at", prepared_at)
    promotion_id = str(promotion.get("promotion_id", "") or "")
    transaction = {
        "transaction_id": promotion_id,
        "state": "pending",
        "prepared_at": prepared_at,
        "promotion": promotion,
    }

    def mutate(summary: dict[str, Any]) -> dict[str, Any] | None:
        promotion_audit = _audit_active_promotions(summary.get("negation_promotions"))
        if promotion_audit.unresolved:
            detail = promotion_audit.reasons[0] if promotion_audit.reasons else "unknown evidence"
            raise _GraphTransactionChanged(
                "existing negation-promotion authority requires reconciliation: " + detail
            )
        promotions, promotion_index = _active_promotion_records_for_mutation(
            summary,
            promotion_id=promotion_id,
        )
        if promotion_index is not None:
            raw_existing = promotions[promotion_index]
            assert isinstance(raw_existing, Mapping)
            return dict(raw_existing)
        transactions = _promotion_transaction_records_for_mutation(
            summary,
            transaction_id=promotion_id,
        )
        matching = [
            item
            for item in transactions
            if isinstance(item, Mapping)
            and str(item.get("transaction_id", "") or "").strip() == promotion_id
        ]
        if matching and str(matching[0].get("state", "") or "") != "pending":
            raise _GraphTransactionChanged(
                "promotion transaction already has a contradictory terminal state"
            )
        if not matching:
            transactions.append(transaction)
        summary["negation_promotion_transactions"] = _retained_promotion_transactions(transactions)
        return None

    existing = update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    return transaction, existing


def _finalize_promotion_transaction(
    transaction: Mapping[str, Any], *, project_root: Path
) -> tuple[dict[str, Any], bool]:
    """Atomically expose promotion evidence and mark its transaction committed."""
    promotion = _canonicalize_promotion_record(
        dict(transaction.get("promotion") or {}), project_root
    )
    promotion_id = str(promotion.get("promotion_id", "") or "")
    transaction_id = str(transaction.get("transaction_id", "") or promotion_id)
    committed_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    def mutate(summary: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        promotions, promotion_index = _active_promotion_records_for_mutation(
            summary,
            promotion_id=promotion_id,
        )
        if promotion_index is None:
            promotions.append(promotion)
            committed = promotion
            created = True
        else:
            raw_existing = promotions[promotion_index]
            assert isinstance(raw_existing, Mapping)
            committed = dict(raw_existing)
            created = False
        summary["negation_promotions"] = promotions
        transactions = _promotion_transaction_records_for_mutation(
            summary,
            transaction_id=transaction_id,
        )
        updated = False
        for index, item in enumerate(transactions):
            if (
                not isinstance(item, Mapping)
                or str(item.get("transaction_id", "") or "").strip() != transaction_id
            ):
                continue
            if str(item.get("state", "") or "") not in {"pending", "committed"}:
                raise _GraphTransactionChanged(
                    "promotion transaction cannot commit from its durable state"
                )
            transactions[index] = {
                **dict(item),
                "state": "committed",
                "committed_at": committed_at,
                "promotion": committed,
            }
            updated = True
        if not updated:
            transactions.append(
                {
                    **dict(transaction),
                    "state": "committed",
                    "committed_at": committed_at,
                    "promotion": committed,
                }
            )
        summary["negation_promotion_transactions"] = _retained_promotion_transactions(transactions)
        quarantine_records, quarantine_index = _quarantine_pending_records_for_mutation(
            summary,
            target_id=transaction_id or promotion_id,
            project_root=project_root,
        )
        if quarantine_index is not None:
            quarantine_records.pop(quarantine_index)
        summary["negation_promotion_quarantine_pending"] = quarantine_records
        return committed, created

    committed, created = update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    return dict(committed), bool(created)


def _upgrade_committed_promotion(
    original: Mapping[str, Any],
    upgraded: Mapping[str, Any],
    *,
    project_root: Path,
) -> dict[str, Any]:
    """Atomically replace leased legacy authority and seal its commit record."""
    original_id = str(original.get("promotion_id", "") or "")
    canonical = _canonicalize_promotion_record(upgraded, project_root)
    canonical_id = str(canonical.get("promotion_id", "") or "")
    committed_at = datetime.now(UTC).replace(microsecond=0).isoformat()

    def mutate(summary: dict[str, Any]) -> dict[str, Any]:
        records, record_index = _active_promotion_records_for_mutation(
            summary,
            promotion_id=original_id,
            allow_reconcilable=True,
        )
        if record_index is None:
            raise RuntimeError("committed promotion identity changed during lease")
        if _audit_active_promotions([canonical]).active != 1:
            raise RuntimeError("upgraded promotion is not fully authenticated")
        records[record_index] = canonical
        summary["negation_promotions"] = records

        raw_transactions = summary.get("negation_promotion_transactions")
        if raw_transactions is None:
            transactions: list[object] = []
            transaction_audit = _audit_promotion_transactions(transactions)
        elif isinstance(raw_transactions, list):
            transactions = list(raw_transactions)
            transaction_audit = _audit_promotion_transactions(raw_transactions)
        else:
            raise RuntimeError("negation-promotion transaction registry is not a list")
        matching_transactions: list[int] = []
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
                canonical_id,
            }:
                matching_transactions.append(index)
        if len(matching_transactions) > 1:
            raise RuntimeError("committed promotion transaction identity is duplicated")
        prepared_at = str(canonical.get("promoted_at", "") or committed_at)
        committed_transaction = {
            "transaction_id": canonical_id,
            "state": "committed",
            "prepared_at": prepared_at,
            "committed_at": committed_at,
            "promotion": canonical,
        }
        if matching_transactions:
            transaction_index = matching_transactions[0]
            transaction_record = transaction_audit.records[transaction_index]
            raw_transaction = transactions[transaction_index]
            if (
                transaction_record.disposition != "terminal"
                or not isinstance(raw_transaction, Mapping)
                or str(raw_transaction.get("state", "") or "") != "committed"
            ):
                raise RuntimeError("committed promotion transaction is unauthenticated")
            committed_transaction["prepared_at"] = str(
                raw_transaction.get("prepared_at", "") or prepared_at
            )
            committed_transaction["committed_at"] = str(
                raw_transaction.get("committed_at", "") or committed_at
            )
            transactions[transaction_index] = committed_transaction
        else:
            transactions.append(committed_transaction)
        candidate_audit = _audit_promotion_transactions([committed_transaction])
        if not candidate_audit.records or candidate_audit.records[0].disposition != "terminal":
            reason = candidate_audit.records[0].reason if candidate_audit.records else ""
            raise RuntimeError(
                "upgraded promotion transaction is unauthenticated"
                + (f": {reason}" if reason else "")
            )
        summary["negation_promotion_transactions"] = _retained_promotion_transactions(transactions)
        return canonical

    return dict(update_json_file(plan_state.plan_state_paths().summary_json, mutate))


def _restore_transaction_graph(promotion: Mapping[str, Any]) -> bool:
    """Roll back only graph statuses still equal to this transaction's writes."""
    if not _rollback_plan_is_authenticated(promotion):
        return False
    before = dict(promotion.get("graph_before_statuses") or {})
    after = dict(promotion.get("graph_after_statuses") or {})
    identities = dict(promotion.get("graph_changed_node_identities") or {})
    blueprint = plan_state.load_blueprint()
    restored = blueprint
    if "graph_before_statuses" in promotion:
        try:
            expected_revision = int(promotion.get("graph_expected_revision", -1) or -1)
        except (TypeError, ValueError):
            expected_revision = -1
        node_id = str(promotion.get("node_id", "") or "")
        restore_before = (
            before
            if expected_revision == blueprint.revision
            else ({node_id: before[node_id]} if node_id in before else {})
        )
        for node_id, prior_status in restore_before.items():
            node = restored.node_by_id(str(node_id))
            expected = str(after.get(node_id, "") or "")
            identity = identities.get(str(node_id))
            identity_matches = (
                isinstance(identity, Mapping)
                and node is not None
                and node.name == str(identity.get("name", "") or "")
                and node.file == str(identity.get("file", "") or "")
            )
            if identity_matches and node is not None and (not expected or node.status == expected):
                restored = restored.replace_node(replace(node, status=str(prior_status)))
    else:
        node_id = str(promotion.get("node_id", "") or "")
        node = restored.node_by_id(node_id)
        if node is not None and node.status == "false":
            restored = restored.replace_node(replace(node, status="stated"))
    if restored == blueprint:
        return False
    plan_state.save_blueprint(restored)
    return True


def _graph_reflects_rollback(
    promotion: Mapping[str, Any],
    *,
    expected_revision: int,
    operation: decomposition_provenance.SourceOperation,
    project_root: Path,
) -> bool:
    """Return whether every authenticated transaction write is now reopened."""
    if not _rollback_plan_is_authenticated(promotion):
        return False
    before = _normalized_statuses(promotion.get("graph_before_statuses"))
    identities = dict(promotion.get("graph_changed_node_identities") or {})
    blueprint = plan_state.load_blueprint()
    if blueprint.revision != expected_revision:
        return False
    if not _graph_restore_is_safe(
        promotion,
        blueprint=blueprint,
        project_root=project_root,
        operation=operation,
    ):
        return False
    for node_id, prior_status in before.items():
        node = blueprint.node_by_id(node_id)
        identity = identities.get(node_id)
        if (
            node is None
            or not isinstance(identity, Mapping)
            or node.name != str(identity.get("name", "") or "")
            or node.file != str(identity.get("file", "") or "")
            or node.status != prior_status
        ):
            return False
    return True


def _graph_reflects_promotion_write(
    promotion: Mapping[str, Any],
    *,
    blueprint: plan_state.Blueprint,
    operation: decomposition_provenance.SourceOperation,
    project_root: Path,
    source_bytes: bytes,
    requested_target_symbol: str = "",
    requested_active_file: str = "",
) -> bool:
    """Return whether the graph exactly reflects one pending promotion write."""
    if not _rollback_plan_is_authenticated(promotion):
        return False
    try:
        expected_revision = int(promotion.get("graph_expected_revision", -1))
    except (TypeError, ValueError):
        return False
    if blueprint.revision != expected_revision:
        return False
    binding = _validate_or_upgrade_graph_binding(
        promotion,
        blueprint=blueprint,
        project_root=project_root,
        operation=operation,
        source_bytes=source_bytes,
        requested_target_symbol=requested_target_symbol,
        requested_active_file=requested_active_file,
    )
    if isinstance(binding, PromotionResult):
        return False
    after = _normalized_statuses(promotion.get("graph_after_statuses"))
    identities = dict(promotion.get("graph_changed_node_identities") or {})
    for node_id, expected_status in after.items():
        node = blueprint.node_by_id(node_id)
        identity = identities.get(node_id)
        if (
            node is None
            or not isinstance(identity, Mapping)
            or node.name != str(identity.get("name", "") or "")
            or node.file != str(identity.get("file", "") or "")
            or node.status != expected_status
        ):
            return False
    return True


def _retain_quarantine_pending(
    promotion: Mapping[str, Any],
    *,
    reason: str,
    project_root: Path,
    transaction_id: str,
) -> None:
    """Keep active authority replayable when graph rollback cannot be sealed."""
    canonical = _canonicalize_promotion_record(promotion, project_root)
    promotion_id = str(canonical.get("promotion_id", "") or "")
    pending = {
        **canonical,
        "state": "pending-graph-reconciliation",
        "reason": str(reason),
        "transaction_id": transaction_id or promotion_id,
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }

    def mutate(summary: dict[str, Any]) -> None:
        records, existing_index = _quarantine_pending_records_for_mutation(
            summary,
            target_id=transaction_id or promotion_id,
            project_root=project_root,
        )
        if existing_index is None:
            records.append(pending)
        else:
            records[existing_index] = pending
        summary["negation_promotion_quarantine_pending"] = records
        report = summary.get("final_report")
        if bool(canonical.get("is_main_goal")) and isinstance(report, Mapping):
            if report.get("status") == "disproved":
                summary.pop("final_report", None)
        target_id = transaction_id or promotion_id
        transactions = _promotion_transaction_records_for_mutation(
            summary,
            transaction_id=target_id,
        )
        for index, item in enumerate(transactions):
            if (
                not isinstance(item, Mapping)
                or str(item.get("transaction_id", "") or "").strip() != target_id
            ):
                continue
            transactions[index] = {
                **dict(item),
                "quarantine_pending_reason": str(reason),
            }
        summary["negation_promotion_transactions"] = _retained_promotion_transactions(transactions)

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)


def _promotion_without_quarantine_envelope(promotion: Mapping[str, Any]) -> dict[str, Any]:
    """Return mathematical promotion fields from a replay envelope."""
    stored = dict(promotion)
    for field in (
        "state",
        "reason",
        "transaction_id",
        "updated_at",
        "quarantined_at",
        "quarantine_pending_reason",
    ):
        stored.pop(field, None)
    return stored


def _quarantine_promotion(
    promotion: Mapping[str, Any],
    *,
    reason: str,
    project_root: Path,
    transaction_id: str = "",
    restore_graph: bool = True,
    operation: decomposition_provenance.SourceOperation | None = None,
    source_bytes: bytes | None = None,
) -> bool:
    """Remove stale authority while retaining a complete forensic record."""
    source_promotion_id = str(promotion.get("promotion_id", "") or "").strip()
    canonical = _canonicalize_promotion_record(
        _promotion_without_quarantine_envelope(promotion), project_root
    )
    promotion_id = str(canonical.get("promotion_id", "") or "")
    quarantined_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    quarantine = {
        **canonical,
        "reason": str(reason or "promotion evidence is stale"),
        "quarantined_at": quarantined_at,
        "transaction_id": transaction_id or promotion_id,
    }
    # Reopen the graph first. If the process dies at this boundary, the stale
    # summary evidence remains available for the next startup revalidation;
    # the unsafe inverse ordering could remove the only replay record while a
    # false graph node survived.
    rollback_revision = -1
    if restore_graph:
        if operation is None or source_bytes is None:
            raise _SourceLeaseChanged(
                "cannot restore promotion graph without its exact source lease"
            )
        _assert_source_unchanged(
            operation, source_bytes, stage="immediately before quarantine graph restoration"
        )
        _restore_transaction_graph(canonical)
        _assert_source_unchanged(
            operation, source_bytes, stage="immediately after quarantine graph restoration"
        )
        rollback_revision = plan_state.load_blueprint().revision
        _promotion_transaction_hook("quarantine-graph-persisted")

    def mutate(summary: dict[str, Any]) -> None:
        target_id = transaction_id or source_promotion_id or promotion_id
        transactions = _promotion_transaction_records_for_mutation(
            summary,
            transaction_id=target_id,
        )
        matching_transaction_indexes = [
            index
            for index, item in enumerate(transactions)
            if isinstance(item, Mapping)
            and target_id
            in {
                str(item.get("transaction_id", "") or "").strip(),
                (
                    str((item.get("promotion") or {}).get("promotion_id", "") or "").strip()
                    if isinstance(item.get("promotion"), Mapping)
                    else ""
                ),
            }
        ]
        if len(matching_transaction_indexes) > 1:
            raise _GraphTransactionChanged(
                "negation-promotion transaction target is duplicated before quarantine"
            )
        active_records, active_index = _active_promotion_records_for_mutation(
            summary,
            promotion_id=source_promotion_id or promotion_id,
            allow_reconcilable=True,
        )
        if active_index is None and not matching_transaction_indexes:
            raise _GraphTransactionChanged(
                "negation-promotion authority disappeared before quarantine"
            )
        if active_index is not None:
            active_records.pop(active_index)
        summary["negation_promotions"] = active_records
        if bool(canonical.get("is_main_goal")):
            report = summary.get("final_report")
            if isinstance(report, Mapping) and report.get("status") == "disproved":
                summary.pop("final_report", None)
        updated = False
        for index, item in enumerate(transactions):
            if (
                not isinstance(item, Mapping)
                or str(item.get("transaction_id", "") or "").strip() != target_id
            ):
                continue
            transactions[index] = {
                **dict(item),
                "state": "quarantined",
                "reason": str(reason or "promotion evidence is stale"),
                "quarantined_at": quarantined_at,
            }
            updated = True
        if transaction_id and not updated:
            transactions.append(
                {
                    "transaction_id": transaction_id,
                    "state": "quarantined",
                    "reason": str(reason or "promotion evidence is stale"),
                    "quarantined_at": quarantined_at,
                    "promotion": canonical,
                }
            )
        summary["negation_promotion_transactions"] = _retained_promotion_transactions(transactions)
        quarantine_records, quarantine_index = _promotion_quarantine_records_for_mutation(
            summary,
            promotion_id=promotion_id,
        )
        if quarantine_index is None:
            quarantine_records.append(quarantine)
        else:
            quarantine_records[quarantine_index] = quarantine
        summary["negation_promotion_quarantine"] = _audit_promotion_quarantines(
            quarantine_records
        ).retained_registry
        quarantine_pending, quarantine_pending_index = _quarantine_pending_records_for_mutation(
            summary,
            target_id=transaction_id or promotion_id,
            project_root=project_root,
        )
        if quarantine_pending_index is not None:
            quarantine_pending.pop(quarantine_pending_index)
        summary["negation_promotion_quarantine_pending"] = quarantine_pending

    with plan_state.blueprint_commit_guard():
        if operation is not None and source_bytes is not None:
            _assert_source_unchanged(
                operation, source_bytes, stage="before quarantine transaction finalization"
            )
        if (
            not restore_graph
            or operation is None
            or not _graph_reflects_rollback(
                canonical,
                expected_revision=rollback_revision,
                operation=operation,
                project_root=project_root,
            )
        ):
            pending_reason = (
                f"{str(reason or 'promotion evidence is stale')}; "
                "graph rollback requires reconciliation"
            )
            _retain_quarantine_pending(
                canonical,
                reason=pending_reason,
                project_root=project_root,
                transaction_id=transaction_id,
            )
            return False
        update_json_file(plan_state.plan_state_paths().summary_json, mutate)
        if operation is not None and source_bytes is not None:
            _assert_source_unchanged(
                operation, source_bytes, stage="after quarantine transaction finalization"
            )
    with contextlib.suppress(Exception):
        plan_state.append_journal_event(
            {
                "event": "negation-promotion-quarantined",
                "node_id": str(canonical.get("node_id", "") or ""),
                "name": str(canonical.get("theorem", "") or ""),
                "file": str(canonical.get("file", "") or ""),
                "is_main_goal": bool(canonical.get("is_main_goal")),
                "promotion_id": promotion_id,
                "reason": str(reason or "promotion evidence is stale"),
            }
        )
    if operation is not None and source_bytes is not None:
        _assert_source_unchanged(operation, source_bytes, stage="after quarantine journal")
    with contextlib.suppress(Exception):
        append_workflow_activity(
            "negation-promotion-quarantined",
            f"Quarantined stale negation evidence for {canonical.get('theorem', '[unknown]')}",
            reason=str(reason or "promotion evidence is stale"),
            promotion=canonical,
        )
    if operation is not None and source_bytes is not None:
        _assert_source_unchanged(operation, source_bytes, stage="after quarantine activity")
    return True


def recover_promotion_transactions(
    *,
    cwd: str = "",
    requested_target_symbol: str = "",
    requested_active_file: str = "",
) -> dict[str, int]:
    """Replay or quarantine every durable pending promotion transaction."""
    result = {"pending": 0, "committed": 0, "quarantined": 0}
    if not plan_state.plan_state_enabled():
        return result
    project_root = Path(cwd or ".").expanduser().resolve()
    raw_transactions = plan_state.load_summary().get("negation_promotion_transactions")
    transaction_audit = _audit_promotion_transactions(raw_transactions)
    result["pending"] = transaction_audit.pending
    transactions = (
        [
            dict(raw_transactions[record.index])
            for record in transaction_audit.records
            if record.disposition == "live" and isinstance(raw_transactions[record.index], Mapping)
        ]
        if isinstance(raw_transactions, list)
        else []
    )
    for transaction in transactions:
        promotion = dict(transaction.get("promotion") or {})
        transaction_id = str(transaction.get("transaction_id", "") or "")
        exact_file = _promotion_file_identity(promotion, project_root)
        if not exact_file:
            _quarantine_promotion(
                promotion,
                reason="pending promotion lacks a usable exact source identity",
                project_root=project_root,
                transaction_id=transaction_id,
                restore_graph=False,
            )
            result["quarantined"] += 1
            continue
        canonical = bool(str(promotion.get("operation_path", "") or "").strip())
        try:
            operation_context = decomposition_provenance.source_operation(
                Path(exact_file), canonical=canonical
            )
            operation = operation_context.__enter__()
        except OSError as exc:
            _quarantine_promotion(
                promotion,
                reason=f"pending promotion source unavailable: {str(exc)[:200]}",
                project_root=project_root,
                transaction_id=transaction_id,
                restore_graph=False,
            )
            result["quarantined"] += 1
            continue
        try:
            source_bytes = decomposition_provenance.read_source_bytes(operation)
            validation = _revalidate_promotion_under_operation(
                promotion,
                project_root=project_root,
                operation=operation,
                requested_target_symbol=requested_target_symbol,
                requested_active_file=requested_active_file,
            )
            if not validation.ok:
                restore_graph = _graph_restore_is_safe(
                    promotion,
                    blueprint=plan_state.load_blueprint(),
                    project_root=project_root,
                    operation=operation,
                )
                _quarantine_promotion(
                    promotion,
                    reason=validation.reason,
                    project_root=project_root,
                    transaction_id=transaction_id,
                    operation=operation,
                    source_bytes=source_bytes,
                    restore_graph=restore_graph,
                )
                result["quarantined"] += 1
                continue
            authoritative = dict(validation.evidence or promotion)
            _assert_source_unchanged(
                operation, source_bytes, stage="before recovered graph mutation"
            )
            blueprint = plan_state.load_blueprint()
            rebound = _validate_or_upgrade_graph_binding(
                authoritative,
                blueprint=blueprint,
                project_root=project_root,
                operation=operation,
                source_bytes=source_bytes,
                requested_target_symbol=requested_target_symbol,
                requested_active_file=requested_active_file,
            )
            if isinstance(rebound, PromotionResult):
                restore_graph = _graph_restore_is_safe(
                    authoritative,
                    blueprint=blueprint,
                    project_root=project_root,
                    operation=operation,
                )
                _quarantine_promotion(
                    authoritative,
                    reason=rebound.reason,
                    project_root=project_root,
                    transaction_id=transaction_id,
                    operation=operation,
                    source_bytes=source_bytes,
                    restore_graph=restore_graph,
                )
                result["quarantined"] += 1
                continue
            authoritative = _canonicalize_promotion_record(rebound, project_root)
            node_id = str(authoritative.get("node_id", "") or "")
            promoted = blueprint.invalidate_false_subtree(node_id)
            if promoted != blueprint:
                _assert_source_unchanged(
                    operation, source_bytes, stage="immediately before recovered graph mutation"
                )
                plan_state.save_blueprint(promoted)
                _assert_source_unchanged(
                    operation, source_bytes, stage="immediately after recovered graph mutation"
                )
            recovered_transaction = {
                **transaction,
                "promotion": authoritative,
            }
            with plan_state.blueprint_commit_guard():
                _assert_source_unchanged(
                    operation, source_bytes, stage="before recovered transaction finalization"
                )
                final_blueprint = plan_state.load_blueprint()
                if not _graph_reflects_promotion_write(
                    authoritative,
                    blueprint=final_blueprint,
                    operation=operation,
                    project_root=project_root,
                    source_bytes=source_bytes,
                    requested_target_symbol=requested_target_symbol,
                    requested_active_file=requested_active_file,
                ):
                    raise _GraphTransactionChanged(
                        "dependency graph changed before recovered promotion finalization"
                    )
                committed, created = _finalize_promotion_transaction(
                    recovered_transaction, project_root=project_root
                )
                _assert_source_unchanged(
                    operation, source_bytes, stage="after recovered transaction finalization"
                )
        except _SourceLeaseChanged as exc:
            latest_bytes = decomposition_provenance.read_source_bytes(operation)
            restore_graph = _graph_restore_is_safe(
                promotion,
                blueprint=plan_state.load_blueprint(),
                project_root=project_root,
                operation=operation,
            )
            _quarantine_promotion(
                promotion,
                reason=str(exc),
                project_root=project_root,
                transaction_id=transaction_id,
                operation=operation,
                source_bytes=latest_bytes,
                restore_graph=restore_graph,
            )
            result["quarantined"] += 1
            continue
        except OSError as exc:
            _quarantine_promotion(
                promotion,
                reason=f"source unavailable during transaction recovery: {str(exc)[:200]}",
                project_root=project_root,
                transaction_id=transaction_id,
                restore_graph=False,
            )
            result["quarantined"] += 1
            continue
        except _GraphTransactionChanged as exc:
            restore_graph = _graph_restore_is_safe(
                promotion,
                blueprint=plan_state.load_blueprint(),
                project_root=project_root,
                operation=operation,
            )
            _quarantine_promotion(
                promotion,
                reason=str(exc),
                project_root=project_root,
                transaction_id=transaction_id,
                operation=operation,
                source_bytes=source_bytes,
                restore_graph=restore_graph,
            )
            result["quarantined"] += 1
            continue
        finally:
            operation_context.__exit__(None, None, None)
        if created:
            with contextlib.suppress(Exception):
                plan_state.append_journal_event(
                    {
                        "event": "negation-promoted",
                        "node_id": node_id,
                        "name": str(committed.get("theorem", "") or ""),
                        "file": str(committed.get("file", "") or ""),
                        "is_main_goal": bool(committed.get("is_main_goal")),
                        "promotion_id": str(committed.get("promotion_id", "") or ""),
                        "recovered_transaction": True,
                    }
                )
        result["committed"] += 1
    return result


def recover_promotion_quarantines(*, cwd: str = "") -> dict[str, int]:
    """Finish replayable graph rollback quarantines under exact source leases."""
    result = {"pending": 0, "resolved": 0}
    if not plan_state.plan_state_enabled():
        return result
    project_root = Path(cwd or ".").expanduser().resolve()
    records = [
        dict(item)
        for item in (plan_state.load_summary().get("negation_promotion_quarantine_pending") or [])
        if isinstance(item, Mapping)
    ]
    result["pending"] = len(records)
    for record in records:
        exact_file = _promotion_file_identity(record, project_root)
        if not exact_file or not str(record.get("operation_path", "") or "").strip():
            continue
        try:
            with decomposition_provenance.source_operation(
                Path(exact_file), canonical=True
            ) as operation:
                source_bytes = decomposition_provenance.read_source_bytes(operation)
                blueprint = plan_state.load_blueprint()
                if not _graph_restore_is_safe(
                    record,
                    blueprint=blueprint,
                    project_root=project_root,
                    operation=operation,
                ):
                    continue
                resolved = _quarantine_promotion(
                    record,
                    reason=str(record.get("reason", "") or "promotion quarantine replay"),
                    project_root=project_root,
                    transaction_id=str(record.get("transaction_id", "") or ""),
                    operation=operation,
                    source_bytes=source_bytes,
                )
                if resolved:
                    result["resolved"] += 1
        except (OSError, _SourceLeaseChanged):
            continue
    return result


def reconcile_promotions_on_startup(
    *, cwd: str = "", target_symbol: str = "", active_file: str = ""
) -> PromotionReconciliation:
    """Recover transactions and revalidate every immutable requested-root disproof.

    ``target_symbol`` and ``active_file`` remain compatibility/telemetry inputs;
    mutable current assignment state never narrows mathematical authority.
    """
    if not plan_state.plan_state_enabled():
        return PromotionReconciliation()
    project_root = Path(cwd or ".").expanduser().resolve()
    quarantine_recovery = recover_promotion_quarantines(cwd=str(project_root))
    recovery = recover_promotion_transactions(cwd=str(project_root))
    raw_promotions = plan_state.load_summary().get("negation_promotions")
    promotion_audit = _audit_active_promotions(raw_promotions)
    promotions = (
        [
            dict(raw_promotions[index])
            for index in promotion_audit.selectable_indexes
            if isinstance(raw_promotions[index], Mapping)
        ]
        if isinstance(raw_promotions, list)
        else []
    )
    cleanup_candidates = [
        promotion
        for promotion in promotions
        # A record that claims the sealed requested-scope basis must first go
        # through scoped revalidation below.  If the campaign marker or root
        # registry was damaged, treating that record as a helper here could
        # let later mutable split/provenance metadata delete the requested
        # theorem source before the authority failure is quarantined.
        if not _promotion_claims_manifest_main(promotion)
    ]
    cleanup = false_decomposition_cleanup.reconcile_false_decompositions(
        cleanup_candidates,
        cwd=str(project_root),
        validate_promotion=lambda candidate: revalidate_promotion(candidate, cwd=str(project_root)),
    )
    raw_promotions = plan_state.load_summary().get("negation_promotions")
    promotion_audit = _audit_active_promotions(raw_promotions)
    promotions = (
        [
            dict(raw_promotions[index])
            for index in promotion_audit.selectable_indexes
            if isinstance(raw_promotions[index], Mapping)
        ]
        if isinstance(raw_promotions, list)
        else []
    )
    # The current queue assignment is mutable scheduling state, not requested
    # scope authority. Revalidate every stored main candidate against the
    # immutable full campaign-root registry.
    scoped = [promotion for promotion in promotions if bool(promotion.get("is_main_goal"))]
    quarantined = int(recovery["quarantined"]) + int(quarantine_recovery["resolved"])
    retryable_promotion_reasons: list[str] = []
    for promotion in reversed(scoped):
        exact_file = _promotion_file_identity(promotion, project_root)
        if not exact_file:
            _quarantine_promotion(
                promotion,
                reason="committed promotion lacks a usable exact source identity",
                project_root=project_root,
                restore_graph=False,
            )
            quarantined += 1
            continue
        canonical = bool(str(promotion.get("operation_path", "") or "").strip())
        try:
            operation_context = decomposition_provenance.source_operation(
                Path(exact_file), canonical=canonical
            )
            operation = operation_context.__enter__()
        except OSError as exc:
            _quarantine_promotion(
                promotion,
                reason=f"committed promotion source unavailable: {str(exc)[:200]}",
                project_root=project_root,
                restore_graph=False,
            )
            quarantined += 1
            continue
        try:
            source_bytes = decomposition_provenance.read_source_bytes(operation)
            validation = _revalidate_promotion_under_operation(
                promotion,
                project_root=project_root,
                operation=operation,
            )
            if not validation.ok:
                if validation.retryable:
                    retryable_promotion_reasons.append(
                        f"{validation.failure_kind or 'infrastructure_unavailable'}: "
                        f"{validation.reason}"
                    )
                    continue
                restore_graph = _graph_restore_is_safe(
                    promotion,
                    blueprint=plan_state.load_blueprint(),
                    project_root=project_root,
                    operation=operation,
                )
                _quarantine_promotion(
                    promotion,
                    reason=validation.reason,
                    project_root=project_root,
                    operation=operation,
                    source_bytes=source_bytes,
                    restore_graph=restore_graph,
                )
                quarantined += 1
                continue
            authoritative = dict(validation.evidence or promotion)
            if authoritative != promotion:
                _assert_source_unchanged(
                    operation, source_bytes, stage="before committed identity upgrade"
                )
                authoritative = _upgrade_committed_promotion(
                    promotion,
                    authoritative,
                    project_root=project_root,
                )
                _assert_source_unchanged(
                    operation, source_bytes, stage="after committed identity upgrade"
                )
            _assert_source_unchanged(
                operation, source_bytes, stage="before startup graph identity check"
            )
            blueprint = plan_state.load_blueprint()
            rebound = _validate_or_upgrade_graph_binding(
                authoritative,
                blueprint=blueprint,
                project_root=project_root,
                operation=operation,
                source_bytes=source_bytes,
            )
            if isinstance(rebound, PromotionResult):
                restore_graph = _graph_restore_is_safe(
                    authoritative,
                    blueprint=blueprint,
                    project_root=project_root,
                    operation=operation,
                )
                _quarantine_promotion(
                    authoritative,
                    reason=rebound.reason,
                    project_root=project_root,
                    operation=operation,
                    source_bytes=source_bytes,
                    restore_graph=restore_graph,
                )
                quarantined += 1
                continue
            authoritative = _canonicalize_promotion_record(rebound, project_root)
            if not bool(authoritative.get("is_main_goal")):
                # The negation remains valid mathematical evidence for this
                # sublemma, but it cannot terminate the enclosing theorem.
                continue
            node_id = str(authoritative.get("node_id", "") or "")
            promoted = blueprint.invalidate_false_subtree(node_id)
            if promoted != blueprint:
                _assert_source_unchanged(
                    operation, source_bytes, stage="immediately before startup graph mutation"
                )
                plan_state.save_blueprint(promoted)
                _assert_source_unchanged(
                    operation, source_bytes, stage="immediately after startup graph mutation"
                )
            with plan_state.blueprint_commit_guard():
                _assert_source_unchanged(
                    operation, source_bytes, stage="before terminal disproof result"
                )
                final_blueprint = plan_state.load_blueprint()
                final_binding = _validate_or_upgrade_graph_binding(
                    authoritative,
                    blueprint=final_blueprint,
                    project_root=project_root,
                    operation=operation,
                    source_bytes=source_bytes,
                )
                if isinstance(final_binding, PromotionResult) or not bool(
                    final_binding.get("is_main_goal")
                ):
                    reason = (
                        final_binding.reason
                        if isinstance(final_binding, PromotionResult)
                        else "promotion main/helper classification changed before terminal result"
                    )
                    _retain_quarantine_pending(
                        authoritative,
                        reason=reason,
                        project_root=project_root,
                        transaction_id=str(authoritative.get("promotion_id", "") or ""),
                    )
                    continue
                authoritative = _canonicalize_promotion_record(final_binding, project_root)
                _assert_source_unchanged(
                    operation, source_bytes, stage="at terminal disproof result"
                )
                promotion_pending, promotion_reasons = _promotion_pending_state()
                promotion_pending += len(retryable_promotion_reasons)
                promotion_reasons = tuple(
                    dict.fromkeys([*retryable_promotion_reasons, *promotion_reasons])
                )
                if promotion_pending:
                    continue
                return PromotionReconciliation(
                    terminal_disproof=True,
                    promotion=authoritative,
                    committed=int(recovery["committed"]),
                    quarantined=quarantined,
                    decompositions_cleaned=cleanup.cleaned,
                    cleanup_pending=cleanup.pending,
                    cleanup_quarantined=cleanup.quarantined,
                    cleanup_reasons=cleanup.reasons,
                    promotion_pending=promotion_pending,
                    promotion_reasons=promotion_reasons,
                )
        except _SourceLeaseChanged as exc:
            latest_bytes = decomposition_provenance.read_source_bytes(operation)
            restore_graph = _graph_restore_is_safe(
                promotion,
                blueprint=plan_state.load_blueprint(),
                project_root=project_root,
                operation=operation,
            )
            _quarantine_promotion(
                promotion,
                reason=str(exc),
                project_root=project_root,
                operation=operation,
                source_bytes=latest_bytes,
                restore_graph=restore_graph,
            )
            quarantined += 1
        except OSError as exc:
            _quarantine_promotion(
                promotion,
                reason=f"source unavailable during startup reconciliation: {str(exc)[:200]}",
                project_root=project_root,
                restore_graph=False,
            )
            quarantined += 1
        finally:
            operation_context.__exit__(None, None, None)
    promotion_pending, promotion_reasons = _promotion_pending_state()
    promotion_pending += len(retryable_promotion_reasons)
    promotion_reasons = tuple(dict.fromkeys([*retryable_promotion_reasons, *promotion_reasons]))
    return PromotionReconciliation(
        committed=int(recovery["committed"]),
        quarantined=quarantined,
        decompositions_cleaned=cleanup.cleaned,
        cleanup_pending=cleanup.pending,
        cleanup_quarantined=cleanup.quarantined,
        cleanup_reasons=cleanup.reasons,
        promotion_pending=promotion_pending,
        promotion_reasons=promotion_reasons,
    )


def _commit_promotion(
    *,
    theorem_id: str,
    file_label: str,
    promotion: Mapping[str, Any],
    project_root: Path,
    operation: decomposition_provenance.SourceOperation,
    source_bytes: bytes,
    requested_target_symbol: str = "",
    requested_active_file: str = "",
) -> PromotionResult:
    """Commit evidence and graph falsity through a restart-safe transaction."""
    if not plan_state.plan_state_enabled():
        return PromotionResult(False, "plan-state graph is required for authoritative promotion")
    promotion_audit = _audit_active_promotions(plan_state.load_summary().get("negation_promotions"))
    if promotion_audit.unresolved:
        detail = promotion_audit.reasons[0] if promotion_audit.reasons else "unknown evidence"
        return PromotionResult(
            False,
            "existing negation-promotion authority requires reconciliation: " + detail,
        )
    canonical_file = str(operation.path)
    _assert_source_unchanged(operation, source_bytes, stage="before graph selection")
    blueprint = plan_state.load_blueprint()
    _assert_source_unchanged(operation, source_bytes, stage="after graph selection read")
    node_result = _unique_graph_node_for_new_promotion(
        blueprint,
        theorem=theorem_id,
        file_label=file_label,
        project_root=project_root,
        operation=operation,
    )
    if isinstance(node_result, PromotionResult):
        return node_result
    node = node_result
    node_id = node.id
    bound = _bind_graph_identity(
        {**dict(promotion), "theorem": theorem_id},
        blueprint=blueprint,
        node=node,
        project_root=project_root,
        operation=operation,
        source_bytes=source_bytes,
        requested_target_symbol=requested_target_symbol,
        requested_active_file=requested_active_file,
    )
    if isinstance(bound, PromotionResult):
        return bound
    is_main_goal = bool(bound["is_main_goal"])
    promoted = blueprint.invalidate_false_subtree(node_id)
    graph_before_statuses, graph_after_statuses = _changed_statuses(blueprint, promoted)
    stored = _canonicalize_promotion_record(
        _seal_rollback_plan(
            {
                **bound,
                "graph_before_statuses": graph_before_statuses,
                "graph_after_statuses": graph_after_statuses,
                "graph_changed_node_identities": _changed_node_identities(
                    blueprint, graph_before_statuses
                ),
                "graph_before_revision": blueprint.revision,
                "graph_expected_revision": blueprint.revision + (1 if promoted != blueprint else 0),
            }
        ),
        project_root,
    )
    _assert_source_unchanged(operation, source_bytes, stage="before transaction preparation")
    try:
        transaction, existing = _begin_promotion_transaction(stored, project_root=project_root)
    except _GraphTransactionChanged as exc:
        return PromotionResult(False, str(exc))
    _assert_source_unchanged(operation, source_bytes, stage="after transaction preparation")
    if existing is not None:
        existing_binding = _validate_or_upgrade_graph_binding(
            existing,
            blueprint=blueprint,
            project_root=project_root,
            operation=operation,
            source_bytes=source_bytes,
            requested_target_symbol=requested_target_symbol,
            requested_active_file=requested_active_file,
        )
        if isinstance(existing_binding, PromotionResult):
            return existing_binding
        existing = _canonicalize_promotion_record(existing_binding, project_root)
        if promoted != blueprint:
            _assert_source_unchanged(operation, source_bytes, stage="before graph mutation")
            plan_state.save_blueprint(promoted)
            _assert_source_unchanged(operation, source_bytes, stage="after graph mutation")
        with plan_state.blueprint_commit_guard():
            _assert_source_unchanged(
                operation, source_bytes, stage="before idempotent promotion result"
            )
            final_blueprint = plan_state.load_blueprint()
            final_binding = _validate_or_upgrade_graph_binding(
                existing,
                blueprint=final_blueprint,
                project_root=project_root,
                operation=operation,
                source_bytes=source_bytes,
                requested_target_symbol=requested_target_symbol,
                requested_active_file=requested_active_file,
            )
            graph_is_false = final_blueprint.invalidate_false_subtree(node_id) == final_blueprint
            if isinstance(final_binding, PromotionResult) or not graph_is_false:
                reason = (
                    final_binding.reason
                    if isinstance(final_binding, PromotionResult)
                    else "dependency graph changed before idempotent promotion result"
                )
                _retain_quarantine_pending(
                    existing,
                    reason=reason,
                    project_root=project_root,
                    transaction_id=str(existing.get("promotion_id", "") or ""),
                )
                return PromotionResult(False, reason)
            existing = _canonicalize_promotion_record(final_binding, project_root)
            _assert_source_unchanged(
                operation, source_bytes, stage="at idempotent promotion result"
            )
        if not is_main_goal:
            false_decomposition_cleanup.reconcile_false_decompositions(
                [existing],
                cwd=str(project_root),
            )
        return PromotionResult(
            True,
            "identical authoritative negation promotion already recorded",
            node_id=node_id,
            is_main_goal=is_main_goal,
            evidence=existing,
            already_promoted=True,
        )
    try:
        _promotion_transaction_hook("pending-persisted")
        _assert_source_unchanged(operation, source_bytes, stage="after pending hook")
        if promoted != blueprint:
            _assert_source_unchanged(operation, source_bytes, stage="before graph mutation")
            plan_state.save_blueprint(promoted)
            _assert_source_unchanged(operation, source_bytes, stage="after graph mutation")
        _promotion_transaction_hook("graph-persisted")
        _assert_source_unchanged(operation, source_bytes, stage="after graph hook")
        with plan_state.blueprint_commit_guard():
            _assert_source_unchanged(
                operation, source_bytes, stage="before transaction finalization"
            )
            final_blueprint = plan_state.load_blueprint()
            if not _graph_reflects_promotion_write(
                stored,
                blueprint=final_blueprint,
                operation=operation,
                project_root=project_root,
                source_bytes=source_bytes,
                requested_target_symbol=requested_target_symbol,
                requested_active_file=requested_active_file,
            ):
                raise _GraphTransactionChanged(
                    "dependency graph changed before promotion finalization"
                )
            stored, _created = _finalize_promotion_transaction(
                transaction, project_root=project_root
            )
            _assert_source_unchanged(
                operation, source_bytes, stage="after transaction finalization"
            )
            _promotion_transaction_hook("committed")
            _assert_source_unchanged(operation, source_bytes, stage="after committed hook")
            plan_state.append_journal_event(
                {
                    "event": "negation-promoted",
                    "node_id": node_id,
                    "name": theorem_id,
                    "file": canonical_file,
                    "is_main_goal": is_main_goal,
                    "promotion_id": stored["promotion_id"],
                }
            )
            _assert_source_unchanged(operation, source_bytes, stage="after promotion journal")
            append_workflow_activity(
                "negation-promoted",
                f"Promoted a fresh kernel-checked negation of {theorem_id}",
                **stored,
            )
            _assert_source_unchanged(operation, source_bytes, stage="after promotion activity")
    except _SourceLeaseChanged:
        latest_bytes = decomposition_provenance.read_source_bytes(operation)
        _quarantine_promotion(
            stored,
            reason="source changed during authoritative negation transaction",
            project_root=project_root,
            transaction_id=str(transaction.get("transaction_id", "") or ""),
            operation=operation,
            source_bytes=latest_bytes,
        )
        raise
    except _GraphTransactionChanged as exc:
        restore_graph = _graph_restore_is_safe(
            stored,
            blueprint=plan_state.load_blueprint(),
            project_root=project_root,
            operation=operation,
        )
        _quarantine_promotion(
            stored,
            reason=str(exc),
            project_root=project_root,
            transaction_id=str(transaction.get("transaction_id", "") or ""),
            operation=operation,
            source_bytes=source_bytes,
            restore_graph=restore_graph,
        )
        return PromotionResult(False, str(exc))
    if not is_main_goal:
        false_decomposition_cleanup.reconcile_false_decompositions(
            [stored],
            cwd=str(project_root),
        )
    return PromotionResult(
        True,
        "fresh negation promoted through the authoritative gate",
        node_id=node_id,
        is_main_goal=is_main_goal,
        evidence=stored,
    )


def promote_negation(
    probe_entry: Mapping[str, Any],
    *,
    cwd: str = "",
    requested_target_symbol: str = "",
    requested_active_file: str = "",
) -> PromotionResult:
    """Rerun exact negation evidence and atomically mark its graph node false."""
    entry = dict(probe_entry or {})
    theorem_id = str(entry.get("theorem", "") or "").strip()
    file_label = str(entry.get("file", "") or "").strip()
    evidence = dict(entry.get("promotion_evidence") or {})
    negation = dict(entry.get("negation") or {})
    if not theorem_id or not file_label:
        return PromotionResult(False, "probe entry lacks theorem/file identity")
    if negation.get("verdict") != "negation_proved" or not negation.get("axioms_ok"):
        return PromotionResult(False, "scratch probe is not standard-axiom negation evidence")
    tactic = str(evidence.get("proof_tactic", "") or "").strip()
    if not tactic or re.search(r"\b(?:sorry|admit|sorryAx)\b", tactic):
        return PromotionResult(False, "promotion proof tactic is absent or unsafe")

    project_root = Path(cwd or ".").expanduser().resolve()
    source_path = Path(file_label).expanduser()
    if not source_path.is_absolute():
        source_path = project_root / source_path
    try:
        operation_context = decomposition_provenance.source_operation(source_path)
        operation = operation_context.__enter__()
    except OSError as exc:
        return PromotionResult(False, f"source unavailable: {str(exc)[:200]}")
    try:
        source_bytes = decomposition_provenance.read_source_bytes(operation)
        if _sha256(source_bytes) != str(evidence.get("source_revision_sha256", "") or ""):
            return PromotionResult(False, "source revision changed after the scratch probe")

        goal = negation_probe.build_negation_goal(
            str(operation.path), theorem_id, cwd=str(project_root)
        )
        _assert_source_unchanged(operation, source_bytes, stage="after goal reconstruction")
        if isinstance(goal, dict):
            return PromotionResult(
                False, f"current declaration cannot be negated: {goal.get('error', '')}"
            )
        current_signature_hash = _sha256(goal.original.encode("utf-8"))
        if current_signature_hash != str(evidence.get("declaration_signature_sha256", "") or ""):
            return PromotionResult(False, "declaration signature changed after the scratch probe")
        if goal.prop != str(evidence.get("negation_prop", "") or ""):
            return PromotionResult(False, "reconstructed negation no longer matches the probe")

        rerun = negation_probe.run_negation_attempt(
            goal,
            file_path=str(operation.path),
            cwd=str(project_root),
            timeout_s=negation_probe.probe_timeout_s(),
            tactics=(tactic,),
        )
        _assert_source_unchanged(operation, source_bytes, stage="after negation rerun")
        if rerun.get("verdict") != "negation_proved" or not rerun.get("axioms_ok"):
            return PromotionResult(False, "fresh Lean rerun did not re-prove the negation")
        if not set(rerun.get("axioms") or []) <= negation_probe.STANDARD_AXIOMS:
            return PromotionResult(False, "fresh negation depends on non-standard axioms")
        promotion = {
            "key": f"{operation.path}::{theorem_id}",
            "theorem": theorem_id,
            "file": str(operation.path),
            "canonical_file": str(operation.path),
            "operation_path": str(operation.path),
            "source_revision_sha256": _sha256(source_bytes),
            "declaration_signature_sha256": current_signature_hash,
            "negation_name": goal.name,
            "negation_prop": goal.prop,
            "proof_tactic": tactic,
            "axioms": list(rerun.get("axioms") or []),
            "promoted_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        return _commit_promotion(
            theorem_id=theorem_id,
            file_label=file_label,
            promotion=promotion,
            project_root=project_root,
            operation=operation,
            source_bytes=source_bytes,
            requested_target_symbol=requested_target_symbol,
            requested_active_file=requested_active_file,
        )
    except _SourceLeaseChanged as exc:
        return PromotionResult(False, str(exc))
    except OSError as exc:
        return PromotionResult(False, f"source unavailable: {str(exc)[:200]}")
    finally:
        operation_context.__exit__(None, None, None)


def preflight_source_negation_candidates(
    *,
    theorem_id: str,
    file_label: str,
    proof_declarations: Sequence[str],
    cwd: str = "",
    expected_source_revision_sha256: str = "",
) -> tuple[source_negation_batch.BatchCandidateVerdict, ...]:
    """Classify a bounded candidate batch with one exact full-source check.

    Compatible results are deliberately non-authoritative. Callers must pass
    one such candidate through ``promote_source_negation`` before recording a
    proof, graph mutation, or mathematical outcome.
    """
    theorem = str(theorem_id or "").strip()
    label = str(file_label or "").strip()
    candidates = tuple(
        candidate for raw in proof_declarations if (candidate := str(raw or "").strip())
    )

    def uncertain(
        reason: str, failure_kind: str
    ) -> tuple[source_negation_batch.BatchCandidateVerdict, ...]:
        """Return a retryable scope verdict without advancing scan authority."""
        return tuple(
            source_negation_batch.BatchCandidateVerdict(
                proof_declaration=candidate,
                disposition=source_negation_batch.UNCERTAIN,
                reason=reason,
                failure_kind=failure_kind,
                retryable=True,
            )
            for candidate in candidates
        )

    if not theorem or not label or not candidates or len(set(candidates)) != len(candidates):
        return uncertain(
            "source batch lacks unique theorem/file/proof identities",
            "source_promotion_identity_invalid",
        )
    expected_revision = str(expected_source_revision_sha256 or "").strip().lower()
    project_root = Path(cwd or ".").expanduser().resolve()
    source_path = Path(label).expanduser()
    if not source_path.is_absolute():
        source_path = project_root / source_path
    try:
        with decomposition_provenance.source_operation(source_path) as operation:
            source_bytes = decomposition_provenance.read_source_bytes(operation)
            observed_revision = _sha256(source_bytes)
            if expected_revision and (
                not re.fullmatch(r"[0-9a-f]{64}", expected_revision)
                or expected_revision != observed_revision
            ):
                return uncertain(
                    "source revision changed before source-candidate batch verification",
                    "source_revision_changed_before_candidate_check",
                )
            source_text = source_bytes.decode("utf-8")
            goal = negation_probe.build_negation_goal(
                str(operation.path), theorem, cwd=str(project_root)
            )
            _assert_source_unchanged(
                operation, source_bytes, stage="after batch goal reconstruction"
            )
            if isinstance(goal, dict):
                return uncertain(
                    f"current declaration cannot be negated: {goal.get('error', '')}",
                    "source_goal_reconstruction_unavailable",
                )

            prepared: list[source_negation_batch.BatchCandidateInput] = []
            prepared_region_identities: dict[str, tuple[int, int, str]] = {}
            verdicts: dict[str, source_negation_batch.BatchCandidateVerdict] = {}
            lines = source_text.splitlines()
            for candidate in candidates:
                region = _exact_source_declaration_region(
                    operation.path,
                    source_text,
                    candidate,
                )
                _assert_source_unchanged(
                    operation,
                    source_bytes,
                    stage=f"after batch declaration reconstruction for {candidate}",
                )
                if not region:
                    verdicts[candidate] = source_negation_batch.BatchCandidateVerdict(
                        proof_declaration=candidate,
                        disposition=source_negation_batch.INCOMPATIBLE,
                        reason="source negation declaration was not found",
                        failure_kind=SOURCE_CANDIDATE_DECLARATION_MISSING,
                    )
                    continue
                candidate_statement = _validated_source_candidate_statement(
                    str(region.get("text", "") or "")
                )
                if isinstance(candidate_statement, PromotionResult):
                    verdicts[candidate] = source_negation_batch.BatchCandidateVerdict(
                        proof_declaration=candidate,
                        disposition=source_negation_batch.UNCERTAIN,
                        reason=candidate_statement.reason,
                        failure_kind=candidate_statement.failure_kind,
                        retryable=True,
                    )
                    continue
                insert_at = int(region.get("end_line", 0) or 0)
                if insert_at <= 0 or insert_at > len(lines):
                    verdicts[candidate] = source_negation_batch.BatchCandidateVerdict(
                        proof_declaration=candidate,
                        disposition=source_negation_batch.UNCERTAIN,
                        reason="source negation declaration range is invalid",
                        failure_kind="source_declaration_range_uncertain",
                        retryable=True,
                    )
                    continue
                alias = "leanflowNegationPromotion_" + _sha256((theorem + candidate).encode())[:12]
                candidate_name = str(region.get("name", "") or candidate).strip()
                harness = source_negation_harness.build_source_negation_harness(
                    alias=alias,
                    negation_prop=goal.prop,
                    candidate_name=candidate_name,
                )
                if harness is None:
                    verdicts[candidate] = source_negation_batch.BatchCandidateVerdict(
                        proof_declaration=candidate,
                        disposition=source_negation_batch.UNCERTAIN,
                        reason="source negation harness identity is invalid",
                        failure_kind="source_harness_identity_uncertain",
                        retryable=True,
                    )
                    continue
                prepared.append(
                    source_negation_batch.BatchCandidateInput(
                        proof_declaration=candidate,
                        candidate_name=candidate_name,
                        alias=alias,
                        insert_at=insert_at,
                        harness=harness,
                    )
                )
                prepared_region_identities[candidate] = (
                    int(region.get("line", 0) or 0),
                    insert_at,
                    candidate_name,
                )

            duplicate_region_candidates = {
                candidate
                for candidate, identity in prepared_region_identities.items()
                if sum(
                    other_identity == identity
                    for other_identity in prepared_region_identities.values()
                )
                > 1
            }
            for candidate in duplicate_region_candidates:
                verdicts[candidate] = source_negation_batch.BatchCandidateVerdict(
                    proof_declaration=candidate,
                    disposition=source_negation_batch.UNCERTAIN,
                    reason="source candidate declaration identity is ambiguous",
                    failure_kind="source_candidate_declaration_ambiguous",
                    retryable=True,
                )
            if duplicate_region_candidates:
                prepared = [
                    candidate
                    for candidate in prepared
                    if candidate.proof_declaration not in duplicate_region_candidates
                ]

            if prepared:
                batch_harness = source_negation_batch.build_batch_harness(
                    source_text,
                    prepared,
                )
                rerun = _run_authoritative_source_check(
                    batch_harness.source,
                    cwd=str(project_root),
                    theorem=theorem,
                )
                _assert_source_unchanged(
                    operation,
                    source_bytes,
                    stage="after source-negation batch rerun",
                )
                for verdict in source_negation_batch.classify_batch_check(
                    batch_harness,
                    rerun,
                    allowed_axioms=negation_probe.STANDARD_AXIOMS,
                ):
                    verdicts[verdict.proof_declaration] = verdict
            return tuple(verdicts[candidate] for candidate in candidates)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return uncertain(f"source batch unavailable: {str(exc)[:200]}", "source_unavailable")
    except _SourceLeaseChanged as exc:
        return uncertain(str(exc), "source_lease_changed")


def promote_source_negation(
    *,
    theorem_id: str,
    file_label: str,
    proof_declaration: str,
    cwd: str = "",
    requested_target_symbol: str = "",
    requested_active_file: str = "",
    expected_source_revision_sha256: str = "",
) -> PromotionResult:
    """Promote a source lemma that kernel-proves the exact negation of a target.

    The source declaration is only a candidate.  Rebuild the target's exact
    Pi-closed proposition, insert a scratch alias immediately after the
    candidate so private names remain in scope, elaborate the complete current
    source, and require a standard-axiom ``#print axioms`` result.  Unrelated
    open declarations in the file may warn, but any elaboration error or
    ``sorryAx`` dependency rejects promotion. When a scheduler supplies an
    expected source revision, bind the candidate verdict to bytes read after
    acquiring the source lease so an A-to-B race cannot advance A's cursor.
    """
    theorem = str(theorem_id or "").strip()
    candidate = str(proof_declaration or "").strip()
    label = str(file_label or "").strip()
    expected_revision = str(expected_source_revision_sha256 or "").strip().lower()
    if not theorem or not candidate or not label:
        return PromotionResult(
            False,
            "source promotion lacks theorem/file/proof identity",
            failure_kind="source_promotion_identity_invalid",
        )
    project_root = Path(cwd or ".").expanduser().resolve()
    source_path = Path(label).expanduser()
    if not source_path.is_absolute():
        source_path = project_root / source_path
    try:
        with decomposition_provenance.source_operation(source_path) as operation:
            source_bytes = decomposition_provenance.read_source_bytes(operation)
            observed_revision = _sha256(source_bytes)
            if expected_revision and (
                not re.fullmatch(r"[0-9a-f]{64}", expected_revision)
                or expected_revision != observed_revision
            ):
                return PromotionResult(
                    False,
                    "source revision changed before source-candidate verification",
                    failure_kind="source_revision_changed_before_candidate_check",
                    retryable=True,
                )
            source_text = source_bytes.decode("utf-8")
            goal = negation_probe.build_negation_goal(
                str(operation.path), theorem, cwd=str(project_root)
            )
            _assert_source_unchanged(operation, source_bytes, stage="after goal reconstruction")
            if isinstance(goal, dict):
                return PromotionResult(
                    False,
                    f"current declaration cannot be negated: {goal.get('error', '')}",
                    failure_kind="source_goal_reconstruction_unavailable",
                    retryable=True,
                )
            region = _exact_source_declaration_region(
                operation.path,
                source_text,
                candidate,
            )
            _assert_source_unchanged(
                operation, source_bytes, stage="after declaration reconstruction"
            )
            if not region:
                return PromotionResult(
                    False,
                    "source negation declaration was not found",
                    failure_kind=SOURCE_CANDIDATE_DECLARATION_MISSING,
                )
            candidate_text = str(region.get("text", "") or "")
            candidate_statement = _validated_source_candidate_statement(candidate_text)
            if isinstance(candidate_statement, PromotionResult):
                return candidate_statement

            lines = source_text.splitlines()
            insert_at = int(region.get("end_line", 0) or 0)
            if insert_at <= 0 or insert_at > len(lines):
                return PromotionResult(
                    False,
                    "source negation declaration range is invalid",
                    failure_kind="source_declaration_range_uncertain",
                    retryable=True,
                    scan_may_continue=True,
                )
            alias = f"leanflowNegationPromotion_{_sha256((theorem + candidate).encode())[:12]}"
            candidate_name = str(region.get("name", "") or candidate).strip()
            harness = source_negation_harness.build_source_negation_harness(
                alias=alias,
                negation_prop=goal.prop,
                candidate_name=candidate_name,
            )
            if harness is None:
                return PromotionResult(
                    False,
                    "source negation harness identity is invalid",
                    failure_kind="source_harness_identity_uncertain",
                    retryable=True,
                    scan_may_continue=True,
                )
            # Check only the exact source prefix needed to elaborate the
            # candidate and alias. Subsequent declarations are irrelevant and
            # may independently fail or exhaust Lean's heartbeat budget.
            scratch_source = "\n".join([*lines[:insert_at], harness.declaration]) + "\n"
            rerun = _run_authoritative_source_check(
                scratch_source,
                cwd=str(project_root),
                theorem=theorem,
            )
            _assert_source_unchanged(operation, source_bytes, stage="after source-negation rerun")
            messages = list(rerun.get("messages") or [])
            if not rerun.get("success") or any(
                isinstance(message, Mapping)
                and str(message.get("severity", "") or "").strip().lower() == "error"
                for message in messages
            ):
                underlying_kind = str(
                    rerun.get("failure_kind", "lean_elaboration") or "lean_elaboration"
                )
                underlying_retryable = bool(rerun.get("retryable", False))
                candidate_incompatible = bool(
                    underlying_kind == "lean_elaboration"
                    and not underlying_retryable
                    and _failure_confined_to_harness(
                        rerun,
                        start_line=insert_at + 1,
                        end_line=insert_at + len(harness.declaration.splitlines()),
                    )
                )
                scan_may_continue = bool(
                    not candidate_incompatible
                    and _failure_allows_candidate_scan_continuation(
                        rerun,
                        start_line=insert_at + 1,
                        end_line=insert_at + len(harness.declaration.splitlines()),
                    )
                )
                return PromotionResult(
                    False,
                    "fresh source rerun did not elaborate the exact negation",
                    failure_kind=(
                        SOURCE_CANDIDATE_KERNEL_INCOMPATIBLE
                        if candidate_incompatible
                        else underlying_kind
                    ),
                    retryable=underlying_retryable or not candidate_incompatible,
                    scan_may_continue=scan_may_continue,
                )
            axioms = _printed_axioms(_scratch_messages(rerun), alias)
            if axioms is None:
                return PromotionResult(
                    False,
                    "fresh source negation has no auditable axiom result",
                    failure_kind="source_axiom_audit_unavailable",
                    retryable=True,
                )
            if not set(axioms) <= negation_probe.STANDARD_AXIOMS:
                return PromotionResult(
                    False,
                    "fresh source negation depends on unknown or non-standard axioms",
                    failure_kind=SOURCE_CANDIDATE_AXIOMS_UNACCEPTABLE,
                )

            promotion = {
                "key": f"{operation.path}::{theorem}",
                "theorem": theorem,
                "file": str(operation.path),
                "canonical_file": str(operation.path),
                "operation_path": str(operation.path),
                "source_revision_sha256": _sha256(source_bytes),
                "declaration_signature_sha256": _sha256(goal.original.encode("utf-8")),
                "negation_name": goal.name,
                "negation_prop": goal.prop,
                "proof_tactic": harness.proof_tactic,
                "proof_declaration": candidate,
                "axioms": axioms,
                "promotion_kind": "source_negation",
                "promoted_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            }
            return _commit_promotion(
                theorem_id=theorem,
                file_label=label,
                promotion=promotion,
                project_root=project_root,
                operation=operation,
                source_bytes=source_bytes,
                requested_target_symbol=requested_target_symbol,
                requested_active_file=requested_active_file,
            )
    except (OSError, UnicodeDecodeError) as exc:
        return PromotionResult(
            False,
            f"source unavailable: {str(exc)[:200]}",
            failure_kind="source_unavailable",
            retryable=True,
        )
    except _SourceLeaseChanged as exc:
        return PromotionResult(
            False,
            str(exc),
            failure_kind="source_lease_changed",
            retryable=True,
        )
