"""Fence discovery after a managed prover turn reserves synthesis."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

BROAD_SEARCH_TOOL_NAMES = frozenset(
    {"lean_search", "lean_auto_search", "web_search", "web_fetch", "web_download"}
)
SOURCE_INSPECTION_TOOL_NAMES = frozenset({"read_file", "search_files"})
DISCOVERY_TOOL_NAMES = BROAD_SEARCH_TOOL_NAMES | SOURCE_INSPECTION_TOOL_NAMES


def blocked_search_result(
    *,
    function_name: str,
    tracker: Mapping[str, Any],
    target_symbol: str,
    active_file: str,
) -> dict[str, object] | None:
    """Return a deterministic preflight rejection for a forbidden broad search.

    The reservation belongs to one exact queue assignment. Constructive tools
    and local source inspection remain available so the prover can turn
    preserved evidence into a checked candidate instead of being forced to
    discard useful momentum.
    """
    if function_name not in BROAD_SEARCH_TOOL_NAMES:
        return None
    if not (
        bool(tracker.get("synthesis_grace_pending")) or bool(tracker.get("hard_route_requested"))
    ):
        return None
    if (
        str(tracker.get("target_symbol", "") or "") != target_symbol
        or str(tracker.get("active_file", "") or "") != active_file
    ):
        return None
    search_count = int(tracker.get("search_count", 0) or 0)
    return {
        "success": False,
        "status": "search_synthesis_required",
        "blocked_tool": function_name,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "search_count": search_count,
        "provider_called": False,
        "required_action": (
            "Synthesize the strongest preserved findings and concrete proof shape now. "
            "Do not request another broad search before the outer route handoff."
        ),
        "allowed_actions": [
            "respond without a tool call with the concrete synthesis",
            "make a proof edit",
            "check a concrete Lean candidate",
            "decompose the target into explicit helper lemmas",
        ],
        "reason": (
            "This assignment already reached its bounded search budget. The extra search "
            "was rejected before provider or search execution so the saved evidence can "
            "move to construction."
        ),
    }


def request_description(
    function_name: str,
    args: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Return a stable description for one search or source-inspection request."""
    arguments = dict(args or {})
    result = dict(payload or {})
    ordinary = str(
        result.get("query", "")
        or arguments.get("query", "")
        or arguments.get("q", "")
        or result.get("url", "")
        or arguments.get("url", "")
        or arguments.get("uri", "")
        or ""
    )
    if ordinary:
        return ordinary
    if function_name == "search_files":
        return " ".join(
            part
            for part in (
                f"path={arguments.get('path', '')}",
                f"pattern={arguments.get('pattern', '')}",
                f"glob={arguments.get('file_glob', '')}",
                f"mode={arguments.get('output_mode', '')}",
            )
            if not part.endswith("=")
        )
    if function_name == "read_file":
        return " ".join(
            part
            for part in (
                f"path={arguments.get('path', '')}",
                f"offset={arguments.get('offset', '')}",
                f"limit={arguments.get('limit', '')}",
            )
            if not part.endswith("=")
        )
    return ""
