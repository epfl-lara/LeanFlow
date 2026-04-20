from __future__ import annotations

from epflemma_cli import native_runner as runner
from epflemma_cli.workflow_state import (
    append_workflow_run_log,
    load_workflow_live_status,
    read_workflow_activity,
    read_workflow_run_log,
    reset_workflow_run_log,
)


def test_persist_live_status_writes_shell_visible_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "autoprove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/lean4:autoprove Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "google/gemma-4-31B-it")
    monkeypatch.setenv("EPFLEMMA_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "lean-proof-loop")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(tmp_path / "project"))

    runner._persist_live_status(
        [{"role": "assistant", "content": "Working"}],
        compaction_state={"reason": "auto", "snapshot_text": "snapshot"},
        checkpoint_state={
            "count": 2,
            "current": {
                "label": "proof milestone",
                "linked_filesystem_checkpoint": "abc123def456",
            },
        },
        live_state={
            "active_file": "Main.lean",
            "active_file_label": "Main.lean",
            "target_symbol": "demo",
            "diagnostics": "warning: declaration uses sorry",
            "goals": "x : Nat\n⊢ x = x",
            "build_status": "lake build running",
            "message": "1 goal remaining",
            "sorry_count": 1,
            "blocker_summary": "remaining sorry",
        },
        phase="busy",
    )

    payload = load_workflow_live_status()

    assert payload["phase"] == "busy"
    assert payload["workflow_kind"] == "autoprove"
    assert payload["active_skill"] == "lean-proof-loop"
    assert payload["latest_checkpoint_label"] == "proof milestone"
    assert payload["snapshot_present"] is True
    assert payload["goals"] == "x : Nat\n⊢ x = x"


def test_workflow_state_prefers_project_local_state(monkeypatch, tmp_path):
    project = tmp_path / "project"
    (project / ".epflemma").mkdir(parents=True)
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))

    reset_workflow_run_log()
    append_workflow_run_log("alpha\n")

    assert read_workflow_run_log(1) == "alpha"
    assert (project / ".epflemma" / "workflow-state" / "latest-run.log").is_file()


def test_record_activity_captures_workflow_and_skill_context(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/lean4:prove Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "lean-diagnostics")

    runner._record_activity("resume", "Loaded workflow checkpoint", checkpoint_label="milestone")

    events = read_workflow_activity(limit=4)

    assert len(events) == 1
    assert events[0]["type"] == "resume"
    assert events[0]["details"]["workflow_kind"] == "prove"
    assert events[0]["details"]["active_skill"] == "lean-diagnostics"
    assert events[0]["details"]["checkpoint_label"] == "milestone"


def test_workflow_run_log_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    reset_workflow_run_log()
    append_workflow_run_log("line 1\n")
    append_workflow_run_log("line 2\n")
    append_workflow_run_log("line 3\n")

    assert read_workflow_run_log(tail_lines=2) == "line 2\nline 3"
