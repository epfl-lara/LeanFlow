"""Reserve foreground Lean priority across provider-to-tool handoffs."""

from __future__ import annotations

import math
import os
from typing import Any

from agent.execution.admission_handoff import replace_initial_foreground_lease
from core.project_resource_admission import (
    MAX_FOREGROUND_HANDOFF_LEASE_S,
    ProjectForegroundPriorityLease,
    reserve_project_foreground_priority_lease,
)

SCOPE_ENTRY_FOREGROUND_LEASE_DEFAULT_S = 120.0
_SCOPE_ENTRY_FOREGROUND_LEASE_ENV = "LEANFLOW_SCOPE_ENTRY_FOREGROUND_LEASE_S"


def configured_lease_seconds() -> float:
    """Return the bounded scope-entry foreground priority duration."""
    raw = str(
        os.getenv(
            _SCOPE_ENTRY_FOREGROUND_LEASE_ENV,
            SCOPE_ENTRY_FOREGROUND_LEASE_DEFAULT_S,
        )
        or ""
    ).strip()
    try:
        configured = float(raw)
    except ValueError:
        configured = SCOPE_ENTRY_FOREGROUND_LEASE_DEFAULT_S
    if not math.isfinite(configured):
        configured = SCOPE_ENTRY_FOREGROUND_LEASE_DEFAULT_S
    return max(0.0, min(MAX_FOREGROUND_HANDOFF_LEASE_S, configured))


def arm(
    agent: Any,
    *,
    project_root: str,
    background_workers: int,
    reason: str = "research scope entry awaiting first foreground Lean admission",
) -> ProjectForegroundPriorityLease | None:
    """Arm one cancellable lease before a foreground provider/tool handoff.

    Provider calls and research reasoning remain concurrent. Only background
    operations that reach the project Lean gate wait until the foreground has
    actually secured its first admitted tool or the bounded deadline expires.
    """
    if agent is None or background_workers <= 0:
        return None
    seconds = configured_lease_seconds()
    if seconds <= 0.0:
        return None
    lease = reserve_project_foreground_priority_lease(
        project_root,
        seconds,
        reason=reason,
    )
    if lease is not None:
        try:
            replace_initial_foreground_lease(agent, lease)
        except Exception:
            lease.release()
            raise
    return lease
