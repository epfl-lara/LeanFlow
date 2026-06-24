"""Queue-item classification predicates for the native runner.

Leaf module: pure predicates over inspection/queue items (sorry vs diagnostic blockers,
current-item/status selection, attempted-proof shape). Extracted verbatim from native_runner.py
and re-exported there; imports only stdlib and the lean_services / lean_diagnostic_feedback /
native_utils / queue_manager leaves, so it introduces no import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from epflemma_cli.lean_diagnostic_feedback import _declaration_slice_text
from epflemma_cli.lean_services import diagnostic_items
from epflemma_cli.native_utils import _single_line
from epflemma_cli.proof_state_builder import _find_declaration_entry, _line_in_declaration
from epflemma_cli.queue_manager import TheoremQueueManager


def _queue_item_has_diagnostic_reason(item: Mapping[str, Any]) -> bool:
    reasons = " ".join(str(reason or "") for reason in item.get("reasons", []) or []).lower()
    return bool(
        "diagnostic" in reasons
        or "error" in reasons
        or "unsolved" in reasons
        or "type mismatch" in reasons
        or "failed" in reasons
    )


def _queue_item_has_sorry_reason(item: Mapping[str, Any]) -> bool:
    return any(str(reason or "").strip().lower() == "contains sorry" for reason in item.get("reasons", []) or [])


def _queue_item_has_error_diagnostic(item: Mapping[str, Any], active_file: str, diagnostics: str) -> bool:
    label = str(item.get("label", "") or "").strip()
    entry = _find_declaration_entry(active_file, label)
    if not entry:
        return False
    for diagnostic in diagnostic_items(diagnostics):
        if str(diagnostic.get("severity", "") or "").strip().lower() != "error":
            continue
        if _line_in_declaration(entry, diagnostic.get("line")):
            return True
    return False


def _inspection_queue_item_is_queue_blocker(item: Mapping[str, Any], active_file: str, diagnostics: str) -> bool:
    if _queue_item_has_sorry_reason(item):
        return True
    if _queue_item_has_error_diagnostic(item, active_file, diagnostics):
        return True
    reasons = " ".join(str(reason or "") for reason in item.get("reasons", []) or []).lower()
    return bool(
        "error" in reasons
        or "unsolved" in reasons
        or "type mismatch" in reasons
        or "failed" in reasons
    )


def _current_queue_item(queue: list[dict[str, Any]], active_file: str) -> dict[str, Any] | None:
    if not queue or not active_file:
        return None
    mgr = TheoremQueueManager()
    mgr.set_active_file(active_file)
    mgr.replace_queue(queue)
    selected = mgr.select_next(is_present_in_file=lambda label: bool(_find_declaration_entry(active_file, label)))
    if selected is None:
        return None
    for item in queue:
        if str(item.get("label", "") or "").strip() == selected.label:
            return dict(item)
    return {"label": selected.label, "reasons": list(selected.reasons)}


def _current_queue_status(live_state: Mapping[str, Any]) -> str:
    blocker = str(live_state.get("current_blocker", "") or "").strip()
    if blocker:
        return "blocked"
    item = dict(live_state.get("current_queue_item") or {})
    reasons = ", ".join(item.get("reasons", []) or []).strip()
    if "sorry" in reasons:
        return "pending"
    return "in-progress"


def _attempt_proof_shape(live_state: Mapping[str, Any] | None) -> str:
    item = dict((live_state or {}).get("current_queue_item") or {})
    active_file = str((live_state or {}).get("active_file", "") or "")
    label = str(item.get("label", "") or (live_state or {}).get("target_symbol", "") or "").strip()
    slice_text = _declaration_slice_text(active_file, label) if active_file and label else ""
    if not slice_text:
        slice_text = str((live_state or {}).get("current_queue_item_slice", "") or "").strip()
    if not slice_text:
        return "[no attempted proof shape recorded]"
    _, _, body = slice_text.partition(":\n")
    snippet = body.strip() or slice_text
    lines = [line.rstrip() for line in snippet.splitlines() if line.strip()]
    if len(lines) > 6:
        lines = lines[:6]
    text = " ".join(lines)
    return _single_line(text, 240)
