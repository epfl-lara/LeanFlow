"""Tests for bounded managed-workflow JSONL ingestion."""

from __future__ import annotations

import json

from leanflow_cli.workflows import workflow_activity_reader as reader


def _event(event_type: str, *, iteration: int = 0) -> bytes:
    return (
        json.dumps(
            {
                "timestamp": "2026-07-15T00:00:00+00:00",
                "type": event_type,
                "run_id": "run-1",
                "details": {
                    "agent_session_id": "agent-1",
                    "iteration": iteration,
                },
            },
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def test_iter_jsonl_dicts_discards_oversized_legacy_records_before_decode(monkeypatch, tmp_path):
    path = tmp_path / "activity.jsonl"
    oversized = (
        json.dumps(
            {
                "type": "api-request",
                "details": {
                    "agent_session_id": "legacy-agent",
                    "iteration": 99,
                    "messages": [{"role": "tool", "content": "x" * 20_000}],
                },
            },
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    path.write_bytes(
        _event("conversation-start")
        + oversized
        + b"{malformed json}\n"
        + _event("conversation-end")
    )
    decoded_sizes: list[int] = []
    original_loads = reader.json.loads

    def bounded_loads(record):
        decoded_sizes.append(len(record))
        assert len(record) <= 1_024
        return original_loads(record)

    monkeypatch.setattr(reader.json, "loads", bounded_loads)

    events = list(reader.iter_jsonl_dicts([path], max_record_bytes=1_024))

    assert [event["type"] for event in events] == [
        "conversation-start",
        "conversation-end",
    ]
    assert len(decoded_sizes) == 3
    assert max(decoded_sizes) <= 1_024


def test_iter_jsonl_dicts_discards_oversized_unterminated_record(monkeypatch, tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_bytes(_event("runner-start") + (b"x" * 50_000))
    original_loads = reader.json.loads

    def bounded_loads(record):
        assert len(record) <= 512
        return original_loads(record)

    monkeypatch.setattr(reader.json, "loads", bounded_loads)

    events = list(reader.iter_jsonl_dicts([path], max_record_bytes=512))

    assert [event["type"] for event in events] == ["runner-start"]


def test_iter_jsonl_dicts_reverse_is_newest_first_across_paths(tmp_path):
    older = tmp_path / "older.jsonl"
    newer = tmp_path / "newer.jsonl"
    older.write_bytes(_event("old-1") + _event("old-2"))
    newer.write_bytes(_event("new-1") + _event("new-2").rstrip(b"\n"))

    events = list(reader.iter_jsonl_dicts_reverse([older, newer]))

    assert [event["type"] for event in events] == [
        "new-2",
        "new-1",
        "old-2",
        "old-1",
    ]


def test_iter_jsonl_dicts_reverse_skips_oversized_records_without_decoding(monkeypatch, tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_bytes(
        _event("old")
        + b'{"type":"oversized","content":"'
        + (b"x" * 50_000)
        + b'"}\n'
        + b"{malformed json}\n"
        + _event("new")
    )
    decoded_sizes: list[int] = []
    original_loads = reader.json.loads

    def bounded_loads(record):
        decoded_sizes.append(len(record))
        assert len(record) <= 512
        return original_loads(record)

    monkeypatch.setattr(reader.json, "loads", bounded_loads)

    events = list(reader.iter_jsonl_dicts_reverse([path], max_record_bytes=512))

    assert [event["type"] for event in events] == ["new", "old"]
    assert len(decoded_sizes) == 3
    assert max(decoded_sizes) <= 512
