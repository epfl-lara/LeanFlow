"""LeanFlow welcome banner and shell help rendering."""

from __future__ import annotations

import shutil
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from leanflow_cli import __version__
from leanflow_cli.cli.commands import COMMANDS_BY_CATEGORY
from leanflow_cli.runtime.branding import (
    BRAND_COLORS,
    get_cli_command_name,
    get_product_name,
    get_product_subtitle,
    get_product_tagline,
)

LEANFLOW_WORDMARK = "\n".join(
    [
        f"[bold {BRAND_COLORS['primary_soft']}]██╗     ███████╗ █████╗ ███╗   ██╗███████╗██╗      ██████╗ ██╗    ██╗[/]",
        f"[bold {BRAND_COLORS['primary_soft']}]██║     ██╔════╝██╔══██╗████╗  ██║██╔════╝██║     ██╔═══██╗██║    ██║[/]",
        f"[bold {BRAND_COLORS['primary']}]██║     █████╗  ███████║██╔██╗ ██║█████╗  ██║     ██║   ██║██║ █╗ ██║[/]",
        f"[bold {BRAND_COLORS['primary']}]██║     ██╔══╝  ██╔══██║██║╚██╗██║██╔══╝  ██║     ██║   ██║██║███╗██║[/]",
        f"[bold {BRAND_COLORS['primary_dim']}]███████╗███████╗██║  ██║██║ ╚████║██║     ███████╗╚██████╔╝╚███╔███╔╝[/]",
        f"[bold {BRAND_COLORS['primary_dim']}]╚══════╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝ [/]",
        f"[{BRAND_COLORS['muted']}]{get_product_tagline()}[/]",
    ]
)

MISSION_LINE = "Dedicated to verified Lean proving and autoformalization"

WORKFLOW_DISPLAY_NAMES = {
    "autoprove": "prove",
    "autoformalize": "formalize",
}


def _shorten_middle(text: str, max_len: int) -> str:
    if max_len <= 0 or len(text) <= max_len:
        return text
    if max_len <= 7:
        return text[:max_len]
    head = max(2, (max_len - 3) // 2)
    tail = max(2, max_len - head - 3)
    return f"{text[:head]}...{text[-tail:]}"


def _build_metric_strip(
    *, project_label: str, model: str, provider: str, local_runtime: str
) -> Table:
    strip = Table.grid(expand=True)
    strip.add_column(ratio=1)
    strip.add_column(ratio=1)
    strip.add_column(ratio=1)
    strip.add_row(
        f"[bold {BRAND_COLORS['primary_soft']}]Model[/] [{BRAND_COLORS['text']}]{_shorten_middle(model, 28)}[/]",
        f"[bold {BRAND_COLORS['primary_soft']}]Route[/] [{BRAND_COLORS['text']}]{_shorten_middle(provider, 44)}[/]",
        f"[bold {BRAND_COLORS['primary_soft']}]Runtime[/] [{BRAND_COLORS['text']}]{_shorten_middle(local_runtime, 24)}[/]",
    )
    return strip


def build_welcome_banner(
    console: Console,
    *,
    cwd: str,
    model: str,
    provider: str,
    project_label: str,
    local_runtime: str,
) -> None:
    """Render the LeanFlow welcome banner with wordmark, metrics (model/route/runtime), and launch path hints. Adapts content density based on terminal width (simplified at <90 cols) and prints to console."""
    cli_name = get_cli_command_name()
    product_name = get_product_name()
    width = shutil.get_terminal_size().columns
    simplified = width < 90

    right = Table.grid(padding=(0, 1))
    right.add_column()
    if simplified:
        right.add_row(f"[bold {BRAND_COLORS['primary']}]Start Here[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/project init[/]  [dim]register this Lean repo[/]")
        right.add_row(
            f"[{BRAND_COLORS['text']}]/prove Main.lean[/]  [dim]autonomous proving loop[/]"
        )
        right.add_row(
            f"[{BRAND_COLORS['text']}]/formalize docs/paper.tex[/]  [dim]document formalization[/]"
        )
        right.add_row(f"[{BRAND_COLORS['text']}]/project[/]  [dim]current Lean workspace[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/help[/]  [dim]all commands[/]")
    else:
        right.add_row(f"[bold {BRAND_COLORS['primary']}]Launch Paths[/]")
        right.add_row(
            f"[{BRAND_COLORS['text']}]/project init[/]  [dim]register an existing Lean 4 repo[/]"
        )
        right.add_row(
            f"[{BRAND_COLORS['text']}]/prove Main.lean[/]  [dim]autonomous proving loop[/]"
        )
        right.add_row(
            f"[{BRAND_COLORS['text']}]/prove Main.lean --agents 3[/]  [dim]user-approved Lean swarm[/]"
        )
        right.add_row(
            f"[{BRAND_COLORS['text']}]/formalize docs/paper.tex[/]  [dim]document formalization[/]"
        )
        right.add_row(f"[{BRAND_COLORS['text']}]/project[/]  [dim]show current Lean workspace[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/status[/]  [dim]live project and runner state[/]")
        right.add_row(
            f"[{BRAND_COLORS['text']}]/swarm[/]  [dim]active workflow agents and recent output[/]"
        )
        right.add_row(
            f"[{BRAND_COLORS['text']}]/workflow activity[/]  [dim]recent managed steps[/]"
        )
        right.add_row(
            f"[{BRAND_COLORS['text']}]/workflow log 120[/]  [dim]saved managed runner log[/]"
        )
        right.add_row(f"[{BRAND_COLORS['text']}]/help[/]  [dim]command catalog and tips[/]")

    launch_panel = Panel(
        right,
        title=f"[bold {BRAND_COLORS['primary_soft']}]Workflow[/]",
        border_style=BRAND_COLORS["panel"],
        box=box.SQUARE,
        padding=(0, 1),
    )

    console.print()
    console.print(LEANFLOW_WORDMARK, justify="center")
    console.print()
    banner_group = Group(
        Panel(
            _build_metric_strip(
                project_label=project_label,
                model=model,
                provider=provider,
                local_runtime=local_runtime,
            ),
            title=f"[bold {BRAND_COLORS['primary_soft']}]{product_name} v{__version__}[/]",
            subtitle=f"[dim]{get_product_subtitle()}[/]",
            border_style=BRAND_COLORS["panel"],
            box=box.SQUARE,
            padding=(0, 1),
        ),
        launch_panel,
    )
    console.print(banner_group)
    console.print(f"[bold {BRAND_COLORS['muted']}]{MISSION_LINE}[/]")
    footer = Table.grid(expand=True)
    footer.add_column(ratio=1)
    footer.add_column(justify="right")
    footer.add_row(
        f"[{BRAND_COLORS['muted']}]Prompt-centered Lean shell via `{cli_name}`[/]",
        f"[{BRAND_COLORS['muted']}]Try /project, /help, or prove Main.lean[/]",
    )
    console.print(footer)


def render_help(console: Console) -> None:
    for category, commands in COMMANDS_BY_CATEGORY.items():
        table = Table(box=box.SIMPLE_HEAD, show_header=False, pad_edge=False)
        table.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
        table.add_column(style=BRAND_COLORS["text"])
        for cmd, desc in commands.items():
            table.add_row(cmd, desc)
        console.print()
        console.print(f"[bold {BRAND_COLORS['primary']}]{category}[/]")
        console.print(table)
    console.print()
    console.print(
        "[dim]Tip: workflow commands also accept forgiving forms like `prove Main.lean` without the leading slash.[/]"
    )
    console.print(
        "[dim]Tip: add `--agents N` to `prove` or `formalize` only when you explicitly want user-approved swarm mode.[/]"
    )
    console.print(
        "[dim]Tip: use `/provider local`, `/provider zai`, or `/provider custom` to inspect how a request will resolve before launching a workflow.[/]"
    )
    console.print(
        "[dim]Tip: use `/workflow activity` for structured managed steps and `/workflow log 120` for the full saved runner log.[/]"
    )


def render_swarm_table(console: Console, *, agents: list[dict[str, object]]) -> None:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("Agent", style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column("Task", style=BRAND_COLORS["text"], no_wrap=True)
    table.add_column("State", style=BRAND_COLORS["primary_dim"], no_wrap=True)
    table.add_column("Depth", style=BRAND_COLORS["text"], no_wrap=True)
    table.add_column("Model", style=BRAND_COLORS["text"])
    table.add_column("Calls", style=BRAND_COLORS["text"], no_wrap=True)
    table.add_column("Last Update", style=BRAND_COLORS["muted"], no_wrap=True)
    table.add_column("Latest", style=BRAND_COLORS["text"])
    for agent in agents:
        agent_id = str(agent.get("agent_id", "") or "")
        table.add_row(
            _shorten_middle(agent_id, 18),
            str(agent.get("task_label", "") or "agent"),
            str(agent.get("status", "") or "active"),
            str(agent.get("delegate_depth", 0)),
            _shorten_middle(str(agent.get("model", "") or "[unknown]"), 28),
            str(agent.get("api_calls", 0)),
            str(agent.get("last_event_at", "") or "")[-8:],
            str(agent.get("last_message", "") or ""),
        )
    console.print(table)


def render_swarm_agent_panel(
    console: Console, *, agent: dict[str, object], recent_limit: int = 5
) -> None:
    """Display a single workflow agent's metadata panel (ID, task, parent, state, depth, model, provider, API/tool call counts) and optional recent activity log truncated to recent_limit events."""
    table = Table.grid(padding=(0, 1))
    table.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column(style=BRAND_COLORS["text"])
    table.add_row("Agent", str(agent.get("agent_id", "") or "[unknown]"))
    table.add_row("Task", str(agent.get("task_label", "") or "agent"))
    table.add_row("Parent", str(agent.get("parent_agent_id", "") or "[root]"))
    table.add_row("State", str(agent.get("status", "") or "[unknown]"))
    table.add_row("Depth", str(agent.get("delegate_depth", 0)))
    table.add_row("Model", str(agent.get("model", "") or "[unknown]"))
    table.add_row("Provider", str(agent.get("provider", "") or "[unknown]"))
    table.add_row("Base URL", str(agent.get("base_url", "") or "[unknown]"))
    table.add_row("API calls", str(agent.get("api_calls", 0)))
    table.add_row("Tool calls", str(agent.get("tool_calls", 0)))
    table.add_row("Started", str(agent.get("started_at", "") or "[unknown]"))
    table.add_row("Finished", str(agent.get("finished_at", "") or "[active]"))
    console.print(
        Panel(
            table,
            title=f"[bold {BRAND_COLORS['primary']}]Workflow Agent[/]",
            subtitle="[dim]agent detail[/]",
            border_style=BRAND_COLORS["panel"],
            box=box.SQUARE,
        )
    )

    recent = agent.get("recent_activity")
    if isinstance(recent, list) and recent:
        console.print()
        console.print(f"[bold {BRAND_COLORS['primary']}]Recent Agent Activity[/]")
        activity_table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
        activity_table.add_column(
            "Time", style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True
        )
        activity_table.add_column("Type", style=BRAND_COLORS["primary_dim"], no_wrap=True)
        activity_table.add_column("Preview", style=BRAND_COLORS["text"])
        for event in recent[-max(1, recent_limit) :]:
            activity_table.add_row(
                str(event.get("timestamp", "") or "")[-8:],
                str(event.get("type", "") or ""),
                str(event.get("preview", "") or ""),
            )
        console.print(activity_table)


def render_swarm_transcript(
    console: Console, *, agent: dict[str, object], transcript: list[dict[str, object]]
) -> None:
    title = str(agent.get("agent_id", "") or "[unknown]")
    header = Table.grid(padding=(0, 1))
    header.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    header.add_column(style=BRAND_COLORS["text"])
    header.add_row("Agent", title)
    header.add_row("Task", str(agent.get("task_label", "") or "agent"))
    header.add_row("State", str(agent.get("status", "") or "[unknown]"))
    header.add_row("Model", str(agent.get("model", "") or "[unknown]"))
    header.add_row("Parent", str(agent.get("parent_agent_id", "") or "[root]"))
    console.print(
        Panel(
            header,
            title=f"[bold {BRAND_COLORS['primary']}]Swarm View[/]",
            subtitle="[dim]recent agent transcript[/]",
            border_style=BRAND_COLORS["panel"],
            box=box.SQUARE,
        )
    )

    if not transcript:
        console.print("[dim]No transcript events recorded for this agent yet.[/]")
        return

    console.print()
    for entry in transcript:
        render_swarm_transcript_entry(console, entry=entry)


def render_swarm_transcript_entry(console: Console, *, entry: dict[str, object]) -> None:
    role = str(entry.get("role", "") or "event")
    timestamp = str(entry.get("timestamp", "") or "")[-8:]
    content = str(entry.get("content", "") or "").strip() or "[no content]"
    if role == "user":
        label = "User"
        style = BRAND_COLORS["primary_soft"]
    elif role == "assistant":
        label = "Agent"
        style = BRAND_COLORS["text"]
    elif role == "tool-call":
        label = "Tool Call"
        style = BRAND_COLORS["primary_dim"]
    elif role == "tool-result":
        label = "Tool Result"
        style = BRAND_COLORS["muted"]
    else:
        label = "Event"
        style = BRAND_COLORS["muted"]
    body = Text()
    body.append(f"{timestamp}  {label}\n", style=f"bold {style}")
    body.append(content, style=BRAND_COLORS["text"])
    console.print(Panel(body, border_style=BRAND_COLORS["panel"], box=box.SQUARE, padding=(0, 1)))


def render_status_panel(
    console: Console,
    *,
    cwd: Path,
    project_label: str,
    provider: str,
    model: str,
    local_runtime: str,
    home: Path,
) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column(style=BRAND_COLORS["text"])
    table.add_row("Home", str(home))
    table.add_row("CWD", str(cwd))
    table.add_row("Project", project_label or "(none)")
    table.add_row("Provider", provider)
    table.add_row("Model", model)
    table.add_row("Local runtime", local_runtime)
    console.print(
        Panel(
            table,
            title=f"[bold {BRAND_COLORS['primary']}]{get_product_name()} Status[/]",
            subtitle="[dim]shell context[/]",
            border_style=BRAND_COLORS["panel"],
            box=box.SQUARE,
        )
    )


def render_provider_panel(
    console: Console, *, resolved: dict[str, str], requested: str, targets: list[dict[str, str]]
) -> None:
    """Show the resolved provider configuration (requested vs actual provider, API mode, base URL, model, reasoning effort) and list available target providers to choose from."""
    current = Table.grid(padding=(0, 1))
    current.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    current.add_column(style=BRAND_COLORS["text"])
    current.add_row("Requested", requested or "auto")
    current.add_row("Resolved provider", str(resolved.get("provider", "")))
    current.add_row("API mode", str(resolved.get("api_mode", "")))
    current.add_row("Base URL", str(resolved.get("base_url", "")))
    current.add_row("Model", str(resolved.get("model", "")))
    if resolved.get("reasoning_effort"):
        current.add_row("Reasoning", str(resolved.get("reasoning_effort", "")))
    current.add_row("Source", str(resolved.get("source", "")))

    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("Target", style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column("Kind", style=BRAND_COLORS["primary_dim"], no_wrap=True)
    table.add_column("Description", style=BRAND_COLORS["text"])
    for target in targets:
        table.add_row(
            str(target.get("name", "")),
            str(target.get("kind", "")),
            str(target.get("description", "")),
        )

    console.print(
        Panel(
            current,
            title=f"[bold {BRAND_COLORS['primary']}]Resolved Provider[/]",
            subtitle="[dim]current route[/]",
            border_style=BRAND_COLORS["panel"],
            box=box.SQUARE,
        )
    )
    console.print()
    console.print(f"[bold {BRAND_COLORS['primary']}]Available Targets[/]")
    console.print(table)


def render_project_panel(console: Console, *, project_summary: dict[str, str]) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column(style=BRAND_COLORS["text"])
    for key, value in project_summary.items():
        table.add_row(key, value)
    console.print(
        Panel(
            table,
            title=f"[bold {BRAND_COLORS['primary']}]{get_product_name()} Project[/]",
            subtitle="[dim]registered Lean workspace[/]",
            border_style=BRAND_COLORS["panel"],
            box=box.SQUARE,
        )
    )


def render_local_runtime_table(console: Console, *, runtimes: list[dict[str, object]]) -> None:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("Runtime", style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column("Endpoint", style=BRAND_COLORS["primary_dim"], no_wrap=True)
    table.add_column("Model", style=BRAND_COLORS["text"])
    table.add_column("Active", style=BRAND_COLORS["text"], no_wrap=True)
    for runtime in runtimes:
        endpoint = f"{runtime.get('host', '')}:{runtime.get('port', '')}".strip(":")
        table.add_row(
            str(runtime.get("runtime", "")),
            endpoint,
            str(runtime.get("model", "") or "[configured later]"),
            "yes" if runtime.get("active") else "",
        )
    console.print(table)


def render_workflow_launch(console: Console, *, launch_summary: dict[str, str]) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column(style=BRAND_COLORS["text"])
    table.add_row("Workflow", launch_summary.get("workflow", ""))
    table.add_row("Command", launch_summary.get("command", ""))
    table.add_row("Project", launch_summary.get("project", ""))
    table.add_row("Root", launch_summary.get("project_root", ""))
    table.add_row("Provider", launch_summary.get("provider", ""))
    table.add_row("Model", launch_summary.get("model", ""))
    if launch_summary.get("reasoning_effort"):
        table.add_row("Reasoning", launch_summary.get("reasoning_effort", ""))
    table.add_row("Skill", launch_summary.get("skill", ""))
    table.add_row("Agents", launch_summary.get("agents", "1"))
    if launch_summary.get("document"):
        table.add_row("Document", launch_summary.get("document", ""))
    if launch_summary.get("target_file"):
        table.add_row("Target file", launch_summary.get("target_file", ""))
    table.add_row("Base URL", launch_summary.get("base_url", ""))
    console.print(
        Panel(
            table,
            title=f"[bold {BRAND_COLORS['primary']}]Launching Workflow[/]",
            subtitle="[dim]managed Lean execution plan[/]",
            border_style=BRAND_COLORS["panel"],
            box=box.SQUARE,
        )
    )


def render_workflow_status_panel(
    console: Console,
    *,
    status: dict[str, object],
    activities: list[dict[str, object]] | None = None,
) -> None:
    """Display managed workflow state, including any terminal process outcome.

    Show workflow, proof, queue, provider, checkpoint, and optional recent activity
    details. Flag stale snapshots in the phase display.
    """
    workflow_name = str(status.get("workflow_kind", "[none]") or "[none]")
    workflow_name = WORKFLOW_DISPLAY_NAMES.get(workflow_name, workflow_name)
    phase = str(status.get("phase", "[none]") or "[none]")
    stale_snapshot = bool(status.get("stale_snapshot"))
    if stale_snapshot and phase == "dead":
        phase = "dead (stale snapshot)"
    table = Table.grid(padding=(0, 1))
    table.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column(style=BRAND_COLORS["text"])
    table.add_row("Phase", phase)
    table.add_row("Workflow", workflow_name)
    table.add_row("Command", str(status.get("workflow_command", "[none]")))
    table.add_row("Project", str(status.get("project_root", "[none]")))
    table.add_row("Provider", str(status.get("provider", "[none]")))
    table.add_row("Model", str(status.get("model", "[none]")))
    table.add_row("Skill", str(status.get("active_skill", "(none)")))
    table.add_row("Agents", str(status.get("parallel_agents", "1")))
    if "exit_code" in status:
        exit_code = str(status.get("exit_code", "[unknown]"))
        exit_reason = str(status.get("reason", "") or "").strip()
        table.add_row("Exit", f"{exit_code}: {exit_reason}" if exit_reason else exit_code)
    if bool(status.get("startup_reconciliation_pending")):
        table.add_row("Proof state", "prior durable snapshot; reconciliation pending")
    table.add_row("File", str(status.get("active_file_label", "[unknown]")))
    table.add_row("Theorem", str(status.get("target_symbol", "[unknown]")))
    if bool(status.get("project_prove_manager")):
        raw_queue = status.get("project_prove_file_queue", [])
        raw_completed = status.get("project_prove_completed_files", [])
        queue_items = raw_queue if isinstance(raw_queue, list) else []
        completed_items = raw_completed if isinstance(raw_completed, list) else []
        queue = [str(item or "") for item in queue_items if str(item or "")]
        completed = [str(item or "") for item in completed_items if str(item or "")]
        source = str(status.get("project_prove_plan_source", "") or "active")
        table.add_row(
            "Project manager", f"{source}; {len(queue)} queued, {len(completed)} completed"
        )
        if queue:
            shown = queue[:4]
            suffix = f", +{len(queue) - len(shown)} more" if len(queue) > len(shown) else ""
            table.add_row("File queue", ", ".join(shown) + suffix)
    table.add_row("Build", str(status.get("build_status", "unknown")))
    warning_cleanup_status = str(status.get("warning_cleanup_status", "") or "").strip()
    if warning_cleanup_status:
        warning_count = int(status.get("warning_cleanup_warning_count", 0) or 0)
        attempted = (
            "attempted" if bool(status.get("warning_cleanup_attempted")) else "not attempted"
        )
        if bool(status.get("warning_cleanup_verified")):
            terminal = "accepted" if warning_cleanup_status == "accepted" else "verified"
            attempted = f"{attempted}, {terminal}"
        table.add_row(
            "Warning cleanup", f"{warning_cleanup_status}; {attempted}; warnings {warning_count}"
        )
    project_sorries = status.get("project_sorry_count")
    table.add_row(
        "Project sorries",
        str(project_sorries) if isinstance(project_sorries, int) else "[unknown]",
    )
    table.add_row("Checkpoint", str(status.get("latest_checkpoint_label", "[none]")))
    table.add_row("Locks", str(status.get("held_locks", 0)))
    if stale_snapshot:
        table.add_row("Stale PID", str(status.get("stale_process_id", "[unknown]")))
    table.add_row("Updated", str(status.get("updated_at", "[unknown]")))
    console.print(
        Panel(
            table,
            title=f"[bold {BRAND_COLORS['primary']}]Managed Workflow Status[/]",
            subtitle="[dim]live Lean runner state[/]",
            border_style=BRAND_COLORS["panel"],
            box=box.SQUARE,
        )
    )

    if activities:
        console.print()
        console.print(f"[bold {BRAND_COLORS['primary']}]Recent Activity[/]")
        activity_table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
        activity_table.add_column(
            "Time", style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True
        )
        activity_table.add_column("Type", style=BRAND_COLORS["primary_dim"], no_wrap=True)
        activity_table.add_column("Context", style=BRAND_COLORS["muted"], no_wrap=True)
        activity_table.add_column("Message", style=BRAND_COLORS["text"])
        for event in activities:
            timestamp = str(event.get("timestamp", "") or "")
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            context = (
                str(
                    details.get("tool", "")
                    or details.get("phase", "")
                    or details.get("iteration", "")
                    or ""
                )
                if isinstance(details, dict)
                else ""
            )
            activity_table.add_row(
                timestamp[-8:] if len(timestamp) >= 8 else timestamp,
                str(event.get("type", "")),
                context,
                str(event.get("message", "")),
            )
        console.print(activity_table)


def render_skill_table(
    console: Console, *, skills: list[dict[str, str]], active_skill: str = ""
) -> None:
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("Skill", style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column("Source", style=BRAND_COLORS["primary_dim"], no_wrap=True)
    table.add_column("Description", style=BRAND_COLORS["text"])
    table.add_column("Active", style=BRAND_COLORS["text"], no_wrap=True)
    for skill in skills:
        table.add_row(
            skill.get("name", ""),
            skill.get("source", ""),
            skill.get("description", ""),
            "yes" if skill.get("name", "") == active_skill else "",
        )
    console.print(table)


def render_skill_panel(console: Console, *, skill: dict[str, object], active: bool = False) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column(style=BRAND_COLORS["text"])
    table.add_row("Name", str(skill.get("name", "")))
    table.add_row("Source", str(skill.get("source", "")))
    table.add_row("Active", "yes" if active else "no")
    linked = skill.get("linked_files") or {}
    linked_count = sum(len(items) for items in linked.values()) if isinstance(linked, dict) else 0
    table.add_row("Linked files", str(linked_count))
    console.print(
        Panel(
            table,
            title=f"[bold {BRAND_COLORS['primary']}]Skill[/]",
            subtitle="[dim]active prompt overlay[/]",
            border_style=BRAND_COLORS["panel"],
            box=box.SQUARE,
        )
    )
    console.print()
    console.print(str(skill.get("content", "") or "").strip())
