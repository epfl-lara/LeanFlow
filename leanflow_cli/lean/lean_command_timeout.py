"""Choose deterministic subprocess timeouts for canonical Lean commands."""

from __future__ import annotations

import os
from collections.abc import Sequence

DEFAULT_COMMAND_TIMEOUT_S = 120
RESEARCH_FILE_EXACT_TIMEOUT_FLOOR_S = 900
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


def _configured_hard_timeout_s() -> int | None:
    """Return the optional absolute subprocess cap for one managed run."""
    raw = str(os.getenv("LEANFLOW_LEAN_COMMAND_HARD_TIMEOUT_S", "") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return min(MAX_COMMAND_TIMEOUT_S, max(MIN_COMMAND_TIMEOUT_S, value))


def configured_hard_timeout_s() -> int | None:
    """Return the run-wide hard cap shared by canonical and incremental Lean checks."""
    return _configured_hard_timeout_s()


def _is_canonical_file_check(command: Sequence[str]) -> bool:
    """Return whether a command is the canonical ``lake env lean FILE`` gate."""
    normalized = [str(part or "").strip().lower() for part in command[:3]]
    return normalized == ["lake", "env", "lean"]


def effective_command_timeout_s(command: Sequence[str]) -> int:
    """Return the policy timeout, bounded by an optional per-run hard cap."""
    configured = _configured_timeout_s()
    if _is_canonical_file_check(command) and _env_enabled("LEANFLOW_RESEARCH_MODE"):
        configured = max(configured, RESEARCH_FILE_EXACT_TIMEOUT_FLOOR_S)
    hard_timeout = _configured_hard_timeout_s()
    return configured if hard_timeout is None else min(configured, hard_timeout)
