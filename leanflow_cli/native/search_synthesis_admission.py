"""Fence discovery after a managed prover turn reserves synthesis."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from leanflow_cli.lean.lean_parsing import (
    _is_lean_inspection_only_helper_candidate,
    _is_lean_inspection_only_target_candidate,
)
from tools.utilities.workflow_artifact_guard import is_managed_plan_path

LEAN_INCREMENTAL_INSPECTION_TOOL_NAME = "lean_incremental_check:inspection"

BROAD_SEARCH_TOOL_NAMES = frozenset(
    {"lean_search", "lean_auto_search", "web_search", "web_fetch", "web_download"}
)
SOURCE_INSPECTION_TOOL_NAMES = frozenset(
    {
        "read_file",
        "search_files",
        "lean_capabilities",
        "lean_axioms",
        "lean_inspect",
        "lean_lemma_suggest",
        "lean_outline",
        "lean_proof_context",
        "lean_sorries",
        LEAN_INCREMENTAL_INSPECTION_TOOL_NAME,
    }
)
DISCOVERY_TOOL_NAMES = BROAD_SEARCH_TOOL_NAMES | SOURCE_INSPECTION_TOOL_NAMES
PATH_SCOPED_DISCOVERY_TOOL_NAMES = frozenset({"read_file"})
CONSTRUCTION_DEBT_STATE_KEY = "construction_turn_debt"
CONSTRUCTION_ATTEMPT_SERIAL_KEY = "construction_attempt_serial"

_CONSTRUCTION_WINDOW_KEYS = (
    "construction_source_inspection_cycle",
    "construction_source_inspection_count",
    "construction_source_inspection_same_request_streak",
    "construction_source_inspection_last_fingerprint",
    "construction_source_inspection_nudged",
    "construction_source_inspection_boundary",
    "construction_synthesis_rejection_count",
)


@dataclass(frozen=True)
class SourceInspectionDecision:
    """Describe one construction-mode local source-inspection observation."""

    count: int = 0
    same_request_streak: int = 0
    nudge: bool = False
    close_turn: bool = False


@dataclass(frozen=True)
class ConstructionTurnDecision:
    """Describe assignment-local construction debt after one unresolved turn."""

    count: int = 0
    require_construction: bool = False
    reset_reason: str = ""


def schedule_fresh_construction_window(tracker: Mapping[str, Any]) -> dict[str, Any]:
    """Mark a completed route handoff to refresh source inspection next turn."""
    updated = dict(tracker)
    updated["construction_source_window_reset_pending"] = True
    return updated


def prepare_provider_turn(tracker: Mapping[str, Any]) -> dict[str, Any]:
    """Open a fresh source window after the previous turn yielded to orchestration."""
    updated = dict(tracker)
    if not bool(updated.pop("construction_source_window_reset_pending", False)):
        return updated
    for key in _CONSTRUCTION_WINDOW_KEYS:
        updated.pop(key, None)
    # The boundary cycle fences local source context only for the provider turn
    # that exhausted discovery. Preserve the assignment's broad-search debt,
    # but let the distinct construction route recover exact declarations once.
    updated.pop("synthesis_boundary_cycle", None)
    return updated


def construction_attempt_request(
    function_name: str,
    args: Mapping[str, Any] | None,
    *,
    result_status: str = "",
) -> bool:
    """Return whether a tool request materially constructs or screens Lean code."""
    arguments = dict(args or {})
    if function_name in {"patch", "write_file", "apply_verified_patch"}:
        return str(result_status or "").strip().lower() not in {
            "direct_self_reference_rejected",
            "rejected_candidate_replay",
            "isolated_suggestion_probe_required",
        }
    if function_name == "lean_extract_have":
        action = str(arguments.get("action", "extract") or "extract")
        return action.strip().lower().replace("-", "_") not in {
            "inventory",
            "inspect",
            "list",
            "plan",
        }
    if function_name == "lean_decompose_helpers":
        return True
    if function_name == "lean_multi_attempt":
        attempts = arguments.get("attempts")
        return isinstance(attempts, (list, tuple)) and bool(attempts)
    if function_name != "lean_incremental_check":
        return False
    action = str(arguments.get("action", "check_target") or "check_target")
    action = action.strip().lower().replace("-", "_")
    replacement = str(arguments.get("replacement", "") or "").strip()
    return bool(
        replacement
        and action in {"check_helper", "check_target"}
        and not is_inspection_only_incremental_check(function_name, arguments)
    )


def record_construction_attempt(state: Mapping[str, Any]) -> dict[str, Any]:
    """Advance the durable serial used to distinguish concrete prover work."""
    updated = dict(state)
    try:
        serial = int(updated.get(CONSTRUCTION_ATTEMPT_SERIAL_KEY, 0) or 0)
    except (TypeError, ValueError):
        serial = 0
    updated[CONSTRUCTION_ATTEMPT_SERIAL_KEY] = serial + 1
    return updated


def observe_unresolved_construction_turn(
    tracker: Mapping[str, Any] | None,
    *,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
    construction_attempt_serial: int,
    requested_route: str,
    limit: int,
) -> tuple[dict[str, Any], ConstructionTurnDecision]:
    """Accumulate no-construction debt across route labels and campaign epochs."""
    previous = dict(tracker or {})
    same_scope = bool(
        str(previous.get("target_symbol", "") or "") == target_symbol
        and str(previous.get("active_file", "") or "") == active_file
    )
    same_source = bool(
        same_scope
        and str(previous.get("source_revision_sha256", "") or "") == source_revision_sha256
    )
    try:
        prior_serial = int(previous.get("construction_attempt_serial", 0) or 0)
    except (TypeError, ValueError):
        prior_serial = 0
    reset_reason = ""
    if not same_scope:
        count = 1
        reset_reason = "assignment-changed" if previous else ""
        routes: list[str] = []
    elif not same_source:
        count = 0
        reset_reason = "source-changed"
        routes = []
    elif prior_serial != int(construction_attempt_serial):
        count = 0
        reset_reason = "construction-attempted"
        routes = []
    else:
        count = int(previous.get("count", 0) or 0) + 1
        routes = [str(route) for route in (previous.get("routes") or []) if str(route)]
    route = str(requested_route or "").strip().lower()
    if route:
        routes.append(route)
    routes = routes[-8:]
    require_construction = bool(limit > 0 and count >= limit)
    updated = {
        "target_symbol": target_symbol,
        "active_file": active_file,
        "source_revision_sha256": source_revision_sha256,
        "construction_attempt_serial": int(construction_attempt_serial),
        "count": count,
        "routes": routes,
        "require_construction": require_construction,
    }
    return updated, ConstructionTurnDecision(
        count=count,
        require_construction=require_construction,
        reset_reason=reset_reason,
    )


def construction_required_for_assignment(
    tracker: Mapping[str, Any] | None,
    *,
    target_symbol: str,
    active_file: str,
    source_revision_sha256: str,
    construction_attempt_serial: int | None = None,
) -> bool:
    """Return whether unchanged assignment state owes a concrete construction."""
    current = dict(tracker or {})
    try:
        stored_serial = int(current.get("construction_attempt_serial", 0) or 0)
        requested_serial = (
            stored_serial
            if construction_attempt_serial is None
            else int(construction_attempt_serial)
        )
    except (TypeError, ValueError):
        return False
    serial_matches = stored_serial == requested_serial
    return bool(
        current.get("require_construction")
        and serial_matches
        and str(current.get("target_symbol", "") or "") == target_symbol
        and str(current.get("active_file", "") or "") == active_file
        and str(current.get("source_revision_sha256", "") or "") == source_revision_sha256
    )


def is_inspection_only_incremental_check(
    function_name: str,
    args: Mapping[str, Any] | None,
) -> bool:
    """Return whether an incremental check is only browsing the Lean environment.

    Models sometimes wrap ``#check``, ``#print``, or ``run_cmd`` commands in a
    dummy ``True`` lemma so ``check_helper`` accepts the request. Such calls do
    not construct a reusable helper and must consume the bounded discovery
    window instead of resetting it as kernel-verified proof progress.
    """
    if function_name != "lean_incremental_check":
        return False
    arguments = dict(args or {})
    action = str(arguments.get("action", "") or "").strip().lower()
    replacement = str(arguments.get("replacement", "") or "")
    if action == "check_helper":
        return bool(replacement and _is_lean_inspection_only_helper_candidate(replacement))
    if action == "check_target":
        return bool(replacement and _is_lean_inspection_only_target_candidate(replacement))
    return False


def discovery_tool_name(
    function_name: str,
    args: Mapping[str, Any] | None,
) -> str | None:
    """Return the discovery accounting name for one managed tool request."""
    arguments = dict(args or {})
    if function_name == "read_file" and is_managed_plan_path(str(arguments.get("path", "") or "")):
        # The file tool already exposes only the bounded generated plan view.
        # Reading that durable handoff consumes neither source nor remote-search
        # budget and must remain available when the prover owes construction.
        return None
    if function_name in DISCOVERY_TOOL_NAMES:
        return function_name
    if is_inspection_only_incremental_check(function_name, args):
        return LEAN_INCREMENTAL_INSPECTION_TOOL_NAME
    return None


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


def blocked_construction_source_result(
    *,
    function_name: str,
    tracker: Mapping[str, Any],
    target_symbol: str,
    active_file: str,
    current_cycle: int,
) -> dict[str, object] | None:
    """Reject source inspection after one construction window is exhausted.

    The threshold-producing read must return to the model so it can synthesize
    from that result. Only later inspection requests in the same orchestration
    cycle are rejected; constructive checks and edits remain available.
    """
    if function_name not in SOURCE_INSPECTION_TOOL_NAMES:
        return None
    if not bool(tracker.get("construction_source_inspection_boundary")):
        return None
    stored_cycle = tracker.get("construction_source_inspection_cycle")
    if stored_cycle is None or int(stored_cycle) != int(current_cycle):
        return None
    return {
        "success": False,
        "status": "construction_synthesis_required",
        "blocked_tool": function_name,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "source_inspection_count": int(tracker.get("construction_source_inspection_count", 0) or 0),
        "provider_called": False,
        "required_action": (
            "Use the source declarations already returned to make or check a concrete "
            "proof edit. Further source inspection is reserved until orchestration advances."
        ),
        "allowed_actions": [
            "make a proof edit",
            "check a concrete Lean candidate",
            "decompose the target into explicit helper lemmas",
            "respond without a tool call with the concrete construction",
        ],
        "reason": (
            "This construction cycle already received its bounded local source window. "
            "The threshold read completed successfully and must now be synthesized."
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
    if function_name == "lean_lemma_suggest":
        return " ".join(
            part
            for part in (
                f"file={arguments.get('file_path', '')}",
                f"theorem={arguments.get('theorem_id', '')}",
            )
            if not part.endswith("=")
        )
    if function_name == LEAN_INCREMENTAL_INSPECTION_TOOL_NAME:
        replacement = str(arguments.get("replacement", "") or "")
        normalized = " ".join(replacement.split())
        return normalized[:1000]
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
    elif function_name == LEAN_INCREMENTAL_INSPECTION_TOOL_NAME:
        material = "|".join(
            (
                function_name,
                " ".join(str(arguments.get("replacement", "") or "").split()),
            )
        )
    elif function_name in {"lean_axioms", "lean_lemma_suggest"}:
        material = "|".join(
            (
                function_name,
                str(arguments.get("file_path", "") or "").strip(),
                str(arguments.get("theorem_id", "") or arguments.get("target", "") or "").strip(),
            )
        )
    else:
        return ""
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:16]


def duplicate_lemma_suggest_result(
    tracker: Mapping[str, Any],
    *,
    args: Mapping[str, Any] | None,
    current_cycle: int,
    target_symbol: str,
    active_file: str,
) -> dict[str, Any] | None:
    """Reject an unchanged theorem-scoped lemma search already returned this cycle."""
    fingerprint = source_inspection_fingerprint("lean_lemma_suggest", args)
    if not fingerprint:
        return None
    construction_duplicate = bool(
        int(tracker.get("construction_source_inspection_cycle", -1) or -1) == int(current_cycle)
        and str(tracker.get("construction_source_inspection_last_fingerprint", "") or "")
        == fingerprint
    )
    description = request_description("lean_lemma_suggest", args)
    ordinary_duplicate = bool(
        str(tracker.get("last_request_fingerprint", "") or "")
        == f"lean_lemma_suggest:{description}"
    )
    if not construction_duplicate and not ordinary_duplicate:
        return None
    return {
        "success": False,
        "status": "duplicate_lemma_suggest_blocked",
        "provider_called": False,
        "target_symbol": target_symbol,
        "active_file": active_file,
        "request": description,
        "reason": (
            "The same theorem-scoped lemma search already completed against the "
            "unchanged assignment."
        ),
        "required_action": (
            "Use the candidate list already returned, inspect a specific candidate, "
            "or make and check a concrete proof edit."
        ),
    }


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
        updated.pop("construction_source_inspection_boundary", None)
        updated.pop("construction_synthesis_rejection_count", None)
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
