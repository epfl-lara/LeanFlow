"""Describe observable execution of native orchestrator routes."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

PLANNER_TERMINAL_OBSTACLE_STATE_KEY = "planner_terminal_obstacle"


@dataclass(frozen=True)
class RouteExecution:
    """Return whether one exact-target route performed durable work.

    A route is complete only after its declared evidence kind is persisted.
    Deferred results deliberately leave fresh-epoch and in-flight route tokens
    intact for a later exact-scope retry.
    """

    status: Literal["completed", "deferred"]
    route: str
    target_symbol: str
    active_file: str
    outcome: str = ""
    reason: str = ""
    evidence_kind: str = ""
    explicit_request: bool = False

    @property
    def completed(self) -> bool:
        """Return whether durable exact-target route evidence exists."""
        return self.status == "completed" and bool(self.evidence_kind)

    @property
    def verdict(self) -> str:
        """Return the compatibility name for a negation probe outcome."""
        return self.outcome

    @property
    def probe_recorded(self) -> bool:
        """Return whether the evidence is an exact-target negation probe."""
        return self.evidence_kind == "negation-probe"

    @property
    def promotion_recorded(self) -> bool:
        """Return whether the evidence includes authoritative negation promotion."""
        return self.evidence_kind == "negation-promotion"

    @classmethod
    def deferred(
        cls,
        *,
        route: str,
        target_symbol: str,
        active_file: str,
        reason: str,
        outcome: str = "",
        explicit_request: bool = False,
    ) -> RouteExecution:
        """Build a result that keeps route obligations resumable."""
        return cls(
            status="deferred",
            route=route,
            target_symbol=target_symbol,
            active_file=active_file,
            outcome=outcome,
            reason=reason,
            explicit_request=explicit_request,
        )

    @classmethod
    def recorded(
        cls,
        *,
        route: str,
        target_symbol: str,
        active_file: str,
        outcome: str,
        evidence_kind: str,
        reason: str = "",
        explicit_request: bool = False,
    ) -> RouteExecution:
        """Build a result backed by persisted route evidence."""
        if not str(evidence_kind or "").strip():
            raise ValueError("recorded route execution requires an evidence kind")
        return cls(
            status="completed",
            route=route,
            target_symbol=target_symbol,
            active_file=active_file,
            outcome=outcome,
            reason=reason,
            evidence_kind=evidence_kind,
            explicit_request=explicit_request,
        )

    @classmethod
    def obstacle(
        cls,
        *,
        route: str,
        target_symbol: str,
        active_file: str,
        outcome: str,
        reason: str,
        explicit_request: bool = False,
    ) -> RouteExecution:
        """Complete a route whose exact-scope deterministic action cannot proceed.

        The persisted obstacle is route evidence, not mathematical evidence. It
        retires the selected strategy so a fresh route can run instead of
        replaying the same unsupported action across campaign epochs.
        """
        normalized_route = str(route or "").strip().lower()
        if not normalized_route:
            raise ValueError("route obstacle requires a route")
        return cls.recorded(
            route=normalized_route,
            target_symbol=target_symbol,
            active_file=active_file,
            outcome=outcome,
            reason=reason,
            evidence_kind=f"{normalized_route}-route-obstacle",
            explicit_request=explicit_request,
        )

    @classmethod
    def from_payload(cls, payload: Any) -> RouteExecution | None:
        """Rebuild a validated execution result from process-local state."""
        if not isinstance(payload, dict):
            return None
        status = str(payload.get("status", "") or "")
        route = str(payload.get("route", "") or "").strip().lower()
        target_symbol = str(payload.get("target_symbol", "") or "").strip()
        active_file = str(payload.get("active_file", "") or "").strip()
        evidence_kind = str(payload.get("evidence_kind", "") or "").strip()
        if status not in {"completed", "deferred"} or not route:
            return None
        if status == "completed" and not evidence_kind:
            return None
        normalized_status: Literal["completed", "deferred"] = (
            "completed" if status == "completed" else "deferred"
        )
        return cls(
            status=normalized_status,
            route=route,
            target_symbol=target_symbol,
            active_file=active_file,
            outcome=str(payload.get("outcome", "") or ""),
            reason=str(payload.get("reason", "") or ""),
            evidence_kind=evidence_kind,
            explicit_request=bool(payload.get("explicit_request")),
        )

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-safe route-execution audit payload."""
        return {
            "status": self.status,
            "route": self.route,
            "target_symbol": self.target_symbol,
            "active_file": self.active_file,
            "outcome": self.outcome,
            "reason": self.reason,
            "evidence_kind": self.evidence_kind,
            "explicit_request": self.explicit_request,
            "probe_recorded": self.probe_recorded,
            "promotion_recorded": self.promotion_recorded,
        }


def record_planner_terminal_obstacle(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    target_signature_sha256: str,
    target_declaration_sha256: str = "",
    outcome: str,
    reason: str,
) -> dict[str, str]:
    """Persist a terminal planner obstacle against the assigned declaration.

    Whole-file revisions are deliberately excluded: integrating a helper
    above an unchanged target must not authorize the same expensive planner
    route again.
    """
    payload = {
        "target_symbol": str(target_symbol or "").strip(),
        "active_file": str(active_file or "").strip(),
        "target_signature_sha256": str(target_signature_sha256 or "").strip(),
        "target_declaration_sha256": str(target_declaration_sha256 or "").strip(),
        "outcome": str(outcome or "").strip(),
        "reason": str(reason or "").strip(),
    }
    autonomy_state[PLANNER_TERMINAL_OBSTACLE_STATE_KEY] = payload
    return payload


def planner_terminal_obstacle_blocks_request(
    autonomy_state: Mapping[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    target_signature_sha256: str,
    target_declaration_sha256: str = "",
) -> bool:
    """Return whether an unchanged target must first run a distinct route."""
    raw = autonomy_state.get(PLANNER_TERMINAL_OBSTACLE_STATE_KEY)
    obstacle = raw if isinstance(raw, Mapping) else {}
    signature = str(target_signature_sha256 or "").strip()
    assignment_matches = bool(
        signature
        and str(obstacle.get("target_symbol", "") or "").strip() == str(target_symbol or "").strip()
        and _same_file(obstacle.get("active_file", ""), active_file)
        and str(obstacle.get("target_signature_sha256", "") or "").strip() == signature
    )
    if not assignment_matches:
        return False
    recorded_declaration = str(obstacle.get("target_declaration_sha256", "") or "").strip()
    if not recorded_declaration:
        return True
    current_declaration = str(target_declaration_sha256 or "").strip()
    return not current_declaration or recorded_declaration == current_declaration


def clear_planner_terminal_obstacle_after_target_change(
    autonomy_state: dict[str, Any],
    *,
    target_symbol: str,
    active_file: str,
    target_signature_sha256: str,
    target_declaration_sha256: str = "",
) -> bool:
    """Clear planner cooldown when the assigned declaration materially changes."""
    raw = autonomy_state.get(PLANNER_TERMINAL_OBSTACLE_STATE_KEY)
    obstacle = raw if isinstance(raw, Mapping) else {}
    scope_changed = bool(
        str(obstacle.get("target_symbol", "") or "").strip() != str(target_symbol or "").strip()
        or not _same_file(obstacle.get("active_file", ""), active_file)
    )
    if scope_changed or not str(target_signature_sha256 or "").strip():
        return False
    recorded_declaration = str(obstacle.get("target_declaration_sha256", "") or "").strip()
    if recorded_declaration:
        current_declaration = str(target_declaration_sha256 or "").strip()
        if not current_declaration or recorded_declaration == current_declaration:
            return False
    elif (
        str(obstacle.get("target_signature_sha256", "") or "").strip()
        == str(target_signature_sha256 or "").strip()
    ):
        return False
    autonomy_state.pop(PLANNER_TERMINAL_OBSTACLE_STATE_KEY, None)
    return True


def clear_planner_terminal_obstacle_after_distinct_route(
    autonomy_state: dict[str, Any],
    execution: RouteExecution,
) -> bool:
    """Clear planner cooldown after durable work on another route."""
    if not execution.completed or execution.route == "plan":
        return False
    raw = autonomy_state.get(PLANNER_TERMINAL_OBSTACLE_STATE_KEY)
    obstacle = raw if isinstance(raw, Mapping) else {}
    if str(obstacle.get("outcome", "") or "").strip() in {
        "planner-completed",
        "advisor-completed",
    }:
        # Completed planner advice stays active until the assigned declaration
        # itself changes. Banking another helper or consulting a different
        # route is not evidence that the concrete plan was attempted.
        return False
    if str(
        obstacle.get("target_symbol", "") or ""
    ).strip() != execution.target_symbol or not _same_file(
        obstacle.get("active_file", ""), execution.active_file
    ):
        return False
    autonomy_state.pop(PLANNER_TERMINAL_OBSTACLE_STATE_KEY, None)
    return True


def _activity_time(value: Any) -> datetime | None:
    """Parse one persisted activity timestamp conservatively."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _same_file(left: Any, right: Any) -> bool:
    """Compare activity and selection paths without requiring either to exist."""
    return bool(left and right) and os.path.realpath(str(left)) == os.path.realpath(str(right))


def legacy_completion_from_activity(
    selection: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> RouteExecution | None:
    """Recover strong pre-result route evidence for one pending selection.

    This migration is intentionally narrow: the exact scope and route must
    match, the evidence must postdate selection, and no generic/no-op activity
    is accepted. It exists only for campaigns that executed mechanical work
    before structured route results were persisted.
    """
    token = str(selection.get("token", "") or "").strip()
    route = str(selection.get("route", "") or "").strip().lower()
    target_symbol = str(selection.get("target_symbol", "") or "").strip()
    active_file = str(selection.get("active_file", "") or "").strip()
    selected_at = _activity_time(selection.get("selected_at"))
    if (
        not token
        or route not in {"decompose", "negate", "plan"}
        or not target_symbol
        or not active_file
        or selected_at is None
    ):
        return None
    for event in events:
        event_at = _activity_time(event.get("timestamp"))
        # Historical activity and route selections can both be rounded to a
        # whole second.  Equality is deliberately ambiguous: replaying safe,
        # idempotence-guarded mechanical work is preferable to laundering an
        # event that may have happened just before the selection.
        if event_at is None or event_at <= selected_at:
            continue
        details_value = event.get("details")
        details = details_value if isinstance(details_value, Mapping) else {}
        if str(details.get("target_symbol", "") or "").strip() != target_symbol or not _same_file(
            details.get("active_file", ""), active_file
        ):
            continue
        event_type = str(event.get("type", "") or "")
        event_id = str(event.get("event_id", "") or "")
        if route == "decompose" and event_type == "decomposer":
            placed = tuple(str(name) for name in (details.get("placed") or []) if str(name))
            if details.get("ok") is True and placed:
                return RouteExecution.recorded(
                    route=route,
                    target_symbol=target_symbol,
                    active_file=active_file,
                    outcome=", ".join(placed),
                    reason=f"reconciled legacy activity {event_id or event_type}",
                    evidence_kind="decomposition-helper",
                )
        if route in {"decompose", "plan"} and event_type == "multi-direction":
            if details.get("ok") is True and str(details.get("winner", "") or "").strip():
                return RouteExecution.recorded(
                    route=route,
                    target_symbol=target_symbol,
                    active_file=active_file,
                    outcome=str(details.get("winner", "") or ""),
                    reason=f"reconciled legacy activity {event_id or event_type}",
                    evidence_kind="multi-direction",
                )
        if route == "plan" and event_type == "planner":
            if (
                details.get("ok") is True
                and str(details.get("synthesis_status", "") or "") != "capacity-deferred"
            ):
                return RouteExecution.recorded(
                    route=route,
                    target_symbol=target_symbol,
                    active_file=active_file,
                    outcome=str(details.get("reason", "") or "planner completed"),
                    reason=f"reconciled legacy activity {event_id or event_type}",
                    evidence_kind="planner",
                )
        if route == "negate" and event_type == "negation-probe":
            if details.get("probe_recorded") is True:
                return RouteExecution.recorded(
                    route=route,
                    target_symbol=target_symbol,
                    active_file=active_file,
                    outcome=str(details.get("verdict", "") or "unknown"),
                    reason=f"reconciled legacy activity {event_id or event_type}",
                    evidence_kind="negation-probe",
                )
    return None
