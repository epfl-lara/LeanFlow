"""Derive checkpoint handoff status from structured workflow authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CHECKPOINT_ADVISORY_ITEM_CAP = 8
CHECKPOINT_ADVISORY_ITEM_CHARS = 500


def _bounded_advisory_line(value: Any) -> str:
    """Return one compact checkpoint advisory line without Markdown structure."""
    text = " ".join(str(value or "").split())
    return text[:CHECKPOINT_ADVISORY_ITEM_CHARS]


def extract_negative_evidence(summary_text: str) -> tuple[str, ...]:
    """Extract explicitly labeled negative evidence from a checkpoint summary.

    Checkpoint prose is advisory rather than kernel authority. Preserve only
    bullets nested below an explicit ``Negative evidence`` marker so a process
    restart does not silently revive a route the prior epoch already ruled
    out. Other generated next steps remain outside the authoritative resume
    projection.
    """
    items: list[str] = []
    collecting = False
    marker_indent = 0
    for raw_line in str(summary_text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            collecting = False
            continue
        normalized = stripped.lstrip("-* ").lower()
        if normalized.startswith("negative evidence"):
            collecting = True
            marker_indent = len(raw_line) - len(raw_line.lstrip())
            continue
        if not collecting:
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        if stripped.startswith(("- ", "* ")) and indent > marker_indent:
            item = _bounded_advisory_line(stripped[2:])
            if item and item not in items:
                items.append(item)
                if len(items) >= CHECKPOINT_ADVISORY_ITEM_CAP:
                    break
            continue
        if stripped.startswith(("- ", "* ")) and indent <= marker_indent:
            collecting = False
    return tuple(items)


def checkpoint_success_state(
    live_state: Mapping[str, Any] | None,
    *,
    verified: bool,
    blocker_summary: str,
) -> str:
    """Return one status for both checkpoint metadata and summary prose.

    A signal-interrupted campaign remains mathematically in progress even when
    its target has a concrete blocker. Blocker text is resume evidence, not an
    authoritative terminal verdict.
    """
    if verified:
        return "verified"
    current = dict(live_state or {})
    try:
        exit_code = int(current.get("exit_code", 0) or 0)
    except (TypeError, ValueError):
        exit_code = 0
    interrupt_source = str(current.get("interrupt_source", "") or "").strip().lower()
    if exit_code == 130 or interrupt_source == "signal":
        return "in-progress"
    return "blocked" if str(blocker_summary or "").strip() else "in-progress"
