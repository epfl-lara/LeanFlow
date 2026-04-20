from __future__ import annotations

from opengauss_cli import native_runner as runner
from opengauss_cli.workflow_state import load_workflow_live_status, read_workflow_activity


def test_persist_live_status_writes_shell_visible_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENGAUSS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENGAUSS_NATIVE_WORKFLOW_KIND", "autoprove")
    monkeypatch.setenv("OPENGAUSS_NATIVE_WORKFLOW_COMMAND", "/lean4:autoprove Main.lean")
    monkeypatch.setenv("OPENGAUSS_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("OPENGAUSS_NATIVE_MODEL", "google/gemma-4-31B-it")
    monkeypatch.setenv("OPENGAUSS_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("OPENGAUSS_NATIVE_ACTIVE_SKILL", "lean-proof-loop")
    monkeypatch.setenv("OPENGAUSS_PROJECT_ROOT", str(tmp_path / "project"))

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


def test_record_activity_captures_workflow_and_skill_context(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENGAUSS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENGAUSS_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("OPENGAUSS_NATIVE_WORKFLOW_COMMAND", "/lean4:prove Main.lean")
    monkeypatch.setenv("OPENGAUSS_NATIVE_ACTIVE_SKILL", "lean-diagnostics")

    runner._record_activity("resume", "Loaded workflow checkpoint", checkpoint_label="milestone")

    events = read_workflow_activity(limit=4)

    assert len(events) == 1
    assert events[0]["type"] == "resume"
    assert events[0]["details"]["workflow_kind"] == "prove"
    assert events[0]["details"]["active_skill"] == "lean-diagnostics"
    assert events[0]["details"]["checkpoint_label"] == "milestone"
