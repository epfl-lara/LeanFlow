"""Tests for bounded, lossless workflow outcome retention."""

from __future__ import annotations

import gzip
import json

from leanflow_cli.workflows.workflow_outcome_retention import append_outcome_entry


def _append(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def test_large_outcome_payload_moves_to_compressed_sidecar(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_OUTCOME_RECORD_MAX_BYTES", "1024")
    path = tmp_path / "outcomes.jsonl"
    entry = {
        "kind": "lean-proof-context",
        "payload": {"output": "kernel-output-" * 500},
    }

    result = append_outcome_entry(path, entry, append=_append)

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["payload"]["externalized"] is True
    assert record["payload"]["uncompressed_bytes"] > 1024
    sidecar = tmp_path / record["payload"]["artifact"]
    assert result["sidecar"] == str(sidecar)
    with gzip.open(sidecar, "rt", encoding="utf-8") as handle:
        assert json.load(handle) == entry


def test_oversized_stream_rotates_to_compressed_archive(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_OUTCOME_STREAM_MAX_BYTES", "1024")
    monkeypatch.setenv("LEANFLOW_OUTCOME_RECORD_MAX_BYTES", "100000")
    path = tmp_path / "outcomes.jsonl"
    original = (json.dumps({"kind": "old", "payload": "x" * 200}) + "\n") * 8
    path.write_text(original, encoding="utf-8")
    entry = {"kind": "new", "payload": {"ok": True}}

    result = append_outcome_entry(path, entry, append=_append)

    assert json.loads(path.read_text(encoding="utf-8")) == entry
    archive = result["archive"]
    assert archive
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        assert handle.read() == original
