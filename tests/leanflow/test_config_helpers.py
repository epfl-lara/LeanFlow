from __future__ import annotations

from pathlib import Path

import pytest

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
    assert config["model"] == {"default": "", "provider": "codex"}
    for task in config["auxiliary"].values():
        assert task == {"provider": "", "model": "", "reasoning_effort": ""}
    assert config["agent"] == {"reasoning_effort": ""}
    assert config["limits"] == {"agent_calls": 250, "tokens": None, "wall_time_s": 86400}
    assert "compression" not in config
    assert "local_models" not in config
    assert "custom_providers" not in config
    assert config["logging"]["preview_lines"] == 8
    assert config["logging"]["preview_chars"] == 1600
    assert config["logging"]["activity_preview_chars"] == 420
    assert get_config_path().exists()
    rendered = get_config_path().read_text(encoding="utf-8")
    assert "Native backends: codex, claude, lemex" in rendered
    assert "sampling, context compression" in rendered


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
    assert "seed" not in config["agent"]
    assert config["agent"]["reasoning_effort"] == ""


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

    for sub in ("sessions", "logs", "memories", "workflow-state"):
        assert (home / sub).is_dir(), f"{sub} directory was not created"
    assert (home / "config.yaml").exists()
    assert (home / ".env").exists()
    assert (home / "SOUL.md").exists()
    config_text = (home / "config.yaml").read_text(encoding="utf-8")
    assert "Native backends: codex, claude, lemex" in config_text
    assert "lean_decompose_helpers:" in config_text
    assert "manager_nudge:" in config_text
    assert "blueprint_verification:" in config_text
    assert "autoformalizer_verification:" in config_text
    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "LEANFLOW_OPENAI_BASE_URL=" not in env_text
    assert "LEANFLOW_INFERENCE_PROVIDER=" in env_text
    assert "KIMI_API_KEY=" not in env_text
    assert "AUXILIARY_LEAN_REASONING_PROVIDER=" not in env_text


def test_ensure_leanflow_home_backfills_missing_env_template_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    home = Path(str(tmp_path / "home"))
    home.mkdir(parents=True)
    (home / ".env").write_text("LEANFLOW_OPENAI_API_KEY=keep-me\n", encoding="utf-8")

    ensure_leanflow_home()

    env_text = (home / ".env").read_text(encoding="utf-8")
    assert "LEANFLOW_OPENAI_API_KEY=keep-me" in env_text
    assert "LEANFLOW_INFERENCE_PROVIDER=" in env_text
    assert "KIMI_API_KEY=" not in env_text
    assert "AUXILIARY_LEAN_REASONING_PROVIDER=" not in env_text


def test_existing_config_is_preserved_and_defaults_are_only_merged_in_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    original = "# keep my comment\nmodel:\n  provider: lemex\n"
    (home / "config.yaml").write_text(original)
    ensure_leanflow_home()
    assert (home / "config.yaml").read_text() == original
    config = load_config()
    assert config["model"] == {"provider": "lemex", "default": ""}
    assert config["schema_version"] == 2


def test_legacy_config_gets_in_memory_migration_marker_without_rewriting(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_HOME", str(tmp_path / "home"))
    home = tmp_path / "home"
    home.mkdir()
    original = "model:\n  provider: custom\n  default: old-installed-model\n  base_url: ''\nagent:\n  temperature: 0.3\n"
    (home / "config.yaml").write_text(original)
    config = load_config()
    assert config["schema_version"] == 1
    assert config["model"]["default"] == "old-installed-model"
    assert (home / "config.yaml").read_text() == original


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
