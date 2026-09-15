"""Describe bounded request failures and preserve controller interruption outcomes."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from agent.accounting.redact import redact_sensitive_text

_MESSAGE_LIMIT = 1000
_CHAIN_LIMIT = 4
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_AUTH_VALUE = re.compile(r"((?:proxy-)?authorization[\"']?\s*[:=]\s*)[^\r\n]+", re.IGNORECASE)


class SessionInterrupted(RuntimeError):
    """Stop a controller-cancelled session while retaining its admitted usage."""

    status = "interrupted"


def _safe_attribute(error: BaseException, name: str) -> Any:
    """Read only an allowed exception field without masking the original failure."""
    try:
        return getattr(error, name, None)
    except Exception:
        return None


def _safe_text(text: str, exact_secrets: Sequence[str]) -> str:
    """Remove credentials and request URLs before bounding durable diagnostic text."""
    for secret in exact_secrets:
        if isinstance(secret, str) and secret:
            text = text.replace(secret, "[REDACTED]")
    text = redact_sensitive_text(text, force=True)
    text = _URL.sub("[URL omitted]", text)
    return _AUTH_VALUE.sub(r"\1[REDACTED]", text)[:_MESSAGE_LIMIT]


def _exception_record(error: BaseException, exact_secrets: Sequence[str]) -> dict[str, Any]:
    """Describe one exception without reading its request, response, body, or headers."""
    try:
        message = str(error)
    except Exception:
        message = "Exception message unavailable"
    record: dict[str, Any] = {
        "exception_type": _safe_text(type(error).__name__, exact_secrets),
        "message": _safe_text(message, exact_secrets),
    }
    status_code = _safe_attribute(error, "status_code")
    if type(status_code) is int and 100 <= status_code <= 599:
        record["status_code"] = status_code
    for name in ("status", "code", "request_id"):
        value = _safe_attribute(error, name)
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            record[name] = _safe_text(value, exact_secrets)
    return record


def request_error_details(
    error: BaseException, *, exact_secrets: Sequence[str] = ()
) -> dict[str, Any]:
    """Return redacted failure evidence without changing status or retry decisions.

    SDK wrappers often say only ``Connection error`` or ``Request timed out``.
    Retain up to three underlying causes and safe SDK identifiers so a later
    audit can distinguish a read timeout from a connection or protocol failure.
    Never inspect client/request objects or copy provider response bodies.
    """
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(records) < _CHAIN_LIMIT:
        seen.add(id(current))
        records.append(_exception_record(current, exact_secrets))
        cause = current.__cause__
        current = (
            cause
            if cause is not None
            else (None if current.__suppress_context__ else current.__context__)
        )
    result = records[0]
    if len(records) > 1:
        result["causes"] = records[1:]
    if current is not None:
        result["cause_chain_truncated"] = True
    return result
