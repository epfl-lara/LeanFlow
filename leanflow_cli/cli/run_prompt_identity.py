"""Hash persistent inputs that shape the managed agent system prompt.

This leaf binds the exact sanitized/truncated project-context prompt plus the
enabled bounded MEMORY.md and USER.md snapshots. It publishes digests and safe
status metadata only; prompt text never enters durable run evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.home import leanflow_home

_MEMORY_ENTRY_DELIMITER = "\n§\n"


def _sha256_text(value: str) -> str:
    """Return a stable digest for one exact prompt string."""
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _context_input_paths(project_root: Path, home: Path) -> tuple[list[Path], list[str]]:
    """Return context files the prompt builder can read and audit discovery errors."""
    paths: list[Path] = []
    issues: list[str] = []
    try:
        root = project_root.expanduser().resolve(strict=True)
        if not root.is_dir():
            raise OSError("project root is not a directory")
    except OSError:
        return [], ["project-root-unavailable"]

    top_level_agents = next(
        (root / name for name in ("AGENTS.md", "agents.md") if (root / name).exists()),
        None,
    )
    if top_level_agents is not None:
        walk_errors: list[OSError] = []
        try:
            for current_root, dirs, files in os.walk(root, onerror=walk_errors.append):
                dirs[:] = [
                    name
                    for name in dirs
                    if not name.startswith(".")
                    and name not in ("node_modules", "__pycache__", "venv", ".venv")
                ]
                paths.extend(
                    Path(current_root) / name for name in files if name.lower() == "agents.md"
                )
        except OSError:
            issues.append("agents-discovery-unavailable")
        if walk_errors:
            issues.append("agents-discovery-incomplete")

    cursorrules = root / ".cursorrules"
    if cursorrules.exists():
        paths.append(cursorrules)
    cursor_rules = root / ".cursor" / "rules"
    if cursor_rules.exists() and cursor_rules.is_dir():
        try:
            paths.extend(sorted(cursor_rules.glob("*.mdc")))
        except OSError:
            issues.append("cursor-rules-discovery-unavailable")

    soul = home / "SOUL.md"
    if soul.exists():
        paths.append(soul)
    for index, path in enumerate(paths):
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            issues.append(f"context-input-{index}-unreadable")
    return paths, issues


def _context_prompt_identity(project_root: Path, home: Path) -> dict[str, Any]:
    """Hash the exact sanitized/truncated context-files prompt."""
    from agent.prompting.prompt_builder import build_context_files_prompt

    prompt = ""
    build_complete = True
    try:
        prompt = build_context_files_prompt(cwd=str(project_root))
    except Exception:
        build_complete = False
    paths, issues = _context_input_paths(project_root, home)
    complete = build_complete and not issues
    all_issues = issues + ([] if build_complete else ["context-prompt-build-failed"])
    return {
        "status": "complete" if complete else "incomplete",
        "sha256": _sha256_text(prompt),
        "chars": len(prompt),
        "input_file_count": len(paths),
        "complete": complete,
        "issue_count": len(all_issues),
        "issues_sha256": _sha256_text("\n".join(all_issues)),
    }


def _memory_snapshot_identity(
    *,
    path: Path,
    target: str,
    enabled: bool,
    raw_limit: Any,
) -> dict[str, Any]:
    """Hash the exact frozen MemoryStore block without persisting its content."""
    if not enabled:
        return {
            "enabled": False,
            "status": "disabled",
            "sha256": _sha256_text(""),
            "chars": 0,
            "entry_count": 0,
            "configured_char_limit": None,
            "complete": True,
        }
    try:
        limit = int(raw_limit)
        if limit < 0:
            raise ValueError("negative memory limit")
    except (TypeError, ValueError):
        return {
            "enabled": True,
            "status": "invalid-limit",
            "sha256": _sha256_text(""),
            "chars": 0,
            "entry_count": 0,
            "configured_char_limit": None,
            "complete": False,
        }
    if not path.exists():
        return {
            "enabled": True,
            "status": "missing-empty",
            "sha256": _sha256_text(""),
            "chars": 0,
            "entry_count": 0,
            "configured_char_limit": limit,
            "complete": True,
        }
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {
            "enabled": True,
            "status": "unreadable",
            "sha256": _sha256_text(""),
            "chars": 0,
            "entry_count": 0,
            "configured_char_limit": limit,
            "complete": False,
        }
    entries = [entry.strip() for entry in raw.split(_MEMORY_ENTRY_DELIMITER)]
    entries = list(dict.fromkeys(entry for entry in entries if entry))
    content = _MEMORY_ENTRY_DELIMITER.join(entries)
    if not content:
        block = ""
    else:
        percent = int((len(content) / limit) * 100) if limit > 0 else 0
        if target == "user":
            header = (
                f"USER PROFILE (who the user is) "
                f"[{percent}% — {len(content):,}/{limit:,} chars]"
            )
        else:
            header = f"MEMORY (your personal notes) [{percent}% — {len(content):,}/{limit:,} chars]"
        separator = "═" * 46
        block = f"{separator}\n{header}\n{separator}\n{content}"
    return {
        "enabled": True,
        "status": "complete" if block else "empty",
        "sha256": _sha256_text(block),
        "chars": len(block),
        "entry_count": len(entries),
        "configured_char_limit": limit,
        "complete": True,
    }


def persistent_prompt_identity(project_root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Return digest-only identities for every persistent system-prompt layer."""
    home = leanflow_home().expanduser()
    memory_config = config.get("memory")
    memory_config = memory_config if isinstance(memory_config, Mapping) else {}
    context = _context_prompt_identity(project_root, home)
    memory = _memory_snapshot_identity(
        path=home / "memories" / "MEMORY.md",
        target="memory",
        enabled=bool(memory_config.get("memory_enabled", False)),
        raw_limit=memory_config.get("memory_char_limit", 2200),
    )
    user = _memory_snapshot_identity(
        path=home / "memories" / "USER.md",
        target="user",
        enabled=bool(memory_config.get("user_profile_enabled", False)),
        raw_limit=memory_config.get("user_char_limit", 1375),
    )
    inputs = {"context_files": context, "memory": memory, "user": user}
    encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "persistent_prompt_inputs": inputs,
        "persistent_prompt_inputs_sha256": hashlib.sha256(encoded).hexdigest(),
        "persistent_prompt_inputs_complete": all(
            item.get("complete") is True for item in inputs.values()
        ),
    }
