"""Build bounded activity identifiers for acknowledged research findings."""

from __future__ import annotations

import json
from collections.abc import Sequence
from hashlib import sha256

DELIVERY_ACTIVITY_IDENTIFIER_CAP = 8
DELIVERY_ACTIVITY_COMPONENT_CHAR_CAP = 256
DELIVERY_ACTIVITY_MARKER_CHAR_CAP = 600
DELIVERY_ACK_TOKEN_PREFIX = "leanflow-research-ack:"


def _normalized_markers(markers: Sequence[str]) -> tuple[str, ...]:
    """Return non-empty delivery markers in deterministic set order."""
    return tuple(sorted({str(marker) for marker in markers if str(marker)}))


def _marker_parts(marker: str) -> tuple[str, str]:
    """Parse one canonical ``delivery_key`` without guessing malformed values."""
    try:
        payload = json.loads(marker)
    except (TypeError, ValueError):
        return "", ""
    if not isinstance(payload, list) or len(payload) != 2:
        return "", ""
    job_id, target_symbol = payload
    if not isinstance(job_id, str) or not isinstance(target_symbol, str):
        return "", ""
    return job_id, target_symbol


def _ack_token(marker: str) -> str:
    """Return a fixed-size token for correlating an event with its receipt marker."""
    return DELIVERY_ACK_TOKEN_PREFIX + sha256(marker.encode("utf-8")).hexdigest()


def _marker_set_sha256(markers: Sequence[str]) -> str:
    """Return a stable digest for the complete acknowledged marker set."""
    payload = json.dumps(list(markers), ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def delivery_activity_details(markers: Sequence[str]) -> dict[str, object]:
    """Return bounded, auditable identifiers for one delivery activity event.

    Ordinary foreground batches retain their exact job ids and receipt markers.
    Fixed-size acknowledgement tokens and a digest of the complete set preserve
    correlation when hostile legacy identifiers or an oversized batch exceed
    the activity payload limits.
    """
    normalized = _normalized_markers(markers)
    included = normalized[:DELIVERY_ACTIVITY_IDENTIFIER_CAP]
    job_ids: list[str] = []
    target_symbols: list[str] = []
    receipt_markers: list[str] = []
    exact_fields_omitted = 0
    for marker in included:
        job_id, target_symbol = _marker_parts(marker)
        marker_complete = len(marker) <= DELIVERY_ACTIVITY_MARKER_CHAR_CAP
        job_complete = bool(job_id) and len(job_id) <= DELIVERY_ACTIVITY_COMPONENT_CHAR_CAP
        target_complete = bool(target_symbol) and (
            len(target_symbol) <= DELIVERY_ACTIVITY_COMPONENT_CHAR_CAP
        )
        if marker_complete:
            receipt_markers.append(marker)
        if job_complete:
            job_ids.append(job_id)
        if target_complete:
            target_symbols.append(target_symbol)
        if not marker_complete or not job_complete or not target_complete:
            exact_fields_omitted += 1

    identifier_omissions = max(0, len(normalized) - len(included))
    return {
        "marker_count": len(normalized),
        "delivery_job_ids": list(dict.fromkeys(job_ids)),
        "delivery_target_symbols": list(dict.fromkeys(target_symbols)),
        "delivery_receipt_markers": receipt_markers,
        "delivery_ack_tokens": [_ack_token(marker) for marker in included],
        "delivery_identifier_set_sha256": _marker_set_sha256(normalized),
        "delivery_identifiers_included": len(included),
        "delivery_identifiers_omitted": identifier_omissions,
        "delivery_exact_fields_omitted": exact_fields_omitted,
        "delivery_identifiers_truncated": bool(identifier_omissions or exact_fields_omitted),
    }
