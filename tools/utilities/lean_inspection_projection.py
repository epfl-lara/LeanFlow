"""Bound model-facing exact-symbol Lean inspection payloads.

The Lean service keeps authoritative file-wide diagnostics and queue state. This module only
shapes the copy returned to a model when the caller supplied a resolvable declaration symbol.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

from leanflow_cli.lean.lean_diagnostics import classify_blocker_kind, diagnostic_items

_CAPABILITY_SIGNAL_TEXT_MAX_CHARS = 320
_CAPABILITY_SIGNAL_LIST_MAX_ITEMS = 8
_CAPABILITY_SIGNAL_LIST_ITEM_MAX_CHARS = 180


def _positive_int(value: Any) -> int | None:
    """Return a positive integer or ``None`` for an unusable location."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _severity_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count normalized diagnostics by severity for projection observability."""
    counts = Counter(
        str(item.get("severity", "") or "unknown").strip().lower() or "unknown" for item in items
    )
    return dict(sorted(counts.items()))


def _bounded_text(value: Any, *, max_chars: int) -> tuple[str, int]:
    """Return normalized status text plus the number of omitted characters."""
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized, 0
    marker = " ...[truncated]"
    kept = normalized[: max(0, max_chars - len(marker))].rstrip() + marker
    return kept, len(normalized) - len(kept) + len(marker)


def _bounded_signal_list(value: Any) -> tuple[list[str], int, int]:
    """Return bounded status strings with omitted-item and character counts."""
    if not isinstance(value, list):
        return [], 0, 0
    source = [" ".join(str(item or "").split()) for item in value if str(item or "").strip()]
    returned: list[str] = []
    omitted_chars = 0
    for item in source[:_CAPABILITY_SIGNAL_LIST_MAX_ITEMS]:
        bounded, truncated_chars = _bounded_text(
            item,
            max_chars=_CAPABILITY_SIGNAL_LIST_ITEM_MAX_CHARS,
        )
        returned.append(bounded)
        omitted_chars += truncated_chars
    omitted_items = max(0, len(source) - len(returned))
    omitted_chars += sum(len(item) for item in source[len(returned) :])
    return returned, omitted_items, omitted_chars


def _false_keys(value: Any) -> list[str]:
    """Return sorted mapping keys whose capability flag is explicitly false."""
    if not isinstance(value, Mapping):
        return []
    return sorted(str(key) for key, enabled in value.items() if enabled is False)


def _compact_capability_report(value: Any) -> dict[str, Any]:
    """Return a lossy exact-symbol capability digest that retains failure signals.

    The dedicated ``lean_capabilities`` tool remains the full capability surface. Exact-symbol
    inspection needs only project validity and actionable degradation state; enumerating every
    configured MCP name, cache path, and resource-admission detail duplicates that tool and can
    dominate an otherwise compact theorem-local result.
    """
    report = dict(value) if isinstance(value, Mapping) else {}
    report_text = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    project_error, project_error_omitted_chars = _bounded_text(
        report.get("project_error", ""),
        max_chars=_CAPABILITY_SIGNAL_TEXT_MAX_CHARS,
    )
    degraded_reasons, omitted_degraded_reasons, degraded_reason_omitted_chars = (
        _bounded_signal_list(report.get("degraded_reasons", []))
    )
    incremental = report.get("incremental")
    incremental_mapping = dict(incremental) if isinstance(incremental, Mapping) else {}
    incremental_reasons, omitted_incremental_reasons, incremental_reason_omitted_chars = (
        _bounded_signal_list(incremental_mapping.get("degraded_reasons", []))
    )
    incremental_codes, omitted_incremental_codes, incremental_code_omitted_chars = (
        _bounded_signal_list(incremental_mapping.get("degraded_codes", []))
    )
    signal_keys = {
        "project_valid",
        "project_error",
        "binaries",
        "helper_tools",
        "managed_mcp_servers",
        "degraded_reasons",
        "incremental",
    }
    omitted_keys = sorted(str(key) for key in report if key not in signal_keys)
    omitted_signal_chars = (
        project_error_omitted_chars
        + degraded_reason_omitted_chars
        + incremental_reason_omitted_chars
        + incremental_code_omitted_chars
    )
    summary: dict[str, Any] = {
        "projection": "compact_exact_symbol",
        "full_report_tool": "lean_capabilities",
        "project_valid": report.get("project_valid"),
        "project_error": project_error,
        "degraded": bool(
            report.get("project_valid") is False
            or project_error
            or degraded_reasons
            or incremental_reasons
            or incremental_codes
        ),
        "degraded_reasons": degraded_reasons,
        "incremental_degraded_reasons": incremental_reasons,
        "incremental_degraded_codes": incremental_codes,
        "unavailable_binaries": _false_keys(report.get("binaries")),
        "unavailable_helper_tools": _false_keys(report.get("helper_tools")),
        "unavailable_managed_mcp_servers": _false_keys(report.get("managed_mcp_servers")),
        # These identify the serialized capability report, not the inspected
        # Lean source. Precise names prevent a stable capability digest from
        # masquerading as stale source-revision evidence after an edit.
        "report_sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
        "report_char_count": len(report_text),
        "report_top_level_key_count": len(report),
        "omitted_top_level_key_count": len(omitted_keys),
        "omitted_top_level_keys": omitted_keys,
        "omitted_degraded_reason_count": omitted_degraded_reasons,
        "omitted_incremental_degraded_reason_count": omitted_incremental_reasons,
        "omitted_incremental_degraded_code_count": omitted_incremental_codes,
        "omitted_signal_char_count": omitted_signal_chars,
        "projected_char_count": 0,
        "omitted_char_count": 0,
    }
    # These two values describe the serialized value returned under ``capability_report``. Iterate
    # because their own digit widths contribute a handful of bytes to that representation.
    for _ in range(4):
        projected_text = json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        summary["projected_char_count"] = len(projected_text)
        summary["omitted_char_count"] = max(0, len(report_text) - len(projected_text))
    return summary


def _queue_item_matches_region(
    item: Mapping[str, Any],
    *,
    symbol: str,
    declaration_name: str,
    start_line: int,
) -> bool:
    """Return whether a file-queue item identifies the requested declaration."""
    label = str(item.get("label", "") or "").strip()
    short_symbol = str(symbol or "").strip().split(".")[-1]
    if not label or label not in {declaration_name, symbol, short_symbol}:
        return False
    return _positive_int(item.get("line")) == start_line


def project_exact_symbol_inspection(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    declaration: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return a compact exact-symbol copy while retaining every file-wide error.

    Non-error diagnostics are limited to the declaration range, and queue items are limited to
    the exact declaration. Aggregate and omitted counts make the projection explicit. Return
    ``None`` when the supplied region is not trustworthy so the caller can fail open to the full
    service payload.
    """
    start_line = _positive_int(declaration.get("line"))
    end_line = _positive_int(declaration.get("end_line"))
    if start_line is None or end_line is None or end_line < start_line:
        return None

    projected = dict(payload)
    diagnostics_text = str(payload.get("diagnostics", "") or "")
    blocker_diagnostics = diagnostics_text
    parsed_diagnostics = diagnostic_items(diagnostics_text)
    if parsed_diagnostics or not diagnostics_text.strip():
        returned_diagnostics = [
            item
            for item in parsed_diagnostics
            if str(item.get("severity", "") or "").strip().lower() == "error"
            or (isinstance(item.get("line"), int) and start_line <= int(item["line"]) <= end_line)
        ]
        omitted_diagnostics = [
            item for item in parsed_diagnostics if item not in returned_diagnostics
        ]
        projected["diagnostics"] = json.dumps(
            {"items": returned_diagnostics},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        projected["diagnostics_projection"] = "all_errors_and_target_range"
        projected["file_diagnostic_count"] = len(parsed_diagnostics)
        projected["returned_diagnostic_count"] = len(returned_diagnostics)
        projected["omitted_diagnostic_count"] = len(omitted_diagnostics)
        projected["file_diagnostic_counts_by_severity"] = _severity_counts(parsed_diagnostics)
        projected["omitted_diagnostic_counts_by_severity"] = _severity_counts(omitted_diagnostics)
        blocker_diagnostics = projected["diagnostics"] if returned_diagnostics else ""
    else:
        # Unknown diagnostic formats remain byte-for-byte visible; absence of parsed items is not
        # evidence that the text contains no compiler error.
        projected["diagnostics_projection"] = "unparsed_full"
        projected["file_diagnostic_count"] = None
        projected["returned_diagnostic_count"] = None
        projected["omitted_diagnostic_count"] = 0
        projected["file_diagnostic_counts_by_severity"] = {}
        projected["omitted_diagnostic_counts_by_severity"] = {}

    raw_queue_items = payload.get("queue_items", [])
    queue_items = (
        [dict(item) for item in raw_queue_items if isinstance(item, Mapping)]
        if isinstance(raw_queue_items, list)
        else []
    )
    declaration_name = str(declaration.get("name", "") or "").strip()
    returned_queue_items: list[dict[str, Any]] = []
    for item in queue_items:
        if not _queue_item_matches_region(
            item,
            symbol=symbol,
            declaration_name=declaration_name,
            start_line=start_line,
        ):
            continue
        bounded_item = dict(item)
        bounded_item["line"] = start_line
        bounded_item["end_line"] = end_line
        returned_queue_items.append(bounded_item)
    projected["queue_items"] = returned_queue_items
    projected["blocker_kind"] = classify_blocker_kind(
        "",
        diagnostics=blocker_diagnostics,
        goals=str(projected.get("goals", "") or ""),
        queue_reasons=tuple(
            str(reason)
            for item in returned_queue_items
            for reason in item.get("reasons", []) or []
            if str(reason).strip()
        ),
    )
    projected["queue_projection"] = "target_only"
    projected["file_queue_item_count"] = len(queue_items)
    projected["returned_queue_item_count"] = len(returned_queue_items)
    projected["omitted_queue_item_count"] = len(queue_items) - len(returned_queue_items)
    projected["inspection_scope"] = "symbol"
    projected["inspected_symbol"] = str(symbol or "").strip()
    projected["declaration_region"] = {
        "kind": str(declaration.get("kind", "") or ""),
        "name": declaration_name,
        "line": start_line,
        "end_line": end_line,
    }
    projected["projection_note"] = (
        "All file-wide errors are retained; non-error diagnostics and queue items are scoped "
        "to the requested declaration. File and project sorry counts remain aggregate. The "
        "capability report is a lossy status digest; use lean_capabilities for the full surface."
    )
    projected["capability_report"] = _compact_capability_report(
        payload.get("capability_report", {})
    )
    return projected
