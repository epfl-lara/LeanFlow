"""Detect terminal commands that may perform destructive filesystem changes.

The detector is independent of ``AIAgent`` state and remains re-exported from
``run_agent`` for compatibility with existing integrations.
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
        git\s+(?:reset|clean|checkout|restore|switch|revert|merge|rebase|cherry-pick|apply|am)\s
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
