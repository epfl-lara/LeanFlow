"""Check that removed provider APIs cannot inspect credentials or issue SDK requests."""

import asyncio
from pathlib import Path

import pytest

from agent.providers import auxiliary_client


@pytest.mark.parametrize(
    "name",
    [
        "resolve_provider_client",
        "get_text_auxiliary_client",
        "get_async_text_auxiliary_client",
        "resolve_auxiliary_call_identity",
        "call_llm",
    ],
)
def test_retired_provider_entrypoints_fail_before_auth_or_network(name, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("retired provider attempted to read credentials or construct an SDK client")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-read")
    monkeypatch.setenv("LEMEX_API_KEY", "native-owned")
    with pytest.raises(auxiliary_client.RetiredProviderError, match="codex, claude, or lemex"):
        getattr(auxiliary_client, name)(provider="auto", model="old-default")


def test_retired_async_provider_entrypoint_fails_without_fallback():
    with pytest.raises(auxiliary_client.RetiredProviderError, match="LEMEX_BASE_URL"):
        asyncio.run(auxiliary_client.async_call_llm(messages=[]))
