#!/usr/bin/env python3
"""
Model Tools Module

Thin orchestration layer over the tool registry. Each tool file in tools/
self-registers its schema, handler, and metadata via tools.registry.register().
This module triggers discovery (by importing the supported Lean-kernel tool
modules), then provides the public API that run_agent.py and the leanflow
shell consume.

Public API retained for the Lean-first runtime:
    get_tool_definitions(enabled_toolsets, disabled_toolsets, quiet_mode) -> list
    handle_function_call(function_name, function_args, task_id, user_task) -> str
    TOOL_TO_TOOLSET_MAP: dict
    TOOLSET_REQUIREMENTS: dict
    get_all_tool_names() -> list
    get_toolset_for_tool(name) -> str
    get_available_toolsets() -> dict
    check_toolset_requirements() -> dict
    check_tool_availability(quiet) -> tuple
"""

import asyncio
import json
import logging
from typing import Any

from tools.registry import registry
from toolsets import resolve_toolset, validate_toolset

logger = logging.getLogger(__name__)

# =============================================================================
# Async Bridging  (single source of truth -- used by registry.dispatch too)
# =============================================================================


def _run_async(coro):
    """Run an async coroutine from a sync context.

    If the current thread already has a running event loop, we spin up a
    disposable thread so asyncio.run() can create its own loop without
    conflicting.

    This is the single source of truth for sync->async bridging in tool
    handlers. The RL paths (agent_loop.py, tool_context.py) also provide
    outer thread-pool wrapping as defense-in-depth, but each handler is
    self-protecting via this function.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=300)
    return asyncio.run(coro)


# =============================================================================
# Tool Discovery  (importing each module triggers its registry.register calls)
# =============================================================================


def _discover_tools():
    """Import all tool modules to trigger their registry.register() calls.

    Wrapped in a function so import errors in optional tools (e.g., fal_client
    not installed) don't prevent the rest from loading.
    """
    _modules = [
        "tools.implementations.web_tools",
        "tools.implementations.web_fetch",
        "tools.implementations.repo_clone",
        "tools.implementations.file_tools",
        "tools.implementations.file_lock_tool",
        "tools.implementations.terminal_tool",
        "tools.implementations.empirical_compute",
        "tools.implementations.session_search_tool",
        "tools.implementations.skills_tool",
        "tools.implementations.document_tool",
        "tools.implementations.lean_tool",
        "tools.implementations.delegate_tool",
    ]
    import importlib

    for mod_name in _modules:
        try:
            importlib.import_module(mod_name)
        except Exception as e:
            logger.debug("Could not import %s: %s", mod_name, e)


_discover_tools()

# MCP tool discovery (external MCP servers from config)
try:
    from tools.mcp.mcp_tool import discover_mcp_tools

    discover_mcp_tools()
except Exception as e:
    logger.debug("MCP tool discovery failed: %s", e)

# =============================================================================
# Backward-compat constants  (built once after discovery)
# =============================================================================

TOOL_TO_TOOLSET_MAP: dict[str, str] = registry.get_tool_to_toolset_map()

TOOLSET_REQUIREMENTS: dict[str, dict] = registry.get_toolset_requirements()

# Resolved tool names from the last get_tool_definitions() call.
# Used by the active runtime to track which tools are available in this session.
_last_resolved_tool_names: list[str] = []

# =============================================================================
# Legacy toolset name mapping  (old _tools-suffixed names -> tool name lists)
# =============================================================================

_LEGACY_TOOLSET_MAP = {
    "web_tools": ["web_search", "web_fetch", "web_download"],
    "file_tools": ["read_file", "write_file", "patch", "search_files"],
}

# =============================================================================
# get_tool_definitions  (the main schema provider)
# =============================================================================


def get_tool_definitions(
    enabled_toolsets: list[str] = None,
    disabled_toolsets: list[str] = None,
    quiet_mode: bool = False,
) -> list[dict[str, Any]]:
    """
    Get tool definitions for model API calls with toolset-based filtering.

    All tools must be part of a toolset to be accessible.

    Args:
        enabled_toolsets: Only include tools from these toolsets.
        disabled_toolsets: Exclude tools from these toolsets (if enabled_toolsets is None).
        quiet_mode: Suppress status prints.

    Returns:
        Filtered list of OpenAI-format tool definitions.
    """
    # Determine which tool names the caller wants
    tools_to_include: set = set()

    if enabled_toolsets:
        for toolset_name in enabled_toolsets:
            if validate_toolset(toolset_name):
                resolved = resolve_toolset(toolset_name)
                tools_to_include.update(resolved)
                if not quiet_mode:
                    print(
                        f"✅ Requested toolset '{toolset_name}' "
                        f"({len(resolved)} configured tools; availability filtered below)"
                    )
            elif toolset_name in _LEGACY_TOOLSET_MAP:
                legacy_tools = _LEGACY_TOOLSET_MAP[toolset_name]
                tools_to_include.update(legacy_tools)
                if not quiet_mode:
                    print(
                        f"✅ Requested legacy toolset '{toolset_name}' "
                        f"({len(legacy_tools)} configured tools; availability filtered below)"
                    )
            else:
                if not quiet_mode:
                    print(f"⚠️  Unknown toolset: {toolset_name}")

    elif disabled_toolsets:
        from toolsets import get_all_toolsets

        for ts_name in get_all_toolsets():
            tools_to_include.update(resolve_toolset(ts_name))

        for toolset_name in disabled_toolsets:
            if validate_toolset(toolset_name):
                resolved = resolve_toolset(toolset_name)
                tools_to_include.difference_update(resolved)
                if not quiet_mode:
                    print(
                        f"🚫 Disabled toolset '{toolset_name}': {', '.join(resolved) if resolved else 'no tools'}"
                    )
            elif toolset_name in _LEGACY_TOOLSET_MAP:
                legacy_tools = _LEGACY_TOOLSET_MAP[toolset_name]
                tools_to_include.difference_update(legacy_tools)
                if not quiet_mode:
                    print(f"🚫 Disabled legacy toolset '{toolset_name}': {', '.join(legacy_tools)}")
            else:
                if not quiet_mode:
                    print(f"⚠️  Unknown toolset: {toolset_name}")
    else:
        from toolsets import get_all_toolsets

        for ts_name in get_all_toolsets():
            tools_to_include.update(resolve_toolset(ts_name))

    # Ask the registry for schemas (only returns tools whose check_fn passes)
    filtered_tools = registry.get_definitions(tools_to_include, quiet=quiet_mode)

    if not quiet_mode:
        if filtered_tools:
            tool_names = [t["function"]["name"] for t in filtered_tools]
            print(
                f"🛠️  Final tool selection ({len(filtered_tools)} tools): {', '.join(tool_names)}"
            )
        else:
            print("🛠️  No tools selected (all filtered out or unavailable)")

    global _last_resolved_tool_names
    _last_resolved_tool_names = [t["function"]["name"] for t in filtered_tools]

    return filtered_tools


# =============================================================================
# handle_function_call  (the main dispatcher)
# =============================================================================

# Tools whose execution is intercepted by the agent loop (run_agent.py)
# because they need agent-level state (TodoStore, MemoryStore, etc.).
# The registry still holds their schemas; dispatch just returns a stub error
# so if something slips through, the LLM sees a sensible message.
_AGENT_LOOP_TOOLS = {"todo", "memory", "session_search", "delegate_task"}


def handle_function_call(
    function_name: str,
    function_args: dict[str, Any],
    task_id: str | None = None,
    user_task: str | None = None,
    enabled_tools: list[str] | None = None,
    owner_id: str | None = None,
    parent_agent: Any | None = None,
) -> str:
    """
    Main function call dispatcher that routes calls to the tool registry.

    Args:
        function_name: Name of the function to call.
        function_args: Arguments for the function.
        task_id: Unique identifier for terminal/browser session isolation.
        user_task: The user's original task.
        enabled_tools: Tool names enabled for this session.

    Returns:
        Function result as a JSON string.
    """
    # Notify the read-loop tracker when a non-read/search tool runs,
    # so the *consecutive* counter resets (reads after other work are fine).
    _READ_SEARCH_TOOLS = {"read_file", "search_files"}
    if function_name not in _READ_SEARCH_TOOLS:
        try:
            from tools.implementations.file_tools import notify_other_tool_call

            notify_other_tool_call(task_id or "default")
        except Exception:
            pass  # file_tools may not be loaded yet

    try:
        if function_name in _AGENT_LOOP_TOOLS:
            return json.dumps({"error": f"{function_name} must be handled by the agent loop"})

        result = registry.dispatch(
            function_name,
            function_args,
            task_id=task_id,
            user_task=user_task,
            enabled_tools=enabled_tools or _last_resolved_tool_names,
            owner_id=owner_id,
            parent_agent=parent_agent,
        )

        return result

    except Exception as e:
        error_msg = f"Error executing {function_name}: {str(e)}"
        logger.error(error_msg)
        return json.dumps({"error": error_msg}, ensure_ascii=False)


# =============================================================================
# Backward-compat wrapper functions
# =============================================================================


def get_all_tool_names() -> list[str]:
    """Return all registered tool names."""
    return registry.get_all_tool_names()


def get_toolset_for_tool(tool_name: str) -> str | None:
    """Return the toolset a tool belongs to."""
    return registry.get_toolset_for_tool(tool_name)


def get_available_toolsets() -> dict[str, dict]:
    """Return toolset availability info for UI display."""
    return registry.get_available_toolsets()


def check_toolset_requirements(
    enabled_toolsets: list[str] | None = None,
) -> dict[str, bool]:
    """Return availability only for direct toolsets selected by this session.

    Meta-toolsets expand into tools owned by several registry toolsets. Check
    those concrete owners so a foreground prover does not warn about an
    empirical-only capability it never requested.
    """
    requirements = registry.check_toolset_requirements()
    if not enabled_toolsets:
        return requirements
    selected_tools: set[str] = set()
    for toolset_name in enabled_toolsets:
        if validate_toolset(toolset_name):
            selected_tools.update(resolve_toolset(toolset_name))
        else:
            selected_tools.update(_LEGACY_TOOLSET_MAP.get(toolset_name, ()))
    selected_owners = {
        owner for name in selected_tools if (owner := registry.get_toolset_for_tool(name))
    }
    return {name: available for name, available in requirements.items() if name in selected_owners}


def check_tool_availability(quiet: bool = False) -> tuple[list[str], list[dict]]:
    """Return (available_toolsets, unavailable_info)."""
    return registry.check_tool_availability(quiet=quiet)
