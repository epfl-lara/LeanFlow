"""Tests for the agent swarm manager."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from swarm_manager import (
    SwarmManager,
    SwarmTask,
    _format_elapsed,
    _normalize_claude_session_launch,
    _parse_stream_event,
    _remember_recent_output,
    _replay_recent_output,
    _run_claude_code_background,
    _run_claude_code_interactive,
    attach_to_task,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure each test gets a fresh SwarmManager."""
    SwarmManager.reset()
    yield
    SwarmManager.reset()


class TestSwarmTaskDataclass:
    def test_defaults(self):
        t = SwarmTask(task_id="af-001", description="test", theorem="1+1=2")
        assert t.status == "queued"
        assert t.progress == "Waiting"
        assert t.thread is None
        assert t.session_id is None
        assert t.lean_status is None
        assert t.process is None


class TestSwarmManagerSingleton:
    def test_singleton_identity(self):
        a = SwarmManager()
        b = SwarmManager()
        assert a is b

    def test_reset_creates_new_instance(self):
        a = SwarmManager()
        SwarmManager.reset()
        b = SwarmManager()
        assert a is not b


class TestSpawn:
    def test_spawn_without_run_fn(self):
        mgr = SwarmManager()
        task = mgr.spawn(theorem="1+1=2", description="simple addition")
        assert task.task_id == "af-001"
        assert task.status == "queued"
        assert task.theorem == "1+1=2"
        assert task.description == "simple addition"
        assert task.thread is None

    def test_spawn_increments_ids(self):
        mgr = SwarmManager()
        t1 = mgr.spawn(theorem="a", description="first")
        t2 = mgr.spawn(theorem="b", description="second")
        t3 = mgr.spawn(theorem="c", description="third")
        assert t1.task_id == "af-001"
        assert t2.task_id == "af-002"
        assert t3.task_id == "af-003"

    def test_spawn_with_run_fn(self):
        mgr = SwarmManager()
        done = threading.Event()

        def worker(task):
            task.progress = "Working"
            done.set()

        task = mgr.spawn(theorem="x", description="bg test", run_fn=worker)
        done.wait(timeout=5)
        task.thread.join(timeout=5)

        assert task.status == "complete"
        assert task.progress == "Working"
        assert task.session_id is not None
        assert task.start_time is not None
        assert task.end_time is not None

    def test_spawn_run_fn_failure_sets_failed(self):
        mgr = SwarmManager()

        def failing_worker(task):
            raise RuntimeError("boom")

        task = mgr.spawn(theorem="x", description="fail test", run_fn=failing_worker)
        task.thread.join(timeout=5)

        assert task.status == "failed"
        assert task.error == "boom"
        assert task.end_time is not None

    def test_on_complete_callback_fires(self):
        mgr = SwarmManager()
        completed_ids = []
        mgr.set_on_complete(lambda t: completed_ids.append(t.task_id))

        def worker(task):
            task.progress = "done"

        task = mgr.spawn(theorem="x", description="cb test", run_fn=worker)
        task.thread.join(timeout=5)
        assert "af-001" in completed_ids


class TestSpawnClaude:
    def test_spawn_claude_starts_thread(self):
        mgr = SwarmManager()
        with patch("swarm_manager._run_claude_code_background") as mock_run:
            task = mgr.spawn_claude(
                theorem="test thm",
                description="test desc",
                argv=["echo", "hello"],
                cwd="/tmp",
                env={},
                workflow_kind="prove",
                workflow_command="/lean4:prove File.lean",
                project_name="Demo Project",
                project_root="/tmp/project",
                backend_name="codex",
            )
            task.thread.join(timeout=5)

        assert task.task_id == "af-001"
        assert task.session_id is not None
        assert task.workflow_kind == "prove"
        assert task.workflow_command == "/lean4:prove File.lean"
        assert task.project_name == "Demo Project"
        assert task.project_root == "/tmp/project"
        assert task.backend_name == "codex"
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] is task
        assert call_args[0][1] == ["echo", "hello"]


class TestClaudeLaunchNormalization:
    def test_claude_launch_forces_bypass_and_yolo_for_non_root(self):
        with patch("swarm_manager._is_effective_root", return_value=False):
            argv, env = _normalize_claude_session_launch(
                ["/usr/bin/claude", "--model", "sonnet", "--permission-mode", "acceptEdits", "prove this"],
                {"HOME": "/tmp/home"},
            )

        assert argv == [
            "/usr/bin/claude",
            "--dangerously-skip-permissions",
            "--model",
            "sonnet",
            "prove this",
        ]
        assert env["GAUSS_YOLO_MODE"] == "1"

    def test_claude_launch_uses_bypass_permissions_mode_for_root(self):
        with patch("swarm_manager._is_effective_root", return_value=True):
            argv, env = _normalize_claude_session_launch(
                ["/usr/bin/claude", "--model", "sonnet", "--permission-mode", "acceptEdits", "prove this"],
                {"HOME": "/tmp/home"},
            )

        assert argv == [
            "/usr/bin/claude",
            "--permission-mode",
            "bypassPermissions",
            "--model",
            "sonnet",
            "prove this",
        ]
        assert env["GAUSS_YOLO_MODE"] == "1"

    def test_non_claude_launch_is_unchanged(self):
        argv, env = _normalize_claude_session_launch(
            ["/usr/bin/codex", "--dangerously-bypass-approvals-and-sandbox"],
            {"HOME": "/tmp/home"},
        )

        assert argv == ["/usr/bin/codex", "--dangerously-bypass-approvals-and-sandbox"]
        assert env == {"HOME": "/tmp/home"}


class TestRecentOutputReplay:
    def test_replay_recent_output_writes_buffer(self):
        task = SwarmTask(task_id="af-001", description="replay", theorem="thm")
        _remember_recent_output(task, b"hello ")
        _remember_recent_output(task, b"world")

        with patch("swarm_manager.os.write") as mock_write:
            _replay_recent_output(task, 1)

        mock_write.assert_called_once_with(1, b"hello world")


class TestGetTask:
    def test_get_existing(self):
        mgr = SwarmManager()
        task = mgr.spawn(theorem="t", description="d")
        assert mgr.get_task("af-001") is task

    def test_get_missing(self):
        mgr = SwarmManager()
        assert mgr.get_task("af-999") is None

    def test_latest_task_returns_most_recent_match(self):
        mgr = SwarmManager()
        first = mgr.spawn(theorem="a", description="first")
        second = mgr.spawn(theorem="b", description="second")
        second.status = "running"
        second.pty_master_fd = 42
        first.status = "running"
        first.pty_master_fd = 41

        assert mgr.latest_task() is second
        assert mgr.latest_task(status="running", require_pty=True) is second

    def test_latest_task_skips_non_attachable_entries(self):
        mgr = SwarmManager()
        older = mgr.spawn(theorem="a", description="older")
        older.status = "running"
        older.pty_master_fd = 41
        newer = mgr.spawn(theorem="b", description="newer")
        newer.status = "running"

        assert mgr.latest_task(status="running", require_pty=True) is older


class TestListTasks:
    def test_list_all(self):
        mgr = SwarmManager()
        mgr.spawn(theorem="a", description="first")
        mgr.spawn(theorem="b", description="second")
        assert len(mgr.list_tasks()) == 2

    def test_list_filtered(self):
        mgr = SwarmManager()
        t1 = mgr.spawn(theorem="a", description="first")
        t2 = mgr.spawn(theorem="b", description="second")
        t1.status = "running"
        assert len(mgr.list_tasks(status="running")) == 1
        assert mgr.list_tasks(status="running")[0] is t1


class TestCancel:
    def test_cancel_queued(self):
        mgr = SwarmManager()
        task = mgr.spawn(theorem="t", description="d")
        assert mgr.cancel("af-001") is True
        assert task.status == "cancelled"
        assert task.end_time is not None

    def test_cancel_completed_returns_false(self):
        mgr = SwarmManager()
        task = mgr.spawn(theorem="t", description="d")
        task.status = "complete"
        assert mgr.cancel("af-001") is False

    def test_cancel_missing_returns_false(self):
        mgr = SwarmManager()
        assert mgr.cancel("af-999") is False

    def test_cancel_terminates_process(self):
        mgr = SwarmManager()
        task = mgr.spawn(theorem="t", description="d")
        task.status = "running"
        mock_proc = MagicMock()
        task.process = mock_proc
        mgr.cancel("af-001")
        mock_proc.terminate.assert_called_once()


class TestCounts:
    def test_empty(self):
        mgr = SwarmManager()
        assert mgr.counts() == {}

    def test_counts_grouping(self):
        mgr = SwarmManager()
        t1 = mgr.spawn(theorem="a", description="a")
        t2 = mgr.spawn(theorem="b", description="b")
        t3 = mgr.spawn(theorem="c", description="c")
        t1.status = "running"
        t2.status = "complete"
        c = mgr.counts()
        assert c == {"running": 1, "complete": 1, "queued": 1}


class TestFormatElapsed:
    def test_seconds(self):
        assert _format_elapsed(45) == "45s"

    def test_minutes(self):
        assert _format_elapsed(720) == "12m"

    def test_hours(self):
        assert _format_elapsed(5400) == "1h30m"


class TestParseStreamEvent:
    def test_result_event(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        event = json.dumps({"type": "result", "result": "Proof complete", "subtype": ""})
        _parse_stream_event(task, event)
        assert task.progress == "Session complete"
        assert task.result == "Proof complete"

    def test_tool_use_lean(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        event = json.dumps({
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "lean-lsp-diagnostics"},
        })
        _parse_stream_event(task, event)
        assert task.lean_status == "active"
        assert "lean-lsp" in task.progress

    def test_tool_use_non_lean(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        event = json.dumps({
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": "Edit"},
        })
        _parse_stream_event(task, event)
        assert task.progress == "Tool: Edit"

    def test_error_max_turns(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        event = json.dumps({"type": "result", "result": "", "subtype": "error_max_turns"})
        _parse_stream_event(task, event)
        assert task.progress == "Hit max turns"

    def test_malformed_json_ignored(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        _parse_stream_event(task, "not json at all")
        assert task.progress == "Waiting"

    def test_tool_result_verified(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        event = json.dumps({"type": "tool_result", "content": "No errors found"})
        _parse_stream_event(task, event)
        assert task.lean_status == "verified"

    def test_tool_result_sorry(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        event = json.dumps({"type": "tool_result", "content": "declaration uses sorry"})
        _parse_stream_event(task, event)
        assert task.lean_status == "has sorry"


class TestRunClaudeCodeBackground:
    def test_successful_run(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        _run_claude_code_background(
            task,
            argv=["echo", '{"type":"result","result":"done","subtype":""}'],
            cwd="/tmp",
            env={},
        )
        assert task.status == "complete"
        assert task.start_time is not None
        assert task.end_time is not None

    def test_failed_run_nonzero_exit(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        _run_claude_code_background(
            task,
            argv=["false"],
            cwd="/tmp",
            env={},
        )
        assert task.status == "failed"
        assert task.error is not None
        assert "exit 1" in task.error

    def test_missing_executable(self):
        task = SwarmTask(task_id="t", description="d", theorem="thm")
        _run_claude_code_background(
            task,
            argv=["nonexistent_program_xyz_12345"],
            cwd="/tmp",
            env={},
        )
        assert task.status == "failed"
        assert "not found" in task.error


class TestRenderTable:
    def test_empty_table(self):
        mgr = SwarmManager()
        table = mgr.render_table()
        assert table.title is not None
        assert table.row_count == 0

    def test_table_has_rows(self):
        mgr = SwarmManager()
        mgr.spawn(theorem="a", description="first")
        mgr.spawn(theorem="b", description="second")
        table = mgr.render_table()
        assert table.row_count == 2

    def test_table_columns(self):
        mgr = SwarmManager()
        table = mgr.render_table()
        col_names = [c.header for c in table.columns]
        assert "ID" in col_names
        assert "Workflow" in col_names
        assert "Project" in col_names
        assert "Status" in col_names
        assert "Lean" in col_names


class TestRenderDetail:
    def test_missing_task(self):
        mgr = SwarmManager()
        assert mgr.render_detail("af-999") is None

    def test_detail_for_existing_task(self):
        mgr = SwarmManager()
        task = mgr.spawn(
            theorem="Fermat",
            description="FLT n=5",
            workflow_kind="autoprove",
            workflow_command="/lean4:autoprove Fermat.lean",
            project_name="Fermat",
            project_root="/tmp/fermat",
            backend_name="claude-code",
        )
        task.status = "running"
        task.start_time = time.time() - 120
        task.lean_status = "checking"
        detail = mgr.render_detail("af-001")
        assert detail is not None
        assert detail.row_count >= 11


class TestSummaryLine:
    def test_empty_returns_none(self):
        mgr = SwarmManager()
        assert mgr.summary_line() is None

    def test_with_tasks(self):
        mgr = SwarmManager()
        t1 = mgr.spawn(theorem="a", description="a")
        t2 = mgr.spawn(theorem="b", description="b")
        t1.status = "running"
        t2.status = "complete"
        line = mgr.summary_line()
        assert line is not None
        assert "1 agent running" in line
        assert "1 complete" in line

    def test_all_queued(self):
        mgr = SwarmManager()
        mgr.spawn(theorem="a", description="a")
        line = mgr.summary_line()
        assert line is not None
        assert "1 queued" in line


class TestStatusBarFragment:
    def test_empty_returns_none(self):
        mgr = SwarmManager()
        assert mgr.status_bar_fragment() is None

    def test_running_tasks(self):
        mgr = SwarmManager()
        t1 = mgr.spawn(theorem="a", description="a")
        t2 = mgr.spawn(theorem="b", description="b")
        t1.status = "running"
        frag = mgr.status_bar_fragment()
        assert frag == "af:1/2"

    def test_no_running_tasks(self):
        mgr = SwarmManager()
        t1 = mgr.spawn(theorem="a", description="a")
        t1.status = "complete"
        frag = mgr.status_bar_fragment()
        assert frag == "af:1"


class TestSpawnInteractive:
    def test_spawn_interactive_creates_task_and_thread(self):
        mgr = SwarmManager()
        with patch("swarm_manager._run_claude_code_interactive") as mock_run:
            task = mgr.spawn_interactive(
                theorem="a + b = b + a",
                description="commutativity test",
                argv=["echo", "hello"],
                cwd="/tmp",
                env={},
                workflow_kind="prove",
                workflow_command="/lean4:prove Add.lean",
                project_name="Algebra",
                project_root="/tmp/algebra",
                backend_name="claude-code",
            )
        assert task.task_id == "af-001"
        assert task.description == "commutativity test"
        assert task.workflow_kind == "prove"
        assert task.workflow_command == "/lean4:prove Add.lean"
        assert task.project_name == "Algebra"
        assert task.project_root == "/tmp/algebra"
        assert task.backend_name == "claude-code"
        assert task.thread is not None
        assert task.session_id is not None
        task.thread.join(timeout=2)

    def test_render_table_shows_attach_hint_for_pty_tasks(self):
        mgr = SwarmManager()
        task = mgr.spawn(theorem="t", description="test pty")
        task.status = "running"
        task.pty_master_fd = 42
        table = mgr.render_table()
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        Console(file=buf, force_terminal=True, width=120).print(table)
        output = buf.getvalue()
        assert "attach" in output

    def test_render_detail_shows_attach_for_pty_task(self):
        mgr = SwarmManager()
        task = mgr.spawn(theorem="t", description="detail test")
        task.status = "running"
        task.pty_master_fd = 42
        table = mgr.render_detail(task.task_id)
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        Console(file=buf, force_terminal=True, width=120).print(table)
        output = buf.getvalue()
        assert "/swarm attach" in output
        assert "Ctrl-]" in output

    def test_render_detail_no_attach_for_non_pty_task(self):
        mgr = SwarmManager()
        task = mgr.spawn(theorem="t", description="no pty test")
        task.status = "running"
        table = mgr.render_detail(task.task_id)
        from io import StringIO
        from rich.console import Console
        buf = StringIO()
        Console(file=buf, force_terminal=True, width=120).print(table)
        output = buf.getvalue()
        assert "/swarm attach" not in output

    def test_attach_to_task_raises_when_no_pty(self):
        task = SwarmTask(task_id="af-999", description="no pty", theorem="x")
        task.status = "running"
        with pytest.raises(RuntimeError, match="no PTY"):
            attach_to_task(task)


class TestInteractiveRunner:
    def test_interactive_runner_with_echo(self):
        """Verify _run_claude_code_interactive works with a simple command."""
        import os
        task = SwarmTask(task_id="af-test", description="echo test", theorem="x")
        _run_claude_code_interactive(
            task,
            argv=["echo", "hello interactive"],
            cwd=os.getcwd(),
            env=dict(os.environ),
        )
        assert task.status == "complete"
        assert task.pty_master_fd is None
        assert task._output_lines is not None
        assert task._recent_output is not None
        combined = "\n".join(task._output_lines)
        assert "hello interactive" in combined
        assert b"hello interactive" in bytes(task._recent_output)


class TestThreadSafety:
    def test_concurrent_spawns(self):
        mgr = SwarmManager()
        errors = []

        def spawn_many(start: int):
            try:
                for i in range(20):
                    mgr.spawn(theorem=f"thm-{start}-{i}", description=f"task-{start}-{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=spawn_many, args=(n,)) for n in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert len(mgr.list_tasks()) == 100
