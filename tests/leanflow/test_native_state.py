"""Tests for the extracted native_state mutable caches."""

from collections import deque

from leanflow_cli.native import native_state


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
