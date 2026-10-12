"""``leanflow flags`` — inspect the declared runtime-knob catalog.

Rendering lives here rather than in ``main.py`` so the catalog surface stays one
cohesive leaf. Every subcommand supports ``--json`` because the VS Code extension
and the evaluation harness consume the same output the terminal renders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from leanflow_cli.flags import (
    catalog_payload,
    effective_flag_values,
    lookup_flag,
    profile_payload,
    resolve_profile,
)
from leanflow_cli.flags.resolve import diff_profiles, load_profiles, profile_launch_surfaces

_KIND_STYLES = {
    "feature": "bold cyan",
    "tuning": "yellow",
    "runtime": "green",
    "internal": "dim",
}


def _kind_markup(kind: str) -> str:
    """Render a knob kind, falling back to plain text for an unstyled kind.

    An empty style would emit ``[]…[/]``, which rich rejects as malformed
    markup — a display concern must not be able to abort the command.
    """
    style = _KIND_STYLES.get(kind, "")
    return f"[{style}]{kind}[/]" if style else kind


def register_flags_parser(subparsers: Any) -> None:
    """Attach the ``flags`` command tree to the top-level CLI parser."""
    flags_parser = subparsers.add_parser(
        "flags",
        help="Inspect the LEANFLOW_* runtime knob catalog and knob profiles",
    )
    flags_sub = flags_parser.add_subparsers(dest="flags_command")

    list_parser = flags_sub.add_parser("list", help="List catalogued knobs")
    list_parser.add_argument("--json", action="store_true", dest="json_output")
    list_parser.add_argument("--group", default="", help="Restrict to one group")
    list_parser.add_argument(
        "--kind",
        default="",
        choices=["", "feature", "tuning", "runtime", "internal"],
        help="Restrict to one knob kind",
    )
    list_parser.add_argument(
        "--ablatable",
        action="store_true",
        help="Only knobs worth flipping in an ablation",
    )

    show_parser = flags_sub.add_parser("show", help="Show one knob in detail")
    show_parser.add_argument("name")
    show_parser.add_argument("--json", action="store_true", dest="json_output")

    effective_parser = flags_sub.add_parser(
        "effective", help="Show each knob's effective value and where it came from"
    )
    effective_parser.add_argument("--json", action="store_true", dest="json_output")
    effective_parser.add_argument("--profile", default="", help="Resolve against this profile")
    effective_parser.add_argument(
        "--changed", action="store_true", help="Only knobs that differ from their default"
    )
    effective_parser.add_argument(
        "--include-internal", action="store_true", help="Include launcher plumbing"
    )

    profiles_parser = flags_sub.add_parser("profiles", help="List built-in and saved profiles")
    profiles_parser.add_argument("--json", action="store_true", dest="json_output")

    diff_parser = flags_sub.add_parser("diff", help="Diff two profiles")
    diff_parser.add_argument("left")
    diff_parser.add_argument("right")
    diff_parser.add_argument("--json", action="store_true", dest="json_output")


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _project_root() -> Path | None:
    from leanflow_cli.workflows.project import discover_leanflow_project

    try:
        return discover_leanflow_project(Path.cwd()).root
    except Exception:
        # Profiles are still useful outside a registered project; fall back to
        # the home-level search path rather than failing the command.
        return None


def _handle_list(args: argparse.Namespace) -> int:
    payload = catalog_payload()
    group_filter = str(getattr(args, "group", "") or "").strip().lower()
    kind_filter = str(getattr(args, "kind", "") or "").strip()
    ablatable_only = bool(getattr(args, "ablatable", False))

    groups = []
    for group in payload["groups"]:
        flags = [
            flag
            for flag in group["flags"]
            if (not kind_filter or flag["kind"] == kind_filter)
            and (not ablatable_only or flag["ablatable"])
        ]
        if not flags:
            continue
        if group_filter and group_filter not in group["name"].lower():
            continue
        groups.append({"name": group["name"], "flags": flags})

    if getattr(args, "json_output", False):
        _print_json({"version": 1, "count": sum(len(g["flags"]) for g in groups), "groups": groups})
        return 0

    console = Console()
    for group in groups:
        table = Table(title=group["name"], title_justify="left", header_style="bold")
        table.add_column("Knob", overflow="fold")
        table.add_column("Kind")
        table.add_column("Type")
        table.add_column("Default")
        table.add_column("Summary", overflow="fold")
        for flag in group["flags"]:
            name = flag["name"]
            if flag["ablatable"]:
                name = f"{name} ⚗"
            table.add_row(
                name,
                _kind_markup(flag["kind"]),
                flag["value_type"],
                flag["default"] or "—",
                flag["summary"],
            )
        console.print(table)
        console.print()
    console.print("[dim]⚗ marks knobs worth flipping in an ablation.[/dim]")
    return 0


def _handle_show(args: argparse.Namespace) -> int:
    spec = lookup_flag(args.name)
    if spec is None:
        print(f"Unknown knob: {args.name}", file=sys.stderr)
        print("Run `leanflow flags list` to see the catalog.", file=sys.stderr)
        return 1
    if getattr(args, "json_output", False):
        _print_json(spec.to_payload())
        return 0
    console = Console()
    console.print(f"[bold]{spec.name}[/bold]")
    console.print(f"  group     {spec.group}")
    console.print(f"  kind      {_kind_markup(spec.kind)}")
    console.print(f"  type      {spec.value_type}")
    console.print(f"  default   {spec.default or '—'}")
    if spec.choices:
        console.print(f"  choices   {', '.join(c or '—' for c in spec.choices)}")
    if spec.minimum is not None or spec.maximum is not None:
        low = spec.minimum if spec.minimum is not None else "—"
        high = spec.maximum if spec.maximum is not None else "—"
        console.print(f"  range     {low} .. {high}")
    console.print(f"  ablatable {'yes' if spec.ablatable else 'no'}")
    console.print(f"  read in   {', '.join(spec.read_in) or '—'}")
    console.print()
    console.print(spec.summary)
    current = os.environ.get(spec.name)
    if current is not None:
        console.print()
        console.print(f"[yellow]Currently set in this environment:[/yellow] {current}")
    return 0


def _handle_effective(args: argparse.Namespace) -> int:
    profile = None
    profile_name = str(getattr(args, "profile", "") or "").strip()
    if profile_name:
        try:
            profile = resolve_profile(profile_name, _project_root())
        except KeyError:
            print(f"Unknown profile: {profile_name}", file=sys.stderr)
            return 1
    rows = effective_flag_values(
        profile=profile,
        include_internal=bool(getattr(args, "include_internal", False)),
    )
    if profile is not None:
        surfaces = profile_launch_surfaces(profile)
        if surfaces["terminal_only_knobs"] or not surfaces["terminal"]["supported"]:
            # Say up front where this profile can and cannot launch, so the mismatch
            # is not discovered by a rejected launch later.
            print(_surface_note(profile.name, surfaces), file=sys.stderr)
    if getattr(args, "changed", False):
        rows = [row for row in rows if not row["is_default"]]

    if getattr(args, "json_output", False):
        _print_json(
            {
                "version": 1,
                "profile": profile.name if profile is not None else "",
                "count": len(rows),
                "flags": rows,
            }
        )
        return 0

    console = Console()
    table = Table(
        title=f"Effective knobs{f' · profile {profile.name}' if profile else ''}",
        title_justify="left",
        header_style="bold",
    )
    table.add_column("Knob", overflow="fold")
    table.add_column("Value")
    table.add_column("Source")
    table.add_column("Default")
    for row in rows:
        source_style = {"environment": "bold yellow", "profile": "cyan", "default": "dim"}[
            row["source"]
        ]
        table.add_row(
            row["name"],
            str(row["raw"]) or "—",
            f"[{source_style}]{row['source']}[/]",
            row["default"] or "—",
        )
    console.print(table)
    if not rows:
        console.print("[dim]No knobs differ from their declared defaults.[/dim]")
    return 0


def _surface_note(name: str, surfaces: dict[str, Any]) -> str:
    """Render one line saying where a profile can launch and which knobs block it."""
    if not surfaces["terminal"]["supported"]:
        return (
            f"Profile {name} cannot launch anywhere: unusable knobs "
            f"{', '.join(surfaces['terminal']['rejected'])}"
        )
    if surfaces["terminal_only_knobs"]:
        return (
            f"Profile {name} is terminal-only; the VS Code extension rejects "
            f"{', '.join(surfaces['terminal_only_knobs'])}"
        )
    return f"Profile {name} launches from the terminal and the VS Code extension"


def _surface_label(surfaces: dict[str, Any]) -> str:
    """Return a compact surface column value for the profile table."""
    if not surfaces["terminal"]["supported"]:
        return f"none ({', '.join(surfaces['terminal']['rejected'])})"
    if surfaces["terminal_only_knobs"]:
        return f"terminal only ({', '.join(surfaces['terminal_only_knobs'])})"
    return "terminal, extension"


def _handle_profiles(args: argparse.Namespace) -> int:
    root = _project_root()
    payload = profile_payload(root)
    if getattr(args, "json_output", False):
        _print_json(payload)
        return 0
    console = Console()
    table = Table(title="Knob profiles", title_justify="left", header_style="bold")
    table.add_column("Name")
    table.add_column("Origin")
    table.add_column("Overrides", justify="right")
    table.add_column("Surfaces", overflow="fold")
    table.add_column("Summary", overflow="fold")
    for profile in payload["profiles"]:
        table.add_row(
            profile["name"],
            "built-in" if profile["builtin"] else "saved",
            str(len(profile["overrides"])),
            _surface_label(profile["launch_surfaces"]),
            profile["summary"],
        )
    console.print(table)
    console.print()
    console.print("[dim]Saved profiles are read from:[/dim]")
    for path in payload["search_paths"]:
        console.print(f"[dim]  {path}[/dim]")
    return 0


def _handle_diff(args: argparse.Namespace) -> int:
    root = _project_root()
    profiles = load_profiles(root)
    missing = [name for name in (args.left, args.right) if name not in profiles]
    if missing:
        print(f"Unknown profile(s): {', '.join(missing)}", file=sys.stderr)
        return 1
    rows = diff_profiles(profiles[args.left], profiles[args.right])
    if getattr(args, "json_output", False):
        _print_json(
            {"version": 1, "left": args.left, "right": args.right, "count": len(rows), "diff": rows}
        )
        return 0
    console = Console()
    if not rows:
        console.print(f"[green]{args.left} and {args.right} resolve to the same knobs.[/green]")
        return 0
    table = Table(title=f"{args.left} → {args.right}", title_justify="left", header_style="bold")
    table.add_column("Knob", overflow="fold")
    table.add_column(args.left)
    table.add_column(args.right)
    table.add_column("Group")
    for row in rows:
        name = row["name"] if row["known"] else f"{row['name']} [red](unknown)[/red]"
        table.add_row(name, row["left"] or "—", row["right"] or "—", row["group"])
    console.print(table)
    return 0


def handle_flags(args: argparse.Namespace) -> int:
    """Dispatch one ``leanflow flags`` invocation."""
    command = str(getattr(args, "flags_command", "") or "list")
    handlers = {
        "list": _handle_list,
        "show": _handle_show,
        "effective": _handle_effective,
        "profiles": _handle_profiles,
        "diff": _handle_diff,
    }
    handler = handlers.get(command)
    if handler is None:
        print(f"Unknown flags command: {command}", file=sys.stderr)
        return 1
    return handler(args)
