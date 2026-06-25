"""MCP schema conversion and utility-tool schema selection.

Pure, side-effect-free helpers extracted from ``tools/mcp_tool.py``:
convert an MCP tool listing into the LeanFlow registry schema format, build the
resources/prompts utility-tool schemas, parse include/exclude and boolish config
values, and select which utility schemas to register based on config and server
capabilities.

These are re-exported from ``tools/mcp_tool.py`` so callers/tests that resolve
``tools.mcp.mcp_tool.<name>`` keep working.  This module does NOT import mcp_tool,
so it introduces no import cycle.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _convert_mcp_schema(server_name: str, mcp_tool) -> dict:
    """Convert an MCP tool listing to the LeanFlow registry schema format.

    Args:
        server_name: The logical server name for prefixing.
        mcp_tool:    An MCP ``Tool`` object with ``.name``, ``.description``,
                     and ``.inputSchema``.

    Returns:
        A dict suitable for ``registry.register(schema=...)``.
    """
    # Sanitize: replace hyphens and dots with underscores for LLM API compatibility
    safe_tool_name = mcp_tool.name.replace("-", "_").replace(".", "_")
    safe_server_name = server_name.replace("-", "_").replace(".", "_")
    prefixed_name = f"mcp_{safe_server_name}_{safe_tool_name}"
    return {
        "name": prefixed_name,
        "description": mcp_tool.description or f"MCP tool {mcp_tool.name} from {server_name}",
        "parameters": (
            mcp_tool.inputSchema
            if mcp_tool.inputSchema
            else {
                "type": "object",
                "properties": {},
            }
        ),
    }


def _build_utility_schemas(server_name: str) -> list[dict]:
    """Build schemas for the MCP utility tools (resources & prompts).

    Returns a list of (schema, handler_factory_name) tuples encoded as dicts
    with keys: schema, handler_key.
    """
    safe_name = server_name.replace("-", "_").replace(".", "_")
    return [
        {
            "schema": {
                "name": f"mcp_{safe_name}_list_resources",
                "description": f"List available resources from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_resources",
        },
        {
            "schema": {
                "name": f"mcp_{safe_name}_read_resource",
                "description": f"Read a resource by URI from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "description": "URI of the resource to read",
                        },
                    },
                    "required": ["uri"],
                },
            },
            "handler_key": "read_resource",
        },
        {
            "schema": {
                "name": f"mcp_{safe_name}_list_prompts",
                "description": f"List available prompts from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            "handler_key": "list_prompts",
        },
        {
            "schema": {
                "name": f"mcp_{safe_name}_get_prompt",
                "description": f"Get a prompt by name from MCP server '{server_name}'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Name of the prompt to retrieve",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Optional arguments to pass to the prompt",
                        },
                    },
                    "required": ["name"],
                },
            },
            "handler_key": "get_prompt",
        },
    ]


def _normalize_name_filter(value: Any, label: str) -> set[str]:
    """Normalize include/exclude config to a set of tool names."""
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value}
    logger.warning("MCP config %s must be a string or list of strings; ignoring %r", label, value)
    return set()


def _parse_boolish(value: Any, default: bool = True) -> bool:
    """Parse a bool-like config value with safe fallback."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    logger.warning(
        "MCP config expected a boolean-ish value, got %r; using default=%s", value, default
    )
    return default


_UTILITY_CAPABILITY_METHODS = {
    "list_resources": "list_resources",
    "read_resource": "read_resource",
    "list_prompts": "list_prompts",
    "get_prompt": "get_prompt",
}


def _select_utility_schemas(
    server_name: str, server: "MCPServerTask", config: dict  # noqa: F821
) -> list[dict]:
    """Select utility schemas based on config and server capabilities."""
    tools_filter = config.get("tools") or {}
    resources_enabled = _parse_boolish(tools_filter.get("resources"), default=True)
    prompts_enabled = _parse_boolish(tools_filter.get("prompts"), default=True)

    selected: list[dict] = []
    for entry in _build_utility_schemas(server_name):
        handler_key = entry["handler_key"]
        if handler_key in {"list_resources", "read_resource"} and not resources_enabled:
            logger.debug(
                "MCP server '%s': skipping utility '%s' (resources disabled)",
                server_name,
                handler_key,
            )
            continue
        if handler_key in {"list_prompts", "get_prompt"} and not prompts_enabled:
            logger.debug(
                "MCP server '%s': skipping utility '%s' (prompts disabled)",
                server_name,
                handler_key,
            )
            continue

        required_method = _UTILITY_CAPABILITY_METHODS[handler_key]
        if not hasattr(server.session, required_method):
            logger.debug(
                "MCP server '%s': skipping utility '%s' (session lacks %s)",
                server_name,
                handler_key,
                required_method,
            )
            continue
        selected.append(entry)
    return selected
