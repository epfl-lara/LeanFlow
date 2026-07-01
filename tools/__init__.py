#!/usr/bin/env python3
"""Lazy re-exports for the ``tools`` package.

Historically this package eagerly imported nearly every tool module at import
time. That meant a simple ``from tools.registry import registry`` also loaded
and registered optional legacy toolsets that LeanFlow no longer ships.

Keep the public ``from tools import ...`` API, but only import a tool module
when that specific attribute is requested.
"""

from __future__ import annotations

from importlib import import_module

_MODULE_EXPORTS = {
    "tools.implementations.web_tools": (
        "web_search_tool",
        "web_extract_tool",
        "check_firecrawl_api_key",
    ),
    "tools.implementations.terminal_tool": (
        "terminal_tool",
        "check_terminal_requirements",
        "cleanup_vm",
        "cleanup_all_environments",
        "get_active_environments_info",
        "register_task_env_overrides",
        "clear_task_env_overrides",
        "TERMINAL_TOOL_DESCRIPTION",
    ),
    "tools.implementations.skills_tool": (
        "skills_list",
        "skill_view",
        "check_skills_requirements",
        "SKILLS_TOOL_DESCRIPTION",
    ),
    "tools.implementations.lean_tool": (
        "lean_capabilities",
        "lean_inspect_tool",
        "lean_verify_tool",
        "lean_search_tool",
        "lean_sorries_tool",
        "lean_axioms_tool",
        "lean_proof_context_tool",
        "lean_multi_attempt_tool",
        "lean_auto_search_tool",
        "lean_worker_dispatch_tool",
        "check_lean_requirements",
    ),
    "tools.implementations.file_tools": (
        "read_file_tool",
        "write_file_tool",
        "patch_tool",
        "search_tool",
        "get_file_tools",
        "clear_file_ops_cache",
    ),
    "tools.implementations.todo_tool": (
        "todo_tool",
        "check_todo_requirements",
        "TODO_SCHEMA",
        "TodoStore",
    ),
    "tools.implementations.clarify_tool": (
        "clarify_tool",
        "check_clarify_requirements",
        "CLARIFY_SCHEMA",
    ),
    "tools.implementations.delegate_tool": (
        "delegate_task",
        "check_delegate_requirements",
        "DELEGATE_TASK_SCHEMA",
    ),
}

_EXPORTS = {
    name: (module_name, name) for module_name, names in _MODULE_EXPORTS.items() for name in names
}


def __getattr__(name: str):
    if name == "check_file_requirements":
        return check_file_requirements

    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module 'tools' has no attribute {name!r}") from exc

    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))


def check_file_requirements():
    """File tools only require the terminal backend to be available."""
    from .implementations.terminal_tool import check_terminal_requirements

    return check_terminal_requirements()


__all__ = sorted(list(_EXPORTS) + ["check_file_requirements"])
