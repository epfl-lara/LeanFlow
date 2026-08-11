"""Persist bounded helper-integration credit until an exact target gate accepts."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.queue_manager import TheoremKey

STATE_KEY = "pending_promoted_helper_integration"
SUMMARY_KEY = "pending_promoted_helper_integration"
SCHEMA_VERSION = 1
MAX_HELPERS = 16
MAX_GATE_ATTEMPTS = 8


def _helper_names(values: Sequence[object]) -> tuple[str, ...]:
    """Return a stable bounded set of nonempty helper identifiers."""
    return tuple(
        dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip())
    )[:MAX_HELPERS]


@dataclass(frozen=True)
class PendingHelperIntegration:
    """Describe one assignment-scoped structural promotion awaiting proof authority."""

    target_symbol: str
    active_file: str
    helper_names: tuple[str, ...]
    gate_attempts: int = 0

    @property
    def key(self) -> TheoremKey:
        """Return the normalized queue identity for this pending record."""
        return TheoremKey.make(self.target_symbol, self.active_file)

    @property
    def exhausted(self) -> bool:
        """Return whether bounded failed-gate retention has been spent."""
        return self.gate_attempts >= MAX_GATE_ATTEMPTS

    def matches(self, target_symbol: str, active_file: str) -> bool:
        """Return whether this record belongs to the exact active assignment."""
        return self.key == TheoremKey.make(target_symbol, active_file)

    def to_mapping(self) -> dict[str, object]:
        """Serialize the bounded record for autonomy and campaign state."""
        return {
            "version": SCHEMA_VERSION,
            "target_symbol": self.target_symbol,
            "active_file": self.active_file,
            "helper_names": list(self.helper_names),
            "gate_attempts": self.gate_attempts,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> PendingHelperIntegration | None:
        """Parse one valid bounded pending record, rejecting malformed state."""
        target_symbol = str(raw.get("target_symbol", "") or "").strip()
        active_file = str(raw.get("active_file", "") or "").strip()
        helpers_raw = raw.get("helper_names")
        helpers = _helper_names(helpers_raw if isinstance(helpers_raw, (list, tuple)) else ())
        try:
            gate_attempts = max(0, min(MAX_GATE_ATTEMPTS, int(raw.get("gate_attempts", 0) or 0)))
        except (TypeError, ValueError):
            gate_attempts = 0
        candidate = cls(
            target_symbol=target_symbol,
            active_file=active_file,
            helper_names=helpers,
            gate_attempts=gate_attempts,
        )
        if not candidate.key.is_valid() or not helpers:
            return None
        return candidate


def _persist(autonomy_state: dict[str, Any], record: PendingHelperIntegration | None) -> None:
    """Write one pending record to memory and the durable plan summary."""
    payload = record.to_mapping() if record is not None else {}
    if record is None:
        autonomy_state.pop(STATE_KEY, None)
    else:
        autonomy_state[STATE_KEY] = payload
    if plan_state.plan_state_enabled():
        with contextlib.suppress(Exception):
            plan_state.save_summary({SUMMARY_KEY: payload})


def load(autonomy_state: dict[str, Any]) -> PendingHelperIntegration | None:
    """Load pending integration state, hydrating durable state when necessary."""
    raw = autonomy_state.get(STATE_KEY)
    record = PendingHelperIntegration.from_mapping(raw) if isinstance(raw, Mapping) else None
    if record is not None:
        return record
    if not plan_state.plan_state_enabled():
        autonomy_state.pop(STATE_KEY, None)
        return None
    with contextlib.suppress(Exception):
        summary_raw = plan_state.load_summary().get(SUMMARY_KEY)
        if isinstance(summary_raw, Mapping):
            record = PendingHelperIntegration.from_mapping(summary_raw)
            if record is not None:
                autonomy_state[STATE_KEY] = record.to_mapping()
                return record
    autonomy_state.pop(STATE_KEY, None)
    return None


def remember(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    helper_names: Sequence[str],
) -> PendingHelperIntegration | None:
    """Merge newly promoted helpers into the bounded exact-assignment record."""
    helpers = _helper_names(helper_names)
    key = TheoremKey.make(target_symbol, active_file)
    if not key.is_valid() or not helpers:
        return load(autonomy_state)
    existing = load(autonomy_state)
    if existing is not None and existing.matches(target_symbol, active_file):
        fresh_helpers = tuple(name for name in helpers if name not in existing.helper_names)
        merged = (
            _helper_names((*fresh_helpers, *existing.helper_names))
            if fresh_helpers
            else existing.helper_names
        )
        # A newly authenticated helper is fresh structural evidence. Its first
        # parent integration attempts must not inherit a nearly exhausted gate
        # budget from older helpers on the same long-running assignment.
        record = replace(
            existing,
            helper_names=merged,
            gate_attempts=(0 if fresh_helpers else existing.gate_attempts),
        )
    else:
        record = PendingHelperIntegration(
            target_symbol=str(target_symbol or "").strip(),
            active_file=str(active_file or "").strip(),
            helper_names=helpers,
        )
    _persist(autonomy_state, record)
    return record


def note_gate_attempt(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
) -> PendingHelperIntegration | None:
    """Increment the bounded committed-gate counter for the exact assignment."""
    existing = load(autonomy_state)
    if existing is None or not existing.matches(target_symbol, active_file):
        return existing
    record = replace(
        existing,
        gate_attempts=min(MAX_GATE_ATTEMPTS, existing.gate_attempts + 1),
    )
    _persist(autonomy_state, record)
    return record


def retire(autonomy_state: dict[str, Any]) -> PendingHelperIntegration | None:
    """Clear and return the current pending integration record."""
    existing = load(autonomy_state)
    _persist(autonomy_state, None)
    return existing
