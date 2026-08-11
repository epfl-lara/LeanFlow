"""Slow transition visibility tests."""

from io import StringIO

from leanflow_cli.native import transition_visibility


class _ImmediateTimer:
    def __init__(self, _delay, callback):
        self.callback = callback
        self.daemon = False

    def start(self):
        self.callback()

    def cancel(self):
        return None


def test_slow_notice_reports_start_and_finish(monkeypatch):
    messages: list[str] = []
    monkeypatch.setattr(transition_visibility.threading, "Timer", _ImmediateTimer)

    result = transition_visibility.run_with_slow_notice(
        lambda: {"active": 2},
        start_message="refresh started",
        finish_message=lambda value, elapsed: f"refresh finished: {value['active']} in {elapsed:.1f}s",
        emit=messages.append,
    )

    assert result == {"active": 2}
    assert messages[0] == "refresh started"
    assert messages[1].startswith("refresh finished: 2 in ")


def test_run_with_heartbeat_reports_progress_until_completion():
    messages: list[str] = []

    result = transition_visibility.run_with_heartbeat(
        lambda: transition_visibility.time.sleep(0.035) or "ok",
        start_message="checking",
        heartbeat_message=lambda elapsed: f"heartbeat {elapsed:.3f}",
        finish_message=lambda value, elapsed: f"finished {value}",
        delay_s=0.005,
        heartbeat_s=0.01,
        emit=messages.append,
    )

    assert result == "ok"
    assert messages[0] == "checking"
    assert messages[-1] == "finished ok"


def test_epoch_transition_reaches_activity_log_and_terminal(monkeypatch):
    events: list[tuple[tuple, dict]] = []
    durable: list[str] = []
    terminal = StringIO()

    def run_with_heartbeat(operation, **kwargs):
        kwargs["emit"](kwargs["start_message"])
        kwargs["emit"](kwargs["heartbeat_message"](35.0))
        result = operation()
        kwargs["emit"](kwargs["finish_message"](result, 36.0))
        return result

    monkeypatch.setattr(transition_visibility, "run_with_heartbeat", run_with_heartbeat)
    result = transition_visibility.run_epoch_transition(
        lambda: "ok",
        target_symbol="demo",
        previous_epoch=3,
        reason="route-no-progress",
        activity_emit=lambda *args, **kwargs: events.append((args, kwargs)),
        run_log_emit=durable.append,
        terminal_stream=terminal,
    )

    assert result == "ok"
    assert [args[0] for args, _kwargs in events] == ["campaign-epoch-transition-heartbeat"] * 3
    assert all(kwargs["target_symbol"] == "demo" for _args, kwargs in events)
    assert len(durable) == 3
    assert "remains active (35s elapsed)" in durable[1]
    assert terminal.getvalue() == "".join(durable)


def test_research_portfolio_progress_reports_changes_and_bounded_heartbeat():
    state = {}
    messages = []
    active = {
        "active": 1,
        "active_jobs": ["campaign.ds-001"],
        "launched": ["campaign.ds-001"],
    }

    assert transition_visibility.report_research_portfolio_progress(
        state,
        active,
        target_symbol="result",
        now=10.0,
        heartbeat_s=60.0,
        emit=messages.append,
    )
    assert not transition_visibility.report_research_portfolio_progress(
        state,
        {"active": 1, "active_jobs": ["campaign.ds-001"]},
        target_symbol="result",
        now=40.0,
        heartbeat_s=60.0,
        emit=messages.append,
    )
    assert transition_visibility.report_research_portfolio_progress(
        state,
        {"active": 1, "active_jobs": ["campaign.ds-001"]},
        target_symbol="result",
        now=70.0,
        heartbeat_s=60.0,
        emit=messages.append,
    )
    assert transition_visibility.report_research_portfolio_progress(
        state,
        {"active": 0, "active_jobs": [], "consumed": ["campaign.ds-001"]},
        target_symbol="result",
        now=71.0,
        heartbeat_s=60.0,
        emit=messages.append,
    )

    assert messages == [
        "🔬 Research portfolio for result: active 1, launched 1.",
        "🔬 Research portfolio for result: active 1, still working.",
        "🔬 Research portfolio for result: active 0, completed 1.",
    ]


def test_research_portfolio_counts_live_planner_phase():
    """Never report zero activity while planner-owned work is still live."""
    state = {"_planner_phase_active": True}
    messages = []

    assert transition_visibility.report_research_portfolio_progress(
        state,
        {"active": 0, "active_jobs": [], "consumed": ["campaign.ds-001"]},
        target_symbol="result",
        now=10.0,
        emit=messages.append,
    )

    assert messages == ["🔬 Research portfolio for result: active 1, completed 1, planner active."]
