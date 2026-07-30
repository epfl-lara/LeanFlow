"""Load MCP server configuration from LeanFlow settings.

The import of ``leanflow_cli.config.load_config`` is kept LAZY (in-function)
and this module does not import ``mcp_tool``, so it
introduces no import cycle.
"""

import logging
import os

from core.runtime_modes import (
    dispatch_worker_enabled,
    env_flag_enabled,
    low_memory_mode_enabled,
)

logger = logging.getLogger(__name__)

_DISPATCH_MCP_SERVERS_ENV = "LEANFLOW_DISPATCH_MCP_SERVERS"
_DEFAULT_DISPATCH_MCP_SERVERS: frozenset[str] = frozenset()


def _dispatch_mcp_server_allowlist() -> frozenset[str] | None:
    """Return the MCP servers allowed in a process-isolated research worker.

    Process-isolated research workers default to no MCP service tree. They keep
    built-in Lean tools, exact parent-deliverable checks, public search, and
    native text fallbacks under project resource admission without retaining a
    private multi-gigabyte lean-lsp child. ``*`` restores the full configured
    MCP portfolio for an explicitly provisioned worker; a comma-separated list
    can opt into specific services.
    """
    raw = str(os.getenv(_DISPATCH_MCP_SERVERS_ENV, "") or "").strip()
    if not raw:
        return _DEFAULT_DISPATCH_MCP_SERVERS
    if raw.casefold() in {"*", "all"}:
        return None
    if raw.casefold() in {"none", "off", "disabled"}:
        return frozenset()
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def _load_mcp_config() -> dict[str, dict]:
    """Read ``mcp_servers`` from the LeanFlow config file.

    Returns a dict of ``{server_name: server_config}`` or empty dict.
    Server config can contain either ``command``/``args``/``env`` for stdio
    transport or ``url``/``headers`` for HTTP transport, plus optional
    ``timeout`` and ``connect_timeout`` overrides.
    """
    if low_memory_mode_enabled() or env_flag_enabled("LEANFLOW_DISABLE_MCP"):
        return {}
    try:
        from leanflow_cli.config import load_config

        config = load_config()
        servers = config.get("mcp_servers")
        if not servers or not isinstance(servers, dict):
            return {}
        if dispatch_worker_enabled():
            allowlist = _dispatch_mcp_server_allowlist()
            if allowlist is not None:
                return {name: value for name, value in servers.items() if name in allowlist}
        return servers
    except Exception as exc:
        logger.debug("Failed to load MCP config: %s", exc)
        return {}
