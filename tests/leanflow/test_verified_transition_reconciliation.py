"""Verified queue-transition graph synchronization tests."""

from __future__ import annotations

import hashlib

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state, verified_transition_reconciliation


def _transition(file: str) -> dict[str, str]:
    return {
        "previous_target": "scale_three_unit_fraction",
        "previous_file": file,
        "current_target": "main_target",
        "current_file": file,
    }


def _outcome(file: str, *, status: str = "solved") -> dict[str, object]:
    return {
        "target_symbol": "scale_three_unit_fraction",
        "active_file": file,
        "status": status,
        "last_verification": {
            "scope": "target:scale_three_unit_fraction",
            "target": "scale_three_unit_fraction",
            "ok": True,
            "errors": 0,
            "warnings": 1,
            "sorry": 0,
            "axiom_profile_checked": True,
            "axiom_profile_blockers": [],
        },
    }


def _live_state(file: str) -> dict[str, object]:
    return {
        "active_file": file,
        "active_file_label": file,
        "target_symbol": "main_target",
        "current_queue_item": {"label": "main_target", "reasons": ["contains sorry"]},
        "current_queue_item_slice": "theorem main_target : True := by\n  sorry",
        "declaration_queue_summary": "- main_target — contains sorry",
        "current_blocker": "main_target contains sorry",
    }


@pytest.mark.parametrize(
    ("accepted", "status", "current_target"),
    [
        (False, "solved", "main_target"),
        (True, "unverified", "main_target"),
        (True, "solved", "different_target"),
    ],
)
def test_verified_transition_sync_rejects_non_authoritative_boundaries(
    tmp_path, accepted, status, current_target
):
    active = str(tmp_path / "Main.lean")
    live_state = _live_state(active)
    live_state["current_queue_item"] = {
        "label": current_target,
        "reasons": ["contains sorry"],
    }

    request = verified_transition_reconciliation.verified_transition_sync(
        transition=_transition(active),
        outcome=_outcome(active, status=status),
        live_state=live_state,
        verification_accepted=accepted,
    )

    assert request is None


def test_verified_transition_sync_preserves_the_live_next_assignment(tmp_path):
    active = str(tmp_path / "Main.lean")

    request = verified_transition_reconciliation.verified_transition_sync(
        transition=_transition(active),
        outcome=_outcome(active),
        live_state=_live_state(active),
        verification_accepted=True,
    )

    assert request is not None
    assert request.completed_target == "scale_three_unit_fraction"
    assert request.assignment_mapping() == {
        "target_symbol": "main_target",
        "active_file": active,
        "slice": "theorem main_target : True := by\n  sorry",
    }


def test_warning_accepted_transition_promotes_graph_before_next_target_warmup(
    monkeypatch, tmp_path
):
    """Reproduce the Erdős run's formerly stale 88-second graph interval."""
    state_dir = tmp_path / "plan-state"
    active = tmp_path / "Main.lean"
    old_source = (
        "private lemma scale_three_unit_fraction (hk : 0 < 1) : True := by\n"
        "  sorry\n\n"
        "theorem main_target : True := by\n"
        "  sorry\n"
    )
    current_source = (
        "private lemma scale_three_unit_fraction (hk : 0 < 1) : True := by\n"
        "  trivial\n\n"
        "theorem main_target : True := by\n"
        "  sorry\n"
    )
    active.write_text(current_source, encoding="utf-8")
    file = str(active)
    completed_id = plan_state.node_id_for("scale_three_unit_fraction", file)

    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", "prove Main.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    monkeypatch.setenv("LEANFLOW_NATIVE_RUNNER_OWNER", "run-live")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=completed_id,
                    kind="lemma",
                    name="scale_three_unit_fraction",
                    file=file,
                    statement=old_source.split("\n\ntheorem", 1)[0],
                    source_sha256=hashlib.sha256(old_source.encode()).hexdigest(),
                    status="proving",
                    owner="run-live",
                    generated_by="decomposer",
                ),
            )
        )
    )
    events: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "scale_three_unit_fraction",
            "active_file": file,
            "slice": old_source.split("\n\ntheorem", 1)[0],
        },
        "last_verification": _outcome(file)["last_verification"],
    }
    live_state = _live_state(file)

    rebuilt, transition = runner._rebuild_history_for_theorem_transition(
        [{"role": "assistant", "content": "warning cleanup inspected; advance"}],
        {"snapshot_text": "compact"},
        autonomy_state,
        live_state,
    )

    assert rebuilt is not None
    assert transition == _transition(file)
    completed = plan_state.load_blueprint().node_by_id(completed_id)
    assert completed is not None
    assert completed.status == "proved"
    assert completed.owner == ""
    assert "trivial" in completed.statement
    assert "sorry" not in completed.statement
    assert completed.source_sha256 == hashlib.sha256(active.read_bytes()).hexdigest()
    current = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("main_target", file))
    assert current is not None
    assert current.status == "proving"
    assert current.owner == "run-live"
    assert plan_state.load_summary()["counters"] == {"proved": 1, "proving": 1}
    assert any(
        args[0] == "plan-graph-verified-transition-synced"
        and kwargs["before_next_target_warmup"] is True
        for args, kwargs in events
    )

    journal_before = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert journal_before.count('"to": "proved"') == 1

    # The ordinary post-warmup sync is idempotent: it must not promote or
    # count the completed node a second time.
    autonomy_state["current_queue_assignment"] = {
        "target_symbol": "main_target",
        "active_file": file,
        "slice": "theorem main_target : True := by\n  sorry",
    }
    runner._maybe_sync_plan_state(autonomy_state, live_state)
    journal_after = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert journal_after.count('"to": "proved"') == 1
