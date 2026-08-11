"""Expose slow synchronous transition work without making fast polls noisy."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any, TypeVar

T = TypeVar("T")

_PORTFOLIO_SIGNATURE_KEY = "_research_portfolio_visible_signature"
_PORTFOLIO_NOTICE_AT_KEY = "_research_portfolio_visible_notice_at"


def run_with_slow_notice(
    operation: Callable[[], T],
    *,
    start_message: str,
    finish_message: Callable[[T, float], str],
    delay_s: float = 5.0,
    emit: Callable[[str], None] = print,
) -> T:
    """Run an operation and announce only when it crosses the slow threshold."""
    started = time.monotonic()
    announced = threading.Event()

    def announce() -> None:
        announced.set()
        emit(start_message)

    timer = threading.Timer(max(0.0, float(delay_s)), announce)
    timer.daemon = True
    timer.start()
    try:
        result = operation()
    finally:
        timer.cancel()
    elapsed = max(0.0, time.monotonic() - started)
    if announced.is_set():
        emit(finish_message(result, elapsed))
    return result


def run_with_heartbeat(
    operation: Callable[[], T],
    *,
    start_message: str,
    heartbeat_message: Callable[[float], str],
    finish_message: Callable[[T, float], str],
    delay_s: float = 5.0,
    heartbeat_s: float = 60.0,
    emit: Callable[[str], None] = print,
) -> T:
    """Run slow synchronous work with a delayed notice and bounded heartbeats."""
    started = time.monotonic()
    stopped = threading.Event()
    announced = threading.Event()

    def report() -> None:
        if stopped.is_set():
            return
        elapsed = max(0.0, time.monotonic() - started)
        if announced.is_set():
            emit(heartbeat_message(elapsed))
        else:
            announced.set()
            emit(start_message)
        timer = threading.Timer(max(1.0, float(heartbeat_s)), report)
        timer.daemon = True
        timer.start()

    first = threading.Timer(max(0.0, float(delay_s)), report)
    first.daemon = True
    first.start()
    try:
        result = operation()
    finally:
        stopped.set()
        first.cancel()
    elapsed = max(0.0, time.monotonic() - started)
    if announced.is_set():
        emit(finish_message(result, elapsed))
    return result


def run_epoch_transition(
    operation: Callable[[], T],
    *,
    target_symbol: str,
    previous_epoch: int,
    reason: str,
    activity_emit: Callable[..., Any],
    run_log_emit: Callable[[str], Any],
    terminal_stream: Any = None,
    delay_s: float = 5.0,
    heartbeat_s: float = 30.0,
) -> T:
    """Run epoch reconciliation with activity, durable-log, and terminal heartbeats."""
    label = str(target_symbol or "[project scope]")

    def emit(message: str) -> None:
        with contextlib.suppress(Exception):
            activity_emit(
                "campaign-epoch-transition-heartbeat",
                message,
                target_symbol=target_symbol,
                previous_epoch=previous_epoch,
                reason=reason,
                campaign_progress=False,
            )
        line = f"{message}\n"
        with contextlib.suppress(Exception):
            run_log_emit(line)
        if terminal_stream is not None:
            with contextlib.suppress(Exception):
                terminal_stream.write(line)
                terminal_stream.flush()

    return run_with_heartbeat(
        operation,
        start_message=(
            f"⏳ Campaign epoch {previous_epoch} transition for {label} "
            "is reconciling saved state."
        ),
        heartbeat_message=lambda elapsed: (
            f"⏳ Campaign epoch {previous_epoch} transition for {label} "
            f"remains active ({elapsed:.0f}s elapsed)."
        ),
        finish_message=lambda _result, elapsed: (
            f"✓ Campaign epoch {previous_epoch} transition for {label} "
            f"finished in {elapsed:.1f}s."
        ),
        delay_s=delay_s,
        heartbeat_s=heartbeat_s,
        emit=emit,
    )


def report_research_portfolio_progress(
    state: MutableMapping[str, Any],
    status: Mapping[str, Any] | None,
    *,
    target_symbol: str,
    now: float | None = None,
    heartbeat_s: float = 60.0,
    emit: Callable[[str], None] = print,
) -> bool:
    """Emit changed worker state and bounded heartbeats for active research.

    Parent maintenance polls every second for liveness, but repeating that
    cadence in the readable log would be noise. Report launches/completions
    immediately, active-set changes once, and otherwise one heartbeat per
    minute so a long Codex research worker never looks abandoned.
    """
    payload = dict(status or {})
    active_jobs = tuple(
        str(job_id) for job_id in (payload.get("active_jobs") or []) if str(job_id or "").strip()
    )
    launched = tuple(
        str(job_id) for job_id in (payload.get("launched") or []) if str(job_id or "").strip()
    )
    consumed = tuple(
        str(job_id) for job_id in (payload.get("consumed") or []) if str(job_id or "").strip()
    )
    portfolio_active_count = max(len(active_jobs), int(payload.get("active", 0) or 0))
    planner_active = bool(state.get("_planner_phase_active"))
    active_count = portfolio_active_count + int(planner_active)
    signature = ("|".join(active_jobs) if active_jobs else f"count:{portfolio_active_count}") + (
        "|planner" if planner_active else ""
    )
    current = time.monotonic() if now is None else float(now)
    try:
        last_notice = float(state.get(_PORTFOLIO_NOTICE_AT_KEY, 0.0) or 0.0)
    except (TypeError, ValueError):
        last_notice = 0.0
    previous_signature = str(state.get(_PORTFOLIO_SIGNATURE_KEY, "") or "")
    changed = signature != previous_signature
    heartbeat_due = active_count > 0 and current - last_notice >= max(1.0, heartbeat_s)
    noteworthy = bool(launched or consumed or (changed and (active_count or previous_signature)))
    if not noteworthy and not heartbeat_due:
        return False

    label = str(target_symbol or "[project scope]")
    details = [f"active {active_count}"]
    if launched:
        details.append(f"launched {len(launched)}")
    if consumed:
        details.append(f"completed {len(consumed)}")
    if planner_active:
        details.append("planner active")
    if heartbeat_due and not launched and not consumed and not changed:
        details.append("still working")
    emit(f"🔬 Research portfolio for {label}: " + ", ".join(details) + ".")
    state[_PORTFOLIO_SIGNATURE_KEY] = signature
    state[_PORTFOLIO_NOTICE_AT_KEY] = current
    return True
