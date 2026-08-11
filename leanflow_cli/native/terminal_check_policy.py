"""Route managed theorem checks away from ad hoc terminal Lean processes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_DIRECT_LEAN_TERMINAL_RE = re.compile(
    r"(?ix)"
    r"(?:^|[;&|]\s*)"
    r"(?:env\s+[^;&|]*\s+)?"
    r"(?:[^\s;&|]*/)?"
    r"(?:"
    r"lake\s+(?:env\s+lean|build)\b"
    r"|lean\b"
    r")"
)


def incremental_check_route(
    function_name: str,
    args: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the incremental route for a direct managed terminal Lean check."""
    if function_name != "terminal":
        return None
    arguments = dict(args or {})
    command = str(arguments.get("command", "") or arguments.get("cmd", "") or "").strip()
    if not command or _DIRECT_LEAN_TERMINAL_RE.search(command) is None:
        return None
    return {
        "success": False,
        "status": "managed_incremental_check_required",
        "blocked_tool": "terminal",
        "blocked_command": command,
        "required_tool": "lean_incremental_check",
        "required_action": (
            "Use `lean_incremental_check(action=check_target)` for an assigned theorem, "
            "`action=check_helper` for an inline helper candidate, or `action=check_file` "
            "after changing a managed companion file."
        ),
        "error": (
            "Managed theorem queues keep LeanFlow's LeanProbe session warm and reuse its "
            "per-declaration cache. Direct `lean`, `lake env lean`, and `lake build` terminal "
            "checks bypass that cache and are reserved for manager-owned canonical gates."
        ),
    }
