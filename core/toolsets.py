#!/usr/bin/env python3
"""Toolset definitions for the LeanFlow kernel."""

from typing import Any

_FILE_TOOLS = ["read_file", "write_file", "patch", "search_files"]
_WEB_TOOLS = ["web_search", "web_fetch", "web_download", "repo_clone"]
_WEB_RESEARCH_TOOLS = ["web_search", "web_fetch"]
_TERMINAL_TOOLS = ["terminal"]
_EMPIRICAL_COMPUTE_TOOLS = ["empirical_compute"]
_SKILL_TOOLS = ["skills_list", "skill_view"]
_SESSION_TOOLS = ["session_search"]
_COORDINATION_TOOLS = ["acquire_file_lock", "release_file_lock", "list_file_locks"]
_DELEGATION_TOOLS = ["delegate_task"]
_DOCUMENT_TOOLS = ["formalization_document_inspect", "read_pdf"]
_LEAN_TOOLS = [
    "lean_capabilities",
    "lean_inspect",
    "lean_verify",
    "lean_incremental_check",
    "lean_search",
    "lean_sorries",
    "lean_axioms",
    "lean_proof_context",
    "lean_multi_attempt",
    "lean_auto_search",
    "lean_lemma_suggest",
    "lean_outline",
    "apply_verified_patch",
    "lean_extract_have",
    "lean_reasoning_help",
    "lean_decompose_helpers",
]
_LEAN_RESEARCH_TOOLS = [
    tool
    for tool in _LEAN_TOOLS
    if tool
    not in {
        "apply_verified_patch",
        "lean_extract_have",
        "lean_reasoning_help",
        "lean_decompose_helpers",
    }
]

_LEANFLOW_CORE_TOOLS = [
    *_FILE_TOOLS,
    *_WEB_TOOLS,
    *_TERMINAL_TOOLS,
    *_SKILL_TOOLS,
    *_SESSION_TOOLS,
    *_COORDINATION_TOOLS,
    *_DOCUMENT_TOOLS,
    *_LEAN_TOOLS,
]


TOOLSETS: dict[str, dict[str, Any]] = {
    "web": {
        "description": "Web research tools",
        "tools": _WEB_TOOLS,
        "includes": [],
    },
    "web-research": {
        "description": "Read-only web research without project-state download or clone tools",
        "tools": _WEB_RESEARCH_TOOLS,
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
    "empirical-compute": {
        "description": "Process-isolated bounded exact arithmetic for empirical dispatch workers",
        "tools": _EMPIRICAL_COMPUTE_TOOLS,
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
    "document": {
        "description": "Project-local source document inspection for formalization workflows",
        "tools": _DOCUMENT_TOOLS,
        "includes": [],
    },
    "lean": {
        "description": "Native Lean workflow tools",
        "tools": _LEAN_TOOLS,
        "includes": [],
    },
    "lean-research": {
        "description": (
            "Read/check-only Lean research tools without shared-file patch authority or "
            "nested LLM advisors"
        ),
        "tools": _LEAN_RESEARCH_TOOLS,
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
        "includes": [
            "file",
            "web",
            "terminal",
            "skills",
            "session_search",
            "coordination",
            "document",
            "lean",
        ],
    },
    "leanflow-cli": {
        "description": "LeanFlow kernel CLI toolset",
        "tools": _LEANFLOW_CORE_TOOLS,
        "includes": [],
    },
    "leanflow-native": {
        "description": "LeanFlow native Lean workflow toolset",
        "tools": _LEANFLOW_CORE_TOOLS,
        "includes": [],
    },
    "leanflow-prove-worker": {
        # Focused single-declaration proving surface for the inner /prove worker. Drops
        # session_search (cross-session recall is irrelevant to autonomous theorem repair, and
        # dropping it also removes its system-prompt guidance) and the document tools
        # (formalization_document_inspect / read_pdf are formalize-only). Web is kept available
        # for hard problems. Lean + file + skills + coordination + terminal remain.
        "description": "Focused single-declaration Lean proving toolset for the /prove worker",
        "tools": [],
        "includes": ["file", "web", "terminal", "skills", "coordination", "lean"],
    },
    "leanflow-native-swarm": {
        "description": "User-approved LeanFlow Lean swarm workflow toolset",
        "tools": [*_LEANFLOW_CORE_TOOLS, *_DELEGATION_TOOLS],
        "includes": [],
    },
}


def get_toolset(name: str) -> dict[str, Any] | None:
    return TOOLSETS.get(name)


def resolve_toolset(name: str, visited: set[str] = None) -> list[str]:
    if visited is None:
        visited = set()

    if name in {"all", "*"}:
        all_tools: set[str] = set()
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


def resolve_multiple_toolsets(toolset_names: list[str]) -> list[str]:
    all_tools = set()
    for name in toolset_names:
        all_tools.update(resolve_toolset(name))
    return list(all_tools)


def get_all_toolsets() -> dict[str, dict[str, Any]]:
    return TOOLSETS.copy()


def get_toolset_names() -> list[str]:
    return list(TOOLSETS.keys())


def validate_toolset(name: str) -> bool:
    return name in TOOLSETS or name in {"all", "*"}


def create_custom_toolset(
    name: str, description: str, tools: list[str], includes: list[str] = None
):
    TOOLSETS[name] = {
        "description": description,
        "tools": list(tools),
        "includes": list(includes or []),
    }


def get_toolset_info(name: str) -> dict[str, Any] | None:
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
