"""Shared serialization helpers for tool handler return values.

Tool handlers return a JSON string to the agent. The dominant convention across ``tools/`` is
``json.dumps(payload, ensure_ascii=False)`` for success and
``json.dumps({"error": str(e)}, ensure_ascii=False)`` for failure (36+ call sites). These
helpers centralize that convention so the ``ensure_ascii`` setting and error shape live in one
place.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["dumps", "error"]


def dumps(payload: Any, *, indent: int | None = None) -> str:
    """Serialize a tool result the way tool handlers do: UTF-8 preserving (``ensure_ascii=False``).

    Equivalent to ``json.dumps(payload, ensure_ascii=False)`` (optionally pretty-printed).
    """
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def error(message: str, **extra: Any) -> str:
    """Serialize a tool error result: ``{"error": message, **extra}`` (``ensure_ascii=False``).

    Mirrors the ubiquitous ``json.dumps({"error": str(e)}, ensure_ascii=False)`` pattern; any
    extra keyword fields are merged into the object (``error`` always present).
    """
    payload: dict[str, Any] = {"error": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)
