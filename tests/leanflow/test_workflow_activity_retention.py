"""Characterize crash-safe bounded retention for managed activity streams."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import workflow_activity_retention as retention
from leanflow_cli.workflows import workflow_state
from leanflow_cli.workflows.workflow_activity_reader import iter_jsonl_dicts
from leanflow_cli.workflows.workflow_state import (
    append_workflow_activity,
    compact_closed_workflow_activity,
    read_workflow_activity,
    summarize_workflow_agents,
    workflow_agent_activity_path,
    workflow_run_activity_path,
)
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root


class SimulatedRetentionCrash(RuntimeError):
    """Mark a deterministic transaction-boundary failure in tests."""


def _configure_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LEANFLOW_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "lean-proof-loop")


def _identity_live_only(*live_pids: int):
    selected = set(live_pids)

    def is_live(payload, *, require_verified=False):
        del require_verified
        return int(payload.get("process_id", 0) or 0) in selected

    return is_live


def _append_closed_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str = "closed-run",
    agent_id: str = "closed-agent",
    process_id: int = 41_001,
) -> None:
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", run_id)
    common = {
        "agent_session_id": agent_id,
        "parent_agent_session_id": "manager-agent",
        "process_id": process_id,
        "run_scope": "background-session",
    }
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        user_message="Prove the archived theorem",
        **common,
    )
    append_workflow_activity(
        "api-request",
        "API call #3",
        iteration=3,
        message_count=7,
        approx_tokens=1_234,
        **common,
    )
    append_workflow_activity(
        "tool-call",
        "Calling lean_check",
        tool="lean_check",
        arguments={"path": "Main.lean"},
        **common,
    )
    append_workflow_activity(
        "conversation-end",
        "Agent conversation finished",
        completed=True,
        api_calls=3,
        **common,
    )
    append_workflow_activity(
        "runner-exit",
        "Managed workflow runner exited",
        exit_code=0,
        **common,
    )


def _catalog() -> dict[str, object]:
    return json.loads(
        retention.activity_retention_catalog_path(workflow_state_root()).read_text(encoding="utf-8")
    )


def _status_shard(entry: dict[str, object], prefix: str) -> Path:
    """Return one cataloged status shard in the configured test state."""
    return workflow_state_root() / str(entry[f"{prefix}_path"])


def _compact_fixture_run(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, object], Path]:
    """Compact the standard closed run and return its entry and archive."""
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(),
    )
    compact_closed_workflow_activity()
    entry = _catalog()["runs"]["closed-run"]
    return entry, workflow_state_root() / str(entry["archive_path"])


def _replace_retained_payload(
    entry: dict[str, object],
    payload: bytes,
    *,
    valid_events: int,
    skipped_records: int,
) -> Path:
    """Replace one test archive and synchronize its cataloged identities."""
    archive_path = workflow_state_root() / str(entry["archive_path"])
    archive_bytes = gzip.compress(payload, compresslevel=6, mtime=0)
    archive_path.write_bytes(archive_bytes)
    catalog = _catalog()
    mutable_entry = catalog["runs"]["closed-run"]
    mutable_entry.update(
        {
            "source_sha256": hashlib.sha256(payload).hexdigest(),
            "source_bytes": len(payload),
            "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
            "archive_bytes": len(archive_bytes),
            "valid_events": valid_events,
            "skipped_records": skipped_records,
        }
    )
    retention.activity_retention_catalog_path(workflow_state_root()).write_text(
        json.dumps(catalog),
        encoding="utf-8",
    )
    return archive_path


def test_migrates_closed_run_and_preserves_hot_status_without_archive_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    closed_run_path = workflow_run_activity_path("closed-run")
    closed_agent_path = workflow_agent_activity_path("closed-agent", "prove")
    closed_run_bytes = closed_run_path.read_bytes()
    closed_agent_bytes = closed_agent_path.read_bytes()

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    append_workflow_activity(
        "conversation-start",
        "Current agent conversation started",
        agent_session_id="current-agent",
        process_id=os.getpid(),
        run_scope="top-level",
        user_message="Continue the live theorem",
    )
    current_run_path = workflow_run_activity_path("current-run")
    current_run_bytes = current_run_path.read_bytes()
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(os.getpid()),
    )

    before = {item["agent_id"]: item for item in summarize_workflow_agents(activity_limit=5)}
    result = compact_closed_workflow_activity()

    assert result.archived_runs == ("closed-run",)
    assert result.archived_agent_streams == ("prove-closed-agent.jsonl",)
    assert not closed_run_path.exists()
    assert not closed_agent_path.exists()
    assert current_run_path.read_bytes() == current_run_bytes

    run_archive = workflow_state_root() / "activity/archive/runs/closed-run.jsonl.gz"
    agent_archive = workflow_state_root() / "activity/archive/agents/prove-closed-agent.jsonl.gz"
    with gzip.open(run_archive, "rb") as handle:
        assert handle.read() == closed_run_bytes
    with gzip.open(agent_archive, "rb") as handle:
        assert handle.read() == closed_agent_bytes

    catalog = _catalog()
    run_entry = catalog["runs"]["closed-run"]
    assert run_entry["source_bytes"] == len(closed_run_bytes)
    assert run_entry["valid_events"] == 5
    assert run_entry["source_sha256"] == hashlib.sha256(closed_run_bytes).hexdigest()
    assert run_entry["archive_sha256"] == hashlib.sha256(run_archive.read_bytes()).hexdigest()
    assert catalog["layout"] == retention.RETENTION_LAYOUT
    assert not {
        "agent_summaries",
        "recent_agent_events",
        "recent_events",
    }.intersection(run_entry)
    for prefix in ("summary", "recent"):
        shard_path = _status_shard(run_entry, prefix)
        assert shard_path.is_file()
        assert run_entry[f"{prefix}_bytes"] == shard_path.stat().st_size
        assert run_entry[f"{prefix}_sha256"] == hashlib.sha256(shard_path.read_bytes()).hexdigest()

    original_open = Path.open

    def reject_cold_open(path: Path, *args, **kwargs):
        if path.suffix == ".gz":
            raise AssertionError(f"status opened cold evidence: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_cold_open)
    after = {item["agent_id"]: item for item in summarize_workflow_agents(activity_limit=5)}
    recent = read_workflow_activity(limit=8, agent_id="closed-agent")

    for key in (
        "parent_agent_id",
        "task_label",
        "workflow_kind",
        "delegate_depth",
        "status",
        "api_calls",
        "tool_calls",
        "started_at",
        "finished_at",
        "last_event_type",
    ):
        assert after["closed-agent"][key] == before["closed-agent"][key]
    assert after["closed-agent"]["parent_agent_id"] == "manager-agent"
    assert after["closed-agent"]["status"] == "exited"
    assert after["closed-agent"]["api_calls"] == 3
    assert after["closed-agent"]["tool_calls"] == 1
    assert [event["type"] for event in recent][-2:] == ["conversation-end", "runner-exit"]
    assert "archived theorem" in recent[0]["details"]["archived_preview"]


def test_runner_exit_with_live_child_keeps_entire_stream_hot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "closed-parent-live-child")
    append_workflow_activity(
        "conversation-start",
        "Child started",
        agent_session_id="live-child",
        parent_agent_session_id="parent",
        process_id=52_002,
    )
    append_workflow_activity(
        "runner-exit",
        "Parent exited",
        agent_session_id="parent",
        process_id=52_001,
    )
    source_path = workflow_run_activity_path("closed-parent-live-child")
    source_bytes = source_path.read_bytes()
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(52_002),
    )

    result = compact_closed_workflow_activity()

    assert result.archived_runs == ()
    assert result.skipped_live_runs == ("closed-parent-live-child",)
    assert source_path.read_bytes() == source_bytes
    assert not (
        workflow_state_root() / "activity/archive/runs/closed-parent-live-child.jsonl.gz"
    ).exists()


def test_identityless_canonical_run_archives_after_real_writer_pid_dies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    writer = subprocess.Popen([sys.executable, "-c", "pass"])
    writer.wait(timeout=10)
    assert writer.returncode == 0
    assert not workflow_state._process_seems_alive(writer.pid)
    run_id = f"agent-20260102T030405Z-pid{writer.pid}"
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", run_id)
    append_workflow_activity(
        "dispatch-job",
        "Recorded standalone telemetry without an embedded identity",
    )
    source_path = workflow_run_activity_path(run_id)
    source_bytes = source_path.read_bytes()
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")

    result = compact_closed_workflow_activity()

    assert result.archived_runs == (run_id,)
    assert result.skipped_live_runs == ()
    assert result.skipped_unproven_runs == ()
    assert not source_path.exists()
    archive_path = workflow_state_root() / f"activity/archive/runs/{run_id}.jsonl.gz"
    with gzip.open(archive_path, "rb") as handle:
        assert handle.read() == source_bytes


def test_identityless_canonical_run_with_real_live_pid_stays_hot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    run_id = f"agent-20260102T030405Z-pid{os.getpid()}"
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", run_id)
    append_workflow_activity(
        "dispatch-job",
        "Recorded standalone telemetry without an embedded identity",
    )
    source_path = workflow_run_activity_path(run_id)
    source_bytes = source_path.read_bytes()
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")

    result = compact_closed_workflow_activity()

    assert result.archived_runs == ()
    assert result.skipped_live_runs == (run_id,)
    assert result.skipped_unproven_runs == ()
    assert source_path.read_bytes() == source_bytes


@pytest.mark.parametrize(
    "run_id",
    (
        "standalone-telemetry-without-pid",
        "agent-20261302T030405Z-pid12345",
        "agent-20260102T030405Z-pid99999999999999999999",
    ),
)
def test_identityless_noncanonical_run_fails_closed_as_unproven(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    run_id: str,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", run_id)
    append_workflow_activity(
        "dispatch-job",
        "Recorded telemetry under a user-defined run name",
    )
    source_path = workflow_run_activity_path(run_id)
    source_bytes = source_path.read_bytes()
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")

    result = compact_closed_workflow_activity()

    assert result.archived_runs == ()
    assert result.skipped_live_runs == ()
    assert result.skipped_unproven_runs == (run_id,)
    assert source_path.read_bytes() == source_bytes


def test_archive_before_catalog_crash_reuses_verified_orphan_idempotently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    source_path = workflow_run_activity_path("closed-run")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(),
    )
    raised = False

    def crash_once(stage: str, _path: Path) -> None:
        nonlocal raised
        if stage == "run-archive-committed" and not raised:
            raised = True
            raise SimulatedRetentionCrash(stage)

    monkeypatch.setattr(retention, "_retention_fault", crash_once)
    with pytest.raises(SimulatedRetentionCrash):
        compact_closed_workflow_activity()

    archive_path = workflow_state_root() / "activity/archive/runs/closed-run.jsonl.gz"
    assert archive_path.is_file()
    assert source_path.is_file()
    orphan_inode = archive_path.stat().st_ino
    monkeypatch.setattr(retention, "_retention_fault", lambda _stage, _path: None)

    result = compact_closed_workflow_activity()
    catalog_bytes = retention.activity_retention_catalog_path(workflow_state_root()).read_bytes()
    second = compact_closed_workflow_activity()

    assert result.archived_runs == ("closed-run",)
    assert archive_path.stat().st_ino == orphan_inode
    assert not source_path.exists()
    assert len(_catalog()["runs"]) == 1
    assert summarize_workflow_agents(activity_limit=2)[0]["tool_calls"] == 1
    assert second.archived_runs == ()
    assert second.archived_agent_streams == ()
    assert (
        retention.activity_retention_catalog_path(workflow_state_root()).read_bytes()
        == catalog_bytes
    )


def test_catalog_crash_then_append_replaces_summary_on_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    source_path = workflow_run_activity_path("closed-run")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(),
    )
    raised = False

    def crash_once(stage: str, _path: Path) -> None:
        nonlocal raised
        if stage == "run-catalog-committed" and not raised:
            raised = True
            raise SimulatedRetentionCrash(stage)

    monkeypatch.setattr(retention, "_retention_fault", crash_once)
    with pytest.raises(SimulatedRetentionCrash):
        compact_closed_workflow_activity()
    first_entry = _catalog()["runs"]["closed-run"]
    assert source_path.is_file()

    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "closed-run")
    append_workflow_activity(
        "tool-call",
        "Late durable tool call",
        agent_session_id="closed-agent",
        parent_agent_session_id="manager-agent",
        process_id=41_001,
        run_scope="background-session",
        tool="lean_check",
        arguments={"path": "Late.lean"},
    )
    updated_source_bytes = source_path.read_bytes()
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    # A changed source is authoritative during the crash window: status must
    # not merge the stale catalog summary with the complete hot JSONL.
    assert summarize_workflow_agents(activity_limit=4)[0]["tool_calls"] == 2
    monkeypatch.setattr(retention, "_retention_fault", lambda _stage, _path: None)

    compact_closed_workflow_activity()

    replacement = _catalog()["runs"]["closed-run"]
    assert replacement["source_sha256"] != first_entry["source_sha256"]
    assert replacement["source_bytes"] == len(updated_source_bytes)
    summary_records = list(
        iter_jsonl_dicts(
            [_status_shard(replacement, "summary")],
            max_record_bytes=retention.MAX_AGENT_SUMMARY_BYTES,
        )
    )
    assert [item["agent_id"] for item in summary_records] == ["closed-agent"]
    assert summary_records[0]["tool_calls"] == 2
    summary = summarize_workflow_agents(activity_limit=4)[0]
    assert summary["agent_id"] == "closed-agent"
    assert summary["tool_calls"] == 2
    archive_path = workflow_state_root() / replacement["archive_path"]
    with gzip.open(archive_path, "rb") as handle:
        assert handle.read() == updated_source_bytes


def test_status_streams_large_shards_with_a_small_evidence_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "many-agents-closed")
    for agent_number in range(32):
        agent_id = f"archived-agent-{agent_number:02d}"
        common = {
            "agent_session_id": agent_id,
            "parent_agent_session_id": "manager-agent",
            "process_id": 80_000 + agent_number,
            "run_scope": "background-session",
        }
        append_workflow_activity(
            "conversation-start",
            f"Started {agent_id}",
            user_message=f"Prove theorem {agent_number}",
            **common,
        )
        for call_number in range(66):
            append_workflow_activity(
                "tool-call",
                f"Tool call {call_number}: " + "x" * 1_200,
                tool="lean_check",
                arguments={"path": f"Agent{agent_number}.lean"},
                **common,
            )
        append_workflow_activity(
            "conversation-end",
            f"Finished {agent_id}",
            completed=True,
            api_calls=4,
            **common,
        )
        append_workflow_activity(
            "runner-exit",
            f"Exited {agent_id}",
            exit_code=0,
            **common,
        )

    before = summarize_workflow_agents(activity_limit=3)
    before_digest = hashlib.sha256(
        json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(),
    )

    compact_closed_workflow_activity()

    catalog = _catalog()
    entry = catalog["runs"]["many-agents-closed"]
    catalog_path = retention.activity_retention_catalog_path(workflow_state_root())
    summary_path = _status_shard(entry, "summary")
    assert summary_path.stat().st_size > catalog_path.stat().st_size * 4
    assert entry["summary_agents"] == 32
    assert not {
        "agent_summaries",
        "recent_agent_events",
        "recent_events",
    }.intersection(entry)

    original_read_json = retention.read_json_file
    original_open = Path.open

    def reject_status_materialization(path: Path):
        if "historical-runs" in path.parts:
            raise AssertionError(f"materialized status shard: {path}")
        return original_read_json(path)

    def reject_cold_open(path: Path, *args, **kwargs):
        if path.suffix == ".gz":
            raise AssertionError(f"status opened cold evidence: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(retention, "read_json_file", reject_status_materialization)
    monkeypatch.setattr(Path, "open", reject_cold_open)

    after = summarize_workflow_agents(activity_limit=3)
    after_digest = hashlib.sha256(
        json.dumps(after, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert after_digest == before_digest
    assert len(after) == 32
    assert {item["tool_calls"] for item in after} == {66}
    assert len(read_workflow_activity(limit=5)) == 5
    agent_recent = read_workflow_activity(limit=5, agent_id="archived-agent-00")
    assert [event["type"] for event in agent_recent][-2:] == [
        "conversation-end",
        "runner-exit",
    ]


def test_compaction_upgrades_legacy_monolithic_catalog_to_streaming_shards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(),
    )
    compact_closed_workflow_activity()

    catalog = _catalog()
    entry = catalog["runs"]["closed-run"]
    summaries = list(
        iter_jsonl_dicts(
            [_status_shard(entry, "summary")],
            max_record_bytes=retention.MAX_AGENT_SUMMARY_BYTES,
        )
    )
    recent = list(iter_jsonl_dicts([_status_shard(entry, "recent")]))
    for prefix in ("summary", "recent"):
        _status_shard(entry, prefix).unlink()
        for key in ("path", "sha256", "bytes"):
            entry.pop(f"{prefix}_{key}", None)
    entry.pop("summary_agents", None)
    entry.pop("recent_event_count", None)
    entry.pop("recent_events", None)
    entry["agent_summaries"] = {item["agent_id"]: item for item in summaries}
    entry["recent_events"] = recent
    catalog.pop("layout", None)
    retention.write_json_file(
        retention.activity_retention_catalog_path(workflow_state_root()),
        catalog,
    )
    legacy = retention.load_retained_agent_summaries(workflow_state_root(), activity_limit=5)

    compact_closed_workflow_activity()

    upgraded = _catalog()
    upgraded_entry = upgraded["runs"]["closed-run"]
    assert upgraded["layout"] == retention.RETENTION_LAYOUT
    assert not {"agent_summaries", "recent_events"}.intersection(upgraded_entry)
    assert _status_shard(upgraded_entry, "summary").is_file()
    assert _status_shard(upgraded_entry, "recent").is_file()
    assert (
        retention.load_retained_agent_summaries(workflow_state_root(), activity_limit=5) == legacy
    )


def test_corrupt_status_shard_fails_open_without_reading_gzip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(),
    )
    compact_closed_workflow_activity()
    entry = _catalog()["runs"]["closed-run"]
    summary_path = _status_shard(entry, "summary")
    summary_path.write_bytes(summary_path.read_bytes() + b"corrupt\n")
    original_open = Path.open

    def reject_cold_open(path: Path, *args, **kwargs):
        if path.suffix == ".gz":
            raise AssertionError(f"status opened cold evidence: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_cold_open)
    caplog.set_level(logging.WARNING, logger=retention.__name__)

    assert retention.load_retained_agent_summaries(workflow_state_root(), activity_limit=5) == {}
    assert "Ignoring corrupt workflow activity status shard" in caplog.text


def test_status_parses_the_same_inode_it_checksum_verified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(),
    )
    compact_closed_workflow_activity()
    entry = _catalog()["runs"]["closed-run"]
    summary_path = _status_shard(entry, "summary")
    replacement_path = summary_path.with_suffix(".replacement")
    replacement_path.write_text(
        json.dumps({"agent_id": "unverified-replacement", "tool_calls": 999}) + "\n",
        encoding="utf-8",
    )
    replaced = False

    def replace_after_verification(stage: str, path: Path) -> None:
        nonlocal replaced
        if stage == "verified" and path == summary_path and not replaced:
            replaced = True
            os.replace(replacement_path, summary_path)

    monkeypatch.setattr(retention, "_status_shard_fault", replace_after_verification)

    summaries = retention.load_retained_agent_summaries(
        workflow_state_root(),
        activity_limit=5,
    )

    assert replaced
    assert list(summaries) == ["closed-agent"]
    assert summaries["closed-agent"]["tool_calls"] == 1
    assert "unverified-replacement" not in summaries


def test_strict_retained_run_audit_reports_complete_selected_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    _compact_fixture_run(monkeypatch)
    selected: list[dict[str, object]] = []

    result = retention.audit_retained_run_events(
        workflow_state_root(),
        event_types={"tool-call"},
        on_event=selected.append,
    )

    assert result.complete
    assert result.catalog_status == "verified"
    assert result.catalog_runs == result.verified_runs == 1
    assert result.verified_events == 5
    assert result.matched_events == 1
    assert result.issue_counts == ()
    assert [event["type"] for event in selected] == ["tool-call"]


def test_strict_retained_run_audit_distinguishes_missing_and_malformed_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)

    missing = retention.audit_retained_run_events(workflow_state_root())

    assert not missing.complete
    assert missing.catalog_status == "missing"
    assert missing.issue_count("catalog_missing") == 1

    catalog_path = retention.activity_retention_catalog_path(workflow_state_root())
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text("{not-json", encoding="utf-8")

    malformed = retention.audit_retained_run_events(workflow_state_root())

    assert not malformed.complete
    assert malformed.catalog_status == "malformed"
    assert malformed.issue_count("catalog_malformed") == 1


def test_strict_retained_run_audit_reports_oversized_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    catalog_path = retention.activity_retention_catalog_path(workflow_state_root())
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps({"version": 1, "runs": {}, "agent_streams": {}}))
    monkeypatch.setattr(retention, "MAX_AUDIT_CATALOG_BYTES", 8)

    result = retention.audit_retained_run_events(workflow_state_root())

    assert not result.complete
    assert result.catalog_status == "oversized"
    assert result.issue_count("catalog_oversized") == 1


@pytest.mark.parametrize(
    ("damage", "issue_code"),
    [
        ("missing", "archive_missing"),
        ("corrupt", "archive_checksum_mismatch"),
        ("non-authoritative", "run_non_authoritative"),
    ],
)
def test_strict_retained_run_audit_classifies_unusable_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    damage: str,
    issue_code: str,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    _entry, archive_path = _compact_fixture_run(monkeypatch)
    if damage == "missing":
        archive_path.unlink()
    elif damage == "corrupt":
        archive_path.write_bytes(archive_path.read_bytes() + b"tamper")
    else:
        workflow_run_activity_path("closed-run").write_text("changed\n", encoding="utf-8")
    selected: list[dict[str, object]] = []

    result = retention.audit_retained_run_events(
        workflow_state_root(),
        event_types={"tool-call"},
        on_event=selected.append,
    )

    assert not result.complete
    assert result.catalog_status == "verified"
    assert result.verified_runs == 0
    assert result.issue_count(issue_code) == 1
    assert selected == []


def test_strict_retained_run_audit_rejects_archive_symlink_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    _entry, archive_path = _compact_fixture_run(monkeypatch)
    outside = tmp_path / "outside.jsonl.gz"
    outside.write_bytes(archive_path.read_bytes())
    archive_path.unlink()
    archive_path.symlink_to(outside)

    result = retention.audit_retained_run_events(workflow_state_root())

    assert not result.complete
    assert result.issue_count("archive_path_malformed") == 1
    assert result.verified_runs == 0


@pytest.mark.parametrize(
    ("bad_tail", "valid_events", "issue_code"),
    [
        (b"{not-json}\n", 1, "record_malformed"),
        (b"x" * (retention.MAX_AUDIT_RECORD_BYTES + 1) + b"\n", 1, "record_oversized"),
    ],
)
def test_strict_retained_run_audit_rejects_entire_archive_with_bad_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bad_tail: bytes,
    valid_events: int,
    issue_code: str,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    entry, _archive_path = _compact_fixture_run(monkeypatch)
    selected_event = {"type": "tool-call", "details": {"tool": "verified-old-tool"}}
    payload = (json.dumps(selected_event) + "\n").encode() + bad_tail
    _replace_retained_payload(
        entry,
        payload,
        valid_events=valid_events,
        skipped_records=1,
    )
    selected: list[dict[str, object]] = []

    result = retention.audit_retained_run_events(
        workflow_state_root(),
        event_types={"tool-call"},
        on_event=selected.append,
    )

    assert not result.complete
    assert result.issue_count(issue_code) == 1
    assert result.issue_count("record_count_mismatch") == 0
    assert result.verified_runs == 0
    assert result.matched_events == 0
    assert selected == []


def test_strict_retained_run_audit_parses_valid_records_larger_than_status_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Large API metadata cannot hide a later legacy provenance event."""
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    entry, _archive_path = _compact_fixture_run(monkeypatch)
    large = {
        "type": "api-request",
        "details": {"effective_prompt": "x" * retention.MAX_SUMMARY_RECORD_BYTES},
    }
    selected_event = {"type": "decomposer", "details": {"ok": True, "placed": ["helper"]}}
    payload = (json.dumps(large) + "\n").encode() + (json.dumps(selected_event) + "\n").encode()
    _replace_retained_payload(
        entry,
        payload,
        valid_events=1,
        skipped_records=1,
    )
    selected: list[dict[str, object]] = []

    result = retention.audit_retained_run_events(
        workflow_state_root(),
        event_types={"decomposer"},
        on_event=selected.append,
    )

    assert result.complete
    assert result.verified_events == 2
    assert result.matched_events == 1
    assert selected == [selected_event]


def test_strict_retained_run_audit_bounds_issue_samples(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    entry, _archive_path = _compact_fixture_run(monkeypatch)
    malformed_count = retention.MAX_AUDIT_ISSUE_SAMPLES + 9
    payload = b"{bad}\n" * malformed_count
    _replace_retained_payload(
        entry,
        payload,
        valid_events=0,
        skipped_records=malformed_count,
    )

    result = retention.audit_retained_run_events(workflow_state_root())

    assert not result.complete
    assert result.issue_count("record_malformed") == malformed_count
    assert len(result.issue_samples) == retention.MAX_AUDIT_ISSUE_SAMPLES


def test_strict_retained_run_audit_replays_the_same_inode_it_verified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    _entry, archive_path = _compact_fixture_run(monkeypatch)
    replacement_path = archive_path.with_suffix(".replacement")
    replacement_path.write_bytes(
        gzip.compress(
            (json.dumps({"type": "tool-call", "details": {"tool": "unverified"}}) + "\n").encode(),
            mtime=0,
        )
    )
    replaced = False

    def replace_after_verification(stage: str, path: Path) -> None:
        nonlocal replaced
        if stage == "archive-verified" and path == archive_path and not replaced:
            replaced = True
            os.replace(replacement_path, archive_path)

    monkeypatch.setattr(retention, "_retained_audit_fault", replace_after_verification)
    selected: list[dict[str, object]] = []

    result = retention.audit_retained_run_events(
        workflow_state_root(),
        event_types={"tool-call"},
        on_event=selected.append,
    )

    assert replaced
    assert result.complete
    assert result.matched_events == 1
    assert selected[0]["details"]["tool"] == "lean_check"


def test_strict_retained_run_audit_revalidates_hot_authority_at_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hot source created after the first check invalidates cold callbacks."""
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    _entry, archive_path = _compact_fixture_run(monkeypatch)
    hot_source = workflow_run_activity_path("closed-run")
    created = False

    def create_new_hot_source(stage: str, path: Path) -> None:
        nonlocal created
        if stage == "archive-verified" and path == archive_path and not created:
            created = True
            hot_source.write_text(
                json.dumps({"type": "decomposer", "timestamp": "2026-01-04T00:00:00+00:00"}) + "\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(retention, "_retained_audit_fault", create_new_hot_source)
    selected: list[dict[str, object]] = []

    result = retention.audit_retained_run_events(
        workflow_state_root(),
        event_types={"tool-call"},
        on_event=selected.append,
    )

    assert created
    assert not result.complete
    assert result.issue_count("run_non_authoritative") == 1
    assert selected  # Per-archive callbacks remain explicitly provisional.


def test_strict_retained_run_audit_revalidates_catalog_generation_at_end(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An atomic catalog replacement makes all replayed callbacks provisional."""
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    _entry, archive_path = _compact_fixture_run(monkeypatch)
    catalog_path = retention.activity_retention_catalog_path(workflow_state_root())
    replacement = catalog_path.with_suffix(".replacement")
    replacement.write_bytes(catalog_path.read_bytes())
    replaced = False

    def replace_catalog(stage: str, path: Path) -> None:
        nonlocal replaced
        if stage == "archive-verified" and path == archive_path and not replaced:
            replaced = True
            os.replace(replacement, catalog_path)

    monkeypatch.setattr(retention, "_retained_audit_fault", replace_catalog)

    result = retention.audit_retained_run_events(workflow_state_root())

    assert replaced
    assert not result.complete
    assert result.issue_count("catalog_changed") == 1


def test_strict_retained_run_audit_rejects_symlinked_archive_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resolving the archive root cannot escape the workflow state tree."""
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    _entry, archive_path = _compact_fixture_run(monkeypatch)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    (outside / archive_path.name).write_bytes(archive_path.read_bytes())
    archive_path.unlink()
    archive_path.parent.rmdir()
    archive_path.parent.symlink_to(outside, target_is_directory=True)

    result = retention.audit_retained_run_events(workflow_state_root())

    assert not result.complete
    assert result.issue_count("archive_path_malformed") == 1
    assert result.verified_runs == 0


def test_strict_retained_run_audit_rejects_catalog_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The strict reader never follows a catalog symlink outside activity state."""
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    _entry, _archive_path = _compact_fixture_run(monkeypatch)
    catalog_path = retention.activity_retention_catalog_path(workflow_state_root())
    outside = tmp_path / "outside-catalog.json"
    outside.write_bytes(catalog_path.read_bytes())
    catalog_path.unlink()
    catalog_path.symlink_to(outside)

    result = retention.audit_retained_run_events(workflow_state_root())

    assert not result.complete
    assert result.catalog_status == "unreadable"
    assert result.issue_count("catalog_unreadable") == 1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO test requires POSIX")
def test_strict_retained_run_audit_rejects_catalog_fifo_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A nonblocking secure catalog open rejects a FIFO immediately."""
    _configure_state(monkeypatch, tmp_path)
    catalog_path = retention.activity_retention_catalog_path(workflow_state_root())
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(catalog_path)

    result = retention.audit_retained_run_events(workflow_state_root())

    assert not result.complete
    assert result.catalog_status == "unreadable"
    assert result.issue_count("catalog_unreadable") == 1


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO test requires POSIX")
def test_strict_retained_run_audit_rejects_archive_fifo_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A nonblocking secure open rejects a FIFO at the cataloged archive name."""
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    _entry, archive_path = _compact_fixture_run(monkeypatch)
    archive_path.unlink()
    os.mkfifo(archive_path)

    result = retention.audit_retained_run_events(workflow_state_root())

    assert not result.complete
    assert result.issue_count("archive_unreadable") == 1
    assert result.verified_runs == 0


def test_catalog_crash_without_append_finishes_unlink_without_rescan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    _append_closed_run(monkeypatch)
    source_path = workflow_run_activity_path("closed-run")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(),
    )
    raised = False

    def crash_once(stage: str, _path: Path) -> None:
        nonlocal raised
        if stage == "run-catalog-committed" and not raised:
            raised = True
            raise SimulatedRetentionCrash(stage)

    monkeypatch.setattr(retention, "_retention_fault", crash_once)
    with pytest.raises(SimulatedRetentionCrash):
        compact_closed_workflow_activity()

    assert source_path.is_file()
    monkeypatch.setattr(retention, "_retention_fault", lambda _stage, _path: None)

    def reject_rescan(*_args, **_kwargs):
        raise AssertionError("unchanged cataloged source was rescanned")

    monkeypatch.setattr(retention, "_scan_run_to_archive", reject_rescan)
    result = compact_closed_workflow_activity()

    assert result.archived_runs == ("closed-run",)
    assert not source_path.exists()
    assert summarize_workflow_agents(activity_limit=2)[0]["tool_calls"] == 1


def test_migration_streams_oversized_records_and_preserves_raw_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "large-closed-run")
    append_workflow_activity(
        "assistant-response",
        "Large assistant response",
        agent_session_id="large-agent",
        process_id=61_001,
        content="x" * (retention.MAX_SUMMARY_RECORD_BYTES * 2),
    )
    append_workflow_activity(
        "runner-exit",
        "Large runner exited",
        agent_session_id="large-agent",
        process_id=61_001,
    )
    source_path = workflow_run_activity_path("large-closed-run")
    source_bytes = source_path.read_bytes()
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "current-run")
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(),
    )
    original_read_text = Path.read_text

    def reject_jsonl_materialization(path: Path, *args, **kwargs):
        if path.suffix == ".jsonl":
            raise AssertionError(f"materialized JSONL during migration: {path}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_jsonl_materialization)
    compact_closed_workflow_activity()

    entry = _catalog()["runs"]["large-closed-run"]
    assert entry["skipped_records"] == 1
    archive_path = workflow_state_root() / entry["archive_path"]
    with gzip.open(archive_path, "rb") as handle:
        assert handle.read() == source_bytes


def test_native_startup_retention_hook_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def compact():
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            archived_runs=("old",),
            archived_agent_streams=("agent",),
            reclaimed_bytes=42,
        )

    monkeypatch.setattr(runner, "compact_closed_workflow_activity", compact)
    runner._compact_closed_activity_on_startup()
    assert calls == 1

    def fail():
        raise RuntimeError("simulated retention failure")

    monkeypatch.setattr(runner, "compact_closed_workflow_activity", fail)
    runner._compact_closed_activity_on_startup()


@pytest.mark.parametrize(
    "catalog_text",
    [
        "{not valid json",
        json.dumps(
            {
                "version": 99,
                "updated_at": "",
                "runs": {},
                "agent_streams": {},
            }
        ),
    ],
    ids=["malformed", "unsupported-version"],
)
def test_status_fails_open_to_hot_streams_when_catalog_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    catalog_text: str,
) -> None:
    _configure_state(monkeypatch, tmp_path)
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "hot-run")
    append_workflow_activity(
        "conversation-start",
        "Hot agent started",
        agent_session_id="hot-agent",
        process_id=71_001,
        user_message="Preserve this hot status",
    )
    catalog_path = retention.activity_retention_catalog_path(workflow_state_root())
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(catalog_text, encoding="utf-8")
    cold_path = workflow_state_root() / "activity/archive/runs/cold.jsonl.gz"
    cold_path.parent.mkdir(parents=True, exist_ok=True)
    cold_path.write_bytes(b"must not be opened")
    original_open = Path.open

    def reject_cold_open(path: Path, *args, **kwargs):
        if path.suffix == ".gz":
            raise AssertionError(f"status opened cold evidence: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_cold_open)
    monkeypatch.setattr(
        workflow_state,
        "_workflow_process_identity_is_live",
        _identity_live_only(71_001),
    )
    caplog.set_level(logging.WARNING, logger=retention.__name__)

    summaries = summarize_workflow_agents(activity_limit=2)
    recent = read_workflow_activity(limit=2)

    assert summaries[0]["agent_id"] == "hot-agent"
    assert recent[-1]["run_id"] == "hot-run"
    assert "Ignoring unreadable workflow activity retention catalog" in caplog.text


def test_main_publishes_restored_assignment_before_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active_file = tmp_path / "Main.lean"
    active_file.write_text("theorem restored_target : True := by trivial\n", encoding="utf-8")
    order: list[tuple[str, str, str]] = []
    verified_state = {
        "active_file": str(active_file),
        "declaration_scope": "file",
        "target_symbol": "restored_target",
        "sorry_count": 0,
        "project_sorry_count": 0,
        "verification_ok": True,
        "last_verification": {"ok": True, "scope": "file", "tool": "lean_verify"},
    }
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(runner, "install_native_termination_handlers", lambda: {})
    monkeypatch.setattr(runner, "restore_native_termination_handlers", lambda _handlers: None)
    monkeypatch.setattr(runner, "restore_sigint", lambda _handler: None)
    monkeypatch.setattr(runner, "_install_workflow_run_log_capture", lambda: None)
    monkeypatch.setattr(
        runner,
        "_persist_startup_live_status",
        lambda phase, live_state=None: order.append(
            ("startup", phase, str((live_state or {}).get("target_symbol", "")))
        ),
    )
    monkeypatch.setattr(runner, "_reconcile_stale_workflow_file_locks", lambda: 0)
    monkeypatch.setattr(runner.campaign_epoch, "ensure_campaign", lambda _state: {})
    monkeypatch.setattr(runner, "_cleanup_scratch_artifacts_on_startup", lambda _state: {})
    monkeypatch.setattr(runner.environment_memory, "hydrate", lambda _state: {})
    monkeypatch.setattr(runner, "_migrate_negation_promotions_on_startup", lambda: {})
    monkeypatch.setattr(runner.research_findings, "hydrate_delivery_markers", lambda _state: None)

    def restore_assignment(state):
        state["current_queue_assignment"] = {
            "active_file": str(active_file),
            "target_symbol": "restored_target",
        }
        return True

    monkeypatch.setattr(runner, "_restore_queue_manager_state", restore_assignment)
    monkeypatch.setattr(
        runner,
        "_restored_queue_assignment_live_state",
        lambda _state: dict(verified_state),
    )
    monkeypatch.setattr(
        runner,
        "_reconcile_negation_promotions_on_startup",
        lambda _state: SimpleNamespace(terminal_disproof=False),
    )
    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_plan_state_resume_block", lambda _state: "")
    monkeypatch.setattr(
        runner,
        "_verified_startup_preflight",
        lambda _history, _checkpoint, _autonomy: dict(verified_state),
    )
    monkeypatch.setattr(
        runner,
        "_persist_live_status",
        lambda *args, phase="", **_kwargs: order.append(
            ("persist", str(phase), str(args[3].get("target_symbol", "")))
        ),
    )
    monkeypatch.setattr(
        runner,
        "_print_header",
        lambda: order.append(("header", "", "")),
    )

    def compact_after_ready() -> None:
        assert ("persist", "ready", "restored_target") in order
        order.append(("retention", "", "restored_target"))

    monkeypatch.setattr(runner, "_compact_closed_activity_on_startup", compact_after_ready)
    monkeypatch.setattr(
        runner,
        "_verified_workflow_should_exit_without_prompt",
        lambda _state: True,
    )
    monkeypatch.setattr(runner, "_maybe_sync_plan_state", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(runner, "_maybe_record_learnings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_negation_reconciliation_barrier", lambda _state: False)
    monkeypatch.setattr(
        runner,
        "_revalidate_verified_scope_after_quiescence",
        lambda *_args, **_kwargs: dict(verified_state),
    )
    monkeypatch.setattr(
        runner,
        "_terminal_authority_snapshot_is_current",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(runner, "_record_agent_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_stop_native_owned_work", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_release_native_runner_locks", lambda _agent: None)
    monkeypatch.setattr(runner, "_record_campaign_exit", lambda code, *_args, **_kwargs: code)

    assert runner.main() == 0
    assert order.index(("persist", "ready", "restored_target")) < order.index(
        ("retention", "", "restored_target")
    )
    assert order.index(("header", "", "")) < order.index(("retention", "", "restored_target"))
