#!/usr/bin/env python3
"""Toolset definitions for the EPFLemma kernel."""

from typing import List, Dict, Any, Set, Optional


_FILE_TOOLS = ["read_file", "write_file", "patch", "search_files"]
_WEB_TOOLS = ["web_search"]
_TERMINAL_TOOLS = ["terminal"]
_SKILL_TOOLS = ["skills_list", "skill_view"]
_SESSION_TOOLS = ["session_search"]
_COORDINATION_TOOLS = ["acquire_file_lock", "release_file_lock", "list_file_locks"]
_DELEGATION_TOOLS = ["delegate_task"]
_LEAN_TOOLS = [
    "lean_capabilities",
    "lean_inspect",
    "lean_verify",
    "lean_search",
    "lean_sorries",
    "lean_axioms",
    "lean_worker_dispatch",
]

_EPFLEMMA_CORE_TOOLS = [
    *_FILE_TOOLS,
    *_WEB_TOOLS,
    *_TERMINAL_TOOLS,
    *_SKILL_TOOLS,
    *_SESSION_TOOLS,
    *_COORDINATION_TOOLS,
    *_LEAN_TOOLS,
]


TOOLSETS: Dict[str, Dict[str, Any]] = {
    "web": {
        "description": "Web research tools",
        "tools": _WEB_TOOLS,
        "includes": [],
    },
    "search": {
        "description": "Web search only",
        "tools": _WEB_TOOLS,
        "includes": [],
    },
    "file": {
        "description": "File manipulation tools",
        "tools": _FILE_TOOLS,
        "includes": [],
    },
    "terminal": {
        "description": "Local shell execution",
        "tools": _TERMINAL_TOOLS,
        "includes": [],
    },
    "skills": {
        "description": "Installed skill discovery tools",
        "tools": _SKILL_TOOLS,
        "includes": [],
    },
    "session_search": {
        "description": "Past-session search tools",
        "tools": _SESSION_TOOLS,
        "includes": [],
    },
    "coordination": {
        "description": "File reservation and coordination tools for Lean workflows",
        "tools": _COORDINATION_TOOLS,
        "includes": [],
    },
    "lean": {
        "description": "Native Lean workflow tools",
        "tools": _LEAN_TOOLS,
        "includes": [],
    },
    "delegation": {
        "description": "Explicit user-approved subagent delegation",
        "tools": _DELEGATION_TOOLS,
        "includes": [],
    },
    "autoformalize": {
        "description": "Lean workflow tool surface",
        "tools": [],
        "includes": ["file", "web", "terminal", "skills", "session_search", "coordination"],
    },
    "epflemma-cli": {
        "description": "EPFLemma kernel CLI toolset",
        "tools": _EPFLEMMA_CORE_TOOLS,
        "includes": [],
    },
    "epflemma-native": {
        "description": "EPFLemma native Lean workflow toolset",
        "tools": _EPFLEMMA_CORE_TOOLS,
        "includes": [],
    },
    "epflemma-native-swarm": {
        "description": "User-approved EPFLemma Lean swarm workflow toolset",
        "tools": [*_EPFLEMMA_CORE_TOOLS, *_DELEGATION_TOOLS],
        "includes": [],
    },
}


def get_toolset(name: str) -> Optional[Dict[str, Any]]:
    return TOOLSETS.get(name)


def resolve_toolset(name: str, visited: Set[str] = None) -> List[str]:
    if visited is None:
        visited = set()

    if name in {"all", "*"}:
        all_tools: Set[str] = set()
        for toolset_name in get_toolset_names():
            all_tools.update(resolve_toolset(toolset_name, visited.copy()))
        return list(all_tools)

    if name in visited:
        return []
    visited.add(name)

    toolset = TOOLSETS.get(name)
    if not toolset:
        return []

    tools = set(toolset.get("tools", []))
    for included_name in toolset.get("includes", []):
        tools.update(resolve_toolset(included_name, visited.copy()))
    return list(tools)


def resolve_multiple_toolsets(toolset_names: List[str]) -> List[str]:
    all_tools = set()
    for name in toolset_names:
        all_tools.update(resolve_toolset(name))
    return list(all_tools)


def get_all_toolsets() -> Dict[str, Dict[str, Any]]:
    return TOOLSETS.copy()


def get_toolset_names() -> List[str]:
    return list(TOOLSETS.keys())


def validate_toolset(name: str) -> bool:
    return name in TOOLSETS or name in {"all", "*"}


def create_custom_toolset(name: str, description: str, tools: List[str], includes: List[str] = None):
    TOOLSETS[name] = {
        "description": description,
        "tools": list(tools),
        "includes": list(includes or []),
    }


def get_toolset_info(name: str) -> Optional[Dict[str, Any]]:
    toolset = get_toolset(name)
    if not toolset:
        return None
    direct_tools = list(toolset.get("tools", []))
    resolved_tools = resolve_toolset(name)
    return {
        "name": name,
        "description": toolset.get("description", ""),
        "direct_tools": direct_tools,
        "resolved_tools": resolved_tools,
        "tool_count": len(resolved_tools),
        "is_composite": bool(toolset.get("includes")),
        "includes": list(toolset.get("includes", [])),
    }
