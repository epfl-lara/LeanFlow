"""Choose deterministic subprocess timeouts for canonical Lean commands."""

from __future__ import annotations

import os
from collections.abc import Sequence

DEFAULT_COMMAND_TIMEOUT_S = 120
RESEARCH_FILE_EXACT_TIMEOUT_FLOOR_S = 300
MIN_COMMAND_TIMEOUT_S = 1
MAX_COMMAND_TIMEOUT_S = 3600


def _env_enabled(name: str) -> bool:
    """Return whether one LeanFlow environment switch is enabled."""
    return str(os.getenv(name, "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _configured_timeout_s() -> int:
    """Return the bounded user-configured command timeout or its default."""
    raw = str(os.getenv("LEANFLOW_LEAN_COMMAND_TIMEOUT_S", "") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_COMMAND_TIMEOUT_S
    except ValueError:
        value = DEFAULT_COMMAND_TIMEOUT_S
    return min(MAX_COMMAND_TIMEOUT_S, max(MIN_COMMAND_TIMEOUT_S, value))


def _is_canonical_file_check(command: Sequence[str]) -> bool:
    """Return whether a command is the canonical ``lake env lean FILE`` gate."""
    normalized = [str(part or "").strip().lower() for part in command[:3]]
    return normalized == ["lake", "env", "lean"]


def effective_command_timeout_s(command: Sequence[str]) -> int:
    """Return a timeout that preserves the research cold-start file-check floor."""
    configured = _configured_timeout_s()
    if _is_canonical_file_check(command) and _env_enabled("LEANFLOW_RESEARCH_MODE"):
        return max(configured, RESEARCH_FILE_EXACT_TIMEOUT_FLOOR_S)
    return configured
