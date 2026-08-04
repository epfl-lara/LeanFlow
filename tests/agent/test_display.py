"""Test structured tool-result presentation and failure classification."""

from __future__ import annotations

import json

import pytest

from agent.display.display import _detect_tool_failure


@pytest.mark.parametrize(
    "payload",
    [
        {
            "success": True,
            "status": "empirical_compute_ok",
            "error": None,
        },
        {"success": True, "failed": []},
        {"ok": True, "error": ""},
    ],
)
def test_structured_success_with_empty_error_fields_is_not_failure(payload):
    """Treat stable nullable error fields as success when the contract says so."""
    assert _detect_tool_failure("empirical_compute", json.dumps(payload)) == (False, "")


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "error": None},
        {"success": True, "ok": False, "valid_without_sorry": False},
        {"success": True, "error": "isolated child failed"},
        {"ok": True, "failed": ["worker-one"]},
        {"status": "empirical_compute_timeout", "error": None},
    ],
)
def test_structured_failure_signals_remain_fail_closed(payload):
    """Reject explicit or contradictory structured failure signals."""
    assert _detect_tool_failure("empirical_compute", json.dumps(payload)) == (
        True,
        " [error]",
    )


def test_legacy_text_failure_fallback_remains_available():
    """Keep failure detection for tools that still return unstructured text."""
    assert _detect_tool_failure("legacy", "Error executing legacy tool") == (
        True,
        " [error]",
    )
