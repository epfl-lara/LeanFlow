"""Tests for workflow_json_io — the crash-atomic, loud-on-corruption state I/O contract."""

import json

import pytest

from leanflow_cli.workflows.workflow_json_io import (
    WorkflowStateCorruptionError,
    read_json_file,
    update_json_file_if_changed,
    write_json_file,
)


class TestWriteJsonFile:
    def test_round_trip(self, tmp_path):
        target = tmp_path / "state.json"
        payload = {"b": 2, "a": {"nested": True}}
        write_json_file(target, payload)
        assert read_json_file(target) == payload

    def test_creates_parent_directories(self, tmp_path):
        target = tmp_path / "deep" / "dir" / "state.json"
        write_json_file(target, {"ok": True})
        assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}

    def test_sorts_keys_like_legacy_writer(self, tmp_path):
        target = tmp_path / "state.json"
        write_json_file(target, {"z": 1, "a": 2})
        text = target.read_text(encoding="utf-8")
        assert text.index('"a"') < text.index('"z"')

    def test_no_leftover_temp_files(self, tmp_path):
        target = tmp_path / "state.json"
        write_json_file(target, {"ok": True})
        assert not [f.name for f in tmp_path.iterdir() if ".tmp" in f.name]
        assert target.exists()

    def test_conditional_update_skips_noop_atomic_write(self, monkeypatch, tmp_path):
        from leanflow_cli.workflows import workflow_json_io

        target = tmp_path / "state.json"
        write_json_file(target, {"stable": True})
        before = target.read_bytes()
        before_mtime = target.stat().st_mtime_ns
        writes: list[dict] = []
        original_write = workflow_json_io.atomic_json_write

        def record_write(path, payload, *, sort_keys):
            writes.append(dict(payload))
            original_write(path, payload, sort_keys=sort_keys)

        monkeypatch.setattr(workflow_json_io, "atomic_json_write", record_write)

        outcome = update_json_file_if_changed(
            target,
            lambda payload: (payload.get("stable"), False),
        )

        assert outcome is True
        assert writes == []
        assert target.read_bytes() == before
        assert target.stat().st_mtime_ns == before_mtime

    def test_conditional_update_writes_reported_change(self, monkeypatch, tmp_path):
        from leanflow_cli.workflows import workflow_json_io

        target = tmp_path / "state.json"
        write_json_file(target, {"generation": 1})
        original_write = workflow_json_io.atomic_json_write
        writes: list[dict] = []

        def record_write(path, payload, *, sort_keys):
            writes.append(dict(payload))
            original_write(path, payload, sort_keys=sort_keys)

        def advance(payload):
            payload["generation"] = 2
            return "advanced", True

        monkeypatch.setattr(workflow_json_io, "atomic_json_write", record_write)

        assert update_json_file_if_changed(target, advance) == "advanced"
        assert writes == [{"generation": 2}]
        assert read_json_file(target) == {"generation": 2}


class TestReadJsonFile:
    def test_missing_file_returns_empty(self, tmp_path):
        assert read_json_file(tmp_path / "absent.json") == {}

    def test_empty_file_returns_empty(self, tmp_path):
        target = tmp_path / "empty.json"
        target.write_text("", encoding="utf-8")
        assert read_json_file(target) == {}

    def test_whitespace_only_file_returns_empty(self, tmp_path):
        target = tmp_path / "blank.json"
        target.write_text(" \n\t\n", encoding="utf-8")
        assert read_json_file(target) == {}

    def test_truncated_json_raises_corruption_error(self, tmp_path):
        target = tmp_path / "truncated.json"
        target.write_text('{"version": 1, "checkpo', encoding="utf-8")
        with pytest.raises(WorkflowStateCorruptionError) as excinfo:
            read_json_file(target)
        assert "truncated.json" in str(excinfo.value)

    def test_garbage_content_raises_corruption_error(self, tmp_path):
        target = tmp_path / "garbage.json"
        target.write_text("not json at all", encoding="utf-8")
        with pytest.raises(WorkflowStateCorruptionError):
            read_json_file(target)

    def test_non_utf8_content_raises_corruption_error(self, tmp_path):
        target = tmp_path / "binary.json"
        target.write_bytes(b'{"key": "\xff\xfe garbled"}')
        with pytest.raises(WorkflowStateCorruptionError):
            read_json_file(target)

    def test_non_object_payload_raises_corruption_error(self, tmp_path):
        target = tmp_path / "list.json"
        target.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(WorkflowStateCorruptionError) as excinfo:
            read_json_file(target)
        assert "expected a JSON object" in str(excinfo.value)

    def test_corruption_error_is_runtime_error(self):
        assert issubclass(WorkflowStateCorruptionError, RuntimeError)


class TestCheckpointReaderDelegation:
    """native_checkpoints reads go through the shared loud-on-corruption reader."""

    def test_corrupt_checkpoint_index_raises(self, tmp_path):
        from leanflow_cli.native import native_checkpoints

        target = tmp_path / "index.json"
        target.write_text('{"version": 1, "checkpo', encoding="utf-8")
        with pytest.raises(WorkflowStateCorruptionError):
            native_checkpoints._read_json_file(target)

    def test_missing_checkpoint_file_still_tolerated(self, tmp_path):
        from leanflow_cli.native import native_checkpoints

        assert native_checkpoints._read_json_file(tmp_path / "absent.json") == {}
