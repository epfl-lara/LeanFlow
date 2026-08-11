"""Persist the best checked-but-incomplete exact-target candidate.

LeanProbe replacement checks do not mutate source, so a strong partial proof can
otherwise disappear when a provider turn ends, context is compressed, or the
native process restarts.  This module retains one bounded, statement-bound
candidate per assignment and presents it only as editable proof state, never as
verification evidence.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _declaration_line_index_from_text,
    _strip_lean_comments_and_strings,
)
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.queue_edit_guard import (
    _queue_edit_assigned_statement_signature,
)
from leanflow_cli.workflows.workflow_json_io import read_json_file, update_json_file

SUMMARY_KEY = "target_candidate_checkpoints"
SCHEMA_VERSION = 1
MAX_CANDIDATE_CHARS = 32_000
GLOBAL_CANDIDATE_CAP = 8
MAX_DIAGNOSTIC_CHARS = 1_200

_PLACEHOLDER_RE = re.compile(r"\b(?:sorry|admit)\b")
_STATE = "checked_with_errors"


def _now_iso() -> str:
    """Return a compact UTC timestamp for one checkpoint transition."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical_file(value: object) -> str:
    """Return a stable absolute path without requiring the file to exist."""
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


def _placeholder_free(replacement: str) -> bool:
    """Reject proof placeholders while allowing those words in comments or strings."""
    stripped = _strip_lean_comments_and_strings(str(replacement or ""))
    return _PLACEHOLDER_RE.search(stripped) is None


def declaration_signature_sha256(target_symbol: str, active_file: str) -> str:
    """Hash the current assigned statement while ignoring its proof body."""
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    if not target or not active:
        return ""
    try:
        source = Path(active).read_text(encoding="utf-8")
    except OSError:
        return ""
    signature = _queue_edit_assigned_statement_signature(source, target)
    return _sha256(signature) if signature else ""


def _error_messages(check: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return structured Lean errors from one completed replacement check."""
    raw = check.get("messages")
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        return []
    return [
        dict(message)
        for message in raw
        if isinstance(message, Mapping)
        and str(message.get("severity", "") or "").strip().lower() == "error"
    ]


def _message_line(message: Mapping[str, Any]) -> int:
    """Return a positive candidate-relative diagnostic line when available."""
    start = message.get("start")
    if not isinstance(start, Mapping):
        return 0
    try:
        return max(0, int(start.get("line", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _diagnostic_excerpt(check: Mapping[str, Any], errors: Sequence[Mapping[str, Any]]) -> str:
    """Render a bounded exact diagnostic summary for the next provider turn."""
    messages = [
        str(message.get("message", "") or "").strip()
        for message in errors[:3]
        if str(message.get("message", "") or "").strip()
    ]
    fallback = str(check.get("output", "") or check.get("error", "") or "").strip()
    rendered = "\n".join(messages) or fallback or "LeanProbe reported an unresolved error."
    return rendered[:MAX_DIAGNOSTIC_CHARS]


def _quality(record: Mapping[str, Any]) -> tuple[int, int, int]:
    """Rank fewer errors, then a longer verified prefix, then more retained context."""
    try:
        error_count = max(1, int(record.get("error_count", 1) or 1))
        first_error_line = max(0, int(record.get("first_error_line", 0) or 0))
        candidate_lines = max(0, int(record.get("candidate_lines", 0) or 0))
    except (TypeError, ValueError):
        return (-10_000, 0, 0)
    return (-error_count, first_error_line, candidate_lines)


def _record_matches_assignment(
    record: Mapping[str, Any], *, target_symbol: str, active_file: str
) -> bool:
    """Return whether a record belongs to one canonical declaration assignment."""
    return bool(
        str(record.get("target_symbol", "") or "").strip() == str(target_symbol or "").strip()
        and _canonical_file(record.get("active_file")) == _canonical_file(active_file)
    )


def _records_from_raw(value: object) -> list[dict[str, Any]]:
    """Return only mapping records from the bounded persisted schema."""
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value[-GLOBAL_CANDIDATE_CAP:] if isinstance(item, Mapping)]


def _valid_record(record: Mapping[str, Any], *, current_signature_sha256: str) -> bool:
    """Validate one persisted candidate against its current target statement."""
    replacement = record.get("replacement")
    return bool(
        record.get("schema_version") == SCHEMA_VERSION
        and record.get("state") == _STATE
        and isinstance(replacement, str)
        and 0 < len(replacement) <= MAX_CANDIDATE_CHARS
        and _placeholder_free(replacement)
        and str(record.get("replacement_sha256", "") or "") == _sha256(replacement)
        and str(record.get("declaration_signature_sha256", "") or "") == current_signature_sha256
        and current_signature_sha256
        and _quality(record)[0] > -10_000
    )


def capture_checked_candidate(
    *,
    target_symbol: str,
    active_file: str,
    replacement: str,
    check: Mapping[str, Any],
    campaign_id: str = "",
) -> dict[str, Any] | None:
    """Persist a better exact-target replacement with localized Lean errors.

    The candidate must be placeholder-free, preserve exactly the assigned
    declaration, and come from a completed LeanProbe target-candidate check.
    Successful candidates belong to the authoritative commit path and retire
    any older partial checkpoint instead.
    """
    if not plan_state.plan_state_enabled() or not isinstance(replacement, str):
        return None
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    checked = dict(check or {})
    checked_target = str(checked.get("target", "") or "").strip()
    checked_file = _canonical_file(checked.get("file"))
    exact_names = tuple(
        str(entry.get("name", "") or "").strip()
        for entry in _declaration_line_index_from_text(replacement)
        if str(entry.get("name", "") or "").strip()
    )
    exact_candidate = bool(
        target
        and active
        and checked_target == target
        and checked_file == active
        and checked.get("replacement_matches_target") is True
        and str(checked.get("verification_scope", "") or "") == "target_candidate"
        and exact_names == (target,)
    )
    if exact_candidate and checked.get("has_errors") is False and checked.get("has_sorry") is False:
        retire_candidate(target_symbol=target, active_file=active)
        return None
    errors = _error_messages(checked)
    signature_sha256 = declaration_signature_sha256(target, active)
    if (
        not exact_candidate
        or checked.get("success") is not True
        or checked.get("has_errors") is not True
        or checked.get("has_sorry") is not False
        or bool(checked.get("timed_out"))
        or not replacement.strip()
        or len(replacement) > MAX_CANDIDATE_CHARS
        or not _placeholder_free(replacement)
        or not signature_sha256
    ):
        return None
    error_count = len(errors) or 1
    positive_lines = [line for line in map(_message_line, errors) if line > 0]
    first_error_line = min(positive_lines) if positive_lines else 0
    candidate_lines = replacement.count("\n") + 1
    replacement_sha256 = _sha256(replacement)
    record = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": "tcc-"
        + _sha256("\0".join((active, target, signature_sha256, replacement_sha256)))[:24],
        "campaign_id": str(campaign_id or "").strip(),
        "target_symbol": target,
        "active_file": active,
        "declaration_signature_sha256": signature_sha256,
        "replacement_sha256": replacement_sha256,
        "replacement": replacement,
        "state": _STATE,
        "error_count": error_count,
        "first_error_line": first_error_line,
        "candidate_lines": candidate_lines,
        "diagnostic_excerpt": _diagnostic_excerpt(checked, errors),
        "checked_backend": str(checked.get("backend", "") or "").strip(),
        "captured_at": _now_iso(),
    }
    retained: dict[str, Any] | None = None

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal retained
        records = _records_from_raw(summary.get(SUMMARY_KEY))
        kept: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for item in records:
            if _record_matches_assignment(item, target_symbol=target, active_file=active):
                if _valid_record(item, current_signature_sha256=signature_sha256):
                    previous = dict(item)
                continue
            kept.append(item)
        selected = record
        if previous is not None and _quality(previous) > _quality(record):
            selected = previous
        kept.append(dict(selected))
        summary[SUMMARY_KEY] = kept[-GLOBAL_CANDIDATE_CAP:]
        summary["version"] = 1
        summary["updated_at"] = _now_iso()
        if selected.get("candidate_id") == record["candidate_id"]:
            retained = dict(record)

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    return retained


def matching_candidate(*, target_symbol: str, active_file: str) -> dict[str, Any] | None:
    """Return the best valid checkpoint and remove stale assignment records."""
    if not plan_state.plan_state_enabled():
        return None
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    signature_sha256 = declaration_signature_sha256(target, active)
    if not target or not active or not signature_sha256:
        return None
    snapshot = read_json_file(plan_state.plan_state_paths().summary_json)
    if snapshot.get(SUMMARY_KEY) is not None and not isinstance(snapshot.get(SUMMARY_KEY), list):
        update_json_file(
            plan_state.plan_state_paths().summary_json,
            lambda summary: summary.pop(SUMMARY_KEY, None),
        )
        return None
    selected: dict[str, Any] | None = None

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal selected
        kept: list[dict[str, Any]] = []
        for record in _records_from_raw(summary.get(SUMMARY_KEY)):
            if not _record_matches_assignment(record, target_symbol=target, active_file=active):
                kept.append(record)
                continue
            if not _valid_record(record, current_signature_sha256=signature_sha256):
                continue
            selected = dict(record)
        if selected is not None:
            kept.append(dict(selected))
        if kept:
            summary[SUMMARY_KEY] = kept[-GLOBAL_CANDIDATE_CAP:]
        else:
            summary.pop(SUMMARY_KEY, None)

    update_json_file(plan_state.plan_state_paths().summary_json, mutate)
    return selected


def retire_candidate(*, target_symbol: str, active_file: str) -> bool:
    """Remove every partial candidate for one exact assignment."""
    if not plan_state.plan_state_enabled():
        return False
    target = str(target_symbol or "").strip()
    active = _canonical_file(active_file)
    removed = False

    def mutate(summary: dict[str, Any]) -> None:
        nonlocal removed
        records = _records_from_raw(summary.get(SUMMARY_KEY))
        kept = [
            record
            for record in records
            if not _record_matches_assignment(record, target_symbol=target, active_file=active)
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


def candidate_prompt(record: Mapping[str, Any] | None) -> str:
    """Render a checked partial candidate verbatim without implying validity."""
    current = dict(record or {})
    replacement = current.get("replacement")
    if (
        current.get("schema_version") != SCHEMA_VERSION
        or current.get("state") != _STATE
        or not isinstance(replacement, str)
        or not replacement
        or len(replacement) > MAX_CANDIDATE_CHARS
        or str(current.get("replacement_sha256", "") or "") != _sha256(replacement)
        or not _placeholder_free(replacement)
    ):
        return ""
    diagnostic = str(current.get("diagnostic_excerpt", "") or "").strip()
    return "\n".join(
        [
            "[LEANFLOW CHECKED PARTIAL TARGET CANDIDATE]",
            "- authority: this is resumable working state, not a verified proof and not an on-disk edit",
            f"- remaining Lean errors: {current.get('error_count', 1)}",
            f"- first reported candidate line: {current.get('first_error_line', 0)}",
            "- next action: continue from this exact declaration, repair its remaining diagnostics, and run `lean_incremental_check(check_target)` again",
            "- do not restart from the source `sorry` body merely because a turn ended or context was compressed",
            "",
            "Remaining diagnostic:",
            diagnostic or "LeanProbe reported an unresolved error.",
            "",
            "----- BEGIN CHECKED PARTIAL LEAN CANDIDATE -----",
            replacement,
            "----- END CHECKED PARTIAL LEAN CANDIDATE -----",
        ]
    )


def raw_summary() -> dict[str, Any]:
    """Return the persisted summary for diagnostics and focused tests."""
    if not plan_state.plan_state_enabled():
        return {}
    return read_json_file(plan_state.plan_state_paths().summary_json)
