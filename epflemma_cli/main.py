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
from typing import Any, Mapping

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from rich.console import Console

from epflemma_cli import __version__
from epflemma_cli.banner import (
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
from epflemma_cli.commands import SlashCommandCompleter
from epflemma_cli.config import (
    ensure_epflemma_home,
    get_config_value,
    get_epflemma_home,
    load_config,
    set_config_value,
)
from epflemma_cli.doctor import run_doctor
from epflemma_cli.env_loader import load_epflemma_dotenv
from epflemma_cli.local_models import (
    get_local_runtime_status,
    list_local_runtimes,
    read_local_runtime_logs,
    resolve_active_local_runtime,
    start_local_runtime,
    stop_local_runtime,
    use_local_runtime,
)
from epflemma_cli.mcp_bootstrap import bootstrap_lean_mcp
from epflemma_cli.project import (
    ProjectNotFoundError,
    clone_project_template,
    discover_epflemma_project,
    format_project_summary,
    initialize_epflemma_project,
    resolve_template_source,
    setup_project_power_modes,
)
from epflemma_cli.runtime_provider import (
    format_runtime_provider_error,
    list_runtime_provider_targets,
    resolve_runtime_provider,
)
from epflemma_cli.sandbox_runtime import (
    SandboxRuntimeError,
    build_sandbox_image,
    format_sandbox_status,
    run_sandbox,
    sandbox_status,
)
from epflemma_cli.skill_core import discover_skill_commands, discover_skills, load_skill
from epflemma_cli.workflow import (
    FORGIVING_WORKFLOW_ALIAS_MAP,
    describe_launch_plan,
    resolve_workflow_request,
    run_workflow,
    spawn_workflow,
)
from epflemma_cli.workflow_state import (
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
from tools.mcp_tool import get_mcp_status

WORKFLOW_COMMANDS = {
    "/draft",
    "/review",
    "/checkpoint",
    "/refactor",
    "/golf",
    "/prove",
    "/formalize",
    "/autoprove",
    "/autoformalize",
}


def _seed_environment() -> None:
    home = get_epflemma_home()
    os.environ.setdefault("EPFLEMMA_HOME", str(home))
    os.environ.setdefault("OPENGAUSS_HOME", str(home))
    os.environ.setdefault("GAUSS_HOME", str(home))


def _load_runtime_env(*, cwd: Path | None = None) -> None:
    project_env = (cwd or Path.cwd()).resolve() / ".env"
    load_epflemma_dotenv(epflemma_home=get_epflemma_home(), project_env=project_env)


def _parse_config_value(raw: str) -> Any:
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _build_parser() -> argparse.ArgumentParser:
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
    project_init = project_sub.add_parser("init", help="Initialize a Lean repo as an EPFLemma project")
    project_init.add_argument("path", nargs="?", default=".")
    project_init.add_argument("--name", default="")
    project_create = project_sub.add_parser("create", help="Clone a project template and register it")
    project_create.add_argument("path")
    project_create.add_argument("--template-source", default="")
    project_create.add_argument("--name", default="")
    project_show = project_sub.add_parser("show", help="Show current project")
    project_show.add_argument("path", nargs="?", default=".")

    workflow_parser = subparsers.add_parser("workflow", help="Run a Lean workflow in the native runtime")
    workflow_parser.add_argument(
        "--provider",
        default=None,
        help="Override the configured provider for this workflow run",
    )
    workflow_parser.add_argument("workflow")
    workflow_parser.add_argument("args", nargs=argparse.REMAINDER)

    sandbox_parser = subparsers.add_parser("sandbox", help="Run EPFLemma inside an isolated container worktree")
    sandbox_sub = sandbox_parser.add_subparsers(dest="sandbox_command")
    sandbox_status_parser = sandbox_sub.add_parser("status", help="Show sandbox engine, image, cache, and recent runs")
    sandbox_status_parser.add_argument("--json", action="store_true", dest="json_output")
    sandbox_status_parser.add_argument("--engine", default=None, choices=["auto", "docker", "podman"])
    sandbox_status_parser.add_argument("--image", default=None)
    sandbox_status_parser.add_argument("--env-file", default=None)
    sandbox_doctor = sandbox_sub.add_parser("doctor", help="Check whether the sandbox runtime is ready")
    sandbox_doctor.add_argument("--json", action="store_true", dest="json_output")
    sandbox_doctor.add_argument("--engine", default=None, choices=["auto", "docker", "podman"])
    sandbox_doctor.add_argument("--image", default=None)
    sandbox_doctor.add_argument("--env-file", default=None)
    sandbox_build = sandbox_sub.add_parser("build", help="Build or update the local EPFLemma sandbox image")
    sandbox_build.add_argument("--engine", default=None, choices=["auto", "docker", "podman"])
    sandbox_build.add_argument("--image", default=None)
    sandbox_build.add_argument("--pull", action="store_true")
    sandbox_build.add_argument("--no-cache", action="store_true")
    sandbox_build.add_argument("--with-local-lean-explore", action="store_true")
    sandbox_run = sandbox_sub.add_parser("run", help="Run an EPFLemma command in a copied project sandbox")
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


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _print_project_power_setup(report: Mapping[str, Any]) -> None:
    status = str(report.get("status", "") or "unknown")
    if status == "ready":
        print(f"REPL acceleration ready: {report.get('repl_path', '')}")
    elif status in {"lake-missing", "manual-setup-needed", "update-failed", "build-failed", "binary-missing", "toolchain-missing"}:
        print(f"REPL acceleration deferred: {status}")
    else:
        print(f"REPL acceleration status: {status}")
    manual_steps = list(report.get("manual_steps", []) or [])
    if manual_steps:
        print("Manual REPL setup:")
        for step in manual_steps:
            print(f"  - {step}")


def _handle_project(args: argparse.Namespace) -> int:
    if args.project_command == "init":
        project = initialize_epflemma_project(args.path, name=args.name or None)
        setup_report = setup_project_power_modes(project.lean_root, progress=lambda message: print(message))
        print(f"Initialized project: {project.label}")
        print(project.root)
        _print_project_power_setup(setup_report)
        return 0
    if args.project_command == "create":
        template_source = args.template_source or resolve_template_source(load_config(), os.environ)
        if not template_source:
            print("No template source configured. Use --template-source or set epflemma.project.template_source.", file=sys.stderr)
            return 1
        project = clone_project_template(args.path, template_source=template_source, name=args.name or None)
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
        "blueprint_markers": ", ".join(project.blueprint_markers) if project.blueprint_markers else "[none]",
    }


def _handle_models(args: argparse.Namespace) -> int:
    if args.local_command == "list":
        _print_json(list_local_runtimes())
        return 0
    if args.local_command == "status":
        _print_json(get_local_runtime_status(args.runtime))
        return 0
    if args.local_command == "start":
        _print_json(start_local_runtime(args.runtime, model=args.model, host=args.host, port=args.port))
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
            print(f"  power: local Loogle={power.get('loogle_local_status', 'unknown')}, REPL={power.get('repl_status', 'unknown')}")
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
            print(f"  local Loogle: {power.get('loogle_local_status', 'unknown')} ({power.get('loogle_cache_dir', '')})")
            print(f"  REPL: {power.get('repl_status', 'unknown')}")


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


class InteractiveShell:
    def __init__(self) -> None:
        self.console = Console()
        self.cwd = Path.cwd().resolve()
        self.active_skill = ""
        history_path = ensure_epflemma_home() / "history"
        self.session = PromptSession(
            completer=SlashCommandCompleter(skill_commands_provider=self._skill_commands),
            history=FileHistory(str(history_path)),
        )

    def _skill_commands(self) -> dict[str, dict[str, str]]:
        return discover_skill_commands(self.cwd)

    def _workflow_status_payload(self) -> dict[str, Any]:
        return load_workflow_live_status()

    def _workflow_activity(self, limit: int = 8) -> list[dict[str, Any]]:
        return read_workflow_activity(limit=limit)

    def _workflow_agents(self, activity_limit: int = 5) -> list[dict[str, Any]]:
        return summarize_workflow_agents(activity_limit=activity_limit)

    def _model_label(self) -> str:
        config = load_config()
        model_cfg = config.get("model")
        if isinstance(model_cfg, dict):
            return str(model_cfg.get("default", "") or "")
        return str(model_cfg or "")

    def _project_label(self) -> str:
        try:
            return format_project_summary(discover_epflemma_project(self.cwd))
        except Exception:
            return "(none)"

    def _project_name(self) -> str:
        try:
            project = discover_epflemma_project(self.cwd)
            return project.label
        except Exception:
            return "No Project"

    def _provider_label(self, requested: str | None = None) -> str:
        try:
            resolved = resolve_runtime_provider(requested=requested)
            provider = str(resolved.get("provider", ""))
            model = str(resolved.get("model", ""))
            base_url = str(resolved.get("base_url", ""))
            parts = [provider]
            if model:
                parts.append(model)
            if base_url:
                parts.append(f"@ {base_url}")
            return " ".join(parts)
        except Exception as exc:
            return f"unavailable ({format_runtime_provider_error(exc)})"

    def _local_runtime_label(self) -> str:
        runtime = resolve_active_local_runtime()
        if not runtime:
            return "(none)"
        return f"{runtime.get('runtime', '')} {runtime.get('model', '')}".strip()

    def _active_skill_label(self) -> str:
        if self.active_skill:
            return self.active_skill
        workflow_status = self._workflow_status_payload()
        return str(workflow_status.get("active_skill", "") or "(none)")

    def _target_label(self) -> str:
        workflow_status = self._workflow_status_payload()
        value = str(workflow_status.get("target_symbol", "") or "").strip()
        if value in {"", "-", "[unknown]", "[launching]"}:
            return "-"
        return value

    def _phase_label(self) -> str:
        workflow_status = self._workflow_status_payload()
        return str(workflow_status.get("phase", "") or "idle")

    def _latest_activity_label(self) -> str:
        events = self._workflow_activity(limit=1)
        if not events:
            return "-"
        latest = events[-1]
        label = str(latest.get("type", "") or "")
        message = str(latest.get("message", "") or "")
        combined = f"{label}: {message}" if label else message
        return self._toolbar_piece(combined, 22)

    @staticmethod
    def _plain_notice(message: str) -> None:
        print(message)

    @staticmethod
    def _toolbar_piece(value: str, max_len: int = 22) -> str:
        text = str(value or "-")
        if len(text) <= max_len:
            return text
        return f"{text[:max_len - 3]}..."

    def _prompt_focus_label(self) -> str:
        theorem = self._target_label()
        if theorem != "-":
            return theorem
        workflow_status = self._workflow_status_payload()
        file_label = str(workflow_status.get("active_file_label", "") or "").strip()
        if file_label not in {"", "-", "[unknown]", "[launching]"}:
            return Path(file_label).name
        build = str(workflow_status.get("build_status", "") or "").strip()
        if build not in {"", "-", "unknown", "workflow launching"}:
            return build
        return "-"

    def _prompt_message(self) -> FormattedText:
        project = self._toolbar_piece(self._project_name(), 28)
        phase = self._toolbar_piece(self._phase_label(), 18)
        theorem = self._prompt_focus_label()
        status_suffix = phase
        if theorem and theorem != "-":
            status_suffix = f"{status_suffix} · {self._toolbar_piece(theorem, 28)}"
        return FormattedText(
            [
                ("fg:#ff5a5f bold", project),
                ("fg:#b0b0b0", "  "),
                ("fg:#d9d9d9", status_suffix),
                ("", "\n"),
                ("fg:#f5f5f5 bold", "› "),
            ]
        )

    def _bottom_toolbar(self) -> str:
        workflow_status = self._workflow_status_payload()
        phase = str(workflow_status.get("phase", "idle") or "idle")
        file_label = str(workflow_status.get("active_file_label", "") or "-")
        build = str(workflow_status.get("build_status", "") or "-")
        model = self._toolbar_piece(self._model_label(), 22)
        file_short = self._toolbar_piece(Path(file_label).name if file_label != "-" else "-", 18)
        theorem = self._toolbar_piece(self._target_label(), 16)
        skill = self._toolbar_piece(self._active_skill_label(), 18)
        latest = self._latest_activity_label()
        return (
            f" @ {model} | {phase} | {build} | {file_short} | {theorem} | {skill} | {latest} "
        )

    def show_banner(self) -> None:
        build_welcome_banner(
            self.console,
            cwd=str(self.cwd),
            model=self._model_label(),
            provider=self._provider_label(),
            project_label=self._project_label(),
            local_runtime=self._local_runtime_label(),
        )

    def show_help(self) -> None:
        render_help(self.console)

    def show_status(self, argv: list[str] | None = None) -> int:
        argv = argv or []
        render_status_panel(
            self.console,
            cwd=self.cwd,
            project_label=self._project_label(),
            provider=self._provider_label(),
            model=self._model_label(),
            local_runtime=self._local_runtime_label(),
            home=get_epflemma_home(),
        )
        workflow_status = self._workflow_status_payload()
        if argv:
            agent_id = argv[0]
            recent_limit = 5
            if len(argv) > 1:
                try:
                    recent_limit = max(1, int(argv[1]))
                except ValueError:
                    self.console.print("[dim]Usage: /status [agent-id] [recent-events][/]")
                    return 1
            agent = workflow_agent_detail(agent_id, activity_limit=recent_limit)
            if not agent:
                self.console.print()
                self.console.print(f"[bold red]Agent not found:[/] {agent_id}")
                return 1
            self.console.print()
            render_swarm_agent_panel(self.console, agent=agent, recent_limit=recent_limit)
            return 0
        if workflow_status:
            self.console.print()
            render_workflow_status_panel(self.console, status=workflow_status, activities=self._workflow_activity(limit=6))
        agents = self._workflow_agents(activity_limit=4)
        if agents:
            self.console.print()
            self.console.print("[bold #5DB8F5]Workflow Agents[/]")
            render_swarm_table(self.console, agents=agents)
        return 0

    def _run_swarm_command(self, argv: list[str]) -> int:
        agents = self._workflow_agents(activity_limit=8)
        if not argv:
            if not agents:
                self.console.print("[dim]No workflow agents have been recorded yet.[/]")
                return 1
            render_swarm_table(self.console, agents=agents)
            return 0

        if argv[0] == "kill":
            if len(argv) < 2:
                self.console.print("[dim]Usage: /swarm kill <agent-id>[/]")
                return 1
            result = terminate_workflow_agent(argv[1])
            if not result.get("success"):
                self.console.print(f"[bold red]{result.get('error', 'Failed to kill workflow agent.')}[/]")
                return 1
            self.console.print(
                f"[bold #5DB8F5]Sent interrupt to workflow agent[/] "
                f"{result.get('agent_id')} (pid {result.get('process_id')})."
            )
            return 0

        agent_id = resolve_workflow_agent_id(argv[0]) or argv[0]
        recent_limit = 5
        if len(argv) > 1:
            try:
                recent_limit = max(1, int(argv[1]))
            except ValueError:
                self.console.print("[dim]Usage: /swarm [agent-id] [recent-events][/]")
                return 1
        agent = workflow_agent_detail(agent_id, activity_limit=recent_limit)
        if not agent:
            self.console.print(f"[bold red]Agent not found:[/] {agent_id}")
            return 1
        transcript = workflow_agent_transcript(agent_id, limit=recent_limit)
        render_swarm_transcript(self.console, agent=agent, transcript=transcript)
        self.console.print("[dim]Following agent output. Press Ctrl+C to return to the swarm prompt.[/]")
        shown = len(workflow_agent_transcript_all(agent_id))
        while True:
            try:
                while True:
                    time.sleep(0.5)
                    agent = workflow_agent_detail(agent_id, activity_limit=recent_limit) or agent
                    transcript_all = workflow_agent_transcript_all(agent_id)
                    new_entries = transcript_all[shown:]
                    for entry in new_entries:
                        render_swarm_transcript_entry(self.console, entry=entry)
                    shown = len(transcript_all)
                    if str(agent.get("status", "") or "") != "active":
                        break
            except KeyboardInterrupt:
                self.console.print("\n[dim]Stopped following live output. Agent remains available in swarm mode.[/]")
            state = str(agent.get("status", "") or "[unknown]")
            if state in {"exited", "completed", "stopped", "interrupted"}:
                self.console.print(f"[dim]Agent {agent.get('agent_id')} is {state}. Returning to the main shell.[/]")
                return 0
            self.console.print(
                f"[dim]Agent {agent.get('agent_id')} is {state}. "
                "Enter a follow-up prompt, `/status`, `/kill`, or `/exit`.[/]"
            )
            try:
                command = self.session.prompt(
                    FormattedText(
                        [
                            ("#7AA2F7", f"swarm:{agent.get('agent_id', agent_id)}"),
                            ("#AAB6C3", " "),
                            ("#E6EDF3", "› "),
                        ]
                    )
                )
            except KeyboardInterrupt:
                self.console.print()
                return 0
            except EOFError:
                self.console.print()
                return 0
            text = command.strip()
            if not text or text in {"/exit", "/quit", "exit", "quit"}:
                return 0
            if text == "/status":
                agent = workflow_agent_detail(agent_id, activity_limit=recent_limit) or agent
                render_swarm_agent_panel(self.console, agent=agent, recent_limit=recent_limit)
                continue
            if text == "/kill":
                result = terminate_workflow_agent(agent_id)
                if not result.get("success"):
                    self.console.print(f"[bold red]{result.get('error', 'Failed to kill workflow agent.')}[/]")
                    return 1
                self.console.print(
                    f"[bold #5DB8F5]Sent interrupt to workflow agent[/] "
                    f"{result.get('agent_id')} (pid {result.get('process_id')})."
                )
                return 0
            result = enqueue_workflow_agent_message(agent_id, text)
            if not result.get("success"):
                self.console.print(f"[bold red]{result.get('error', 'Failed to queue prompt.')}[/]")
                return 1
            self.console.print(f"[dim]Queued prompt for agent {result.get('agent_id')}.[/]")
            agent = workflow_agent_detail(agent_id, activity_limit=recent_limit) or agent
            transcript_all = workflow_agent_transcript_all(agent_id)
            new_entries = transcript_all[shown:]
            for entry in new_entries:
                render_swarm_transcript_entry(self.console, entry=entry)
            shown = len(transcript_all)
            self.console.print("[dim]Following agent output. Press Ctrl+C to return to the swarm prompt.[/]")

    def _run_kill_command(self, argv: list[str]) -> int:
        if not argv:
            self.console.print("[dim]Usage: /kill <agent-id>[/]")
            return 1
        result = terminate_workflow_agent(argv[0])
        if not result.get("success"):
            self.console.print(f"[bold red]{result.get('error', 'Failed to kill workflow agent.')}[/]")
            return 1
        self.console.print(
            f"[bold #5DB8F5]Sent interrupt to workflow agent[/] "
            f"{result.get('agent_id')} (pid {result.get('process_id')})."
        )
        return 0

    def _render_workflow_history(self) -> None:
        checkpoints = list(reversed(load_workflow_checkpoints()))
        if not checkpoints:
            self.console.print("[dim]No persisted workflow checkpoints yet.[/]")
            return
        table = []
        for entry in checkpoints[:12]:
            table.append(
                {
                    "timestamp": str(entry.get("created_at", "") or ""),
                    "label": str(entry.get("label", "") or "[none]"),
                    "state": str(entry.get("success_state", "") or "in-progress"),
                    "files": ", ".join(entry.get("active_files") or []) or "[none]",
                }
            )
        activity = [{"type": row["state"], "timestamp": row["timestamp"], "message": f"{row['label']} — {row['files']}"} for row in table]
        render_workflow_status_panel(
            self.console,
            status=self._workflow_status_payload() or {"phase": "idle", "workflow_kind": "[none]"},
            activities=activity,
        )

    def _print_live_section(self, key: str) -> int:
        payload = self._workflow_status_payload()
        if not payload:
            self.console.print("[dim]No managed workflow state has been recorded yet.[/]")
            return 1
        value = str(payload.get(key, "") or "")
        print(value if value else "[none]")
        return 0

    def _run_workflow_status_command(self, argv: list[str]) -> int:
        subcmd = argv[0] if argv else "status"
        if subcmd == "status":
            payload = self._workflow_status_payload()
            if not payload:
                self.console.print("[dim]No managed workflow state has been recorded yet.[/]")
                return 1
            render_workflow_status_panel(self.console, status=payload, activities=self._workflow_activity(limit=10))
            return 0
        if subcmd == "history":
            self._render_workflow_history()
            return 0
        if subcmd == "activity":
            payload = self._workflow_status_payload()
            render_workflow_status_panel(self.console, status=payload or {"phase": "idle", "workflow_kind": "[none]"}, activities=self._workflow_activity(limit=20))
            return 0
        if subcmd == "log":
            tail = 120
            if len(argv) > 1:
                try:
                    tail = max(1, int(argv[1]))
                except ValueError:
                    self.console.print("[dim]Usage: /workflow log [tail-lines][/]")
                    return 1
            payload = read_workflow_run_log(tail_lines=tail)
            print(payload if payload else "[no workflow run log recorded yet]")
            return 0
        self.console.print("[dim]Usage: /workflow status|history|activity|log [tail-lines][/]")
        return 1

    def _run_skills_command(self, argv: list[str]) -> int:
        if not argv:
            skills = [
                {"name": skill.name, "source": skill.source, "description": skill.description}
                for skill in discover_skills(self.cwd)
            ]
            render_skill_table(self.console, skills=skills, active_skill=self.active_skill)
            return 0

        subcmd = argv[0]
        if subcmd == "reload":
            count = len(discover_skills(self.cwd))
            self.console.print(f"[bold #5DB8F5]Reloaded skill overlays[/] ({count} skill(s) available)")
            return 0

        if subcmd.startswith("/"):
            subcmd = subcmd[1:]

        payload = load_skill(subcmd, self.cwd)
        if not payload:
            self.console.print(f"[bold red]Skill not found:[/] {subcmd}")
            return 1
        self.active_skill = str(payload.get("name", "") or "")
        render_skill_panel(self.console, skill=payload, active=True)
        return 0

    def _run_project_command(self, argv: list[str]) -> int:
        if not argv:
            try:
                project = discover_epflemma_project(self.cwd)
            except ProjectNotFoundError:
                self.console.print("[dim]No active Lean workspace is open here. Use `/project init` in a Lean repo or `/project create <path>` to start one.[/]")
                return 1
            render_project_panel(self.console, project_summary=_project_payload(project))
            return 0
        subcmd = argv[0]
        if subcmd == "init":
            path = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else str(self.cwd)
            name = ""
            if "--name" in argv:
                idx = argv.index("--name")
                if idx + 1 < len(argv):
                    name = argv[idx + 1]
            target = Path(path).expanduser().resolve()
            already_initialized = (
                (target / ".epflemma" / "project.yaml").is_file()
                or (target / ".epflemma" / "project.yaml").is_file()
                or (target / ".gauss" / "project.yaml").is_file()
            )
            project = initialize_epflemma_project(path, name=name or None)
            setup_report = setup_project_power_modes(
                project.lean_root,
                progress=lambda message: self.console.print(message),
            )
            self.cwd = project.root
            if already_initialized:
                self._plain_notice(f"Project already initialized: {project.label}")
            else:
                self._plain_notice(f"Initialized project: {project.label}")
            _print_project_power_setup(setup_report)
            return 0
        if subcmd == "create":
            if len(argv) < 2:
                self.console.print("[dim]Usage: /project create <path> [--template-source <source>] [--name <name>] [/]")
                return 1
            path = argv[1]
            template_source = ""
            name = ""
            if "--template-source" in argv:
                idx = argv.index("--template-source")
                if idx + 1 < len(argv):
                    template_source = argv[idx + 1]
            if "--name" in argv:
                idx = argv.index("--name")
                if idx + 1 < len(argv):
                    name = argv[idx + 1]
            if not template_source:
                template_source = resolve_template_source(load_config(), os.environ)
            if not template_source:
                self.console.print("[bold red]No template source configured.[/] Set `epflemma.project.template_source` or pass `--template-source`.")
                return 1
            project = clone_project_template(path, template_source=template_source, name=name or None)
            self.cwd = project.root
            self._plain_notice(f"Created project: {project.label}")
            return 0
        if subcmd == "show":
            target = argv[1] if len(argv) > 1 else str(self.cwd)
            try:
                project = discover_epflemma_project(target)
            except ProjectNotFoundError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return 1
            render_project_panel(self.console, project_summary=_project_payload(project))
            return 0
        self.console.print(f"[bold red]Unknown /project subcommand:[/] {subcmd}")
        return 1

    def _run_models_command(self, argv: list[str]) -> int:
        if not argv or argv[0] != "local":
            self.console.print("[dim]Usage: /models local list|status|start|stop|logs|use ...[/]")
            return 1
        args = argv[1:]
        if not args:
            self.console.print("[dim]Usage: /models local list|status|start|stop|logs|use ...[/]")
            return 1
        subcmd = args[0]
        if subcmd == "list":
            render_local_runtime_table(self.console, runtimes=list_local_runtimes())
            return 0
        if subcmd == "status" and len(args) >= 2:
            _print_json(get_local_runtime_status(args[1]))
            return 0
        if subcmd == "start" and len(args) >= 3:
            _print_json(start_local_runtime(args[1], model=args[2]))
            return 0
        if subcmd == "stop" and len(args) >= 2:
            _print_json(stop_local_runtime(args[1]))
            return 0
        if subcmd == "logs" and len(args) >= 2:
            print(read_local_runtime_logs(args[1]))
            return 0
        if subcmd == "use" and len(args) >= 3:
            _print_json(use_local_runtime(args[1], model=args[2]))
            return 0
        self.console.print("[dim]Usage: /models local list|status <runtime>|start <runtime> <model>|stop <runtime>|logs <runtime>|use <runtime> <model>[/]")
        return 1

    def _run_config_command(self, argv: list[str]) -> int:
        if not argv or argv[0] == "show":
            _print_json(load_config())
            return 0
        if argv[0] == "get" and len(argv) >= 2:
            print(get_config_value(argv[1], ""))
            return 0
        if argv[0] == "set" and len(argv) >= 3:
            set_config_value(argv[1], _parse_config_value(argv[2]))
            self.console.print(f"[bold #5DB8F5]Updated[/] {argv[1]}")
            return 0
        self.console.print("[dim]Usage: /config show|get <key>|set <key> <value>[/]")
        return 1

    def _run_provider_command(self, raw: str) -> int:
        requested = ""
        parts = shlex.split(raw)
        if len(parts) > 1:
            requested = parts[1]
        try:
            resolved = resolve_runtime_provider(requested=requested or None)
        except Exception as exc:
            self.console.print(f"[bold red]{format_runtime_provider_error(exc)}[/]")
            return 1
        render_provider_panel(
            self.console,
            resolved=resolved,
            requested=requested or "auto",
            targets=list_runtime_provider_targets(),
        )
        return 0

    def _run_mcp_command(self, raw: str) -> int:
        parts = shlex.split(raw)
        args = parts[1:]
        json_output = "--json" in args
        subcmd = next((arg for arg in args if not arg.startswith("-")), "status")
        if subcmd == "status":
            payload = _mcp_status_payload()
            if json_output:
                _print_json(payload)
            else:
                _print_mcp_status(payload)
            return 0
        if subcmd == "bootstrap":
            target = next((arg for arg in args if arg not in {"bootstrap", "--json"} and not arg.startswith("--")), "lean")
            if target != "lean":
                self.console.print("[dim]Usage: /mcp bootstrap lean [--json][/]")
                return 1
            payload = bootstrap_lean_mcp()
            if json_output:
                _print_json(payload)
            else:
                _print_mcp_bootstrap(payload)
            return 0
        self.console.print("[dim]Usage: /mcp status [--json] | /mcp bootstrap lean [--json][/]")
        return 1

    def _run_workflow_command(self, raw: str) -> int:
        try:
            plan = resolve_workflow_request(raw, active_cwd=self.cwd, active_skill=self.active_skill or None)
        except ProjectNotFoundError as exc:
            self.console.print(f"[bold red]{exc}[/]")
            self.console.print("[dim]Use `/project init` inside a Lean repo, or `/project create <path>` to clone a template.[/]")
            return 1
        except Exception as exc:
            self.console.print(f"[bold red]{format_runtime_provider_error(exc)}[/]")
            return 1

        existing = next(
            (
                agent for agent in self._workflow_agents(activity_limit=1)
                if not str(agent.get("parent_agent_id", "") or "")
                and str(agent.get("project_root", "") or "") == str(plan.project.root)
                and str(agent.get("workflow_kind", "") or "") == str(plan.workflow.workflow_kind)
                and str(agent.get("workflow_command", "") or "") == str(plan.workflow.backend_command)
                and str(agent.get("status", "") or "") in {"active", "blocked", "paused", "queued"}
            ),
            None,
        )
        if existing:
            self.console.print(
                f"[dim]Workflow already running for {plan.project.root}: "
                f"{existing.get('agent_id')} [{existing.get('status')}]. "
                "Use `/status`, `/swarm`, or `/kill <agent-id>` instead of launching a duplicate.[/]"
            )
            return 0

        render_workflow_launch(self.console, launch_summary=describe_launch_plan(plan))
        current_status = self._workflow_status_payload()
        status_payload = {
            "version": int(current_status.get("version", 1) or 1),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "phase": "busy",
            "workflow_kind": str(plan.workflow.workflow_kind or "[none]"),
            "workflow_command": str(plan.workflow.frontend_command or raw),
            "effective_prompt": str(plan.workflow.explicit_goal or ""),
            "project_root": str(plan.project.root),
            "provider": str(plan.runtime.get("provider", "") or "[unknown]"),
            "model": str(plan.runtime.get("model", "") or "[unknown]"),
            "base_url": str(plan.runtime.get("base_url", "") or "[unknown]"),
            "process_id": int(current_status.get("process_id", 0) or 0),
            "active_skill": str(plan.active_skill or current_status.get("active_skill", "") or "[none]"),
            "parallel_agents": 1,
            "active_file": str(current_status.get("active_file", "") or ""),
            "active_file_label": str(current_status.get("active_file_label", "") or "[launching]"),
            "target_symbol": str(current_status.get("target_symbol", "") or "[launching]"),
            "diagnostics": str(current_status.get("diagnostics", "") or "Workflow launching..."),
            "goals": str(current_status.get("goals", "") or "Workflow launching..."),
            "build_status": "workflow launching",
            "proof_state_message": "Workflow launching in background.",
            "sorry_count": current_status.get("sorry_count"),
            "project_sorry_count": current_status.get("project_sorry_count"),
            "checkpoint_count": int(current_status.get("checkpoint_count", 0) or 0),
            "latest_checkpoint_label": str(current_status.get("latest_checkpoint_label", "") or "[none]"),
            "latest_filesystem_checkpoint": str(current_status.get("latest_filesystem_checkpoint", "") or "[none]"),
            "last_compaction_reason": str(current_status.get("last_compaction_reason", "") or "[none]"),
            "snapshot_present": bool(current_status.get("snapshot_present", False)),
            "held_locks": int(current_status.get("held_locks", 0) or 0),
        }
        if plan.formalization_document is not None:
            status_payload.update(
                {
                    "formalization_document": plan.formalization_document.source_relative,
                    "formalization_document_kind": plan.formalization_document.source_kind,
                    "formalization_request_kind": str(
                        plan.formalization_document.metadata.get("document_request_kind", "file") or "file"
                    ),
                    "formalization_request": str(
                        plan.formalization_document.metadata.get(
                            "document_request_relative",
                            plan.formalization_document.source_relative,
                        )
                        or plan.formalization_document.source_relative
                    ),
                    "formalization_selected_source_document": plan.formalization_document.source_relative,
                    "formalization_context": str(plan.formalization_document.context_path),
                    "formalization_blueprint": str(plan.formalization_document.blueprint_path),
                    "formalization_extracted_blueprint_path": str(plan.formalization_document.blueprint_path),
                    "formalization_target_file": plan.formalization_document.target_lean_relative,
                }
            )
        save_workflow_live_status(status_payload)
        _, process = spawn_workflow(
            raw,
            active_cwd=self.cwd,
            active_skill=self.active_skill or None,
            interactive=False,
        )
        status_payload["process_id"] = process.pid
        status_payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_workflow_live_status(status_payload)
        self.console.print()
        self.console.print(
            f"[bold #5DB8F5]Workflow running in background[/] (pid {process.pid}). "
            "Use `/status`, `/status <agent-id>`, `/swarm`, `/workflow activity`, or `/workflow log 120`."
        )
        workflow_status = self._workflow_status_payload()
        if workflow_status:
            self.console.print()
            render_workflow_status_panel(self.console, status=workflow_status, activities=self._workflow_activity(limit=6))
        return 0

    def _shutdown_project_workflows(self) -> None:
        project_root = ""
        try:
            project = discover_epflemma_project(self.cwd)
            project_root = str(project.root)
        except Exception:
            status = self._workflow_status_payload()
            project_root = str(status.get("project_root", "") or "")
        if not project_root:
            return
        graceful = request_project_workflow_runner_exit(project_root)
        queued = [str(agent_id or "") for agent_id in graceful.get("queued", []) if str(agent_id or "")]
        if queued:
            deadline = time.monotonic() + 1.5
            remaining = queued
            while remaining and time.monotonic() < deadline:
                unresolved: list[str] = []
                for agent_id in remaining:
                    detail = workflow_agent_detail(agent_id, activity_limit=1)
                    status = str(detail.get("status", "") or "")
                    try:
                        process_id = int(detail.get("process_id", 0) or 0)
                    except Exception:
                        process_id = 0
                    if status in {"exited", "stopped", "interrupted", "completed"}:
                        continue
                    if process_id <= 0:
                        continue
                    try:
                        os.kill(process_id, 0)
                    except ProcessLookupError:
                        continue
                    except Exception:
                        pass
                    unresolved.append(agent_id)
                remaining = unresolved
                if remaining:
                    time.sleep(0.1)
            if not remaining:
                self.console.print(
                    f"[dim]Requested clean exit for {len(queued)} workflow runner(s) in {project_root} before exiting the shell.[/]"
                )
                return

        result = terminate_project_workflow_agents(project_root)
        count = int(result.get("count", 0) or 0)
        if count:
            self.console.print(
                f"[dim]Interrupted {count} workflow agent(s) for {project_root} before exiting the shell.[/]"
            )
            return

        status = self._workflow_status_payload()
        status_root = str(status.get("project_root", "") or "")
        if project_root and status_root and status_root != project_root:
            return
        if str(status.get("phase", "") or "") == "exited":
            return
        try:
            process_id = int(status.get("process_id", 0) or 0)
        except Exception:
            process_id = 0
        if process_id <= 0 or process_id == os.getpid():
            return
        try:
            os.killpg(process_id, signal.SIGINT)
        except Exception:
            try:
                os.kill(process_id, signal.SIGINT)
            except ProcessLookupError:
                return
            except Exception:
                return
        self.console.print(
            f"[dim]Interrupted background workflow runner (pid {process_id}) for {project_root} before exiting the shell.[/]"
        )

    def _handle_command(self, raw: str) -> bool:
        stripped = raw.strip()
        if not stripped:
            return True

        first_token = stripped.split(" ", 1)[0]
        if first_token in WORKFLOW_COMMANDS or first_token in FORGIVING_WORKFLOW_ALIAS_MAP:
            self._run_workflow_command(stripped)
            return True

        if stripped in {"/exit", "/quit", "exit", "quit"}:
            self._shutdown_project_workflows()
            return False
        if stripped in {"/help", "help"}:
            self.show_help()
            return True
        if stripped == "/status" or stripped.startswith("/status "):
            try:
                argv = shlex.split(stripped)[1:]
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            self.show_status(argv)
            return True
        if stripped == "/swarm" or stripped.startswith("/swarm "):
            try:
                argv = shlex.split(stripped)[1:]
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            self._run_swarm_command(argv)
            return True
        if stripped == "/kill" or stripped.startswith("/kill "):
            try:
                argv = shlex.split(stripped)[1:]
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            self._run_kill_command(argv)
            return True
        if stripped == "/goals":
            self._print_live_section("goals")
            return True
        if stripped == "/diagnostics":
            self._print_live_section("diagnostics")
            return True
        if stripped == "/proof-state":
            self._print_live_section("proof_state_message")
            return True
        if stripped == "/banner":
            self.show_banner()
            return True
        if stripped == "/clear":
            self.console.clear()
            self.show_banner()
            return True
        if stripped == "/pwd":
            print(self.cwd)
            return True
        if stripped.startswith("/cd "):
            target = Path(stripped[len("/cd ") :].strip()).expanduser()
            resolved = (self.cwd / target).resolve() if not target.is_absolute() else target.resolve()
            if not resolved.exists() or not resolved.is_dir():
                self.console.print(f"[bold red]Directory not found:[/] {resolved}")
                return True
            self.cwd = resolved
            self.show_status()
            return True
        if stripped.startswith("/doctor"):
            try:
                tokens = shlex.split(stripped)
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            args = tokens[1:]
            json_output = "--json" in args
            mode = next((token for token in args if not token.startswith("-")), "all")
            issues, output = run_doctor(self.cwd, mode=mode, json_output=json_output)
            if json_output:
                _print_json(output)
            else:
                print(output)
            if issues:
                self.console.print(f"[dim]{len(issues)} issue(s) found.[/]")
            return True
        if stripped.startswith("/provider"):
            self._run_provider_command(stripped)
            return True
        if stripped.startswith("/mcp"):
            self._run_mcp_command(stripped)
            return True
        if stripped.startswith("/workflow"):
            try:
                argv = shlex.split(stripped)[1:]
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            self._run_workflow_status_command(argv)
            return True
        if stripped.startswith("/project"):
            try:
                argv = shlex.split(stripped)[1:]
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            self._run_project_command(argv)
            return True
        if stripped.startswith("/models"):
            try:
                argv = shlex.split(stripped)[1:]
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            self._run_models_command(argv)
            return True
        if stripped.startswith("/config"):
            try:
                argv = shlex.split(stripped)[1:]
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            self._run_config_command(argv)
            return True
        if stripped.startswith("/skills"):
            try:
                argv = shlex.split(stripped)[1:]
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            self._run_skills_command(argv)
            return True
        if stripped.startswith("/skill"):
            try:
                argv = shlex.split(stripped)[1:]
            except ValueError as exc:
                self.console.print(f"[bold red]{exc}[/]")
                return True
            self._run_skills_command(argv)
            return True
        if stripped in self._skill_commands():
            self._run_skills_command([stripped[1:]])
            return True
        if stripped.startswith("/"):
            self.console.print(f"[bold red]Unknown command:[/] {first_token}")
            self.console.print("[dim]Type /help for available commands.[/]")
            return True

        if stripped.split(" ", 1)[0] in FORGIVING_WORKFLOW_ALIAS_MAP:
            self._run_workflow_command(stripped)
            return True

        self.console.print("[dim]Plain chat is not part of the kernel build. Use /help, /project, or a Lean workflow command.[/]")
        return True

    def run(self) -> int:
        self.show_banner()
        while True:
            try:
                raw = self.session.prompt(
                    self._prompt_message(),
                    bottom_toolbar=self._bottom_toolbar,
                    refresh_interval=0.5,
                )
            except EOFError:
                self._shutdown_project_workflows()
                print()
                return 0
            except KeyboardInterrupt:
                print()
                continue
            if not self._handle_command(raw):
                return 0


def main(argv: list[str] | None = None) -> int:
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
                render_workflow_status_panel(Console(), status=payload or {"phase": "idle", "workflow_kind": "[none]"}, activities=[
                    {
                        "timestamp": str(entry.get("created_at", "") or ""),
                        "type": str(entry.get("success_state", "") or "in-progress"),
                        "message": str(entry.get("label", "") or "[none]"),
                    }
                    for entry in reversed(load_workflow_checkpoints())[:12]
                ])
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
                activities=read_workflow_activity(limit=20) if args.workflow != "status" else read_workflow_activity(limit=8),
            )
            return 0 if payload else 1
        text = f"/{args.workflow}" if not str(args.workflow).startswith("/") else str(args.workflow)
        if args.args:
            text = f"{text} {' '.join(args.args)}"
        try:
            plan = resolve_workflow_request(text, active_cwd=Path.cwd(), requested_provider=args.provider)
        except ProjectNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            print("Use `epflemma project init` inside a Lean repo or `epflemma project create <path>` to clone one.", file=sys.stderr)
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
