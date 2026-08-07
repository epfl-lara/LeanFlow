"""Tests for context compression, provider fallback, and local handoffs."""

import logging
from unittest.mock import MagicMock, patch

import pytest

from agent.compression.context_compressor import (
    LEGACY_SUMMARY_PREFIX,
    STALE_TOOL_OUTPUT_MARKER,
    SUMMARY_PREFIX,
    ContextCompressor,
)
from agent.providers.isolated_auxiliary import AuxiliaryTextResponse


@pytest.fixture()
def compressor():
    """Create a ContextCompressor with mocked dependencies."""
    with patch(
        "agent.compression.context_compressor.get_model_context_length", return_value=100000
    ):
        c = ContextCompressor(
            model="test/model",
            threshold_percent=0.85,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )
        return c


class TestShouldCompress:
    def test_below_threshold(self, compressor):
        compressor.last_prompt_tokens = 50000
        assert compressor.should_compress() is False

    def test_above_threshold(self, compressor):
        compressor.last_prompt_tokens = 90000
        assert compressor.should_compress() is True

    def test_exact_threshold(self, compressor):
        compressor.last_prompt_tokens = 85000
        assert compressor.should_compress() is True

    def test_explicit_tokens(self, compressor):
        assert compressor.should_compress(prompt_tokens=90000) is True
        assert compressor.should_compress(prompt_tokens=50000) is False


class TestShouldCompressPreflight:
    def test_short_messages(self, compressor):
        msgs = [{"role": "user", "content": "short"}]
        assert compressor.should_compress_preflight(msgs) is False

    def test_long_messages(self, compressor):
        # Each message ~100k chars / 4 = 25k tokens, need >85k threshold
        msgs = [{"role": "user", "content": "x" * 400000}]
        assert compressor.should_compress_preflight(msgs) is True


class TestUpdateFromResponse:
    def test_updates_fields(self, compressor):
        compressor.update_from_response(
            {
                "prompt_tokens": 5000,
                "completion_tokens": 1000,
                "total_tokens": 6000,
            }
        )
        assert compressor.last_prompt_tokens == 5000
        assert compressor.last_completion_tokens == 1000
        assert compressor.last_total_tokens == 6000

    def test_missing_fields_default_zero(self, compressor):
        compressor.update_from_response({})
        assert compressor.last_prompt_tokens == 0


class TestGetStatus:
    def test_returns_expected_keys(self, compressor):
        status = compressor.get_status()
        assert "last_prompt_tokens" in status
        assert "threshold_tokens" in status
        assert "percent_threshold_tokens" in status
        assert "base_threshold_tokens" in status
        assert "absolute_threshold_tokens" in status
        assert "context_length" in status
        assert "usage_percent" in status
        assert "compression_count" in status

    def test_usage_percent_calculation(self, compressor):
        compressor.last_prompt_tokens = 50000
        status = compressor.get_status()
        assert status["usage_percent"] == 50.0

    def test_absolute_cap_description_is_truthful(self):
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=200_000,
        ):
            compressor = ContextCompressor(
                model="test",
                threshold_percent=0.75,
                absolute_threshold_tokens=96_000,
                quiet_mode=True,
            )

        assert compressor.threshold_tokens == 96_000
        assert "managed cap 96,000 = 48%" in compressor.threshold_description()
        assert "base policy 75% = 150,000" in compressor.threshold_description()

    def test_output_reserve_description_does_not_claim_percentage_equality(self):
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100_000,
        ):
            compressor = ContextCompressor(
                model="test",
                threshold_percent=0.75,
                reserved_output_tokens=40_000,
                quiet_mode=True,
            )

        assert compressor.threshold_tokens == 60_000
        assert "base threshold 60,000 after output reserve" in compressor.threshold_description()
        assert "percentage policy 75% = 75,000" in compressor.threshold_description()


class TestCompress:
    def _make_messages(self, n):
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"} for i in range(n)
        ]

    def test_too_few_messages_returns_unchanged(self, compressor):
        msgs = self._make_messages(4)  # protect_first=2 + protect_last=2 + 1 = 5 needed
        result = compressor.compress(msgs)
        assert result == msgs

    def test_truncation_fallback_no_client(self, compressor):
        # Provider failure must still leave a deterministic handoff.
        msgs = [{"role": "system", "content": "System prompt"}] + self._make_messages(10)
        with patch(
            "agent.compression.context_compressor.call_llm", side_effect=RuntimeError("No provider")
        ):
            result = compressor.compress(msgs)
        assert len(result) < len(msgs)
        # Should keep system message and last N
        assert result[0]["role"] == "system"
        assert any(
            "Deterministic Recovery Handoff" in str(message.get("content", ""))
            for message in result
        )
        assert compressor.compression_count == 1

    def test_generator_none_still_inserts_local_handoff(self, compressor):
        msgs = self._make_messages(10)

        with patch.object(compressor, "_generate_summary", return_value=None):
            result = compressor.compress(msgs)

        summaries = [
            str(message.get("content", ""))
            for message in result
            if str(message.get("content", "")).startswith(SUMMARY_PREFIX)
        ]
        assert len(summaries) == 1
        assert "Deterministic Recovery Handoff" in summaries[0]

    def test_compression_increments_count(self, compressor):
        msgs = self._make_messages(10)
        with patch(
            "agent.compression.context_compressor.call_llm", side_effect=RuntimeError("No provider")
        ):
            compressor.compress(msgs)
        assert compressor.compression_count == 1
        with patch(
            "agent.compression.context_compressor.call_llm", side_effect=RuntimeError("No provider")
        ):
            compressor.compress(msgs)
        assert compressor.compression_count == 2

    def test_protects_first_and_last(self, compressor):
        msgs = self._make_messages(10)
        with patch(
            "agent.compression.context_compressor.call_llm", side_effect=RuntimeError("No provider")
        ):
            result = compressor.compress(msgs)
        # First 2 messages should be preserved (protect_first_n=2)
        # Last 2 messages should be preserved (protect_last_n=2)
        assert result[-1]["content"] == msgs[-1]["content"]
        assert result[-2]["content"] == msgs[-2]["content"]

    def test_prunes_stale_tool_outputs_but_keeps_recent_ones(self):
        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(
                model="test/model",
                quiet_mode=True,
                prune_tool_output=True,
                prune_keep_recent_user_turns=2,
            )

        pruned, pruned_count = c._prune_stale_tool_outputs(
            [
                {"role": "assistant", "content": "old assistant"},
                {"role": "tool", "content": "old tool output"},
                {"role": "user", "content": "middle user"},
                {"role": "assistant", "content": "middle assistant"},
                {"role": "tool", "content": "recent tool output"},
                {"role": "user", "content": "latest user"},
                {"role": "assistant", "content": "latest assistant"},
            ]
        )

        assert pruned_count == 1
        assert pruned[1]["content"] == STALE_TOOL_OUTPUT_MARKER
        assert pruned[4]["content"] == "recent tool output"


class TestGenerateSummaryNoneContent:
    """Regression: content=None (from tool-call-only assistant messages) must not crash."""

    def test_none_content_does_not_crash(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: tool calls happened"

        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(model="test", quiet_mode=True)

        messages = [
            {"role": "user", "content": "do something"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "search"}}],
            },
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": None},
            {"role": "user", "content": "thanks"},
        ]

        with patch("agent.compression.context_compressor.call_llm", return_value=mock_response):
            summary = c._generate_summary(messages)
        assert isinstance(summary, str)
        assert summary.startswith(SUMMARY_PREFIX)

    def test_none_content_in_system_message_compress(self):
        """System message with content=None should not crash during compress."""
        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(
                model="test", quiet_mode=True, protect_first_n=2, protect_last_n=2
            )

        msgs = [{"role": "system", "content": None}] + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(10)
        ]
        with patch(
            "agent.compression.context_compressor.call_llm", side_effect=RuntimeError("No provider")
        ):
            result = c.compress(msgs)
        assert len(result) < len(msgs)


class TestNonStringContent:
    """Regression: content as dict (e.g., llama.cpp tool calls) must not crash."""

    def test_dict_content_coerced_to_string(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = {"text": "some summary"}

        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(model="test", quiet_mode=True)

        messages = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok"},
        ]

        with patch("agent.compression.context_compressor.call_llm", return_value=mock_response):
            summary = c._generate_summary(messages)
        assert isinstance(summary, str)
        assert summary.startswith(SUMMARY_PREFIX)

    def test_none_content_uses_main_then_local_handoff(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = None

        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(model="test", quiet_mode=True)

        messages = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "ok"},
        ]

        with patch("agent.compression.context_compressor.call_llm", return_value=mock_response):
            summary = c._generate_summary(messages)
        assert summary.startswith(SUMMARY_PREFIX)
        assert "Deterministic Recovery Handoff" in summary
        assert "do something" in summary


class TestSummaryProviderFallback:
    @staticmethod
    def _response(content):
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = content
        return response

    @staticmethod
    def _messages():
        return [
            {"role": "user", "content": "Prove the remaining Lean theorem."},
            {"role": "assistant", "content": "The first route did not elaborate."},
        ]

    def test_auxiliary_success_does_not_invoke_main_fallback(self):
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="gpt-5.3-codex",
                main_provider="openai-codex",
                main_api_mode="codex_responses",
                quiet_mode=True,
            )
        response = self._response("Auxiliary handoff")

        with patch(
            "agent.compression.context_compressor.call_llm", return_value=response
        ) as mock_call:
            summary = compressor._generate_summary(self._messages())

        assert summary == f"{SUMMARY_PREFIX}\nAuxiliary handoff"
        assert mock_call.call_count == 1
        assert mock_call.call_args.kwargs["task"] == "compression"

    def test_codex_main_fallback_uses_responses_adapter_route(self):
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="gpt-5.3-codex",
                main_provider="custom",
                main_api_mode="codex_responses",
                base_url="https://chatgpt.com/backend-api/codex",
                api_key="codex-secret-that-must-not-be-forwarded",
                quiet_mode=True,
            )
        response = self._response("Main Codex handoff")

        with patch(
            "agent.compression.context_compressor.call_llm",
            side_effect=[ConnectionError("auxiliary unavailable"), response],
        ) as mock_call:
            summary = compressor._generate_summary(self._messages())

        assert summary == f"{SUMMARY_PREFIX}\nMain Codex handoff"
        assert mock_call.call_count == 2
        fallback_kwargs = mock_call.call_args_list[1].kwargs
        assert fallback_kwargs["task"] is None
        assert fallback_kwargs["provider"] == "openai-codex"
        assert fallback_kwargs["model"] == "gpt-5.3-codex"
        assert "base_url" not in fallback_kwargs
        assert "api_key" not in fallback_kwargs

    def test_auxiliary_exception_opens_instance_circuit_for_later_compactions(self):
        """A dead summary route pays its timeout once per compressor, not per tool turn."""
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="gpt-5.3-codex",
                main_provider="openai-codex",
                main_api_mode="codex_responses",
                quiet_mode=True,
            )
        first_main = self._response("First main handoff")
        second_main = self._response("Second main handoff")
        events: list[tuple[str, dict]] = []

        with (
            patch(
                "agent.compression.context_compressor.call_llm",
                side_effect=[TimeoutError("auxiliary timed out"), first_main, second_main],
            ) as mock_call,
            patch(
                "agent.compression.summary_handoff._emit_workflow_event",
                side_effect=lambda event, _message, **details: events.append((event, details)),
            ),
        ):
            first = compressor._generate_summary(self._messages())
            second = compressor._generate_summary(self._messages())

        assert first == f"{SUMMARY_PREFIX}\nFirst main handoff"
        assert second == f"{SUMMARY_PREFIX}\nSecond main handoff"
        assert mock_call.call_count == 3
        assert mock_call.call_args_list[0].kwargs["task"] == "compression"
        assert mock_call.call_args_list[0].kwargs["timeout"] == 10.0
        assert mock_call.call_args_list[1].kwargs["task"] is None
        assert mock_call.call_args_list[2].kwargs["task"] is None
        assert compressor._summary_auxiliary_failure == "TimeoutError"
        assert any(
            event == "compression-summary-route-skipped" and details["outcome"] == "circuit-open"
            for event, details in events
        )
        failed = next(
            details
            for event, details in events
            if event == "compression-summary-route-finished" and details["route"] == "auxiliary"
        )
        assert failed["outcome"] == "failed"
        assert failed["failure_type"] == "TimeoutError"

    def test_production_summary_routes_use_killable_process_boundary(self):
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="gpt-5.3-codex",
                main_provider="openai-codex",
                main_api_mode="codex_responses",
                quiet_mode=True,
            )

        with patch(
            "agent.compression.summary_handoff.run_isolated_auxiliary_text",
            return_value=AuxiliaryTextResponse("isolated handoff", "small-model"),
        ) as isolated:
            summary = compressor._generate_summary(self._messages())

        assert summary == f"{SUMMARY_PREFIX}\nisolated handoff"
        assert isolated.call_count == 1
        assert isolated.call_args.kwargs["task"] == "compression"
        assert isolated.call_args.kwargs["timeout"] == 10.0

    def test_isolated_main_fallback_preserves_explicit_custom_route(self):
        secret = "ordinary-unpatterned-custom-credential"
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="custom-proof-model",
                main_provider="custom",
                main_api_mode="chat_completions",
                base_url="https://custom.invalid/v1",
                api_key=secret,
                quiet_mode=True,
            )

        with patch(
            "agent.compression.summary_handoff.run_isolated_auxiliary_text",
            side_effect=[
                TimeoutError("auxiliary"),
                AuxiliaryTextResponse("custom main handoff", "custom-proof-model"),
            ],
        ) as isolated:
            summary = compressor._generate_summary(self._messages())

        assert summary == f"{SUMMARY_PREFIX}\ncustom main handoff"
        main_call = isolated.call_args_list[1].kwargs
        assert main_call["task"] is None
        assert main_call["provider"] == "custom"
        assert main_call["model"] == "custom-proof-model"
        assert main_call["base_url"] == "https://custom.invalid/v1"
        assert main_call["api_key"] == secret
        assert main_call["timeout"] == 30.0

    def test_both_failed_routes_open_circuits_for_later_compactions(self):
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="gpt-5.3-codex",
                main_provider="openai-codex",
                main_api_mode="codex_responses",
                quiet_mode=True,
            )

        with patch(
            "agent.compression.summary_handoff.run_isolated_auxiliary_text",
            side_effect=[TimeoutError("auxiliary"), TimeoutError("main")],
        ) as isolated:
            first = compressor._generate_summary(self._messages())
            second = compressor._generate_summary(self._messages())

        assert "Deterministic Recovery Handoff" in first
        assert "Deterministic Recovery Handoff" in second
        assert isolated.call_count == 2
        assert compressor._summary_auxiliary_failure == "TimeoutError"
        assert compressor._summary_main_failure == "TimeoutError"

    def test_main_circuit_resets_only_when_effective_route_changes(self):
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="gpt-5.3-codex",
                main_provider="openai-codex",
                main_api_mode="codex_responses",
                base_url="https://first.invalid",
                api_key="first-unpatterned-key",
                quiet_mode=True,
            )
        compressor._disable_summary_main("TimeoutError")

        # Codex ignores the custom endpoint/key in the summary route, and the
        # provider alias resolves to the same effective Responses adapter.
        compressor.bind_main_summary_route(
            model="gpt-5.3-codex",
            provider="codex",
            api_mode="codex_responses",
            base_url="https://second.invalid",
            api_key="second-unpatterned-key",
        )
        assert compressor._summary_main_failure == "TimeoutError"

        compressor.bind_main_summary_route(
            model="gpt-5.4-codex",
            provider="openai-codex",
            api_mode="codex_responses",
            base_url="https://second.invalid",
            api_key="second-unpatterned-key",
        )
        assert compressor._summary_main_failure == ""

    def test_custom_main_fallback_carries_explicit_endpoint(self):
        secret = "sk-custom-compression-secret-12345"
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="research-prover",
                main_provider="custom",
                main_api_mode="chat_completions",
                base_url="https://inference.example.test/v1",
                api_key=secret,
                quiet_mode=True,
            )
        response = self._response("Main custom handoff")

        with patch(
            "agent.compression.context_compressor.call_llm",
            side_effect=[TimeoutError("auxiliary timed out"), response],
        ) as mock_call:
            summary = compressor._generate_summary(self._messages())

        assert summary == f"{SUMMARY_PREFIX}\nMain custom handoff"
        fallback_kwargs = mock_call.call_args_list[1].kwargs
        assert fallback_kwargs["provider"] == "custom"
        assert fallback_kwargs["model"] == "research-prover"
        assert fallback_kwargs["base_url"] == "https://inference.example.test/v1"
        assert fallback_kwargs["api_key"] == secret

    @pytest.mark.parametrize(
        "unusable_response",
        [
            pytest.param(None, id="empty-content"),
            pytest.param("wrapper", id="wrapper-only"),
            pytest.param("missing", id="missing-choices"),
        ],
    )
    def test_unusable_auxiliary_response_invokes_main_fallback(self, unusable_response):
        if unusable_response == "missing":
            first_response = MagicMock(choices=[])
        else:
            first_response = self._response(
                LEGACY_SUMMARY_PREFIX if unusable_response == "wrapper" else None
            )
        second_response = self._response("Recovered handoff")
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="main-model",
                main_provider="openrouter",
                main_api_mode="chat_completions",
                quiet_mode=True,
            )

        with patch(
            "agent.compression.context_compressor.call_llm",
            side_effect=[first_response, second_response],
        ) as mock_call:
            summary = compressor._generate_summary(self._messages())

        assert summary == f"{SUMMARY_PREFIX}\nRecovered handoff"
        assert mock_call.call_count == 2
        assert mock_call.call_args_list[1].kwargs["provider"] == "openrouter"
        assert compressor._summary_auxiliary_failure == "UnusableSummaryResponse"

    def test_both_failures_use_bounded_deterministic_redacted_handoff(self, caplog):
        secret = "sk-compression-secret-123456789"
        with patch(
            "agent.compression.context_compressor.get_model_context_length",
            return_value=100000,
        ):
            compressor = ContextCompressor(
                model="main-model",
                main_provider="custom",
                main_api_mode="chat_completions",
                base_url="https://inference.example.test/v1",
                api_key=secret,
                summary_target_tokens=500,
                quiet_mode=True,
            )
        messages = [
            {
                "role": "user",
                "content": f"Keep proving the residual theorem. OPENAI_API_KEY={secret}",
            },
            {"role": "assistant", "content": "Lean rejected exact_mod_cast at line 42."},
            {"role": "tool", "content": "kernel verdict: declaration uses sorry"},
        ]

        with (
            caplog.at_level(logging.WARNING),
            patch(
                "agent.compression.context_compressor.call_llm",
                side_effect=[
                    ConnectionError(f"connection used {secret}"),
                    RuntimeError(f"Authorization: Bearer {secret}"),
                ],
            ) as mock_call,
        ):
            summary = compressor._generate_summary(messages)

        assert mock_call.call_count == 2
        assert summary.startswith(SUMMARY_PREFIX)
        assert "Deterministic Recovery Handoff" in summary
        assert "residual theorem" in summary
        assert "kernel verdict" in summary
        assert len(summary) <= 2_000
        assert secret not in summary
        assert secret not in caplog.text
        assert "connection used" not in caplog.text
        assert "Authorization" not in caplog.text
        assert "ConnectionError" in caplog.text
        assert "RuntimeError" in caplog.text


class TestSummaryPrefixNormalization:
    def test_compaction_handoff_forbids_bootstrap_restarts(self):
        assert "Compaction does not start a new task" in SUMMARY_PREFIX
        assert "do not repeat capability discovery" in SUMMARY_PREFIX
        assert "bootstrap inspection" in SUMMARY_PREFIX

    def test_legacy_prefix_is_replaced(self):
        summary = ContextCompressor._with_summary_prefix("[CONTEXT SUMMARY]: did work")
        assert summary == f"{SUMMARY_PREFIX}\ndid work"

    def test_existing_new_prefix_is_not_duplicated(self):
        summary = ContextCompressor._with_summary_prefix(f"{SUMMARY_PREFIX}\ndid work")
        assert summary == f"{SUMMARY_PREFIX}\ndid work"


class TestCompressWithClient:
    def test_summarization_path(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: stuff happened"
        mock_client.chat.completions.create.return_value = mock_response

        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(model="test", quiet_mode=True)

        msgs = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(10)
        ]
        with patch("agent.compression.context_compressor.call_llm", return_value=mock_response):
            result = c.compress(msgs)

        # Should have summary message in the middle
        contents = [m.get("content", "") for m in result]
        assert any(c.startswith(SUMMARY_PREFIX) for c in contents)
        assert len(result) < len(msgs)

    def test_summarization_does_not_split_tool_call_pairs(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: compressed middle"
        mock_client.chat.completions.create.return_value = mock_response

        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(
                model="test",
                quiet_mode=True,
                protect_first_n=3,
                protect_last_n=4,
            )

        msgs = [
            {"role": "user", "content": "Could you address the reviewer comments in PR#71"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "skill_view", "arguments": "{}"},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "skill_view", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "output a"},
            {"role": "tool", "tool_call_id": "call_b", "content": "output b"},
            {"role": "user", "content": "later 1"},
            {"role": "assistant", "content": "later 2"},
            {"role": "tool", "tool_call_id": "call_x", "content": "later output"},
            {"role": "assistant", "content": "later 3"},
            {"role": "user", "content": "later 4"},
        ]

        with patch("agent.compression.context_compressor.call_llm", return_value=mock_response):
            result = c.compress(msgs)

        answered_ids = {
            msg.get("tool_call_id")
            for msg in result
            if msg.get("role") == "tool" and msg.get("tool_call_id")
        }
        for msg in result:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    assert tc["id"] in answered_ids

    def test_summary_role_avoids_consecutive_user_messages(self):
        """Summary role should alternate with the last head message to avoid consecutive same-role messages."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: stuff happened"
        mock_client.chat.completions.create.return_value = mock_response

        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(
                model="test", quiet_mode=True, protect_first_n=2, protect_last_n=2
            )

        # Last head message (index 1) is "assistant" → summary should be "user"
        msgs = [
            {"role": "user", "content": "msg 0"},
            {"role": "assistant", "content": "msg 1"},
            {"role": "user", "content": "msg 2"},
            {"role": "assistant", "content": "msg 3"},
            {"role": "user", "content": "msg 4"},
            {"role": "assistant", "content": "msg 5"},
        ]
        with patch("agent.compression.context_compressor.call_llm", return_value=mock_response):
            result = c.compress(msgs)
        summary_msg = [m for m in result if (m.get("content") or "").startswith(SUMMARY_PREFIX)]
        assert len(summary_msg) == 1
        assert summary_msg[0]["role"] == "user"

    def test_summary_role_avoids_consecutive_user_when_head_ends_with_user(self):
        """When last head message is 'user', summary must be 'assistant' to avoid two consecutive user messages."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: stuff happened"
        mock_client.chat.completions.create.return_value = mock_response

        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(
                model="test", quiet_mode=True, protect_first_n=3, protect_last_n=2
            )

        # Last head message (index 2) is "user" → summary should be "assistant"
        msgs = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "msg 1"},
            {"role": "user", "content": "msg 2"},  # last head — user
            {"role": "assistant", "content": "msg 3"},
            {"role": "user", "content": "msg 4"},
            {"role": "assistant", "content": "msg 5"},
            {"role": "user", "content": "msg 6"},
            {"role": "assistant", "content": "msg 7"},
        ]
        with patch("agent.compression.context_compressor.call_llm", return_value=mock_response):
            result = c.compress(msgs)
        summary_msg = [m for m in result if (m.get("content") or "").startswith(SUMMARY_PREFIX)]
        assert len(summary_msg) == 1
        assert summary_msg[0]["role"] == "assistant"

    def test_summarization_does_not_start_tail_with_tool_outputs(self):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "[CONTEXT SUMMARY]: compressed middle"

        with patch(
            "agent.compression.context_compressor.get_model_context_length", return_value=100000
        ):
            c = ContextCompressor(
                model="test",
                quiet_mode=True,
                protect_first_n=2,
                protect_last_n=3,
            )

        msgs = [
            {"role": "user", "content": "earlier 1"},
            {"role": "assistant", "content": "earlier 2"},
            {"role": "user", "content": "earlier 3"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_c",
                        "type": "function",
                        "function": {"name": "search_files", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_c", "content": "output c"},
            {"role": "user", "content": "latest user"},
        ]

        with patch("agent.compression.context_compressor.call_llm", return_value=mock_response):
            result = c.compress(msgs)

        called_ids = {
            tc["id"]
            for msg in result
            if msg.get("role") == "assistant" and msg.get("tool_calls")
            for tc in msg["tool_calls"]
        }
        for msg in result:
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                assert msg["tool_call_id"] in called_ids
