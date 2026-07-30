"""Resolve active Lean targets and count project placeholders."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _extract_target_symbol, _strip_lean_comments_and_strings
from leanflow_cli.native.native_config import _project_root, _read_native_env
from leanflow_cli.native.native_utils import _collect_message_text

PROJECT_SCAN_SKIP_DIRS = {
    ".artifacts",
    ".git",
    ".lake",
    ".leanflow",
    "build",
}


def _extract_active_files(text: str) -> list[str]:
    seen: list[str] = []
    for match in re.findall(r"[\w./-]+\.lean\b", text or ""):
        normalized = match.strip()
        if normalized.startswith("a//") or normalized.startswith("b//"):
            normalized = normalized[2:]
        project_root = _project_root()
        try:
            path = Path(normalized).expanduser()
            if not path.is_absolute() and project_root:
                candidate = (Path(project_root) / path).resolve()
                if candidate.is_file():
                    normalized = str(candidate.relative_to(Path(project_root).resolve()))
            elif path.is_absolute() and path.is_file() and project_root:
                try:
                    normalized = str(path.resolve().relative_to(Path(project_root).resolve()))
                except Exception:
                    normalized = str(path.resolve())
        except Exception:
            normalized = match.strip()
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen[:8]


def _checkpoint_active_files(live_state: Mapping[str, Any] | None, text: str) -> list[str]:
    """Return real checkpoint files, preferring the structured active file.

    Conversation history can contain unified-diff headers such as
    ``a/Project/Main.lean`` or truncated fragments. Keep extra history files
    only when they exist under the project root so those artifacts cannot
    poison a resume handoff.
    """
    state = dict(live_state or {})
    structured = _extract_active_files(
        "\n".join(str(state.get(key, "") or "") for key in ("active_file_label", "active_file"))
    )
    candidates = _extract_active_files(text)
    if not structured:
        return candidates
    root = Path(_project_root()).expanduser().resolve()
    result = list(structured)
    for candidate in candidates:
        path = Path(candidate).expanduser()
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        if not resolved.is_file():
            continue
        try:
            normalized = str(resolved.relative_to(root))
        except ValueError:
            normalized = str(resolved)
        if normalized not in result:
            result.append(normalized)
    return result[:8]


def _resolve_active_file(
    history: list[dict[str, Any]], checkpoint_state: Mapping[str, Any] | None = None
) -> str:
    configured_active_file = _read_native_env("ACTIVE_FILE")
    if configured_active_file:
        configured_path = Path(configured_active_file)
        if configured_path.is_file():
            return str(configured_path.resolve())
        candidate = Path(_project_root()) / configured_active_file
        if candidate.is_file():
            return str(candidate.resolve())

    workflow_command = _read_native_env("WORKFLOW_COMMAND")
    command_files = _extract_active_files(workflow_command)
    if command_files:
        candidate = Path(_project_root()) / command_files[0]
        if candidate.is_file():
            return str(candidate)

    current = (checkpoint_state or {}).get("current") or {}
    for file_name in current.get("active_files") or []:
        candidate = Path(_project_root()) / str(file_name)
        if candidate.is_file():
            return str(candidate)

    recent_text = _collect_message_text(history[-16:])
    for file_name in _extract_active_files(recent_text):
        direct = Path(file_name)
        if direct.is_file():
            return str(direct)
        candidate = Path(_project_root()) / file_name
        if candidate.is_file():
            return str(candidate)

    return ""


def _resolve_target_symbol(
    history: list[dict[str, Any]], checkpoint_state: Mapping[str, Any] | None = None
) -> str:
    current = (checkpoint_state or {}).get("current") or {}
    target = str(current.get("target_symbol", "") or "").strip()
    if target:
        return target
    return _extract_target_symbol(_read_native_env("WORKFLOW_COMMAND"))


def _find_symbol_line(active_file: str, target_symbol: str) -> int | None:
    if not active_file or not target_symbol:
        return None
    try:
        lines = Path(active_file).read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    pattern = re.compile(rf"\b(?:theorem|lemma|def)\s+{re.escape(target_symbol)}\b")
    for idx, line in enumerate(lines, start=1):
        if pattern.search(line):
            return idx
    return None


def _count_sorries(active_file: str) -> int | None:
    if not active_file:
        return None
    try:
        text = Path(active_file).read_text(encoding="utf-8")
    except Exception:
        return None
    sanitized = _strip_lean_comments_and_strings(text)
    return len(re.findall(r"\bsorry\b", sanitized))


def _project_lean_files(project_root: str) -> list[Path]:
    root = Path(project_root)
    if not root.is_dir():
        return []
    paths: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in PROJECT_SCAN_SKIP_DIRS]
            base = Path(dirpath)
            for filename in filenames:
                if filename.endswith(".lean"):
                    paths.append(base / filename)
    except OSError:
        return []
    return sorted(paths)


def _count_project_sorries(project_root: str) -> tuple[int | None, list[str]]:
    if not project_root:
        return None, []
    total = 0
    files: list[str] = []
    try:
        for path in _project_lean_files(project_root):
            count = _count_sorries(str(path))
            if not isinstance(count, int) or count <= 0:
                continue
            total += count
            try:
                label = str(path.resolve().relative_to(Path(project_root).resolve()))
            except Exception:
                label = str(path)
            files.append(f"{label} ({count})")
    except Exception:
        return None, []
    return total, files[:8]
