"""Preserve diagnostic redaction and reject the retired SDK subprocess entrypoint."""

import pytest

from agent.providers import isolated_auxiliary


def test_auxiliary_errors_are_unconditionally_redacted_and_bounded(monkeypatch):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    secrets = (
        "sk-testCredential1234567890",
        "header-token-1234567890",
        "query-key-1234567890",
        "query-token-1234567890",
        "x-header-key-1234567890",
    )
    error = (
        f"OPENAI_API_KEY={secrets[0]} "
        f"Authorization: Bearer {secrets[1]} "
        f"https://provider.invalid/v1?api_key={secrets[2]}&token={secrets[3]} "
        f"x-api-key: {secrets[4]} " + ("detail " * 100)
    )

    sanitized = isolated_auxiliary.sanitize_auxiliary_error(error, limit=180)

    assert len(sanitized) == 180
    assert "[REDACTED]" in sanitized
    assert all(secret not in sanitized for secret in secrets)


def test_auxiliary_error_redacts_unlabelled_jwt_unconditionally(monkeypatch):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    token = (
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwic2NvcGUiOiJjb2RleCJ9."
        "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-"
    )

    sanitized = isolated_auxiliary.sanitize_auxiliary_error(f"provider failure returned {token}.")

    assert sanitized == "provider failure returned [REDACTED]."
    assert token not in sanitized
    assert token[:16] not in sanitized
    assert token[-16:] not in sanitized


@pytest.mark.parametrize(
    "value",
    [
        "release 4.27.0 is installed",
        "connect to api.example.com",
        "resolve namespace.module.identifier",
        "read alpha-beta.gamma_delta.component-name",
        "verylongsubdomainname.verylongdomainlabel.verylongtoplevelname",
        "ordinary_identifier_component.another_identifier_component.final_identifier_component",
        "aaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbb.cccccccccccccccccccc.dddddddddddddddddddd",
    ],
)
def test_auxiliary_error_preserves_ordinary_dotted_text(monkeypatch, value):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")

    assert isolated_auxiliary.sanitize_auxiliary_error(value) == value


@pytest.mark.parametrize(
    "template",
    [
        "OPENAI_API_KEY={secret}",
        "Authorization: Bearer {secret}",
        "https://provider.invalid/v1?api_key={secret}&page=1",
        "https://provider.invalid/v1?key={secret}&page=1",
        "X-API-Key: {secret}",
        '{{"access_token": "{secret}"}}',
    ],
)
def test_auxiliary_error_redaction_covers_common_credential_shapes(monkeypatch, template):
    monkeypatch.setenv("LEANFLOW_REDACT_SECRETS", "0")
    secret = "ordinaryCredentialValue1234567890"

    sanitized = isolated_auxiliary.sanitize_auxiliary_error(template.format(secret=secret))

    assert secret not in sanitized
    assert "[REDACTED]" in sanitized


@pytest.mark.parametrize(
    "entrypoint", [isolated_auxiliary.run_isolated_auxiliary_text, isolated_auxiliary.worker_main]
)
def test_retired_auxiliary_subprocess_cannot_run(entrypoint):
    with pytest.raises(
        isolated_auxiliary.IsolatedAuxiliaryUnavailable, match="native.*codex, claude, or lemex"
    ):
        entrypoint()
