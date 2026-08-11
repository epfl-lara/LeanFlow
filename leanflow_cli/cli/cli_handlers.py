"""Handle state-free CLI subcommands and format their output.

Handlers that depend on patchable entrypoint state remain in ``leanflow_cli.main``.
The functions here are re-exported there for compatibility.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.config import (
    get_config_value,
    load_config,
    set_config_value,
)
from leanflow_cli.local_models import (
    get_local_runtime_status,
    list_local_runtimes,
    read_local_runtime_logs,
    start_local_runtime,
    stop_local_runtime,
    use_local_runtime,
)
from leanflow_cli.runtime.sandbox_runtime import (
    SandboxRuntimeError,
    build_sandbox_image,
    format_sandbox_status,
    run_sandbox,
    sandbox_status,
)

__all__ = [
    "_parse_config_value",
    "_print_json",
    "_print_project_power_setup",
    "_project_payload",
    "_handle_models",
    "_handle_config",
    "_handle_sandbox",
    "_print_mcp_status",
    "_print_mcp_bootstrap",
    "workflow_run_help_text",
]


def workflow_run_help_text(workflow: str) -> str:
    """Return safe leaf-command help without launching a native workflow."""
    name = str(workflow or "workflow").strip().lstrip("/") or "workflow"
    lines = [
        f"usage: leanflow workflow {name} FILE [options]",
        "",
        f"Run the {name} workflow in the native Lean runtime.",
        "",
        "options:",
        "  --provider PROVIDER       Override the configured runtime provider",
        "  --model MODEL             Override the model for this workflow",
        "  --no-parallel             Disable parallel prover/research execution",
    ]
    if name in {"prove", "autoprove"}:
        lines.extend(
            [
                "  --research                Enable the complete research profile",
                "  --research-workers N      Set background workers (implies --research)",
                "  --clean-room              Block benchmark solution research and sibling tasks",
                "  --clean-room-label VALUE  Add a protected task spelling (repeatable)",
                "  --human-review            Allow explicit human-review pauses",
            ]
        )
    lines.append("  -h, --help                Show this help and exit")
    return "\n".join(lines)


def _parse_config_value(raw: str) -> Any:
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _print_project_power_setup(report: Mapping[str, Any]) -> None:
    status = str(report.get("status", "") or "unknown")
    if status == "ready":
        print(f"REPL acceleration ready: {report.get('repl_path', '')}")
    elif status in {
        "lake-missing",
        "manual-setup-needed",
        "update-failed",
        "build-failed",
        "binary-missing",
        "toolchain-missing",
    }:
        print(f"REPL acceleration deferred: {status}")
    else:
        print(f"REPL acceleration status: {status}")
    manual_steps = list(report.get("manual_steps", []) or [])
    if manual_steps:
        print("Manual REPL setup:")
        for step in manual_steps:
            print(f"  - {step}")


def _project_payload(project: Any) -> dict[str, str]:
    return {
        "name": str(project.name),
        "root": str(project.root),
        "lean_root": str(project.lean_root),
        "manifest": str(project.manifest_path),
        "runtime_dir": str(project.runtime_dir),
        "cache_dir": str(project.cache_dir),
        "workflows_dir": str(project.workflows_dir),
        "template_source": str(project.template_source or "[none]"),
        "source_mode": str(project.source_mode),
        "blueprint_markers": (
            ", ".join(project.blueprint_markers) if project.blueprint_markers else "[none]"
        ),
    }


def _handle_models(args: argparse.Namespace) -> int:
    if args.local_command == "list":
        _print_json(list_local_runtimes())
        return 0
    if args.local_command == "status":
        _print_json(get_local_runtime_status(args.runtime))
        return 0
    if args.local_command == "start":
        _print_json(
            start_local_runtime(args.runtime, model=args.model, host=args.host, port=args.port)
        )
        return 0
    if args.local_command == "stop":
        _print_json(stop_local_runtime(args.runtime))
        return 0
    if args.local_command == "logs":
        print(read_local_runtime_logs(args.runtime, tail=args.tail))
        return 0
    if args.local_command == "use":
        _print_json(use_local_runtime(args.runtime, model=args.model))
        return 0
    raise SystemExit("Unknown local models command")


def _handle_config(args: argparse.Namespace) -> int:
    if args.config_command in {None, "show"}:
        _print_json(load_config())
        return 0
    if args.config_command == "get":
        print(get_config_value(args.key, ""))
        return 0
    if args.config_command == "set":
        set_config_value(args.key, _parse_config_value(args.value))
        return 0
    raise SystemExit("Unknown config command")


def _print_mcp_status(payload: Mapping[str, Any]) -> None:
    servers = list(payload.get("servers", []) or [])
    if not servers:
        print("No MCP servers configured.")
        return
    print("MCP Status")
    for entry in servers:
        name = str(entry.get("name", "") or "[unknown]")
        transport = str(entry.get("transport", "") or "stdio")
        connected = "connected" if entry.get("connected") else "down"
        tools = int(entry.get("tools", 0) or 0)
        line = f"- {name}: {connected} ({transport}, {tools} tools)"
        role = str(entry.get("role", "") or "").strip()
        if role:
            line += f", role={role}"
        if entry.get("managed"):
            line += ", managed"
        if entry.get("configured") is False:
            line += ", not configured"
        if entry.get("installed") is False:
            line += ", not installed"
        sampling = dict(entry.get("sampling", {}) or {})
        if sampling:
            line += (
                f", sampling requests={int(sampling.get('requests', 0) or 0)}"
                f", errors={int(sampling.get('errors', 0) or 0)}"
            )
        if entry.get("bootstrap_recommended"):
            line += ", bootstrap recommended"
        print(line)
        power = dict(entry.get("power_modes", {}) or {})
        if power:
            print(
                f"  power: local Loogle={power.get('loogle_local_status', 'unknown')}, REPL={power.get('repl_status', 'unknown')}"
            )
            print(f"  search: {power.get('remote_search_policy', 'public-fallbacks-enabled')}")


def _print_mcp_bootstrap(payload: Mapping[str, Any]) -> None:
    if not payload.get("success"):
        print("Managed MCP bootstrap failed.")
        return
    print("Managed Lean MCP bootstrap complete")
    print(f"- home: {payload.get('home', '')}")
    print(f"- config: {payload.get('config_path', '')}")
    print(f"- search policy: {payload.get('remote_search_policy', 'public-fallbacks-enabled')}")
    for entry in list(payload.get("servers", []) or []):
        print(
            f"- {entry.get('name', '[unknown]')}: "
            f"{entry.get('role', '') or '[no role]'} -> {entry.get('command', '')}"
        )
        power = dict(entry.get("power_modes", {}) or {})
        if power:
            print(
                f"  local Loogle: {power.get('loogle_local_status', 'unknown')} ({power.get('loogle_cache_dir', '')})"
            )
            print(f"  REPL: {power.get('repl_status', 'unknown')}")


def _handle_sandbox(args: argparse.Namespace) -> int:
    command = getattr(args, "sandbox_command", None) or "status"
    try:
        if command in {"status", "doctor"}:
            payload = sandbox_status(
                engine=getattr(args, "engine", None),
                image=getattr(args, "image", None),
                env_file=getattr(args, "env_file", None),
            )
            if getattr(args, "json_output", False):
                _print_json(payload)
            else:
                print(format_sandbox_status(payload))
            if command == "doctor":
                return 0 if payload.get("engine_ready") and payload.get("image_ready") else 1
            return 0
        if command == "build":
            return build_sandbox_image(
                engine=getattr(args, "engine", None),
                image=getattr(args, "image", None),
                pull=bool(getattr(args, "pull", False)),
                no_cache=bool(getattr(args, "no_cache", False)),
                local_lean_explore=bool(getattr(args, "with_local_lean_explore", False)),
            )
        if command == "run":
            return run_sandbox(
                command_args=getattr(args, "args", []) or [],
                active_cwd=Path.cwd(),
                engine=getattr(args, "engine", None),
                image=getattr(args, "image", None),
                env_file=getattr(args, "env_file", None),
                network=not bool(getattr(args, "no_network", False)),
            )
    except SandboxRuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    raise SystemExit("Unknown sandbox command")
