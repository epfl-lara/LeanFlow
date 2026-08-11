"""Focused tests for the ApiCaller collaborator extracted from AIAgent.

Covers the resolve-accessor materialization (real agents, ``__new__`` agents,
MagicMock fakes), the AIAgent delegation identity (wrappers forward to the
collaborator), and a few behavior checks for the moved logic: per-request
timeout resolution, ``build_api_kwargs`` mode branching, and the interrupt/
timeout abort paths of the background-thread request runner.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import run_agent
from agent.providers.api_caller import (
    TRANSIENT_PROVIDER_MAX_ATTEMPTS,
    TRANSIENT_PROVIDER_RETRY_DELAYS_S,
    ApiCaller,
    TransientProviderRetriesExhausted,
    transient_provider_recovery_deadline_monotonic,
    transient_provider_retry_delay_s,
    transient_provider_retry_delay_within_deadline_s,
)
from run_agent import AIAgent, _resolve_api_caller


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


def test_init_builds_real_api_caller(agent):
    assert isinstance(agent._api_caller_obj, ApiCaller)
    assert agent._api_caller_obj._agent is agent


def test_resolve_returns_cached_instance(agent):
    caller = _resolve_api_caller(agent)
    assert caller is agent._api_caller_obj
    # Repeated resolution returns the same cached instance.
    assert _resolve_api_caller(agent) is caller


def test_resolve_materializes_for_new_agent():
    """Agents built via __new__ (bypassing __init__) get a fresh real caller."""
    a = AIAgent.__new__(AIAgent)
    caller = _resolve_api_caller(a)
    assert isinstance(caller, ApiCaller)
    assert caller._agent is a
    # Now cached on the instance.
    assert a._api_caller_obj is caller


def test_resolve_materializes_for_mock_agent():
    """A MagicMock fake agent gets a real ApiCaller (isinstance guard)."""
    fake = MagicMock()
    caller = _resolve_api_caller(fake)
    assert isinstance(caller, ApiCaller)
    assert caller._agent is fake


def test_wrappers_delegate_to_collaborator(agent):
    sentinel = object()
    with patch.object(agent._api_caller_obj, "build_api_kwargs", return_value=sentinel) as m:
        out = agent._build_api_kwargs(["msg"])
    m.assert_called_once_with(["msg"])
    assert out is sentinel

    with patch.object(
        agent._api_caller_obj, "provider_request_timeout_seconds", return_value=7.5
    ) as m:
        assert agent._provider_request_timeout_seconds({"timeout": 3}) == 7.5
    m.assert_called_once_with({"timeout": 3})

    with patch.object(agent._api_caller_obj, "interruptible_api_call", return_value="r") as m:
        assert agent._interruptible_api_call({"k": "v"}) == "r"
    m.assert_called_once_with({"k": "v"})


# ── provider_request_timeout_seconds behavior ───────────────────────────────


def test_timeout_uses_api_kwargs_value(agent):
    assert agent._provider_request_timeout_seconds({"timeout": 42.0}) == 42.0


def test_timeout_floors_at_one_second(agent):
    assert agent._provider_request_timeout_seconds({"timeout": 0.1}) == 1.0


def test_timeout_falls_back_to_env(agent, monkeypatch):
    monkeypatch.setenv("LEANFLOW_API_TIMEOUT", "55.0")
    # Non-numeric/absent timeout in kwargs -> env default.
    assert agent._provider_request_timeout_seconds({}) == 55.0


def test_timeout_is_clipped_to_conversation_deadline(agent, monkeypatch):
    agent._conversation_deadline_monotonic = 110.0
    monkeypatch.setattr("run_agent.time.monotonic", lambda: 100.0)

    assert agent._provider_request_timeout_seconds({"timeout": 1200.0}) == 10.0


def test_timeout_is_clipped_to_transient_provider_recovery_deadline(agent, monkeypatch):
    agent._transient_provider_recovery_deadline_monotonic = 108.0
    monkeypatch.setattr("run_agent.time.monotonic", lambda: 100.0)

    assert agent._provider_request_timeout_seconds({"timeout": 1200.0}) == 8.0


def test_request_client_uses_effective_managed_timeout(agent):
    """Keep the request transport timeout aligned with managed heartbeats."""
    agent.client = object()
    agent._client_kwargs = {
        "api_key": "test-key-1234567890",
        "base_url": "https://example.test/v1",
    }
    request_client = object()
    with (
        patch.object(agent, "_is_openai_client_closed", return_value=False),
        patch.object(agent, "_provider_request_timeout_seconds", return_value=1200.0) as timeout,
        patch.object(agent, "_create_openai_client", return_value=request_client) as create,
    ):
        result = agent._create_request_openai_client(reason="test_request")

    assert result is request_client
    timeout.assert_called_once_with({})
    assert create.call_args.args[0]["timeout"] == 1200.0


def test_transient_provider_retry_policy_is_exactly_three_managed_retries():
    """Expose the 5/15/45 contract independently of real sleeping."""
    assert TRANSIENT_PROVIDER_RETRY_DELAYS_S == (5.0, 15.0, 45.0)
    assert TRANSIENT_PROVIDER_MAX_ATTEMPTS == 4
    assert [transient_provider_retry_delay_s(attempt) for attempt in range(1, 5)] == [
        5.0,
        15.0,
        45.0,
        None,
    ]


def test_transient_provider_retry_respects_enclosing_deadline():
    """Do not spend backoff time when no useful request window remains."""
    assert (
        transient_provider_retry_delay_within_deadline_s(
            1,
            deadline_monotonic=116.0,
            now_monotonic=100.0,
        )
        == 5.0
    )
    assert (
        transient_provider_retry_delay_within_deadline_s(
            1,
            deadline_monotonic=114.0,
            now_monotonic=100.0,
        )
        is None
    )
    assert (
        transient_provider_retry_delay_within_deadline_s(
            2,
            deadline_monotonic=None,
            now_monotonic=100.0,
        )
        == 15.0
    )


def test_transient_provider_recovery_deadline_is_stable_and_clipped(monkeypatch):
    monkeypatch.setenv("LEANFLOW_PROVIDER_RECOVERY_BUDGET_S", "180")

    first = transient_provider_recovery_deadline_monotonic(
        current_deadline_monotonic=None,
        conversation_deadline_monotonic=500.0,
        now_monotonic=100.0,
    )
    repeated = transient_provider_recovery_deadline_monotonic(
        current_deadline_monotonic=first,
        conversation_deadline_monotonic=500.0,
        now_monotonic=150.0,
    )
    clipped = transient_provider_recovery_deadline_monotonic(
        current_deadline_monotonic=None,
        conversation_deadline_monotonic=200.0,
        now_monotonic=100.0,
    )

    assert first == 280.0
    assert repeated == first
    assert clipped == 200.0


def test_transient_provider_exhaustion_marker_redacts_persisted_message():
    secret = "sk-testprovidersecret1234567890"
    error = RuntimeError(f"rate limited Authorization: Bearer {secret}")

    exhausted = TransientProviderRetriesExhausted(error)

    assert exhausted.provider_retries_exhausted is True
    assert exhausted.original_error_type == "RuntimeError"
    assert "rate limited" in str(exhausted)
    assert secret not in str(exhausted)


# ── build_api_kwargs mode branching ─────────────────────────────────────────


def test_build_api_kwargs_chat_completions(agent):
    agent.api_mode = "chat_completions"
    messages = [{"role": "user", "content": "hi"}]
    kwargs = agent._build_api_kwargs(messages)
    assert kwargs["model"] == agent.model
    assert kwargs["messages"] == messages
    assert "timeout" in kwargs


def test_build_api_kwargs_codex_responses(agent):
    agent.api_mode = "codex_responses"
    agent.reasoning_config = {"enabled": True, "effort": "high"}
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hello"},
    ]
    kwargs = agent._build_api_kwargs(messages)
    assert kwargs["instructions"] == "SYS"
    assert kwargs["store"] is False
    assert kwargs["reasoning"] == {"effort": "high", "summary": "auto"}


# ── interruptible_api_call abort paths ──────────────────────────────────────


def test_interruptible_api_call_returns_response(agent):
    agent.api_mode = "chat_completions"
    req_client = MagicMock()
    req_client.chat.completions.create.return_value = "the-response"
    with (
        patch.object(agent, "_create_request_openai_client", return_value=req_client),
        patch.object(agent, "_close_request_openai_client"),
    ):
        out = agent._interruptible_api_call({"model": "m", "messages": []})
    assert out == "the-response"


@pytest.mark.parametrize(
    ("delegate_depth", "dispatch_worker", "expected_enabled"),
    [(0, "", False), (1, "", True), (0, "1", True)],
)
def test_only_background_agents_enter_capacity_gate(
    agent, monkeypatch, delegate_depth, dispatch_worker, expected_enabled
):
    agent.api_mode = "chat_completions"
    agent._delegate_depth = delegate_depth
    if dispatch_worker:
        monkeypatch.setenv("LEANFLOW_DISPATCH_WORKER", dispatch_worker)
    else:
        monkeypatch.delenv("LEANFLOW_DISPATCH_WORKER", raising=False)
        monkeypatch.delenv("LEANFLOW_DISPATCH_JOB_ID", raising=False)
    req_client = MagicMock()
    req_client.chat.completions.create.return_value = "response"
    gate_calls: list[bool] = []

    @contextmanager
    def fake_gate(*, enabled, cancelled):
        gate_calls.append(enabled)
        yield None

    with (
        patch("agent.providers.api_caller.background_provider_lease", fake_gate),
        patch.object(agent, "_create_request_openai_client", return_value=req_client),
        patch.object(agent, "_close_request_openai_client"),
    ):
        assert agent._interruptible_api_call({"model": "m", "messages": []}) == "response"

    assert gate_calls == [expected_enabled]


def test_interruptible_api_call_raises_on_interrupt(agent):
    """When the interrupt flag is set while the request is in-flight, abort and raise."""
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = True

    blocker = MagicMock()

    # Make the worker block so the main loop observes the interrupt flag.
    def _never_returns(**_kwargs):
        import time as _t

        _t.sleep(5)

    blocker.chat.completions.create.side_effect = _never_returns

    with (
        patch.object(agent, "_create_request_openai_client", return_value=blocker),
        patch.object(agent, "_close_request_openai_client"),
        patch.object(agent, "_abort_inflight_provider_request") as abort,
    ):
        with pytest.raises(InterruptedError):
            agent._interruptible_api_call({"model": "m", "messages": []})
    assert abort.called


def test_run_agent_time_patch_is_honored(agent):
    """The loop reads time via run_agent.time so test patches still intercept."""
    agent.api_mode = "chat_completions"
    req_client = MagicMock()
    req_client.chat.completions.create.return_value = "ok"

    fake_time = MagicMock()
    fake_time.monotonic.return_value = 1000.0
    with (
        patch.object(agent, "_create_request_openai_client", return_value=req_client),
        patch.object(agent, "_close_request_openai_client"),
        patch("run_agent.time", fake_time),
    ):
        out = agent._interruptible_api_call({"model": "m", "messages": []})
    assert out == "ok"
    assert fake_time.monotonic.called

    # SimpleNamespace import side-effect smoke (streaming variant uses it lazily).
    assert isinstance(run_agent.SimpleNamespace(x=1), SimpleNamespace)
