"""Rank source-backed negation candidates without granting proof authority."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.workflow_json_io import update_json_file

PROCESS_STATE_KEY = "source_negation_candidate_scan"
CONTINUATION_STATE_KEY = "source_negation_candidate_continuation"
SUMMARY_KEY = "source_negation_candidate_scans"
SCHEMA_VERSION = 3
CHECK_CONTRACT_VERSION = "exact-source-harness-v4"
DEFAULT_GENERIC_BATCH_LIMIT = 4
DEFAULT_UNCERTAIN_CONTINUATION_LIMIT = 2
DEFAULT_SOURCE_PROMOTION_CHECK_LIMIT = 4
_SCOPE_CAP = 32

_NEGATION_MARKERS = frozenset(
    {
        "counterexample",
        "false",
        "impossible",
        "neg",
        "negation",
        "not",
        "obstruction",
        "refutation",
    }
)
_SYMBOL_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_NEGATE_ROUTE_METADATA_KEYS = (
    "prover_requested_route",
    "campaign_inflight_route",
    "campaign_epoch_route_selection",
)


@dataclass(frozen=True)
class RankedSourceNegationCandidate:
    """Describe one non-authoritative candidate and its deterministic rank."""

    name: str
    rank_reason: str
    exact_scope_evidence: bool
    target_derived_name: bool
    shared_prefix_tokens: int
    suffix_distance: int
    likely_negation_name: bool
    outcome_order: int

    def activity_payload(self) -> dict[str, object]:
        """Return bounded JSON-like telemetry for the ranking decision."""
        return {
            "name": self.name,
            "rank_reason": self.rank_reason,
            "exact_scope_evidence": self.exact_scope_evidence,
            "target_derived_name": self.target_derived_name,
            "shared_prefix_tokens": self.shared_prefix_tokens,
            "suffix_distance": self.suffix_distance,
            "likely_negation_name": self.likely_negation_name,
        }


@dataclass(frozen=True)
class ScheduledSourceNegationCandidate:
    """Bind one candidate to its monotonic lane cursor position."""

    candidate: RankedSourceNegationCandidate
    lane: str
    lane_index: int
    lane_order_sha256: str

    @property
    def name(self) -> str:
        """Return the underlying Lean declaration name."""
        return self.candidate.name

    def activity_payload(self) -> dict[str, object]:
        """Return ranking and cursor telemetry for this scheduled check."""
        return {
            **self.candidate.activity_payload(),
            "scan_lane": self.lane,
            "scan_lane_index": self.lane_index,
        }


@dataclass(frozen=True)
class SourceNegationCandidateBatch:
    """Describe one bounded promotion batch and deferred scan work."""

    candidates: tuple[ScheduledSourceNegationCandidate, ...]
    continuation_candidates: tuple[ScheduledSourceNegationCandidate, ...]
    previously_rejected_count: int
    deferred_generic_count: int


@dataclass(frozen=True)
class SourceNegationContinuationWindow:
    """Describe one nonauthoritative circular window after an uncertain head."""

    candidates: tuple[ScheduledSourceNegationCandidate, ...]
    order_sha256: str
    start_offset: int
    tail_size: int
    anchor_lane: str
    anchor_lane_index: int


def _qualified_parts(symbol: str) -> tuple[str, str]:
    """Split a Lean symbol into a case-folded namespace and local name."""
    value = str(symbol or "").strip().casefold()
    namespace, separator, local = value.rpartition(".")
    return (namespace, local) if separator else ("", value)


def _name_tokens(symbol: str) -> tuple[str, ...]:
    """Return comparison-only tokens from one Lean local declaration name."""
    _namespace, local = _qualified_parts(symbol)
    return tuple(token.casefold() for token in _SYMBOL_TOKEN_RE.findall(local))


def _same_symbol_identity(left: str, right: str) -> bool:
    """Match qualified or unqualified spellings of the same local symbol."""
    left_namespace, left_local = _qualified_parts(left)
    right_namespace, right_local = _qualified_parts(right)
    if not left_local or left_local != right_local:
        return False
    return not left_namespace or not right_namespace or left_namespace == right_namespace


def _canonical_file(value: object) -> str:
    """Return a comparison-only canonical file identity."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve(strict=False))
    except OSError:
        return text


def _assignment_matches(
    state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether one gate request owns the current queue assignment."""
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    assignment = state.get("current_queue_assignment")
    current = dict(assignment) if isinstance(assignment, Mapping) else {}
    return bool(
        target
        and active
        and str(current.get("target_symbol", "") or "").strip() == target
        and _canonical_file(current.get("active_file")) == active
    )


def observed_negate_route_keys(
    state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> tuple[str, ...]:
    """Return matching negate metadata keys for telemetry, never authority."""
    if not _assignment_matches(
        state,
        target_symbol=target_symbol,
        active_file=active_file,
    ):
        return ()
    observed: list[str] = []
    if str(state.get("orchestrator_current_route", "") or "").strip().lower() == "negate":
        observed.append("orchestrator_current_route")
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    for key in _NEGATE_ROUTE_METADATA_KEYS:
        raw_route = state.get(key)
        route = dict(raw_route) if isinstance(raw_route, Mapping) else {}
        if (
            str(route.get("route", "") or "").strip().lower() == "negate"
            and str(route.get("target_symbol", "") or "").strip() == target
            and _canonical_file(route.get("active_file")) == active
        ):
            observed.append(key)
    return tuple(sorted(observed))


def eager_helper_promotion_provenance(
    state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    proof_declaration: str,
    exact_counterexample_names: Iterable[str] = (),
) -> str:
    """Return exact evidence authority for eager post-helper promotion.

    Route metadata is deliberately excluded: it can remain visible after the
    selected negate work has completed. Ordinary verified helpers stay in the
    bounded exhaustive negate-route scan, while only an authenticated exact
    counterexample pays the immediate full-source promotion check.
    """
    proof = str(proof_declaration or "").strip()
    if not proof or not _assignment_matches(
        state,
        target_symbol=target_symbol,
        active_file=active_file,
    ):
        return ""
    if any(
        _same_symbol_identity(proof, str(name or "").strip()) for name in exact_counterexample_names
    ):
        return "verified-counterexample-evidence"
    return ""


def eager_promotion_route_provenance(
    state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    proof_declaration: str,
    exact_counterexample_names: Iterable[str] = (),
) -> str:
    """Return eager helper authority under the exact-evidence-only contract.

    Keep the historical name as a compatibility shim. Matching route metadata
    is observable through :func:`observed_negate_route_keys` but no longer
    grants expensive post-helper promotion authority.
    """
    return eager_helper_promotion_provenance(
        state,
        target_symbol=target_symbol,
        active_file=active_file,
        proof_declaration=proof_declaration,
        exact_counterexample_names=exact_counterexample_names,
    )


def _same_symbol_family(candidate: str, target: str) -> bool:
    """Return whether two local names can be compared as one namespace family."""
    candidate_namespace, _candidate_local = _qualified_parts(candidate)
    target_namespace, _target_local = _qualified_parts(target)
    return (
        not candidate_namespace or not target_namespace or candidate_namespace == target_namespace
    )


def _target_derived(candidate: str, target: str) -> tuple[bool, int]:
    """Recognize conventional direct helpers derived from the exact target name."""
    if not _same_symbol_family(candidate, target):
        return False, 1_000_000
    _candidate_namespace, candidate_local = _qualified_parts(candidate)
    _target_namespace, target_local = _qualified_parts(target)
    if not candidate_local or not target_local:
        return False, 1_000_000

    candidate_tokens = _name_tokens(candidate)
    target_tokens = _name_tokens(target)
    if candidate_local.startswith(f"{target_local}_"):
        return True, max(0, len(candidate_tokens) - len(target_tokens))
    if candidate_local in {f"not_{target_local}", f"neg_{target_local}"}:
        return True, 1
    return False, 1_000_000


def _shared_prefix_length(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    """Count equal leading local-name tokens."""
    count = 0
    for left_token, right_token in zip(left, right):
        if left_token != right_token:
            break
        count += 1
    return count


def _likely_negation(symbol: str) -> bool:
    """Return whether a local name contains a complete negation marker token."""
    return bool(set(_name_tokens(symbol)).intersection(_NEGATION_MARKERS))


def rank_source_negation_candidates(
    candidates: Iterable[str],
    *,
    target_symbol: str,
    exact_scope_evidence_names: Iterable[str] = (),
) -> tuple[RankedSourceNegationCandidate, ...]:
    """Rank every candidate while leaving compatibility to the exact Lean gate.

    Authenticated graph evidence attached to the exact target ranks first.
    Conventional helpers derived from the target name rank next, with a short
    suffix ahead of a more synthetic specialization. Remaining names use only
    affinity and negation-shaped tokens as scheduling hints. No candidate is
    filtered or capped because naming is not mathematical authority.
    """
    target = str(target_symbol or "").strip()
    evidence_names = tuple(
        name for raw_name in exact_scope_evidence_names if (name := str(raw_name or "").strip())
    )
    seen: set[str] = set()
    ranked: list[RankedSourceNegationCandidate] = []
    target_tokens = _name_tokens(target)
    for outcome_order, raw_candidate in enumerate(candidates):
        candidate = str(raw_candidate or "").strip()
        if not candidate or candidate == target or candidate in seen:
            continue
        seen.add(candidate)
        exact_scope = any(
            _same_symbol_identity(candidate, evidence_name) for evidence_name in evidence_names
        )
        target_derived, suffix_distance = _target_derived(candidate, target)
        shared_prefix = (
            _shared_prefix_length(_name_tokens(candidate), target_tokens)
            if _same_symbol_family(candidate, target)
            else 0
        )
        likely_negation = _likely_negation(candidate)
        if exact_scope:
            reason = "exact-target-graph-evidence"
        elif target_derived:
            reason = "target-derived-helper-name"
        elif shared_prefix:
            reason = "target-name-affinity"
        elif likely_negation:
            reason = "generic-negation-name"
        else:
            reason = "same-file-verified-fallback"
        ranked.append(
            RankedSourceNegationCandidate(
                name=candidate,
                rank_reason=reason,
                exact_scope_evidence=exact_scope,
                target_derived_name=target_derived,
                shared_prefix_tokens=shared_prefix,
                suffix_distance=suffix_distance,
                likely_negation_name=likely_negation,
                outcome_order=outcome_order,
            )
        )

    def priority(candidate: RankedSourceNegationCandidate) -> tuple[int, int, int, int, int]:
        """Put authenticated scope and structural affinity ahead of name hints."""
        if candidate.exact_scope_evidence:
            tier = 0
        elif candidate.target_derived_name:
            tier = 1
        elif candidate.shared_prefix_tokens:
            tier = 2
        elif candidate.likely_negation_name:
            tier = 3
        else:
            tier = 4
        return (
            tier,
            candidate.suffix_distance if candidate.target_derived_name else 0,
            -candidate.shared_prefix_tokens,
            0 if candidate.likely_negation_name else 1,
            candidate.outcome_order,
        )

    return tuple(sorted(ranked, key=priority))


def _sha256(value: str) -> str:
    """Hash one exact candidate-scan identity."""
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _valid_digest(value: object) -> str:
    """Return one normalized SHA-256 digest or an empty string."""
    normalized = str(value or "").strip().casefold()
    return normalized if re.fullmatch(r"[0-9a-f]{64}", normalized) else ""


def _matching_record(
    raw: object,
    *,
    scope_key: str,
    source_revision_sha256: str,
) -> dict[str, Any] | None:
    """Return one source- and contract-matched cursor record."""
    if not isinstance(raw, Mapping):
        return None
    record = dict(raw)
    if (
        record.get("schema_version") != SCHEMA_VERSION
        or str(record.get("check_contract_version", "") or "") != CHECK_CONTRACT_VERSION
        or str(record.get("scope_key", "") or "").strip() != scope_key
        or _valid_digest(record.get("source_revision_sha256")) != source_revision_sha256
    ):
        return None
    return record


def _durable_record(
    *,
    scope_key: str,
    source_revision_sha256: str,
) -> dict[str, Any] | None:
    """Load the current source-scoped cursor from the durable plan summary."""
    if not plan_state.plan_state_enabled():
        return None
    try:
        raw_records = plan_state.load_summary().get(SUMMARY_KEY)
    except Exception:
        return None
    if not isinstance(raw_records, list):
        return None
    for raw in reversed(raw_records[-_SCOPE_CAP:]):
        matched = _matching_record(
            raw,
            scope_key=scope_key,
            source_revision_sha256=source_revision_sha256,
        )
        if matched is not None:
            return matched
    return None


def _order_sha256(candidates: Iterable[RankedSourceNegationCandidate], *, lane: str) -> str:
    """Bind a cursor to the complete deterministic order of one scan lane."""
    names = tuple(candidate.name for candidate in candidates)
    return _sha256("\0".join((CHECK_CONTRACT_VERSION, lane, *names)))


def _cursor(value: object, *, size: int) -> int:
    """Return a bounded nonnegative lane cursor."""
    try:
        return min(size, max(0, int(str(value or 0))))
    except (TypeError, ValueError):
        return 0


def _aligned_cursor(
    record: Mapping[str, Any] | None,
    *,
    lane: str,
    order_sha256: str,
    size: int,
) -> int:
    """Read a cursor only when its complete candidate-order identity matches."""
    current = dict(record or {})
    if _valid_digest(current.get(f"{lane}_order_sha256")) != order_sha256:
        return 0
    return _cursor(current.get(f"{lane}_cursor"), size=size)


def _current_record(
    state: MutableMapping[str, Any],
    *,
    scope_key: str,
    source_revision_sha256: str,
    exact_order_sha256: str,
    exact_size: int,
    generic_order_sha256: str,
    generic_size: int,
) -> dict[str, Any]:
    """Merge process and durable cursors for the current stable lane orders."""
    process = _matching_record(
        state.get(PROCESS_STATE_KEY),
        scope_key=scope_key,
        source_revision_sha256=source_revision_sha256,
    )
    durable = _durable_record(
        scope_key=scope_key,
        source_revision_sha256=source_revision_sha256,
    )
    exact_cursor = max(
        _aligned_cursor(
            process,
            lane="exact",
            order_sha256=exact_order_sha256,
            size=exact_size,
        ),
        _aligned_cursor(
            durable,
            lane="exact",
            order_sha256=exact_order_sha256,
            size=exact_size,
        ),
    )
    generic_cursor = max(
        _aligned_cursor(
            process,
            lane="generic",
            order_sha256=generic_order_sha256,
            size=generic_size,
        ),
        _aligned_cursor(
            durable,
            lane="generic",
            order_sha256=generic_order_sha256,
            size=generic_size,
        ),
    )
    record = {
        "schema_version": SCHEMA_VERSION,
        "check_contract_version": CHECK_CONTRACT_VERSION,
        "scope_key": scope_key,
        "source_revision_sha256": source_revision_sha256,
        "exact_order_sha256": exact_order_sha256,
        "exact_cursor": exact_cursor,
        "generic_order_sha256": generic_order_sha256,
        "generic_cursor": generic_cursor,
    }
    state[PROCESS_STATE_KEY] = record
    return record


def select_candidate_batch(
    ranked: Iterable[RankedSourceNegationCandidate],
    *,
    state: MutableMapping[str, Any],
    scope_key: str,
    source_revision_sha256: str,
    generic_limit: int = DEFAULT_GENERIC_BATCH_LIMIT,
) -> SourceNegationCandidateBatch:
    """Select all exact candidates plus a bounded resumable generic tail.

    Definitive incompatibilities advance source-revision-scoped lane cursors
    that survive resume. Each cursor is authenticated by the complete ordered
    candidate-list digest, so a changed order restarts conservatively instead
    of skipping new evidence. Stable orders are eventually exhausted with
    constant-size durable state, regardless of candidate count.
    """
    candidates = tuple(ranked)
    scope = str(scope_key or "").strip()
    revision = _valid_digest(source_revision_sha256)
    limit = max(0, int(generic_limit))
    exact = tuple(
        candidate
        for candidate in candidates
        if candidate.exact_scope_evidence or candidate.target_derived_name
    )
    generic = tuple(
        candidate
        for candidate in candidates
        if not candidate.exact_scope_evidence and not candidate.target_derived_name
    )
    exact_order_sha256 = _order_sha256(exact, lane="exact")
    generic_order_sha256 = _order_sha256(generic, lane="generic")
    if not scope or not revision:
        exact_cursor = 0
        generic_cursor = 0
    else:
        record = _current_record(
            state,
            scope_key=scope,
            source_revision_sha256=revision,
            exact_order_sha256=exact_order_sha256,
            exact_size=len(exact),
            generic_order_sha256=generic_order_sha256,
            generic_size=len(generic),
        )
        exact_cursor = int(record["exact_cursor"])
        generic_cursor = int(record["generic_cursor"])
    exact_scheduled = tuple(
        ScheduledSourceNegationCandidate(
            candidate=candidate,
            lane="exact",
            lane_index=index,
            lane_order_sha256=exact_order_sha256,
        )
        for index, candidate in enumerate(exact[exact_cursor:], start=exact_cursor)
    )
    generic_end = min(len(generic), generic_cursor + limit)
    generic_scheduled = tuple(
        ScheduledSourceNegationCandidate(
            candidate=candidate,
            lane="generic",
            lane_index=index,
            lane_order_sha256=generic_order_sha256,
        )
        for index, candidate in enumerate(generic[generic_cursor:generic_end], start=generic_cursor)
    )
    all_generic_scheduled = tuple(
        ScheduledSourceNegationCandidate(
            candidate=candidate,
            lane="generic",
            lane_index=index,
            lane_order_sha256=generic_order_sha256,
        )
        for index, candidate in enumerate(generic[generic_cursor:], start=generic_cursor)
    )
    return SourceNegationCandidateBatch(
        candidates=(*exact_scheduled, *generic_scheduled),
        continuation_candidates=(*exact_scheduled, *all_generic_scheduled),
        previously_rejected_count=exact_cursor + generic_cursor,
        deferred_generic_count=max(0, len(generic) - generic_end),
    )


def select_uncertain_continuation_window(
    batch: SourceNegationCandidateBatch,
    *,
    state: MutableMapping[str, Any],
    scope_key: str,
    source_revision_sha256: str,
    anchor: ScheduledSourceNegationCandidate,
    limit: int = DEFAULT_UNCERTAIN_CONTINUATION_LIMIT,
) -> SourceNegationContinuationWindow:
    """Select a bounded circular tail without advancing rejection authority."""
    candidates = batch.continuation_candidates
    anchor_position = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if candidate.lane == anchor.lane
            and candidate.lane_index == anchor.lane_index
            and candidate.name == anchor.name
        ),
        -1,
    )
    tail = candidates[anchor_position + 1 :] if anchor_position >= 0 else ()
    identity_parts = tuple(
        f"{candidate.lane}:{candidate.lane_index}:{candidate.lane_order_sha256}:{candidate.name}"
        for candidate in tail
    )
    order_sha256 = _sha256(
        "\0".join(
            (
                CHECK_CONTRACT_VERSION,
                "uncertain-continuation",
                anchor.lane,
                str(anchor.lane_index),
                anchor.lane_order_sha256,
                anchor.name,
                *identity_parts,
            )
        )
    )
    scope = str(scope_key or "").strip()
    revision = _valid_digest(source_revision_sha256)
    raw = state.get(CONTINUATION_STATE_KEY)
    record = dict(raw) if isinstance(raw, Mapping) else {}
    try:
        recorded_anchor_index = int(record["anchor_lane_index"])
    except (KeyError, TypeError, ValueError):
        recorded_anchor_index = -1
    matching = bool(
        record.get("schema_version") == SCHEMA_VERSION
        and str(record.get("check_contract_version", "") or "") == CHECK_CONTRACT_VERSION
        and str(record.get("scope_key", "") or "").strip() == scope
        and _valid_digest(record.get("source_revision_sha256")) == revision
        and _valid_digest(record.get("order_sha256")) == order_sha256
        and str(record.get("anchor_lane", "") or "") == anchor.lane
        and recorded_anchor_index == anchor.lane_index
    )
    offset = _cursor(record.get("next_offset"), size=len(tail)) if matching else 0
    if offset >= len(tail):
        offset = 0
    count = min(max(0, int(limit)), len(tail))
    selected = tuple(tail[(offset + index) % len(tail)] for index in range(count)) if tail else ()
    return SourceNegationContinuationWindow(
        candidates=selected,
        order_sha256=order_sha256,
        start_offset=offset,
        tail_size=len(tail),
        anchor_lane=anchor.lane,
        anchor_lane_index=anchor.lane_index,
    )


def record_uncertain_continuation_attempts(
    state: MutableMapping[str, Any],
    *,
    scope_key: str,
    source_revision_sha256: str,
    window: SourceNegationContinuationWindow,
    attempted_count: int,
) -> None:
    """Rotate scheduling only after later candidates were actually checked."""
    attempted = min(len(window.candidates), max(0, int(attempted_count)))
    revision = _valid_digest(source_revision_sha256)
    scope = str(scope_key or "").strip()
    if not attempted or not revision or not scope or not window.tail_size:
        return
    state[CONTINUATION_STATE_KEY] = {
        "schema_version": SCHEMA_VERSION,
        "check_contract_version": CHECK_CONTRACT_VERSION,
        "scope_key": scope,
        "source_revision_sha256": revision,
        "order_sha256": window.order_sha256,
        "anchor_lane": window.anchor_lane,
        "anchor_lane_index": window.anchor_lane_index,
        "next_offset": (window.start_offset + attempted) % window.tail_size,
    }


def record_definitive_incompatibility(
    state: MutableMapping[str, Any],
    *,
    scope_key: str,
    source_revision_sha256: str,
    scheduled: ScheduledSourceNegationCandidate,
) -> bool:
    """Advance one monotonic lane cursor after explicit candidate incompatibility."""
    scope = str(scope_key or "").strip()
    revision = _valid_digest(source_revision_sha256)
    lane = str(scheduled.lane or "").strip()
    if lane not in {"exact", "generic"} or not scope or not revision:
        return False
    process = _matching_record(
        state.get(PROCESS_STATE_KEY),
        scope_key=scope,
        source_revision_sha256=revision,
    )
    if process is None:
        return False
    if _valid_digest(process.get(f"{lane}_order_sha256")) != scheduled.lane_order_sha256:
        return False
    current_cursor = _cursor(process.get(f"{lane}_cursor"), size=scheduled.lane_index + 1)
    if current_cursor > scheduled.lane_index:
        return True
    if current_cursor != scheduled.lane_index:
        return False
    updated = dict(process)
    updated[f"{lane}_cursor"] = scheduled.lane_index + 1
    state[PROCESS_STATE_KEY] = updated
    if not plan_state.plan_state_enabled():
        return True

    def mutate(summary: dict[str, Any]) -> None:
        raw_records = summary.get(SUMMARY_KEY)
        records = (
            [dict(raw) for raw in raw_records[-_SCOPE_CAP:] if isinstance(raw, Mapping)]
            if isinstance(raw_records, list)
            else []
        )
        retained = [
            record for record in records if str(record.get("scope_key", "") or "").strip() != scope
        ]
        durable: dict[str, Any] | None = None
        for record in reversed(records):
            if str(record.get("scope_key", "") or "").strip() != scope:
                continue
            durable = _matching_record(
                record,
                scope_key=scope,
                source_revision_sha256=revision,
            )
            break
        merged = dict(updated)
        for cursor_lane in ("exact", "generic"):
            order_field = f"{cursor_lane}_order_sha256"
            cursor_field = f"{cursor_lane}_cursor"
            if durable is not None and _valid_digest(durable.get(order_field)) == _valid_digest(
                merged.get(order_field)
            ):
                merged[cursor_field] = max(
                    _cursor(durable.get(cursor_field), size=2**31 - 1),
                    _cursor(merged.get(cursor_field), size=2**31 - 1),
                )
        merged["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        retained.append(merged)
        summary[SUMMARY_KEY] = retained[-_SCOPE_CAP:]
        summary["version"] = 1

    try:
        update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    except Exception:
        # The process cursor still prevents immediate repetition. Losing the
        # durable optimization cannot weaken the exact Lean promotion gate.
        pass
    return True
