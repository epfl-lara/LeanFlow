"""Reject redundant exact-target checks of unchanged placeholder source."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _strip_lean_comments_and_strings
from leanflow_cli.proof_state_builder import _find_declaration_entry


@dataclass(frozen=True)
class SourcePlaceholderBlock:
    """Describe one exact assigned target whose source still has placeholders."""

    target_symbol: str
    active_file: str
    placeholders: tuple[str, ...]

    def to_tool_result(self) -> dict[str, Any]:
        """Return deterministic feedback for the skipped Lean invocation."""
        names = ", ".join(f"`{name}`" for name in self.placeholders)
        return {
            "success": False,
            "ok": False,
            "status": "source_placeholder_check_skipped",
            "blocked_by": "unchanged_assigned_source_placeholder",
            "action": "check_target",
            "target": self.target_symbol,
            "file": self.active_file,
            "source_placeholders": list(self.placeholders),
            "lean_started": False,
            "message": (
                f"The exact assigned source declaration `{self.target_symbol}` still contains "
                f"{names}. LeanFlow already knows this unchanged target is unresolved, so it "
                "did not start Lean for a redundant exact-target check. Edit the assigned target "
                "to remove its source placeholder, or submit a complete `replacement` candidate, "
                "then call `check_target`."
            ),
        }


def _canonical_file(value: str, project_root: str) -> str:
    """Return a stable absolute file identity without requiring the path to exist."""
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path(project_root).expanduser() / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def block_unchanged_target_check(
    function_name: str,
    arguments: Mapping[str, Any] | None,
    assignment: Mapping[str, Any] | None,
    *,
    project_root: str,
) -> SourcePlaceholderBlock | None:
    """Return a block for a no-replacement check of placeholder-bearing source."""
    if str(function_name or "").strip() != "lean_incremental_check":
        return None
    args = dict(arguments or {})
    action = str(args.get("action", "") or "check_target").strip().casefold().replace("-", "_")
    if action != "check_target" or str(args.get("replacement", "") or "").strip():
        return None
    current = dict(assignment or {})
    target_symbol = str(current.get("target_symbol", "") or "").strip()
    active_file = _canonical_file(str(current.get("active_file", "") or ""), project_root)
    if not target_symbol or not active_file:
        return None
    requested_target = str(
        args.get("theorem_id", "") or args.get("target_symbol", "") or ""
    ).strip()
    if requested_target and requested_target != target_symbol:
        return None
    requested_file = str(args.get("file_path", "") or args.get("active_file", "") or "").strip()
    if requested_file and _canonical_file(requested_file, project_root) != active_file:
        return None
    entry = _find_declaration_entry(active_file, target_symbol)
    if not entry:
        return None
    declaration = _strip_lean_comments_and_strings(str(entry.get("text", "") or ""))
    placeholders = tuple(
        name for name in ("sorry", "admit") if re.search(rf"\b{re.escape(name)}\b", declaration)
    )
    if not placeholders:
        return None
    return SourcePlaceholderBlock(target_symbol, active_file, placeholders)
