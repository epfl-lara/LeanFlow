"""Expose slow synchronous transition work without making fast polls noisy."""

from __future__ import annotations

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
    active_count = max(len(active_jobs), int(payload.get("active", 0) or 0))
    signature = "|".join(active_jobs) if active_jobs else f"count:{active_count}"
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
    if heartbeat_due and not launched and not consumed and not changed:
        details.append("still working")
    emit(f"🔬 Research portfolio for {label}: " + ", ".join(details) + ".")
    state[_PORTFOLIO_SIGNATURE_KEY] = signature
    state[_PORTFOLIO_NOTICE_AT_KEY] = current
    return True
