"""Verify native prompt isolation without exposing or modifying native credentials."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.runtime.harness import context_policy
from agent.runtime.harness.context_policy import validate_restricted_native_context
from agent.runtime.harness.models import BackendError, HarnessRequest


def _request(tmp_path: Path, backend: str = "codex", *, restricted: bool = True) -> HarnessRequest:
    """Create a request with a private native home and isolated working directory."""
    return HarnessRequest(
        backend=backend,
        prompt="Prove the assigned statement",
        cwd=tmp_path,
        restricted_tools=restricted,
    )


def _native_config(tmp_path: Path) -> Path:
    """Create a native configuration path separately from ordinary project files."""
    native = tmp_path / ".codex"
    native.mkdir(exist_ok=True)
    return native / "config.toml"


@pytest.mark.parametrize("backend", ["codex", "lemex"])
@pytest.mark.parametrize(
    "setting",
    [
        "model_instructions_file",
        "experimental_compact_prompt_file",
        "instructions",
        "compact_prompt",
    ],
)
def test_rejects_custom_prompts_without_reading_or_disclosing_them(
    tmp_path: Path, backend: str, setting: str
) -> None:
    native = tmp_path / "native"
    native.mkdir()
    config = native / "config.toml"
    content = f'{setting} = "private-key-or-prompt-content"\nmodel = "native-model"\n'
    config.write_text(content)
    with pytest.raises(BackendError) as caught:
        validate_restricted_native_context(
            _request(tmp_path, backend), {f"{backend.upper()}_HOME": str(native)}
        )
    assert setting in str(caught.value)
    assert "private-key-or-prompt-content" not in str(caught.value)
    assert "No agent was started" in str(caught.value)
    assert config.read_text() == content


@pytest.mark.parametrize("standalone", [False, True])
def test_rejects_prompt_overrides_in_inactive_profiles(tmp_path: Path, standalone: bool) -> None:
    native = tmp_path / ".codex"
    native.mkdir()
    path = native / ("other.config.toml" if standalone else "config.toml")
    heading = "" if standalone else '[profiles."secret-profile-name"]\n'
    path.write_text(heading + 'model_instructions_file = "unreadable-prompt.md"\n')
    with pytest.raises(BackendError) as caught:
        validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})
    assert "model_instructions_file" in str(caught.value)
    assert "secret-profile-name" not in str(caught.value)
    assert "unreadable-prompt.md" not in str(caught.value)


def test_preserves_native_model_provider_and_credential_settings(tmp_path: Path) -> None:
    native = tmp_path / ".lemex"
    native.mkdir()
    config = native / "config.toml"
    content = """model = "native-model"
model_provider = "custom"
model_reasoning_effort = "high"
developer_instructions = "Cleared by the restricted command override."
[model_providers.custom]
base_url = "https://models.invalid/v1"
env_key = "LEMEX_API_KEY"
experimental_bearer_token = "private-key"
[profiles.testing]
model = "other-model"
model_reasoning_effort = "low"
"""
    config.write_text(content)
    validate_restricted_native_context(_request(tmp_path, "lemex"), {"HOME": str(tmp_path)})
    assert config.read_text() == content


@pytest.mark.parametrize("backend,restricted", [("codex", False), ("claude", True)])
def test_other_launches_leave_context_to_their_native_policy(
    tmp_path: Path, backend: str, restricted: bool
) -> None:
    _native_config(tmp_path).write_text('model_instructions_file = "custom.md"')
    validate_restricted_native_context(
        _request(tmp_path, backend, restricted=restricted), {"HOME": str(tmp_path)}
    )


def test_ordinary_project_config_is_not_interpreted_as_native_settings(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('compact_prompt = "prior context"')
    validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})
    # Unrelated project configuration need not even use TOML syntax.
    (tmp_path / "config.toml").write_text("ordinary project data: [")
    validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})


@pytest.mark.parametrize("content", [b'instructions = "secret\n', b"\xff", b"#" * 1048577])
def test_unreadable_configuration_fails_without_echoing_contents(
    tmp_path: Path, content: bytes
) -> None:
    _native_config(tmp_path).write_bytes(content)
    with pytest.raises(BackendError) as caught:
        validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})
    assert "secret" not in str(caught.value)
    assert "could not safely parse" in str(caught.value)


def test_nonregular_configuration_is_rejected_without_opening_it(tmp_path: Path) -> None:
    _native_config(tmp_path).mkdir()
    with pytest.raises(BackendError, match="regular files"):
        validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})


def test_missing_or_empty_prompt_settings_do_not_change_native_defaults(tmp_path: Path) -> None:
    validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})
    _native_config(tmp_path).write_text('instructions = ""\ncompact_prompt = ""\n')
    validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})


def test_unrecognized_prompt_fields_are_not_mistaken_for_native_context(tmp_path: Path) -> None:
    _native_config(tmp_path).write_text(
        'model_experimental_instructions_file = "unused.md"\n'
        'experimental_model_instructions_file = "unused.md"\n'
    )
    validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})


def test_system_prompt_configuration_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def read(path: Path) -> dict[str, str]:
        return (
            {"model_instructions_file": "private-path"}
            if path == Path("/etc/codex/config.toml")
            else {}
        )

    monkeypatch.setattr(context_policy, "_read_config", read)
    with pytest.raises(BackendError, match="model_instructions_file"):
        validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})


def test_unreadable_profile_directory_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(path: Path) -> None:
        raise PermissionError("secret detail")

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(BackendError) as caught:
        validate_restricted_native_context(_request(tmp_path), {"HOME": str(tmp_path)})
    assert "configuration directory permissions" in str(caught.value)
    assert "secret detail" not in str(caught.value)
