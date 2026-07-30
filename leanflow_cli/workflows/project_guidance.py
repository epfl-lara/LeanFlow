"""Attach explicit project-owned proof guidance to matching managed turns."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

_MAX_GUIDANCE_FILES = 4
_MAX_GUIDANCE_FILE_CHARS = 12_000
_MAX_GUIDANCE_TOTAL_CHARS = 24_000


def _string_list(value: Any) -> tuple[str, ...]:
    """Return nonempty strings from one manifest list."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item or "").strip() for item in value if str(item or "").strip())


def _active_file_relative(project_root: Path, active_file: str) -> str:
    """Return a normalized project-relative active file when it is in scope."""
    try:
        return str(Path(active_file).expanduser().resolve(strict=False).relative_to(project_root))
    except (OSError, ValueError):
        return ""


def _entry_matches(
    entry: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file_relative: str,
) -> bool:
    """Return whether one guidance entry applies to the current assignment."""
    targets = _string_list(entry.get("targets"))
    active_files = _string_list(entry.get("active_files"))
    if targets and target_symbol not in targets:
        return False
    if active_files and active_file_relative not in active_files:
        return False
    return True


def _history_text(conversation_history: Sequence[Mapping[str, Any]]) -> str:
    """Return bounded text used only to detect already-delivered markers."""
    return "\n".join(str(message.get("content", "") or "") for message in conversation_history)[
        -200_000:
    ]


def attach_project_guidance(
    *,
    project_root: str,
    target_symbol: str,
    active_file: str,
    user_message: str,
    conversation_history: Sequence[Mapping[str, Any]],
) -> str:
    """Append matching manifest guidance unless its content hash is in history.

    Projects opt in through top-level ``workflow_guidance`` entries in
    ``.leanflow/project.yaml``. Each entry names a project-relative Markdown
    file and can restrict delivery with ``targets`` and ``active_files``.
    Invalid, escaping, missing, or oversized entries are ignored so guidance
    remains advisory and can never block the proof manager.
    """
    root = Path(project_root).expanduser().resolve(strict=False)
    manifest_path = root / ".leanflow" / "project.yaml"
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return user_message
    raw_entries = payload.get("workflow_guidance", []) if isinstance(payload, Mapping) else []
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
        return user_message

    active_relative = _active_file_relative(root, active_file)
    delivered_text = f"{_history_text(conversation_history)}\n{user_message}"
    blocks: list[str] = []
    total_chars = 0
    for raw_entry in list(raw_entries)[:_MAX_GUIDANCE_FILES]:
        if not isinstance(raw_entry, Mapping) or not _entry_matches(
            raw_entry,
            target_symbol=target_symbol,
            active_file_relative=active_relative,
        ):
            continue
        relative_text = str(raw_entry.get("path", "") or "").strip()
        if not relative_text:
            continue
        try:
            candidate = (root / relative_text).resolve(strict=True)
            candidate.relative_to(root)
            if not candidate.is_file():
                continue
            content = candidate.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError, ValueError):
            continue
        if not content or len(content) > _MAX_GUIDANCE_FILE_CHARS:
            continue
        source = str(candidate.relative_to(root))
        digest = hashlib.sha256(f"{source}\0{content}".encode()).hexdigest()[:16]
        marker = f"[LEANFLOW PROJECT GUIDANCE sha256={digest} source={source}]"
        if marker in delivered_text:
            continue
        block = (
            f"{marker}\n"
            "This project-owned handoff is scoped to the current assignment. "
            "Use its strongest concrete route and preserve corrections, dead branches, "
            "and kernel-verified progress in durable workflow artifacts.\n\n"
            f"{content}"
        )
        if total_chars + len(block) > _MAX_GUIDANCE_TOTAL_CHARS:
            continue
        blocks.append(block)
        total_chars += len(block)
    return "\n\n".join([user_message.strip(), *blocks]).strip()
