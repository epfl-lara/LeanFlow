"""Tests for exact workflow-process ownership validation."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from core.process_identity import PROCESS_TOKEN_ENV, process_token_sha256
from leanflow_cli import workflow
from leanflow_cli.workflows import workflow_state


def test_spawn_workflow_mints_a_fresh_child_ownership_token(monkeypatch, tmp_path):
    captured_envs: list[dict[str, str]] = []
    plan = workflow.NativeLaunchPlan(
        project=SimpleNamespace(root=tmp_path),
        workflow=workflow.NativeWorkflowSpec(
            workflow_kind="prove",
            frontend_command="/prove",
            canonical_command="/prove",
            backend_command="/lean4:prove",
            workflow_args="Main.lean",
        ),
        runtime={},
        child_env={PROCESS_TOKEN_ENV: "inherited-parent-token"},
        argv=[sys.executable, "-m", "leanflow_cli.native.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="leanflow-native",
    )

    class _Process:
        pid = 24680

    def launch(*args, **kwargs):
        captured_envs.append(dict(kwargs["env"]))
        return _Process()

    monkeypatch.setattr(workflow, "resolve_workflow_request", lambda *args, **kwargs: plan)
    monkeypatch.setattr(workflow.subprocess, "Popen", launch)

    first_plan, _ = workflow.spawn_workflow(
        "/prove Main.lean", extra_env={PROCESS_TOKEN_ENV: "job-token"}
    )
    second_plan, _ = workflow.spawn_workflow("/prove Main.lean")

    first = captured_envs[0][PROCESS_TOKEN_ENV]
    second = captured_envs[1][PROCESS_TOKEN_ENV]
    assert first not in {"inherited-parent-token", "job-token"}
    assert second != "inherited-parent-token"
    assert first != second
    assert first_plan.child_env[PROCESS_TOKEN_ENV] == first
    assert second_plan.child_env[PROCESS_TOKEN_ENV] == second


def test_spawn_workflow_cannot_weaken_solution_boundary_with_extra_env(monkeypatch, tmp_path):
    captured_envs: list[dict[str, str]] = []
    plan = workflow.NativeLaunchPlan(
        project=SimpleNamespace(root=tmp_path),
        workflow=workflow.NativeWorkflowSpec(
            workflow_kind="prove",
            frontend_command="/prove",
            canonical_command="/prove",
            backend_command="/lean4:prove",
            workflow_args="IMO2026/P2.lean",
            clean_room=True,
            clean_room_labels=("P2", "IMO 2026 Problem 2"),
        ),
        runtime={},
        child_env={},
        argv=[sys.executable, "-m", "leanflow_cli.native.native_runner"],
        active_skill="lean-proof-loop",
        toolset_name="leanflow-native",
    )

    class _Process:
        pid = 24680

    def launch(*_args, **kwargs):
        captured_envs.append(dict(kwargs["env"]))
        return _Process()

    monkeypatch.setattr(workflow, "resolve_workflow_request", lambda *args, **kwargs: plan)
    monkeypatch.setattr(workflow.subprocess, "Popen", launch)

    launched, _ = workflow.spawn_workflow(
        "/prove IMO2026/P2.lean --clean-room",
        extra_env={
            "LEANFLOW_DISABLE_REPOSITORY_RESEARCH": "0",
            "LEANFLOW_DISABLE_SOLUTION_RESEARCH": "0",
            "LEANFLOW_CLEAN_ROOM_TASK_LABELS": "weakened",
        },
    )

    assert captured_envs[0]["LEANFLOW_DISABLE_REPOSITORY_RESEARCH"] == "0"
    assert captured_envs[0]["LEANFLOW_DISABLE_SOLUTION_RESEARCH"] == "1"
    assert captured_envs[0]["LEANFLOW_CLEAN_ROOM_TASK_LABELS"] == ("P2|IMO 2026 Problem 2")
    assert launched.child_env == captured_envs[0]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process sessions")
def test_interrupt_workflow_process_rejects_reused_identity_before_signal():
    token = "workflow-owner-token"
    env = dict(os.environ)
    env[PROCESS_TOKEN_ENV] = token
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
    )
    identity = {
        "process_id": process.pid,
        "process_group_id": process.pid,
        "process_session_id": process.pid,
        "process_token_sha256": process_token_sha256(token),
    }
    try:
        mismatched = dict(identity, process_token_sha256=process_token_sha256("reused-pid"))
        rejected = workflow_state.interrupt_workflow_process(mismatched)

        assert rejected["success"] is False
        assert "no longer matches" in rejected["error"]
        assert process.poll() is None

        interrupted = workflow_state.interrupt_workflow_process(identity)
        assert interrupted["success"] is True
        assert interrupted["identity_verified"] is True
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def test_interrupt_workflow_process_refuses_legacy_pid(monkeypatch):
    signalled: list[int] = []
    monkeypatch.setattr(workflow_state.os, "kill", lambda pid, sig: signalled.append(pid))

    result = workflow_state.interrupt_workflow_process({"process_id": 24680})

    assert result["success"] is False
    assert "identity is unavailable" in result["error"]
    assert signalled == []


def test_interrupt_workflow_process_revalidates_shared_group_as_pid_only(monkeypatch):
    identity = {
        "process_id": 24680,
        "process_group_id": 24000,
        "process_session_id": 23000,
        "process_token_sha256": "a" * 64,
    }
    signalled: list[tuple[str, int]] = []
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda current: True)
    monkeypatch.setattr(
        workflow_state.os,
        "killpg",
        lambda pid, sig: signalled.append(("group", pid)),
    )
    monkeypatch.setattr(
        workflow_state.os,
        "kill",
        lambda pid, sig: signalled.append(("pid", pid)),
    )

    result = workflow_state.interrupt_workflow_process(identity)

    assert result["success"] is True
    assert signalled == [("pid", 24680)]


def test_save_live_status_records_only_a_token_fingerprint(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(PROCESS_TOKEN_ENV, "do-not-persist-this-token")

    workflow_state.save_workflow_live_status(
        {"version": 1, "phase": "busy", "process_id": os.getpid()}
    )
    payload = workflow_state.load_workflow_live_status()

    assert payload["process_token_sha256"] == process_token_sha256("do-not-persist-this-token")
    assert "do-not-persist-this-token" not in str(payload)
    assert payload["process_group_id"] == os.getpgid(os.getpid())
    assert payload["process_session_id"] == os.getsid(os.getpid())


def test_live_status_marks_reused_pid_snapshot_stale(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    workflow_state.save_workflow_live_status(
        {
            "version": 1,
            "phase": "busy",
            "process_id": 24680,
            "process_group_id": 24680,
            "process_session_id": 24680,
            "process_token_sha256": "a" * 64,
            "held_locks": 2,
        }
    )
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda identity: False)
    monkeypatch.setattr(workflow_state, "_process_seems_alive", lambda pid: True)

    payload = workflow_state.load_workflow_live_status()

    assert payload["phase"] == "dead"
    assert payload["stale_snapshot"] is True
    assert payload["stale_process_id"] == 24680
    assert payload["process_id"] == 0
    assert payload["held_locks"] == 0


def test_historical_agent_identity_cannot_signal_a_reused_live_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    workflow_state.append_workflow_activity(
        "conversation-start",
        "Agent conversation started",
        agent_session_id="agent-old",
        process_id=24680,
        process_group_id=24680,
        process_session_id=24680,
        process_token_sha256="a" * 64,
    )
    monkeypatch.setattr(workflow_state, "process_identity_matches", lambda identity: False)
    monkeypatch.setattr(workflow_state, "_process_seems_alive", lambda pid: True)

    def unexpected_signal(_pid, _signal):
        raise AssertionError("a reused PID must never receive a workflow signal")

    monkeypatch.setattr(workflow_state.os, "kill", unexpected_signal)
    monkeypatch.setattr(workflow_state.os, "killpg", unexpected_signal)

    summary = workflow_state.summarize_workflow_agents(activity_limit=1)[0]
    termination = workflow_state.terminate_workflow_agent("agent-old")
    queued = workflow_state.enqueue_workflow_agent_message("agent-old", "exit", kind="exit")

    assert summary["status"] == "dead"
    assert termination["success"] is False
    assert "no longer matches" in termination["error"]
    assert queued["success"] is False


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process sessions")
def test_launch_token_is_visible_to_identity_probe():
    token = "probe-visible-token"
    env = dict(os.environ)
    env[PROCESS_TOKEN_ENV] = token
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 2
        while process.poll() is None and time.monotonic() < deadline:
            identity = {
                "process_id": process.pid,
                "process_group_id": process.pid,
                "process_session_id": process.pid,
                "process_token_sha256": process_token_sha256(token),
            }
            if workflow_state._workflow_process_identity_is_live(identity, require_verified=True):
                break
            time.sleep(0.02)
        assert workflow_state._workflow_process_identity_is_live(identity, require_verified=True)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
