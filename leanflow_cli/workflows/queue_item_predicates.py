"""Classify native-runner queue items and attempted proof shapes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from leanflow_cli.lean.lean_diagnostic_feedback import _declaration_slice_text
from leanflow_cli.lean.lean_services import diagnostic_items
from leanflow_cli.native.native_utils import _single_line
from leanflow_cli.proof_state_builder import (
    _declaration_line_index,
    _find_declaration_entry,
    _line_in_declaration,
)
from leanflow_cli.workflows.queue_manager import TheoremQueueManager


def _queue_item_has_sorry_reason(item: Mapping[str, Any]) -> bool:
    return any(
        str(reason or "").strip().lower() == "contains sorry"
        for reason in item.get("reasons", []) or []
    )


def _queue_item_has_error_diagnostic(
    item: Mapping[str, Any], active_file: str, diagnostics: str
) -> bool:
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


def _inspection_queue_item_is_queue_blocker(
    item: Mapping[str, Any], active_file: str, diagnostics: str
) -> bool:
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


def _current_queue_item(
    queue: list[dict[str, Any]],
    active_file: str,
    precedence: Callable[[str], int] | None = None,
    order_key: Callable[[str], Any] | None = None,
) -> dict[str, Any] | None:
    """Select one present queue item after indexing the active source once.

    Queue selection evaluates the presence predicate for every candidate. A
    predicate implemented with ``_find_declaration_entry`` reparses the whole
    Lean file per candidate, which turns a long research file into quadratic
    refresh work and large transient allocation. Build the exact declaration
    name set once; the manager's ordering and filtering semantics are
    otherwise unchanged.
    """
    if not queue or not active_file:
        return None
    present_labels = {
        str(entry.get("name", "") or "").strip()
        for entry in _declaration_line_index(active_file)
        if str(entry.get("name", "") or "").strip()
    }
    mgr = TheoremQueueManager()
    mgr.set_active_file(active_file)
    mgr.replace_queue(queue)
    selected = mgr.select_next(
        is_present_in_file=lambda label: str(label or "").strip() in present_labels,
        precedence=precedence,
        order_key=order_key,
    )
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
