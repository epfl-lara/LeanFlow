"""Constrain native runtimes to controller tools without changing model defaults."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from .isolation import IsolationPolicy

# Current runtimes retain apply_patch independently of its obsolete feature flag.
# The outer read-only workspace is therefore the final native-write boundary.
_RESTRICTED_FEATURES = (
    "shell_tool",
    "js_repl",
    "apply_patch_freeform",
    "view_image",
    "apps",
    "browser_use",
    "computer_use",
    "image_generation",
    "memories",
    "memory_tool",
    "external_agent_memory_import",
    "skill_search",
    "tool_suggest",
    "remote_control",
    "shell_snapshot",
    "recommended_plugins",
    "remote_plugin",
    "browser_use_external",
    "in_app_browser",
    "skill_mcp_dependency_install",
)


def codex_restrictions(temporary: Path) -> list[str]:
    """Disable autonomous retrieval and automatic context in Codex-compatible runtimes."""
    values = [
        'web_search="disabled"',
        "project_doc_max_bytes=0",
        'developer_instructions=""',
        "skills.include_instructions=false",
        "cloud.skills.enabled=false",
        "memories.use_memories=false",
        "memories.generate_memories=false",
        "notify=[]",
        "features.skip_host_skill_discovery=true",
        "orchestrator.mcp.enabled=false",
        "sqlite_home=" + json.dumps(str(temporary)),
        *(f"features.{feature}=false" for feature in _RESTRICTED_FEATURES),
    ]
    return ["--ephemeral", "--ignore-rules", *[part for value in values for part in ("-c", value)]]


def restricted_isolation(policy: IsolationPolicy, native_home: Path) -> IsolationPolicy:
    """Exclude previous native context while retaining native authentication and config.

    Ephemeral native invocations do not resume historical sessions. The controller
    retains assignment/proof state; native compaction continues within each call.
    """
    historical = tuple(
        native_home / name
        for name in (
            "sessions",
            "archived_sessions",
            "projects",
            "memories",
            "memory",
            "history.jsonl",
            "session_index.jsonl",
            "AGENTS.md",
            "AGENTS.override.md",
            "CLAUDE.md",
            "skills",
            "plugins",
            "rules",
            "prompts",
            "hooks",
            "worktrees",
            "logs",
            "log",
            "debug",
            "backups",
            "plans",
            "todos",
            "tasks",
            "file-history",
            "shell_snapshots",
            "shell-snapshots",
            "session-env",
        )
    )
    historical += tuple(native_home.glob("*.sqlite*"))
    return replace(
        policy,
        denied_roots=(*policy.denied_roots, *historical),
    )
