"""Tests for tools/response.py serialization helpers (Phase 1)."""

import json

from tools import response


def test_dumps_matches_inline_convention():
    payload = {"output": "café", "n": 1}
    assert response.dumps(payload) == json.dumps(payload, ensure_ascii=False)
    # ensure_ascii=False: non-ASCII is preserved, not escaped.
    assert "café" in response.dumps(payload)


def test_dumps_indent_passthrough():
    assert response.dumps({"a": 1}, indent=2) == json.dumps({"a": 1}, ensure_ascii=False, indent=2)


def test_error_shape_matches_inline_convention():
    assert response.error("boom") == json.dumps({"error": "boom"}, ensure_ascii=False)
    out = json.loads(response.error("bad", code=3))
    assert out == {"error": "bad", "code": 3}
