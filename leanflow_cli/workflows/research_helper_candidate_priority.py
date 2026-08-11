"""Persist and prioritize exact checked helpers until the parent acts."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _contains_lean_suggestion_tactic,
    _declaration_line_index_from_text,
    _is_lean_inspection_only_helper_candidate,
    _strip_lean_comments_and_strings,
    _text_has_sorry,
)
from leanflow_cli.workflows import (
    decomposition_provenance,
    plan_state,
    queue_edit_guard,
    research_findings,
    research_helper_candidate_backlog,
    research_helper_source_coverage,
)
from leanflow_cli.workflows.queue_manager import TheoremKey
from leanflow_cli.workflows.verification_candidate_replay import declaration_signature_sha256
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file

STATE_KEY = "pending_research_helper_candidate"
SUMMARY_KEY = "pending_research_helper_candidate"
BACKLOG_STATE_KEY = "research_helper_candidate_backlog"
BACKLOG_SUMMARY_KEY = "research_helper_candidate_backlog"
RESOLVED_STATE_KEY = "resolved_research_helper_candidates"
RESOLVED_SUMMARY_KEY = "resolved_research_helper_candidates"
CONSUMPTION_STATE_KEY = "research_helper_target_consumption"
CONSUMPTION_SUMMARY_KEY = "research_helper_target_consumption"
SCHEMA_VERSION = 5
MAX_RESOLVED_CANDIDATES = 128
MAX_DECLARATION_CHARS = 16_000
MAX_INTEGRATION_ATTEMPTS = 2
AWAITING_RECHECK = "awaiting_parent_recheck"
AWAITING_PRODUCTION_RENAME = "awaiting_production_rename"
READY_TO_INTEGRATE = "ready_to_integrate"
_VALID_STATES = frozenset({AWAITING_PRODUCTION_RENAME, AWAITING_RECHECK, READY_TO_INTEGRATE})
_VALID_RECHECK_STATUSES = frozenset(
    {"not_attempted", "accepted", "operationally_unavailable", "rejected"}
)
_HYDRATION_KEY = "_research_helper_candidate_hydration_token"
_PROCESS_HYDRATION_TOKEN = uuid.uuid4().hex
_UNSET = object()
_PARENT_RECHECK_EVIDENCE_VERSION = "parent-helper-recheck-v1"
_NONPRODUCTION_HELPER_NAME_RE = re.compile(
    r"(?:^|_)(?:scratch|temp|test|tmp|counterexample|probe|obstruction|not_universal|"
    r"without_universal|false_of)(?:_|$)|(?:^|_)(?:do|does)_not(?:_|$)"
)
_DECLARATION_NAME_RE = re.compile(
    r"(?m)^(?P<prefix>\s*(?:private\s+)?(?:theorem|lemma|example|def|abbrev)\s+)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_']*)"
)


def _now_iso() -> str:
    """Return one UTC timestamp for durable audit records."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical_file(value: object) -> str:
    """Return one canonical path without requiring it to exist."""
    text = str(value or "").strip()
    return str(Path(text).expanduser().resolve(strict=False)) if text else ""


def _same_file(left: object, right: object) -> bool:
    """Return whether two path spellings identify the same canonical file."""
    first = _canonical_file(left)
    second = _canonical_file(right)
    return bool(first and second and os.path.normcase(first) == os.path.normcase(second))


def _sha256(text: str) -> str:
    """Return the stable SHA-256 identity for exact source text."""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _helper_name_is_nonproduction(name: str) -> bool:
    """Return whether a checked declaration is unsuitable for source integration.

    Scratch/test helpers, counterexamples, and explicit method obstructions may
    be kernel-valid while remaining unsuitable for mandatory production-source
    integration. Keep them available to the research graph without letting an
    incorrect worker disposition preempt the foreground proof.
    """
    short_name = str(name or "").strip().rsplit(".", 1)[-1].casefold()
    return bool(short_name and _NONPRODUCTION_HELPER_NAME_RE.search(short_name))


def _normalized_axioms(values: Sequence[object]) -> tuple[str, ...]:
    """Return one stable, duplicate-free axiom profile."""
    return tuple(sorted({str(value or "").strip() for value in values if str(value or "").strip()}))


def _parent_recheck_evidence_sha256(
    *,
    candidate_id: str,
    target_signature_sha256: str,
    rechecked_source_revision_sha256: str,
    expected_integrated_source_revision_sha256: str,
    helper_name: str,
    declaration_sha256: str,
    axiom_profile_axioms: Sequence[object],
) -> str:
    """Bind reusable parent evidence to all source and policy identities."""
    payload = "\0".join(
        (
            _PARENT_RECHECK_EVIDENCE_VERSION,
            str(candidate_id or "").strip(),
            str(target_signature_sha256 or "").strip(),
            str(rechecked_source_revision_sha256 or "").strip(),
            str(expected_integrated_source_revision_sha256 or "").strip(),
            str(helper_name or "").strip(),
            str(declaration_sha256 or "").strip(),
            *(_normalized_axioms(axiom_profile_axioms)),
        )
    )
    return _sha256(payload)


def source_revision_sha256(active_file: str) -> str:
    """Return the current whole-file source identity, or an empty value."""
    try:
        return hashlib.sha256(Path(active_file).read_bytes()).hexdigest()
    except OSError:
        return ""


def target_signature_sha256(active_file: str, target_symbol: str) -> str:
    """Return the current assigned declaration statement identity."""
    return declaration_signature_sha256(target_symbol, active_file)


def target_declaration_sha256(active_file: str, target_symbol: str) -> str:
    """Return the exact assigned declaration identity, including its proof body."""
    try:
        source = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return ""
    declaration = decomposition_provenance.declaration_slice(source, target_symbol)
    return declaration.declaration_sha256 if declaration is not None else ""


def target_placeholder_count(active_file: str, target_symbol: str) -> int | None:
    """Return the assigned declaration's source placeholder count."""
    try:
        source = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return None
    declaration = decomposition_provenance.declaration_slice(source, target_symbol)
    if declaration is None:
        return None
    stripped = _strip_lean_comments_and_strings(declaration.text)
    return len(re.findall(r"\b(?:sorry|admit)\b", stripped))


def _target_body_consumes_helper(record: Mapping[str, str]) -> bool:
    """Return whether the assigned proof now makes concrete use of its helper."""
    active_file = str(record.get("active_file", "") or "")
    target_symbol = str(record.get("target_symbol", "") or "")
    current_declaration = target_declaration_sha256(active_file, target_symbol)
    if not current_declaration or current_declaration == record.get(
        "target_declaration_sha256", ""
    ):
        return False
    try:
        source = Path(active_file).read_text(encoding="utf-8")
    except OSError:
        return False
    declaration = decomposition_provenance.declaration_slice(source, target_symbol)
    if declaration is None:
        return False
    helper_name = str(record.get("helper_name", "") or "").strip().split(".")[-1]
    if not helper_name:
        return False
    stripped = _strip_lean_comments_and_strings(declaration.text)
    return re.search(rf"(?<![\w.]){re.escape(helper_name)}(?![\w.])", stripped) is not None


def _consumption_record(raw: object) -> dict[str, str]:
    """Return one valid helper-consumption marker or an empty mapping."""
    if not isinstance(raw, Mapping):
        return {}
    target_symbol = str(raw.get("target_symbol", "") or "").strip()
    active_file = _canonical_file(raw.get("active_file"))
    target_signature = str(raw.get("target_signature_sha256", "") or "").strip()
    target_declaration = str(raw.get("target_declaration_sha256", "") or "").strip()
    candidate_id = str(raw.get("candidate_id", "") or "").strip()
    helper_name = str(raw.get("helper_name", "") or "").strip()
    try:
        target_placeholders = max(0, int(raw.get("target_placeholder_count", 1) or 0))
    except (TypeError, ValueError):
        target_placeholders = 1
    if (
        not TheoremKey.make(target_symbol, active_file).is_valid()
        or len(target_signature) != 64
        or len(target_declaration) != 64
        or target_placeholders < 1
        or not candidate_id.startswith("rhcp-")
        or not helper_name
    ):
        return {}
    return {
        "target_symbol": target_symbol,
        "active_file": active_file,
        "target_signature_sha256": target_signature,
        "target_declaration_sha256": target_declaration,
        "target_placeholder_count": str(target_placeholders),
        "candidate_id": candidate_id,
        "helper_name": helper_name,
        "integrated_at": str(raw.get("integrated_at", "") or "").strip(),
    }


def _candidate_id(
    *,
    active_file: str,
    target_symbol: str,
    target_signature: str,
    declaration_sha256: str,
) -> str:
    """Build one exact assignment-and-source-bound helper candidate identity."""
    payload = "\0".join(
        (
            _canonical_file(active_file),
            str(target_symbol or "").strip(),
            str(target_signature or "").strip(),
            str(declaration_sha256 or "").strip(),
        )
    )
    return "rhcp-" + _sha256(payload)[:24]


@dataclass(frozen=True)
class PendingResearchHelperCandidate:
    """Describe one parent-owned helper integration opportunity."""

    candidate_id: str
    state: str
    campaign_id: str
    job_id: str
    delivery_markers: tuple[str, ...]
    target_symbol: str
    active_file: str
    target_signature_sha256: str
    observed_source_revision_sha256: str
    rechecked_source_revision_sha256: str
    helper_name: str
    declaration: str
    declaration_sha256: str
    parent_recheck_status: str = "not_attempted"
    parent_recheck_detail: str = ""
    parent_recheck_axioms: tuple[str, ...] = ()
    expected_integrated_source_revision_sha256: str = ""
    parent_recheck_evidence_sha256: str = ""
    integration_attempts: int = 0
    created_at: str = ""
    updated_at: str = ""

    @property
    def key(self) -> TheoremKey:
        """Return the normalized exact assignment identity."""
        return TheoremKey.make(self.target_symbol, self.active_file)

    @property
    def ready(self) -> bool:
        """Return whether the current candidate passed its parent recheck."""
        return self.state == READY_TO_INTEGRATE and self.parent_recheck_status == "accepted"

    @property
    def integration_fence_active(self) -> bool:
        """Return whether the bounded exact-insertion fence remains active."""
        return self.ready and self.integration_attempts < MAX_INTEGRATION_ATTEMPTS

    def matches(self, target_symbol: str, active_file: str) -> bool:
        """Return whether this record belongs to the exact active assignment."""
        return self.key == TheoremKey.make(target_symbol, active_file)

    def to_mapping(self) -> dict[str, object]:
        """Serialize the bounded record for autonomy and plan summary state."""
        return {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            "state": self.state,
            "campaign_id": self.campaign_id,
            "job_id": self.job_id,
            "delivery_markers": list(self.delivery_markers),
            "target_symbol": self.target_symbol,
            "active_file": self.active_file,
            "target_signature_sha256": self.target_signature_sha256,
            "observed_source_revision_sha256": self.observed_source_revision_sha256,
            "rechecked_source_revision_sha256": self.rechecked_source_revision_sha256,
            "helper_name": self.helper_name,
            "declaration": self.declaration,
            "declaration_sha256": self.declaration_sha256,
            "parent_recheck_status": self.parent_recheck_status,
            "parent_recheck_detail": self.parent_recheck_detail,
            "parent_recheck_axioms": list(self.parent_recheck_axioms),
            "expected_integrated_source_revision_sha256": (
                self.expected_integrated_source_revision_sha256
            ),
            "parent_recheck_evidence_sha256": self.parent_recheck_evidence_sha256,
            "integration_attempts": self.integration_attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> PendingResearchHelperCandidate | None:
        """Parse one fail-closed pending candidate record."""
        declaration = str(raw.get("declaration", "") or "").strip()
        declaration_hash = str(raw.get("declaration_sha256", "") or "").strip()
        target_symbol = str(raw.get("target_symbol", "") or "").strip()
        active_file = _canonical_file(raw.get("active_file"))
        target_signature = str(raw.get("target_signature_sha256", "") or "").strip()
        helper_name = str(raw.get("helper_name", "") or "").strip()
        state = str(raw.get("state", "") or "").strip()
        recheck_status = str(raw.get("parent_recheck_status", "") or "not_attempted").strip()
        raw_parent_axioms = raw.get("parent_recheck_axioms")
        parent_axioms = (
            _normalized_axioms(raw_parent_axioms)
            if isinstance(raw_parent_axioms, Sequence)
            and not isinstance(raw_parent_axioms, (str, bytes, bytearray))
            else ()
        )
        raw_markers = raw.get("delivery_markers")
        markers = (
            tuple(
                dict.fromkeys(
                    str(value or "").strip() for value in raw_markers if str(value or "").strip()
                )
            )[:16]
            if isinstance(raw_markers, Sequence)
            and not isinstance(raw_markers, (str, bytes, bytearray))
            else ()
        )
        expected_id = _candidate_id(
            active_file=active_file,
            target_symbol=target_symbol,
            target_signature=target_signature,
            declaration_sha256=declaration_hash,
        )
        try:
            integration_attempts = max(
                0,
                min(
                    MAX_INTEGRATION_ATTEMPTS,
                    int(raw.get("integration_attempts", 0) or 0),
                ),
            )
        except (TypeError, ValueError):
            integration_attempts = 0
        candidate = cls(
            candidate_id=str(raw.get("candidate_id", "") or "").strip(),
            state=state,
            campaign_id=str(raw.get("campaign_id", "") or "").strip(),
            job_id=str(raw.get("job_id", "") or "").strip(),
            delivery_markers=markers,
            target_symbol=target_symbol,
            active_file=active_file,
            target_signature_sha256=target_signature,
            observed_source_revision_sha256=str(
                raw.get("observed_source_revision_sha256", "") or ""
            ).strip(),
            rechecked_source_revision_sha256=str(
                raw.get("rechecked_source_revision_sha256", "") or ""
            ).strip(),
            helper_name=helper_name,
            declaration=declaration,
            declaration_sha256=declaration_hash,
            parent_recheck_status=recheck_status,
            parent_recheck_detail=str(raw.get("parent_recheck_detail", "") or "")[:1000],
            parent_recheck_axioms=parent_axioms,
            expected_integrated_source_revision_sha256=str(
                raw.get("expected_integrated_source_revision_sha256", "") or ""
            ).strip(),
            parent_recheck_evidence_sha256=str(
                raw.get("parent_recheck_evidence_sha256", "") or ""
            ).strip(),
            integration_attempts=integration_attempts,
            created_at=str(raw.get("created_at", "") or "").strip(),
            updated_at=str(raw.get("updated_at", "") or "").strip(),
        )
        declared_names = tuple(
            str(entry.get("name", "") or "").strip()
            for entry in _declaration_line_index_from_text(declaration)
            if str(entry.get("name", "") or "").strip()
        )
        if (
            not candidate.key.is_valid()
            or not candidate.job_id
            or not helper_name
            or not declaration
            or len(declaration) > MAX_DECLARATION_CHARS
            or declaration_hash != _sha256(declaration)
            or not target_signature
            or candidate.candidate_id != expected_id
            or state not in _VALID_STATES
            or recheck_status not in _VALID_RECHECK_STATUSES
            or declared_names != (helper_name,)
            or (_helper_name_is_nonproduction(helper_name) != (state == AWAITING_PRODUCTION_RENAME))
            or (
                state == AWAITING_PRODUCTION_RENAME
                and (
                    recheck_status != "not_attempted"
                    or _is_lean_inspection_only_helper_candidate(declaration)
                )
            )
        ):
            return None
        return candidate


def _resolved_entries(raw: object) -> tuple[dict[str, str], ...]:
    """Return a bounded valid resolved-candidate audit list."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, Mapping):
            continue
        candidate_id = str(value.get("candidate_id", "") or "").strip()
        if not candidate_id.startswith("rhcp-") or candidate_id in seen:
            continue
        seen.add(candidate_id)
        entries.append(
            {
                "candidate_id": candidate_id,
                "disposition": str(value.get("disposition", "") or "resolved").strip()[:80],
                "resolved_at": str(value.get("resolved_at", "") or "").strip(),
            }
        )
    return tuple(entries[-MAX_RESOLVED_CANDIDATES:])


def _backlog_entries(raw: object) -> tuple[PendingResearchHelperCandidate, ...]:
    """Return a bounded valid candidate backlog in promotion order."""
    return research_helper_candidate_backlog.normalize(
        raw,
        parse=PendingResearchHelperCandidate.from_mapping,
        candidate_id=lambda candidate: candidate.candidate_id,
    )


def _merge_resolved_entries(*values: object) -> tuple[dict[str, str], ...]:
    """Merge monotonic resolved identities from checkpoint and durable state."""
    by_id: dict[str, dict[str, str]] = {}
    for value in values:
        for entry in _resolved_entries(value):
            prior = by_id.get(entry["candidate_id"])
            if prior is None or entry["resolved_at"] >= prior["resolved_at"]:
                by_id[entry["candidate_id"]] = entry
    ordered = sorted(
        by_id.values(),
        key=lambda entry: (entry["resolved_at"], entry["candidate_id"]),
    )
    return tuple(ordered[-MAX_RESOLVED_CANDIDATES:])


def _update_durable_state(
    *,
    pending: object = _UNSET,
    backlog: object = _UNSET,
    resolved: object = _UNSET,
    consumption: object = _UNSET,
) -> None:
    """Atomically update owner-controlled candidate keys in summary state."""
    if not plan_state.plan_state_enabled():
        return

    def mutate(summary: dict[str, Any]) -> None:
        if pending is not _UNSET:
            summary[SUMMARY_KEY] = dict(pending) if isinstance(pending, Mapping) else {}
        if backlog is not _UNSET:
            summary[BACKLOG_SUMMARY_KEY] = [
                record.to_mapping() for record in _backlog_entries(backlog)
            ]
        if resolved is not _UNSET:
            summary[RESOLVED_SUMMARY_KEY] = [dict(entry) for entry in _resolved_entries(resolved)]
        if consumption is not _UNSET:
            summary[CONSUMPTION_SUMMARY_KEY] = _consumption_record(consumption)
        summary["version"] = 1
        summary["updated_at"] = _now_iso()

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)


def _set_memory_pending(
    autonomy_state: dict[str, Any],
    record: PendingResearchHelperCandidate | None,
) -> None:
    """Mirror one already-committed pending record into process state."""
    if record is None:
        autonomy_state.pop(STATE_KEY, None)
    else:
        autonomy_state[STATE_KEY] = record.to_mapping()


def _set_memory_backlog(
    autonomy_state: dict[str, Any],
    records: Sequence[PendingResearchHelperCandidate],
) -> None:
    """Mirror the ordered candidate backlog into process state."""
    autonomy_state[BACKLOG_STATE_KEY] = [
        record.to_mapping() for record in _backlog_entries(records)
    ]


def _set_memory_resolved(
    autonomy_state: dict[str, Any],
    entries: Sequence[Mapping[str, str]],
) -> None:
    """Mirror the monotonic resolved archive into process state."""
    autonomy_state[RESOLVED_STATE_KEY] = [dict(entry) for entry in _resolved_entries(entries)]


def _set_memory_consumption(
    autonomy_state: dict[str, Any],
    record: Mapping[str, str] | None,
) -> None:
    """Mirror one target-consumption marker into process state."""
    normalized = _consumption_record(record)
    if normalized:
        autonomy_state[CONSUMPTION_STATE_KEY] = normalized
    else:
        autonomy_state.pop(CONSUMPTION_STATE_KEY, None)


def _persist(
    autonomy_state: dict[str, Any],
    record: PendingResearchHelperCandidate | None,
) -> None:
    """Commit one pending record durably before updating process memory."""
    payload = record.to_mapping() if record is not None else {}
    _update_durable_state(pending=payload)
    _set_memory_pending(autonomy_state, record)
    autonomy_state[_HYDRATION_KEY] = _PROCESS_HYDRATION_TOKEN


def _persist_pending_and_backlog(
    autonomy_state: dict[str, Any],
    pending: PendingResearchHelperCandidate | None,
    backlog: Sequence[PendingResearchHelperCandidate],
) -> None:
    """Atomically commit the active candidate and its promotion backlog."""
    pending_payload = pending.to_mapping() if pending is not None else {}
    normalized_backlog = _backlog_entries(backlog)
    _update_durable_state(pending=pending_payload, backlog=normalized_backlog)
    _set_memory_pending(autonomy_state, pending)
    _set_memory_backlog(autonomy_state, normalized_backlog)
    autonomy_state[_HYDRATION_KEY] = _PROCESS_HYDRATION_TOKEN


def _hydrate_state(autonomy_state: dict[str, Any]) -> None:
    """Reconcile a possibly stale checkpoint with current durable authority once."""
    if autonomy_state.get(_HYDRATION_KEY) == _PROCESS_HYDRATION_TOKEN:
        return
    memory_pending_raw = autonomy_state.get(STATE_KEY)
    memory_pending = (
        PendingResearchHelperCandidate.from_mapping(memory_pending_raw)
        if isinstance(memory_pending_raw, Mapping)
        else None
    )
    memory_resolved = _resolved_entries(autonomy_state.get(RESOLVED_STATE_KEY))
    memory_backlog = _backlog_entries(autonomy_state.get(BACKLOG_STATE_KEY))
    memory_consumption = _consumption_record(autonomy_state.get(CONSUMPTION_STATE_KEY))
    if not plan_state.plan_state_enabled():
        _set_memory_pending(autonomy_state, memory_pending)
        _set_memory_backlog(autonomy_state, memory_backlog)
        _set_memory_resolved(autonomy_state, memory_resolved)
        _set_memory_consumption(autonomy_state, memory_consumption)
        autonomy_state[_HYDRATION_KEY] = _PROCESS_HYDRATION_TOKEN
        return

    summary = read_json_file(plan_state.plan_state_paths().summary_json)
    disk_pending_raw = summary.get(SUMMARY_KEY)
    disk_pending = (
        PendingResearchHelperCandidate.from_mapping(disk_pending_raw)
        if isinstance(disk_pending_raw, Mapping)
        else None
    )
    disk_resolved = _resolved_entries(summary.get(RESOLVED_SUMMARY_KEY))
    disk_backlog = _backlog_entries(summary.get(BACKLOG_SUMMARY_KEY))
    disk_consumption = _consumption_record(summary.get(CONSUMPTION_SUMMARY_KEY))
    resolved = _merge_resolved_entries(memory_resolved, disk_resolved)
    resolved_ids = {entry["candidate_id"] for entry in resolved}
    pending: PendingResearchHelperCandidate | None
    if disk_pending is not None:
        pending = disk_pending
    elif SUMMARY_KEY not in summary:
        # One-time migration for checkpoints written before this owner key was
        # durable. An explicit empty mapping on disk always outranks memory.
        pending = memory_pending
    else:
        pending = None
    if pending is not None and pending.candidate_id in resolved_ids:
        pending = None

    if BACKLOG_SUMMARY_KEY in summary:
        backlog = disk_backlog
    else:
        # One-time migration for checkpoints written before the backlog key.
        backlog = memory_backlog
    backlog = tuple(
        candidate
        for candidate in backlog
        if candidate.candidate_id not in resolved_ids
        and (pending is None or candidate.candidate_id != pending.candidate_id)
    )

    desired_pending = pending.to_mapping() if pending is not None else {}
    desired_resolved = [dict(entry) for entry in resolved]
    disk_pending_payload = dict(disk_pending_raw) if isinstance(disk_pending_raw, Mapping) else {}
    pending_changed = (SUMMARY_KEY not in summary and pending is not None) or (
        SUMMARY_KEY in summary and disk_pending_payload != desired_pending
    )
    resolved_changed = [dict(entry) for entry in disk_resolved] != desired_resolved
    desired_backlog = [record.to_mapping() for record in backlog]
    disk_backlog_payload = [record.to_mapping() for record in disk_backlog]
    backlog_changed = (BACKLOG_SUMMARY_KEY not in summary and bool(backlog)) or (
        BACKLOG_SUMMARY_KEY in summary and disk_backlog_payload != desired_backlog
    )
    if CONSUMPTION_SUMMARY_KEY in summary:
        consumption = disk_consumption
    else:
        # One-time migration for checkpoints written before the owner key.
        consumption = memory_consumption
    consumption_changed = CONSUMPTION_SUMMARY_KEY not in summary and bool(consumption)
    if pending_changed or backlog_changed or resolved_changed or consumption_changed:
        _update_durable_state(
            pending=desired_pending if pending_changed else _UNSET,
            backlog=desired_backlog if backlog_changed else _UNSET,
            resolved=desired_resolved if resolved_changed else _UNSET,
            consumption=consumption if consumption_changed else _UNSET,
        )
    _set_memory_pending(autonomy_state, pending)
    _set_memory_backlog(autonomy_state, backlog)
    _set_memory_resolved(autonomy_state, resolved)
    _set_memory_consumption(autonomy_state, consumption)
    autonomy_state[_HYDRATION_KEY] = _PROCESS_HYDRATION_TOKEN


def _load_resolved_entries(autonomy_state: dict[str, Any]) -> tuple[dict[str, str], ...]:
    """Return the reconciled bounded resolved-candidate archive."""
    _hydrate_state(autonomy_state)
    return _resolved_entries(autonomy_state.get(RESOLVED_STATE_KEY))


def resolved_candidate_ids(autonomy_state: dict[str, Any]) -> frozenset[str]:
    """Return helper candidate identities with an authoritative disposition."""
    return frozenset(entry["candidate_id"] for entry in _load_resolved_entries(autonomy_state))


def load(autonomy_state: dict[str, Any]) -> PendingResearchHelperCandidate | None:
    """Load the pending candidate, hydrating durable state when needed."""
    _hydrate_state(autonomy_state)
    raw = autonomy_state.get(STATE_KEY)
    record = PendingResearchHelperCandidate.from_mapping(raw) if isinstance(raw, Mapping) else None
    if record is not None and record.candidate_id not in resolved_candidate_ids(autonomy_state):
        return record
    _set_memory_pending(autonomy_state, None)
    return None


def backlog(
    autonomy_state: dict[str, Any],
) -> tuple[PendingResearchHelperCandidate, ...]:
    """Return verified candidates waiting behind the active candidate."""
    _hydrate_state(autonomy_state)
    resolved = resolved_candidate_ids(autonomy_state)
    records = tuple(
        record
        for record in _backlog_entries(autonomy_state.get(BACKLOG_STATE_KEY))
        if record.candidate_id not in resolved
    )
    _set_memory_backlog(autonomy_state, records)
    return records


def matching(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> PendingResearchHelperCandidate | None:
    """Return the pending candidate only for the exact active assignment."""
    record = load(autonomy_state)
    if record is None or not record.matches(target_symbol, active_file):
        return None
    if (
        record.state == AWAITING_PRODUCTION_RENAME
        and target_signature_sha256(active_file, target_symbol) != record.target_signature_sha256
    ):
        resolve(
            autonomy_state,
            disposition="stale_target_signature_before_production_rename",
            require_target_consumption=False,
        )
        return None
    return record


def target_consumption_pending(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether an integrated helper still awaits assigned-proof use.

    Whole-file growth and unrelated target edits are ignored. The marker
    clears after the assigned proof body concretely references the integrated
    helper, the manager accepts the target, or the theorem statement changes.
    """
    _hydrate_state(autonomy_state)
    record = _consumption_record(autonomy_state.get(CONSUMPTION_STATE_KEY))
    if not record:
        return False
    if str(record.get("target_symbol", "") or "").strip() != str(
        target_symbol or ""
    ).strip() or not _same_file(record.get("active_file", ""), active_file):
        return False
    current_signature = target_signature_sha256(active_file, target_symbol)
    if not current_signature:
        return True
    if current_signature != record["target_signature_sha256"]:
        _update_durable_state(consumption={})
        _set_memory_consumption(autonomy_state, None)
        autonomy_state[_HYDRATION_KEY] = _PROCESS_HYDRATION_TOKEN
        return False
    if _target_body_consumes_helper(record):
        _update_durable_state(consumption={})
        _set_memory_consumption(autonomy_state, None)
        autonomy_state[_HYDRATION_KEY] = _PROCESS_HYDRATION_TOKEN
        return False
    return True


def release_target_consumption_after_verified_target(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Release helper consumption after the manager accepts the exact target."""
    _hydrate_state(autonomy_state)
    record = _consumption_record(autonomy_state.get(CONSUMPTION_STATE_KEY))
    if not record:
        return False
    if str(record.get("target_symbol", "") or "").strip() != str(
        target_symbol or ""
    ).strip() or not _same_file(record.get("active_file", ""), active_file):
        return False
    _update_durable_state(consumption={})
    _set_memory_consumption(autonomy_state, None)
    autonomy_state[_HYDRATION_KEY] = _PROCESS_HYDRATION_TOKEN
    return True


def target_consumption_record(
    autonomy_state: dict[str, Any],
) -> dict[str, str]:
    """Return the current helper-consumption marker for observability."""
    _hydrate_state(autonomy_state)
    return _consumption_record(autonomy_state.get(CONSUMPTION_STATE_KEY))


def exact_source_duplicate(
    record: PendingResearchHelperCandidate,
) -> research_helper_source_coverage.ExactSourceDuplicate | None:
    """Return an exact current-source declaration duplicate."""
    return research_helper_source_coverage.exact_source_duplicate(
        record.declaration,
        target_symbol=record.target_symbol,
        active_file=record.active_file,
    )


def source_name_collision(
    record: PendingResearchHelperCandidate,
) -> research_helper_source_coverage.ExactSourceDuplicate | None:
    """Return a same-name current-source declaration blocking insertion."""
    return research_helper_source_coverage.source_name_collision(
        record.declaration,
        target_symbol=record.target_symbol,
        active_file=record.active_file,
    )


def _remember_exact_candidate(
    autonomy_state: dict[str, Any],
    *,
    campaign_id: str,
    job_id: str,
    target_symbol: str,
    active_file: str,
    helper_name: str,
    declaration: str,
    delivery_markers: Sequence[str] = (),
    state: str = AWAITING_RECHECK,
    replace_pending: bool = False,
    preempt_pending: bool = False,
) -> PendingResearchHelperCandidate | None:
    """Persist one exact helper candidate without discarding checked work.

    A newly verified foreground helper may preempt an older unchecked active
    candidate. The displaced record enters a bounded durable backlog and is
    promoted after the newer helper receives an authoritative disposition.
    """
    existing = load(autonomy_state)
    canonical_file = _canonical_file(active_file)
    normalized_target = str(target_symbol or "").strip()
    normalized_name = str(helper_name or "").strip()
    normalized_declaration = str(declaration or "").strip()
    target_signature = target_signature_sha256(canonical_file, normalized_target)
    observed_revision = source_revision_sha256(canonical_file)
    key = TheoremKey.make(normalized_target, canonical_file)
    if (
        not key.is_valid()
        or not target_signature
        or not observed_revision
        or not str(job_id or "").strip()
        or not normalized_name
        or not normalized_declaration
        or len(normalized_declaration) > MAX_DECLARATION_CHARS
        or _text_has_sorry(normalized_declaration)
        or _contains_lean_suggestion_tactic(normalized_declaration)
        or _is_lean_inspection_only_helper_candidate(normalized_declaration)
        or (_helper_name_is_nonproduction(normalized_name) != (state == AWAITING_PRODUCTION_RENAME))
    ):
        return None
    entries = _declaration_line_index_from_text(normalized_declaration)
    declared_names = tuple(
        str(entry.get("name", "") or "").strip()
        for entry in entries
        if str(entry.get("name", "") or "").strip()
    )
    if declared_names != (normalized_name,):
        return None
    if any(
        detector(
            normalized_declaration,
            target_symbol=normalized_target,
            active_file=canonical_file,
        )
        is not None
        for detector in (
            research_helper_source_coverage.exact_source_duplicate,
            research_helper_source_coverage.source_name_collision,
        )
    ):
        return None
    declaration_hash = _sha256(normalized_declaration)
    candidate_id = _candidate_id(
        active_file=canonical_file,
        target_symbol=normalized_target,
        target_signature=target_signature,
        declaration_sha256=declaration_hash,
    )
    if candidate_id in resolved_candidate_ids(autonomy_state):
        return None
    if existing is not None and existing.candidate_id == candidate_id:
        return existing
    if existing is not None and not replace_pending and not preempt_pending:
        return existing
    now = _now_iso()
    record = PendingResearchHelperCandidate(
        candidate_id=candidate_id,
        state=state,
        campaign_id=str(campaign_id or "").strip(),
        job_id=str(job_id or "").strip(),
        delivery_markers=tuple(
            dict.fromkeys(
                str(marker or "").strip()
                for marker in delivery_markers
                if str(marker or "").strip()
            )
        )[:16],
        target_symbol=normalized_target,
        active_file=canonical_file,
        target_signature_sha256=target_signature,
        observed_source_revision_sha256=observed_revision,
        rechecked_source_revision_sha256="",
        helper_name=normalized_name,
        declaration=normalized_declaration,
        declaration_sha256=declaration_hash,
        created_at=now,
        updated_at=now,
    )
    if existing is not None and preempt_pending:
        queued = research_helper_candidate_backlog.prepend(
            existing,
            backlog(autonomy_state),
            candidate_id=lambda candidate: candidate.candidate_id,
            exclude_id=record.candidate_id,
        )
        _persist_pending_and_backlog(autonomy_state, record, queued)
    else:
        _persist(autonomy_state, record)
    return record


def _successful_foreground_helper(
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> tuple[str, str] | None:
    """Return the declaration identity from one exact successful helper check."""
    action = str(arguments.get("action", "") or "").strip()
    argument_target = str(arguments.get("theorem_id", "") or "").strip()
    argument_file = str(arguments.get("file_path", "") or "").strip()
    declaration = str(arguments.get("replacement", "") or "").strip()
    raw_names = result.get("replacement_declarations")
    names = (
        tuple(
            dict.fromkeys(
                str(value or "").strip() for value in raw_names if str(value or "").strip()
            )
        )
        if isinstance(raw_names, Sequence) and not isinstance(raw_names, (str, bytes, bytearray))
        else ()
    )
    error_code = str(result.get("error_code", "") or "").strip()
    if (
        action != "check_helper"
        or argument_target != str(target_symbol or "").strip()
        or not _same_file(argument_file, active_file)
        or result.get("success") is not True
        or result.get("ok") is not True
        or result.get("valid_without_sorry") is not True
        or result.get("has_errors") is not False
        or result.get("has_sorry") is not False
        or result.get("replacement_matches_target") is not False
        or str(result.get("verification_scope", "") or "").strip() != "helper_candidate"
        or result.get("timed_out") is True
        or "timeout" in error_code.casefold()
        or result.get("diagnostic_only") is True
        or len(names) != 1
    ):
        return None
    declared_names = tuple(
        str(entry.get("name", "") or "").strip()
        for entry in _declaration_line_index_from_text(declaration)
        if str(entry.get("name", "") or "").strip()
    )
    if (
        declared_names != names
        or _text_has_sorry(declaration)
        or _contains_lean_suggestion_tactic(declaration)
        or _is_lean_inspection_only_helper_candidate(declaration)
    ):
        return None
    return names[0], declaration


def successful_nonproduction_foreground_helper_name(
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> str:
    """Return a verified scratch helper name that requires production promotion."""
    helper = _successful_foreground_helper(
        arguments,
        result,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    if (
        helper is None
        or not _helper_name_is_nonproduction(helper[0])
        or _is_direct_forwarding_wrapper(helper[1])
    ):
        return ""
    return helper[0]


def _is_direct_forwarding_wrapper(declaration: str) -> bool:
    """Return whether a scratch helper only forwards to one existing declaration."""
    proof = re.search(r":=\s*by\b", str(declaration or ""))
    if proof is None:
        return False
    body = " ".join(str(declaration or "")[proof.end() :].split())
    return bool(re.fullmatch(r"exact\s+[A-Za-z_«][\w'.«»]*(?:\s+[^;]+)?", body))


def _declaration_with_name(declaration: str, helper_name: str) -> str:
    """Return one exact declaration with only its declared name replaced."""
    normalized_name = str(helper_name or "").strip()
    match = _DECLARATION_NAME_RE.search(str(declaration or ""))
    if match is None or not normalized_name:
        return ""
    return (
        str(declaration or "")[: match.start("name")]
        + normalized_name
        + str(declaration or "")[match.end("name") :]
    )


def is_exact_production_rename(
    record: PendingResearchHelperCandidate,
    arguments: Mapping[str, Any] | None,
) -> bool:
    """Return whether a helper check changes only a pending scratch name."""
    if record.state != AWAITING_PRODUCTION_RENAME or not isinstance(arguments, Mapping):
        return False
    action = str(arguments.get("action", "") or "").strip().lower().replace("-", "_")
    target_symbol = str(
        arguments.get("theorem_id", "") or arguments.get("target_symbol", "") or ""
    ).strip()
    active_file = str(
        arguments.get("file_path", "") or arguments.get("active_file", "") or ""
    ).strip()
    declaration = str(arguments.get("replacement", "") or "").strip()
    names = tuple(
        str(entry.get("name", "") or "").strip()
        for entry in _declaration_line_index_from_text(declaration)
        if str(entry.get("name", "") or "").strip()
    )
    if (
        action != "check_helper"
        or target_symbol != record.target_symbol
        or not _same_file(active_file, record.active_file)
        or len(names) != 1
        or _helper_name_is_nonproduction(names[0])
    ):
        return False
    return declaration == _declaration_with_name(record.declaration, names[0])


def remember_nonproduction_from_foreground_check(
    autonomy_state: dict[str, Any],
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
) -> PendingResearchHelperCandidate | None:
    """Persist one substantive verified scratch helper until its exact rename."""
    existing = load(autonomy_state)
    if existing is not None and existing.state == AWAITING_PRODUCTION_RENAME:
        return existing
    helper = _successful_foreground_helper(
        arguments,
        result,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    if helper is None:
        return None
    helper_name, declaration = helper
    if (
        not _helper_name_is_nonproduction(helper_name)
        or _is_lean_inspection_only_helper_candidate(declaration)
        or _is_direct_forwarding_wrapper(declaration)
    ):
        return None
    check_identity = _sha256(
        "\0".join(
            (
                _canonical_file(active_file),
                str(target_symbol or "").strip(),
                helper_name,
                declaration,
            )
        )
    )[:24]
    return _remember_exact_candidate(
        autonomy_state,
        campaign_id=campaign_id,
        job_id=f"foreground-rename:{check_identity}",
        target_symbol=target_symbol,
        active_file=active_file,
        helper_name=helper_name,
        declaration=declaration,
        delivery_markers=("foreground-check", "production-rename-required"),
        state=AWAITING_PRODUCTION_RENAME,
        preempt_pending=existing is not None,
    )


def remember_from_foreground_check(
    autonomy_state: dict[str, Any],
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
) -> PendingResearchHelperCandidate | None:
    """Persist one exact successful foreground helper check for parent recheck."""
    helper = _successful_foreground_helper(
        arguments,
        result,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    if helper is None:
        return None
    helper_name, declaration = helper
    existing = load(autonomy_state)
    replace_pending = False
    preempt_pending = False
    if existing is not None:
        if is_exact_production_rename(existing, arguments):
            replace_pending = True
        else:
            preempt_pending = True
    check_identity = _sha256(
        "\0".join(
            (
                _canonical_file(active_file),
                str(target_symbol or "").strip(),
                helper_name,
                declaration,
            )
        )
    )[:24]
    return _remember_exact_candidate(
        autonomy_state,
        campaign_id=campaign_id,
        job_id=f"foreground-check:{check_identity}",
        target_symbol=target_symbol,
        active_file=active_file,
        helper_name=helper_name,
        declaration=declaration,
        delivery_markers=("foreground-check",),
        replace_pending=replace_pending,
        preempt_pending=preempt_pending,
    )


def remember_from_findings(
    autonomy_state: dict[str, Any],
    findings: Sequence[Mapping[str, Any]],
    *,
    campaign_id: str,
    target_symbol: str,
    active_file: str,
    delivery_markers: Sequence[str] = (),
) -> PendingResearchHelperCandidate | None:
    """Register the first exact actionable helper without replacing pending work."""
    existing = load(autonomy_state)
    if existing is not None:
        return existing
    canonical_file = _canonical_file(active_file)
    for finding in findings:
        if not _finding_allows_parent_recheck(finding):
            continue
        if str(finding.get("target_symbol", "") or "").strip() != target_symbol:
            continue
        finding_file = str(finding.get("active_file", "") or "").strip()
        if finding_file and not _same_file(finding_file, canonical_file):
            continue
        for helper in research_findings.canonical_checked_helpers(finding):
            if str(helper.get("anchor_target_symbol", "") or "").strip() != target_symbol:
                continue
            if not _same_file(helper.get("active_file"), canonical_file):
                continue
            declaration = str(helper.get("declaration", "") or "").strip()
            declaration_hash = str(helper.get("declaration_sha256", "") or "").strip()
            worker_check = helper.get("worker_check")
            raw_names = (
                worker_check.get("replacement_declarations")
                if isinstance(worker_check, Mapping)
                else None
            )
            names = (
                tuple(
                    dict.fromkeys(
                        str(value or "").strip() for value in raw_names if str(value or "").strip()
                    )
                )
                if isinstance(raw_names, Sequence)
                and not isinstance(raw_names, (str, bytes, bytearray))
                else ()
            )
            if len(names) != 1 or declaration_hash != _sha256(declaration):
                continue
            record = _remember_exact_candidate(
                autonomy_state,
                campaign_id=campaign_id,
                job_id=str(finding.get("job_id", "") or "").strip(),
                target_symbol=target_symbol,
                active_file=canonical_file,
                helper_name=names[0],
                declaration=declaration,
                delivery_markers=delivery_markers,
            )
            if record is not None:
                return record
    return None


def _finding_allows_parent_recheck(finding: Mapping[str, Any]) -> bool:
    """Return whether an exact helper may receive a fresh parent check.

    A whole-file source revision change makes worker advice stale, but it does
    not make captured helper source unsafe to *recheck*. Re-evaluate only that
    staleness reason against the current parent gate; finite, nonadvancing,
    malformed, and semantically ineligible findings remain evidence-only.
    """
    if research_findings.foreground_use_role(finding) == "actionable":
        return True
    if research_findings.foreground_use_reason(finding) != "stale_active_file_revision":
        return False
    current = dict(finding)
    current.pop("source_revision_sha256", None)
    return research_findings.foreground_use_role(current) == "actionable"


def mark_parent_recheck(
    autonomy_state: dict[str, Any],
    *,
    candidate_id: str,
    status: str,
    source_revision_sha256: str = "",
    detail: str = "",
    expected_integrated_source_revision_sha256: str = "",
    axiom_profile_axioms: Sequence[object] | None = None,
) -> PendingResearchHelperCandidate | None:
    """Record one parent recheck while preserving operational retryability."""
    existing = load(autonomy_state)
    normalized = str(status or "").strip()
    if (
        existing is None
        or existing.candidate_id != str(candidate_id or "").strip()
        or normalized not in _VALID_RECHECK_STATUSES
    ):
        return existing
    state = READY_TO_INTEGRATE if normalized == "accepted" else AWAITING_RECHECK
    rechecked_revision = (
        str(source_revision_sha256 or "").strip()
        if normalized == "accepted"
        else existing.rechecked_source_revision_sha256
    )
    expected_revision = (
        str(expected_integrated_source_revision_sha256 or "").strip()
        if normalized == "accepted" and axiom_profile_axioms is not None
        else ""
    )
    parent_axioms = (
        _normalized_axioms(axiom_profile_axioms or ())
        if normalized == "accepted" and axiom_profile_axioms is not None
        else ()
    )
    evidence_sha256 = (
        _parent_recheck_evidence_sha256(
            candidate_id=existing.candidate_id,
            target_signature_sha256=existing.target_signature_sha256,
            rechecked_source_revision_sha256=rechecked_revision,
            expected_integrated_source_revision_sha256=expected_revision,
            helper_name=existing.helper_name,
            declaration_sha256=existing.declaration_sha256,
            axiom_profile_axioms=parent_axioms,
        )
        if rechecked_revision and expected_revision
        else ""
    )
    updated = replace(
        existing,
        state=state,
        rechecked_source_revision_sha256=rechecked_revision,
        parent_recheck_status=normalized,
        parent_recheck_detail=str(detail or "")[:1000],
        parent_recheck_axioms=parent_axioms,
        expected_integrated_source_revision_sha256=expected_revision,
        parent_recheck_evidence_sha256=evidence_sha256,
        updated_at=_now_iso(),
    )
    _persist(autonomy_state, updated)
    return updated


def reset_for_source_change(
    autonomy_state: dict[str, Any],
    *,
    candidate_id: str,
) -> PendingResearchHelperCandidate | None:
    """Require a fresh parent recheck after the active source changes."""
    existing = load(autonomy_state)
    if existing is None or existing.candidate_id != str(candidate_id or "").strip():
        return existing
    updated = replace(
        existing,
        state=AWAITING_RECHECK,
        rechecked_source_revision_sha256="",
        parent_recheck_status="not_attempted",
        parent_recheck_detail="source changed after the prior parent check",
        parent_recheck_axioms=(),
        expected_integrated_source_revision_sha256="",
        parent_recheck_evidence_sha256="",
        updated_at=_now_iso(),
    )
    _persist(autonomy_state, updated)
    return updated


def parent_recheck_evidence_authenticated(record: PendingResearchHelperCandidate) -> bool:
    """Return whether the reusable parent evidence is complete and self-consistent."""
    expected = _parent_recheck_evidence_sha256(
        candidate_id=record.candidate_id,
        target_signature_sha256=record.target_signature_sha256,
        rechecked_source_revision_sha256=record.rechecked_source_revision_sha256,
        expected_integrated_source_revision_sha256=(
            record.expected_integrated_source_revision_sha256
        ),
        helper_name=record.helper_name,
        declaration_sha256=record.declaration_sha256,
        axiom_profile_axioms=record.parent_recheck_axioms,
    )
    return bool(
        record.ready
        and len(record.rechecked_source_revision_sha256) == 64
        and len(record.expected_integrated_source_revision_sha256) == 64
        and len(record.parent_recheck_evidence_sha256) == 64
        and record.parent_recheck_evidence_sha256 == expected
    )


def note_integration_attempt(
    autonomy_state: dict[str, Any],
    *,
    candidate_id: str,
) -> PendingResearchHelperCandidate | None:
    """Increment the bounded exact-insertion opportunity counter."""
    existing = load(autonomy_state)
    if existing is None or existing.candidate_id != str(candidate_id or "").strip():
        return existing
    updated = replace(
        existing,
        integration_attempts=min(
            MAX_INTEGRATION_ATTEMPTS,
            existing.integration_attempts + 1,
        ),
        updated_at=_now_iso(),
    )
    _persist(autonomy_state, updated)
    return updated


def inserted_candidate_matches_source(
    record: PendingResearchHelperCandidate,
    source: str,
) -> bool:
    """Return whether source contains the exact helper before its assigned target."""
    entries = _declaration_line_index_from_text(str(source or ""))

    def exact_entry(symbol: str) -> Mapping[str, Any] | None:
        """Return one unambiguous declaration entry for ``symbol``."""
        wanted = {symbol, symbol.split(".")[-1]}
        matches = [entry for entry in entries if str(entry.get("name", "") or "").strip() in wanted]
        return matches[0] if len(matches) == 1 else None

    helper_region = exact_entry(record.helper_name)
    target_region = exact_entry(record.target_symbol)
    if helper_region is None or target_region is None:
        return False
    # A parent-checked declaration contains no external preamble. If a doc
    # comment or standalone attribute became attached to it during insertion,
    # the helper landed inside another declaration's preamble and is not the
    # exact integration that the parent authenticated.
    if queue_edit_guard._queue_edit_assigned_preamble(source, record.helper_name):
        return False
    current = str(helper_region.get("text", "") or "").strip()
    declaration = record.declaration.strip()
    source_lines = str(source or "").splitlines(keepends=True)
    helper_end_offset = sum(
        len(line) for line in source_lines[: int(helper_region.get("end_line", 0) or 0)]
    )
    source_through_helper = str(source or "")[:helper_end_offset].rstrip()
    exact_source_declaration = bool(
        declaration
        and source_through_helper.endswith(declaration)
        and declaration.endswith(current)
    )
    helper_end = int(helper_region.get("end_line", 0) or 0)
    target_start = int(target_region.get("line", 0) or 0)
    return bool(
        helper_end > 0
        and target_start > helper_end
        and exact_source_declaration
        and _sha256(declaration) == record.declaration_sha256
    )


def inserted_candidate_matches(record: PendingResearchHelperCandidate) -> bool:
    """Return whether the active file contains the exact helper before its target."""
    try:
        source = Path(record.active_file).read_text(encoding="utf-8")
    except OSError:
        return False
    return inserted_candidate_matches_source(record, source)


def retire(autonomy_state: dict[str, Any]) -> PendingResearchHelperCandidate | None:
    """Retire the active candidate and promote the next checked helper."""
    existing = load(autonomy_state)
    promoted, queued = research_helper_candidate_backlog.promote(backlog(autonomy_state))
    _persist_pending_and_backlog(autonomy_state, promoted, queued)
    return existing


def resolve(
    autonomy_state: dict[str, Any],
    *,
    disposition: str,
    require_target_consumption: bool = True,
) -> PendingResearchHelperCandidate | None:
    """Retire and archive one candidate after an authoritative disposition."""
    existing = load(autonomy_state)
    if existing is None:
        return None
    normalized_disposition = str(disposition or "resolved").strip()[:80]
    entries = list(_load_resolved_entries(autonomy_state))
    entries = [entry for entry in entries if entry["candidate_id"] != existing.candidate_id]
    entries.append(
        {
            "candidate_id": existing.candidate_id,
            "disposition": normalized_disposition,
            "resolved_at": _now_iso(),
        }
    )
    payload = [dict(entry) for entry in _resolved_entries(entries)]
    consumption: object = _UNSET
    if normalized_disposition.startswith("integrated") and require_target_consumption:
        declaration_hash = target_declaration_sha256(
            existing.active_file,
            existing.target_symbol,
        )
        placeholder_count = target_placeholder_count(
            existing.active_file,
            existing.target_symbol,
        )
        current_signature = target_signature_sha256(
            existing.active_file,
            existing.target_symbol,
        )
        if (
            declaration_hash
            and placeholder_count
            and current_signature == existing.target_signature_sha256
        ):
            consumption = {
                "target_symbol": existing.target_symbol,
                "active_file": existing.active_file,
                "target_signature_sha256": current_signature,
                "target_declaration_sha256": declaration_hash,
                "target_placeholder_count": str(placeholder_count),
                "candidate_id": existing.candidate_id,
                "helper_name": existing.helper_name,
                "integrated_at": _now_iso(),
            }
    promoted, queued = research_helper_candidate_backlog.promote(backlog(autonomy_state))
    _update_durable_state(
        pending=promoted.to_mapping() if promoted is not None else {},
        backlog=queued,
        resolved=payload,
        consumption=consumption,
    )
    _set_memory_resolved(autonomy_state, payload)
    _set_memory_pending(autonomy_state, promoted)
    _set_memory_backlog(autonomy_state, queued)
    if consumption is not _UNSET:
        _set_memory_consumption(
            autonomy_state,
            consumption if isinstance(consumption, Mapping) else None,
        )
    autonomy_state[_HYDRATION_KEY] = _PROCESS_HYDRATION_TOKEN
    return existing
