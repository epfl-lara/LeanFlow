"""Build bounded assignment history for process-isolated research workers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any

from leanflow_cli.workflows import (
    conditional_helper_progress,
    finite_branch_progress,
    research_semantic_identity,
    target_handoff,
)
from leanflow_cli.workflows.dispatch_models import (
    ASSIGNMENT_REVISION_INPUT_KEY,
    LedgerEntry,
)
from leanflow_cli.workflows.workflow_json_io import read_json_file
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

CONTEXT_VERSION = 3
RECENT_RESEARCH_ROUTE_LIMIT = 4
RECENT_ORCHESTRATOR_ROUTE_LIMIT = 4
RECENT_FAILED_PROOF_SHAPE_LIMIT = 4
JOURNAL_TAIL_MAX_BYTES = 512 * 1024
ROUTE_CONTEXT_JSON_MAX_BYTES = 10_000
ROUTE_CONTEXT_OBJECTIVE_MAX_BYTES = 8_000
SEMANTIC_KNOWLEDGE_LIMIT = 40
VERIFIED_MECHANISM_LIMIT = 8
CONSUMED_TARGET_FACT_LIMIT = 4
SEMANTIC_NOVELTY_VERSION = 9

ROUTE_CONTEXT_INPUT_KEY = "recent_route_context"
ROUTE_CONTEXT_SHA256_INPUT_KEY = "recent_route_context_sha256"
PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY = "parent_route_context"
ROUTE_CONTEXT_MARKER = "[LEANFLOW BOUNDED PARENT ROUTE CONTEXT]"

_CONCRETE_T_RE = re.compile(r"\bt\s*=\s*(\d+)\b(?!\s*[*+/\-^])")
_CONGRUENCE_RE = re.compile(r"\bt\s*%\s*(\d+)\s*=\s*(\d+)\b")
_LEAN_DECLARATION_RE = re.compile(
    r"^\s*((?:(?:private|protected|noncomputable)\s+)*(?:lemma|theorem))\s+" r"[^\s(:]+",
    flags=re.DOTALL,
)
_LEAN_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_'.]*|\d+|:=|[^\s]")
_MECHANISM_TOKENS = frozenset(
    {
        "Nat.mod_add_div",
        "apply",
        "by_cases",
        "erdos_242_factor_pair_certificate",
        "erdos_242_of_nonresidual_factor",
        "exact",
        "have",
        "intro",
        "linarith",
        "nlinarith",
        "norm_num",
        "obtain",
        "omega",
        "rcases",
        "refine",
        "ring",
        "rw",
        "simp",
        "simpa",
        "subst",
    }
)
_CHECKED_CONTAINER_KEYS = frozenset(
    {
        "checked_candidate_helper",
        "checked_helper",
        "checked_proof_delta",
        "checked_replacement",
    }
)
_CHECKED_CODE_KEYS = frozenset(
    {
        "candidate_statement",
        "checked_proof",
        "checked_code",
        "declaration",
        "proof",
        "proposed_helper",
        "replacement",
        "statement",
    }
)
_SEMANTIC_IGNORED_KEYS = frozenset(
    {
        "anchor_consumed",
        "checked_replacement_contract_issues",
        "compared_against",
        "files_created_or_modified",
        "files_modified",
        "issues",
        "issues_encountered",
        "parent_recheck_required",
        "parent_route_context",
        "reported_status",
        "verification_caveat",
        "verification_note",
    }
)
_OBSTRUCTION_KEY_PARTS = (
    "counterexample",
    "countermodel",
    "obstruction",
    "unsupported_assumption",
)
_OBSTRUCTION_PROOF_SHAPE_RE = re.compile(
    r"\b(?:counterexample|countermodel|obstruction)\b",
    flags=re.IGNORECASE,
)
_WITNESS_PATH_PARTS = (
    "candidate",
    "checked",
    "concrete",
    "construction",
    "counter",
    "delta",
    "new_dependency",
    "normal_form",
    "probe",
    "reachable",
    "synthesized",
    "unresolved_dependency",
    "witness",
)
_CONGRUENCE_PATH_PARTS = (
    "candidate",
    "checked",
    "construction",
    "coverage_delta",
    "declaration",
    "helper",
    "proof",
    "replacement",
)
_NEGATIVE_CONGRUENCE_PATH_PARTS = (
    "counter",
    "failure",
    "noncoverage",
    "obstruction",
    "residue",
    "survives",
    "unmatched",
    "unresolved",
)
_OPERATIONAL_STATUS_VALUES = frozenset(
    {
        "backend_error",
        "cancelled",
        "error",
        "failed",
        "infrastructure_error",
        "malformed_response",
        "provider_error",
        "provider_timeout",
        "rate_limited",
        "timed_out",
        "timeout",
        "unavailable",
    }
)
_OPERATIONAL_ERROR_KEYS = frozenset(
    {
        "backend_error",
        "error",
        "exception",
        "failure_reason",
        "infrastructure_error",
        "provider_error",
    }
)
_OPERATIONAL_ERROR_RE = re.compile(
    r"(?:\bprovider\b.{0,32}\b(?:error|failed|timeout|timed out|unavailable)\b|"
    r"\b(?:api|backend|infrastructure)\s+(?:error|failure|timeout)\b|"
    r"\b(?:connection (?:error|failed|reset)|rate limit(?:ed)?|timed out)\b|"
    r"\bmalformed (?:json|provider response)\b)",
    flags=re.IGNORECASE,
)
_RESULT_STATUS_KEYS = (
    "status",
    "reported_status",
    "checked_replacement_status",
)
_NONCLOSING_RESULT_STATUS_MARKERS = (
    "evidence_only",
    "finite_coverage_only",
    "partial",
    "incomplete",
    "not_complete",
    "not_completed",
    "not_completion",
    "not_proof_complete",
    "not_proof_completed",
    "not_proof_completion",
    "noncomplete",
    "noncompleted",
    "noncompletion",
    "nonclosing",
    "non_closing",
    "research_only",
)
_DECLARED_NONCLOSING_STATUS_MARKERS = (
    "nonclosing",
    "non_closing",
)
_DECLARED_FINITE_EVIDENCE_STATUS_MARKERS = (
    "bounded_instance",
    "finite_coverage",
    "finite_instance",
    "fixed_instance",
    "singleton",
)
_NESTED_STATUS_PAYLOAD_KEYS = frozenset(
    {
        "deliverable",
        "finding",
        "findings_report",
        "report",
        "result",
        "summary",
    }
)


@dataclass(frozen=True)
class SemanticEvidence:
    """Hold deterministic parent-derived research semantics for one job."""

    witnesses: tuple[int, ...] = ()
    congruences: tuple[tuple[int, int], ...] = ()
    helper_statements: tuple[str, ...] = ()
    obstructions: tuple[str, ...] = ()
    mechanisms: tuple[str, ...] = ()
    proof_shapes: tuple[str, ...] = ()
    has_checked_helper: bool = False
    malformed: bool = False

    @property
    def fingerprints(self) -> tuple[str, ...]:
        """Return stable human-readable fingerprints for durable deduplication."""
        values = [f"witness:t={value}" for value in self.witnesses]
        values.extend(f"congruence:t%{modulus}={residue}" for modulus, residue in self.congruences)
        values.extend(f"helper-statement:{value}" for value in self.helper_statements)
        values.extend(f"obstruction:{value}" for value in self.obstructions)
        values.extend(f"mechanism:{value}" for value in self.mechanisms)
        values.extend(f"proof-shape:{value}" for value in self.proof_shapes)
        return tuple(sorted(set(values)))


def _bounded_text(value: Any, limit: int) -> str:
    """Return compact text that fits one UTF-8 byte budget."""
    compact = " ".join(str(value or "").split())
    return _bounded_utf8(compact, limit)


def _bounded_utf8(value: Any, limit: int) -> str:
    """Return text that fits one UTF-8 byte budget without changing layout."""
    text = str(value or "")
    encoded = text.encode("utf-8")
    bounded = max(8, int(limit))
    if len(encoded) <= bounded:
        return text
    prefix = encoded[: bounded - 3].decode("utf-8", errors="ignore").rstrip()
    return prefix + "..."


def _canonical_json(value: Mapping[str, Any]) -> str:
    """Serialize route context deterministically for bounds and provenance."""
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _same_file(left: str, right: str) -> bool:
    """Return whether two non-empty assignment paths resolve identically."""
    if not left or not right:
        return left == right
    return os.path.realpath(left) == os.path.realpath(right)


def _same_symbol(left: str, right: str) -> bool:
    """Return whether exact or namespace-qualified declaration names agree."""
    left_name = str(left or "").strip()
    right_name = str(right or "").strip()
    if not left_name or not right_name:
        return left_name == right_name
    return left_name == right_name or left_name.rsplit(".", 1)[-1] == right_name.rsplit(".", 1)[-1]


def _entry_matches_assignment(
    entry: LedgerEntry,
    *,
    target_symbol: str,
    active_file: str,
) -> bool:
    """Return whether one research entry belongs to the exact live assignment."""
    inputs = dict(entry.spec.inputs or {})
    if str(inputs.get("target_symbol", "") or "") != str(target_symbol or ""):
        return False
    return _same_file(
        str(inputs.get("active_file", "") or ""),
        str(active_file or ""),
    )


def strip_parent_route_context(deliverable: Mapping[str, Any] | None) -> dict[str, Any]:
    """Remove parent-authored context before treating a report as new evidence."""
    payload = dict(deliverable or {})
    payload.pop(PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY, None)
    return payload


def _normalized_status_value(value: Any) -> str:
    """Return one case-folded status with stable word separators."""
    return re.sub(r"[\s-]+", "_", str(value or "").strip().casefold())


def _status_value_has_marker(value: Any, markers: Sequence[str]) -> bool:
    """Return whether one normalized status contains a requested marker."""
    normalized = _normalized_status_value(value)
    return any(marker in normalized for marker in markers)


def _result_has_status_marker(
    deliverable: Mapping[str, Any] | None,
    markers: Sequence[str],
) -> bool:
    """Return whether nested result metadata contains one normalized status marker."""

    def visit(value: Any, *, depth: int = 0) -> bool:
        if depth > 8:
            return False
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = str(raw_key).strip().casefold()
                status_key = key in _RESULT_STATUS_KEYS or key.endswith("_status")
                if status_key and not isinstance(item, (Mapping, list, tuple)):
                    if _status_value_has_marker(item, markers):
                        return True
                if isinstance(item, (Mapping, list, tuple)) and visit(item, depth=depth + 1):
                    return True
                if key in _NESTED_STATUS_PAYLOAD_KEYS and isinstance(item, str):
                    text = item.strip()
                    if text.startswith(("{", "[")):
                        try:
                            decoded = json.loads(text)
                        except (TypeError, ValueError):
                            decoded = None
                        if decoded is not None and visit(decoded, depth=depth + 1):
                            return True
            return False
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(visit(item, depth=depth + 1) for item in value)
        return False

    return visit(dict(deliverable or {}))


def explicitly_nonclosing_result(deliverable: Mapping[str, Any] | None) -> bool:
    """Return whether nested status metadata labels a result non-closing.

    Workers sometimes serialize a report as JSON inside ``summary``. Inspect
    status-like keys recursively so those reports receive the same containment
    as ordinary structured deliverables without interpreting arbitrary prose.
    """

    return _result_has_status_marker(deliverable, _NONCLOSING_RESULT_STATUS_MARKERS)


def explicitly_declared_nonclosing_result(
    deliverable: Mapping[str, Any] | None,
) -> bool:
    """Return whether result status literally declares the route non-closing.

    Keep this narrower than :func:`explicitly_nonclosing_result`: generic
    partial-result labels still control prompt containment, but do not erase
    genuine mathematical novelty unless the worker explicitly says the result
    is non-closing.
    """
    return _result_has_status_marker(deliverable, _DECLARED_NONCLOSING_STATUS_MARKERS)


def explicitly_declared_finite_evidence_result(
    deliverable: Mapping[str, Any] | None,
) -> bool:
    """Return whether status metadata declares bounded finite-instance evidence.

    Fixed and bounded witnesses remain useful deduplication facts, but their
    fresh helper names or proof syntax cannot make them general target progress.
    """
    return _result_has_status_marker(
        deliverable,
        _DECLARED_FINITE_EVIDENCE_STATUS_MARKERS,
    )


def _semantic_payload(value: Any) -> Any:
    """Remove provenance and commentary fields before semantic extraction."""
    if isinstance(value, Mapping):
        return {
            str(key): _semantic_payload(item)
            for key, item in value.items()
            if str(key) not in _SEMANTIC_IGNORED_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_semantic_payload(item) for item in value]
    return value


def _walk_semantic_values(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], Any]]:
    """Return scalar semantic values with their normalized mapping paths."""
    found: list[tuple[tuple[str, ...], Any]] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold()
            if key in _SEMANTIC_IGNORED_KEYS:
                continue
            found.extend(_walk_semantic_values(item, path=(*path, key)))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            found.extend(_walk_semantic_values(item, path=path))
    elif isinstance(value, (str, int)):
        found.append((path, value))
    return found


def _canonical_semantic_value(value: Any) -> str:
    """Return bounded canonical JSON for exact obstruction fingerprints."""
    compact, malformed = research_semantic_identity.canonical_semantic_value(
        _semantic_payload(value)
    )
    if malformed:
        return ""
    return _bounded_utf8(compact, 4000)


def _valid_worker_check(value: Any) -> bool:
    """Return whether a checked-code container carries a successful Lean check."""
    if not isinstance(value, Mapping):
        return False
    payload = dict(value)
    if payload.get("valid_without_sorry") is True:
        return payload.get("has_errors") is not True and payload.get("has_sorry") is not True
    if payload.get("has_errors") is False and payload.get("has_sorry") is False:
        return True
    for key in ("worker_check", "verification", "result"):
        if _valid_worker_check(payload.get(key)):
            return True
    return False


def _code_values(value: Any) -> list[str]:
    """Return declaration or proof strings from one checked container."""
    values: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold()
            if key in _CHECKED_CODE_KEYS and isinstance(item, str) and item.strip():
                values.append(item)
            elif isinstance(item, (Mapping, list, tuple)):
                values.extend(_code_values(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            values.extend(_code_values(item))
    return values


def _checked_code_candidates(
    deliverable: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    """Return valid checked code, normalized statements, and helper status."""
    incomplete = (
        str(deliverable.get("status", "") or "").strip().casefold() == "incomplete_unverified"
    )
    if incomplete:
        # Replacement-contract enforcement may downgrade the enclosing report
        # after independently checked helper artifacts were attached. Preserve
        # only that canonical parent-owned container; model-authored checked_*
        # aliases remain quarantined by the report-level downgrade.
        checked_helpers = deliverable.get("checked_helpers")
        if not isinstance(checked_helpers, (Mapping, list, tuple)):
            return (), (), False
        checked_source: Mapping[str, Any] = {"checked_helpers": checked_helpers}
    else:
        checked_source = deliverable

    values: list[str] = []
    statements: list[str] = []

    def record_checked(candidate: Mapping[str, Any]) -> None:
        """Record one independently validated checked-code container."""
        if not _valid_worker_check(candidate):
            return
        candidate_values = _code_values(candidate)
        values.extend(candidate_values)
        declarations = [
            statement
            for code in candidate_values
            if (statement := _lean_declaration_statement(code))
        ]
        statements.extend(declarations)
        bare_statement = candidate.get("statement") or candidate.get("candidate_statement")
        bare_proof = candidate.get("proof") or candidate.get("checked_proof")
        if (
            isinstance(bare_statement, str)
            and bare_statement.strip()
            and isinstance(bare_proof, str)
            and bare_proof.strip()
        ):
            statements.append("statement " + " ".join(bare_statement.split()))

    def visit(value: Any) -> None:
        if not isinstance(value, Mapping):
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    visit(item)
            return
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold()
            if key.startswith("unchecked"):
                # Contract failures preserve exact code here for diagnosis and
                # foreground research, but it is not checked-helper evidence.
                continue
            if key == "checked_replacements":
                candidates = (
                    item
                    if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray))
                    else (item,)
                )
                for candidate in candidates:
                    worker_check = (
                        dict(candidate.get("worker_check") or {})
                        if isinstance(candidate, Mapping)
                        else {}
                    )
                    if (
                        isinstance(candidate, Mapping)
                        and worker_check.get("replacement_matches_target") is True
                    ):
                        record_checked(candidate)
                continue
            elif key in _CHECKED_CONTAINER_KEYS or key.startswith("checked_") or "_checked_" in key:
                candidates = (
                    item
                    if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray))
                    else (item,)
                )
                for candidate in candidates:
                    if isinstance(candidate, Mapping):
                        record_checked(candidate)
            visit(item)

    visit(checked_source)
    deduplicated = tuple(dict.fromkeys(value for value in values if value.strip()))
    normalized_statements = tuple(dict.fromkeys(value for value in statements if value.strip()))
    return deduplicated, normalized_statements, bool(normalized_statements)


def _has_checked_target_replacement(
    deliverable: Mapping[str, Any],
    *,
    target_symbol: str,
) -> bool:
    """Return whether a worker supplied a contract-valid target replacement."""
    raw_candidates = deliverable.get("checked_replacements")
    candidates = (
        raw_candidates
        if isinstance(raw_candidates, Sequence)
        and not isinstance(raw_candidates, (str, bytes, bytearray))
        else (raw_candidates,)
    )
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or not _valid_worker_check(candidate):
            continue
        worker_check = candidate.get("worker_check")
        if not isinstance(worker_check, Mapping):
            continue
        if worker_check.get("replacement_matches_target") is not True:
            continue
        candidate_target = str(candidate.get("target_symbol", "") or "").strip()
        if candidate_target and candidate_target != str(target_symbol or "").strip():
            continue
        return True
    return False


def _lean_declaration_statement(code: str) -> str:
    """Return a name-independent normalized Lean declaration statement."""
    text = str(code or "").strip()
    if not _LEAN_DECLARATION_RE.search(text):
        return ""
    proof_boundary = re.search(r"\s:=\s*by\b", text)
    header = text[: proof_boundary.start()] if proof_boundary else text
    header = _LEAN_DECLARATION_RE.sub(r"\1 $declaration", header, count=1)
    return " ".join(header.split())


def _mechanism_shape(code: str) -> str:
    """Return a residue-agnostic checked proof-mechanism fingerprint."""
    text = str(code or "")
    proof_boundary = re.search(r"\s:=\s*by\b", text)
    body = text[proof_boundary.end() :] if proof_boundary else text
    signals: list[str] = []
    normalized: list[str] = []
    for token in _LEAN_TOKEN_RE.findall(body):
        if token.isdigit():
            normalized.append("$num")
        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'.]*", token):
            if token in _MECHANISM_TOKENS or token.startswith("erdos_242_"):
                normalized.append(token)
                if token not in signals:
                    signals.append(token)
            elif "." in token and token[:1].isupper():
                normalized.append(token)
                if token not in signals:
                    signals.append(token)
            else:
                normalized.append("$id")
        else:
            normalized.append(token)
    if not signals:
        return ""
    digest = hashlib.sha256(" ".join(normalized).encode("utf-8")).hexdigest()[:12]
    label = "+".join(sorted(signals))[:180]
    return f"{label}:{digest}"


def _obstruction_fingerprints(value: Any) -> tuple[str, ...]:
    """Return exact deterministic fingerprints for explicit obstruction fields."""
    fingerprints: list[str] = []
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold()
            explicit_obstruction = any(part in key for part in _OBSTRUCTION_KEY_PARTS)
            checked_obstruction_shape = bool(
                key in {"new_proof_shape", "proof_shape"}
                and isinstance(item, str)
                and _OBSTRUCTION_PROOF_SHAPE_RE.search(item)
            )
            if explicit_obstruction or checked_obstruction_shape:
                canonical = _canonical_semantic_value(item)
                if canonical:
                    fingerprints.append(hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20])
            if key not in _SEMANTIC_IGNORED_KEYS:
                fingerprints.extend(_obstruction_fingerprints(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            fingerprints.extend(_obstruction_fingerprints(item))
    return tuple(sorted(set(fingerprints)))


def _path_has_part(path: Sequence[str], parts: Sequence[str]) -> bool:
    """Return whether any normalized path component contains one marker."""
    return any(marker in component for component in path for marker in parts)


def _witness_bearing_path(path: Sequence[str]) -> bool:
    """Return whether a prose field is intended to report concrete evidence."""
    return _path_has_part(path, _WITNESS_PATH_PARTS) and not any(
        "integration" in component for component in path
    )


def _congruence_bearing_path(path: Sequence[str]) -> bool:
    """Return whether a prose field states positive construction coverage."""
    return _path_has_part(path, _CONGRUENCE_PATH_PARTS) and not _path_has_part(
        path,
        _NEGATIVE_CONGRUENCE_PATH_PARTS,
    )


def _positive_congruences(value: str) -> list[tuple[int, int]]:
    """Return positive literal residue equalities, excluding negated hypotheses."""
    congruences: list[tuple[int, int]] = []
    for match in _CONGRUENCE_RE.finditer(value):
        prefix = value[max(0, match.start() - 8) : match.start()].rstrip().casefold()
        if prefix.endswith("¬") or prefix.endswith("not"):
            continue
        modulus = int(match.group(1))
        residue = int(match.group(2))
        if modulus > 0 and residue < modulus:
            congruences.append((modulus, residue))
    return congruences


def semantic_evidence(entry: LedgerEntry) -> SemanticEvidence:
    """Derive bounded cross-archetype semantics from one worker deliverable."""
    raw_deliverable = entry.result.get("deliverable")
    deliverable = strip_parent_route_context(
        raw_deliverable if isinstance(raw_deliverable, Mapping) else None
    )
    payload = _semantic_payload(deliverable)
    shape_identities = research_semantic_identity.proof_shape_identities(raw_deliverable)
    witnesses: set[int] = set()
    congruences: set[tuple[int, int]] = set()
    for path, value in _walk_semantic_values(payload):
        if path and path[-1] in {"t", "normal_form_t"} and isinstance(value, int) and value >= 0:
            witnesses.add(value)
        if not isinstance(value, str):
            continue
        if _witness_bearing_path(path):
            witnesses.update(int(match.group(1)) for match in _CONCRETE_T_RE.finditer(value))
        if _congruence_bearing_path(path):
            congruences.update(_positive_congruences(value))

    checked_code, checked_statements, has_checked_helper = _checked_code_candidates(deliverable)
    status = str(deliverable.get("status", "") or "").strip().casefold()
    helper_statements = {
        hashlib.sha256(statement.encode("utf-8")).hexdigest()[:20]
        for statement in checked_statements
    }
    mechanisms: set[str] = set()
    for code in checked_code:
        statement = _lean_declaration_statement(code)
        if statement and status != "incomplete_unverified":
            helper_statements.add(hashlib.sha256(statement.encode("utf-8")).hexdigest()[:20])
        mechanism = _mechanism_shape(code)
        if mechanism:
            mechanisms.add(mechanism)
    return SemanticEvidence(
        witnesses=tuple(sorted(witnesses)),
        congruences=tuple(sorted(congruences)),
        helper_statements=tuple(sorted(helper_statements)),
        obstructions=_obstruction_fingerprints(payload),
        mechanisms=tuple(sorted(mechanisms)),
        proof_shapes=shape_identities.values,
        has_checked_helper=has_checked_helper,
        malformed=shape_identities.malformed,
    )


def _semantic_evidence_with_cache(
    entry: LedgerEntry,
    evidence_cache: MutableMapping[int, tuple[LedgerEntry, SemanticEvidence]] | None,
) -> SemanticEvidence:
    """Return semantic evidence while reusing one traversal per entry object."""
    if evidence_cache is None:
        return semantic_evidence(entry)
    cache_key = id(entry)
    cached = evidence_cache.get(cache_key)
    if cached is not None and cached[0] is entry:
        return cached[1]
    evidence = semantic_evidence(entry)
    # Retain the entry beside its evidence so CPython cannot recycle an object
    # id and accidentally alias two normalized findings during one migration.
    evidence_cache[cache_key] = (entry, evidence)
    return evidence


def _operational_error_reason(
    entry: LedgerEntry,
    deliverable: Mapping[str, Any],
) -> str:
    """Return a deterministic operational-failure reason, if one is present."""
    if entry.state in {"failed", "stuck", "killed"}:
        return f"terminal_state:{entry.state}"
    outer_status = str(entry.result.get("status", "") or "").strip().casefold()
    if outer_status in _OPERATIONAL_STATUS_VALUES:
        return f"result_status:{outer_status}"
    deliverable_status = str(deliverable.get("status", "") or "").strip().casefold()
    if deliverable_status in _OPERATIONAL_STATUS_VALUES:
        return f"deliverable_status:{deliverable_status}"
    for path, value in _walk_semantic_values(deliverable):
        if not isinstance(value, str) or not value.strip():
            continue
        if path and path[-1] in _OPERATIONAL_ERROR_KEYS:
            return f"error_field:{path[-1]}"
        if _OPERATIONAL_ERROR_RE.search(value):
            return "operational_error_prose"
    if _OPERATIONAL_ERROR_RE.search(str(entry.notes or "")):
        return "operational_error_notes"
    return ""


def _has_preserved_route_boundary(deliverable: Mapping[str, Any]) -> bool:
    """Return whether a managed boundary contains non-operational tool evidence."""
    if str(deliverable.get("status", "") or "").strip().casefold() != ("interrupted_with_evidence"):
        return False
    raw_boundary = deliverable.get("route_boundary")
    if not isinstance(raw_boundary, Mapping):
        return False
    try:
        completed_tool_calls = int(raw_boundary.get("completed_tool_calls", 0) or 0)
    except (TypeError, ValueError):
        return False
    if completed_tool_calls <= 0:
        return False
    for item in raw_boundary.get("evidence") or []:
        if not isinstance(item, Mapping):
            continue
        excerpt = str(item.get("result_excerpt", "") or "").strip()
        if excerpt and not _OPERATIONAL_ERROR_RE.search(excerpt):
            return True
    return False


def semantic_result_is_operational_error(
    entry: LedgerEntry,
    *,
    evidence_cache: MutableMapping[int, tuple[LedgerEntry, SemanticEvidence]] | None = None,
) -> bool:
    """Return whether one result contains only an unpreserved operational failure.

    Match the history-independent ``operational_error`` branch of semantic
    novelty classification without constructing or comparing assignment
    history. Malformed evidence remains classified as malformed, while any
    extracted mathematical semantics and managed route-boundary evidence
    survive an accompanying provider or process error.
    """
    evidence = _semantic_evidence_with_cache(entry, evidence_cache)
    if evidence.malformed:
        return False
    raw_deliverable = entry.result.get("deliverable")
    deliverable = strip_parent_route_context(
        raw_deliverable if isinstance(raw_deliverable, Mapping) else None
    )
    # A nested research tool may fail after the worker has already derived a
    # usable obstruction or proof shape; that local failure cannot erase it.
    semantic_present = bool(evidence.fingerprints)
    return bool(
        _operational_error_reason(entry, deliverable)
        and not semantic_present
        and not _has_preserved_route_boundary(deliverable)
    )


def _congruence_subsumes(
    prior: tuple[int, int],
    current: tuple[int, int],
) -> bool:
    """Return whether the prior residue class contains the current class."""
    prior_modulus, prior_residue = prior
    current_modulus, current_residue = current
    return (
        prior_modulus > 0
        and current_modulus % prior_modulus == 0
        and current_residue % prior_modulus == prior_residue
    )


def semantic_anchor_superseded(
    entry: LedgerEntry,
    entries: Sequence[LedgerEntry],
) -> bool:
    """Return whether a later checked helper covers this entry's concrete primary facts."""
    evidence = semantic_evidence(entry)
    if evidence.malformed:
        return True
    primary_present = bool(evidence.witnesses or evidence.congruences)
    if not primary_present:
        return False
    inputs = dict(entry.spec.inputs or {})
    target_symbol = str(inputs.get("target_symbol", "") or "")
    active_file = str(inputs.get("active_file", "") or "")
    found = False
    for candidate in entries:
        if candidate.spec.job_id == entry.spec.job_id:
            found = True
            continue
        if (
            not found
            or not candidate.is_terminal()
            or not _entry_matches_assignment(
                candidate,
                target_symbol=target_symbol,
                active_file=active_file,
            )
        ):
            continue
        later = semantic_evidence(candidate)
        if later.malformed or not later.has_checked_helper:
            continue
        witnesses_covered = all(
            witness in later.witnesses
            or any(witness % modulus == residue for modulus, residue in later.congruences)
            for witness in evidence.witnesses
        )
        congruences_covered = all(
            any(_congruence_subsumes(covering, congruence) for covering in later.congruences)
            for congruence in evidence.congruences
        )
        if witnesses_covered and congruences_covered:
            return True
    return False


def _prior_assignment_entries(
    entry: LedgerEntry,
    entries: Sequence[LedgerEntry],
) -> list[LedgerEntry]:
    """Return terminal exact-assignment entries preceding one ledger entry."""
    inputs = dict(entry.spec.inputs or {})
    target_symbol = str(inputs.get("target_symbol", "") or "")
    active_file = str(inputs.get("active_file", "") or "")
    prior: list[LedgerEntry] = []
    found = False
    for candidate in entries:
        if candidate.spec.job_id == entry.spec.job_id:
            found = True
            break
        if candidate.is_terminal() and _entry_matches_assignment(
            candidate,
            target_symbol=target_symbol,
            active_file=active_file,
        ):
            prior.append(candidate)
    if found:
        return prior
    return [
        candidate
        for candidate in entries
        if candidate.spec.job_id != entry.spec.job_id
        and candidate.is_terminal()
        and _entry_matches_assignment(
            candidate,
            target_symbol=target_symbol,
            active_file=active_file,
        )
    ]


def classify_semantic_novelty(
    entry: LedgerEntry,
    entries: Sequence[LedgerEntry],
    *,
    evidence_cache: MutableMapping[int, tuple[LedgerEntry, SemanticEvidence]] | None = None,
) -> dict[str, Any]:
    """Classify one result against all prior exact-assignment research lanes."""
    evidence = _semantic_evidence_with_cache(entry, evidence_cache)
    fingerprints = set(evidence.fingerprints)
    prior_entries = _prior_assignment_entries(entry, entries)
    prior_evidence = [
        (candidate, candidate_evidence)
        for candidate in prior_entries
        if not (
            candidate_evidence := _semantic_evidence_with_cache(candidate, evidence_cache)
        ).malformed
    ]
    prior_fingerprints = {
        fingerprint
        for _candidate, candidate_evidence in prior_evidence
        for fingerprint in candidate_evidence.fingerprints
    }
    duplicate = fingerprints.intersection(prior_fingerprints)
    subsumed: set[str] = set()
    subsumed_by: set[str] = set()

    prior_witnesses = {
        witness: candidate.spec.job_id
        for candidate, candidate_evidence in prior_evidence
        for witness in candidate_evidence.witnesses
    }
    prior_congruences = [
        (congruence, candidate.spec.job_id)
        for candidate, candidate_evidence in prior_evidence
        for congruence in candidate_evidence.congruences
    ]
    novel_witnesses: set[int] = set()
    for witness in evidence.witnesses:
        fingerprint = f"witness:t={witness}"
        if witness in prior_witnesses:
            subsumed_by.add(prior_witnesses[witness])
            continue
        covering_job = next(
            (
                job_id
                for (modulus, residue), job_id in prior_congruences
                if witness % modulus == residue
            ),
            "",
        )
        if covering_job:
            subsumed.add(fingerprint)
            subsumed_by.add(covering_job)
        else:
            novel_witnesses.add(witness)

    novel_congruences: set[tuple[int, int]] = set()
    for congruence in evidence.congruences:
        fingerprint = f"congruence:t%{congruence[0]}={congruence[1]}"
        if fingerprint in prior_fingerprints:
            continue
        covering_job = next(
            (
                job_id
                for prior_congruence, job_id in prior_congruences
                if _congruence_subsumes(prior_congruence, congruence)
            ),
            "",
        )
        if covering_job:
            subsumed.add(fingerprint)
            subsumed_by.add(covering_job)
        else:
            novel_congruences.add(congruence)

    novel_helpers = {
        fingerprint
        for fingerprint in fingerprints
        if fingerprint.startswith("helper-statement:") and fingerprint not in prior_fingerprints
    }
    novel_obstructions = {
        fingerprint
        for fingerprint in fingerprints
        if fingerprint.startswith("obstruction:") and fingerprint not in prior_fingerprints
    }
    novel_mechanisms = {
        fingerprint
        for fingerprint in fingerprints
        if fingerprint.startswith("mechanism:") and fingerprint not in prior_fingerprints
    }
    novel_proof_shapes = {
        fingerprint
        for fingerprint in fingerprints
        if fingerprint.startswith("proof-shape:") and fingerprint not in prior_fingerprints
    }
    prior_mechanisms = {
        mechanism
        for _candidate, candidate_evidence in prior_evidence
        for mechanism in candidate_evidence.mechanisms
    }
    repeated_mechanisms = set(evidence.mechanisms).intersection(prior_mechanisms)
    repeated_mechanism_job_ids = {
        candidate.spec.job_id
        for candidate, candidate_evidence in prior_evidence
        if repeated_mechanisms.intersection(candidate_evidence.mechanisms)
    }
    materially_broader_coverage: list[dict[str, str]] = []
    for current in sorted(novel_congruences):
        for candidate, candidate_evidence in prior_evidence:
            if not repeated_mechanisms.intersection(candidate_evidence.mechanisms):
                continue
            for prior in candidate_evidence.congruences:
                if current == prior or not _congruence_subsumes(current, prior):
                    continue
                materially_broader_coverage.append(
                    {
                        "current": f"congruence:t%{current[0]}={current[1]}",
                        "prior": f"congruence:t%{prior[0]}={prior[1]}",
                        "prior_job_id": candidate.spec.job_id,
                    }
                )
    primary_present = bool(evidence.witnesses or evidence.congruences or evidence.helper_statements)
    semantic_present = bool(fingerprints)
    new_primary = bool(novel_witnesses or novel_congruences or novel_helpers)
    raw_deliverable = entry.result.get("deliverable")
    deliverable = strip_parent_route_context(
        raw_deliverable if isinstance(raw_deliverable, Mapping) else None
    )
    operational_error = _operational_error_reason(entry, deliverable)
    preserved_boundary = _has_preserved_route_boundary(deliverable)
    target_symbol = str(dict(entry.spec.inputs or {}).get("target_symbol", "") or "")
    checked_target_replacement = _has_checked_target_replacement(
        deliverable,
        target_symbol=target_symbol,
    )
    declared_nonclosing = explicitly_declared_nonclosing_result(deliverable)
    declared_finite_evidence = explicitly_declared_finite_evidence_result(deliverable)
    checked_code, _checked_statements, _has_checked_code = _checked_code_candidates(deliverable)
    active_file = str(dict(entry.spec.inputs or {}).get("active_file", "") or "")
    circular_obligations = (
        conditional_helper_progress.checked_code_target_assumption_obligations(
            checked_code,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        if evidence.has_checked_helper and not checked_target_replacement
        else ()
    )
    if evidence.malformed:
        classification = "malformed"
    elif operational_error and not semantic_present and not preserved_boundary:
        classification = "operational_error"
    elif declared_nonclosing and not checked_target_replacement:
        classification = "nonclosing"
    elif not fingerprints and preserved_boundary:
        classification = "preserved_evidence"
    elif not fingerprints:
        classification = "unclassified"
    elif new_primary:
        classification = "novel"
    elif primary_present:
        classification = "duplicate" if fingerprints.issubset(prior_fingerprints) else "subsumed"
    elif novel_obstructions or novel_mechanisms or novel_proof_shapes:
        classification = "novel"
    else:
        classification = "duplicate"

    # A fresh residue number is mathematical coverage, but it is not a fresh
    # strategy when its checked code repeats an already-seen mechanism. Keep
    # the finding in the semantic archive while withholding portfolio-anchor
    # authority unless it strictly contains prior verified coverage or closes
    # the assigned target itself.
    novel_mechanism_values = set(evidence.mechanisms).difference(prior_mechanisms)
    repeated_narrow_residue = (
        classification == "novel"
        and evidence.has_checked_helper
        and bool(evidence.congruences)
        and bool(repeated_mechanisms)
        and not novel_mechanism_values
        and not materially_broader_coverage
        and not checked_target_replacement
    )
    if repeated_narrow_residue:
        classification = "mechanism_repeat"

    current_finite_branches = {
        branch.fingerprint: branch
        for branch in finite_branch_progress.branches_from_checked_declarations(
            checked_code,
            target_symbol=target_symbol,
        )
    }
    if not current_finite_branches:
        current_fact_branch = finite_branch_progress.branch_from_facts(
            evidence.witnesses,
            evidence.congruences,
        )
        if current_fact_branch is not None:
            current_finite_branches[current_fact_branch.fingerprint] = current_fact_branch
    prior_finite_branches: dict[str, tuple[str, finite_branch_progress.FiniteBranch]] = {}
    for candidate, candidate_evidence in prior_evidence:
        if not candidate_evidence.has_checked_helper:
            continue
        raw_candidate_deliverable = candidate.result.get("deliverable")
        candidate_deliverable = strip_parent_route_context(
            raw_candidate_deliverable if isinstance(raw_candidate_deliverable, Mapping) else None
        )
        candidate_code, _candidate_statements, _candidate_checked = _checked_code_candidates(
            candidate_deliverable
        )
        candidate_target = str(dict(candidate.spec.inputs or {}).get("target_symbol", "") or "")
        candidate_branches = finite_branch_progress.branches_from_checked_declarations(
            candidate_code,
            target_symbol=candidate_target,
        )
        if not candidate_branches:
            candidate_fact_branch = finite_branch_progress.branch_from_facts(
                candidate_evidence.witnesses,
                candidate_evidence.congruences,
            )
            candidate_branches = (
                (candidate_fact_branch,) if candidate_fact_branch is not None else ()
            )
        for candidate_branch in candidate_branches:
            prior_finite_branches.setdefault(
                candidate_branch.fingerprint,
                (candidate.spec.job_id, candidate_branch),
            )
    if current_finite_branches:
        broader_keys = {
            (current.fingerprint, prior.fingerprint, job_id)
            for current in current_finite_branches.values()
            for job_id, prior in prior_finite_branches.values()
            if finite_branch_progress.strictly_broader_than(current, prior)
        }
        existing_broader = {
            (
                str(record.get("current", "") or ""),
                str(record.get("prior", "") or ""),
                str(record.get("prior_job_id", "") or ""),
            )
            for record in materially_broader_coverage
        }
        materially_broader_coverage.extend(
            {
                "current": current,
                "prior": prior,
                "prior_job_id": job_id,
            }
            for current, prior, job_id in sorted(broader_keys - existing_broader)
        )
    immediate_finite_evidence = any(
        finite_branch_progress.immediate_evidence_branch(branch)
        for branch in current_finite_branches.values()
    )
    contained_finite_branch = bool(
        evidence.has_checked_helper
        and current_finite_branches
        and (
            immediate_finite_evidence
            or len(prior_finite_branches) >= finite_branch_progress.SATURATION_MIN_PRIOR_BRANCHES
        )
        and not materially_broader_coverage
        and not checked_target_replacement
    )
    # Family identity outranks tactic-level fingerprints: after several
    # distinct one-branch helpers, changing the modulus, theorem dependency,
    # or proof syntax cannot reset the research portfolio. Exact checked code
    # remains deliverable and actionable foreground evidence.
    if contained_finite_branch:
        classification = "finite_branch_repeat"
    elif (
        declared_finite_evidence
        and not materially_broader_coverage
        and not checked_target_replacement
        and classification not in {"malformed", "operational_error"}
    ):
        classification = "finite_evidence_only"
    if circular_obligations:
        classification = "circular_helper"

    novel = fingerprints.difference(prior_fingerprints).difference(subsumed)
    progress_anchor_eligible = classification in {"novel", "preserved_evidence"}
    return {
        "version": SEMANTIC_NOVELTY_VERSION,
        "classification": classification,
        "progress_anchor_eligible": progress_anchor_eligible,
        "progress_anchor_reason": {
            "novel": "new_mathematical_semantics",
            "preserved_evidence": "managed_boundary_evidence",
            "duplicate": "duplicate_mathematical_semantics",
            "subsumed": "subsumed_mathematical_semantics",
            "mechanism_repeat": "repeated_mechanism_without_material_coverage",
            "finite_branch_repeat": "saturated_finite_branch_family",
            "finite_evidence_only": "declared_finite_evidence_only",
            "circular_helper": "helper_assumes_unresolved_target",
            "nonclosing": "explicit_nonclosing_result",
            "unclassified": "no_classified_mathematical_semantics",
            "malformed": "malformed_semantic_evidence",
        }.get(classification, operational_error or "ineligible_route_evidence"),
        "has_checked_helper": evidence.has_checked_helper,
        "malformed": evidence.malformed,
        "fingerprints": sorted(fingerprints),
        "novel_fingerprints": sorted(novel),
        "duplicate_fingerprints": sorted(duplicate),
        "subsumed_fingerprints": sorted(subsumed),
        "subsumed_by_job_ids": sorted(subsumed_by),
        "repeated_mechanism_fingerprints": [
            f"mechanism:{mechanism}" for mechanism in sorted(repeated_mechanisms)
        ],
        "repeated_mechanism_job_ids": sorted(repeated_mechanism_job_ids),
        "materially_broader_coverage": materially_broader_coverage,
        "checked_target_replacement": checked_target_replacement,
        "finite_branch_family": (
            finite_branch_progress.FINITE_BRANCH_FAMILY if contained_finite_branch else ""
        ),
        "finite_branch_current_count": len(current_finite_branches),
        "finite_branch_current_fingerprints": sorted(current_finite_branches),
        "finite_branch_prior_count": len(prior_finite_branches),
        "finite_branch_prior_job_ids": sorted(
            {job_id for job_id, _branch in prior_finite_branches.values()}
        ),
        "circular_helper_obligation_hashes": [
            hashlib.sha256(value.encode("utf-8")).hexdigest()[:20] for value in circular_obligations
        ],
    }


def semantic_knowledge(
    entries: Sequence[LedgerEntry],
    *,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any]:
    """Return a bounded full-history semantic index for worker prompts."""
    first_seen: dict[str, str] = {}
    for entry in entries:
        if not entry.is_terminal() or not _entry_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        ):
            continue
        standalone_novelty = classify_semantic_novelty(entry, [entry])
        if standalone_novelty["classification"] in {
            "malformed",
            "operational_error",
            "unclassified",
        }:
            continue
        for fingerprint in semantic_evidence(entry).fingerprints:
            first_seen.setdefault(fingerprint, entry.spec.job_id)
    ordered = sorted(
        first_seen.items(),
        key=lambda item: (
            item[0].startswith("mechanism:"),
            item[0],
        ),
    )
    canonical = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    selected = ordered[:SEMANTIC_KNOWLEDGE_LIMIT]
    return {
        "version": SEMANTIC_NOVELTY_VERSION,
        "items": [
            {
                "fingerprint": fingerprint,
                "first_job_id": job_id,
            }
            for fingerprint, job_id in selected
        ],
        "total": len(ordered),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "truncated": len(selected) < len(ordered),
    }


def consumed_target_fact_knowledge(
    entries: Sequence[LedgerEntry],
    *,
    target_symbol: str,
    active_file: str,
    assignment_revision: str = "",
) -> dict[str, Any]:
    """Return bounded consumed facts without granting progress authority.

    Completion order is authoritative, while a semantic key coalesces repeated
    rediscoveries of the same finite witness. Running and merely completed-but-
    unconsumed jobs remain coordination state and never enter this channel.
    When the caller supplies the current declaration revision, only an exact
    revision match is eligible; missing legacy revisions fail closed.
    """
    current_revision = str(assignment_revision or "").strip()
    completed: list[tuple[str, int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        if (
            entry.state != "done"
            or not entry.consumed
            or not _entry_matches_assignment(
                entry,
                target_symbol=target_symbol,
                active_file=active_file,
            )
        ):
            continue
        entry_revision = str(
            dict(entry.spec.inputs or {}).get(ASSIGNMENT_REVISION_INPUT_KEY, "") or ""
        ).strip()
        if current_revision and entry_revision != current_revision:
            continue
        raw_deliverable = entry.result.get("deliverable")
        deliverable = strip_parent_route_context(
            raw_deliverable if isinstance(raw_deliverable, Mapping) else None
        )
        consumed_at = str(entry.finished_at or entry.created_at or "")
        record = target_handoff.compact_consumed_finding_fact(
            {
                "job_id": entry.spec.job_id,
                "consumed_at": consumed_at,
                "archetype": entry.spec.archetype,
                "deliverable": deliverable,
            }
        )
        if not str(record.get("evidence_excerpt", "") or "").strip():
            continue
        completed.append((consumed_at, index, record))
    completed.sort(key=lambda item: (bool(item[0]), item[0], item[1]))

    distinct: dict[str, dict[str, Any]] = {}
    ordered_keys: list[str] = []
    for _completed_at, _index, raw_record in completed:
        semantic_key = str(raw_record.get("semantic_key", "") or "")
        if not semantic_key:
            continue
        existing = distinct.get(semantic_key)
        if existing is None:
            record = dict(raw_record)
            record["repeat_count"] = 1
            record["latest_job_id"] = str(record.get("job_id", "") or "")
            record["latest_consumed_at"] = str(record.get("consumed_at", "") or "")
            distinct[semantic_key] = record
            ordered_keys.append(semantic_key)
            continue
        existing["repeat_count"] = int(existing.get("repeat_count", 1) or 1) + 1
        existing["latest_job_id"] = str(raw_record.get("job_id", "") or "")
        existing["latest_consumed_at"] = str(raw_record.get("consumed_at", "") or "")
        ordered_keys.remove(semantic_key)
        ordered_keys.append(semantic_key)

    records = [distinct[key] for key in ordered_keys]
    canonical = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    mandatory_keys: set[str] = set()
    for role in (
        "METHOD OBSTRUCTION ONLY",
        "PARENT-RECHECKABLE FINITE INSTANCE WITNESS",
    ):
        for key in reversed(ordered_keys):
            if str(distinct[key].get("role", "") or "") == role:
                mandatory_keys.add(key)
                break
    selected_keys = list(mandatory_keys)
    for generic_only in (False, True):
        for key in reversed(ordered_keys):
            if key in mandatory_keys or key in selected_keys:
                continue
            is_generic = str(distinct[key].get("role", "") or "") == "RESEARCH EVIDENCE"
            if is_generic != generic_only:
                continue
            if len(selected_keys) >= CONSUMED_TARGET_FACT_LIMIT:
                break
            selected_keys.append(key)
        if len(selected_keys) >= CONSUMED_TARGET_FACT_LIMIT:
            break
    selected_set = set(selected_keys)
    selected = [distinct[key] for key in ordered_keys if key in selected_set]
    return {
        "items": selected,
        "total": len(records),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "truncated": len(selected) < len(records),
    }


def verified_mechanism_knowledge(
    entries: Sequence[LedgerEntry],
    *,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any]:
    """Return bounded graph and checked-worker mechanism counts."""
    summary = read_json_file(workflow_state_root() / "summary.json")
    campaign = summary.get("campaign")
    ledger = campaign.get("verified_mechanisms") if isinstance(campaign, Mapping) else None
    raw_entries = ledger.get("entries") if isinstance(ledger, Mapping) else None
    records: list[dict[str, Any]] = []
    if isinstance(raw_entries, Mapping):
        for raw_record in raw_entries.values():
            if not isinstance(raw_record, Mapping):
                continue
            parent_name = str(raw_record.get("parent_name", "") or "")
            parent_file = str(raw_record.get("parent_file", "") or "")
            signature = str(raw_record.get("mechanism_signature", "") or "").strip()
            if (
                not signature
                or not _same_symbol(parent_name, target_symbol)
                or not _same_file(parent_file, active_file)
            ):
                continue
            raw_dependencies = raw_record.get("local_dependencies")
            dependencies = [
                str(value)
                for value in (
                    raw_dependencies
                    if isinstance(raw_dependencies, Sequence)
                    and not isinstance(raw_dependencies, (str, bytes, bytearray))
                    else ()
                )
                if str(value).strip()
            ]
            records.append(
                {
                    "signature": signature,
                    "seen_count": max(0, _safe_int(raw_record.get("seen_count", 0))),
                    "source": "campaign_graph",
                    "first_node_name": str(raw_record.get("first_node_name", "") or ""),
                    "local_dependencies": dependencies,
                    "body_provenance_excerpt": str(
                        raw_record.get("body_provenance_excerpt", "") or ""
                    ),
                }
            )
    checked_research: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not entry.is_terminal() or not _entry_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        ):
            continue
        for signature in semantic_evidence(entry).mechanisms:
            record = checked_research.setdefault(
                signature,
                {
                    "signature": signature,
                    "seen_count": 0,
                    "source": "checked_research",
                    "first_node_name": entry.spec.job_id,
                    "local_dependencies": [],
                    "body_provenance_excerpt": "checked worker mechanism fingerprint",
                },
            )
            record["seen_count"] = int(record["seen_count"]) + 1
    records.extend(checked_research.values())
    records.sort(
        key=lambda record: (
            -int(record["seen_count"]),
            0 if record["source"] == "campaign_graph" else 1,
            str(record["signature"]),
        )
    )
    canonical = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    selected = records[:VERIFIED_MECHANISM_LIMIT]
    return {
        "items": selected,
        "total": len(records),
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "truncated": len(selected) < len(records),
    }


def semantic_worker_objective(objective: str) -> str:
    """Return the route-defining objective without its changing history window."""
    text = str(objective or "")
    marker = f"\n\n{ROUTE_CONTEXT_MARKER}"
    return text.split(marker, 1)[0].rstrip()


def _research_route_record(
    entry: LedgerEntry,
    *,
    semantic_entries: Sequence[LedgerEntry] = (),
) -> dict[str, Any]:
    """Return one prompt-safe prior-worker route and terminal outcome record.

    Active siblings are coordination context only. Their absent or provisional
    result cannot be classified as mathematical evidence before the ledger
    publishes a terminal state.
    """
    inputs = dict(entry.spec.inputs or {})
    objective = semantic_worker_objective(entry.spec.objective)
    if not entry.is_terminal():
        result_excerpt = "Active job; no terminal result or mathematical evidence is available yet."
    else:
        deliverable = strip_parent_route_context(
            entry.result.get("deliverable")
            if isinstance(entry.result.get("deliverable"), Mapping)
            else None
        )
        novelty = classify_semantic_novelty(entry, semantic_entries or (entry,))
        nonclosing = explicitly_nonclosing_result(deliverable)
        progress_eligible = bool(novelty.get("progress_anchor_eligible", False))
        if nonclosing or not progress_eligible:
            # Recent-route context is itself a worker action prompt. Keep the raw
            # deliverable in the ledger/archive, but expose only identity and a
            # digest here so evidence-only helper code or integration prose cannot
            # seed another recursive job.
            deliverable_sha256 = hashlib.sha256(
                _canonical_json(deliverable).encode("utf-8")
            ).hexdigest()
            objective_sha256 = hashlib.sha256(objective.encode("utf-8")).hexdigest()
            objective = (
                "Evidence-only non-closing prior route; original objective "
                f"suppressed (sha256:{objective_sha256[:16]})."
            )
            result_excerpt = _canonical_json(
                {
                    "foreground_use_role": "evidence_only",
                    "foreground_use_reason": (
                        "partial_coverage_without_completion"
                        if nonclosing
                        else str(
                            novelty.get("progress_anchor_reason", "")
                            or "semantic_progress_ineligible"
                        )
                    ),
                    "suppressed_deliverable_sha256": deliverable_sha256,
                }
            )
        elif deliverable:
            result_excerpt = _canonical_json(deliverable)
        else:
            result_excerpt = str(entry.notes or entry.result.get("status", "") or "")
    record = {
        "job_id": _bounded_text(entry.spec.job_id, 180),
        "archetype": _bounded_text(entry.spec.archetype, 60),
        "route_key": _bounded_text(inputs.get("route_key", ""), 180),
        "route_signature": _bounded_text(inputs.get("route_signature", ""), 80),
        "state": _bounded_text(entry.state, 40),
        "objective": _bounded_text(objective, 420),
        "result_excerpt": _bounded_text(result_excerpt, 320),
    }
    delta_signature = _bounded_text(inputs.get("mathematical_delta_signature", ""), 80)
    if delta_signature:
        record["mathematical_delta_signature"] = delta_signature
    return record


def _event_matches_assignment(
    event: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> tuple[bool, str]:
    """Match one journal event, retaining a label for legacy name-only routes."""
    requested_target = str(target_symbol or "")
    raw_name = event.get("name")
    raw_target = event.get("target_symbol")
    identities: list[str] = []
    for raw_identity in (raw_name, raw_target):
        if raw_identity is None or raw_identity == "":
            continue
        if not isinstance(raw_identity, str):
            return False, ""
        identities.append(raw_identity)
    if not identities or any(identity != requested_target for identity in identities):
        return False, ""
    file_identities: list[str] = []
    for key in ("file", "active_file"):
        raw_file = event.get(key)
        if raw_file is None or raw_file == "":
            continue
        if not isinstance(raw_file, str):
            return False, ""
        file_identities.append(raw_file)
    if len(file_identities) > 1 and not all(
        _same_file(candidate, file_identities[0]) for candidate in file_identities[1:]
    ):
        return False, ""
    event_file = file_identities[0] if file_identities else ""
    if event_file:
        return _same_file(event_file, str(active_file or "")), "exact_assignment"
    # Older orchestrator-route events omitted the file. The theorem name is the
    # strongest available scope boundary; expose that downgrade to the worker.
    return True, "legacy_target_symbol_only"


def _journal_context(
    *,
    target_symbol: str,
    active_file: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Read recent target-scoped routes and rejected proof shapes from a bounded tail."""
    path = workflow_state_root() / "journal.jsonl"
    summary = read_json_file(workflow_state_root() / "summary.json")
    campaign = summary.get("campaign")
    reconciliation = (
        campaign.get("epoch_route_replay_reconciliation") if isinstance(campaign, Mapping) else None
    )
    removed_raw = (
        reconciliation.get("removed_decisions") if isinstance(reconciliation, Mapping) else ()
    )
    removed_decisions = {
        str(value or "").strip()
        for value in (
            removed_raw
            if isinstance(removed_raw, Sequence)
            and not isinstance(removed_raw, (str, bytes, bytearray))
            else ()
        )
        if str(value or "").strip()
    }
    try:
        size = path.stat().st_size
        start = max(0, size - JOURNAL_TAIL_MAX_BYTES)
        with path.open("rb") as handle:
            handle.seek(start)
            payload = handle.read(JOURNAL_TAIL_MAX_BYTES)
    except OSError:
        return [], [], False
    if start:
        _partial, separator, payload = payload.partition(b"\n")
        if not separator:
            return [], [], True

    routes: list[dict[str, Any]] = []
    proof_shapes: list[dict[str, Any]] = []
    for raw_line in reversed(payload.splitlines()):
        try:
            event = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, Mapping):
            continue
        matches, assignment_scope = _event_matches_assignment(
            event,
            target_symbol=target_symbol,
            active_file=active_file,
        )
        if not matches:
            continue
        event_kind = str(event.get("event", "") or "")
        # The journal is append-only, so campaign reconciliation tombstones a
        # provider-pause replay in summary state instead of deleting its row.
        # Skip tombstones before filling the bounded window so an older genuine
        # decision is backfilled rather than displaced by an unexecuted route.
        if event_kind == "orchestrator-route" and str(event.get("ts", "") or "") in (
            removed_decisions
        ):
            continue
        if event_kind == "orchestrator-route" and len(routes) < RECENT_ORCHESTRATOR_ROUTE_LIMIT:
            routes.append(
                {
                    "route": _bounded_text(event.get("route", ""), 80),
                    "trigger": _bounded_text(event.get("trigger", ""), 80),
                    "source": _bounded_text(event.get("source", ""), 80),
                    "reason": _bounded_text(event.get("reason", ""), 480),
                    "ts": _bounded_text(event.get("ts", ""), 80),
                    "assignment_scope": assignment_scope,
                }
            )
        elif (
            event_kind == "proof-attempt-rejected"
            and len(proof_shapes) < RECENT_FAILED_PROOF_SHAPE_LIMIT
        ):
            proof_shapes.append(
                {
                    "attempt": _safe_int(event.get("attempt", 0)),
                    "cycle": _safe_int(event.get("cycle", 0)),
                    "proof_shape": _bounded_text(event.get("proof_shape", ""), 520),
                    "reason": _bounded_text(event.get("reason", ""), 240),
                    "ts": _bounded_text(event.get("ts", ""), 80),
                }
            )
        if (
            len(routes) >= RECENT_ORCHESTRATOR_ROUTE_LIMIT
            and len(proof_shapes) >= RECENT_FAILED_PROOF_SHAPE_LIMIT
        ):
            break
    routes.reverse()
    proof_shapes.reverse()
    return routes, proof_shapes, bool(start)


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    """Return mapping members from one untrusted context list."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _normalize_research_route(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one untrusted prior-worker route record."""
    record = {
        "job_id": _bounded_text(raw.get("job_id", ""), 180),
        "archetype": _bounded_text(raw.get("archetype", ""), 60),
        "route_key": _bounded_text(raw.get("route_key", ""), 180),
        "route_signature": _bounded_text(raw.get("route_signature", ""), 80),
        "state": _bounded_text(raw.get("state", ""), 40),
        "objective": _bounded_text(raw.get("objective", ""), 420),
        "result_excerpt": _bounded_text(raw.get("result_excerpt", ""), 320),
    }
    delta_signature = _bounded_text(raw.get("mathematical_delta_signature", ""), 80)
    if delta_signature:
        record["mathematical_delta_signature"] = delta_signature
    return record


def _normalize_orchestrator_route(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one untrusted foreground route record."""
    return {
        "route": _bounded_text(raw.get("route", ""), 80),
        "trigger": _bounded_text(raw.get("trigger", ""), 80),
        "source": _bounded_text(raw.get("source", ""), 80),
        "reason": _bounded_text(raw.get("reason", ""), 480),
        "ts": _bounded_text(raw.get("ts", ""), 80),
        "assignment_scope": _bounded_text(raw.get("assignment_scope", ""), 40),
    }


def _safe_int(value: Any) -> int:
    """Return one context ordinal without allowing malformed input to escape."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_failed_shape(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one untrusted rejected-proof record."""
    return {
        "attempt": _safe_int(raw.get("attempt", 0)),
        "cycle": _safe_int(raw.get("cycle", 0)),
        "proof_shape": _bounded_text(raw.get("proof_shape", ""), 520),
        "reason": _bounded_text(raw.get("reason", ""), 240),
        "ts": _bounded_text(raw.get("ts", ""), 80),
    }


def _normalize_semantic_knowledge_item(raw: Mapping[str, Any]) -> dict[str, str]:
    """Normalize one parent-owned semantic fact for bounded worker context."""
    return {
        "fingerprint": _bounded_text(raw.get("fingerprint", ""), 260),
        "first_job_id": _bounded_text(raw.get("first_job_id", ""), 180),
    }


def _normalize_consumed_target_fact(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one exact-target fact without accepting proof-body fields."""
    raw_instances = raw.get("covered_instances")
    instances = (
        raw_instances
        if isinstance(raw_instances, Sequence)
        and not isinstance(raw_instances, (str, bytes, bytearray))
        else ()
    )
    return {
        "job_id": _bounded_text(raw.get("job_id", ""), 180),
        "latest_job_id": _bounded_text(raw.get("latest_job_id", ""), 180),
        "consumed_at": _bounded_text(raw.get("consumed_at", ""), 80),
        "latest_consumed_at": _bounded_text(raw.get("latest_consumed_at", ""), 80),
        "role": _bounded_text(raw.get("role", ""), 80),
        "scope": _bounded_text(raw.get("scope", ""), 420),
        "covered_instances": [
            _bounded_text(value, 80) for value in instances[:8] if str(value).strip()
        ],
        "finite_witness": _bounded_text(raw.get("finite_witness", ""), 280),
        "evidence_excerpt": _bounded_text(
            raw.get("evidence_excerpt", ""),
            target_handoff.WORKER_FACT_EVIDENCE_CAP,
        ),
        "evidence_sha256": _bounded_text(raw.get("evidence_sha256", ""), 80),
        "semantic_key": _bounded_text(raw.get("semantic_key", ""), 40),
        "repeat_count": max(1, _safe_int(raw.get("repeat_count", 1))),
    }


def _normalize_verified_mechanism(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one authoritative graph-mechanism summary."""
    raw_dependencies = raw.get("local_dependencies")
    dependencies = (
        raw_dependencies
        if isinstance(raw_dependencies, Sequence)
        and not isinstance(raw_dependencies, (str, bytes, bytearray))
        else ()
    )
    return {
        "signature": _bounded_text(raw.get("signature", ""), 260),
        "seen_count": max(0, _safe_int(raw.get("seen_count", 0))),
        "source": _bounded_text(raw.get("source", ""), 40),
        "first_node_name": _bounded_text(raw.get("first_node_name", ""), 240),
        "local_dependencies": [
            _bounded_text(value, 180) for value in dependencies[:6] if str(value).strip()
        ],
        "body_provenance_excerpt": _bounded_text(raw.get("body_provenance_excerpt", ""), 300),
    }


def _consumed_fact_removal_index(facts: Sequence[Mapping[str, Any]]) -> int:
    """Choose the oldest redundant fact while preserving the correction pair."""
    for index, fact in enumerate(facts):
        if str(fact.get("role", "") or "") == "RESEARCH EVIDENCE":
            return index
    role_counts: dict[str, int] = {}
    for fact in facts:
        role = str(fact.get("role", "") or "")
        role_counts[role] = role_counts.get(role, 0) + 1
    for index, fact in enumerate(facts):
        if role_counts.get(str(fact.get("role", "") or ""), 0) > 1:
            return index
    return 0


def normalize_route_context(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and cap a persisted worker context before prompt or result use."""
    source = dict(raw) if isinstance(raw, Mapping) else {}
    assignment_raw = source.get("assignment")
    assignment = dict(assignment_raw) if isinstance(assignment_raw, Mapping) else {}
    research_raw = _mapping_items(source.get("recent_research_routes"))
    orchestrator_raw = _mapping_items(source.get("recent_orchestrator_routes"))
    failed_raw = _mapping_items(source.get("recent_failed_proof_shapes"))
    mechanisms_raw_value = source.get("verified_mechanisms")
    if isinstance(mechanisms_raw_value, Mapping):
        mechanisms_raw = dict(mechanisms_raw_value)
        mechanism_items_raw = _mapping_items(mechanisms_raw.get("items"))
        mechanism_total = _safe_int(mechanisms_raw.get("total", len(mechanism_items_raw)))
        mechanism_sha256 = mechanisms_raw.get("sha256", "")
        mechanism_truncated = bool(mechanisms_raw.get("truncated"))
    else:
        mechanism_items_raw = _mapping_items(mechanisms_raw_value)
        mechanism_total = _safe_int(
            source.get("verified_mechanism_total", len(mechanism_items_raw))
        )
        mechanism_sha256 = source.get("verified_mechanism_sha256", "")
        mechanism_truncated = bool(source.get("verified_mechanism_truncated"))
    semantic_raw_value = source.get("semantic_knowledge")
    if isinstance(semantic_raw_value, Mapping):
        semantic_raw = dict(semantic_raw_value)
        semantic_items_raw = _mapping_items(semantic_raw.get("items"))
        semantic_total = _safe_int(semantic_raw.get("total", len(semantic_items_raw)))
        semantic_sha256 = semantic_raw.get("sha256", "")
        semantic_truncated = bool(semantic_raw.get("truncated"))
    else:
        # Normalized contexts store the bounded items directly and keep their
        # full-set metadata in sibling fields. Accept both shapes so repeated
        # normalization, rendering, and hashing are idempotent.
        semantic_items_raw = _mapping_items(semantic_raw_value)
        semantic_total = _safe_int(source.get("semantic_knowledge_total", len(semantic_items_raw)))
        semantic_sha256 = source.get("semantic_knowledge_sha256", "")
        semantic_truncated = bool(source.get("semantic_knowledge_truncated"))
    facts_raw_value = source.get("consumed_target_facts")
    if isinstance(facts_raw_value, Mapping):
        facts_raw = dict(facts_raw_value)
        fact_items_raw = _mapping_items(facts_raw.get("items"))
        fact_total = _safe_int(facts_raw.get("total", len(fact_items_raw)))
        fact_sha256 = facts_raw.get("sha256", "")
        fact_truncated = bool(facts_raw.get("truncated"))
    else:
        fact_items_raw = _mapping_items(facts_raw_value)
        fact_total = _safe_int(source.get("consumed_target_fact_total", len(fact_items_raw)))
        fact_sha256 = source.get("consumed_target_fact_sha256", "")
        fact_truncated = bool(source.get("consumed_target_fact_truncated"))
    context: dict[str, Any] = {
        "version": CONTEXT_VERSION,
        "assignment": {
            "target_symbol": _bounded_text(assignment.get("target_symbol", ""), 240),
            "active_file": _bounded_text(assignment.get("active_file", ""), 700),
        },
        "recent_research_routes": [
            _normalize_research_route(item) for item in research_raw[-RECENT_RESEARCH_ROUTE_LIMIT:]
        ],
        "recent_orchestrator_routes": [
            _normalize_orchestrator_route(item)
            for item in orchestrator_raw[-RECENT_ORCHESTRATOR_ROUTE_LIMIT:]
        ],
        "recent_failed_proof_shapes": [
            _normalize_failed_shape(item) for item in failed_raw[-RECENT_FAILED_PROOF_SHAPE_LIMIT:]
        ],
        "verified_mechanisms": [
            _normalize_verified_mechanism(item)
            for item in mechanism_items_raw[:VERIFIED_MECHANISM_LIMIT]
            if str(item.get("signature", "") or "").strip()
        ],
        "verified_mechanism_total": mechanism_total,
        "verified_mechanism_sha256": _bounded_text(mechanism_sha256, 80),
        "verified_mechanism_truncated": mechanism_truncated
        or len(mechanism_items_raw) > VERIFIED_MECHANISM_LIMIT,
        "semantic_knowledge": [
            _normalize_semantic_knowledge_item(item)
            for item in semantic_items_raw[:SEMANTIC_KNOWLEDGE_LIMIT]
            if str(item.get("fingerprint", "") or "").strip()
        ],
        "semantic_knowledge_total": semantic_total,
        "semantic_knowledge_sha256": _bounded_text(semantic_sha256, 80),
        "semantic_knowledge_truncated": semantic_truncated
        or len(semantic_items_raw) > SEMANTIC_KNOWLEDGE_LIMIT,
        "consumed_target_facts": [
            _normalize_consumed_target_fact(item)
            for item in fact_items_raw[-CONSUMED_TARGET_FACT_LIMIT:]
            if str(item.get("evidence_excerpt", "") or "").strip()
        ],
        "consumed_target_fact_total": fact_total,
        "consumed_target_fact_sha256": _bounded_text(fact_sha256, 80),
        "consumed_target_fact_truncated": fact_truncated
        or len(fact_items_raw) > CONSUMED_TARGET_FACT_LIMIT,
        "truncated": bool(source.get("truncated"))
        or len(research_raw) > RECENT_RESEARCH_ROUTE_LIMIT
        or len(orchestrator_raw) > RECENT_ORCHESTRATOR_ROUTE_LIMIT
        or len(failed_raw) > RECENT_FAILED_PROOF_SHAPE_LIMIT
        or len(mechanism_items_raw) > VERIFIED_MECHANISM_LIMIT,
    }
    categories = (
        "recent_research_routes",
        "recent_orchestrator_routes",
        "recent_failed_proof_shapes",
        "semantic_knowledge",
        "verified_mechanisms",
        "consumed_target_facts",
    )
    while (
        len(
            json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                default=str,
            ).encode("utf-8")
        )
        > ROUTE_CONTEXT_JSON_MAX_BYTES
    ):
        removable_categories = [
            key
            for key in categories
            if len(context[key])
            > (2 if key == "consumed_target_facts" else 1 if key == "verified_mechanisms" else 0)
        ]
        if not removable_categories:
            break
        removable = max(removable_categories, key=lambda key: len(context[key]))
        if removable == "verified_mechanisms":
            context[removable].pop()
            context["verified_mechanism_truncated"] = True
        elif removable == "consumed_target_facts":
            context[removable].pop(_consumed_fact_removal_index(context[removable]))
        else:
            context[removable].pop(0)
        if removable == "semantic_knowledge":
            context["semantic_knowledge_truncated"] = True
        if removable == "consumed_target_facts":
            context["consumed_target_fact_truncated"] = True
        context["truncated"] = True
    return context


def route_context_for_assignment(
    raw: Any,
    *,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any]:
    """Return context only when its persisted assignment exactly matches the caller.

    A queue transition may race a portfolio heartbeat. Treat a missing,
    malformed, or stale assignment as a fresh window so foreground routes and
    rejected proof shapes never cross theorem boundaries.
    """
    requested_assignment = {
        "target_symbol": str(target_symbol or ""),
        "active_file": str(active_file or ""),
    }
    empty = {"assignment": requested_assignment}
    if not isinstance(raw, Mapping):
        return normalize_route_context(empty)
    raw_assignment = raw.get("assignment")
    if not isinstance(raw_assignment, Mapping):
        return normalize_route_context(empty)
    persisted_target = raw_assignment.get("target_symbol")
    persisted_file = raw_assignment.get("active_file")
    if not isinstance(persisted_target, str) or not isinstance(persisted_file, str):
        return normalize_route_context(empty)
    if persisted_target != requested_assignment["target_symbol"] or not _same_file(
        persisted_file,
        requested_assignment["active_file"],
    ):
        return normalize_route_context(empty)
    return normalize_route_context(raw)


def build_route_context(
    entries: Sequence[LedgerEntry],
    *,
    target_symbol: str,
    active_file: str,
    assignment_revision: str = "",
) -> dict[str, Any]:
    """Build one recent explicit history window for a new research worker."""
    recent_entries: deque[LedgerEntry] = deque(maxlen=RECENT_RESEARCH_ROUTE_LIMIT)
    matching_count = 0
    for entry in entries:
        if not _entry_matches_assignment(
            entry,
            target_symbol=target_symbol,
            active_file=active_file,
        ):
            continue
        matching_count += 1
        recent_entries.append(entry)
    orchestrator_routes, failed_shapes, journal_truncated = _journal_context(
        target_symbol=target_symbol,
        active_file=active_file,
    )
    knowledge = semantic_knowledge(
        entries,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    mechanisms = verified_mechanism_knowledge(
        entries,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    consumed_facts = consumed_target_fact_knowledge(
        entries,
        target_symbol=target_symbol,
        active_file=active_file,
        assignment_revision=assignment_revision,
    )
    return normalize_route_context(
        {
            "assignment": {
                "target_symbol": target_symbol,
                "active_file": active_file,
            },
            "recent_research_routes": [
                _research_route_record(entry, semantic_entries=entries) for entry in recent_entries
            ],
            "recent_orchestrator_routes": orchestrator_routes,
            "recent_failed_proof_shapes": failed_shapes,
            "verified_mechanisms": mechanisms,
            "semantic_knowledge": knowledge,
            "consumed_target_facts": consumed_facts,
            "truncated": journal_truncated or matching_count > RECENT_RESEARCH_ROUTE_LIMIT,
        }
    )


def route_context_sha256(raw: Mapping[str, Any] | None) -> str:
    """Return the stable digest of one normalized recent-history window."""
    normalized = normalize_route_context(raw)
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


def render_route_context(raw: Mapping[str, Any] | None) -> str:
    """Render explicit recent history and a novelty contract for a worker goal."""
    context = normalize_route_context(raw)
    lines = [
        ROUTE_CONTEXT_MARKER,
        (
            "This is the parent campaign's authoritative bounded recent window. The route-set "
            "digest in the route key remains the full dedupe identity; use the explicit records "
            "below for novelty analysis and treat this supplied window as the available history."
        ),
    ]
    research_routes = context["recent_research_routes"]
    orchestrator_routes = context["recent_orchestrator_routes"]
    failed_shapes = context["recent_failed_proof_shapes"]
    verified_mechanisms = context["verified_mechanisms"]
    semantic_items = context["semantic_knowledge"]
    consumed_facts = context["consumed_target_facts"]
    if consumed_facts:
        lines.extend(
            [
                "Consumed exact-target facts for deduplication (oldest to newest):",
                (
                    "Evidence-only means non-progress, not erased content. Use these facts as "
                    "premises, but do not search for, re-derive, or report any listed finite "
                    "witness or method obstruction as a new result."
                ),
            ]
        )
        for item in consumed_facts:
            covered = ", ".join(item["covered_instances"]) or "no finite instance label"
            witness = item["finite_witness"] or "no explicit x/y/z tuple"
            repeated = (
                f"; coalesced {item['repeat_count']} equivalent reports, latest "
                f"{item['latest_job_id']} at {item['latest_consumed_at']}"
                if item["repeat_count"] > 1
                else ""
            )
            lines.extend(
                [
                    f"- {item['job_id']} [{item['role']}; {covered}; {witness}{repeated}]",
                    f"  scope: {item['scope']}",
                    f"  fact: {item['evidence_excerpt']}",
                ]
            )
        if context.get("consumed_target_fact_truncated"):
            lines.append(
                "- exact-fact index truncated; full distinct set identity: "
                f"{context.get('consumed_target_fact_sha256', '')} "
                f"({context.get('consumed_target_fact_total', 0)} facts)"
            )
    if verified_mechanisms:
        lines.append(
            "Parent-verified proof mechanisms for this target (mechanism identity outranks "
            "residue-number novelty):"
        )
        for item in verified_mechanisms:
            dependencies = ", ".join(item["local_dependencies"]) or "direct proof body"
            first_node = item["first_node_name"] or "unknown helper"
            excerpt = item["body_provenance_excerpt"] or "[no provenance excerpt]"
            lines.append(
                f"- {item['signature']} (seen {item['seen_count']}; first helper: "
                f"{first_node}; source: {item['source'] or 'unknown'}; "
                f"dependencies: {dependencies}; shape: {excerpt})"
            )
        if context.get("verified_mechanism_truncated"):
            lines.append(
                "- mechanism index truncated; full set identity: "
                f"{context.get('verified_mechanism_sha256', '')} "
                f"({context.get('verified_mechanism_total', 0)} mechanisms)"
            )
        lines.append(
            "A new modulus or residue using a listed mechanism is additional coverage, not a "
            "fresh proof shape or progress anchor, unless its checked class strictly contains "
            "prior verified coverage or it supplies a checked replacement for the target."
        )
    if semantic_items:
        lines.append(
            "Parent-owned semantic knowledge across all research archetypes "
            "(duplicate or subsumed results are not new progress):"
        )
        for item in semantic_items:
            lines.append(f"- {item['fingerprint']} (first: {item['first_job_id']})")
        if context.get("semantic_knowledge_truncated"):
            lines.append(
                "- semantic index truncated; full set identity: "
                f"{context.get('semantic_knowledge_sha256', '')} "
                f"({context.get('semantic_knowledge_total', 0)} facts)"
            )
    if research_routes:
        lines.append("Recent process-isolated research routes:")
        for item in research_routes:
            lines.append(
                "- "
                f"{item['job_id']} [{item['archetype']}/{item['route_key']}; "
                f"{item['state']}]: {item['objective']} Result: {item['result_excerpt']}"
            )
        active_siblings = [
            item for item in research_routes if item["state"] in {"proposed", "deployed", "running"}
        ]
        if active_siblings:
            lines.append(
                "Concurrent-lane contract: investigate a mathematical delta disjoint from each "
                "active sibling below; its provisional work is coordination only, never evidence."
            )
            for item in active_siblings:
                lines.append(
                    f"- avoid duplicating active {item['job_id']} "
                    f"({item['archetype']}/{item['route_key']}): {item['objective']}"
                )
    if orchestrator_routes:
        lines.append("Recent foreground orchestrator routes:")
        for item in orchestrator_routes:
            lines.append(
                "- "
                f"{item['route']} ({item['trigger']}/{item['source']}; "
                f"{item['assignment_scope']}): {item['reason']}"
            )
    if failed_shapes:
        lines.append("Recent kernel-rejected proof shapes:")
        for item in failed_shapes:
            lines.append(
                f"- attempt {item['attempt']} cycle {item['cycle']}: "
                f"{item['proof_shape']} Rejection: {item['reason']}"
            )
    if (
        not verified_mechanisms
        and not consumed_facts
        and not semantic_items
        and not research_routes
        and not orchestrator_routes
        and not failed_shapes
    ):
        lines.append(
            "No prior route or rejected-proof record exists for this exact assignment; treat it "
            "as a fresh research lane, not as missing history."
        )
    requirement = (
        "Deliverable requirement: name the listed route or proof shape you compared against and "
        "state the concrete new dependency, construction, counterexample evidence, or checked "
        "proof delta. Do not merely restate a digest, repeat a listed attempt, or rediscover a "
        "parent-owned semantic fingerprint."
    )
    requirement_bytes = len(requirement.encode("utf-8")) + 1
    history = _bounded_utf8(
        "\n".join(lines),
        ROUTE_CONTEXT_OBJECTIVE_MAX_BYTES - requirement_bytes,
    )
    return f"{history}\n{requirement}"


def consumed_fact_objective_conflict(
    objective: str,
    raw: Mapping[str, Any] | None,
) -> str:
    """Return the consumed fact that an explicit finite-instance objective repeats."""
    text = str(objective or "")
    if not text.strip():
        return ""
    context = normalize_route_context(raw)
    for item in context["consumed_target_facts"]:
        if item["role"] != "PARENT-RECHECKABLE FINITE INSTANCE WITNESS":
            continue
        for label in item["covered_instances"]:
            raw_variable, separator, raw_value = label.partition("=")
            variable = raw_variable.strip()
            value = raw_value.strip()
            if (
                separator
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_']*", variable)
                and value
                and re.search(
                    rf"\b{re.escape(variable)}\s*=\s*{re.escape(value)}\b",
                    text,
                )
            ):
                return str(item["job_id"] or item["semantic_key"])
    return ""


def objective_with_route_context(
    objective: str,
    raw: Mapping[str, Any] | None,
) -> str:
    """Append changing history after the stable route-defining objective text."""
    base = semantic_worker_objective(objective)
    return f"{base}\n\n{render_route_context(raw)}"


def attach_parent_route_context(
    deliverable: Mapping[str, Any],
    raw: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach authoritative explicit history to a structured worker deliverable."""
    if raw is None:
        return dict(deliverable)
    context = normalize_route_context(raw)
    context["sha256"] = route_context_sha256(context)
    payload = dict(deliverable)
    payload[PARENT_ROUTE_CONTEXT_DELIVERABLE_KEY] = context
    return payload
