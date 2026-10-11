"""Build native CLI arguments while preserving backend authentication and defaults."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from .models import BackendError, HarnessRequest
from .restricted import codex_restrictions

BACKENDS = ("codex", "claude", "lemex")


def child_environment(request: HarnessRequest) -> dict[str, str]:
    """Inherit native configuration and map an explicit OpenAI key in the child."""
    env = dict(os.environ)
    if request.env is not None:
        env.update(request.env)
    if request.backend == "codex" and env.get("OPENAI_API_KEY"):
        env["CODEX_API_KEY"] = env["OPENAI_API_KEY"]
    if request.backend == "claude" and request.restricted_tools:
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    return env


def _toml(value: Any) -> str:
    """Encode the small JSON-compatible MCP configuration subset as TOML."""
    if isinstance(value, Mapping):
        return (
            "{"
            + ", ".join(
                f"{json.dumps(str(key))} = {_toml(value[key])}" for key in sorted(value, key=str)
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value)
    raise BackendError(
        "MCP configuration contains a value unsupported by native TOML configuration."
    )


def _mcp_servers(request: HarnessRequest) -> dict[str, Any]:
    """Read only the explicit job bridge configuration, never native auth files."""
    if request.mcp_config is None:
        return {}
    try:
        data = json.loads(request.mcp_config.read_text())
        servers = data["mcpServers"]
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be an object")
        return servers
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BackendError(f"Invalid job MCP configuration: {exc}") from exc


def build_command(
    request: HarnessRequest,
    *,
    native_mcp_servers: Mapping[str, Any] | None = None,
    externally_isolated: bool = False,
) -> list[str]:
    """Build one non-interactive native invocation with explicit overrides only."""
    if request.backend not in BACKENDS:
        raise BackendError(
            f"Unsupported backend {request.backend!r}; choose codex, claude, or lemex."
        )
    if externally_isolated and request.isolation is None:
        raise BackendError("Disabling a nested native sandbox requires an outer isolation policy.")
    executable = request.executable or request.backend
    if request.backend == "claude":
        command = [
            executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--strict-mcp-config",
            "--settings",
            json.dumps(
                {
                    "disableAllHooks": True,
                    **(
                        {"autoMemoryEnabled": False, "claudeMdExcludes": ["**"]}
                        if request.restricted_tools
                        else {}
                    ),
                    **(
                        {"sandbox": {"enabled": False}}
                        if externally_isolated and request.tools
                        else {}
                    ),
                }
            ),
        ]
        if request.tools and not request.restricted_tools:
            # The outer OS sandbox constrains all tools, including native file tools.
            allowed = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
            allowed.extend(f"mcp__{name}__*" for name in sorted(_mcp_servers(request)))
            command.extend(["--allowedTools", ",".join(allowed)])
        else:
            command.extend(["--tools", ""])
            allowed = [f"mcp__{name}__*" for name in sorted(_mcp_servers(request))]
            if allowed:
                command.extend(["--allowedTools", ",".join(allowed)])
        if request.restricted_tools:
            command.extend(["--disable-slash-commands", "--no-session-persistence", "--no-chrome"])
        if request.model:
            command.extend(["--model", request.model])
        if request.effort:
            command.extend(["--effort", request.effort])
        if request.session_id and not request.restricted_tools:
            command.extend(["--resume", request.session_id])
        if request.mcp_config:
            command.extend(["--mcp-config", str(request.mcp_config)])
        return command
    command = [
        executable,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-c",
        'approval_policy="never"',
        "-c",
        f'sandbox_mode="{"danger-full-access" if externally_isolated and request.tools else "workspace-write" if request.tools else "read-only"}"',
    ]
    if request.model:
        command.extend(["--model", request.model])
    if request.effort:
        command.extend(["-c", f"model_reasoning_effort={json.dumps(request.effort)}"])
    servers = _mcp_servers(request)
    native_mcp_servers = native_mcp_servers or {}
    collisions = set(servers) & set(native_mcp_servers)
    if collisions:
        raise BackendError(
            "The job MCP bridge name conflicts with native configuration; choose a unique bridge name."
        )
    # Native configuration layers merge tables; an empty table does not remove servers.
    configured = dict(native_mcp_servers)
    configured.update(servers)
    command.extend(["-c", "mcp_servers=" + _toml(configured)])
    for feature in ("multi_agent", "multi_agent_v2", "hooks", "plugins"):
        command.extend(["-c", f"features.{feature}=false"])
    if not request.tools:
        for feature in ("shell_tool", "apply_patch_freeform", "js_repl"):
            command.extend(["-c", f"features.{feature}=false"])
        command.extend(["-c", 'web_search="disabled"'])
    if request.restricted_tools:
        temporary = request.isolation.temp_root if request.isolation else None
        command.extend(codex_restrictions(temporary or request.cwd / ".leanflow-runtime" / "tmp"))
    if request.session_id and not request.restricted_tools:
        command.extend(["resume", request.session_id])
    command.append("-")
    return command
