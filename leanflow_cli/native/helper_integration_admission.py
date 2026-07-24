"""Reserve foreground Lean priority while an exact helper awaits integration.

``LEANFLOW_HELPER_INTEGRATION_ADMISSION_LEASE_S=0`` disables the reservation.
Any positive value remains enabled and is raised to the minimum duration that
can safely overlap the refresher's scheduling floor.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.project_resource_admission import (
    MAX_FOREGROUND_HANDOFF_LEASE_S,
    ProjectForegroundPriorityLease,
    reserve_project_foreground_priority_lease,
)

logger = logging.getLogger(__name__)

HELPER_INTEGRATION_LEASE_DEFAULT_S = MAX_FOREGROUND_HANDOFF_LEASE_S
_HELPER_INTEGRATION_LEASE_ENV = "LEANFLOW_HELPER_INTEGRATION_ADMISSION_LEASE_S"
_RESERVATION_ATTR = "_research_helper_integration_admission_reservation"
_MIN_REFRESH_INTERVAL_S = 0.05
_MAX_REFRESH_INTERVAL_S = 30.0
_REFRESH_DEADLINE_FRACTION = 1.0 / 3.0
# Keep the minimum scheduler tick at no more than one quarter of the shortest
# effective lease; the normal cadence is additionally capped at one third.
_MIN_CONTINUOUS_LEASE_S = _MIN_REFRESH_INTERVAL_S * 4.0

AdmissionObserver = Callable[[str, Mapping[str, object]], None]


def _bounded_lease_seconds(value: Any) -> float:
    """Return zero for disabled leases or a continuity-safe positive lease."""
    try:
        configured = float(value)
    except (TypeError, ValueError):
        configured = HELPER_INTEGRATION_LEASE_DEFAULT_S
    if not math.isfinite(configured):
        configured = HELPER_INTEGRATION_LEASE_DEFAULT_S
    bounded = max(0.0, min(MAX_FOREGROUND_HANDOFF_LEASE_S, configured))
    if bounded <= 0.0:
        return 0.0
    return max(_MIN_CONTINUOUS_LEASE_S, bounded)


def _configured_lease_seconds() -> float:
    """Return one bounded, continuity-safe crash-recovery deadline."""
    raw = str(
        os.getenv(
            _HELPER_INTEGRATION_LEASE_ENV,
            HELPER_INTEGRATION_LEASE_DEFAULT_S,
        )
        or ""
    ).strip()
    return _bounded_lease_seconds(raw)


def _refresh_interval(lease_seconds: float) -> float:
    """Return an early refresh cadence that always overlaps valid markers."""
    return max(
        _MIN_REFRESH_INTERVAL_S,
        min(
            _MAX_REFRESH_INTERVAL_S,
            lease_seconds * _REFRESH_DEADLINE_FRACTION,
        ),
    )


def _bounded_refresh_interval(
    lease_seconds: float,
    requested: Any | None,
) -> float:
    """Return a cadence below the lease deadline with scheduler margin."""
    default = _refresh_interval(lease_seconds)
    if requested is None:
        preferred = default
    else:
        try:
            preferred = float(requested)
        except (TypeError, ValueError):
            preferred = default
        if not math.isfinite(preferred):
            preferred = default
    preferred = max(_MIN_REFRESH_INTERVAL_S, preferred)
    latest_safe = min(
        _MAX_REFRESH_INTERVAL_S,
        lease_seconds * _REFRESH_DEADLINE_FRACTION,
    )
    return min(preferred, latest_safe)


@dataclass
class HelperIntegrationAdmissionReservation:
    """Refresh one crash-bounded marker until its helper is resolved.

    Every refresh publishes the replacement marker before releasing the prior
    one. Background Lean therefore sees a continuous foreground reservation,
    while a process crash still releases authority at the last finite marker
    deadline. The reservation never owns the main Lean gate and never cancels
    background work that was already admitted.
    """

    candidate_id: str
    project_root: str
    lease_seconds: float
    refresh_interval_s: float
    reason: str
    _lease: ProjectForegroundPriorityLease
    _observer: AdmissionObserver | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _released: bool = field(default=False, repr=False)
    refresh_count: int = 0

    @classmethod
    def start(
        cls,
        *,
        candidate_id: str,
        project_root: str,
        reason: str,
        observer: AdmissionObserver | None = None,
        lease_seconds: float | None = None,
        refresh_interval_s: float | None = None,
    ) -> HelperIntegrationAdmissionReservation | None:
        """Publish the initial marker and start its daemon refresher."""
        seconds = (
            _configured_lease_seconds()
            if lease_seconds is None
            else _bounded_lease_seconds(lease_seconds)
        )
        if not candidate_id or seconds <= 0.0:
            return None
        lease = reserve_project_foreground_priority_lease(
            project_root,
            seconds,
            reason=reason,
        )
        if lease is None:
            return None
        interval = _bounded_refresh_interval(seconds, refresh_interval_s)
        reservation = cls(
            candidate_id=str(candidate_id),
            project_root=str(project_root),
            lease_seconds=seconds,
            refresh_interval_s=interval,
            reason=str(reason or "checked helper awaiting integration")[:300],
            _lease=lease,
            _observer=observer,
        )
        reservation._notify("started", reservation.snapshot())
        thread = threading.Thread(
            target=reservation._refresh_loop,
            name=f"leanflow-helper-admission-{str(candidate_id)[-8:]}",
            daemon=True,
        )
        reservation._thread = thread
        thread.start()
        return reservation

    @property
    def active(self) -> bool:
        """Return whether this owner still refreshes its marker."""
        with self._guard:
            return not self._released

    def snapshot(self) -> dict[str, object]:
        """Return JSON-safe reservation metadata for workflow events."""
        with self._guard:
            lease = self._lease
            released = self._released
            refresh_count = self.refresh_count
        return {
            "candidate_id": self.candidate_id,
            "project_root": self.project_root,
            "marker_path": lease.marker_path,
            "expires_at": lease.expires_at,
            "lease_seconds": self.lease_seconds,
            "refresh_interval_s": self.refresh_interval_s,
            "refresh_count": refresh_count,
            "released": released,
            "reason": self.reason,
        }

    def refresh(self) -> bool:
        """Overlap one fresh marker with the current marker before release."""
        if self._stop.is_set():
            return False
        replacement = reserve_project_foreground_priority_lease(
            self.project_root,
            self.lease_seconds,
            reason=self.reason,
        )
        if replacement is None:
            self._notify("refresh_failed", self.snapshot())
            return False
        with self._guard:
            if self._released or self._stop.is_set():
                replacement.release()
                return False
            prior = self._lease
            self._lease = replacement
            self.refresh_count += 1
            details = self.snapshot_unlocked()
            details["prior_marker_path"] = prior.marker_path
        # The replacement is visible before the old marker disappears. This
        # ordering closes the same check/acquire race as the core waiter scan.
        prior.release()
        self._notify("refreshed", details)
        return True

    def snapshot_unlocked(self) -> dict[str, object]:
        """Return metadata while ``_guard`` is already held."""
        return {
            "candidate_id": self.candidate_id,
            "project_root": self.project_root,
            "marker_path": self._lease.marker_path,
            "expires_at": self._lease.expires_at,
            "lease_seconds": self.lease_seconds,
            "refresh_interval_s": self.refresh_interval_s,
            "refresh_count": self.refresh_count,
            "released": self._released,
            "reason": self.reason,
        }

    def release(self, *, reason: str) -> bool:
        """Stop refreshing and consume the exact current marker."""
        self._stop.set()
        with self._guard:
            if self._released:
                return False
            self._released = True
            lease = self._lease
            details = self.snapshot_unlocked()
            details["release_reason"] = str(reason or "resolved")[:160]
        released = lease.release()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(1.0, max(0.1, self.refresh_interval_s)))
        details["marker_released"] = released
        self._notify("released", details)
        return True

    def _refresh_loop(self) -> None:
        """Renew the bounded marker until its owner retires the candidate."""
        while not self._stop.wait(self.refresh_interval_s):
            try:
                self.refresh()
            except Exception:
                logger.debug("helper integration admission refresh failed", exc_info=True)
                self._notify("refresh_failed", self.snapshot())

    def _notify(self, phase: str, details: Mapping[str, object]) -> None:
        """Report reservation lifecycle without weakening admission authority."""
        if self._observer is None:
            return
        try:
            self._observer(str(phase), dict(details))
        except Exception:
            logger.debug("helper integration admission observer failed", exc_info=True)


def current(agent: Any) -> HelperIntegrationAdmissionReservation | None:
    """Return the active reservation installed on an agent."""
    value = getattr(agent, _RESERVATION_ATTR, None)
    return value if isinstance(value, HelperIntegrationAdmissionReservation) else None


def ensure(
    agent: Any,
    *,
    candidate_id: str,
    project_root: str,
    background_workers: int,
    reason: str,
    observer: AdmissionObserver | None = None,
) -> HelperIntegrationAdmissionReservation | None:
    """Install or retain the exact candidate's continuous reservation."""
    existing = current(agent)
    if background_workers <= 0 or not candidate_id:
        release(agent, reason="no background helper integration contention")
        return None
    if existing is not None and existing.candidate_id == candidate_id and existing.active:
        return existing

    replacement = HelperIntegrationAdmissionReservation.start(
        candidate_id=candidate_id,
        project_root=project_root,
        reason=reason,
        observer=observer,
    )
    if replacement is None:
        if existing is not None and existing.candidate_id != candidate_id:
            release(agent, reason="helper integration candidate changed")
        return None
    try:
        setattr(agent, _RESERVATION_ATTR, replacement)
    except Exception:
        replacement.release(reason="agent rejected helper integration reservation")
        return None
    # Start the new candidate first so replacing stale ownership has no marker
    # gap in which a queued background verifier can enter.
    if existing is not None:
        existing.release(reason="helper integration candidate replaced")
    return replacement


def release(
    agent: Any,
    *,
    reason: str,
    expected_candidate_id: str = "",
) -> bool:
    """Release the installed reservation when its exact candidate is gone."""
    existing = current(agent)
    if existing is None:
        return False
    expected = str(expected_candidate_id or "").strip()
    if expected and existing.candidate_id != expected:
        return False
    try:
        delattr(agent, _RESERVATION_ATTR)
    except AttributeError:
        pass
    return existing.release(reason=reason)
