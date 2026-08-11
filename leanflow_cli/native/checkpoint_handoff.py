"""Derive checkpoint handoff status from structured workflow authority."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CHECKPOINT_ADVISORY_ITEM_CAP = 8
CHECKPOINT_ADVISORY_ITEM_CHARS = 500

_DEAD_BRANCH_MARKERS = (
    " cannot ",
    " could not ",
    " does not ",
    " did not ",
    " was rejected",
    " were rejected",
    " is insufficient",
    " are insufficient",
    " must not be repeated",
    " must not repeat",
)


def _bounded_advisory_line(value: Any) -> str:
    """Return one compact checkpoint advisory line without Markdown structure."""
    text = " ".join(str(value or "").split())
    return text[:CHECKPOINT_ADVISORY_ITEM_CHARS]


def _is_dead_branch_blocker(value: str) -> bool:
    """Return whether a blocker bullet records a ruled-out proof route."""
    normalized = f" {value.casefold()} "
    return any(marker in normalized for marker in _DEAD_BRANCH_MARKERS)


def extract_negative_evidence(summary_text: str) -> tuple[str, ...]:
    """Extract explicitly labeled negative evidence from a checkpoint summary.

    Checkpoint prose is advisory rather than kernel authority. Preserve only
    bullets below an explicit ``Negative evidence`` marker and clearly
    route-excluding bullets in ``## Blockers`` so a process restart does not
    silently revive a route the prior epoch already ruled out. Other generated
    next steps remain outside the authoritative resume projection.

    Managed prompts request a top-level ``Negative evidence:`` label, while
    older checkpoints sometimes used a nested list label or placed concrete
    exclusions under ``## Blockers``. Accept all three persisted shapes.
    """
    items: list[str] = []
    section = ""
    collecting_explicit = False
    marker_indent = 0
    marker_is_list_item = False
    for raw_line in str(summary_text or "").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            section = stripped[3:].strip().casefold().rstrip(":")
            collecting_explicit = section == "negative evidence"
            marker_indent = len(raw_line) - len(raw_line.lstrip())
            marker_is_list_item = False
            continue
        normalized = stripped.lstrip("-* ").lower()
        if normalized.startswith("negative evidence"):
            collecting_explicit = True
            marker_indent = len(raw_line) - len(raw_line.lstrip())
            marker_is_list_item = stripped.startswith(("- ", "* "))
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        if (
            collecting_explicit
            and stripped.startswith(("- ", "* "))
            and (not marker_is_list_item or indent > marker_indent)
        ):
            item = _bounded_advisory_line(stripped[2:])
            if item and item not in items:
                items.append(item)
                if len(items) >= CHECKPOINT_ADVISORY_ITEM_CAP:
                    break
            continue
        if collecting_explicit:
            if marker_is_list_item and stripped.startswith(("- ", "* ")):
                collecting_explicit = False
            elif not stripped.startswith(("- ", "* ")):
                collecting_explicit = False
        if section == "blockers" and stripped.startswith(("- ", "* ")):
            item = _bounded_advisory_line(stripped[2:])
            if item and _is_dead_branch_blocker(item) and item not in items:
                items.append(item)
                if len(items) >= CHECKPOINT_ADVISORY_ITEM_CAP:
                    break
    return tuple(items)


def checkpoint_advisory_records(
    entries: Sequence[Mapping[str, Any]],
    *,
    target_symbol: str,
    active_file: str,
    limit: int = 4,
) -> tuple[dict[str, Any], ...]:
    """Return recent persisted dead-branch records for one exact assignment.

    This provides a backward-compatible migration path for checkpoints written
    before structured advisories existed. Results are chronological so callers
    can append them while preserving newest-first prompt projection.
    """
    target = str(target_symbol or "").strip()
    file_text = str(active_file or "").strip()
    if not target or not file_text or limit <= 0:
        return ()
    expected_file = Path(file_text).resolve(strict=False)

    def matches_active_file(value: str) -> bool:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate.resolve(strict=False) == expected_file
        parts = candidate.parts
        return bool(parts) and expected_file.parts[-len(parts) :] == parts

    selected: list[dict[str, Any]] = []
    for raw in reversed(entries):
        checkpoint_id = str(raw.get("checkpoint_id", "") or "").strip()
        if not checkpoint_id or str(raw.get("target_symbol", "") or "").strip() != target:
            continue
        raw_files = raw.get("active_files") or []
        if isinstance(raw_files, (str, bytes)):
            raw_files = [raw_files]
        files = [str(value or "").strip() for value in raw_files if str(value or "").strip()]
        raw_active_file = str(raw.get("active_file", "") or "").strip()
        if raw_active_file:
            files.append(raw_active_file)
        if not any(matches_active_file(value) for value in files):
            continue
        evidence = extract_negative_evidence(str(raw.get("summary_text", "") or ""))
        if not evidence:
            continue
        selected.append(
            {
                "checkpoint_id": checkpoint_id,
                "created_at": str(raw.get("created_at", "") or ""),
                "target_symbol": target,
                "active_file": file_text,
                "negative_evidence": evidence,
            }
        )
        if len(selected) >= limit:
            break
    selected.reverse()
    return tuple(selected)


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
