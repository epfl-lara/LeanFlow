"""The struggle-triggered LLM-manager (nudger) — Phase 2, specs §2.

Advisory-only and message-shaping-only by construction (locked decision #2):
the deterministic kernel gate has already judged the attempt before this
module is consulted, and nothing here can touch that verdict. The nudger
picks ONE next action and writes an optimistic-but-strict paragraph for the
prover; feasibility actions are proposed deterministically elsewhere (the
nudger may only SUGGEST a dispatch, never launch one), and ``stop`` without
a report note is rejected as unusable (never-silent, N1).

Modes via ``LEANFLOW_MANAGER_LLM_MODE``: ``off`` (default — the gate stays
byte-identical), ``dark`` (log-only dark launch into
``summary.json.manager_nudges`` + a ``manager-nudge`` activity event), and
``live`` (the message is appended to the manager feedback as clearly
delimited advisory guidance). Provider routing reuses the existing
``auxiliary.manager_nudge.*`` config family — no new plumbing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.utils import atomic_json_write
from leanflow_cli.native.native_utils import _extract_json_payload, _single_line
from leanflow_cli.workflows.struggle_signals import StruggleReport
from leanflow_cli.workflows.verification_providers import run_model_verification_review
from leanflow_cli.workflows.workflow_json_io import read_json_file
from leanflow_cli.workflows.workflow_state import append_workflow_activity
from leanflow_cli.workflows.workflow_state_paths import workflow_state_root

NUDGE_TASK = "manager_nudge"
NUDGE_ACTIONS = ("continue", "replan", "redraft", "falsify", "dispatch", "stop")
NUDGE_LOG_CAP = 50

_SYSTEM_PROMPT = (
    "You are LeanFlow's proving manager — optimistic, strict, and concrete. A "
    "deterministic Lean kernel gate has already judged this attempt; you can NEVER "
    "change that verdict. Your only job: pick ONE next action and write a short, "
    "energizing, concrete instruction for the prover. Difficulty is a routing "
    "signal, never a terminal state. Do not use give-up language. Actions: "
    '"continue" (stop searching, commit to a concrete proof attempt now), '
    '"replan" (step back, list sub-goals, pick the easiest), '
    '"redraft" (the current proof shape is dead; start a different shape), '
    '"falsify" (suspect the statement; recommend a negation probe), '
    '"dispatch" (suggest a sub-job: an empirical check or a literature/mathlib '
    "search — you may only SUGGEST, never launch), "
    '"stop" (only when every alternative above is exhausted; you MUST include '
    "report_note summarizing what was tried and learned — silence is forbidden). "
    'Reply with strict JSON only: {"action": ..., "message": ..., "rationale": ..., '
    '"confidence": 0.0-1.0, "report_note": ...}.'
)


@dataclass(frozen=True)
class NudgeResult:
    action: str
    message: str  # the optimistic-but-strict paragraph handed to the prover (live mode)
    rationale: str
    confidence: float
    raw_status: str  # provider status from VerificationReviewResult
    report_note: str = ""

    def is_usable(self) -> bool:
        if self.action not in NUDGE_ACTIONS or not self.message.strip():
            return False
        if self.action == "stop" and not self.report_note.strip():
            # Never-silent: a stop without an account of what was tried is refused.
            return False
        return True

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "message": self.message,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "raw_status": self.raw_status,
            "report_note": self.report_note,
        }


def nudge_mode() -> str:
    """Resolve LEANFLOW_MANAGER_LLM_MODE ∈ {off, dark, live}; default off."""
    raw = str(os.getenv("LEANFLOW_MANAGER_LLM_MODE", "") or "").strip().lower()
    if raw in {"off", "dark", "live"}:
        return raw
    legacy = str(os.getenv("LEANFLOW_MANAGER_LLM_ENABLED", "") or "").strip().lower()
    if legacy in {"1", "true", "yes", "on"}:
        return "live"
    return "off"


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
        f"Kernel-verified helpers already banked: "
        f"{', '.join(str(name) for name in packet.get('proved_helpers', []) or []) or '[none]'}",
        "",
        "Pick ONE action and reply with the strict JSON object only.",
    ]
    return _SYSTEM_PROMPT, "\n".join(lines)


def request_nudge(
    report: StruggleReport,
    packet: Mapping[str, Any],
    *,
    timeout_s: int = 45,
    max_tokens: int = 600,
) -> NudgeResult | None:
    """Call the manager-nudge review task; None on any failure (fail-open).

    The packet is consumed read-only; callers pass a copy so the LLM path
    can never mutate gate state.
    """
    system_prompt, user_prompt = build_nudge_prompt(report, packet)
    try:
        result = run_model_verification_review(
            provider="auto",
            task=NUDGE_TASK,
            prompt=user_prompt,
            system_prompt=system_prompt,
            timeout_s=timeout_s,
            max_tokens=max_tokens,
        )
    except Exception:
        return None
    if result.status != "ok" or not str(result.response or "").strip():
        return None
    payload = _extract_json_payload(result.response)
    if not isinstance(payload, Mapping):
        return None
    try:
        confidence = float(payload.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    nudge = NudgeResult(
        action=str(payload.get("action", "") or "").strip().lower(),
        message=str(payload.get("message", "") or "").strip(),
        rationale=str(payload.get("rationale", "") or "").strip(),
        confidence=max(0.0, min(1.0, confidence)),
        raw_status=result.status,
        report_note=str(payload.get("report_note", "") or "").strip(),
    )
    return nudge if nudge.is_usable() else None


def record_nudge(
    result: NudgeResult | None,
    report: StruggleReport,
    *,
    applied: bool,
    mode: str,
    target_symbol: str = "",
    active_file: str = "",
) -> None:
    """Dark-launch log: append to summary.json['manager_nudges'] (cap 50) + activity.

    Writes the Phase-2-owned summary key directly (read + atomic write) so
    the log works regardless of the plan-state flag; Phase 1 owns the other
    keys and both writers preserve each other's content.
    """
    entry = {
        "timestamp": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "theorem": target_symbol,
        "file": active_file,
        **report.to_payload(),
        "mode": mode,
        "applied": applied,
        "nudge": result.to_payload() if result is not None else None,
    }
    try:
        summary_path = workflow_state_root() / "summary.json"
        summary = read_json_file(summary_path)
        nudges = [
            dict(existing)
            for existing in (summary.get("manager_nudges") or [])
            if isinstance(existing, Mapping)
        ]
        nudges.append(entry)
        summary["manager_nudges"] = nudges[-NUDGE_LOG_CAP:]
        atomic_json_write(summary_path, summary, sort_keys=True)
    except Exception:
        # The activity event below is the fallback record; never raise.
        pass
    append_workflow_activity("manager-nudge", "Manager nudge evaluated", **entry)
