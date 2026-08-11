"""Gate expensive research-ledger reconciliation on exact delivery work."""

from __future__ import annotations

import threading
from collections.abc import Mapping, MutableMapping
from typing import Any

from leanflow_cli.workflows.queue_models import TheoremKey

STATE_KEY = "research_findings_delivery_gate"
# Version 2 reopens checkpoints written before checked-candidate priority.  A
# v1 scan could commit the whole completion watermark after staging only an
# earlier bounded FIFO batch, leaving later checked source with no dirty event.
SCHEMA_VERSION = 2

_LOCK = threading.RLock()


def _counter(value: object) -> int:
    """Return a nonnegative persisted watermark."""
    try:
        return max(0, int(str(value or 0)))
    except (TypeError, ValueError):
        return 0


def _record(state: MutableMapping[str, Any], scope: str) -> dict[str, Any]:
    """Return a fail-closed record for the exact assignment scope.

    Missing, stale-schema, malformed, and assignment-mismatched state all
    require one reconciliation scan. This preserves upgrade and resume
    recovery while allowing a clean same-process callback to remain O(1).
    """
    normalized_scope = str(scope or "[project-scope]")
    raw = state.get(STATE_KEY)
    current = dict(raw) if isinstance(raw, dict) else {}
    if (
        _counter(current.get("schema_version")) != SCHEMA_VERSION
        or str(current.get("scope", "") or "") != normalized_scope
    ):
        current = {
            "schema_version": SCHEMA_VERSION,
            "scope": normalized_scope,
            "requested_watermark": 0,
            "scanned_watermark": 0,
            "dirty": True,
        }
        state[STATE_KEY] = current
    return current


def mark_published(
    state: MutableMapping[str, Any],
    *,
    scope: str,
    watermark: int,
) -> None:
    """Mark one newly published completion prefix for delivery scanning.

    Repeated publication of an already-scanned source returns its old event
    watermark and therefore cannot dirty the gate again.
    """
    with _LOCK:
        current = _record(state, scope)
        requested = max(_counter(current.get("requested_watermark")), _counter(watermark))
        scanned = _counter(current.get("scanned_watermark"))
        current["requested_watermark"] = requested
        if requested > scanned:
            current["dirty"] = True
        state[STATE_KEY] = current


def scan_required(state: MutableMapping[str, Any], *, scope: str) -> bool:
    """Return whether this assignment has unscanned delivery work."""
    with _LOCK:
        current = _record(state, scope)
        return bool(current.get("dirty")) or _counter(
            current.get("requested_watermark")
        ) > _counter(current.get("scanned_watermark"))


def request_scan(state: MutableMapping[str, Any], *, scope: str) -> None:
    """Reopen delivery after a foreground acknowledgement frees capacity."""
    with _LOCK:
        current = _record(state, scope)
        current["dirty"] = True
        state[STATE_KEY] = current


def current_assignment_scope(state: MutableMapping[str, Any]) -> str:
    """Return the active queue scope used by foreground delivery."""
    raw_assignment = state.get("current_queue_assignment")
    assignment = dict(raw_assignment) if isinstance(raw_assignment, Mapping) else {}
    key = TheoremKey.make(
        str(assignment.get("target_symbol", "") or ""),
        str(assignment.get("active_file", "") or ""),
    )
    return key.storage_key() if key.is_valid() else "[project-scope]"


def request_current_assignment_scan(state: MutableMapping[str, Any]) -> None:
    """Reopen the scope whose staging capacity an acknowledgement changed."""
    request_scan(state, scope=current_assignment_scope(state))


def mark_scanned(state: MutableMapping[str, Any], *, scope: str) -> None:
    """Commit a successful full reconciliation scan for this scope."""
    with _LOCK:
        current = _record(state, scope)
        current["scanned_watermark"] = _counter(current.get("requested_watermark"))
        current["dirty"] = False
        state[STATE_KEY] = current
