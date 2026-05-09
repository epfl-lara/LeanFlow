from __future__ import annotations

import json

from epflemma_cli import native_runner as runner
from epflemma_cli.config import save_config
from epflemma_cli.workflow_state import (
    append_workflow_run_log,
    append_workflow_activity,
    enqueue_workflow_agent_message,
    _agent_event_preview,
    load_workflow_live_status,
    save_workflow_live_status,
    read_workflow_activity,
    read_workflow_agent_inbox,
    read_workflow_run_log,
    resolve_workflow_agent_id,
    reset_workflow_run_log,
    summarize_workflow_agents,
    workflow_agent_activity_path,
    workflow_latest_run_activity_path,
    workflow_run_activity_path,
    workflow_run_metadata_path,
    terminate_project_workflow_agents,
    terminate_all_workflow_agents,
    terminate_workflow_agent,
    terminate_workflow_agent_descendants,
    workflow_agent_transcript,
    workflow_runs_root,
    workflow_agent_detail,
)


def test_persist_live_status_writes_shell_visible_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
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
            "proof_solved": True,
            "warning_cleanup_status": "verified",
            "warning_cleanup_attempted": True,
            "warning_cleanup_verified": True,
            "warning_cleanup_warning_count": 0,
            "warning_cleanup_diagnostics": "warning cleanup verified; no warnings remain",
            "warning_cleanup": {
                "status": "verified",
                "proof_solved": True,
                "attempted": True,
                "verified": True,
                "skipped": False,
                "blocked": False,
                "warning_count": 0,
                "warning_summary": "",
                "diagnostics": "warning cleanup verified; no warnings remain",
            },
        },
        phase="busy",
    )

    payload = load_workflow_live_status()

    assert payload["phase"] == "busy"
    assert payload["workflow_kind"] == "prove"
    assert payload["active_skill"] == "lean-proof-loop"
    assert payload["latest_checkpoint_label"] == "proof milestone"
    assert payload["snapshot_present"] is True
    assert payload["goals"] == "x : Nat\n⊢ x = x"
    assert payload["proof_solved"] is True
    assert payload["warning_cleanup_status"] == "verified"
    assert payload["warning_cleanup_verified"] is True
    assert payload["warning_cleanup"]["status"] == "verified"


def test_persist_live_status_releases_locks_before_exit_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_RUNNER_OWNER", "agent-a")

    released: list[str] = []
    monkeypatch.setattr(
        runner,
        "release_all_file_locks",
        lambda *, owner_id: released.append(owner_id) or {"released": 1},
    )
    monkeypatch.setattr(runner, "_held_lock_count", lambda owner_id: 0 if released else 1)

    runner._persist_live_status(
        [{"role": "assistant", "content": "Done"}],
        live_state={
            "active_file": "Main.lean",
            "active_file_label": "Main.lean",
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "lake env lean Main.lean exits 0",
            "verification_ok": True,
            "last_verification": {"ok": True, "scope": "file", "tool": "lean_verify"},
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "sorry_count": 0,
        },
        phase="exited",
    )

    payload = load_workflow_live_status()

    assert released == ["agent-a"]
    assert payload["phase"] == "exited"
    assert payload["held_locks"] == 0


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
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/review Main.lean")
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
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.delenv("EPFLEMMA_WORKFLOW_RUN_ID", raising=False)

    reset_workflow_run_log()
    append_workflow_run_log("alpha\nbeta\n")

    run_logs = list(workflow_runs_root().glob("*.log"))
    assert len(run_logs) == 1
    assert run_logs[0].name.startswith("prove-")
    assert run_logs[0].read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_workflow_activity_preserves_full_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "lean-proof-loop")
    full_text = "x" * 500

    append_workflow_activity("assistant-response", "Assistant response received", content=full_text)

    events = read_workflow_activity(limit=1)
    assert events[0]["event_id"]
    assert events[0]["run_id"]
    assert events[0]["timestamp"]
    assert events[0]["task_label"] == "prove"
    assert events[0]["details"]["content"] == full_text


def test_workflow_activity_preview_uses_reasoning_when_content_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        content="",
        reasoning_content="Plan: inspect diagnostics, patch theorem, rerun lake env lean.",
    )

    events = read_workflow_activity(limit=1)
    preview = _agent_event_preview(events[0])
    assert preview.startswith("Reasoning: ")
    assert "inspect diagnostics" in preview


def test_workflow_activity_preview_uses_configured_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    save_config(
        {
            "logging": {
                "activity_preview_chars": 80,
            }
        }
    )

    append_workflow_activity(
        "assistant-response",
        "Assistant response received",
        content="This is a deliberately long assistant response that should be truncated much earlier once the configured activity preview limit is applied.",
    )

    events = read_workflow_activity(limit=1)
    preview = _agent_event_preview(events[0])
    assert len(preview) <= 80
    assert preview.endswith("...")


def test_api_request_preview_includes_step_size(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "api-request",
        "API call #7",
        iteration=7,
        message_count=14,
        approx_tokens=12345,
    )

    events = read_workflow_activity(limit=1)
    preview = _agent_event_preview(events[0])
    assert preview == "API step #7 · 14 messages · ~12,345 tokens"


def test_conversation_start_preview_uses_larger_default_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    prompt = "Start " + ("x" * 360)

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        user_message=prompt,
    )

    events = read_workflow_activity(limit=1)
    preview = _agent_event_preview(events[0])
    assert preview.startswith("Prompt: Start ")
    assert "x" * 300 in preview


def test_workflow_activity_writes_run_and_agent_jsonl_streams(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.delenv("EPFLEMMA_WORKFLOW_RUN_ID", raising=False)

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        workflow_kind="prove",
        active_skill="lean-proof-loop",
    )

    latest_run_path = workflow_latest_run_activity_path()
    assert latest_run_path is not None
    root_events = latest_run_path.read_text(encoding="utf-8").splitlines()
    root_event = json.loads(root_events[0])
    run_path = workflow_run_activity_path(root_event["run_id"])
    agent_path = workflow_agent_activity_path("12345", "prove")

    assert run_path.is_file()
    assert agent_path.is_file()
    assert not (tmp_path / "home" / "workflow-state" / "activity.jsonl").exists()

    run_event = json.loads(run_path.read_text(encoding="utf-8").splitlines()[0])
    agent_event = json.loads(agent_path.read_text(encoding="utf-8").splitlines()[0])

    assert run_event["event_id"] == root_event["event_id"]
    assert agent_event["agent_id"] == "12345"
    assert agent_event["task_label"] == "prove"


def test_workflow_activity_marks_runner_start_as_top_level(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_EFFECTIVE_PROMPT", "use abs_abs_sub first")
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project_root))
    monkeypatch.delenv("EPFLEMMA_WORKFLOW_RUN_ID", raising=False)

    append_workflow_activity("runner-start", "Managed workflow runner started")

    latest_run_path = workflow_latest_run_activity_path()
    assert latest_run_path is not None
    event = json.loads(latest_run_path.read_text(encoding="utf-8").splitlines()[0])
    metadata = json.loads(workflow_run_metadata_path(event["run_id"]).read_text(encoding="utf-8"))

    assert event["run_scope"] == "top-level"
    assert event["details"]["run_scope"] == "top-level"
    assert event["details"]["project_root"] == str(project_root)
    assert event["details"]["effective_prompt"] == "use abs_abs_sub first"
    assert metadata["run_scope"] == "top-level"
    assert metadata["project_root"] == str(project_root)
    assert metadata["effective_prompt"] == "use abs_abs_sub first"


def test_workflow_latest_run_activity_path_prefers_top_level_run(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "prove-background-test")

    append_workflow_activity(
        "agent-awaiting-input",
        "Background workflow agent is waiting for input",
        agent_session_id="12345",
        process_id=24680,
    )
    background_run_id = json.loads(workflow_latest_run_activity_path(prefer_top_level=False).read_text(encoding="utf-8").splitlines()[0])["run_id"]

    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "prove-top-level-test")
    append_workflow_activity("runner-start", "Managed workflow runner started")

    latest_top_level = workflow_latest_run_activity_path()
    assert latest_top_level is not None
    latest_event = json.loads(latest_top_level.read_text(encoding="utf-8").splitlines()[0])

    assert latest_event["type"] == "runner-start"
    assert latest_event["run_id"] != background_run_id
    assert latest_event["run_scope"] == "top-level"


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
    assert summaries[0]["task_label"] == "agent"

    detail = workflow_agent_detail("agent-main", activity_limit=2)
    assert detail["agent_id"] == "agent-main"
    assert len(detail["recent_activity"]) == 2


def test_workflow_agent_summary_uses_workflow_task_label(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="12345",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-proof-loop",
    )

    summaries = summarize_workflow_agents(activity_limit=1)

    assert summaries[0]["task_label"] == "prove"


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
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: True)

    result = terminate_all_workflow_agents(exclude_agent_id="22222", exclude_process_id=303)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["11111"]
    assert killed == [101]


def test_terminate_all_workflow_agents_skips_dead_and_completed(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    append_workflow_activity("conversation-start", "start", agent_session_id="11111", process_id=101)
    append_workflow_activity("conversation-start", "start", agent_session_id="22222", process_id=202)
    append_workflow_activity(
        "conversation-end",
        "done",
        agent_session_id="22222",
        process_id=202,
        completed=True,
    )
    append_workflow_activity("conversation-start", "start", agent_session_id="33333", process_id=303)

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("epflemma_cli.workflow_state.os.killpg", _fake_killpg)
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: pid == 101)

    result = terminate_all_workflow_agents()

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["11111"]
    assert killed == [101]


def test_terminate_project_workflow_agents_filters_by_project_root(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    project_a = str(tmp_path / "A")
    project_b = str(tmp_path / "B")
    append_workflow_activity("conversation-start", "start", agent_session_id="11111", process_id=101, project_root=project_a)
    append_workflow_activity("conversation-start", "start", agent_session_id="22222", process_id=202, project_root=project_b)

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("epflemma_cli.workflow_state.os.killpg", _fake_killpg)
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: True)

    result = terminate_project_workflow_agents(project_a)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["11111"]
    assert killed == [101]


def test_terminate_project_workflow_agents_skips_missing_project_root(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    project_a = str(tmp_path / "A")
    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "prove-run-a")
    append_workflow_activity("conversation-start", "start", agent_session_id="11111", process_id=101)
    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "prove-run-b")
    append_workflow_activity("conversation-start", "start", agent_session_id="22222", process_id=202, project_root=project_a)

    killed: list[int] = []

    def _fake_killpg(pid: int, sig: int) -> None:
        killed.append(pid)

    monkeypatch.setattr("epflemma_cli.workflow_state.os.killpg", _fake_killpg)
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: True)

    result = terminate_project_workflow_agents(project_a)

    assert result["success"] is True
    assert result["count"] == 1
    assert result["terminated"] == ["22222"]
    assert killed == [202]


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
        model="zai-org/GLM-5.1",
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


def test_workflow_agent_summary_prefers_live_busy_phase_over_conversation_end(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    append_workflow_activity(
        "conversation-end",
        "Agent conversation finished",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
        completed=True,
        api_calls=2,
    )
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "busy",
            "workflow_kind": "prove",
            "active_skill": "lean-theorem-queue-worker",
            "process_id": 24680,
        }
    )
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: True)

    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["agent_id"] == "agent-main"
    assert summaries[0]["status"] == "active"
    assert summaries[0]["finished_at"] == ""


def test_workflow_agent_summary_maps_live_stalled_phase_to_blocked(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "stalled",
            "workflow_kind": "prove",
            "active_skill": "lean-theorem-queue-worker",
            "process_id": 24680,
        }
    )
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: True)

    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["agent_id"] == "agent-main"
    assert summaries[0]["status"] == "blocked"


def test_background_workflow_conversation_end_is_not_terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "prove-background-test")

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    append_workflow_activity(
        "conversation-end",
        "Agent conversation finished",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
        completed=True,
    )

    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: True)
    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["status"] == "active"
    assert summaries[0]["finished_at"] == ""


def test_workflow_agent_summary_marks_dead_processes_dead(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    append_workflow_activity(
        "agent-awaiting-input",
        "Background workflow agent is waiting for input",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
        status="paused",
    )

    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: False)
    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["agent_id"] == "agent-main"
    assert summaries[0]["status"] == "dead"
    assert summaries[0]["finished_at"] != ""


def test_workflow_agent_summary_does_not_override_dead_process_with_live_phase(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-main",
        process_id=24680,
        workflow_kind="prove",
        active_skill="lean-theorem-queue-worker",
    )
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "busy",
            "workflow_kind": "prove",
            "active_skill": "lean-theorem-queue-worker",
            "process_id": 24680,
        }
    )

    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: False)
    summaries = summarize_workflow_agents(activity_limit=2)

    assert summaries[0]["status"] == "dead"


def test_load_workflow_live_status_marks_dead_runner_snapshot_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "paused",
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "process_id": 24680,
            "current_queue_item": {"label": "demo"},
        }
    )
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: False)

    payload = load_workflow_live_status()

    assert payload["phase"] == "dead"
    assert payload["process_id"] == 0
    assert payload["stale_process_id"] == 24680
    assert payload["stale_snapshot"] is True
    assert load_workflow_live_status()["phase"] == "dead"


def test_load_workflow_live_status_preserves_terminal_phase_for_dead_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    save_workflow_live_status(
        {
            "version": 1,
            "phase": "exited",
            "workflow_kind": "prove",
            "process_id": 24680,
        }
    )
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: False)

    payload = load_workflow_live_status()

    assert payload["phase"] == "exited"
    assert payload["process_id"] == 0
    assert payload["stale_process_id"] == 24680
    assert payload["stale_snapshot"] is True


def test_workflow_agent_summary_includes_multiple_run_streams(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "prove-run-a")
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="11111",
        process_id=101,
        workflow_kind="prove",
        workflow_command="/prove Main.lean",
        project_root=str(tmp_path / "A"),
    )
    monkeypatch.setenv("EPFLEMMA_WORKFLOW_RUN_ID", "prove-run-b")
    append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="22222",
        process_id=202,
        workflow_kind="prove",
        workflow_command="/prove Other.lean",
        project_root=str(tmp_path / "B"),
    )
    monkeypatch.setattr("epflemma_cli.workflow_state._process_seems_alive", lambda pid: True)

    summaries = summarize_workflow_agents(activity_limit=1)

    assert {summary["agent_id"] for summary in summaries} == {"11111", "22222"}
