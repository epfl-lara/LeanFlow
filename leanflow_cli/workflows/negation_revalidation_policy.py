"""Define cold-start deadlines for authoritative source-negation checks."""

from __future__ import annotations

import os

SOURCE_PROMOTION_TIMEOUT_FLOOR_S = 300
SOURCE_PROMOTION_TIMEOUT_MAX_S = 1800


def source_promotion_timeout_s(*, probe_timeout_s: int) -> int:
    """Return a cold-safe whole-source Lean deadline.

    Scratch negation probes are small, while authoritative source promotion
    elaborates the complete current module in a new process.  A lower or
    malformed override cannot undercut either the cold-start floor or a larger
    general probe budget; operators may only raise the deadline.
    """
    try:
        configured = int(
            os.getenv(
                "LEANFLOW_NEGATION_SOURCE_PROMOTION_TIMEOUT_S",
                str(SOURCE_PROMOTION_TIMEOUT_FLOOR_S),
            )
            or SOURCE_PROMOTION_TIMEOUT_FLOOR_S
        )
    except ValueError:
        configured = SOURCE_PROMOTION_TIMEOUT_FLOOR_S
    return min(
        SOURCE_PROMOTION_TIMEOUT_MAX_S,
        max(SOURCE_PROMOTION_TIMEOUT_FLOOR_S, int(probe_timeout_s), configured),
    )
