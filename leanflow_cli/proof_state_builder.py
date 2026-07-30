"""Shape declaration snapshots and queue-horizon proof-state summaries."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from leanflow_cli.lean.lean_parsing import _declaration_line_index_from_text
from leanflow_cli.lean.lean_services import diagnostic_items
from leanflow_cli.native.native_utils import _format_diagnostic_for_model

__all__ = [
    "_line_in_declaration",
    "_declaration_line_index",
    "_find_declaration_entry",
    "_queue_horizon_summary",
    "_diagnostics_for_queue_horizon",
    "_proof_status_lines_for_queue_horizon",
]


def _line_in_declaration(entry: Mapping[str, Any] | None, line: Any) -> bool:
    if not isinstance(line, int) or line <= 0 or not entry:
        return False
    start = int(entry.get("line", 0) or 0)
    end = int(entry.get("end_line", 0) or start)
    return bool(start > 0 and start <= line <= max(start, end))


def _declaration_line_index(active_file: str) -> list[dict[str, Any]]:
    if not active_file:
        return []
    path = Path(active_file)
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return []
    return _declaration_line_index_from_text(content)


def _find_declaration_entry(active_file: str, label: str) -> dict[str, Any] | None:
    wanted = str(label or "").strip()
    if not active_file or not wanted:
        return None
    for entry in _declaration_line_index(active_file):
        if str(entry.get("name", "") or "").strip() == wanted:
            return entry
    return None


def _queue_horizon_summary(
    *,
    declaration_scope: str,
    queue_needs_final_file_sweep: bool,
    current_queue_item: Mapping[str, Any] | None,
    declaration_queue_summary: str,
    declaration_queue_total: int = 0,
) -> str:
    if declaration_scope != "file" or queue_needs_final_file_sweep:
        return declaration_queue_summary or "[none]"
    item = dict(current_queue_item or {})
    if not item:
        return "[none]"
    label = str(item.get("label", "") or "[unnamed]")
    reasons = (
        ", ".join(str(reason) for reason in item.get("reasons", []) or [] if str(reason).strip())
        or "pending"
    )
    hidden_count = max(0, int(declaration_queue_total or 0) - 1)
    lines = [
        f"- assigned declaration: {label} - {reasons}",
        (
            f"- future queue items: hidden until the manager assigns them ({hidden_count} pending)"
            if hidden_count
            else "- future queue items: no further queue items pending (0 pending)"
        ),
    ]
    return "\n".join(lines)


def _diagnostics_for_queue_horizon(
    *,
    active_file: str,
    target_symbol: str,
    diagnostics: str,
    declaration_scope: str,
    queue_needs_final_file_sweep: bool,
) -> str:
    text = str(diagnostics or "").strip()
    if declaration_scope != "file" or queue_needs_final_file_sweep or not target_symbol:
        return text or "unavailable"
    entry = _find_declaration_entry(active_file, target_symbol)
    parsed = diagnostic_items(text)
    if parsed and entry:
        scoped = [item for item in parsed if _line_in_declaration(entry, item.get("line"))]
        if scoped:
            lines = [_format_diagnostic_for_model(item) for item in scoped[:12]]
            hidden = len(scoped) - len(lines)
            if hidden > 0:
                lines.append(f"- ... plus {hidden} more diagnostic(s) in the assigned declaration")
            return "\n".join(lines)
        return (
            "No diagnostics in the assigned declaration. "
            "Diagnostics from future queue items are hidden until the manager assigns them."
        )
    lowered = text.lower()
    if (
        not text
        or "no errors found" in lowered
        or "no diagnostics" in lowered
        or "no errors" in lowered
    ):
        return text or "No diagnostics in the assigned declaration."
    return (
        "Diagnostics could not be scoped reliably for the assigned declaration. "
        "Use `lean_inspect` on the assigned declaration before editing."
    )


def _proof_status_lines_for_queue_horizon(
    *,
    active_file: str,
    target_symbol: str,
    declaration_scope: str,
    queue_needs_final_file_sweep: bool,
    sorry_count: Any,
    project_sorry_count: Any,
    project_sorry_files: list[str],
) -> list[str]:
    if declaration_scope == "file" and target_symbol and not queue_needs_final_file_sweep:
        entry = _find_declaration_entry(active_file, target_symbol)
        if entry:
            has_sorry = "yes" if entry.get("has_sorry") else "no"
        else:
            has_sorry = "[unknown]"
        return [
            f"assigned declaration has sorry: {has_sorry}",
            "future declaration sorry counts: hidden until manager assignment",
        ]
    return [
        f"sorry count: {sorry_count if sorry_count is not None else '[unknown]'}",
        f"project sorry count: {project_sorry_count if project_sorry_count is not None else '[unknown]'}",
        (
            "project files with sorry: " + ", ".join(project_sorry_files)
            if project_sorry_files
            else "project files with sorry: [none]"
        ),
    ]
