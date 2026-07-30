"""Cache one live ``TheoremQueueManager`` per autonomy-state mapping.

The sidecar keeps one live instance per mapping, keyed by
``id(autonomy_state)`` and guarded by a fingerprint over the manager-owned
keys. Direct external mutation of compatibility keys such as
``autonomy_state.pop("current_queue_assignment")`` changes the fingerprint
and triggers a safe rebuild.

The dict remains the compatibility serialization: every flush rewrites the
exact legacy ``OWNED_AUTONOMY_KEYS`` shape, so every reader of
``autonomy_state["current_queue_assignment"]`` etc. is unaffected by the
cache.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from leanflow_cli.workflows.queue_manager import TheoremQueueManager

# id(autonomy_state) -> (state dict, manager, fingerprint at last hydrate/flush).
# The dict itself is stored (dicts are not weakref-able) and lookups require
# identity (`entry_state is autonomy_state`) so id() reuse after GC can never
# serve a manager for the wrong state. Bounded: one entry per live run dict;
# eviction only matters for code paths that churn throwaway dicts.
_REGISTRY: dict[int, tuple[Mapping[str, Any], TheoremQueueManager, str]] = {}
_MAX_REGISTRY_ENTRIES = 8

# Hydration count for runtime observability and tests.
_HYDRATION_COUNT = 0


def hydration_count() -> int:
    """Return how many from_autonomy_state hydrations have run in this process."""
    return _HYDRATION_COUNT


def _invariant_checks_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_QUEUE_INVARIANT_CHECKS", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _fingerprint(autonomy_state: Mapping[str, Any]) -> str:
    owned = {
        key: autonomy_state.get(key)
        for key in sorted(TheoremQueueManager.OWNED_AUTONOMY_KEYS)
        if key in autonomy_state
    }
    return json.dumps(owned, sort_keys=True, ensure_ascii=False, default=str)


def _apply_live_state(mgr: TheoremQueueManager, live_state: Mapping[str, Any] | None) -> None:
    """Mirror the legacy bridge: refresh active file + queue from live state.

    An explicit (possibly empty) queue list in live_state replaces the cached
    queue — legacy fresh hydrations started empty, so a cached instance must
    not keep a stale queue across a refresh that says "no items". Only a
    ``None`` live_state leaves the cached queue untouched.
    """
    if live_state is None:
        return
    current = dict(live_state)
    active_file = str(
        current.get("active_file", "") or current.get("active_file_label", "") or ""
    ).strip()
    if active_file:
        mgr.set_active_file(active_file)
    raw_queue = current.get("declaration_queue")
    if not isinstance(raw_queue, list):
        raw_queue = current.get("declaration_queue_preview")
    if isinstance(raw_queue, list):
        mgr.replace_queue([dict(item) for item in raw_queue if isinstance(item, Mapping)])


def _hydrate(autonomy_state: Mapping[str, Any]) -> TheoremQueueManager:
    global _HYDRATION_COUNT
    _HYDRATION_COUNT += 1
    return TheoremQueueManager.from_autonomy_state(dict(autonomy_state))


def live_queue_manager(
    autonomy_state: Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None = None,
) -> TheoremQueueManager:
    """Return the single live manager for this autonomy_state dict.

    Get-or-create keyed by ``id(autonomy_state)``; a fingerprint mismatch
    (someone mutated the legacy keys directly since the last flush) rebuilds
    from the dict. Non-dict states get an uncached throwaway hydration, the
    legacy behavior for ``None``/read-only mappings.
    """
    if not isinstance(autonomy_state, dict):
        mgr = _hydrate(autonomy_state or {})
        _apply_live_state(mgr, live_state)
        return mgr
    key = id(autonomy_state)
    fingerprint = _fingerprint(autonomy_state)
    entry = _REGISTRY.get(key)
    if entry is not None and entry[0] is autonomy_state and entry[2] == fingerprint:
        mgr = entry[1]
    else:
        mgr = _hydrate(autonomy_state)
        _remember(autonomy_state, mgr, fingerprint)
    _apply_live_state(mgr, live_state)
    return mgr


def _remember(state: Mapping[str, Any], mgr: TheoremQueueManager, fingerprint: str) -> None:
    key = id(state)
    _REGISTRY.pop(key, None)
    _REGISTRY[key] = (state, mgr, fingerprint)
    while len(_REGISTRY) > _MAX_REGISTRY_ENTRIES:
        _REGISTRY.pop(next(iter(_REGISTRY)))


def flush_live_queue_manager(
    autonomy_state: Mapping[str, Any] | None, mgr: TheoremQueueManager
) -> None:
    """Render ``mgr`` into the legacy OWNED_AUTONOMY_KEYS dict shape.

    Byte-identical to the legacy flush (pop owned keys, update from
    ``to_autonomy_state()``), then re-register the fingerprint so the next
    lookup reuses this instance. Invariants run under
    LEANFLOW_QUEUE_INVARIANT_CHECKS=1, as before.
    """
    if not isinstance(autonomy_state, dict):
        return
    serialized = mgr.to_autonomy_state()
    for key in TheoremQueueManager.OWNED_AUTONOMY_KEYS:
        autonomy_state.pop(key, None)
    autonomy_state.update(serialized)
    _remember(autonomy_state, mgr, _fingerprint(autonomy_state))
    if _invariant_checks_enabled():
        mgr.check_invariants()


def invalidate_live_queue_manager(autonomy_state: Mapping[str, Any] | None) -> None:
    """Drop the cached instance (test or resume paths replacing dict contents)."""
    if autonomy_state is not None:
        _REGISTRY.pop(id(autonomy_state), None)
