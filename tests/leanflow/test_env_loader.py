from __future__ import annotations

import os

from leanflow_cli.runtime.env_loader import (
    NATIVE_AUXILIARY_API_KEY_ENV,
    NATIVE_AUXILIARY_BASE_URL_ENV,
    NATIVE_AUXILIARY_MODEL_ENV,
    NATIVE_AUXILIARY_PROVIDER_ENV,
    NATIVE_AUXILIARY_PROVIDER_TARGETS,
    NATIVE_AUXILIARY_REASONING_EFFORT_ENV,
    _native_auxiliary_targets,
    load_leanflow_dotenv,
    reassert_native_auxiliary_provider,
)


def _clean_env(monkeypatch, *names: str) -> None:
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_load_leanflow_dotenv_reads_home_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("LEANFLOW_TEST_KEY=home-value\n", encoding="utf-8")
    _clean_env(monkeypatch, "LEANFLOW_TEST_KEY")

    loaded = load_leanflow_dotenv(leanflow_home=home)

    assert loaded == [home / ".env"]
    assert os.environ["LEANFLOW_TEST_KEY"] == "home-value"


def test_load_leanflow_dotenv_returns_empty_when_no_env_files(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    loaded = load_leanflow_dotenv(leanflow_home=home)

    assert loaded == []


def test_load_leanflow_dotenv_project_env_supplements_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("FROM_HOME=home-only\n", encoding="utf-8")
    project_env = tmp_path / "project" / ".env"
    project_env.parent.mkdir()
    project_env.write_text("FROM_PROJECT=project-only\n", encoding="utf-8")
    _clean_env(monkeypatch, "FROM_HOME", "FROM_PROJECT")

    loaded = load_leanflow_dotenv(leanflow_home=home, project_env=project_env)

    assert loaded == [home / ".env", project_env]
    assert os.environ["FROM_HOME"] == "home-only"
    assert os.environ["FROM_PROJECT"] == "project-only"


def test_load_leanflow_dotenv_preserves_existing_process_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("OVERRIDE_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OVERRIDE_KEY", "from-shell")

    load_leanflow_dotenv(leanflow_home=home)

    assert os.environ["OVERRIDE_KEY"] == "from-shell"


def test_load_leanflow_dotenv_project_does_not_override_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("SHARED_KEY=home-wins\n", encoding="utf-8")
    project_env = tmp_path / "project" / ".env"
    project_env.parent.mkdir()
    project_env.write_text("SHARED_KEY=project-loses\n", encoding="utf-8")
    _clean_env(monkeypatch, "SHARED_KEY")

    load_leanflow_dotenv(leanflow_home=home, project_env=project_env)

    # When both exist, home .env is loaded first with override; project loads with override=False.
    assert os.environ["SHARED_KEY"] == "home-wins"


def test_load_leanflow_dotenv_falls_back_to_latin1_on_decode_error(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # Write bytes that are not valid UTF-8 (0xFF) but are valid latin-1.
    (home / ".env").write_bytes(b"LATIN_KEY=caf\xe9\n")
    _clean_env(monkeypatch, "LATIN_KEY")

    loaded = load_leanflow_dotenv(leanflow_home=home)

    assert loaded == [home / ".env"]
    # The value should decode under latin-1 even though utf-8 would have failed.
    assert "LATIN_KEY" in os.environ


def test_load_leanflow_dotenv_accepts_explicit_home_kwarg(monkeypatch, tmp_path):
    explicit_home = tmp_path / "explicit"
    explicit_home.mkdir()
    (explicit_home / ".env").write_text("EXPLICIT_ONLY_KEY=yes\n", encoding="utf-8")
    _clean_env(monkeypatch, "EXPLICIT_ONLY_KEY")

    loaded = load_leanflow_dotenv(leanflow_home=explicit_home)

    assert loaded == [explicit_home / ".env"]
    assert os.environ["EXPLICIT_ONLY_KEY"] == "yes"


def test_leanflow_env_files_cannot_replace_native_runtime_setup(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    names = (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_HOME",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "LEMEX_HOME",
        "LEMEX_API_KEY",
        "LEMEX_BASE_URL",
        "LEANFLOW_OPENAI_API_KEY",
        "LEANFLOW_NATIVE_API_KEY",
        "LEANFLOW_NATIVE_BASE_URL",
    )
    (home / ".env").write_text("".join(f"{name}=stale-file-value\n" for name in names))
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEMEX_API_KEY", "exported-native-key")
    monkeypatch.setenv("LEMEX_BASE_URL", "https://inference.rcp.epfl.ch/v1")
    load_leanflow_dotenv(leanflow_home=home)
    for name in names:
        if name not in {"LEMEX_API_KEY", "LEMEX_BASE_URL"}:
            assert name not in os.environ
    assert os.environ["LEMEX_API_KEY"] == "exported-native-key"
    assert os.environ["LEMEX_BASE_URL"] == "https://inference.rcp.epfl.ch/v1"


def test_auxiliary_override_does_not_relabel_native_credentials(monkeypatch):
    monkeypatch.setenv(NATIVE_AUXILIARY_PROVIDER_ENV, "lemex")
    monkeypatch.setenv(NATIVE_AUXILIARY_API_KEY_ENV, "obsolete-secret")
    monkeypatch.setenv(NATIVE_AUXILIARY_BASE_URL_ENV, "https://obsolete.example")
    monkeypatch.setenv(NATIVE_AUXILIARY_MODEL_ENV, "chosen-model")
    monkeypatch.setenv(NATIVE_AUXILIARY_REASONING_EFFORT_ENV, "high")
    for name in _native_auxiliary_targets("API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert reassert_native_auxiliary_provider() == "lemex"
    for name in NATIVE_AUXILIARY_PROVIDER_TARGETS:
        assert os.environ[name] == "lemex"
    for name in _native_auxiliary_targets("API_KEY"):
        assert name not in os.environ


def test_reassert_native_auxiliary_provider_is_noop_without_override(monkeypatch):
    monkeypatch.delenv(NATIVE_AUXILIARY_PROVIDER_ENV, raising=False)
    monkeypatch.setenv("AUXILIARY_ORCHESTRATION_PROVIDER", "custom")

    provider = reassert_native_auxiliary_provider()

    assert provider == ""
    assert os.environ["AUXILIARY_ORCHESTRATION_PROVIDER"] == "custom"


def test_workflow_tool_credentials_are_not_native_provider_configuration(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("BRAVE_API_KEY=tool-secret\n")
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    load_leanflow_dotenv(leanflow_home=tmp_path)
    assert os.environ["BRAVE_API_KEY"] == "tool-secret"
