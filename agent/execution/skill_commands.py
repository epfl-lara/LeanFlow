"""Shared slash command helpers for skills and built-in prompt-style modes.

Shared between CLI (cli.py) and gateway (gateway/run.py) so both surfaces
can invoke skills via /skill-name commands and prompt-only built-ins like
/plan.
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import tools.implementations.skills_tool as skills_tool_module
from leanflow_cli.runtime.skill_core import discover_skill_commands, find_skill, load_skill

logger = logging.getLogger(__name__)

_skill_commands: dict[str, dict[str, Any]] = {}
_PLAN_SLUG_RE = re.compile(r"[^a-z0-9]+")


def build_plan_path(
    user_instruction: str = "",
    *,
    now: datetime | None = None,
) -> Path:
    """Return the default workspace-relative markdown path for a /plan invocation.

    Relative paths are intentional: file tools are task/backend-aware and resolve
    them against the active working directory for local, docker, ssh, modal,
    daytona, and similar terminal backends. That keeps the plan with the active
    workspace instead of the LeanFlow host's global home directory.
    """
    slug_source = (user_instruction or "").strip().splitlines()[0] if user_instruction else ""
    slug = _PLAN_SLUG_RE.sub("-", slug_source.lower()).strip("-")
    if slug:
        slug = "-".join(part for part in slug.split("-")[:8] if part)[:48].strip("-")
    slug = slug or "conversation-plan"
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
    return Path(".leanflow") / "plans" / f"{timestamp}-{slug}.md"


def _load_skill_payload(
    skill_identifier: str, task_id: str | None = None
) -> tuple[dict[str, Any], Path | None, str] | None:
    """Load a skill by name/path and return (loaded_payload, skill_dir, display_name)."""
    raw_identifier = (skill_identifier or "").strip()
    if not raw_identifier:
        return None

    if _local_skill_override_active():
        payload = skills_tool_module._local_skill_payload(raw_identifier)
        if not payload:
            return None
        skill_file = (
            Path(str(payload.get("file") or "")).expanduser() if payload.get("file") else None
        )
        skill_dir = skill_file.parent if skill_file else None
        skill_name = str(payload.get("name") or raw_identifier)
        return payload, skill_dir, skill_name

    try:
        normalized = raw_identifier.lstrip("/")
        loaded_skill = load_skill(normalized)
    except Exception:
        return None

    if not loaded_skill:
        return None

    skill_name = str(loaded_skill.get("name") or normalized)
    record = find_skill(normalized)
    skill_dir = record.skill_dir if record else None

    return loaded_skill, skill_dir, skill_name


def _local_skill_override_active() -> bool:
    default_skills_dir = skills_tool_module.LEANFLOW_HOME_DIR / "skills"
    return default_skills_dir != skills_tool_module.SKILLS_DIR


def _discover_local_skill_commands() -> dict[str, dict[str, Any]]:
    commands: dict[str, dict[str, Any]] = {}
    for skill in skills_tool_module._find_all_skills():
        name = str(skill.get("name") or "").strip()
        if not name:
            continue
        command = "/" + name.lower().replace(" ", "-").replace("_", "-")
        description = str(skill.get("description") or "").strip()
        commands[command] = {
            "name": name,
            "description": description or f"Invoke the {name} skill",
            "source": "local",
        }
    return commands


def _build_skill_message(
    loaded_skill: dict[str, Any],
    skill_dir: Path | None,
    activation_note: str,
    user_instruction: str = "",
    runtime_note: str = "",
) -> str:
    """Format a loaded skill into a user/system message payload."""
    content = str(loaded_skill.get("content") or "")

    parts = [activation_note, "", content.strip()]

    if loaded_skill.get("setup_skipped"):
        parts.extend(
            [
                "",
                "[Skill setup note: Required environment setup was skipped. Continue loading the skill and explain any reduced functionality if it matters.]",
            ]
        )
    elif loaded_skill.get("gateway_setup_hint"):
        parts.extend(
            [
                "",
                f"[Skill setup note: {loaded_skill['gateway_setup_hint']}]",
            ]
        )
    elif loaded_skill.get("setup_needed") and loaded_skill.get("setup_note"):
        parts.extend(
            [
                "",
                f"[Skill setup note: {loaded_skill['setup_note']}]",
            ]
        )

    supporting = []
    linked_files = loaded_skill.get("linked_files") or {}
    for entries in linked_files.values():
        if isinstance(entries, list):
            supporting.extend(entries)

    if supporting:
        skill_view_target = str(loaded_skill.get("name") or "")
        parts.append("")
        parts.append("[This skill has supporting files you can load with the skill_view tool:]")
        for sf in supporting:
            parts.append(f"- {sf}")
        parts.append(
            f'\nTo view any of these, use: skill_view(name="{skill_view_target}", file_path="<path>")'
        )

    if user_instruction:
        parts.append("")
        parts.append(
            f"The user has provided the following instruction alongside the skill invocation: {user_instruction}"
        )

    if runtime_note:
        parts.append("")
        parts.append(f"[Runtime note: {runtime_note}]")

    return "\n".join(parts)


def scan_skill_commands() -> dict[str, dict[str, Any]]:
    """Return the current LeanFlow skill command map."""
    global _skill_commands
    if _local_skill_override_active():
        _skill_commands = _discover_local_skill_commands()
        return _skill_commands

    _skill_commands = dict(discover_skill_commands())
    try:
        for command, payload in list(_skill_commands.items()):
            payload.setdefault(
                "description", f"Invoke the {payload.get('name', command.lstrip('/'))} skill"
            )
    except Exception:
        logger.debug("Failed to scan LeanFlow skill commands", exc_info=True)
    return _skill_commands


def get_skill_commands() -> dict[str, dict[str, Any]]:
    """Return the current skill commands mapping (scan first if empty)."""
    if not _skill_commands:
        scan_skill_commands()
    return _skill_commands


def build_skill_invocation_message(
    cmd_key: str,
    user_instruction: str = "",
    task_id: str | None = None,
    runtime_note: str = "",
) -> str | None:
    """Build the user message content for a skill slash command invocation.

    Args:
        cmd_key: The command key including leading slash (e.g., "/gif-search").
        user_instruction: Optional text the user typed after the command.

    Returns:
        The formatted message string, or None if the skill wasn't found.
    """
    commands = get_skill_commands()
    skill_info = commands.get(cmd_key)
    if not skill_info:
        return None

    loaded = _load_skill_payload(str(skill_info["name"]), task_id=task_id)
    if not loaded:
        return f"[Failed to load skill: {skill_info['name']}]"

    loaded_skill, skill_dir, skill_name = loaded
    activation_note = (
        f'[SYSTEM: The user has invoked the "{skill_name}" skill, indicating they want '
        "you to follow its instructions. The full skill content is loaded below.]"
    )
    return _build_skill_message(
        loaded_skill,
        skill_dir,
        activation_note,
        user_instruction=user_instruction,
        runtime_note=runtime_note,
    )


def build_preloaded_skills_prompt(
    skill_identifiers: list[str],
    task_id: str | None = None,
) -> tuple[str, list[str], list[str]]:
    """Load one or more skills for session-wide CLI preloading.

    Returns (prompt_text, loaded_skill_names, missing_identifiers).
    """
    prompt_parts: list[str] = []
    loaded_names: list[str] = []
    missing: list[str] = []

    seen: set[str] = set()
    for raw_identifier in skill_identifiers:
        identifier = (raw_identifier or "").strip()
        if not identifier or identifier in seen:
            continue
        seen.add(identifier)

        loaded = _load_skill_payload(identifier, task_id=task_id)
        if not loaded:
            missing.append(identifier)
            continue

        loaded_skill, skill_dir, skill_name = loaded
        activation_note = (
            f'[SYSTEM: The user launched this CLI session with the "{skill_name}" skill '
            "preloaded. Treat its instructions as active guidance for the duration of this "
            "session unless the user overrides them.]"
        )
        prompt_parts.append(
            _build_skill_message(
                loaded_skill,
                skill_dir,
                activation_note,
            )
        )
        loaded_names.append(skill_name)

    return "\n\n".join(prompt_parts), loaded_names, missing
