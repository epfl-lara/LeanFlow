"""Render prompt and toolbar state for the LeanFlow interactive shell."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from prompt_toolkit.formatted_text import FormattedText


def toolbar_piece(value: str, max_len: int = 22) -> str:
    text = str(value or "-")
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def prompt_focus_label(workflow_status: Mapping[str, Any], target_label: str) -> str:
    if target_label != "-":
        return target_label
    file_label = str(workflow_status.get("active_file_label", "") or "").strip()
    if file_label not in {"", "-", "[unknown]", "[launching]"}:
        return Path(file_label).name
    build = str(workflow_status.get("build_status", "") or "").strip()
    if build not in {"", "-", "unknown", "workflow launching"}:
        return build
    return "-"


def prompt_message(project_name: str, phase_label: str, focus_label: str) -> FormattedText:
    project = toolbar_piece(project_name, 28)
    phase = toolbar_piece(phase_label, 18)
    theorem = focus_label
    status_suffix = phase
    if theorem and theorem != "-":
        status_suffix = f"{status_suffix} · {toolbar_piece(theorem, 28)}"
    return FormattedText(
        [
            ("fg:#ff5a5f bold", project),
            ("fg:#b0b0b0", "  "),
            ("fg:#d9d9d9", status_suffix),
            ("", "\n"),
            ("fg:#f5f5f5 bold", "› "),
        ]
    )


def bottom_toolbar(
    workflow_status: Mapping[str, Any],
    model_label: str,
    target_label: str,
    active_skill_label: str,
    latest_activity: str,
) -> str:
    phase = str(workflow_status.get("phase", "idle") or "idle")
    file_label = str(workflow_status.get("active_file_label", "") or "-")
    build = str(workflow_status.get("build_status", "") or "-")
    model = toolbar_piece(model_label, 22)
    file_short = toolbar_piece(Path(file_label).name if file_label != "-" else "-", 18)
    theorem = toolbar_piece(target_label, 16)
    skill = toolbar_piece(active_skill_label, 18)
    latest = latest_activity
    return f" @ {model} | {phase} | {build} | {file_short} | {theorem} | {skill} | {latest} "
