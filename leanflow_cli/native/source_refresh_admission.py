"""Admit one bounded source refresh after a managed patch loses its anchor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

PATCH_ANCHOR_MISS_REASON = "patch_anchor_miss"


def _same_file(left: str, right: str) -> bool:
    """Return whether two path spellings identify the same source file."""
    if not left or not right:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return left == right


def patch_anchor_miss_reservation(
    *,
    function_name: str,
    args: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
) -> dict[str, str] | None:
    """Return a one-read reservation for an unchanged failed patch hunk."""
    if function_name != "apply_verified_patch":
        return None
    result = dict(payload or {})
    detail = " ".join(
        str(result.get(key, "") or "") for key in ("message", "error", "output")
    ).lower()
    if (
        str(result.get("status", "") or "").strip().lower() != "patch_failed"
        or result.get("patch_applied") is True
        or not any(
            marker in detail
            for marker in (
                "could not apply hunk",
                "could not find a match for old_string",
                "anchor region",
            )
        )
    ):
        return None
    requested_file = str(
        dict(args or {}).get("path", "") or dict(args or {}).get("file_path", "") or ""
    ).strip()
    if (
        not target_symbol
        or not active_file
        or not requested_file
        or not _same_file(requested_file, active_file)
    ):
        return None
    return {
        "target_symbol": target_symbol,
        "active_file": active_file,
        "source_revision_sha256": source_revision_sha256,
        "reason": PATCH_ANCHOR_MISS_REASON,
    }


def is_patch_anchor_miss(pending: Mapping[str, Any] | None) -> bool:
    """Return whether a pending refresh follows a patch-anchor miss."""
    return str(dict(pending or {}).get("reason", "") or "").strip() == PATCH_ANCHOR_MISS_REASON


def bounded_patch_anchor_read_matches(
    pending: Mapping[str, Any] | None,
    *,
    function_name: str,
    args: Mapping[str, Any] | None,
    active_file: str,
) -> bool:
    """Return whether one bounded active-file read can refresh a missed hunk."""
    if function_name != "read_file" or not is_patch_anchor_miss(pending):
        return False
    requested_file = str(
        dict(args or {}).get("path", "") or dict(args or {}).get("file_path", "") or ""
    ).strip()
    if not requested_file or not _same_file(requested_file, active_file):
        return False
    try:
        requested_start = int(dict(args or {}).get("offset", 0) or 0)
        requested_limit = int(dict(args or {}).get("limit", 0) or 0)
    except (TypeError, ValueError):
        return False
    # The missed hunk can insert a helper well before the assigned theorem.
    # The runner consumes this reservation after one successful read.
    return requested_start > 0 and requested_limit > 0
