"""Prevent logging stalls on long Lean feedback while retaining credential matching."""

import re
import time

import pytest

from agent.accounting.redact import _ENV_ASSIGN_RE, _SECRET_ENV_NAMES, redact_sensitive_text


@pytest.mark.parametrize("prefix", ["", "1", "theorem_", "a" * 128, "a-", "a.", "K"])
@pytest.mark.parametrize("name", ["API_KEY", "secret", "MY_TOKEN_VALUE", "password"])
def test_assignment_boundary_preserves_existing_matches(prefix: str, name: str) -> None:
    legacy = re.compile(
        rf"([A-Z_]*{_SECRET_ENV_NAMES}[A-Z_]*)\s*=\s*(['\"]?)(\S+)\2", re.IGNORECASE
    )
    source = prefix + name + "='synthetic_value_for_test'"
    assert _ENV_ASSIGN_RE.findall(source) == legacy.findall(source)
    assert "synthetic_value_for_test" not in redact_sensitive_text(source, force=True)


def test_large_noncredential_feedback_does_not_stall_logging() -> None:
    source = '"evidence": "' + "evidence" * 8000 + '"'
    start = time.monotonic()
    assert redact_sensitive_text(source, force=True) == source
    assert time.monotonic() - start < 2, "redaction retried every suffix of one long identifier"
