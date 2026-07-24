"""Validate immediate graph synchronization for verified queue transitions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _normalized_path(value: str) -> str:
    """Return one best-effort absolute path without requiring it to exist."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError):
        return text


def _same_file(left: str, right: str) -> bool:
    """Return whether two absolute or project-relative labels identify one file."""
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text or not right_text:
        return False
    if left_text == right_text or _normalized_path(left_text) == _normalized_path(right_text):
        return True
    try:
        left_parts = Path(left_text).parts
        right_parts = Path(right_text).parts
    except (OSError, RuntimeError, ValueError):
        return False
    return bool(
        left_parts
        and right_parts
        and (
            (len(left_parts) >= len(right_parts) and left_parts[-len(right_parts) :] == right_parts)
            or (
                len(right_parts) >= len(left_parts)
                and right_parts[-len(left_parts) :] == left_parts
            )
        )
    )


@dataclass(frozen=True)
class VerifiedTransitionSync:
    """Describe the exact completed and newly active queue assignments."""

    completed_target: str
    completed_file: str
    current_target: str
    current_file: str
    current_slice: str = ""

    def assignment_mapping(self) -> dict[str, str]:
        """Return the new assignment view consumed by graph synchronization."""
        return {
            "target_symbol": self.current_target,
            "active_file": self.current_file,
            "slice": self.current_slice,
        }


def verified_transition_sync(
    *,
    transition: Mapping[str, Any] | None,
    outcome: Mapping[str, Any] | None,
    live_state: Mapping[str, Any] | None,
    verification_accepted: bool,
) -> VerifiedTransitionSync | None:
    """Return an immediate sync request only for one exact verified transition.

    The caller supplies the deterministic gate verdict. This module only
    validates that the solved outcome, transition identities, and live next
    assignment all describe the same boundary; advisory text cannot create a
    graph-promotion request.
    """
    if not verification_accepted:
        return None
    transition_data = dict(transition or {})
    outcome_data = dict(outcome or {})
    current = dict(live_state or {})
    if str(outcome_data.get("status", "") or "").strip().lower() != "solved":
        return None

    completed_target = str(transition_data.get("previous_target", "") or "").strip()
    completed_file = str(transition_data.get("previous_file", "") or "").strip()
    current_target = str(transition_data.get("current_target", "") or "").strip()
    current_file = str(transition_data.get("current_file", "") or "").strip()
    outcome_target = str(outcome_data.get("target_symbol", "") or "").strip()
    outcome_file = str(outcome_data.get("active_file", "") or "").strip()
    item = dict(current.get("current_queue_item") or {})
    live_target = str(item.get("label", "") or current.get("target_symbol", "") or "").strip()
    live_file = str(
        current.get("active_file", "") or current.get("active_file_label", "") or ""
    ).strip()
    if not all(
        (
            completed_target,
            completed_file,
            current_target,
            current_file,
            outcome_target,
            outcome_file,
            live_target,
            live_file,
        )
    ):
        return None
    if outcome_target != completed_target or not _same_file(outcome_file, completed_file):
        return None
    if live_target != current_target or not _same_file(live_file, current_file):
        return None
    if current_target == completed_target and _same_file(current_file, completed_file):
        return None
    return VerifiedTransitionSync(
        completed_target=completed_target,
        completed_file=completed_file,
        current_target=current_target,
        current_file=current_file,
        current_slice=str(current.get("current_queue_item_slice", "") or ""),
    )
