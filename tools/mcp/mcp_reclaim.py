"""Select managed MCP calls that require bounded post-call process reclamation."""

from __future__ import annotations

import os
from collections.abc import Mapping

RESEARCH_MODE_ENV = "LEANFLOW_RESEARCH_MODE"
RESEARCH_MULTI_ATTEMPT_RECYCLE_ENV = "LEANFLOW_RESEARCH_RECYCLE_MULTI_ATTEMPT_MCP"


def _truthy(value: object) -> bool:
    """Return whether an environment-style value is explicitly enabled."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _enabled_with_default(
    env: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    """Return an environment switch while preserving an explicit false value."""
    if name not in env:
        return default
    return _truthy(env.get(name))


def should_recycle_after_tool(
    server_name: str,
    tool_name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return whether a completed MCP call should retire its backing server.

    ``lean_multi_attempt`` can transiently grow lean-lsp's shared Lean worker by
    several gigabytes. A normal short workflow benefits from retaining that
    warmed server, but a multi-day research campaign cannot leave reclamation to
    Lean's eventual internal cleanup. Research therefore recycles only this
    managed server/tool pair after preserving the tool result. The next native
    capability probe reconnects it lazily.
    """
    env = os.environ if environ is None else environ
    if not _truthy(env.get(RESEARCH_MODE_ENV)):
        return False
    if not _enabled_with_default(
        env,
        RESEARCH_MULTI_ATTEMPT_RECYCLE_ENV,
        default=True,
    ):
        return False
    return (
        str(server_name or "").strip() == "lean-lsp"
        and str(tool_name or "").strip() == "lean_multi_attempt"
    )
