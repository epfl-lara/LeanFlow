"""Reject custom native prompt overrides before a restricted agent can load them."""

from __future__ import annotations

import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import BackendError, HarnessRequest

_MAX_CONFIG_BYTES = 1024 * 1024
_PROMPT_FIELDS = (
    "model_instructions_file",
    "experimental_compact_prompt_file",
    "instructions",
    "compact_prompt",
)


def _prompt_fields(config: Mapping[str, Any]) -> set[str]:
    """Find prompt overrides in the base configuration and every named profile.

    Check inactive profiles too: native profile selection and merging remain the
    runtime's responsibility, so this boundary must not guess which one wins.
    Developer instructions are excluded because the restricted invocation clears
    that field explicitly without changing the runtime's built-in model prompt.
    """
    found = {name for name in _PROMPT_FIELDS if config.get(name) not in (None, "")}
    profiles = config.get("profiles", {})
    if isinstance(profiles, Mapping):
        for profile in profiles.values():
            if isinstance(profile, Mapping):
                found.update(name for name in _PROMPT_FIELDS if profile.get(name) not in (None, ""))
    return found


def _read_config(path: Path) -> dict[str, Any]:
    """Read bounded TOML without including its contents in failures or diagnostics."""
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        raise BackendError(
            "Clean room could not inspect native configuration. Check that the runtime's "
            "config.toml and profile configuration files are readable, then retry."
        ) from None
    try:
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_CONFIG_BYTES:
            raise ValueError("Unsupported configuration file")
        with path.open("rb") as stream:
            content = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(content) > _MAX_CONFIG_BYTES:
            raise ValueError("Oversized configuration file")
        return tomllib.loads(content.decode("utf-8"))
    except (OSError, ValueError):
        # TOML errors can contain fragments of credentials or custom prompts.
        raise BackendError(
            "Clean room could not safely parse native configuration. Check that the "
            "runtime's config.toml and profile files are valid TOML regular files "
            "smaller than 1 MiB, then retry."
        ) from None


def validate_restricted_native_context(
    request: HarnessRequest, environment: Mapping[str, str]
) -> None:
    """Refuse native prompt overrides while preserving auth and model configuration.

    Inspect supported local user/system layers and standalone profile files.
    Never treat an ordinary project config.toml as native runtime configuration,
    read the referenced prompt files,
    copy credentials, change native configuration, or print configured values.
    The caller must separately disable native automatic context, exclude prior
    histories and project configuration paths, and use fresh native sessions.
    """
    if not request.restricted_tools or request.backend not in {"codex", "lemex"}:
        return
    home = Path(environment.get("HOME", str(Path.home()))).expanduser()
    native_home = Path(
        environment.get(f"{request.backend.upper()}_HOME") or home / f".{request.backend}"
    ).expanduser()
    try:
        profiles = sorted(
            path for path in native_home.iterdir() if path.name.endswith(".config.toml")
        )
    except FileNotFoundError:
        profiles = []
    except OSError:
        raise BackendError(
            "Clean room could not inspect native profile files. Check the runtime's "
            "configuration directory permissions, then retry."
        ) from None
    system_home = Path("/etc") / request.backend
    paths = (
        native_home / "config.toml",
        *profiles,
        system_home / "config.toml",
        system_home / "managed_config.toml",
    )
    fields = set().union(*(_prompt_fields(_read_config(path)) for path in paths))
    if fields:
        raise BackendError(
            "Clean room cannot use custom native prompt overrides: "
            + ", ".join(sorted(fields))
            + ". Remove these settings from the runtime's config.toml and profile files "
            "before retrying. Native login, API credentials, model and reasoning settings "
            "can remain configured. No agent was started."
        )
