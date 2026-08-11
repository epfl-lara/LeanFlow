"""Focused tests for the CompressionPolicy collaborator extracted from AIAgent.

Covers the resolve-accessor materialization (real agents, ``__new__`` agents,
MagicMock fakes), the AIAgent delegation identity (wrappers forward to the
collaborator), and a few behavior checks for the moved gating logic: the
pre-send threshold gate + retry loop, the pre-advisor reserve gate, the
suffix-preserving split, and the payload-size estimate.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.compression.compression_policy import CompressionPolicy
from run_agent import AIAgent, _resolve_compression_policy


def _make_tool_defs(*names):
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


# ── resolve accessor + delegation identity ──────────────────────────────────


def test_init_builds_real_compression_policy(agent):
    assert isinstance(agent._compression_policy_obj, CompressionPolicy)
    assert agent._compression_policy_obj._agent is agent


def test_resolve_returns_cached_instance(agent):
    policy = _resolve_compression_policy(agent)
    assert policy is agent._compression_policy_obj
    assert _resolve_compression_policy(agent) is policy


def test_resolve_materializes_for_new_agent():
    """Agents built via __new__ (bypassing __init__) get a fresh real policy."""
    a = AIAgent.__new__(AIAgent)
    policy = _resolve_compression_policy(a)
    assert isinstance(policy, CompressionPolicy)
    assert policy._agent is a
    assert a._compression_policy_obj is policy


def test_resolve_materializes_for_mock_agent():
    """A MagicMock fake agent gets a real CompressionPolicy (isinstance guard)."""
    fake = MagicMock()
    policy = _resolve_compression_policy(fake)
    assert isinstance(policy, CompressionPolicy)
    assert policy._agent is fake


def test_wrappers_delegate_to_collaborator(agent):
    sentinel = (["m"], "sys")
    with patch.object(
        agent._compression_policy_obj, "compress_context", return_value=sentinel
    ) as m:
        out = agent._compress_context(
            [{"role": "user", "content": "x"}], "sys", approx_tokens=10, task_id="t"
        )
    m.assert_called_once_with(
        [{"role": "user", "content": "x"}], "sys", approx_tokens=10, task_id="t"
    )
    assert out is sentinel

    with patch.object(
        agent._compression_policy_obj, "api_payload_size_estimate", return_value=(3, 12)
    ) as m:
        assert agent._api_payload_size_estimate([{"a": 1}]) == (3, 12)
    m.assert_called_once_with([{"a": 1}])


# ── api_payload_size_estimate behavior ──────────────────────────────────────


def test_api_payload_size_estimate_divides_chars_by_four(agent):
    api_messages = [{"role": "user", "content": "x" * 40}]
    expected_chars = sum(len(str(msg)) for msg in api_messages)
    tokens, chars = agent._api_payload_size_estimate(api_messages)
    assert chars == expected_chars
    assert tokens == expected_chars // 4


# ── pre-send threshold gate ─────────────────────────────────────────────────


def test_pre_send_skips_when_compression_disabled(agent):
    agent.compression_enabled = False
    api_messages = [{"role": "user", "content": "hi"}]
    with patch.object(agent, "_compress_context") as mock_compress:
        out = agent._maybe_compress_before_api_send(
            [{"role": "user", "content": "hi"}],
            "sys",
            "sys",
            api_messages=api_messages,
            approx_tokens=999_999,
            task_id="t",
        )
    mock_compress.assert_not_called()
    assert out[0] == [{"role": "user", "content": "hi"}]
    assert out[2] is api_messages


def test_pre_send_skips_below_threshold(agent):
    agent.compression_enabled = True
    agent.context_compressor.threshold_tokens = 1_000
    api_messages = [{"role": "user", "content": "hi"}]
    with patch.object(agent, "_compress_context") as mock_compress:
        out = agent._maybe_compress_before_api_send(
            [{"role": "user", "content": "hi"}],
            "sys",
            "sys",
            api_messages=api_messages,
            approx_tokens=10,
            task_id="t",
        )
    mock_compress.assert_not_called()
    assert out[3] == 10


def test_pre_send_compresses_and_routes_through_agent_wrapper(agent):
    """Over threshold compresses via agent._compress_context (so patches intercept)."""
    agent.compression_enabled = True
    agent.context_compressor.threshold_tokens = 1_000
    api_messages = [{"role": "user", "content": "x" * 8_000}]
    with (
        patch.object(
            agent,
            "_compress_context",
            return_value=([{"role": "user", "content": "compact"}], "compressed sys"),
        ) as mock_compress,
        patch.object(
            agent,
            "_build_api_messages_for_turn",
            return_value=[{"role": "user", "content": "compact"}],
        ),
    ):
        new_messages, new_system, _, new_tokens, _ = agent._maybe_compress_before_api_send(
            [{"role": "user", "content": "x" * 8_000}],
            "sys",
            "sys",
            api_messages=api_messages,
            approx_tokens=5_000,
            task_id="t",
        )
    mock_compress.assert_called()
    assert mock_compress.call_args.kwargs["approx_tokens"] == 5_000
    assert new_messages == [{"role": "user", "content": "compact"}]
    assert new_system == "compressed sys"
    assert new_tokens < 1_000


# ── pre-advisor reserve gate ────────────────────────────────────────────────


def test_pre_advisor_noop_without_reserve(agent):
    agent.compression_enabled = True
    agent._advisor_result_context_reserve_tokens = 0
    messages = [{"role": "user", "content": "hi"}]
    with patch.object(agent, "_compress_context") as mock_compress:
        out_messages, out_system = agent._maybe_precompress_before_advisor_tool(
            messages,
            "sys",
            "active sys",
            effective_task_id="t",
        )
    mock_compress.assert_not_called()
    assert out_messages is messages
    assert out_system == "active sys"


def test_pre_advisor_compresses_fresh_history_only_once(agent):
    """Do not recompress a new handoff when the reserve still exceeds the cap."""
    agent.compression_enabled = True
    agent._advisor_result_context_reserve_tokens = 90_000
    messages = [
        {"role": "user", "content": "old theorem context"},
        {"role": "assistant", "content": "old proof attempt"},
    ]
    with (
        patch.object(agent.context_compressor, "should_compress", return_value=True),
        patch.object(
            agent,
            "_compress_context",
            return_value=([{"role": "user", "content": "fresh handoff"}], "compressed sys"),
        ) as mock_compress,
    ):
        out_messages, out_system = agent._maybe_precompress_before_advisor_tool(
            messages,
            "sys",
            "active sys",
            effective_task_id="t",
        )

    mock_compress.assert_called_once()
    assert out_messages == [{"role": "user", "content": "fresh handoff"}]
    assert out_system == "compressed sys"


def test_pre_advisor_admission_callback_avoids_rejected_call_compression(agent):
    """Honor a side-effect-free managed circuit check before compression."""
    observed = []
    agent._advisor_precompression_admission_callback = lambda names: observed.append(names) or False

    admitted = agent._advisor_precompression_admitted({"lean_reasoning_help"})

    assert admitted is False
    assert observed == [frozenset({"lean_reasoning_help"})]


def test_pre_advisor_admission_callback_fails_open(agent):
    """Do not disable reserve compression when an optional callback crashes."""

    def fail(_names):
        raise RuntimeError("stale managed state")

    agent._advisor_precompression_admission_callback = fail

    assert agent._advisor_precompression_admitted({"lean_reasoning_help"}) is True


# ── suffix-preserving split ─────────────────────────────────────────────────


def test_preserving_suffix_keeps_tail_verbatim(agent):
    messages = [
        {"role": "user", "content": "old-1"},
        {"role": "assistant", "content": "old-2"},
        {"role": "user", "content": "advisor-turn"},
    ]
    with patch.object(
        agent,
        "_compress_context",
        return_value=([{"role": "user", "content": "[SUMMARY]"}], "compressed sys"),
    ) as mock_compress:
        out_messages, out_system = agent._compress_context_preserving_suffix(
            messages,
            suffix_start=2,
            system_message="sys",
            approx_tokens=5_000,
            task_id="t",
        )
    # Only the prefix was compressed; the suffix tail is preserved verbatim.
    assert mock_compress.call_args.args[0] == messages[:2]
    assert out_messages == [
        {"role": "user", "content": "[SUMMARY]"},
        {"role": "user", "content": "advisor-turn"},
    ]
    assert out_system == "compressed sys"


def test_preserving_suffix_degenerate_compresses_whole(agent):
    """suffix_start out of range falls back to compressing everything."""
    messages = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
    with patch.object(
        agent,
        "_compress_context",
        return_value=([{"role": "user", "content": "[SUMMARY]"}], "compressed sys"),
    ) as mock_compress:
        out_messages, _ = agent._compress_context_preserving_suffix(
            messages,
            suffix_start=0,
            system_message="sys",
            approx_tokens=5_000,
            task_id="t",
        )
    mock_compress.assert_called_once()
    assert out_messages == [{"role": "user", "content": "[SUMMARY]"}]
