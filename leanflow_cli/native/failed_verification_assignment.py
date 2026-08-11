"""Restore a durable theorem assignment after a sorry-free verification failure."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


def _same_file(left: str, right: str) -> bool:
    """Return whether two non-empty paths identify the same file."""
    return bool(left and right and os.path.realpath(left) == os.path.realpath(right))


def restore_failed_assignment(
    live_state: Mapping[str, Any],
    assignment: Mapping[str, Any],
    *,
    queue_item: Mapping[str, Any] | None,
    declaration_prefix: str,
    declaration_slice: str,
    failure: str,
) -> dict[str, Any]:
    """Keep a present durable assignment visible after exact verification fails.

    A sorry-free file can have an empty source-derived queue even though its
    assigned theorem still fails elaboration or exceeds a resource limit. In
    that state the exact verification result, not the empty placeholder scan,
    owns work selection. Restore only an otherwise empty file-scoped queue and
    require the assigned declaration to still be present on disk.
    """
    restored = dict(live_state)
    target = str(assignment.get("target_symbol", "") or "").strip()
    assigned_file = str(assignment.get("active_file", "") or "").strip()
    active_file = str(restored.get("active_file", "") or "").strip()
    item = dict(queue_item or {})
    if (
        str(restored.get("declaration_scope", "") or "").strip() != "file"
        or not target
        or not _same_file(active_file, assigned_file)
        or not item
        or str(item.get("label", "") or "").strip() != target
        or dict(restored.get("current_queue_item") or {})
        or int(restored.get("declaration_queue_total", 0) or 0) != 0
    ):
        return restored

    reasons = [str(reason) for reason in item.get("reasons", []) or [] if str(reason)]
    if "exact verification failed" not in reasons:
        reasons.append("exact verification failed")
    item["reasons"] = reasons
    queue = [item]
    restored.update(
        {
            "target_symbol": target,
            "declaration_queue_total": 1,
            "declaration_queue": queue,
            "declaration_queue_preview": queue,
            "current_queue_item": item,
            "current_queue_item_prefix": declaration_prefix,
            "current_queue_item_slice": declaration_slice,
            "current_blocker": failure,
            "blocker_summary": failure,
            "queue_frontier_exhausted": False,
            "queue_needs_final_file_sweep": False,
        }
    )
    return restored
