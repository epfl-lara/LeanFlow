from __future__ import annotations

import json

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.native import source_placeholder_guard


@pytest.mark.parametrize("action", ["check_target", ""])
def test_guard_blocks_explicit_or_default_check_target_without_replacement(tmp_path, action):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    arguments = {"theorem_id": "demo", "file_path": str(active)}
    if action:
        arguments["action"] = action

    block = source_placeholder_guard.block_unchanged_target_check(
        "lean_incremental_check",
        arguments,
        {"target_symbol": "demo", "active_file": str(active)},
        project_root=str(tmp_path),
    )

    assert block is not None
    payload = block.to_tool_result()
    assert payload["status"] == "source_placeholder_check_skipped"
    assert payload["source_placeholders"] == ["sorry"]
    assert payload["lean_started"] is False
    assert "did not start Lean" in payload["message"]


@pytest.mark.parametrize("placeholder", ["sorry", "admit"])
def test_guard_recognizes_real_source_placeholders_but_not_comments(tmp_path, placeholder):
    active = tmp_path / "Main.lean"
    active.write_text(f"theorem demo : True := by\n  {placeholder}\n", encoding="utf-8")
    assignment = {"target_symbol": "demo", "active_file": str(active)}

    assert (
        source_placeholder_guard.block_unchanged_target_check(
            "lean_incremental_check",
            {"action": "check_target", "theorem_id": "demo"},
            assignment,
            project_root=str(tmp_path),
        )
        is not None
    )

    active.write_text(
        "theorem demo : True := by\n  -- sorry and admit are only prose\n  trivial\n",
        encoding="utf-8",
    )
    assert (
        source_placeholder_guard.block_unchanged_target_check(
            "lean_incremental_check",
            {"action": "check_target", "theorem_id": "demo"},
            assignment,
            project_root=str(tmp_path),
        )
        is None
    )


def test_guard_allows_replacement_candidate_or_changed_sorry_free_target(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    assignment = {"target_symbol": "demo", "active_file": str(active)}
    replacement = "theorem demo : True := by\n  trivial"

    assert (
        source_placeholder_guard.block_unchanged_target_check(
            "lean_incremental_check",
            {
                "action": "check_target",
                "theorem_id": "demo",
                "replacement": replacement,
            },
            assignment,
            project_root=str(tmp_path),
        )
        is None
    )

    active.write_text(replacement + "\n", encoding="utf-8")
    assert (
        source_placeholder_guard.block_unchanged_target_check(
            "lean_incremental_check",
            {"action": "check_target", "theorem_id": "demo"},
            assignment,
            project_root=str(tmp_path),
        )
        is None
    )


def test_managed_pre_tool_guard_skips_lean_and_never_records_target_attempt(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    events: list[tuple[str, dict]] = []

    class _Agent:
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            }
        }

        def is_interrupted(self):
            return False

    agent = _Agent()
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setattr(
        runner,
        "_record_agent_activity",
        lambda _agent, event, _message, **details: events.append((event, details)),
    )
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_poll_research_portfolio_after_tool_result", lambda *_args: None)
    monkeypatch.setattr(
        runner,
        "_finish_queue_step_boundary",
        lambda *_args, **_kwargs: pytest.fail("placeholder skip entered target failure gate"),
    )
    arguments = {
        "action": "check_target",
        "theorem_id": "demo",
        "file_path": str(active),
    }

    result = runner._managed_pre_tool_call(agent, "lean_incremental_check", arguments)

    assert result is not None
    payload = json.loads(result)
    assert payload["status"] == "source_placeholder_check_skipped"
    assert payload["lean_started"] is False
    assert events == [
        (
            "queue-source-placeholder-check-skipped",
            {
                "target_symbol": "demo",
                "active_file": str(active.resolve()),
                "source_placeholders": ["sorry"],
                "action": "check_target",
                "lean_started": False,
                "target_attempt_consumed": False,
                "campaign_progress": False,
            },
        )
    ]

    runner._handle_managed_tool_result(
        agent,
        "lean_incremental_check",
        arguments,
        result,
    )
    assert "failed_attempts" not in agent._managed_autonomy_state


def test_pending_checked_helper_priority_outranks_generic_placeholder_guard(monkeypatch, tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")

    class _Agent:
        _managed_autonomy_state = {
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            }
        }

    agent = _Agent()
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setattr(
        runner,
        "_research_helper_candidate_pre_tool_guard",
        lambda *_args, **_kwargs: "integrate the exact parent-checked helper first",
    )
    monkeypatch.setattr(
        runner.source_placeholder_guard,
        "block_unchanged_target_check",
        lambda *_args, **_kwargs: pytest.fail("generic source guard shadowed helper priority"),
    )

    assert (
        runner._managed_pre_tool_call(
            agent,
            "lean_incremental_check",
            {"action": "check_target", "theorem_id": "demo"},
        )
        == "integrate the exact parent-checked helper first"
    )
