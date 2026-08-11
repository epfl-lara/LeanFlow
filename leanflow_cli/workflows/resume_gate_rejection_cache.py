"""Persist exact-revision resume-gate axiom-policy rejections.

This cache is negative authority only.  It may suppress a repeated expensive
resume check when the same declaration already completed exact elaboration and
was rejected solely by the configured axiom allowlist.  Cache hits never imply
proof truth and cannot promote a dependency-graph node.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.lean import lean_axiom_batch
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file

SUMMARY_KEY = "resume_gate_axiom_policy_rejections"
SCHEMA_VERSION = 1
VERIFIER_CONTRACT_VERSION = "resume-exact-target-axiom-policy-v1"
PER_TARGET_RECORD_CAP = 4
GLOBAL_RECORD_CAP = 32

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_UNAVAILABLE_BLOCKERS = frozenset(
    {
        "axiom-profile-unavailable",
        "backend-unavailable",
        "cancelled",
        "infrastructure-pause",
        "process-error",
        "resource-admission",
        "timeout",
    }
)
_OPERATIONAL_BLOCKER_MARKERS = (
    "backend",
    "cancel",
    "exception",
    "infrastructure",
    "interrupt",
    "process-error",
    "resource-admission",
    "timeout",
    "unavailable",
)


def _now_iso() -> str:
    """Return a compact UTC timestamp for one durable rejection."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical_root(value: str | Path) -> str:
    """Return one absolute project-root identity without requiring existence."""
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return ""


def _canonical_file(value: str | Path, project_root: str | Path) -> str:
    """Resolve one source path relative to its explicit project root."""
    raw = str(value or "").strip()
    root = _canonical_root(project_root)
    if not raw or not root:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(root) / path
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return os.path.realpath(str(path))


def _source_sha256(active_file: str) -> str:
    """Return the full raw-source digest, or empty when the file cannot be read."""
    try:
        return hashlib.sha256(Path(active_file).read_bytes()).hexdigest()
    except (OSError, RuntimeError, ValueError):
        return ""


def _normalized_axioms(values: Sequence[str]) -> tuple[str, ...]:
    """Return the deterministic set representation used by axiom policy."""
    return tuple(sorted({str(value or "").strip() for value in values if str(value or "").strip()}))


def axiom_policy_fingerprint(
    *,
    profile_enabled: bool,
    allowed_axioms: Sequence[str],
) -> str:
    """Hash profile enablement and the sorted semantic axiom allowlist."""
    payload = json.dumps(
        {
            "profile_enabled": bool(profile_enabled),
            "allowed_axioms": list(_normalized_axioms(allowed_axioms)),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResumeGateRejectionIdentity:
    """Bind negative gate evidence to the complete verifier environment."""

    project_root: str
    canonical_active_file: str
    target_symbol: str
    source_sha256: str
    import_environment_sha256: str
    verifier_contract_version: str
    axiom_profile_enabled: bool
    allowed_axioms: tuple[str, ...]
    axiom_policy_sha256: str

    @property
    def valid(self) -> bool:
        """Return whether every fail-closed identity component is authentic."""
        expected_policy = axiom_policy_fingerprint(
            profile_enabled=self.axiom_profile_enabled,
            allowed_axioms=self.allowed_axioms,
        )
        return bool(
            self.project_root
            and Path(self.project_root).is_absolute()
            and self.canonical_active_file
            and Path(self.canonical_active_file).is_absolute()
            and self.target_symbol
            and _SHA256_RE.fullmatch(self.source_sha256)
            and _SHA256_RE.fullmatch(self.import_environment_sha256)
            and self.verifier_contract_version
            and len(self.verifier_contract_version) <= 200
            and self.allowed_axioms == _normalized_axioms(self.allowed_axioms)
            and _SHA256_RE.fullmatch(self.axiom_policy_sha256)
            and self.axiom_policy_sha256 == expected_policy
        )

    def to_mapping(self) -> dict[str, Any]:
        """Serialize the complete cache identity without truncating digests."""
        return {
            "project_root": self.project_root,
            "canonical_active_file": self.canonical_active_file,
            "target_symbol": self.target_symbol,
            "source_sha256": self.source_sha256,
            "import_environment_sha256": self.import_environment_sha256,
            "verifier_contract_version": self.verifier_contract_version,
            "axiom_profile_enabled": self.axiom_profile_enabled,
            "allowed_axioms": list(self.allowed_axioms),
            "axiom_policy_sha256": self.axiom_policy_sha256,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
    ) -> ResumeGateRejectionIdentity | None:
        """Parse one persisted identity and reject malformed legacy state."""
        value = dict(raw or {})
        raw_axioms = value.get("allowed_axioms")
        if not isinstance(raw_axioms, list) or any(
            not isinstance(item, str) for item in raw_axioms
        ):
            return None
        identity = cls(
            project_root=str(value.get("project_root", "") or ""),
            canonical_active_file=str(value.get("canonical_active_file", "") or ""),
            target_symbol=str(value.get("target_symbol", "") or ""),
            source_sha256=str(value.get("source_sha256", "") or ""),
            import_environment_sha256=str(value.get("import_environment_sha256", "") or ""),
            verifier_contract_version=str(value.get("verifier_contract_version", "") or ""),
            axiom_profile_enabled=value.get("axiom_profile_enabled") is True,
            allowed_axioms=tuple(raw_axioms),
            axiom_policy_sha256=str(value.get("axiom_policy_sha256", "") or ""),
        )
        return identity if identity.valid else None


@dataclass(frozen=True)
class CachedResumeGateRejection:
    """Describe one reusable negative axiom-policy verdict."""

    rejection_id: str
    identity: ResumeGateRejectionIdentity
    blocker_axioms: tuple[str, ...]
    recorded_at: str

    def to_mapping(self) -> dict[str, Any]:
        """Serialize one bounded negative record for ``summary.json``."""
        return {
            "schema_version": SCHEMA_VERSION,
            "rejection_id": self.rejection_id,
            "identity": self.identity.to_mapping(),
            "blocker_axioms": list(self.blocker_axioms),
            "rejection_kind": "disallowed_axioms",
            "negative_authority_only": True,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
    ) -> CachedResumeGateRejection | None:
        """Parse a schema-valid negative record without inferring acceptance."""
        value = dict(raw or {})
        identity_raw = value.get("identity")
        identity = ResumeGateRejectionIdentity.from_mapping(
            identity_raw if isinstance(identity_raw, Mapping) else None
        )
        raw_blockers = value.get("blocker_axioms")
        if (
            value.get("schema_version") != SCHEMA_VERSION
            or value.get("rejection_kind") != "disallowed_axioms"
            or value.get("negative_authority_only") is not True
            or identity is None
            or not isinstance(raw_blockers, list)
            or not raw_blockers
            or any(not isinstance(item, str) or not item.strip() for item in raw_blockers)
        ):
            return None
        blockers = _normalized_axioms(raw_blockers)
        if len(blockers) != len(raw_blockers) or not _mathematical_blockers(blockers, identity):
            return None
        recorded_at = str(value.get("recorded_at", "") or "").strip()
        rejection_id = str(value.get("rejection_id", "") or "").strip()
        expected_id = _rejection_id(identity, blockers)
        if not recorded_at or rejection_id != expected_id:
            return None
        return cls(
            rejection_id=rejection_id,
            identity=identity,
            blocker_axioms=blockers,
            recorded_at=recorded_at,
        )


def capture_identity(
    *,
    active_file: str | Path,
    target_symbol: str,
    project_root: str | Path,
    profile_enabled: bool,
    allowed_axioms: Sequence[str],
    verifier_contract_version: str = VERIFIER_CONTRACT_VERSION,
) -> ResumeGateRejectionIdentity | None:
    """Capture the current source, import environment, contract, and policy."""
    root = _canonical_root(project_root)
    active = _canonical_file(active_file, root)
    target = str(target_symbol or "").strip()
    source_sha256 = _source_sha256(active)
    contract = str(verifier_contract_version or "").strip()
    axioms = _normalized_axioms(allowed_axioms)
    if not root or not active or not target or not source_sha256 or not contract:
        return None
    try:
        environment = lean_axiom_batch.import_environment_fingerprint(Path(root))
    except Exception:
        # Cache availability is optional; verifier/environment failures must
        # fall through to the ordinary authoritative resume check.
        return None
    identity = ResumeGateRejectionIdentity(
        project_root=root,
        canonical_active_file=active,
        target_symbol=target,
        source_sha256=source_sha256,
        import_environment_sha256=str(environment or "").strip(),
        verifier_contract_version=contract,
        axiom_profile_enabled=bool(profile_enabled),
        allowed_axioms=axioms,
        axiom_policy_sha256=axiom_policy_fingerprint(
            profile_enabled=profile_enabled,
            allowed_axioms=axioms,
        ),
    )
    return identity if identity.valid else None


def _same_target(left: str, right: str) -> bool:
    """Return whether exact or qualified Lean names denote one target."""
    normalized_left = str(left or "").strip().removeprefix("_root_.")
    normalized_right = str(right or "").strip().removeprefix("_root_.")
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True
    if "." in normalized_left and "." in normalized_right:
        return False
    return normalized_left.endswith(f".{normalized_right}") or normalized_right.endswith(
        f".{normalized_left}"
    )


def _sequence_values(value: object) -> tuple[str, ...] | None:
    """Return one nonempty unique string sequence, or ``None`` when malformed."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    raw = list(value)
    if not raw or any(not isinstance(item, str) or not item.strip() for item in raw):
        return None
    normalized = _normalized_axioms(raw)
    return normalized if len(normalized) == len(raw) else None


def _mathematical_blockers(
    blockers: Sequence[str],
    identity: ResumeGateRejectionIdentity,
) -> bool:
    """Return whether blockers are actual disallowed axioms, not outages."""
    allowed = set(identity.allowed_axioms)
    normalized = {str(value or "").strip().lower() for value in blockers}
    return bool(
        identity.axiom_profile_enabled
        and blockers
        and not (set(blockers) & allowed)
        and not (normalized & _UNAVAILABLE_BLOCKERS)
        and not any(
            marker in value for value in normalized for marker in _OPERATIONAL_BLOCKER_MARKERS
        )
    )


def _operationally_tainted(payload: Mapping[str, Any]) -> bool:
    """Return whether a verifier payload reports interruption or unavailable work."""
    return bool(
        payload.get("timed_out") is True
        or payload.get("cancelled") is True
        or payload.get("retryable") is True
        or str(payload.get("error", "") or "").strip()
        or str(payload.get("error_code", "") or "").strip()
        or str(payload.get("failure_kind", "") or "").strip()
    )


def completed_axiom_policy_blockers(
    manager_check: Mapping[str, Any] | None,
    verification: Mapping[str, Any] | None,
    identity: ResumeGateRejectionIdentity,
) -> tuple[str, ...]:
    """Return completed disallowed axioms eligible for negative persistence.

    The exact target must have elaborated cleanly before the manager changed the
    overall verdict to false solely because of a complete axiom allowlist result.
    Any target/file mismatch or operational uncertainty returns an empty tuple.
    """
    if not identity.valid or not identity.axiom_profile_enabled:
        return ()
    checked = dict(manager_check or {})
    nested_raw = checked.get("incremental")
    if not isinstance(nested_raw, Mapping):
        return ()
    incremental = dict(nested_raw)
    record = dict(verification or {})
    if _operationally_tainted(checked) or _operationally_tainted(incremental):
        return ()
    action = str(incremental.get("action", "") or "").strip().lower().replace("-", "_")
    checked_target = str(checked.get("target", "") or "").strip()
    incremental_target = str(incremental.get("target", "") or "").strip()
    checked_file = str(incremental.get("file", "") or "").strip()
    if (
        checked.get("ok") is not False
        or str(checked.get("mode", "") or "") != "incremental_target"
        or checked.get("has_errors") is not True
        or incremental.get("success") is not True
        or incremental.get("ok") is not True
        or incremental.get("has_errors") is not False
        or incremental.get("has_sorry") is not False
        or action != "check_target"
        or not _same_target(checked_target, identity.target_symbol)
        or not _same_target(incremental_target, identity.target_symbol)
        or _canonical_file(checked_file, identity.project_root) != identity.canonical_active_file
        or checked.get("axiom_profile_checked") is not True
    ):
        return ()
    blockers = _sequence_values(checked.get("axiom_profile_blockers"))
    violations = _sequence_values(checked.get("axiom_violation"))
    if blockers is None or violations != blockers or not _mathematical_blockers(blockers, identity):
        return ()
    record_blockers = _sequence_values(record.get("axiom_profile_blockers"))
    scope = str(record.get("scope", "") or "")
    scope_target = scope.removeprefix("target:") if scope.startswith("target:") else ""
    if (
        record.get("ok") is not False
        or str(record.get("tool", "") or "") != "lean_incremental_check"
        or not _same_target(scope_target, identity.target_symbol)
        or not _same_target(str(record.get("target", "") or ""), identity.target_symbol)
        or _canonical_file(
            str(record.get("active_file", "") or ""),
            identity.project_root,
        )
        != identity.canonical_active_file
        or record.get("axiom_profile_checked") is not True
        or record_blockers != blockers
    ):
        return ()
    try:
        error_count = int(record.get("errors", 0) or 0)
        sorry_count = int(record.get("sorry", record.get("sorry_count", 0)) or 0)
    except (TypeError, ValueError):
        return ()
    return blockers if error_count > 0 and sorry_count == 0 else ()


def _rejection_id(
    identity: ResumeGateRejectionIdentity,
    blockers: Sequence[str],
) -> str:
    """Return a stable full-identity id for one negative verdict."""
    payload = json.dumps(
        [identity.to_mapping(), list(blockers)],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "rgr-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _records_from_raw(value: object) -> list[CachedResumeGateRejection]:
    """Parse only the bounded tail of schema-valid negative records."""
    if not isinstance(value, list):
        return []
    parsed: list[CachedResumeGateRejection] = []
    for raw in value[-GLOBAL_RECORD_CAP:]:
        if not isinstance(raw, Mapping):
            continue
        record = CachedResumeGateRejection.from_mapping(raw)
        if record is not None:
            parsed.append(record)
    return parsed


def matching_rejection(
    identity: ResumeGateRejectionIdentity,
    summary: Mapping[str, Any] | None = None,
) -> CachedResumeGateRejection | None:
    """Return an exact negative cache hit, never an acceptance verdict."""
    if not identity.valid:
        return None
    if summary is None:
        if not plan_state.plan_state_enabled():
            return None
        state = read_json_file(plan_state.plan_state_paths().summary_json)
    else:
        state = dict(summary)
    selected: CachedResumeGateRejection | None = None
    for record in _records_from_raw(state.get(SUMMARY_KEY)):
        if record.identity == identity:
            selected = record
    return selected


def remember_completed_rejection(
    precheck_identity: ResumeGateRejectionIdentity,
    *,
    manager_check: Mapping[str, Any] | None,
    verification: Mapping[str, Any] | None,
) -> CachedResumeGateRejection | None:
    """Persist one completed policy rejection after rechecking all identity fields.

    Recapturing the source and import environment after the verifier returns
    rejects source races and build-environment changes.  Operational failures,
    incomplete profiles, and target mismatches never reach ``summary.json``.
    """
    if not plan_state.plan_state_enabled() or not precheck_identity.valid:
        return None
    blockers = completed_axiom_policy_blockers(
        manager_check,
        verification,
        precheck_identity,
    )
    if not blockers:
        return None
    postcheck_identity = capture_identity(
        active_file=precheck_identity.canonical_active_file,
        target_symbol=precheck_identity.target_symbol,
        project_root=precheck_identity.project_root,
        profile_enabled=precheck_identity.axiom_profile_enabled,
        allowed_axioms=precheck_identity.allowed_axioms,
        verifier_contract_version=precheck_identity.verifier_contract_version,
    )
    if postcheck_identity != precheck_identity:
        return None
    record = CachedResumeGateRejection(
        rejection_id=_rejection_id(precheck_identity, blockers),
        identity=precheck_identity,
        blocker_axioms=blockers,
        recorded_at=_now_iso(),
    )
    retained: CachedResumeGateRejection | None = None

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal retained
        records = [
            item
            for item in _records_from_raw(summary.get(SUMMARY_KEY))
            if item.identity != precheck_identity
        ]
        records.append(record)
        matching_indexes = [
            index
            for index, item in enumerate(records)
            if item.identity.canonical_active_file == precheck_identity.canonical_active_file
            and item.identity.target_symbol == precheck_identity.target_symbol
        ]
        excess = max(0, len(matching_indexes) - PER_TARGET_RECORD_CAP)
        drop_indexes = set(matching_indexes[:excess])
        records = [item for index, item in enumerate(records) if index not in drop_indexes]
        records = records[-GLOBAL_RECORD_CAP:]
        summary[SUMMARY_KEY] = [item.to_mapping() for item in records]
        summary["version"] = 1
        summary["updated_at"] = _now_iso()
        retained = record

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    return retained
