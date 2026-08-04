"""Pin `_finish_queue_step_boundary` behavior in the native runner.

The boundary unifies verdict paths behind `TheoremQueueManager.decide()`.
These goldens preserve its load-bearing behavior:

- D2: post-edit triggers consume hard retries against the limit of 8;
  verification-tool triggers (lean_verify etc.) consume NOTHING.
- Signature idempotency: an identical manager check does not advance retries.
- Warning-once: first warning grants one cleanup turn, the second is accepted.
- Failed attempts are recorded on still-blocked continues, never on exhaustion.

Only I/O seams are stubbed (live-state rebuild, activity sink, declaration
lookup, diagnostic feedback reason); the verdict logic under test runs real.
"""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner


@pytest.fixture(autouse=True, params=[False, True], ids=["legacy", "authority"])
def _decide_authority(request, monkeypatch):
    """Run every boundary golden under BOTH verdict sources.

    The decide-authoritative feature flag must reproduce every legacy verdict
    on the same-assignment domain. Parametrizing here makes that parity a
    permanent CI invariant.
    """
    if request.param:
        monkeypatch.setenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", "1")
    else:
        monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", raising=False)


class _StubAgent:
    """Minimal AIAgent stand-in for direct `_finish_queue_step_boundary` calls."""

    quiet_mode = True

    def __init__(self, autonomy_state: dict[str, Any]):
        self._managed_autonomy_state = autonomy_state
        self._session_messages = [{"role": "assistant", "content": "partial"}]
        self._managed_pending_theorem_feedback: dict[str, str] | None = {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
        }
        self.interrupt_messages: list[str | None] = []

    def is_interrupted(self) -> bool:
        return False

    def interrupt(self, message: str | None = None) -> None:
        self.interrupt_messages.append(message)

    def set_tool_result_appendix(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self._post_tool_result_appendix = text
        elif hasattr(self, "_post_tool_result_appendix"):
            del self._post_tool_result_appendix

    def clear_tool_result_appendix(self) -> None:
        if hasattr(self, "_post_tool_result_appendix"):
            del self._post_tool_result_appendix


def _autonomy_state() -> dict[str, Any]:
    return {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
            "slice": "theorem demo : True := by\n  sorry",
        },
        "current_cycle": 2,
    }


def _blocked_live_state() -> dict[str, Any]:
    return {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "current_queue_item_slice": "theorem demo : True := by\n  sorry",
        "diagnostics": "error: unsolved goals",
        "goals": "⊢ False",
        "build_status": "unknown",
        "blocker_summary": "error: unsolved goals",
    }


def _warning_only_live_state() -> dict[str, Any]:
    """Same assignment, closed goals, no sorry — only a warning ON the assigned
    declaration's lines (a `file:line:col:` diagnostic, which is what makes the
    live evidence classify WARNING_ONCE rather than ACCEPT)."""
    return {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": []},
        "diagnostics": "Demo/Main.lean:2:4: warning: unused variable `h`\n",
        "goals": "no goals",
        "build_status": "ok",
    }


def _clean_advanced_live_state() -> dict[str, Any]:
    return {
        "target_symbol": "next_demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "next_demo", "reasons": ["contains sorry"]},
        "diagnostics": "warning: declaration uses sorry",
        "goals": "no goals",
        "build_status": "ok",
    }


def _wire(monkeypatch, live_state, *, cleanup_reason="", has_sorry=False):
    """Stub the I/O seams around the boundary; return the captured activity events."""
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state",
        lambda history, checkpoint_state=None: dict(live_state),
    )
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    monkeypatch.setattr(
        runner, "_declaration_diagnostic_feedback_reason", lambda *args, **kwargs: cleanup_reason
    )
    monkeypatch.setattr(
        runner, "_find_declaration_entry", lambda file, label: {"has_sorry": has_sorry}
    )
    return events


def _boundary_event(events):
    boundary_types = {
        "queue-theorem-feedback",
        "queue-theorem-cleanup-feedback",
        "queue-theorem-retry-exhausted",
        "queue-step-boundary",
    }
    matches = [(args, kwargs) for args, kwargs in events if args[0] in boundary_types]
    assert len(matches) == 1, f"expected exactly one boundary event, got {matches}"
    # Pin the non-exception path: the broad except also yields with a
    # queue-step-boundary event, distinguishable only by refresh_error.
    assert matches[0][1]["refresh_error"] == ""
    return matches[0]


def _hard_retry_counts(autonomy_state):
    return {
        key: dict(entry)
        for key, entry in dict(autonomy_state.get("manager_feedback_retries") or {}).items()
    }


def test_post_edit_hard_error_consumes_retry_records_attempt_and_continues(monkeypatch):
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    events = _wire(monkeypatch, _blocked_live_state())
    scope = runner._queue_key("demo", "Demo/Main.lean").storage_key()
    assert runner.orchestrator_event_watermark.arm_foreground_grace(
        autonomy_state,
        scope=scope,
    )

    manager_check = {
        "ok": False,
        "mode": "incremental_target",
        "command": "lean_interact check_target",
        "target": "demo",
        "output": "error: unsolved goals",
    }
    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="patch+lean_incremental_check",
        manager_verification=manager_check,
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-theorem-feedback"
    review = kwargs["manager_verification"]
    assert review["feedback_kind"] == "error"
    assert review["feedback_retry_limit"] == runner.MANAGER_POST_EDIT_HARD_RETRY_LIMIT
    assert kwargs["still_blocked"] is True
    assert kwargs["hard_retry_exhausted"] is False

    # One hard retry consumed for the assignment key.
    counts = _hard_retry_counts(autonomy_state)
    assert len(counts) == 1
    assert next(iter(counts.values())) == {"hard": 1}
    # Failed attempt recorded with the manager feedback as the reason.
    attempts = autonomy_state["failed_attempts"]
    assert attempts[-1]["target_symbol"] == "demo"
    assert "error: unsolved goals" in attempts[-1]["reason"]
    assert any(a[0] == "failed-attempt-recorded" for a, _k in events)

    # Continue-same-turn: appendix set, no interrupt, attempt flagged, pending cleared.
    assert "[LEANFLOW-NATIVE THEOREM FEEDBACK]" in agent._post_tool_result_appendix
    assert "still blocked; continue the same theorem turn" in agent._post_tool_result_appendix
    assert agent.interrupt_messages == []
    assert agent._managed_step_boundary_recorded_attempt is True
    assert agent._managed_pending_theorem_feedback is None
    assert not getattr(agent, "_managed_step_boundary_closed", False)
    assert runner.orchestrator_event_watermark.foreground_grace_active(
        autonomy_state,
        scope=scope,
    )

    # Signature idempotency: re-running the identical check does NOT advance retries.
    agent._managed_pending_theorem_feedback = {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
    }
    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="patch+lean_incremental_check",
        manager_verification=dict(manager_check),
    )
    counts = _hard_retry_counts(autonomy_state)
    assert next(iter(counts.values())) == {"hard": 1}


def test_restored_edit_feedback_names_discarded_after_image(monkeypatch):
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    events = _wire(monkeypatch, _blocked_live_state())
    monkeypatch.setattr(runner, "_source_revision_sha256", lambda path: "restored-sha")

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="patch+lean_incremental_check",
        manager_verification={
            "ok": False,
            "output": "error: unexpected token at the rejected edit",
            "failed_edit_restored": True,
        },
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-theorem-feedback"
    review = kwargs["manager_verification"]
    assert review["source_restored"] is True
    assert review["restored_source_sha256"] == "restored-sha"
    assert review["feedback_describes_rejected_after_image"] is True
    assert "discarded after-image" in agent._post_tool_result_appendix
    assert "re-read the complete edited region" in agent._post_tool_result_appendix
    assert autonomy_state[runner.ROLLBACK_REFRESH_READ_STATE_KEY] == {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "source_revision_sha256": "restored-sha",
    }
    assert any(event[0] == "manager-restored-source-reused" for event, _ in events)


def test_rejection_stages_coached_feedback_before_portfolio_maintenance(monkeypatch):
    """Slow research refill must not sit ahead of rejection feedback staging."""
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    _wire(monkeypatch, _blocked_live_state())
    order: list[str] = []

    monkeypatch.setattr(
        runner,
        "_maybe_manager_nudge",
        lambda *args, **kwargs: order.append("coach") or "[PERSISTENCE COACH]\n- continue",
    )
    monkeypatch.setattr(
        runner,
        "_maintain_research_portfolio",
        lambda *args, **kwargs: order.append("portfolio"),
    )

    def stage_feedback(text: str) -> None:
        order.append("feedback")
        agent._post_tool_result_appendix = text

    agent.set_tool_result_appendix = stage_feedback

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="lean_verify",
        manager_verification={
            "ok": False,
            "command": "lake env lean Demo/Main.lean",
            "output": "error: unsolved goals",
        },
    )

    assert order == ["coach", "feedback", "portfolio"]
    assert "[PERSISTENCE COACH]" in agent._post_tool_result_appendix


def test_post_edit_hard_error_below_limit_consumes_up_to_the_limit(monkeypatch):
    """Exhaustion triggers only when the PRE-consumption count has reached 8."""
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    events = _wire(monkeypatch, _blocked_live_state())
    monkeypatch.setattr(
        runner,
        "_restore_queue_assignment_to_baseline_sorry",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("restore must not run")),
    )
    for _ in range(runner.MANAGER_POST_EDIT_HARD_RETRY_LIMIT - 1):
        runner._increment_manager_feedback_retry(
            autonomy_state,
            target_symbol="demo",
            active_file="Demo/Main.lean",
            kind="error",
        )

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="patch+lean_incremental_check",
        manager_verification={
            "ok": False,
            "command": "lean_interact check_target",
            "target": "demo",
            "output": "error: unsolved goals",
        },
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-theorem-feedback"
    assert kwargs["hard_retry_exhausted"] is False
    counts = _hard_retry_counts(autonomy_state)
    assert next(iter(counts.values())) == {"hard": runner.MANAGER_POST_EDIT_HARD_RETRY_LIMIT}
    assert agent.interrupt_messages == []


def test_post_edit_hard_error_at_limit_restores_baseline_and_yields(monkeypatch):
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    events = _wire(monkeypatch, _blocked_live_state())
    restore_calls: list[tuple] = []
    monkeypatch.setattr(
        runner,
        "_restore_queue_assignment_to_baseline_sorry",
        lambda *args, **kwargs: (
            restore_calls.append(args),
            {"restored": True, "reason": "restored"},
        )[1],
    )
    for _ in range(runner.MANAGER_POST_EDIT_HARD_RETRY_LIMIT):
        runner._increment_manager_feedback_retry(
            autonomy_state,
            target_symbol="demo",
            active_file="Demo/Main.lean",
            kind="error",
        )

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="patch+lean_incremental_check",
        manager_verification={
            "ok": False,
            "command": "lean_interact check_target",
            "target": "demo",
            "output": "error: unsolved goals",
        },
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-theorem-retry-exhausted"
    assert kwargs["hard_retry_exhausted"] is True
    assert kwargs["manager_verification"]["retry_exhausted"] is True
    assert kwargs["manager_verification"]["restore"] == {
        "restored": True,
        "reason": "reverted current declaration to its baseline `sorry` slice "
        "after the local feedback window completed",
    }
    assert args[1] == "Local feedback window complete for demo; route change requested"
    assert len(restore_calls) == 1

    # Exhaustion yields: interrupt fired, boundary closed, no failed attempt recorded.
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    assert agent._managed_step_boundary_closed is True
    assert agent._managed_step_boundary_recorded_attempt is False
    assert "failed_attempts" not in autonomy_state
    assert "manager_feedback_retries" not in autonomy_state
    assert "manager_feedback_retry_consumed_signatures" not in autonomy_state
    assert not hasattr(agent, "_post_tool_result_appendix")


def test_verification_tool_hard_error_consumes_no_retry_but_records_attempt(monkeypatch):
    """Pins drift D2: non-edit triggers never consume hard retries."""
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    events = _wire(monkeypatch, _blocked_live_state())

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="lean_verify",
        manager_verification={
            "ok": False,
            "command": "lake env lean Demo/Main.lean",
            "output": "error: unsolved goals",
        },
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-theorem-feedback"
    assert kwargs["still_blocked"] is True
    assert "manager_feedback_retries" not in autonomy_state
    assert "manager_feedback_retry_consumed_signatures" not in autonomy_state
    # A non-edit hard continue does NOT stamp manager_verification.feedback_kind
    # (the hard-retry branch is post-edit-gated) — pins the authority parity.
    assert "feedback_kind" not in kwargs["manager_verification"]
    assert autonomy_state["failed_attempts"][-1]["target_symbol"] == "demo"
    assert agent.interrupt_messages == []
    assert agent._managed_step_boundary_recorded_attempt is True


def test_warning_cleanup_first_pass_grants_one_cleanup_turn(monkeypatch):
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    live_state = _blocked_live_state()
    live_state.update(
        {"diagnostics": "warning: unused variable `h`", "goals": "no goals", "build_status": "ok"}
    )
    events = _wire(monkeypatch, live_state, cleanup_reason="unused variable `h`")

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="patch+lean_incremental_check",
        manager_verification={
            "ok": True,
            "mode": "incremental_target",
            "command": "lean_interact check_target",
            "target": "demo",
            "output": "warning: unused variable `h`",
        },
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-theorem-cleanup-feedback"
    review = kwargs["manager_verification"]
    assert review["feedback_kind"] == "warning"
    assert review["feedback_retry_limit"] == runner.MANAGER_WARNING_RETRY_LIMIT
    assert kwargs["warning_retry_accepted"] is False

    counts = _hard_retry_counts(autonomy_state)
    assert next(iter(counts.values())) == {"warning": 1}

    # Cleanup continue: appendix carries the cleanup contract, no interrupt.
    appendix = agent._post_tool_result_appendix
    assert "[LEANFLOW-NATIVE THEOREM FEEDBACK]" in appendix
    assert "local cleanup" in appendix
    assert "bail clause" in appendix
    assert agent.interrupt_messages == []
    assert agent._managed_pending_theorem_feedback is None


def test_warning_cleanup_second_pass_accepts_and_yields(monkeypatch):
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    live_state = _blocked_live_state()
    live_state.update(
        {"diagnostics": "warning: unused variable `h`", "goals": "no goals", "build_status": "ok"}
    )
    events = _wire(monkeypatch, live_state, cleanup_reason="unused variable `h`")
    manager_check = {
        "ok": True,
        "mode": "incremental_target",
        "command": "lean_interact check_target",
        "target": "demo",
        "output": "warning: unused variable `h`",
    }

    for _ in range(2):
        agent._managed_pending_theorem_feedback = {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
        }
        runner._finish_queue_step_boundary(
            agent,
            pending_target="demo",
            pending_file="Demo/Main.lean",
            verification_tool="patch+lean_incremental_check",
            manager_verification=dict(manager_check),
        )

    second_args, second_kwargs = [
        (a, k)
        for a, k in events
        if a[0] in {"queue-step-boundary", "queue-theorem-cleanup-feedback"}
    ][-1]
    assert second_args[0] == "queue-step-boundary"
    assert second_kwargs["warning_retry_accepted"] is True
    assert second_kwargs["manager_verification"]["accepted_after_warning_retry_limit"] is True

    # Acceptance clears the retry bookkeeping entirely and yields.
    assert "manager_feedback_retries" not in autonomy_state
    assert "manager_feedback_retry_consumed_signatures" not in autonomy_state
    assert agent.interrupt_messages[-1] == runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT
    assert agent._managed_step_boundary_closed is True


def test_clean_advance_yields_with_step_boundary_interrupt(monkeypatch):
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    events = _wire(monkeypatch, _clean_advanced_live_state())
    scope = runner._queue_key("demo", "Demo/Main.lean").storage_key()
    assert runner.orchestrator_event_watermark.arm_foreground_grace(
        autonomy_state,
        scope=scope,
    )

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="patch+lean_incremental_check",
        manager_verification={
            "ok": True,
            "mode": "incremental_target",
            "command": "lean_interact check_target",
            "target": "demo",
            "output": "",
        },
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-step-boundary"
    assert kwargs["still_blocked"] is False
    assert kwargs["yielded"] is True
    assert kwargs["queue_item"]["label"] == "next_demo"
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    assert agent._managed_step_boundary_closed is True
    assert agent._managed_pending_theorem_feedback is None
    assert "manager_feedback_retries" not in autonomy_state
    assert "failed_attempts" not in autonomy_state
    assert not hasattr(agent, "_post_tool_result_appendix")
    assert not runner.orchestrator_event_watermark.foreground_grace_active(
        autonomy_state,
        scope=scope,
    )


def test_clean_exact_gate_hands_fast_state_to_outer_loop(monkeypatch):
    """A passed exact gate must not immediately rebuild the same Lean state."""
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    fast_state = _clean_advanced_live_state()
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner,
        "_build_verified_gate_handoff_state",
        lambda *args, **kwargs: dict(fast_state),
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_build_live_proof_state_compat",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("successful gate must not run comprehensive live refresh")
        ),
    )
    monkeypatch.setattr(
        runner.verified_gate_handoff,
        "remember",
        lambda target_agent, state: (
            setattr(target_agent, "_managed_step_boundary_live_state", dict(state)) or True
        ),
    )
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    monkeypatch.setattr(
        runner, "_declaration_diagnostic_feedback_reason", lambda *args, **kwargs: ""
    )
    monkeypatch.setattr(runner, "_find_declaration_entry", lambda file, label: {"has_sorry": False})

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="patch+lean_incremental_check",
        manager_verification={
            "ok": True,
            "action": "check_target",
            "target": "demo",
            "axiom_profile_checked": True,
            "output": "target:demo passed",
        },
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-step-boundary"
    assert kwargs["refresh_error"] == ""
    assert agent._managed_step_boundary_live_state == fast_state
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]


def test_live_warning_without_cleanup_advances(monkeypatch):
    """No TARGETED cleanup reason + a warning-only live state must advance —
    the legacy live probe is hard-blocker-only, and the authority flip strips
    the warning bit so decide() matches (never grants a stray cleanup turn).
    Runs under BOTH flag states via the autouse fixture."""
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    events = _wire(monkeypatch, _warning_only_live_state(), cleanup_reason="", has_sorry=False)
    # The warning must fall ON the assigned declaration's lines to classify
    # WARNING_ONCE (otherwise the test is vacuous); give the entry a range.
    monkeypatch.setattr(
        runner,
        "_find_declaration_entry",
        lambda file, label: {"has_sorry": False, "line": 1, "end_line": 3, "name": "demo"},
    )

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="patch+lean_incremental_check",
        manager_verification={
            "ok": True,
            "mode": "incremental_target",
            "command": "lean_interact check_target",
            "target": "demo",
            "output": "",
        },
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-step-boundary"
    assert kwargs["still_blocked"] is False
    # No warning retry may be consumed — warnings only matter with a targeted
    # cleanup reason (the authority flip strips the live-warning bit to match).
    assert "manager_feedback_retries" not in autonomy_state
    assert "failed_attempts" not in autonomy_state


def test_failed_check_after_queue_advance_still_yields(monkeypatch):
    """A failing file check does not hold the turn once the assignment moved on."""
    autonomy_state = _autonomy_state()
    agent = _StubAgent(autonomy_state)
    events = _wire(monkeypatch, _clean_advanced_live_state())

    runner._finish_queue_step_boundary(
        agent,
        pending_target="demo",
        pending_file="Demo/Main.lean",
        verification_tool="lean_verify",
        manager_verification={
            "ok": False,
            "command": "lake env lean Demo/Main.lean",
            "output": "error: future declaration broken",
        },
    )

    args, kwargs = _boundary_event(events)
    assert args[0] == "queue-step-boundary"
    assert kwargs["still_blocked"] is False
    assert kwargs["queue_item"]["label"] == "next_demo"
    assert agent.interrupt_messages == [runner.WORKFLOW_STEP_BOUNDARY_INTERRUPT]
    assert "manager_feedback_retries" not in autonomy_state
