"""MCP config loading.

Reads the ``mcp_servers`` section from the EPFLemma config file.  Extracted
from ``tools/mcp_tool.py`` and re-exported there so callers/tests that resolve
``tools.mcp_tool._load_mcp_config`` (including the ~8 patch sites in
``tests/tools/test_mcp_tool.py``) keep working.

The import of ``epflemma_cli.config.load_config`` is kept LAZY (in-function)
exactly as in the original, and this module does NOT import mcp_tool, so it
introduces no import cycle.
"""

import logging
from typing import Dict

logger = logging.getLogger(__name__)


def _load_mcp_config() -> dict[str, dict]:
    """Read ``mcp_servers`` from the Gauss config file.

    Returns a dict of ``{server_name: server_config}`` or empty dict.
    Server config can contain either ``command``/``args``/``env`` for stdio
    transport or ``url``/``headers`` for HTTP transport, plus optional
    ``timeout`` and ``connect_timeout`` overrides.
    """
    try:
        from epflemma_cli.config import load_config
        config = load_config()
        servers = config.get("mcp_servers")
        if not servers or not isinstance(servers, dict):
            return {}
        return servers
    except Exception as exc:
        logger.debug("Failed to load MCP config: %s", exc)
        return {}
