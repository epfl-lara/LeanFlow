"""P1.2 runner-side tests: per-cycle queue->graph sync + reconcile (dark)."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state


@pytest.fixture()
def plan_enabled(monkeypatch, tmp_path):
    state_dir = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Demo.lean")
    monkeypatch.setenv("LEANFLOW_NATIVE_EFFECTIVE_PROMPT", "prove demo")
    return state_dir


def _events(monkeypatch) -> list[tuple[tuple, dict]]:
    events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    return events


def test_sync_noops_when_flag_off(tmp_path, monkeypatch):
    state_dir = tmp_path / "plan-state"
    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))

    runner._maybe_sync_plan_state({"current_queue_assignment": {}}, {})

    assert not state_dir.exists()


def test_sync_creates_proving_node_and_artifacts(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "theorem demo : True := by\n  sorry",
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    bp = plan_state.load_blueprint()
    node = bp.node_by_id(plan_state.node_id_for("demo", str(active)))
    assert node is not None
    assert node.status == "proving"
    summary = plan_state.load_summary()
    assert summary["counters"] == {"proving": 1}
    assert summary["goal"] == "prove demo"
    assert (plan_enabled / "plan.md").is_file()
    assert (plan_enabled / "journal.jsonl").is_file()


def test_gate_backed_outcome_promotes_to_proved_and_reconcile_downgrades(
    plan_enabled, monkeypatch, tmp_path
):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "solved",
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})
    node_id = plan_state.node_id_for("demo", str(active))
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proved"

    # Kernel-truth anti-drift: reinsert a sorry -> downgraded within one sync,
    # and the stale 'solved' outcome is retired so it can never re-promote.
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"
    assert any(args[0] == "plan-graph-reconcile" for args, _kwargs in events)
    outcome = dict(autonomy_state["theorem_outcomes"])
    assert list(outcome.values())[0]["status"] == "reverted-to-sorry"

    # No flapping: a further sync keeps the node downgraded.
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})
    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"


def test_stale_solved_outcome_never_promotes_dirty_declaration(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    # Declaration is dirty from the start: a stale solved outcome must not
    # produce a proved node at any point.
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "solved",
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("demo", str(active)))
    assert node.status != "proved"


def test_vanished_declaration_downgrades_proved_node(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "solved",
            }
        }
    }
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})
    node_id = plan_state.node_id_for("demo", str(active))
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proved"

    # The declaration disappears but the file stays readable.
    active.write_text("-- everything deleted\n", encoding="utf-8")
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    assert plan_state.load_blueprint().node_by_id(node_id).status == "conjectured"


def test_reverted_outcome_moves_proving_node_back_to_stated(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    # Cycle 1: theorem is the active assignment -> proving.
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "theorem demo : True := by\n  sorry",
        }
    }
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    # Cycle 2: manager moved on after a baseline restore.
    autonomy_state = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "reverted-to-sorry",
            }
        }
    }
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("demo", str(active)))
    assert node.status == "stated"


def test_blocked_outcome_marks_node_blocked(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "blocked",
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("demo", str(active)))
    assert node.status == "blocked"


def test_clean_declaration_is_never_promoted_without_gate(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "theorem demo : True := by\n  trivial",
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("demo", str(active)))
    # Clean on disk but no gate accept: stays proving, never proved.
    assert node.status == "proving"


# ---------------------------------------------------------------------------
# Queue-drain outcome for the LAST theorem (the transition path never fires for
# it — nothing follows it — so its gate-backed 'solved' outcome is recorded on
# drain, then the ORDINARY gate-accept sync promotes it).
# ---------------------------------------------------------------------------


def _drained_verified_live_state(active) -> dict[str, Any]:
    # No current_queue_item => the queue has drained.
    return {"active_file": str(active), "goals": "no goals", "build_status": "ok"}


def test_drain_records_gate_backed_outcome_and_sync_promotes_last_theorem(
    plan_enabled, monkeypatch, tmp_path
):
    """The last theorem: no transition fires, so a per-cycle sync leaves it
    'proving'. On drain its 'solved' outcome is recorded and the ordinary sync
    promotes it via the SAME gate-accept path (present + sorry-free + error-free
    disk check) — not a bypass. via_gate=True is journaled."""
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda ls: True)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem last_thm : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "last_thm",
            "active_file": str(active),
            "slice": "theorem last_thm : True := by\n  trivial",
        }
    }
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})
    node_id = plan_state.node_id_for("last_thm", str(active))
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"

    live = _drained_verified_live_state(active)
    assert runner._maybe_record_drain_theorem_outcome(autonomy_state, live, []) is True
    runner._maybe_sync_plan_state(autonomy_state, live)
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proved"

    events = [
        line
        for line in (plan_enabled / "journal.jsonl").read_text().splitlines()
        if '"node-status"' in line and '"to": "proved"' in line
    ]
    assert events and all('"via_gate": true' in line for line in events)

    # Idempotent: a second drain call records nothing more.
    assert runner._maybe_record_drain_theorem_outcome(autonomy_state, live, []) is False


def test_drain_overwrites_a_stale_non_solved_outcome(plan_enabled, monkeypatch, tmp_path):
    """A revisited theorem carrying a stale 'blocked' outcome that solves on the
    final drain must be OVERWRITTEN to 'solved' (matching the transition path),
    not skipped by the idempotency guard."""
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda ls: True)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem last_thm : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {"target_symbol": "last_thm", "active_file": str(active)}
    }
    runner._record_theorem_outcome(
        autonomy_state,
        {"target_symbol": "last_thm", "active_file": str(active), "status": "blocked"},
    )

    assert (
        runner._maybe_record_drain_theorem_outcome(
            autonomy_state, _drained_verified_live_state(active), []
        )
        is True
    )
    mgr = runner._queue_manager_from_state(autonomy_state)
    assert mgr.outcome_for(runner._queue_key("last_thm", str(active))).status == "solved"


def test_solved_outcome_never_resurrects_a_false_node(plan_enabled, monkeypatch, tmp_path):
    """Kernel-truth: a stale 'solved' outcome never overrides a kernel-`false`
    node (negation promotion wins)."""
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("demo", str(active))
    bp = plan_state.load_blueprint().replace_node(
        plan_state.GraphNode(id=node_id, name="demo", file=str(active), status="false")
    )
    plan_state.save_blueprint(bp)
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "solved",
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})
    assert plan_state.load_blueprint().node_by_id(node_id).status == "false"


def test_drive_followups_wrapper_promotes_last_theorem_on_any_exit(
    plan_enabled, monkeypatch, tmp_path
):
    """Integration: _drive_autonomous_followups wraps the loop so that HOWEVER
    it exits (verified stop, ceiling, stall), a drained+verified last theorem is
    recorded and synced. Stubbing the inner loop to return a verified-drained
    state exercises the wrapper end-to-end."""
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda ls: True)
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem last_thm : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {"target_symbol": "last_thm", "active_file": str(active)}
    }
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})
    node_id = plan_state.node_id_for("last_thm", str(active))
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proving"

    live = _drained_verified_live_state(active)  # no current item => drained
    monkeypatch.setattr(
        runner, "_drive_autonomous_followups_inner", lambda *a, **k: ([], {}, {}, live)
    )
    runner._drive_autonomous_followups(None, "", [], {}, {}, autonomy_state)

    assert plan_state.load_blueprint().node_by_id(node_id).status == "proved"


def test_drain_does_not_record_when_not_verified(plan_enabled, monkeypatch, tmp_path):
    """Guarded on a fresh verified live state: an unverified drain records no
    outcome (so nothing can promote)."""
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda ls: False)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem last_thm : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {"target_symbol": "last_thm", "active_file": str(active)}
    }
    assert (
        runner._maybe_record_drain_theorem_outcome(
            autonomy_state, _drained_verified_live_state(active), []
        )
        is False
    )


def test_drain_skips_when_queue_not_drained(plan_enabled, monkeypatch, tmp_path):
    """A live 'current' item means the ordinary per-transition path still owns
    the outcome — the drain recorder must not fire."""
    monkeypatch.setattr(runner, "_live_state_is_verified", lambda ls: True)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem last_thm : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {"target_symbol": "last_thm", "active_file": str(active)}
    }
    live = {
        "active_file": str(active),
        "current_queue_item": {"label": "last_thm"},  # not drained
    }
    assert runner._maybe_record_drain_theorem_outcome(autonomy_state, live, []) is False


def test_unchanged_graph_skips_rewrites(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "theorem demo : True := by\n  sorry",
        }
    }
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})
    revision = plan_state.load_blueprint().revision

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    assert plan_state.load_blueprint().revision == revision


def test_sync_failure_is_loud_but_not_fatal(plan_enabled, monkeypatch):
    events = _events(monkeypatch)
    monkeypatch.setattr(
        runner.plan_state,
        "load_blueprint",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    runner._maybe_sync_plan_state({}, {})

    assert any(args[0] == "plan-state-sync-error" for args, _kwargs in events)


def test_collect_declaration_truth_reads_sorry_and_active_errors(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem clean_thm : True := by\n  trivial\n\n" "theorem sorried : True := by\n  sorry\n",
        encoding="utf-8",
    )

    truth = runner._collect_declaration_truth(
        [str(active), str(tmp_path / "Missing.lean")],
        {
            "active_file": str(active),
            "diagnostics": f"{active}:1:0: error: unsolved goals",
        },
    )

    assert truth[(str(active), "clean_thm")].has_error_diag is True
    assert truth[(str(active), "clean_thm")].has_sorry is False
    assert truth[(str(active), "sorried")].has_sorry is True
    assert all(file != str(tmp_path / "Missing.lean") for file, _name in truth)
