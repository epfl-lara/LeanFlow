"""Build persistence-coach messages after rejected Lean proof turns.

The deterministic queue manager and Lean kernel have already judged the
attempt before this module is consulted. The coach can only acknowledge
verified progress and encourage the prover to execute the route assigned by
the orchestrator; it cannot select a route, launch work, change a verdict, or
stop a campaign.

``LEANFLOW_MANAGER_LLM_MODE`` controls only the model call: ``off`` uses the
deterministic fallback, ``dark`` logs the model result while still applying
the fallback, and ``live`` applies the model result when usable. Prove and
autoprove default to ``live``. Provider routing continues to use the existing
``auxiliary.manager_nudge.*`` configuration family. The message-only model
call has a short, hard-bounded wall-clock budget; expiry immediately selects
the deterministic coach so it cannot materially stall the proof loop.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from leanflow_cli.native.native_utils import _extract_json_payload, _single_line
from leanflow_cli.workflows import orchestrator_llm_circuit, research_mode
from leanflow_cli.workflows.struggle_signals import StruggleReport
from leanflow_cli.workflows.verification_providers import run_model_verification_review
from leanflow_cli.workflows.workflow_json_io import update_json_file
from leanflow_cli.workflows.workflow_state import append_workflow_activity
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

NUDGE_TASK = "manager_nudge"
COACH_COMMITMENTS = ("continue_current_route", "execute_assigned_route")
NUDGE_LOG_CAP = 50
MAX_COACH_MESSAGE_CHARS = 360
MANAGER_NUDGE_TIMEOUT_DEFAULT_S = 5
MANAGER_NUDGE_TIMEOUT_MIN_S = 1
MANAGER_NUDGE_TIMEOUT_MAX_S = 10

logger = logging.getLogger(__name__)

_SURRENDER_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnot solved\b",
        r"\bgiv(?:e|es|en|ing) up\b",
        r"\b(?:cannot|can't|unable to) (?:continue|proceed|solve|complete|finish)\b",
        r"\bdecid(?:e|ed|ing) to (?:halt|stop)\b",
        r"\b(?:halt|stop)(?:ping)? further attempts\b",
        r"\bno (?:viable|productive) (?:path|route|approach) remains\b",
    )
)

_MODEL_PROOF_STATE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Model prose is intentionally barred from interpreting proof state.
        # Verified helpers are appended from deterministic state below, so the
        # coach loses nothing by staying at the encouragement layer.
        r"\b(?:lean|kernel|sorry|proof(?:[- ]state)?|scaffold|shape|candidate|replacement|declaration|lemma|theorem|subgoal|goal|obligation|source|edit)\b",
        r"\b(?:compil(?:e|es|ed|ing)|elaborat(?:e|es|ed|ing)|type[- ]?check(?:s|ed|ing)?|verif(?:y|ies|ied|ying)|prov(?:e|es|ed|ing)|clos(?:e|es|ed|ing)|solv(?:e|es|ed|ing))\b",
        r"\b(?:progress|headway|advance(?:d|ment)?|narrow(?:ed|ing)?|clarif(?:y|ies|ied|ying)|ruled out|establish(?:es|ed|ing)?|confirm(?:s|ed|ing)?|validat(?:e|es|ed|ing))\b",
        r"\b(?:first|second|third|another|this|the|an|\d+(?:st|nd|rd|th)?)\s+(?:proof\s+)?attempt\b.{0,48}\b(?:logged|recorded|checked|tested|compiled|elaborated|narrowed)\b",
        r"\bstarting line\b",
    )
)

_STRATEGY_SELECTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # The live Erdős 242 campaign produced "mirror the eq_two/eq_four
        # proof shape" despite the prompt's advisory-only contract.
        r"\b(?:mirror|copy|adapt|reuse)\b.{0,96}\b(?:proof|argument|construction|pattern|case|lemma|theorem|toolkit)\b",
        r"\b(?:switch|pivot|reroute)\b.{0,64}\b(?:route|strategy|approach|proof|job|search|decomposition|negation)\b",
        r"\b(?:try|use|apply|invoke|launch|run|explore|search|decompose|negate|construct|derive)\b.{0,80}\b(?:tactic|proof shape|witness|identity|lemma|theorem|decomposition|negation|search|job|worker)\b",
        r"\b(?:next|best|strongest|preferred)\b.{0,64}\b(?:route|strategy|approach|proof shape|job|search)\b",
        r"\b(?:simp|omega|linarith|ring|field_simp|norm_num|aesop)\b",
    )
)

_SYSTEM_PROMPT = (
    "You are LeanFlow's persistence coach — optimistic, warm, and concrete. A "
    "deterministic Lean kernel gate has already judged this attempt; you can NEVER "
    "change that verdict. Your only job is to encourage the prover to keep executing "
    "the route assigned by the orchestrator. If deterministic routing has requested "
    "a refresh, encourage the prover to follow the next assigned route instead of "
    "continuing the exhausted one. Difficulty is useful evidence, never permission to stop. Do not "
    "choose a strategy, propose a job, claim success, or use surrender language. "
    "Deterministic code appends any verified proof progress separately. Your message "
    "must not mention or infer Lean or kernel state, proof artifacts, compilation, "
    "verification, solved goals, logged attempts, or proof progress. "
    "Do not elaborate the assigned route: never name a tactic, proof shape, sibling "
    "theorem, helper to use, search, decomposition, negation, witness, job, or concrete "
    "next step. Refer to it only as 'the assigned route'. "
    f"Keep the message at or below {MAX_COACH_MESSAGE_CHARS} characters so it stays "
    "brief enough for the active prover context. "
    'Reply with strict JSON only: {"message": "...", '
    '"commitment": "continue_current_route|execute_assigned_route"}.'
)


@dataclass(frozen=True)
class NudgeResult:
    """Return one message-only persistence-coach response."""

    message: str
    progress_acknowledged: tuple[str, ...]
    commitment: str
    raw_status: str

    def is_usable(self) -> bool:
        if (
            self.commitment not in COACH_COMMITMENTS
            or not self.message.strip()
            or len(self.message) > MAX_COACH_MESSAGE_CHARS
        ):
            return False
        if contains_surrender_language(self.message):
            return False
        return True

    def to_payload(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "progress_acknowledged": list(self.progress_acknowledged),
            "commitment": self.commitment,
            "raw_status": self.raw_status,
        }


def contains_surrender_language(text: str) -> bool:
    """Return whether text contains an affirmative proof-surrender phrase."""
    normalized = str(text or "").strip()
    return any(pattern.search(normalized) for pattern in _SURRENDER_PATTERNS)


def claims_model_owned_proof_state(text: str) -> bool:
    """Reject model prose that interprets Lean or proof progress.

    The deterministic acknowledgement field is the only proof-progress
    authority. Applying the same restriction when helpers exist prevents one
    unrelated verified fact from licensing invented target progress.
    """
    normalized = str(text or "").strip()
    return any(pattern.search(normalized) for pattern in _MODEL_PROOF_STATE_PATTERNS)


def selects_strategy_language(text: str) -> bool:
    """Return whether coach text prescribes a route, proof shape, tactic, or job."""
    normalized = str(text or "").strip()
    return any(pattern.search(normalized) for pattern in _STRATEGY_SELECTION_PATTERNS)


def nudge_mode() -> str:
    """Resolve the model-call mode, defaulting prove/autoprove to live."""
    raw = str(os.getenv("LEANFLOW_MANAGER_LLM_MODE", "") or "").strip().lower()
    if raw in {"off", "dark", "live"}:
        return raw
    legacy = str(os.getenv("LEANFLOW_MANAGER_LLM_ENABLED", "") or "").strip().lower()
    if legacy in {"1", "true", "yes", "on"}:
        return "live"
    workflow_kind = str(os.getenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "") or "").strip().lower()
    if workflow_kind in {"prove", "autoprove"}:
        return "live"
    return "off"


def _bounded_timeout_s(value: Any, *, default: int) -> int:
    """Return a short timeout inside the coach's hard wall-clock bounds."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(MANAGER_NUDGE_TIMEOUT_MIN_S, min(parsed, MANAGER_NUDGE_TIMEOUT_MAX_S))


def manager_nudge_timeout_s() -> int:
    """Return the configured message-only coach deadline, capped at ten seconds."""
    return _bounded_timeout_s(
        os.getenv("LEANFLOW_MANAGER_NUDGE_TIMEOUT_S"),
        default=MANAGER_NUDGE_TIMEOUT_DEFAULT_S,
    )


def _kernel_owned_acknowledgements(packet: Mapping[str, Any]) -> tuple[str, ...]:
    """Return bounded deterministic acknowledgements without upgrading evidence.

    Evidence-only helpers are named before proof-support helpers so a newly
    verified probe cannot be hidden behind an older helper-bank cap. Its label
    preserves the unresolved target verdict explicitly.
    """
    helpers = tuple(
        dict.fromkeys(
            str(name).strip()
            for name in packet.get("proved_helpers", []) or []
            if str(name).strip()
        )
    )
    evidence = tuple(
        dict.fromkeys(
            str(name).strip()
            for name in packet.get("verified_evidence", []) or []
            if str(name).strip() and str(name).strip() not in helpers
        )
    )
    evidence_labels = tuple(
        f"{name} (kernel-verified evidence only; assigned target remains unresolved)"
        for name in evidence
    )
    return (*evidence_labels, *helpers)


def fallback_nudge(packet: Mapping[str, Any]) -> NudgeResult:
    """Return the deterministic coach message used when no model result is usable."""
    helpers = tuple(str(name) for name in packet.get("proved_helpers", []) or [] if str(name))
    evidence = tuple(str(name) for name in packet.get("verified_evidence", []) or [] if str(name))
    assigned_route = str(packet.get("assigned_route", "") or "current route").strip()
    if helpers:
        progress_action = "Bank the kernel-verified progress"
    elif evidence:
        progress_action = (
            "Retain the kernel-verified evidence without treating it as target progress"
        )
    else:
        progress_action = "Preserve the effort and diagnostic evidence from this turn"
    reroute_requested = bool(packet.get("reroute_requested"))
    if reroute_requested:
        route_action = "follow the orchestrator's next assigned route"
        commitment = "execute_assigned_route"
    else:
        route_action = f"follow the assigned {assigned_route}"
        commitment = "continue_current_route"
    message = (
        f"This rejection is evidence, not an ending. {progress_action}, {route_action}, "
        "and make the next distinct Lean-checked attempt."
    )
    return NudgeResult(
        message=message,
        progress_acknowledged=_kernel_owned_acknowledgements(packet),
        commitment=commitment,
        raw_status="fallback",
    )


def build_nudge_prompt(report: StruggleReport, packet: Mapping[str, Any]) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for the manager-nudge review call.

    The user prompt is bounded (~2.5 kB): identity, recent attempt reasons,
    the gate's output head, the fired signals, and the remaining budget.
    """
    attempts = [entry for entry in list(packet.get("attempts") or []) if isinstance(entry, Mapping)]
    attempt_lines = [
        f"- attempt {entry.get('attempt', '?')}: "
        f"{_single_line(str(entry.get('reason', '') or '[no reason]'), 200)}"
        for entry in attempts[-3:]
    ]
    signal_lines = [
        f"- {signal['kind']} [{signal['severity']}]: {signal['evidence']}"
        for signal in report.to_payload()["signals"]
    ]
    verified_helper_count = len(
        [name for name in packet.get("proved_helpers", []) or [] if str(name).strip()]
    )
    verified_evidence_count = len(
        [name for name in packet.get("verified_evidence", []) or [] if str(name).strip()]
    )
    lines = [
        f"Theorem: {packet.get('target_symbol', '?')} ({packet.get('active_file', '?')})",
        f"Attempts so far: {len(attempts)}",
        "Recent failed attempts:",
        *(attempt_lines or ["- [none recorded]"]),
        f"Gate feedback kind: {packet.get('feedback_kind', '') or '[none]'}",
        f"Gate output (head): {_single_line(str(packet.get('gate_output', '') or ''), 700)}",
        "Fired struggle signals:",
        *(signal_lines or ["- [none]"]),
        f"Turn budget: {packet.get('api_calls', 0)}/{packet.get('max_iterations', 0)} steps used",
        f"Kernel-verified proof-support helper count already banked: {verified_helper_count}",
        f"Kernel-verified evidence-only helper count already banked: {verified_evidence_count}",
        (
            "The deterministic manager will append the banked verified helpers and label "
            "evidence-only facts without upgrading the unresolved target. Do not mention, "
            "count, name, or interpret those facts in your message, and do not call this "
            "the starting line."
            if verified_helper_count or verified_evidence_count
            else "No kernel-verified proof progress is available for this turn. An "
            "unchanged `sorry` only marks unresolved source; it is not evidence that a "
            "new candidate, scaffold, proof shape, or attempt compiled."
        ),
        "",
        (
            "Deterministic routing has requested a route refresh. Encourage continued work "
            "through the orchestrator's next assigned route; do not encourage the exhausted route."
            if report.severity.value == "reroute"
            else "The orchestrator has already assigned a route. Do not name, restate, or elaborate it."
        ),
        "Write only positive encouragement to execute the assigned route. You may call "
        "the rejection useful evidence or acknowledge effort and a recorded blocker, but "
        "make no Lean or proof-state assertion. Reply with strict JSON only.",
    ]
    return _SYSTEM_PROMPT, "\n".join(lines)


def request_nudge(
    report: StruggleReport,
    packet: Mapping[str, Any],
    *,
    timeout_s: int | None = None,
    max_tokens: int = 600,
) -> NudgeResult | None:
    """Call the model coach and return ``None`` for deterministic fallback.

    The packet is consumed read-only; callers pass a copy so the LLM path
    can never mutate gate state. Provider, schema, and language failures are
    model-path failures only; the caller still emits the fallback coach.
    """
    circuit_active = research_mode.research_mode_enabled()
    if circuit_active and not orchestrator_llm_circuit.request_allowed(task=NUDGE_TASK):
        circuit = orchestrator_llm_circuit.circuit_snapshot()
        try:
            append_workflow_activity(
                "verification-review-skipped",
                "Persistence coach model review skipped while the shared advisory circuit is open",
                task=NUDGE_TASK,
                status="circuit_open",
                open_until=str(circuit.get("open_until", "") or ""),
                campaign_id=str(circuit.get("campaign_id", "") or ""),
                deterministic_fallback=True,
            )
        except Exception:
            logger.debug("Failed to record persistence-coach circuit skip", exc_info=True)
        return None
    system_prompt, user_prompt = build_nudge_prompt(report, packet)
    effective_timeout_s = (
        manager_nudge_timeout_s()
        if timeout_s is None
        else _bounded_timeout_s(timeout_s, default=MANAGER_NUDGE_TIMEOUT_DEFAULT_S)
    )
    try:
        result = run_model_verification_review(
            provider="auto",
            task=NUDGE_TASK,
            prompt=user_prompt,
            system_prompt=system_prompt,
            timeout_s=effective_timeout_s,
            max_tokens=max_tokens,
        )
    except Exception as exc:
        if circuit_active:
            orchestrator_llm_circuit.record_provider_failure(
                "error",
                error=f"{type(exc).__name__}: {exc}",
                task=NUDGE_TASK,
            )
        return None
    status = str(getattr(result, "status", "") or "").strip().lower()
    if circuit_active:
        circuit_details = {
            "provider": str(getattr(result, "provider", "") or ""),
            "model": str(getattr(result, "model", "") or ""),
            "error": str(getattr(result, "error", "") or ""),
            "task": NUDGE_TASK,
        }
        if status == "timeout" or bool(getattr(result, "timed_out", False)):
            orchestrator_llm_circuit.record_timeout(**circuit_details)
        elif status in {"error", "unavailable"}:
            orchestrator_llm_circuit.record_provider_failure(status, **circuit_details)
        elif status:
            # ``no_answer`` and schema failures are unusable coach replies,
            # but they prove that the provider connection itself recovered.
            orchestrator_llm_circuit.record_success(task=NUDGE_TASK)
    if status != "ok" or not str(result.response or "").strip():
        return None
    payload = _extract_json_payload(result.response)
    if not isinstance(payload, Mapping):
        return None
    nudge = NudgeResult(
        message=str(payload.get("message", "") or "").strip(),
        # Verified progress is a kernel-owned fact and is never copied from
        # model output. Keeping it out of the model schema also prevents long
        # declaration names from consuming the coach's small response budget.
        progress_acknowledged=_kernel_owned_acknowledgements(packet),
        commitment=str(payload.get("commitment", "") or "").strip().lower(),
        raw_status=result.status,
    )
    if (
        not nudge.is_usable()
        or claims_model_owned_proof_state(nudge.message)
        or selects_strategy_language(nudge.message)
    ):
        return None
    return nudge


def record_nudge(
    result: NudgeResult | None,
    report: StruggleReport,
    *,
    applied: bool,
    mode: str,
    target_symbol: str = "",
    active_file: str = "",
    coverage_key: str = "",
    gate_verdict: str = "",
    fallback_used: bool = False,
) -> None:
    """Record one auditable rejected-turn coach application.

    ``applied`` means the model response itself was applied. The deterministic
    fallback still counts as coach coverage when the model is disabled,
    malformed, surrendering, or unavailable. Summary and activity persistence
    are independent best-effort sinks and never raise into the proof loop.
    """
    entry = {
        "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "theorem": target_symbol,
        "file": active_file,
        **report.to_payload(),
        "mode": mode,
        "applied": applied,
        "coach_applied": True,
        "fallback_used": fallback_used,
        "coverage_key": coverage_key,
        "gate_verdict": gate_verdict,
        "nudge": result.to_payload() if result is not None else None,
    }
    try:

        def mutate(summary: dict[str, Any]) -> None:
            nudges = [
                dict(existing)
                for existing in (summary.get("manager_nudges") or [])
                if isinstance(existing, Mapping)
            ]
            nudges.append(entry)
            summary["manager_nudges"] = nudges[-NUDGE_LOG_CAP:]
            metrics = dict(summary.get("campaign_metrics") or {})
            metrics["rejected_turns"] = int(metrics.get("rejected_turns", 0) or 0) + 1
            metrics["coach_messages"] = int(metrics.get("coach_messages", 0) or 0) + 1
            if fallback_used:
                metrics["coach_fallbacks"] = int(metrics.get("coach_fallbacks", 0) or 0) + 1
            summary["campaign_metrics"] = metrics

        update_json_file(workflow_state_root() / "summary.json", mutate)
    except Exception:
        # The activity event below is the fallback record; never raise.
        pass
    try:
        append_workflow_activity("manager-nudge", "Manager nudge evaluated", **entry)
    except Exception:
        # Coaching is correctness-neutral but coverage-critical. A secondary
        # observability sink must never suppress the already-selected message.
        pass
