"""Shape Lean declarations restored during managed workflow recovery."""

from __future__ import annotations

import re

_TRACE_STATE_LINE_RE = re.compile(r"^[ \t]*trace_state(?:[ \t]*--.*)?$")


def strip_transient_diagnostics(declaration: str) -> str:
    """Remove standalone diagnostic commands from a recoverable declaration slice."""
    lines = str(declaration or "").splitlines(keepends=True)
    return "".join(
        line for line in lines if _TRACE_STATE_LINE_RE.fullmatch(line.rstrip("\r\n")) is None
    )
