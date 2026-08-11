"""Run the prompt-toolkit LeanFlow shell and its workflow controls."""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from rich.console import Console

from core.process_identity import PROCESS_TOKEN_ENV, process_token_sha256
from leanflow_cli.cli.banner import (
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
from leanflow_cli.cli.cli_handlers import (
    _handle_config,  # noqa: F401
    _handle_models,  # noqa: F401
    _handle_sandbox,  # noqa: F401
    _parse_config_value,
    _print_json,
    _print_mcp_bootstrap,
    _print_mcp_status,
    _print_project_power_setup,
    _project_payload,
)
from leanflow_cli.cli.commands import SlashCommandCompleter, build_workflow_command_set
from leanflow_cli.cli.doctor import run_doctor
from leanflow_cli.cli.mcp_bootstrap import bootstrap_lean_mcp
from leanflow_cli.cli.shell_ui import (
    bottom_toolbar as _build_bottom_toolbar,
)
from leanflow_cli.cli.shell_ui import (
    prompt_focus_label as _build_prompt_focus_label,
)
from leanflow_cli.cli.shell_ui import (
    prompt_message as _build_prompt_message,
)
from leanflow_cli.cli.shell_ui import (
    toolbar_piece as _build_toolbar_piece,
)
from leanflow_cli.config import (
    ensure_leanflow_home,
    get_config_value,
    get_leanflow_home,
    load_config,
    set_config_value,
)
from leanflow_cli.local_models import (
    get_local_runtime_status,
    list_local_runtimes,
    read_local_runtime_logs,
    resolve_active_local_runtime,
    start_local_runtime,
    stop_local_runtime,
    use_local_runtime,
)
from leanflow_cli.runtime.runtime_provider import (
    format_runtime_provider_error,
    list_runtime_provider_targets,
    resolve_runtime_provider,
)
from leanflow_cli.runtime.skill_core import discover_skill_commands, discover_skills, load_skill
from leanflow_cli.workflow import (
    FORGIVING_WORKFLOW_ALIAS_MAP,
    describe_launch_plan,
    resolve_workflow_request,
    spawn_workflow,
)
from leanflow_cli.workflows.project import (
    ProjectNotFoundError,
    clone_project_template,
    discover_leanflow_project,
    format_project_summary,
    initialize_leanflow_project,
    resolve_template_source,
    setup_project_power_modes,
)
from leanflow_cli.workflows.workflow_state import (
    enqueue_workflow_agent_message,
    interrupt_workflow_process,
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

# Derived from the single COMMAND_REGISTRY in leanflow_cli.cli.commands: the set of all
# frontend workflow slash commands (canonical commands plus their long-form aliases).
from tools.mcp.mcp_tool import get_mcp_status

WORKFLOW_COMMANDS = build_workflow_command_set()


def _mcp_status_payload() -> dict[str, Any]:
    servers = list(get_mcp_status())
    return {
        "servers": servers,
        "count": len(servers),
    }


class InteractiveShell:
    def __init__(self) -> None:
        self.console = Console()
        self.cwd = Path.cwd().resolve()
        self.active_skill = ""
        history_path = ensure_leanflow_home() / "history"
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
            return format_project_summary(discover_leanflow_project(self.cwd))
        except Exception:
            return "(none)"

    def _project_name(self) -> str:
        try:
            project = discover_leanflow_project(self.cwd)
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
        return _build_toolbar_piece(value, max_len)

    def _prompt_focus_label(self) -> str:
        return _build_prompt_focus_label(self._workflow_status_payload(), self._target_label())

    def _prompt_message(self) -> FormattedText:
        return _build_prompt_message(
            self._project_name(),
            self._phase_label(),
            self._prompt_focus_label(),
        )

    def _bottom_toolbar(self) -> str:
        return _build_bottom_toolbar(
            self._workflow_status_payload(),
            self._model_label(),
            self._target_label(),
            self._active_skill_label(),
            self._latest_activity_label(),
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
        """Display overall shell status (project, provider, model, local runtime) and optionally show a specific workflow agent's details if agent-id is provided."""
        argv = argv or []
        render_status_panel(
            self.console,
            cwd=self.cwd,
            project_label=self._project_label(),
            provider=self._provider_label(),
            model=self._model_label(),
            local_runtime=self._local_runtime_label(),
            home=get_leanflow_home(),
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
            render_workflow_status_panel(
                self.console, status=workflow_status, activities=self._workflow_activity(limit=6)
            )
        agents = self._workflow_agents(activity_limit=4)
        if agents:
            self.console.print()
            self.console.print("[bold #5DB8F5]Workflow Agents[/]")
            render_swarm_table(self.console, agents=agents)
        return 0

    def _run_swarm_command(self, argv: list[str]) -> int:
        """Dispatch /swarm subcommands: list workflow agents, kill an agent by ID, or attach to an agent's transcript with a live-follow loop and interactive prompt handling."""
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
                self.console.print(
                    f"[bold red]{result.get('error', 'Failed to kill workflow agent.')}[/]"
                )
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
        self.console.print(
            "[dim]Following agent output. Press Ctrl+C to return to the swarm prompt.[/]"
        )
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
                self.console.print(
                    "\n[dim]Stopped following live output. Agent remains available in swarm mode.[/]"
                )
            state = str(agent.get("status", "") or "[unknown]")
            if state in {"exited", "completed", "stopped", "interrupted"}:
                self.console.print(
                    f"[dim]Agent {agent.get('agent_id')} is {state}. Returning to the main shell.[/]"
                )
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
                    self.console.print(
                        f"[bold red]{result.get('error', 'Failed to kill workflow agent.')}[/]"
                    )
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
            self.console.print(
                "[dim]Following agent output. Press Ctrl+C to return to the swarm prompt.[/]"
            )

    def _run_kill_command(self, argv: list[str]) -> int:
        if not argv:
            self.console.print("[dim]Usage: /kill <agent-id>[/]")
            return 1
        result = terminate_workflow_agent(argv[0])
        if not result.get("success"):
            self.console.print(
                f"[bold red]{result.get('error', 'Failed to kill workflow agent.')}[/]"
            )
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
        activity = [
            {
                "type": row["state"],
                "timestamp": row["timestamp"],
                "message": f"{row['label']} — {row['files']}",
            }
            for row in table
        ]
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
            render_workflow_status_panel(
                self.console, status=payload, activities=self._workflow_activity(limit=10)
            )
            return 0
        if subcmd == "history":
            self._render_workflow_history()
            return 0
        if subcmd == "activity":
            payload = self._workflow_status_payload()
            render_workflow_status_panel(
                self.console,
                status=payload or {"phase": "idle", "workflow_kind": "[none]"},
                activities=self._workflow_activity(limit=20),
            )
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
            self.console.print(
                f"[bold #5DB8F5]Reloaded skill overlays[/] ({count} skill(s) available)"
            )
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
        """Dispatch /project subcommands: init (initialize Lean workspace), create (clone template), and show (render project state). Updates shell cwd on successful init/create."""
        if not argv:
            try:
                project = discover_leanflow_project(self.cwd)
            except ProjectNotFoundError:
                self.console.print(
                    "[dim]No active Lean workspace is open here. Use `/project init` in a Lean repo or `/project create <path>` to start one.[/]"
                )
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
            already_initialized = (target / ".leanflow" / "project.yaml").is_file()
            project = initialize_leanflow_project(path, name=name or None)
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
                self.console.print(
                    "[dim]Usage: /project create <path> [--template-source <source>] [--name <name>] [/]"
                )
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
                self.console.print(
                    "[bold red]No template source configured.[/] Set `leanflow.project.template_source` or pass `--template-source`."
                )
                return 1
            project = clone_project_template(
                path, template_source=template_source, name=name or None
            )
            self.cwd = project.root
            self._plain_notice(f"Created project: {project.label}")
            return 0
        if subcmd == "show":
            target = argv[1] if len(argv) > 1 else str(self.cwd)
            try:
                project = discover_leanflow_project(target)
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
        self.console.print(
            "[dim]Usage: /models local list|status <runtime>|start <runtime> <model>|stop <runtime>|logs <runtime>|use <runtime> <model>[/]"
        )
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
            target = next(
                (
                    arg
                    for arg in args
                    if arg not in {"bootstrap", "--json"} and not arg.startswith("--")
                ),
                "lean",
            )
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
        """Parse workflow request, deduplicate against active agents, build live-status payload (including formalization metadata), spawn background process, and display launch confirmation."""
        try:
            plan = resolve_workflow_request(
                raw, active_cwd=self.cwd, active_skill=self.active_skill or None
            )
        except ProjectNotFoundError as exc:
            self.console.print(f"[bold red]{exc}[/]")
            self.console.print(
                "[dim]Use `/project init` inside a Lean repo, or `/project create <path>` to clone a template.[/]"
            )
            return 1
        except Exception as exc:
            self.console.print(f"[bold red]{format_runtime_provider_error(exc)}[/]")
            return 1

        existing = next(
            (
                agent
                for agent in self._workflow_agents(activity_limit=1)
                if not str(agent.get("parent_agent_id", "") or "")
                and str(agent.get("project_root", "") or "") == str(plan.project.root)
                and str(agent.get("workflow_kind", "") or "") == str(plan.workflow.workflow_kind)
                and str(agent.get("workflow_command", "") or "")
                == str(plan.workflow.backend_command)
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
            "active_skill": str(
                plan.active_skill or current_status.get("active_skill", "") or "[none]"
            ),
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
            "latest_checkpoint_label": str(
                current_status.get("latest_checkpoint_label", "") or "[none]"
            ),
            "latest_filesystem_checkpoint": str(
                current_status.get("latest_filesystem_checkpoint", "") or "[none]"
            ),
            "last_compaction_reason": str(
                current_status.get("last_compaction_reason", "") or "[none]"
            ),
            "snapshot_present": bool(current_status.get("snapshot_present", False)),
            "held_locks": int(current_status.get("held_locks", 0) or 0),
        }
        if plan.formalization_document is not None:
            status_payload.update(
                {
                    "formalization_document": plan.formalization_document.source_relative,
                    "formalization_document_kind": plan.formalization_document.source_kind,
                    "formalization_request_kind": str(
                        plan.formalization_document.metadata.get("document_request_kind", "file")
                        or "file"
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
                    "formalization_extracted_blueprint_path": str(
                        plan.formalization_document.blueprint_path
                    ),
                    "formalization_target_file": plan.formalization_document.target_lean_relative,
                }
            )
        save_workflow_live_status(status_payload)
        launched_plan, process = spawn_workflow(
            raw,
            active_cwd=self.cwd,
            active_skill=self.active_skill or None,
            interactive=False,
        )
        status_payload.update(
            {
                "process_id": process.pid,
                "process_group_id": process.pid if os.name == "posix" else 0,
                "process_session_id": process.pid if os.name == "posix" else 0,
                "process_token_sha256": process_token_sha256(
                    launched_plan.child_env.get(PROCESS_TOKEN_ENV, "")
                ),
            }
        )
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
            render_workflow_status_panel(
                self.console, status=workflow_status, activities=self._workflow_activity(limit=6)
            )
        return 0

    def _shutdown_project_workflows(self) -> None:
        """Gracefully shut down all running workflow agents for the current project. First requests graceful exit with timeout, then forcefully interrupts via SIGINT if needed."""
        project_root = ""
        try:
            project = discover_leanflow_project(self.cwd)
            project_root = str(project.root)
        except Exception:
            status = self._workflow_status_payload()
            project_root = str(status.get("project_root", "") or "")
        if not project_root:
            return
        graceful = request_project_workflow_runner_exit(project_root)
        queued = [
            str(agent_id or "") for agent_id in graceful.get("queued", []) if str(agent_id or "")
        ]
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
                    f"[dim]Requested clean exit for {len(queued)} workflow runner(s) in {project_root} before exiting the shell.[/]",
                    soft_wrap=True,
                )
                return

        result = terminate_project_workflow_agents(project_root)
        count = int(result.get("count", 0) or 0)
        if count:
            self.console.print(
                f"[dim]Interrupted {count} workflow agent(s) for {project_root} before exiting the shell.[/]",
                soft_wrap=True,
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
        interrupted = interrupt_workflow_process(status)
        if not interrupted.get("success"):
            return
        self.console.print(
            f"[dim]Interrupted background workflow runner (pid {process_id}) for {project_root} before exiting the shell.[/]",
            soft_wrap=True,
        )

    def _handle_command(self, raw: str) -> bool:
        """Parse and dispatch raw shell input to slash-command handlers, workflow commands, and exit/control flow. Return False to exit the REPL; True to continue."""
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
            resolved = (
                (self.cwd / target).resolve() if not target.is_absolute() else target.resolve()
            )
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

        self.console.print(
            "[dim]Plain chat is not part of the kernel build. Use /help, /project, or a Lean workflow command.[/]"
        )
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
