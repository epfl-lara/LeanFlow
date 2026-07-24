"""Persist and replay bounded exact-target proof candidates across verifier outages.

Foreground prover tool arguments normally live only in the model conversation.  This
module retains one small, kernel-valid exact replacement per declaration when its
candidate-bound axiom evidence is operationally unavailable, then exposes it only
after the current verifier rechecks that same statement and axiom profile.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.queue_edit_guard import _queue_edit_assigned_statement_signature
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file

SUMMARY_KEY = "verification_candidate_replays"
SCHEMA_VERSION = 1
VERIFIER_CONTRACT_VERSION = "exact-target-inline-axiom-v1"
MAX_CANDIDATE_CHARS = 16_000
GLOBAL_CANDIDATE_CAP = 8

# The native workflow normally supplies a launch token, but direct/internal
# invocations may not.  Keep a process-start nonce so raw PID reuse can never
# suppress a fresh verifier replay in that fallback mode.
_PROCESS_START_NONCE = uuid.uuid4().hex

_REPLAY_STATES = frozenset({"awaiting_axiom_profile", "ready_to_commit"})
_PLACEHOLDER_RE = re.compile(r"\b(?:sorry|admit)\b")


def _now_iso() -> str:
    """Return a compact UTC timestamp for one candidate transition."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical_file(value: str) -> str:
    """Return one stable absolute source identity without requiring existence."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return os.path.realpath(os.path.expanduser(raw))


def _sha256(value: str) -> str:
    """Hash exact UTF-8 text for candidate and statement identities."""
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()


def _process_id(value: object) -> int:
    """Normalize persisted process identity without trusting JSON scalar types."""
    try:
        return max(0, int(str(value or 0).strip()))
    except (TypeError, ValueError):
        return 0


def current_process_fingerprint() -> str:
    """Return a non-secret fingerprint for this exact verifier process launch."""
    launch_token = os.getenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "").strip()
    launch_identity = launch_token or _PROCESS_START_NONCE
    return _sha256(f"{os.getpid()}\0{launch_identity}")


def _process_fingerprint(value: object) -> str:
    """Normalize one persisted launch fingerprint without accepting arbitrary size."""
    normalized = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", normalized):
        return normalized
    return ""


def _records_from_raw(value: object) -> list[dict[str, Any]]:
    """Return mapping records only from the persisted list-shaped schema."""
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value[-GLOBAL_CANDIDATE_CAP:] if isinstance(item, Mapping)]


def declaration_signature_sha256(target_symbol: str, active_file: str) -> str:
    """Hash the current declaration statement while ignoring proof-body changes."""
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    if not target or not active:
        return ""
    try:
        content = Path(active).read_text(encoding="utf-8")
    except OSError:
        return ""
    signature = _queue_edit_assigned_statement_signature(content, target)
    return _sha256(signature) if signature else ""


def _placeholder_free(replacement: str) -> bool:
    """Reject proof placeholders while allowing those words in comments or strings."""
    stripped = _strip_lean_comments_and_strings(str(replacement or ""))
    return _PLACEHOLDER_RE.search(stripped) is None


def _record_matches_assignment(
    record: Mapping[str, Any], *, target_symbol: str, active_file: str
) -> bool:
    """Return whether a record belongs to one canonical declaration assignment."""
    return bool(
        str(record.get("target_symbol", "") or "").strip() == str(target_symbol or "").strip()
        and _canonical_file(str(record.get("active_file", "") or ""))
        == _canonical_file(active_file)
    )


def _valid_record(record: Mapping[str, Any], *, current_signature_sha256: str) -> bool:
    """Validate the bounded record schema and its current statement identity."""
    replacement = record.get("replacement")
    replacement_sha256 = str(record.get("replacement_sha256", "") or "")
    return bool(
        record.get("schema_version") == SCHEMA_VERSION
        and str(record.get("state", "") or "") in _REPLAY_STATES
        and isinstance(replacement, str)
        and 0 < len(replacement) <= MAX_CANDIDATE_CHARS
        and _placeholder_free(replacement)
        and replacement_sha256 == _sha256(replacement)
        and str(record.get("declaration_signature_sha256", "") or "") == current_signature_sha256
        and current_signature_sha256
    )


def capture_operational_candidate(
    *,
    target_symbol: str,
    active_file: str,
    replacement: str,
    campaign_id: str = "",
    process_id: int = 0,
    process_fingerprint: str = "",
    verifier_contract_version: str = VERIFIER_CONTRACT_VERSION,
    backend: str = "",
) -> dict[str, Any] | None:
    """Persist one exact replacement whose axiom-profile gate was unavailable.

    The caller must already have authenticated a successful exact assigned-target
    kernel check.  This layer independently enforces canonical assignment identity,
    a current declaration-signature hash, placeholder freedom, and a hard text cap.
    """
    if not plan_state.plan_state_enabled():
        return None
    if not isinstance(replacement, str):
        return None
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    candidate = replacement
    signature_sha256 = declaration_signature_sha256(target, active)
    if (
        not target
        or not active
        or not candidate.strip()
        or len(candidate) > MAX_CANDIDATE_CHARS
        or not _placeholder_free(candidate)
        or not signature_sha256
    ):
        return None
    candidate_id = (
        "vcr-" + _sha256("\0".join((active, target, signature_sha256, _sha256(candidate))))[:24]
    )
    record = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "campaign_id": str(campaign_id or "").strip(),
        "target_symbol": target,
        "active_file": active,
        "declaration_signature_sha256": signature_sha256,
        "replacement_sha256": _sha256(candidate),
        "replacement": candidate,
        "state": "awaiting_axiom_profile",
        "captured_reason": "exact target kernel check passed; axiom profile unavailable",
        "captured_backend": str(backend or "").strip(),
        "captured_at": _now_iso(),
        "last_replay": {
            "process_id": _process_id(process_id),
            "process_fingerprint": _process_fingerprint(process_fingerprint),
            "verifier_contract_version": str(verifier_contract_version or "").strip(),
            "status": "operationally_unavailable",
            "attempted_at": _now_iso(),
        },
    }
    retained: dict[str, Any] | None = None

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal retained
        raw_records = summary.get(SUMMARY_KEY)
        records = _records_from_raw(raw_records)
        records = [
            item
            for item in records
            if not _record_matches_assignment(
                item,
                target_symbol=target,
                active_file=active,
            )
        ]
        records.append(dict(record))
        summary[SUMMARY_KEY] = records[-GLOBAL_CANDIDATE_CAP:]
        summary["version"] = 1
        summary["updated_at"] = _now_iso()
        retained = dict(record)

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    return retained


def matching_candidate(*, target_symbol: str, active_file: str) -> dict[str, Any] | None:
    """Return the newest schema-valid candidate for the current exact statement.

    Stale, malformed, or superseded records for this assignment are removed while
    records for other declarations remain untouched.
    """
    if not plan_state.plan_state_enabled():
        return None
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    signature_sha256 = declaration_signature_sha256(target, active)
    if not target or not active or not signature_sha256:
        return None
    summary_snapshot = read_json_file(plan_state.plan_state_paths().summary_json)
    raw_snapshot = summary_snapshot.get(SUMMARY_KEY)
    if raw_snapshot is not None and not isinstance(raw_snapshot, list):

        def remove_malformed_store(summary: dict[str, Any]) -> None:
            if not isinstance(summary.get(SUMMARY_KEY), list):
                summary.pop(SUMMARY_KEY, None)

        update_json_file(plan_state.plan_state_paths().summary_json, remove_malformed_store)
        return None
    snapshot_records = _records_from_raw(raw_snapshot)
    if not any(
        _record_matches_assignment(
            record,
            target_symbol=target,
            active_file=active,
        )
        for record in snapshot_records
    ):
        return None
    selected: dict[str, Any] | None = None

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal selected
        raw_records = summary.get(SUMMARY_KEY)
        records = _records_from_raw(raw_records)
        kept: list[dict[str, Any]] = []
        for record in records:
            if not _record_matches_assignment(
                record,
                target_symbol=target,
                active_file=active,
            ):
                kept.append(record)
                continue
            if not _valid_record(record, current_signature_sha256=signature_sha256):
                continue
            selected = dict(record)
        if kept or selected is not None:
            if selected is not None:
                kept.append(dict(selected))
            summary[SUMMARY_KEY] = kept[-GLOBAL_CANDIDATE_CAP:]
        else:
            summary.pop(SUMMARY_KEY, None)

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    return selected


def replay_due(
    record: Mapping[str, Any],
    *,
    process_id: int,
    process_fingerprint: str = "",
    verifier_contract_version: str = VERIFIER_CONTRACT_VERSION,
) -> bool:
    """Return whether a retained candidate needs replay in this verifier process."""
    if str(record.get("state", "") or "") not in _REPLAY_STATES:
        return False
    last = record.get("last_replay")
    replay = dict(last) if isinstance(last, Mapping) else {}
    current_fingerprint = _process_fingerprint(process_fingerprint)
    persisted_fingerprint = _process_fingerprint(replay.get("process_fingerprint"))
    if current_fingerprint:
        same_process = persisted_fingerprint == current_fingerprint
    else:
        # Compatibility fallback for focused callers and legacy records.  The
        # native runner always supplies the launch fingerprint.
        same_process = _process_id(replay.get("process_id")) == _process_id(process_id)
    same_contract = str(replay.get("verifier_contract_version", "") or "") == str(
        verifier_contract_version or ""
    )
    return not (same_process and same_contract)


def mark_replay(
    candidate_id: str,
    *,
    status: str,
    process_id: int,
    process_fingerprint: str = "",
    verifier_contract_version: str = VERIFIER_CONTRACT_VERSION,
    detail: str = "",
) -> dict[str, Any] | None:
    """Persist one replay verdict or retire a mathematically rejected candidate."""
    wanted = str(candidate_id or "").strip()
    normalized_status = str(status or "").strip()
    if not wanted or normalized_status not in {
        "ready_to_commit",
        "operationally_unavailable",
        "mathematically_rejected",
    }:
        return None
    updated: dict[str, Any] | None = None

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal updated
        raw_records = summary.get(SUMMARY_KEY)
        records = _records_from_raw(raw_records)
        kept: list[dict[str, Any]] = []
        for record in records:
            if str(record.get("candidate_id", "") or "") != wanted:
                kept.append(record)
                continue
            if normalized_status == "mathematically_rejected":
                continue
            changed = dict(record)
            changed["state"] = (
                "ready_to_commit"
                if normalized_status == "ready_to_commit"
                else "awaiting_axiom_profile"
            )
            changed["last_replay"] = {
                "process_id": _process_id(process_id),
                "process_fingerprint": _process_fingerprint(process_fingerprint),
                "verifier_contract_version": str(verifier_contract_version or "").strip(),
                "status": normalized_status,
                "detail": str(detail or "").strip()[:600],
                "attempted_at": _now_iso(),
            }
            kept.append(changed)
            updated = dict(changed)
        if kept:
            summary[SUMMARY_KEY] = kept[-GLOBAL_CANDIDATE_CAP:]
        else:
            summary.pop(SUMMARY_KEY, None)
        summary["version"] = 1
        summary["updated_at"] = _now_iso()

    if plan_state.plan_state_enabled():
        update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    return updated


def retire_candidate(*, target_symbol: str, active_file: str) -> bool:
    """Remove every retained candidate for one exact assignment."""
    if not plan_state.plan_state_enabled():
        return False
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    removed = False

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal removed
        raw_records = summary.get(SUMMARY_KEY)
        records = _records_from_raw(raw_records)
        kept = [
            record
            for record in records
            if not _record_matches_assignment(
                record,
                target_symbol=target,
                active_file=active,
            )
        ]
        removed = len(kept) != len(records)
        if kept:
            summary[SUMMARY_KEY] = kept[-GLOBAL_CANDIDATE_CAP:]
        else:
            summary.pop(SUMMARY_KEY, None)
        if removed:
            summary["version"] = 1
            summary["updated_at"] = _now_iso()

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    return removed


def ready_candidate_prompt(record: Mapping[str, Any] | None) -> str:
    """Render one fully revalidated candidate verbatim for deterministic commit."""
    current = dict(record or {})
    replacement = current.get("replacement")
    if (
        current.get("schema_version") != SCHEMA_VERSION
        or current.get("state") != "ready_to_commit"
        or not isinstance(replacement, str)
        or not replacement
        or len(replacement) > MAX_CANDIDATE_CHARS
        or str(current.get("replacement_sha256", "") or "") != _sha256(replacement)
        or not _placeholder_free(replacement)
    ):
        return ""
    return "\n".join(
        [
            "[LEANFLOW REVALIDATED EXACT CANDIDATE]",
            "- authority: this temporary replacement passed the current exact-target kernel check and candidate-bound axiom allowlist",
            "- status: ready to commit, but not yet authoritative on-disk verification",
            f"- candidate sha256: {current.get('replacement_sha256')}",
            "- next action: apply this exact declaration to the assigned source, preserving its statement, then let the parent manager run the on-disk target gate",
            "- do not discard or replace this proof shape before attempting that deterministic commit",
            "",
            "----- BEGIN EXACT LEAN CANDIDATE -----",
            replacement,
            "----- END EXACT LEAN CANDIDATE -----",
        ]
    )


def raw_summary() -> dict[str, Any]:
    """Return the persisted summary for diagnostics and focused tests."""
    if not plan_state.plan_state_enabled():
        return {}
    return read_json_file(plan_state.plan_state_paths().summary_json)
