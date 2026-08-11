"""Pin same-turn reuse of unchanged exact-target rejection evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanflow_cli.native import native_runner as runner


class _Agent:
    """Expose only the managed-turn state needed by the pre-tool snapshot."""

    def __init__(self, autonomy_state: dict[str, object]) -> None:
        self._managed_autonomy_state = autonomy_state


def _state(active: Path) -> dict[str, object]:
    return {
        "current_cycle": 7,
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "theorem demo : True := by\n  sorry",
        },
    }


def _failed_source_check(active: Path) -> dict[str, object]:
    return {
        "success": True,
        "ok": False,
        "action": "check_target",
        "target": "demo",
        "file": str(active),
        "command": "lean_probe check_target",
        "has_errors": False,
        "has_sorry": True,
        "messages": [
            {
                "severity": "warning",
                "line": 2,
                "message": "declaration uses 'sorry'",
            }
        ],
        "output": "warning: declaration uses 'sorry'",
    }


def _remember_failure(active: Path, state: dict[str, object]) -> None:
    agent = _Agent(state)
    runner._capture_exact_check_source_snapshot(
        agent,
        "lean_incremental_check",
        {
            "action": "check_target",
            "file_path": str(active),
            "theorem_id": "demo",
        },
    )
    snapshot = runner._take_exact_check_source_snapshot(agent, "lean_incremental_check")
    runner._remember_final_report_failure_check(
        state,
        active_file=str(active),
        target_symbol="demo",
        manager_check=_failed_source_check(active),
        manager_tool="lean_incremental_check",
        source_snapshot=snapshot,
    )


def test_managed_pre_tool_hook_captures_only_exact_on_disk_check(monkeypatch, tmp_path):
    """Wire the source snapshot through the production pre-tool callback."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _state(active)
    agent = _Agent(state)
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    result = runner._managed_pre_tool_call(
        agent,
        "lean_incremental_check",
        {
            "action": "check_target",
            "file_path": str(active),
            "theorem_id": "demo",
        },
    )

    assert result is None
    snapshot = runner._take_exact_check_source_snapshot(agent, "lean_incremental_check")
    assert snapshot["assignment_scope"] == runner._queue_key("demo", str(active)).storage_key()
    assert snapshot["source_sha256"] == runner._source_revision_sha256(str(active))


def test_managed_pre_tool_hook_captures_exact_file_verification(monkeypatch, tmp_path):
    """Capture source identity for a canonical assigned-file verification."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    agent = _Agent(_state(active))
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)

    result = runner._managed_pre_tool_call(
        agent,
        "lean_verify",
        {"mode": "file_exact", "target": str(active)},
    )

    assert result is None
    snapshot = runner._take_exact_check_source_snapshot(agent, "lean_verify")
    assert snapshot["assignment_scope"] == runner._queue_key("demo", str(active)).storage_key()
    assert snapshot["source_sha256"] == runner._source_revision_sha256(str(active))


def test_managed_post_tool_hook_forwards_exact_check_snapshot(monkeypatch, tmp_path):
    """Carry the pre-tool identity into the authoritative step-boundary hook."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _state(active)
    agent = _Agent(state)
    agent._managed_step_boundary_closed = False
    agent._managed_pending_theorem_feedback = None
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_workflow_kind", lambda: "review")
    monkeypatch.setattr(runner, "_poll_research_portfolio_after_tool_result", lambda *args: None)
    monkeypatch.setattr(runner, "_note_non_search_tool_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner, "_maybe_append_formalization_handoff_feedback", lambda *args, **kwargs: None
    )
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        runner,
        "_finish_queue_step_boundary",
        lambda *args, **kwargs: observed.append(kwargs),
    )
    arguments = {
        "action": "check_target",
        "file_path": str(active),
        "theorem_id": "demo",
    }
    runner._managed_pre_tool_call(agent, "lean_incremental_check", arguments)

    runner._handle_managed_tool_result(
        agent,
        "lean_incremental_check",
        arguments,
        json.dumps(_failed_source_check(active)),
    )

    assert len(observed) == 1
    snapshot = dict(observed[0]["exact_check_source_snapshot"])
    assert snapshot["assignment_scope"] == runner._queue_key("demo", str(active)).storage_key()
    assert snapshot["source_sha256"] == runner._source_revision_sha256(str(active))


def test_final_report_reuses_unchanged_exact_source_failure(monkeypatch, tmp_path):
    """Reject a no-edit final report without launching the identical Lean check again."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _state(active)
    _remember_failure(active, state)
    assert runner._FINAL_REPORT_FAILURE_CHECK_KEY in state

    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(
        runner,
        "_manager_check_queue_item_transaction",
        lambda *args, **kwargs: pytest.fail("unchanged failed target check must be reused"),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_maybe_manager_nudge", lambda *args, **kwargs: "")
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_AUTHORITY", raising=False)
    monkeypatch.delenv("LEANFLOW_QUEUE_DECIDE_SHADOW", raising=False)

    result = runner._review_agent_final_report(
        {
            "completed": True,
            "interrupted": False,
            "final_response": "The theorem remains open.",
            "messages": [{"role": "assistant", "content": "The theorem remains open."}],
        },
        state,
    )

    review = result["manager_final_report_review"]
    assert review["ok"] is False
    assert review["verification_reused"] is True
    assert review["manager_tool"] == "lean_incremental_check"
    assert runner._FINAL_REPORT_FAILURE_CHECK_KEY not in state


@pytest.mark.parametrize("change", ["source", "assignment"])
def test_final_report_failure_reuse_invalidates_on_identity_change(tmp_path, change):
    """Require a fresh gate after any source or exact-assignment change."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _state(active)
    _remember_failure(active, state)

    target = "demo"
    if change == "source":
        active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    else:
        target = "other"

    assert (
        runner._take_final_report_failure_check(
            state,
            active_file=str(active),
            target_symbol=target,
        )
        is None
    )
    assert runner._FINAL_REPORT_FAILURE_CHECK_KEY not in state


@pytest.mark.parametrize("kind", ["success", "replacement", "timeout"])
def test_final_report_failure_reuse_rejects_unsafe_evidence(tmp_path, kind):
    """Never cache positive, replacement, or operational check results."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _state(active)
    agent = _Agent(state)
    runner._capture_exact_check_source_snapshot(
        agent,
        "lean_incremental_check",
        {
            "action": "check_target",
            "file_path": str(active),
            "theorem_id": "demo",
        },
    )
    snapshot = runner._take_exact_check_source_snapshot(agent, "lean_incremental_check")
    check = _failed_source_check(active)
    if kind == "success":
        check.update({"ok": True, "has_sorry": False, "messages": [], "output": ""})
    elif kind == "replacement":
        check.update(
            {
                "replacement_matches_target": True,
                "replacement_declarations": ["demo"],
                "verification_scope": "target_candidate",
            }
        )
    else:
        check.update(
            {
                "success": True,
                "timed_out": True,
                "error_code": "lean_probe_timeout",
                "error": "timed out waiting for LeanProbe",
                "messages": [],
                "output": "",
            }
        )

    runner._remember_final_report_failure_check(
        state,
        active_file=str(active),
        target_symbol="demo",
        manager_check=check,
        manager_tool="lean_incremental_check",
        source_snapshot=snapshot,
    )

    assert runner._FINAL_REPORT_FAILURE_CHECK_KEY not in state


def test_new_managed_turn_clears_failure_reuse(monkeypatch, tmp_path):
    """Do not carry negative evidence into a later provider turn."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _state(active)
    _remember_failure(active, state)
    agent = _Agent(state)
    monkeypatch.setattr(runner, "_workflow_kind", lambda: "prove")

    runner._prepare_managed_turn_state(agent, state)

    assert runner._FINAL_REPORT_FAILURE_CHECK_KEY not in state


def test_newer_unbound_theorem_gate_clears_prior_failure(tmp_path):
    """Never let older negative evidence outlive a newer uncached theorem gate."""
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    state = _state(active)
    _remember_failure(active, state)

    runner._remember_final_report_failure_check(
        state,
        active_file=str(active),
        target_symbol="demo",
        manager_check=_failed_source_check(active),
        manager_tool="lean_incremental_check",
        source_snapshot=None,
    )

    assert runner._FINAL_REPORT_FAILURE_CHECK_KEY not in state
