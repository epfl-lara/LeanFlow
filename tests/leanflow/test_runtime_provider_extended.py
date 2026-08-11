from __future__ import annotations

import json

import pytest
import yaml

from leanflow_cli.runtime.runtime_provider import (
    PROVIDER_SPECS,
    RuntimeProviderError,
    apply_runtime_model_override,
    list_runtime_provider_targets,
    resolve_requested_provider,
    resolve_runtime_provider,
)

PROVIDER_ENV_VARS = (
    "GLM_API_KEY",
    "ZAI_API_KEY",
    "Z_AI_API_KEY",
    "KIMI_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "LEANFLOW_OPENAI_API_KEY",
    "LEANFLOW_OPENAI_BASE_URL",
    "LEANFLOW_OPENROUTER_API_KEY",
    "LEANFLOW_OPENROUTER_BASE_URL",
    "LEANFLOW_CODEX_MODEL",
    "LEANFLOW_USE_LEGACY_CODEX_AUTH",
    "LEANFLOW_INFERENCE_PROVIDER",
    "CODEX_HOME",
    "GLM_BASE_URL",
    "RCP_OPENAI_API_KEY",
    "RCP_OPENAI_BASE_URL",
    "KIMI_BASE_URL",
    "MINIMAX_BASE_URL",
    "MINIMAX_CN_BASE_URL",
    "DEEPSEEK_BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolated_provider_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    for name in PROVIDER_ENV_VARS:
        if name not in {"CODEX_HOME"}:
            monkeypatch.delenv(name, raising=False)
    yield


def test_list_runtime_provider_targets_includes_every_direct_provider():
    names = {entry["name"] for entry in list_runtime_provider_targets()}

    # Meta selectors
    assert {"auto", "local", "custom", "rcp", "openrouter", "codex", "anthropic"} <= names
    # All direct providers are exposed
    assert set(PROVIDER_SPECS.keys()) <= names


def test_list_runtime_provider_targets_direct_entries_advertise_credentials():
    direct_entries = {
        entry["name"]: entry
        for entry in list_runtime_provider_targets()
        if entry["kind"] == "direct" and entry["name"] in PROVIDER_SPECS
    }

    for provider_id, entry in direct_entries.items():
        assert entry["credentials"], f"{provider_id} missing credential listing"
        # Each listed env var is the actual API key env var the spec expects.
        for env_name in PROVIDER_SPECS[provider_id].api_key_env_vars:
            assert env_name in entry["credentials"]


def test_resolve_requested_provider_defaults_to_auto():
    assert resolve_requested_provider(None) == "auto"
    assert resolve_requested_provider("") == "auto"
    assert resolve_requested_provider("   ") == "auto"


def test_runtime_provider_resolves_codex_from_cli_auth(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "codex-access-token",
                    "refresh_token": "codex-refresh-token",
                },
            }
        ),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.5"\nmodel_reasoning_effort = "xhigh"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    resolved = resolve_runtime_provider(requested="codex")

    assert resolved["provider"] == "openai-codex"
    assert resolved["requested_provider"] == "codex"
    assert resolved["api_mode"] == "codex_responses"
    assert resolved["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert resolved["api_key"] == "codex-access-token"
    assert resolved["model"] == "gpt-5.5"
    assert resolved["reasoning_effort"] == "xhigh"


def test_runtime_provider_codex_env_model_override_wins(monkeypatch, tmp_path):
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "codex-access-token"}}),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.5"\nmodel_reasoning_effort = "xhigh"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LEANFLOW_CODEX_MODEL", "gpt-5.4")
    monkeypatch.setenv("LEANFLOW_CODEX_REASONING_EFFORT", "medium")

    resolved = resolve_runtime_provider(requested="codex")

    assert resolved["provider"] == "openai-codex"
    assert resolved["requested_provider"] == "codex"
    assert resolved["model"] == "gpt-5.4"
    assert resolved["reasoning_effort"] == "medium"


def test_runtime_provider_codex_reports_missing_login():
    with pytest.raises(RuntimeProviderError, match="codex login"):
        resolve_runtime_provider(requested="codex")


def test_resolve_requested_provider_normalizes_name():
    assert resolve_requested_provider("  ZAI  ") == "zai"
    assert resolve_requested_provider("Kimi Coding") == "kimi-coding"


def test_resolve_requested_provider_reads_env_override_when_config_empty(monkeypatch, tmp_path):
    # Config with empty model.provider — env var is the tie-breaker.
    cfg_path = tmp_path / "home" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump({"model": {"provider": ""}}), encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_INFERENCE_PROVIDER", "deepseek")

    assert resolve_requested_provider() == "deepseek"


def test_resolve_requested_provider_config_wins_over_env(monkeypatch, tmp_path):
    # When config sets model.provider, env var does NOT override it.
    cfg_path = tmp_path / "home" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump({"model": {"provider": "zai"}}), encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_INFERENCE_PROVIDER", "deepseek")

    assert resolve_requested_provider() == "zai"


def test_resolve_runtime_provider_direct_uses_spec_base_url_by_default(monkeypatch):
    monkeypatch.setenv("KIMI_API_KEY", "kimi-key")

    resolved = resolve_runtime_provider(requested="kimi-coding")

    assert resolved["provider"] == "kimi-coding"
    assert resolved["base_url"] == PROVIDER_SPECS["kimi-coding"].base_url
    assert resolved["api_key"] == "kimi-key"


def test_resolve_runtime_provider_direct_honors_base_url_env_override(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://ds.custom/v1/")

    resolved = resolve_runtime_provider(requested="deepseek")

    # Trailing slash is stripped.
    assert resolved["base_url"] == "https://ds.custom/v1"
    assert resolved["api_key"] == "ds-key"


def test_resolve_runtime_provider_direct_raises_without_credentials():
    with pytest.raises(RuntimeProviderError, match="credentials"):
        resolve_runtime_provider(requested="zai")


def test_resolve_runtime_provider_anthropic_requires_token():
    with pytest.raises(RuntimeProviderError, match="Anthropic"):
        resolve_runtime_provider(requested="anthropic")


def test_resolve_runtime_provider_anthropic_accepts_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_TOKEN", "anthropic-token")

    resolved = resolve_runtime_provider(requested="anthropic")

    assert resolved["provider"] == "anthropic"
    assert resolved["api_mode"] == "anthropic_messages"
    assert resolved["api_key"] == "anthropic-token"


def test_resolve_runtime_provider_openrouter_prefers_openrouter_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    resolved = resolve_runtime_provider(requested="openrouter")

    assert resolved["provider"] == "openrouter"
    assert "openrouter.ai" in resolved["base_url"]
    assert resolved["api_key"] == "or-key"


def test_resolve_runtime_provider_uses_custom_providers_entry(monkeypatch, tmp_path):
    cfg_path = tmp_path / "home" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "custom_providers": [
                    {
                        "name": "epfl-rcp",
                        "base_url": "https://inference.rcp.epfl.ch/v1",
                        "api_key": "rcp-token",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    resolved = resolve_runtime_provider(requested="epfl-rcp")

    assert resolved["provider"] == "custom"
    assert resolved["base_url"] == "https://inference.rcp.epfl.ch/v1"
    assert resolved["api_key"] == "rcp-token"
    assert resolved["source"] == "custom_provider:epfl-rcp"


def test_resolve_runtime_provider_custom_providers_respects_custom_prefix(monkeypatch, tmp_path):
    cfg_path = tmp_path / "home" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "custom_providers": [
                    {"name": "alt", "base_url": "https://alt.example/v1", "api_key": "k"},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    resolved = resolve_runtime_provider(requested="custom:alt")

    assert resolved["base_url"] == "https://alt.example/v1"


def test_resolve_runtime_provider_auto_defaults_to_openrouter_without_other_hints():
    resolved = resolve_runtime_provider(requested="auto")

    assert resolved["provider"] == "openrouter"
    assert "openrouter.ai" in resolved["base_url"]


def test_resolve_runtime_provider_carries_requested_provider_label(monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "glm-key")

    resolved = resolve_runtime_provider(requested="zai")

    assert resolved["requested_provider"] == "zai"


def test_rcp_model_override_selects_glm_specific_credential(monkeypatch, tmp_path):
    cfg_path = tmp_path / "home" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"model": {"default": "moonshotai/Kimi-K2.7-Code"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_OPENAI_API_KEY", "generic-key")
    monkeypatch.setenv("LEANFLOW_OPENAI_BASE_URL", "https://generic.example/v1")
    monkeypatch.setenv("RCP_OPENAI_API_KEY", "kimi-rcp-key")
    monkeypatch.setenv("RCP_OPENAI_BASE_URL", "https://rcp.example/v1")
    monkeypatch.setenv("GLM_API_KEY", "glm-rcp-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://glm-rcp.example/v1")

    runtime = resolve_runtime_provider(requested="rcp")
    overridden = apply_runtime_model_override(
        runtime,
        requested_provider="rcp",
        model="zai-org/GLM-5.2",
    )

    assert runtime["api_key"] == "kimi-rcp-key"
    assert runtime["model"] == "moonshotai/Kimi-K2.7-Code"
    assert overridden["api_key"] == "glm-rcp-key"
    assert overridden["base_url"] == "https://glm-rcp.example/v1"
    assert overridden["model"] == "zai-org/GLM-5.2"
    assert overridden["requested_provider"] == "rcp"


def test_rcp_kimi_model_prefers_general_rcp_credential(monkeypatch, tmp_path):
    cfg_path = tmp_path / "home" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"model": {"default": "moonshotai/Kimi-K2.7-Code"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RCP_OPENAI_API_KEY", "kimi-rcp-key")
    monkeypatch.setenv("RCP_OPENAI_BASE_URL", "https://rcp.example/v1")
    monkeypatch.setenv("GLM_API_KEY", "glm-rcp-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://glm-rcp.example/v1")

    resolved = resolve_runtime_provider(requested="rcp")

    assert resolved["provider"] == "custom"
    assert resolved["api_key"] == "kimi-rcp-key"
    assert resolved["base_url"] == "https://rcp.example/v1"
    assert resolved["model"] == "moonshotai/Kimi-K2.7-Code"


def test_rcp_source_reports_the_credential_that_was_actually_selected(monkeypatch, tmp_path):
    cfg_path = tmp_path / "home" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"model": {"default": "zai-org/GLM-5.2"}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("RCP_OPENAI_API_KEY", "fallback-rcp-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://glm-rcp.example/v1")

    resolved = resolve_runtime_provider(requested="rcp")

    assert resolved["api_key"] == "fallback-rcp-key"
    assert resolved["source"] == "RCP_OPENAI_API_KEY"


def test_rcp_glm_fallback_prefers_endpoint_key_before_general_pool_key(monkeypatch, tmp_path):
    """A general RCP pool key may expose fewer models than the endpoint key."""
    cfg_path = tmp_path / "home" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        yaml.safe_dump({"model": {"default": "zai-org/GLM-5.2"}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.setenv("LEANFLOW_OPENAI_API_KEY", "glm-capable-endpoint-key")
    monkeypatch.setenv("RCP_OPENAI_API_KEY", "restricted-general-pool-key")
    monkeypatch.setenv("GLM_BASE_URL", "https://glm-rcp.example/v1")

    resolved = resolve_runtime_provider(requested="rcp")

    assert resolved["api_key"] == "glm-capable-endpoint-key"
    assert resolved["source"] == "LEANFLOW_OPENAI_API_KEY"
