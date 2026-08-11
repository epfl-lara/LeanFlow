"""Contract tests for ``leanflow runs`` — the read-only run-state JSON surface.

External observers (the VS Code extension, the evaluation harness) depend on
these payload shapes, so the tests pin the contract rather than the rendering.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from leanflow_cli.cli import run_metrics
from leanflow_cli.cli.runs_command import handle_runs
from leanflow_cli.workflows import workflow_activity_retention as retention
from leanflow_cli.workflows import workflow_state


def _state_root(tmp_path: Path) -> Path:
    root = tmp_path / ".leanflow" / "workflow-state"
    (root / "activity" / "runs").mkdir(parents=True, exist_ok=True)
    (root / "activity" / "run-metadata").mkdir(parents=True, exist_ok=True)
    return root


def _write_events(root: Path, run_id: str, events: list[dict]) -> None:
    path = root / "activity" / "runs" / f"{run_id}.jsonl"
    path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )


def _event(event_id: str, event_type: str, message: str, timestamp: str) -> dict:
    return {
        "event_id": event_id,
        "timestamp": timestamp,
        "type": event_type,
        "message": message,
        "run_id": "prove-run-a",
        "details": {"workflow_kind": "prove", "workflow_command": "/prove Main.lean"},
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = _state_root(tmp_path)
    _write_events(
        root,
        "prove-run-a",
        [
            _event("e1", "runner-start", "started", "2026-08-01T00:00:00+00:00"),
            _event("e2", "tool-call", "lean_check", "2026-08-01T00:00:05+00:00"),
            _event("e3", "tool-result", "ok", "2026-08-01T00:00:09+00:00"),
        ],
    )
    (root / "journal.jsonl").write_text(
        json.dumps({"event": "node-created", "name": "foo", "ts": "2026-08-01T00:00:01+00:00"})
        + "\n",
        encoding="utf-8",
    )
    (root / "outcomes.jsonl").write_text(
        json.dumps(
            {
                "kind": "workflow-route",
                "timestamp": "2026-08-01T00:00:02+00:00",
                "payload": {"route_action": "queue-worker"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return tmp_path


def _run(project: Path, command: str, **kwargs) -> dict:
    args = argparse.Namespace(project=str(project), runs_command=command, pretty=False, **kwargs)
    assert handle_runs(args) == 0
    return args


def test_list_reports_one_run_with_metadata(project: Path, capsys) -> None:
    _run(project, "list", limit=10)
    payload = json.loads(capsys.readouterr().out)
    assert payload["complete"] is True
    assert payload["archive_audit"]["catalog_status"] == "complete-empty"
    assert payload["count"] == 1
    (run,) = payload["runs"]
    assert run["run_id"] == "prove-run-a"
    assert run["event_count"] == 3
    assert run["workflow_kind"] == "prove"
    assert run["workflow_command"] == "/prove Main.lean"
    assert run["started_at"] == "2026-08-01T00:00:00+00:00"
    assert run["updated_at"] == "2026-08-01T00:00:09+00:00"
    assert run["last_event_type"] == "tool-result"
    assert run["terminal"] is False
    assert run["exit_code"] is None
    assert run["terminal_phase"] == ""
    assert run["terminal_status"] == "running"
    assert run["final_snapshot_recorded"] is False


def test_list_keeps_archived_final_runs_visible_for_reload_reconciliation(
    project: Path, capsys
) -> None:
    root = _state_root(project)
    results = root / "activity" / "run-results"
    results.mkdir(parents=True)
    result_payload = {
        "version": 1,
        "run_id": "archived-run",
        "finalized_at": "2026-08-02T00:00:02+00:00",
        "stream": {"event_count": 12},
        "run": {
            "workflow_kind": "prove",
            "workflow_command": "/prove Archived.lean",
            "started_at": "2026-08-02T00:00:00+00:00",
            "updated_at": "2026-08-02T00:00:02+00:00",
        },
        "outcome": {
            "reason": "runtime failure",
            "phase": "exited",
            "exit_code": 1,
        },
    }
    (results / "archived-run.json").write_text(json.dumps(result_payload), encoding="utf-8")
    integrity = root / "activity" / "run-result-integrity"
    integrity.mkdir(parents=True)
    (integrity / "archived-run.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "archived-run",
                "algorithm": "sha256",
                "payload_sha256": run_metrics._snapshot_sha256(result_payload),
            }
        ),
        encoding="utf-8",
    )

    _run(project, "list", limit=10)
    payload = json.loads(capsys.readouterr().out)
    archived = next(run for run in payload["runs"] if run["run_id"] == "archived-run")

    assert archived["terminal"] is True
    assert archived["final_snapshot_recorded"] is True
    assert archived["last_event_type"] == "runner-exit"
    assert archived["event_count"] == 12
    assert archived["exit_code"] == 1
    assert archived["terminal_phase"] == "exited"
    assert archived["terminal_status"] == "failed"


def test_list_does_not_attach_a_stale_result_to_a_reused_hot_run(project: Path, capsys) -> None:
    root = _state_root(project)
    results = root / "activity" / "run-results"
    results.mkdir(parents=True)
    (results / "prove-run-a.json").write_text(
        json.dumps(
            {
                "version": 1,
                "run_id": "prove-run-a",
                "finalized_at": "2026-07-01T00:00:00+00:00",
                "stream": {
                    "event_count": 99,
                    "events_sha256": "0" * 64,
                    "terminal_event_id": "old-exit",
                },
                "outcome": {"phase": "exited", "exit_code": 0},
            }
        ),
        encoding="utf-8",
    )

    _run(project, "list", limit=10)
    payload = json.loads(capsys.readouterr().out)
    current = next(run for run in payload["runs"] if run["run_id"] == "prove-run-a")

    assert current["terminal"] is False
    assert current["exit_code"] is None
    assert current["terminal_status"] == "running"
    assert current["final_snapshot_recorded"] is False
    assert payload["complete"] is False
    assert any(
        issue.startswith("final-snapshot-invalid:prove-run-a")
        for issue in payload["completeness_issues"]
    )


@pytest.mark.parametrize(
    ("exit_code", "expected_status"),
    [(0, "succeeded"), (2, "paused"), (3, "disproved"), (130, "interrupted"), (17, "failed")],
)
def test_list_preserves_hot_runner_exit_semantics(
    project: Path, capsys, exit_code: int, expected_status: str
) -> None:
    root = _state_root(project)
    run_id = f"terminal-{exit_code}"
    _write_events(
        root,
        run_id,
        [
            {
                "event_id": f"{run_id}-exit",
                "timestamp": "2026-08-03T00:00:00+00:00",
                "type": "runner-exit",
                "message": "done",
                "run_id": run_id,
                "details": {"exit_code": exit_code, "phase": "exited"},
            }
        ],
    )

    _run(project, "list", limit=20)
    payload = json.loads(capsys.readouterr().out)
    summary = next(run for run in payload["runs"] if run["run_id"] == run_id)

    assert summary["terminal"] is True
    assert summary["exit_code"] == exit_code
    assert summary["terminal_phase"] == "exited"
    assert summary["terminal_status"] == expected_status


def test_list_includes_strictly_verified_retained_crash_history(
    project: Path, capsys, monkeypatch
) -> None:
    def audit(_state_root, *, on_event):
        on_event(
            {
                "event_id": "crash-1",
                "run_id": "crashed-run",
                "timestamp": "2026-07-31T00:00:00+00:00",
                "type": "runner-start",
                "message": "started",
                "details": {
                    "workflow_kind": "prove",
                    "workflow_command": "/prove Crashed.lean",
                },
            }
        )
        on_event(
            {
                "event_id": "crash-2",
                "run_id": "crashed-run",
                "timestamp": "2026-07-31T00:00:03+00:00",
                "type": "tool-call",
                "message": "last retained evidence",
                "details": {},
            }
        )
        return SimpleNamespace(
            complete=True,
            catalog_status="verified",
            catalog_runs=1,
            verified_runs=1,
            verified_events=2,
            matched_events=2,
            issue_counts=(),
            issue_samples=(),
        )

    monkeypatch.setattr(retention, "audit_retained_run_events", audit)

    _run(project, "list", limit=10)
    payload = json.loads(capsys.readouterr().out)
    retained = next(run for run in payload["runs"] if run["run_id"] == "crashed-run")

    assert payload["version"] == 2
    assert payload["complete"] is True
    assert payload["archive_audit"]["complete"] is True
    assert retained["stream_source"] == "retained"
    assert retained["started_at"] == "2026-07-31T00:00:00+00:00"
    assert retained["updated_at"] == "2026-07-31T00:00:03+00:00"
    assert retained["terminal"] is False
    assert retained["exit_code"] is None
    assert retained["terminal_phase"] == ""
    assert retained["terminal_status"] == "unknown"


def test_list_discards_provisional_retained_rows_when_archive_audit_fails(
    project: Path, capsys, monkeypatch
) -> None:
    def audit(_state_root, *, on_event):
        on_event(
            {
                "event_id": "bad-1",
                "run_id": "untrusted-run",
                "timestamp": "2026-07-30T00:00:00+00:00",
                "type": "runner-start",
                "message": "provisional",
                "details": {},
            }
        )
        return SimpleNamespace(
            complete=False,
            catalog_status="malformed",
            catalog_runs=1,
            verified_runs=0,
            verified_events=0,
            matched_events=1,
            issue_counts=(("archive_checksum_mismatch", 1),),
            issue_samples=(
                SimpleNamespace(
                    code="archive_checksum_mismatch",
                    run_id="untrusted-run",
                    path="archive.gz",
                    detail="digest mismatch",
                ),
            ),
        )

    monkeypatch.setattr(retention, "audit_retained_run_events", audit)

    _run(project, "list", limit=10)
    payload = json.loads(capsys.readouterr().out)

    assert payload["complete"] is False
    assert "retained-archive:archive_checksum_mismatch" in payload["completeness_issues"]
    assert all(run["run_id"] != "untrusted-run" for run in payload["runs"])


def test_list_marks_hot_stream_corruption_incomplete(project: Path, capsys) -> None:
    path = project / ".leanflow" / "workflow-state" / "activity" / "runs" / "prove-run-a.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event_id":"partial"')

    _run(project, "list", limit=10)
    payload = json.loads(capsys.readouterr().out)
    summary = next(run for run in payload["runs"] if run["run_id"] == "prove-run-a")

    assert payload["complete"] is False
    assert "hot-stream-invalid:prove-run-a" in payload["completeness_issues"]
    assert summary["stream_integrity_complete"] is False


def test_list_limit_truncation_cannot_claim_complete_history(project: Path, capsys) -> None:
    root = _state_root(project)
    _write_events(
        root,
        "second-run",
        [
            {
                "event_id": "second-1",
                "timestamp": "2026-08-02T00:00:00+00:00",
                "type": "runner-start",
                "message": "started",
                "run_id": "second-run",
                "details": {},
            }
        ],
    )

    _run(project, "list", limit=1)
    payload = json.loads(capsys.readouterr().out)

    assert payload["count"] == 1
    assert payload["total_count"] == 2
    assert payload["truncated"] is True
    assert payload["complete"] is False
    assert "history-truncated-by-limit:1" in payload["completeness_issues"]


def test_missing_catalog_with_orphan_retained_artifact_is_incomplete(project: Path, capsys) -> None:
    archive = project / ".leanflow" / "workflow-state" / "activity" / "archive" / "runs"
    archive.mkdir(parents=True)
    (archive / "lost-run.jsonl.gz").write_bytes(b"orphan")

    _run(project, "list", limit=10)
    payload = json.loads(capsys.readouterr().out)

    assert payload["complete"] is False
    assert payload["archive_audit"]["catalog_status"] == "missing"
    assert "retained-archive:catalog_missing" in payload["completeness_issues"]


def test_events_returns_the_whole_stream_and_a_cursor(project: Path, capsys) -> None:
    _run(project, "events", run_id="", limit=200, since="", event_types="")
    payload = json.loads(capsys.readouterr().out)
    assert [event["event_id"] for event in payload["events"]] == ["e1", "e2", "e3"]
    assert payload["cursor"] == "e3"


def test_events_since_cursor_returns_only_newer_events(project: Path, capsys) -> None:
    _run(project, "events", run_id="prove-run-a", limit=200, since="e1", event_types="")
    payload = json.loads(capsys.readouterr().out)
    assert [event["event_id"] for event in payload["events"]] == ["e2", "e3"]


def test_unknown_cursor_falls_back_to_the_tail(project: Path, capsys) -> None:
    """A rotated stream must not stall a follower that holds a stale cursor."""
    _run(project, "events", run_id="prove-run-a", limit=200, since="gone", event_types="")
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["events"]) == 3


def test_events_filter_by_type(project: Path, capsys) -> None:
    _run(project, "events", run_id="", limit=200, since="", event_types="tool-call,tool-result")
    payload = json.loads(capsys.readouterr().out)
    assert {event["type"] for event in payload["events"]} == {"tool-call", "tool-result"}


def test_types_counts_events_for_the_filter_UI(project: Path, capsys) -> None:
    _run(project, "types", run_id="")
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 3
    assert {row["type"] for row in payload["types"]} == {
        "runner-start",
        "tool-call",
        "tool-result",
    }


def test_journal_and_outcomes_are_readable(project: Path, capsys) -> None:
    _run(project, "journal", limit=10)
    journal = json.loads(capsys.readouterr().out)
    assert journal["records"][0]["event"] == "node-created"

    _run(project, "outcomes", limit=10)
    outcomes = json.loads(capsys.readouterr().out)
    assert outcomes["outcomes"][0]["kind"] == "workflow-route"


def test_missing_state_is_reported_as_empty_not_an_error(tmp_path: Path, capsys) -> None:
    args = argparse.Namespace(project=str(tmp_path), runs_command="list", pretty=False, limit=10)
    assert handle_runs(args) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0


def test_project_override_does_not_leak_into_the_process_environment(project: Path, capsys) -> None:
    """--project must not redirect state reads for anything that runs afterwards."""
    import os

    before = os.environ.get("LEANFLOW_PROJECT_ROOT")
    _run(project, "list", limit=5)
    capsys.readouterr()
    assert os.environ.get("LEANFLOW_PROJECT_ROOT") == before


def test_project_override_restores_a_pre_existing_value(project: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", "/somewhere/else")
    _run(project, "list", limit=5)
    capsys.readouterr()
    import os

    assert os.environ["LEANFLOW_PROJECT_ROOT"] == "/somewhere/else"


def test_status_binds_legacy_snapshot_to_exact_process_identity(
    project: Path, capsys, monkeypatch
) -> None:
    root = _state_root(project)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_PROCESS_TOKEN", "legacy-live-owner")
    workflow_state.save_workflow_live_status(
        {
            "phase": "busy",
            "process_id": os.getpid(),
            "sorry_count": 2,
            "project_sorry_count": 5,
        }
    )
    status = workflow_state.load_workflow_live_status()
    _write_events(
        root,
        "legacy-live-run",
        [
            {
                "event_id": "legacy-live-event",
                "timestamp": "2026-08-11T11:00:00+00:00",
                "type": "tool-call",
                "run_id": "legacy-live-run",
                "message": "lean_check",
                "details": {
                    "workflow_kind": "prove",
                    "process_id": os.getpid(),
                    "process_token_sha256": status["process_token_sha256"],
                },
            }
        ],
    )

    _run(project, "status")
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"]["run_id"] == "legacy-live-run"
    assert payload["status"]["sorry_count"] == 2


def test_status_refuses_pid_only_legacy_attribution(project: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        workflow_state,
        "load_workflow_live_status",
        lambda: {"phase": "busy", "process_id": os.getpid(), "sorry_count": 1},
    )

    _run(project, "status")
    payload = json.loads(capsys.readouterr().out)

    assert "run_id" not in payload["status"]


def test_corrupt_jsonl_lines_are_skipped(project: Path, capsys) -> None:
    """A partially written line is normal while a run is live; it must not throw."""
    path = project / ".leanflow" / "workflow-state" / "activity" / "runs" / "prove-run-a.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"event_id": "e4", "type": "tool-c')
    _run(project, "events", run_id="prove-run-a", limit=200, since="", event_types="")
    payload = json.loads(capsys.readouterr().out)
    assert [event["event_id"] for event in payload["events"]] == ["e1", "e2", "e3"]


def test_log_with_run_id_reads_only_that_timestamped_log(project: Path, capsys) -> None:
    root = _state_root(project)
    logs = root / "runs"
    logs.mkdir(parents=True)
    (logs / "prove-run-a.log").write_text("a-one\na-two\n", encoding="utf-8")
    (logs / "other-run.log").write_text("wrong run\n", encoding="utf-8")
    (root / "latest-run.log").write_text("wrong latest\n", encoding="utf-8")

    _run(project, "log", run_id="prove-run-a", tail=1)

    assert capsys.readouterr().out.strip() == "a-two"


def test_unknown_run_log_returns_nonzero_json(project: Path, capsys) -> None:
    args = argparse.Namespace(
        project=str(project), runs_command="log", run_id="missing-run", tail=20
    )

    assert handle_runs(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is False
    assert payload["run_id"] == "missing-run"


def test_invalid_run_id_is_rejected_before_reading(project: Path, capsys) -> None:
    args = argparse.Namespace(
        project=str(project),
        runs_command="events",
        run_id="../../outside",
        limit=20,
        since="",
        event_types="",
        pretty=False,
    )

    assert handle_runs(args) == 2
    assert "run id" in json.loads(capsys.readouterr().out)["error"]


def test_metrics_unknown_run_returns_nonzero_without_global_fallback(project: Path, capsys) -> None:
    args = argparse.Namespace(
        project=str(project),
        runs_command="metrics",
        run_id="missing-run",
        pretty=False,
        no_provenance=False,
    )

    assert handle_runs(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"]["exact"] is False
    assert payload["declarations"] == []
    assert payload["outcome"]["proof_solved"] is None


def test_stop_requires_the_requested_run_to_be_the_live_owner(
    project: Path, capsys, monkeypatch
) -> None:
    called = False

    def interrupt(payload):
        nonlocal called
        called = True
        return {"success": True}

    monkeypatch.setattr(
        workflow_state,
        "load_workflow_live_status",
        lambda: {"run_id": "other-run", "process_id": 123},
    )
    monkeypatch.setattr(workflow_state, "interrupt_workflow_process", interrupt)
    args = argparse.Namespace(project=str(project), runs_command="stop", run_id="prove-run-a")

    assert handle_runs(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped"] is False
    assert called is False


def test_stop_propagates_verified_identity_rejection(project: Path, capsys, monkeypatch) -> None:
    identity = {
        "run_id": "prove-run-a",
        "process_id": 24680,
        "process_group_id": 24680,
        "process_session_id": 24680,
        "process_token_sha256": "a" * 64,
    }
    monkeypatch.setattr(workflow_state, "load_workflow_live_status", lambda: identity)
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda payload: False)
    signalled: list[int] = []
    monkeypatch.setattr(workflow_state.os, "killpg", lambda pid, sig: signalled.append(pid))
    args = argparse.Namespace(project=str(project), runs_command="stop", run_id="prove-run-a")

    assert handle_runs(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped"] is False
    assert "no longer matches" in payload["reason"]
    assert signalled == []


def test_stop_reports_success_for_the_revalidated_owner(project: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        workflow_state,
        "load_workflow_live_status",
        lambda: {"run_id": "prove-run-a", "process_id": 24680},
    )
    monkeypatch.setattr(
        workflow_state,
        "interrupt_workflow_process",
        lambda payload: {
            "success": True,
            "process_id": payload["process_id"],
            "process_group_id": 24680,
            "identity_verified": True,
        },
    )
    args = argparse.Namespace(project=str(project), runs_command="stop", run_id="prove-run-a")

    assert handle_runs(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped"] is True
    assert payload["identity_verified"] is True


def test_advertised_json_aliases_parse() -> None:
    from leanflow_cli.main import _build_parser

    metrics = _build_parser().parse_args(["runs", "metrics", "run-a", "--json"])
    provenance = _build_parser().parse_args(["runs", "provenance", "--json"])

    assert metrics.runs_command == "metrics" and metrics.json is True
    assert provenance.runs_command == "provenance" and provenance.json is True
