"""Tests for queue_manager_live — the single live manager per autonomy_state.

Phase 0 P0.3: `_queue_manager_from_state` used to rebuild the manager from the
legacy dict at every helper call; the live sidecar caches one instance per
dict, fingerprint-guarded so direct external mutation of the legacy keys
rebuilds instead of going stale. The flush stays byte-identical to the legacy
dict shape.
"""

from __future__ import annotations

from leanflow_cli.workflows import queue_manager_live as live
from leanflow_cli.workflows.queue_manager import QueueItem, TheoremQueueManager


def _seeded_state(tmp_path) -> dict:
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo", reasons=("contains sorry",)), active_file=str(active))
    mgr.consume_retry_once(kind="hard", signature="sig-1")
    state: dict = {"current_cycle": 3}
    live.flush_live_queue_manager(state, mgr)
    live.invalidate_live_queue_manager(state)
    return state


def test_get_or_create_returns_same_instance(tmp_path):
    state = _seeded_state(tmp_path)

    first = live.live_queue_manager(state)
    second = live.live_queue_manager(state)

    assert first is second
    assert first.hard_retries_for_current() == 1


def test_hydration_happens_once_at_steady_state(tmp_path):
    """P0.6 acceptance criterion 3: mutate->flush->read cycles reuse one instance."""
    state = _seeded_state(tmp_path)
    baseline = live.hydration_count()

    mgr = live.live_queue_manager(state)
    for index in range(5):
        mgr = live.live_queue_manager(state)
        mgr.consume_retry_once(kind="hard", signature=f"sig-extra-{index}")
        live.flush_live_queue_manager(state, mgr)
        assert live.live_queue_manager(state) is mgr

    assert live.hydration_count() - baseline == 1
    assert mgr.hard_retries_for_current() == 6


def test_external_mutation_triggers_fingerprint_rebuild(tmp_path):
    state = _seeded_state(tmp_path)
    cached = live.live_queue_manager(state)

    # Legacy code paths still pop owned keys directly (e.g. clearing the
    # assignment wholesale); the fingerprint guard must rebuild, not serve
    # the stale cached instance.
    state.pop("current_queue_assignment")
    rebuilt = live.live_queue_manager(state)

    assert rebuilt is not cached
    assert rebuilt.current is None


def test_flush_matches_legacy_serialization(tmp_path):
    state = _seeded_state(tmp_path)
    mgr = live.live_queue_manager(state)

    flushed: dict = {"unowned": "survives"}
    live.flush_live_queue_manager(flushed, mgr)

    expected = mgr.to_autonomy_state()
    assert flushed.pop("unowned") == "survives"
    assert flushed == expected
    # Round-trip stays lossless (mirrors test_queue_manager legacy round-trip).
    restored = TheoremQueueManager.from_autonomy_state(dict(expected))
    assert restored.to_autonomy_state() == expected


def test_flush_registers_instance_for_next_lookup(tmp_path):
    state = _seeded_state(tmp_path)
    mgr = TheoremQueueManager.from_autonomy_state(dict(state))

    live.flush_live_queue_manager(state, mgr)

    assert live.live_queue_manager(state) is mgr


def test_non_dict_state_gets_uncached_hydration():
    first = live.live_queue_manager(None)
    second = live.live_queue_manager(None)

    assert first is not second
    assert first.current is None


def test_invalidate_drops_cached_instance(tmp_path):
    state = _seeded_state(tmp_path)
    cached = live.live_queue_manager(state)

    live.invalidate_live_queue_manager(state)

    assert live.live_queue_manager(state) is not cached


def test_explicit_empty_queue_refresh_clears_cached_queue(tmp_path):
    """A live_state that says 'no items' must not leave a stale cached queue."""
    state = _seeded_state(tmp_path)
    live.live_queue_manager(
        state, {"declaration_queue": [{"label": "old", "reasons": ["contains sorry"]}]}
    )

    refreshed = live.live_queue_manager(state, {"declaration_queue": []})
    assert refreshed.queue == ()

    # A live_state without queue keys leaves the cached queue untouched...
    live.live_queue_manager(
        state, {"declaration_queue": [{"label": "kept", "reasons": ["contains sorry"]}]}
    )
    untouched = live.live_queue_manager(state, {"active_file": str(tmp_path / "Main.lean")})
    assert [item.label for item in untouched.queue] == ["kept"]
    # ...and so does live_state=None.
    assert [item.label for item in live.live_queue_manager(state).queue] == ["kept"]


def test_equal_content_dicts_get_distinct_managers(tmp_path):
    """Cache entries are identity-checked: equal fingerprints never share state."""
    state = _seeded_state(tmp_path)
    twin = dict(state)

    first = live.live_queue_manager(state)
    second = live.live_queue_manager(twin)

    assert first is not second
    second.consume_retry_once(kind="hard", signature="twin-only")
    assert first.hard_retries_for_current() == 1
    assert second.hard_retries_for_current() == 2


def test_live_state_refresh_applies_on_cache_hit(tmp_path):
    state = _seeded_state(tmp_path)
    live.live_queue_manager(state)

    refreshed = live.live_queue_manager(
        state,
        {
            "active_file": str(tmp_path / "Main.lean"),
            "declaration_queue": [{"label": "next", "reasons": ["contains sorry"]}],
        },
    )

    assert [item.label for item in refreshed.queue] == ["next"]
