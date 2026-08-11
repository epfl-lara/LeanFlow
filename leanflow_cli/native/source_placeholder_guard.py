"""Keep assigned target checks on canonical source and skip known placeholders."""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping
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


def normalize_assigned_target_check(
    function_name: str,
    arguments: MutableMapping[str, Any] | None,
    assignment: Mapping[str, Any] | None,
    *,
    project_root: str,
) -> tuple[str, str] | None:
    """Route an assigned target check to its canonical queue-owned file.

    A model-authored path is not authoritative once the manager has selected a
    target. Rewrite only exact ``check_target`` calls for that same target; an
    explicit request for another declaration remains untouched.
    """
    if str(function_name or "").strip() != "lean_incremental_check" or arguments is None:
        return None
    action = str(arguments.get("action", "") or "check_target").strip().casefold().replace("-", "_")
    if action != "check_target":
        return None
    current = dict(assignment or {})
    target_symbol = str(current.get("target_symbol", "") or "").strip()
    active_file = _canonical_file(str(current.get("active_file", "") or ""), project_root)
    requested_target = str(
        arguments.get("theorem_id", "") or arguments.get("target_symbol", "") or ""
    ).strip()
    if (
        not target_symbol
        or not active_file
        or not Path(active_file).is_file()
        or (requested_target and requested_target != target_symbol)
    ):
        return None
    requested_file = str(
        arguments.get("file_path", "") or arguments.get("active_file", "") or ""
    ).strip()
    canonical_requested = _canonical_file(requested_file, project_root)
    if canonical_requested == active_file and requested_target == target_symbol:
        return None
    arguments["file_path"] = active_file
    arguments["theorem_id"] = target_symbol
    return canonical_requested, active_file


def normalize_assigned_declaration_context(
    function_name: str,
    arguments: MutableMapping[str, Any] | None,
    assignment: Mapping[str, Any] | None,
    *,
    project_root: str,
) -> tuple[str, str, str] | None:
    """Recover a declaration name misplaced in a proof-context file field.

    Repair only an unambiguous local typo: the requested path must not exist,
    no theorem id may be present, and the path basename must exactly name a
    declaration in the queue-owned source file.
    """
    if str(function_name or "").strip() != "lean_proof_context" or arguments is None:
        return None
    if str(arguments.get("theorem_id", "") or "").strip():
        return None
    current = dict(assignment or {})
    active_file = _canonical_file(str(current.get("active_file", "") or ""), project_root)
    requested_file = str(arguments.get("file_path", "") or "").strip()
    canonical_requested = _canonical_file(requested_file, project_root)
    if not active_file or not Path(active_file).is_file() or not canonical_requested:
        return None
    requested_path = Path(canonical_requested)
    if requested_path.exists():
        return None
    candidate = requested_path.name
    if candidate.endswith(".lean"):
        candidate = candidate[: -len(".lean")]
    if not candidate or _find_declaration_entry(active_file, candidate) is None:
        return None
    arguments["file_path"] = active_file
    arguments["theorem_id"] = candidate
    return canonical_requested, active_file, candidate


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
