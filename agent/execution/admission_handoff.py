"""Request bounded post-tool foreground priority from an agent policy hook."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ForegroundHandoffAdmission(Protocol):
    """Describe the admission capability needed by the executor hook."""

    def reserve_foreground_handoff(self, seconds: float, *, reason: str = "") -> float:
        """Request an unlocked bounded handoff after admission release."""


HandoffRequestCallback = Callable[[str, Mapping[str, Any], str], float]
_INITIAL_FOREGROUND_LEASE_ATTR = "_project_lean_initial_foreground_lease"


class InitialForegroundLease(Protocol):
    """Describe a cancellable pre-admission priority marker."""

    def release(self) -> bool:
        """Consume the priority marker and return whether it was still active."""


def replace_initial_foreground_lease(agent: Any, lease: InitialForegroundLease) -> None:
    """Install one lease, releasing any older unconsumed reservation first."""
    clear_initial_foreground_lease(agent)
    setattr(agent, _INITIAL_FOREGROUND_LEASE_ATTR, lease)


def current_initial_foreground_lease(agent: Any) -> InitialForegroundLease | None:
    """Return the currently installed lease without consuming it."""
    lease = getattr(agent, _INITIAL_FOREGROUND_LEASE_ATTR, None)
    return lease if callable(getattr(lease, "release", None)) else None


def clear_initial_foreground_lease(
    agent: Any,
    *,
    expected: InitialForegroundLease | None = None,
) -> bool:
    """Release and forget the expected pending foreground lease.

    A post-tool callback may replace a batch's lease with a fresh reservation
    for the next provider/tool handoff. Identity matching prevents the batch
    finalizer from accidentally consuming that newer lease.
    """
    lease = getattr(agent, _INITIAL_FOREGROUND_LEASE_ATTR, None)
    if lease is None:
        return False
    if expected is not None and lease is not expected:
        return False
    setattr(agent, _INITIAL_FOREGROUND_LEASE_ATTR, None)
    release = getattr(lease, "release", None)
    if not callable(release):
        return False
    try:
        return bool(release())
    except Exception:
        logger.debug("initial project Lean priority release failed", exc_info=True)
        return False


def consume_initial_foreground_lease(agent: Any) -> bool:
    """Consume the one-shot lease after foreground admission is secured."""
    return clear_initial_foreground_lease(agent)


def reserve_post_tool_foreground_handoff(
    agent: Any,
    admission: ForegroundHandoffAdmission,
    *,
    function_name: str,
    arguments: Mapping[str, Any],
    result: str,
) -> float:
    """Apply an optional agent policy before the foreground admission exits.

    Policy errors are observability failures, not tool failures. The core
    admission authority still bounds every positive request.
    """
    callback = getattr(agent, "_project_lean_handoff_request_callback", None)
    if not callable(callback):
        return 0.0
    try:
        requested = float(callback(function_name, dict(arguments), result) or 0.0)
    except Exception:
        logger.debug("project Lean handoff policy failed", exc_info=True)
        return 0.0
    if not math.isfinite(requested) or requested <= 0.0:
        return 0.0
    try:
        return admission.reserve_foreground_handoff(
            requested,
            reason=f"native exact-candidate commit handoff after {function_name}",
        )
    except Exception:
        logger.debug("project Lean handoff reservation failed", exc_info=True)
        return 0.0
