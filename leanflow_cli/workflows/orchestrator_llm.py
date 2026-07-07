"""LLM routing layer over the deterministic orchestrator floor (specs §4.4).

Phase 4 ships the PLUMBING only: prompt composition, a fence-tolerant JSON
decision parser, and the upgrade-only guard. The enable flag
(``LEANFLOW_ORCHESTRATOR_LLM_ENABLED``) flips in Phase 6 after the Phase E
gate; until then no production call happens.

Non-negotiables: the LLM may REFINE the floor's route but never downgrade a
working route to park/escalate (upgrade-only rule — a park/escalate answer
against a non-park floor is logged and ignored); a PROTECTED floor route
(park/escalate/ask-human) is LLM-immutable — escalate encodes kernel-proved
negation evidence, ask-human a fidelity integrity stop only a human may
clear — so the consult is skipped entirely; any parse failure falls back to
the floor; nothing here can reach the kernel gate.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from typing import Any

from leanflow_cli.workflows.orchestrator import ROUTES, OrchestratorRoute, RouteContext
from leanflow_cli.workflows.verification_providers import run_model_verification_review

logger = logging.getLogger(__name__)

ORCHESTRATION_TASK = "orchestration"

#: Routes the LLM may never introduce against a non-terminal floor
#: (the upgrade-only rule: no giving up on a working route).
_TERMINAL_ROUTES = frozenset({"park", "escalate"})

#: Floor routes the LLM may never override — the consult is skipped.
#: park carries a decision packet, escalate kernel-proved negation
#: evidence, ask-human a fidelity stop only a human may clear.
_PROTECTED_FLOOR_ROUTES = _TERMINAL_ROUTES | {"ask-human"}

#: The §4.4 decision vocabulary. ``ask-human`` is deliberately absent: it is
#: the runtime's own conversion (fail-closed ACK gate), never an LLM choice.
_LLM_ROUTES = frozenset(ROUTES) - {"ask-human"}

_SYSTEM_PROMPT = (
    "You are the orchestrator of an autonomous Lean 4 proving harness for hard, "
    "possibly open problems. The deterministic Lean kernel gate is the sole "
    "authority on correctness and is never yours to override. Difficulty is a "
    "routing signal, never a terminal state: every scope must end in a "
    "kernel-verified proof, a kernel-verified refutation, or a parked node with "
    "a complete decision packet. Silent surrender is a protocol violation. "
    "Prefer decomposition, feasibility probes, and re-planning over repetition."
)


def orchestrator_llm_enabled() -> bool:
    raw = str(os.getenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def build_llm_prompt(
    ctx: RouteContext,
    floor_route: OrchestratorRoute,
    *,
    plan_md_text: str = "",
) -> tuple[str, str]:
    """Compose (system, user) prompts. Research mode is context-rich: the
    full plan.md rides along (N5 — token cost is not the constraint there);
    easy runs get frontier + packet only."""
    packet = dict(ctx.decision_packet or {})
    lines = [
        f"Trigger: {ctx.trigger} | workflow: {ctx.workflow_kind}",
        f"Target: `{ctx.target_symbol}` in {ctx.active_file or '[none]'}",
        f"Attempts: {ctx.attempt_count} | hard retries: {ctx.hard_retries} | "
        f"search exhausted: {ctx.search_exhausted}",
        f"Queue: {ctx.declaration_queue_total} items, {ctx.pending_count} pending, "
        f"{ctx.project_sorry_count} project sorries",
        f"Graph frontier: {', '.join(ctx.graph_frontier) or '[empty]'}",
        f"Blocked nodes: {', '.join(ctx.graph_blocked) or '[none]'}",
        f"Negation status: {ctx.negation_status or 'none'} | "
        f"negation proved: {ctx.negation_proved}",
        f"Fidelity suspect: {ctx.fidelity_suspect}",
        "",
        f"Deterministic floor proposes: {floor_route.route} — {floor_route.reason}",
    ]
    if ctx.diagnostics:
        lines += ["", "Diagnostics (truncated):", ctx.diagnostics[:1200]]
    if packet:
        lines += ["", "Decision packet:", json.dumps(packet, ensure_ascii=False, sort_keys=True)]
    if plan_md_text and ctx.research_mode:
        lines += ["", "plan.md (full, research mode):", plan_md_text]
    lines += [
        "",
        "Decide the route. Reply with ONE JSON object only:",
        '{"route": "direct-prove|decompose|plan|negate|park|re-state|escalate",',
        ' "reason": "...",',
        ' "target_node": "...",',
        ' "statements_to_state": [{"name": "...", "file": "...", "statement": "..."}],',
        ' "probes": [{"archetype": "negation|empirical|deep-search", "objective": "..."}]}',
        "Rules: never choose park or escalate unless the floor already proposed it;",
        "prefer a strategy CHANGE over repeating the failed approach.",
    ]
    return _SYSTEM_PROMPT, "\n".join(lines)


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_llm_decision(text: str) -> dict[str, Any] | None:
    """Fence-tolerant, strict-vocabulary decision parser; None on any doubt."""
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [match.group(1) for match in _JSON_FENCE_RE.finditer(raw)]
    if not candidates:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            candidates = [raw[start : end + 1]]
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        route = str(payload.get("route", "") or "").strip()
        if route not in _LLM_ROUTES:
            continue
        return {
            "route": route,
            "reason": str(payload.get("reason", "") or "")[:500],
            "target_node": str(payload.get("target_node", "") or ""),
            "statements_to_state": _mapping_entries(payload.get("statements_to_state")),
            "probes": _mapping_entries(payload.get("probes")),
        }
    return None


def _mapping_entries(value: Any) -> list[dict[str, Any]]:
    """Total list-of-mappings coercion: any non-list shape is just []."""
    if not isinstance(value, list):
        return []
    return [dict(entry) for entry in value if isinstance(entry, Mapping)]


def llm_route(
    ctx: RouteContext,
    floor_route: OrchestratorRoute,
    *,
    plan_md_text: str = "",
    timeout_s: int = 300,
) -> tuple[OrchestratorRoute | None, str]:
    """One LLM routing turn; (route, note) — route None means keep the floor.

    Notes: '' on success, 'floor-protected' / 'parse-failure' /
    'llm-downgrade-rejected' / 'unavailable' when the floor stays
    authoritative. Never raises.
    """
    if not orchestrator_llm_enabled():
        return None, ""
    if floor_route.route in _PROTECTED_FLOOR_ROUTES:
        return None, "floor-protected"
    try:
        system_prompt, user_prompt = build_llm_prompt(ctx, floor_route, plan_md_text=plan_md_text)
        result = run_model_verification_review(
            provider="auto",
            task=ORCHESTRATION_TASK,
            prompt=user_prompt,
            system_prompt=system_prompt,
            timeout_s=timeout_s,
            max_tokens=2000,
        )
        status = str(getattr(result, "status", "") or "").strip().lower()
        if status and status != "ok":
            # The provider layer swallows its own failures into status
            # ('unavailable'/'error'/'no_answer') — never parse those.
            return None, "unavailable"
        response = str(getattr(result, "response", "") or "")
        decision = parse_llm_decision(response)
        if decision is None:
            return None, "parse-failure"
        if decision["route"] in _TERMINAL_ROUTES and floor_route.route not in _TERMINAL_ROUTES:
            # Upgrade-only: the LLM may never give up on a working route.
            return None, "llm-downgrade-rejected"
        return (
            OrchestratorRoute(
                route=decision["route"],
                reason=decision["reason"] or "llm routing decision",
                target={
                    "target_symbol": ctx.target_symbol,
                    "active_file": ctx.active_file,
                    "target_node": decision["target_node"],
                    "statements_to_state": decision["statements_to_state"],
                    "probes": decision["probes"],
                },
                source="llm",
            ),
            "",
        )
    except Exception:
        logger.debug("llm routing turn failed", exc_info=True)
        return None, "unavailable"
