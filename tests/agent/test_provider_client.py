"""Tests for retry-safe primary provider client construction."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.providers.provider_client import ProviderClientFactory


def test_explicit_client_kwargs_disable_sdk_retries() -> None:
    kwargs = ProviderClientFactory.build_explicit_client_kwargs(
        "test-key", "https://provider.example/v1"
    )

    assert kwargs["max_retries"] == 0


def test_routed_client_kwargs_disable_sdk_retries_and_preserve_headers() -> None:
    routed = SimpleNamespace(
        api_key="test-key",
        base_url="https://provider.example/v1",
        _default_headers={"X-Test": "yes"},
    )
    with patch(
        "agent.providers.auxiliary_client.resolve_provider_client",
        return_value=(routed, "model"),
    ):
        kwargs = ProviderClientFactory.build_routed_client_kwargs("provider", "model")

    assert kwargs["max_retries"] == 0
    assert kwargs["default_headers"] == {"X-Test": "yes"}


def test_constructor_forces_zero_retries_even_if_caller_requests_more() -> None:
    with patch("run_agent.OpenAI") as constructor:
        ProviderClientFactory.create_openai_client(
            {
                "api_key": "test-key",
                "base_url": "https://provider.example/v1",
                "max_retries": 9,
            },
            reason="test",
            shared=True,
        )

    assert constructor.call_args.kwargs["max_retries"] == 0
