"""Classify and bound retries for sorry-free helper verification gates.

The declaration queue is intentionally derived from Lean ``sorry`` and
diagnostic evidence.  A prover-created helper can therefore disappear from
that queue after elaboration while its transitive axiom-profile check remains
temporarily unavailable.  This module keeps the retry policy deterministic:
the durable theorem outcome identifies pending helpers, while attempt
reservations are process-local and source-revision scoped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AXIOM_PROFILE_UNAVAILABLE = "axiom-profile-unavailable"
ATTEMPTS_KEY = "helper_gate_retry_attempts"
TEMPORARY_NOTE_MARKER = "helper gate temporarily unavailable"
_MAX_PROCESS_ATTEMPTS = 128


@dataclass(frozen=True)
class PendingHelperGate:
    """Identify one prover-created helper awaiting an infrastructure gate."""

    target_symbol: str
    active_file: str


def _string_values(raw: Any) -> tuple[str, ...]:
    """Normalize one legacy scalar-or-list field into strings."""
    if raw in (None, ""):
        return ()
    if isinstance(raw, (str, bytes)):
        return (str(raw),)
    try:
        return tuple(str(value) for value in raw)
    except TypeError:
        return (str(raw),)


def _normalized_file(path: str) -> str:
    """Return a comparison-safe file identity without requiring it to exist."""
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return str(path or "").strip()


def pending_helpers(
    outcomes: Mapping[str, Any] | None,
    *,
    active_file: str,
) -> tuple[PendingHelperGate, ...]:
    """Return unverified prover helpers whose last gate was unavailable.

    The helper-origin note is required so an unrelated theorem outcome cannot
    be pulled into this narrow retry path.  The explicit axiom blocker accepts
    legacy outcomes emitted before the temporary-note marker was introduced.
    """
    wanted_file = _normalized_file(active_file)
    candidates: list[PendingHelperGate] = []
    seen: set[tuple[str, str]] = set()
    for storage_key, raw_outcome in dict(outcomes or {}).items():
        if not isinstance(raw_outcome, Mapping):
            continue
        outcome = dict(raw_outcome)
        if str(outcome.get("status", "") or "").strip().lower() != "unverified":
            continue
        note = str(outcome.get("note", "") or "").strip().lower()
        helper_origin = note.startswith("helper edit for ") or TEMPORARY_NOTE_MARKER in note
        if not helper_origin:
            continue
        verification = dict(outcome.get("last_verification") or {})
        blockers = {
            str(value or "").strip().lower()
            for value in _string_values(verification.get("axiom_profile_blockers"))
        }
        retryable = AXIOM_PROFILE_UNAVAILABLE in blockers or TEMPORARY_NOTE_MARKER in note
        if not retryable:
            continue
        target = str(outcome.get("target_symbol", "") or "").strip()
        file = str(outcome.get("active_file", "") or "").strip()
        if not target or not file:
            file_part, _separator, target_part = str(storage_key).rpartition("::")
            file = file or file_part
            target = target or target_part
        if not target or not file or _normalized_file(file) != wanted_file:
            continue
        key = (_normalized_file(file), target)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(PendingHelperGate(target_symbol=target, active_file=file))
    return tuple(candidates)


def gate_temporarily_unavailable(
    check: Mapping[str, Any] | None,
    verification: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether gate failure is operational rather than mathematical."""
    current = dict(check or {})
    record = dict(verification or {})
    blockers = {
        str(value or "").strip().lower()
        for value in (
            _string_values(current.get("axiom_profile_blockers"))
            + _string_values(current.get("axiom_violation"))
            + _string_values(record.get("axiom_profile_blockers"))
        )
    }
    if AXIOM_PROFILE_UNAVAILABLE in blockers:
        return True
    incremental = dict(current.get("incremental") or {})
    if (
        incremental
        and incremental.get("success") is False
        and (incremental.get("error") or current.get("error"))
    ):
        return True
    # Runner wrapper exceptions have no verification mode.  Lean proof
    # diagnostics arrive through a mode/output/messages payload instead and
    # must remain mathematical repair work, not infrastructure retries.
    if current.get("error") and not current.get("mode"):
        return True
    text = " ".join(
        str(value or "")
        for value in (
            current.get("error"),
            incremental.get("error"),
            incremental.get("error_code"),
            record.get("summary"),
        )
    ).lower()
    return any(
        marker in text
        for marker in (
            "axiom-profile-unavailable",
            "axiom profile unavailable",
            "inspection unavailable",
            "could not inspect",
            "timed out",
            "timeout",
            "connection refused",
            "failed to start",
        )
    )


def reserve_attempt(
    autonomy_state: dict[str, Any],
    candidate: PendingHelperGate,
    *,
    source_revision_text: str,
    terminal: bool,
) -> bool:
    """Reserve one process-local retry for this helper source revision.

    Priority and terminal retries use distinct stages.  Consequently a helper
    gets at most one retry before unrelated queue work and one last retry when
    no mathematical item remains.  The reservations are deliberately not
    queue-manager-owned, so a resumable process restart gets a fresh bounded
    opportunity.
    """
    stage = "terminal" if terminal else "priority"
    payload = "\0".join(
        (
            _normalized_file(candidate.active_file),
            candidate.target_symbol,
            source_revision_text,
            stage,
        )
    )
    signature = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    attempts = [
        str(value) for value in list(autonomy_state.get(ATTEMPTS_KEY) or []) if str(value).strip()
    ]
    if signature in attempts:
        return False
    attempts.append(signature)
    autonomy_state[ATTEMPTS_KEY] = attempts[-_MAX_PROCESS_ATTEMPTS:]
    return True


def unavailable_note(parent_target: str, detail: str = "") -> str:
    """Build a durable retry marker without claiming mathematical failure."""
    suffix = f": {detail.strip()}" if str(detail or "").strip() else ""
    return (
        f"{TEMPORARY_NOTE_MARKER} for prover helper from {parent_target or 'assigned target'}"
        f"{suffix}"
    )
