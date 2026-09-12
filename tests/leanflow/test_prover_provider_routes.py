"""Keep role-specific provider credentials separate in bounded prover sessions."""

import os
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from leanflow_cli.workflows.prover import session_transport
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.session_provider import resolve_session_provider


def test_legacy_transport_inherits_all_native_provider_fields(monkeypatch):
    for key, value in {
        "PROVIDER": "custom",
        "API_MODE": "chat_completions",
        "BASE_URL": "https://example.invalid/v1",
        "API_KEY": "native-test-key",
    }.items():
        monkeypatch.setenv("LEANFLOW_NATIVE_" + key, value)
    monkeypatch.setattr(session_transport, "_load_agent_class", lambda: SimpleNamespace)
    agent = session_transport.build_transport({"model": "model"}, [])
    assert (agent.provider, agent.api_mode, agent.base_url, agent.api_key) == (
        "custom",
        "chat_completions",
        "https://example.invalid/v1",
        "native-test-key",
    )


def test_legacy_role_model_selection_preserves_efforts():
    config = ProverConfig(
        model="worker",
        orchestrator_model="planner",
        reasoning_effort="medium",
        orchestrator_reasoning_effort="low",
    )
    assert config.to_mapping("prover")["model"] == "worker"
    assert config.to_mapping("negation")["reasoning_effort"] == "medium"
    for role in ("orchestrator", "review", "research"):
        assert config.to_mapping(role)["model"] == "planner"
        assert config.to_mapping(role)["reasoning_effort"] == "low"


def test_parallel_roles_select_different_endpoints_without_changing_environment(monkeypatch):
    from leanflow_cli.runtime import runtime_provider

    monkeypatch.setenv("RCP_API_KEY", "rcp-test-key")
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "native-test-key")
    monkeypatch.setattr(session_transport, "_load_agent_class", lambda: SimpleNamespace)

    def resolve(*, requested, explicit_api_key=None, explicit_base_url=None):
        if requested == "openai-codex":
            assert explicit_api_key is None and explicit_base_url is None
            return dict(
                provider=requested,
                api_mode="codex_responses",
                base_url="https://codex.invalid",
                api_key="codex-test-key",
            )
        assert requested == "custom"
        return dict(
            provider="custom",
            api_mode="chat_completions",
            base_url=explicit_base_url,
            api_key=explicit_api_key,
        )

    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", resolve)
    config = ProverConfig(
        model="zai-org/GLM-5.3-Flash",
        provider="rcp",
        base_url="https://inference.rcp.epfl.ch/v1",
        api_key_env="RCP_API_KEY",
        orchestrator_model="gpt-6-astra",
        orchestrator_provider="openai-codex",
        reasoning_effort="medium",
        orchestrator_reasoning_effort="low",
    )
    roles = ["prover", "negation", "orchestrator", "review", "research"]
    with ThreadPoolExecutor(max_workers=5) as pool:
        agents = list(
            pool.map(
                lambda role: session_transport.build_transport(config.to_mapping(role), []), roles
            )
        )
    for role, agent in zip(roles, agents):
        if role in {"prover", "negation"}:
            assert agent.api_key == "rcp-test-key"
            assert agent.api_mode == "chat_completions"
            assert agent.model == "zai-org/GLM-5.3-Flash"
        else:
            assert agent.api_key == "codex-test-key"
            assert agent.api_mode == "codex_responses"
            assert agent.model == "gpt-6-astra"
            assert agent.reasoning_config == {"effort": "low"}
    assert os.environ["LEANFLOW_NATIVE_API_KEY"] == "native-test-key"
    assert "rcp-test-key" not in repr(config.to_mapping())


def test_missing_explicit_key_cannot_fall_back_to_native_credentials(monkeypatch):
    monkeypatch.delenv("UNSET_TEST_PROVIDER_KEY", raising=False)
    monkeypatch.setenv("LEANFLOW_NATIVE_API_KEY", "must-not-be-used")
    with pytest.raises(ValueError, match="UNSET_TEST_PROVIDER_KEY"):
        resolve_session_provider(
            {
                "provider": "rcp",
                "api_key_env": "UNSET_TEST_PROVIDER_KEY",
                "base_url": "https://example.invalid/v1",
            }
        )


def test_provider_routes_round_trip_through_environment():
    config = ProverConfig.from_env(
        {
            "LEANFLOW_PROVER_PROVIDER": "rcp",
            "LEANFLOW_PROVER_BASE_URL": "https://example.invalid/v1",
            "LEANFLOW_PROVER_API_KEY_ENV": "RCP_API_KEY",
            "LEANFLOW_PROVER_ORCHESTRATOR_PROVIDER": "openai-codex",
        }
    )
    assert config.to_mapping("prover")["api_key_env"] == "RCP_API_KEY"
    assert config.to_mapping("review")["api_key_env"] == ""
    assert config.to_mapping("review")["base_url"] == ""
    with pytest.raises(ValueError, match="environment variable"):
        ProverConfig(provider="custom", api_key_env="not a variable")
    with pytest.raises(ValueError, match="provider is required"):
        ProverConfig(api_key_env="TEST_KEY")
