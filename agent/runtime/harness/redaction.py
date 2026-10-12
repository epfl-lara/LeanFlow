"""Redact native transcripts before persistence or editor observation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from agent.accounting.redact import redact_sensitive_text

_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", re.IGNORECASE)


def environment_secrets(environment: Mapping[str, str]) -> tuple[str, ...]:
    """Capture exact native credentials, including keys with no recognizable prefix."""
    return tuple(
        sorted(
            {value for key, value in environment.items() if value and _SECRET_NAME.search(key)},
            key=len,
            reverse=True,
        )
    )


def redact_native_value(value: Any, secrets: tuple[str, ...]) -> Any:
    """Preserve structured observations while forcing credential redaction."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {
            redact_native_value(key, secrets): (
                "[REDACTED]"
                if isinstance(item, str)
                and re.search(
                    r"(?i)(?:api_?key|authorization|password|secret|access_token|refresh_token|auth_token)$",
                    str(key),
                )
                else redact_native_value(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_native_value(item, secrets) for item in value]
    return value
