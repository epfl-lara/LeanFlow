"""Shadow-compare harness for the unified queue verdict policy (Phase 0, P0.4).

While the legacy open-coded verdict branches remain authoritative, each of the
four production gates can — under ``LEANFLOW_QUEUE_DECIDE_SHADOW=1`` — also
evaluate the pure ``TheoremQueueManager.decide()`` on a fresh hydration of the
same state and compare the outcomes. A divergence produces a mismatch payload
the runner logs as a ``queue-decide-shadow-mismatch`` activity event
(greppable post-hoc in ``activity/agents/*.jsonl``). Zero mismatches over the
demo-project corpus and a real multi-theorem run is the promotion criterion
for flipping decide() authoritative.

decide() is pure and the shadow hydrates its own manager instance, so shadow
evaluation can never mutate production retry counters.
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

#: The comparison surface (spec P0.4): every field the legacy branches encode.
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
    """The promotion companion to shadow: make ``decide()`` authoritative.

    When set, the four production gates take their VERDICT from ``decide()``
    instead of the legacy open-coded classification (which stays in place as
    the flag-off path — flag-off is byte-identical to today). ``decide()`` is
    the verdict oracle only: the retry side effects still run through the
    proven legacy helpers (``_increment``/``_clear``, keyed by explicit
    target/file), never ``apply_decision``. The promotion criterion is zero
    ``queue-decide-shadow-mismatch`` events over the demo corpus and a real
    multi-theorem run; on the exercised (source × classification) cells the
    two are equal, so the flip preserves behavior there. A few cells the
    shadow never exercised (e.g. FINAL_REPORT + FUTURE_ONLY) adopt decide()'s
    canonical golden-grid verdict, which is the intended policy.
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
