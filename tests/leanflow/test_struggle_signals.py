"""Phase 2.1 tests: the pure struggle-signal classifier."""

from __future__ import annotations

from leanflow_cli.workflows import struggle_signals
from leanflow_cli.workflows.struggle_signals import Severity, StruggleContext, evaluate


def test_quiet_context_is_none():
    report = evaluate(StruggleContext())

    assert report.severity is Severity.NONE
    assert report.fired() is False
    assert report.signals == ()


def test_failed_attempts_thresholds():
    assert evaluate(StruggleContext(attempt_count=1)).severity is Severity.NONE
    nudged = evaluate(StruggleContext(attempt_count=2))
    assert nudged.severity is Severity.NUDGE
    assert nudged.signals[0].kind == "failed_attempts"
    assert evaluate(StruggleContext(attempt_count=4)).severity is Severity.REROUTE


def test_repeat_error_signature_threshold():
    assert evaluate(StruggleContext(repeated_signature_count=1)).severity is Severity.NONE
    report = evaluate(StruggleContext(repeated_signature_count=2))
    assert [signal.kind for signal in report.signals] == ["repeat_error_signature"]
    assert report.severity is Severity.NUDGE


def test_search_spiral_fires_on_streak_or_total():
    assert (
        evaluate(
            StruggleContext(search_progress={"same_query_streak": 1, "search_count": 5})
        ).severity
        is Severity.NONE
    )
    by_streak = evaluate(StruggleContext(search_progress={"same_query_streak": 2}))
    assert by_streak.signals[0].kind == "search_spiral"
    by_total = evaluate(StruggleContext(search_progress={"search_count": 6}))
    assert by_total.signals[0].kind == "search_spiral"


def test_no_progress_thresholds():
    assert evaluate(StruggleContext(stable_cycles=1)).severity is Severity.NONE
    assert evaluate(StruggleContext(stable_cycles=2)).severity is Severity.NUDGE
    assert evaluate(StruggleContext(stable_cycles=3)).severity is Severity.REROUTE


def test_budget_pressure_ratio():
    assert evaluate(StruggleContext(api_calls=69, max_iterations=100)).severity is Severity.NONE
    report = evaluate(StruggleContext(api_calls=70, max_iterations=100))
    assert report.signals[0].kind == "budget_pressure"
    # No max_iterations -> no division, no signal.
    assert evaluate(StruggleContext(api_calls=500)).severity is Severity.NONE


def test_give_up_phrasing_alone_nudges_and_with_stall_reroutes():
    alone = evaluate(StruggleContext(blocker_summary="type mismatch blocks the rewrite"))
    assert alone.severity is Severity.NUDGE
    combined = evaluate(StruggleContext(blocker_summary="stuck", stable_cycles=2))
    assert combined.severity is Severity.REROUTE
    kinds = {signal.kind for signal in combined.signals}
    assert kinds == {"no_progress", "give_up_phrasing"}


def test_two_distinct_nudge_kinds_escalate_to_reroute():
    report = evaluate(StruggleContext(attempt_count=2, search_progress={"search_count": 6}))
    assert {signal.severity for signal in report.signals} == {Severity.NUDGE}
    assert report.severity is Severity.REROUTE


def test_payload_round_trip_is_json_safe():
    import json

    report = evaluate(
        StruggleContext(attempt_count=3, blocker_summary="x" * 500, api_calls=9, max_iterations=10)
    )
    payload = json.loads(json.dumps(report.to_payload()))
    assert payload["severity"] == "reroute"
    assert all(len(signal["evidence"]) <= 240 for signal in payload["signals"])


def test_search_constants_match_the_runner():
    from leanflow_cli.native import native_runner as runner

    assert struggle_signals.SEARCH_REPEAT_STREAK_NUDGE == runner.SEARCH_PROGRESS_REPEAT_NUDGE_LIMIT
    assert struggle_signals.SEARCH_TOTAL_NUDGE == runner.SEARCH_PROGRESS_TOTAL_NUDGE_LIMIT
