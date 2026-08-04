"""Fence discovery after a managed prover turn reserves synthesis."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

BROAD_SEARCH_TOOL_NAMES = frozenset(
    {"lean_search", "lean_auto_search", "web_search", "web_fetch", "web_download"}
)
SOURCE_INSPECTION_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        "lean_capabilities",
        "lean_inspect",
        "lean_lemma_suggest",
        "lean_outline",
        "lean_proof_context",
        "lean_sorries",
    }
)
DISCOVERY_TOOL_NAMES = BROAD_SEARCH_TOOL_NAMES | SOURCE_INSPECTION_TOOL_NAMES


@dataclass(frozen=True)
class SourceInspectionDecision:
    """Describe one construction-mode local source-inspection observation."""

    count: int = 0
    same_request_streak: int = 0
    nudge: bool = False
    close_turn: bool = False


def blocked_search_result(
    *,
    function_name: str,
    tracker: Mapping[str, Any],
    target_symbol: str,
    active_file: str,
    current_cycle: int | None = None,
) -> dict[str, object] | None:
    """Return a deterministic preflight rejection for forbidden discovery.

    The reservation belongs to one exact queue assignment. Constructive tools
    remain available so the prover can turn preserved evidence into a checked
    candidate. Exact source inspection becomes available again only after the
    outer orchestrator advances to a fresh construction cycle.
    """
    if function_name not in DISCOVERY_TOOL_NAMES:
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
    boundary_cycle = tracker.get("synthesis_boundary_cycle")
    same_cycle = (
        current_cycle is not None
        and boundary_cycle is not None
        and int(boundary_cycle) == int(current_cycle)
    )
    if function_name in SOURCE_INSPECTION_TOOL_NAMES and not same_cycle:
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


def source_inspection_fingerprint(
    function_name: str,
    args: Mapping[str, Any] | None,
) -> str:
    """Return a presentation-insensitive identity for one local source lookup."""
    arguments = dict(args or {})
    if function_name == "search_files":
        material = "|".join(
            (
                function_name,
                str(arguments.get("path", "") or "").strip(),
                str(arguments.get("pattern", "") or "").strip(),
                str(arguments.get("file_glob", "") or "").strip(),
            )
        )
    elif function_name == "read_file":
        material = "|".join(
            (
                function_name,
                str(arguments.get("path", "") or "").strip(),
                str(arguments.get("offset", "") or "").strip(),
                str(arguments.get("limit", "") or "").strip(),
            )
        )
    else:
        return ""
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


def observe_source_inspection(
    tracker: Mapping[str, Any],
    *,
    function_name: str,
    args: Mapping[str, Any] | None,
    cycle: int,
    hard_limit: int,
    repeat_hard_limit: int,
) -> tuple[dict[str, Any], SourceInspectionDecision]:
    """Advance one cycle-local construction source window."""
    updated = dict(tracker)
    stored_cycle = updated.get("construction_source_inspection_cycle")
    if stored_cycle is None or int(stored_cycle) != int(cycle):
        updated["construction_source_inspection_cycle"] = int(cycle)
        updated["construction_source_inspection_count"] = 0
        updated["construction_source_inspection_same_request_streak"] = 0
        updated.pop("construction_source_inspection_last_fingerprint", None)
        updated.pop("construction_source_inspection_nudged", None)
    fingerprint = source_inspection_fingerprint(function_name, args)
    same_request_streak = (
        int(updated.get("construction_source_inspection_same_request_streak", 0) or 0) + 1
        if fingerprint
        and str(updated.get("construction_source_inspection_last_fingerprint", "") or "")
        == fingerprint
        else 1
    )
    count = int(updated.get("construction_source_inspection_count", 0) or 0) + 1
    updated["construction_source_inspection_count"] = count
    updated["construction_source_inspection_same_request_streak"] = same_request_streak
    updated["construction_source_inspection_last_fingerprint"] = fingerprint
    close_turn = bool(
        (hard_limit and count >= hard_limit)
        or (repeat_hard_limit and same_request_streak >= repeat_hard_limit)
    )
    nudge_at = max(2, hard_limit // 2) if hard_limit else 0
    nudge = bool(
        not close_turn
        and nudge_at
        and count >= nudge_at
        and not bool(updated.get("construction_source_inspection_nudged"))
    )
    if nudge:
        updated["construction_source_inspection_nudged"] = True
    if close_turn:
        updated["construction_source_inspection_boundary"] = True
    return updated, SourceInspectionDecision(
        count=count,
        same_request_streak=same_request_streak,
        nudge=nudge,
        close_turn=close_turn,
    )
