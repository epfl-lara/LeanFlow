from __future__ import annotations

import os

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


def test_workflow_startup_guidance_mentions_autonomous_loop():
    text = runner._workflow_startup_guidance("autoprove", "/lean4:autoprove Main.lean")

    assert "autonomous proving session" in text
    assert "/lean4:autoprove Main.lean" in text
    assert "continue iterating" in text


def test_workflow_startup_guidance_mentions_user_approved_swarm(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_PARALLEL_AGENTS", "3")
    monkeypatch.setenv("EPFLEMMA_NATIVE_USER_APPROVED_SWARM", "1")

    text = runner._workflow_startup_guidance("autoprove", "/lean4:autoprove Main.lean")

    assert "User-approved swarm mode" in text
    assert "3 agents total" in text


def test_history_status_lines_summarize_message_counts(monkeypatch):
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/lean4:prove Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", "/tmp/project")

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


def test_build_agent_uses_epflemma_native_toolset(monkeypatch):
    captured = {}

    class DummyAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(runner, "AIAgent", DummyAgent)
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5")
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
    assert callable(captured["tool_progress_callback"])
    assert callable(captured["step_callback"])


def test_build_agent_uses_swarm_toolset_when_user_enabled_swarm(monkeypatch):
    captured = {}

    class DummyAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = "runner-session"

    monkeypatch.setattr(runner, "AIAgent", DummyAgent)
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5")
    monkeypatch.setenv("EPFLEMMA_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_KEY", "sk-test")
    monkeypatch.setenv("EPFLEMMA_NATIVE_PROVIDER", "zai")
    monkeypatch.setenv("EPFLEMMA_NATIVE_API_MODE", "responses")
    monkeypatch.setenv("EPFLEMMA_NATIVE_TOOLSET", "epflemma-native-swarm")

    runner._build_agent()

    assert captured["enabled_toolsets"] == ["epflemma-native-swarm"]
    assert os.getenv("EPFLEMMA_NATIVE_RUNNER_OWNER", "") == "runner-session"


def test_tool_progress_callback_persists_structured_events(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/lean4:prove Main.lean")
    monkeypatch.setenv("EPFLEMMA_NATIVE_ACTIVE_SKILL", "lean-proof-loop")

    runner._tool_progress_callback("terminal", "Run lake build", {"command": "lake build"})
    runner._tool_progress_callback("_thinking", "Inspecting theorem and diagnostics")
    runner._step_callback(3, ["search_files", "terminal"])

    events = read_workflow_activity(limit=3)

    assert [event["type"] for event in events] == ["tool-start", "assistant-plan", "api-call"]
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


def test_live_state_is_not_verified_when_project_still_has_sorries():
    live_state = {
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake build succeeded",
        "sorry_count": 0,
        "project_sorry_count": 2,
    }

    assert runner._live_state_is_verified(live_state) is False


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

    assert calls == [(str(active), True)]
    assert promoted["build_status"] == "lake build Main reported errors: unresolved import"
    assert runner._live_state_is_verified(promoted) is False


def test_recommended_verification_command_prefers_module_build(tmp_path, monkeypatch):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", str(project))

    command = runner._recommended_verification_command(str(active))

    assert command == "lake build Demo.Main"


def test_write_workflow_checkpoint_persists_index_and_current(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path))
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "autoprove")
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_COMMAND", "/lean4:autoprove Main.lean")
    monkeypatch.setenv("EPFLEMMA_PROJECT_ROOT", "/tmp/project")
    monkeypatch.setenv("EPFLEMMA_NATIVE_MODEL", "zai-org/GLM-5")
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
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "autoprove")
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
    monkeypatch.setenv("EPFLEMMA_NATIVE_WORKFLOW_KIND", "autoprove")
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

    live_states = iter(
        [
            {
                "diagnostics": "warning: declaration uses sorry",
                "goals": "x : Nat\n⊢ x = x",
                "build_status": "unknown",
                "sorry_count": 1,
                "message": "live-1",
            },
            {
                "diagnostics": "warning: declaration uses sorry",
                "goals": "x : Nat\n⊢ x = x",
                "build_status": "unknown",
                "sorry_count": 1,
                "message": "live-2",
            },
            {
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake build succeeded",
                "sorry_count": 0,
                "message": "live-verified",
            },
            {
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake build succeeded",
                "sorry_count": 0,
                "message": "live-verified-stable",
            },
        ]
    )

    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states))
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
