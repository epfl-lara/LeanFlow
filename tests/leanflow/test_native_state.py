"""Tests for the extracted native_state mutable caches + native_runner re-export (Phase 2)."""

from collections import deque

from leanflow_cli.native import native_runner, native_state


def test_native_runner_reexports_are_identical():
    # Every re-exported name in native_runner must be the SAME object as in native_state, so the
    # bounded de-dup caches are shared (mutated in place), not accidentally duplicated.
    for name in native_state.__all__:
        assert getattr(native_runner, name) is getattr(native_state, name), name


def test_cache_once_dedupes_and_is_bounded():
    cache: set[str] = set()
    order: deque[str] = deque(maxlen=3)
    limit = 3

    # First sighting of a signature returns True (record it); a repeat returns False (skip).
    assert native_state._cache_once("a", cache=cache, order=order, limit=limit) is True
    assert native_state._cache_once("a", cache=cache, order=order, limit=limit) is False

    # Empty signatures are never cached and always report "already seen".
    assert native_state._cache_once("", cache=cache, order=order, limit=limit) is False
    assert "" not in cache

    # Filling past the limit evicts the oldest entry (FIFO) and keeps cache/order in lockstep.
    for sig in ("b", "c", "d"):
        assert native_state._cache_once(sig, cache=cache, order=order, limit=limit) is True
    assert len(cache) == limit
    assert len(order) == limit
    assert "a" not in cache  # oldest evicted
    assert cache == {"b", "c", "d"}
    assert list(order) == ["b", "c", "d"]
    # Evicted signature is treated as new again.
    assert native_state._cache_once("a", cache=cache, order=order, limit=limit) is True


def test_manager_verification_log_cache_round_trips_and_clears():
    cache = native_state._MANAGER_VERIFICATION_LOG_CACHE
    order = native_state._MANAGER_VERIFICATION_LOG_CACHE_ORDER
    cache.clear()
    order.clear()

    signature = ("file", "Main.lean", True, "lake env lean Main.lean succeeded")
    cache.add(signature)
    order.append(signature)
    assert signature in cache
    # The re-exported native_runner name observes the same mutation (shared object).
    assert signature in native_runner._MANAGER_VERIFICATION_LOG_CACHE
    assert list(native_runner._MANAGER_VERIFICATION_LOG_CACHE_ORDER) == [signature]

    cache.clear()
    order.clear()
    assert not native_runner._MANAGER_VERIFICATION_LOG_CACHE
    assert not native_runner._MANAGER_VERIFICATION_LOG_CACHE_ORDER
