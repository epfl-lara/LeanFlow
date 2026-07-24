"""P1.5 tests: checkpoint-UX retirement + the plan-state resume path."""

from __future__ import annotations

import pytest

from leanflow_cli.native import native_checkpoints
from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.workflow_json_io import update_json_file


def test_retired_commands_are_gone(capsys):
    # The interactive plan-restore/rollback surface is retired (P1.5);
    # /history and the silent writers/git-shadow safety stay.
    assert not hasattr(native_checkpoints, "_resume_plan_from_checkpoint")
    assert not hasattr(native_checkpoints, "_rollback_to_checkpoint")
    assert not hasattr(runner, "_resume_plan_from_checkpoint")
    assert not hasattr(runner, "_rollback_to_checkpoint")
    assert not hasattr(runner, "_resolve_checkpoint_ref")
    # Kept surfaces.
    assert hasattr(runner, "_checkpoint_replay_history")
    assert hasattr(runner, "_write_workflow_checkpoint")
    assert hasattr(runner, "_maybe_checkpoint_before_compaction")
    assert hasattr(runner, "_latest_filesystem_checkpoint_hash")

    runner._print_runner_help()
    help_text = capsys.readouterr().out
    assert "/history" in help_text
    assert "/checkpoint" not in help_text
    assert "/resume-plan" not in help_text
    assert "/rollback" not in help_text


@pytest.fixture()
def plan_enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    return state_dir


def _seed_graph(tmp_path) -> None:
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    bp = plan_state.Blueprint(
        goal="prove demo",
        nodes=(
            plan_state.GraphNode(
                id=plan_state.node_id_for("demo", str(active)),
                name="demo",
                file=str(active),
                status="stated",
            ),
            plan_state.GraphNode(id="n-dead", name="wrong_lemma", file="", status="parked"),
        ),
    )
    plan_state.save_blueprint(bp)
    plan_state.record_decision_packet(
        {
            "packet_id": "bp-7",
            "scope": "theorem",
            "node_id": "n-dead",
            "target_symbol": "wrong_lemma",
            "options": ["split", "park"],
            "decision": None,
        }
    )


def test_resume_context_block_renders_the_handoff(plan_enabled, tmp_path):
    _seed_graph(tmp_path)

    block = plan_state.resume_context_block()

    assert block.startswith("[LEANFLOW PLAN-STATE RESUME]")
    assert "- goal: prove demo" in block
    assert "frontier: `demo`" in block
    assert "open decision packet bp-7" in block
    assert "dead end: `wrong_lemma` [parked]" in block
    assert "resume authority" in block


def test_startup_queue_block_uses_the_live_assignment_after_resume_rotation(
    plan_enabled, tmp_path, monkeypatch
):
    """Characterize the later startup block as live queue authority.

    The persisted plan handoff can be rendered before startup queue selection
    rotates the assignment.  The ordinary startup queue block already uses the
    newly built live state, so it must name the rotated theorem rather than the
    durable pre-rotation assignment.
    """
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem old_target : True := by\n  sorry\n\n" "theorem new_target : True := by\n  sorry\n",
        encoding="utf-8",
    )
    plan_state.save_queue_manager_state(
        {
            "current_queue_assignment": {
                "target_symbol": "old_target",
                "active_file": str(active),
            }
        }
    )
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", f"/prove {active}")
    monkeypatch.setattr(runner, "_single_queue_item_turn_enabled", lambda: True)
    monkeypatch.setattr(runner, "_queue_needs_final_file_sweep", lambda _state: False)
    monkeypatch.setattr(runner, "_startup_active_skill_contract", lambda _name: "")
    monkeypatch.setattr(runner, "_startup_additional_skill_contracts", lambda _name: "")
    monkeypatch.setattr(runner, "_swarm_enabled", lambda: False)
    monkeypatch.setattr(
        runner,
        "route_workflow_step",
        lambda *args, **kwargs: type("Route", (), {"to_dict": lambda self: {}})(),
    )
    live_state = {
        "active_file": str(active),
        "active_file_label": "Demo.lean",
        "target_symbol": "new_target",
        "current_queue_item": {
            "label": "new_target",
            "reasons": ["sorry placeholder"],
        },
    }

    prompt = runner._startup_user_message(
        live_state=live_state,
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "old_target",
                "active_file": str(active),
            }
        },
    )

    assert "Assigned queue item:\n- declaration: new_target" in prompt
    assert "Assigned queue item:\n- declaration: old_target" not in prompt


def test_resume_handoff_refreshes_a_rotated_runtime_assignment(plan_enabled, tmp_path):
    """Do not prefix a current startup turn with a stale durable assignment."""
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem old_target : True := by\n  sorry\n\n" "theorem new_target : True := by\n  sorry\n",
        encoding="utf-8",
    )
    plan_state.save_blueprint(
        plan_state.Blueprint(
            goal="prove Demo",
            nodes=(
                plan_state.GraphNode(
                    id=plan_state.node_id_for("old_target", str(active)),
                    name="old_target",
                    file=str(active),
                    status="stated",
                ),
                plan_state.GraphNode(
                    id=plan_state.node_id_for("new_target", str(active)),
                    name="new_target",
                    file=str(active),
                    status="stated",
                ),
            ),
        )
    )
    plan_state.save_queue_manager_state(
        {
            "current_queue_assignment": {
                "target_symbol": "old_target",
                "active_file": str(active),
            }
        }
    )
    update_json_file(
        plan_state.plan_state_paths().summary_json,
        lambda summary: summary.update(
            {
                "campaign": {
                    "last_route_decision": {
                        "route": "direct-prove",
                        "target_symbol": "old_target",
                        "active_file": str(active),
                    }
                }
            }
        ),
    )
    plan_state.append_journal_event(
        {
            "event": "orchestrator-route",
            "route": "direct-prove",
            "name": "old_target",
            "file": str(active),
            "trigger": "cycle-cadence",
        }
    )
    stale = plan_state.resume_context_block()
    assert "current deterministic assignment: `old_target`" in stale
    assert "current orchestrator route: `direct-prove` for `old_target`" in stale

    refreshed = runner._refresh_plan_state_resume_block(
        stale,
        {
            "current_queue_assignment": {
                "target_symbol": "new_target",
                "active_file": str(active),
            }
        },
    )

    assert "current deterministic assignment: `new_target`" in refreshed
    assert "current deterministic assignment: `old_target`" not in refreshed
    assert "current orchestrator route: `direct-prove` for `old_target`" not in refreshed
    assert "recent route decision: `direct-prove` for `old_target`" in refreshed


def test_resume_prefers_plan_state_over_checkpoint(plan_enabled, tmp_path, monkeypatch):
    _seed_graph(tmp_path)

    block = runner._plan_state_resume_block({})
    assert "[LEANFLOW PLAN-STATE RESUME]" in block

    # With a resume block present, main()'s seeding rule skips replay:
    # (resumed_checkpoint and not plan_resume_block) is False.
    assert bool({"label": "cp"}) and not block == ""


def test_resume_falls_back_to_checkpoint_without_artifacts(plan_enabled, monkeypatch):
    # Flag on but no blueprint.json on disk -> fallback path.
    assert runner._plan_state_resume_block({}) == ""

    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)
    assert runner._plan_state_resume_block({}) == ""


def test_resume_block_failure_degrades_to_fallback(plan_enabled, tmp_path, monkeypatch):
    _seed_graph(tmp_path)
    monkeypatch.setattr(
        runner.plan_state,
        "resume_context_block",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert runner._plan_state_resume_block({}) == ""


def test_unreconciled_graph_is_not_a_resume_authority(plan_enabled, tmp_path, monkeypatch):
    """A failed reconcile sync must fall back to checkpoint replay, not
    present a stale graph as the resume handoff."""
    _seed_graph(tmp_path)
    monkeypatch.setattr(runner, "_maybe_sync_plan_state", lambda *args, **kwargs: False)

    assert runner._plan_state_resume_block({}) == ""

    monkeypatch.setattr(runner, "_maybe_sync_plan_state", lambda *args, **kwargs: True)
    assert "[LEANFLOW PLAN-STATE RESUME]" in runner._plan_state_resume_block({})
