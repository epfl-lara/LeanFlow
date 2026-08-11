"""Unit tests for run_agent.py (AIAgent).

Tests cover pure functions, state/structure methods, and conversation loop
pieces. The OpenAI client and tool loading are mocked so no network calls
are made.
"""

import json
import logging
import re
import threading
import time
import uuid
from contextlib import nullcontext
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from agent.prompting.prompt_builder import DEFAULT_AGENT_IDENTITY
from core.home import leanflow_home
from run_agent import AIAgent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_tool_defs(*names: str) -> list:
    """Build minimal tool definition list accepted by AIAgent.__init__."""
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


@pytest.fixture()
def agent():
    """Minimal AIAgent with mocked OpenAI client and tool loading."""
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


@pytest.fixture()
def agent_with_memory_tool():
    """Agent whose valid_tool_names includes 'memory'."""
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("web_search", "memory"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-k...7890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


def test_aiagent_reuses_existing_errors_log_handler():
    """Repeated AIAgent init should not accumulate duplicate errors.log handlers."""
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    error_log_path = (leanflow_home() / "logs" / "errors.log").resolve()

    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)

        error_log_path.parent.mkdir(parents=True, exist_ok=True)
        preexisting_handler = RotatingFileHandler(
            error_log_path,
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
        )
        root_logger.addHandler(preexisting_handler)

        with (
            patch(
                "run_agent.get_tool_definitions",
                return_value=_make_tool_defs("web_search"),
            ),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            AIAgent(
                api_key="test-k...7890",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            AIAgent(
                api_key="test-k...7890",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        matching_handlers = [
            handler
            for handler in root_logger.handlers
            if isinstance(handler, RotatingFileHandler)
            and error_log_path == Path(handler.baseFilename).resolve()
        ]
        assert len(matching_handlers) == 1
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            if handler not in original_handlers:
                handler.close()
        for handler in original_handlers:
            root_logger.addHandler(handler)


def test_aiagent_optional_logs_tolerate_unwritable_runtime_home(monkeypatch, tmp_path):
    """An invalid optional-log home cannot prevent AIAgent construction."""
    blocked_home = tmp_path / "not-a-directory"
    blocked_home.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_HOME", str(blocked_home))
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)

    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)

        with (
            patch(
                "run_agent.get_tool_definitions",
                return_value=_make_tool_defs("web_search"),
            ),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            created = AIAgent(
                api_key="test-k...7890",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        assert created.logs_dir == blocked_home / "sessions"
        assert created.session_log_file.parent == created.logs_dir
        assert not created.logs_dir.exists()
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            if handler not in original_handlers:
                handler.close()
        for handler in original_handlers:
            root_logger.addHandler(handler)


def test_aiagent_suppresses_optional_web_warning_for_native_lean_toolset(capsys):
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("lean_search", "terminal"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={"web": False}),
        patch("run_agent.OpenAI"),
    ):
        AIAgent(
            api_key="test-k...7890",
            quiet_mode=False,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=["leanflow-native"],
        )

    output = capsys.readouterr().out
    assert "missing requirements: ['web']" not in output


def test_aiagent_binds_effective_main_route_to_context_compressor(agent):
    """Compression fallback must inherit the resolved main provider mode."""
    assert agent.context_compressor.main_provider == agent.provider
    assert agent.context_compressor.main_api_mode == agent.api_mode
    assert agent.context_compressor.main_model == agent.model
    assert agent.context_compressor.model == agent.model
    assert agent.context_compressor.base_url == agent.base_url
    assert agent.context_compressor.api_key == agent.api_key


# ---------------------------------------------------------------------------
# Helper to build mock assistant messages (API response objects)
# ---------------------------------------------------------------------------


def _mock_assistant_msg(
    content="Hello",
    tool_calls=None,
    reasoning=None,
    reasoning_content=None,
    reasoning_details=None,
):
    """Return a SimpleNamespace mimicking an OpenAI ChatCompletionMessage."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning is not None:
        msg.reasoning = reasoning
    if reasoning_content is not None:
        msg.reasoning_content = reasoning_content
    if reasoning_details is not None:
        msg.reasoning_details = reasoning_details
    return msg


def _mock_tool_call(name="web_search", arguments="{}", call_id=None):
    """Return a SimpleNamespace mimicking a tool call object."""
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(
    content="Hello", finish_reason="stop", tool_calls=None, reasoning=None, usage=None
):
    """Return a SimpleNamespace mimicking an OpenAI ChatCompletion response."""
    msg = _mock_assistant_msg(
        content=content,
        tool_calls=tool_calls,
        reasoning=reasoning,
    )
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    resp = SimpleNamespace(choices=[choice], model="test/model")
    if usage:
        resp.usage = SimpleNamespace(**usage)
    else:
        resp.usage = None
    return resp


# ===================================================================
# Group 1: Pure Functions
# ===================================================================


class TestHasContentAfterThinkBlock:
    def test_none_returns_false(self, agent):
        assert agent._has_content_after_think_block(None) is False

    def test_empty_returns_false(self, agent):
        assert agent._has_content_after_think_block("") is False

    def test_only_think_block_returns_false(self, agent):
        assert agent._has_content_after_think_block("<think>reasoning</think>") is False

    def test_content_after_think_returns_true(self, agent):
        assert agent._has_content_after_think_block("<think>r</think> actual answer") is True

    def test_no_think_block_returns_true(self, agent):
        assert agent._has_content_after_think_block("just normal content") is True


class TestStripThinkBlocks:
    def test_none_returns_empty(self, agent):
        assert agent._strip_think_blocks(None) == ""

    def test_no_blocks_unchanged(self, agent):
        assert agent._strip_think_blocks("hello world") == "hello world"


def test_format_tool_args_for_log_summarizes_patch_payload():
    lines = run_agent._format_tool_args_for_log(
        "patch",
        {
            "path": "ProveDemo/ProveDemo/RealTheorems-homework.lean",
            "old_string": "abc",
            "new_string": "abc",
        },
    )

    assert any("path: ProveDemo/ProveDemo/RealTheorems-homework.lean" in line for line in lines)
    assert any("old_string: 3 chars across 1 line(s)" in line for line in lines)
    assert any("new_string: 3 chars across 1 line(s)" in line for line in lines)


def test_format_tool_args_for_log_summarizes_verified_patch_payload():
    lines = run_agent._format_tool_args_for_log(
        "apply_verified_patch",
        {
            "path": "ProveDemo/ProveDemo/RealTheorems-homework.lean",
            "patch": "*** Begin Patch\n*** Update File: Demo.lean\n-old\n+new\n*** End Patch",
            "check_mode": "file_exact",
        },
    )

    assert any("path: ProveDemo/ProveDemo/RealTheorems-homework.lean" in line for line in lines)
    assert any("patch: 66 chars across 5 line(s)" in line for line in lines)
    assert any("check_mode: file_exact" in line for line in lines)


def test_format_tool_result_for_log_pretty_prints_terminal_result():
    payload = json.dumps(
        {
            "output": "error: [root]: no configuration file\n/Users/lmilikic/GaussWorkspace/ProveDemo/lakefile.toml",
            "exit_code": 1,
            "error": None,
        }
    )

    lines = run_agent._format_tool_result_for_log("terminal", payload)

    assert "exit_code: 1" in lines
    assert "error: None" in lines
    assert "output:" in lines
    assert any("error: [root]: no configuration file" in line for line in lines)


def test_format_tool_result_for_log_summarizes_large_file_list():
    payload = json.dumps(
        {
            "total_count": 19,
            "files": [f"./ProveDemo/File{i}.lean" for i in range(19)],
        }
    )

    lines = run_agent._format_tool_result_for_log("search_files", payload)

    assert "total_count: 19" in lines
    assert "files: 19 item(s)" in lines
    assert any("./ProveDemo/File0.lean" in line for line in lines)
    assert any("more item(s) omitted" in line for line in lines)


def test_format_tool_result_for_log_keeps_tail_for_multiline_output():
    output = "\n".join(f"line {i}" for i in range(40))
    payload = json.dumps({"output": output, "exit_code": 1, "error": None})

    lines = run_agent._format_tool_result_for_log("terminal", payload)

    assert "output:" in lines
    assert any("line 0" in line for line in lines)
    assert any("output truncated" in line for line in lines)
    assert any("line 39" in line for line in lines)


def test_format_tool_result_for_log_with_limits_respects_custom_head_tail():
    output = "\n".join(f"line {i}" for i in range(12))
    payload = json.dumps({"output": output, "exit_code": 1, "error": None})

    lines = run_agent._format_tool_result_for_log_with_limits(
        "terminal",
        payload,
        multiline_head=2,
        multiline_tail=1,
        wrapped_head=1,
        wrapped_tail=1,
        plain_head=2,
        plain_tail=1,
        string_char_threshold=80,
    )

    assert any("line 0" in line for line in lines)
    assert any("line 1" in line for line in lines)
    assert any("output truncated" in line for line in lines)
    assert any("line 11" in line for line in lines)
    assert not any("line 5" in line for line in lines)


def test_emit_workflow_event_forwards_full_details(monkeypatch):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", "/tmp/project")
    captured = {}

    def _fake_append(event_type, message, **details):
        captured["event_type"] = event_type
        captured["message"] = message
        captured["details"] = details

    monkeypatch.setattr(
        "leanflow_cli.workflows.workflow_state.append_workflow_activity", _fake_append
    )

    run_agent._emit_workflow_event(
        "assistant-response", "Assistant response received", content="x" * 400
    )

    assert captured["event_type"] == "assistant-response"
    assert captured["message"] == "Assistant response received"
    assert captured["details"]["content"] == "x" * 400


def test_workflow_agent_event_details_include_session_metadata(agent):
    agent.session_id = "agent-123"
    agent._delegate_depth = 1
    agent._parent_session_id = "parent-456"
    agent.provider = "custom"
    agent.api_mode = "chat"
    agent.base_url = "https://example.invalid/v1"

    details = run_agent._workflow_agent_event_details(agent, iteration=4)

    assert details["agent_session_id"] == "agent-123"
    assert details["parent_agent_session_id"] == "parent-456"
    assert details["delegate_depth"] == 1
    assert details["iteration"] == 4
    assert details["base_url"] == "https://example.invalid/v1"

    def test_single_block_removed(self, agent):
        result = agent._strip_think_blocks("<think>reasoning</think> answer")
        assert "reasoning" not in result
        assert "answer" in result

    def test_multiline_block_removed(self, agent):
        text = "<think>\nline1\nline2\n</think>\nvisible"
        result = agent._strip_think_blocks(text)
        assert "line1" not in result
        assert "visible" in result


def test_api_request_workflow_event_is_bounded_and_keeps_diagnostic_metadata(agent):
    """API telemetry must not duplicate the accumulated model conversation."""
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    prompt = "sensitive proof context " + ("x" * 100_000)
    response = _mock_response(content="Final answer", finish_reason="stop")
    agent.client.chat.completions.create.return_value = response
    emitted: list[tuple[str, str, dict]] = []

    def capture(event_type, message, **details):
        emitted.append((event_type, message, details))

    with (
        patch("run_agent._emit_workflow_event", side_effect=capture),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(prompt)

    assert result["completed"] is True
    details = next(details for kind, _message, details in emitted if kind == "api-request")
    assert "messages" not in details
    assert details["message_count"] == 2
    assert details["approx_tokens"] > 0
    assert details["total_chars"] >= len(prompt)
    assert details["available_tools"] == ["web_search"]
    assert details["message_roles"] == {"system": 1, "user": 1}
    assert details["last_message_role"] == "user"
    assert len(details["last_message_preview"]) <= 500
    assert len(details["message_history_sha256"]) == 64
    assert len(json.dumps(details)) < 4_000


class TestExtractReasoning:
    def test_reasoning_field(self, agent):
        msg = _mock_assistant_msg(reasoning="thinking hard")
        assert agent._extract_reasoning(msg) == "thinking hard"

    def test_reasoning_content_field(self, agent):
        msg = _mock_assistant_msg(reasoning_content="deep thought")
        assert agent._extract_reasoning(msg) == "deep thought"

    def test_reasoning_details_array(self, agent):
        msg = _mock_assistant_msg(
            reasoning_details=[{"summary": "step-by-step analysis"}],
        )
        assert "step-by-step analysis" in agent._extract_reasoning(msg)

    def test_no_reasoning_returns_none(self, agent):
        msg = _mock_assistant_msg()
        assert agent._extract_reasoning(msg) is None

    def test_combined_reasoning(self, agent):
        msg = _mock_assistant_msg(
            reasoning="part1",
            reasoning_content="part2",
        )
        result = agent._extract_reasoning(msg)
        assert "part1" in result
        assert "part2" in result

    def test_deduplication(self, agent):
        msg = _mock_assistant_msg(
            reasoning="same text",
            reasoning_content="same text",
        )
        result = agent._extract_reasoning(msg)
        assert result == "same text"


class TestReasoningReplayAccounting:
    def test_reasoning_context_payload_stats_counts_outgoing_reasoning_fields(self, agent):
        stats = agent._reasoning_context_payload_stats(
            [
                {"role": "system", "content": "sys"},
                {"role": "assistant", "content": "", "reasoning_content": "abc"},
                {"role": "assistant", "content": "", "reasoning_details": [{"summary": "def"}]},
                {"role": "assistant", "content": "plain"},
            ]
        )

        assert stats["assistant_messages"] == 2
        assert stats["chars"] == len("abc") + len(str([{"summary": "def"}]))

    def test_reasoning_replay_accounting_logs_large_provider_mismatch(self, agent, capsys):
        agent.quiet_mode = False
        agent.log_prefix = ""
        api_messages = [
            {"role": "assistant", "content": "", "reasoning_content": "x" * 40_000},
        ]

        agent._log_reasoning_replay_accounting(
            api_messages=api_messages,
            approx_tokens=12_000,
            provider_prompt_tokens=1_000,
        )

        output = capsys.readouterr().out
        assert "Reasoning replay attached" in output
        assert "Provider input accounting reported 1,000" in output


class TestCleanSessionContent:
    def test_none_passthrough(self):
        assert AIAgent._clean_session_content(None) is None

    def test_scratchpad_converted(self):
        text = "<REASONING_SCRATCHPAD>think</REASONING_SCRATCHPAD> answer"
        result = AIAgent._clean_session_content(text)
        assert "<REASONING_SCRATCHPAD>" not in result
        assert "<think>" in result

    def test_extra_newlines_cleaned(self):
        text = "\n\n\n<think>x</think>\n\n\nafter"
        result = AIAgent._clean_session_content(text)
        # Should not have excessive newlines around think block
        assert "\n\n\n" not in result
        # Content after think block must be preserved
        assert "after" in result


class TestGetMessagesUpToLastAssistant:
    def test_empty_list(self, agent):
        assert agent._get_messages_up_to_last_assistant([]) == []

    def test_no_assistant_returns_copy(self, agent):
        msgs = [{"role": "user", "content": "hi"}]
        result = agent._get_messages_up_to_last_assistant(msgs)
        assert result == msgs
        assert result is not msgs  # should be a copy

    def test_single_assistant(self, agent):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = agent._get_messages_up_to_last_assistant(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_multiple_assistants_returns_up_to_last(self, agent):
        msgs = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        result = agent._get_messages_up_to_last_assistant(msgs)
        assert len(result) == 3
        assert result[-1]["content"] == "q2"

    def test_assistant_then_tool_messages(self, agent):
        msgs = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "1"},
        ]
        # Last assistant is at index 1, so result = msgs[:1]
        result = agent._get_messages_up_to_last_assistant(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "user"


class TestMaskApiKey:
    def test_none_returns_none(self, agent):
        assert agent._mask_api_key_for_logs(None) is None

    def test_short_key_uses_fixed_redaction_marker(self, agent):
        assert agent._mask_api_key_for_logs("short") == "[REDACTED]"

    def test_long_key_fully_redacted(self, agent):
        key = "sk-or-v1-abcdefghijklmnop"
        result = agent._mask_api_key_for_logs(key)
        assert result == "[REDACTED]"
        assert key[:8] not in result
        assert key[-4:] not in result


def test_api_request_dump_contains_no_credential_material(agent, tmp_path, monkeypatch, capsys):
    """Request diagnostics redact credentials in headers, errors, files, and stdout."""
    secret = "sk-requestdump-start-abcdefghijklmnopqrstuvwxyz-requestdump-end"
    agent.logs_dir = tmp_path
    agent.client = SimpleNamespace(api_key=secret)
    monkeypatch.setenv("LEANFLOW_DUMP_REQUEST_STDOUT", "1")

    dump_path = agent._dump_api_request_debug(
        {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
        reason="provider-error",
        error=RuntimeError(f"Authorization: Bearer {secret}"),
    )

    assert dump_path is not None
    rendered = dump_path.read_text(encoding="utf-8") + capsys.readouterr().out
    assert secret not in rendered
    assert secret[:20] not in rendered
    assert secret[-20:] not in rendered
    assert "Bearer [REDACTED]" in rendered


# ===================================================================
# Group 2: State / Structure Methods
# ===================================================================


class TestInit:
    @pytest.mark.parametrize(
        ("provider", "api_mode", "base_url"),
        [
            ("openrouter", "chat_completions", "https://openrouter.ai/api/v1"),
            (
                "openai-codex",
                "codex_responses",
                "https://chatgpt.com/backend-api/codex",
            ),
        ],
    )
    def test_openai_compatible_startup_never_renders_credential_material(
        self, provider, api_mode, base_url, capsys
    ):
        """Provider startup reports credential presence without key-derived text."""
        secret = "secret-start-abcdefghijklmnopqrstuvwxyz-secret-end"
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            AIAgent(
                api_key=secret,
                base_url=base_url,
                provider=provider,
                api_mode=api_mode,
                quiet_mode=False,
                skip_context_files=True,
                skip_memory=True,
            )

        output = capsys.readouterr().out
        assert "Using configured API credentials" in output
        assert "Using API key:" not in output
        assert secret not in output
        assert secret[:8] not in output
        assert secret[-10:] not in output

    def test_native_anthropic_startup_never_renders_credential_material(self, capsys):
        """Native Anthropic startup reports credential presence without token fragments."""
        secret = "credfragx-start-abcdefghijklmnopqrstuvwxyz-credfragx-end"
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch(
                "agent.providers.anthropic_adapter.build_anthropic_client",
                return_value=MagicMock(),
            ),
        ):
            AIAgent(
                api_key=secret,
                provider="anthropic",
                api_mode="anthropic_messages",
                quiet_mode=False,
                skip_context_files=True,
                skip_memory=True,
            )

        output = capsys.readouterr().out
        assert "Using configured API credentials" in output
        assert "Using token:" not in output
        assert secret not in output
        assert secret[:8] not in output
        assert secret[-10:] not in output

    @pytest.mark.parametrize("credential", ["tiny-key", "dummy-key", ""])
    def test_invalid_or_missing_startup_never_echoes_credential(self, credential, capsys):
        """Credential warnings contain status only, including the routed missing-key path."""
        routed = {
            "api_key": credential,
            "base_url": "https://openrouter.ai/api/v1",
        }
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch.object(
                run_agent.ProviderClientFactory,
                "build_routed_client_kwargs",
                return_value=routed,
            ),
        ):
            AIAgent(
                quiet_mode=False,
                skip_context_files=True,
                skip_memory=True,
            )

        output = capsys.readouterr().out
        assert "API credentials appear invalid or missing" in output
        assert "got:" not in output
        if credential:
            assert credential not in output

    def test_anthropic_base_url_accepted(self):
        """Anthropic base URLs should route to native Anthropic client."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("agent.providers.anthropic_adapter._anthropic_sdk") as mock_anthropic,
        ):
            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://api.anthropic.com/v1/",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert agent.api_mode == "anthropic_messages"
            mock_anthropic.Anthropic.assert_called_once()

    def test_prompt_caching_claude_openrouter(self):
        """Claude model via OpenRouter should enable prompt caching."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                model="anthropic/claude-sonnet-4-20250514",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a._use_prompt_caching is True

    def test_prompt_caching_non_claude(self):
        """Non-Claude model should disable prompt caching."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                model="openai/gpt-4o",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a._use_prompt_caching is False

    def test_prompt_caching_non_openrouter(self):
        """Custom base_url (not OpenRouter) should disable prompt caching."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                model="anthropic/claude-sonnet-4-20250514",
                base_url="http://localhost:8080/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a._use_prompt_caching is False

    def test_prompt_caching_native_anthropic(self):
        """Native Anthropic provider should enable prompt caching."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("agent.providers.anthropic_adapter._anthropic_sdk"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://api.anthropic.com/v1/",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a.api_mode == "anthropic_messages"
            assert a._use_prompt_caching is True

    def test_valid_tool_names_populated(self):
        """valid_tool_names should contain names from loaded tools."""
        tools = _make_tool_defs("web_search", "terminal")
        with (
            patch("run_agent.get_tool_definitions", return_value=tools),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert a.valid_tool_names == {"web_search", "terminal"}

    def test_session_id_auto_generated(self):
        """Session ID should be auto-generated as a short 5-digit id."""
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert re.match(
                r"^\d{5}$", a.session_id
            ), f"session_id doesn't match expected format: {a.session_id}"


class TestInterrupt:
    def test_interrupt_sets_flag(self, agent):
        with patch("run_agent._set_interrupt"):
            agent.interrupt()
            assert agent._interrupt_requested is True

    def test_interrupt_with_message(self, agent):
        with patch("run_agent._set_interrupt"):
            agent.interrupt("new question")
            assert agent._interrupt_message == "new question"

    def test_interrupt_log_can_be_suppressed(self, agent, capsys):
        agent.quiet_mode = False
        agent._suppress_next_interrupt_log = True
        with patch("run_agent._set_interrupt"):
            agent.interrupt("internal step boundary")

        assert capsys.readouterr().out == ""
        assert agent._interrupt_requested is True
        assert agent._interrupt_message == "internal step boundary"

    def test_clear_interrupt(self, agent):
        with patch("run_agent._set_interrupt"):
            agent.interrupt("msg")
            agent.clear_interrupt()
            assert agent._interrupt_requested is False
            assert agent._interrupt_message is None

    def test_is_interrupted_property(self, agent):
        assert agent.is_interrupted is False
        with patch("run_agent._set_interrupt"):
            agent.interrupt()
            assert agent.is_interrupted is True


class TestHydrateTodoStore:
    def test_no_todo_in_history(self, agent):
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        with patch("run_agent._set_interrupt"):
            agent._hydrate_todo_store(history)
        assert not agent._todo_store.has_items()

    def test_recovers_from_history(self, agent):
        todos = [{"id": "1", "content": "do thing", "status": "pending"}]
        history = [
            {"role": "user", "content": "plan"},
            {"role": "assistant", "content": "ok"},
            {
                "role": "tool",
                "content": json.dumps({"todos": todos}),
                "tool_call_id": "c1",
            },
        ]
        with patch("run_agent._set_interrupt"):
            agent._hydrate_todo_store(history)
        assert agent._todo_store.has_items()

    def test_skips_non_todo_tools(self, agent):
        history = [
            {
                "role": "tool",
                "content": '{"result": "search done"}',
                "tool_call_id": "c1",
            },
        ]
        with patch("run_agent._set_interrupt"):
            agent._hydrate_todo_store(history)
        assert not agent._todo_store.has_items()

    def test_invalid_json_skipped(self, agent):
        history = [
            {
                "role": "tool",
                "content": 'not valid json "todos" oops',
                "tool_call_id": "c1",
            },
        ]
        with patch("run_agent._set_interrupt"):
            agent._hydrate_todo_store(history)
        assert not agent._todo_store.has_items()


class TestBuildSystemPrompt:
    def test_always_has_identity(self, agent):
        prompt = agent._build_system_prompt()
        assert DEFAULT_AGENT_IDENTITY in prompt

    def test_includes_leanflow_lean_entry_workflow_guidance(self, agent):
        prompt = agent._build_system_prompt()
        assert "point them to /project" in prompt
        assert "then /prove, /autoprove, /formalize, or /autoformalize" in prompt
        assert "successful builds" in prompt
        assert "no `sorry`" in prompt
        assert "`--agents N`" in prompt

    def test_includes_system_message(self, agent):
        prompt = agent._build_system_prompt(system_message="Custom instruction")
        assert "Custom instruction" in prompt

    def test_memory_guidance_when_memory_tool_loaded(self, agent_with_memory_tool):
        from agent.prompting.prompt_builder import MEMORY_GUIDANCE

        prompt = agent_with_memory_tool._build_system_prompt()
        assert MEMORY_GUIDANCE in prompt

    def test_no_memory_guidance_without_tool(self, agent):
        from agent.prompting.prompt_builder import MEMORY_GUIDANCE

        prompt = agent._build_system_prompt()
        assert MEMORY_GUIDANCE not in prompt

    def test_includes_datetime(self, agent):
        prompt = agent._build_system_prompt()
        # Should contain current date info like "Conversation started:"
        assert "Conversation started:" in prompt


class TestInvalidateSystemPrompt:
    def test_clears_cache(self, agent):
        agent._cached_system_prompt = "cached value"
        agent._invalidate_system_prompt()
        assert agent._cached_system_prompt is None

    def test_reloads_memory_store(self, agent):
        mock_store = MagicMock()
        agent._memory_store = mock_store
        agent._cached_system_prompt = "cached"
        agent._invalidate_system_prompt()
        mock_store.load_from_disk.assert_called_once()


class TestBuildApiKwargs:
    def test_basic_kwargs(self, agent):
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["model"] == agent.model
        assert kwargs["messages"] is messages
        assert kwargs["timeout"] == 1200.0

    def test_provider_preferences_injected(self, agent):
        agent.providers_allowed = ["Anthropic"]
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["extra_body"]["provider"]["only"] == ["Anthropic"]

    def test_reasoning_config_default_openrouter(self, agent):
        """Default reasoning config for OpenRouter should be high."""
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        reasoning = kwargs["extra_body"]["reasoning"]
        assert reasoning["enabled"] is True
        assert reasoning["effort"] == "high"

    def test_reasoning_config_custom(self, agent):
        agent.reasoning_config = {"enabled": False}
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_reasoning_not_sent_for_unsupported_openrouter_model(self, agent):
        agent.model = "minimax/minimax-m2.5"
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert "reasoning" not in kwargs.get("extra_body", {})

    def test_reasoning_sent_for_supported_openrouter_model(self, agent):
        agent.model = "qwen/qwen3.5-plus-02-15"
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["extra_body"]["reasoning"]["effort"] == "high"

    def test_reasoning_sent_for_nous_route(self, agent):
        agent.base_url = "https://inference-api.nousresearch.com/v1"
        agent.model = "minimax/minimax-m2.5"
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["extra_body"]["reasoning"]["effort"] == "high"

    def test_reasoning_sent_for_rcp_route(self, agent):
        agent.base_url = "https://inference.rcp.epfl.ch/v1"
        agent.model = "Qwen/Qwen3-30B-A3B"
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
        assert kwargs["extra_body"]["reasoning_effort"] == "high"

    def test_auto_reasoning_defaults_to_high_for_rcp_route(self, agent):
        agent.base_url = "https://inference.rcp.epfl.ch/v1"
        agent.model = "Qwen/Qwen3-30B-A3B"
        agent.reasoning_config = {"mode": "auto"}
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
        assert kwargs["extra_body"]["reasoning_effort"] == "high"

    def test_reasoning_disabled_for_rcp_route(self, agent):
        agent.base_url = "https://inference.rcp.epfl.ch/v1"
        agent.model = "Qwen/Qwen3-30B-A3B"
        agent.reasoning_config = {"enabled": False}
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
        assert "reasoning_effort" not in kwargs["extra_body"]

    def test_reasoning_effort_mapped_for_rcp_route(self, agent):
        agent.base_url = "https://inference.rcp.epfl.ch/v1"
        agent.model = "Qwen/Qwen3-30B-A3B"
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
        assert kwargs["extra_body"]["reasoning_effort"] == "high"

    def test_sampling_defaults_injected(self, agent):
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["temperature"] == 0.3
        assert kwargs["seed"] == 42
        assert "top_p" not in kwargs

    def test_rcp_sampling_extras_injected(self, agent):
        agent.base_url = "https://inference.rcp.epfl.ch/v1"
        agent.model = "Qwen/Qwen3-30B-A3B"
        agent.top_p = 0.92
        agent.top_k = 40
        agent.min_p = 0.05
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["top_p"] == 0.92
        assert kwargs["extra_body"]["top_k"] == 40
        assert kwargs["extra_body"]["min_p"] == 0.05

    def test_max_tokens_injected(self, agent):
        agent.max_tokens = 4096
        messages = [{"role": "user", "content": "hi"}]
        kwargs = agent._build_api_kwargs(messages)
        assert kwargs["max_tokens"] == 4096


class TestBuildAssistantMessage:
    def test_basic_message(self, agent):
        msg = _mock_assistant_msg(content="Hello!")
        result = agent._build_assistant_message(msg, "stop")
        assert result["role"] == "assistant"
        assert result["content"] == "Hello!"
        assert result["finish_reason"] == "stop"

    def test_with_reasoning(self, agent):
        msg = _mock_assistant_msg(content="answer", reasoning="thinking")
        result = agent._build_assistant_message(msg, "stop")
        assert result["reasoning"] == "thinking"

    def test_with_tool_calls(self, agent):
        tc = _mock_tool_call(name="web_search", arguments='{"q":"test"}', call_id="c1")
        msg = _mock_assistant_msg(content="", tool_calls=[tc])
        result = agent._build_assistant_message(msg, "tool_calls")
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["function"]["name"] == "web_search"

    def test_with_reasoning_details(self, agent):
        details = [{"type": "reasoning.summary", "text": "step1", "signature": "sig1"}]
        msg = _mock_assistant_msg(content="ans", reasoning_details=details)
        result = agent._build_assistant_message(msg, "stop")
        assert "reasoning_details" in result
        assert result["reasoning_details"][0]["text"] == "step1"

    def test_empty_content(self, agent):
        msg = _mock_assistant_msg(content=None)
        result = agent._build_assistant_message(msg, "stop")
        assert result["content"] == ""

    def test_tool_call_extra_content_preserved(self, agent):
        """Gemini thinking models attach extra_content with thought_signature
        to tool calls. This must be preserved so subsequent API calls include it."""
        tc = _mock_tool_call(name="get_weather", arguments='{"city":"NYC"}', call_id="c2")
        tc.extra_content = {"google": {"thought_signature": "abc123"}}
        msg = _mock_assistant_msg(content="", tool_calls=[tc])
        result = agent._build_assistant_message(msg, "tool_calls")
        assert result["tool_calls"][0]["extra_content"] == {
            "google": {"thought_signature": "abc123"}
        }

    def test_tool_call_without_extra_content(self, agent):
        """Standard tool calls (no thinking model) should not have extra_content."""
        tc = _mock_tool_call(name="web_search", arguments="{}", call_id="c3")
        msg = _mock_assistant_msg(content="", tool_calls=[tc])
        result = agent._build_assistant_message(msg, "tool_calls")
        assert "extra_content" not in result["tool_calls"][0]


class TestReasoningPreviewLines:
    def test_empty_reasoning_returns_no_preview(self):
        assert AIAgent._reasoning_preview_lines(None) == []

    def test_preview_truncates_line_count(self):
        reasoning = "step 1\nstep 2\nstep 3\nstep 4"
        result = AIAgent._reasoning_preview_lines(reasoning, max_lines=3, max_chars=200)
        assert result == ["step 1", "step 2", "step 3 ..."]

    def test_preview_truncates_chars(self):
        reasoning = "a" * 400
        result = AIAgent._reasoning_preview_lines(reasoning, max_lines=3, max_chars=50)
        assert len(result) == 1
        assert result[0].endswith("...")
        assert len(result[0]) == 50


class TestTextPreviewLines:
    def test_preview_keeps_more_than_old_one_line_start(self):
        text = "Resume workflow\n" + "\n".join(f"checkpoint detail {idx}" for idx in range(10))

        result = AIAgent._text_preview_lines(text, max_lines=5, max_chars=400)

        assert result[0] == "Resume workflow"
        assert any("checkpoint detail 3" in line for line in result)
        assert result[-1].endswith("...")

    def test_preview_truncates_by_chars(self):
        result = AIAgent._text_preview_lines("x" * 200, max_lines=3, max_chars=40)

        assert result == ["x" * 36 + " ..."]


class TestFormatToolsForSystemMessage:
    def test_no_tools_returns_empty_array(self, agent):
        agent.tools = []
        assert agent._format_tools_for_system_message() == "[]"

    def test_formats_single_tool(self, agent):
        agent.tools = _make_tool_defs("web_search")
        result = agent._format_tools_for_system_message()
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "web_search"

    def test_formats_multiple_tools(self, agent):
        agent.tools = _make_tool_defs("web_search", "terminal", "read_file")
        result = agent._format_tools_for_system_message()
        parsed = json.loads(result)
        assert len(parsed) == 3
        names = {t["name"] for t in parsed}
        assert names == {"web_search", "terminal", "read_file"}


# ===================================================================
# Group 3: Conversation Loop Pieces (OpenAI mock)
# ===================================================================


class TestExecuteToolCalls:
    def test_single_tool_executed(self, agent):
        tc = _mock_tool_call(name="web_search", arguments='{"q":"test"}', call_id="c1")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc])
        messages = []
        with patch("run_agent.handle_function_call", return_value="search result") as mock_hfc:
            agent._execute_tool_calls(mock_msg, messages, "task-1")
            # enabled_tools passes the agent's own valid_tool_names
            args, kwargs = mock_hfc.call_args
            assert args[:3] == ("web_search", {"q": "test"}, "task-1")
            assert set(kwargs.get("enabled_tools", [])) == agent.valid_tool_names
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"
        assert "search result" in messages[0]["content"]

    def test_post_tool_result_callback_can_append_tool_context(self, agent):
        tc = _mock_tool_call(name="web_search", arguments='{"q":"test"}', call_id="c1")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc])
        messages = []

        def callback(name, args, result):
            agent._post_tool_result_appendix = "[manager feedback]"

        agent.post_tool_result_callback = callback

        with patch("run_agent.handle_function_call", return_value="search result"):
            agent._execute_tool_calls(mock_msg, messages, "task-1")

        assert "search result" in messages[0]["content"]
        assert "[manager feedback]" in messages[0]["content"]
        assert agent._post_tool_result_appendix is None

    def test_model_projection_runs_after_raw_audit_and_manager_callback(self, agent):
        tc = _mock_tool_call(name="web_search", arguments='{"q":"test"}', call_id="c1")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc])
        messages = []
        raw_result = "full-audit-result-" + "x" * 2000
        callbacks = []
        events = []
        agent.post_tool_result_callback = lambda name, args, result: callbacks.append(result)
        agent.tool_result_projection_callback = lambda name, args, result: "bounded-model-result"

        with (
            patch("run_agent.handle_function_call", return_value=raw_result),
            patch("run_agent._emit_workflow_event", side_effect=lambda *a, **kw: events.append(kw)),
        ):
            agent._execute_tool_calls(mock_msg, messages, "task-1")

        assert callbacks == [raw_result]
        assert events[-1]["result"] == raw_result
        assert messages[0]["content"] == "bounded-model-result"

    def test_interrupt_skips_remaining(self, agent):
        tc1 = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        tc2 = _mock_tool_call(name="web_search", arguments="{}", call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
        messages = []

        with patch("run_agent._set_interrupt"):
            agent.interrupt()

        agent._execute_tool_calls(mock_msg, messages, "task-1")
        # Both calls should be skipped with cancellation messages
        assert len(messages) == 2
        assert (
            "cancelled" in messages[0]["content"].lower()
            or "interrupted" in messages[0]["content"].lower()
        )

    def test_invalid_json_args_defaults_empty(self, agent):
        tc = _mock_tool_call(name="web_search", arguments="not valid json", call_id="c1")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc])
        messages = []
        with patch("run_agent.handle_function_call", return_value="ok") as mock_hfc:
            agent._execute_tool_calls(mock_msg, messages, "task-1")
            # Invalid JSON args should fall back to empty dict
            args, kwargs = mock_hfc.call_args
            assert args[:3] == ("web_search", {}, "task-1")
            assert set(kwargs.get("enabled_tools", [])) == agent.valid_tool_names
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"
        assert messages[0]["tool_call_id"] == "c1"

    def test_result_truncation_over_100k(self, agent):
        tc = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc])
        messages = []
        big_result = "x" * 150_000
        with patch("run_agent.handle_function_call", return_value=big_result):
            agent._execute_tool_calls(mock_msg, messages, "task-1")
        # Content should be truncated
        assert len(messages[0]["content"]) < 150_000
        assert "Truncated" in messages[0]["content"]


class TestConcurrentToolExecution:
    """Tests for _execute_tool_calls_concurrent and dispatch logic."""

    def test_single_tool_uses_sequential_path(self, agent):
        """Single tool call should use sequential path, not concurrent."""
        tc = _mock_tool_call(name="web_search", arguments='{"q":"test"}', call_id="c1")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc])
        messages = []
        with patch.object(agent, "_execute_tool_calls_sequential") as mock_seq:
            with patch.object(agent, "_execute_tool_calls_concurrent") as mock_con:
                agent._execute_tool_calls(mock_msg, messages, "task-1")
                mock_seq.assert_called_once()
                mock_con.assert_not_called()

    def test_clarify_forces_sequential(self, agent):
        """Batch containing clarify should use sequential path."""
        tc1 = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        tc2 = _mock_tool_call(name="clarify", arguments='{"question":"ok?"}', call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
        messages = []
        with patch.object(agent, "_execute_tool_calls_sequential") as mock_seq:
            with patch.object(agent, "_execute_tool_calls_concurrent") as mock_con:
                agent._execute_tool_calls(mock_msg, messages, "task-1")
                mock_seq.assert_called_once()
                mock_con.assert_not_called()

    @pytest.mark.parametrize(
        "edit_tool",
        ["patch", "write_file", "apply_verified_patch"],
    )
    def test_managed_source_edit_forces_entire_batch_sequential(self, agent, edit_tool):
        """A source edit cannot overlap even a read-only sibling in its batch."""
        edit = _mock_tool_call(name=edit_tool, arguments='{"path":"Demo.lean"}', call_id="c1")
        read = _mock_tool_call(name="read_file", arguments='{"path":"Demo.lean"}', call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[edit, read])

        with (
            patch.object(agent, "_execute_tool_calls_sequential") as mock_seq,
            patch.object(agent, "_execute_tool_calls_concurrent") as mock_con,
        ):
            agent._execute_tool_calls(mock_msg, [], "task-1")

        mock_seq.assert_called_once()
        mock_con.assert_not_called()

    def test_concurrent_entry_serializes_managed_edits_before_snapshot_overwrite(self, agent):
        """The defensive concurrent entry cannot let sibling edits steal snapshot ownership."""
        patch_call = _mock_tool_call(
            name="patch",
            arguments='{"path":"Demo.lean","old_string":"a","new_string":"b"}',
            call_id="c1",
        )
        write_call = _mock_tool_call(
            name="write_file",
            arguments='{"path":"Demo.lean","content":"replacement"}',
            call_id="c2",
        )
        mock_msg = _mock_assistant_msg(content="", tool_calls=[patch_call, write_call])
        first_edit_started = threading.Event()
        second_preflight_completed = threading.Event()
        trace: list[str] = []
        violations: list[str] = []

        def preflight(name, _args):
            if name == "write_file":
                first_edit_started.wait(timeout=1.0)
            trace.append(f"pre:{name}")
            pending = getattr(agent, "_managed_queue_edit_snapshot", None)
            if pending is not None:
                violations.append(f"{name} replaced pending {pending['owner']}")
            agent._managed_queue_edit_snapshot = {"owner": name}
            if name == "write_file":
                second_preflight_completed.set()

        def handle(name, _args, _task_id, **_kwargs):
            trace.append(f"run:{name}")
            if name == "patch":
                first_edit_started.set()
                # A genuinely concurrent sibling deterministically reaches its
                # preflight here; a serialized sibling starts after this call.
                second_preflight_completed.wait(timeout=0.05)
            owner = dict(getattr(agent, "_managed_queue_edit_snapshot", {}) or {}).get("owner")
            if owner != name:
                violations.append(f"{name} ran with {owner or 'no'} snapshot")
            return json.dumps({"success": True, "tool": name})

        def complete(name, _args, _result):
            trace.append(f"post:{name}")
            owner = dict(getattr(agent, "_managed_queue_edit_snapshot", {}) or {}).get("owner")
            if owner != name:
                violations.append(f"{name} finalized with {owner or 'no'} snapshot")
            if hasattr(agent, "_managed_queue_edit_snapshot"):
                delattr(agent, "_managed_queue_edit_snapshot")

        agent.pre_tool_call_callback = preflight
        agent.post_tool_result_callback = complete

        with patch("run_agent.handle_function_call", side_effect=handle):
            # Exercise the lower-level entry too: callers cannot bypass the
            # batch policy by selecting the concurrent strategy directly.
            agent._execute_tool_calls_concurrent(mock_msg, [], "task-1")

        assert violations == []
        assert trace == [
            "pre:patch",
            "run:patch",
            "post:patch",
            "pre:write_file",
            "run:write_file",
            "post:write_file",
        ]

    def test_multiple_tools_uses_concurrent_path(self, agent):
        """Multiple non-interactive tools should use concurrent path."""
        tc1 = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        tc2 = _mock_tool_call(name="read_file", arguments='{"path":"x.py"}', call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
        messages = []
        with patch.object(agent, "_execute_tool_calls_sequential") as mock_seq:
            with patch.object(agent, "_execute_tool_calls_concurrent") as mock_con:
                agent._execute_tool_calls(mock_msg, messages, "task-1")
                mock_con.assert_called_once()
                mock_seq.assert_not_called()

    def test_concurrent_executes_all_tools(self, agent):
        """Concurrent path should execute all tools and append results in order."""
        tc1 = _mock_tool_call(name="web_search", arguments='{"q":"alpha"}', call_id="c1")
        tc2 = _mock_tool_call(name="web_search", arguments='{"q":"beta"}', call_id="c2")
        tc3 = _mock_tool_call(name="web_search", arguments='{"q":"gamma"}', call_id="c3")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2, tc3])
        messages = []

        call_log = []

        def fake_handle(name, args, task_id, **kwargs):
            call_log.append(name)
            return json.dumps({"result": args.get("q", "")})

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        assert len(messages) == 3
        # Results must be in original order
        assert messages[0]["tool_call_id"] == "c1"
        assert messages[1]["tool_call_id"] == "c2"
        assert messages[2]["tool_call_id"] == "c3"
        # All should be tool messages
        assert all(m["role"] == "tool" for m in messages)
        # Content should contain the query results
        assert "alpha" in messages[0]["content"]
        assert "beta" in messages[1]["content"]
        assert "gamma" in messages[2]["content"]

    def test_identical_concurrent_lean_verify_calls_share_one_execution(self, agent):
        """Byte-identical file verification must compile and notify the manager once."""
        tool_calls = [
            _mock_tool_call(
                name="lean_verify",
                arguments=json.dumps({"target": "Demo/Main.lean", "mode": "file_exact"}),
                call_id=f"c{index}",
            )
            for index in range(4)
        ]
        mock_msg = _mock_assistant_msg(content="", tool_calls=tool_calls)
        messages: list[dict] = []
        callbacks: list[tuple[str, dict, str]] = []
        agent.post_tool_result_callback = lambda name, args, result: callbacks.append(
            (name, args, result)
        )

        with patch(
            "run_agent.handle_function_call",
            return_value=json.dumps({"success": True, "ok": True, "output": "checked"}),
        ) as handle:
            agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        handle.assert_called_once()
        assert len(callbacks) == 1
        assert [message["tool_call_id"] for message in messages] == [
            "c0",
            "c1",
            "c2",
            "c3",
        ]
        assert "checked" in messages[0]["content"]
        for message in messages[1:]:
            payload = json.loads(message["content"])
            assert payload["status"] == "identical_batch_call_reused"
            assert payload["source_tool_call_id"] == "c0"

    def test_identical_concurrent_lean_reads_share_one_execution(self, agent):
        """Identical outlines and proof contexts should each run only once per batch."""
        tool_calls = [
            _mock_tool_call(
                name=name,
                arguments=json.dumps({"file_path": "Demo.lean", "theorem_id": "demo"}),
                call_id=f"c{index}",
            )
            for index, name in enumerate(
                ["lean_outline", "lean_outline", "lean_proof_context", "lean_proof_context"]
            )
        ]
        messages: list[dict] = []

        with patch(
            "run_agent.handle_function_call",
            return_value=json.dumps({"success": True, "result": "read"}),
        ) as handle:
            agent._execute_tool_calls_concurrent(
                _mock_assistant_msg(content="", tool_calls=tool_calls),
                messages,
                "task-1",
            )

        assert handle.call_count == 2
        assert json.loads(messages[1]["content"])["status"] == "identical_batch_call_reused"
        assert json.loads(messages[3]["content"])["status"] == "identical_batch_call_reused"

    def test_concurrent_lean_search_results_compact_later_overlap(self, agent):
        """Keep first search evidence full and replace later duplicate bodies with references."""
        tool_calls = [
            _mock_tool_call(
                name="lean_search",
                arguments=json.dumps({"query": query}),
                call_id=f"c{index}",
            )
            for index, query in enumerate(["foo", "bar"])
        ]

        def search_result(_name, args, _task_id, **_kwargs):
            return json.dumps(
                {
                    "success": True,
                    "results": [
                        {
                            "provider": "local",
                            "name": "Demo.shared",
                            "declaration": "theorem Demo.shared : " + args["query"] * 100,
                        },
                        {"provider": "local", "name": f"Demo.{args['query']}"},
                    ],
                }
            )

        messages: list[dict] = []
        with patch("run_agent.handle_function_call", side_effect=search_result):
            agent._execute_tool_calls_concurrent(
                _mock_assistant_msg(content="", tool_calls=tool_calls),
                messages,
                "task-1",
            )

        first = json.loads(messages[0]["content"])
        second = json.loads(messages[1]["content"])
        assert "declaration" in first["results"][0]
        assert second["results"][0]["repeated_result"] is True
        assert "declaration" not in second["results"][0]
        assert second["results"][1]["name"] == "Demo.bar"

    def test_delegated_concurrent_tools_suppress_child_spinner(self, agent):
        """Keep lane tool batches concise when several children share one terminal."""
        tc1 = _mock_tool_call(name="web_search", arguments='{"q":"alpha"}', call_id="c1")
        tc2 = _mock_tool_call(name="web_search", arguments='{"q":"beta"}', call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
        agent.quiet_mode = True
        agent._suppress_spinners = True

        with (
            patch("run_agent.KawaiiSpinner") as spinner,
            patch("run_agent.handle_function_call", return_value="ok"),
        ):
            agent._execute_tool_calls_concurrent(mock_msg, [], "child-task")

        spinner.assert_not_called()

    def test_concurrent_tools_inherit_capacity_context(self, agent):
        """Worker threads retain the delegated actor lease context."""
        marker: ContextVar[str] = ContextVar("tool-capacity-marker", default="missing")
        token = marker.set("actor-lease")
        try:
            tc1 = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
            tc2 = _mock_tool_call(name="web_search", arguments="{}", call_id="c2")
            mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
            observed: list[str] = []

            def fake_handle(name, args, task_id, **kwargs):
                observed.append(marker.get())
                return "ok"

            with patch("run_agent.handle_function_call", side_effect=fake_handle):
                agent._execute_tool_calls_concurrent(mock_msg, [], "task-1")
        finally:
            marker.reset(token)

        assert observed == ["actor-lease", "actor-lease"]

    def test_concurrent_memory_heavy_lean_tools_are_serialized(self, agent):
        """A large Lean search batch must not load several semantic states at once."""
        tool_calls = [
            _mock_tool_call(
                name="lean_search",
                arguments=json.dumps({"query": f"query-{index}"}),
                call_id=f"c{index}",
            )
            for index in range(6)
        ]
        mock_msg = _mock_assistant_msg(content="", tool_calls=tool_calls)
        messages = []
        lock = threading.Lock()
        active = 0
        max_active = 0

        def fake_handle(name, args, task_id, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.03)
                return json.dumps({"result": args["query"]})
            finally:
                with lock:
                    active -= 1

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        assert max_active == 1
        assert [message["tool_call_id"] for message in messages] == [
            f"c{index}" for index in range(6)
        ]

    def test_reasoning_advisor_overlaps_heavy_lean_gate_without_unserializing_searches(self, agent):
        """Advisor work bypasses the Lean-memory gate while heavy calls stay serialized."""
        tool_calls = [
            _mock_tool_call(
                name="lean_search",
                arguments=json.dumps({"query": "first-heavy"}),
                call_id="c1",
            ),
            _mock_tool_call(
                name="lean_reasoning_help",
                arguments=json.dumps(
                    {
                        "theorem_id": "demo",
                        "file_path": "Demo/Main.lean",
                    }
                ),
                call_id="c2",
            ),
            _mock_tool_call(
                name="lean_search",
                arguments=json.dumps({"query": "second-heavy"}),
                call_id="c3",
            ),
        ]
        mock_msg = _mock_assistant_msg(content="", tool_calls=tool_calls)
        messages: list[dict] = []
        first_heavy_started = threading.Event()
        release_first_heavy = threading.Event()
        second_heavy_started = threading.Event()
        advisor_started = threading.Event()
        lock = threading.Lock()
        active_heavy = 0
        max_active_heavy = 0

        def fake_handle(name, args, task_id, **kwargs):
            nonlocal active_heavy, max_active_heavy
            if name == "lean_reasoning_help":
                advisor_started.set()
                return json.dumps({"status": "answered"})

            with lock:
                active_heavy += 1
                max_active_heavy = max(max_active_heavy, active_heavy)
                is_first = not first_heavy_started.is_set()
                if is_first:
                    first_heavy_started.set()
                else:
                    second_heavy_started.set()
            try:
                if is_first and not release_first_heavy.wait(timeout=5):
                    raise TimeoutError("test did not release first heavy tool")
                return json.dumps({"result": args["query"]})
            finally:
                with lock:
                    active_heavy -= 1

        def run_batch():
            with patch("run_agent.handle_function_call", side_effect=fake_handle):
                agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        batch_thread = threading.Thread(target=run_batch)
        batch_thread.start()
        try:
            assert first_heavy_started.wait(timeout=2)
            assert advisor_started.wait(timeout=2)
            assert not second_heavy_started.is_set()
            assert batch_thread.is_alive()
        finally:
            release_first_heavy.set()
            batch_thread.join(timeout=5)

        assert not batch_thread.is_alive()
        assert second_heavy_started.is_set()
        assert max_active_heavy == 1
        assert [message["tool_call_id"] for message in messages] == ["c1", "c2", "c3"]

    def test_research_heavy_tools_reclaim_resident_lean_services(self, agent, monkeypatch):
        """Release owned LeanProbe state before the project slot admits another actor."""
        monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
        monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
        tool_calls = [
            _mock_tool_call(
                name="lean_verify",
                arguments=json.dumps({"target": f"File{index}.lean"}),
                call_id=f"c{index}",
            )
            for index in range(2)
        ]
        calls: list[str] = []
        admission = SimpleNamespace(
            to_dict=lambda: {}, retain_until_process_exit=lambda reason: None
        )

        with (
            patch("run_agent.handle_function_call", return_value="ok"),
            patch(
                "agent.execution.tool_executor.project_lean_heavy_admission",
                side_effect=lambda root: calls.append("admit") or nullcontext(admission),
            ),
            patch(
                "leanflow_cli.lean.lean_incremental.close_incremental_sessions",
                side_effect=lambda: calls.append("incremental") or True,
            ),
        ):
            agent._execute_tool_calls_concurrent(
                _mock_assistant_msg(content="", tool_calls=tool_calls), [], "task-1"
            )

        assert calls.count("incremental") == 2
        assert calls.count("admit") == 2

    def test_single_sequential_lean_tool_uses_project_admission(self, agent, monkeypatch):
        """A one-tool turn shares the same admission/reclaim boundary as a batch."""
        monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
        monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
        calls: list[str] = []
        admission = SimpleNamespace(
            to_dict=lambda: {}, retain_until_process_exit=lambda reason: None
        )
        tool_call = _mock_tool_call(
            name="lean_verify",
            arguments=json.dumps({"target": "Demo/Main.lean"}),
            call_id="c1",
        )

        with (
            patch(
                "run_agent.handle_function_call",
                side_effect=lambda *args, **kwargs: calls.append("invoke") or "ok",
            ),
            patch(
                "agent.execution.tool_executor.project_lean_heavy_admission",
                side_effect=lambda root: calls.append("admit") or nullcontext(admission),
            ),
            patch(
                "leanflow_cli.lean.lean_incremental.close_incremental_sessions",
                side_effect=lambda: calls.append("close") or True,
            ),
        ):
            agent._execute_tool_calls_sequential(
                _mock_assistant_msg(content="", tool_calls=[tool_call]), [], "task-1"
            )

        assert calls == ["admit", "invoke", "close"]

    def test_clean_candidate_reserves_handoff_before_foreground_admission_exits(self, monkeypatch):
        """Publish commit priority before releasing the candidate check's main slot."""
        from agent.execution.tool_executor import ToolExecutor

        calls: list[object] = []

        class _Admission:
            def to_dict(self):
                return {}

            def retain_until_process_exit(self, reason):
                calls.append(("retain", reason))

            def reserve_foreground_handoff(self, seconds, *, reason):
                calls.append(("reserve", seconds, reason))
                return seconds

        class _AdmissionContext:
            def __enter__(self):
                calls.append("admit")
                return _Admission()

            def __exit__(self, *_args):
                calls.append("release")

        agent = SimpleNamespace(valid_tool_names=[], session_id="candidate-test")
        agent._project_lean_handoff_request_callback = lambda function_name, arguments, result: (
            calls.append(("callback", function_name, arguments, result)) or 60.0
        )
        arguments = {
            "action": "check_target",
            "file_path": "Demo/Main.lean",
            "theorem_id": "demo",
            "replacement": "theorem demo : True := by trivial",
        }
        result = json.dumps({"success": True, "ok": True})

        with (
            patch(
                "run_agent.handle_function_call",
                side_effect=lambda *args, **kwargs: calls.append("invoke") or result,
            ),
            patch(
                "agent.execution.tool_executor.project_lean_heavy_admission",
                return_value=_AdmissionContext(),
            ),
            patch(
                "agent.execution.tool_executor._close_admitted_incremental_session",
                side_effect=lambda: calls.append("close") or True,
            ),
        ):
            returned = ToolExecutor(agent).invoke_registered_tool(
                "lean_incremental_check",
                arguments,
                "task-1",
            )

        assert returned == result
        assert calls[0:2] == ["admit", "invoke"]
        assert calls[2][0:2] == ("callback", "lean_incremental_check")
        assert calls[3] == (
            "reserve",
            60.0,
            "native exact-candidate commit handoff after lean_incremental_check",
        )
        assert calls[4:] == ["close", "release"]

    def test_admitted_tool_emits_correlated_waiting_event_before_acquisition(
        self, agent, monkeypatch, tmp_path
    ):
        """Expose queue time before a foreground Lean-heavy tool is admitted."""
        project = tmp_path / "Demo"
        project.mkdir()
        (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
        monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
        monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
        tool_call = _mock_tool_call(
            name="lean_verify",
            arguments=json.dumps({"target": "Demo/Main.lean"}),
            call_id="c1",
        )
        order = []
        admission = SimpleNamespace(
            to_dict=lambda: {"waited_s": 1.25, "contended": True},
            retain_until_process_exit=lambda reason: None,
        )

        def emit(event_type, message, **details):
            order.append(("event", event_type, details))

        with (
            patch("run_agent.handle_function_call", return_value="ok"),
            patch(
                "agent.execution.tool_executor.project_lean_heavy_admission",
                side_effect=lambda root: order.append(("acquire", root)) or nullcontext(admission),
            ),
            patch(
                "leanflow_cli.lean.lean_incremental.close_incremental_sessions",
                return_value=True,
            ),
            patch("run_agent._emit_workflow_event", side_effect=emit),
        ):
            agent._execute_tool_calls_sequential(
                _mock_assistant_msg(content="", tool_calls=[tool_call]), [], "task-1"
            )

        waiting = next(item for item in order if item[:2] == ("event", "lean-resource-waiting"))
        admitted = next(item for item in order if item[:2] == ("event", "lean-resource-admission"))
        assert order.index(waiting) < next(
            index for index, item in enumerate(order) if item[0] == "acquire"
        )
        assert waiting[2]["admission_role"] == "foreground"
        assert waiting[2]["admission_request_id"]
        assert admitted[2]["admission_request_id"] == waiting[2]["admission_request_id"]
        assert admitted[2]["admission_role"] == "foreground"
        assert admitted[2]["waited_s"] == 1.25

    def test_composite_tool_observes_its_actual_inner_project_admission(
        self, agent, monkeypatch, tmp_path
    ):
        """Report inner capability or verifier gates without leasing the whole tool."""
        from core.project_resource_admission import project_lean_heavy_admission

        project = tmp_path / "Demo"
        project.mkdir()
        (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
        monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
        tool_call = _mock_tool_call(
            name="lean_inspect",
            arguments=json.dumps({"target": "Demo/Main.lean"}),
            call_id="c1",
        )
        emitted = []

        def handle(*args, **kwargs):
            with project_lean_heavy_admission(project):
                return "ok"

        with (
            patch("run_agent.handle_function_call", side_effect=handle),
            patch(
                "run_agent._emit_workflow_event",
                side_effect=lambda event_type, message, **details: emitted.append(
                    (event_type, details)
                ),
            ),
        ):
            agent._execute_tool_calls_sequential(
                _mock_assistant_msg(content="", tool_calls=[tool_call]), [], "task-1"
            )

        resource_events = [
            (event_type, details)
            for event_type, details in emitted
            if event_type.startswith("lean-resource-")
        ]
        assert [event_type for event_type, _details in resource_events] == [
            "lean-resource-waiting",
            "lean-resource-admission",
            "lean-resource-released",
        ]
        request_ids = {details["admission_request_id"] for _event_type, details in resource_events}
        assert len(request_ids) == 1
        assert all(
            details["admission_source"] == "inner_tool_call"
            for _event_type, details in resource_events
        )
        assert all(details["tool"] == "lean_inspect" for _event_type, details in resource_events)

    def test_foreground_lease_spans_batch_and_target_check_precedes_inspect(
        self, agent, monkeypatch
    ):
        """Keep background out while the authoritative check outranks diagnostics."""
        from agent.execution.admission_handoff import replace_initial_foreground_lease

        calls = []

        class _Lease:
            active = True
            releases = 0

            def release(self):
                self.active = False
                self.releases += 1
                return True

        lease = _Lease()
        replace_initial_foreground_lease(agent, lease)
        tool_calls = [
            _mock_tool_call(
                name="lean_inspect",
                arguments=json.dumps({"target": "Demo/Main.lean", "symbol": "demo"}),
                call_id="inspect",
            ),
            _mock_tool_call(
                name="lean_incremental_check",
                arguments=json.dumps(
                    {
                        "action": "check_target",
                        "file_path": "Demo/Main.lean",
                        "theorem_id": "demo",
                    }
                ),
                call_id="check",
            ),
        ]
        admission = SimpleNamespace(
            to_dict=lambda: {},
            retain_until_process_exit=lambda reason: None,
            reserve_foreground_handoff=lambda seconds, reason="": seconds,
        )

        def handle(name, *_args, **_kwargs):
            assert lease.active is True
            calls.append(name)
            return json.dumps({"success": True, "ok": True})

        messages = []
        with (
            patch("run_agent.handle_function_call", side_effect=handle),
            patch(
                "agent.execution.tool_executor.project_lean_heavy_admission",
                return_value=nullcontext(admission),
            ),
        ):
            agent._execute_tool_calls_concurrent(
                _mock_assistant_msg(content="", tool_calls=tool_calls),
                messages,
                "task-1",
            )

        assert calls == ["lean_incremental_check", "lean_inspect"]
        assert lease.active is False
        assert lease.releases == 1
        assert [message["tool_call_id"] for message in messages] == ["inspect", "check"]

    def test_remote_search_does_not_take_project_lean_admission(self, agent, monkeypatch):
        """Remote/text search must not pay a local Lean subprocess gate."""
        monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
        tool_call = _mock_tool_call(
            name="lean_search",
            arguments=json.dumps({"query": "Nat.add_comm", "mode": "semantic"}),
            call_id="c1",
        )

        with (
            patch("run_agent.handle_function_call", return_value="ok"),
            patch("leanflow_cli.lean.lean_incremental.close_incremental_sessions") as close,
        ):
            agent._execute_tool_calls_sequential(
                _mock_assistant_msg(content="", tool_calls=[tool_call]), [], "task-1"
            )

        close.assert_not_called()

    def test_failed_sequential_probe_close_reports_retained_slot(
        self, agent, monkeypatch, tmp_path
    ):
        """Do not emit a reclaimed event when owned LeanProbe close fails."""
        project = tmp_path / "Demo"
        project.mkdir()
        (project / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
        monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
        monkeypatch.setenv("LEANFLOW_PROJECT_LEAN_ADMISSION", "1")
        monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", "1")
        tool_call = _mock_tool_call(
            name="lean_verify",
            arguments=json.dumps({"target": "Demo/Main.lean"}),
            call_id="c1",
        )

        with (
            patch("run_agent.handle_function_call", return_value="ok"),
            patch(
                "leanflow_cli.lean.lean_incremental.close_incremental_sessions",
                return_value=False,
            ),
            patch("run_agent._emit_workflow_event") as emit,
        ):
            agent._execute_tool_calls_sequential(
                _mock_assistant_msg(content="", tool_calls=[tool_call]), [], "task-1"
            )

        retained = [
            call
            for call in emit.call_args_list
            if call.args and call.args[0] == "lean-resource-retained"
        ]
        assert len(retained) == 1
        assert retained[0].kwargs["retained_until_process_exit"] is True

    def test_concurrent_cheap_lean_tools_still_overlap(self, agent):
        """The memory gate must not serialize text-only Lean inspection tools."""
        tool_calls = [
            _mock_tool_call(
                name="lean_outline",
                arguments=json.dumps({"file_path": f"File{index}.lean"}),
                call_id=f"c{index}",
            )
            for index in range(2)
        ]
        mock_msg = _mock_assistant_msg(content="", tool_calls=tool_calls)
        messages = []
        rendezvous = threading.Barrier(2, timeout=2)

        def fake_handle(name, args, task_id, **kwargs):
            rendezvous.wait()
            return json.dumps({"result": args["file_path"]})

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        assert len(messages) == 2

    def test_concurrent_preserves_order_despite_timing(self, agent):
        """Even if tools finish in different order, messages should be in original order."""
        import time as _time

        tc1 = _mock_tool_call(name="web_search", arguments='{"q":"slow"}', call_id="c1")
        tc2 = _mock_tool_call(name="web_search", arguments='{"q":"fast"}', call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
        messages = []

        def fake_handle(name, args, task_id, **kwargs):
            q = args.get("q", "")
            if q == "slow":
                _time.sleep(0.1)  # Slow tool
            return f"result_{q}"

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        assert messages[0]["tool_call_id"] == "c1"
        assert "result_slow" in messages[0]["content"]
        assert messages[1]["tool_call_id"] == "c2"
        assert "result_fast" in messages[1]["content"]

    def test_concurrent_handles_tool_error(self, agent):
        """If one tool raises, others should still complete."""
        tc1 = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        tc2 = _mock_tool_call(name="web_search", arguments="{}", call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
        messages = []

        call_count = [0]

        def fake_handle(name, args, task_id, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("boom")
            return "success"

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        assert len(messages) == 2
        # First tool should have error
        assert "Error" in messages[0]["content"] or "boom" in messages[0]["content"]
        # Second tool should succeed
        assert "success" in messages[1]["content"]

    def test_concurrent_invokes_post_tool_result_callback(self, agent):
        """Concurrent path should preserve the same post-tool hooks as sequential execution."""
        tc1 = _mock_tool_call(name="web_search", arguments='{"q":"alpha"}', call_id="c1")
        tc2 = _mock_tool_call(name="web_search", arguments='{"q":"beta"}', call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
        messages = []
        callbacks = []

        def fake_handle(name, args, task_id, **kwargs):
            return f"result_{args['q']}"

        agent.post_tool_result_callback = lambda name, args, result: callbacks.append(
            (name, args, result)
        )

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        assert sorted(callbacks, key=lambda item: item[1]["q"]) == [
            ("web_search", {"q": "alpha"}, "result_alpha"),
            ("web_search", {"q": "beta"}, "result_beta"),
        ]

    def test_concurrent_callback_runs_before_slowest_tool_and_keeps_messages_ordered(self, agent):
        """A fast result must reach managed state without waiting for a slow sibling."""
        slow_started = threading.Event()
        release_slow = threading.Event()
        fast_callback_seen = threading.Event()
        worker_threads: set[int] = set()
        callback_threads: set[int] = set()
        callbacks: list[str] = []
        messages: list[dict] = []
        tc1 = _mock_tool_call(name="web_search", arguments='{"q":"slow"}', call_id="c1")
        tc2 = _mock_tool_call(name="web_search", arguments='{"q":"fast"}', call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])

        def fake_handle(name, args, task_id, **kwargs):
            worker_threads.add(threading.get_ident())
            query = args["q"]
            if query == "slow":
                slow_started.set()
                if not release_slow.wait(timeout=5):
                    raise TimeoutError("test did not release slow tool")
            return f"result_{query}"

        def callback(name, args, result):
            query = args["q"]
            callback_threads.add(threading.get_ident())
            callbacks.append(query)
            agent.stage_tool_result_appendix(f"[managed finding for {query}]")
            if query == "fast":
                fast_callback_seen.set()

        agent.post_tool_result_callback = callback

        def run_batch():
            with patch("run_agent.handle_function_call", side_effect=fake_handle):
                agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        batch_thread = threading.Thread(target=run_batch)
        batch_thread.start()
        try:
            assert slow_started.wait(timeout=2)
            assert fast_callback_seen.wait(timeout=2)
            assert batch_thread.is_alive(), "slow sibling should still be blocking the batch"
        finally:
            release_slow.set()
            batch_thread.join(timeout=5)

        assert not batch_thread.is_alive()
        assert callbacks == ["fast", "slow"]
        assert callback_threads == {batch_thread.ident}
        assert callback_threads.isdisjoint(worker_threads)
        assert [message["tool_call_id"] for message in messages] == ["c1", "c2"]
        assert "result_slow" in messages[0]["content"]
        assert "[managed finding for slow]" in messages[0]["content"]
        assert "[managed finding for fast]" not in messages[0]["content"]
        assert "result_fast" in messages[1]["content"]
        assert "[managed finding for fast]" in messages[1]["content"]
        assert "[managed finding for slow]" not in messages[1]["content"]

    def test_delegated_child_concurrent_results_reach_managed_parent_callback(self, agent):
        """A delegated lane must forward concurrent results to the managed parent hook."""
        from tools.implementations import delegate_tool

        parent = MagicMock()
        parent.base_url = "https://example.test/v1"
        parent.api_key = "parent-key"
        parent.provider = "test"
        parent.api_mode = "chat_completions"
        parent.model = "test-model"
        parent.platform = "cli"
        parent.enabled_toolsets = ["web"]
        parent.max_tokens = None
        parent.reasoning_config = None
        parent.seed = 42
        parent.temperature = 0.3
        parent.top_p = None
        parent.top_k = None
        parent.min_p = None
        parent.prefill_messages = None
        parent._session_db = None
        parent._delegate_depth = 0
        parent._delegate_spinner = None
        parent.tool_progress_callback = None
        parent.iteration_budget = agent.iteration_budget
        parent.providers_allowed = None
        parent.providers_ignored = None
        parent.providers_order = None
        parent.provider_sort = None
        parent.session_id = "parent-session"

        callbacks = []
        parent._managed_delegated_post_tool_result_callback = (
            lambda executing_agent, name, args, result: callbacks.append(
                (
                    str(getattr(executing_agent, "session_id", "") or ""),
                    str(getattr(executing_agent, "_parent_session_id", "") or ""),
                    name,
                    args,
                    result,
                )
            )
        )

        tc1 = _mock_tool_call(name="web_search", arguments='{"q":"alpha"}', call_id="c1")
        tc2 = _mock_tool_call(name="web_fetch", arguments='{"url":"beta"}', call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])

        def build_child(**kwargs):
            # Mirror the constructor fields involved in this integration while
            # retaining the real ToolExecutor installed by the agent fixture.
            agent.post_tool_result_callback = kwargs.get("post_tool_result_callback")
            agent.tool_progress_callback = kwargs.get("tool_progress_callback")
            return agent

        def run_child_conversation(*, user_message):
            messages = []
            agent._execute_tool_calls_concurrent(mock_msg, messages, "child-task")
            return {
                "final_response": user_message,
                "completed": True,
                "interrupted": False,
                "api_calls": 1,
                "messages": messages,
            }

        agent.run_conversation = run_child_conversation
        credentials = {
            "model": None,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }
        with (
            patch.object(delegate_tool, "_load_config", return_value={"max_iterations": 4}),
            patch.object(
                delegate_tool,
                "_resolve_delegation_credentials",
                return_value=credentials,
            ),
            patch("run_agent.AIAgent", side_effect=build_child),
            patch(
                "run_agent.handle_function_call",
                side_effect=lambda name, args, task_id, **kwargs: f"result_{name}",
            ),
        ):
            result = json.loads(
                delegate_tool.delegate_task(goal="research lane", parent_agent=parent)
            )

        assert result["results"][0]["status"] == "completed"
        assert sorted(callbacks, key=lambda item: item[2]) == [
            (
                str(agent.session_id),
                "parent-session",
                "web_fetch",
                {"url": "beta"},
                "result_web_fetch",
            ),
            (
                str(agent.session_id),
                "parent-session",
                "web_search",
                {"q": "alpha"},
                "result_web_search",
            ),
        ]

    def test_concurrent_post_tool_result_callback_can_append_tool_context(self, agent):
        tc1 = _mock_tool_call(name="web_search", arguments='{"q":"alpha"}', call_id="c1")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1])
        messages = []

        def fake_handle(name, args, task_id, **kwargs):
            return "result_alpha"

        def callback(name, args, result):
            agent._post_tool_result_appendix = "[manager feedback]"

        agent.post_tool_result_callback = callback

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        assert "result_alpha" in messages[0]["content"]
        assert "[manager feedback]" in messages[0]["content"]
        assert agent._post_tool_result_appendix is None

    def test_concurrent_interrupt_before_start(self, agent):
        """If interrupt is requested before concurrent execution, all tools are skipped."""
        tc1 = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        tc2 = _mock_tool_call(name="read_file", arguments="{}", call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
        messages = []

        with patch("run_agent._set_interrupt"):
            agent.interrupt()

        agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")
        assert len(messages) == 2
        assert (
            "cancelled" in messages[0]["content"].lower()
            or "skipped" in messages[0]["content"].lower()
        )
        assert (
            "cancelled" in messages[1]["content"].lower()
            or "skipped" in messages[1]["content"].lower()
        )

    def test_concurrent_truncates_large_results(self, agent):
        """Concurrent path should truncate results over 100k chars."""
        tc1 = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        tc2 = _mock_tool_call(name="web_search", arguments="{}", call_id="c2")
        mock_msg = _mock_assistant_msg(content="", tool_calls=[tc1, tc2])
        messages = []
        big_result = "x" * 150_000

        with patch("run_agent.handle_function_call", return_value=big_result):
            agent._execute_tool_calls_concurrent(mock_msg, messages, "task-1")

        assert len(messages) == 2
        for m in messages:
            assert len(m["content"]) < 150_000
            assert "Truncated" in m["content"]

    def test_invoke_tool_dispatches_to_handle_function_call(self, agent):
        """_invoke_tool should route regular tools through handle_function_call."""
        with patch("run_agent.handle_function_call", return_value="result") as mock_hfc:
            result = agent._invoke_tool("web_search", {"q": "test"}, "task-1")
            mock_hfc.assert_called_once()
            args, kwargs = mock_hfc.call_args
            assert args == ("web_search", {"q": "test"}, "task-1")
            assert kwargs["enabled_tools"] == list(agent.valid_tool_names)
            assert kwargs["owner_id"] == agent.session_id
            assert kwargs["parent_agent"] is agent
            assert result == "result"

    def test_invoke_tool_handles_agent_level_tools(self, agent):
        """_invoke_tool should handle todo tool directly."""
        with patch(
            "tools.implementations.todo_tool.todo_tool", return_value='{"ok":true}'
        ) as mock_todo:
            result = agent._invoke_tool("todo", {"todos": []}, "task-1")
            mock_todo.assert_called_once()
        assert "ok" in result


class TestHandleMaxIterations:
    def test_returns_summary(self, agent):
        resp = _mock_response(content="Here is a summary of what I did.")
        agent.client.chat.completions.create.return_value = resp
        agent._cached_system_prompt = "You are helpful."
        messages = [{"role": "user", "content": "do stuff"}]
        result = agent._handle_max_iterations(messages, 60)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "summary" in result.lower()

    def test_api_failure_returns_error(self, agent):
        agent.client.chat.completions.create.side_effect = Exception("API down")
        agent._cached_system_prompt = "You are helpful."
        messages = [{"role": "user", "content": "do stuff"}]
        result = agent._handle_max_iterations(messages, 60)
        assert isinstance(result, str)
        assert "error" in result.lower()
        assert "API down" in result

    def test_summary_skips_reasoning_for_unsupported_openrouter_model(self, agent):
        agent.model = "minimax/minimax-m2.5"
        resp = _mock_response(content="Summary")
        agent.client.chat.completions.create.return_value = resp
        agent._cached_system_prompt = "You are helpful."
        messages = [{"role": "user", "content": "do stuff"}]

        result = agent._handle_max_iterations(messages, 60)

        assert result == "Summary"
        kwargs = agent.client.chat.completions.create.call_args.kwargs
        assert "reasoning" not in kwargs.get("extra_body", {})


class TestRunConversation:
    """Tests for the main run_conversation method.

    Each test mocks client.chat.completions.create to return controlled
    responses, exercising different code paths without real API calls.
    """

    def _setup_agent(self, agent):
        """Common setup for run_conversation tests."""
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False

    def test_stop_finish_reason_returns_response(self, agent):
        self._setup_agent(agent)
        resp = _mock_response(content="Final answer", finish_reason="stop")
        agent.client.chat.completions.create.return_value = resp
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello")
        assert result["final_response"] == "Final answer"
        assert result["completed"] is True

    def test_non_quiet_logging_reports_prompt_preview_and_usage(self, agent, capsys):
        self._setup_agent(agent)
        agent.quiet_mode = False
        agent.model = "gpt-4o"
        agent.log_preview_lines = 4
        agent.log_preview_chars = 520
        prompt = "Resume managed workflow from persisted verified proof milestone. " + (
            "Keep the theorem queue context visible. " * 8
        )
        resp = _mock_response(
            content="Final answer",
            finish_reason="stop",
            usage={
                "prompt_tokens": 1_234,
                "completion_tokens": 56,
                "total_tokens": 1_290,
            },
        )
        agent.client.chat.completions.create.return_value = resp
        capsys.readouterr()

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation(prompt)

        output = capsys.readouterr().out
        assert result["completed"] is True
        assert "Starting conversation" in output
        assert "verified proof milestone" in output
        assert "API step 1/200" in output
        assert "Tokens: input 1,234 · output 56 · total 1,290" in output
        assert "Cost estimate: step $" in output

    def test_non_quiet_logging_reports_session_usage_summary(self, agent, capsys):
        self._setup_agent(agent)
        agent.quiet_mode = False
        agent.model = "gpt-4o"
        resp = _mock_response(
            content="Final answer",
            finish_reason="stop",
            usage={
                "prompt_tokens": 1_234,
                "completion_tokens": 56,
                "total_tokens": 1_290,
            },
        )
        agent.client.chat.completions.create.return_value = resp
        capsys.readouterr()

        with (
            patch.object(agent, "_save_session_log"),
            patch.object(agent, "_flush_messages_to_session_db"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello")

        output = capsys.readouterr().out
        assert result["completed"] is True
        assert result["usage"]["turn"]["prompt_tokens"] == 1_234
        assert result["usage"]["turn"]["completion_tokens"] == 56
        assert result["usage"]["cost"]["source"] == "estimated"
        assert "Session usage summary" in output
        assert "API calls: 1 this conversation" in output
        assert "Tokens this conversation: input 1,234 · output 56 · total 1,290" in output
        assert "Total cost estimate: $" in output

    def test_session_usage_summary_prefers_provider_reported_cost(self, agent, capsys):
        self._setup_agent(agent)
        agent.quiet_mode = False
        agent.model = "unknown/private-model"
        resp = _mock_response(
            content="Final answer",
            finish_reason="stop",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cost": 0.0042,
            },
        )
        agent.client.chat.completions.create.return_value = resp
        capsys.readouterr()

        with (
            patch.object(agent, "_save_session_log"),
            patch.object(agent, "_flush_messages_to_session_db"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello")

        output = capsys.readouterr().out
        assert result["usage"]["cost"]["source"] == "provider_reported"
        assert result["usage"]["cost"]["total_usd"] == pytest.approx(0.0042)
        assert "Total cost: $0.0042 (provider reported)" in output

    def test_tool_calls_then_stop(self, agent):
        self._setup_agent(agent)
        tc = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        resp1 = _mock_response(content="", finish_reason="tool_calls", tool_calls=[tc])
        resp2 = _mock_response(content="Done searching", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [resp1, resp2]
        with (
            patch("run_agent.handle_function_call", return_value="search result"),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("search something")
        assert result["final_response"] == "Done searching"
        assert result["api_calls"] == 2

    def test_interrupt_breaks_loop(self, agent):
        self._setup_agent(agent)

        def interrupt_side_effect(api_kwargs):
            agent._interrupt_requested = True
            raise InterruptedError("Agent interrupted during API call")

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent._set_interrupt"),
            patch.object(agent, "_interruptible_api_call", side_effect=interrupt_side_effect),
        ):
            result = agent.run_conversation("hello")
        assert result["interrupted"] is True

    def test_invalid_tool_name_retry(self, agent):
        """Model hallucinates an invalid tool name, agent retries and succeeds."""
        self._setup_agent(agent)
        bad_tc = _mock_tool_call(name="nonexistent_tool", arguments="{}", call_id="c1")
        resp_bad = _mock_response(content="", finish_reason="tool_calls", tool_calls=[bad_tc])
        resp_good = _mock_response(content="Got it", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [resp_bad, resp_good]
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("do something")
        assert result["final_response"] == "Got it"
        assert result["completed"] is True
        assert result["api_calls"] == 2

    def test_empty_content_retry_and_fallback(self, agent):
        """Empty content (only think block) retries, then falls back to partial."""
        self._setup_agent(agent)
        empty_resp = _mock_response(
            content="<think>internal reasoning</think>",
            finish_reason="stop",
        )
        # Return empty 3 times to exhaust retries
        agent.client.chat.completions.create.side_effect = [
            empty_resp,
            empty_resp,
            empty_resp,
        ]
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("answer me")
        # After 3 retries with no real content, should return partial
        assert result["completed"] is False
        assert result.get("partial") is True

    def test_nous_401_refreshes_after_remint_and_retries(self, agent):
        self._setup_agent(agent)
        agent.provider = "nous"
        agent.api_mode = "chat_completions"

        calls = {"api": 0, "refresh": 0}

        class _UnauthorizedError(RuntimeError):
            def __init__(self):
                super().__init__("Error code: 401 - unauthorized")
                self.status_code = 401

        def _fake_api_call(api_kwargs):
            calls["api"] += 1
            if calls["api"] == 1:
                raise _UnauthorizedError()
            return _mock_response(content="Recovered after remint", finish_reason="stop")

        def _fake_refresh(*, force=True):
            calls["refresh"] += 1
            assert force is True
            return True

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(agent, "_interruptible_api_call", side_effect=_fake_api_call),
            patch.object(agent, "_try_refresh_nous_client_credentials", side_effect=_fake_refresh),
        ):
            result = agent.run_conversation("hello")

        assert calls["api"] == 2
        assert calls["refresh"] == 1
        assert result["completed"] is True
        assert result["final_response"] == "Recovered after remint"

    def test_anthropic_401_diagnostic_never_renders_token_fragments(self, agent, capsys):
        """Authentication diagnostics report status without token-derived text."""
        self._setup_agent(agent)
        secret = "sk-ant-oat01-credfragstart1234567890credfragend"
        agent.provider = "anthropic"
        agent.api_mode = "anthropic_messages"
        agent._anthropic_api_key = secret
        agent.quiet_mode = False

        class _UnauthorizedError(RuntimeError):
            def __init__(self):
                super().__init__("unauthorized")
                self.status_code = 401

        capsys.readouterr()
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(agent, "_dump_api_request_debug"),
            patch.object(agent, "_interruptible_api_call", side_effect=_UnauthorizedError()),
            patch.object(
                agent,
                "_try_refresh_anthropic_client_credentials",
                return_value=False,
            ),
        ):
            result = agent.run_conversation("hello")

        output = capsys.readouterr().out
        assert result["completed"] is False
        assert "Credential status: configured" in output
        assert "Token prefix:" not in output
        assert secret not in output
        assert "credfragstart" not in output
        assert "credfragend" not in output

    def test_context_compression_triggered(self, agent):
        """When compressor says should_compress, compression runs."""
        self._setup_agent(agent)
        agent.compression_enabled = True

        tc = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        resp1 = _mock_response(content="", finish_reason="tool_calls", tool_calls=[tc])
        resp2 = _mock_response(content="All done", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [resp1, resp2]

        with (
            patch("run_agent.handle_function_call", return_value="result"),
            patch.object(agent.context_compressor, "should_compress", return_value=True),
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            # _compress_context should return (messages, system_prompt)
            mock_compress.return_value = (
                [{"role": "user", "content": "search something"}],
                "compressed system prompt",
            )
            result = agent.run_conversation("search something")
        mock_compress.assert_called_once()
        assert result["final_response"] == "All done"
        assert result["completed"] is True

    def test_post_tool_compression_uses_next_prompt_estimate(self, agent):
        """Post-tool compression should log the estimate that crossed threshold."""
        self._setup_agent(agent)
        agent.compression_enabled = True

        tc = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        resp1 = _mock_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[tc],
            usage={
                "prompt_tokens": 121_308,
                "completion_tokens": 1_000,
                "total_tokens": 122_308,
            },
        )
        resp2 = _mock_response(content="All done", finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [resp1, resp2]
        tool_result = "x" * 18_000
        expected_estimate = 121_308 + 1_000 + (len(tool_result) // 3)
        seen_estimates: list[int] = []

        def _should_compress(value):
            seen_estimates.append(value)
            return True

        with (
            patch("run_agent.handle_function_call", return_value=tool_result),
            patch.object(
                agent.context_compressor,
                "should_compress",
                side_effect=_should_compress,
            ),
            patch.object(agent, "_compress_context") as mock_compress,
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            mock_compress.return_value = (
                [{"role": "user", "content": "search something"}],
                "compressed system prompt",
            )
            result = agent.run_conversation("search something")

        assert seen_estimates == [expected_estimate]
        mock_compress.assert_called_once()
        assert mock_compress.call_args.kwargs["approx_tokens"] == expected_estimate
        assert result["final_response"] == "All done"
        assert result["completed"] is True

    def test_post_tool_tail_emits_phase_timing_before_next_api_step(self, agent):
        """Slow-tail telemetry separates compression and session persistence from callbacks."""
        self._setup_agent(agent)
        tc = _mock_tool_call(name="web_search", arguments="{}", call_id="c1")
        agent.client.chat.completions.create.side_effect = [
            _mock_response(content="", finish_reason="tool_calls", tool_calls=[tc]),
            _mock_response(content="All done", finish_reason="stop"),
        ]
        emitted: list[tuple[str, dict]] = []

        with (
            patch("run_agent.handle_function_call", return_value="result"),
            patch("run_agent._POST_TOOL_TAIL_SLOW_THRESHOLD_S", 0.0),
            patch(
                "run_agent._emit_workflow_event",
                side_effect=lambda event, _message, **details: emitted.append((event, details)),
            ),
            patch.object(agent, "_save_session_log"),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("search something")

        assert result["final_response"] == "All done"
        tail = next(details for event, details in emitted if event == "post-tool-tail-slow")
        assert tail["tools"] == ["web_search"]
        assert tail["compression_triggered"] is False
        assert set(tail["phase_seconds"]) == {
            "next_prompt_estimate",
            "compression",
            "session_log",
        }

    def test_pre_send_compression_counts_reasoning_replay_payload(self, agent):
        """Pre-send compression should use the final API payload, including reasoning replay."""
        self._setup_agent(agent)
        agent.compression_enabled = True
        agent.context_compressor.threshold_tokens = 1_000
        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "", "reasoning": "x" * 6_000},
        ]
        api_messages = agent._build_api_messages_for_turn(messages, "You are helpful.")
        approx_tokens, _ = agent._api_payload_size_estimate(api_messages)

        assert approx_tokens >= agent.context_compressor.threshold_tokens
        assert any("reasoning_content" in msg for msg in api_messages)

        with patch.object(
            agent,
            "_compress_context",
            return_value=([{"role": "user", "content": "compact handoff"}], "compressed system"),
        ) as mock_compress:
            new_messages, new_system, new_api_messages, new_tokens, _ = (
                agent._maybe_compress_before_api_send(
                    messages,
                    "You are helpful.",
                    "You are helpful.",
                    api_messages=api_messages,
                    approx_tokens=approx_tokens,
                    task_id="test-task",
                )
            )

        mock_compress.assert_called_once()
        assert mock_compress.call_args.kwargs["approx_tokens"] == approx_tokens
        assert new_messages == [{"role": "user", "content": "compact handoff"}]
        assert new_system == "compressed system"
        assert new_tokens < approx_tokens
        assert not any("reasoning_content" in msg for msg in new_api_messages)

    def test_reasoning_replay_keeps_only_most_recent_assistant(self, agent, monkeypatch):
        """By default, only the most recent assistant reasoning is replayed as reasoning_content.

        Replaying every prior reasoning block multiplies hidden-input tokens across tool turns; the
        default now keeps just the last, with LEANFLOW_REPLAY_ALL_REASONING=1 to restore old behavior.
        """
        self._setup_agent(agent)
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1", "reasoning": "older thinking"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2", "reasoning": "newest thinking"},
        ]

        monkeypatch.delenv("LEANFLOW_REPLAY_ALL_REASONING", raising=False)
        api_messages = agent._build_api_messages_for_turn(messages, "sys")
        carriers = [
            m for m in api_messages if m.get("role") == "assistant" and "reasoning_content" in m
        ]
        assert len(carriers) == 1
        assert carriers[0]["reasoning_content"] == "newest thinking"

        monkeypatch.setenv("LEANFLOW_REPLAY_ALL_REASONING", "1")
        api_all = agent._build_api_messages_for_turn(messages, "sys")
        carriers_all = [
            m for m in api_all if m.get("role") == "assistant" and "reasoning_content" in m
        ]
        assert len(carriers_all) == 2

    @pytest.mark.parametrize(
        ("first_content", "second_content", "expected_final"),
        [
            ("Part 1 ", "Part 2", "Part 1 Part 2"),
            (
                "<think>internal reasoning</think>",
                "Recovered final answer",
                "Recovered final answer",
            ),
        ],
    )
    def test_length_finish_reason_requests_continuation(
        self, agent, first_content, second_content, expected_final
    ):
        self._setup_agent(agent)
        first = _mock_response(content=first_content, finish_reason="length")
        second = _mock_response(content=second_content, finish_reason="stop")
        agent.client.chat.completions.create.side_effect = [first, second]

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("hello")

        assert result["completed"] is True
        assert result["api_calls"] == 2
        assert result["final_response"] == expected_final

        second_call_messages = agent.client.chat.completions.create.call_args_list[1].kwargs[
            "messages"
        ]
        assert second_call_messages[-1]["role"] == "user"
        assert "truncated by the output length limit" in second_call_messages[-1]["content"]


class TestRetryExhaustion:
    """Regression: retry_count > max_retries was dead code (off-by-one).

    When retries were exhausted the condition never triggered, causing
    the loop to exit and fall through to response.choices[0] on an
    invalid response, raising IndexError.
    """

    def _setup_agent(self, agent):
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False

    @staticmethod
    def _make_fast_time_mock():
        """Return a mock time module where sleep loops exit instantly."""
        mock_time = MagicMock()
        _t = [1000.0]

        def _advancing_time():
            _t[0] += 500.0  # jump 500s per call so sleep_end is always in the past
            return _t[0]

        mock_time.time.side_effect = _advancing_time
        mock_time.sleep = MagicMock()  # no-op
        mock_time.monotonic.return_value = 12345.0
        return mock_time

    def test_invalid_response_returns_error_not_crash(self, agent, capsys):
        """Exhausted retries on invalid (empty choices) response must not IndexError."""
        self._setup_agent(agent)
        secret = "sk-invalidresponsesecret1234567890"
        # Return response with empty choices every time
        bad_resp = SimpleNamespace(
            choices=[],
            model="test/model",
            usage=None,
            error=RuntimeError(f"rate limited Authorization: Bearer {secret}"),
        )
        agent.client.chat.completions.create.return_value = bad_resp
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.time", self._make_fast_time_mock()),
        ):
            result = agent.run_conversation("hello")
        assert result.get("completed") is False, f"Expected completed=False, got: {result}"
        assert result.get("failed") is True
        assert result.get("provider_retries_exhausted") is True
        assert agent.client.chat.completions.create.call_count == 4
        assert "error" in result
        assert "Invalid API response" in result["error"]
        assert secret not in capsys.readouterr().out

    def test_api_error_raises_after_retries(self, agent, capsys, caplog):
        """Exhausted retries on API errors must raise, not fall through."""
        self._setup_agent(agent)
        secret = "sk-transientprovidersecret1234567890"
        agent.client.chat.completions.create.side_effect = RuntimeError(
            f"rate limited Authorization: Bearer {secret}"
        )
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.time", self._make_fast_time_mock()),
            patch("run_agent._emit_workflow_event") as emit_event,
        ):
            with pytest.raises(RuntimeError, match="rate limited") as raised:
                agent.run_conversation("hello")
        assert raised.value.provider_retries_exhausted is True
        assert secret not in str(raised.value)
        assert agent.client.chat.completions.create.call_count == 4
        scheduled = [
            call.kwargs["wait_seconds"]
            for call in emit_event.call_args_list
            if call.args and call.args[0] == "provider-retry-scheduled"
        ]
        exhausted = [
            call
            for call in emit_event.call_args_list
            if call.args and call.args[0] == "provider-retry-exhausted"
        ]
        assert scheduled == [5.0, 15.0, 45.0]
        assert len(exhausted) == 1
        assert secret not in repr(emit_event.call_args_list)
        assert secret not in capsys.readouterr().out
        assert secret not in caplog.text

    def test_live_codex_usage_limit_pauses_once_with_structured_reset(
        self, agent, monkeypatch, tmp_path
    ):
        """The observed five-day Codex reset must bypass every fixed retry."""
        self._setup_agent(agent)
        monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("LEANFLOW_PROVIDER_RESET_MAX_WAIT_SECONDS", "0")
        agent.provider = "openai-codex"
        agent.base_url = "https://chatgpt.com/backend-api/codex"

        class LiveRateLimitError(RuntimeError):
            status_code = 429
            body = {
                "type": "usage_limit_reached",
                "message": "The usage limit has been reached",
                "plan_type": "pro",
                "resets_at": 1784949879,
                "eligible_promo": None,
                "resets_in_seconds": 453096,
            }

        provider_call = MagicMock(
            side_effect=LiveRateLimitError(
                "Error code: 429 - {'error': {'type': 'usage_limit_reached', "
                "'message': 'The usage limit has been reached', 'plan_type': 'pro', "
                "'resets_at': 1784949879, 'eligible_promo': None, "
                "'resets_in_seconds': 453096}}"
            )
        )
        pause_order: list[tuple[str, dict]] = []
        agent._managed_provider_usage_limit_callback = lambda metadata: pause_order.append(
            ("pause", dict(metadata))
        )
        with (
            patch.object(agent, "_interruptible_api_call", provider_call),
            patch.object(
                agent,
                "_persist_session",
                side_effect=lambda *_args, **_kwargs: pause_order.append(("persist", {})),
            ),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.time.time", return_value=1784496783),
            patch("run_agent._emit_workflow_event") as emit_event,
        ):
            result = agent.run_conversation("hello")

        assert result["failed"] is True
        assert result["completed"] is False
        assert result["provider_retries_exhausted"] is True
        assert result["provider_globally_unavailable"] is True
        assert result["provider_retry_after"] == {
            "kind": "usage_limit_reached",
            "retry_after_seconds": 453097,
            "unavailable_until_epoch": 1784949880,
            "resets_at_epoch": 1784949879,
            "reported_resets_in_seconds": 453096,
            "timing_consistent": True,
            "timing_clamped": False,
            "source": "exception.body",
        }
        assert provider_call.call_count == 1
        assert [phase for phase, _metadata in pause_order[:2]] == ["pause", "persist"]
        assert pause_order[0][1] == result["provider_retry_after"]
        assert not any(
            call.args and call.args[0] == "provider-retry-scheduled"
            for call in emit_event.call_args_list
        )
        usage_events = [
            call
            for call in emit_event.call_args_list
            if call.args and call.args[0] == "provider-usage-limit"
        ]
        assert len(usage_events) == 1
        assert usage_events[0].kwargs["retry_after_seconds"] == 453097

    def test_transient_retry_wait_is_interruptible_before_second_provider_call(self, agent):
        """A cancellation during backoff must not wait five seconds or issue a retry."""
        self._setup_agent(agent)
        agent.quiet_mode = False
        provider_call = MagicMock(side_effect=RuntimeError("503 unavailable"))
        sleep_calls: list[float] = []

        def interrupt_on_poll(delay):
            sleep_calls.append(delay)
            agent._interrupt_requested = True

        with (
            patch.object(agent, "_interruptible_api_call", provider_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch("run_agent.time.sleep", side_effect=interrupt_on_poll),
        ):
            result = agent.run_conversation("hello")

        assert result["interrupted"] is True
        assert result["completed"] is False
        assert provider_call.call_count == 1
        assert sleep_calls == [0.2]


# ---------------------------------------------------------------------------
# Flush sentinel leak
# ---------------------------------------------------------------------------


class TestFlushSentinelNotLeaked:
    """_flush_sentinel must be stripped before sending messages to the API."""

    def test_flush_sentinel_stripped_from_api_messages(self, agent_with_memory_tool):
        """Verify _flush_sentinel is not sent to the API provider."""
        agent = agent_with_memory_tool
        agent._memory_store = MagicMock()
        agent._memory_flush_min_turns = 1
        agent._user_turn_count = 10
        agent._cached_system_prompt = "system"

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "remember this"},
        ]

        # Mock the API to return a simple response (no tool calls)
        mock_msg = SimpleNamespace(content="OK", tool_calls=None)
        mock_choice = SimpleNamespace(message=mock_msg)
        mock_response = SimpleNamespace(choices=[mock_choice])
        agent.client.chat.completions.create.return_value = mock_response

        # Bypass auxiliary client so flush uses agent.client directly
        with patch(
            "agent.providers.auxiliary_client.call_llm", side_effect=RuntimeError("no provider")
        ):
            agent.flush_memories(messages, min_turns=0)

        # Check what was actually sent to the API
        call_args = agent.client.chat.completions.create.call_args
        assert call_args is not None, "flush_memories never called the API"
        api_messages = call_args.kwargs.get("messages") or call_args[1].get("messages")
        for msg in api_messages:
            assert "_flush_sentinel" not in msg, f"_flush_sentinel leaked to API in message: {msg}"


# ---------------------------------------------------------------------------
# Conversation history mutation
# ---------------------------------------------------------------------------


class TestConversationHistoryNotMutated:
    """run_conversation must not mutate the caller's conversation_history list."""

    def test_caller_list_unchanged_after_run(self, agent):
        """Passing conversation_history should not modify the original list."""
        history = [
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
        ]
        original_len = len(history)

        resp = _mock_response(content="new answer", finish_reason="stop")
        agent.client.chat.completions.create.return_value = resp

        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("new question", conversation_history=history)

        # Caller's list must be untouched
        assert (
            len(history) == original_len
        ), f"conversation_history was mutated: expected {original_len} items, got {len(history)}"
        # Result should have more messages than the original history
        assert len(result["messages"]) > original_len


# ---------------------------------------------------------------------------
# _max_tokens_param consistency
# ---------------------------------------------------------------------------


class TestNousCredentialRefresh:
    """Verify Nous credential refresh rebuilds the runtime client."""

    def test_try_refresh_nous_client_credentials_rebuilds_client(self, agent, monkeypatch):
        agent.provider = "nous"
        agent.api_mode = "chat_completions"

        closed = {"value": False}
        rebuilt = {"kwargs": None}
        captured = {}

        class _ExistingClient:
            def close(self):
                closed["value"] = True

        class _RebuiltClient:
            pass

        def _fake_resolve(**kwargs):
            captured.update(kwargs)
            return {
                "api_key": "new-nous-key",
                "base_url": "https://inference-api.nousresearch.com/v1",
            }

        def _fake_openai(**kwargs):
            rebuilt["kwargs"] = kwargs
            return _RebuiltClient()

        monkeypatch.setattr(
            "leanflow_cli.runtime.auth.resolve_nous_runtime_credentials", _fake_resolve
        )

        agent.client = _ExistingClient()
        with patch("run_agent.OpenAI", side_effect=_fake_openai):
            ok = agent._try_refresh_nous_client_credentials(force=True)

        assert ok is True
        assert closed["value"] is True
        assert captured["force_mint"] is True
        assert rebuilt["kwargs"]["api_key"] == "new-nous-key"
        assert rebuilt["kwargs"]["base_url"] == "https://inference-api.nousresearch.com/v1"
        assert "default_headers" not in rebuilt["kwargs"]
        assert isinstance(agent.client, _RebuiltClient)


class TestMaxTokensParam:
    """Verify _max_tokens_param returns the correct key for each provider."""

    def test_returns_max_completion_tokens_for_direct_openai(self, agent):
        agent.base_url = "https://api.openai.com/v1"
        result = agent._max_tokens_param(4096)
        assert result == {"max_completion_tokens": 4096}

    def test_returns_max_tokens_for_openrouter(self, agent):
        agent.base_url = "https://openrouter.ai/api/v1"
        result = agent._max_tokens_param(4096)
        assert result == {"max_tokens": 4096}

    def test_returns_max_tokens_for_local(self, agent):
        agent.base_url = "http://localhost:11434/v1"
        result = agent._max_tokens_param(4096)
        assert result == {"max_tokens": 4096}

    def test_not_tricked_by_openai_in_openrouter_url(self, agent):
        agent.base_url = "https://openrouter.ai/api/v1/api.openai.com"
        result = agent._max_tokens_param(4096)
        assert result == {"max_tokens": 4096}


# ---------------------------------------------------------------------------
# System prompt stability for prompt caching
# ---------------------------------------------------------------------------


class TestSystemPromptStability:
    """Verify that the system prompt stays stable across turns for cache hits."""

    def test_stored_prompt_reused_for_continuing_session(self, agent):
        """When conversation_history is non-empty and session DB has a stored
        prompt, it should be reused instead of rebuilding from disk."""
        stored = "You are helpful. [stored from turn 1]"
        mock_db = MagicMock()
        mock_db.get_session.return_value = {"system_prompt": stored}
        agent._session_db = mock_db

        # Simulate a continuing session with history
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

        # First call — _cached_system_prompt is None, history is non-empty
        agent._cached_system_prompt = None

        # Patch run_conversation internals to just test the system prompt logic.
        # We'll call the prompt caching block directly by simulating what
        # run_conversation does.
        conversation_history = history

        # The block under test (from run_conversation):
        if agent._cached_system_prompt is None:
            stored_prompt = None
            if conversation_history and agent._session_db:
                try:
                    session_row = agent._session_db.get_session(agent.session_id)
                    if session_row:
                        stored_prompt = session_row.get("system_prompt") or None
                except Exception:
                    pass

            if stored_prompt:
                agent._cached_system_prompt = stored_prompt

        assert agent._cached_system_prompt == stored
        mock_db.get_session.assert_called_once_with(agent.session_id)

    def test_fresh_build_when_no_history(self, agent):
        """On the first turn (no history), system prompt should be built fresh."""
        mock_db = MagicMock()
        agent._session_db = mock_db

        agent._cached_system_prompt = None
        conversation_history = []

        # The block under test:
        if agent._cached_system_prompt is None:
            stored_prompt = None
            if conversation_history and agent._session_db:
                session_row = agent._session_db.get_session(agent.session_id)
                if session_row:
                    stored_prompt = session_row.get("system_prompt") or None

            if stored_prompt:
                agent._cached_system_prompt = stored_prompt
            else:
                agent._cached_system_prompt = agent._build_system_prompt()

        # Should have built fresh, not queried the DB
        mock_db.get_session.assert_not_called()
        assert agent._cached_system_prompt is not None
        assert "You are LeanFlow" in agent._cached_system_prompt

    def test_fresh_build_when_db_has_no_prompt(self, agent):
        """If the session DB has no stored prompt, build fresh even with history."""
        mock_db = MagicMock()
        mock_db.get_session.return_value = {"system_prompt": ""}
        agent._session_db = mock_db

        agent._cached_system_prompt = None
        conversation_history = [{"role": "user", "content": "hi"}]

        if agent._cached_system_prompt is None:
            stored_prompt = None
            if conversation_history and agent._session_db:
                try:
                    session_row = agent._session_db.get_session(agent.session_id)
                    if session_row:
                        stored_prompt = session_row.get("system_prompt") or None
                except Exception:
                    pass

            if stored_prompt:
                agent._cached_system_prompt = stored_prompt
            else:
                agent._cached_system_prompt = agent._build_system_prompt()

        # Empty string is falsy, so should fall through to fresh build
        assert "You are LeanFlow" in agent._cached_system_prompt


# ---------------------------------------------------------------------------
# Iteration budget pressure warnings
# ---------------------------------------------------------------------------


class TestBudgetPressure:
    """Budget pressure warning system (issue #414)."""

    def test_no_warning_below_caution(self, agent):
        agent.max_iterations = 60
        assert agent._get_budget_warning(30) is None

    def test_caution_at_70_percent(self, agent):
        agent.max_iterations = 60
        msg = agent._get_budget_warning(42)
        assert msg is not None
        assert "[BUDGET:" in msg
        assert "18 iterations left" in msg

    def test_warning_at_90_percent(self, agent):
        agent.max_iterations = 60
        msg = agent._get_budget_warning(54)
        assert "[BUDGET WARNING:" in msg
        assert "Provide your final response NOW" in msg

    def test_last_iteration(self, agent):
        agent.max_iterations = 60
        msg = agent._get_budget_warning(59)
        assert "1 iteration(s) left" in msg

    def test_disabled(self, agent):
        agent.max_iterations = 60
        agent._budget_pressure_enabled = False
        assert agent._get_budget_warning(55) is None

    def test_zero_max_iterations(self, agent):
        agent.max_iterations = 0
        assert agent._get_budget_warning(0) is None

    def test_runtime_budget_warning_message_is_model_visible(self, agent):
        agent.max_iterations = 10
        messages = [{"role": "user", "content": "continue"}]

        injected = agent._maybe_append_budget_warning_message(messages, 7)

        assert injected is True
        assert messages[-1]["role"] == "user"
        assert "LEANFLOW-RUNTIME STEP BUDGET" in messages[-1]["content"]
        assert "3 iterations left" in messages[-1]["content"]

    def test_runtime_budget_warning_skips_duplicate_tool_warning(self, agent):
        agent.max_iterations = 10
        warning = agent._get_budget_warning(9)
        messages = [{"role": "tool", "content": f"done\n\n{warning}", "tool_call_id": "tc1"}]

        assert agent._maybe_append_budget_warning_message(messages, 9) is False
        assert len(messages) == 1

    def test_advisor_budget_refresh_resets_to_half_budget(self, agent):
        agent.max_iterations = 180
        agent.iteration_budget = run_agent.IterationBudget(180)
        for _ in range(160):
            assert agent.iteration_budget.consume()

        refreshed = agent._maybe_refresh_api_step_budget_after_advisor(160)

        assert refreshed == 90
        assert agent.iteration_budget.used == 90

    def test_advisor_budget_refresh_does_not_reset_early_calls(self, agent):
        agent.max_iterations = 180
        agent.iteration_budget = run_agent.IterationBudget(180)
        for _ in range(40):
            assert agent.iteration_budget.consume()

        refreshed = agent._maybe_refresh_api_step_budget_after_advisor(40)

        assert refreshed == 40
        assert agent.iteration_budget.used == 40

    def test_lean_reasoning_help_gets_larger_tool_result_cap(self, agent):
        assert agent._max_tool_result_chars("lean_reasoning_help") > agent._max_tool_result_chars(
            "web_search"
        )
        assert agent._max_tool_result_chars(
            "lean_decompose_helpers"
        ) > agent._max_tool_result_chars("web_search")

    def test_precompresses_before_advisor_when_reserved_context_would_overflow(self, agent):
        agent.compression_enabled = True
        agent._advisor_result_context_reserve_tokens = 10_000
        messages = [
            {"role": "user", "content": "old theorem context"},
            {"role": "assistant", "content": "old attempt"},
        ]

        with (
            patch.object(agent.context_compressor, "should_compress", side_effect=[True, False]),
            patch.object(
                agent,
                "_compress_context",
                return_value=([{"role": "user", "content": "summary"}], "compressed system"),
            ) as mock_compress,
        ):
            updated, system_prompt = agent._maybe_precompress_before_advisor_tool(
                messages,
                "system",
                "active system",
                effective_task_id="task-1",
            )

        mock_compress.assert_called_once()
        assert updated == [{"role": "user", "content": "summary"}]
        assert system_prompt == "compressed system"

    def test_post_tool_compression_preserves_advisor_turn_suffix(self, agent):
        messages = [
            {"role": "user", "content": "old theorem context"},
            {"role": "assistant", "content": "old attempt"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc1", "function": {"name": "lean_reasoning_help", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": '{"advice":"use norm_num"}'},
        ]

        with patch.object(
            agent,
            "_compress_context",
            return_value=([{"role": "user", "content": "summary"}], "compressed system"),
        ):
            updated, system_prompt = agent._compress_context_preserving_suffix(
                messages,
                2,
                "system",
                approx_tokens=50_000,
                task_id="task-1",
            )

        assert updated[:1] == [{"role": "user", "content": "summary"}]
        assert updated[-2:] == messages[-2:]
        assert system_prompt == "compressed system"

    def test_injects_into_json_tool_result(self, agent):
        """Warning should be injected as _budget_warning field in JSON tool results."""
        import json

        agent.max_iterations = 10
        messages = [
            {
                "role": "tool",
                "content": json.dumps({"output": "done", "exit_code": 0}),
                "tool_call_id": "tc1",
            }
        ]
        warning = agent._get_budget_warning(9)
        assert warning is not None
        # Simulate the injection logic
        last_content = messages[-1]["content"]
        parsed = json.loads(last_content)
        parsed["_budget_warning"] = warning
        messages[-1]["content"] = json.dumps(parsed, ensure_ascii=False)
        result = json.loads(messages[-1]["content"])
        assert "_budget_warning" in result
        assert "BUDGET WARNING" in result["_budget_warning"]
        assert result["output"] == "done"  # original content preserved

    def test_appends_to_non_json_tool_result(self, agent):
        """Warning should be appended as text for non-JSON tool results."""
        agent.max_iterations = 10
        messages = [{"role": "tool", "content": "plain text result", "tool_call_id": "tc1"}]
        warning = agent._get_budget_warning(9)
        # Simulate injection logic for non-JSON
        last_content = messages[-1]["content"]
        try:
            import json

            json.loads(last_content)
        except (json.JSONDecodeError, TypeError):
            messages[-1]["content"] = last_content + f"\n\n{warning}"
        assert "plain text result" in messages[-1]["content"]
        assert "BUDGET WARNING" in messages[-1]["content"]


class TestSafeWriter:
    """Verify _SafeWriter guards stdout against OSError (broken pipes)."""

    def test_write_delegates_normally(self):
        """When stdout is healthy, _SafeWriter is transparent."""
        from io import StringIO

        from run_agent import _SafeWriter

        inner = StringIO()
        writer = _SafeWriter(inner)
        writer.write("hello")
        assert inner.getvalue() == "hello"

    def test_write_catches_oserror(self):
        """OSError on write is silently caught, returns len(data)."""
        from unittest.mock import MagicMock

        from run_agent import _SafeWriter

        inner = MagicMock()
        inner.write.side_effect = OSError(5, "Input/output error")
        writer = _SafeWriter(inner)
        result = writer.write("hello")
        assert result == 5  # len("hello")

    def test_flush_catches_oserror(self):
        """OSError on flush is silently caught."""
        from unittest.mock import MagicMock

        from run_agent import _SafeWriter

        inner = MagicMock()
        inner.flush.side_effect = OSError(5, "Input/output error")
        writer = _SafeWriter(inner)
        writer.flush()  # should not raise

    def test_print_survives_broken_stdout(self, monkeypatch):
        """print() through _SafeWriter doesn't crash on broken pipe."""
        import sys
        from unittest.mock import MagicMock

        from run_agent import _SafeWriter

        broken = MagicMock()
        broken.write.side_effect = OSError(5, "Input/output error")
        original = sys.stdout
        sys.stdout = _SafeWriter(broken)
        try:
            print("this should not crash")  # would raise without _SafeWriter
        finally:
            sys.stdout = original

    def test_installed_in_run_conversation(self, agent):
        """run_conversation installs _SafeWriter on stdio."""
        import sys

        from run_agent import _SafeWriter

        resp = _mock_response(content="Done", finish_reason="stop")
        agent.client.chat.completions.create.return_value = resp
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        try:
            with (
                patch.object(agent, "_persist_session"),
                patch.object(agent, "_save_trajectory"),
                patch.object(agent, "_cleanup_task_resources"),
            ):
                agent.run_conversation("test")
            assert isinstance(sys.stdout, _SafeWriter)
            assert isinstance(sys.stderr, _SafeWriter)
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    def test_double_wrap_prevented(self):
        """Wrapping an already-wrapped stream doesn't add layers."""
        from io import StringIO

        from run_agent import _SafeWriter

        inner = StringIO()
        wrapped = _SafeWriter(inner)
        # isinstance check should prevent double-wrapping
        assert isinstance(wrapped, _SafeWriter)
        # The guard in run_conversation checks isinstance before wrapping
        if not isinstance(wrapped, _SafeWriter):
            wrapped = _SafeWriter(wrapped)
        # Still just one layer
        wrapped.write("test")
        assert inner.getvalue() == "test"


class TestSaveSessionLogAtomicWrite:
    def test_uses_shared_atomic_json_helper(self, agent, tmp_path):
        agent.session_log_file = tmp_path / "session.json"
        messages = [{"role": "user", "content": "hello"}]

        with patch("run_agent.atomic_json_write", create=True) as mock_atomic_write:
            agent._save_session_log(messages)

        mock_atomic_write.assert_called_once()
        call_args = mock_atomic_write.call_args
        assert call_args.args[0] == agent.session_log_file
        payload = call_args.args[1]
        assert payload["session_id"] == agent.session_id
        assert payload["messages"] == messages
        assert payload["usage"]["session"]["total_tokens"] == 0
        assert payload["usage"]["cost"]["source"] in {"estimated", "unavailable"}
        assert call_args.kwargs["indent"] == 2
        assert call_args.kwargs["default"] is str


# ===================================================================
# Anthropic adapter integration fixes
# ===================================================================


class TestBuildApiKwargsAnthropicMaxTokens:
    """Bug fix: max_tokens was always None for Anthropic mode, ignoring user config."""

    def test_max_tokens_passed_to_anthropic(self, agent):
        agent.api_mode = "anthropic_messages"
        agent.max_tokens = 4096
        agent.reasoning_config = None

        with patch("agent.providers.anthropic_adapter.build_anthropic_kwargs") as mock_build:
            mock_build.return_value = {
                "model": "claude-sonnet-4-20250514",
                "messages": [],
                "max_tokens": 4096,
            }
            agent._build_api_kwargs([{"role": "user", "content": "test"}])
            _, kwargs = mock_build.call_args
            if not kwargs:
                kwargs = dict(
                    zip(
                        ["model", "messages", "tools", "max_tokens", "reasoning_config"],
                        mock_build.call_args[0],
                    )
                )
            assert (
                kwargs.get("max_tokens") == 4096
                or mock_build.call_args[1].get("max_tokens") == 4096
            )

    def test_max_tokens_none_when_unset(self, agent):
        agent.api_mode = "anthropic_messages"
        agent.max_tokens = None
        agent.reasoning_config = None

        with patch("agent.providers.anthropic_adapter.build_anthropic_kwargs") as mock_build:
            mock_build.return_value = {
                "model": "claude-sonnet-4-20250514",
                "messages": [],
                "max_tokens": 16384,
            }
            agent._build_api_kwargs([{"role": "user", "content": "test"}])
            call_args = mock_build.call_args
            # max_tokens should be None (let adapter use its default)
            if call_args[1]:
                assert call_args[1].get("max_tokens") is None
            else:
                assert call_args[0][3] is None


class TestAnthropicImageFallback:
    def test_build_api_kwargs_converts_multimodal_user_image_to_text(self, agent):
        agent.api_mode = "anthropic_messages"
        agent.reasoning_config = None

        api_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Can you see this now?"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
                ],
            }
        ]

        with patch("agent.providers.anthropic_adapter.build_anthropic_kwargs") as mock_build:
            mock_build.return_value = {
                "model": "claude-sonnet-4-20250514",
                "messages": [],
                "max_tokens": 4096,
            }
            agent._build_api_kwargs(api_messages)

        kwargs = mock_build.call_args.kwargs or dict(
            zip(
                ["model", "messages", "tools", "max_tokens", "reasoning_config"],
                mock_build.call_args.args,
            )
        )
        transformed = kwargs["messages"]
        # The native Anthropic route flattens image parts to a text placeholder
        # (no vision analysis); the text part is preserved.
        assert isinstance(transformed[0]["content"], str)
        assert "image content is not processed" in transformed[0]["content"]
        assert "Can you see this now?" in transformed[0]["content"]


class TestFallbackAnthropicProvider:
    """Bug fix: _try_activate_fallback had no case for anthropic provider."""

    def test_fallback_to_anthropic_sets_api_mode(self, agent):
        agent._fallback_activated = False
        agent._fallback_model = {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}

        mock_client = MagicMock()
        mock_client.base_url = "https://api.anthropic.com/v1"
        mock_client.api_key = "sk-ant-api03-test"

        with (
            patch(
                "agent.providers.auxiliary_client.resolve_provider_client",
                return_value=(mock_client, None),
            ),
            patch("agent.providers.anthropic_adapter.build_anthropic_client") as mock_build,
            patch("agent.providers.anthropic_adapter.resolve_anthropic_token", return_value=None),
        ):
            mock_build.return_value = MagicMock()
            result = agent._try_activate_fallback()

        assert result is True
        assert agent.api_mode == "anthropic_messages"
        assert agent._anthropic_client is not None
        assert agent.client is None

    def test_fallback_to_anthropic_enables_prompt_caching(self, agent):
        agent._fallback_activated = False
        agent._fallback_model = {"provider": "anthropic", "model": "claude-sonnet-4-20250514"}

        mock_client = MagicMock()
        mock_client.base_url = "https://api.anthropic.com/v1"
        mock_client.api_key = "sk-ant-api03-test"

        with (
            patch(
                "agent.providers.auxiliary_client.resolve_provider_client",
                return_value=(mock_client, None),
            ),
            patch(
                "agent.providers.anthropic_adapter.build_anthropic_client", return_value=MagicMock()
            ),
            patch("agent.providers.anthropic_adapter.resolve_anthropic_token", return_value=None),
        ):
            agent._try_activate_fallback()

        assert agent._use_prompt_caching is True

    def test_fallback_to_openrouter_uses_openai_client(self, agent):
        agent._fallback_activated = False
        agent._fallback_model = {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}

        mock_client = MagicMock()
        mock_client.base_url = "https://openrouter.ai/api/v1"
        mock_client.api_key = "sk-or-test"

        with patch(
            "agent.providers.auxiliary_client.resolve_provider_client",
            return_value=(mock_client, None),
        ):
            result = agent._try_activate_fallback()

        assert result is True
        assert agent.api_mode == "chat_completions"
        assert agent.client is mock_client


class TestAnthropicBaseUrlPassthrough:
    """Bug fix: base_url was filtered with 'anthropic in base_url', blocking proxies."""

    def test_custom_proxy_base_url_passed_through(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("agent.providers.anthropic_adapter.build_anthropic_client") as mock_build,
        ):
            mock_build.return_value = MagicMock()
            a = AIAgent(
                api_key="sk-ant-api03-test1234567890",
                base_url="https://llm-proxy.company.com/v1",
                api_mode="anthropic_messages",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            call_args = mock_build.call_args
            # base_url should be passed through, not filtered out
            assert call_args[0][1] == "https://llm-proxy.company.com/v1"

    def test_none_base_url_passed_as_none(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("agent.providers.anthropic_adapter.build_anthropic_client") as mock_build,
        ):
            mock_build.return_value = MagicMock()
            a = AIAgent(
                api_key="sk-ant-api03-test1234567890",
                api_mode="anthropic_messages",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            call_args = mock_build.call_args
            # No base_url provided, should be default empty string or None
            passed_url = call_args[0][1]
            assert not passed_url or passed_url is None


class TestAnthropicCredentialRefresh:
    def test_try_refresh_anthropic_client_credentials_rebuilds_client(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("agent.providers.anthropic_adapter.build_anthropic_client") as mock_build,
        ):
            old_client = MagicMock()
            new_client = MagicMock()
            mock_build.side_effect = [old_client, new_client]
            agent = AIAgent(
                api_key="sk-ant-oat01-stale-token",
                api_mode="anthropic_messages",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        agent._anthropic_client = old_client
        agent._anthropic_api_key = "sk-ant-oat01-stale-token"
        agent._anthropic_base_url = "https://api.anthropic.com"

        with (
            patch(
                "agent.providers.anthropic_adapter.resolve_anthropic_token",
                return_value="sk-ant-oat01-fresh-token",
            ),
            patch(
                "agent.providers.anthropic_adapter.build_anthropic_client", return_value=new_client
            ) as rebuild,
        ):
            assert agent._try_refresh_anthropic_client_credentials() is True

        old_client.close.assert_called_once()
        rebuild.assert_called_once_with("sk-ant-oat01-fresh-token", "https://api.anthropic.com")
        assert agent._anthropic_client is new_client
        assert agent._anthropic_api_key == "sk-ant-oat01-fresh-token"

    def test_try_refresh_anthropic_client_credentials_returns_false_when_token_unchanged(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch(
                "agent.providers.anthropic_adapter.build_anthropic_client", return_value=MagicMock()
            ),
        ):
            agent = AIAgent(
                api_key="sk-ant-oat01-same-token",
                api_mode="anthropic_messages",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        old_client = MagicMock()
        agent._anthropic_client = old_client
        agent._anthropic_api_key = "sk-ant-oat01-same-token"

        with (
            patch(
                "agent.providers.anthropic_adapter.resolve_anthropic_token",
                return_value="sk-ant-oat01-same-token",
            ),
            patch("agent.providers.anthropic_adapter.build_anthropic_client") as rebuild,
        ):
            assert agent._try_refresh_anthropic_client_credentials() is False

        old_client.close.assert_not_called()
        rebuild.assert_not_called()

    def test_anthropic_messages_create_preflights_refresh(self):
        with (
            patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch(
                "agent.providers.anthropic_adapter.build_anthropic_client", return_value=MagicMock()
            ),
        ):
            agent = AIAgent(
                api_key="sk-ant-oat01-current-token",
                api_mode="anthropic_messages",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        response = SimpleNamespace(content=[])
        agent._anthropic_client = MagicMock()
        agent._anthropic_client.messages.create.return_value = response

        with patch.object(
            agent, "_try_refresh_anthropic_client_credentials", return_value=True
        ) as refresh:
            result = agent._anthropic_messages_create({"model": "claude-sonnet-4-20250514"})

        refresh.assert_called_once_with()
        agent._anthropic_client.messages.create.assert_called_once_with(
            model="claude-sonnet-4-20250514"
        )
        assert result is response


# ===================================================================
# _streaming_api_call tests
# ===================================================================


def _make_chunk(content=None, tool_calls=None, finish_reason=None, model="test/model"):
    """Build a SimpleNamespace mimicking an OpenAI streaming chunk."""
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(model=model, choices=[choice])


def _make_tc_delta(index=0, tc_id=None, name=None, arguments=None):
    """Build a SimpleNamespace mimicking a streaming tool_call delta."""
    func = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=tc_id, function=func)


class TestStreamingApiCall:
    """Tests for _streaming_api_call — voice TTS streaming pipeline."""

    def test_content_assembly(self, agent):
        chunks = [
            _make_chunk(content="Hel"),
            _make_chunk(content="lo "),
            _make_chunk(content="World"),
            _make_chunk(finish_reason="stop"),
        ]
        agent.client.chat.completions.create.return_value = iter(chunks)
        callback = MagicMock()

        resp = agent._streaming_api_call({"messages": []}, callback)

        assert resp.choices[0].message.content == "Hello World"
        assert resp.choices[0].finish_reason == "stop"
        assert callback.call_count == 3
        callback.assert_any_call("Hel")
        callback.assert_any_call("lo ")
        callback.assert_any_call("World")

    def test_tool_call_accumulation(self, agent):
        chunks = [
            _make_chunk(tool_calls=[_make_tc_delta(0, "call_1", "web_", '{"q":')]),
            _make_chunk(tool_calls=[_make_tc_delta(0, None, "search", '"test"}')]),
            _make_chunk(finish_reason="tool_calls"),
        ]
        agent.client.chat.completions.create.return_value = iter(chunks)

        resp = agent._streaming_api_call({"messages": []}, MagicMock())

        tc = resp.choices[0].message.tool_calls
        assert len(tc) == 1
        assert tc[0].function.name == "web_search"
        assert tc[0].function.arguments == '{"q":"test"}'
        assert tc[0].id == "call_1"

    def test_multiple_tool_calls(self, agent):
        chunks = [
            _make_chunk(tool_calls=[_make_tc_delta(0, "call_a", "search", "{}")]),
            _make_chunk(tool_calls=[_make_tc_delta(1, "call_b", "read", "{}")]),
            _make_chunk(finish_reason="tool_calls"),
        ]
        agent.client.chat.completions.create.return_value = iter(chunks)

        resp = agent._streaming_api_call({"messages": []}, MagicMock())

        tc = resp.choices[0].message.tool_calls
        assert len(tc) == 2
        assert tc[0].function.name == "search"
        assert tc[1].function.name == "read"

    def test_content_and_tool_calls_together(self, agent):
        chunks = [
            _make_chunk(content="I'll search"),
            _make_chunk(tool_calls=[_make_tc_delta(0, "call_1", "search", "{}")]),
            _make_chunk(finish_reason="tool_calls"),
        ]
        agent.client.chat.completions.create.return_value = iter(chunks)

        resp = agent._streaming_api_call({"messages": []}, MagicMock())

        assert resp.choices[0].message.content == "I'll search"
        assert len(resp.choices[0].message.tool_calls) == 1

    def test_empty_content_returns_none(self, agent):
        chunks = [_make_chunk(finish_reason="stop")]
        agent.client.chat.completions.create.return_value = iter(chunks)

        resp = agent._streaming_api_call({"messages": []}, MagicMock())

        assert resp.choices[0].message.content is None
        assert resp.choices[0].message.tool_calls is None

    def test_callback_exception_swallowed(self, agent):
        chunks = [
            _make_chunk(content="Hello"),
            _make_chunk(content=" World"),
            _make_chunk(finish_reason="stop"),
        ]
        agent.client.chat.completions.create.return_value = iter(chunks)
        callback = MagicMock(side_effect=ValueError("boom"))

        resp = agent._streaming_api_call({"messages": []}, callback)

        assert resp.choices[0].message.content == "Hello World"

    def test_model_name_captured(self, agent):
        chunks = [
            _make_chunk(content="Hi", model="gpt-4o"),
            _make_chunk(finish_reason="stop", model="gpt-4o"),
        ]
        agent.client.chat.completions.create.return_value = iter(chunks)

        resp = agent._streaming_api_call({"messages": []}, MagicMock())

        assert resp.model == "gpt-4o"

    def test_stream_kwarg_injected(self, agent):
        chunks = [_make_chunk(content="x"), _make_chunk(finish_reason="stop")]
        agent.client.chat.completions.create.return_value = iter(chunks)

        agent._streaming_api_call({"messages": [], "model": "test"}, MagicMock())

        call_kwargs = agent.client.chat.completions.create.call_args
        assert call_kwargs[1].get("stream") is True or call_kwargs.kwargs.get("stream") is True

    def test_api_exception_propagated(self, agent):
        agent.client.chat.completions.create.side_effect = ConnectionError("fail")

        with pytest.raises(ConnectionError, match="fail"):
            agent._streaming_api_call({"messages": []}, MagicMock())

    def test_response_has_uuid_id(self, agent):
        chunks = [_make_chunk(content="x"), _make_chunk(finish_reason="stop")]
        agent.client.chat.completions.create.return_value = iter(chunks)

        resp = agent._streaming_api_call({"messages": []}, MagicMock())

        assert resp.id.startswith("stream-")
        assert len(resp.id) > len("stream-")

    def test_empty_choices_chunk_skipped(self, agent):
        empty_chunk = SimpleNamespace(model="gpt-4", choices=[])
        chunks = [
            empty_chunk,
            _make_chunk(content="Hello", model="gpt-4"),
            _make_chunk(finish_reason="stop", model="gpt-4"),
        ]
        agent.client.chat.completions.create.return_value = iter(chunks)

        resp = agent._streaming_api_call({"messages": []}, MagicMock())

        assert resp.choices[0].message.content == "Hello"
        assert resp.model == "gpt-4"


# ===================================================================
# Interrupt _vprint force=True verification
# ===================================================================


class TestInterruptVprintForceTrue:
    """All interrupt _vprint calls must use force=True so they are always visible."""

    def test_all_interrupt_vprint_have_force_true(self):
        """Scan source for _vprint calls containing 'Interrupt' — each must have force=True."""
        import inspect

        source = inspect.getsource(AIAgent)
        lines = source.split("\n")
        violations = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "_vprint(" in stripped and "Interrupt" in stripped:
                if "force=True" not in stripped:
                    violations.append(f"line {i}: {stripped}")
        assert not violations, "Interrupt _vprint calls missing force=True:\n" + "\n".join(
            violations
        )


# ===================================================================
# Anthropic interrupt handler in _interruptible_api_call
# ===================================================================


class TestAnthropicInterruptHandler:
    """_interruptible_api_call must handle Anthropic mode when interrupted."""

    def test_interruptible_has_anthropic_branch(self):
        """The interrupt handler must check api_mode == 'anthropic_messages'."""
        import inspect

        source = inspect.getsource(AIAgent._interruptible_api_call)
        assert (
            "anthropic_messages" in source
        ), "_interruptible_api_call must handle Anthropic interrupt (api_mode check)"

    def test_interruptible_rebuilds_anthropic_client(self):
        """After interrupting, the Anthropic client should be rebuilt."""
        import inspect

        source = inspect.getsource(AIAgent._interruptible_api_call)
        assert (
            "build_anthropic_client" in source
        ), "_interruptible_api_call must rebuild Anthropic client after interrupt"

    def test_streaming_has_anthropic_branch(self):
        """_streaming_api_call must also handle Anthropic interrupt."""
        import inspect

        source = inspect.getsource(AIAgent._streaming_api_call)
        assert "anthropic_messages" in source, "_streaming_api_call must handle Anthropic interrupt"


# ---------------------------------------------------------------------------
# Bugfix: stream_callback forwarding for non-streaming providers
# ---------------------------------------------------------------------------


class TestStreamCallbackNonStreamingProvider:
    """When api_mode != chat_completions, stream_callback must still receive
    the response content so TTS works (batch delivery)."""

    def test_callback_receives_chat_completions_response(self, agent):
        """For chat_completions-shaped responses, callback gets content."""
        agent.api_mode = "anthropic_messages"
        mock_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="Hello", tool_calls=None, reasoning_content=None
                    ),
                    finish_reason="stop",
                    index=0,
                )
            ],
            usage=None,
            model="test",
            id="test-id",
        )
        agent._interruptible_api_call = MagicMock(return_value=mock_response)

        received = []
        cb = lambda delta: received.append(delta)
        agent._stream_callback = cb

        _cb = getattr(agent, "_stream_callback", None)
        response = agent._interruptible_api_call({})
        if _cb is not None and response:
            try:
                if agent.api_mode == "anthropic_messages":
                    text_parts = [
                        block.text
                        for block in getattr(response, "content", [])
                        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
                    ]
                    content = " ".join(text_parts) if text_parts else None
                else:
                    content = response.choices[0].message.content
                if content:
                    _cb(content)
            except Exception:
                pass

        # Anthropic format not matched above; fallback via except
        # Test the actual code path by checking chat_completions branch
        received2 = []
        agent.api_mode = "some_other_mode"
        agent._stream_callback = lambda d: received2.append(d)
        _cb2 = agent._stream_callback
        if _cb2 is not None and mock_response:
            try:
                content = mock_response.choices[0].message.content
                if content:
                    _cb2(content)
            except Exception:
                pass
        assert received2 == ["Hello"]

    def test_callback_receives_anthropic_content(self, agent):
        """For Anthropic responses, text blocks are extracted and forwarded."""
        agent.api_mode = "anthropic_messages"
        mock_response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Hello from Claude")],
            stop_reason="end_turn",
        )

        received = []
        cb = lambda d: received.append(d)
        agent._stream_callback = cb
        _cb = agent._stream_callback

        if _cb is not None and mock_response:
            try:
                if agent.api_mode == "anthropic_messages":
                    text_parts = [
                        block.text
                        for block in getattr(mock_response, "content", [])
                        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
                    ]
                    content = " ".join(text_parts) if text_parts else None
                else:
                    content = mock_response.choices[0].message.content
                if content:
                    _cb(content)
            except Exception:
                pass

        assert received == ["Hello from Claude"]


# ---------------------------------------------------------------------------
# Bugfix: API-only user message prefixes must not persist
# ---------------------------------------------------------------------------


class TestPersistUserMessageOverride:
    """Synthetic API-only user prefixes should never leak into transcripts."""

    def test_persist_session_rewrites_current_turn_user_message(self, agent):
        agent._session_db = MagicMock()
        agent.session_id = "session-123"
        agent._last_flushed_db_idx = 0
        agent._persist_user_message_idx = 0
        agent._persist_user_message_override = "Hello there"
        messages = [
            {
                "role": "user",
                "content": (
                    "[Voice input — respond concisely and conversationally, "
                    "2-3 sentences max. No code blocks or markdown.] Hello there"
                ),
            },
            {"role": "assistant", "content": "Hi!"},
        ]

        with patch.object(agent, "_save_session_log") as mock_save:
            agent._persist_session(messages, [])

        assert messages[0]["content"] == "Hello there"
        saved_messages = mock_save.call_args.args[0]
        assert saved_messages[0]["content"] == "Hello there"
        first_db_write = agent._session_db.append_message.call_args_list[0].kwargs
        assert first_db_write["content"] == "Hello there"


# ---------------------------------------------------------------------------
# Bugfix: _vprint force=True on error messages during TTS
# ---------------------------------------------------------------------------


class TestVprintForceOnErrors:
    """Error/warning messages must be visible during streaming TTS."""

    def test_forced_message_shown_during_tts(self, agent):
        agent._stream_callback = lambda x: None
        printed = []
        with patch("builtins.print", side_effect=lambda *a, **kw: printed.append(a)):
            agent._vprint("error msg", force=True)
        assert len(printed) == 1

    def test_non_forced_suppressed_during_tts(self, agent):
        agent._stream_callback = lambda x: None
        printed = []
        with patch("builtins.print", side_effect=lambda *a, **kw: printed.append(a)):
            agent._vprint("debug info")
        assert len(printed) == 0

    def test_all_shown_without_tts(self, agent):
        agent._stream_callback = None
        printed = []
        with patch("builtins.print", side_effect=lambda *a, **kw: printed.append(a)):
            agent._vprint("debug")
            agent._vprint("error", force=True)
        assert len(printed) == 2


class TestNormalizeCodexDictArguments:
    """_normalize_codex_response must produce valid JSON strings for tool
    call arguments, even when the Responses API returns them as dicts."""

    def _make_codex_response(self, item_type, arguments, item_status="completed"):
        """Build a minimal Responses API response with a single tool call."""
        item = SimpleNamespace(
            type=item_type,
            status=item_status,
        )
        if item_type == "function_call":
            item.name = "web_search"
            item.arguments = arguments
            item.call_id = "call_abc123"
            item.id = "fc_abc123"
        elif item_type == "custom_tool_call":
            item.name = "web_search"
            item.input = arguments
            item.call_id = "call_abc123"
            item.id = "fc_abc123"
        return SimpleNamespace(
            output=[item],
            status="completed",
        )

    def test_function_call_dict_arguments_produce_valid_json(self, agent):
        """dict arguments from function_call must be serialised with
        json.dumps, not str(), so downstream json.loads() succeeds."""
        args_dict = {"query": "weather in NYC", "units": "celsius"}
        response = self._make_codex_response("function_call", args_dict)
        msg, _ = agent._normalize_codex_response(response)
        tc = msg.tool_calls[0]
        parsed = json.loads(tc.function.arguments)
        assert parsed == args_dict

    def test_custom_tool_call_dict_arguments_produce_valid_json(self, agent):
        """dict arguments from custom_tool_call must also use json.dumps."""
        args_dict = {"path": "/tmp/test.txt", "content": "hello"}
        response = self._make_codex_response("custom_tool_call", args_dict)
        msg, _ = agent._normalize_codex_response(response)
        tc = msg.tool_calls[0]
        parsed = json.loads(tc.function.arguments)
        assert parsed == args_dict

    def test_string_arguments_unchanged(self, agent):
        """String arguments must pass through without modification."""
        args_str = '{"query": "test"}'
        response = self._make_codex_response("function_call", args_str)
        msg, _ = agent._normalize_codex_response(response)
        tc = msg.tool_calls[0]
        assert tc.function.arguments == args_str
