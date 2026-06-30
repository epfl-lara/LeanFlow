from __future__ import annotations

import json
import os
from itertools import chain, repeat

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows.workflow_state import read_workflow_activity


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


class _ManagedRunAgentStub:
    """Minimal managed-run contract surface (see agent/runtime/managed_run.py) for runner tests.

    native_runner stages guidance through the agent's stage/set/clear_tool_result_appendix methods.
    These mirror AIAgent's semantics (accumulate / replace / discard) over the same backing
    ``_post_tool_result_appendix`` attribute the tests inspect, so a stub honestly satisfies the
    contract rather than exposing only a raw attribute.
    """

    def stage_tool_result_appendix(self, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        previous = str(getattr(self, "_post_tool_result_appendix", "") or "").strip()
        self._post_tool_result_appendix = f"{previous}\n\n{text}".strip() if previous else text

    def set_tool_result_appendix(self, text: str) -> None:
        text = str(text or "").strip()
        if text:
            self._post_tool_result_appendix = text
        else:
            self.clear_tool_result_appendix()

    def clear_tool_result_appendix(self) -> None:
        if hasattr(self, "_post_tool_result_appendix"):
            delattr(self, "_post_tool_result_appendix")


class _FakeAgent(_ManagedRunAgentStub):
    compression_enabled = True

    def __init__(self):
        self.context_compressor = _FakeCompressor()
        self._checkpoint_mgr = _FakeCheckpointManager()


def test_verified_workflow_exits_without_prompt_when_stdin_is_not_interactive(monkeypatch):
    class _Stdin:
        def isatty(self):
            return False

    monkeypatch.setattr(runner.sys, "stdin", _Stdin())
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda live_state: True)

    assert runner._verified_workflow_should_exit_without_prompt({"phase": "verified"}) is True


def test_verified_workflow_exits_without_prompt_when_stdin_is_tty(monkeypatch):
    # A fully verified proof in a real terminal must exit cleanly, NOT drop into the
    # chat prompt loop, so the user's shell is handed back for new commands.
    class _Stdin:
        def isatty(self):
            return True

    monkeypatch.delenv("LEANFLOW_NATIVE_STAY_AFTER_VERIFIED", raising=False)
    monkeypatch.setattr(runner.sys, "stdin", _Stdin())
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda live_state: True)

    assert runner._verified_workflow_should_exit_without_prompt({"phase": "verified"}) is True


def test_verified_workflow_stays_interactive_when_opt_in_flag_set(monkeypatch):
    # Opt-in escape hatch: with the flag set and a real TTY, keep the legacy chat loop.
    class _Stdin:
        def isatty(self):
            return True

    monkeypatch.setenv("LEANFLOW_NATIVE_STAY_AFTER_VERIFIED", "1")
    monkeypatch.setattr(runner.sys, "stdin", _Stdin())
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda live_state: True)

    assert runner._verified_workflow_should_exit_without_prompt({"phase": "verified"}) is False


def test_unverified_workflow_does_not_exit_without_prompt(monkeypatch):
    # An unfinished proof in a TTY still gets the interactive prompt (guidance/resume).
    class _Stdin:
        def isatty(self):
            return True

    monkeypatch.delenv("LEANFLOW_NATIVE_STAY_AFTER_VERIFIED", raising=False)
    monkeypatch.setattr(runner.sys, "stdin", _Stdin())
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda live_state: False)

    assert runner._verified_workflow_should_exit_without_prompt({"phase": "paused"}) is False


def test_interactive_prompt_loop_disallowed_when_stdin_not_tty(monkeypatch):
    # Headless run: main() must not enter the blocking input() loop, or it hangs forever.
    class _Stdin:
        def isatty(self):
            return False

    monkeypatch.setattr(runner.sys, "stdin", _Stdin())
    assert runner._interactive_prompt_loop_allowed() is False


def test_interactive_prompt_loop_allowed_when_stdin_is_tty(monkeypatch):
    class _Stdin:
        def isatty(self):
            return True

    monkeypatch.setattr(runner.sys, "stdin", _Stdin())
    assert runner._interactive_prompt_loop_allowed() is True


def test_run_managed_conversation_passes_through_result():
    class _Agent(_ManagedRunAgentStub):
        def run_conversation(self, **kwargs):
            return {"messages": [], "interrupted": False, "kwargs": kwargs}

    result = runner._run_managed_conversation(
        _Agent(), user_message="hello", persist_user_message="hello"
    )

    assert result["interrupted"] is False
    assert result["kwargs"]["user_message"] == "hello"


def test_run_managed_conversation_uses_managed_tool_task_id():
    class _Agent(_ManagedRunAgentStub):
        _managed_tool_task_id = "managed-task"

        def run_conversation(self, **kwargs):
            return {"messages": [], "interrupted": False, "kwargs": kwargs}

    result = runner._run_managed_conversation(_Agent(), user_message="hello")

    assert result["kwargs"]["task_id"] == "managed-task"


def test_run_managed_conversation_preserves_explicit_task_id():
    class _Agent(_ManagedRunAgentStub):
        _managed_tool_task_id = "managed-task"

        def run_conversation(self, **kwargs):
            return {"messages": [], "interrupted": False, "kwargs": kwargs}

    result = runner._run_managed_conversation(
        _Agent(), user_message="hello", task_id="explicit-task"
    )

    assert result["kwargs"]["task_id"] == "explicit-task"


def test_run_managed_conversation_returns_failed_payload_on_provider_error(capsys):
    class _Agent(_ManagedRunAgentStub):
        _session_messages = [{"role": "assistant", "content": "partial"}]

        def run_conversation(self, **kwargs):
            raise RuntimeError("Connection error.")

    result = runner._run_managed_conversation(
        _Agent(),
        user_message="hello",
        conversation_history=[{"role": "user", "content": "hello"}],
    )

    assert result["failed"] is True
    assert result["completed"] is False
    assert result["partial"] is True
    assert result["messages"] == [{"role": "assistant", "content": "partial"}]
    assert "RuntimeError: Connection error." in result["error"]
    output = capsys.readouterr().out
    assert "Managed workflow stopped after provider/API error" in output


def test_run_managed_conversation_interrupts_on_ctrl_c(monkeypatch, capsys):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self.interrupt_calls = 0

        def interrupt(self, message=None):
            self.interrupt_calls += 1
            self.last_interrupt_message = message

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


def test_run_managed_conversation_returns_interrupted_result_when_no_payload_arrives_after_interrupt(
    monkeypatch, capsys
):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self.interrupt_calls = 0
            self._session_messages = [{"role": "assistant", "content": "partial"}]

        def interrupt(self, message=None):
            self.interrupt_calls += 1
            self.last_interrupt_message = message

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
    class _Agent(_ManagedRunAgentStub):
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
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self.interrupt_calls = 0

        def interrupt(self, message=None):
            self.interrupt_calls += 1
            self.last_interrupt_message = message

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


def test_interactive_mode_header_uses_formalizer_label(monkeypatch, capsys):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")

    runner._print_interactive_mode_header(
        {
            "build_status": "waiting for document formalization handoff verifier",
            "active_file_label": "DocFormalizationDemo/Pyth/Main.lean",
            "target_symbol": "[unknown]",
        }
    )

    output = capsys.readouterr().out
    assert "formalizer-agent mode  ·  waiting for document formalization handoff verifier" in output
    assert "prover-agent mode" not in output


def test_interactive_mode_header_keeps_prover_label(monkeypatch, capsys):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")

    runner._print_interactive_mode_header(
        {
            "build_status": "lake build running",
            "active_file_label": "Main.lean",
            "target_symbol": "demo",
        }
    )

    output = capsys.readouterr().out
    assert "prover-agent mode  ·  lake build running" in output


def test_handle_managed_tool_result_records_failed_attempt_after_verification_feedback(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    live_state = {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "current_queue_item_slice": "theorem demo : True := by\n  intro h\n  exact h",
        "diagnostics": "error: unsolved goals",
        "goals": "⊢ False",
        "build_status": "unknown",
        "blocker_summary": "error: unsolved goals",
    }

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: dict(live_state)
    )
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {"ok": False, "command": "lake env lean Demo/Main.lean"},
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    attempts = agent._managed_autonomy_state["failed_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["attempt"] == 1
    assert attempts[0]["reason"] == (
        "file failed | tool: patch+lean_verify | errors: 0, warnings: 0, sorry: 0 | "
        "lake env lean Demo/Main.lean"
    )
    assert "THEOREM FEEDBACK" in agent._post_tool_result_appendix
    assert "continue the same theorem turn" in agent._post_tool_result_appendix
    assert agent.interrupt_messages == []
    assert agent._managed_pending_theorem_feedback is None


def test_handle_managed_tool_result_nudges_after_repeated_failed_edits(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
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
                        "proof_shape": "same rewrite shape",
                        "reason": "type mismatch",
                    }
                    # Seed one below the escalation limit so the next recorded attempt lands
                    # exactly on the firing boundary, regardless of how the limit is tuned.
                    for i in range(runner.FAILED_ATTEMPT_ESCALATION_NUDGE_LIMIT - 1)
                ],
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    live_state = {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": ["diagnostic near line 3"]},
        "current_queue_item_slice": "theorem demo : True := by\n  exact False.elim ?h",
        "diagnostics": "error: type mismatch",
        "goals": "⊢ False",
        "build_status": "unknown",
        "blocker_summary": "error: type mismatch",
    }
    events = []

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: dict(live_state)
    )
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {"ok": False, "command": "lake env lean Demo/Main.lean"},
    )
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    attempts = agent._managed_autonomy_state["failed_attempts"]
    assert attempts[-1]["attempt"] == runner.FAILED_ATTEMPT_ESCALATION_NUDGE_LIMIT
    assert "[LEANFLOW-NATIVE FAILED ATTEMPT NUDGE]" in agent._post_tool_result_appendix
    assert "lean_decompose_helpers" in agent._post_tool_result_appendix
    assert "lean_reasoning_help" in agent._post_tool_result_appendix
    assert "lean_worker_dispatch" not in agent._post_tool_result_appendix
    assert any(args[0] == "failed-attempt-escalation-nudge" for args, _kwargs in events)


def test_handle_managed_incremental_feedback_records_current_output(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    live_state = {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "current_queue_item_slice": "theorem demo : True := by\n  exact False.elim ?h",
        "blocker_summary": "stale blocker",
    }
    payload = {
        "success": True,
        "ok": False,
        "action": "feedback",
        "backend": "lean_interact",
        "command": "lean_interact feedback",
        "target": "demo",
        "output": "current feedback diagnostic",
        "messages": [{"severity": "error", "message": "current feedback diagnostic"}],
    }

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: dict(live_state)
    )

    agent = _Agent()
    runner._handle_managed_tool_result(
        agent, "lean_incremental_check", {"action": "feedback"}, json.dumps(payload)
    )

    attempts = agent._managed_autonomy_state["failed_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["reason"] == "current feedback diagnostic"
    assert "current feedback diagnostic" in agent._post_tool_result_appendix


def test_handle_managed_tool_result_nudges_repeated_successful_search(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = []
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }

        def is_interrupted(self):
            return False

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    agent = _Agent()
    result = json.dumps(
        {
            "success": True,
            "query": "padicValNat.pow_sub_pow",
            "results": [{"provider": "mcp-leanexplore", "match": "padicValNat.pow_sub_pow"}],
        }
    )

    for _ in range(3):
        runner._handle_managed_tool_result(
            agent, "lean_search", {"query": "padicValNat.pow_sub_pow"}, result
        )

    appendix = agent._post_tool_result_appendix
    assert "SEARCH PROGRESS NUDGE" in appendix
    assert "same lean_search query repeated" in appendix
    assert "search providers are responding" in appendix
    assert "do not call `lean_search` again" in appendix
    assert "lean_decompose_helpers" in appendix


def test_search_progress_nudge_is_honest_about_degraded_providers(monkeypatch, tmp_path):
    """When the search payload reports degraded providers, the nudge must say so and steer off
    search — not claim 'search providers are responding'."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = []
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }

        def is_interrupted(self):
            return False

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    agent = _Agent()
    degraded_result = json.dumps(
        {
            "success": True,
            "query": "sq_div_le",
            "results": [],
            "degraded_reasons": [
                "LeanExplore local search failed: (sqlite3.DatabaseError) database disk image is malformed",
                "local Loogle disabled for this project because its managed Lean toolchain differs",
            ],
        }
    )
    for _ in range(runner.SEARCH_PROGRESS_REPEAT_NUDGE_LIMIT):
        runner._handle_managed_tool_result(agent, "lean_search", {"query": "sq_div_le"}, degraded_result)

    appendix = agent._post_tool_result_appendix
    assert "SEARCH PROGRESS NUDGE" in appendix
    assert "search is DEGRADED" in appendix
    assert "search providers are responding" not in appendix
    assert "malformed" in appendix


def test_record_turn_prompt_fingerprint_tracks_change_and_size(monkeypatch):
    events = []
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: events.append((a, k)))
    state: dict = {}

    runner._record_turn_prompt_fingerprint(state, "hello world prompt", phase="startup", cycle=0)
    runner._record_turn_prompt_fingerprint(state, "hello world prompt", phase="autonomous", cycle=1)
    runner._record_turn_prompt_fingerprint(
        state, "a different, longer prompt body here", phase="autonomous", cycle=2
    )

    assert [a[0] for a, _ in events] == ["turn-prompt", "turn-prompt", "turn-prompt"]
    k0, k1, k2 = (events[0][1], events[1][1], events[2][1])
    # First turn has no previous -> treated as changed; size is recorded.
    assert k0["prompt_changed"] is True
    assert k0["prompt_char_count"] == len("hello world prompt")
    # Identical re-send is flagged unchanged with zero delta (the optimization target).
    assert k1["prompt_changed"] is False
    assert k1["prompt_delta_chars"] == 0
    # A genuinely different prompt is flagged changed with a distinct fingerprint.
    assert k2["prompt_changed"] is True
    assert k2["prompt_fingerprint"] != k1["prompt_fingerprint"]
    assert "prompt_preview" in k2


def test_search_progress_nudge_ignores_non_search_capability_degradation(monkeypatch, tmp_path):
    """Capability degradation unrelated to search (e.g. proof-context MCP) must NOT flip the nudge
    to 'search is DEGRADED' — lean_search seeds degraded_reasons from the full capability report."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = []
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }

        def is_interrupted(self):
            return False

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    agent = _Agent()
    # Non-search degradation + valid search results: must stay "providers are responding".
    result = json.dumps(
        {
            "success": True,
            "query": "sq_div_le",
            "results": [{"provider": "mcp-leanexplore", "match": "sq_div_le"}],
            "degraded_reasons": [
                "lean proof context MCP unavailable",
                "LeanInteract incremental verifier unavailable",
            ],
        }
    )
    for _ in range(runner.SEARCH_PROGRESS_REPEAT_NUDGE_LIMIT):
        runner._handle_managed_tool_result(agent, "lean_search", {"query": "sq_div_le"}, result)

    appendix = agent._post_tool_result_appendix
    assert "SEARCH PROGRESS NUDGE" in appendix
    assert "search is DEGRADED" not in appendix
    assert "search providers are responding" in appendix


def test_generate_checkpoint_summary_falls_back_on_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(
        runner, "call_llm", lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )

    summary = runner._generate_checkpoint_summary(
        _FakeCompressor(),
        [{"role": "user", "content": "prove Demo.main"}],
        label="manual",
        trigger="exit",
        note="leaving",
    )

    assert "manual" in summary
    assert "exit" in summary


def test_handle_managed_tool_result_ignores_failed_patch_result(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    verify_calls = []
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner, "_manager_verify_queue_file", lambda active_file: verify_calls.append(active_file)
    )

    agent = _Agent()
    runner._handle_managed_tool_result(
        agent, "patch", {}, json.dumps({"success": False, "error": "no match"})
    )

    assert verify_calls == []
    assert "failed_attempts" not in agent._managed_autonomy_state
    assert agent.interrupt_messages == []
    assert agent._managed_pending_theorem_feedback is None


def test_apply_verified_patch_counts_as_edit_and_verification_feedback(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    live_state = {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "current_queue_item_slice": "theorem demo : True := by\n  sorry",
        "diagnostics": "error: unsolved goals",
        "goals": "⊢ True",
        "build_status": "unknown",
        "blocker_summary": "error: unsolved goals",
    }

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: dict(live_state)
    )

    agent = _Agent()
    runner._handle_managed_tool_result(
        agent,
        "apply_verified_patch",
        {"path": "Demo/Main.lean", "theorem_id": "demo"},
        json.dumps({"status": "check_failed"}),
    )

    attempts = agent._managed_autonomy_state["failed_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["reason"] == "error: unsolved goals"
    assert "THEOREM FEEDBACK" in agent._post_tool_result_appendix
    assert agent.interrupt_messages == []
    assert agent._managed_pending_theorem_feedback is None


def test_handle_managed_tool_result_keeps_assigned_theorem_when_queue_advances(monkeypatch, capsys):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    incremental_calls = []
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda active_file, target_symbol: (
            incremental_calls.append((active_file, target_symbol))
            or {
                "ok": True,
                "mode": "incremental_target",
                "backend": "lean_interact",
                "command": "lean_interact check_target",
                "target": target_symbol,
                "incremental": {"success": True, "ok": True},
            }
        ),
    )
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: pytest.fail(
            "successful incremental queue checks should not fall back to Lake"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "next_demo",
            "active_file": "Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "current_queue_item_slice": "theorem next_demo : True := by\n  sorry",
            "diagnostics": "warning: declaration uses sorry",
            "goals": "no goals",
            "build_status": "unknown",
            "blocker_summary": "warning: declaration uses sorry",
        },
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    assert "failed_attempts" not in agent._managed_autonomy_state
    output = capsys.readouterr().out
    assert "Workflow step verified for demo" in output
    assert "selecting the next target" in output
    assert "Queue step boundary: demo verified" in output
    assert incremental_calls == [("Demo/Main.lean", "demo")]
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    assert agent._managed_pending_theorem_feedback is None


def test_handle_managed_tool_result_advances_when_target_check_clean_despite_inspect_warning(
    monkeypatch, tmp_path, capsys
):
    """Regression: file-wide ``lean_inspect`` style warnings on the assigned
    declaration must NOT block queue advancement when the targeted manager
    check is clean. The targeted check is the spec-authoritative gate; the
    user-visible ``Manager verification ... warnings: 0`` line and the
    runner's actual decision must agree."""

    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem addLipschitz : True := by",
                "  have h : True := by",
                "    simp [addF]",
                "  trivial",
                "",
                "theorem next_demo : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "addLipschitz",
                    "active_file": str(active),
                    "slice": "theorem addLipschitz : True := by\n  trivial",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda active_file, target_symbol: {
            "ok": True,
            "mode": "incremental_target",
            "backend": "lean_interact",
            "command": "lean_interact check_target",
            "target": target_symbol,
            "output": "",
            "incremental": {"success": True, "ok": True},
        },
    )
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: pytest.fail(
            "successful incremental queue checks should not fall back to Lake"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "next_demo",
            "active_file": str(active),
            "active_file_label": "Main.lean",
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "current_queue_item_slice": "theorem next_demo : True := by\n  sorry",
            # File-wide inspect surfaces a style warning at line 3 (inside
            # addLipschitz's range) — this used to spuriously trigger a
            # cleanup-feedback opportunity even though the targeted check
            # said warnings: 0.
            "diagnostics": (
                f"{active}:3:5: warning: simp [addF] is a flexible tactic; "
                "consider using `simp only [addF]`"
            ),
            "goals": "no goals",
            "build_status": "unknown",
            "blocker_summary": "",
        },
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    assert not getattr(
        agent, "_post_tool_result_appendix", ""
    ), "C2: file-wide warnings must not synthesize a cleanup-feedback appendix"
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    output = capsys.readouterr().out
    assert "Workflow step verified for addLipschitz" in output
    assert "Queue step boundary: addLipschitz verified" in output
    assert "🟡 Cleanup feedback" not in output


def test_handle_managed_tool_result_prints_cleanup_feedback_when_warning_in_target_check(
    monkeypatch, tmp_path, capsys
):
    """Regression for Fix B: when the post-patch boundary keeps the same
    theorem turn for a focused warning-cleanup opportunity, the runner must
    print a visible ``🟡 Cleanup feedback`` line so the user can see why the
    next API step happens. Before this fix the cleanup branch fell through
    silently and looked indistinguishable from a hallucinated re-edit."""

    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  trivial",
                "",
                "theorem next_demo : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "slice": "theorem demo : True := by\n  trivial",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda active_file, target_symbol: {
            "ok": True,
            "mode": "incremental_target",
            "backend": "lean_interact",
            "command": "lean_interact check_target",
            "target": target_symbol,
            # Warning is in the targeted check's own output -> Fix C2
            # correctly keeps this as a real cleanup opportunity.
            "output": f"{active}:2:3: warning: This line exceeds the 100 character limit, please shorten it!",
            "incremental": {"success": True, "ok": True},
        },
    )
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: pytest.fail(
            "successful incremental queue checks should not fall back to Lake"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "demo",
            "active_file": str(active),
            "active_file_label": "Main.lean",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
            "current_queue_item_slice": "theorem demo : True := by\n  trivial",
            "diagnostics": "",
            "goals": "no goals",
            "build_status": "unknown",
            "blocker_summary": "",
        },
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    assert "THEOREM FEEDBACK" in agent._post_tool_result_appendix
    assert "warning-only cleanup" in agent._post_tool_result_appendix
    output = capsys.readouterr().out
    assert "🟡 Cleanup feedback for demo" in output
    assert "focused warning-cleanup opportunity granted" in output
    # Cleanup branch must NOT close the boundary or interrupt the agent —
    # the model gets the same theorem turn back with the appendix attached.
    assert agent.interrupt_messages == []
    assert "Queue step boundary" not in output


def test_handle_managed_tool_result_fires_cleanup_from_incremental_check_structured_messages(
    monkeypatch, tmp_path, capsys
):
    """Regression: ``lean_incremental_check`` reports warnings as plain
    ``warning: ...`` lines without the ``<file>:<line>:<col>:`` prefix that
    ``diagnostic_items()``'s text fallback regex requires. Before forwarding
    the structured ``messages`` list to the cleanup helper, those warnings
    were invisible to the post-patch boundary even though the runner's own
    ``🔎 Manager verification`` line reported the count correctly. The
    focused warning-cleanup opportunity must fire when the targeted check
    surfaces a warning whose line falls inside the assigned declaration."""

    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  have h : True := by",
                "    all_goals trivial",
                "  trivial",
                "",
                "theorem next_demo : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "slice": "theorem demo : True := by\n  trivial",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda active_file, target_symbol: {
            "ok": True,
            "mode": "incremental_target",
            "backend": "lean_interact",
            "command": "lean_interact check_target",
            "target": target_symbol,
            # `lean_incremental_check`-style flat output: no `:line:col:`
            # prefix, so `diagnostic_items()` returns [] for this text.
            "output": (
                "warning: 'all_goals trivial' tactic does nothing "
                "Note: This linter can be disabled with `set_option linter.unusedTactic false`"
            ),
            # Structured form survives via the new `messages` field.
            "messages": [
                {
                    "severity": "warning",
                    "message": "'all_goals trivial' tactic does nothing",
                    "line": 3,
                    "column": 5,
                }
            ],
            "incremental": {"success": True, "ok": True},
        },
    )
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: pytest.fail(
            "successful incremental queue checks should not fall back to Lake"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "demo",
            "active_file": str(active),
            "active_file_label": "Main.lean",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
            "current_queue_item_slice": "theorem demo : True := by\n  trivial",
            "diagnostics": "",
            "goals": "no goals",
            "build_status": "unknown",
            "blocker_summary": "",
        },
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    appendix = getattr(agent, "_post_tool_result_appendix", "")
    assert "THEOREM FEEDBACK" in appendix
    assert "warning-only cleanup" in appendix
    assert "all_goals trivial" in appendix
    output = capsys.readouterr().out
    assert "🟡 Cleanup feedback for demo" in output
    assert "warning near line 3" in output
    assert "focused warning-cleanup opportunity granted" in output
    # Cleanup branch must NOT close the boundary or interrupt the agent —
    # the model gets the same theorem turn back with the appendix attached.
    assert agent.interrupt_messages == []
    assert "Queue step boundary" not in output


def test_declaration_diagnostic_feedback_reason_prefers_structured_items_over_text(tmp_path):
    """Pin the helper contract: a structured warning inside the assigned
    declaration's range wins over text-form parsing, so `lean_interact`
    output that text-parsers cannot locate still produces a cleanup reason."""

    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  have h : True := by",
                "    all_goals trivial",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    text_only = runner._declaration_diagnostic_feedback_reason(
        str(active),
        "demo",
        "warning: 'all_goals trivial' tactic does nothing",
    )
    structured = runner._declaration_diagnostic_feedback_reason(
        str(active),
        "demo",
        "warning: 'all_goals trivial' tactic does nothing",
        structured_items=[
            {
                "severity": "warning",
                "message": "'all_goals trivial' tactic does nothing",
                "line": 3,
                "column": 5,
            }
        ],
    )

    # Without structured items, the regex fallback cannot locate the warning
    # because there is no `<file>:<line>:<col>:` prefix.
    assert text_only == ""
    assert "warning near line 3" in structured
    assert "all_goals trivial" in structured


def test_declaration_diagnostic_feedback_reason_accepts_lean_interact_file_start(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  have h : True := by",
                "    all_goals trivial",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    structured = runner._declaration_diagnostic_feedback_reason(
        str(active),
        "demo",
        "warning: this tactic is never executed",
        structured_items=[
            {
                "severity": "warning",
                "message": "this tactic is never executed",
                "start": {"line": 300, "column": 1},
                "file_start": {"line": 3, "column": 5},
            }
        ],
    )

    assert "warning near line 3" in structured
    assert "never executed" in structured


def test_handle_managed_tool_result_keeps_same_theorem_for_local_warning_cleanup(
    monkeypatch, tmp_path
):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  trivial",
                "",
                "theorem next_demo : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "slice": "theorem demo : True := by\n  trivial",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {
            "ok": True,
            "command": "lake env lean Main.lean",
            "output": f"{active}:2:3: warning: This line exceeds the 100 character limit, please shorten it!",
        },
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "next_demo",
            "active_file": str(active),
            "active_file_label": "Main.lean",
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "current_queue_item_slice": "theorem next_demo : True := by\n  sorry",
            "diagnostics": "warning: declaration uses sorry",
            "goals": "no goals",
            "build_status": "unknown",
            "blocker_summary": "warning: declaration uses sorry",
        },
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    assert "failed_attempts" not in agent._managed_autonomy_state
    assert "THEOREM FEEDBACK" in agent._post_tool_result_appendix
    assert "warning-only cleanup" in agent._post_tool_result_appendix
    assert "local cleanup" in agent._post_tool_result_appendix
    assert "do not edit future queued declarations" in agent._post_tool_result_appendix
    # Per-theorem cleanup appendix must (a) require an attempt before the
    # bail clause, (b) name concrete low-risk fixes, (c) make the bail
    # clause conditional on having read the declaration first. Prevents the
    # model from running `lean_verify` and immediately declaring done.
    assert "expected effort" in agent._post_tool_result_appendix.lower()
    assert "at least one safe edit" in agent._post_tool_result_appendix.lower()
    assert "all_goals" in agent._post_tool_result_appendix.lower()
    assert "bail clause" in agent._post_tool_result_appendix.lower()
    assert "inspected the declaration first" in agent._post_tool_result_appendix.lower()
    assert (
        runner._manager_feedback_retry_count(
            agent._managed_autonomy_state,
            target_symbol="demo",
            active_file=str(active),
            kind="warning",
        )
        == 1
    )
    assert agent.interrupt_messages == []
    assert agent._managed_pending_theorem_feedback is None


def test_handle_managed_tool_result_yields_after_warning_cleanup_retry(
    monkeypatch, tmp_path, capsys
):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  trivial",
                "",
                "theorem later : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    class _Agent(_ManagedRunAgentStub):
        quiet_mode = False

        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "slice": "theorem demo : True := by\n  trivial",
                }
            }
            runner._increment_manager_feedback_retry(
                self._managed_autonomy_state,
                target_symbol="demo",
                active_file=str(active),
                kind="warning",
            )
            self._managed_pending_theorem_feedback = None
            self._managed_step_boundary_closed = False
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {
            "ok": True,
            "command": "lake env lean Main.lean",
            "output": f"{active}:2:3: warning: This line exceeds the 100 character limit, please shorten it!",
        },
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "demo",
            "active_file": str(active),
            "active_file_label": "Main.lean",
            "current_queue_item": {"label": "demo", "reasons": ["diagnostic near line 2"]},
            "current_queue_item_slice": "theorem demo : True := by\n  trivial",
            "diagnostics": f"{active}:2:3: warning: This line exceeds the 100 character limit, please shorten it!",
            "goals": "no goals",
            "build_status": "unknown",
            "blocker_summary": "diagnostic near line 2",
        },
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    assert "failed_attempts" not in agent._managed_autonomy_state
    assert "manager_feedback_retries" not in agent._managed_autonomy_state
    assert not hasattr(agent, "_post_tool_result_appendix")
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    assert agent._managed_step_boundary_closed is True
    output = capsys.readouterr().out
    assert "warning-only cleanup opportunity already used" in output


def test_handle_managed_tool_result_yields_after_hard_retry_limit(monkeypatch, tmp_path, capsys):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    events = []

    class _Agent(_ManagedRunAgentStub):
        quiet_mode = False

        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }
            for index in range(runner.MANAGER_POST_EDIT_HARD_RETRY_LIMIT):
                runner._increment_manager_feedback_retry(
                    self._managed_autonomy_state,
                    target_symbol="demo",
                    active_file=str(active),
                    kind="error",
                    signature=f"previous-{index}",
                )
            self._managed_pending_theorem_feedback = None
            self._managed_step_boundary_closed = False
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {
            "ok": False,
            "command": "lake env lean Main.lean",
            "output": f"{active}:2:3: error: unsolved goals",
        },
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "demo",
            "active_file": str(active),
            "active_file_label": "Main.lean",
            "current_queue_item": {"label": "demo", "reasons": ["diagnostic near line 2"]},
            "current_queue_item_slice": "theorem demo : True := by\n  trivial",
            "diagnostics": f"{active}:2:3: error: unsolved goals",
            "goals": "unsolved goals",
            "build_status": "error",
            "blocker_summary": "error: unsolved goals",
        },
    )
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    output = capsys.readouterr().out
    text = active.read_text(encoding="utf-8")
    assert "Manager retry limit reached for demo" in output
    assert "-- LeanFlow failed attempt preserved after API step budget exhaustion." in text
    assert "theorem demo : True := by\n  sorry" in text
    assert not hasattr(agent, "_post_tool_result_appendix")
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    assert agent._managed_step_boundary_closed is True
    assert any(kwargs.get("hard_retry_exhausted") is True for _, kwargs in events)


def test_manager_feedback_kind_treats_nonzero_verification_as_error(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    result = runner._manager_feedback_kind(
        str(active),
        "demo",
        {
            "ok": False,
            "output": f"{active}:2:3: warning: this tactic is never executed",
        },
    )

    assert result == "error"


def test_manager_feedback_kind_adapter_golden_cases(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  trivial",
                "",
                "theorem future : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    assigned_error = runner._manager_check_for_feedback_kind(
        str(active),
        "demo",
        {
            "ok": False,
            "output": '{"items":[{"severity":"error","message":"type mismatch","line":2}]}',
        },
    )
    assigned_warning = runner._manager_check_for_feedback_kind(
        str(active),
        "demo",
        {"ok": True, "output": '{"items":[{"severity":"warning","message":"style","line":2}]}'},
    )
    future_only = runner._manager_check_for_feedback_kind(
        str(active),
        "demo",
        {
            "ok": False,
            "output": '{"items":[{"severity":"error","message":"future failed","line":5}]}',
        },
    )
    no_decl_evidence = runner._manager_check_for_feedback_kind(
        str(active),
        "demo",
        {"ok": False, "output": "lean verification failed without scoped diagnostics"},
    )
    clean = runner._manager_check_for_feedback_kind(str(active), "demo", {"ok": True, "output": ""})

    assert runner.classify_check(assigned_error) is runner.Classification.HARD_BLOCKER
    assert (
        runner._manager_feedback_kind(
            str(active),
            "demo",
            {
                "ok": False,
                "output": '{"items":[{"severity":"error","message":"type mismatch","line":2}]}',
            },
        )
        == "error"
    )
    assert runner.classify_check(assigned_warning) is runner.Classification.WARNING_ONCE
    assert runner.classify_check(future_only) is runner.Classification.FUTURE_ONLY
    assert (
        runner._manager_feedback_kind(
            str(active),
            "demo",
            {
                "ok": False,
                "output": '{"items":[{"severity":"error","message":"future failed","line":5}]}',
            },
        )
        == ""
    )
    assert runner.classify_check(no_decl_evidence) is runner.Classification.HARD_BLOCKER
    assert runner.classify_check(clean) is runner.Classification.ACCEPT


def test_manager_feedback_kind_adapter_detects_assigned_sorry(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    assert runner._manager_feedback_kind(str(active), "demo", {"ok": True, "output": ""}) == "sorry"


def test_handle_managed_tool_result_yields_for_unrelated_warning_cleanup(
    monkeypatch, tmp_path, capsys
):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  trivial",
                "",
                "theorem next_demo : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    class _Agent(_ManagedRunAgentStub):
        quiet_mode = False

        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "slice": "theorem demo : True := by\n  trivial",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {
            "ok": False,
            "command": "lake env lean Main.lean",
            "output": f"{active}:4:1: warning: This line exceeds the 100 character limit, please shorten it!",
        },
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "",
            "active_file": str(active),
            "active_file_label": "Main.lean",
            "current_queue_item": {},
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "unknown",
            "blocker_summary": "",
        },
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    assert not hasattr(agent, "_post_tool_result_appendix")
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    output = capsys.readouterr().out
    assert "file verification still has remaining blockers" in output


def test_handle_managed_tool_result_supports_interrupted_property(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        @property
        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {"ok": True, "command": "lake env lean Demo/Main.lean"},
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "next_demo",
            "active_file": "Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "current_queue_item_slice": "theorem next_demo : True := by\n  sorry",
            "diagnostics": "warning: declaration uses sorry",
            "goals": "no goals",
            "build_status": "unknown",
            "blocker_summary": "warning: declaration uses sorry",
        },
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")

    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    assert agent._managed_pending_theorem_feedback is None


def test_handle_managed_tool_result_logs_target_verification_and_stores_record(monkeypatch, capsys):
    class _Agent(_ManagedRunAgentStub):
        quiet_mode = False

        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  trivial",
                }
            }
            self._managed_pending_theorem_feedback = {
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
            }
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None, autonomy_state=None: {
            "target_symbol": "next_demo",
            "active_file": "Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "current_queue_item_slice": "theorem next_demo : True := by\n  sorry",
            "diagnostics": "warning: declaration uses sorry",
            "goals": "no goals",
            "build_status": "",
            "blocker_summary": "",
        },
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    agent = _Agent()
    runner._handle_managed_tool_result(
        agent,
        "lean_incremental_check",
        {"action": "check_target"},
        json.dumps(
            {
                "success": True,
                "ok": True,
                "action": "check_target",
                "target": "demo",
                "elapsed_s": 0.42,
                "cache": {"cache_hit": True},
                "messages": [],
            }
        ),
    )

    output = capsys.readouterr().out
    assert "Manager verification (target demo): passed" in output
    assert agent._managed_autonomy_state["last_verification"]["scope"] == "target:demo"
    assert agent._managed_autonomy_state["last_verification"]["tool"] == "lean_incremental_check"


def test_handle_managed_tool_result_disables_auto_try_schema_for_run(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self.tools = [
                {"type": "function", "function": {"name": "lean_auto_try"}},
                {"type": "function", "function": {"name": "lean_inspect"}},
            ]
            self.valid_tool_names = {"lean_auto_try", "lean_inspect"}
            self._managed_autonomy_state = {}

        def is_interrupted(self):
            return False

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    agent = _Agent()

    runner._handle_managed_tool_result(
        agent,
        "lean_auto_try",
        {},
        json.dumps(
            {
                "success": False,
                "degraded_reasons": [
                    "lean automation try disabled for this run before MCP call because the project contains an option the backend rejects"
                ],
            }
        ),
    )

    assert "lean_auto_try" not in agent.valid_tool_names
    assert [tool["function"]["name"] for tool in agent.tools] == ["lean_inspect"]
    assert agent._managed_autonomy_state["disabled_tools_this_run"][0]["name"] == "lean_auto_try"


def test_handle_managed_tool_result_does_not_treat_inspect_as_verification_feedback(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }
            self._managed_pending_theorem_feedback = None

        def is_interrupted(self):
            return False

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    agent = _Agent()
    runner._handle_managed_tool_result(
        agent,
        "lean_inspect",
        {},
        json.dumps({"success": True, "diagnostics": "warning: declaration uses `sorry`"}),
    )

    assert not hasattr(agent, "_post_tool_result_appendix")
    assert agent._managed_pending_theorem_feedback is None
    assert "failed_attempts" not in agent._managed_autonomy_state


def test_review_agent_final_report_accepts_claim_only_after_manager_check(
    monkeypatch, tmp_path, capsys
):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    events = []

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "project")
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {
            "ok": True,
            "command": "lake env lean Main.lean",
            "output": "lake env lean Main.lean succeeded",
        },
    )
    monkeypatch.setattr(
        runner, "_query_live_diagnostics", lambda active_file, target_symbol="": "no errors found"
    )
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    result = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "`demo` solved. `lake env lean Main.lean` passes.",
            "messages": [{"role": "assistant", "content": "`demo` solved."}],
        },
        {"current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}},
    )

    assert result["manager_final_report_review"]["ok"] is True
    assert result["messages"] == [{"role": "assistant", "content": "`demo` solved."}]
    output = capsys.readouterr().out
    assert "Manager review of agent final report for demo: accepted" in output
    assert "Queue step boundary: demo verified" in output
    assert events


def test_review_agent_final_report_accepts_incremental_queue_success_with_future_file_errors(
    monkeypatch, tmp_path
):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  trivial",
                "",
                "theorem later : True := by",
                "  exact bad",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "file")
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda active_file, target_symbol: {
            "ok": True,
            "mode": "incremental_target",
            "command": "lean_interact check_target",
            "target": target_symbol,
            "output": "",
            "incremental": {
                "success": True,
                "ok": True,
                "valid_without_sorry": True,
                "has_errors": False,
                "has_sorry": False,
            },
        },
    )
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: pytest.fail(
            "clean assigned declarations must not be rejected by future file errors"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_query_live_diagnostics",
        lambda active_file, target_symbol="": pytest.fail(
            "clean target check should not query file-wide diagnostics"
        ),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    result = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "`demo` solved.",
            "messages": [{"role": "assistant", "content": "`demo` solved."}],
        },
        {"current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}},
    )

    assert result["manager_final_report_review"]["ok"] is True
    assert result["manager_final_report_review"]["manager_tool"] == "lean_incremental_check"
    assert len(result["messages"]) == 1


def test_review_agent_final_report_does_not_classify_future_errors_as_current_feedback(
    monkeypatch, tmp_path
):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  trivial",
                "",
                "theorem later : True := by",
                "  exact bad",
            ]
        ),
        encoding="utf-8",
    )
    future_error = f"{active}:5:8: error: unknown identifier 'bad'"

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "file")
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda active_file, target_symbol: {
            "ok": True,
            "mode": "incremental_target",
            "command": "lean_interact check_target",
            "target": target_symbol,
            "output": future_error,
            "incremental": {
                "success": True,
                "ok": True,
                "valid_without_sorry": True,
                "has_errors": False,
                "has_sorry": False,
            },
        },
    )
    monkeypatch.setattr(
        runner,
        "_query_live_diagnostics",
        lambda active_file, target_symbol="": pytest.fail(
            "clean target check should not query file-wide diagnostics"
        ),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    result = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "`demo` solved.",
            "messages": [{"role": "assistant", "content": "`demo` solved."}],
        },
        {"current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}},
    )

    review = result["manager_final_report_review"]
    assert review["ok"] is True
    assert review["manager_tool"] == "lean_incremental_check"
    assert "feedback_kind" not in review
    assert len(result["messages"]) == 1


def test_review_agent_final_report_rejects_same_declaration_warning(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "project")
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {
            "ok": True,
            "command": "lake env lean Main.lean",
            "output": f"{active}:2:3: warning: The `cases'` tactic is discouraged",
        },
    )
    monkeypatch.setattr(
        runner,
        "_query_live_diagnostics",
        lambda active_file, target_symbol="": pytest.fail(
            "final-report cleanup should use manager check output"
        ),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    result = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "`demo` solved and verified.",
            "messages": [{"role": "assistant", "content": "`demo` solved."}],
        },
        {"current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}},
    )

    assert result["manager_final_report_review"]["ok"] is False
    assert "local_cleanup_reason" in result["manager_final_report_review"]
    assert "local cleanup" in result["messages"][-1]["content"]
    assert "blocker kind: warning" in result["messages"][-1]["content"]
    assert "do not solve unrelated future queue items" in result["messages"][-1]["content"]


def test_review_agent_final_report_ignores_same_declaration_info_diagnostics(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n#check True\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "file")
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda active_file, target_symbol: {
            "ok": True,
            "mode": "incremental_target",
            "command": "lean_interact check_target",
            "target": target_symbol,
            "output": "Main.lean:3:1: info: True : Prop",
            "incremental": {"success": True, "ok": True},
        },
    )
    monkeypatch.setattr(
        runner,
        "_query_live_diagnostics",
        lambda active_file, target_symbol="": pytest.fail(
            "final-report cleanup should use manager check output"
        ),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    result = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "`demo` solved.",
            "messages": [{"role": "assistant", "content": "`demo` solved."}],
        },
        {"current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}},
    )

    assert result["manager_final_report_review"]["ok"] is True
    assert "local_cleanup_reason" not in result["manager_final_report_review"]


def test_review_agent_final_report_accepts_warning_only_after_one_retry(
    monkeypatch, tmp_path, capsys
):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "project")
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {
            "ok": True,
            "command": "lake env lean Main.lean",
            "output": f"{active}:2:3: warning: The `cases'` tactic is discouraged",
        },
    )
    monkeypatch.setattr(
        runner,
        "_query_live_diagnostics",
        lambda active_file, target_symbol="": pytest.fail(
            "final-report cleanup should use manager check output"
        ),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    autonomy_state = {
        "current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}
    }

    first = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "`demo` solved and verified.",
            "messages": [{"role": "assistant", "content": "`demo` solved."}],
        },
        autonomy_state,
    )
    second = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "`demo` solved and verified.",
            "messages": [{"role": "assistant", "content": "`demo` solved."}],
        },
        autonomy_state,
    )

    assert first["manager_final_report_review"]["ok"] is False
    assert second["manager_final_report_review"]["ok"] is True
    assert second["manager_final_report_review"]["accepted_after_warning_retry_limit"] is True
    assert len(second["messages"]) == 1
    output = capsys.readouterr().out
    assert "warning-only cleanup opportunity already used" in output


def test_review_agent_final_report_restores_sorry_after_hard_retry_limit(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "theorem demo : True := by\n  exact False.elim ?bad\n",
        encoding="utf-8",
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "theorem demo : True := by\n  sorry",
        }
    }
    runner._increment_manager_feedback_retry(
        autonomy_state,
        target_symbol="demo",
        active_file=str(active),
        kind="error",
    )
    runner._increment_manager_feedback_retry(
        autonomy_state,
        target_symbol="demo",
        active_file=str(active),
        kind="error",
    )

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "project")
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {
            "ok": False,
            "command": "lake env lean Main.lean",
            "output": "Main.lean:2:3: error: unsolved goals",
        },
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    result = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "`demo` solved and verified.",
            "messages": [{"role": "assistant", "content": "`demo` solved."}],
        },
        autonomy_state,
    )

    assert result["completed"] is False
    assert result["exit_reason"] == "manager_retry_exhausted"
    assert result["manager_final_report_review"]["retry_exhausted"] is True
    assert result["manager_final_report_review"]["restore"]["restored"] is True
    assert "LEANFLOW-NATIVE MANAGER RETRY LIMIT REACHED" in result["messages"][-1]["content"]
    text = active.read_text(encoding="utf-8")
    assert "-- LeanFlow failed attempt preserved after API step budget exhaustion." in text
    assert "theorem demo : True := by\n  sorry" in text


def test_review_agent_final_report_rejects_claim_with_manager_feedback(
    monkeypatch, tmp_path, capsys
):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_declaration_queue_scope", lambda: "project")
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {
            "ok": False,
            "command": "lake env lean Main.lean",
            "output": "Main.lean:2:3: error: unsolved goals",
        },
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    result = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "`demo` solved and verified.",
            "messages": [{"role": "assistant", "content": "`demo` solved."}],
        },
        {"current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}},
    )

    assert result["manager_final_report_review"]["ok"] is False
    assert len(result["messages"]) == 2
    assert result["messages"][-1]["role"] == "user"
    assert "LEANFLOW-NATIVE MANAGER REVIEW" in result["messages"][-1]["content"]
    assert "continue the same theorem" in result["messages"][-1]["content"]
    output = capsys.readouterr().out
    assert "needs work" in output
    assert "Queue step boundary: demo needs manager feedback" in output


def test_declaration_queue_ignores_names_referenced_only_in_future_error_context(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "def isLipschitz : Prop := True",
                "",
                "theorem clean : isLipschitz := by",
                "  trivial",
                "",
                "theorem later : True := by",
                "  exact bad",
            ]
        ),
        encoding="utf-8",
    )
    diagnostics = (
        '{"items":[{"severity":"error","message":"h : isLipschitz\\nunknown identifier bad",'
        '"line":7,"column":8}]}'
    )

    queue = runner._declaration_work_queue(str(active), diagnostics, scope="file")

    labels = [item["label"] for item in queue]
    assert "later" in labels
    assert "isLipschitz" not in labels
    assert "clean" not in labels


def test_declaration_cleanup_ignores_info_diagnostics(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "theorem demo : True := by\n  trivial\n#check True\n",
        encoding="utf-8",
    )

    reason = runner._declaration_diagnostic_feedback_reason(
        str(active),
        "demo",
        f"{active}:2:3: info: True : Prop",
    )

    assert reason == ""


def test_same_queue_assignment_ignores_future_file_errors_when_assigned_clean(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  trivial",
                "",
                "theorem later : True := by",
                "  exact bad",
            ]
        ),
        encoding="utf-8",
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        }
    }
    live_state = {
        "target_symbol": "demo",
        "active_file": str(active),
        "current_queue_item": {"label": "demo", "reasons": []},
        "diagnostics": f"{active}:5:8: error: unknown identifier 'bad'",
        "goals": "no goals",
        "build_status": "lake env lean Main.lean reported errors",
        "blocker_summary": "",
    }

    assert runner._same_queue_assignment_still_blocked(autonomy_state, live_state) is False


def test_managed_pre_tool_call_blocks_terminal_edits_in_queue(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    class _Agent(_ManagedRunAgentStub):
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            }
        }

        def is_interrupted(self):
            return False

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    result = runner._managed_pre_tool_call(
        _Agent(),
        "terminal",
        {"command": "sed -i '' 's/sorry/trivial/' Main.lean"},
    )

    payload = json.loads(result)
    assert payload["success"] is False
    assert "terminal-based file edits" in payload["error"]
    assert active.read_text(encoding="utf-8") == "theorem demo : True := by\n  sorry\n"
    assert (
        runner._managed_pre_tool_call(
            _Agent(),
            "terminal",
            {"command": "lake env lean Main.lean 2>&1"},
        )
        is None
    )


def test_prepare_queue_assignment_state_warms_incremental_once(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        runner,
        "_manager_prepare_incremental_queue_item",
        lambda active_file, target_symbol: (
            calls.append((active_file, target_symbol))
            or {
                "success": True,
                "ok": True,
                "backend": "lean_interact",
                "action": "prepare_file",
                "target": target_symbol,
                "elapsed_s": 0.1,
                "cache": {"cache_hit": False},
            }
        ),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    autonomy_state = {}
    live_state = {
        "active_file": str(active),
        "current_queue_item": {"label": "demo"},
        "current_queue_item_slice": active.read_text(encoding="utf-8"),
    }

    runner._prepare_queue_assignment_state(autonomy_state, live_state)
    runner._prepare_queue_assignment_state(autonomy_state, live_state)

    assert calls == [(str(active), "demo")]
    assert autonomy_state["current_queue_assignment"]["incremental_prepare"]["success"] is True


def test_manager_incremental_prepare_uses_cold_mathlib_timeout(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    active = project / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    calls = []

    def _fake_incremental_check(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "ok": True,
            "backend": "lean_interact",
            "action": "prepare_file",
            "target": "demo",
            "elapsed_s": 201.196,
            "cache": {"cache_hit": False},
        }

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.delenv("LEANFLOW_MANAGER_INCREMENTAL_PREPARE_TIMEOUT_S", raising=False)
    monkeypatch.setattr(runner, "lean_incremental_check", _fake_incremental_check)

    result = runner._manager_prepare_incremental_queue_item(str(active), "demo")

    assert result["success"] is True
    assert calls[0]["action"] == "prepare_file"
    assert calls[0]["timeout_s"] == runner.MANAGER_INCREMENTAL_PREPARE_TIMEOUT_DEFAULT_S
    assert calls[0]["timeout_s"] >= 240


def test_manager_incremental_check_uses_configurable_timeout(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    project.mkdir()
    active = project / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    calls = []

    def _fake_incremental_check(**kwargs):
        calls.append(kwargs)
        return {
            "success": True,
            "ok": True,
            "backend": "lean_interact",
            "command": "lean_probe check_target",
            "target": "demo",
            "output": "",
            "messages": [],
            "cache": {"cache_hit": True},
            "elapsed_s": 0.04,
        }

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_MANAGER_INCREMENTAL_CHECK_TIMEOUT_S", "180")
    monkeypatch.setattr(runner, "lean_incremental_check", _fake_incremental_check)

    result = runner._manager_incremental_check_queue_item(str(active), "demo")

    assert result["ok"] is True
    assert calls[0]["action"] == "check_target"
    assert calls[0]["timeout_s"] == 180


def test_out_of_scope_queue_edit_guard_restores_future_declarations(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "theorem demo : True := by\n  sorry\n\ntheorem later : True := by\n  sorry\n",
        encoding="utf-8",
    )

    class _Agent(_ManagedRunAgentStub):
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            }
        }

        def is_interrupted(self):
            return False

    agent = _Agent()
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    assert runner._managed_pre_tool_call(agent, "patch", {"path": str(active)}) is None
    active.write_text(
        "theorem demo : True := by\n  trivial\n\ntheorem later : True := by\n  trivial\n",
        encoding="utf-8",
    )

    feedback = runner._restore_out_of_scope_queue_edit(agent, "patch")

    assert "restored those protected declarations" in feedback
    assert "later" in feedback
    assert active.read_text(encoding="utf-8") == (
        "theorem demo : True := by\n  trivial\n\ntheorem later : True := by\n  sorry\n"
    )


def test_out_of_scope_queue_edit_guard_allows_new_helper_declarations(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "theorem demo : True := by\n  sorry\n\ntheorem later : True := by\n  sorry\n",
        encoding="utf-8",
    )

    class _Agent(_ManagedRunAgentStub):
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            }
        }

        def is_interrupted(self):
            return False

    agent = _Agent()
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    assert runner._managed_pre_tool_call(agent, "patch", {"path": str(active)}) is None
    active.write_text(
        "private lemma demo_helper : True := by\n"
        "  trivial\n"
        "\n"
        "theorem demo : True := by\n"
        "  trivial\n"
        "\n"
        "theorem later : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )

    feedback = runner._restore_out_of_scope_queue_edit(agent, "patch")

    assert feedback == ""
    assert "demo_helper" in active.read_text(encoding="utf-8")


def test_out_of_scope_queue_edit_guard_allows_iterating_on_added_helpers(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "theorem demo : True := by\n  sorry\n\ntheorem later : True := by\n  sorry\n",
        encoding="utf-8",
    )

    class _Agent(_ManagedRunAgentStub):
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            }
        }

        def is_interrupted(self):
            return False

    agent = _Agent()
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    assert runner._managed_pre_tool_call(agent, "patch", {"path": str(active)}) is None
    active.write_text(
        "lemma demo_helper : True := by\n"
        "  trivial\n"
        "\n"
        "theorem demo : True := by\n"
        "  exact demo_helper\n"
        "\n"
        "theorem later : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    assert runner._restore_out_of_scope_queue_edit(agent, "patch") == ""

    assert runner._managed_pre_tool_call(agent, "patch", {"path": str(active)}) is None
    active.write_text(
        "lemma demo_helper : True := by\n"
        "  exact True.intro\n"
        "\n"
        "theorem demo : True := by\n"
        "  exact demo_helper\n"
        "\n"
        "theorem later : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )

    assert runner._restore_out_of_scope_queue_edit(agent, "patch") == ""
    assert "exact True.intro" in active.read_text(encoding="utf-8")


def test_queue_statement_guard_restores_initial_assigned_statement_change(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    original = "theorem demo : True := by\n  sorry\n"
    active.write_text(original, encoding="utf-8")

    class _Agent(_ManagedRunAgentStub):
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            }
        }

        def is_interrupted(self):
            return False

    agent = _Agent()
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    assert runner._managed_pre_tool_call(agent, "patch", {"path": str(active)}) is None
    active.write_text("theorem demo : False := by\n  trivial\n", encoding="utf-8")

    feedback = runner._restore_out_of_scope_queue_edit(agent, "patch")

    assert "QUEUE STATEMENT GUARD" in feedback
    assert "protected source statement" in feedback
    assert active.read_text(encoding="utf-8") == original


def test_queue_statement_guard_allows_model_created_helper_statement_change(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    class _Agent(_ManagedRunAgentStub):
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            }
        }

        def is_interrupted(self):
            return False

    agent = _Agent()
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    assert runner._managed_pre_tool_call(agent, "patch", {"path": str(active)}) is None
    active.write_text(
        "lemma demo_helper : True := by\n"
        "  trivial\n"
        "\n"
        "theorem demo : True := by\n"
        "  exact demo_helper\n",
        encoding="utf-8",
    )
    assert runner._restore_out_of_scope_queue_edit(agent, "patch") == ""

    agent._managed_autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo_helper",
            "active_file": str(active),
        }
    }
    assert runner._managed_pre_tool_call(agent, "patch", {"path": str(active)}) is None
    active.write_text(
        "lemma demo_helper : And True True := by\n"
        "  exact And.intro trivial trivial\n"
        "\n"
        "theorem demo : True := by\n"
        "  exact demo_helper\n",
        encoding="utf-8",
    )

    assert runner._restore_out_of_scope_queue_edit(agent, "patch") == ""
    assert "And True True" in active.read_text(encoding="utf-8")


def test_formalization_queue_statement_guard_protects_source_declaration(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    original = (
        "import Mathlib\n\n"
        "/-- Source proof: the source proof closes the toy claim directly. -/\n"
        "theorem t : True := by\n"
        "  sorry\n"
    )
    active.write_text(original, encoding="utf-8")
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "## Source Statement Inventory\n\n"
        "### thm:demo\n"
        "- Source locator: `docs/paper.tex:1-3`\n"
        "- Planned Lean declarations: `t`\n"
        "- Formal statement review: source statement exactly matches `True`\n"
        "- Source proof / prover notes: prove by `trivial`\n",
        encoding="utf-8",
    )
    manifest = (
        project / ".leanflow" / "workflow-state" / "formalization" / "paper" / "manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {"theorem_blocks": [{"label": "thm:demo", "kind": "theorem", "proof": "trivial"}]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_MANIFEST", str(manifest))

    class _Agent(_ManagedRunAgentStub):
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "t",
                "active_file": str(active),
            }
        }

        def is_interrupted(self):
            return False

    agent = _Agent()
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    assert runner._managed_pre_tool_call(agent, "patch", {"path": str(active)}) is None
    active.write_text(original.replace("theorem t : True", "theorem t : False"), encoding="utf-8")

    feedback = runner._restore_out_of_scope_queue_edit(agent, "patch")

    assert "QUEUE STATEMENT GUARD" in feedback
    assert active.read_text(encoding="utf-8") == original


def test_build_agent_registers_project_tool_cwd(monkeypatch, tmp_path):
    project = tmp_path / "Project"
    project.mkdir()
    registered = []

    class _Agent(_ManagedRunAgentStub):
        session_id = "abc123"

        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "model")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "key")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "provider")
    monkeypatch.delenv("LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS", raising=False)
    monkeypatch.setattr(runner, "AIAgent", _Agent)
    monkeypatch.setattr(
        "tools.implementations.terminal_tool.register_task_env_overrides",
        lambda task_id, overrides: registered.append((task_id, overrides)),
    )

    agent = runner._build_agent()

    assert os.environ["TERMINAL_CWD"] == str(project)
    assert os.environ["LEANFLOW_ALLOW_LEAN_STATEMENT_EDITS"] == "1"
    assert agent._managed_tool_task_id == "leanflow-native-abc123"
    assert registered == [("leanflow-native-abc123", {"cwd": str(project)})]


def test_handle_managed_tool_result_interrupts_even_if_live_refresh_fails(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "partial"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  sorry",
                }
            }
            self._managed_pending_theorem_feedback = None
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: (_ for _ in ()).throw(
            RuntimeError("lsp unavailable")
        ),
    )
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda active_file: {"ok": False, "command": "lake env lean Demo/Main.lean"},
    )
    recorded = []
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda kind, message, **details: recorded.append((kind, details)),
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "patch", {}, "")
    runner._handle_managed_tool_result(agent, "lean_verify", {}, "")

    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    assert agent._managed_pending_theorem_feedback is None
    assert recorded[-1][0] == "queue-step-boundary"
    assert "lsp unavailable" in recorded[-1][1]["refresh_error"]


def test_handle_managed_lean_verify_uses_current_assignment_without_pending(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        quiet_mode = True

        def __init__(self):
            self._session_messages = [{"role": "assistant", "content": "verified"}]
            self._managed_autonomy_state = {
                "current_queue_assignment": {
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "slice": "theorem demo : True := by\n  trivial",
                }
            }
            self._managed_pending_theorem_feedback = None
            self._managed_step_boundary_closed = False
            self.interrupt_messages: list[str | None] = []

        def is_interrupted(self):
            return False

        def interrupt(self, message=None):
            self.interrupt_messages.append(message)

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: {
            "target_symbol": "next_demo",
            "active_file": "Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "current_queue_item_slice": "theorem next_demo : True := by\n  sorry",
            "diagnostics": "warning: declaration uses sorry",
            "goals": "no goals",
            "build_status": "unknown",
            "blocker_summary": "warning: declaration uses sorry",
        },
    )

    agent = _Agent()
    runner._handle_managed_tool_result(agent, "lean_verify", {"mode": "file_exact"}, "")

    assert "failed_attempts" not in agent._managed_autonomy_state
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    assert agent._managed_step_boundary_closed is True


def test_background_control_loop_processes_queued_prompt_and_remote_exit(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    agent = _Agent()
    recorded: list[tuple[str, str, dict[str, object]]] = []
    persisted: list[str] = []
    queue_reads = iter(
        [
            [{"seq": 1, "kind": "message", "text": "Try another proof."}],
            [
                {"seq": 1, "kind": "message", "text": "Try another proof."},
                {"seq": 2, "kind": "exit", "text": "exit"},
            ],
        ]
    )

    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "chat")

    monkeypatch.setattr(
        runner,
        "_persist_live_status",
        lambda *args, phase=None, **kwargs: persisted.append(str(phase or "")),
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
    )
    monkeypatch.setattr(runner, "read_workflow_agent_inbox", lambda agent_id: next(queue_reads))
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state: {"message": "ok"}
    )
    monkeypatch.setattr(
        runner,
        "_promote_live_state_to_verified",
        lambda live_state: dict(live_state, verified=False),
    )
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda live_state: False)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_auto_compact_history",
        lambda history, agent, force=False: (history, {"compacted": False}),
    )
    monkeypatch.setattr(
        runner,
        "_run_managed_conversation",
        lambda *args, **kwargs: {
            "messages": [{"role": "assistant", "content": "done"}],
            "interrupted": False,
        },
    )
    monkeypatch.setattr(runner, "_record_turn_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_journal_status", lambda: {"count": 0, "current": {}})
    monkeypatch.setattr(
        runner,
        "_drive_autonomous_followups",
        lambda *args, **kwargs: (
            args[2],
            {"compacted": False},
            {"count": 0, "current": {}},
            {"verified": False},
        ),
    )
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
    assert any(
        event_type == "agent-resume" and details.get("text") == "Try another proof."
        for event_type, _, details in recorded
    )
    assert any(event_type == "runner-exit" for event_type, _, _ in recorded)
    assert "busy" in persisted
    assert "exited" in persisted


def test_background_control_loop_exits_after_verified_completion(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    agent = _Agent()
    recorded: list[tuple[str, str, dict[str, object]]] = []
    persisted: list[str] = []

    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "chat")

    monkeypatch.setattr(
        runner,
        "_persist_live_status",
        lambda *args, phase=None, **kwargs: persisted.append(str(phase or "")),
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
    )
    monkeypatch.setattr(
        runner,
        "read_workflow_agent_inbox",
        lambda agent_id: [{"seq": 1, "kind": "message", "text": "Finish the proof."}],
    )
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state: {"message": "ok"}
    )
    monkeypatch.setattr(
        runner,
        "_promote_live_state_to_verified",
        lambda live_state: dict(live_state, verified=bool(live_state.get("verified"))),
    )
    monkeypatch.setattr(
        runner, "_live_state_is_verified", lambda live_state: bool(live_state.get("verified"))
    )
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_auto_compact_history",
        lambda history, agent, force=False: (history, {"compacted": False}),
    )
    monkeypatch.setattr(
        runner,
        "_run_managed_conversation",
        lambda *args, **kwargs: {
            "messages": [{"role": "assistant", "content": "done"}],
            "interrupted": False,
        },
    )
    monkeypatch.setattr(runner, "_record_turn_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_journal_status", lambda: {"count": 0, "current": {}})
    monkeypatch.setattr(
        runner,
        "_drive_autonomous_followups",
        lambda *args, **kwargs: (
            args[2],
            {"compacted": False},
            {"count": 0, "current": {}},
            {"verified": True},
        ),
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
    assert any(
        event_type == "agent-resume" and details.get("text") == "Finish the proof."
        for event_type, _, details in recorded
    )
    assert any(
        event_type == "runner-exit" and "verified completion" in message
        for event_type, message, _ in recorded
    )
    assert "busy" in persisted
    assert "exited" in persisted


def test_terminate_descendant_agents_records_shutdown_activity(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    recorded: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "chat")
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
    )
    monkeypatch.setattr(
        runner,
        "terminate_workflow_agent_descendants",
        lambda agent_id: {
            "success": True,
            "count": 2,
            "terminated": ["22222", "33333"],
            "failed": [],
        },
    )

    runner._terminate_descendant_agents(_Agent())

    assert any(event_type == "descendants-terminated" for event_type, _, _ in recorded)


def test_terminate_other_agents_records_shutdown_activity(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    recorded: list[tuple[str, str, dict[str, object]]] = []
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "chat")
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
    )
    monkeypatch.setattr(
        runner,
        "terminate_project_workflow_agents",
        lambda project_root, **kwargs: {
            "success": True,
            "count": 2,
            "terminated": ["22222", "33333"],
            "failed": [],
        },
    )

    runner._terminate_other_agents(_Agent())

    assert any(event_type == "agents-terminated" for event_type, _, _ in recorded)


def test_background_runner_exits_immediately_after_verified_completion(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    recorded: list[tuple[str, str, dict[str, object]]] = []
    persisted: list[str] = []

    monkeypatch.setenv("LEANFLOW_NATIVE_INTERACTIVE", "0")
    monkeypatch.setattr(runner, "_install_workflow_run_log_capture", lambda: None)
    monkeypatch.setattr(runner, "_build_agent", lambda: _Agent())
    monkeypatch.setattr(runner, "_managed_system_prompt", lambda: "system")
    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(runner, "_print_header", lambda: None)
    monkeypatch.setattr(runner, "_startup_user_message", lambda resumed, **kwargs: "start")
    monkeypatch.setattr(runner, "_attach_live_proof_state", lambda text, live_state: text)
    monkeypatch.setattr(
        runner,
        "_run_managed_conversation",
        lambda *args, **kwargs: {
            "messages": [{"role": "assistant", "content": "done"}],
            "interrupted": False,
        },
    )
    monkeypatch.setattr(runner, "_record_turn_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_drive_autonomous_followups",
        lambda *args, **kwargs: (
            args[2],
            {"compacted": False},
            {},
            {
                "active_file": "/tmp/project/Main.lean",
                "diagnostics": "no errors found",
                "goals": "no goals",
                "sorry_count": 0,
                "project_sorry_count": 0,
                "verification_ok": True,
            },
        ),
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state: {
            "active_file": "/tmp/project/Main.lean",
            "diagnostics": "no errors found",
            "goals": "no goals",
            "sorry_count": 0,
            "project_sorry_count": 0,
        },
    )
    monkeypatch.setattr(
        runner,
        "_promote_live_state_to_verified",
        lambda live_state: dict(live_state, verification_ok=True),
    )
    monkeypatch.setattr(
        runner,
        "_live_state_is_verified",
        lambda live_state: bool(live_state.get("verification_ok")),
    )
    monkeypatch.setattr(
        runner,
        "_terminate_descendant_agents",
        lambda agent: recorded.append(("terminate", "descendants", {})),
    )
    monkeypatch.setattr(
        runner,
        "_persist_live_status",
        lambda *args, phase=None, **kwargs: persisted.append(str(phase or "")),
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
    )

    assert runner.main() == 0
    assert "exited" in persisted
    assert any(event_type == "terminate" for event_type, _, _ in recorded)
    assert any(
        event_type == "runner-exit" and "verified completion" in message
        for event_type, message, _ in recorded
    )
    assert any(
        event_type == "runner-start"
        and details.get("agent_session_id") == "12345"
        and details.get("process_id")
        for event_type, _, details in recorded
    )


def test_background_control_loop_handles_keyboard_interrupt_cleanly(monkeypatch):
    class _Agent(_ManagedRunAgentStub):
        session_id = "12345"
        _parent_session_id = ""
        _delegate_depth = 0

    recorded: list[tuple[str, str, dict[str, object]]] = []
    persisted: list[str] = []

    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "custom")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "chat")
    monkeypatch.setattr(
        runner,
        "_persist_live_status",
        lambda *args, phase=None, **kwargs: persisted.append(str(phase or "")),
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
    )
    monkeypatch.setattr(runner, "read_workflow_agent_inbox", lambda agent_id: [])
    monkeypatch.setattr(
        runner.time, "sleep", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    )

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
    assert any(
        event_type == "runner-exit" and "interrupted by signal" in message
        for event_type, message, _ in recorded
    )
    assert "exited" in persisted


def test_workflow_startup_guidance_mentions_autonomous_loop():
    text = runner._workflow_startup_guidance("prove", "/prove Main.lean")

    assert "autonomous proving session" in text
    assert "/prove Main.lean" in text
    assert "lean_capabilities" in text
    assert "lean_worker_dispatch" not in text


def test_workflow_startup_guidance_mentions_user_approved_swarm(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_PARALLEL_AGENTS", "3")
    monkeypatch.setenv("LEANFLOW_NATIVE_USER_APPROVED_SWARM", "1")

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
                "failed_attempt_reasoning_threshold": 5,
                "effective_reasoning_effort": "high",
                "reasoning_enabled": True,
                "cycle": 3,
            },
        )
    ]


def test_startup_user_message_snapshot_with_runner_lean_prompt(monkeypatch):
    monkeypatch.setenv("LEANFLOW_RUNNER_LEAN_PROMPT", "1")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "/tmp/project/Main.lean")
    monkeypatch.setattr(runner, "_project_root", lambda: "/tmp/project")
    monkeypatch.setattr(
        runner,
        "load_skill",
        lambda name, cwd=None: {
            "name": name,
            "source": "builtin",
            "content": "Queue worker contract body",
            "linked_files": {"workflow_specs": ["prove.md"]},
        },
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
        "use `lean_search` before guessing; the live queue, route decision, "
        "and verification gate below are the state for this turn.\n\n"
        "Route decision:\n"
        "- skill: lean-theorem-queue-worker\n"
        "- action: queue-worker\n"
        "- blocker kind: compiler\n"
        "- reason: queue item active\n\n"
        "Assigned queue item:\n"
        "- declaration: foo\n\n"
        "[LEANFLOW ACTIVE SKILL: lean-theorem-queue-worker (builtin)]\n\n"
        "Queue worker contract body\n\n"
        "Linked skill files are not expanded in startup; use `skill_view` if they become relevant.\n"
        "Linked workflow specs available through `skill_view`: formalize, prove."
    )


def test_startup_user_message_surfaces_effective_prompt(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", "use abs_abs_sub first")
    monkeypatch.setattr(runner, "_runner_lean_prompt_enabled", lambda: False)
    monkeypatch.setattr(runner, "_startup_active_skill_contract", lambda _name: "")
    monkeypatch.setattr(runner, "_startup_additional_skill_contracts", lambda _name: "")
    monkeypatch.setattr(runner, "_queue_assignment_block", lambda *args, **kwargs: "")
    monkeypatch.setattr(runner, "_swarm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "route_workflow_step",
        lambda *args, **kwargs: type("Route", (), {"to_dict": lambda self: {}})(),
    )

    prompt = runner._startup_user_message(live_state={}, autonomy_state={})

    assert "User prompt: use abs_abs_sub first" in prompt


def test_autonomous_continuation_prompt_snapshot_with_runner_lean_prompt(monkeypatch):
    monkeypatch.setenv("LEANFLOW_RUNNER_LEAN_PROMPT", "1")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
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
    monkeypatch.setattr(
        runner,
        "_recent_failed_attempts_summary",
        lambda *args, **kwargs: "Recent failed attempts:\n- same blocker twice",
    )

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
        "- reason: queue item active\n\n"
        "Assigned queue item:\n"
        "- declaration: foo"
    )


def test_history_status_lines_summarize_message_counts(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", "/tmp/project")

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


def test_build_agent_uses_leanflow_native_toolset(monkeypatch):
    captured = {}

    class DummyAgent(_ManagedRunAgentStub):
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
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "sk-test")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "zai")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "responses")
    monkeypatch.setenv("AGENT_MAX_TURNS", "77")

    runner._build_agent()

    assert captured["enabled_toolsets"] == ["leanflow-native"]
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


def test_build_agent_uses_runtime_reasoning_effort_when_config_auto(monkeypatch):
    captured = {}

    class DummyAgent(_ManagedRunAgentStub):
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
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "gpt-5.5")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://chatgpt.com/backend-api/codex")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "codex-token")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "openai-codex")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "codex_responses")
    monkeypatch.setenv("LEANFLOW_NATIVE_REASONING_EFFORT", "xhigh")

    runner._build_agent()

    assert captured["reasoning_config"] == {"enabled": True, "effort": "xhigh"}


def test_build_agent_uses_swarm_toolset_when_user_enabled_swarm(monkeypatch):
    captured = {}

    class DummyAgent(_ManagedRunAgentStub):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = "runner-session"

    monkeypatch.setattr(runner, "AIAgent", DummyAgent)
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setenv("LEANFLOW_NATIVE_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "sk-test")
    monkeypatch.setenv("LEANFLOW_NATIVE_PROVIDER", "zai")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_MODE", "responses")
    monkeypatch.setenv("LEANFLOW_NATIVE_TOOLSET", "leanflow-native-swarm")

    runner._build_agent()

    assert captured["enabled_toolsets"] == ["leanflow-native-swarm"]
    assert os.getenv("LEANFLOW_NATIVE_RUNNER_OWNER", "") == "runner-session"


def test_resolve_managed_reasoning_config_auto_defaults_to_high_for_new_theorem():
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

    assert resolved == {"enabled": True, "effort": "high"}


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


def test_resolve_managed_reasoning_config_auto_stays_high_below_default_threshold():
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
                for i in range(4)
            ]
        },
    )

    assert resolved == {"enabled": True, "effort": "high"}


def test_resolve_managed_reasoning_config_auto_uses_configured_threshold(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_FAILED_ATTEMPT_REASONING_THRESHOLD", "3")

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
                for i in range(3)
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


def test_apply_managed_reasoning_policy_keeps_high_on_theorem_transition():
    class _Agent(_ManagedRunAgentStub):
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
    assert second == {"enabled": True, "effort": "high"}
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}


def test_tool_progress_callback_persists_structured_events(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "lean-proof-loop")
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

    class _Agent(_ManagedRunAgentStub):
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
    assert history[0]["content"].endswith(
        "[leanflow-native pruned older tool output to preserve context budget]"
    )


def test_count_project_sorries_ignores_dependencies_and_build_dirs(tmp_path):
    project = tmp_path / "Demo"
    (project / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    (project / "build" / "ir").mkdir(parents=True)
    (project / ".leanflow" / "runtime").mkdir(parents=True)
    (project / "Demo").mkdir(parents=True)
    (project / "Demo" / "Main.lean").write_text(
        "theorem t : True := by\n  sorry\n", encoding="utf-8"
    )
    (project / ".lake" / "packages" / "mathlib" / "Ignored.lean").write_text(
        "theorem x : True := by\n  sorry\n", encoding="utf-8"
    )
    (project / "build" / "ir" / "Ignored.lean").write_text(
        "theorem y : True := by\n  sorry\n", encoding="utf-8"
    )

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


def test_declaration_ranges_do_not_include_next_declaration_doc_comment(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem first : True := by",
                "  trivial",
                "",
                "/-- Doc comment for the next theorem. -/",
                "theorem second : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    entries = runner._declaration_line_index(str(active))
    first = entries[0]

    assert first["name"] == "first"
    assert first["end_line"] == 2
    assert "Doc comment for the next theorem" not in first["text"]
    assert runner._diagnostic_reason_for_entry(first, [4]) == ""


def test_declaration_work_queue_scans_project_when_scope_is_project(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    first = module_dir / "A.lean"
    second = module_dir / "B.lean"
    first.write_text("theorem a : True := by\n  sorry\n", encoding="utf-8")
    second.write_text("theorem b : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    queue = runner._declaration_work_queue("", "", project_root=str(project), scope="project")

    assert len(queue) == 1
    assert queue[0]["label"] == "Demo/A.lean"
    assert queue[0]["reasons"] == ["1 sorry placeholder(s)"]


def test_project_prove_manager_uses_llm_file_order(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    first = module_dir / "A.lean"
    second = module_dir / "B.lean"
    first.write_text("theorem a : True := by\n  sorry\n", encoding="utf-8")
    second.write_text("theorem b : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove")
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    events: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    class _Message:
        content = '{"files": ["Demo/B.lean", "Demo/A.lean"], "reason": "B is shorter"}'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    monkeypatch.setattr(runner, "call_llm", lambda **kwargs: _Response())

    state: dict[str, object] = {}
    assert runner._ensure_project_prove_manager_started(state, phase="startup") is True

    assert os.environ["LEANFLOW_NATIVE_ACTIVE_FILE"] == "Demo/B.lean"
    assert state["project_prove_file_queue"] == ["Demo/B.lean", "Demo/A.lean"]
    assert state["project_prove_plan_source"] == "llm"
    assert [args[0] for args, _ in events] == [
        "project-prove-file-queue-planned",
        "project-prove-file-assigned",
    ]
    planned = events[0][1]
    assert planned["ordered_files"] == ["Demo/B.lean", "Demo/A.lean"]
    assert planned["plan_source"] == "llm"
    assert planned["total_candidates"] == 2


def test_project_prove_manager_llm_prompt_includes_context_and_dependency_data(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    base = module_dir / "Base.lean"
    later = module_dir / "Later.lean"
    base.write_text(
        "import Mathlib\n\n"
        "/-- Doc for the base theorem. -/\n"
        "theorem base_easy : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    later.write_text(
        "import Demo.Base\n\ntheorem later : True := by\n  sorry\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove")
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    prompts: list[str] = []

    class _Message:
        content = (
            '{"files": ["Demo/Base.lean", "Demo/Later.lean"], "reason": "Base unblocks Later"}'
        )

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    def _fake_call_llm(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _Response()

    monkeypatch.setattr(runner, "call_llm", _fake_call_llm)

    state: dict[str, object] = {}
    assert runner._ensure_project_prove_manager_started(state, phase="startup") is True

    prompt = prompts[0]
    assert "Doc for the base theorem" in prompt
    assert "theorem base_easy" in prompt
    assert "candidate_downstream_count" in prompt
    assert "Demo/Later.lean" in prompt
    assert '"kind": "full"' in prompt


def test_project_prove_manager_fallback_prefers_upstream_files(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    upstream = module_dir / "Base.lean"
    downstream = module_dir / "Later.lean"
    upstream.write_text("theorem base : True := by\n  sorry\n", encoding="utf-8")
    downstream.write_text(
        "import Demo.Base\n\ntheorem later : True := by\n  sorry\n", encoding="utf-8"
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove")
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "call_llm", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no aux"))
    )

    state: dict[str, object] = {}
    assert runner._ensure_project_prove_manager_started(state, phase="startup") is True

    assert os.environ["LEANFLOW_NATIVE_ACTIVE_FILE"] == "Demo/Base.lean"
    assert state["project_prove_file_queue"][0] == "Demo/Base.lean"
    assert state["project_prove_plan_source"] == "fallback"


def test_project_prove_manager_fallback_uses_transitive_candidate_dependencies(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    base = module_dir / "Base.lean"
    middle = module_dir / "Middle.lean"
    leaf = module_dir / "Leaf.lean"
    base.write_text("theorem base : True := by\n  sorry\n", encoding="utf-8")
    middle.write_text(
        "import Demo.Base\n\ntheorem middle : True := by\n  sorry\n", encoding="utf-8"
    )
    leaf.write_text("import Demo.Middle\n\ntheorem leaf : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    candidates = runner._collect_project_prove_file_candidates(project)
    by_label = {item["label"]: item for item in candidates}

    assert by_label["Demo/Base.lean"]["candidate_downstream_count"] == 2
    assert by_label["Demo/Middle.lean"]["candidate_downstream_count"] == 1
    assert by_label["Demo/Leaf.lean"]["candidate_downstream_count"] == 0
    assert runner._project_prove_fallback_order(candidates) == [
        "Demo/Base.lean",
        "Demo/Middle.lean",
        "Demo/Leaf.lean",
    ]


def test_project_prove_manager_fallback_prefers_hinted_easy_file(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    homework = module_dir / "Homework.lean"
    competition = module_dir / "Competition.lean"
    homework.write_text(
        "import Mathlib\n\n"
        "example (x : ℝ) : |x| ≥ 0 := by exact abs_nonneg x\n"
        "#check abs_nonneg\n"
        "-- TODO: prove this\n"
        "theorem absLipschitz1 : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    competition.write_text(
        "import Mathlib\n\ntheorem putnam_2020_a1 : True := by\n  sorry\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove")
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "call_llm", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no aux"))
    )

    state: dict[str, object] = {}
    assert runner._ensure_project_prove_manager_started(state, phase="startup") is True

    assert os.environ["LEANFLOW_NATIVE_ACTIVE_FILE"] == "Demo/Homework.lean"
    assert state["project_prove_file_queue"][0] == "Demo/Homework.lean"


def test_project_prove_manager_guards_llm_order_by_difficulty(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    homework = module_dir / "Homework.lean"
    competition = module_dir / "Competition.lean"
    homework.write_text(
        "import Mathlib\n\n"
        "example (x : ℝ) : |x| ≥ 0 := by exact abs_nonneg x\n"
        "#check abs_nonneg\n"
        "-- TODO: prove this\n"
        "theorem absLipschitz1 : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    competition.write_text(
        "import Mathlib\n\ntheorem putnam_2020_a1 : True := by\n  sorry\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove")
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    class _Message:
        content = '{"files": ["Demo/Competition.lean", "Demo/Homework.lean"], "reason": "bad model order"}'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    monkeypatch.setattr(runner, "call_llm", lambda **kwargs: _Response())

    state: dict[str, object] = {}
    assert runner._ensure_project_prove_manager_started(state, phase="startup") is True

    assert os.environ["LEANFLOW_NATIVE_ACTIVE_FILE"] == "Demo/Homework.lean"
    assert state["project_prove_file_queue"] == ["Demo/Homework.lean", "Demo/Competition.lean"]
    assert state["project_prove_plan_source"] == "llm"


def test_project_prove_manager_skips_artifact_lean_files(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    artifact_dir = project / ".artifacts" / "run1" / "Demo"
    module_dir.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    source = module_dir / "A.lean"
    artifact = artifact_dir / "Old.lean"
    source.write_text("theorem a : True := by\n  sorry\n", encoding="utf-8")
    artifact.write_text("theorem old : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    candidates = runner._collect_project_prove_file_candidates(project)
    labels = {item["label"] for item in candidates}

    assert labels == {"Demo/A.lean"}


def test_project_prove_manager_advances_to_next_file_after_verified_file(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    first = module_dir / "A.lean"
    second = module_dir / "B.lean"
    first.write_text("theorem a : True := by\n  trivial\n", encoding="utf-8")
    second.write_text("theorem b : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/A.lean")
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "call_llm", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no aux"))
    )
    monkeypatch.setattr(
        runner,
        "_live_state_is_verified",
        lambda live_state: bool(live_state.get("verification_ok")),
    )

    state: dict[str, object] = {
        "project_prove_manager_enabled": True,
        "project_prove_file_queue": ["Demo/A.lean", "Demo/B.lean"],
    }
    live_state = {
        "active_file": str(first),
        "active_file_label": "Demo/A.lean",
        "verification_ok": True,
        "project_sorry_count": 1,
    }

    assert (
        runner._advance_project_prove_manager_if_needed(state, live_state, phase="autonomous")
        is True
    )
    assert os.environ["LEANFLOW_NATIVE_ACTIVE_FILE"] == "Demo/B.lean"
    assert state["project_prove_completed_files"] == ["Demo/A.lean"]


def test_project_prove_manager_does_not_take_over_explicit_file(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "A.lean"
    active.write_text("theorem a : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo/A.lean")
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)

    state: dict[str, object] = {}
    assert runner._ensure_project_prove_manager_started(state, phase="startup") is False
    assert "project_prove_manager_enabled" not in state


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
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/Main.lean")
    monkeypatch.setattr(
        runner, "_resolve_target_symbol", lambda history, checkpoint_state=None: "outcome"
    )
    monkeypatch.setattr(
        runner,
        "_query_live_diagnostics",
        lambda path, symbol="": "lean-lsp diagnostics tool unavailable.",
    )
    monkeypatch.setattr(
        runner, "_query_live_goals", lambda path, symbol: "lean-lsp goals tool unavailable."
    )
    monkeypatch.setattr(runner, "_extract_recent_build_status", lambda history: "unknown")
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)

    live_state = runner._build_live_proof_state([])

    assert live_state["target_symbol"] == "first"
    assert live_state["current_queue_item"]["label"] == "first"
    assert "Current file prefix ending at `first`" in live_state["current_queue_item_prefix"]
    assert "theorem first" in live_state["current_queue_item_prefix"]
    assert "theorem first" in live_state["current_queue_item_slice"]


def test_declaration_prefix_text_keeps_last_200_lines(tmp_path):
    active = tmp_path / "Main.lean"
    prefix = [f"-- prefix marker {i}" for i in range(1, 221)]
    active.write_text(
        "\n".join(prefix + ["theorem demo : True := by", "  sorry"]),
        encoding="utf-8",
    )

    text = runner._declaration_prefix_text(str(active), "demo")
    lines = text.splitlines()

    assert "Current file prefix ending at `demo` (23-222)" in text
    assert "-- prefix marker 22" not in lines
    assert "-- prefix marker 23" in lines
    assert "theorem demo : True := by" in text


def test_declaration_work_queue_prefers_named_sorry_over_anonymous_diagnostic_noise(
    monkeypatch, tmp_path
):
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
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    queue = runner._declaration_work_queue(
        str(active),
        "lean-lsp diagnostics tool unavailable.",
        project_root=str(project),
        scope="file",
    )

    assert queue
    assert queue[0]["label"] == "first"
    assert queue[0]["reasons"] == ["contains sorry"]


def test_declaration_work_queue_leaves_style_warnings_for_final_sweep(tmp_path):
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
                "theorem already_done : True := by",
                "  trivial",
                "",
                "theorem later : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    queue = runner._declaration_work_queue(
        str(active),
        "\n".join(
            [
                "Demo/Main.lean:2:3: warning: This line exceeds the 100 character limit, please shorten it!",
                "Demo/Main.lean:4:1: warning: This line exceeds the 100 character limit, please shorten it!",
                "Demo/Main.lean:8:3: warning: declaration uses `sorry`",
            ]
        ),
        project_root=str(project),
        scope="file",
    )

    assert [item["label"] for item in queue] == ["later"]
    assert queue[0]["reasons"] == ["contains sorry"]


def test_declaration_work_queue_returns_empty_for_style_warnings_without_pending_proofs(tmp_path):
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
                "theorem already_done : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    queue = runner._declaration_work_queue(
        str(active),
        "Demo/Main.lean:2:3: warning: This line exceeds the 100 character limit, please shorten it!",
        project_root=str(project),
        scope="file",
    )

    assert queue == []


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
    assert (
        "diagnostic near line 4" in queue[0]["reasons"]
        or "referenced in diagnostics" in queue[0]["reasons"]
    )


def test_declaration_work_queue_maps_body_diagnostic_to_declaration(tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem broken : True := by",
                "  have h : False := by",
                "    exact ?missing",
                "  exact False.elim h",
                "",
                "theorem future_demo : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    queue = runner._declaration_work_queue(
        str(active),
        '{"severity": "error", "message": "unsolved goals", "line": 3, "column": 11}',
        project_root=str(project),
        scope="file",
    )

    assert queue
    assert queue[0]["label"] == "broken"
    assert "diagnostic near line 3" in queue[0]["reasons"]


def test_current_queue_item_prefers_diagnostic_blocker_before_later_sorry(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem broken : True := by",
                "  exact ?missing",
                "",
                "theorem later : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )
    queue = [
        {"label": "broken", "reasons": ["diagnostic near line 2"]},
        {"label": "later", "reasons": ["contains sorry"]},
    ]

    assert runner._current_queue_item(queue, str(active))["label"] == "broken"


def test_current_queue_item_does_not_select_clean_declaration(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem clean : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )
    queue = [{"label": "clean", "reasons": []}]

    assert runner._current_queue_item(queue, str(active)) is None


def test_diagnostics_for_queue_horizon_does_not_leak_unparseable_future_lines(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem assigned : True := by",
                "  trivial",
                "",
                "theorem future : True := by",
                "  exact bad",
            ]
        ),
        encoding="utf-8",
    )

    text = runner._diagnostics_for_queue_horizon(
        active_file=str(active),
        target_symbol="assigned",
        diagnostics="Lean said something failed around line 5: unknown identifier bad",
        declaration_scope="file",
        queue_needs_final_file_sweep=False,
    )

    assert "line 5" not in text
    assert "future queue items are hidden" not in text
    assert "could not be scoped reliably" in text


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


def test_declaration_work_queue_ignores_info_only_diagnostics_without_sorries(tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "def isLipschitz (f : Nat -> Nat) : Prop := True",
                "#check isLipschitz",
                "",
                "theorem solved : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    queue = runner._declaration_work_queue(
        str(active),
        '{"items":[{"severity":"info","message":"isLipschitz : Prop","line":2,"column":1}]}',
        project_root=str(project),
        scope="file",
    )

    assert queue == []


def test_declaration_work_queue_maps_only_error_structured_diagnostics(tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "def isLipschitz (f : Nat -> Nat) : Prop := True",
                "#check isLipschitz",
                "",
                "lemma broken : True := by",
                "  have h : True := by trivial",
                "  exact ?hole",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    queue = runner._declaration_work_queue(
        str(active),
        json.dumps(
            {
                "items": [
                    {"severity": "info", "message": "isLipschitz : Prop", "line": 2, "column": 1},
                    {
                        "severity": "error",
                        "message": "unsolved goals",
                        "line": 6,
                        "column": 3,
                    },
                ]
            }
        ),
        project_root=str(project),
        scope="file",
    )

    assert [item["label"] for item in queue] == ["broken"]
    assert queue[0]["reasons"] == ["diagnostic near line 6"]


def test_queue_assignment_block_mentions_only_assigned_theorem():
    text = runner._queue_assignment_block(
        {
            "target_symbol": "absLipschitz1",
            "active_file_label": "ProveDemo/RealTheorems-homework.lean",
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
                    "active_file": "ProveDemo/RealTheorems-homework.lean",
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
    assert "do not start solving unrelated future queue items" in text
    assert "Verification for this queue item:" in text
    assert "`lake env lean ProveDemo/RealTheorems-homework.lean`" in text
    assert "do not treat `lake build`, `grep`, `head`, or truncated output" in text
    assert "Current file prefix ending at `absLipschitz1`" in text
    assert "PREVIOUS ATTEMPTS:" not in text
    assert "attempt: 1" not in text
    assert "proof shape: direct `simpa [isLipschitz] using abs_abs_sub_abs_le`" not in text
    assert "why it failed: type mismatch" not in text
    assert "Task:" in text
    assert "Repair `absLipschitz1` from its current state." in text


def test_effective_skill_name_uses_queue_worker_for_file_scoped_queue_turn(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "lean-proof-loop")

    selected = runner._effective_skill_name(
        {
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
            "declaration_queue_total": 1,
        }
    )

    assert selected == "lean-theorem-queue-worker"


def test_effective_skill_name_returns_proof_loop_for_final_file_sweep(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_SKILL", "lean-proof-loop")

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


def test_document_formalization_scaffold_is_not_verified_before_planner_drafts(
    monkeypatch, tmp_path
):
    active = tmp_path / "Formalization" / "Paper.lean"
    active.parent.mkdir(parents=True)
    active.write_text("import Demo\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")

    live_state = {
        "active_file": str(active),
        "declaration_scope": "file",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake env lean Formalization/Paper.lean succeeded",
        "verification_ok": True,
        "sorry_count": 0,
        "project_sorry_count": 0,
    }

    assert runner._live_state_is_verified(live_state) is False


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


def test_live_state_is_not_verified_when_typed_verification_failed():
    live_state = {
        "active_file": "/tmp/project/Main.lean",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake build succeeded",
        "verification_ok": True,
        "sorry_count": 0,
        "project_sorry_count": 0,
        "last_verification": {
            "scope": "file",
            "ok": False,
            "tool": "lean_verify",
            "summary": "lake build reported errors",
        },
    }

    assert runner._verification_outcome(live_state=live_state) == "failed"
    assert runner._live_state_is_verified(live_state) is False


def test_live_state_ignores_legacy_reported_errors_when_typed_verification_passed():
    live_state = {
        "active_file": "/tmp/project/Main.lean",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake build reported errors",
        "verification_ok": True,
        "sorry_count": 0,
        "project_sorry_count": 0,
    }

    assert runner._verification_outcome(live_state=live_state) == "ok"
    assert runner._live_state_is_verified(live_state) is True


def test_diagnostics_indicate_failure_for_warnings():
    assert runner._diagnostics_indicate_failure("warning: declaration uses simp") is True


def test_diagnostics_indicate_failure_ignores_structured_info_messages():
    assert (
        runner._diagnostics_indicate_failure(
            '{"items":[{"severity":"info","message":"#check output","line":2,"column":1}]}'
        )
        is False
    )


def test_diagnostics_indicate_failure_keeps_structured_warnings_blocking():
    assert (
        runner._diagnostics_indicate_failure(
            '{"items":[{"severity":"warning","message":"style warning","line":6,"column":3}]}'
        )
        is True
    )


def test_diagnostics_indicate_hard_failure_ignores_warning_only_diagnostics():
    assert (
        runner._diagnostics_indicate_hard_failure(
            "Demo/Main.lean:2:3: warning: this tactic is never executed"
        )
        is False
    )
    assert (
        runner._diagnostics_indicate_hard_failure("Demo/Main.lean:2:3: error: unsolved goals")
        is True
    )


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


def test_promote_document_formalization_scaffold_waits_for_planner(monkeypatch, tmp_path):
    active = tmp_path / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text("import Demo\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")

    calls = []
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda *args, **kwargs: calls.append(args) or (True, "ok"),
    )

    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "declaration_scope": "file",
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "",
            "sorry_count": 0,
        }
    )

    assert calls == []
    assert promoted["verification_ok"] is False
    assert "planner has not drafted" in promoted["blocker_summary"]


def test_document_formalization_placeholder_blueprint_blocks_verified(monkeypatch, tmp_path):
    active = tmp_path / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    blueprint = (
        tmp_path / ".leanflow" / "workflow-state" / "formalization" / "paper" / "blueprint.md"
    )
    blueprint.parent.mkdir(parents=True)
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planner preflight created; replace this with the agent's dependency plan.\n\n"
        "### thm:demo\n"
        "- Planned Lean declarations: _pending_\n"
        "- Dependencies: _pending_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))

    live_state = {
        "active_file": str(active),
        "declaration_scope": "file",
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake env lean Formalization/Paper.lean succeeded",
        "verification_ok": True,
        "sorry_count": 0,
        "project_sorry_count": 0,
    }

    assert runner._live_state_is_verified(live_state) is False

    calls = []
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda *args, **kwargs: calls.append(args) or (True, "ok"),
    )
    promoted = runner._promote_live_state_to_verified(live_state)

    assert calls == []
    assert promoted["verification_ok"] is False
    assert "blueprint has not been updated" in promoted["blocker_summary"]


def test_document_formalization_handoff_blocks_queue_without_root_import(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Mathlib\n", encoding="utf-8")
    active.write_text("import Mathlib\n\ntheorem t : True := by\n  sorry\n", encoding="utf-8")
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "## Import Plan\n\n"
        "`Main.lean` imports:\n"
        "- `Mathlib`\n\n"
        "### thm:demo\n"
        "- Source locator: `docs/paper.tex:1-3`\n"
        "- Planned Lean declarations: `t`\n"
        "- Dependencies: none\n"
        "- Formal statement review: source statement exactly matches `True` in the fixture\n"
        "- Source proof / prover notes: prove by `trivial`\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", str(active))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))

    class _Caps:
        def to_dict(self):
            return {"degraded_reasons": []}

    class _Inspection:
        diagnostics = "no errors found"
        goals = "no goals"
        sorry_count = 1
        project_sorry_count = 1
        capability_report = {}
        queue_items = []

    class _Route:
        def to_dict(self):
            return {
                "route_action": "final-sweep",
                "skill_name": "lean-proof-loop",
                "reason": "queue is empty",
            }

    monkeypatch.setattr(runner, "probe_capabilities", lambda root: _Caps())
    monkeypatch.setattr(runner, "lean_inspect", lambda *args, **kwargs: _Inspection())
    monkeypatch.setattr(runner, "route_workflow_step", lambda *args, **kwargs: _Route())

    live_state = runner._build_live_proof_state([], {})

    assert live_state["declaration_queue_total"] == 0
    assert live_state["current_queue_item"] == {}
    assert live_state["queue_needs_final_file_sweep"] is False
    assert live_state["route_decision"]["route_action"] == "planner-review-gate"
    assert live_state["route_decision"]["skill_name"] == "lean-formalization"
    assert "lean_verify(mode=project)" in live_state["verification_hint"]
    handoff = live_state["document_formalization_handoff"]
    assert handoff["ok"] is False
    assert "root module `Demo` does not import `Demo.Paper.Main`" in handoff["summary"]


def test_document_formalization_review_gate_stops_before_proving(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo/Paper/Main.lean",
        "document_formalization_handoff": {
            "ok": False,
            "issues": ["blueprint entry `thm:demo` statement/source verification is not approved"],
            "summary": "document formalization handoff verifier blocked queue: statement/source verification is not approved",
        },
    }
    autonomy_state = {}

    assert runner._autonomous_stop_reason([], live_state, autonomy_state) == "blocked"

    live_state["route_decision"] = {
        "route_action": "planner-review-gate",
        "skill_name": "lean-formalization",
        "reason": "document formalization handoff is blocked",
    }
    monkeypatch.setattr(runner, "_runner_lean_prompt_enabled", lambda: False)
    monkeypatch.setattr(runner, "_swarm_enabled", lambda: False)
    prompt = runner._autonomous_continuation_prompt(live_state, 1, autonomy_state)

    assert "planner/review work" in prompt
    assert "lean_verify" in prompt
    assert "planner-review-gate" in prompt
    assert "do not fill theorem/lemma `sorry` proofs" in prompt


def test_document_formalization_pending_blueprint_blocks_final_sweep(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text(
        "/-- Source proof: pending. -/\ntheorem demo : True := by\n  sorry\n", encoding="utf-8"
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "\n".join(
            [
                "# Blueprint",
                "",
                "## Source Inventory",
                "",
                "### line-1",
                "- Source locator: `docs/paper.tex:1`",
                "- Planned Lean declarations: `demo`",
                "- Formal statement review: planner self-check complete",
                "- Source qualifiers: none",
                "- Lean coverage: covers statement",
                "- Scope changes: none",
                "- Statement verification status: pending review",
                "- Source proof / prover notes: source proof is trivial",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_document_formalization_manifest_blocks",
        lambda: [{"label": "line-1", "kind": "theorem", "has_proof": True}],
    )

    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo/Paper/Main.lean",
        "declaration_scope": "file",
        "declaration_queue_total": 0,
        "sorry_count": 1,
        "document_formalization_handoff": {"ok": True, "issues": []},
    }

    assert runner._document_formalization_waiting_for_independent_review(live_state) is True
    assert runner._queue_needs_final_file_sweep(live_state) is False
    assert runner._autonomous_stop_reason([], live_state, {}) == "blocked"


def test_document_formalization_review_due_for_approval_only_gate(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text("import Mathlib\n\ntheorem t : True := by\n  sorry\n", encoding="utf-8")
    blueprint = tmp_path / "Blueprint.md"
    blueprint.write_text("# Blueprint\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo/Paper/Main.lean",
        "document_formalization_handoff": {
            "ok": False,
            "issues": ["blueprint entry `thm:demo` statement/source verification is not approved"],
        },
    }

    assert runner._document_formalization_review_due(live_state, {}) is True
    autonomy_state = {
        "document_formalization_review_signature": runner._document_formalization_review_signature(
            live_state
        )
    }
    assert runner._document_formalization_review_due(live_state, autonomy_state) is False
    blueprint.write_text("# Blueprint\n\nupdated\n", encoding="utf-8")
    assert runner._document_formalization_review_due(live_state, autonomy_state) is True


def test_configured_command_blueprint_verifier_runs_without_review_agent(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(tmp_path / "Blueprint.md"))
    monkeypatch.setenv("AUXILIARY_BLUEPRINT_VERIFICATION_PROVIDER", "codex")
    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo/Paper/Main.lean",
        "document_formalization_handoff": {
            "ok": False,
            "issues": ["blueprint entry `thm:demo` statement/source verification is not approved"],
        },
    }
    calls = []
    events = []

    def fake_command_review(**kwargs):
        calls.append(kwargs)

        class Result:
            task = kwargs["task"]
            provider = kwargs["provider"]
            mode = "command"
            response = "review the blueprint entry before approval"
            status = "ok"
            command = ["codex", "exec"]
            exit_status = 0
            truncated = False
            response_chars = len(response)
            max_response_chars = 64000
            timed_out = False
            model = ""
            error = ""

        return Result()

    monkeypatch.setattr(runner, "run_command_verification_review", fake_command_review)
    monkeypatch.setattr(
        runner,
        "_run_document_formalization_review_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("review agent should not run")
        ),
    )
    monkeypatch.setattr(
        runner, "_record_agent_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    autonomy_state = {}
    assert (
        runner._maybe_run_document_formalization_review_agent(
            _FakeAgent(), "system", live_state, autonomy_state
        )
        is True
    )

    assert len(calls) == 1
    assert calls[0]["task"] == "blueprint_verification"
    assert calls[0]["provider"] == "codex"
    assert autonomy_state["document_formalization_review_provider"] == "codex"
    assert autonomy_state["document_formalization_review_result"]["status"] == "ok"
    assert "document_formalization_review_signature" in autonomy_state


def test_configured_blueprint_verifier_block_queues_drafting_feedback(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(tmp_path / "Blueprint.md"))
    monkeypatch.setenv("AUXILIARY_BLUEPRINT_VERIFICATION_PROVIDER", "codex")
    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo/Paper/Main.lean",
        "document_formalization_handoff": {
            "ok": False,
            "issues": ["blueprint entry `thm:demo` statement/source verification is not approved"],
        },
    }

    def fake_command_review(**kwargs):
        class Result:
            task = kwargs["task"]
            provider = kwargs["provider"]
            mode = "command"
            response = "BLOCK\nFindings:\n- add a companion representation bridge theorem"
            status = "ok"
            command = ["codex", "exec"]
            exit_status = 0
            truncated = False
            response_chars = len(response)
            max_response_chars = 64000
            timed_out = False
            model = ""
            error = ""

        return Result()

    monkeypatch.setattr(runner, "run_command_verification_review", fake_command_review)
    monkeypatch.setattr(runner, "_record_agent_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    autonomy_state = {}
    assert (
        runner._maybe_run_document_formalization_review_agent(
            _FakeAgent(), "system", live_state, autonomy_state
        )
        is True
    )

    assert autonomy_state["document_formalization_review_feedback_pending"] is True
    assert (
        "[LEANFLOW FORMALIZATION STATEMENT REVIEW BLOCK]"
        in autonomy_state["document_formalization_review_feedback_message"]
    )
    assert (
        "companion representation bridge theorem"
        in autonomy_state["document_formalization_review_feedback_message"]
    )
    assert runner._autonomous_stop_reason([], live_state, autonomy_state) == "continue"
    assert "document_formalization_review_feedback_pending" not in autonomy_state


def test_drive_autonomous_followups_runs_independent_document_review_before_block(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(tmp_path / "Blueprint.md"))
    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo/Paper/Main.lean",
        "declaration_scope": "file",
        "declaration_queue_total": 0,
        "document_formalization_handoff": {
            "ok": False,
            "issues": ["blueprint entry `thm:demo` statement/source verification is not approved"],
        },
    }
    review_calls = []
    persisted = []

    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(
        runner, "_build_live_proof_state_compat", lambda *args, **kwargs: dict(live_state)
    )
    monkeypatch.setattr(
        runner, "_promote_live_state_to_verified_compat", lambda state, autonomy_state=None: state
    )
    monkeypatch.setattr(
        runner, "_advance_project_prove_manager_if_needed", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        runner, "_rebuild_history_for_theorem_transition", lambda *args, **kwargs: (None, None)
    )
    monkeypatch.setattr(
        runner, "_maybe_announce_final_file_sweep_state", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        runner,
        "_persist_live_status",
        lambda *args, phase=None, **kwargs: persisted.append(str(phase or "")),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    def fake_review(agent, system_prompt, current_live_state, autonomy_state):
        review_calls.append(dict(current_live_state))
        autonomy_state["document_formalization_review_signature"] = (
            runner._document_formalization_review_signature(current_live_state)
        )
        return {"messages": [], "interrupted": False}

    monkeypatch.setattr(runner, "_run_document_formalization_review_agent", fake_review)

    history, _, _, final_live_state = runner._drive_autonomous_followups(
        _FakeAgent(),
        "system",
        [],
        {},
        {},
        {},
    )

    assert history == []
    assert final_live_state["active_file_label"] == "Demo/Paper/Main.lean"
    assert len(review_calls) == 1
    assert persisted[-1] == "blocked"


def test_document_formalization_reviewed_draft_stops_ready_for_prove(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    events = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo/Paper/Main.lean",
        "declaration_scope": "file",
        "declaration_queue_total": 0,
        "sorry_count": 2,
        "document_formalization_handoff": {"ok": True, "issues": []},
    }
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "T1_T2_relation",
            "active_file": str(active),
        },
    }

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    assert runner._queue_needs_final_file_sweep(live_state) is False
    runner._maybe_announce_final_file_sweep_state(autonomy_state, live_state)
    assert runner._autonomous_stop_reason([], live_state, autonomy_state) == "continue"
    assert autonomy_state["document_formalization_organization_turn_started"] is True

    prompt = runner._autonomous_continuation_prompt(live_state, 1, autonomy_state)
    assert "FINAL ORGANIZATION PASS" in prompt
    assert "Run `lean_verify(mode=project)` once" in prompt

    assert (
        runner._autonomous_stop_reason([], live_state, autonomy_state)
        == "formalization-prover-handoff-ready"
    )
    runner._prepare_queue_assignment_state(autonomy_state, live_state)
    rebuilt, transition = runner._rebuild_history_for_theorem_transition(
        [], {}, autonomy_state, live_state
    )

    assert events[0][0][0] == "final-file-sweep-deferred-for-prover-handoff"
    assert events[1][0][0] == "formalization-organization-pass-started"
    assert events[2][0][0] == "formalization-organization-pass-completed"
    assert events[3][0][0] == "formalization-prover-handoff-ready"
    assert "ready for /prove" in events[3][0][1]
    assert "current_queue_assignment" not in autonomy_state
    assert rebuilt is None
    assert transition is None


def test_formalization_handoff_requires_manual_scoped_prove_workflow(monkeypatch, tmp_path, capsys):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text(
        "import Mathlib\n\n/-- Source proof: trivial. -/\ntheorem t : True := by\n  sorry\n",
        encoding="utf-8",
    )
    manifest = (
        project / ".leanflow" / "workflow-state" / "formalization" / "paper" / "manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"target_lean_relative": "Demo/Paper/Main.lean"}), encoding="utf-8"
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_MANIFEST", str(manifest))
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", str(active))

    events = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("prove workflow must not auto-start")
        ),
    )

    runner._record_formalization_manual_prove_handoff(
        {
            "active_file": str(active),
            "active_file_label": "Demo/Paper/Main.lean",
            "document_formalization_proof_sorry_count": 1,
        },
        {},
    )

    assert [event[0][0] for event in events[:2]] == [
        "formalizer-ended",
        "prove-workflow-manual-start-required",
    ]
    assert events[1][1]["suggested_command"] == "leanflow workflow prove Demo/Paper/Main.lean"
    output = capsys.readouterr().out
    assert "Prove workflow not started automatically" in output
    assert "leanflow workflow prove Demo/Paper/Main.lean" in output


def test_project_prove_scope_limits_candidates_in_given_order(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    (project / "Demo").mkdir(parents=True)
    first = project / "Demo" / "Basic.lean"
    second = project / "Demo" / "Theorems.lean"
    unrelated = project / "Demo" / "Other.lean"
    first.write_text("theorem basic : True := by\n  sorry\n", encoding="utf-8")
    second.write_text("theorem main : True := by\n  sorry\n", encoding="utf-8")
    unrelated.write_text("theorem other : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv(
        "LEANFLOW_PROVE_FILE_SCOPE", os.pathsep.join(["Demo/Basic.lean", "Demo/Theorems.lean"])
    )

    candidates = runner._collect_project_prove_file_candidates(project)

    assert [item["label"] for item in candidates] == ["Demo/Basic.lean", "Demo/Theorems.lean"]


def test_document_formalization_handoff_blocks_construction_sorry_and_filters_queue(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text(
        "import Mathlib\n\n"
        "noncomputable def qRationalNum : Nat := by\n"
        "  sorry\n\n"
        "/-- Source proof: the source proof closes the toy claim directly; prover notes: use `trivial`. -/\n"
        "theorem t : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "## Import Plan\n\n"
        "Direct Lean imports:\n"
        "- `Mathlib`\n\n"
        "## Suggested Search Modules\n\n"
        "- `Mathlib.Data.Nat.Basic`\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))

    queue = runner._declaration_work_queue(str(active), "", project_root=str(project), scope="file")
    filtered = runner._filter_document_formalization_proof_queue(queue)
    handoff = runner._document_formalization_handoff_verification(str(active), sorry_count=2)

    assert [item["label"] for item in filtered] == ["t"]
    assert handoff["ok"] is False
    assert "construction gap before proof handoff" in handoff["summary"]
    assert "qRationalNum" in handoff["summary"]


def test_document_formalization_gate_clears_stale_queue_assignment(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text("theorem t : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "t",
            "active_file": str(active),
        },
    }
    live_state = {
        "active_file": str(active),
        "current_queue_item": {"label": "t"},
        "document_formalization_handoff": {
            "ok": False,
            "issues": ["blueprint entry `thm:demo` statement/source verification is not approved"],
            "summary": "document formalization handoff verifier blocked queue",
        },
    }

    runner._prepare_queue_assignment_state(autonomy_state, live_state)

    assert "current_queue_assignment" not in autonomy_state
    rebuilt, transition = runner._rebuild_history_for_theorem_transition(
        [], {}, autonomy_state, live_state
    )
    assert rebuilt is None
    assert transition is None


def test_additional_skill_contracts_are_attached_to_continuations(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    skill = project / ".leanflow" / "skills" / "paper-blueprint" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: paper-blueprint\ndescription: Paper blueprint.\n---\n\nRead `Demo/Paper/Blueprint.md`.",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_ADDITIONAL_SKILLS", str(skill))
    live_state = {
        "message": "live state",
        "route_decision": {"skill_name": "lean-proof-loop"},
    }

    attached = runner._attach_live_proof_state("continue", live_state)

    assert "LEANFLOW SUPPLEMENTAL SKILLS" in attached
    assert "Read `Demo/Paper/Blueprint.md`" in attached


def test_document_formalization_handoff_detects_stale_import_plan(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text("import Mathlib\n\ntheorem t : True := by\n  trivial\n", encoding="utf-8")
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: active formalization\n\n"
        "## Planner Checklist\n\n"
        "- [ ] Hand stable `sorry` declarations to the managed prover queue.\n\n"
        "## Lean Import Plan\n\n"
        "`Main.lean` imports:\n"
        "- `Mathlib.Data.Nat.Coprime.Basic`\n\n"
        "### thm:demo\n"
        "- Planned Lean declarations: `t`\n"
        "- Dependencies: none\n"
        "- Formal statement review: source statement exactly matches `True` in the fixture\n"
        "- Source proof / prover notes: prove by `trivial`\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))

    handoff = runner._document_formalization_handoff_verification(
        str(active),
        sorry_count=0,
        completion=True,
    )

    assert handoff["ok"] is False
    assert "Mathlib.Data.Nat.Coprime.Basic" in handoff["summary"]
    assert "Mathlib`" in handoff["summary"]
    assert "active formalization" in handoff["summary"]
    assert (
        runner._live_state_is_verified(
            {
                "active_file": str(active),
                "declaration_scope": "file",
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake build succeeded",
                "verification_ok": True,
                "sorry_count": 0,
                "project_sorry_count": 0,
            }
        )
        is False
    )


def test_document_formalization_handoff_ignores_suggested_search_modules(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text(
        "import Mathlib\n\n/-- Source proof: prove by `trivial`. -/\ntheorem t : True := by\n  sorry\n",
        encoding="utf-8",
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "## Import Plan\n\n"
        "Direct Lean imports:\n"
        "- `Mathlib`\n\n"
        "## Suggested Search Modules\n\n"
        "- `Mathlib.Data.Nat.Coprime.Basic`\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))

    handoff = runner._document_formalization_handoff_verification(
        str(active),
        sorry_count=1,
        last_verification={"ok": True, "scope": "project", "tool": "lean_verify"},
    )

    assert handoff == {
        "ok": True,
        "issues": [],
        "summary": "document formalization handoff verifier passed",
    }


def test_configured_autoformalizer_block_prevents_proof_handoff(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text(
        "import Mathlib\n\n/-- Source proof: prove by `trivial`. -/\ntheorem t : True := by\n  sorry\n",
        encoding="utf-8",
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "## Import Plan\n\n"
        "Direct Lean imports:\n"
        "- `Mathlib`\n\n"
        "## Source Statement Inventory\n\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))

    def fake_resolve(task, explicit=None):
        if task == runner.AUTOFORMALIZER_VERIFICATION_TASK:
            return "codex"
        return "local"

    class Result:
        task = runner.AUTOFORMALIZER_VERIFICATION_TASK
        provider = "codex"
        mode = "command"
        response = "BLOCK\nFindings:\n- theorem statement weakens the source statement\nCorrection steps:\n- revise the Lean statement"
        status = "ok"
        command = ["codex", "exec"]
        exit_status = 0
        truncated = False
        response_chars = len(response)
        max_response_chars = 64000
        timed_out = False
        model = ""
        error = ""

    monkeypatch.setattr(runner, "resolve_verification_provider", fake_resolve)
    monkeypatch.setattr(runner, "is_command_verification_provider", lambda provider: True)
    monkeypatch.setattr(runner, "run_command_verification_review", lambda **kwargs: Result())

    handoff = runner._document_formalization_handoff_verification(
        str(active),
        sorry_count=1,
        last_verification={"ok": True, "scope": "project", "tool": "lean_verify"},
    )

    assert handoff["ok"] is False
    assert "configured autoformalizer verifier returned BLOCK" in handoff["summary"]
    assert any("weakens the source statement" in issue for issue in handoff["issues"])


def test_formalization_verifier_block_is_appended_to_next_tool_turn(monkeypatch):
    class Agent(_ManagedRunAgentStub):
        quiet_mode = True

    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    live_state = {
        "active_file_label": "Demo/Paper/Main.lean",
        "document_formalization_handoff": {
            "ok": False,
            "issues": [
                "configured autoformalizer verifier returned BLOCK: blueprint verification is still not approved",
                "line-240 needs verifier-accepted scope-change recording",
            ],
        },
    }
    agent = Agent()

    runner._maybe_append_formalization_handoff_feedback(
        agent,
        function_name="patch",
        live_state=live_state,
    )

    appendix = agent._post_tool_result_appendix
    assert "[LEANFLOW FORMALIZATION VERIFIER BLOCK]" in appendix
    assert "next required correction task" in appendix
    assert "blueprint verification is still not approved" in appendix
    assert "line-240 needs verifier-accepted scope-change recording" in appendix


def test_document_formalization_raw_lean_edit_runs_immediate_file_check(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text("import Mathlib\n", encoding="utf-8")
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "## Source Statement Inventory\n\n"
        "### line-1\n"
        "- Planned Lean declarations: `t`\n"
        "- Formal statement review: drafted\n"
        "- Source proof / prover notes: use `trivial`\n"
        "- Statement verification status: drafted, awaiting independent review\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner, "_maybe_append_formalization_handoff_feedback", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    calls = []
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda path: (
            calls.append(path)
            or {"ok": False, "command": f"lake env lean {path}", "output": "type mismatch"}
        ),
    )

    class Agent(_ManagedRunAgentStub):
        quiet_mode = True
        _session_messages = []
        _managed_autonomy_state = {}

    agent = Agent()

    runner._handle_managed_tool_result(
        agent,
        "patch",
        {"path": str(active), "mode": "replace", "new_string": "import Mathlib\n\n"},
        json.dumps({"success": True}),
    )

    assert calls == [str(active.resolve())]
    assert "[LEANFLOW FORMALIZATION LEAN CHECK FAILED]" in agent._post_tool_result_appendix
    assert "type mismatch" in agent._post_tool_result_appendix
    assert (
        runner._document_formalization_pre_tool_guard(
            agent,
            "patch",
            {"path": str(active), "mode": "replace", "new_string": "import Mathlib\n\n"},
        )
        is None
    )
    assert (
        runner._document_formalization_pre_tool_guard(
            agent,
            "apply_verified_patch",
            {"path": str(active), "patch": "theorem t : True := by\n  sorry\n"},
        )
        is None
    )


def test_document_formalization_blocks_drafting_model_blueprint_self_approval(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text("import Mathlib\n", encoding="utf-8")
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "## Source Statement Inventory\n\n"
        "### line-1\n"
        "- Statement verification status: drafted, awaiting independent review\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))

    class Agent(_ManagedRunAgentStub):
        _managed_autonomy_state = {}

    blocked = runner._document_formalization_pre_tool_guard(
        Agent(),
        "patch",
        {
            "path": str(blueprint),
            "mode": "replace",
            "old_string": "- Statement verification status: drafted, awaiting independent review\n",
            "new_string": "- Statement verification status: approved\n",
        },
    )

    assert blocked is not None
    payload = json.loads(blocked)
    assert payload["next_required_step"] == "independent_statement_source_verification"
    assert "must be written by the independent statement/source verifier" in payload["error"]


def test_document_formalization_lean_edit_gate_does_not_apply_to_prove(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text("import Mathlib\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(
        runner, "_manager_verify_queue_file", lambda path: calls.append(path) or {"ok": True}
    )

    class Agent(_ManagedRunAgentStub):
        quiet_mode = True
        _session_messages = []
        _managed_autonomy_state = {}

    assert (
        runner._document_formalization_pre_tool_guard(
            Agent(),
            "patch",
            {"path": str(active), "mode": "replace", "new_string": "import Mathlib\n\n"},
        )
        is None
    )
    runner._handle_managed_tool_result(
        Agent(),
        "patch",
        {"path": str(active), "mode": "replace", "new_string": "import Mathlib\n\n"},
        json.dumps({"success": True}),
    )
    assert calls == []


def test_configured_blueprint_verifier_pass_stamps_readonly_approval(monkeypatch, tmp_path):
    blueprint = tmp_path / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planner draft in progress\n\n"
        "- [ ] Verify drafted Lean statements match the source document.\n\n"
        "- [ ] Run independent statement/source verification review and apply corrections.\n\n"
        "- [ ] Mark stable theorem/lemma/example `sorry` declarations ready for a user-started prove workflow. (Only check after independent review approves every source entry.)\n\n"
        "## Source Statement Inventory\n\n"
        "### line-143\n"
        "- Statement verification status: pending review\n\n"
        "### line-194\n"
        "- Statement verification status: ready for review\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)

    assert runner._stamp_blueprint_statement_review_approved(
        provider="codex", active_file="Demo/Paper/Main.lean"
    )

    text = blueprint.read_text(encoding="utf-8")
    assert (
        "- Status: statement/source review approved; ready for user-started prove workflow" in text
    )
    assert "- [x] Verify drafted Lean statements match the source document." in text
    assert (
        "- [x] Run independent statement/source verification review and apply corrections." in text
    )
    assert (
        "- [x] Mark stable theorem/lemma/example `sorry` declarations ready for a user-started prove workflow."
        in text
    )
    assert text.count("Statement verification status: approved by codex verifier") == 2


def test_document_formalization_handoff_checks_blueprint_inventory_against_target(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text("import Mathlib\n\ntheorem t : True := by\n  trivial\n", encoding="utf-8")
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "## Lean Import Plan\n\n"
        "`Main.lean` imports:\n"
        "- `Mathlib`\n\n"
        "## Source Statement Inventory\n\n"
        "### thm:demo\n\n"
        "- Planned Lean declarations: `missing_t`\n"
        "- Dependencies: none\n"
        "- Formal statement review: _pending_\n"
        "- Source proof / prover notes: _pending_\n",
        encoding="utf-8",
    )
    manifest = (
        project / ".leanflow" / "workflow-state" / "formalization" / "paper" / "manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"theorem_blocks": [{"label": "thm:demo", "kind": "theorem"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_MANIFEST", str(manifest))

    handoff = runner._document_formalization_handoff_verification(
        str(active),
        sorry_count=1,
        last_verification={"ok": True, "scope": "project", "tool": "lean_verify"},
    )

    assert handoff["ok"] is False
    assert "missing_t" in handoff["summary"]
    assert "statement-fidelity review" in handoff["summary"]
    assert any("source proof/prover notes" in issue for issue in handoff["issues"])


def test_document_formalization_handoff_blocks_approved_without_fidelity_axes(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text(
        "import Mathlib\n\n"
        "def f_param (x y z w : ℤ) : ℚ := x\n"
        "def g_param (x y z w : ℤ) : ℚ := y\n"
        "def h_param (x y z w : ℤ) : ℚ := z\n\n"
        "/-- Source proof: old weak statement only proves image equality. -/\n"
        "theorem integer_valued_parametrization : True := by\n"
        "  sorry\n\n"
        "def f_pos (x y z w : ℤ) : ℚ := x\n"
        "def g_pos (x y z w : ℤ) : ℚ := y\n"
        "def h_pos (x y z w : ℤ) : ℚ := z\n\n"
        "/-- Source proof: old weak positive statement omits four-square conversion. -/\n"
        "theorem positive_pythagorean_parametrization : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "## Lean Import Plan\n\n"
        "`Main.lean` imports:\n"
        "- `Mathlib`\n\n"
        "## Source Statement Inventory\n\n"
        "### line-194\n"
        "- Source locator: `docs/paper.tex:194-203`\n"
        "- Planned Lean declarations:\n"
        "  - `f_param x y z w`, `g_param x y z w`, `h_param x y z w`\n"
        "  - `integer_valued_parametrization`\n"
        "- Dependencies: none\n"
        "- Formal statement review:\n"
        "  - Source: There exist `f,g,h ∈ Int(ℤ⁴)` parametrizing all PTs.\n"
        "  - Lean: set equality for the displayed functions.\n"
        "  - Fidelity note: proof must verify integer-valuedness.\n"
        "- Statement verification status: approved\n"
        "- Source proof / prover notes: use parity and the `T` parametrization.\n\n"
        "### line-240\n"
        "- Source locator: `docs/paper.tex:240-253`\n"
        "- Planned Lean declarations:\n"
        "  - `f_pos x y z w`, `g_pos x y z w`, `h_pos x y z w`\n"
        "  - `positive_pythagorean_parametrization`\n"
        "- Dependencies: none\n"
        "- Formal statement review:\n"
        "  - Source: positive parameters plus a four-square conversion to integer parameters.\n"
        "  - Lean: constrained-domain set equality only.\n"
        "- Statement verification status: approved\n"
        "- Source proof / prover notes: use positivity and the four-square theorem.\n",
        encoding="utf-8",
    )
    manifest = (
        project / ".leanflow" / "workflow-state" / "formalization" / "paper" / "manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "theorem_blocks": [
                    {
                        "label": "line-194",
                        "kind": "theorem",
                        "statement": "There exist f,g,h in Int(ℤ⁴) parametrizing all Pythagorean triples.",
                        "proof": "parity proof",
                    },
                    {
                        "label": "line-240",
                        "kind": "remark",
                        "statement": "The set of positive triples is parametrized with positive integers and non-negative integers. From this formula, a parametrization with integer parameters can be obtained using the 4-square theorem.",
                        "proof": "four-square proof",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_MANIFEST", str(manifest))

    handoff = runner._document_formalization_handoff_verification(str(active), sorry_count=2)

    assert handoff["ok"] is False
    assert any("source qualifiers / fidelity axes" in issue for issue in handoff["issues"])
    assert any("Lean coverage" in issue for issue in handoff["issues"])
    assert any("scope changes" in issue.lower() for issue in handoff["issues"])


def test_document_formalization_handoff_accepts_resolved_generic_fidelity_axes(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text(
        "import Mathlib\n\n/-- Source proof: fixture proof. -/\ntheorem t : True := by\n  sorry\n",
        encoding="utf-8",
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "## Lean Import Plan\n\n"
        "`Main.lean` imports:\n"
        "- `Mathlib`\n\n"
        "## Source Statement Inventory\n\n"
        "### thm:demo\n"
        "- Source locator: `docs/paper.tex:1-3`\n"
        "- Planned Lean declarations: `t`\n"
        "- Dependencies: none\n"
        "- Formal statement review: source statement exactly matches `True` in the fixture\n"
        "- Source qualifiers:\n"
        "  - object class: proposition\n"
        "  - quantifier shape: closed theorem\n"
        "- Lean coverage:\n"
        "  - proposition: theorem `t : True`\n"
        "  - quantifier shape: closed theorem\n"
        "- Scope changes: none\n"
        "- Statement verification status: approved\n"
        "- Source proof / prover notes: prove by `trivial`\n",
        encoding="utf-8",
    )
    manifest = (
        project / ".leanflow" / "workflow-state" / "formalization" / "paper" / "manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "theorem_blocks": [
                    {
                        "label": "thm:demo",
                        "kind": "theorem",
                        "statement": "True",
                        "proof": "trivial",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_MANIFEST", str(manifest))

    handoff = runner._document_formalization_handoff_verification(
        str(active),
        sorry_count=1,
        last_verification={"ok": True, "scope": "project", "tool": "lean_verify"},
    )

    assert handoff["ok"] is True


def test_document_formalization_handoff_blocks_hard_draft_diagnostics(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text(
        "import Mathlib\n\n"
        "/-- Source proof: the fixture proof is `trivial`. -/\n"
        "theorem t : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "## Lean Import Plan\n\n"
        "`Main.lean` imports:\n"
        "- `Mathlib`\n\n"
        "## Source Statement Inventory\n\n"
        "### thm:demo\n"
        "- Source locator: `docs/paper.tex:1-3`\n"
        "- Planned Lean declarations: `t`\n"
        "- Dependencies: none\n"
        "- Formal statement review: source statement exactly matches `True` in the fixture\n"
        "- Source proof / prover notes: prove by `trivial`\n",
        encoding="utf-8",
    )
    manifest = (
        project / ".leanflow" / "workflow-state" / "formalization" / "paper" / "manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"theorem_blocks": [{"label": "thm:demo", "kind": "theorem"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_MANIFEST", str(manifest))

    handoff = runner._document_formalization_handoff_verification(
        str(active),
        diagnostics=f"{active}:4:8: error: Type mismatch",
        sorry_count=1,
    )

    assert handoff["ok"] is False
    assert "hard diagnostics" in handoff["summary"]
    assert "Type mismatch" in handoff["summary"]


def test_document_formalization_handoff_requires_proof_notes_in_lean_comment(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text(
        "import Mathlib\n\n/-- The source theorem says `True`. -/\ntheorem t : True := by\n  sorry\n",
        encoding="utf-8",
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "## Lean Import Plan\n\n"
        "`Main.lean` imports:\n"
        "- `Mathlib`\n\n"
        "## Source Statement Inventory\n\n"
        "### thm:demo\n"
        "- Source locator: `docs/paper.tex:1-3`\n"
        "- Planned Lean declarations: `t`\n"
        "- Dependencies: none\n"
        "- Formal statement review: source statement exactly matches `True` in the fixture\n"
        "- Source proof / prover notes: prove by `trivial`\n",
        encoding="utf-8",
    )
    manifest = (
        project / ".leanflow" / "workflow-state" / "formalization" / "paper" / "manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"theorem_blocks": [{"label": "thm:demo", "kind": "theorem"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_MANIFEST", str(manifest))

    handoff = runner._document_formalization_handoff_verification(str(active), sorry_count=1)

    assert handoff["ok"] is False
    assert "Lean doc comment above `t` is missing source proof/prover notes" in handoff["summary"]


def test_document_formalization_handoff_accepts_synced_blueprint_and_root_import(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    parent = project / "Demo" / "Paper.lean"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper\n", encoding="utf-8")
    parent.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    active.write_text(
        "import Mathlib\n\n"
        "/-- Source proof: the source proof closes the toy claim directly; prover notes: use `trivial`. -/\n"
        "theorem t : True := by\n"
        "  trivial\n",
        encoding="utf-8",
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: complete; Lean module and project build verified\n\n"
        "## Planner Checklist\n\n"
        "- [x] Run independent statement/source verification review and apply corrections.\n"
        "- [x] Hand stable `sorry` declarations to the managed prover queue.\n\n"
        "## Lean Import Plan\n\n"
        "`Main.lean` imports:\n"
        "- `Mathlib`\n\n"
        "## Source Statement Inventory\n\n"
        "### thm:demo\n"
        "- Source locator: `docs/paper.tex:1-3`\n"
        "- Planned Lean declarations: `t`\n"
        "- Dependencies: none\n"
        "- Formal statement review: source statement exactly matches `True` in the fixture\n"
        "- Source qualifiers:\n"
        "  - object class: proposition\n"
        "  - quantifier shape: closed theorem\n"
        "- Lean coverage:\n"
        "  - proposition: theorem `t : True`\n"
        "  - quantifier shape: closed theorem\n"
        "- Scope changes: none\n"
        "- Statement verification status: approved by review workflow\n"
        "- Source proof / prover notes: prove by `trivial`\n",
        encoding="utf-8",
    )
    manifest = (
        project / ".leanflow" / "workflow-state" / "formalization" / "paper" / "manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"theorem_blocks": [{"label": "thm:demo", "kind": "theorem"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_MANIFEST", str(manifest))

    handoff = runner._document_formalization_handoff_verification(
        str(active),
        sorry_count=0,
        last_verification={"ok": True, "scope": "project", "tool": "lean_verify"},
        completion=True,
    )

    assert handoff == {
        "ok": True,
        "issues": [],
        "summary": "document formalization handoff verifier passed",
    }


def test_document_formalization_handoff_accepts_split_aggregator_layout(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    root = project / "Demo.lean"
    parent = project / "Demo" / "Paper.lean"
    main = project / "Demo" / "Paper" / "Main.lean"
    basic = project / "Demo" / "Paper" / "Basic.lean"
    theorems = project / "Demo" / "Paper" / "Theorems.lean"
    main.parent.mkdir(parents=True)
    root.write_text("import Demo.Paper\n", encoding="utf-8")
    parent.write_text("import Demo.Paper.Main\n", encoding="utf-8")
    main.write_text("import Demo.Paper.Basic\nimport Demo.Paper.Theorems\n", encoding="utf-8")
    basic.write_text("import Mathlib\n\ndef Helper : Prop := True\n", encoding="utf-8")
    theorems.write_text(
        "import Mathlib\n"
        "import Demo.Paper.Basic\n\n"
        "/-- Source proof: the source proof closes the toy claim directly; prover notes: unfold `Helper`. -/\n"
        "theorem t : Helper := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planner draft complete\n\n"
        "## Planner Checklist\n\n"
        "- [x] Run independent statement/source verification review and apply corrections.\n"
        "- [x] Mark stable theorem/lemma/example `sorry` declarations ready for a user-started prove workflow.\n\n"
        "## Import Plan\n\n"
        "Direct Lean imports expected in generated Lean files only:\n"
        "- `Mathlib`\n\n"
        "## Generated File Layout\n\n"
        "- Aggregator entry file: `Demo/Paper/Main.lean`\n"
        "- `Demo/Paper/Basic.lean` -- definitions\n"
        "- `Demo/Paper/Theorems.lean` -- theorem statements\n\n"
        "## Source Statement Inventory\n\n"
        "### thm:demo\n"
        "- Source locator: `docs/paper.tex:1-3`\n"
        "- Planned Lean declarations: `t`\n"
        "- Dependencies: `Helper`\n"
        "- Formal statement review: source statement exactly matches `Helper` in the fixture\n"
        "- Source qualifiers:\n"
        "  - object class: proposition\n"
        "  - quantifier shape: closed theorem\n"
        "- Lean coverage:\n"
        "  - proposition: theorem `t : Helper`\n"
        "  - quantifier shape: closed theorem\n"
        "- Scope changes: none\n"
        "- Statement verification status: approved by review workflow\n"
        "- Source proof / prover notes: unfold `Helper`, then prove by `trivial`\n",
        encoding="utf-8",
    )
    manifest = (
        project / ".leanflow" / "workflow-state" / "formalization" / "paper" / "manifest.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "target_lean_relative": "Demo/Paper/Main.lean",
                "theorem_blocks": [{"label": "thm:demo", "kind": "theorem", "proof": "trivial"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_MANIFEST", str(manifest))
    monkeypatch.setattr(
        runner, "resolve_verification_provider", lambda task, explicit=None: "local"
    )

    handoff = runner._document_formalization_handoff_verification(
        str(main),
        sorry_count=1,
        last_verification={"ok": True, "scope": "project", "tool": "lean_verify"},
    )

    assert handoff == {
        "ok": True,
        "issues": [],
        "summary": "document formalization handoff verifier passed",
    }
    assert runner._document_formalization_needs_planner_draft(str(main)) is False


def test_document_formalization_write_target_requires_blueprint_plan(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    active = project / "Demo" / "Paper" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text("import Demo\n", encoding="utf-8")
    blueprint = project / "Demo" / "Paper" / "Blueprint.md"
    blueprint.parent.mkdir(parents=True, exist_ok=True)
    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planner preflight created; replace this with the agent's dependency plan.\n\n"
        "### thm:demo\n"
        "- Planned Lean declarations: _pending_\n"
        "- Dependencies: _pending_\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_TARGET_FILE", "Demo/Paper/Main.lean")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_BLUEPRINT", str(blueprint))

    result = runner._managed_pre_tool_call(
        _FakeAgent(),
        "write_file",
        {
            "path": "Demo/Paper/Main.lean",
            "content": "import Demo\n\ntheorem t : True := by\n  trivial\n",
        },
    )

    assert result is not None
    payload = json.loads(result)
    assert payload["success"] is False
    assert "must update the planner blueprint before editing" in payload["error"]

    blueprint.write_text(
        "# Formalization Blueprint\n\n"
        "- Status: planned\n\n"
        "### thm:demo\n"
        "- Planned Lean declarations: `t`\n"
        "- Dependencies: none\n"
        "- Formal statement review: source statement exactly matches `True` in the fixture\n"
        "- Source proof / prover notes: prove by `trivial`\n",
        encoding="utf-8",
    )

    planner_result = runner._managed_pre_tool_call(
        _FakeAgent(),
        "write_file",
        {
            "path": "Demo/Paper/Main.lean",
            "content": "import Demo\n\ntheorem t : True := by\n  trivial\n",
        },
    )
    assert planner_result is not None
    planner_payload = json.loads(planner_result)
    assert "must leave theorem/lemma proofs as `sorry`" in planner_payload["error"]

    assert (
        runner._managed_pre_tool_call(
            _FakeAgent(),
            "write_file",
            {
                "path": "Demo/Paper/Main.lean",
                "content": "import Demo\n\ntheorem t : True := by\n  sorry\n",
            },
        )
        is None
    )

    prover_agent = _FakeAgent()
    prover_agent._managed_autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "t",
            "active_file": str(active),
        }
    }
    stale_queue_result = runner._managed_pre_tool_call(
        prover_agent,
        "write_file",
        {
            "path": "Demo/Paper/Main.lean",
            "content": "import Demo\n\ntheorem t : True := by\n  trivial\n",
        },
    )
    assert stale_queue_result is not None
    stale_queue_payload = json.loads(stale_queue_result)
    assert "must leave theorem/lemma proofs as `sorry`" in stale_queue_payload["error"]


def test_promote_live_state_accepts_warning_only_final_file_sweep(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda active_file="", full_project=False: (True, "lake env lean Demo/Main.lean exits 0"),
    )

    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "diagnostics": f"{active}:2:3: warning: this tactic is never executed",
            "goals": "no goals",
            "build_status": "unknown",
            "sorry_count": 0,
        }
    )

    assert promoted["verification_ok"] is True
    assert runner._live_state_is_verified(promoted) is True
    assert runner._queue_needs_final_file_sweep(promoted) is False


def test_live_state_is_verified_blocks_when_warning_cleanup_pending():
    """Regression: in a multi-file project workflow, the project-prove
    manager calls ``_advance_project_prove_manager_if_needed`` after every
    queue-empty cycle and uses ``_live_state_is_verified`` as the "this file
    is done" gate. If that gate ignores the cleanup-pending flag, the
    project manager advances to the next file *before* the cleanup
    conversation runs and ``_assign_project_prove_file`` pops the cleanup
    state — so warnings persist forever in multi-file mode. Pin the gate."""

    base_state = {
        "active_file": "/tmp/Demo.lean",
        "declaration_scope": "file",
        "declaration_queue_total": 0,
        "sorry_count": 0,
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake env lean Demo.lean exits 0",
        "verification_ok": False,  # promote() set this when cleanup fired
        "last_verification": {
            "ok": True,
            "scope": "file",
            "tool": "lean_verify",
            "summary": "lake env lean Demo.lean exits 0",
        },
    }

    # Without the cleanup-pending flag, the file is treated as verified
    # (lake build passed, no sorries, no errors).
    assert runner._live_state_is_verified(base_state) is True

    # With cleanup pending, the file is NOT yet verified — the cleanup turn
    # must run first, otherwise the project manager will advance and pop
    # the cleanup state before the conversation drives.
    pending_state = dict(base_state, final_sweep_warning_cleanup_pending=True)
    assert runner._live_state_is_verified(pending_state) is False


def test_advance_project_prove_manager_blocks_while_cleanup_pending(monkeypatch):
    """End-to-end: project-prove manager must NOT advance to the next file
    while the current file's cleanup conversation is still pending."""

    autonomy_state: dict = {
        "project_prove_manager_enabled": True,
        "project_prove_active_file": "Demo/A.lean",
        "project_prove_active_file_path": "/tmp/Demo/A.lean",
        "final_sweep_cleanup_attempted": True,
        "final_sweep_baseline": {
            "active_file": "/tmp/Demo/A.lean",
            "content": "theorem t : True := by trivial\n",
        },
    }
    live_state = {
        "active_file": "/tmp/Demo/A.lean",
        "declaration_scope": "file",
        "declaration_queue_total": 0,
        "sorry_count": 0,
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake env lean exits 0",
        "verification_ok": False,
        "final_sweep_warning_cleanup_pending": True,
        "last_verification": {
            "ok": True,
            "scope": "file",
            "tool": "lean_verify",
            "summary": "lake env lean exits 0",
        },
    }

    # If the manager were to advance, it would pop the cleanup state via
    # `_assign_project_prove_file`. Patch that to fail loudly so the test
    # also catches a future regression where the gate is bypassed elsewhere.
    monkeypatch.setattr(
        runner,
        "_assign_project_prove_file",
        lambda *args, **kwargs: pytest.fail(
            "project manager must not assign a new file while cleanup is pending"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_refresh_project_prove_file_queue",
        lambda autonomy_state: [{"label": "Demo/B.lean", "path": "/tmp/Demo/B.lean"}],
    )

    advanced = runner._advance_project_prove_manager_if_needed(
        autonomy_state, live_state, phase="autonomous"
    )

    assert advanced is False
    # Cleanup state must survive the (non-)advancement.
    assert autonomy_state["final_sweep_cleanup_attempted"] is True
    assert "final_sweep_baseline" in autonomy_state


def test_promote_live_state_grants_one_final_sweep_warning_cleanup(monkeypatch, tmp_path, capsys):
    """Spec :672 — final sweep is the canonical whole-file warning cleanup
    window. When the queue is empty, lake build is clean, and warnings remain
    on the active file, the runner must grant exactly one focused cleanup
    cycle: snapshot the file, set the one-shot flag, and suppress
    verification so the workflow loop drives one more conversation."""

    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda active_file="", full_project=False: (True, "lake env lean Demo/Main.lean exits 0"),
    )

    autonomy_state: dict = {}
    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "diagnostics": f"{active}:2:3: warning: this tactic is never executed",
            "goals": "no goals",
            "build_status": "unknown",
            "sorry_count": 0,
        },
        autonomy_state,
    )

    assert (
        promoted["verification_ok"] is False
    ), "warnings present must hold the workflow open for cleanup"
    assert promoted["final_sweep_warning_cleanup_pending"] is True
    assert promoted["final_sweep_warning_count"] == 1
    assert "this tactic is never executed" in promoted["final_sweep_warning_summary"]
    assert "line 2" in promoted["final_sweep_warning_summary"]
    assert promoted["queue_needs_final_file_sweep"] is True
    assert promoted["proof_solved"] is True
    assert promoted["warning_cleanup_status"] == "pending"
    assert promoted["warning_cleanup_attempted"] is True
    assert promoted["warning_cleanup_verified"] is False
    assert promoted["warning_cleanup"]["warning_count"] == 1
    assert autonomy_state["final_sweep_cleanup_attempted"] is True
    baseline = autonomy_state["final_sweep_baseline"]
    assert baseline["content"] == "theorem t : True := by\n  trivial\n"
    output = capsys.readouterr().out
    assert "🟢 Final file sweep — warning cleanup opportunity granted (1/1)" in output
    assert "1 warning(s) remain on the active file" in output


def test_promote_live_state_keeps_cleanup_pending_until_model_turn_starts(monkeypatch, tmp_path):
    """Granting the cleanup window is not the same as spending it.

    Regression coverage for the autonomous loop: after the first promote pass
    grants final-sweep cleanup, the next live-state rebuild happens before the
    model receives the cleanup prompt. That rebuild must preserve the pending
    state instead of accepting the warnings immediately.
    """

    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda active_file="", full_project=False: (True, "lake env lean Demo/Main.lean exits 0"),
    )

    autonomy_state: dict = {}
    initial = {
        "active_file": str(active),
        "declaration_scope": "file",
        "declaration_queue_total": 0,
        "diagnostics": f"{active}:2:3: warning: this tactic is never executed",
        "goals": "no goals",
        "build_status": "unknown",
        "sorry_count": 0,
    }

    first = runner._promote_live_state_to_verified(initial, autonomy_state)
    assert first["final_sweep_warning_cleanup_pending"] is True
    assert "final_sweep_baseline" in autonomy_state
    assert "final_sweep_cleanup_turn_started" not in autonomy_state

    second = runner._promote_live_state_to_verified(initial, autonomy_state)

    assert second["verification_ok"] is False
    assert second["final_sweep_warning_cleanup_pending"] is True
    assert second["queue_needs_final_file_sweep"] is True
    assert second["warning_cleanup_status"] == "pending"
    assert "final_sweep_baseline" in autonomy_state
    assert "final_sweep_cleanup_outcome_recorded" not in autonomy_state


def test_promote_live_state_skips_final_sweep_cleanup_when_flag_already_set(monkeypatch, tmp_path):
    """One-shot guarantee: once the flag is set, the runner accepts the
    warning-only state instead of looping the cleanup window."""

    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda active_file="", full_project=False: (True, "lake env lean Demo/Main.lean exits 0"),
    )

    autonomy_state: dict = {"final_sweep_cleanup_attempted": True}
    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "diagnostics": f"{active}:2:3: warning: this tactic is never executed",
            "goals": "no goals",
            "build_status": "unknown",
            "sorry_count": 0,
        },
        autonomy_state,
    )

    assert promoted["verification_ok"] is True
    assert "final_sweep_warning_cleanup_pending" not in promoted
    assert autonomy_state.get("final_sweep_cleanup_attempted") is True
    assert promoted["proof_solved"] is True
    assert promoted["warning_cleanup_status"] == "accepted"
    assert promoted["warning_cleanup_attempted"] is True
    assert promoted["warning_cleanup_verified"] is True
    assert promoted["warning_cleanup_warning_count"] == 1


def test_promote_live_state_records_final_sweep_cleanup_outcome(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")

    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda active_file="", full_project=False: (True, "lake env lean Demo/Main.lean exits 0"),
    )

    autonomy_state: dict = {"final_sweep_cleanup_attempted": True}
    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "unknown",
            "sorry_count": 0,
        },
        autonomy_state,
    )
    runner._promote_live_state_to_verified(dict(promoted), autonomy_state)

    cleanup_events = [
        event
        for event in read_workflow_activity(limit=10)
        if event["type"] == "final-sweep-warning-cleanup-verified"
    ]
    assert len(cleanup_events) == 1
    assert cleanup_events[0]["details"]["warning_count"] == 0


def test_promote_live_state_skips_final_sweep_cleanup_when_no_warnings(monkeypatch, tmp_path):
    """Clean files do not consume an extra cycle just because the queue is empty."""

    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda active_file="", full_project=False: (True, "lake env lean Demo/Main.lean exits 0"),
    )

    autonomy_state: dict = {}
    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "unknown",
            "sorry_count": 0,
        },
        autonomy_state,
    )

    assert promoted["verification_ok"] is True
    assert "final_sweep_warning_cleanup_pending" not in promoted
    assert "final_sweep_cleanup_attempted" not in autonomy_state
    assert "final_sweep_baseline" not in autonomy_state
    assert promoted["proof_solved"] is True
    assert promoted["warning_cleanup_status"] == "skipped"
    assert promoted["warning_cleanup_skipped"] is True
    assert "no warnings" in promoted["warning_cleanup_diagnostics"]


def test_promote_live_state_restores_baseline_when_cleanup_attempt_regresses(
    monkeypatch, tmp_path, capsys
):
    """If the cleanup-cycle edit breaks the file (lake newly fails), the
    runner must restore the captured baseline and accept the original
    warning-only state. We promised warning-tolerant — never ship worse."""

    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    baseline_content = "theorem t : True := by\n  trivial\n"
    # File on disk is the (broken) post-cleanup version; baseline below holds
    # the original passing content.
    broken_content = "theorem t : True := by\n  this_does_not_compile\n"
    active.write_text(broken_content, encoding="utf-8")

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (0, []))

    build_calls: list[tuple[str, bool]] = []

    def _fake_build(active_file="", *, full_project=False):
        build_calls.append((active_file, full_project))
        if len(build_calls) == 1:
            return False, "lake env lean Demo/Main.lean reported errors: unknown identifier"
        return True, "lake env lean Demo/Main.lean exits 0"

    monkeypatch.setattr(runner, "_run_explicit_verification_build", _fake_build)

    autonomy_state: dict = {
        "final_sweep_cleanup_attempted": True,
        "final_sweep_cleanup_turn_started": True,
        "final_sweep_baseline": {
            "active_file": str(active.resolve()),
            "content": baseline_content,
        },
    }
    promoted = runner._promote_live_state_to_verified(
        {
            "active_file": str(active),
            "declaration_scope": "file",
            "declaration_queue_total": 0,
            "diagnostics": "no errors found",
            "goals": "no goals",
            "build_status": "unknown",
            "sorry_count": 0,
        },
        autonomy_state,
    )

    assert (
        promoted["verification_ok"] is True
    ), "post-restore lake build should succeed against baseline"
    assert active.read_text(encoding="utf-8") == baseline_content
    assert (
        "final_sweep_baseline" not in autonomy_state
    ), "baseline payload should be released after one shot"
    assert "final_sweep_cleanup_turn_started" not in autonomy_state
    assert promoted["warning_cleanup_status"] == "blocked"
    assert promoted["warning_cleanup_blocked"] is True
    assert "unknown identifier" in promoted["warning_cleanup_diagnostics"]
    output = capsys.readouterr().out
    assert "↩️" in output and "restored active file to pre-cleanup baseline" in output


def test_startup_prompt_surfaces_final_sweep_warning_cleanup_on_resume(monkeypatch, tmp_path):
    """Regression: when the cleanup gate has fired (queue empty + warnings)
    and the workflow is being resumed from a checkpoint, the initial
    conversation prompt must include the warning-cleanup invitation. The
    bug was that ``_startup_user_message`` only consulted
    ``_queue_assignment_block`` (which returns "" for an empty queue) and
    never ``_final_file_sweep_block``, so the model resumed, saw the route
    line ``action: final-sweep``, observed the file was clean, and bailed
    without ever reading the cleanup-effort wording."""

    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
    monkeypatch.setattr(runner, "_runner_lean_prompt_enabled", lambda: False)
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_swarm_enabled", lambda: False)
    monkeypatch.setattr(runner, "_startup_active_skill_contract", lambda _name: "")
    monkeypatch.setattr(
        runner,
        "route_workflow_step",
        lambda *args, **kwargs: type(
            "Route",
            (),
            {
                "to_dict": lambda self: {
                    "skill_name": "lean-proof-loop",
                    "route_action": "final-sweep",
                    "blocker_kind": "open_goals",
                    "reason": "queue empty",
                    "recommended_worker": "",
                }
            },
        )(),
    )

    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo/Main.lean",
        "declaration_scope": "file",
        "declaration_queue_total": 0,
        "diagnostics": f"{active}:2:3: warning: this tactic is never executed",
        "verification_ok": False,
        "final_sweep_warning_cleanup_pending": True,
        "final_sweep_warning_count": 3,
        "final_sweep_warning_summary": (
            "- line 2: this tactic is never executed\n"
            "- line 5: 'all_goals omega' tactic does nothing\n"
            "- line 12: unused variable `hfpos`"
        ),
        "current_queue_item": {},
    }

    prompt = runner._startup_user_message(
        resumed_checkpoint={"label": "verified proof milestone"},
        live_state=live_state,
        autonomy_state={"final_sweep_cleanup_attempted": True},
    )

    # The cleanup-pending wording must reach the resume prompt.
    assert "warning cleanup (1/1 opportunity)" in prompt.lower()
    assert "3 warning(s) remain" in prompt
    assert "expected effort" in prompt.lower()
    assert "at least one safe edit" in prompt.lower()
    assert "bail clause" in prompt.lower()
    # The detected-warnings summary must be visible in the resume prompt.
    assert "all_goals omega" in prompt
    # Hard-blocker sweep wording should not bleed in (no `current blocker`).
    assert "current blocker:" not in prompt.lower()


def test_final_file_sweep_block_renders_warning_cleanup_wording(tmp_path):
    """Prompt block should switch to warning-cleanup wording when the cleanup
    is pending, instead of the generic hard-blocker sweep wording."""

    block = runner._final_file_sweep_block(
        {
            "active_file": str(tmp_path / "Main.lean"),
            "active_file_label": "Demo/Main.lean",
            "diagnostics": "Demo/Main.lean:2:3: warning: this tactic is never executed",
            "final_sweep_warning_cleanup_pending": True,
            "final_sweep_warning_count": 2,
            "final_sweep_warning_summary": "- line 2: this tactic is never executed\n- line 5: 'all_goals omega' tactic does nothing",
        }
    )

    assert "warning cleanup (1/1 opportunity)" in block.lower()
    assert "2 warning(s) remain" in block
    # Updated wording must require an attempt before the bail clause and
    # surface concrete safe-cleanup examples so the model can't trivially
    # decline without inspecting the file.
    assert "expected effort" in block.lower()
    assert "at least one safe edit" in block.lower()
    assert "apply_verified_patch" in block
    assert "bail clause" in block.lower()
    assert "actually inspected the file first" in block.lower()
    assert "do not touch theorem statements" in block.lower()
    assert "warnings will be accepted as-is" in block.lower()
    # Detected-warning summary must surface so the model has concrete targets.
    assert "all_goals omega" in block
    # Hard-blocker wording should not bleed in.
    assert "current blocker" not in block.lower()


def test_promote_live_state_does_not_mark_non_module_file_verified_from_project_build(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "RealTheorems-homework.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
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


def test_manager_verification_log_cache_is_bounded(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_project_root", lambda: str(tmp_path))
    runner._MANAGER_VERIFICATION_LOG_CACHE.clear()
    runner._MANAGER_VERIFICATION_LOG_CACHE_ORDER.clear()
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")

    for index in range(runner._MANAGER_VERIFICATION_LOG_CACHE_LIMIT + 5):
        runner._log_manager_verification(
            str(active),
            full_project=False,
            ok=True,
            build_status=f"lake env lean Main.lean succeeded {index}",
        )

    assert (
        len(runner._MANAGER_VERIFICATION_LOG_CACHE) == runner._MANAGER_VERIFICATION_LOG_CACHE_LIMIT
    )
    assert (
        len(runner._MANAGER_VERIFICATION_LOG_CACHE_ORDER)
        == runner._MANAGER_VERIFICATION_LOG_CACHE_LIMIT
    )
    assert (
        "file",
        "Main.lean",
        True,
        "lake env lean Main.lean succeeded 0",
    ) not in runner._MANAGER_VERIFICATION_LOG_CACHE
    capsys.readouterr()


def test_promote_live_state_file_scope_does_not_block_on_other_project_sorries(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
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


def test_promote_live_state_logs_internal_manager_verification(monkeypatch, tmp_path, capsys):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    recorded = []

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setattr(runner, "_MANAGER_VERIFICATION_LOG_CACHE", set())
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (2, ["Other.lean (2)"]))
    monkeypatch.setattr(
        runner,
        "_run_explicit_verification_build",
        lambda active_file="", full_project=False: (
            True,
            "lake env lean Demo/Main.lean succeeded",
        ),
    )
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda event_type, message, **details: recorded.append((event_type, message, details)),
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

    output = capsys.readouterr().out
    assert promoted["verification_ok"] is True
    assert "Manager verification (file): passed" in output
    assert "lake env lean Demo/Main.lean succeeded" in output
    assert recorded == [
        (
            "manager-verification",
            "Manager verification (file) passed",
            {
                "active_file": str(active),
                "active_file_label": "Demo/Main.lean",
                "full_project": False,
                "verification_ok": True,
                "build_status": "lake env lean Demo/Main.lean succeeded",
            },
        )
    ]


def test_normalize_blocker_summary_clears_resolved_text():
    assert runner._normalize_blocker_summary("None. All blockers resolved.") == ""
    assert (
        runner._normalize_blocker_summary("type mismatch in `simpa`") == "type mismatch in `simpa`"
    )


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


def test_recommended_verification_command_prefers_module_build_outside_single_item_turn(
    tmp_path, monkeypatch
):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.delenv("LEANFLOW_NATIVE_WORKFLOW_KIND", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)

    command = runner._recommended_verification_command(str(active))

    assert (
        command
        == "`lean_inspect` first, then `lean_verify(mode=module)` when the file is close to clean"
    )


def test_recommended_verification_command_requires_canonical_file_check_for_single_item_turn(
    tmp_path, monkeypatch
):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/Main.lean")

    command = runner._recommended_verification_command(str(active))

    assert command == (
        "`lean_inspect` on Demo/Main.lean, then `lean_incremental_check(check_target)` "
        "for this file-scoped queue step"
    )


def test_recommended_verification_command_falls_back_to_lake_env_lean_for_non_module_file(
    tmp_path, monkeypatch
):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "RealTheorems-homework.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    command = runner._recommended_verification_command(str(active))

    assert command == (
        "`lean_inspect` on Demo/RealTheorems-homework.lean, "
        "then final `lean_verify(mode=file_exact)` when close to clean"
    )


def test_extract_active_files_normalizes_project_relative_paths(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    files = runner._extract_active_files(f"a//{target}\n{target}\nDemo/Main.lean\nMain.lean\n")

    assert files == ["Demo/Main.lean", "Main.lean"]


def test_goals_still_open_ignores_structured_history_fields_without_current_goals():
    payload = {
        "line_context": "theorem demo : True := by",
        "goals": None,
        "goals_before": [],
        "goals_after": ["⊢ True"],
    }

    assert runner._goals_still_open(json.dumps(payload)) is False


def test_resolve_active_file_prefers_configured_active_file(monkeypatch, tmp_path):
    project = tmp_path / "ProveDemo"
    target = project / "ProveDemo" / "RealTheorems-homework.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "ProveDemo/RealTheorems-homework.lean")

    resolved = runner._resolve_active_file([])

    assert resolved == str(target.resolve())


def test_resolve_target_symbol_does_not_drift_from_history(monkeypatch):
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove ./ProveDemo/RealTheorems-homework.lean"
    )

    symbol = runner._resolve_target_symbol(
        [
            {"role": "assistant", "content": "reading Basic.lean"},
            {"role": "tool", "content": 'def hello := "world"'},
        ]
    )

    assert symbol == ""


def test_explicit_verification_build_uses_lake_env_lean_for_non_module_file(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "RealTheorems-homework.lean"
    active.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    captured: dict[str, object] = {}

    monkeypatch.setattr(
        runner,
        "lean_verify",
        lambda target="", cwd="", mode="project": (
            captured.update({"target": target, "cwd": cwd, "mode": mode})
            or type(
                "_Result",
                (),
                {
                    "ok": True,
                    "command": "lake build Demo.RealTheorems-homework",
                    "output": "",
                },
            )()
        ),
    )

    ok, status = runner._run_explicit_verification_build(str(active), full_project=False)

    assert ok is True
    assert captured["target"] == str(active)
    assert captured["cwd"] == str(project)
    assert captured["mode"] == "file_exact"
    assert status == "lake build Demo.RealTheorems-homework succeeded"


def test_write_workflow_checkpoint_persists_index_and_current(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", "/tmp/project")
    monkeypatch.setenv("LEANFLOW_NATIVE_MODEL", "zai-org/GLM-5.1")
    monkeypatch.setattr(
        runner, "_generate_checkpoint_summary", lambda *args, **kwargs: "## Goal\nResume proof"
    )
    monkeypatch.setattr(
        runner, "_latest_filesystem_checkpoint_hash", lambda *args, **kwargs: "abc123def456"
    )

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


def test_current_checkpoint_ignored_for_different_workflow_command(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")

    snapshot = project / ".leanflow" / "workflow-state" / "ckpt-old.json"
    runner._write_json_file(
        snapshot,
        {
            "version": 1,
            "checkpoint_id": "ckpt-old",
            "label": "verified proof milestone",
            "workflow_kind": "prove",
            "workflow_command": "/prove Other.lean",
            "project_root": str(project),
            "summary_text": "old run",
        },
    )
    runner._write_json_file(
        project / ".leanflow" / "workflow-state" / "current.json",
        {
            "version": 1,
            "checkpoint_id": "ckpt-old",
            "snapshot_path": str(snapshot),
        },
    )

    assert runner._load_current_checkpoint() is None
    status = runner._journal_status()
    assert status["current"] is None
    assert status["latest_label"] == ""


def test_current_checkpoint_loads_for_same_workflow_command(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")

    snapshot = project / ".leanflow" / "workflow-state" / "ckpt-current.json"
    runner._write_json_file(
        snapshot,
        {
            "version": 1,
            "checkpoint_id": "ckpt-current",
            "label": "verified proof milestone",
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "project_root": str(project),
            "summary_text": "current run",
        },
    )
    runner._write_json_file(
        project / ".leanflow" / "workflow-state" / "current.json",
        {
            "version": 1,
            "checkpoint_id": "ckpt-current",
            "snapshot_path": str(snapshot),
        },
    )

    loaded = runner._load_current_checkpoint()
    assert loaded is not None
    assert loaded["checkpoint_id"] == "ckpt-current"


def test_maybe_checkpoint_before_compaction_emits_pre_compaction_checkpoint(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
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
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_AUTONOMOUS_FOLLOWUPS", "3")

    class _LoopAgent(_FakeAgent):
        def __init__(self):
            super().__init__()
            self.calls = []

        def run_conversation(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            persist_user_message=None,
        ):
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
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states)
    )
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_auto_compact_history",
        lambda history, agent: (history, {"snapshot_text": "", "reason": "no-op"}),
    )
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
    assert agent.calls[0]["persist_user_message"] == "[leanflow-native autonomous continuation #1]"
    assert "Verification requires all of the following" in agent.calls[0]["user_message"]
    assert history[-1]["content"] == "continuation 1"
    assert runner._live_state_is_verified(live_state) is True


def test_drive_autonomous_followups_does_not_pause_at_followup_limit(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")

    class _LoopAgent(_FakeAgent):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def run_conversation(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            persist_user_message=None,
        ):
            self.calls += 1
            return {
                "messages": list(conversation_history or [])
                + [{"role": "assistant", "content": f"continuation {self.calls}"}]
            }

    def _live_state(idx: int, *, verified: bool = False) -> dict[str, object]:
        return {
            "active_file": "/tmp/project/Main.lean",
            "active_file_label": "Main.lean",
            "target_symbol": "demo",
            "diagnostics": (
                "no errors found" if verified else f"warning: declaration uses sorry {idx}"
            ),
            "goals": "no goals",
            "build_status": "lake env lean Main.lean exits 0" if verified else "unknown",
            "verification_ok": verified,
            "sorry_count": 0 if verified else 1,
            "message": f"live-{idx}",
        }

    verified_state = _live_state(99, verified=True)
    live_states = chain(
        [_live_state(idx) for idx in range(1, 10)] + [verified_state],
        repeat(verified_state),
    )

    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states)
    )
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_auto_compact_history",
        lambda history, agent: (history, {"snapshot_text": "", "reason": "no-op"}),
    )
    monkeypatch.setattr(runner, "_maybe_write_milestone_checkpoint", lambda *args, **kwargs: None)

    agent = _LoopAgent()
    history, _, _, live_state = runner._drive_autonomous_followups(
        agent,
        "system",
        [{"role": "assistant", "content": "initial"}],
        {"snapshot_text": "", "reason": "[none]"},
        {},
        {},
    )

    assert agent.calls == 3
    assert history[-1]["content"] == "continuation 3"
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
                },
                {
                    "attempt": 2,
                    "cycle": 2,
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "have h : True := by trivial",
                    "reason": "unsolved goals remain",
                },
            ]
        },
    )

    assert "PREVIOUS ATTEMPTS:" in prompt
    assert "attempt: 1" in prompt
    assert "proof shape: intro x y; simp" in prompt
    assert "why it failed: warning: declaration uses sorry" in prompt
    assert "attempt: 2" not in prompt


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
            "declaration_scope": "file",
            "declaration_queue_total": 2,
            "declaration_queue_summary": (
                "- next_demo [Demo/Main.lean] — contains sorry\n"
                "- future_demo [Demo/Main.lean] — contains sorry"
            ),
            "build_status": "unknown",
        },
    )

    assert "- file: Demo/Main.lean" in message
    assert "- exact tool path: /tmp/project/Demo/Main.lean" in message
    assert "- pending count: 1 pending" in message
    assert "future_demo" not in message
    assert "future queue items: hidden until the manager assigns them (1 pending)" in message


def test_handoff_pending_count_parses_summary_when_queue_items_are_absent():
    mgr = runner.TheoremQueueManager()

    assert (
        runner._handoff_pending_count(
            mgr,
            {
                "declaration_queue_summary": (
                    "- current_demo [Demo/Main.lean] — contains sorry\n"
                    "- future_demo [Demo/Main.lean] — contains sorry\n"
                    "- another_future [Demo/Main.lean] — diagnostic near line 10"
                ),
                "declaration_queue_total": 99,
            },
            "current_demo",
        )
        == 2
    )


def test_queue_handoff_sequence_matches_realtheorems_order_and_hides_future_items(
    monkeypatch, tmp_path
):
    project = tmp_path / "ProveDemo"
    module_dir = project / "ProveDemo"
    module_dir.mkdir(parents=True)
    active = module_dir / "RealTheorems.lean"
    labels = ["absLipschitz1", "abs_add_diff", "addLipschitz", "abs_mul_diff", "mulLipschitz"]

    def write_state(solved_count: int) -> None:
        lines = ["def isLipschitz (_f : Nat -> Nat) (_L : Nat) : Prop := True", ""]
        for index, label in enumerate(labels):
            lines.extend(
                [
                    f"theorem {label} : True := by",
                    "  trivial" if index < solved_count else "  sorry",
                    "",
                ]
            )
        active.write_text("\n".join(lines), encoding="utf-8")

    def live_state_for_queue() -> dict[str, object]:
        queue = runner._declaration_work_queue(
            str(active), "", project_root=str(project), scope="file"
        )
        current = runner._current_queue_item(queue, str(active))
        current_label = str((current or {}).get("label", "") or "")
        return {
            "active_file": str(active),
            "active_file_label": "ProveDemo/RealTheorems.lean",
            "target_symbol": current_label,
            "current_queue_item": dict(current or {}),
            "declaration_scope": "file",
            "declaration_queue_total": len(queue),
            "declaration_queue_summary": runner._format_declaration_queue(queue),
            "build_status": "lake env lean ProveDemo/RealTheorems.lean succeeded",
        }

    monkeypatch.setattr(runner, "_project_root", lambda: str(project))

    for solved_count, previous in enumerate(labels):
        write_state(solved_count)
        queue = runner._declaration_work_queue(
            str(active), "", project_root=str(project), scope="file"
        )
        assert [item["label"] for item in queue] == labels[solved_count:]
        assert runner._current_queue_item(queue, str(active))["label"] == previous

        write_state(solved_count + 1)
        next_queue = runner._declaration_work_queue(
            str(active), "", project_root=str(project), scope="file"
        )
        assert [item["label"] for item in next_queue] == labels[solved_count + 1 :]

        if solved_count == len(labels) - 1:
            assert next_queue == []
            assert live_state_for_queue()["declaration_queue_total"] == 0
            continue

        current = labels[solved_count + 1]
        handoff = runner._theorem_transition_handoff_message(
            {
                "target_symbol": previous,
                "active_file": str(active),
                "status": "solved",
                "note": f"{previous} no longer appears in the pending declaration queue.",
                "build_status": "lake env lean ProveDemo/RealTheorems.lean succeeded",
            },
            live_state_for_queue(),
        )

        assert f"- declaration: {previous}" in handoff
        assert f"- declaration: {current}" in handoff
        assert f"- assigned declaration: {current} - contains sorry" in handoff
        expected_pending = len(labels[solved_count + 2 :])
        assert f"- pending count: {expected_pending} pending" in handoff
        if expected_pending:
            assert (
                f"- future queue items: hidden until the manager assigns them ({expected_pending} pending)"
                in handoff
            )
        else:
            assert "- future queue items: no further queue items pending (0 pending)" in handoff
        for hidden_label in labels[solved_count + 2 :]:
            assert hidden_label not in handoff


def test_autonomous_continuation_prompt_switches_to_final_file_sweep_when_queue_empty(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/Main.lean")

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
    assert "intro x" in autonomy_state["current_queue_assignment"]["slice"]


def test_remember_failed_attempt_prefers_current_manager_reason():
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
            "current_queue_item_slice": "theorem demo : True := by\n  exact False.elim ?h",
            "blocker_summary": "stale previous linarith failure",
        },
        cycle_number=3,
        reason="latest rewrite failure",
    )

    attempt = autonomy_state["failed_attempts"][0]
    assert attempt["reason"] == "latest rewrite failure"


def test_remember_failed_attempt_prefers_restored_assignment_over_live_queue_item(
    tmp_path, monkeypatch
):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem original : True := by",
                "  exact True.intro",
                "",
                "theorem future : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "original",
            "active_file": str(active),
            "slice": "theorem original : True := by\n  sorry",
        }
    }

    runner._remember_failed_attempt(
        autonomy_state,
        {
            "target_symbol": "future",
            "active_file": str(active),
            "current_queue_item": {"label": "future", "reasons": ["contains sorry"]},
            "current_queue_item_slice": "theorem future : True := by\n  sorry",
            "blocker_summary": "unsolved goals",
        },
        cycle_number=7,
    )

    attempt = autonomy_state["failed_attempts"][0]
    assert attempt["target_symbol"] == "original"
    assert attempt["active_file"] == str(active)
    assert "theorem original" in autonomy_state["current_queue_assignment"]["slice"]


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


def test_recent_failed_attempts_summary_excludes_latest_in_file_attempt_and_honors_limit(
    monkeypatch,
):
    monkeypatch.setenv("LEANFLOW_NATIVE_FAILED_ATTEMPT_HISTORY", "2")

    summary = runner._recent_failed_attempts_summary(
        {
            "failed_attempts": [
                {
                    "attempt": 1,
                    "cycle": 1,
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "attempt one",
                    "reason": "first failure",
                },
                {
                    "attempt": 2,
                    "cycle": 2,
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "attempt two",
                    "reason": "second failure",
                },
                {
                    "attempt": 3,
                    "cycle": 3,
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "attempt three",
                    "reason": "third failure",
                },
                {
                    "attempt": 4,
                    "cycle": 4,
                    "target_symbol": "demo",
                    "active_file": "Demo/Main.lean",
                    "proof_shape": "attempt four",
                    "reason": "fourth failure",
                },
            ]
        },
        {
            "target_symbol": "demo",
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        },
    )

    assert "attempt: 1" not in summary
    assert "attempt: 2" in summary
    assert "attempt: 3" in summary
    assert "attempt: 4" not in summary


def test_failed_attempt_count_uses_latest_attempt_number_even_after_pruning(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_FAILED_ATTEMPT_HISTORY", "2")

    autonomy_state = {
        "failed_attempts": [
            {
                "attempt": 7,
                "cycle": 7,
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
                "proof_shape": "attempt seven",
                "reason": "failure seven",
            },
            {
                "attempt": 8,
                "cycle": 8,
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
                "proof_shape": "attempt eight",
                "reason": "failure eight",
            },
            {
                "attempt": 9,
                "cycle": 9,
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
                "proof_shape": "attempt nine",
                "reason": "failure nine",
            },
        ]
    }

    assert (
        runner._failed_attempt_count_for_theorem(
            autonomy_state,
            target_symbol="demo",
            active_file="Demo/Main.lean",
        )
        == 9
    )


def test_summarize_theorem_transition_outcome_marks_reverted_to_sorry():
    outcome = runner._summarize_theorem_transition_outcome(
        {
            "current_queue_assignment": {
                "target_symbol": "amc12a_2021_p19",
                "active_file": "ProveDemo/MiniF2F.lean",
                "slice": "theorem amc12a_2021_p19 : True := by\n  sorry",
            }
        },
        {
            "active_file_label": "ProveDemo/MiniF2F.lean",
            "current_queue_item": {
                "label": "algebra_amgm_sumasqdivbgeqsuma",
                "reasons": ["contains sorry"],
            },
            "declaration_queue_summary": (
                "- amc12a_2021_p19 [ProveDemo/MiniF2F.lean] — contains sorry\n"
                "- algebra_amgm_sumasqdivbgeqsuma [ProveDemo/MiniF2F.lean] — contains sorry"
            ),
            "current_blocker": "amc12a_2021_p19 remains pending after being reverted to `sorry`.",
            "build_status": "lake env lean ProveDemo/MiniF2F.lean exits 0",
        },
        [
            {
                "role": "assistant",
                "content": "amc12a_2021_p19 was reverted to `sorry` to unblock file compilation.",
            }
        ],
    )

    assert outcome["status"] == "reverted-to-sorry"
    assert "reverted" in outcome["note"]


def test_rebuild_history_for_theorem_transition_uses_compact_handoff(monkeypatch):
    monkeypatch.delenv("LEANFLOW_NATIVE_WORKFLOW_KIND", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)

    rebuilt, transition = runner._rebuild_history_for_theorem_transition(
        [
            {"role": "assistant", "content": "Detailed search transcript for amc12a_2021_p19"},
            {"role": "tool", "content": "Very long raw tool output for the previous theorem"},
        ],
        {"snapshot_text": "Compact workflow snapshot"},
        {
            "current_queue_assignment": {
                "target_symbol": "amc12a_2021_p19",
                "active_file": "ProveDemo/MiniF2F.lean",
                "slice": "theorem amc12a_2021_p19 : True := by\n  sorry",
            }
        },
        {
            "active_file_label": "ProveDemo/MiniF2F.lean",
            "current_queue_item": {
                "label": "algebra_amgm_sumasqdivbgeqsuma",
                "reasons": ["contains sorry"],
            },
            "declaration_queue_summary": "- algebra_amgm_sumasqdivbgeqsuma [ProveDemo/MiniF2F.lean] — contains sorry",
            "build_status": "lake env lean ProveDemo/MiniF2F.lean exits 0",
            "current_blocker": "",
        },
    )

    assert transition == {
        "previous_target": "amc12a_2021_p19",
        "previous_file": "ProveDemo/MiniF2F.lean",
        "current_target": "algebra_amgm_sumasqdivbgeqsuma",
        "current_file": "ProveDemo/MiniF2F.lean",
    }
    assert len(rebuilt) == 2
    assert rebuilt[0]["content"] == "Compact workflow snapshot"
    assert "Previous theorem outcome:" in rebuilt[1]["content"]
    assert "final status: solved" in rebuilt[1]["content"]
    joined = "\n".join(msg["content"] for msg in rebuilt)
    assert "Detailed search transcript" not in joined
    assert "Very long raw tool output" not in joined


def test_rebuild_history_for_theorem_transition_preserves_full_active_skill_contract(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/Main.lean")
    monkeypatch.setattr(runner, "_project_root", lambda: "/tmp/project")
    monkeypatch.setattr(
        runner,
        "load_skill",
        lambda name, cwd=None: {
            "name": name,
            "source": "builtin",
            "content": "Queue worker full contract body",
            "linked_files": {},
        },
    )

    class _Spec:
        spec_id = "prove"

    monkeypatch.setattr(runner, "specs_for_skill", lambda name: [_Spec()])

    rebuilt, transition = runner._rebuild_history_for_theorem_transition(
        [
            {"role": "assistant", "content": "Detailed search transcript for old theorem"},
            {"role": "tool", "content": "Very long raw tool output for old theorem"},
        ],
        {"snapshot_text": "Compact workflow snapshot"},
        {
            "current_queue_assignment": {
                "target_symbol": "old_demo",
                "active_file": "Demo/Main.lean",
                "slice": "theorem old_demo : True := by\n  exact True.intro",
            }
        },
        {
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "declaration_queue_summary": "- next_demo [Demo/Main.lean] — contains sorry",
            "build_status": "lake env lean Demo/Main.lean exits 0",
            "current_blocker": "",
        },
    )

    assert transition is not None
    assert len(rebuilt) == 3
    assert rebuilt[1]["content"].startswith("[LEANFLOW-NATIVE THEOREM TRANSITION ACTIVE SKILL]")
    assert "[LEANFLOW ACTIVE SKILL: lean-theorem-queue-worker (builtin)]" in rebuilt[1]["content"]
    assert "Queue worker full contract body" in rebuilt[1]["content"]
    assert "Linked workflow specs available through `skill_view`: prove." in rebuilt[1]["content"]
    assert rebuilt[2]["content"].startswith("[LEANFLOW-NATIVE THEOREM TRANSITION HANDOFF]")
    joined = "\n".join(msg["content"] for msg in rebuilt)
    assert "Detailed search transcript for old theorem" not in joined
    assert "Very long raw tool output for old theorem" not in joined


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
    assert (
        runner._same_queue_assignment_still_blocked(
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
        )
        is True
    )

    assert (
        runner._same_queue_assignment_still_blocked(
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
        )
        is False
    )


def test_restore_queue_assignment_to_baseline_sorry_replaces_only_assigned_declaration(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem demo : True := by\n"
        "  exact False.elim ?bad\n\n"
        "theorem next_demo : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "Assigned declaration slice (1-2):\ntheorem demo : True := by\n  sorry",
        }
    }

    result = runner._restore_queue_assignment_to_baseline_sorry(autonomy_state, {})

    assert result["restored"] is True
    text = active.read_text(encoding="utf-8")
    assert "-- LeanFlow failed attempt preserved after API step budget exhaustion." in text
    assert "--   exact False.elim ?bad" in text
    assert "theorem demo : True := by\n  sorry\n" in text
    assert "theorem next_demo : True := by\n  sorry" in text


def test_handle_api_step_budget_exhaustion_records_attempt_and_restores_sorry(
    monkeypatch, tmp_path
):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem demo : True := by\n  exact False.elim ?bad\n",
        encoding="utf-8",
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "Assigned declaration slice (1-2):\ntheorem demo : True := by\n  sorry",
        }
    }
    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo.lean",
        "target_symbol": "demo",
        "current_queue_item": {"label": "demo", "reasons": ["diagnostic near line 2"]},
        "current_queue_item_slice": (
            "Assigned declaration slice (1-2):\ntheorem demo : True := by\n  exact False.elim ?bad"
        ),
        "diagnostics": "error: unsolved goals",
        "goals": "⊢ True",
        "build_status": "error",
        "current_blocker": "error: unsolved goals",
    }
    post_live_state = {
        **live_state,
        "current_queue_item_slice": "Assigned declaration slice (1-2):\ntheorem demo : True := by\n  sorry",
        "diagnostics": "warning: declaration uses sorry",
        "goals": "no goals",
        "build_status": "lake env lean Demo.lean exits 0",
        "current_blocker": "contains sorry",
    }
    events = []

    class _Agent(_ManagedRunAgentStub):
        max_iterations = 180

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda path: {"ok": True, "command": f"lake env lean {path}"},
    )
    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: post_live_state
    )
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda state: state)
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    history, updated_live_state, attempt_recorded = runner._handle_api_step_budget_exhaustion(
        _Agent(),
        {"completed": False, "exit_reason": "max_iterations", "api_calls": 180},
        [{"role": "assistant", "content": "failed attempt"}],
        autonomy_state,
        live_state,
        cycle=3,
        phase="autonomous",
    )

    assert attempt_recorded is True
    assert updated_live_state is post_live_state
    assert "LEANFLOW-NATIVE API STEP BUDGET EXHAUSTED" in history[-1]["content"]
    assert "baseline `sorry` slice" in history[-1]["content"]
    assert autonomy_state["failed_attempts"][-1]["cycle"] == 3
    assert "exact False.elim" in autonomy_state["failed_attempts"][-1]["proof_shape"]
    restored_text = active.read_text(encoding="utf-8")
    assert "-- theorem demo : True := by" in restored_text
    assert "--   exact False.elim ?bad" in restored_text
    assert "theorem demo : True := by\n  sorry" in restored_text
    assert events[-1][0][0] == "api-step-budget-exhausted"


def test_rebuild_history_for_theorem_transition_records_blocked_outcome_without_fake_attempt():
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
    assert len(attempts) == 1
    assert attempts[0]["target_symbol"] == "blocked_demo"
    assert attempts[0]["active_file"] == "Demo/Main.lean"
    assert "still has unresolved goals" in attempts[0]["reason"]
    outcomes = autonomy_state["theorem_outcomes"]
    assert outcomes["Demo/Main.lean::blocked_demo"]["status"] == "blocked"


def test_rebuild_history_for_theorem_transition_clears_solved_theorem_failed_attempts():
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "solved_demo",
            "active_file": "Demo/Main.lean",
            "slice": "theorem solved_demo : True := by\n  exact True.intro",
        },
        "failed_attempts": [
            {
                "attempt": 1,
                "cycle": 1,
                "target_symbol": "solved_demo",
                "active_file": "Demo/Main.lean",
                "proof_shape": "attempt one",
                "reason": "first failure",
            },
            {
                "attempt": 2,
                "cycle": 2,
                "target_symbol": "other_demo",
                "active_file": "Demo/Main.lean",
                "proof_shape": "other attempt",
                "reason": "other failure",
            },
        ],
    }

    rebuilt_history, transition = runner._rebuild_history_for_theorem_transition(
        [{"role": "assistant", "content": "solved theorem, move on"}],
        {"snapshot_text": "Compact workflow snapshot"},
        autonomy_state,
        {
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
            "declaration_queue_summary": "- next_demo [Demo/Main.lean] — contains sorry",
            "build_status": "lake env lean Demo/Main.lean exits 0",
            "current_blocker": "",
        },
    )

    assert rebuilt_history is not None
    assert transition is not None
    attempts = autonomy_state["failed_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["target_symbol"] == "other_demo"


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
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
    monkeypatch.setattr(
        runner, "_resolve_active_file", lambda history, checkpoint_state=None: str(active)
    )
    monkeypatch.setattr(
        runner, "_resolve_target_symbol", lambda history, checkpoint_state=None: "demo"
    )
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


def test_build_live_proof_state_keeps_warning_only_items_out_of_primary_queue(
    monkeypatch, tmp_path
):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem style_warning : True := by",
                "  trivial",
                "",
                "theorem later : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
    monkeypatch.setattr(runner, "_project_root", lambda: str(project))
    monkeypatch.setattr(
        runner, "_resolve_active_file", lambda history, checkpoint_state=None: str(active)
    )
    monkeypatch.setattr(runner, "_resolve_target_symbol", lambda history, checkpoint_state=None: "")
    monkeypatch.setattr(runner, "_extract_recent_build_status", lambda history: "unknown")
    monkeypatch.setattr(runner, "_collect_message_text", lambda history: "")
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (1, ["Demo/Main.lean (1)"]))
    monkeypatch.setattr(runner, "recent_empty_search_streak", lambda workflow_command: 0)
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
                "diagnostics": f"{active}:2:3: warning: This line exceeds the 100 character limit",
                "goals": "no goals",
                "sorry_count": 1,
                "project_sorry_count": 1,
                "blocker_kind": "diagnostics",
                "queue_items": [
                    {
                        "label": "style_warning",
                        "kind": "theorem",
                        "line": 1,
                        "end_line": 2,
                        "reasons": ["diagnostic near line 2"],
                    },
                    {
                        "label": "future_demo",
                        "kind": "theorem",
                        "line": 4,
                        "end_line": 5,
                        "reasons": ["contains sorry"],
                    },
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

    assert live_state["current_queue_item"]["label"] == "later"
    assert "style_warning" not in live_state["declaration_queue_summary"]
    assert "later" in live_state["declaration_queue_summary"]


def test_build_live_proof_state_hides_future_sorries_from_model_message(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    module_dir = project / "Demo"
    module_dir.mkdir(parents=True)
    active = module_dir / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  sorry",
                "",
                "theorem future_demo : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "Demo/Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo/Main.lean")
    monkeypatch.setattr(runner, "_project_root", lambda: str(project))
    monkeypatch.setattr(
        runner, "_resolve_active_file", lambda history, checkpoint_state=None: str(active)
    )
    monkeypatch.setattr(runner, "_resolve_target_symbol", lambda history, checkpoint_state=None: "")
    monkeypatch.setattr(runner, "_extract_recent_build_status", lambda history: "unknown")
    monkeypatch.setattr(runner, "_collect_message_text", lambda history: "")
    monkeypatch.setattr(runner, "_count_project_sorries", lambda root: (2, ["Demo/Main.lean (2)"]))
    monkeypatch.setattr(runner, "recent_empty_search_streak", lambda workflow_command: 0)
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
                "diagnostics": "\n".join(
                    [
                        f"{active}:2:3: warning: declaration uses `sorry`",
                        f"{active}:5:3: warning: declaration uses `sorry`",
                    ]
                ),
                "goals": "no goals",
                "sorry_count": 2,
                "project_sorry_count": 2,
                "blocker_kind": "diagnostics",
                "queue_items": [
                    {
                        "label": "demo",
                        "kind": "theorem",
                        "line": 1,
                        "end_line": 2,
                        "reasons": ["contains sorry", "diagnostic near line 2"],
                    },
                    {
                        "label": "future_demo",
                        "kind": "theorem",
                        "line": 4,
                        "end_line": 5,
                        "reasons": ["contains sorry", "diagnostic near line 5"],
                    },
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

    assert "future_demo" in live_state["declaration_queue_summary"]
    assert "line 2" in live_state["message"]
    assert "line 5" not in live_state["message"]
    assert "future_demo" not in live_state["message"]
    assert "project files with sorry" not in live_state["message"]
    assert "future declaration sorry counts: hidden" in live_state["message"]


def test_drive_autonomous_followups_rebuilds_history_when_theorem_changes(monkeypatch, capsys):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_FILE", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_SKILL", raising=False)
    monkeypatch.delenv("LEANFLOW_NATIVE_ACTIVE_SKILL", raising=False)

    class _LoopAgent(_FakeAgent):
        def __init__(self):
            super().__init__()
            self.calls = []

        def run_conversation(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            persist_user_message=None,
        ):
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
                "active_file": "/tmp/project/ProveDemo/MiniF2F.lean",
                "active_file_label": "ProveDemo/MiniF2F.lean",
                "target_symbol": "algebra_amgm_sumasqdivbgeqsuma",
                "current_queue_item": {
                    "label": "algebra_amgm_sumasqdivbgeqsuma",
                    "reasons": ["contains sorry"],
                },
                "declaration_queue_summary": "- algebra_amgm_sumasqdivbgeqsuma [ProveDemo/MiniF2F.lean] — contains sorry",
                "diagnostics": "warning: declaration uses sorry",
                "goals": "no goals",
                "build_status": "lake env lean ProveDemo/MiniF2F.lean exits 0",
                "current_blocker": "",
                "message": "live-next-theorem",
                "sorry_count": 1,
            },
            {
                "active_file": "/tmp/project/ProveDemo/MiniF2F.lean",
                "active_file_label": "ProveDemo/MiniF2F.lean",
                "target_symbol": "algebra_amgm_sumasqdivbgeqsuma",
                "current_queue_item": {
                    "label": "algebra_amgm_sumasqdivbgeqsuma",
                    "reasons": ["contains sorry"],
                },
                "declaration_queue_summary": "- algebra_amgm_sumasqdivbgeqsuma [ProveDemo/MiniF2F.lean] — contains sorry",
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake env lean ProveDemo/MiniF2F.lean exits 0",
                "current_blocker": "",
                "message": "live-next-theorem-verified",
                "sorry_count": 0,
                "verification_ok": True,
            },
        ],
        repeat(
            {
                "active_file": "/tmp/project/ProveDemo/MiniF2F.lean",
                "active_file_label": "ProveDemo/MiniF2F.lean",
                "target_symbol": "algebra_amgm_sumasqdivbgeqsuma",
                "current_queue_item": {
                    "label": "algebra_amgm_sumasqdivbgeqsuma",
                    "reasons": ["contains sorry"],
                },
                "declaration_queue_summary": "- algebra_amgm_sumasqdivbgeqsuma [ProveDemo/MiniF2F.lean] — contains sorry",
                "diagnostics": "no errors found",
                "goals": "no goals",
                "build_status": "lake env lean ProveDemo/MiniF2F.lean exits 0",
                "current_blocker": "",
                "message": "live-next-theorem-verified",
                "sorry_count": 0,
                "verification_ok": True,
            }
        ),
    )

    monkeypatch.setattr(runner, "_journal_status", lambda: {})
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states)
    )
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_auto_compact_history",
        lambda history, agent: (
            history,
            {"snapshot_text": "Compact workflow snapshot", "reason": "no-op"},
        ),
    )
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
                "active_file": "ProveDemo/MiniF2F.lean",
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
    output = capsys.readouterr().out
    assert "Queue handoff for next model turn:" in output
    assert "Previous theorem outcome:" in output
    assert "Current queue focus:" in output
    assert "amc12a_2021_p19" in output
    assert "algebra_amgm_sumasqdivbgeqsuma" in output


def test_maybe_announce_final_file_sweep_skips_when_file_already_clean(monkeypatch, capsys):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "/tmp/project/Demo/Main.lean")
    events = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    autonomy_state = {"continuation_blocked_runs": 2, "continuation_stable_cycles": 2}
    live_state = {
        "declaration_scope": "file",
        "active_file": "/tmp/project/Demo/Main.lean",
        "declaration_queue_total": 0,
        "diagnostics": "no errors found",
        "goals": "no goals",
        "build_status": "lake env lean Demo/Main.lean exits 0",
        "sorry_count": 0,
        "verification_ok": True,
    }

    runner._maybe_announce_final_file_sweep_state(autonomy_state, live_state)
    runner._maybe_announce_final_file_sweep_state(autonomy_state, live_state)

    output = capsys.readouterr().out
    assert output.count("no further sweep needed") == 1
    assert events[0][0][0] == "final-file-sweep-skipped"


def test_maybe_announce_final_file_sweep_starts_when_queue_empty_but_file_blocked(
    monkeypatch, capsys
):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "/tmp/project/Demo/Main.lean")
    events = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    autonomy_state = {"continuation_blocked_runs": 2, "continuation_stable_cycles": 2}
    live_state = {
        "declaration_scope": "file",
        "active_file": "/tmp/project/Demo/Main.lean",
        "declaration_queue_total": 0,
        "diagnostics": "warning: declaration uses sorry",
        "goals": "no goals",
        "build_status": "lake env lean Demo/Main.lean exits 0",
        "sorry_count": 0,
        "verification_ok": False,
        "current_blocker": "warning: declaration uses sorry",
    }

    runner._maybe_announce_final_file_sweep_state(autonomy_state, live_state)
    runner._maybe_announce_final_file_sweep_state(autonomy_state, live_state)

    output = capsys.readouterr().out
    assert output.count("starting file-level cleanup") == 1
    assert events[0][0][0] == "final-file-sweep-started"
    assert autonomy_state["continuation_blocked_runs"] == 0
    assert autonomy_state["continuation_stable_cycles"] == 0


def test_maybe_announce_final_file_sweep_defers_for_document_review_gate(monkeypatch, capsys):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "formalize")
    monkeypatch.setenv("LEANFLOW_FORMALIZATION_DOCUMENT_RELATIVE", "docs/paper.tex")
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "/tmp/project/Demo/Main.lean")
    events = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )

    autonomy_state = {"continuation_blocked_runs": 2, "continuation_stable_cycles": 2}
    live_state = {
        "declaration_scope": "file",
        "active_file": "/tmp/project/Demo/Main.lean",
        "declaration_queue_total": 0,
        "diagnostics": "warning: declaration uses sorry",
        "goals": "no goals",
        "build_status": "waiting for document formalization handoff verifier",
        "sorry_count": 3,
        "verification_ok": False,
        "current_blocker": "statement/source verification is not approved",
        "document_formalization_handoff": {
            "ok": False,
            "issues": ["blueprint entry `line-143` statement/source verification is not approved"],
        },
    }

    runner._maybe_announce_final_file_sweep_state(autonomy_state, live_state)
    runner._maybe_announce_final_file_sweep_state(autonomy_state, live_state)

    output = capsys.readouterr().out
    assert output.count("waiting for independent statement/source verification") == 1
    assert "starting file-level cleanup" not in output
    assert runner._queue_needs_final_file_sweep(live_state) is False
    assert events[0][0][0] == "final-file-sweep-deferred-for-document-review"
    assert autonomy_state["continuation_blocked_runs"] == 0
    assert autonomy_state["continuation_stable_cycles"] == 0


def test_drive_autonomous_followups_keeps_history_when_theorem_does_not_change(monkeypatch):
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")

    class _LoopAgent(_FakeAgent):
        def __init__(self):
            super().__init__()
            self.calls = []

        def run_conversation(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            persist_user_message=None,
        ):
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
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states)
    )
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_auto_compact_history",
        lambda history, agent: (
            history,
            {"snapshot_text": "Compact workflow snapshot", "reason": "no-op"},
        ),
    )
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
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")

    class _LoopAgent(_FakeAgent):
        def __init__(self):
            super().__init__()
            self._managed_base_reasoning_config = {"mode": "auto"}
            self.reasoning_config = None
            self.calls = []

        def run_conversation(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            persist_user_message=None,
        ):
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
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states)
    )
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_auto_compact_history",
        lambda history, agent: (
            history,
            {"snapshot_text": "Compact workflow snapshot", "reason": "no-op"},
        ),
    )
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
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_AUTONOMOUS_FOLLOWUPS", "2")

    class _LoopAgent(_FakeAgent):
        def run_conversation(
            self,
            user_message,
            system_message=None,
            conversation_history=None,
            persist_user_message=None,
        ):
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
    monkeypatch.setattr(
        runner, "_build_live_proof_state", lambda history, checkpoint_state=None: next(live_states)
    )
    monkeypatch.setattr(runner, "_promote_live_state_to_verified", lambda live_state: live_state)
    monkeypatch.setattr(runner, "_maybe_checkpoint_before_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_auto_compact_history",
        lambda history, agent: (
            history,
            {"snapshot_text": "Compact workflow snapshot", "reason": "no-op"},
        ),
    )
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


def test_autonomous_stop_reason_stalls_despite_volatile_elapsed(monkeypatch):
    # Regression for the post-verification livelock: build_status carries a per-cycle
    # "elapsed: <wall-clock>s" token. If that volatile token enters the stall signature, a
    # genuinely no-progress autonomous loop never trips the "stalled" safety net and spins
    # forever. The signature must ignore the elapsed token.
    monkeypatch.setattr(
        runner, "_document_formalization_ready_for_prover_handoff", lambda ls: False
    )
    monkeypatch.setattr(
        runner, "_document_formalization_waiting_for_independent_review", lambda ls: False
    )
    autonomy_state: dict = {}
    reasons = []
    for i in range(8):
        live_state = {
            "active_file": "",  # -> _live_state_is_verified() is False, so we reach the stall logic
            "active_file_label": "F.lean",
            "target_symbol": "thm",
            "diagnostics": "",
            "goals": "",
            # Only the elapsed token changes each cycle; everything else is identical.
            "build_status": f"lake build succeeded | elapsed: {i}.123s",
            "sorry_count": 0,
        }
        reasons.append(runner._autonomous_stop_reason([], live_state, autonomy_state))
    assert "stalled" in reasons, f"stall net never tripped despite a stable state: {reasons}"


def test_autonomous_stop_reason_blocker_prose_does_not_stop_while_state_advances(monkeypatch):
    # Regression: GPT/codex models constantly emit give-up phrasing ("I was unable
    # to ...", "failed to typecheck") even while still editing. The hard "blocked"
    # stop must require BOTH a declared blocker AND a non-advancing live state, so
    # that ordinary progress narration never terminates a run that is making real
    # changes.
    monkeypatch.setattr(
        runner, "_document_formalization_ready_for_prover_handoff", lambda ls: False
    )
    monkeypatch.setattr(
        runner, "_document_formalization_waiting_for_independent_review", lambda ls: False
    )
    blocker_history = [
        {
            "role": "assistant",
            "content": "I was unable to close the goal; the tactic failed to unify. Still working.",
        }
    ]

    # State advances every cycle (diagnostics differ) -> never "blocked".
    autonomy_state: dict = {}
    advancing = []
    for i in range(6):
        live_state = {
            "active_file": "",
            "active_file_label": "F.lean",
            "target_symbol": "thm",
            "diagnostics": f"error variant {i}",
            "goals": "open goal",
            "build_status": "reported errors",
            "sorry_count": 1,
        }
        advancing.append(
            runner._autonomous_stop_reason(blocker_history, live_state, autonomy_state)
        )
    assert "blocked" not in advancing, f"blocker prose ended an advancing run: {advancing}"
    assert autonomy_state.get("continuation_blocked_runs", 0) == 0

    # Unchanging state + the same blocker prose -> genuinely stuck -> "blocked".
    autonomy_state = {}
    stuck = []
    for _ in range(4):
        live_state = {
            "active_file": "",
            "active_file_label": "F.lean",
            "target_symbol": "thm",
            "diagnostics": "identical error",
            "goals": "open goal",
            "build_status": "reported errors",
            "sorry_count": 1,
        }
        stuck.append(runner._autonomous_stop_reason(blocker_history, live_state, autonomy_state))
    assert "blocked" in stuck, f"genuinely stuck run never gave up: {stuck}"
