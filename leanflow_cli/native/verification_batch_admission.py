"""Reserve foreground priority across post-edit verification transactions.

``apply_verified_patch`` performs its first authoritative Lean check while it
still owns the project gate. The native runner then performs helper/target
gates and refreshes the queue after the registry call returns. This module
keeps one crash-bounded foreground marker alive until every overlapping patch
callback has completed that unlocked transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from leanflow_cli.native.helper_integration_admission import (
    AdmissionObserver,
    HelperIntegrationAdmissionReservation,
)

logger = logging.getLogger(__name__)

_RESERVATION_ATTR = "_native_post_edit_verification_admission_reservation"
_REGISTRY_GUARD = threading.Lock()


@dataclass
class VerificationBatchAdmission:
    """Own one refreshed marker shared by overlapping patch callbacks."""

    batch_id: str
    reservation: HelperIntegrationAdmissionReservation
    pending_invocations: dict[str, int]
    _observer: AdmissionObserver | None = field(default=None, repr=False)

    @property
    def pending_batches(self) -> int:
        """Return the number of joined tool invocations awaiting callbacks."""
        return sum(self.pending_invocations.values())

    @property
    def active(self) -> bool:
        """Return whether this group still owns an active marker."""
        return self.pending_batches > 0 and self.reservation.active

    def snapshot(self) -> dict[str, object]:
        """Return JSON-safe group and marker metadata."""
        details = dict(self.reservation.snapshot())
        details.pop("candidate_id", None)
        details["batch_id"] = self.batch_id
        details["pending_batches"] = self.pending_batches
        return details

    def notify(self, phase: str, details: Mapping[str, object]) -> None:
        """Report aggregate lifecycle without weakening admission authority."""
        if self._observer is None:
            return
        try:
            self._observer(phase, dict(details))
        except Exception:
            logger.debug("verification batch admission observer failed", exc_info=True)


def _current_unlocked(agent: Any) -> VerificationBatchAdmission | None:
    """Return the installed group while the registry guard is held."""
    value = getattr(agent, _RESERVATION_ATTR, None)
    return value if isinstance(value, VerificationBatchAdmission) else None


def current(agent: Any) -> VerificationBatchAdmission | None:
    """Return the active post-edit verification group on an agent."""
    with _REGISTRY_GUARD:
        return _current_unlocked(agent)


def invocation_key(arguments: Mapping[str, Any] | None) -> str:
    """Return a stable identity shared by handoff and post-result callbacks."""
    try:
        payload = json.dumps(
            dict(arguments or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        payload = repr(dict(arguments or {}))
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def has_pending(agent: Any, *, expected_invocation_key: str) -> bool:
    """Return whether one exact tool invocation joined the active group."""
    expected = str(expected_invocation_key or "").strip()
    if not expected:
        return False
    with _REGISTRY_GUARD:
        existing = _current_unlocked(agent)
        return bool(existing is not None and existing.pending_invocations.get(expected, 0) > 0)


def begin(
    agent: Any,
    *,
    project_root: str,
    background_workers: int,
    expected_invocation_key: str,
    reason: str,
    observer: AdmissionObserver | None = None,
) -> VerificationBatchAdmission | None:
    """Join or start a continuous marker before the patch tool releases Lean.

    Successful patch tools can complete concurrently even though their
    Lean-heavy bodies are capacity-limited. Sharing one reference-counted
    marker means completion order cannot release foreground priority while
    another patch callback is still verifying or refreshing live state.
    """
    if agent is None or background_workers <= 0:
        release(agent, reason="post-edit verification has no background contention")
        return None
    expected = str(expected_invocation_key or "").strip()
    if not expected:
        return None
    with _REGISTRY_GUARD:
        existing = _current_unlocked(agent)
        if existing is not None and existing.active:
            existing.pending_invocations[expected] = (
                existing.pending_invocations.get(expected, 0) + 1
            )
            details = existing.snapshot()
            joined = existing
        else:
            batch_id = f"verified-patch-{uuid.uuid4().hex}"
            try:
                reservation = HelperIntegrationAdmissionReservation.start(
                    candidate_id=batch_id,
                    project_root=project_root,
                    reason=reason,
                    observer=observer,
                )
            except Exception:
                logger.debug("post-edit verification admission start failed", exc_info=True)
                return None
            if reservation is None:
                return None
            joined = VerificationBatchAdmission(
                batch_id=batch_id,
                reservation=reservation,
                pending_invocations={expected: 1},
                _observer=observer,
            )
            try:
                setattr(agent, _RESERVATION_ATTR, joined)
            except Exception:
                reservation.release(reason="agent rejected post-edit verification reservation")
                return None
            details = {}
    if details:
        joined.notify("joined", details)
    return joined


def complete_one(
    agent: Any,
    *,
    expected_invocation_key: str,
    reason: str,
) -> bool:
    """Complete one exact callback and release after the last pending batch."""
    expected = str(expected_invocation_key or "").strip()
    if not expected:
        return False
    with _REGISTRY_GUARD:
        existing = _current_unlocked(agent)
        if existing is None:
            return False
        pending = existing.pending_invocations.get(expected, 0)
        if pending <= 0:
            return False
        if pending == 1:
            existing.pending_invocations.pop(expected, None)
        else:
            existing.pending_invocations[expected] = pending - 1
        details = existing.snapshot()
        if existing.pending_batches > 0:
            remaining = True
        else:
            remaining = False
            try:
                delattr(agent, _RESERVATION_ATTR)
            except AttributeError:
                pass
    if remaining:
        details["completion_reason"] = str(reason or "batch completed")[:160]
        existing.notify("batch_completed", details)
        return True
    return existing.reservation.release(reason=reason)


def release(agent: Any, *, reason: str) -> bool:
    """Force-release the installed group during shutdown or mode changes."""
    with _REGISTRY_GUARD:
        existing = _current_unlocked(agent)
        if existing is None:
            return False
        existing.pending_invocations.clear()
        try:
            delattr(agent, _RESERVATION_ATTR)
        except AttributeError:
            pass
    return existing.reservation.release(reason=reason)
