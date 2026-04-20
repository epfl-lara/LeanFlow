from __future__ import annotations

from epflemma_cli import native_runner as runner
from epflemma_cli.workflow_state import (
    append_workflow_run_log,
    append_workflow_activity,
    enqueue_workflow_agent_message,
    load_workflow_live_status,
    read_workflow_activity,
    read_workflow_agent_inbox,
    read_workflow_run_log,
    resolve_workflow_agent_id,
    reset_workflow_run_log,
    summarize_workflow_agents,
    terminate_all_workflow_agents,
    terminate_workflow_agent,
    terminate_workflow_agent_descendants,
    workflow_agent_transcript,
    workflow_runs_root,
    workflow_agent_detail,
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
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "review")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/lean4:review Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "lean-diagnostics")

    runner._record_activity("resume", "Loaded workflow checkpoint", checkpoint_label="milestone")

    events = read_workflow_activity(limit=4)

    assert len(events) == 1
    assert events[0]["type"] == "resume"
    assert events[0]["details"]["workflow_kind"] == "review"
    assert events[0]["details"]["active_skill"] == "lean-diagnostics"
    assert events[0]["details"]["checkpoint_label"] == "milestone"


def test_workflow_run_log_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    reset_workflow_run_log()
    append_workflow_run_log("line 1\n")
    append_workflow_run_log("line 2\n")
    append_workflow_run_log("line 3\n")

    assert read_workflow_run_log(tail_lines=2) == "line 2\nline 3"


def test_workflow_run_log_creates_timestamped_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    reset_workflow_run_log()
    append_workflow_run_log("alpha\nbeta\n")

    run_logs = list(workflow_runs_root().glob("*.log"))
    assert len(run_logs) == 1
    assert run_logs[0].read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_workflow_activity_preserves_full_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    full_text = "x" * 500

    append_workflow_activity("assistant-response", "Assistant response received", content=full_text)

    events = read_workflow_activity(limit=1)
    assert events[0]["details"]["content"] == full_text


def test_workflow_agent_summary_groups_events(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=12345,
        model="google/gemma-4-31B-it",
        provider="custom",
        delegate_depth=0,
        user_message="Prove theorem t",
    )
    append_workflow_activity(
        "api-request",
        "API call #1",
        agent_session_id="agent-main",
        iteration=1,
    )
    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        agent_session_id="agent-main",
        content="I will inspect diagnostics first.",
    )
    append_workflow_activity(
        "conversation-end",
        "Agent conversation finished",
        agent_session_id="agent-main",
        completed=True,
        api_calls=1,
    )

    summaries = summarize_workflow_agents(activity_limit=3)

    assert len(summaries) == 1
    assert summaries[0]["agent_id"] == "agent-main"
    assert summaries[0]["status"] == "completed"
    assert summaries[0]["api_calls"] == 1
    assert summaries[0]["model"] == "google/gemma-4-31B-it"
    assert summaries[0]["process_id"] == 12345

    detail = workflow_agent_detail("agent-main", activity_limit=2)
    assert detail["agent_id"] == "agent-main"
    assert len(detail["recent_activity"]) == 2


def test_workflow_agent_resolution_and_termination(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        process_id=24680,
    )

    assert resolve_workflow_agent_id("123") == "12345"

    captured: dict[str, tuple[int, int]] = {}

    def _fake_killpg(pid: int, sig: int) -> None:
        captured["killpg"] = (pid, sig)

    monkeypatch.setattr("epflemma_cli.workflow_state.os.killpg", _fake_killpg)

    result = terminate_workflow_agent("123")

    assert result["success"] is True
    assert result["agent_id"] == "12345"
    assert captured["killpg"][0] == 24680


def test_workflow_agent_descendant_termination(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity("conversation-start", "start", agent_session_id="11111", process_id=101)
    append_workflow_activity("conversation-start", "start", agent_session_id="22222", parent_agent_session_id="11111", process_id=202)
    append_workflow_activity("conversation-start", "start", agent_session_id="33333", parent_agent_session_id="22222", process_id=303)

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("epflemma_cli.workflow_state.os.killpg", _fake_killpg)

    result = terminate_workflow_agent_descendants("11111")

    assert result["success"] is True
    assert result["count"] == 2
    assert set(result["terminated"]) == {"22222", "33333"}
    assert killed == [303, 202] or killed == [202, 303]


def test_terminate_all_workflow_agents_excludes_current(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity("conversation-start", "start", agent_session_id="11111", process_id=101)
    append_workflow_activity("conversation-start", "start", agent_session_id="22222", process_id=202)
    append_workflow_activity("conversation-start", "start", agent_session_id="33333", process_id=303)

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("epflemma_cli.workflow_state.os.killpg", _fake_killpg)

    result = terminate_all_workflow_agents(exclude_agent_id="22222", exclude_process_id=303)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["11111"]
    assert killed == [101]


def test_workflow_agent_transcript_collects_recent_interactions(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="54321",
        user_message="Prove theorem foo",
    )
    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        agent_session_id="54321",
        content="I will inspect diagnostics first.",
    )
    append_workflow_activity(
        "tool-call",
        "Tool call: terminal",
        agent_session_id="54321",
        tool="terminal",
    )

    transcript = workflow_agent_transcript("54321", limit=5)

    assert transcript[0]["role"] == "user"
    assert transcript[0]["content"] == "Prove theorem foo"
    assert transcript[1]["role"] == "assistant"
    assert "inspect diagnostics" in transcript[1]["content"]
    assert transcript[2]["role"] == "tool-call"


def test_workflow_agent_transcript_uses_specific_tool_previews(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        agent_session_id="55555",
        content="",
        tool_calls=[
            {
                "name": "patch",
                "arguments": '{"path":"./GaussTest/GaussTest/RealTheorems-homework.lean","mode":"replace"}',
            }
        ],
    )
    append_workflow_activity(
        "tool-result",
        "Tool result: patch",
        agent_session_id="55555",
        tool="patch",
        is_error=False,
        result='{"success":true,"files_modified":["./GaussTest/GaussTest/RealTheorems-homework.lean"],"diff":"@@ -1 +1 @@\\n-old\\n+new\\n"}',
    )

    transcript = workflow_agent_transcript("55555", limit=4)

    assert transcript[0]["role"] == "assistant"
    assert "Edit ./GaussTest/GaussTest/RealTheorems-homework.lean (replace)" in transcript[0]["content"]
    assert transcript[1]["role"] == "tool-result"
    assert "updated ./GaussTest/GaussTest/RealTheorems-homework.lean" in transcript[1]["content"]
    assert "1 hunk(s)" in transcript[1]["content"]


def test_workflow_agent_queue_and_waiting_state(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        process_id=24680,
        model="zai-org/GLM-5",
    )
    append_workflow_activity(
        "agent-awaiting-input",
        "Background workflow agent is waiting for input",
        agent_session_id="12345",
        process_id=24680,
        status="verified",
    )
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: True)

    result = enqueue_workflow_agent_message("12345", "Try a different proof strategy.")

    assert result["success"] is True
    inbox = read_workflow_agent_inbox("12345")
    assert inbox[-1]["text"] == "Try a different proof strategy."

    summaries = summarize_workflow_agents(activity_limit=4)
    assert summaries[0]["status"] == "queued"

    transcript = workflow_agent_transcript("12345", limit=6)
    assert transcript[-1]["role"] == "user"
    assert "different proof strategy" in transcript[-1]["content"]


def test_enqueue_workflow_agent_message_rejects_dead_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        process_id=24680,
    )
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: False)

    result = enqueue_workflow_agent_message("12345", "Try again")

    assert result["success"] is False
    assert result["error"] == "Agent process is no longer running."
