"""Tests for the managed diagnostic feedback loop guard."""

from leanflow_cli.native import diagnostic_loop_guard


class _Agent:
    pass


def test_feedback_guard_exhausts_once_at_configured_limit(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DIAGNOSTIC_FEEDBACK_LIMIT", "4")
    agent = _Agent()
    diagnostic_loop_guard.reset(agent)

    for _ in range(3):
        assert (
            diagnostic_loop_guard.observe(
                agent,
                function_name="lean_incremental_check",
                args={"action": "feedback"},
                source_revision_sha256="rev-a",
            )
            is None
        )

    decision = diagnostic_loop_guard.observe(
        agent,
        function_name="lean_incremental_check",
        args={"action": "feedback"},
        source_revision_sha256="rev-a",
    )
    assert decision is not None
    assert decision.attempts == 4
    assert decision.limit == 4
    assert (
        diagnostic_loop_guard.observe(
            agent,
            function_name="lean_incremental_check",
            args={"action": "feedback"},
            source_revision_sha256="rev-a",
        )
        is None
    )


def test_feedback_guard_resets_when_source_revision_changes(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DIAGNOSTIC_FEEDBACK_LIMIT", "4")
    agent = _Agent()
    diagnostic_loop_guard.reset(agent)

    for _ in range(3):
        diagnostic_loop_guard.observe(
            agent,
            function_name="lean_incremental_check",
            args={"action": "feedback"},
            source_revision_sha256="rev-a",
        )

    assert (
        diagnostic_loop_guard.observe(
            agent,
            function_name="lean_incremental_check",
            args={"action": "feedback"},
            source_revision_sha256="rev-b",
        )
        is None
    )


def test_feedback_guard_ignores_construction_and_exact_checks(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DIAGNOSTIC_FEEDBACK_LIMIT", "4")
    agent = _Agent()
    diagnostic_loop_guard.reset(agent)

    for function_name, action in (
        ("lean_incremental_check", "check_target"),
        ("lean_incremental_check", "check_helper"),
        ("apply_verified_patch", "feedback"),
    ):
        assert (
            diagnostic_loop_guard.observe(
                agent,
                function_name=function_name,
                args={"action": action},
                source_revision_sha256="rev-a",
            )
            is None
        )

    state = getattr(agent, "_managed_diagnostic_loop_guard_state")
    assert state["attempts"] == 0
