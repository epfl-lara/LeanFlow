"""EPFLemma shell over the stable opengauss CLI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from opengauss_cli import __version__
from opengauss_cli.banner import (
    build_welcome_banner,
    render_help,
    render_local_runtime_table,
    render_project_panel,
    render_provider_panel,
    render_skill_panel,
    render_skill_table,
    render_status_panel,
    render_workflow_status_panel,
    render_workflow_launch,
)
from opengauss_cli.commands import SlashCommandCompleter
from opengauss_cli.config import (
    ensure_opengauss_home,
    get_config_value,
    get_opengauss_home,
    load_config,
    set_config_value,
)
from opengauss_cli.doctor import run_doctor
from opengauss_cli.local_models import (
    get_local_runtime_status,
    list_local_runtimes,
    read_local_runtime_logs,
    resolve_active_local_runtime,
    start_local_runtime,
    stop_local_runtime,
    use_local_runtime,
)
from opengauss_cli.project import (
    ProjectNotFoundError,
    clone_project_template,
    discover_opengauss_project,
    format_project_summary,
    initialize_opengauss_project,
    resolve_template_source,
)
from opengauss_cli.runtime_provider import (
    format_runtime_provider_error,
    list_runtime_provider_targets,
    resolve_runtime_provider,
)
from opengauss_cli.skill_core import discover_skill_commands, discover_skills, load_skill
from opengauss_cli.workflow import FORGIVING_WORKFLOW_ALIAS_MAP, describe_launch_plan, resolve_workflow_request, run_workflow
from opengauss_cli.workflow_state import load_workflow_checkpoints, load_workflow_live_status, read_workflow_activity


WORKFLOW_COMMANDS = {
    "/prove",
    "/draft",
    "/review",
    "/checkpoint",
    "/refactor",
    "/golf",
    "/autoprove",
    "/formalize",
    "/autoformalize",
}


def _seed_environment() -> None:
    home = get_opengauss_home()
    os.environ.setdefault("OPENGAUSS_HOME", str(home))
    os.environ.setdefault("GAUSS_HOME", str(home))


def _parse_config_value(raw: str) -> Any:
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opengauss",
        description="EPFLemma Lean AI for Math shell (stable `opengauss` CLI)",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("version", help="Show version")

    config_parser = subparsers.add_parser("config", help="Inspect or modify config")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_sub.add_parser("show", help="Print merged config")
    config_get = config_sub.add_parser("get", help="Read one config key")
    config_get.add_argument("key")
    config_set = config_sub.add_parser("set", help="Write one config key")
    config_set.add_argument("key")
    config_set.add_argument("value")

    doctor_parser = subparsers.add_parser("doctor", help="Check local setup")
    doctor_parser.add_argument("--cwd", default=".")

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
    workflow_parser.add_argument("workflow")
    workflow_parser.add_argument("args", nargs=argparse.REMAINDER)

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


def _handle_project(args: argparse.Namespace) -> int:
    if args.project_command == "init":
        project = initialize_opengauss_project(args.path, name=args.name or None)
        print(f"Initialized project: {project.label}")
        print(project.root)
        return 0
    if args.project_command == "create":
        template_source = args.template_source or resolve_template_source(load_config(), os.environ)
        if not template_source:
            print("No template source configured. Use --template-source or set opengauss.project.template_source.", file=sys.stderr)
            return 1
        project = clone_project_template(args.path, template_source=template_source, name=args.name or None)
        print(f"Created project: {project.label}")
        print(project.root)
        return 0
    if args.project_command == "show":
        try:
            project = discover_opengauss_project(args.path)
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


class InteractiveShell:
    def __init__(self) -> None:
        self.console = Console()
        self.cwd = Path.cwd().resolve()
        self.active_skill = ""
        history_path = ensure_opengauss_home() / "history"
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

    def _model_label(self) -> str:
        config = load_config()
        model_cfg = config.get("model")
        if isinstance(model_cfg, dict):
            return str(model_cfg.get("default", "") or "")
        return str(model_cfg or "")

    def _project_label(self) -> str:
        try:
            return format_project_summary(discover_opengauss_project(self.cwd))
        except Exception:
            return "(none)"

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

    def _overlay_label(self) -> str:
        sources = {skill.source for skill in discover_skills(self.cwd) if skill.source in {"project", "user"}}
        if not sources:
            return "builtin"
        return "+".join(sorted(sources))

    def _bottom_toolbar(self) -> str:
        workflow_status = self._workflow_status_payload()
        phase = str(workflow_status.get("phase", "idle") or "idle")
        file_label = str(workflow_status.get("active_file_label", "") or "-")
        theorem = str(workflow_status.get("target_symbol", "") or "-")
        build = str(workflow_status.get("build_status", "") or "-")
        checkpoint = str(workflow_status.get("latest_checkpoint_label", "") or "-")
        return (
            f" workflow:{phase} | file:{file_label} | theorem:{theorem} | "
            f"build:{build} | checkpoint:{checkpoint} | skill:{self._active_skill_label()} | overlays:{self._overlay_label()} "
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

    def show_status(self) -> None:
        render_status_panel(
            self.console,
            cwd=self.cwd,
            project_label=self._project_label(),
            provider=self._provider_label(),
            model=self._model_label(),
            local_runtime=self._local_runtime_label(),
            home=get_opengauss_home(),
        )
        workflow_status = self._workflow_status_payload()
        if workflow_status:
            self.console.print()
            render_workflow_status_panel(self.console, status=workflow_status, activities=self._workflow_activity(limit=6))

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
            render_workflow_status_panel(self.console, status=payload or {"phase": "idle", "workflow_kind": "[none]"}, activities=self._workflow_activity(limit=12))
            return 0
        self.console.print("[dim]Usage: /workflow status|history|activity[/]")
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
            self.console.print("[dim]Usage: /project init|create|show ...[/]")
            return 1
        subcmd = argv[0]
        if subcmd == "init":
            path = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else str(self.cwd)
            name = ""
            if "--name" in argv:
                idx = argv.index("--name")
                if idx + 1 < len(argv):
                    name = argv[idx + 1]
            project = initialize_opengauss_project(path, name=name or None)
            self.cwd = project.root
            self.console.print(f"[bold #5DB8F5]Initialized project[/] {project.label}")
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
                self.console.print("[bold red]No template source configured.[/] Set `opengauss.project.template_source` or pass `--template-source`.")
                return 1
            project = clone_project_template(path, template_source=template_source, name=name or None)
            self.cwd = project.root
            self.console.print(f"[bold #5DB8F5]Created project[/] {project.label}")
            return 0
        if subcmd == "show":
            target = argv[1] if len(argv) > 1 else str(self.cwd)
            try:
                project = discover_opengauss_project(target)
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

        render_workflow_launch(self.console, launch_summary=describe_launch_plan(plan))
        code = run_workflow(raw, active_cwd=self.cwd, active_skill=self.active_skill or None)
        if code != 0:
            self.console.print(f"[bold red]Workflow exited with code {code}[/]")
        workflow_status = self._workflow_status_payload()
        if workflow_status:
            self.console.print()
            render_workflow_status_panel(self.console, status=workflow_status, activities=self._workflow_activity(limit=6))
        return code

    def _handle_command(self, raw: str) -> bool:
        stripped = raw.strip()
        if not stripped:
            return True

        first_token = stripped.split(" ", 1)[0]
        if first_token in WORKFLOW_COMMANDS or first_token in FORGIVING_WORKFLOW_ALIAS_MAP:
            self._run_workflow_command(stripped)
            return True

        if stripped in {"/exit", "/quit", "exit", "quit"}:
            return False
        if stripped in {"/help", "help"}:
            self.show_help()
            return True
        if stripped == "/status":
            self.show_status()
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
        if stripped == "/doctor":
            issues, output = run_doctor(self.cwd)
            print(output)
            if issues:
                self.console.print(f"[dim]{len(issues)} issue(s) found.[/]")
            return True
        if stripped.startswith("/provider"):
            self._run_provider_command(stripped)
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
        with patch_stdout():
            while True:
                try:
                    raw = self.session.prompt("opengauss> ", bottom_toolbar=self._bottom_toolbar)
                except EOFError:
                    print()
                    return 0
                except KeyboardInterrupt:
                    print()
                    continue
                if not self._handle_command(raw):
                    return 0


def main(argv: list[str] | None = None) -> int:
    _seed_environment()
    ensure_opengauss_home()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        return InteractiveShell().run()
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "config":
        return _handle_config(args)
    if args.command == "doctor":
        issues, output = run_doctor(args.cwd)
        print(output)
        return 0 if not issues else 1
    if args.command == "project":
        return _handle_project(args)
    if args.command == "workflow":
        if args.workflow in {"status", "history", "activity"}:
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
            render_workflow_status_panel(Console(), status=payload or {"phase": "idle", "workflow_kind": "[none]"}, activities=read_workflow_activity(limit=12) if args.workflow != "status" else read_workflow_activity(limit=8))
            return 0 if payload else 1
        text = f"/{args.workflow}" if not str(args.workflow).startswith("/") else str(args.workflow)
        if args.args:
            text = f"{text} {' '.join(args.args)}"
        try:
            plan = resolve_workflow_request(text, active_cwd=Path.cwd())
        except ProjectNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            print("Use `opengauss project init` inside a Lean repo or `opengauss project create <path>` to clone one.", file=sys.stderr)
            return 1
        except Exception as exc:
            print(format_runtime_provider_error(exc), file=sys.stderr)
            return 1
        render_workflow_launch(Console(), launch_summary=describe_launch_plan(plan))
        return run_workflow(text, active_cwd=Path.cwd())
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

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
