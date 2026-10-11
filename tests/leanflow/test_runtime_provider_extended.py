"""Pin native backend selection and configuration ownership boundaries."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from leanflow_cli.runtime import runtime_provider as providers


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    monkeypatch.delenv("LEANFLOW_INFERENCE_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(providers, "load_config", lambda: {})


def test_supported_targets_are_exactly_native_backends():
    targets = providers.list_runtime_provider_targets()
    assert [target["name"] for target in targets] == ["codex", "claude", "lemex"]
    assert {target["kind"] for target in targets} == {"native"}


@pytest.mark.parametrize("requested", [None, "", " "])
def test_unset_backend_defaults_to_codex(requested):
    assert providers.resolve_requested_provider(requested) == "codex"


@pytest.mark.parametrize("backend", providers.NATIVE_BACKENDS)
def test_native_auth_and_config_are_not_read_or_copied(monkeypatch, backend):
    def fail_read(*args, **kwargs):
        pytest.fail("Provider resolution must not read native configuration or credentials")

    monkeypatch.setattr(Path, "read_text", fail_read)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("LEMEX_API_KEY", "lemex-secret")
    monkeypatch.setenv("LEMEX_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    before = dict(os.environ)
    resolved = providers.resolve_runtime_provider(requested=backend)
    assert resolved["provider"] == backend
    assert resolved["api_mode"] == "native"
    assert resolved["api_key"] == resolved["base_url"] == ""
    assert resolved["model"] == resolved["reasoning_effort"] == ""
    assert dict(os.environ) == before
    assert "secret" not in str(resolved)


def test_explicit_selection_then_environment_then_saved_setting(monkeypatch):
    monkeypatch.setattr(providers, "load_config", lambda: {"model": {"provider": "lemex"}})
    assert providers.resolve_requested_provider() == "lemex"
    monkeypatch.setenv("LEANFLOW_INFERENCE_PROVIDER", "claude")
    assert providers.resolve_requested_provider() == "claude"
    assert providers.resolve_requested_provider(" CODEX ") == "codex"


def test_only_explicit_model_and_effort_are_returned(monkeypatch):
    monkeypatch.setattr(
        providers,
        "load_config",
        lambda: {
            "model": {"provider": "lemex", "default": "chosen-model"},
            "agent": {"reasoning_effort": "high"},
        },
    )
    resolved = providers.resolve_runtime_provider()
    assert resolved["model"] == "chosen-model"
    assert resolved["reasoning_effort"] == "high"
    updated = providers.apply_runtime_model_override(
        resolved, requested_provider="lemex", model="other-model"
    )
    assert updated["model"] == "other-model"
    assert resolved["model"] == "chosen-model"
    assert updated["api_key"] == updated["base_url"] == ""


@pytest.mark.parametrize(
    "legacy",
    [
        "auto",
        "openrouter",
        "rcp",
        "custom",
        "local",
        "anthropic",
        "openai-codex",
        "claude-code",
        "zai",
        "unknown",
    ],
)
def test_legacy_routes_require_explicit_migration(legacy):
    with pytest.raises(providers.RuntimeProviderError, match="LEMEX_BASE_URL and LEMEX_API_KEY"):
        providers.resolve_runtime_provider(requested=legacy)


@pytest.mark.parametrize(
    "kwargs", [{"explicit_api_key": "secret"}, {"explicit_base_url": "https://example.com/v1"}]
)
def test_leanflow_owned_auth_and_endpoints_are_rejected(kwargs):
    with pytest.raises(providers.RuntimeProviderError, match="Configure the native CLI"):
        providers.resolve_runtime_provider(requested="lemex", **kwargs)


def test_saved_legacy_backend_is_not_silently_relabelled(monkeypatch):
    monkeypatch.setattr(providers, "load_config", lambda: {"model": {"provider": "custom"}})
    with pytest.raises(providers.RuntimeProviderError, match="start a new run"):
        providers.resolve_runtime_provider()


def test_legacy_defaults_require_review_even_with_explicit_native_backend(monkeypatch):
    monkeypatch.setattr(
        providers, "load_config", lambda: {"model": {"default": "old-default", "base_url": ""}}
    )
    with pytest.raises(providers.RuntimeProviderError, match="Clear model.default"):
        providers.resolve_runtime_provider(requested="lemex")


def test_codex_does_not_inherit_an_external_openai_endpoint(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    with pytest.raises(providers.RuntimeProviderError, match="select lemex"):
        providers.resolve_runtime_provider(requested="codex")
    assert providers.resolve_runtime_provider(requested="lemex")["provider"] == "lemex"
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    assert providers.resolve_runtime_provider(requested="codex")["provider"] == "codex"
