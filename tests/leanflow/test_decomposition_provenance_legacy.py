"""Exercise fail-closed legacy decomposition ownership selection."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

import pytest

from leanflow_cli.workflows import decomposition_provenance
from leanflow_cli.workflows import workflow_activity_retention as retention

HELPER = "legacy_helper"


def _decomposer_event(
    source: Path,
    *,
    event_id: str,
    timestamp: str,
    parent: str,
) -> dict[str, object]:
    """Build one successful legacy placement event for ``source``."""
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "type": "decomposer",
        "details": {
            "ok": True,
            "placed": [HELPER],
            "target_symbol": parent,
            "active_file": str(source),
            "project_root": str(source.parent),
        },
    }


def _write_run(
    state_root: Path,
    run_id: str,
    events: Iterable[dict[str, object]],
    *,
    close: bool = False,
) -> Path:
    """Write one hot run stream, optionally with a terminal runner event."""
    path = state_root / "activity" / "runs" / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    records = list(events)
    if close:
        records.append(
            {
                "event_id": f"{run_id}-exit",
                "timestamp": "2026-01-31T00:00:00+00:00",
                "type": "runner-exit",
                "details": {"exit_code": 2},
            }
        )
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _archive_runs(state_root: Path, *run_ids: str) -> None:
    """Move the named closed test runs into checksum-cataloged cold storage."""
    result = retention.compact_closed_activity(
        state_root,
        current_run_id="current-run",
        reduce_event=lambda *_args: None,
        compact_event=lambda event: dict(event),
        identity_is_live=lambda _identity: False,
    )
    assert set(result.archived_runs) == set(run_ids)


def _select(state_root: Path, source: Path) -> tuple[dict[str, object] | None, str]:
    """Select legacy ownership for the shared helper fixture."""
    event, reason = decomposition_provenance._legacy_decomposer_event(
        state_root=state_root,
        helper_name=HELPER,
        file_identity=decomposition_provenance.canonical_file(str(source), source.parent),
    )
    return event, reason


def test_newer_cold_placement_beats_older_hot_placement(tmp_path: Path) -> None:
    """Selection compares timestamps across both complete evidence tiers."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    cold = _decomposer_event(
        source,
        event_id="newer-cold",
        timestamp="2026-01-02T00:00:00+00:00",
        parent="cold_parent",
    )
    hot = _decomposer_event(
        source,
        event_id="older-hot",
        timestamp="2026-01-01T00:00:00+00:00",
        parent="hot_parent",
    )
    _write_run(state_root, "cold-run", [cold], close=True)
    _archive_runs(state_root, "cold-run")
    _write_run(state_root, "hot-run", [hot])

    selected, reason = _select(state_root, source)

    assert reason == ""
    assert selected == {
        "event_id": "newer-cold",
        "timestamp": "2026-01-02T00:00:00+00:00",
        "parent": "cold_parent",
    }


def test_corrupt_newer_cold_run_rejects_older_valid_cold_evidence(tmp_path: Path) -> None:
    """An unreadable newer tier cannot silently expose an older valid owner."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    older = _decomposer_event(
        source,
        event_id="older-valid-cold",
        timestamp="2026-01-01T00:00:00+00:00",
        parent="old_parent",
    )
    newer = _decomposer_event(
        source,
        event_id="newer-corrupt-cold",
        timestamp="2026-01-02T00:00:00+00:00",
        parent="new_parent",
    )
    _write_run(state_root, "a-old", [older], close=True)
    _write_run(state_root, "z-new", [newer], close=True)
    _archive_runs(state_root, "a-old", "z-new")
    archive = state_root / "activity" / "archive" / "runs" / "z-new.jsonl.gz"
    archive.write_bytes(archive.read_bytes() + b"tamper")

    selected, reason = _select(state_root, source)

    assert selected is None
    assert "retained workflow activity evidence is incomplete" in reason
    assert "archive_checksum_mismatch=1" in reason


@pytest.mark.parametrize(
    ("damage", "reason_fragment"),
    [
        (b"{not-json}\n", "malformed hot workflow activity record"),
        (b"x" * 129 + b"\n", "oversized hot workflow activity record"),
    ],
)
def test_bad_hot_stream_rejects_older_valid_cold_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    damage: bytes,
    reason_fragment: str,
) -> None:
    """Malformed and oversized hot tails make legacy selection fail closed."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    older = _decomposer_event(
        source,
        event_id="older-valid-cold",
        timestamp="2026-01-01T00:00:00+00:00",
        parent="old_parent",
    )
    _write_run(state_root, "cold-run", [older], close=True)
    _archive_runs(state_root, "cold-run")
    bad_path = state_root / "activity" / "runs" / "new-hot.jsonl"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_bytes(damage)
    monkeypatch.setattr(decomposition_provenance, "_MAX_LEGACY_ACTIVITY_RECORD_BYTES", 128)

    selected, reason = _select(state_root, source)

    assert selected is None
    assert reason_fragment in reason


def test_same_timestamp_different_parents_is_ambiguous_across_tiers(tmp_path: Path) -> None:
    """Equal-time contradictory ownership never depends on stream ordering."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    timestamp = "2026-01-02T00:00:00+00:00"
    cold = _decomposer_event(
        source,
        event_id="same-time-cold",
        timestamp=timestamp,
        parent="cold_parent",
    )
    hot = _decomposer_event(
        source,
        event_id="same-time-hot",
        timestamp=timestamp,
        parent="hot_parent",
    )
    _write_run(state_root, "cold-run", [cold], close=True)
    _archive_runs(state_root, "cold-run")
    _write_run(state_root, "hot-run", [hot])

    selected, reason = _select(state_root, source)

    assert selected is None
    assert reason == "same-timestamp decomposer evidence names different parents"


def test_matching_event_collection_stops_at_the_safety_cap(tmp_path: Path) -> None:
    """A hostile run cannot grow the legacy match table beyond its fixed cap."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    events = [
        _decomposer_event(
            source,
            event_id=f"event-{index:04d}",
            timestamp=f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
            parent="same_parent",
        )
        for index in range(decomposition_provenance._MAX_LEGACY_DECOMPOSER_MATCHES + 1)
    ]
    _write_run(state_root, "many-hot", events)

    selected, reason = _select(state_root, source)

    assert selected is None
    assert reason == "too many matching decomposer events to resolve ownership safely"


def test_missing_catalog_is_incomplete_when_orphaned_archive_exists(tmp_path: Path) -> None:
    """Catalog loss cannot make an older hot owner authoritative."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    hot = _decomposer_event(
        source,
        event_id="older-hot",
        timestamp="2026-01-01T00:00:00+00:00",
        parent="hot_parent",
    )
    _write_run(state_root, "hot-run", [hot])
    orphan = state_root / "activity" / "archive" / "runs" / "orphan.jsonl.gz"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphaned retained evidence")

    selected, reason = _select(state_root, source)

    assert selected is None
    assert "retained workflow activity evidence is incomplete" in reason
    assert "missing" in reason


def test_hot_snapshot_change_during_cold_audit_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A late hot event cannot be omitted from global-newest selection."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    hot_path = _write_run(
        state_root,
        "hot-run",
        [
            _decomposer_event(
                source,
                event_id="older-hot",
                timestamp="2026-01-01T00:00:00+00:00",
                parent="old_parent",
            )
        ],
    )
    original_audit = retention.audit_retained_run_events

    def audit_then_append(*args, **kwargs):
        result = original_audit(*args, **kwargs)
        with hot_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _decomposer_event(
                        source,
                        event_id="late-hot",
                        timestamp="2026-01-02T00:00:00+00:00",
                        parent="late_parent",
                    ),
                    sort_keys=True,
                )
                + "\n"
            )
        return result

    monkeypatch.setattr(retention, "audit_retained_run_events", audit_then_append)

    selected, reason = _select(state_root, source)

    assert selected is None
    assert reason == "hot workflow activity changed during legacy ownership audit"


def test_hot_activity_file_enumeration_has_a_fixed_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Directory cardinality cannot force unbounded path materialization."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    event = _decomposer_event(
        source,
        event_id="event",
        timestamp="2026-01-01T00:00:00+00:00",
        parent="parent",
    )
    _write_run(state_root, "one", [event])
    _write_run(state_root, "two", [event])
    monkeypatch.setattr(decomposition_provenance, "_MAX_LEGACY_HOT_ACTIVITY_FILES", 1)

    selected, reason = _select(state_root, source)

    assert selected is None
    assert reason == "too many hot workflow activity files to audit safely"


def test_hot_activity_symlink_is_rejected(tmp_path: Path) -> None:
    """A hot run symlink cannot import ownership evidence from outside state."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    outside = tmp_path / "outside.jsonl"
    outside.write_text(
        json.dumps(
            _decomposer_event(
                source,
                event_id="outside",
                timestamp="2026-01-01T00:00:00+00:00",
                parent="outside_parent",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    hot = state_root / "activity" / "runs" / "linked.jsonl"
    hot.parent.mkdir(parents=True, exist_ok=True)
    hot.symlink_to(outside)

    selected, reason = _select(state_root, source)

    assert selected is None
    assert "unreadable hot workflow activity linked.jsonl" in reason


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO test requires POSIX")
def test_hot_activity_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    """A FIFO cannot hang the strict hot evidence reader."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    fifo = state_root / "activity" / "runs" / "blocked.jsonl"
    fifo.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)

    selected, reason = _select(state_root, source)

    assert selected is None
    assert "unreadable hot workflow activity blocked.jsonl" in reason


def test_newest_placement_uses_utc_instant_not_timestamp_text(tmp_path: Path) -> None:
    """Different offsets are ordered by time rather than lexicographically."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    lexically_newer_but_earlier = _decomposer_event(
        source,
        event_id="earlier-instant",
        timestamp="2026-01-02T00:00:00+14:00",
        parent="earlier_parent",
    )
    lexically_older_but_later = _decomposer_event(
        source,
        event_id="later-instant",
        timestamp="2026-01-01T23:00:00-12:00",
        parent="later_parent",
    )
    _write_run(
        state_root,
        "offsets",
        [lexically_newer_but_earlier, lexically_older_but_later],
    )

    selected, reason = _select(state_root, source)

    assert reason == ""
    assert selected is not None
    assert selected["event_id"] == "later-instant"
    assert selected["parent"] == "later_parent"


def test_same_instant_different_parents_is_ambiguous(tmp_path: Path) -> None:
    """Offset spellings of one instant retain the parent-conflict gate."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    _write_run(
        state_root,
        "same-instant",
        [
            _decomposer_event(
                source,
                event_id="utc",
                timestamp="2026-01-02T00:00:00+00:00",
                parent="utc_parent",
            ),
            _decomposer_event(
                source,
                event_id="offset",
                timestamp="2026-01-02T01:00:00+01:00",
                parent="offset_parent",
            ),
        ],
    )

    selected, reason = _select(state_root, source)

    assert selected is None
    assert reason == "same-timestamp decomposer evidence names different parents"


def test_matching_decomposer_event_requires_timezone_aware_timestamp(tmp_path: Path) -> None:
    """Missing timezone semantics cannot influence ownership ordering."""
    state_root = tmp_path / "state"
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : True := by trivial\n", encoding="utf-8")
    _write_run(
        state_root,
        "invalid-time",
        [
            _decomposer_event(
                source,
                event_id="invalid",
                timestamp="2026-01-02T00:00:00",
                parent="invalid_parent",
            )
        ],
    )

    selected, reason = _select(state_root, source)

    assert selected is None
    assert reason == "matching decomposer evidence has an invalid timestamp"
