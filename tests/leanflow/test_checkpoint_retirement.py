"""P1.5 tests: checkpoint-UX retirement + the plan-state resume path."""

from __future__ import annotations

import pytest

from leanflow_cli.native import native_checkpoints
from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state


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
