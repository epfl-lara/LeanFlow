"""Classify existing ``/prove`` runtime signals into escalation levels.

Callers provide already-recorded attempts, retry signatures, search progress,
stall counters, budgets, and blocker summaries. The classifier performs no
I/O, logging, or routing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

# Thresholds mirror the verified runner sources; keep in sync (tests assert
# equality where a counterpart constant exists).
FAILED_ATTEMPTS_NUDGE = 2  # owner decision D7: nudge after ~2 genuine failures
FAILED_ATTEMPTS_REROUTE = 4
REPEAT_SIGNATURE_NUDGE = 2
# Copies of native_runner.py SEARCH_PROGRESS_REPEAT_NUDGE_LIMIT / _TOTAL_NUDGE_LIMIT
# (layering forbids importing the runner here; test_struggle_signals pins equality).
SEARCH_REPEAT_STREAK_NUDGE = 2
SEARCH_TOTAL_NUDGE = 6
NO_PROGRESS_NUDGE = 2  # below the stall stop limit (4) — nudge BEFORE the stop trips
NO_PROGRESS_REROUTE = 3
BUDGET_PRESSURE_RATIO = 0.7  # same floor as run_agent's budget-caution threshold
COMBINED_NUDGE_KINDS_FOR_REROUTE = 2


class Severity(str, Enum):
    NONE = "none"
    NUDGE = "nudge"
    REROUTE = "reroute"
    BREAKPOINT = "breakpoint"


_SEVERITY_ORDER = {
    Severity.NONE: 0,
    Severity.NUDGE: 1,
    Severity.REROUTE: 2,
    Severity.BREAKPOINT: 3,
}


@dataclass(frozen=True)
class StruggleSignal:
    kind: str  # failed_attempts | repeat_error_signature | search_spiral
    #          | no_progress | budget_pressure | give_up_phrasing
    severity: Severity
    evidence: str  # one human-readable line
    metric: float  # the raw number that fired

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity.value,
            "evidence": self.evidence[:240],
            "metric": self.metric,
        }


@dataclass(frozen=True)
class StruggleContext:
    """Snapshot of already-tracked runner state; assembled by the caller."""

    attempt_count: int = 0
    hard_retry_count: int = 0
    repeated_signature_count: int = 0  # max occurrences of one failure reason (non-deduped)
    search_progress: Mapping[str, Any] | None = None
    stable_cycles: int = 0
    blocked_runs: int = 0
    api_calls: int = 0
    max_iterations: int = 0
    blocker_summary: str = ""


@dataclass(frozen=True)
class StruggleReport:
    signals: tuple[StruggleSignal, ...]
    severity: Severity

    def fired(self) -> bool:
        return self.severity is not Severity.NONE

    def to_payload(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "signals": [signal.to_payload() for signal in self.signals],
        }


def evaluate(ctx: StruggleContext) -> StruggleReport:
    """Classify already-tracked runner state into struggle signals.

    Pure and deterministic. Signals never reach BREAKPOINT here — the
    exhaustion paths map that severity directly.
    """
    signals: list[StruggleSignal] = []

    if ctx.attempt_count >= FAILED_ATTEMPTS_NUDGE:
        severity = (
            Severity.REROUTE if ctx.attempt_count >= FAILED_ATTEMPTS_REROUTE else Severity.NUDGE
        )
        signals.append(
            StruggleSignal(
                kind="failed_attempts",
                severity=severity,
                evidence=f"{ctx.attempt_count} verified failed attempts on this declaration",
                metric=float(ctx.attempt_count),
            )
        )

    if ctx.repeated_signature_count >= REPEAT_SIGNATURE_NUDGE:
        signals.append(
            StruggleSignal(
                kind="repeat_error_signature",
                severity=Severity.NUDGE,
                evidence=(
                    f"the same error signature came back {ctx.repeated_signature_count} times"
                ),
                metric=float(ctx.repeated_signature_count),
            )
        )

    search = dict(ctx.search_progress or {})
    same_query_streak = int(search.get("same_query_streak", 0) or 0)
    search_count = int(search.get("search_count", 0) or 0)
    if same_query_streak >= SEARCH_REPEAT_STREAK_NUDGE or search_count >= SEARCH_TOTAL_NUDGE:
        signals.append(
            StruggleSignal(
                kind="search_spiral",
                severity=Severity.NUDGE,
                evidence=(
                    f"search spiral: {search_count} searches, "
                    f"same-query streak {same_query_streak}"
                ),
                metric=float(max(search_count, same_query_streak)),
            )
        )

    if ctx.stable_cycles >= NO_PROGRESS_NUDGE:
        severity = Severity.REROUTE if ctx.stable_cycles >= NO_PROGRESS_REROUTE else Severity.NUDGE
        signals.append(
            StruggleSignal(
                kind="no_progress",
                severity=severity,
                evidence=f"{ctx.stable_cycles} cycles with an unchanged state signature",
                metric=float(ctx.stable_cycles),
            )
        )

    if ctx.max_iterations > 0 and ctx.api_calls / ctx.max_iterations >= BUDGET_PRESSURE_RATIO:
        ratio = ctx.api_calls / ctx.max_iterations
        signals.append(
            StruggleSignal(
                kind="budget_pressure",
                severity=Severity.NUDGE,
                evidence=f"turn budget {ratio:.0%} spent ({ctx.api_calls}/{ctx.max_iterations})",
                metric=round(ratio, 3),
            )
        )

    if ctx.blocker_summary.strip():
        has_no_progress = any(signal.kind == "no_progress" for signal in signals)
        signals.append(
            StruggleSignal(
                kind="give_up_phrasing",
                severity=Severity.REROUTE if has_no_progress else Severity.NUDGE,
                evidence=f"prover reported a blocker: {ctx.blocker_summary.strip()[:180]}",
                metric=1.0,
            )
        )

    severity = Severity.NONE
    for signal in signals:
        if _SEVERITY_ORDER[signal.severity] > _SEVERITY_ORDER[severity]:
            severity = signal.severity
    # Persistent combinations escalate: >=2 distinct struggling dimensions is
    # a routing matter, not a wording matter.
    nudge_kinds = {signal.kind for signal in signals if signal.severity is Severity.NUDGE}
    if severity is Severity.NUDGE and len(nudge_kinds) >= COMBINED_NUDGE_KINDS_FOR_REROUTE:
        severity = Severity.REROUTE
    return StruggleReport(signals=tuple(signals), severity=severity)
