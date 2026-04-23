from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from epflemma_cli import config as config_mod
from epflemma_cli.config import (
    DEFAULT_CONFIG,
    _transform_legacy_config,
    ensure_epflemma_home,
    get_config_path,
    get_config_value,
    get_env_path,
    get_env_value,
    get_epflemma_home,
    load_config,
    load_env_file,
    save_config,
    save_env_value,
    set_config_value,
)


def test_load_config_returns_defaults_on_fresh_home(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GAUSS_HOME", raising=False)
    monkeypatch.delenv("OPENGAUSS_HOME", raising=False)
    # Prevent legacy ~/.gauss import from leaking host config into the test.
    monkeypatch.setattr(config_mod, "get_legacy_homes", lambda: [])

    config = load_config()

    assert config["model"]["default"] == DEFAULT_CONFIG["model"]["default"]
    assert config["agent"]["reasoning_effort"] == "auto"
    assert config["agent"]["seed"] == 42
    assert config["agent"]["temperature"] == 0.3
    assert config["agent"]["top_p"] is None
    assert config["compression"]["prune_tool_output"] is True
    assert config["compression"]["prune_keep_recent_user_turns"] == 2
    assert config["compression"]["reserved_output_tokens"] == 20000
    assert config["logging"]["preview_chars"] == 900
    assert get_config_path().exists()


def test_load_config_falls_back_to_defaults_on_malformed_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    ensure_epflemma_home()
    get_config_path().write_text("::: not yaml :::\n  - [", encoding="utf-8")

    config = load_config()

    assert config["model"]["default"] == DEFAULT_CONFIG["model"]["default"]


def test_load_config_merges_user_overrides_onto_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    save_config({"model": {"default": "custom/model"}, "agent": {"temperature": 0.1}})

    config = load_config()

    assert config["model"]["default"] == "custom/model"
    # Unspecified agent fields should remain at defaults (deep merge).
    assert config["agent"]["temperature"] == 0.1
    assert config["agent"]["seed"] == 42
    assert config["agent"]["reasoning_effort"] == "auto"


def test_get_and_set_config_value_with_dotted_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    set_config_value("model.default", "alt/model")
    set_config_value("logging.preview_chars", 42)
    set_config_value("new.nested.key", "created")

    assert get_config_value("model.default") == "alt/model"
    assert get_config_value("logging.preview_chars") == 42
    assert get_config_value("new.nested.key") == "created"


def test_get_config_value_returns_default_for_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    assert get_config_value("does.not.exist") is None
    assert get_config_value("does.not.exist", default="fallback") == "fallback"


def test_set_config_value_raises_for_empty_path(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    with pytest.raises(KeyError):
        set_config_value("", "value")


def test_transform_legacy_config_rewrites_toolset_names():
    merged = _transform_legacy_config(
        {
            "gauss": {"project": {"template_source": "tmpl"}},
            "toolsets": ["gauss-native", "gauss-cli", "gauss-voice", "epflemma-native"],
        }
    )

    assert merged["toolsets"] == ["epflemma-native", "epflemma-cli"]


def test_transform_legacy_config_falls_back_to_default_when_all_stripped():
    merged = _transform_legacy_config({"toolsets": ["gauss-voice", "gauss-browser"]})

    assert merged["toolsets"] == ["epflemma-cli"]


def test_env_file_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    save_env_value("GLM_API_KEY", "abc123")
    save_env_value("EPFLEMMA_OPENAI_BASE_URL", "https://rcp.example/v1")

    loaded = load_env_file()
    assert loaded["GLM_API_KEY"] == "abc123"
    assert loaded["EPFLEMMA_OPENAI_BASE_URL"] == "https://rcp.example/v1"


def test_env_file_sorted_and_quoted_free(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    save_env_value("Z_LAST", "z")
    save_env_value("A_FIRST", "a")

    body = get_env_path().read_text(encoding="utf-8")
    lines = [line for line in body.splitlines() if line]
    assert lines == ["A_FIRST=a", "Z_LAST=z"]


def test_env_file_ignores_blank_and_commented_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    ensure_epflemma_home()
    get_env_path().write_text(
        "# a comment\n\n   \nKEY=value\n  # indented comment\n",
        encoding="utf-8",
    )

    assert load_env_file() == {"KEY": "value"}


def test_get_env_value_prefers_os_environ(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    save_env_value("MYKEY", "from-file")

    monkeypatch.setenv("MYKEY", "from-os")

    assert get_env_value("MYKEY") == "from-os"


def test_get_env_value_falls_back_to_env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    save_env_value("FILEONLY", "file-value")
    monkeypatch.delenv("FILEONLY", raising=False)

    assert get_env_value("FILEONLY") == "file-value"


def test_get_env_value_returns_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MISSING_UNIQUE_KEY_42", raising=False)

    assert get_env_value("MISSING_UNIQUE_KEY_42", default="x") == "x"
    assert get_env_value("MISSING_UNIQUE_KEY_42") is None


def test_get_epflemma_home_prefers_explicit_env(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "explicit"))
    monkeypatch.setenv("OPENGAUSS_HOME", str(tmp_path / "branded"))
    monkeypatch.setenv("GAUSS_HOME", str(tmp_path / "legacy"))

    assert get_epflemma_home() == Path(str(tmp_path / "explicit"))


def test_get_epflemma_home_uses_branded_legacy_when_explicit_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("EPFLEMMA_HOME", raising=False)
    monkeypatch.setenv("OPENGAUSS_HOME", str(tmp_path / "branded"))
    monkeypatch.setenv("GAUSS_HOME", str(tmp_path / "legacy"))

    assert get_epflemma_home() == Path(str(tmp_path / "branded"))


def test_get_epflemma_home_ignores_legacy_gauss_unless_dotepflemma(monkeypatch, tmp_path):
    monkeypatch.delenv("EPFLEMMA_HOME", raising=False)
    monkeypatch.delenv("OPENGAUSS_HOME", raising=False)
    # A generic $GAUSS_HOME that does NOT point at an `.epflemma` directory must be ignored
    monkeypatch.setenv("GAUSS_HOME", str(tmp_path / ".gauss"))

    assert get_epflemma_home() == config_mod.EPFLEMMA_HOME_DEFAULT


def test_ensure_epflemma_home_creates_expected_subdirectories(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))

    home = ensure_epflemma_home()

    for sub in ("sessions", "logs", "memories", "workflow-state", "local-models"):
        assert (home / sub).is_dir(), f"{sub} directory was not created"
    assert (home / ".env").exists()
    assert (home / "SOUL.md").exists()


def test_load_config_rewrites_legacy_payload_and_persists_transform(monkeypatch, tmp_path):
    monkeypatch.setenv("EPFLEMMA_HOME", str(tmp_path / "home"))
    ensure_epflemma_home()
    get_config_path().write_text(
        yaml.safe_dump({
            "gauss": {
                "project": {"template_source": "legacy-tmpl"},
                "autoformalize": {"managed_state_dir": ".gauss/workflow-state"},
            },
            "model": {"default": "legacy-model", "provider": "zai"},
            "toolsets": ["gauss-native"],
        }, sort_keys=False),
        encoding="utf-8",
    )

    first = load_config()
    assert first["epflemma"]["project"]["template_source"] == "legacy-tmpl"
    assert first["toolsets"] == ["epflemma-native"]

    # After transform, the on-disk config should have been rewritten with an
    # `epflemma` key and a subsequent load should not re-trigger the transform.
    persisted = yaml.safe_load(get_config_path().read_text(encoding="utf-8"))
    assert "epflemma" in persisted
    assert "gauss" not in persisted
