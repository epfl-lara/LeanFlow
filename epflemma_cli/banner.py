"""EPFLemma welcome banner and shell help rendering."""

from __future__ import annotations

import shutil
from pathlib import Path

from rich import box
from rich.console import Console
from rich.console import Group
from rich.panel import Panel
from rich.table import Table

from epflemma_cli import __version__
from epflemma_cli.branding import (
    BRAND_COLORS,
    get_cli_command_name,
    get_product_name,
    get_product_subtitle,
    get_product_tagline,
)
from epflemma_cli.commands import COMMANDS_BY_CATEGORY


EPFL_EMMA_WORDMARK = "\n".join(
    [
        f"[bold {BRAND_COLORS['primary_soft']}]███████╗██████╗ ███████╗██╗     ███████╗███╗   ███╗███╗   ███╗ █████╗[/]",
        f"[bold {BRAND_COLORS['primary_soft']}]██╔════╝██╔══██╗██╔════╝██║     ██╔════╝████╗ ████║████╗ ████║██╔══██╗[/]",
        f"[bold {BRAND_COLORS['primary']     }]╚═███╗  ██████╔╝╚═███╗  ██║     █████╗  ██╔████╔██║██╔████╔██║███████║[/]",
        f"[bold {BRAND_COLORS['primary']}     ]██╔══╝  ██╔═══╝ ██╔══╝  ██║     ██╔══╝  ██║╚██╔╝██║██║╚██╔╝██║██╔══██║[/]",
        f"[bold {BRAND_COLORS['primary_dim'] }]███████╗██║     ██║     ███████╗███████╗██║ ╚═╝ ██║██║ ╚═╝ ██║██║  ██║[/]",
        f"[bold {BRAND_COLORS['primary_dim']  }]╚══════╝╚═╝     ╚═╝     ╚══════╝╚══════╝╚═╝     ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝[/]",
        f"[{BRAND_COLORS['muted']}]{get_product_tagline()}[/]",
    ]
)

MISSION_LINE = "Dedicated to verified Lean proving and autoformalization"


def _shorten_middle(text: str, max_len: int) -> str:
    if max_len <= 0 or len(text) <= max_len:
        return text
    if max_len <= 7:
        return text[:max_len]
    head = max(2, (max_len - 3) // 2)
    tail = max(2, max_len - head - 3)
    return f"{text[:head]}...{text[-tail:]}"


def _build_metric_strip(*, project_label: str, model: str, provider: str, local_runtime: str) -> Table:
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
    cli_name = get_cli_command_name()
    product_name = get_product_name()
    width = shutil.get_terminal_size().columns
    simplified = width < 90

    right = Table.grid(padding=(0, 1))
    right.add_column()
    if simplified:
        right.add_row(f"[bold {BRAND_COLORS['primary']}]Start Here[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/project init[/]  [dim]register this Lean repo[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/autoprove Main.lean[/]  [dim]autonomous proving loop[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/autoformalize \"statement\"[/]  [dim]autonomous formalization[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/project[/]  [dim]current Lean workspace[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/help[/]  [dim]all commands[/]")
    else:
        right.add_row(f"[bold {BRAND_COLORS['primary']}]Launch Paths[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/project init[/]  [dim]register an existing Lean 4 repo[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/autoprove Main.lean[/]  [dim]autonomous proving loop[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/autoprove Main.lean --agents 3[/]  [dim]user-approved Lean swarm[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/autoformalize \"statement\"[/]  [dim]autonomous formalization[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/project[/]  [dim]show current Lean workspace[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/status[/]  [dim]live project and runner state[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/workflow activity[/]  [dim]recent managed steps[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/workflow log 120[/]  [dim]saved managed runner log[/]")
        right.add_row(f"[{BRAND_COLORS['text']}]/help[/]  [dim]command catalog and tips[/]")

    launch_panel = Panel(
        right,
        title=f"[bold {BRAND_COLORS['primary_soft']}]Workflow[/]",
        border_style=BRAND_COLORS["panel"],
        box=box.SQUARE,
        padding=(0, 1),
    )

    console.print()
    console.print(EPFL_EMMA_WORDMARK, justify="center")
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
        f"[{BRAND_COLORS['muted']}]Try /project, /help, or autoprove Main.lean[/]",
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
    console.print("[dim]Tip: workflow commands also accept forgiving forms like `autoprove Main.lean` without the leading slash.[/]")
    console.print("[dim]Tip: add `--agents N` to `autoprove` or `autoformalize` only when you explicitly want user-approved swarm mode.[/]")
    console.print("[dim]Tip: use `/provider local`, `/provider zai`, or `/provider custom` to inspect how a request will resolve before launching a workflow.[/]")
    console.print("[dim]Tip: use `/workflow activity` for structured managed steps and `/workflow log 120` for the full saved runner log.[/]")


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
    console.print(Panel(table, title=f"[bold {BRAND_COLORS['primary']}]{get_product_name()} Status[/]", subtitle="[dim]shell context[/]", border_style=BRAND_COLORS["panel"], box=box.SQUARE))


def render_provider_panel(console: Console, *, resolved: dict[str, str], requested: str, targets: list[dict[str, str]]) -> None:
    current = Table.grid(padding=(0, 1))
    current.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    current.add_column(style=BRAND_COLORS["text"])
    current.add_row("Requested", requested or "auto")
    current.add_row("Resolved provider", str(resolved.get("provider", "")))
    current.add_row("API mode", str(resolved.get("api_mode", "")))
    current.add_row("Base URL", str(resolved.get("base_url", "")))
    current.add_row("Model", str(resolved.get("model", "")))
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

    console.print(Panel(current, title=f"[bold {BRAND_COLORS['primary']}]Resolved Provider[/]", subtitle="[dim]current route[/]", border_style=BRAND_COLORS["panel"], box=box.SQUARE))
    console.print()
    console.print(f"[bold {BRAND_COLORS['primary']}]Available Targets[/]")
    console.print(table)


def render_project_panel(console: Console, *, project_summary: dict[str, str]) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column(style=BRAND_COLORS["text"])
    for key, value in project_summary.items():
        table.add_row(key, value)
    console.print(Panel(table, title=f"[bold {BRAND_COLORS['primary']}]{get_product_name()} Project[/]", subtitle="[dim]registered Lean workspace[/]", border_style=BRAND_COLORS["panel"], box=box.SQUARE))


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
    table.add_row("Skill", launch_summary.get("skill", ""))
    table.add_row("Agents", launch_summary.get("agents", "1"))
    table.add_row("Base URL", launch_summary.get("base_url", ""))
    console.print(Panel(table, title=f"[bold {BRAND_COLORS['primary']}]Launching Workflow[/]", subtitle="[dim]managed Lean execution plan[/]", border_style=BRAND_COLORS["panel"], box=box.SQUARE))


def render_workflow_status_panel(console: Console, *, status: dict[str, object], activities: list[dict[str, object]] | None = None) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_column(style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
    table.add_column(style=BRAND_COLORS["text"])
    table.add_row("Phase", str(status.get("phase", "[none]")))
    table.add_row("Workflow", str(status.get("workflow_kind", "[none]")))
    table.add_row("Command", str(status.get("workflow_command", "[none]")))
    table.add_row("Project", str(status.get("project_root", "[none]")))
    table.add_row("Provider", str(status.get("provider", "[none]")))
    table.add_row("Model", str(status.get("model", "[none]")))
    table.add_row("Skill", str(status.get("active_skill", "(none)")))
    table.add_row("Agents", str(status.get("parallel_agents", "1")))
    table.add_row("File", str(status.get("active_file_label", "[unknown]")))
    table.add_row("Theorem", str(status.get("target_symbol", "[unknown]")))
    table.add_row("Build", str(status.get("build_status", "unknown")))
    table.add_row("Project sorries", str(status.get("project_sorry_count", "[unknown]")))
    table.add_row("Checkpoint", str(status.get("latest_checkpoint_label", "[none]")))
    table.add_row("Locks", str(status.get("held_locks", 0)))
    table.add_row("Updated", str(status.get("updated_at", "[unknown]")))
    console.print(Panel(table, title=f"[bold {BRAND_COLORS['primary']}]Managed Workflow Status[/]", subtitle="[dim]live Lean runner state[/]", border_style=BRAND_COLORS["panel"], box=box.SQUARE))

    if activities:
        console.print()
        console.print(f"[bold {BRAND_COLORS['primary']}]Recent Activity[/]")
        activity_table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
        activity_table.add_column("Time", style=f"bold {BRAND_COLORS['primary_soft']}", no_wrap=True)
        activity_table.add_column("Type", style=BRAND_COLORS["primary_dim"], no_wrap=True)
        activity_table.add_column("Context", style=BRAND_COLORS["muted"], no_wrap=True)
        activity_table.add_column("Message", style=BRAND_COLORS["text"])
        for event in activities:
            timestamp = str(event.get("timestamp", "") or "")
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            context = (
                str(details.get("tool", "") or details.get("phase", "") or details.get("iteration", "") or "")
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


def render_skill_table(console: Console, *, skills: list[dict[str, str]], active_skill: str = "") -> None:
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
    console.print(Panel(table, title=f"[bold {BRAND_COLORS['primary']}]Skill[/]", subtitle="[dim]active prompt overlay[/]", border_style=BRAND_COLORS["panel"], box=box.SQUARE))
    console.print()
    console.print(str(skill.get("content", "") or "").strip())
