"""EPFLemma shell over the stable epflemma CLI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from rich.console import Console

from epflemma_cli import __version__
from epflemma_cli.cli.banner import (
    build_welcome_banner,
    render_help,
    render_local_runtime_table,
    render_project_panel,
    render_provider_panel,
    render_skill_panel,
    render_skill_table,
    render_status_panel,
    render_swarm_agent_panel,
    render_swarm_table,
    render_swarm_transcript,
    render_swarm_transcript_entry,
    render_workflow_launch,
    render_workflow_status_panel,
)
from epflemma_cli.cli.cli_handlers import (
    _handle_config,
    _handle_models,
    _handle_sandbox,
    _parse_config_value,
    _print_json,
    _print_mcp_bootstrap,
    _print_mcp_status,
    _print_project_power_setup,
    _project_payload,
)
from epflemma_cli.cli.commands import SlashCommandCompleter, build_workflow_command_set
from epflemma_cli.cli.doctor import run_doctor
from epflemma_cli.cli.mcp_bootstrap import bootstrap_lean_mcp
from epflemma_cli.cli.shell_ui import (
    bottom_toolbar as _build_bottom_toolbar,
)
from epflemma_cli.cli.shell_ui import (
    prompt_focus_label as _build_prompt_focus_label,
)
from epflemma_cli.cli.shell_ui import (
    prompt_message as _build_prompt_message,
)
from epflemma_cli.cli.shell_ui import (
    toolbar_piece as _build_toolbar_piece,
)
from epflemma_cli.config import (
    ensure_epflemma_home,
    get_config_value,
    get_epflemma_home,
    load_config,
    set_config_value,
)
from epflemma_cli.local_models import (
    get_local_runtime_status,
    list_local_runtimes,
    read_local_runtime_logs,
    resolve_active_local_runtime,
    start_local_runtime,
    stop_local_runtime,
    use_local_runtime,
)
from epflemma_cli.runtime.env_loader import load_epflemma_dotenv
from epflemma_cli.runtime.runtime_provider import (
    format_runtime_provider_error,
    list_runtime_provider_targets,
    resolve_runtime_provider,
)
from epflemma_cli.runtime.sandbox_runtime import (
    SandboxRuntimeError,
    build_sandbox_image,
    format_sandbox_status,
    run_sandbox,
    sandbox_status,
)
from epflemma_cli.runtime.skill_core import discover_skill_commands, discover_skills, load_skill
from epflemma_cli.workflow import (
    FORGIVING_WORKFLOW_ALIAS_MAP,
    describe_launch_plan,
    resolve_workflow_request,
    run_workflow,
    spawn_workflow,
)
from epflemma_cli.workflows.project import (
    ProjectNotFoundError,
    clone_project_template,
    discover_epflemma_project,
    format_project_summary,
    initialize_epflemma_project,
    resolve_template_source,
    setup_project_power_modes,
)
from epflemma_cli.workflows.workflow_state import (
    enqueue_workflow_agent_message,
    load_workflow_checkpoints,
    load_workflow_live_status,
    read_workflow_activity,
    read_workflow_run_log,
    request_project_workflow_runner_exit,
    resolve_workflow_agent_id,
    save_workflow_live_status,
    summarize_workflow_agents,
    terminate_project_workflow_agents,
    terminate_workflow_agent,
    workflow_agent_detail,
    workflow_agent_transcript,
    workflow_agent_transcript_all,
)

# Derived from the single COMMAND_REGISTRY in epflemma_cli.cli.commands: the set of all
# frontend workflow slash commands (canonical commands plus their long-form aliases).
from tools.mcp.mcp_tool import get_mcp_status

WORKFLOW_COMMANDS = build_workflow_command_set()


def _seed_environment() -> None:
    home = get_epflemma_home()
    os.environ.setdefault("EPFLEMMA_HOME", str(home))


def _load_runtime_env(*, cwd: Path | None = None) -> None:
    project_env = (cwd or Path.cwd()).resolve() / ".env"
    load_epflemma_dotenv(epflemma_home=get_epflemma_home(), project_env=project_env)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all CLI subcommands and options. Constructs a hierarchical parser for version, status, config, doctor, mcp, project, workflow, sandbox, provider, and models commands, each with their own nested subparsers and flags."""
    parser = argparse.ArgumentParser(
        prog="epflemma",
        description="EPFLemma Lean AI for Math shell",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("version", help="Show version")

    status_parser = subparsers.add_parser("status", help="Show workflow and sandbox status")
    status_parser.add_argument("--json", action="store_true", dest="json_output")

    config_parser = subparsers.add_parser("config", help="Inspect or modify config")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_sub.add_parser("show", help="Print merged config")
    config_get = config_sub.add_parser("get", help="Read one config key")
    config_get.add_argument("key")
    config_set = config_sub.add_parser("set", help="Write one config key")
    config_set.add_argument("key")
    config_set.add_argument("value")

    doctor_parser = subparsers.add_parser("doctor", help="Check local setup")
    doctor_parser.add_argument("mode", nargs="?", default="all")
    doctor_parser.add_argument("--cwd", default=".")
    doctor_parser.add_argument("--json", action="store_true", dest="json_output")

    mcp_parser = subparsers.add_parser("mcp", help="Inspect configured MCP servers")
    mcp_sub = mcp_parser.add_subparsers(dest="mcp_command")
    mcp_status = mcp_sub.add_parser("status", help="Show MCP server status")
    mcp_status.add_argument("--json", action="store_true", dest="json_output")
    mcp_bootstrap = mcp_sub.add_parser("bootstrap", help="Install or repair managed MCP backends")
    mcp_bootstrap.add_argument("target", nargs="?", default="lean")
    mcp_bootstrap.add_argument("--json", action="store_true", dest="json_output")
    mcp_bootstrap.add_argument("--python", default=None)

    project_parser = subparsers.add_parser("project", help="Manage EPFLemma projects")
    project_sub = project_parser.add_subparsers(dest="project_command")
    project_init = project_sub.add_parser(
        "init", help="Initialize a Lean repo as an EPFLemma project"
    )
    project_init.add_argument("path", nargs="?", default=".")
    project_init.add_argument("--name", default="")
    project_create = project_sub.add_parser(
        "create", help="Clone a project template and register it"
    )
    project_create.add_argument("path")
    project_create.add_argument("--template-source", default="")
    project_create.add_argument("--name", default="")
    project_show = project_sub.add_parser("show", help="Show current project")
    project_show.add_argument("path", nargs="?", default=".")

    workflow_parser = subparsers.add_parser(
        "workflow", help="Run a Lean workflow in the native runtime"
    )
    workflow_parser.add_argument(
        "--provider",
        default=None,
        help="Override the configured provider for this workflow run",
    )
    workflow_parser.add_argument("workflow")
    workflow_parser.add_argument("args", nargs=argparse.REMAINDER)

    sandbox_parser = subparsers.add_parser(
        "sandbox", help="Run EPFLemma inside an isolated container worktree"
    )
    sandbox_sub = sandbox_parser.add_subparsers(dest="sandbox_command")
    sandbox_status_parser = sandbox_sub.add_parser(
        "status", help="Show sandbox engine, image, cache, and recent runs"
    )
    sandbox_status_parser.add_argument("--json", action="store_true", dest="json_output")
    sandbox_status_parser.add_argument(
        "--engine", default=None, choices=["auto", "docker", "podman"]
    )
    sandbox_status_parser.add_argument("--image", default=None)
    sandbox_status_parser.add_argument("--env-file", default=None)
    sandbox_doctor = sandbox_sub.add_parser(
        "doctor", help="Check whether the sandbox runtime is ready"
    )
    sandbox_doctor.add_argument("--json", action="store_true", dest="json_output")
    sandbox_doctor.add_argument("--engine", default=None, choices=["auto", "docker", "podman"])
    sandbox_doctor.add_argument("--image", default=None)
    sandbox_doctor.add_argument("--env-file", default=None)
    sandbox_build = sandbox_sub.add_parser(
        "build", help="Build or update the local EPFLemma sandbox image"
    )
    sandbox_build.add_argument("--engine", default=None, choices=["auto", "docker", "podman"])
    sandbox_build.add_argument("--image", default=None)
    sandbox_build.add_argument("--pull", action="store_true")
    sandbox_build.add_argument("--no-cache", action="store_true")
    sandbox_build.add_argument("--with-local-lean-explore", action="store_true")
    sandbox_run = sandbox_sub.add_parser(
        "run", help="Run an EPFLemma command in a copied project sandbox"
    )
    sandbox_run.add_argument("--engine", default=None, choices=["auto", "docker", "podman"])
    sandbox_run.add_argument("--image", default=None)
    sandbox_run.add_argument("--env-file", default=None)
    sandbox_run.add_argument("--no-network", action="store_true")
    sandbox_run.add_argument("args", nargs=argparse.REMAINDER)

    provider_parser = subparsers.add_parser("provider", help="Show the resolved runtime provider")
    provider_parser.add_argument("--requested", default=None)

    model_parser = subparsers.add_parser("models", help="Manage local model runtimes")
    model_sub = model_parser.add_subparsers(dest="models_command")
    local_parser = model_sub.add_parser("local", help="Manage local runtimes")
    local_sub = local_parser.add_subparsers(dest="local_command")
    local_sub.add_parser("list", help="List local runtimes")
    local_status = local_sub.add_parser("status", help="Show local runtime status")
    local_status.add_argument("runtime")
    local_start = local_sub.add_parser("start", help="Start a local runtime")
    local_start.add_argument("runtime")
    local_start.add_argument("model")
    local_start.add_argument("--host", default=None)
    local_start.add_argument("--port", type=int, default=None)
    local_stop = local_sub.add_parser("stop", help="Stop a local runtime")
    local_stop.add_argument("runtime")
    local_logs = local_sub.add_parser("logs", help="Show local runtime logs")
    local_logs.add_argument("runtime")
    local_logs.add_argument("--tail", type=int, default=80)
    local_use = local_sub.add_parser("use", help="Select the active local runtime/model")
    local_use.add_argument("runtime")
    local_use.add_argument("model")

    return parser


def _handle_project(args: argparse.Namespace) -> int:
    if args.project_command == "init":
        project = initialize_epflemma_project(args.path, name=args.name or None)
        setup_report = setup_project_power_modes(
            project.lean_root, progress=lambda message: print(message)
        )
        print(f"Initialized project: {project.label}")
        print(project.root)
        _print_project_power_setup(setup_report)
        return 0
    if args.project_command == "create":
        template_source = args.template_source or resolve_template_source(load_config(), os.environ)
        if not template_source:
            print(
                "No template source configured. Use --template-source or set epflemma.project.template_source.",
                file=sys.stderr,
            )
            return 1
        project = clone_project_template(
            args.path, template_source=template_source, name=args.name or None
        )
        print(f"Created project: {project.label}")
        print(project.root)
        return 0
    if args.project_command == "show":
        try:
            project = discover_epflemma_project(args.path)
        except ProjectNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        _print_json(_project_payload(project))
        return 0
    raise SystemExit("Unknown project command")


def _handle_status(args: argparse.Namespace) -> int:
    workflow = load_workflow_live_status()
    sandbox = sandbox_status()
    payload = {
        "workflow": workflow or {"phase": "idle", "workflow_kind": "[none]"},
        "sandbox": sandbox,
    }
    if getattr(args, "json_output", False):
        _print_json(payload)
        return 0
    render_workflow_status_panel(
        Console(),
        status=payload["workflow"],
        activities=read_workflow_activity(limit=8),
    )
    print(format_sandbox_status(sandbox))
    return 0


def _mcp_status_payload() -> dict[str, Any]:
    servers = list(get_mcp_status())
    return {
        "servers": servers,
        "count": len(servers),
    }


def _handle_mcp(args: argparse.Namespace) -> int:
    command = getattr(args, "mcp_command", None) or "status"
    if command == "status":
        payload = _mcp_status_payload()
        if getattr(args, "json_output", False):
            _print_json(payload)
        else:
            _print_mcp_status(payload)
        return 0
    if command == "bootstrap":
        target = str(getattr(args, "target", "lean") or "lean").strip().lower()
        if target != "lean":
            raise SystemExit("Unknown MCP bootstrap target")
        payload = bootstrap_lean_mcp(python_bin=getattr(args, "python", None))
        if getattr(args, "json_output", False):
            _print_json(payload)
        else:
            _print_mcp_bootstrap(payload)
        return 0
    raise SystemExit("Unknown MCP command")


from epflemma_cli.shell import InteractiveShell  # noqa: F401


def main(argv: list[str] | None = None) -> int:
    # Invariant: env (EPFLEMMA_HOME/etc) is seeded here BEFORE any handler runs. tools.mcp.mcp_tool
    # is imported at module load but only reads env at call time, so the order is safe — keep
    # it that way (do not add module-load-time env reads to the MCP import chain). (B2)
    """Entry point: seed environment (EPFLEMMA_HOME, .env), parse CLI args, and dispatch to command handlers. If no command is given, launch the interactive shell; _seed_environment() must run before MCP imports to ensure env is initialized at module load time."""
    _seed_environment()
    ensure_epflemma_home()
    _load_runtime_env()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        return InteractiveShell().run()
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "status":
        return _handle_status(args)
    if args.command == "config":
        return _handle_config(args)
    if args.command == "doctor":
        issues, output = run_doctor(args.cwd, mode=args.mode, json_output=args.json_output)
        if args.json_output:
            _print_json(output)
        else:
            print(output)
        return 0 if not issues else 1
    if args.command == "project":
        return _handle_project(args)
    if args.command == "sandbox":
        return _handle_sandbox(args)
    if args.command == "workflow":
        if args.workflow in {"status", "history", "activity", "log"}:
            payload = load_workflow_live_status()
            if args.workflow == "history":
                render_workflow_status_panel(
                    Console(),
                    status=payload or {"phase": "idle", "workflow_kind": "[none]"},
                    activities=[
                        {
                            "timestamp": str(entry.get("created_at", "") or ""),
                            "type": str(entry.get("success_state", "") or "in-progress"),
                            "message": str(entry.get("label", "") or "[none]"),
                        }
                        for entry in reversed(load_workflow_checkpoints())[:12]
                    ],
                )
                return 0
            if args.workflow == "log":
                tail = 120
                if args.args:
                    try:
                        tail = max(1, int(args.args[0]))
                    except ValueError:
                        print("Usage: epflemma workflow log [tail-lines]", file=sys.stderr)
                        return 1
                output = read_workflow_run_log(tail_lines=tail)
                print(output if output else "[no workflow run log recorded yet]")
                return 0
            render_workflow_status_panel(
                Console(),
                status=payload or {"phase": "idle", "workflow_kind": "[none]"},
                activities=(
                    read_workflow_activity(limit=20)
                    if args.workflow != "status"
                    else read_workflow_activity(limit=8)
                ),
            )
            return 0 if payload else 1
        text = f"/{args.workflow}" if not str(args.workflow).startswith("/") else str(args.workflow)
        if args.args:
            text = f"{text} {' '.join(args.args)}"
        try:
            plan = resolve_workflow_request(
                text, active_cwd=Path.cwd(), requested_provider=args.provider
            )
        except ProjectNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            print(
                "Use `epflemma project init` inside a Lean repo or `epflemma project create <path>` to clone one.",
                file=sys.stderr,
            )
            return 1
        except Exception as exc:
            print(format_runtime_provider_error(exc), file=sys.stderr)
            return 1
        render_workflow_launch(Console(), launch_summary=describe_launch_plan(plan))
        return run_workflow(text, active_cwd=Path.cwd(), requested_provider=args.provider)
    if args.command == "models":
        return _handle_models(args)
    if args.command == "provider":
        try:
            render_provider_panel(
                Console(),
                resolved=resolve_runtime_provider(requested=args.requested),
                requested=args.requested or "auto",
                targets=list_runtime_provider_targets(),
            )
            return 0
        except Exception as exc:
            print(format_runtime_provider_error(exc), file=sys.stderr)
            return 1
    if args.command == "mcp":
        return _handle_mcp(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
