from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from leanflow_cli.config import (
    DEFAULT_CONFIG,
    ensure_leanflow_home,
    get_config_path,
    get_config_value,
    get_env_path,
    get_env_value,
    get_leanflow_home,
    load_config,
    load_env_file,
    save_config,
    save_env_value,
    set_config_value,
)


def test_load_config_returns_defaults_on_fresh_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    config = load_config()

    assert config["model"]["default"] == DEFAULT_CONFIG["model"]["default"]
    assert config["model"]["default"] == "moonshotai/Kimi-K2.7-Code"
    assert "context_lengths" not in config["model"]
    assert config["auxiliary"]["lean_reasoning"]["provider"] == "main"
    assert config["auxiliary"]["lean_reasoning"]["model"] == "moonshotai/Kimi-K2.6-int4"
    assert config["auxiliary"]["lean_reasoning"]["reasoning_effort"] == "high"
    assert config["auxiliary"]["lean_decompose_helpers"]["provider"] == ""
    assert config["auxiliary"]["lean_decompose_helpers"]["model"] == ""
    assert config["auxiliary"]["lean_decompose_helpers"]["reasoning_effort"] == ""
    assert config["auxiliary"]["manager_nudge"]["provider"] == "auto"
    assert config["auxiliary"]["manager_nudge"]["model"] == ""
    assert config["auxiliary"]["manager_nudge"]["reasoning_effort"] == "low"
    assert config["auxiliary"]["orchestration"]["reasoning_effort"] == "off"
    assert config["agent"]["max_turns"] == 200
    assert config["agent"]["reasoning_effort"] == "auto"
    assert config["agent"]["seed"] == 42
    assert config["agent"]["temperature"] == 0.3
    assert config["agent"]["top_p"] is None
    assert config["compression"]["prune_tool_output"] is True
    assert config["compression"]["prune_keep_recent_user_turns"] == 2
    assert config["compression"]["reserved_output_tokens"] == 20000
    assert config["compression"]["threshold"] == 0.75
    assert config["logging"]["preview_lines"] == 8
    assert config["logging"]["preview_chars"] == 1600
    assert config["logging"]["activity_preview_chars"] == 420
    assert get_config_path().exists()
    rendered = get_config_path().read_text(encoding="utf-8")
    assert "Main workflow model" in rendered
    assert "Auxiliary theorem advisor" in rendered
    assert "Auxiliary helper decomposer" in rendered
    assert "Persistence coach" in rendered


def test_load_config_falls_back_to_defaults_on_malformed_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    ensure_leanflow_home()
    get_config_path().write_text("::: not yaml :::\n  - [", encoding="utf-8")

    config = load_config()

    assert config["model"]["default"] == DEFAULT_CONFIG["model"]["default"]


def test_load_config_merges_user_overrides_onto_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    save_config({"model": {"default": "custom/model"}, "agent": {"temperature": 0.1}})

    config = load_config()

    assert config["model"]["default"] == "custom/model"
    # Unspecified agent fields should remain at defaults (deep merge).
    assert config["agent"]["temperature"] == 0.1
    assert config["agent"]["seed"] == 42
    assert config["agent"]["reasoning_effort"] == "auto"


def test_get_and_set_config_value_with_dotted_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    set_config_value("model.default", "alt/model")
    set_config_value("logging.preview_chars", 42)
    set_config_value("new.nested.key", "created")

    assert get_config_value("model.default") == "alt/model"
    assert get_config_value("logging.preview_chars") == 42
    assert get_config_value("new.nested.key") == "created"


def test_get_config_value_returns_default_for_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    assert get_config_value("does.not.exist") is None
    assert get_config_value("does.not.exist", default="fallback") == "fallback"


def test_set_config_value_raises_for_empty_path(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    with pytest.raises(KeyError):
        set_config_value("", "value")


def test_env_file_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    save_env_value("GLM_API_KEY", "abc123")
    save_env_value("LEANFLOW_OPENAI_BASE_URL", "https://rcp.example/v1")

    loaded = load_env_file()
    assert loaded["GLM_API_KEY"] == "abc123"
    assert loaded["LEANFLOW_OPENAI_BASE_URL"] == "https://rcp.example/v1"


def test_env_file_sorted_and_quoted_free(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    save_env_value("Z_LAST", "z")
    save_env_value("A_FIRST", "a")

    body = get_env_path().read_text(encoding="utf-8")
    lines = [line for line in body.splitlines() if line and not line.startswith("#")]
    assert "A_FIRST=a" in lines
    assert "Z_LAST=z" in lines
    assert all('"' not in line for line in lines)


def test_env_file_ignores_blank_and_commented_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    ensure_leanflow_home()
    get_env_path().write_text(
        "# a comment\n\n   \nKEY=value\n  # indented comment\n",
        encoding="utf-8",
    )

    loaded = load_env_file()
    assert loaded["KEY"] == "value"
    assert "# a comment" not in loaded


def test_get_env_value_prefers_os_environ(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    save_env_value("MYKEY", "from-file")

    monkeypatch.setenv("MYKEY", "from-os")

    assert get_env_value("MYKEY") == "from-os"


def test_get_env_value_falls_back_to_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    save_env_value("FILEONLY", "file-value")
    monkeypatch.delenv("FILEONLY", raising=False)

    assert get_env_value("FILEONLY") == "file-value"


def test_get_env_value_returns_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MISSING_UNIQUE_KEY_42", raising=False)

    assert get_env_value("MISSING_UNIQUE_KEY_42", default="x") == "x"
    assert get_env_value("MISSING_UNIQUE_KEY_42") is None


def test_get_leanflow_home_prefers_explicit_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "explicit"))

    assert get_leanflow_home() == Path(str(tmp_path / "explicit"))


def test_ensure_leanflow_home_creates_expected_subdirectories(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))

    home = ensure_leanflow_home()

    for sub in ("sessions", "logs", "memories", "workflow-state", "local-models"):
        assert (home / sub).is_dir(), f"{sub} directory was not created"
    assert (home / "config.yaml").exists()
    assert (home / ".env").exists()
    assert (home / "SOUL.md").exists()
    config_text = (home / "config.yaml").read_text(encoding="utf-8")
    assert "Auxiliary helper decomposer" in config_text
    assert "lean_decompose_helpers:" in config_text
    assert "manager_nudge:" in config_text
    assert "blueprint_verification:" in config_text
    assert "autoformalizer_verification:" in config_text
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "LEANFLOW_OPENAI_BASE_URL=" in env_text
    assert "KIMI_API_KEY=" in env_text
    assert "AUXILIARY_LEAN_REASONING_MODEL=" in env_text
    assert "AUXILIARY_LEAN_REASONING_REASONING_EFFORT=" in env_text
    assert "AUXILIARY_LEAN_DECOMPOSE_HELPERS_MODEL=" in env_text
    assert "AUXILIARY_LEAN_DECOMPOSE_HELPERS_REASONING_EFFORT=" in env_text
    assert "AUXILIARY_MANAGER_NUDGE_MODEL=" in env_text
    assert "AUXILIARY_MANAGER_NUDGE_REASONING_EFFORT=" in env_text
    assert "AUXILIARY_ORCHESTRATION_MODEL=" in env_text
    assert "AUXILIARY_ORCHESTRATION_REASONING_EFFORT=" in env_text
    assert "LEANFLOW_ORCHESTRATOR_LLM_TIMEOUT_S=" in env_text
    assert "AUXILIARY_BLUEPRINT_VERIFICATION_PROVIDER=" in env_text
    assert "AUXILIARY_AUTOFORMALIZER_VERIFICATION_PROVIDER=" in env_text


def test_ensure_leanflow_home_backfills_missing_env_template_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    home = Path(str(tmp_path / "home"))
    home.mkdir(parents=True)
    (home / ".env").write_text("LEANFLOW_OPENAI_API_KEY=keep-me\n", encoding="utf-8")

    ensure_leanflow_home()

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "LEANFLOW_OPENAI_API_KEY=keep-me" in env_text
    assert "KIMI_API_KEY=" in env_text
    assert "AUXILIARY_LEAN_REASONING_PROVIDER=" in env_text
    assert "AUXILIARY_LEAN_REASONING_REASONING_EFFORT=" in env_text
    assert "AUXILIARY_LEAN_DECOMPOSE_HELPERS_PROVIDER=" in env_text
    assert "AUXILIARY_LEAN_DECOMPOSE_HELPERS_REASONING_EFFORT=" in env_text
    assert "AUXILIARY_BLUEPRINT_VERIFICATION_PROVIDER=" in env_text
    assert "AUXILIARY_AUTOFORMALIZER_VERIFICATION_PROVIDER=" in env_text


def test_ensure_leanflow_home_backfills_missing_config_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    home = Path(str(tmp_path / "home"))
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "auxiliary": {
                    "lean_reasoning": {
                        "provider": "codex",
                        "model": "",
                    }
                },
                "mcp_servers": {
                    "lean-lsp": {
                        "command": "/tmp/lean-lsp-mcp",
                        "enabled": True,
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    ensure_leanflow_home()

    rendered = (home / "config.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(rendered)
    assert "Auxiliary helper decomposer" in rendered
    assert config["auxiliary"]["lean_reasoning"]["provider"] == "codex"
    assert config["auxiliary"]["lean_decompose_helpers"]["provider"] == ""
    assert config["auxiliary"]["lean_decompose_helpers"]["model"] == ""
    assert config["auxiliary"]["manager_nudge"]["reasoning_effort"] == "low"
    assert config["auxiliary"]["blueprint_verification"]["provider"] == "main"
    assert config["auxiliary"]["autoformalizer_verification"]["provider"] == "local"
    assert config["mcp_servers"]["lean-lsp"]["command"] == "/tmp/lean-lsp-mcp"


def test_load_config_is_cached_and_isolated(monkeypatch, tmp_path):
    # load_config() is hammered in per-item loops (workflow exit cleanup); it must be cheap and
    # must not let callers corrupt the shared cache. Regression for a multi-minute exit hang.
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    from leanflow_cli import config

    config.invalidate_config_cache()
    first = config.load_config()
    # Mutating a returned config must not leak into the cache (deepcopy isolation).
    first["__scratch__"] = 123
    assert "__scratch__" not in config.load_config()

    # A write invalidates the cache so the new value is observed.
    config.set_config_value("logging.activity_preview_chars", 4242)
    assert config.load_config().get("logging", {}).get("activity_preview_chars") == 4242


def test_load_config_cache_keys_on_home(monkeypatch, tmp_path):
    from leanflow_cli import config

    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "a"))
    config.invalidate_config_cache()
    config.set_config_value("logging.activity_preview_chars", 111)
    # Switching home must re-read from the new location, not serve the stale cache.
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "b"))
    assert config.load_config().get("logging", {}).get("activity_preview_chars") != 111
