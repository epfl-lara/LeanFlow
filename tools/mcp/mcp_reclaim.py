"""Select managed MCP calls that require bounded post-call process reclamation."""

from __future__ import annotations

import os
from collections.abc import Mapping

RESEARCH_MODE_ENV = "LEANFLOW_RESEARCH_MODE"
RESEARCH_MULTI_ATTEMPT_RECYCLE_ENV = "LEANFLOW_RESEARCH_RECYCLE_MULTI_ATTEMPT_MCP"
RESEARCH_STATEFUL_LEAN_LSP_RECYCLE_ENV = "LEANFLOW_RESEARCH_RECYCLE_STATEFUL_LEAN_LSP_MCP"

_STATEFUL_LEAN_LSP_TOOLS = frozenset(
    {
        "declaration_file",
        "diagnostics",
        "file_outline",
        "goals",
        "hammer_premise",
        "hover_info",
        "lean_declaration_file",
        "lean_diagnostic_messages",
        "lean_file_outline",
        "lean_goal",
        "lean_hammer_premise",
        "lean_hover_info",
        "lean_multi_attempt",
        "lean_profile_proof",
        "lean_state_search",
        "multi_attempt",
        "profile_proof",
        "state_search",
    }
)


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

    Stateful lean-lsp calls can leave a shared Lean worker elaborating a large
    file long after the requested result was returned. A normal short workflow
    benefits from retaining that warmed server, but a multi-day research
    campaign cannot let the old worker compete with LeanProbe after later source
    edits. Research therefore recycles stateful calls after preserving their
    result. The next native capability probe reconnects the server lazily.
    """
    env = os.environ if environ is None else environ
    if not _truthy(env.get(RESEARCH_MODE_ENV)):
        return False
    server = str(server_name or "").strip()
    tool = str(tool_name or "").strip()
    if server != "lean-lsp" or tool not in _STATEFUL_LEAN_LSP_TOOLS:
        return False
    setting = (
        RESEARCH_MULTI_ATTEMPT_RECYCLE_ENV
        if tool in {"lean_multi_attempt", "multi_attempt"}
        else RESEARCH_STATEFUL_LEAN_LSP_RECYCLE_ENV
    )
    return _enabled_with_default(env, setting, default=True)
