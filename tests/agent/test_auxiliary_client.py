"""Tests for agent.auxiliary_client resolution chain, provider overrides, and model overrides."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.providers.auxiliary_adapters import _CodexCompletionsAdapter
from agent.providers.auxiliary_client import (
    _build_call_kwargs,
    _compatible_explicit_model,
    _get_auxiliary_provider,
    _get_cached_client,
    _read_codex_access_token,
    _resolve_forced_provider,
    _resolve_task_provider_model,
    _resolve_task_reasoning_effort,
    async_call_llm,
    auxiliary_max_tokens_param,
    call_llm,
    get_text_auxiliary_client,
    resolve_auxiliary_call_identity,
)
from agent.providers.isolated_auxiliary import AuxiliaryTextResponse
from core.provider_capacity import (
    BACKGROUND_PROVIDER_CAPACITY_ENV,
    BACKGROUND_PROVIDER_NAMESPACE_ENV,
    background_actor_lease,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip provider env vars so each test starts clean."""
    for key in (
        "OPENROUTER_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "LLM_MODEL",
        "NOUS_INFERENCE_BASE_URL",
        "CODEX_HOME",
        "LEANFLOW_USE_LEGACY_CODEX_AUTH",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        # Per-task provider/model/direct-endpoint overrides
        "AUXILIARY_WEB_EXTRACT_PROVIDER",
        "AUXILIARY_WEB_EXTRACT_MODEL",
        "AUXILIARY_WEB_EXTRACT_BASE_URL",
        "AUXILIARY_WEB_EXTRACT_API_KEY",
        "AUXILIARY_LEAN_REASONING_PROVIDER",
        "AUXILIARY_LEAN_REASONING_MODEL",
        "AUXILIARY_LEAN_REASONING_BASE_URL",
        "AUXILIARY_LEAN_REASONING_API_KEY",
        "AUXILIARY_LEAN_REASONING_REASONING_EFFORT",
        "AUXILIARY_LEAN_REASONING_COMMAND_TEMPLATE",
        "AUXILIARY_LEAN_DECOMPOSE_HELPERS_PROVIDER",
        "AUXILIARY_LEAN_DECOMPOSE_HELPERS_MODEL",
        "AUXILIARY_LEAN_DECOMPOSE_HELPERS_BASE_URL",
        "AUXILIARY_LEAN_DECOMPOSE_HELPERS_API_KEY",
        "AUXILIARY_LEAN_DECOMPOSE_HELPERS_REASONING_EFFORT",
        "AUXILIARY_LEAN_DECOMPOSE_HELPERS_COMMAND_TEMPLATE",
        "LEANFLOW_EXPERT_CODEX_COMMAND_TEMPLATE",
        "LEANFLOW_EXPERT_CLAUDE_CODE_COMMAND_TEMPLATE",
        "CONTEXT_COMPRESSION_PROVIDER",
        "CONTEXT_COMPRESSION_MODEL",
        BACKGROUND_PROVIDER_CAPACITY_ENV,
        BACKGROUND_PROVIDER_NAMESPACE_ENV,
        "LEANFLOW_RESEARCH_MODE",
        "LEANFLOW_RESEARCH_WORKERS",
        "LEANFLOW_DISPATCH_WORKER",
    ):
        monkeypatch.delenv(key, raising=False)


def test_codex_provider_drops_openrouter_model_slug():
    assert _compatible_explicit_model("openai-codex", "google/gemini-3-flash-preview") is None
    assert _compatible_explicit_model("openai-codex", "gpt-5.6-sol") == "gpt-5.6-sol"


def test_explicit_codex_provider_allows_cli_auth_store(monkeypatch):
    """An explicit Codex auxiliary route must match the main runtime's auth policy."""
    token_calls: list[bool | None] = []
    client = SimpleNamespace(
        api_key="codex-token",
        base_url="https://chatgpt.com/backend-api/codex",
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._read_codex_access_token",
        lambda *, allow_legacy_store=None: (
            token_calls.append(allow_legacy_store) or "codex-token"
        ),
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client.OpenAI",
        lambda **_kwargs: client,
    )

    resolved_client, model = _get_cached_client("openai-codex", "gpt-5.6-sol")

    assert resolved_client is not None
    assert model == "gpt-5.6-sol"
    assert token_calls == [True]


def test_explicit_codex_provider_inherits_main_runtime_model(monkeypatch):
    """A selected Codex lane must not fall back to an obsolete auxiliary model."""
    client = SimpleNamespace(
        api_key="codex-token",
        base_url="https://chatgpt.com/backend-api/codex",
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._read_codex_access_token",
        lambda *, allow_legacy_store=None: "codex-token",
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client.resolve_runtime_provider",
        lambda **_kwargs: {"model": "gpt-5.6-sol"},
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client.OpenAI",
        lambda **_kwargs: client,
    )

    _resolved_client, model = _get_cached_client("openai-codex")

    assert model == "gpt-5.6-sol"


def test_get_cached_client_preserves_provider_resolved_model(monkeypatch):
    """The cache wrapper must not resurrect a rejected provider model."""
    client = object()
    monkeypatch.setattr(
        "agent.providers.auxiliary_client.resolve_provider_client",
        lambda *_args, **_kwargs: (client, "gpt-5.6-sol"),
    )

    resolved_client, model = _get_cached_client(
        "openai-codex",
        "google/gemini-3-flash-preview",
    )

    assert resolved_client is client
    assert model == "gpt-5.6-sol"


def test_auxiliary_call_reuses_delegated_actor_capacity(monkeypatch, tmp_path):
    """A delegated tool's model helper must not acquire a second actor slot."""
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    completions = SimpleNamespace(create=lambda **_kwargs: response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "1")
    monkeypatch.setenv(BACKGROUND_PROVIDER_NAMESPACE_ENV, "auxiliary-nested")
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._resolve_task_provider_model",
        lambda *_args, **_kwargs: ("custom", "model", None, "token"),
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._get_cached_client",
        lambda *_args, **_kwargs: (client, "model"),
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._resolve_task_reasoning_effort",
        lambda *_args, **_kwargs: None,
    )

    with background_actor_lease() as actor:
        assert actor is not None
        result = call_llm(task="lean_reasoning", messages=[{"role": "user", "content": "x"}])

    assert result is response


def test_call_llm_isolate_routes_through_hard_deadline_worker(monkeypatch):
    """An isolated synchronous call must bypass the in-process provider client."""
    captured: dict[str, object] = {}

    def fake_isolated_call(**kwargs):
        captured.update(kwargs)
        return AuxiliaryTextResponse(content="bounded answer", model="open-model")

    monkeypatch.setattr(
        "agent.providers.isolated_auxiliary.run_isolated_auxiliary_text",
        fake_isolated_call,
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._resolve_task_provider_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the parent must not create a provider client")
        ),
    )

    result = call_llm(
        task="lean_decompose_helpers",
        provider="main",
        messages=[{"role": "user", "content": "split the proof"}],
        temperature=0.1,
        max_tokens=2048,
        timeout=37,
        isolate=True,
    )

    assert result.model == "open-model"
    assert result.choices[0].message.content == "bounded answer"
    assert captured == {
        "task": "lean_decompose_helpers",
        "provider": "main",
        "model": None,
        "base_url": None,
        "api_key": None,
        "messages": [{"role": "user", "content": "split the proof"}],
        "timeout": 37,
        "temperature": 0.1,
        "max_tokens": 2048,
    }


def test_foreground_auxiliary_call_does_not_wait_for_background_actors(monkeypatch, tmp_path):
    """Manager/orchestrator control calls remain outside background capacity."""
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs: response))
    )
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setenv(BACKGROUND_PROVIDER_CAPACITY_ENV, "2")
    monkeypatch.setenv(BACKGROUND_PROVIDER_NAMESPACE_ENV, "foreground-control")
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._resolve_task_provider_model",
        lambda *_args, **_kwargs: ("custom", "model", None, "token"),
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._get_cached_client",
        lambda *_args, **_kwargs: (client, "model"),
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._resolve_task_reasoning_effort",
        lambda *_args, **_kwargs: None,
    )
    actors_ready = threading.Barrier(3, timeout=2.0)
    release_actors = threading.Event()

    def hold_actor() -> None:
        with background_actor_lease():
            actors_ready.wait()
            release_actors.wait(2.0)

    holders = [threading.Thread(target=hold_actor, daemon=True) for _index in range(2)]
    for holder in holders:
        holder.start()
    actors_ready.wait()

    completed = threading.Event()

    def foreground_control() -> None:
        call_llm(task="orchestration", messages=[{"role": "user", "content": "route"}])
        completed.set()

    control = threading.Thread(target=foreground_control, daemon=True)
    control.start()
    try:
        assert completed.wait(0.5)
    finally:
        release_actors.set()
        control.join(timeout=2.0)
        for holder in holders:
            holder.join(timeout=2.0)


def test_resolved_call_identity_is_credential_free_and_normalizes_main(monkeypatch):
    secret = "sk-route-secret-that-must-not-be-recorded"
    client = SimpleNamespace(base_url="https://inference.example.test/v1")
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._resolve_task_provider_model",
        lambda *_args, **_kwargs: ("main", "zai-org/GLM-5.2", None, secret),
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._get_cached_client",
        lambda *_args, **_kwargs: (client, "zai-org/GLM-5.2"),
    )

    identity = resolve_auxiliary_call_identity("orchestration")

    assert identity.provider == "custom"
    assert identity.model == "zai-org/GLM-5.2"
    assert secret not in repr(identity)


@pytest.fixture
def codex_auth_dir(tmp_path, monkeypatch):
    """Provide a writable ~/.codex/ directory with a valid auth.json."""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "codex-test-token-abc123",
                    "refresh_token": "codex-refresh-xyz",
                }
            }
        )
    )
    monkeypatch.setattr(
        "agent.providers.auxiliary_client._read_codex_access_token",
        lambda: "codex-test-token-abc123",
    )
    return codex_dir


class TestReadCodexAccessToken:
    def test_valid_auth_store(self, tmp_path, monkeypatch):
        leanflow_home = tmp_path / "leanflow"
        leanflow_home.mkdir(parents=True, exist_ok=True)
        (leanflow_home / "auth.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {
                        "openai-codex": {
                            "tokens": {"access_token": "tok-123", "refresh_token": "r-456"},
                        },
                    },
                }
            )
        )
        monkeypatch.setenv("LEANFLOW_HOME", str(leanflow_home))
        result = _read_codex_access_token()
        assert result == "tok-123"

    def test_missing_returns_none(self, tmp_path, monkeypatch):
        leanflow_home = tmp_path / "leanflow"
        leanflow_home.mkdir(parents=True, exist_ok=True)
        (leanflow_home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
        monkeypatch.setenv("LEANFLOW_HOME", str(leanflow_home))
        result = _read_codex_access_token()
        assert result is None

    def test_empty_token_returns_none(self, tmp_path, monkeypatch):
        leanflow_home = tmp_path / "leanflow"
        leanflow_home.mkdir(parents=True, exist_ok=True)
        (leanflow_home / "auth.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {
                        "openai-codex": {
                            "tokens": {"access_token": "  ", "refresh_token": "r"},
                        },
                    },
                }
            )
        )
        monkeypatch.setenv("LEANFLOW_HOME", str(leanflow_home))
        result = _read_codex_access_token()
        assert result is None

    def test_malformed_json_returns_none(self, tmp_path, monkeypatch):
        leanflow_home = tmp_path / "leanflow"
        leanflow_home.mkdir()
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text("{bad json")
        monkeypatch.setenv("LEANFLOW_HOME", str(leanflow_home))
        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setenv("LEANFLOW_USE_LEGACY_CODEX_AUTH", "1")
        result = _read_codex_access_token()
        assert result is None

    def test_missing_tokens_key_returns_none(self, tmp_path, monkeypatch):
        leanflow_home = tmp_path / "leanflow"
        leanflow_home.mkdir()
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "auth.json").write_text(json.dumps({"other": "data"}))
        monkeypatch.setenv("LEANFLOW_HOME", str(leanflow_home))
        monkeypatch.setenv("CODEX_HOME", str(codex_dir))
        monkeypatch.setenv("LEANFLOW_USE_LEGACY_CODEX_AUTH", "1")
        result = _read_codex_access_token()
        assert result is None


class TestGetTextAuxiliaryClient:
    """Test the full resolution chain for get_text_auxiliary_client."""

    def test_openrouter_takes_priority(self, monkeypatch, codex_auth_dir):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        with patch("agent.providers.auxiliary_client.OpenAI") as mock_openai:
            client, model = get_text_auxiliary_client()
        assert model == "google/gemini-3-flash-preview"
        mock_openai.assert_called_once()
        call_kwargs = mock_openai.call_args
        assert call_kwargs.kwargs["api_key"] == "or-key"

    def test_nous_takes_priority_over_codex(self, monkeypatch, codex_auth_dir):
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth") as mock_nous,
            patch("agent.providers.auxiliary_client.OpenAI") as mock_openai,
        ):
            mock_nous.return_value = {"access_token": "nous-tok"}
            client, model = get_text_auxiliary_client()
        assert model == "gemini-3-flash"

    def test_custom_endpoint_over_codex(self, monkeypatch, codex_auth_dir):
        monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:1234/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "lm-studio-key")
        monkeypatch.setenv("OPENAI_MODEL", "my-local-model")
        # Override the autouse monkeypatch for codex
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._read_codex_access_token",
            lambda: "codex-test-token-abc123",
        )
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client.OpenAI") as mock_openai,
        ):
            client, model = get_text_auxiliary_client()
        assert model == "my-local-model"
        call_kwargs = mock_openai.call_args
        assert call_kwargs.kwargs["base_url"] == "http://localhost:1234/v1"

    def test_task_direct_endpoint_override(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_BASE_URL", "http://localhost:2345/v1")
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_API_KEY", "task-key")
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_MODEL", "task-model")
        with patch("agent.providers.auxiliary_client.OpenAI") as mock_openai:
            client, model = get_text_auxiliary_client("web_extract")
        assert model == "task-model"
        assert mock_openai.call_args.kwargs["base_url"] == "http://localhost:2345/v1"
        assert mock_openai.call_args.kwargs["api_key"] == "task-key"

    def test_task_direct_endpoint_without_openai_key_does_not_fall_back(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_BASE_URL", "http://localhost:2345/v1")
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_MODEL", "task-model")
        with patch("agent.providers.auxiliary_client.OpenAI") as mock_openai:
            client, model = get_text_auxiliary_client("web_extract")
        assert client is None
        assert model is None
        mock_openai.assert_not_called()

    def test_custom_endpoint_uses_config_saved_base_url(self, monkeypatch):
        config = {
            "model": {
                "provider": "custom",
                "base_url": "http://localhost:1234/v1",
                "default": "my-local-model",
            }
        }
        monkeypatch.setenv("OPENAI_API_KEY", "lm-studio-key")
        monkeypatch.setattr("leanflow_cli.config.load_config", lambda: config)
        monkeypatch.setattr("leanflow_cli.runtime.runtime_provider.load_config", lambda: config)

        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client._read_codex_access_token", return_value=None),
            patch(
                "agent.providers.auxiliary_client._resolve_api_key_provider",
                return_value=(None, None),
            ),
            patch("agent.providers.auxiliary_client.OpenAI") as mock_openai,
        ):
            client, model = get_text_auxiliary_client()

        assert client is not None
        assert model == "my-local-model"
        call_kwargs = mock_openai.call_args
        assert call_kwargs.kwargs["base_url"] == "http://localhost:1234/v1"

    def test_codex_fallback_when_nothing_else(self, codex_auth_dir):
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client.OpenAI") as mock_openai,
        ):
            client, model = get_text_auxiliary_client()
        assert model == "gpt-5.2-codex"
        # Returns a CodexAuxiliaryClient wrapper, not a raw OpenAI client
        from agent.providers.auxiliary_client import CodexAuxiliaryClient

        assert isinstance(client, CodexAuxiliaryClient)

    def test_returns_none_when_nothing_available(self, monkeypatch):
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client._read_codex_access_token", return_value=None),
            patch(
                "agent.providers.auxiliary_client._resolve_api_key_provider",
                return_value=(None, None),
            ),
        ):
            client, model = get_text_auxiliary_client()
        assert client is None
        assert model is None


class TestGetAuxiliaryProvider:
    """Tests for _get_auxiliary_provider env var resolution."""

    def test_no_task_returns_auto(self):
        assert _get_auxiliary_provider() == "auto"
        assert _get_auxiliary_provider("") == "auto"

    def test_auxiliary_prefix_takes_priority(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_PROVIDER", "openrouter")
        assert _get_auxiliary_provider("web_extract") == "openrouter"

    def test_context_prefix_fallback(self, monkeypatch):
        monkeypatch.setenv("CONTEXT_COMPRESSION_PROVIDER", "nous")
        assert _get_auxiliary_provider("compression") == "nous"

    def test_auxiliary_prefix_over_context_prefix(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_COMPRESSION_PROVIDER", "openrouter")
        monkeypatch.setenv("CONTEXT_COMPRESSION_PROVIDER", "nous")
        assert _get_auxiliary_provider("compression") == "openrouter"

    def test_auto_value_treated_as_auto(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_PROVIDER", "auto")
        assert _get_auxiliary_provider("web_extract") == "auto"

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_PROVIDER", "  openrouter  ")
        assert _get_auxiliary_provider("web_extract") == "openrouter"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_PROVIDER", "OpenRouter")
        assert _get_auxiliary_provider("web_extract") == "openrouter"

    def test_main_provider(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_PROVIDER", "main")
        assert _get_auxiliary_provider("web_extract") == "main"


class TestResolveForcedProvider:
    """Tests for _resolve_forced_provider with explicit provider selection."""

    def test_forced_openrouter(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        with patch("agent.providers.auxiliary_client.OpenAI") as mock_openai:
            client, model = _resolve_forced_provider("openrouter")
        assert model == "google/gemini-3-flash-preview"
        assert client is not None

    def test_forced_openrouter_no_key(self, monkeypatch):
        with patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None):
            client, model = _resolve_forced_provider("openrouter")
        assert client is None
        assert model is None

    def test_forced_nous(self, monkeypatch):
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth") as mock_nous,
            patch("agent.providers.auxiliary_client.OpenAI"),
        ):
            mock_nous.return_value = {"access_token": "nous-tok"}
            client, model = _resolve_forced_provider("nous")
        assert model == "gemini-3-flash"
        assert client is not None

    def test_forced_nous_not_configured(self, monkeypatch):
        with patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None):
            client, model = _resolve_forced_provider("nous")
        assert client is None
        assert model is None

    def test_forced_main_uses_custom(self, monkeypatch):
        monkeypatch.setenv("OPENAI_BASE_URL", "http://local:8080/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "local-key")
        monkeypatch.setenv("OPENAI_MODEL", "my-local-model")
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client.OpenAI") as mock_openai,
        ):
            client, model = _resolve_forced_provider("main")
        assert model == "my-local-model"

    def test_forced_main_uses_config_saved_custom_endpoint(self, monkeypatch):
        config = {
            "model": {
                "provider": "custom",
                "base_url": "http://local:8080/v1",
                "default": "my-local-model",
            }
        }
        monkeypatch.setenv("OPENAI_API_KEY", "local-key")
        monkeypatch.setattr("leanflow_cli.config.load_config", lambda: config)
        monkeypatch.setattr("leanflow_cli.runtime.runtime_provider.load_config", lambda: config)
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client._read_codex_access_token", return_value=None),
            patch(
                "agent.providers.auxiliary_client._resolve_api_key_provider",
                return_value=(None, None),
            ),
            patch("agent.providers.auxiliary_client.OpenAI") as mock_openai,
        ):
            client, model = _resolve_forced_provider("main")
        assert client is not None
        assert model == "my-local-model"
        call_kwargs = mock_openai.call_args
        assert call_kwargs.kwargs["base_url"] == "http://local:8080/v1"

    def test_forced_main_skips_openrouter_nous(self, monkeypatch):
        """Even if OpenRouter key is set, 'main' skips it."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        monkeypatch.setenv("OPENAI_BASE_URL", "http://local:8080/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "local-key")
        monkeypatch.setenv("OPENAI_MODEL", "my-local-model")
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client.OpenAI") as mock_openai,
        ):
            client, model = _resolve_forced_provider("main")
        # Should use custom endpoint, not OpenRouter
        assert model == "my-local-model"

    def test_forced_main_falls_to_codex(self, codex_auth_dir, monkeypatch):
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client.OpenAI"),
        ):
            client, model = _resolve_forced_provider("main")
        from agent.providers.auxiliary_client import CodexAuxiliaryClient

        assert isinstance(client, CodexAuxiliaryClient)
        assert model == "gpt-5.2-codex"

    def test_forced_codex(self, codex_auth_dir, monkeypatch):
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client.OpenAI"),
        ):
            client, model = _resolve_forced_provider("codex")
        from agent.providers.auxiliary_client import CodexAuxiliaryClient

        assert isinstance(client, CodexAuxiliaryClient)
        assert model == "gpt-5.2-codex"

    def test_forced_codex_no_token(self, monkeypatch):
        with patch("agent.providers.auxiliary_client._read_codex_access_token", return_value=None):
            client, model = _resolve_forced_provider("codex")
        assert client is None
        assert model is None

    def test_forced_unknown_returns_none(self, monkeypatch):
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client._read_codex_access_token", return_value=None),
        ):
            client, model = _resolve_forced_provider("invalid-provider")
        assert client is None
        assert model is None


class TestTaskSpecificOverrides:
    """Integration tests for per-task provider routing via get_text_auxiliary_client(task=...)."""

    def test_text_with_web_extract_provider_override(self, monkeypatch):
        """A per-task override (AUXILIARY_WEB_EXTRACT_PROVIDER) should not affect text tasks."""
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_PROVIDER", "nous")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        with patch("agent.providers.auxiliary_client.OpenAI"):
            client, model = get_text_auxiliary_client()  # no task → auto
        assert model == "google/gemini-3-flash-preview"  # OpenRouter, not Nous

    def test_compression_task_reads_context_prefix(self, monkeypatch):
        """Compression task should check CONTEXT_COMPRESSION_PROVIDER."""
        monkeypatch.setenv("CONTEXT_COMPRESSION_PROVIDER", "nous")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")  # would win in auto
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth") as mock_nous,
            patch("agent.providers.auxiliary_client.OpenAI"),
        ):
            mock_nous.return_value = {"access_token": "nous-tok"}
            client, model = get_text_auxiliary_client("compression")
        assert model == "gemini-3-flash"  # forced to Nous, not OpenRouter

    def test_web_extract_task_override(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_WEB_EXTRACT_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        with patch("agent.providers.auxiliary_client.OpenAI"):
            client, model = get_text_auxiliary_client("web_extract")
        assert model == "google/gemini-3-flash-preview"

    def test_task_direct_endpoint_from_config(self, monkeypatch, tmp_path):
        leanflow_home = tmp_path / "leanflow"
        leanflow_home.mkdir(parents=True, exist_ok=True)
        (leanflow_home / "config.yaml").write_text("""auxiliary:
  web_extract:
    base_url: http://localhost:3456/v1
    api_key: config-key
    model: config-model
""")
        monkeypatch.setenv("LEANFLOW_HOME", str(leanflow_home))
        with patch("agent.providers.auxiliary_client.OpenAI") as mock_openai:
            client, model = get_text_auxiliary_client("web_extract")
        assert model == "config-model"
        assert mock_openai.call_args.kwargs["base_url"] == "http://localhost:3456/v1"
        assert mock_openai.call_args.kwargs["api_key"] == "config-key"

    def test_task_without_override_uses_auto(self, monkeypatch):
        """A task with no provider env var falls through to auto chain."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        with patch("agent.providers.auxiliary_client.OpenAI"):
            client, model = get_text_auxiliary_client("compression")
        assert model == "google/gemini-3-flash-preview"  # auto → OpenRouter


class TestAuxiliaryMaxTokensParam:
    def test_codex_fallback_uses_max_tokens(self, monkeypatch):
        """Codex adapter translates max_tokens internally, so we return max_tokens."""
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client._read_codex_access_token", return_value="tok"),
        ):
            result = auxiliary_max_tokens_param(1024)
        assert result == {"max_tokens": 1024}

    def test_openrouter_uses_max_tokens(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        result = auxiliary_max_tokens_param(1024)
        assert result == {"max_tokens": 1024}

    def test_no_provider_uses_max_tokens(self):
        with (
            patch("agent.providers.auxiliary_client._read_nous_auth", return_value=None),
            patch("agent.providers.auxiliary_client._read_codex_access_token", return_value=None),
        ):
            result = auxiliary_max_tokens_param(1024)
        assert result == {"max_tokens": 1024}


class TestAsyncAuxiliaryLifecycle:
    def test_codex_responses_adapter_repairs_empty_final_from_stream_delta(self):
        class _Stream:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def __iter__(self):
                return iter(
                    (
                        SimpleNamespace(
                            type="response.output_text.delta",
                            delta="PASS",
                        ),
                    )
                )

            def get_final_response(self):
                return SimpleNamespace(output=[], usage=None)

        class _Responses:
            def stream(self, **_kwargs):
                return _Stream()

        class _Client:
            responses = _Responses()

        adapter = _CodexCompletionsAdapter(_Client(), "codex-model")

        result = adapter.create(messages=[{"role": "user", "content": "review"}])

        assert result.choices[0].message.content == "PASS"

    def test_codex_responses_adapter_forwards_timeout(self):
        captured: dict = {}

        class _Stream:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def __iter__(self):
                return iter(())

            def get_final_response(self):
                return SimpleNamespace(output=[], usage=None)

        class _Responses:
            def stream(self, **kwargs):
                captured.update(kwargs)
                return _Stream()

        class _Client:
            def __init__(self):
                self.responses = _Responses()

            def with_options(self, **kwargs):
                captured["client_options"] = kwargs
                return self

        adapter = _CodexCompletionsAdapter(
            _Client(),
            "codex-model",
        )

        adapter.create(
            messages=[{"role": "user", "content": "extract"}],
            timeout=12.5,
        )

        assert captured["timeout"] == 12.5
        assert captured["client_options"] == {"max_retries": 0}

    def test_async_call_closes_client_in_same_loop(self, monkeypatch):
        class _Completions:
            async def create(self, **_kwargs):
                return SimpleNamespace(choices=[])

        class _Client:
            def __init__(self):
                self.chat = SimpleNamespace(completions=_Completions())
                self.closed = 0

            async def close(self):
                self.closed += 1

        client = _Client()
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._resolve_task_provider_model",
            lambda *_args, **_kwargs: ("codex", "codex-model", None, None),
        )
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._get_cached_client",
            lambda *_args, **_kwargs: (client, "codex-model"),
        )

        response = asyncio.run(
            async_call_llm(
                task="web_extract",
                messages=[{"role": "user", "content": "extract"}],
            )
        )

        assert response.choices == []
        assert client.closed == 1

    def test_async_clients_are_not_cached_across_event_loops(self, monkeypatch):
        clients = [object(), object()]
        monkeypatch.setattr(
            "agent.providers.auxiliary_client.resolve_provider_client",
            lambda *_args, **_kwargs: (clients.pop(0), "model"),
        )

        first, _model = _get_cached_client(
            "custom",
            "model",
            async_mode=True,
            base_url="https://example.test/v1",
            api_key="token",
        )
        second, _model = _get_cached_client(
            "custom",
            "model",
            async_mode=True,
            base_url="https://example.test/v1",
            api_key="token",
        )

        assert first is not second


class TestLeanReasoningBudget:
    def test_lean_reasoning_effort_reads_config(self, monkeypatch):
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: {"auxiliary": {"lean_reasoning": {"reasoning_effort": "high"}}},
        )

        assert _resolve_task_reasoning_effort("lean_reasoning") == "high"

    def test_lean_reasoning_effort_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_LEAN_REASONING_REASONING_EFFORT", "medium")
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: {"auxiliary": {"lean_reasoning": {"reasoning_effort": "high"}}},
        )

        assert _resolve_task_reasoning_effort("lean_reasoning") == "medium"

    def test_lean_decompose_helpers_inherits_lean_reasoning_config(self, monkeypatch):
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: {
                "auxiliary": {
                    "lean_reasoning": {
                        "provider": "main",
                        "model": "reasoner/model",
                        "reasoning_effort": "high",
                    },
                    "lean_decompose_helpers": {},
                }
            },
        )

        assert _resolve_task_provider_model("lean_decompose_helpers") == (
            "main",
            "reasoner/model",
            None,
            None,
        )
        assert _resolve_task_reasoning_effort("lean_decompose_helpers") == "high"

    def test_lean_decompose_helpers_own_config_overrides_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: {
                "auxiliary": {
                    "lean_reasoning": {
                        "provider": "main",
                        "model": "reasoner/model",
                        "reasoning_effort": "high",
                    },
                    "lean_decompose_helpers": {
                        "provider": "openrouter",
                        "model": "planner/model",
                        "reasoning_effort": "medium",
                    },
                }
            },
        )

        assert _resolve_task_provider_model("lean_decompose_helpers") == (
            "openrouter",
            "planner/model",
            None,
            None,
        )
        assert _resolve_task_reasoning_effort("lean_decompose_helpers") == "medium"

    def test_lean_decompose_helpers_env_overrides_own_and_fallback_config(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_LEAN_DECOMPOSE_HELPERS_PROVIDER", "custom-provider")
        monkeypatch.setenv("AUXILIARY_LEAN_DECOMPOSE_HELPERS_MODEL", "env/planner")
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: {
                "auxiliary": {
                    "lean_reasoning": {"provider": "main", "model": "reasoner/model"},
                    "lean_decompose_helpers": {"provider": "openrouter", "model": "planner/model"},
                }
            },
        )

        assert _resolve_task_provider_model("lean_decompose_helpers") == (
            "custom-provider",
            "env/planner",
            None,
            None,
        )

    def test_manager_nudge_uses_auto_provider_with_low_reasoning(self, monkeypatch):
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: {
                "auxiliary": {
                    "lean_reasoning": {
                        "provider": "codex",
                        "model": "reasoner/model",
                        "reasoning_effort": "high",
                    },
                    "manager_nudge": {
                        "provider": "auto",
                        "reasoning_effort": "low",
                    },
                }
            },
        )

        assert _resolve_task_provider_model("manager_nudge") == (
            "auto",
            None,
            None,
            None,
        )
        assert _resolve_task_reasoning_effort("manager_nudge") == "low"

    def test_orchestration_defaults_to_non_thinking_json_turn(self, monkeypatch):
        monkeypatch.delenv("AUXILIARY_ORCHESTRATION_REASONING_EFFORT", raising=False)
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: {"auxiliary": {"orchestration": {"reasoning_effort": ""}}},
        )

        assert _resolve_task_reasoning_effort("orchestration") == "off"
        assert _resolve_task_reasoning_effort("statement_fidelity") == "off"

    def test_planner_synthesis_defaults_to_non_thinking_json_turn(self, monkeypatch):
        monkeypatch.delenv(
            "AUXILIARY_PLANNER_SYNTHESIS_REASONING_EFFORT",
            raising=False,
        )
        monkeypatch.delenv("AUXILIARY_ORCHESTRATION_REASONING_EFFORT", raising=False)
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: {
                "auxiliary": {
                    "planner_synthesis": {"reasoning_effort": ""},
                    "orchestration": {"reasoning_effort": ""},
                }
            },
        )

        assert _resolve_task_reasoning_effort("planner_synthesis") == "off"

    def test_planner_synthesis_reasoning_overrides_default(self, monkeypatch):
        config = {
            "auxiliary": {
                "planner_synthesis": {"reasoning_effort": ""},
                "orchestration": {"reasoning_effort": "high"},
            }
        }
        monkeypatch.delenv(
            "AUXILIARY_PLANNER_SYNTHESIS_REASONING_EFFORT",
            raising=False,
        )
        monkeypatch.delenv("AUXILIARY_ORCHESTRATION_REASONING_EFFORT", raising=False)
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: config,
        )

        assert _resolve_task_reasoning_effort("planner_synthesis") == "high"
        config["auxiliary"]["planner_synthesis"]["reasoning_effort"] = "medium"
        assert _resolve_task_reasoning_effort("planner_synthesis") == "medium"
        monkeypatch.setenv("AUXILIARY_PLANNER_SYNTHESIS_REASONING_EFFORT", "low")
        assert _resolve_task_reasoning_effort("planner_synthesis") == "low"

    def test_statement_fidelity_inherits_main_orchestration_endpoint(self, monkeypatch):
        monkeypatch.setattr(
            "agent.providers.auxiliary_client._load_runtime_config",
            lambda: {
                "auxiliary": {
                    "orchestration": {
                        "provider": "main",
                        "model": "zai-org/GLM-5.2",
                    }
                }
            },
        )

        assert _resolve_task_provider_model("statement_fidelity") == (
            "main",
            "zai-org/GLM-5.2",
            None,
            None,
        )

    def test_rcp_orchestration_can_disable_thinking(self):
        kwargs = _build_call_kwargs(
            "main",
            "zai-org/GLM-5.2",
            [{"role": "user", "content": "route"}],
            max_tokens=2000,
            base_url="https://inference.rcp.epfl.ch/v1",
            reasoning_effort="off",
        )

        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
        assert "reasoning_effort" not in kwargs["extra_body"]

    def test_rcp_main_route_gets_high_reasoning_budget(self):
        kwargs = _build_call_kwargs(
            "main",
            "moonshotai/Kimi-K2.6-int4",
            [{"role": "user", "content": "prove"}],
            max_tokens=5000,
            base_url="https://inference.rcp.epfl.ch/v1",
            reasoning_effort="high",
        )

        assert kwargs["max_tokens"] == 5000
        assert kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
        assert kwargs["extra_body"]["reasoning_effort"] == "high"
