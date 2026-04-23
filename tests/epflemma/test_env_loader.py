from __future__ import annotations

import os

from epflemma_cli.env_loader import load_epflemma_dotenv


def _clean_env(monkeypatch, *names: str) -> None:
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_load_epflemma_dotenv_reads_home_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("EPFLEMMA_TEST_KEY=home-value\n", encoding="utf-8")
    _clean_env(monkeypatch, "EPFLEMMA_TEST_KEY")

    loaded = load_epflemma_dotenv(epflemma_home=home)

    assert loaded == [home / ".env"]
    assert os.environ["EPFLEMMA_TEST_KEY"] == "home-value"


def test_load_epflemma_dotenv_returns_empty_when_no_env_files(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    loaded = load_epflemma_dotenv(epflemma_home=home)

    assert loaded == []


def test_load_epflemma_dotenv_project_env_supplements_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("FROM_HOME=home-only\n", encoding="utf-8")
    project_env = tmp_path / "project" / ".env"
    project_env.parent.mkdir()
    project_env.write_text("FROM_PROJECT=project-only\n", encoding="utf-8")
    _clean_env(monkeypatch, "FROM_HOME", "FROM_PROJECT")

    loaded = load_epflemma_dotenv(epflemma_home=home, project_env=project_env)

    assert loaded == [home / ".env", project_env]
    assert os.environ["FROM_HOME"] == "home-only"
    assert os.environ["FROM_PROJECT"] == "project-only"


def test_load_epflemma_dotenv_home_overrides_existing_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("OVERRIDE_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OVERRIDE_KEY", "from-shell")

    load_epflemma_dotenv(epflemma_home=home)

    # The home .env is loaded with override=True so it replaces the existing value.
    assert os.environ["OVERRIDE_KEY"] == "from-file"


def test_load_epflemma_dotenv_project_does_not_override_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("SHARED_KEY=home-wins\n", encoding="utf-8")
    project_env = tmp_path / "project" / ".env"
    project_env.parent.mkdir()
    project_env.write_text("SHARED_KEY=project-loses\n", encoding="utf-8")
    _clean_env(monkeypatch, "SHARED_KEY")

    load_epflemma_dotenv(epflemma_home=home, project_env=project_env)

    # When both exist, home .env is loaded first with override; project loads with override=False.
    assert os.environ["SHARED_KEY"] == "home-wins"


def test_load_epflemma_dotenv_falls_back_to_latin1_on_decode_error(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    # Write bytes that are not valid UTF-8 (0xFF) but are valid latin-1.
    (home / ".env").write_bytes(b"LATIN_KEY=caf\xe9\n")
    _clean_env(monkeypatch, "LATIN_KEY")

    loaded = load_epflemma_dotenv(epflemma_home=home)

    assert loaded == [home / ".env"]
    # The value should decode under latin-1 even though utf-8 would have failed.
    assert "LATIN_KEY" in os.environ


def test_load_epflemma_dotenv_accepts_legacy_gauss_home_kwarg(monkeypatch, tmp_path):
    legacy_home = tmp_path / "legacy"
    legacy_home.mkdir()
    (legacy_home / ".env").write_text("LEGACY_ONLY_KEY=yes\n", encoding="utf-8")
    _clean_env(monkeypatch, "LEGACY_ONLY_KEY")

    loaded = load_epflemma_dotenv(gauss_home=legacy_home)

    assert loaded == [legacy_home / ".env"]
    assert os.environ["LEGACY_ONLY_KEY"] == "yes"
