from __future__ import annotations

import os
from itertools import chain, repeat

from epflemma_cli import native_runner as runner
from epflemma_cli.workflow_state import read_workflow_activity


class _FakeCompressor:
    threshold_tokens = 100
    protect_first_n = 1
    protect_last_n = 2
    summary_target_tokens = 2500
    summary_model = ""

    def _align_boundary_forward(self, messages, idx):
        return idx

    def _align_boundary_backward(self, messages, idx):
        return idx

    def _sanitize_tool_pairs(self, messages):
        return messages


class _FakeCheckpointManager:
    def __init__(self):
        self.enabled = True

    def ensure_checkpoint(self, working_dir, reason):
        return True

    def list_checkpoints(self, working_dir):
        return [{"hash": "abc123def456"}]

    def restore(self, working_dir, commit_hash, file_path=None):
        return {"success": True, "restored_to": commit_hash[:8], "reason": "milestone"}


class _FakeAgent:
    compression_enabled = True

    def __init__(self):
        self.context_compressor = _FakeCompressor()
        self._checkpoint_mgr = _FakeCheckpointManager()


def test_run_managed_conversation_passes_through_result():
    class _Agent:
        def run_conversation(self, **kwargs):
            return {"messages": [], "interrupted": False, "kwargs": kwargs}

    result = runner._run_managed_conversation(_Agent(), user_message="hello", persist_user_message="hello")

    assert result["interrupted"] is False
    assert result["kwargs"]["user_message"] == "hello"


def test_run_managed_conversation_interrupts_on_ctrl_c(monkeypatch, capsys):
    class _Agent:
        def __init__(self):
            self.interrupt_calls = 0

        def interrupt(self):
            self.interrupt_calls += 1

        def run_conversation(self, **kwargs):
            return {"messages": [{"role": "assistant", "content": "partial"}], "interrupted": True}

    agent = _Agent()

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            self._target = target
            self._alive = True
            self._raised = False

        def start(self):
            return None

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            if not self._raised:
                self._raised = True
                raise KeyboardInterrupt
            if self._target is not None:
                self._target()
            self._alive = False

    monkeypatch.setattr(runner.threading, "Thread", _FakeThread)

    result = runner._run_managed_conversation(agent, user_message="hello")

    assert agent.interrupt_calls == 1
    assert result["interrupted"] is True
    output = capsys.readouterr().out
    assert "Interrupt requested" in output
    assert "Returned to prover-agent mode after interrupt." in output


def test_run_managed_conversation_returns_interrupted_result_when_no_payload_arrives_after_interrupt(monkeypatch, capsys):
    class _Agent:
        def __init__(self):
            self.interrupt_calls = 0
            self._session_messages = [{"role": "assistant", "content": "partial"}]

        def interrupt(self):
            self.interrupt_calls += 1

        def clear_interrupt(self):
            return None

        def run_conversation(self, **kwargs):
            return None

    agent = _Agent()

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            self._target = target
            self._alive = True
            self._raised = False

        def start(self):
            return None

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            if not self._raised:
                self._raised = True
                raise KeyboardInterrupt
            self._alive = False

    monkeypatch.setattr(runner.threading, "Thread", _FakeThread)

    result = runner._run_managed_conversation(agent, user_message="hello")

    assert agent.interrupt_calls == 1
    assert result["interrupted"] is True
    assert result["messages"] == [{"role": "assistant", "content": "partial"}]
    output = capsys.readouterr().out
    assert "Returned to prover-agent mode after interrupt." in output


def test_run_managed_conversation_converts_worker_interrupted_error(monkeypatch, capsys):
    class _Agent:
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]

        def clear_interrupt(self):
            return None

        def run_conversation(self, **kwargs):
            raise InterruptedError("interrupted")

    agent = _Agent()

    result = runner._run_managed_conversation(agent, user_message="hello")

    assert result["interrupted"] is True
    assert result["messages"] == [{"role": "assistant", "content": "partial"}]
    output = capsys.readouterr().out
    assert "Returned to prover-agent mode after interrupt." in output


def test_run_managed_conversation_calls_interrupt_callback(monkeypatch):
    class _Agent:
        def __init__(self):
            self.interrupt_calls = 0

        def interrupt(self):
            self.interrupt_calls += 1

        def run_conversation(self, **kwargs):
            return {"messages": [], "interrupted": True}

    agent = _Agent()
    callback_hits = {"count": 0}

    class _FakeThread:
        def __init__(self, target=None, daemon=None):
            self._target = target
            self._alive = True
            self._raised = False

        def start(self):
            return None

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            if not self._raised:
                self._raised = True
                raise KeyboardInterrupt
            if self._target is not None:
                self._target()
            self._alive = False

    monkeypatch.setattr(runner.threading, "Thread", _FakeThread)

    runner._run_managed_conversation(
        agent,
        user_message="hello",
        on_interrupt=lambda: callback_hits.__setitem__("count", callback_hits["count"] + 1),
    )

    assert agent.interrupt_calls == 1
    assert callback_hits["count"] == 1


def test_background_control_loop_processes_queued_prompt_and_remote_exit(monkeypatch):
    class _Agent:
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    agent = _Agent()
    recorded: list[tuple[str, str, dict[str, object]]] = []
    persisted: list[str] = []
    queue_reads = iter(
        [
            [{"seq": 1, "kind": "message", "text": "Try another proof."}],
            [{"seq": 1, "kind": "message", "text": "Try another proof."}, {"seq": 2, "kind": "exit", "text": "exit"}],
        ]
    )

    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("EPFLEMMA_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_MODE", "chat")

    monkeypatch.setattr(runner, "_persist_live_status", lambda *args, phase=None, **kwargs: persisted.append(str(phase or "")))
    monkeypatch.setattr(runner, "_record_activity", lambda event_type, message, **details: recorded.append((event_type, message, details)))
    monkeypatch.setattr(runner, "read_workflow_agent_inbox", lambda agent_id: next(queue_reads))
    monkeypatch.setattr(runner, "_build_live_proof_state", lambda history, checkpoint_state: {"message": "ok"})
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: dict(live_state, verified=False))
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda live_state: False)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_auto_compact_history", lambda history, agent, force=False: (history, {"compacted": False}))
    monkeypatch.setattr(
        runner,
        "_run_managed_conversation",
        lambda *args, **kwargs: {"messages": [{"role": "assistant", "content": "done"}], "interrupted": False},
    )
    monkeypatch.setattr(runner, "_record_turn_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_journal_status", lambda: {"count": 0, "current": {}})
    monkeypatch.setattr(runner, "_drive_autonomous_followups", lambda *args, **kwargs: (args[2], {"compacted": False}, {"count": 0, "current": {}}, {"verified": False}))
    monkeypatch.setattr(runner.time, "sleep", lambda *_args, **_kwargs: None)

    result = runner._run_background_control_loop(
        agent,
        "system",
        [{"role": "assistant", "content": "start"}],
        {"compacted": False},
        {"count": 0, "current": {}},
        {"verified": True},
        {},
    )

    assert result == 0
    assert any(event_type == "agent-resume" and details.get("text") == "Try another proof." for event_type, _, details in recorded)
    assert any(event_type == "runner-exit" for event_type, _, _ in recorded)
    assert "busy" in persisted
    assert "exited" in persisted


def test_background_control_loop_exits_after_verified_completion(monkeypatch):
    class _Agent:
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    agent = _Agent()
    recorded: list[tuple[str, str, dict[str, object]]] = []
    persisted: list[str] = []

    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("EPFLEMMA_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_MODE", "chat")

    monkeypatch.setattr(runner, "_persist_live_status", lambda *args, phase=None, **kwargs: persisted.append(str(phase or "")))
    monkeypatch.setattr(runner, "_record_activity", lambda event_type, message, **details: recorded.append((event_type, message, details)))
    monkeypatch.setattr(runner, "read_workflow_agent_inbox", lambda agent_id: [{"seq": 1, "kind": "message", "text": "Finish the proof."}])
    monkeypatch.setattr(runner, "_build_live_proof_state", lambda history, checkpoint_state: {"message": "ok"})
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: dict(live_state, verified=bool(live_state.get("verified"))))
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda live_state: bool(live_state.get("verified")))
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_auto_compact_history", lambda history, agent, force=False: (history, {"compacted": False}))
    monkeypatch.setattr(
        runner,
        "_run_managed_conversation",
        lambda *args, **kwargs: {"messages": [{"role": "assistant", "content": "done"}], "interrupted": False},
    )
    monkeypatch.setattr(runner, "_record_turn_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_journal_status", lambda: {"count": 0, "current": {}})
    monkeypatch.setattr(
        runner,
        "_drive_autonomous_followups",
        lambda *args, **kwargs: (args[2], {"compacted": False}, {"count": 0, "current": {}}, {"verified": True}),
    )

    result = runner._run_background_control_loop(
        agent,
        "system",
        [{"role": "assistant", "content": "start"}],
        {"compacted": False},
        {"count": 0, "current": {}},
        {"verified": False},
        {},
    )

    assert result == 0
    assert any(event_type == "agent-resume" and details.get("text") == "Finish the proof." for event_type, _, details in recorded)
    assert any(event_type == "runner-exit" and "verified completion" in message for event_type, message, _ in recorded)
    assert "busy" in persisted
    assert "exited" in persisted


def test_terminate_descendant_agents_records_shutdown_activity(monkeypatch):
    class _Agent:
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    recorded: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("EPFLEMMA_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_MODE", "chat")
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
    )
    monkeypatch.setattr(
        runner,
        "terminate_workflow_agent_descendants",
        lambda agent_id: {"success": True, "count": 2, "terminated": ["22222", "33333"], "failed": []},
    )

    runner._terminate_descendant_agents(_Agent())

    assert any(event_type == "descendants-terminated" for event_type, _, _ in recorded)


def test_terminate_other_agents_records_shutdown_activity(monkeypatch):
    class _Agent:
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    recorded: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("EPFLEMMA_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_MODE", "chat")
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
    )
    monkeypatch.setattr(
        runner,
        "terminate_project_workflow_agents",
        lambda project_root, **kwargs: {"success": True, "count": 2, "terminated": ["22222", "33333"], "failed": []},
    )

    runner._terminate_other_agents(_Agent())

    assert any(event_type == "agents-terminated" for event_type, _, _ in recorded)


def test_background_runner_exits_immediately_after_verified_completion(monkeypatch):
    class _Agent:
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    recorded: list[tuple[str, str, dict[str, object]]] = []
    persisted: list[str] = []

    monkeypatch.setenv("EPFLEMMA_NATIVE_INTERACTIVE", "0")
    monkeypatch.setattr(runner, "_install_workflow_run_log_capture", lambda: None)
    monkeypatch.setattr(runner, "_build_agent", lambda: _Agent())
    monkeypatch.setattr(runner, "_managed_system_prompt", lambda: "system")
    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_print_header", lambda: None)
    monkeypatch.setattr(runner, "_startup_user_message", lambda resumed, **kwargs: "start")
    monkeypatch.setattr(runner, "_attach_live_proof_state", lambda text, live_state: text)
    monkeypatch.setattr(runner, "_run_managed_conversation", lambda *args, **kwargs: {"messages": [{"role": "assistant", "content": "done"}], "interrupted": False})
    monkeypatch.setattr(runner, "_record_turn_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_drive_autonomous_followups",
        lambda *args, **kwargs: (args[2], {"compacted": False}, {}, {"active_file": "/tmp/project/Main.lean", "diagnostics": "no errors found", "goals": "no goals", "sorry_count": 0, "project_sorry_count": 0, "verification_ok": True}),
    )
    monkeypatch.setattr(runner, "_build_live_proof_state", lambda history, checkpoint_state: {"active_file": "/tmp/project/Main.lean", "diagnostics": "no errors found", "goals": "no goals", "sorry_count": 0, "project_sorry_count": 0})
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: dict(live_state, verification_ok=True))
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda live_state: bool(live_state.get("verification_ok")))
    monkeypatch.setattr(runner, "_terminate_descendant_agents", lambda agent: recorded.append(("terminate", "descendants", {})))
    monkeypatch.setattr(runner, "_persist_live_status", lambda *args, phase=None, **kwargs: persisted.append(str(phase or "")))
    monkeypatch.setattr(runner, "_record_activity", lambda event_type, message, **details: recorded.append((event_type, message, details)))

    assert runner.main() == 0
    assert "exited" in persisted
    assert any(event_type == "terminate" for event_type, _, _ in recorded)
    assert any(event_type == "runner-exit" and "verified completion" in message for event_type, message, _ in recorded)
    assert any(
        event_type == "runner-start" and details.get("agent_session_id") == "12345" and details.get("process_id")
        for event_type, _, details in recorded
    )


def test_background_control_loop_handles_keyboard_interrupt_cleanly(monkeypatch):
    class _Agent:
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    recorded: list[tuple[str, str, dict[str, object]]] = []
    persisted: list[str] = []

    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("EPFLEMMA_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_MODE", "chat")
    monkeypatch.setattr(runner, "_persist_live_status", lambda *args, phase=None, **kwargs: persisted.append(str(phase or "")))
    monkeypatch.setattr(runner, "_record_activity", lambda event_type, message, **details: recorded.append((event_type, message, details)))
    monkeypatch.setattr(runner, "read_workflow_agent_inbox", lambda agent_id: [])
    monkeypatch.setattr(runner.time, "sleep", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))

    result = runner._run_background_control_loop(
        _Agent(),
        "system",
        [{"role": "assistant", "content": "start"}],
        {"compacted": False},
        {"count": 0, "current": {}},
        {"verified": False},
        {},
    )

    assert result == 0
    assert any(event_type == "runner-exit" and "interrupted by signal" in message for event_type, message, _ in recorded)
    assert "exited" in persisted


def test_workflow_startup_guidance_mentions_autonomous_loop():
    text = runner._workflow_startup_guidance("prove", "/prove Main.lean")

    assert "autonomous proving session" in text
    assert "/prove Main.lean" in text
    assert "lean_capabilities" in text
    assert "lean_worker_dispatch" in text


def test_workflow_startup_guidance_mentions_user_approved_swarm(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_PARALLEL_AGENTS", "3")
    monkeypatch.setenv("EPFLEMMA_NATIVE_USER_APPROVED_SWARM", "1")

    text = runner._workflow_startup_guidance("prove", "/prove Main.lean")

    assert "User-approved swarm mode" in text
    assert "3 agents total" in text


def test_record_managed_reasoning_policy_emits_auditable_fields(monkeypatch):
    recorded = []

    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
    )

    runner._record_managed_reasoning_policy(
        {
            "current_queue_item": {"label": "demo_theorem"},
            "active_file": "/tmp/project/Main.lean",
        },
        {
            "failed_attempts": [
                {"target_symbol": "demo_theorem", "active_file": "/tmp/project/Main.lean"},
                {"target_symbol": "demo_theorem", "active_file": "/tmp/project/Main.lean"},
                {"target_symbol": "other_theorem", "active_file": "/tmp/project/Main.lean"},
            ]
        },
        {"enabled": True, "effort": "high"},
        phase="autonomous",
        cycle=3,
    )

    assert recorded == [
        (
            "managed-reasoning-policy",
            "Managed reasoning policy applied: high",
            {
                "phase": "autonomous",
                "target_symbol": "demo_theorem",
                "active_file": "/tmp/project/Main.lean",
                "failed_attempt_count": 2,
                "effective_reasoning_effort": "high",
                "reasoning_enabled": True,
                "cycle": 3,
            },
        )
    ]


def test_startup_user_message_snapshot_with_runner_lean_prompt(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_RUNNER_LEAN_PROMPT", "1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_FILE", "/tmp/project/Main.lean")
    monkeypatch.setattr(runner, "_project_root", lambda: "/tmp/project")
    monkeypatch.setattr(
        runner,
        "build_skill_prompt",
        lambda name, cwd=None: "[SKILL]\n[WORKFLOW SPEC: prove]\npolicy body",
    )
    monkeypatch.setattr(
        runner,
        "route_workflow_step",
        lambda *args, **kwargs: type(
            "_Route",
            (),
            {
                "to_dict": lambda self: {
                    "skill_name": "lean-theorem-queue-worker",
                    "route_action": "queue-worker",
                    "blocker_kind": "compiler",
                    "recommended_worker": "proof-repair",
                    "reason": "queue item active",
                }
            },
        )(),
    )
    monkeypatch.setattr(
        runner,
        "_queue_assignment_block",
        lambda live_state, autonomy_state=None: "Assigned queue item:\n- declaration: foo",
    )

    text = runner._startup_user_message(
        live_state={"current_queue_item": {"label": "foo"}},
        autonomy_state={},
    )

    assert text == (
        "Begin the requested autonomous proving session now.\n\n"
        "Workflow request: /prove Main.lean\n"
        "Execution guidance: Load the native proving contract from the active skill/spec, begin with `lean_capabilities` and `lean_inspect`, "
        "use `lean_search` before guessing, and use `lean_worker_dispatch` only when the route recommends it; the live queue, route decision, "
        "and verification gate below are the state for this turn.\n\n"
        "Route decision:\n"
        "- skill: lean-theorem-queue-worker\n"
        "- action: queue-worker\n"
        "- blocker kind: compiler\n"
        "- reason: queue item active\n"
        "- recommended worker: proof-repair\n"
        "- use `lean_worker_dispatch` if the next attempt confirms this route\n\n"
        "Assigned queue item:\n"
        "- declaration: foo\n\n"
        "[SKILL]\n"
        "[WORKFLOW SPEC: prove]\n"
        "policy body"
    )


def test_autonomous_continuation_prompt_snapshot_with_runner_lean_prompt(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_RUNNER_LEAN_PROMPT", "1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setattr(runner, "_project_root", lambda: "/tmp/project")
    monkeypatch.setattr(
        runner,
        "route_workflow_step",
        lambda *args, **kwargs: type(
            "_Route",
            (),
            {
                "to_dict": lambda self: {
                    "skill_name": "lean-theorem-queue-worker",
                    "route_action": "queue-worker",
                    "blocker_kind": "compiler",
                    "recommended_worker": "proof-repair",
                    "reason": "queue item active",
                }
            },
        )(),
    )
    monkeypatch.setattr(
        runner,
        "_queue_assignment_block",
        lambda live_state, autonomy_state=None: "Assigned queue item:\n- declaration: foo",
    )
    monkeypatch.setattr(runner, "_queue_needs_final_file_sweep", lambda live_state: False)
    monkeypatch.setattr(runner, "_recent_failed_attempts_summary", lambda *args, **kwargs: "Recent failed attempts:\n- same blocker twice")

    text = runner._autonomous_continuation_prompt(
        {
            "declaration_scope": "file",
            "active_file": "/tmp/project/Main.lean",
            "verification_hint": "`lean_inspect` on Main.lean, then `lake env lean Main.lean`",
            "current_queue_item": {"label": "foo"},
        },
        3,
        autonomy_state={},
    )

    assert text == (
        "Continue the autonomous workflow.\n\n"
        "Follow the loaded native workflow spec as the policy manual. "
        "Use the refreshed live proof state below as the current turn state.\n\n"
        "This is autonomous continuation cycle 3.\n"
        "Current verification gate: `lean_inspect` on Main.lean, then `lake env lean Main.lean`\n"
        "Do not stop until that gate is satisfied or you have a concrete blocker to report.\n\n"
        "Route decision:\n"
        "- skill: lean-theorem-queue-worker\n"
        "- action: queue-worker\n"
        "- blocker kind: compiler\n"
        "- reason: queue item active\n"
        "- recommended worker: proof-repair\n"
        "- use `lean_worker_dispatch` if the blocker still fits this route after the next focused attempt\n\n"
        "Recent failed attempts:\n"
        "- same blocker twice\n\n"
        "Assigned queue item:\n"
        "- declaration: foo"
    )


def test_history_status_lines_summarize_message_counts(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", "/tmp/project")

    monkeypatch.setattr(
        runner,
        "summarize_workflow_agents",
        lambda activity_limit=1: [
            {"status": "active"},
            {"status": "paused"},
            {"status": "dead"},
        ],
    )

    lines = runner._history_status_lines(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "working"},
            {"role": "tool", "content": "output"},
            {"role": "tool", "content": "more"},
        ]
    )

    assert "Messages: 4" in lines
    assert "Users: 1" in lines
    assert "Assistants: 1" in lines
    assert "Tools: 2" in lines
    assert "Workflow: prove" in lines
    assert "Agents: 3 total / 2 live / 1 active / 1 dead" in lines


def test_build_agent_uses_epflemma_native_toolset(monkeypatch):
    captured = {}

    class DummyAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.reasoning_config = kwargs.get("reasoning_config")

    monkeypatch.setattr(runner, "AIAgent", DummyAgent)
    monkeypatch.setattr(
        runner,
        "_agent_config",
        lambda: {
            "reasoning_effort": "auto",
            "seed": 42,
            "temperature": 0.3,
            "top_p": None,
            "top_k": None,
            "min_p": None,
        },
    )
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_KEY", "sk-test")
    monkeypatch.setenv("EPFLEMMA_NATIVE_PROVIDER", "zai")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_MODE", "responses")
    monkeypatch.setenv("AGENT_MAX_TURNS", "77")

    runner._build_agent()

    assert captured["enabled_toolsets"] == ["epflemma-native"]
    assert captured["provider"] == "zai"
    assert captured["api_mode"] == "responses"
    assert captured["max_iterations"] == 77
    assert captured["reasoning_config"] == {"mode": "auto"}
    assert captured["seed"] == 42
    assert captured["temperature"] == 0.3
    assert captured["top_p"] is None
    assert captured["top_k"] is None
    assert captured["min_p"] is None
    assert callable(captured["tool_progress_callback"])
    assert callable(captured["step_callback"])


def test_build_agent_uses_swarm_toolset_when_user_enabled_swarm(monkeypatch):
    captured = {}

    class DummyAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = "runner-session"

    monkeypatch.setattr(runner, "AIAgent", DummyAgent)
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_KEY", "sk-test")
    monkeypatch.setenv("EPFLEMMA_NATIVE_PROVIDER", "zai")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_MODE", "responses")
    monkeypatch.setenv("EPFLEMMA_NATIVE_TOOLSET", "epflemma-native-swarm")

    runner._build_agent()

    assert captured["enabled_toolsets"] == ["epflemma-native-swarm"]
    assert os.getenv("EPFLEMMA_NATIVE_RUNNER_OWNER", "") == "runner-session"


def test_resolve_managed_reasoning_config_auto_defaults_to_medium_for_new_theorem():
    resolved = runner._resolve_managed_reasoning_config(
        {"mode": "auto"},
        {
            "active_file": "/tmp/project/Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "target_symbol": "demo",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        },
        {"failed_attempts": []},
    )

    assert resolved == {"enabled": True, "effort": "medium"}


def test_resolve_managed_reasoning_config_auto_escalates_after_five_failed_attempts():
    resolved = runner._resolve_managed_reasoning_config(
        {"mode": "auto"},
        {
            "active_file": "/tmp/project/Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "target_symbol": "demo",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        },
        {
            "failed_attempts": [
                {
                    "attempt": i + 1,
                    "cycle": i + 1,
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "intro x",
                    "reason": "blocked",
                }
                for i in range(5)
            ]
        },
    )

    assert resolved == {"enabled": True, "effort": "high"}


def test_resolve_managed_reasoning_config_auto_uses_high_for_final_file_sweep(monkeypatch):
    monkeypatch.setattr(runner, "_queue_needs_final_file_sweep", lambda live_state: True)

    resolved = runner._resolve_managed_reasoning_config(
        {"mode": "auto"},
        {
            "active_file": "/tmp/project/Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "target_symbol": "",
            "current_queue_item": {},
        },
        {"failed_attempts": []},
    )

    assert resolved == {"enabled": True, "effort": "high"}


def test_apply_managed_reasoning_policy_resets_to_medium_on_theorem_transition():
    class _Agent:
        def __init__(self):
            self._managed_base_reasoning_config = {"mode": "auto"}
            self.reasoning_config = None

    agent = _Agent()
    autonomy_state = {
        "failed_attempts": [
            {
                "attempt": i + 1,
                "cycle": i + 1,
                "target_symbol": "first_demo",
                "active_file": "Demo/Main.lean",
                "proof_shape": "intro x",
                "reason": "blocked",
            }
            for i in range(5)
        ]
    }

    first = runner._apply_managed_reasoning_policy(
        agent,
        {
            "active_file": "/tmp/project/Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "target_symbol": "first_demo",
            "current_queue_item": {"label": "first_demo", "reasons": ["contains sorry"]},
        },
        autonomy_state,
    )
    second = runner._apply_managed_reasoning_policy(
        agent,
        {
            "active_file": "/tmp/project/Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "target_symbol": "second_demo",
            "current_queue_item": {"label": "second_demo", "reasons": ["contains sorry"]},
        },
        autonomy_state,
    )

    assert first == {"enabled": True, "effort": "high"}
    assert second == {"enabled": True, "effort": "medium"}
    assert agent.reasoning_config == {"enabled": True, "effort": "medium"}


def test_tool_progress_callback_persists_structured_events(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "lean-proof-loop")
    runner._CURRENT_AGENT_ACTIVITY_DETAILS = {"agent_session_id": "12345", "delegate_depth": 0}

    runner._tool_progress_callback("terminal", "Run lake build", {"command": "lake build"})
    runner._tool_progress_callback("_thinking", "Inspecting theorem and diagnostics")
    runner._step_callback(3, ["search_files", "terminal"])

    events = read_workflow_activity(limit=3)

    assert [event["type"] for event in events] == ["tool-start", "assistant-plan", "api-call"]
    assert events[0]["agent_id"] == "12345"
    assert events[0]["task_label"] == "prove"
    assert events[0]["details"]["tool"] == "terminal"
    assert events[2]["details"]["iteration"] == 3


def test_compact_history_creates_snapshot_and_reduces_history(monkeypatch):
    monkeypatch.setattr(runner, "_generate_managed_snapshot", lambda compressor, turns: "snapshot")

    history = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "step 1"},
        {"role": "tool", "content": "x" * 900},
        {"role": "user", "content": "keep going"},
        {"role": "assistant", "content": "step 2"},
        {"role": "user", "content": "finish"},
        {"role": "assistant", "content": "latest"},
    ]

    compacted, status = runner._compact_history(history, _FakeCompressor())

    assert status["compacted"] is True
    assert status["snapshot_created"] is True
    assert len(compacted) < len(history)
    assert any(message.get("content") == "snapshot" for message in compacted)


def test_auto_compact_history_prunes_old_tool_output(monkeypatch):
    monkeypatch.setattr(runner, "estimate_messages_tokens_rough", lambda messages: 123)

    class _Agent:
        compression_enabled = False
        context_compressor = _FakeCompressor()

    history, status = runner._auto_compact_history(
        [
            {"role": "tool", "content": "x" * 5000},
            {"role": "user", "content": "recent 1"},
            {"role": "user", "content": "recent 2"},
        ],
        _Agent(),
    )

    assert status["reason"] == "disabled"
    assert status["compacted"] is False
    assert history[0]["content"].endswith("[epflemma-native pruned older tool output to preserve context budget]")


def test_count_project_sorries_ignores_dependencies_and_build_dirs(tmp_path):
    project = tmp_path / "Demo"
    (project / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    (project / "build" / "ir").mkdir(parents=True)
    (project / ".epflemma" / "runtime").mkdir(parents=True)
    (project / "Demo").mkdir(parents=True)
    (project / "Demo" / "Main.lean").write_text("theorem t : True := by\n  sorry\n", encoding="utf-8")
    (project / ".lake" / "packages" / "mathlib" / "Ignored.lean").write_text("theorem x : True := by\n  sorry\n", encoding="utf-8")
    (project / "build" / "ir" / "Ignored.lean").write_text("theorem y : True := by\n  sorry\n", encoding="utf-8")

    count, files = runner._count_project_sorries(str(project))

    assert count == 1
    assert files == ["Demo/Main.lean (1)"]


def test_declaration_work_queue_lists_pending_theorems_in_active_file(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem first : True := by",
                "  sorry",
                "",
                "lemma second : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    queue = runner._declaration_work_queue(
        str(active),
        "Main.lean:1:3: warning: declaration uses sorry",
        scope="file",
    )

    assert len(queue) == 1
    assert queue[0]["label"] == "first"
    assert "contains sorry" in queue[0]["reasons"]


def test_declaration_work_queue_scans_project_when_scope_is_project(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    first = module_dir / "A.lean"
    second = module_dir / "B.lean"
    first.write_text("theorem a : True := by\n  sorry\n", encoding="utf-8")
    second.write_text("theorem b : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))

    queue = runner._declaration_work_queue("", "", project_root=str(project), scope="project")

    assert len(queue) == 1
    assert queue[0]["label"] == "Demo/A.lean"
    assert queue[0]["reasons"] == ["1 sorry placeholder(s)"]


def test_build_live_proof_state_assigns_current_queue_head_as_target(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem first : True := by",
                "  sorry",
                "",
                "theorem second : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_FILE", "Demo/Main.lean")
    monkeypatch.setattr(runner, "_query_live_diagnostics", lambda path, symbol="": "lean-lsp diagnostics tool unavailable.")
    monkeypatch.setattr(runner, "_query_live_goals", lambda path, symbol: "lean-lsp goals tool unavailable.")
    monkeypatch.setattr(runner, "_extract_recent_build_status", lambda history: "unknown")
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)

    live_state = runner._build_live_proof_state([])

    assert live_state["target_symbol"] == "first"
    assert live_state["current_queue_item"]["label"] == "first"
    assert "Current file prefix ending at `first`" in live_state["current_queue_item_prefix"]
    assert "theorem first" in live_state["current_queue_item_prefix"]
    assert "theorem first" in live_state["current_queue_item_slice"]


def test_declaration_work_queue_prefers_named_sorry_over_anonymous_diagnostic_noise(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "example : True := by",
                "  trivial",
                "",
                "theorem first : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))

    queue = runner._declaration_work_queue(
        str(active),
        "lean-lsp diagnostics tool unavailable.",
        project_root=str(project),
        scope="file",
    )

    assert queue
    assert queue[0]["label"] == "first"
    assert queue[0]["reasons"] == ["contains sorry"]


def test_declaration_work_queue_keeps_named_theorem_with_build_error_without_sorry(tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem first : True := by",
                "  trivial",
                "",
                "theorem second : True := by",
                "  exact False.elim ?h",
            ]
        ),
        encoding="utf-8",
    )

    queue = runner._declaration_work_queue(
        str(active),
        "Demo/Main.lean:4:10: error: unsolved goals in theorem second",
        project_root=str(project),
        scope="file",
    )

    assert queue
    assert queue[0]["label"] == "second"
    assert "diagnostic near line 4" in queue[0]["reasons"] or "referenced in diagnostics" in queue[0]["reasons"]


def test_declaration_work_queue_does_not_match_very_short_names_from_text_alone(tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem h : True := by",
                "  trivial",
                "",
                "theorem long_name : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    queue = runner._declaration_work_queue(
        str(active),
        "type mismatch while rewriting with h in a later proof",
        project_root=str(project),
        scope="file",
    )

    assert queue == []


def test_queue_assignment_block_mentions_only_assigned_theorem():
    text = runner._queue_assignment_block(
        {
            "target_symbol": "absLipschitz1",
            "active_file_label": "GaussTest/RealTheorems-homework.lean",
            "current_blocker": "type mismatch in `simpa using h`",
            "current_queue_item": {"label": "absLipschitz1", "reasons": ["contains sorry"]},
            "current_queue_item_prefix": "Current file prefix ending at `absLipschitz1`:\n...\ntheorem absLipschitz1 : isLipschitz abs 1 := by\n  sorry",
            "current_queue_item_slice": "Assigned declaration slice (97-99):\ntheorem absLipschitz1 : isLipschitz abs 1 := by\n  sorry",
        },
        {
            "failed_attempts": [
                {
                    "attempt": 1,
                    "cycle": 1,
                    "target_symbol": "absLipschitz1",
                    "active_file": "GaussTest/RealTheorems-homework.lean",
                    "proof_shape": "direct `simpa [isLipschitz] using abs_abs_sub_abs_le`",
                    "reason": "type mismatch",
                }
            ]
        },
    )

    assert "declaration: absLipschitz1" in text
    assert "current status: blocked" in text
    assert "current blocker: type mismatch in `simpa using h`" in text
    assert "local helper lemmas or intermediate facts are allowed" in text
    assert "do not start solving unrelated later queue items" in text
    assert "Verification for this queue item:" in text
    assert "`lake env lean GaussTest/RealTheorems-homework.lean`" in text
    assert "do not treat `lake build`, `grep`, `head`, or truncated output" in text
    assert "Current file prefix ending at `absLipschitz1`" in text
    assert "PREVIOUS ATTEMPTS:" in text
    assert "attempt: 1" in text
    assert "proof shape: direct `simpa [isLipschitz] using abs_abs_sub_abs_le`" in text
    assert "why it failed: type mismatch" in text
    assert "Task:" in text
    assert "Repair `absLipschitz1` from its current state." in text


def test_effective_skill_name_uses_queue_worker_for_file_scoped_queue_turn(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_FILE", "Demo/Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "lean-proof-loop")

    selected = runner._effective_skill_name(
        {
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
            "declaration_queue_total": 1,
        }
    )

    assert selected == "lean-theorem-queue-worker"


def test_effective_skill_name_returns_proof_loop_for_final_file_sweep(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_FILE", "Demo/Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "lean-proof-loop")

    selected = runner._effective_skill_name(
        {
            "current_queue_item": {},
            "declaration_queue_total": 0,
        }
    )

    assert selected == "lean-proof-loop"


def test_live_state_is_not_verified_when_project_still_has_sorries():
    live_state = {
        "active_file": "/tmp/project/Main.lean",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake build succeeded",
        "verification_ok": True,
        "sorry_count": 0,
        "project_sorry_count": 2,
    }

    assert runner._live_state_is_verified(live_state) is False


def test_live_state_is_verified_for_file_scope_even_if_project_has_other_sorries():
    live_state = {
        "active_file": "/tmp/project/Main.lean",
        "declaration_scope": "file",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake env lean Demo/Main.lean succeeded",
        "verification_ok": True,
        "sorry_count": 0,
        "project_sorry_count": 2,
    }

    assert runner._live_state_is_verified(live_state) is True


def test_live_state_is_not_verified_without_explicit_verification_result():
    live_state = {
        "active_file": "/tmp/project/Main.lean",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake build succeeded",
        "sorry_count": 0,
        "project_sorry_count": 0,
    }

    assert runner._live_state_is_verified(live_state) is False


def test_diagnostics_indicate_failure_for_warnings():
    assert runner._diagnostics_indicate_failure("warning: declaration uses simp") is True


def test_promote_live_state_uses_focused_build_before_full_project_build(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")

    calls = []

    def _fake_build(active_file="", *, full_project=False):
        calls.append((active_file, full_project))
        return False, "lake build Main reported errors: unresolved import"

    monkeypatch.setattr(runner, "_run_explicit_verification_build", _fake_build)
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))

    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "lake build succeeded",
            "sorry_count": 0,
        }
    )

    assert calls == [(str(active), False)]
    assert promoted["build_status"] == "lake build Main reported errors: unresolved import"
    assert promoted["verification_ok"] is False
    assert runner._live_state_is_verified(promoted) is False


def test_promote_live_state_does_not_mark_non_module_file_verified_from_project_build(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "RealTheorems-homework.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))

    calls = []

    def _fake_build(active_file="", *, full_project=False):
        calls.append((active_file, full_project))
        return False, "lake env lean Demo/RealTheorems-homework.lean reported errors: type mismatch"

    monkeypatch.setattr(runner, "_run_explicit_verification_build", _fake_build)

    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "unknown",
            "sorry_count": 0,
        }
    )

    assert calls == [(str(active), False)]
    assert promoted["verification_ok"] is False
    assert "reported errors" in promoted["build_status"]


def test_promote_live_state_file_scope_does_not_block_on_other_project_sorries(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (3, ["Other.lean (3)"]))
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda active_file="", full_project=False: (True, "lake build Demo.Main succeeded"),
    )

    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "declaration_scope": "file",
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "unknown",
            "sorry_count": 0,
        }
    )

    assert promoted["verification_ok"] is True
    assert promoted["blocker_summary"] == ""


def test_normalize_blocker_summary_clears_resolved_text():
    assert runner._normalize_blocker_summary("None. All blockers resolved.") == ""
    assert runner._normalize_blocker_summary("type mismatch in `simpa`") == "type mismatch in `simpa`"


def test_extract_blocker_summary_does_not_fall_back_to_unrelated_trailing_line():
    text = "\n".join(
        [
            "## Blockers",
            "No blocker declared at this checkpoint.",
            "## Next steps",
            "- Exploring uniform continuity results building on these foundations",
        ]
    )

    assert runner._extract_blocker_summary(text) == ""


def test_recommended_verification_command_prefers_module_build_outside_single_item_turn(tmp_path, monkeypatch):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))
    monkeypatch.delenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", raising=False)
    monkeypatch.delenv("EPFLEMMA_NATIVE_ACTIVE_FILE", raising=False)

    command = runner._recommended_verification_command(str(active))

    assert command == "`lean_inspect` first, then `lake build Demo.Main` when the file is close to clean"


def test_recommended_verification_command_requires_canonical_file_check_for_single_item_turn(tmp_path, monkeypatch):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_FILE", "Demo/Main.lean")

    command = runner._recommended_verification_command(str(active))

    assert command == (
        "`lean_inspect` on Demo/Main.lean, then the required acceptance check "
        "`lake env lean Demo/Main.lean` for this file-scoped theorem turn"
    )


def test_recommended_verification_command_falls_back_to_lake_env_lean_for_non_module_file(tmp_path, monkeypatch):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "RealTheorems-homework.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))

    command = runner._recommended_verification_command(str(active))

    assert command == (
        "`lean_inspect` on Demo/RealTheorems-homework.lean, "
        "then final `lake env lean Demo/RealTheorems-homework.lean` when close to clean"
    )


def test_resolve_active_file_prefers_configured_active_file(monkeypatch, tmp_path):
    project = tmp_path / "GaussTest"
    target = project / "GaussTest" / "RealTheorems-homework.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_FILE", "GaussTest/RealTheorems-homework.lean")

    resolved = runner._resolve_active_file([])

    assert resolved == str(target.resolve())


def test_resolve_target_symbol_does_not_drift_from_history(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove ./GaussTest/RealTheorems-homework.lean")

    symbol = runner._resolve_target_symbol(
        [
            {"role": "assistant", "content": "reading Basic.lean"},
            {"role": "tool", "content": "def hello := \"world\""},
        ]
    )

    assert symbol == ""


def test_explicit_verification_build_uses_lake_env_lean_for_non_module_file(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "RealTheorems-homework.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        runner,
        "lean_verify",
        lambda target="", cwd="", mode="project": captured.update(
            {"target": target, "cwd": cwd, "mode": mode}
        ) or type(
            "_Result",
            (),
            {
                "ok": True,
                "command": "lake build Demo.RealTheorems-homework",
                "output": "",
            },
        )(),
    )

    ok, status = runner._run_explicit_verification_build(str(active), full_project=False)

    assert ok is True
    assert captured["target"] == str(active)
    assert captured["cwd"] == str(project)
    assert captured["mode"] == "file_exact"
    assert status == "lake build Demo.RealTheorems-homework succeeded"


def test_write_workflow_checkpoint_persists_index_and_current(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", "/tmp/project")
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setattr(runner, "_generate_checkpoint_summary", lambda *args, **kwargs: "## Goal\nResume proof")
    monkeypatch.setattr(runner, "_latest_filesystem_checkpoint_hash", lambda *args, **kwargs: "abc123def456")

    entry = runner._write_workflow_checkpoint(
        [{"role": "assistant", "content": "No errors found in Main.lean"}],
        _FakeAgent(),
        label="verified proof milestone",
        trigger="verified-progress",
        force_filesystem_checkpoint=True,
    )

    index_payload = runner._read_json_file(tmp_path / "workflow-state" / "index.json")
    current_payload = runner._read_json_file(tmp_path / "workflow-state" / "current.json")

    assert index_payload["checkpoints"][0]["label"] == "verified proof milestone"
    assert current_payload["checkpoint_id"] == entry["checkpoint_id"]
    assert entry["linked_filesystem_checkpoint"] == "abc123def456"


def test_maybe_checkpoint_before_compaction_emits_pre_compaction_checkpoint(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setattr(runner, "estimate_messages_tokens_rough", lambda messages: 500)
    created = {}

    def _fake_write(history, agent, **kwargs):
        created.update(kwargs)
        return {"checkpoint_id": "ckpt-1", "label": kwargs["label"]}

    monkeypatch.setattr(runner, "_write_workflow_checkpoint", _fake_write)

    entry = runner._maybe_checkpoint_before_compaction(
        [{"role": "assistant", "content": "x"}] * 5,
        _FakeAgent(),
    )

    assert entry["label"] == "pre-compaction checkpoint"
    assert created["trigger"] == "pre-compaction"


def test_drive_autonomous_followups_retries_until_live_state_is_verified(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_AUTONOMOUS_FOLLOWUPS", "3")

    class _LoopAgent(_FakeAgent):
        def __init__(self):
            super().__init__()
            self.calls = []

        def run_conversation(self, user_message, system_message=None, conversation_history=None, persist_user_message=None):
            self.calls.append(
                {
                    "user_message": user_message,
                    "persist_user_message": persist_user_message,
                    "history_len": len(conversation_history or []),
                }
            )
            return {
                "messages": list(conversation_history or [])
                + [{"role": "assistant", "content": f"continuation {len(self.calls)}"}]
            }

    final_state = {
        "active_file": "/tmp/project/Main.lean",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake build succeeded",
        "verification_ok": True,
        "sorry_count": 0,
        "message": "live-verified-stable",
    }
    live_states = chain(
        [
            {
                "active_file": "/tmp/project/Main.lean",
                "diagnostics": "warning: declaration uses sorry",
                "goals": "x : Nat\n⊢ x = x",
                "build_status": "unknown",
                "sorry_count": 1,
                "message": "live-1",
            },
            {
                "active_file": "/tmp/project/Main.lean",
                "diagnostics": "warning: declaration uses sorry",
                "goals": "x : Nat\n⊢ x = x",
                "build_status": "unknown",
                "sorry_count": 1,
                "message": "live-2",
            },
            {
                "active_file": "/tmp/project/Main.lean",
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake build succeeded",
                "verification_ok": True,
                "sorry_count": 0,
                "message": "live-verified",
            },
            final_state,
        ],
        repeat(final_state),
    )

    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states))
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_auto_compact_history", lambda history, agent: (history, {"snapshot_text": "", "reason": "no-op"}))
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)

    agent = _LoopAgent()
    history, _, _, live_state = runner._drive_autonomous_followups(
        agent,
        "system",
        [{"role": "assistant", "content": "initial stop"}],
        {"snapshot_text": "", "reason": "[none]"},
        {},
        {},
    )

    assert len(agent.calls) == 1
    assert agent.calls[0]["persist_user_message"] == "[epflemma-native autonomous continuation #1]"
    assert "Verification requires all of the following" in agent.calls[0]["user_message"]
    assert history[-1]["content"] == "continuation 1"
    assert runner._live_state_is_verified(live_state) is True


def test_autonomous_continuation_prompt_includes_recent_failed_attempts():
    prompt = runner._autonomous_continuation_prompt(
        {
            "target_symbol": "demo",
            "active_file_label": "Demo/Main.lean",
            "current_blocker": "warning: declaration uses sorry",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        },
        3,
        {
            "failed_attempts": [
                {
                    "attempt": 1,
                    "cycle": 1,
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "intro x y; simp",
                    "reason": "warning: declaration uses sorry",
                }
            ]
        },
    )

    assert "PREVIOUS ATTEMPTS:" in prompt
    assert "attempt: 1" in prompt
    assert "proof shape: intro x y; simp" in prompt
    assert "why it failed: warning: declaration uses sorry" in prompt


def test_queue_assignment_block_includes_exact_tool_path():
    block = runner._queue_assignment_block(
        {
            "active_file": "/tmp/project/Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "target_symbol": "demo",
            "current_queue_item": {
                "label": "demo",
                "reasons": ["contains sorry"],
            },
            "current_blocker": "contains sorry",
        }
    )

    assert "- file: Demo/Main.lean" in block
    assert "- exact tool path: /tmp/project/Demo/Main.lean" in block


def test_theorem_transition_handoff_includes_exact_tool_path():
    message = runner._theorem_transition_handoff_message(
        {
            "target_symbol": "previous_demo",
            "active_file": "/tmp/project/Demo/Main.lean",
            "status": "solved",
            "note": "done",
            "build_status": "ok",
        },
        {
            "active_file": "/tmp/project/Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "target_symbol": "next_demo",
            "current_queue_item": {"label": "next_demo"},
            "declaration_queue_summary": "- next_demo [Demo/Main.lean] — contains sorry",
            "build_status": "unknown",
        },
    )

    assert "- file: Demo/Main.lean" in message
    assert "- exact tool path: /tmp/project/Demo/Main.lean" in message


def test_autonomous_continuation_prompt_switches_to_final_file_sweep_when_queue_empty(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_FILE", "Demo/Main.lean")

    prompt = runner._autonomous_continuation_prompt(
        {
            "active_file": "/tmp/project/Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "diagnostics": "warning: malformed declaration body",
            "goals": "no goals",
            "build_status": "unknown",
            "current_blocker": "warning: malformed declaration body",
            "verification_ok": False,
        },
        2,
        {
            "failed_attempts": [
                {
                    "attempt": 1,
                    "cycle": 1,
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "intro x; simp",
                    "reason": "type mismatch",
                }
            ]
        },
    )

    assert "Queue status:" in prompt
    assert "declaration queue is empty" in prompt
    assert "inspect the full file `Demo/Main.lean` now" in prompt
    assert "you are no longer restricted to a single assigned theorem for this pass" in prompt
    assert "PREVIOUS ATTEMPTS:" not in prompt


def test_remember_failed_attempt_uses_theorem_delta_for_proof_shape():
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
            "slice": "Assigned declaration slice (10-12):\ntheorem demo : True := by\n  sorry",
        }
    }

    runner._remember_failed_attempt(
        autonomy_state,
        {
            "target_symbol": "demo",
            "active_file_label": "Demo/Main.lean",
            "current_queue_item_slice": (
                "Assigned declaration slice (10-13):\n"
                "theorem demo : True := by\n"
                "  intro x\n"
                "  simp\n"
            ),
            "blocker_summary": "type mismatch",
        },
        cycle_number=2,
    )

    attempt = autonomy_state["failed_attempts"][0]
    assert attempt["attempt"] == 1
    assert "+ intro x" in attempt["proof_shape"] or "- sorry" in attempt["proof_shape"]
    assert attempt["reason"] == "type mismatch"


def test_recent_failed_attempts_summary_does_not_leak_other_theorem_attempts():
    summary = runner._recent_failed_attempts_summary(
        {
            "failed_attempts": [
                {
                    "attempt": 1,
                    "cycle": 1,
                    "target_symbol": "first",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "intro x",
                    "reason": "type mismatch",
                }
            ]
        },
        {
            "target_symbol": "second",
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "second", "reasons": ["contains sorry"]},
        },
    )

    assert summary == ""


def test_summarize_theorem_transition_outcome_marks_reverted_to_sorry():
    outcome = runner._summarize_theorem_transition_outcome(
        {
            "current_queue_assignment": {
                "target_symbol": "amc12a_2021_p19",
                "active_file": "GaussTest/MiniF2F.lean",
                "slice": "theorem amc12a_2021_p19 : True := by\n  sorry",
            }
        },
        {
            "active_file_label": "GaussTest/MiniF2F.lean",
            "current_queue_item": {"label": "algebra_amgm_sumasqdivbgeqsuma", "reasons": ["contains sorry"]},
            "declaration_queue_summary": (
                "- amc12a_2021_p19 [GaussTest/MiniF2F.lean] — contains sorry\n"
                "- algebra_amgm_sumasqdivbgeqsuma [GaussTest/MiniF2F.lean] — contains sorry"
            ),
            "current_blocker": "amc12a_2021_p19 remains pending after being reverted to `sorry`.",
            "build_status": "lake env lean GaussTest/MiniF2F.lean exits 0",
        },
        [{"role": "assistant", "content": "amc12a_2021_p19 was reverted to `sorry` to unblock file compilation."}],
    )

    assert outcome["status"] == "reverted-to-sorry"
    assert "reverted" in outcome["note"]


def test_rebuild_history_for_theorem_transition_uses_compact_handoff():
    rebuilt, transition = runner._rebuild_history_for_theorem_transition(
        [
            {"role": "assistant", "content": "Detailed search transcript for amc12a_2021_p19"},
            {"role": "tool", "content": "Very long raw tool output for the previous theorem"},
        ],
        {"snapshot_text": "Compact workflow snapshot"},
        {
            "current_queue_assignment": {
                "target_symbol": "amc12a_2021_p19",
                "active_file": "GaussTest/MiniF2F.lean",
                "slice": "theorem amc12a_2021_p19 : True := by\n  sorry",
            }
        },
        {
            "active_file_label": "GaussTest/MiniF2F.lean",
            "current_queue_item": {"label": "algebra_amgm_sumasqdivbgeqsuma", "reasons": ["contains sorry"]},
            "declaration_queue_summary": "- algebra_amgm_sumasqdivbgeqsuma [GaussTest/MiniF2F.lean] — contains sorry",
            "build_status": "lake env lean GaussTest/MiniF2F.lean exits 0",
            "current_blocker": "",
        },
    )

    assert transition == {
        "previous_target": "amc12a_2021_p19",
        "previous_file": "GaussTest/MiniF2F.lean",
        "current_target": "algebra_amgm_sumasqdivbgeqsuma",
        "current_file": "GaussTest/MiniF2F.lean",
    }
    assert len(rebuilt) == 2
    assert rebuilt[0]["content"] == "Compact workflow snapshot"
    assert "Previous theorem outcome:" in rebuilt[1]["content"]
    assert "final status: solved" in rebuilt[1]["content"]
    joined = "\n".join(msg["content"] for msg in rebuilt)
    assert "Detailed search transcript" not in joined
    assert "Very long raw tool output" not in joined


def test_summarize_theorem_transition_outcome_prefers_previous_theorem_failed_attempt_reason():
    outcome = runner._summarize_theorem_transition_outcome(
        {
            "current_queue_assignment": {
                "target_symbol": "blocked_demo",
                "active_file": "Demo/Main.lean",
                "slice": "theorem blocked_demo : True := by\n  sorry",
            },
            "failed_attempts": [
                {
                    "attempt": 2,
                    "cycle": 3,
                    "target_symbol": "blocked_demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "intro x; simp",
                    "reason": "nonlinear arithmetic blocker",
                }
            ],
        },
        {
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "declaration_queue_summary": (
                "- blocked_demo [Demo/Main.lean] — contains sorry\n"
                "- next_demo [Demo/Main.lean] — contains sorry"
            ),
            "current_blocker": "next_demo still pending",
            "build_status": "unknown",
        },
        [{"role": "assistant", "content": "Moving on to another theorem for now."}],
    )

    assert outcome["status"] == "blocked"
    assert outcome["note"] == "nonlinear arithmetic blocker"


def test_same_queue_assignment_still_blocked_requires_same_theorem_and_real_blocker():
    assert runner._same_queue_assignment_still_blocked(
        {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
                "slice": "theorem demo : True := by\n  sorry",
            }
        },
        {
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
            "diagnostics": "error: unsolved goals",
            "goals": "x : Nat\n⊢ False",
            "build_status": "unknown",
        },
    ) is True

    assert runner._same_queue_assignment_still_blocked(
        {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
                "slice": "theorem demo : True := by\n  sorry",
            }
        },
        {
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "next", "reasons": ["contains sorry"]},
            "diagnostics": "warning: declaration uses sorry",
            "goals": "no goals",
            "build_status": "unknown",
        },
    ) is False


def test_rebuild_history_for_theorem_transition_records_blocked_outcome_and_failed_attempt():
    autonomy_state = {
        "current_cycle": 3,
        "current_queue_assignment": {
            "target_symbol": "blocked_demo",
            "active_file": "Demo/Main.lean",
            "slice": "theorem blocked_demo : True := by\n  sorry",
        },
    }
    live_state = {
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
        "declaration_queue_summary": (
            "- blocked_demo [Demo/Main.lean] — contains sorry\n"
            "- next_demo [Demo/Main.lean] — contains sorry"
        ),
        "current_blocker": "blocked_demo still has unresolved goals",
        "build_status": "unknown",
    }

    rebuilt_history, transition = runner._rebuild_history_for_theorem_transition(
        [{"role": "assistant", "content": "Moving on to another theorem for now."}],
        {"snapshot_text": "Compact workflow snapshot"},
        autonomy_state,
        live_state,
    )

    assert rebuilt_history is not None
    assert transition == {
        "previous_target": "blocked_demo",
        "previous_file": "Demo/Main.lean",
        "current_target": "next_demo",
        "current_file": "Demo/Main.lean",
    }
    attempts = autonomy_state["failed_attempts"]
    assert attempts[-1]["target_symbol"] == "blocked_demo"
    assert attempts[-1]["active_file"] == "Demo/Main.lean"
    assert attempts[-1]["cycle"] == 3
    assert attempts[-1]["proof_shape"] == "[transitioned away before theorem was solved]"
    outcomes = autonomy_state["theorem_outcomes"]
    assert outcomes["Demo/Main.lean::blocked_demo"]["status"] == "blocked"


def test_autonomous_stop_reason_blocks_verified_exit_when_prior_theorem_is_unresolved():
    live_state = {
        "active_file": "/tmp/project/Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "target_symbol": "next_demo",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake build Demo.Main succeeded",
        "sorry_count": 0,
        "verification_ok": True,
    }
    autonomy_state = {
        "theorem_outcomes": {
            "Demo/Main.lean::blocked_demo": {
                "target_symbol": "blocked_demo",
                "active_file": "Demo/Main.lean",
                "status": "blocked",
                "note": "still unresolved",
            }
        }
    }

    assert runner._autonomous_stop_reason([], live_state, autonomy_state) == "blocked"


def test_build_live_proof_state_surfaces_search_exhaustion(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
    monkeypatch.setattr(runner, "_resolve_active_file", lambda history, checkpoint_state=None: str(active))
    monkeypatch.setattr(runner, "_resolve_target_symbol", lambda history, checkpoint_state=None: "demo")
    monkeypatch.setattr(runner, "_extract_recent_build_status", lambda history: "unknown")
    monkeypatch.setattr(runner, "_collect_message_text", lambda history: "")
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (1, ["Demo/Main.lean (1)"]))
    monkeypatch.setattr(runner, "recent_empty_search_streak", lambda workflow_command: 3)
    monkeypatch.setattr(
        runner,
        "probe_capabilities",
        lambda cwd=None: type(
            "_Capabilities",
            (),
            {
                "to_dict": lambda self: {
                    "cwd": str(project),
                    "project_root": str(project),
                    "degraded_reasons": [],
                }
            },
        )(),
    )
    monkeypatch.setattr(
        runner,
        "lean_inspect",
        lambda *args, **kwargs: type(
            "_Inspection",
            (),
            {
                "diagnostics": "warning: declaration uses sorry",
                "goals": "Lean goals unavailable.",
                "sorry_count": 1,
                "project_sorry_count": 1,
                "blocker_kind": "open_goals",
                "queue_items": [
                    {
                        "label": "demo",
                        "kind": "theorem",
                        "line": 1,
                        "reasons": ["contains sorry"],
                    }
                ],
                "capability_report": {
                    "cwd": str(project),
                    "project_root": str(project),
                    "degraded_reasons": [],
                },
            },
        )(),
    )

    live_state = runner._build_live_proof_state([])

    assert live_state["search_exhausted"] is True
    assert live_state["recent_empty_search_streak"] == 3
    assert "search exhausted for this theorem" in live_state["message"]


def test_drive_autonomous_followups_rebuilds_history_when_theorem_changes(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")

    class _LoopAgent(_FakeAgent):
        def __init__(self):
            super().__init__()
            self.calls = []

        def run_conversation(self, user_message, system_message=None, conversation_history=None, persist_user_message=None):
            self.calls.append(
                {
                    "user_message": user_message,
                    "persist_user_message": persist_user_message,
                    "conversation_history": list(conversation_history or []),
                }
            )
            return {
                "messages": list(conversation_history or [])
                + [{"role": "assistant", "content": "next theorem continuation"}]
            }

    live_states = chain(
        [
            {
                "active_file": "/tmp/project/GaussTest/MiniF2F.lean",
                "active_file_label": "GaussTest/MiniF2F.lean",
                "target_symbol": "algebra_amgm_sumasqdivbgeqsuma",
                "current_queue_item": {"label": "algebra_amgm_sumasqdivbgeqsuma", "reasons": ["contains sorry"]},
                "declaration_queue_summary": "- algebra_amgm_sumasqdivbgeqsuma [GaussTest/MiniF2F.lean] — contains sorry",
                "diagnostics": "warning: declaration uses sorry",
                "goals": "no goals",
                "build_status": "lake env lean GaussTest/MiniF2F.lean exits 0",
                "current_blocker": "",
                "message": "live-next-theorem",
                "sorry_count": 1,
            },
            {
                "active_file": "/tmp/project/GaussTest/MiniF2F.lean",
                "active_file_label": "GaussTest/MiniF2F.lean",
                "target_symbol": "algebra_amgm_sumasqdivbgeqsuma",
                "current_queue_item": {"label": "algebra_amgm_sumasqdivbgeqsuma", "reasons": ["contains sorry"]},
                "declaration_queue_summary": "- algebra_amgm_sumasqdivbgeqsuma [GaussTest/MiniF2F.lean] — contains sorry",
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake env lean GaussTest/MiniF2F.lean exits 0",
                "current_blocker": "",
                "message": "live-next-theorem-verified",
                "sorry_count": 0,
                "verification_ok": True,
            },
        ],
        repeat(
            {
                "active_file": "/tmp/project/GaussTest/MiniF2F.lean",
                "active_file_label": "GaussTest/MiniF2F.lean",
                "target_symbol": "algebra_amgm_sumasqdivbgeqsuma",
                "current_queue_item": {"label": "algebra_amgm_sumasqdivbgeqsuma", "reasons": ["contains sorry"]},
                "declaration_queue_summary": "- algebra_amgm_sumasqdivbgeqsuma [GaussTest/MiniF2F.lean] — contains sorry",
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake env lean GaussTest/MiniF2F.lean exits 0",
                "current_blocker": "",
                "message": "live-next-theorem-verified",
                "sorry_count": 0,
                "verification_ok": True,
            }
        ),
    )

    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states))
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_auto_compact_history", lambda history, agent: (history, {"snapshot_text": "Compact workflow snapshot", "reason": "no-op"}))
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)

    agent = _LoopAgent()
    history, _, _, _ = runner._drive_autonomous_followups(
        agent,
        "system",
        [
            {"role": "assistant", "content": "Detailed search transcript for amc12a_2021_p19"},
            {"role": "tool", "content": "Very long raw tool output for amc12a_2021_p19"},
        ],
        {"snapshot_text": "Compact workflow snapshot", "reason": "[none]"},
        {},
        {
            "current_queue_assignment": {
                "target_symbol": "amc12a_2021_p19",
                "active_file": "GaussTest/MiniF2F.lean",
                "slice": "theorem amc12a_2021_p19 : True := by\n  sorry",
            }
        },
    )

    assert len(agent.calls) == 1
    rebuilt_history = agent.calls[0]["conversation_history"]
    assert len(rebuilt_history) == 2
    joined = "\n".join(msg["content"] for msg in rebuilt_history)
    assert "Compact workflow snapshot" in joined
    assert "amc12a_2021_p19" in joined
    assert "algebra_amgm_sumasqdivbgeqsuma" in joined
    assert "Detailed search transcript for amc12a_2021_p19" not in joined
    assert "Very long raw tool output for amc12a_2021_p19" not in joined
    assert history[-1]["content"] == "next theorem continuation"


def test_drive_autonomous_followups_keeps_history_when_theorem_does_not_change(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")

    class _LoopAgent(_FakeAgent):
        def __init__(self):
            super().__init__()
            self.calls = []

        def run_conversation(self, user_message, system_message=None, conversation_history=None, persist_user_message=None):
            self.calls.append(list(conversation_history or []))
            return {
                "messages": list(conversation_history or [])
                + [{"role": "assistant", "content": "same theorem continuation"}]
            }

    stable_live_state = {
        "active_file": "/tmp/project/Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "target_symbol": "demo",
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "declaration_queue_summary": "- demo [Demo/Main.lean] — contains sorry",
        "diagnostics": "warning: declaration uses sorry",
        "goals": "no goals",
        "build_status": "unknown",
        "current_blocker": "warning: declaration uses sorry",
        "message": "live-demo",
        "sorry_count": 1,
    }
    verified_live_state = {
        **stable_live_state,
        "diagnostics": "no errors found",
        "build_status": "lake env lean Demo/Main.lean exits 0",
        "current_blocker": "",
        "sorry_count": 0,
        "verification_ok": True,
    }
    live_states = chain([stable_live_state, verified_live_state], repeat(verified_live_state))

    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states))
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_auto_compact_history", lambda history, agent: (history, {"snapshot_text": "Compact workflow snapshot", "reason": "no-op"}))
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)

    original_history = [
        {"role": "assistant", "content": "existing theorem-local transcript"},
        {"role": "tool", "content": "existing tool output"},
    ]
    agent = _LoopAgent()
    runner._drive_autonomous_followups(
        agent,
        "system",
        original_history,
        {"snapshot_text": "Compact workflow snapshot", "reason": "[none]"},
        {},
        {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
                "slice": "theorem demo : True := by\n  sorry",
            }
        },
    )

    assert len(agent.calls) == 1
    assert agent.calls[0] == original_history


def test_drive_autonomous_followups_applies_auto_reasoning_to_current_theorem(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")

    class _LoopAgent(_FakeAgent):
        def __init__(self):
            super().__init__()
            self._managed_base_reasoning_config = {"mode": "auto"}
            self.reasoning_config = None
            self.calls = []

        def run_conversation(self, user_message, system_message=None, conversation_history=None, persist_user_message=None):
            self.calls.append(dict(self.reasoning_config or {}))
            return {
                "messages": list(conversation_history or [])
                + [{"role": "assistant", "content": "same theorem continuation"}]
            }

    stable_live_state = {
        "active_file": "/tmp/project/Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "target_symbol": "demo",
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "declaration_queue_summary": "- demo [Demo/Main.lean] — contains sorry",
        "diagnostics": "warning: declaration uses sorry",
        "goals": "no goals",
        "build_status": "unknown",
        "current_blocker": "warning: declaration uses sorry",
        "message": "live-demo",
        "sorry_count": 1,
    }
    verified_live_state = {
        **stable_live_state,
        "diagnostics": "no errors found",
        "build_status": "lake env lean Demo/Main.lean exits 0",
        "current_blocker": "",
        "sorry_count": 0,
        "verification_ok": True,
    }
    live_states = chain([stable_live_state, verified_live_state], repeat(verified_live_state))

    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states))
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_auto_compact_history", lambda history, agent: (history, {"snapshot_text": "Compact workflow snapshot", "reason": "no-op"}))
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)

    agent = _LoopAgent()
    runner._drive_autonomous_followups(
        agent,
        "system",
        [{"role": "assistant", "content": "existing theorem-local transcript"}],
        {"snapshot_text": "Compact workflow snapshot", "reason": "[none]"},
        {},
        {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
                "slice": "theorem demo : True := by\n  sorry",
            },
            "failed_attempts": [
                {
                    "attempt": i + 1,
                    "cycle": i + 1,
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "intro x",
                    "reason": "blocked",
                }
                for i in range(5)
            ],
        },
    )

    assert agent.calls == [{"enabled": True, "effort": "high"}]


def test_drive_autonomous_followups_records_transition_events(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")

    class _LoopAgent(_FakeAgent):
        def run_conversation(self, user_message, system_message=None, conversation_history=None, persist_user_message=None):
            return {
                "messages": list(conversation_history or [])
                + [{"role": "assistant", "content": "transitioned"}]
            }

    live_states = chain(
        [
            {
                "active_file": "/tmp/project/Demo/Main.lean",
                "active_file_label": "Demo/Main.lean",
                "target_symbol": "next_demo",
                "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
                "declaration_queue_summary": "- next_demo [Demo/Main.lean] — contains sorry",
                "diagnostics": "warning: declaration uses sorry",
                "goals": "no goals",
                "build_status": "unknown",
                "current_blocker": "",
                "message": "live-next",
                "sorry_count": 1,
            },
            {
                "active_file": "/tmp/project/Demo/Main.lean",
                "active_file_label": "Demo/Main.lean",
                "target_symbol": "next_demo",
                "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
                "declaration_queue_summary": "- next_demo [Demo/Main.lean] — contains sorry",
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake env lean Demo/Main.lean exits 0",
                "current_blocker": "",
                "message": "live-next-verified",
                "sorry_count": 0,
                "verification_ok": True,
            },
        ],
        repeat(
            {
                "active_file": "/tmp/project/Demo/Main.lean",
                "active_file_label": "Demo/Main.lean",
                "target_symbol": "next_demo",
                "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
                "declaration_queue_summary": "- next_demo [Demo/Main.lean] — contains sorry",
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake env lean Demo/Main.lean exits 0",
                "current_blocker": "",
                "message": "live-next-verified",
                "sorry_count": 0,
                "verification_ok": True,
            }
        ),
    )

    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states))
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_auto_compact_history", lambda history, agent: (history, {"snapshot_text": "Compact workflow snapshot", "reason": "no-op"}))
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)

    runner._drive_autonomous_followups(
        _LoopAgent(),
        "system",
        [{"role": "assistant", "content": "Detailed theorem A transcript"}],
        {"snapshot_text": "Compact workflow snapshot", "reason": "[none]"},
        {},
        {
            "current_queue_assignment": {
                "target_symbol": "prev_demo",
                "active_file": "Demo/Main.lean",
                "slice": "theorem prev_demo : True := by\n  sorry",
            }
        },
    )

    events = read_workflow_activity(limit=20)
    event_types = [event["type"] for event in events]
    assert "theorem-transition" in event_types
    assert "theorem-context-cleared" in event_types
    assert "theorem-handoff-rebuilt" in event_types
