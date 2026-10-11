"""Check editor setup metadata without loading keys or making model requests."""

from __future__ import annotations

import json

import pytest

from leanflow_cli.runtime import provider_setup as setup


@pytest.fixture(autouse=True)
def native_environment(monkeypatch):
    for name in (*setup.KEY_NAMES.values(), "CODEX_API_KEY", "LEMEX_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(setup, "resolve_backend_executable", lambda backend: f"/bin/{backend}")


def test_presence_never_leaks_keys_or_endpoint_credentials(monkeypatch):
    monkeypatch.setenv("LEMEX_API_KEY", "opaque-secret")
    monkeypatch.setenv(
        "LEMEX_BASE_URL", "https://user:password@example.com/v1?api_key=secret#token"
    )
    monkeypatch.setattr(setup, "_probe", lambda *args: pytest.fail("Unexpected metadata process"))
    row = setup.backend_setup("lemex")
    assert row["auth"] == "environment"
    assert row["endpoint"] == "https://example.com/v1"
    assert "opaque-secret" not in json.dumps(row)
    assert "password" not in json.dumps(row)


@pytest.mark.parametrize("backend", ["codex", "claude"])
def test_native_login_probe_only_returns_typed_status(monkeypatch, backend):
    commands = []

    def probe(executable, args):
        commands.append(args)
        return (
            (0, "native 1.2.3 secret")
            if args == ["--version"]
            else (0, '{"loggedIn":true,"email":"private@example.com"}')
        )

    monkeypatch.setattr(setup, "_probe", probe)
    row = setup.backend_setup(backend, check=True)
    assert row["auth"] == "native_login"
    assert row["version"] == "1.2.3"
    assert "private@example.com" not in json.dumps(row)
    assert commands == [
        ["--version"],
        ["auth", "status", "--json"] if backend == "claude" else ["login", "status"],
    ]


def test_missing_native_cli_and_legacy_default_do_not_hide_setup(monkeypatch):
    def missing(*args):
        raise setup.RuntimeProviderError("legacy config")

    monkeypatch.setattr(setup, "resolve_backend_executable", missing)
    monkeypatch.setattr(setup, "resolve_requested_provider", missing)
    result = setup.provider_setup(check=True)
    assert result["selected"] == ""
    assert len(result["backends"]) == 3
    assert not any(row["installed"] for row in result["backends"])


def test_setup_exposes_leanflow_overrides_separately_from_native_defaults(monkeypatch):
    monkeypatch.setattr(setup, "resolve_requested_provider", lambda requested: "lemex")
    monkeypatch.setattr(
        setup,
        "resolve_runtime_provider",
        lambda **kwargs: {"model": "explicit-model", "reasoning_effort": "max"},
    )
    monkeypatch.setattr(setup, "backend_setup", lambda name, **kwargs: {"id": name})
    result = setup.provider_setup()
    assert result["model_override"] == "explicit-model"
    assert result["reasoning_effort_override"] == "max"
