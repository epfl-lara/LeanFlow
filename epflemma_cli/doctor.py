"""Local setup checks for the EPFLemma shell."""

from __future__ import annotations

import shutil
from pathlib import Path

from epflemma_cli.branding import get_cli_command_name, get_product_name
from epflemma_cli.config import ensure_epflemma_home, get_epflemma_home, load_config
from epflemma_cli.project import ProjectNotFoundError, discover_epflemma_project
from epflemma_cli.runtime_provider import format_runtime_provider_error, resolve_runtime_provider


def run_doctor(active_cwd: str | Path | None = None) -> tuple[list[str], str]:
    ensure_epflemma_home()
    cwd = Path(active_cwd or Path.cwd()).expanduser().resolve()
    issues: list[str] = []
    cli_name = get_cli_command_name()
    lines = [
        f"{get_product_name()} Doctor",
        f"Home: {get_epflemma_home()}",
        f"Config: {get_epflemma_home() / 'config.yaml'}",
    ]

    for binary in ("git", "rg", "lake"):
        if shutil.which(binary):
            lines.append(f"{binary}: OK")
        else:
            lines.append(f"{binary}: missing")
            issues.append(f"Install `{binary}`.")

    config = load_config()
    lines.append(f"Default model: {config.get('model', {}).get('default', '')}")

    try:
        project = discover_epflemma_project(cwd)
        lines.append(f"Project: {project.label} ({project.root})")
    except ProjectNotFoundError:
        lines.append("Project: none")
        issues.append(f"Initialize a Lean project with `{cli_name} project init`.")

    try:
        runtime = resolve_runtime_provider()
        lines.append(f"Provider: {runtime['provider']} @ {runtime['base_url']}")
    except Exception as exc:
        lines.append(f"Provider: unavailable ({format_runtime_provider_error(exc)})")
        issues.append(format_runtime_provider_error(exc))

    return issues, "\n".join(lines)
