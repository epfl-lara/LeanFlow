"""Redact credentials from bounded persisted diagnostic text without provider dependencies."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

# Error strings cross the worker boundary and may then be persisted in workflow
# telemetry. Sanitize unconditionally here instead of using the configurable
# display redactor: disabling display redaction must never expose credentials in
# a machine artifact.
_NAMED_CREDENTIAL_RE = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization|x-api-key|api[-_ ]?key|"
    r"access[-_ ]?token|refresh[-_ ]?token|auth[-_ ]?token|bearer|token|"
    r"secret|password|passwd|credential|key)\b[\"']?\s*(?::|=)\s*"
    r"[\"']?(?:bearer\s+)?)([^\s&,;\"']+)"
)
_ENV_CREDENTIAL_RE = re.compile(
    r"(?i)(\b[A-Z0-9_]*(?:API_?KEY|ACCESS_?TOKEN|REFRESH_?TOKEN|AUTH_?TOKEN|"
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Z0-9_]*\s*=\s*[\"']?)"
    r"([^\s&,;\"']+)"
)
_BEARER_CREDENTIAL_RE = re.compile(r"(?i)(\bbearer\s+)([^\s&,;\"']+)")
_PREFIXED_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_=.-]{8,}|"
    r"ghp_[A-Za-z0-9_=-]{8,}|"
    r"github_pat_[A-Za-z0-9_=-]{8,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"AIza[A-Za-z0-9_-]{20,}|"
    r"hf_[A-Za-z0-9_=-]{8,}|"
    r"pplx-[A-Za-z0-9_=-]{8,}|"
    r"AKIA[A-Z0-9]{16}"
    r")(?![A-Za-z0-9_-])"
)
_JWT_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?<![A-Za-z0-9_-]\.)"
    r"eyJ[A-Za-z0-9_-]{13,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r"(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_-])"
)


def _redact_exact_secrets(value: Any, exact_secrets: Sequence[str] = ()) -> str:
    """Redact exact caller-owned secrets without changing surrounding text."""
    text = str(value or "")
    secrets = sorted(
        {str(secret) for secret in exact_secrets if str(secret)},
        key=len,
        reverse=True,
    )
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return text


def sanitize_auxiliary_error(
    value: Any,
    *,
    limit: int = 1000,
    exact_secrets: Sequence[str] = (),
) -> str:
    """Return one bounded, unconditionally credential-safe telemetry line."""
    bounded_limit = max(0, int(limit))
    if bounded_limit <= 0:
        return ""
    # Retain lookahead so a credential beginning near the output boundary is
    # redacted as a whole before truncation. Error strings are already resident
    # in memory; this slice only bounds the additional normalization work.
    raw = _redact_exact_secrets(value, exact_secrets)[
        : max(bounded_limit * 4, bounded_limit + 4096)
    ]
    single_line = " ".join(raw.split())
    sanitized = _ENV_CREDENTIAL_RE.sub(r"\1[REDACTED]", single_line)
    sanitized = _NAMED_CREDENTIAL_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = _BEARER_CREDENTIAL_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = _PREFIXED_CREDENTIAL_RE.sub("[REDACTED]", sanitized)
    sanitized = _JWT_CREDENTIAL_RE.sub("[REDACTED]", sanitized)
    return sanitized[:bounded_limit]
