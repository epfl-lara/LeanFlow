"""Heuristics for detecting potentially destructive terminal commands.

Extracted verbatim from ``run_agent.py`` (refactor Phase 4). The destructive-command
detector and its compiled patterns are pure: they depend only on stdlib ``re`` and carry
no dependency on ``AIAgent`` or any ``run_agent`` module state. They live here and are
re-exported from ``run_agent`` for backwards compatibility (``run_agent._is_destructive_command``
remains valid). This module does NOT import ``run_agent``, so the re-export adds no cycle.
"""

from __future__ import annotations

import re

__all__ = [
    "_DESTRUCTIVE_PATTERNS",
    "_REDIRECT_OVERWRITE",
    "_is_destructive_command",
]


# Patterns that indicate a terminal command may modify/delete files.
_DESTRUCTIVE_PATTERNS = re.compile(
    r"""(?:^|\s|&&|\|\||;|`)(?:
        rm\s|rmdir\s|
        mv\s|
        sed\s+-i|
        truncate\s|
        dd\s|
        shred\s|
        git\s+(?:reset|clean|checkout)\s
    )""",
    re.VERBOSE,
)


# Output redirects that overwrite files (> but not >>)
_REDIRECT_OVERWRITE = re.compile(r"[^>]>[^>]|^>[^>]")


def _is_destructive_command(cmd: str) -> bool:
    """Heuristic: does this terminal command look like it modifies/deletes files?"""
    if not cmd:
        return False
    if _DESTRUCTIVE_PATTERNS.search(cmd):
        return True
    if _REDIRECT_OVERWRITE.search(cmd):
        return True
    return False
