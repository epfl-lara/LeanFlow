"""Tests for the interrupt system.

Run with: python -m pytest tests/test_interrupt.py -v
"""

import queue
import shlex
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Unit tests: shared interrupt module
# ---------------------------------------------------------------------------


class TestInterruptModule:
    """Tests for tools/interrupt.py"""

    def test_set_and_check(self):
        from tools.utilities.interrupt import is_interrupted, set_interrupt

        set_interrupt(False)
        assert not is_interrupted()

        set_interrupt(True)
        assert is_interrupted()

        set_interrupt(False)
        assert not is_interrupted()

    def test_thread_safety(self):
        """Set from one thread, check from another."""
        from tools.utilities.interrupt import is_interrupted, set_interrupt

        set_interrupt(False)

        seen = {"value": False}

        def _checker():
            while not is_interrupted():
                time.sleep(0.01)
            seen["value"] = True

        t = threading.Thread(target=_checker, daemon=True)
        t.start()

        time.sleep(0.05)
        assert not seen["value"]

        set_interrupt(True)
        t.join(timeout=1)
        assert seen["value"]

        set_interrupt(False)

    def test_raise_if_interrupted_uses_non_recoverable_cooperative_signal(self):
        from tools.utilities.interrupt import (
            CooperativeInterrupt,
            raise_if_interrupted,
            set_interrupt,
        )

        set_interrupt(False)
        raise_if_interrupted()
        set_interrupt(True)
        try:
            with pytest.raises(CooperativeInterrupt, match="planner cancelled"):
                raise_if_interrupted("planner cancelled")
        finally:
            set_interrupt(False)


# ---------------------------------------------------------------------------
# Unit tests: pre-tool interrupt check
# ---------------------------------------------------------------------------


class TestPreToolCheck:
    """Verify that _execute_tool_calls skips all tools when interrupted."""

    def test_all_tools_skipped_when_interrupted(self):
        """Mock an interrupted agent and verify no tools execute."""
        # Build a fake assistant_message with 3 tool calls
        tc1 = MagicMock()
        tc1.id = "tc_1"
        tc1.function.name = "terminal"
        tc1.function.arguments = '{"command": "rm -rf /"}'

        tc2 = MagicMock()
        tc2.id = "tc_2"
        tc2.function.name = "terminal"
        tc2.function.arguments = '{"command": "echo hello"}'

        tc3 = MagicMock()
        tc3.id = "tc_3"
        tc3.function.name = "web_search"
        tc3.function.arguments = '{"query": "test"}'

        assistant_msg = MagicMock()
        assistant_msg.tool_calls = [tc1, tc2, tc3]

        messages = []

        # Create a minimal mock agent with _interrupt_requested = True
        agent = MagicMock()
        agent._interrupt_requested = True
        agent.log_prefix = ""
        agent._persist_session = MagicMock()

        # Import and call the method
        import types

        from run_agent import AIAgent

        # Bind the real methods to our mock so dispatch works correctly
        agent._execute_tool_calls_sequential = types.MethodType(
            AIAgent._execute_tool_calls_sequential, agent
        )
        agent._execute_tool_calls_concurrent = types.MethodType(
            AIAgent._execute_tool_calls_concurrent, agent
        )
        AIAgent._execute_tool_calls(agent, assistant_msg, messages, "default")

        # All 3 should be skipped
        assert len(messages) == 3
        for msg in messages:
            assert msg["role"] == "tool"
            assert "cancelled" in msg["content"].lower() or "interrupted" in msg["content"].lower()

        # No actual tool handlers should have been called
        # (handle_function_call should NOT have been invoked)


# ---------------------------------------------------------------------------
# Unit tests: message combining
# ---------------------------------------------------------------------------


class TestMessageCombining:
    """Verify multiple interrupt messages are joined."""

    def test_cli_interrupt_queue_drain(self):
        """Simulate draining multiple messages from the interrupt queue."""
        q = queue.Queue()
        q.put("Stop!")
        q.put("Don't delete anything")
        q.put("Show me what you were going to delete instead")

        parts = []
        while not q.empty():
            try:
                msg = q.get_nowait()
                if msg:
                    parts.append(msg)
            except queue.Empty:
                break

        combined = "\n".join(parts)
        assert "Stop!" in combined
        assert "Don't delete anything" in combined
        assert "Show me what you were going to delete instead" in combined
        assert combined.count("\n") == 2

    def test_gateway_pending_messages_append(self):
        """Simulate gateway _pending_messages append logic."""
        pending = {}
        key = "agent:main:telegram:dm"

        # First message
        if key in pending:
            pending[key] += "\n" + "Stop!"
        else:
            pending[key] = "Stop!"

        # Second message
        if key in pending:
            pending[key] += "\n" + "Do something else instead"
        else:
            pending[key] = "Do something else instead"

        assert pending[key] == "Stop!\nDo something else instead"


# ---------------------------------------------------------------------------
# Integration tests (require local terminal)
# ---------------------------------------------------------------------------


class TestLocalInterruptedOutput:
    """Pin the oneshot fence protocol at the interrupt boundary."""

    def test_completed_fenced_command_keeps_real_returncode(self):
        """Do not rewrite a completed command to 130 during an interrupt race."""
        from tools.environments.local import _OUTPUT_FENCE, LocalEnvironment

        drained = threading.Event()

        class FenceStream:
            def __iter__(self):
                yield f"{_OUTPUT_FENCE}completed output\n{_OUTPUT_FENCE}logout\n"
                drained.set()

            def close(self):
                return None

        class CompletedFenceProcess:
            def __init__(self):
                self.stdout = FenceStream()
                self.returncode = None

            def poll(self):
                drained.wait(timeout=1)
                return self.returncode

            def wait(self, timeout=None):
                self.returncode = 7
                return self.returncode

        env = LocalEnvironment(cwd="/tmp", timeout=30)
        proc = CompletedFenceProcess()

        with (
            patch("tools.environments.local.is_interrupted", return_value=True),
            patch.object(env, "_terminate_process") as terminate,
        ):
            result = env._wait_for_oneshot_process(
                proc,
                effective_stdin=None,
                effective_timeout=30,
            )

        assert result == {"output": "completed output\n", "returncode": 7}
        terminate.assert_not_called()

    def test_completion_race_before_termination_keeps_real_returncode(self):
        """A process that exits at the final kill boundary is never reported as interrupted."""
        from tools.environments.local import LocalEnvironment

        class PartialStream:
            def __iter__(self):
                yield "partial output\n"

            def close(self):
                return None

        class RacingProcess:
            def __init__(self):
                self.stdout = PartialStream()
                self.returncode = None
                self.poll_count = 0

            def poll(self):
                self.poll_count += 1
                if self.poll_count <= 2:
                    return None
                self.returncode = 9
                return self.returncode

        env = LocalEnvironment(cwd="/tmp", timeout=30)
        proc = RacingProcess()

        with (
            patch("tools.environments.local.is_interrupted", return_value=True),
            patch("tools.environments.local.terminate_process_tree") as terminate_tree,
        ):
            result = env._wait_for_oneshot_process(
                proc,
                effective_stdin=None,
                effective_timeout=30,
            )

        assert result == {"output": "partial output\n", "returncode": 9}
        terminate_tree.assert_not_called()

    @pytest.mark.skipif(not __import__("shutil").which("bash"), reason="Requires bash")
    def test_interrupted_output_hides_fence_and_logout(self, tmp_path):
        """Return useful partial output without exposing login-shell protocol text."""
        from tools.environments.local import _OUTPUT_FENCE, LocalEnvironment
        from tools.utilities.interrupt import set_interrupt

        set_interrupt(False)
        env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
        ready = tmp_path / "command-ready"
        result_holder = {"value": None}

        def _run():
            result_holder["value"] = env.execute(
                f"printf 'partial-output\\n'; touch {shlex.quote(str(ready))}; sleep 60",
                timeout=30,
            )

        thread = threading.Thread(target=_run)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists()

            set_interrupt(True)
            thread.join(timeout=5)
            assert not thread.is_alive()
        finally:
            set_interrupt(False)
            env.cleanup()
            thread.join(timeout=1)

        result = result_holder["value"]
        assert result is not None
        assert result["returncode"] == 130
        assert "partial-output" in result["output"]
        assert "interrupted" in result["output"].lower()
        assert _OUTPUT_FENCE not in result["output"]
        assert "logout" not in result["output"].lower()


class TestSIGKILLEscalation:
    """Test that SIGTERM-resistant processes get SIGKILL'd."""

    @pytest.mark.skipif(not __import__("shutil").which("bash"), reason="Requires bash")
    def test_sigterm_trap_killed_within_2s(self):
        """A process that traps SIGTERM should be SIGKILL'd after 1s grace."""
        from tools.environments.local import LocalEnvironment
        from tools.utilities.interrupt import set_interrupt

        set_interrupt(False)
        env = LocalEnvironment(cwd="/tmp", timeout=30)

        # Start execution in a thread, interrupt after 0.5s
        result_holder = {"value": None}

        def _run():
            result_holder["value"] = env.execute(
                "trap '' TERM; sleep 60",
                timeout=30,
            )

        t = threading.Thread(target=_run)
        t.start()

        time.sleep(0.5)
        set_interrupt(True)

        t.join(timeout=5)
        set_interrupt(False)

        assert result_holder["value"] is not None
        assert result_holder["value"]["returncode"] == 130
        assert "interrupted" in result_holder["value"]["output"].lower()


# ---------------------------------------------------------------------------
# Manual smoke test checklist (not automated)
# ---------------------------------------------------------------------------

SMOKE_TESTS = """
Manual Smoke Test Checklist:

1. CLI: Run `leanflow`, ask it to `sleep 30` in terminal, type "stop" + Enter.
   Expected: command dies within 2s, agent responds to "stop".

2. CLI: Ask it to extract content from 5 URLs, type interrupt mid-way.
   Expected: remaining URLs are skipped, partial results returned.

3. Gateway (Telegram): Send a long task, then send "Stop".
   Expected: agent stops and responds acknowledging the stop.

4. Gateway (Telegram): Send "Stop" then "Do X instead" rapidly.
   Expected: both messages appear as the next prompt (joined by newline).

5. CLI: Start a task that generates 3+ tool calls in one batch.
   Type interrupt during the first tool call.
   Expected: only 1 tool executes, remaining are skipped.
"""
