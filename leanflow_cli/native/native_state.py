"""Own bounded, process-wide de-duplication state for the native runner.

The caches prevent repeated manager and verifier log entries within a workflow.

The caches are deliberately mutated *in place* (``.add`` / ``.discard`` / ``.append`` /
``.popleft`` / ``.clear`` / item assignment) and never rebound. That is what lets
``native_runner`` re-import these names and share the very same objects: the names below and the
re-imported names in ``native_runner`` point at one set of cache objects. ``_cache_once`` takes
the cache/order/limit as arguments and mutates them in place, so it carries no module state of
its own. This module depends only on the standard library and must not import
``native_runner``.
"""

from __future__ import annotations

from collections import deque
from typing import Any

__all__ = [
    "_MANAGER_VERIFICATION_LOG_CACHE_LIMIT",
    "_MANAGER_VERIFICATION_LOG_CACHE_ORDER",
    "_MANAGER_VERIFICATION_LOG_CACHE",
    "_VERIFICATION_DECISION_LOG_CACHE_LIMIT",
    "_VERIFICATION_DECISION_LOG_CACHE_ORDER",
    "_VERIFICATION_DECISION_LOG_CACHE",
    "_VERIFICATION_ADVISORY_CACHE_LIMIT",
    "_VERIFICATION_ADVISORY_CACHE_ORDER",
    "_VERIFICATION_ADVISORY_CACHE",
    "_VERIFICATION_ADVISORY_RESULT_CACHE",
    "_cache_once",
]


_MANAGER_VERIFICATION_LOG_CACHE_LIMIT = 256
_MANAGER_VERIFICATION_LOG_CACHE_ORDER: deque[tuple[str, str, bool, str]] = deque(
    maxlen=_MANAGER_VERIFICATION_LOG_CACHE_LIMIT
)
_MANAGER_VERIFICATION_LOG_CACHE: set[tuple[str, str, bool, str]] = set()
_VERIFICATION_DECISION_LOG_CACHE_LIMIT = 512
_VERIFICATION_DECISION_LOG_CACHE_ORDER: deque[str] = deque(
    maxlen=_VERIFICATION_DECISION_LOG_CACHE_LIMIT
)
_VERIFICATION_DECISION_LOG_CACHE: set[str] = set()
_VERIFICATION_ADVISORY_CACHE_LIMIT = 128
_VERIFICATION_ADVISORY_CACHE_ORDER: deque[str] = deque(maxlen=_VERIFICATION_ADVISORY_CACHE_LIMIT)
_VERIFICATION_ADVISORY_CACHE: set[str] = set()
_VERIFICATION_ADVISORY_RESULT_CACHE: dict[str, dict[str, Any]] = {}


def _cache_once(signature: str, *, cache: set[str], order: deque[str], limit: int) -> bool:
    key = str(signature or "")
    if not key:
        return False
    if key in cache:
        return False
    if len(order) >= limit:
        old = order.popleft()
        cache.discard(old)
    order.append(key)
    cache.add(key)
    return True
