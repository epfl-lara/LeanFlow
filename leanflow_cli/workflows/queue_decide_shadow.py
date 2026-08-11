"""Compare queue-verdict paths without mutating production state.

The optional shadow path evaluates ``TheoremQueueManager.decide()`` on a fresh
hydration and emits a structured mismatch when verdicts diverge. The authority
switch selects the unified verdict policy while runner-owned retry side effects
remain unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from leanflow_cli.workflows.queue_manager import (
    DecisionContext,
    DecisionSource,
    ManagerCheck,
    TheoremQueueManager,
)

#: Every verdict field represented by the compatibility branches.
COMPARED_FIELDS = (
    "action",
    "feedback_kind",
    "retry_limit",
    "record_failed_attempt",
    "restore_baseline",
)


def shadow_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_QUEUE_DECIDE_SHADOW", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def authority_enabled() -> bool:
    """Return whether production gates use ``decide()`` as verdict authority.

    Retry side effects remain in the runner and are keyed by the explicit
    target and file; this switch changes verdict selection only.
    """
    raw = str(os.getenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def legacy_outcome(
    *,
    action: str,
    feedback_kind: str = "",
    retry_limit: int = 0,
    record_failed_attempt: bool = False,
    restore_baseline: bool = False,
) -> dict[str, Any]:
    """Reify a legacy branch outcome into the comparison tuple shape."""
    return {
        "action": action,
        "feedback_kind": feedback_kind,
        "retry_limit": retry_limit,
        "record_failed_attempt": record_failed_attempt,
        "restore_baseline": restore_baseline,
    }


def shadow_compare(
    *,
    autonomy_state: Mapping[str, Any] | None,
    source: DecisionSource,
    check: ManagerCheck,
    legacy: Mapping[str, Any],
    signature: str = "",
    cleanup_reason: str = "",
    axiom_blockers: tuple[str, ...] = (),
    claims_success: bool = True,
) -> dict[str, Any] | None:
    """Evaluate decide() in shadow and return a mismatch payload, or None.

    Uses a throwaway hydration (peek discipline) so neither the live cached
    manager nor the legacy dict is touched. Never raises: a shadow crash is
    reported as a mismatch payload with an ``error`` field rather than
    disturbing the production gate.
    """
    try:
        mgr = TheoremQueueManager.from_autonomy_state(dict(autonomy_state or {}))
        ctx = DecisionContext(
            source=source,
            check=check,
            signature=signature,
            cleanup_reason=cleanup_reason,
            axiom_blockers=axiom_blockers,
            claims_success=claims_success,
        )
        decision = mgr.decide(ctx)
        decided = {
            "action": decision.action,
            "feedback_kind": decision.feedback_kind,
            "retry_limit": decision.retry_limit,
            "record_failed_attempt": decision.record_failed_attempt,
            "restore_baseline": decision.restore_baseline,
        }
    except Exception as exc:
        return {
            "source": source.value,
            "legacy": dict(legacy),
            "decided": {},
            "error": str(exc)[:500],
        }
    diverged = [field for field in COMPARED_FIELDS if decided.get(field) != legacy.get(field)]
    if not diverged:
        return None
    return {
        "source": source.value,
        "legacy": dict(legacy),
        "decided": decided,
        "diverged_fields": diverged,
    }
