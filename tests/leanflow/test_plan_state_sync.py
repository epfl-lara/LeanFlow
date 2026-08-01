"""P1.2 runner-side tests: per-cycle queue->graph sync + reconcile (dark)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.workflow_json_io import update_json_file


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


def _accepted_target_verification(target: str) -> dict[str, object]:
    return {
        "scope": f"target:{target}",
        "target": target,
        "ok": True,
        "errors": 0,
        "sorry": 0,
        "tool": "lean_incremental_check",
    }


def _strict_resume_exact_payload(
    active: Path,
    target: str,
    *,
    inline_axioms: list[str] | None = None,
) -> dict[str, Any]:
    """Return one complete exact-target payload suitable for cache authority."""
    incremental: dict[str, Any] = {
        "success": True,
        "ok": True,
        "action": "check_target",
        "file": str(active),
        "target": target,
        "has_errors": False,
        "has_sorry": False,
        "messages": [],
        "errors": 0,
        "sorry": 0,
    }
    if inline_axioms is not None:
        rendered_axioms = ", ".join(inline_axioms)
        incremental.update(
            {
                "axiom_profile_checked": True,
                "axiom_profile_requested_target": target,
                "axiom_profile_target": target,
                "axiom_profile_declaration_sha256": "a" * 64,
                "axiom_profile_axioms": inline_axioms,
                "axiom_profile_output": (
                    f"depends on axioms: {rendered_axioms}"
                    if inline_axioms
                    else "does not depend on any axioms"
                ),
                "axiom_profile_error": "",
            }
        )
    return {
        "ok": True,
        "mode": "incremental_target",
        "target": target,
        "incremental": incremental,
    }


def _graph_node(
    name: str,
    active_file: str,
    *,
    statement: str,
    status: str,
) -> plan_state.GraphNode:
    return plan_state.GraphNode(
        id=plan_state.node_id_for(name, active_file),
        kind="lemma",
        name=name,
        file=active_file,
        statement=statement,
        status=status,
    )


def _helper_parent_edges(
    helper: plan_state.GraphNode,
    parent: plan_state.GraphNode,
) -> tuple[plan_state.GraphEdge, plan_state.GraphEdge]:
    return (
        plan_state.GraphEdge(source=helper.id, target=parent.id, kind="split_of"),
        plan_state.GraphEdge(source=parent.id, target=helper.id, kind="depends_on"),
    )


def test_resume_reconciles_multiple_historical_finite_branch_resets(
    plan_enabled, monkeypatch, tmp_path
):
    """Old singleton-residue resets cannot postpone a due epoch refresh."""
    events = _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "finite-branch-resume")
    active = tmp_path / "Demo.lean"
    prior_specs = ((5, 2), (5, 3), (11, 6), (13, 1))
    false_specs = ((47, 40), (3529, 21))
    active.write_text(
        "".join(
            f"lemma residue_{modulus}_{residue} (t : Nat) "
            f"(hmod : t % {modulus} = {residue}) : True := by\n  trivial\n\n"
            for modulus, residue in (*prior_specs, *false_specs)
        )
        + "theorem parent : True := by\n  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _graph_node("parent", file, statement="True", status="proving")
    helpers = tuple(
        _graph_node(
            f"residue_{modulus}_{residue}",
            file,
            statement=f"(t : Nat) (hmod : t % {modulus} = {residue}) : True",
            status="proved",
        )
        for modulus, residue in (*prior_specs, *false_specs)
    )
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(parent, *helpers),
            edges=tuple(
                edge for helper in helpers for edge in _helper_parent_edges(helper, parent)
            ),
        )
    )
    proved_times = (
        "2026-07-17T17:20:00+00:00",
        "2026-07-17T17:20:01+00:00",
        "2026-07-17T17:20:02+00:00",
        "2026-07-17T17:20:03+00:00",
        "2026-07-17T17:32:10+00:00",
        "2026-07-17T17:40:12+00:00",
    )
    plan_state.plan_state_paths().journal_jsonl.write_text(
        "\n".join(
            json.dumps(
                {
                    "event": "node-status",
                    "node_id": helper.id,
                    "from": "proving",
                    "to": "proved",
                    "via_gate": True,
                    "ts": proved_at,
                }
            )
            for helper, proved_at in zip(helpers, proved_times, strict=True)
        )
        + "\n",
        encoding="utf-8",
    )
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "parent",
            "active_file": file,
            "slice": "theorem parent : True := by\n  sorry",
        }
    }
    runner.campaign_epoch.ensure_campaign(autonomy_state)
    false_node_ids = [helper.id for helper in helpers[-2:]]

    def seed_historical_resets(summary):
        campaign = dict(summary["campaign"])
        campaign.pop("finite_branch_progress_policy_version", None)
        campaign["epoch"] = 22
        campaign["no_progress_route_limit"] = 4
        campaign["no_progress_route_streak"] = 0
        campaign["epoch_routes"] = [
            {"route": route, "decided_at": decided_at}
            for route, decided_at in (
                ("decompose", "2026-07-17T17:27:06+00:00"),
                ("plan", "2026-07-17T17:29:43+00:00"),
                ("plan", "2026-07-17T17:32:58+00:00"),
                ("plan", "2026-07-17T17:33:24+00:00"),
                ("decompose", "2026-07-17T17:37:25+00:00"),
            )
        ]
        campaign["last_verified_graph_progress"] = {
            "accounting": "parent-scoped-proof-mechanism",
            "node_ids": [false_node_ids[-1]],
            "recorded_at": "2026-07-17T17:40:12+00:00",
        }
        campaign["verified_mechanisms"] = {
            "version": 1,
            "entries": {
                "parent:finite-branches": {
                    "first_node_id": false_node_ids[0],
                    "last_node_id": false_node_ids[-1],
                    "seen_node_ids": false_node_ids,
                    "seen_count": len(false_node_ids),
                }
            },
        }
        summary["campaign"] = campaign

    update_json_file(runner.campaign_epoch._summary_path(), seed_historical_resets)

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": file}) is True

    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 4
    assert "last_verified_graph_progress" not in campaign
    reconciliation = campaign["finite_branch_progress_reconciliation"]
    assert reconciliation["false_reset_node_ids"] == false_node_ids
    assert reconciliation["reconstructed_streak"] == 5
    assert reconciliation["repaired_streak"] == 4
    assert reconciliation["false_reset_predates_epoch_routes"] is False
    assert campaign["verified_mechanisms"]["entries"] == {}
    assert autonomy_state["campaign_epoch_requested"] == "route-no-graph-progress"
    assert any(args[0] == "campaign-finite-branch-progress-reconciled" for args, _ in events)

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": file})
    assert sum(args[0] == "campaign-finite-branch-progress-reconciled" for args, _ in events) == 1


def test_resume_reclassifies_closed_target_case_and_requests_due_rollover(
    plan_enabled,
    monkeypatch,
    tmp_path,
):
    """The live case-k=1 false reset is repaired by the v3 policy migration."""
    events = _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "closed-target-case-resume")
    active = tmp_path / "Demo.lean"
    target = "demo_residual"
    helper_name = f"{target}_case_k_eq_1"
    active.write_text(
        f"private lemma {helper_name} :\n"
        "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
        "      (4 / ((24 * 1 + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by\n"
        "  exact case_certificate\n\n"
        f"private lemma {target} (k : ℕ) : ∃ x : ℕ, x = k := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _graph_node(
        target,
        file,
        statement="(k : ℕ) : ∃ x : ℕ, x = k",
        status="proving",
    )
    helper = _graph_node(
        helper_name,
        file,
        statement="∃ x y z : ℕ, target_instance 1 x y z",
        status="proved",
    )
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(parent, helper),
            edges=_helper_parent_edges(helper, parent),
        )
    )
    plan_state.plan_state_paths().journal_jsonl.write_text(
        json.dumps(
            {
                "event": "node-status",
                "node_id": helper.id,
                "from": "proving",
                "to": "proved",
                "via_gate": True,
                "ts": "2026-07-18T10:02:06+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": target,
            "active_file": file,
            "slice": f"private lemma {target} (k : ℕ) : ∃ x : ℕ, x = k := by sorry",
        }
    }
    runner.campaign_epoch.ensure_campaign(autonomy_state)

    def seed_false_case_reset(summary):
        campaign = dict(summary["campaign"])
        campaign["finite_branch_progress_policy_version"] = 2
        campaign["epoch"] = 28
        campaign["no_progress_route_limit"] = 4
        campaign["no_progress_route_streak"] = 0
        campaign["epoch_routes"] = [
            {"route": route, "decided_at": decided_at}
            for route, decided_at in (
                ("plan", "2026-07-18T09:18:00+00:00"),
                ("decompose", "2026-07-18T09:45:00+00:00"),
                ("negate", "2026-07-18T09:52:00+00:00"),
                ("plan", "2026-07-18T09:56:00+00:00"),
            )
        ]
        campaign["last_verified_graph_progress"] = {
            "accounting": "parent-scoped-proof-mechanism",
            "node_ids": [helper.id],
            "recorded_at": "2026-07-18T10:02:07+00:00",
        }
        campaign["verified_mechanisms"] = {
            "version": 1,
            "entries": {
                "parent:closed-target-case": {
                    "first_node_id": helper.id,
                    "last_node_id": helper.id,
                    "seen_node_ids": [helper.id],
                    "seen_count": 1,
                }
            },
        }
        summary["campaign"] = campaign

    update_json_file(runner.campaign_epoch._summary_path(), seed_false_case_reset)

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": file}) is True

    campaign = runner.campaign_epoch.campaign_snapshot()
    reconciliation = campaign["finite_branch_progress_reconciliation"]
    assert campaign["finite_branch_progress_policy_version"] == 3
    assert campaign["no_progress_route_streak"] == 4
    assert "last_verified_graph_progress" not in campaign
    assert campaign["verified_mechanisms"]["entries"] == {}
    assert reconciliation["false_reset_node_ids"] == [helper.id]
    assert reconciliation["previous_streak"] == 0
    assert reconciliation["reconstructed_streak"] == 4
    assert reconciliation["repaired_streak"] == 4
    assert reconciliation["rollover_required"] is True
    assert autonomy_state["orchestrator_routes_used"] == 4
    assert autonomy_state["campaign_epoch_requested"] == "route-no-graph-progress"
    assert sum(args[0] == "campaign-finite-branch-progress-reconciled" for args, _ in events) == 1

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": file})
    assert sum(args[0] == "campaign-finite-branch-progress-reconciled" for args, _ in events) == 1


def test_resume_cleans_pre_epoch_finite_branch_anchor_without_route_debt(
    plan_enabled, monkeypatch, tmp_path
):
    """An epoch-22 false anchor is cleaned after epoch 23 already started."""
    events = _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "post-rollover-finite-branch")
    active = tmp_path / "Demo.lean"
    prior_specs = ((5, 2), (5, 3), (11, 6), (13, 1))
    stale_specs = ((47, 40), (3529, 21))
    active.write_text(
        "".join(
            f"lemma residue_{modulus}_{residue} (t : Nat) "
            f"(hmod : t % {modulus} = {residue}) : True := by\n  trivial\n\n"
            for modulus, residue in (*prior_specs, *stale_specs)
        )
        + "theorem parent : True := by\n  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _graph_node("parent", file, statement="True", status="proving")
    helpers = tuple(
        _graph_node(
            f"residue_{modulus}_{residue}",
            file,
            statement=f"(t : Nat) (hmod : t % {modulus} = {residue}) : True",
            status="proved",
        )
        for modulus, residue in (*prior_specs, *stale_specs)
    )
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(parent, *helpers),
            edges=tuple(
                edge for helper in helpers for edge in _helper_parent_edges(helper, parent)
            ),
        )
    )
    proved_times = (
        "2026-07-17T17:20:00+00:00",
        "2026-07-17T17:20:01+00:00",
        "2026-07-17T17:20:02+00:00",
        "2026-07-17T17:20:03+00:00",
        "2026-07-17T17:32:10+00:00",
        "2026-07-17T17:40:12+00:00",
    )
    plan_state.plan_state_paths().journal_jsonl.write_text(
        "\n".join(
            json.dumps(
                {
                    "event": "node-status",
                    "node_id": helper.id,
                    "from": "proving",
                    "to": "proved",
                    "via_gate": True,
                    "ts": proved_at,
                }
            )
            for helper, proved_at in zip(helpers, proved_times, strict=True)
        )
        + "\n",
        encoding="utf-8",
    )
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "parent",
            "active_file": file,
            "slice": "theorem parent : True := by\n  sorry",
        }
    }
    runner.campaign_epoch.ensure_campaign(autonomy_state)
    stale_node_ids = [helper.id for helper in helpers[-2:]]

    def seed_post_rollover_anchor(summary):
        campaign = dict(summary["campaign"])
        campaign["finite_branch_progress_policy_version"] = 1
        campaign["epoch"] = 23
        campaign["no_progress_route_limit"] = 4
        campaign["no_progress_route_streak"] = 1
        campaign["epoch_routes"] = [
            {"route": "decompose", "decided_at": "2026-07-17T19:00:00+00:00"}
        ]
        campaign["last_verified_graph_progress"] = {
            "accounting": "parent-scoped-proof-mechanism",
            "node_ids": [stale_node_ids[-1]],
            "recorded_at": "2026-07-17T17:40:12+00:00",
        }
        campaign["verified_mechanisms"] = {
            "version": 1,
            "entries": {
                "parent:stale-finite-branch": {
                    "first_node_id": stale_node_ids[0],
                    "last_node_id": stale_node_ids[-1],
                    "seen_node_ids": stale_node_ids,
                    "seen_count": len(stale_node_ids),
                }
            },
        }
        summary["campaign"] = campaign

    update_json_file(runner.campaign_epoch._summary_path(), seed_post_rollover_anchor)

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": file}) is True

    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 1
    assert "last_verified_graph_progress" not in campaign
    assert campaign["verified_mechanisms"]["entries"] == {}
    reconciliation = campaign["finite_branch_progress_reconciliation"]
    assert reconciliation["false_reset_node_ids"] == stale_node_ids
    assert reconciliation["false_reset_predates_epoch_routes"] is True
    assert reconciliation["previous_streak"] == 1
    assert reconciliation["reconstructed_streak"] == 1
    assert reconciliation["repaired_streak"] == 1
    assert "campaign_epoch_requested" not in autonomy_state
    assert any(args[0] == "campaign-finite-branch-progress-reconciled" for args, _ in events)


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
                "last_verification": _accepted_target_verification("demo"),
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})
    node_id = plan_state.node_id_for("demo", str(active))
    proved = plan_state.load_blueprint().node_by_id(node_id)
    assert proved is not None
    assert proved.status == "proved"
    assert proved.statement == "theorem demo : True := by\n  trivial"
    assert proved.source_sha256 == hashlib.sha256(active.read_bytes()).hexdigest()

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


def test_verified_helper_graph_progress_resets_campaign_route_streak(
    plan_enabled, monkeypatch, tmp_path
):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("lemma helper : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "orchestrator_routes_used": 3,
        "theorem_outcomes": {
            f"{active}::helper": {
                "target_symbol": "helper",
                "active_file": str(active),
                "status": "solved",
                "last_verification": _accepted_target_verification("helper"),
            }
        },
    }

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)}) is True

    node_id = plan_state.node_id_for("helper", str(active))
    assert plan_state.load_blueprint().node_by_id(node_id).status == "proved"
    assert autonomy_state["orchestrator_routes_used"] == 0
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 0
    assert campaign["last_verified_graph_progress"]["node_ids"] == [node_id]


def test_conditional_bridge_stays_proved_without_resetting_campaign_streak(
    plan_enabled, monkeypatch, tmp_path
):
    """A kernel-valid bridge cannot spend an unrepresented theorem premise as progress."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma conditional_bridge\n"
        "    (h_family : ∀ s : ℕ, ∃ x : ℕ, x = 840 * s + 169)\n"
        "    (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : Witness k := by\n"
        "  exact buildWitness h_family k hk hmod\n\n"
        "theorem residual (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : "
        "Witness k := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    helper = _graph_node(
        "conditional_bridge",
        str(active),
        statement="(…) : Witness k",
        status="stated",
    )
    parent = _graph_node(
        "residual",
        str(active),
        statement="(k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : Witness k",
        status="proving",
    )
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(helper, parent),
            edges=_helper_parent_edges(helper, parent),
        )
    )
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "residual",
            "active_file": str(active),
            "slice": "theorem residual (k : ℕ) : Witness k := by sorry",
        },
        "theorem_outcomes": {
            f"{active}::conditional_bridge": {
                "target_symbol": "conditional_bridge",
                "active_file": str(active),
                "status": "solved",
                "last_verification": _accepted_target_verification("conditional_bridge"),
            }
        },
    }
    for route in ("plan", "decompose"):
        runner.campaign_epoch.record_route_decision(
            autonomy_state,
            route=route,
            target_symbol="residual",
            active_file=str(active),
        )

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)}) is True

    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(helper.id).status == "proved"
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 2
    assert "last_verified_graph_progress" not in campaign
    assert campaign["conditional_helper_progress"]["deferred_node_ids"] == [helper.id]
    assert "verified_mechanisms" not in campaign
    assert autonomy_state["orchestrator_routes_used"] == 2
    deferred = [
        kwargs for args, kwargs in events if args[0] == "plan-graph-conditional-helper-deferred"
    ]
    assert len(deferred) == 1
    assert deferred[0]["node_id"] == helper.id
    assert deferred[0]["campaign_progress"] is False

    active.write_text(
        "lemma conditional_bridge\n"
        "    (h_family : ∀ s : ℕ, ∃ x : ℕ, x = 840 * s + 169)\n"
        "    (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : Witness k := by\n"
        "  exact buildWitness h_family k hk hmod\n\n"
        "theorem residual (k : ℕ) (hk : 1 ≤ k) (hmod : k % 7 = 0) : "
        "Witness k := by\n"
        "  apply conditional_bridge\n"
        "  · sorry\n"
        "  · exact k\n"
        "  · exact hk\n"
        "  · exact hmod\n",
        encoding="utf-8",
    )

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)}) is True

    released_campaign = runner.campaign_epoch.campaign_snapshot()
    assert released_campaign["no_progress_route_streak"] == 0
    assert released_campaign["last_verified_graph_progress"]["node_ids"] == [helper.id]
    assert released_campaign["conditional_helper_progress"]["deferred_node_ids"] == []


def test_nonreducing_existential_wrapper_stays_proved_without_campaign_credit(
    plan_enabled, monkeypatch, tmp_path
):
    """A transformed certificate cannot reset the route streak by moving the hard exists."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "private lemma assembly_inputs\n"
        "    (q : ℕ) (hmod : q % 5 = 2) :\n"
        "    ∃ x p₁ p₂ : ℕ,\n"
        "      let n := 168 * q + 25\n"
        "      let Q := 4 * x - n\n"
        "      let B := n * x\n"
        "      1 ≤ x ∧ 0 < Q ∧ p₁ * p₂ = B ^ 2 ∧\n"
        "        Q ∣ (B + p₁) ∧ Q ∣ (B + p₂) ∧\n"
        "        x < (B + p₁) / Q ∧ (B + p₁) / Q < (B + p₂) / Q := by\n"
        "  exact buildAssembly q hmod\n\n"
        "theorem residual (q : ℕ) (hmod : q % 5 = 2) :\n"
        "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
        "      (4 / ((168 * q + 25 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    helper = _graph_node(
        "assembly_inputs",
        file,
        statement="same-premise transformed certificate",
        status="stated",
    )
    parent = _graph_node(
        "residual",
        file,
        statement="original existential target",
        status="proving",
    )
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(helper, parent),
            edges=_helper_parent_edges(helper, parent),
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::assembly_inputs": {
                "target_symbol": "assembly_inputs",
                "active_file": file,
                "status": "solved",
                "last_verification": _accepted_target_verification("assembly_inputs"),
            }
        }
    }
    for route in ("decompose", "direct-prove"):
        runner.campaign_epoch.record_route_decision(
            autonomy_state,
            route=route,
            target_symbol="residual",
            active_file=file,
        )

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": file}) is True

    assert plan_state.load_blueprint().node_by_id(helper.id).status == "proved"
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 2
    assert "last_verified_graph_progress" not in campaign
    assert campaign["conditional_helper_progress"]["deferred_node_ids"] == [helper.id]
    deferred = [
        kwargs for args, kwargs in events if args[0] == "plan-graph-nonreducing-helper-deferred"
    ]
    assert len(deferred) == 1
    assert deferred[0]["node_id"] == helper.id
    assert deferred[0]["reason_code"] == "nonreducing_existential_wrapper"
    assert deferred[0]["parent_obligation_profile"]["logical_atoms"] == 4
    assert deferred[0]["helper_obligation_profile"]["logical_atoms"] == 7
    assert deferred[0]["campaign_progress"] is False


def test_verified_helper_progress_reopens_prior_blocked_parent(plan_enabled, monkeypatch, tmp_path):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma new_helper : True := by\n  trivial\n\n"
        "theorem blocked_parent : True := by\n  sorry\n",
        encoding="utf-8",
    )
    helper_id = plan_state.node_id_for("new_helper", str(active))
    parent_id = plan_state.node_id_for("blocked_parent", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=helper_id,
                    name="new_helper",
                    file=str(active),
                    statement="True",
                    status="stated",
                ),
                plan_state.GraphNode(
                    id=parent_id,
                    name="blocked_parent",
                    file=str(active),
                    statement="True",
                    status="blocked",
                ),
            )
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::new_helper": {
                "target_symbol": "new_helper",
                "active_file": str(active),
                "status": "solved",
                "last_verification": _accepted_target_verification("new_helper"),
            },
            f"{active}::blocked_parent": {
                "target_symbol": "blocked_parent",
                "active_file": str(active),
                "status": "blocked",
                "note": "waiting for a useful lemma",
            },
        }
    }

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)}) is True

    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(helper_id).status == "proved"
    assert blueprint.node_by_id(parent_id).status == "stated"
    parent_outcome = autonomy_state["theorem_outcomes"][f"{active}::blocked_parent"]
    assert parent_outcome["status"] == "unresolved"
    assert any(args[0] == "queue-blocked-outcomes-reopened" for args, _kwargs in events)


def test_verified_covered_helper_does_not_reset_campaign_route_streak(
    plan_enabled, monkeypatch, tmp_path
):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    existing_name = "residual_five_easy_mod_five"
    covered_name = "residual_k_eq_35_mul_s_add_19"
    existing_statement = (
        "(t : ℕ) (hcase : t % 5 = 2 ∨ t % 5 = 3 ∨ t % 5 = 4) : "
        "∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧ "
        "(4 / ((168 * t + 121 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z"
    )
    covered_statement = (
        "(s : ℕ) : ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧ "
        "(4 / ((24 * (35 * s + 19) + 1 : ℕ) : ℚ)) = "
        "1 / x + 1 / y + 1 / z"
    )
    active.write_text(
        f"lemma {existing_name} {existing_statement} := by\n  trivial\n\n"
        f"lemma {covered_name} {covered_statement} := by\n  trivial\n",
        encoding="utf-8",
    )
    existing_id = plan_state.node_id_for(existing_name, str(active))
    covered_id = plan_state.node_id_for(covered_name, str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=existing_id,
                    name=existing_name,
                    file=str(active),
                    statement=existing_statement,
                    status="proved",
                ),
                plan_state.GraphNode(
                    id=covered_id,
                    name=covered_name,
                    file=str(active),
                    statement=covered_statement,
                    status="stated",
                ),
            )
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::{covered_name}": {
                "target_symbol": covered_name,
                "active_file": str(active),
                "status": "solved",
                "last_verification": _accepted_target_verification(covered_name),
            }
        }
    }
    for route in ("direct-prove", "decompose"):
        runner.campaign_epoch.record_route_decision(autonomy_state, route=route)

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)}) is True

    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(covered_id).status == "proved"
    assert plan_state.load_summary()["counters"]["proved"] == 2
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 2
    assert "last_verified_graph_progress" not in campaign
    assert autonomy_state["orchestrator_routes_used"] == 2
    covered_events = [kwargs for args, kwargs in events if args[0] == "plan-graph-covered-progress"]
    assert len(covered_events) == 1
    assert covered_events[0]["node_id"] == covered_id
    assert covered_events[0]["campaign_progress"] is False
    assert (
        "already covers the proposed arithmetic subfamily" in covered_events[0]["coverage_reason"]
    )


def test_same_parent_same_mechanism_helper_does_not_delay_pending_epoch_rollover(
    plan_enabled, monkeypatch, tmp_path
):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma factor_pair (n : Nat) : True := by\n"
        "  trivial\n\n"
        "lemma residue_eleven (n : Nat) (h : n % 17 = 11) : True := by\n"
        "  exact factor_pair n\n\n"
        "lemma residue_thirteen (n : Nat) (h : n % 17 = 13) : True := by\n"
        "  exact factor_pair n\n\n"
        "lemma residue_nine (n : Nat) (h : n % 17 = 9) : True := by\n"
        "  simpa using factor_pair n\n\n"
        "theorem parent : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _graph_node("parent", file, statement="True", status="proving")
    eleven = _graph_node(
        "residue_eleven",
        file,
        statement="(n : Nat) (h : n % 17 = 11) : True",
        status="proved",
    )
    thirteen = _graph_node(
        "residue_thirteen",
        file,
        statement="(n : Nat) (h : n % 17 = 13) : True",
        status="proved",
    )
    nine = _graph_node(
        "residue_nine",
        file,
        statement="(n : Nat) (h : n % 17 = 9) : True",
        status="stated",
    )
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(parent, eleven, thirteen, nine),
            edges=(
                *_helper_parent_edges(eleven, parent),
                *_helper_parent_edges(thirteen, parent),
                *_helper_parent_edges(nine, parent),
            ),
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::residue_nine": {
                "target_symbol": "residue_nine",
                "active_file": file,
                "status": "solved",
                "last_verification": _accepted_target_verification("residue_nine"),
            }
        }
    }
    for route in ("direct-prove", "decompose", "plan", "negate"):
        runner.campaign_epoch.record_route_decision(autonomy_state, route=route)
    assert autonomy_state["campaign_epoch_requested"] == "route-no-graph-progress"

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": file}) is True

    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(nine.id).status == "proved"
    assert blueprint.node_by_id(eleven.id).status == "proved"
    assert blueprint.node_by_id(thirteen.id).status == "proved"
    assert autonomy_state["orchestrator_routes_used"] == 4
    assert autonomy_state["campaign_epoch_requested"] == "route-no-graph-progress"
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 4
    ledger_entries = campaign["verified_mechanisms"]["entries"]
    assert len(ledger_entries) == 1
    assert set(next(iter(ledger_entries.values()))["seen_node_ids"]) == {
        eleven.id,
        thirteen.id,
        nine.id,
    }
    repeats = [kwargs for args, kwargs in events if args[0] == "plan-graph-mechanism-repeat"]
    assert len(repeats) == 1
    assert repeats[0]["node_id"] == nine.id
    assert repeats[0]["parent_id"] == parent.id
    assert repeats[0]["campaign_progress"] is False


@pytest.mark.parametrize("distinct_scope", ["mechanism", "parent"])
def test_distinct_mechanism_or_parent_resets_campaign_route_streak(
    plan_enabled, monkeypatch, tmp_path, distinct_scope
):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    candidate_dependency = (
        "alternate_certificate" if distinct_scope == "mechanism" else "factor_pair"
    )
    candidate_parent = "parent_one" if distinct_scope == "mechanism" else "parent_two"
    active.write_text(
        "lemma factor_pair (n : Nat) : True := by\n"
        "  trivial\n\n"
        "lemma alternate_certificate (n : Nat) : True := by\n"
        "  trivial\n\n"
        "lemma residue_eleven (n : Nat) (h : n % 17 = 11) : True := by\n"
        "  exact factor_pair n\n\n"
        "lemma residue_nine (n : Nat) (h : n % 17 = 9) : True := by\n"
        f"  exact {candidate_dependency} n\n\n"
        "theorem parent_one : True := by\n"
        "  sorry\n\n"
        "theorem parent_two : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent_one = _graph_node("parent_one", file, statement="True", status="proving")
    parent_two = _graph_node("parent_two", file, statement="True", status="proving")
    eleven = _graph_node(
        "residue_eleven",
        file,
        statement="(n : Nat) (h : n % 17 = 11) : True",
        status="proved",
    )
    nine = _graph_node(
        "residue_nine",
        file,
        statement="(n : Nat) (h : n % 17 = 9) : True",
        status="stated",
    )
    parents = {"parent_one": parent_one, "parent_two": parent_two}
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(parent_one, parent_two, eleven, nine),
            edges=(
                *_helper_parent_edges(eleven, parent_one),
                *_helper_parent_edges(nine, parents[candidate_parent]),
            ),
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::residue_nine": {
                "target_symbol": "residue_nine",
                "active_file": file,
                "status": "solved",
                "last_verification": _accepted_target_verification("residue_nine"),
            }
        }
    }
    for route in ("direct-prove", "decompose", "plan"):
        runner.campaign_epoch.record_route_decision(autonomy_state, route=route)

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": file}) is True

    assert plan_state.load_blueprint().node_by_id(nine.id).status == "proved"
    assert autonomy_state["orchestrator_routes_used"] == 0
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 0
    assert campaign["last_verified_graph_progress"]["node_ids"] == [nine.id]
    assert not any(args[0] == "plan-graph-mechanism-repeat" for args, _kwargs in events)


def test_parent_closure_resets_even_after_helper_mechanism_accounting(
    plan_enabled, monkeypatch, tmp_path
):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma helper : True := by\n"
        "  trivial\n\n"
        "theorem parent : True := by\n"
        "  exact helper\n",
        encoding="utf-8",
    )
    file = str(active)
    helper = _graph_node("helper", file, statement="True", status="proved")
    parent = _graph_node("parent", file, statement="True", status="stated")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(helper, parent),
            edges=_helper_parent_edges(helper, parent),
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::parent": {
                "target_symbol": "parent",
                "active_file": file,
                "status": "solved",
                "last_verification": _accepted_target_verification("parent"),
            }
        }
    }
    for route in ("direct-prove", "decompose", "plan"):
        runner.campaign_epoch.record_route_decision(autonomy_state, route=route)

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": file}) is True

    assert plan_state.load_blueprint().node_by_id(parent.id).status == "proved"
    assert autonomy_state["orchestrator_routes_used"] == 0
    assert runner.campaign_epoch.campaign_snapshot()["last_verified_graph_progress"][
        "node_ids"
    ] == [parent.id]
    assert not any(args[0] == "plan-graph-mechanism-repeat" for args, _kwargs in events)


def test_explicit_exhaustive_split_resets_even_for_repeated_mechanism(
    plan_enabled, monkeypatch, tmp_path
):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma factor_pair (n : Nat) : True := by\n"
        "  trivial\n\n"
        "lemma branch_one (n : Nat) (h : n % 2 = 0) : True := by\n"
        "  exact factor_pair n\n\n"
        "lemma branch_two (n : Nat) (h : n % 2 = 1) : True := by\n"
        "  exact factor_pair n\n\n"
        "theorem parent : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    file = str(active)
    parent = _graph_node("parent", file, statement="True", status="split")
    branch_one = _graph_node(
        "branch_one",
        file,
        statement="(n : Nat) (h : n % 2 = 0) : True",
        status="proved",
    )
    branch_two = _graph_node(
        "branch_two",
        file,
        statement="(n : Nat) (h : n % 2 = 1) : True",
        status="stated",
    )
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(parent, branch_one, branch_two),
            edges=(
                *_helper_parent_edges(branch_one, parent),
                *_helper_parent_edges(branch_two, parent),
            ),
        )
    )
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "parent",
            "active_file": file,
            "slice": "theorem parent : True := by\n  sorry",
        },
        "theorem_outcomes": {
            f"{active}::branch_two": {
                "target_symbol": "branch_two",
                "active_file": file,
                "status": "solved",
                "last_verification": _accepted_target_verification("branch_two"),
            }
        },
    }
    for route in ("direct-prove", "decompose", "plan"):
        runner.campaign_epoch.record_route_decision(autonomy_state, route=route)

    assert runner._maybe_sync_plan_state(autonomy_state, {"active_file": file}) is True

    assert plan_state.load_blueprint().node_by_id(branch_two.id).status == "proved"
    assert autonomy_state["orchestrator_routes_used"] == 0
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["last_verified_graph_progress"]["node_ids"] == [branch_two.id]
    assert not any(args[0] == "plan-graph-mechanism-repeat" for args, _kwargs in events)


def test_axiom_profile_mode_retires_solved_outcome_without_stored_profile_gate(
    plan_enabled, monkeypatch, tmp_path
):
    """A direct-sorry-free replay cannot stand in for the transitive axiom gate."""
    events = _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "solved",
                "last_verification": _accepted_target_verification("demo"),
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("demo", str(active)))
    assert node.status != "proved"
    outcome = dict(autonomy_state["theorem_outcomes"])[f"{active}::demo"]
    assert outcome["status"] == "unverified"
    assert any(args[0] == "plan-graph-stale-outcome-retired" for args, _kwargs in events)


def test_axiom_profile_mode_revokes_proved_node_authorized_by_stale_outcome(
    plan_enabled, monkeypatch, tmp_path
):
    events = _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    status="proved",
                ),
            )
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "solved",
                "last_verification": _accepted_target_verification("demo"),
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"
    outcome = dict(autonomy_state["theorem_outcomes"])[f"{active}::demo"]
    assert outcome["status"] == "unverified"
    journal = (plan_enabled / "journal.jsonl").read_text(encoding="utf-8")
    assert '"from": "proved"' in journal
    assert '"to": "stated"' in journal
    assert any(args[0] == "plan-graph-stale-proof-revoked" for args, _kwargs in events)


def test_restart_revokes_proved_node_after_outcome_was_already_retired(
    plan_enabled, monkeypatch, tmp_path
):
    """Checkpoint replay must finish a solved->unverified->stated transition."""
    events = _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    status="proved",
                ),
            )
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "unverified",
                "note": "plan-state sync: solved outcome lacks an accepted exact-target gate",
                "last_verification": _accepted_target_verification("demo"),
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"
    outcome = dict(autonomy_state["theorem_outcomes"])[f"{active}::demo"]
    assert outcome["status"] == "unverified"
    assert any(args[0] == "plan-graph-stale-proof-revoked" for args, _kwargs in events)


def test_axiom_profile_revocation_keeps_proved_node_with_separate_current_gate(
    plan_enabled, monkeypatch, tmp_path
):
    _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    status="proved",
                ),
            )
        )
    )
    clean_profile = {
        **_accepted_target_verification("demo"),
        "axiom_profile_checked": True,
        "axiom_profile_blockers": [],
    }
    autonomy_state: dict[str, Any] = {
        "last_verification": clean_profile,
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "solved",
                "last_verification": _accepted_target_verification("demo"),
            }
        },
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    assert plan_state.load_blueprint().node_by_id(node_id).status == "proved"
    outcome = dict(autonomy_state["theorem_outcomes"])[f"{active}::demo"]
    assert outcome["status"] == "unverified"


def test_restart_keeps_proved_node_with_separate_current_gate(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    status="proved",
                ),
            )
        )
    )
    clean_profile = {
        **_accepted_target_verification("demo"),
        "axiom_profile_checked": True,
        "axiom_profile_blockers": [],
    }
    autonomy_state: dict[str, Any] = {
        "last_verification": clean_profile,
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "unverified",
                "note": "plan-state sync: solved outcome lacks an accepted exact-target gate",
            }
        },
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    assert plan_state.load_blueprint().node_by_id(node_id).status == "proved"


def test_restart_does_not_revoke_unverified_outcome_for_unrelated_reason(
    plan_enabled, monkeypatch, tmp_path
):
    _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    status="proved",
                ),
            )
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "unverified",
                "note": "provider paused before a verification could run",
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    assert plan_state.load_blueprint().node_by_id(node_id).status == "proved"


def test_axiom_profile_mode_does_not_revoke_proved_node_without_queue_outcome(
    plan_enabled, monkeypatch, tmp_path
):
    _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    status="proved",
                ),
            )
        )
    )

    runner._maybe_sync_plan_state({}, {"active_file": str(active)})

    assert plan_state.load_blueprint().node_by_id(node_id).status == "proved"


def test_axiom_profile_mode_promotes_only_persisted_clean_profile_gate(
    plan_enabled, monkeypatch, tmp_path
):
    _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    verification = {
        **_accepted_target_verification("demo"),
        "axiom_profile_checked": True,
        "axiom_profile_blockers": [],
    }
    autonomy_state: dict[str, Any] = {}

    runner._record_theorem_outcome(
        autonomy_state,
        {
            "target_symbol": "demo",
            "active_file": str(active),
            "status": "solved",
            "last_verification": verification,
        },
    )

    stored = dict(autonomy_state["theorem_outcomes"])[f"{active}::demo"]
    assert stored["last_verification"]["axiom_profile_checked"] is True
    assert stored["last_verification"]["axiom_profile_blockers"] == []
    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})
    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("demo", str(active)))
    assert node.status == "proved"


def test_axiom_profile_mode_retires_solved_outcome_with_stored_blocker(
    plan_enabled, monkeypatch, tmp_path
):
    _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_NATIVE_AXIOM_PROFILE_CHECK", "1")
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    verification = {
        **_accepted_target_verification("demo"),
        "axiom_profile_checked": True,
        "axiom_profile_blockers": ["sorryAx"],
    }
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "solved",
                "last_verification": verification,
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("demo", str(active)))
    assert node.status != "proved"
    outcome = dict(autonomy_state["theorem_outcomes"])[f"{active}::demo"]
    assert outcome["status"] == "unverified"


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
                "last_verification": _accepted_target_verification("demo"),
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
                "last_verification": _accepted_target_verification("demo"),
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


def test_assignment_transition_retires_previous_proving_node_without_outcome(
    plan_enabled, monkeypatch, tmp_path
):
    """A route change owns exactly one proving node even without an old verdict."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem first : True := by\n  sorry\n\ntheorem second : True := by\n  sorry\n",
        encoding="utf-8",
    )
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "first",
            "active_file": str(active),
            "slice": "theorem first : True := by\n  sorry",
        }
    }
    runner._maybe_sync_plan_state(
        autonomy_state,
        {"active_file": str(active), "current_queue_item": {"label": "first"}},
    )

    autonomy_state["current_queue_assignment"] = {
        "target_symbol": "second",
        "active_file": str(active),
        "slice": "theorem second : True := by\n  sorry",
    }
    runner._maybe_sync_plan_state(
        autonomy_state,
        {"active_file": str(active), "current_queue_item": {"label": "second"}},
    )

    bp = plan_state.load_blueprint()
    first = bp.node_by_id(plan_state.node_id_for("first", str(active)))
    second = bp.node_by_id(plan_state.node_id_for("second", str(active)))
    assert first is not None and first.status == "stated"
    assert second is not None and second.status == "proving"
    assert [node.name for node in bp.nodes if node.status == "proving"] == ["second"]
    assert any(args[0] == "plan-graph-assignment-retired" for args, _kwargs in events)


def test_restart_retires_stale_proving_node_and_preserves_fidelity_audit(
    plan_enabled, monkeypatch, tmp_path
):
    """Restored queue state cannot retain a prior process's proving owner."""
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem old_route : True := by\n  sorry\n\ntheorem resumed : True := by\n  sorry\n",
        encoding="utf-8",
    )
    old_id = plan_state.node_id_for("old_route", str(active))
    resumed_id = plan_state.node_id_for("resumed", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=old_id,
                    name="old_route",
                    file=str(active),
                    status="proving",
                    notes="fidelity: audited",
                ),
                plan_state.GraphNode(
                    id=resumed_id,
                    name="resumed",
                    file=str(active),
                    status="stated",
                ),
            )
        )
    )
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "resumed",
            "active_file": str(active),
            "slice": "theorem resumed : True := by\n  sorry",
        }
    }

    runner._maybe_sync_plan_state(
        autonomy_state,
        {"active_file": str(active), "current_queue_item": {"label": "resumed"}},
    )

    bp = plan_state.load_blueprint()
    assert bp.node_by_id(old_id).status == "audited"
    assert bp.node_by_id(resumed_id).status == "proving"
    assert sum(node.status == "proving" for node in bp.nodes) == 1
    journal = (plan_enabled / "journal.jsonl").read_text(encoding="utf-8")
    assert '"event": "plan-graph-assignment-retired"' in journal


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


def test_deferred_route_rotates_but_remains_queue_eligible(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem hard_demo : True := by\n  sorry\n\n" "theorem next_demo : True := by\n  sorry\n",
        encoding="utf-8",
    )
    hard_id = plan_state.node_id_for("hard_demo", str(active))
    next_id = plan_state.node_id_for("next_demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=hard_id,
                    name="hard_demo",
                    file=str(active),
                    status="proving",
                ),
                plan_state.GraphNode(
                    id=next_id,
                    name="next_demo",
                    file=str(active),
                    status="stated",
                ),
            )
        )
    )
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "next_demo",
            "active_file": str(active),
            "slice": "theorem next_demo : True := by\n  sorry",
        },
        "theorem_outcomes": {
            f"{active}::hard_demo": {
                "target_symbol": "hard_demo",
                "active_file": str(active),
                "status": "deferred",
                "note": "direct route exhausted",
            }
        },
    }
    live = {
        "active_file": str(active),
        "current_queue_item": {"label": "next_demo"},
    }

    runner._maybe_sync_plan_state(autonomy_state, live)

    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(hard_id).status == "stated"
    queue = [
        {"label": "hard_demo", "reasons": ["contains sorry"]},
        {"label": "next_demo", "reasons": ["contains sorry"]},
    ]
    precedence = runner._graph_frontier_precedence(autonomy_state)
    assert runner._current_queue_item(queue, str(active), precedence=precedence)["label"] == (
        "next_demo"
    )
    # Rank 2 is a cooldown, not exclusion: once it is the remaining work the
    # same deferred theorem is selected again without an epoch restart.
    assert runner._current_queue_item(queue[:1], str(active), precedence=precedence)["label"] == (
        "hard_demo"
    )


def test_current_assignment_ignores_stale_blocked_outcome(plan_enabled, monkeypatch, tmp_path):
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "theorem demo : True := by\n  sorry",
        },
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "blocked",
            }
        },
    }

    live = {"active_file": str(active), "current_queue_item": {"label": "demo"}}
    runner._maybe_sync_plan_state(autonomy_state, live)
    runner._maybe_sync_plan_state(autonomy_state, live)

    node = plan_state.load_blueprint().node_by_id(plan_state.node_id_for("demo", str(active)))
    assert node.status == "proving"
    journal = (plan_enabled / "journal.jsonl").read_text(encoding="utf-8")
    assert '"to": "blocked"' not in journal


def test_epoch_refresh_reopens_blocked_sorry_without_hot_loop(plan_enabled, monkeypatch, tmp_path):
    events = _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_GRAPH_FRONTIER_SELECTION", "1")
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem blocked_parent : True := by\n  sorry\n\n"
        "theorem useful_later : True := by\n  sorry\n",
        encoding="utf-8",
    )
    parent_id = plan_state.node_id_for("blocked_parent", str(active))
    later_id = plan_state.node_id_for("useful_later", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=parent_id,
                    name="blocked_parent",
                    file=str(active),
                    status="blocked",
                ),
                plan_state.GraphNode(
                    id=later_id,
                    name="useful_later",
                    file=str(active),
                    status="stated",
                ),
            )
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::blocked_parent": {
                "target_symbol": "blocked_parent",
                "active_file": str(active),
                "status": "blocked",
                "note": "direct proof shape exhausted",
            }
        }
    }
    queue = [
        {"label": "blocked_parent", "reasons": ["contains sorry"]},
        {"label": "useful_later", "reasons": ["contains sorry"]},
    ]

    before = runner._current_queue_item(
        queue,
        str(active),
        precedence=runner._graph_frontier_precedence(),
    )
    assert before["label"] == "useful_later"

    monkeypatch.setattr(runner, "_write_workflow_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_persist_live_status", lambda *args, **kwargs: None)
    runner._roll_autonomous_campaign_epoch(
        object(),
        [],
        {},
        {},
        autonomy_state,
        {
            "active_file": str(active),
            "target_symbol": "useful_later",
            "current_queue_item": queue[1],
        },
        reason="route-no-graph-progress",
        cycle=4,
    )

    outcome = autonomy_state["theorem_outcomes"][f"{active}::blocked_parent"]
    assert outcome["status"] == "unresolved"
    assert "prior blocker: direct proof shape exhausted" in outcome["note"]
    assert plan_state.load_blueprint().node_by_id(parent_id).status == "stated"
    after = runner._current_queue_item(
        queue,
        str(active),
        precedence=runner._graph_frontier_precedence(),
    )
    assert after["label"] == "blocked_parent"
    assert (
        runner._reopen_blocked_theorem_outcomes(autonomy_state, trigger="duplicate epoch callback")
        == ()
    )
    reopen_events = [
        kwargs for args, kwargs in events if args[0] == "queue-blocked-outcomes-reopened"
    ]
    assert len(reopen_events) == 1


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


def test_resume_rebuilds_lost_exact_gates_for_sorry_free_helpers_only(
    plan_enabled, monkeypatch, tmp_path
):
    """A restart recovers clean helper truth without treating sibling sorry as success."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma recovered_one : True := by\n  trivial\n\n"
        "lemma recovered_two : True := by\n  trivial\n\n"
        "theorem unresolved_parent : True := by\n  sorry\n",
        encoding="utf-8",
    )
    names = ("recovered_one", "recovered_two", "unresolved_parent")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=tuple(
                plan_state.GraphNode(
                    id=plan_state.node_id_for(name, str(active)),
                    name=name,
                    file=str(active),
                    statement="True",
                    status="stated",
                )
                for name in names
            )
        )
    )
    exact_calls: list[str] = []
    axiom_batches: list[tuple[str, ...]] = []

    def exact_check(_file: str, target: str):
        exact_calls.append(target)
        return {
            "ok": True,
            "mode": "incremental_target",
            "target": target,
            "incremental": {
                "success": True,
                "ok": True,
                "target": target,
                "messages": [],
                "errors": 0,
                "sorry": 0,
            },
        }

    def axiom_batch(targets, *, file_path="", **_kwargs):
        axiom_batches.append(tuple(targets))
        assert file_path == str(active)
        return {
            target: SimpleNamespace(
                axioms=["propext"],
                inspection_succeeded=True,
                ok=True,
                note="",
            )
            for target in targets
        }

    monkeypatch.setattr(runner, "_manager_incremental_check_queue_item", exact_check)
    monkeypatch.setattr(runner, "lean_axioms_many", axiom_batch)
    monkeypatch.setattr(
        runner,
        "_manager_verify_queue_file",
        lambda _file: pytest.fail("resume recovery must not use a whole-file acceptance gate"),
    )
    autonomy_state: dict[str, Any] = {}

    assert runner._plan_state_resume_block(autonomy_state)

    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(plan_state.node_id_for("recovered_one", str(active))).status == (
        "proved"
    )
    assert blueprint.node_by_id(plan_state.node_id_for("recovered_two", str(active))).status == (
        "proved"
    )
    assert (
        blueprint.node_by_id(plan_state.node_id_for("unresolved_parent", str(active))).status
        == "stated"
    )
    assert exact_calls == ["recovered_one", "recovered_two"]
    assert axiom_batches == [("recovered_one", "recovered_two")]
    outcomes = dict(autonomy_state["theorem_outcomes"])
    assert set(outcomes) == {
        f"{active}::recovered_one",
        f"{active}::recovered_two",
    }
    assert all(
        outcome["last_verification"]["axiom_profile_checked"] is True
        and outcome["last_verification"]["axiom_profile_blockers"] == []
        for outcome in outcomes.values()
    )
    recovered_events = [
        kwargs for args, kwargs in events if args[0] == "plan-graph-resume-gate-recovered"
    ]
    assert {event["target_symbol"] for event in recovered_events} == {
        "recovered_one",
        "recovered_two",
    }

    # Promotion makes the recovery cost one-shot across later restarts.
    exact_calls.clear()
    axiom_batches.clear()
    assert runner._plan_state_resume_block(autonomy_state)
    assert exact_calls == []
    assert axiom_batches == []


@pytest.mark.parametrize(
    "timeout_reason",
    [
        "Lean server timed out after 300 seconds",
        "LeanProbe call exceeded its 300s wall-clock deadline",
    ],
)
def test_resume_gate_backpressures_same_revision_timeout(
    plan_enabled, monkeypatch, tmp_path, timeout_reason
):
    """Restored foreground work must not replay a known slow gate before startup."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    declaration_hash = runner._failed_attempt_declaration_hash(str(active), "demo", None)
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    statement="True",
                    status="stated",
                ),
            )
        )
    )
    autonomy_state = {
        runner._QUEUE_MANAGER_STATE_RESTORED_KEY: True,
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        },
        "failed_attempts": [
            {
                "target_symbol": "demo",
                "active_file": str(active),
                "declaration_hash": declaration_hash,
                "gate_verdict": timeout_reason,
            },
            {
                "target_symbol": "demo",
                "active_file": str(active),
                "declaration_hash": declaration_hash,
                "gate_verdict": "later parser feedback",
            },
        ],
    }
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda *_args, **_kwargs: pytest.fail("resume replayed known timed-out gate"),
    )
    monkeypatch.setattr(
        runner,
        "_collect_declaration_truth",
        lambda *_args, **_kwargs: pytest.fail(
            "resume inspected graph truth before timeout backpressure"
        ),
    )

    assert runner._plan_state_resume_block(autonomy_state)
    assert plan_state.load_blueprint().node_by_id(node_id).status != "proved"
    assert any(args[0] == "plan-graph-resume-gate-backpressured" for args, _kwargs in events)


def test_resume_migrates_matching_legacy_startup_timeout_before_truth_collection(
    plan_enabled, monkeypatch, tmp_path
):
    """Old source-bound timeout activity must suppress startup Lean replay."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    source_sha256 = runner._source_revision_sha256(str(active))
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    statement="True",
                    status="stated",
                ),
            )
        )
    )
    autonomy_state = {
        runner._QUEUE_MANAGER_STATE_RESTORED_KEY: True,
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        },
    }
    monkeypatch.setattr(
        runner,
        "read_workflow_activity",
        lambda **_kwargs: [
            {
                "type": "startup-exact-verification-deferred",
                "details": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "source_revision_sha256": source_sha256,
                    "timeout_s": 60,
                },
            }
        ],
    )
    monkeypatch.setattr(
        runner,
        "_collect_declaration_truth",
        lambda *_args, **_kwargs: pytest.fail(
            "legacy timeout migration did not precede graph truth collection"
        ),
    )

    assert runner._plan_state_resume_block(autonomy_state)
    declaration_hash = runner._failed_attempt_declaration_hash(str(active), "demo", None)
    assert any(
        attempt.get("declaration_hash") == declaration_hash
        and "timed out" in str(attempt.get("gate_verdict", ""))
        for attempt in autonomy_state["failed_attempts"]
    )
    assert any(args[0] == "verification-timeout-activity-migrated" for args, _ in events)


def test_legacy_startup_timeout_migration_rejects_changed_source(monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    autonomy_state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        },
    }
    monkeypatch.setattr(
        runner,
        "read_workflow_activity",
        lambda **_kwargs: [
            {
                "type": "startup-exact-verification-deferred",
                "details": {
                    "target_symbol": "demo",
                    "active_file": str(active),
                    "source_revision_sha256": "stale-source-revision",
                    "timeout_s": 60,
                },
            }
        ],
    )

    assert (
        runner._migrate_same_revision_verification_timeout_from_activity(
            autonomy_state,
            target_symbol="demo",
            active_file=str(active),
        )
        == ""
    )
    assert "failed_attempts" not in autonomy_state


def test_resume_recovers_missing_assignment_from_exact_timeout_ledger(
    plan_enabled, monkeypatch, tmp_path
):
    """An interrupted verifier must not strand a sorry-free unresolved node."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    declaration_hash = runner._failed_attempt_declaration_hash(str(active), "demo", None)
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    statement="True",
                    status="audited",
                ),
            )
        )
    )
    autonomy_state = {
        runner._QUEUE_MANAGER_STATE_RESTORED_KEY: True,
        "failed_attempts": [
            {
                "target_symbol": "demo",
                "active_file": str(active),
                "declaration_hash": declaration_hash,
                "gate_verdict": "timed out after 600 seconds",
                "reason": "timed out after 600 seconds",
            }
        ],
    }
    monkeypatch.setattr(runner, "read_workflow_activity", lambda **_kwargs: [])
    monkeypatch.setattr(
        runner,
        "_collect_declaration_truth",
        lambda *_args, **_kwargs: pytest.fail(
            "recovered timeout assignment still started graph truth collection"
        ),
    )

    assert runner._plan_state_resume_block(autonomy_state)
    assert autonomy_state["current_queue_assignment"]["target_symbol"] == "demo"
    assert autonomy_state["current_queue_assignment"]["active_file"] == str(active)
    assert any(args[0] == "queue-timeout-assignment-recovered" for args, _ in events)
    assert not any(args[0] == "plan-graph-assignment-retired" for args, _ in events)


def test_missing_timeout_assignment_recovery_requires_one_candidate(
    plan_enabled, monkeypatch, tmp_path
):
    first = tmp_path / "First.lean"
    second = tmp_path / "Second.lean"
    first.write_text("theorem first : True := by\n  trivial\n", encoding="utf-8")
    second.write_text("theorem second : True := by\n  trivial\n", encoding="utf-8")
    nodes = tuple(
        plan_state.GraphNode(
            id=plan_state.node_id_for(symbol, str(path)),
            name=symbol,
            file=str(path),
            statement="True",
            status="audited",
        )
        for path, symbol in ((first, "first"), (second, "second"))
    )
    plan_state.save_blueprint(plan_state.Blueprint(nodes=nodes))
    autonomy_state = {
        runner._QUEUE_MANAGER_STATE_RESTORED_KEY: True,
        "failed_attempts": [
            {
                "target_symbol": symbol,
                "active_file": str(path),
                "declaration_hash": runner._failed_attempt_declaration_hash(
                    str(path), symbol, None
                ),
                "gate_verdict": "timed out after 600 seconds",
                "reason": "timed out after 600 seconds",
            }
            for path, symbol in ((first, "first"), (second, "second"))
        ],
    }

    assert runner._recover_missing_timeout_assignment_from_ledger(autonomy_state) == {}
    assert "current_queue_assignment" not in autonomy_state


@pytest.mark.parametrize(
    ("axioms", "expected_status"),
    [(["propext"], "proved"), (["propext", "sorryAx"], "stated")],
)
def test_resume_reuses_complete_inline_axiom_profile_without_batch_process(
    plan_enabled,
    monkeypatch,
    tmp_path,
    axioms,
    expected_status,
):
    """Declaration-bound inline evidence avoids a duplicate resume axiom compile."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text("lemma recovered : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("recovered", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="recovered",
                    file=str(active),
                    statement="True",
                    status="stated",
                ),
            )
        )
    )

    def exact_check(_file: str, target: str):
        assert target == "recovered"
        rendered_axioms = ", ".join(axioms)
        return {
            "ok": True,
            "mode": "incremental_target",
            "target": target,
            "incremental": {
                "success": True,
                "ok": True,
                "target": target,
                "messages": [],
                "errors": 0,
                "sorry": 0,
                "axiom_profile_checked": True,
                "axiom_profile_requested_target": target,
                "axiom_profile_target": target,
                "axiom_profile_declaration_sha256": "a" * 64,
                "axiom_profile_axioms": axioms,
                "axiom_profile_output": f"depends on axioms: {rendered_axioms}",
                "axiom_profile_error": "",
            },
        }

    monkeypatch.setattr(runner, "_manager_incremental_check_queue_item", exact_check)
    monkeypatch.setattr(
        runner,
        "lean_axioms_many",
        lambda *_args, **_kwargs: pytest.fail(
            "complete inline axiom evidence must suppress the fallback batch"
        ),
    )
    autonomy_state: dict[str, Any] = {}

    assert runner._plan_state_resume_block(autonomy_state)

    assert plan_state.load_blueprint().node_by_id(node_id).status == expected_status
    rejected = [kwargs for args, kwargs in events if args[0] == "plan-graph-resume-gate-rejected"]
    assert bool(rejected) is (expected_status == "stated")


def test_scoped_resume_gate_stages_verified_startup_handoff(
    plan_enabled,
    monkeypatch,
    tmp_path,
):
    """Carry a freshly recovered assignment gate into startup without Lake replay."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    statement="True",
                    status="stated",
                ),
            )
        )
    )
    revision = runner.source_only_startup.capture_source_revision(str(active))
    assert revision is not None
    recovered_state = {
        "active_file": str(active),
        "target_symbol": "",
        "declaration_scope": "file",
        "declaration_queue_total": 0,
        "verification_ok": True,
        "proof_solved": True,
        "proof_state_authority": "authenticated_target_gate",
        "source_revision": revision.to_mapping(),
        "source_revision_sha256": revision.sha256,
    }
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda _file, target: _strict_resume_exact_payload(
            active,
            target,
            inline_axioms=["propext"],
        ),
    )
    monkeypatch.setattr(
        runner,
        "lean_axioms_many",
        lambda *_args, **_kwargs: pytest.fail("inline profile unexpectedly replayed axioms"),
    )
    monkeypatch.setattr(
        runner,
        "_build_verified_gate_handoff_state",
        lambda *args, **kwargs: dict(recovered_state),
    )
    autonomy_state: dict[str, Any] = {
        runner._QUEUE_MANAGER_STATE_RESTORED_KEY: True,
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        },
    }

    assert runner._recover_resume_graph_gate_evidence(autonomy_state) == (node_id,)
    assert runner.verified_gate_handoff.take_mapping(autonomy_state) == recovered_state


def test_resume_gate_timeout_backpressures_later_startup_verification(
    plan_enabled,
    monkeypatch,
    tmp_path,
):
    """Persist a resume-gate timeout before startup can launch a Lake replay."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="demo",
                    file=str(active),
                    statement="True",
                    status="stated",
                ),
            )
        )
    )
    timeout_check = {
        "ok": False,
        "mode": "incremental_target",
        "target": "demo",
        "timed_out": True,
        "output": "LeanProbe call exceeded its 600s wall-clock deadline",
        "incremental": {
            "success": False,
            "ok": False,
            "target": "demo",
            "timed_out": True,
            "has_errors": False,
            "has_sorry": False,
            "messages": [],
        },
    }
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda _file, _target: dict(timeout_check),
    )
    monkeypatch.setattr(
        runner.resume_gate_rejection_cache,
        "capture_identity",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    autonomy_state: dict[str, Any] = {
        runner._QUEUE_MANAGER_STATE_RESTORED_KEY: True,
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        },
    }

    assert runner._recover_resume_graph_gate_evidence(autonomy_state) == ()
    assert runner._restored_assignment_verification_timeout_reason(
        autonomy_state,
        target_symbol="demo",
        active_file=str(active),
    )

    restored = {
        "active_file": str(active),
        "active_file_label": "Demo.lean",
        "target_symbol": "demo",
        "declaration_scope": "file",
        "declaration_queue_total": 1,
        "current_queue_item": {"label": "demo", "reasons": ["restored"]},
    }
    monkeypatch.setattr(
        runner,
        "_restored_queue_assignment_live_state",
        lambda _state: dict(restored),
    )
    monkeypatch.setattr(
        runner,
        "_provider_free_exact_scope_state",
        lambda *args, **kwargs: pytest.fail("resume timeout launched canonical Lake replay"),
    )

    deferred = runner._verified_startup_preflight([], {}, autonomy_state)

    assert deferred["proof_state_authority"] == "source_only_unverified"
    assert deferred["defer_incremental_warmup"] is True


def test_resume_reuses_only_exact_cached_axiom_rejection(
    plan_enabled,
    monkeypatch,
    tmp_path,
):
    """An unchanged policy rejection skips Lean but never promotes graph truth."""
    events = _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    active = tmp_path / "Demo.lean"
    active.write_text("lemma blocked : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("blocked", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="blocked",
                    file=str(active),
                    statement="True",
                    status="stated",
                ),
            )
        )
    )
    monkeypatch.setattr(
        runner.resume_gate_rejection_cache.lean_axiom_batch,
        "import_environment_fingerprint",
        lambda _root: "e" * 64,
    )
    exact_calls: list[str] = []

    def exact_check(file_path: str, target: str):
        exact_calls.append(target)
        assert file_path == str(active)
        return _strict_resume_exact_payload(active, target, inline_axioms=["sorryAx"])

    monkeypatch.setattr(runner, "_manager_incremental_check_queue_item", exact_check)
    monkeypatch.setattr(
        runner,
        "lean_axioms_many",
        lambda *_args, **_kwargs: pytest.fail(
            "complete inline evidence and cache reuse must not launch an axiom batch"
        ),
    )
    autonomy_state: dict[str, Any] = {}

    assert runner._plan_state_resume_block(autonomy_state)
    assert exact_calls == ["blocked"]
    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"
    assert not autonomy_state.get("theorem_outcomes")

    assert runner._plan_state_resume_block(autonomy_state)
    assert exact_calls == ["blocked"]
    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"
    reused = [
        kwargs for args, kwargs in events if args[0] == "plan-graph-resume-gate-rejection-reused"
    ]
    assert len(reused) == 1
    assert reused[0]["blocker_axioms"] == ["sorryAx"]
    assert reused[0]["negative_authority_only"] is True

    active.write_text(
        "lemma blocked : True := by\n  trivial\n\n-- changed source revision\n",
        encoding="utf-8",
    )
    assert runner._plan_state_resume_block(autonomy_state)
    assert exact_calls == ["blocked", "blocked"]
    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"


def test_resume_reuses_cached_fallback_axiom_batch_rejection(
    plan_enabled,
    monkeypatch,
    tmp_path,
):
    """An unchanged batch rejection cannot relaunch the expensive Lean harness."""
    _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    active = tmp_path / "Demo.lean"
    active.write_text("lemma batch_blocked : True := by\n  trivial\n", encoding="utf-8")
    node_id = plan_state.node_id_for("batch_blocked", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=node_id,
                    name="batch_blocked",
                    file=str(active),
                    statement="True",
                    status="stated",
                ),
            )
        )
    )
    monkeypatch.setattr(
        runner.resume_gate_rejection_cache.lean_axiom_batch,
        "import_environment_fingerprint",
        lambda _root: "e" * 64,
    )
    exact_calls: list[str] = []
    batch_calls: list[tuple[str, ...]] = []

    def exact_check(file_path: str, target: str):
        exact_calls.append(target)
        assert file_path == str(active)
        return _strict_resume_exact_payload(active, target)

    def axiom_batch(targets, *, file_path="", **_kwargs):
        selected = tuple(targets)
        batch_calls.append(selected)
        assert file_path == str(active)
        return {
            target: SimpleNamespace(
                axioms=["sorryAx"],
                inspection_succeeded=True,
                ok=False,
                note="depends on sorryAx",
            )
            for target in selected
        }

    monkeypatch.setattr(runner, "_manager_incremental_check_queue_item", exact_check)
    monkeypatch.setattr(runner, "lean_axioms_many", axiom_batch)
    autonomy_state: dict[str, Any] = {}

    assert runner._plan_state_resume_block(autonomy_state)
    assert exact_calls == ["batch_blocked"]
    assert batch_calls == [("batch_blocked",)]
    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"
    assert not autonomy_state.get("theorem_outcomes")

    assert runner._plan_state_resume_block(autonomy_state)
    assert exact_calls == ["batch_blocked"]
    assert batch_calls == [("batch_blocked",)]
    assert plan_state.load_blueprint().node_by_id(node_id).status == "stated"
    assert not autonomy_state.get("theorem_outcomes")


def test_modern_resume_defers_campaign_wide_gate_until_assignment_selection(
    plan_enabled,
    monkeypatch,
    tmp_path,
):
    """Missing modern assignment cannot compile unrelated clean graph declarations."""
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma unrelated_clean : True := by\n  trivial\n\n"
        "theorem selected_parent : True := by\n  sorry\n",
        encoding="utf-8",
    )
    clean_id = plan_state.node_id_for("unrelated_clean", str(active))
    parent_id = plan_state.node_id_for("selected_parent", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(
                    id=clean_id,
                    name="unrelated_clean",
                    file=str(active),
                    statement="True",
                    status="stated",
                ),
                plan_state.GraphNode(
                    id=parent_id,
                    name="selected_parent",
                    file=str(active),
                    statement="True",
                    status="audited",
                ),
            )
        )
    )
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda *_args, **_kwargs: pytest.fail(
            "campaign-wide recovery must wait for a modern queue assignment"
        ),
    )
    autonomy_state: dict[str, Any] = {
        runner._QUEUE_MANAGER_STATE_RESTORED_KEY: True,
        "failed_attempts": [],
    }

    assert runner._plan_state_resume_block(autonomy_state)
    assert autonomy_state[runner._RESUME_GRAPH_RECOVERY_DEFERRED_KEY] is True
    assert plan_state.load_blueprint().node_by_id(clean_id).status == "stated"
    assert any(args[0] == "plan-graph-resume-gate-deferred" for args, _kwargs in events)

    autonomy_state["current_queue_assignment"] = {
        "target_symbol": "selected_parent",
        "active_file": str(active),
        "slice": "theorem selected_parent : True := by sorry",
    }
    assert runner._recover_deferred_resume_graph_gate_evidence(autonomy_state) == ()
    assert runner._RESUME_GRAPH_RECOVERY_DEFERRED_KEY not in autonomy_state
    assert plan_state.load_blueprint().node_by_id(clean_id).status == "stated"


def test_resume_recovered_assignment_does_not_cancel_due_rollover_after_rotation(
    plan_enabled, monkeypatch, tmp_path
):
    """Restored pre-process truth is not fresh progress after queue rotation."""
    events = _events(monkeypatch)
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "resume-proof-before-rollover")
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma recovered_helper : True := by\n  trivial\n\n"
        "theorem unresolved_parent : True := by\n  sorry\n",
        encoding="utf-8",
    )
    helper = _graph_node(
        "recovered_helper",
        str(active),
        statement="True",
        status="proving",
    )
    parent = _graph_node(
        "unresolved_parent",
        str(active),
        statement="True",
        status="stated",
    )
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(helper, parent),
            edges=_helper_parent_edges(helper, parent),
        )
    )

    def exact_check(_file: str, target: str):
        assert target == helper.name
        return {
            "ok": True,
            "mode": "incremental_target",
            "target": target,
            "incremental": {
                "success": True,
                "ok": True,
                "target": target,
                "messages": [],
                "errors": 0,
                "sorry": 0,
            },
        }

    def axiom_batch(targets, *, file_path="", **_kwargs):
        assert tuple(targets) == (helper.name,)
        assert file_path == str(active)
        return {
            helper.name: SimpleNamespace(
                axioms=["propext"],
                inspection_succeeded=True,
                ok=True,
                note="",
            )
        }

    monkeypatch.setattr(runner, "_manager_incremental_check_queue_item", exact_check)
    monkeypatch.setattr(runner, "lean_axioms_many", axiom_batch)
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": helper.name,
            "active_file": str(active),
            "slice": "lemma recovered_helper : True := by trivial",
        }
    }
    for route in ("plan", "decompose", "negate", "plan"):
        runner.campaign_epoch.record_route_decision(
            autonomy_state,
            route=route,
            target_symbol=parent.name,
            active_file=str(active),
        )
    assert autonomy_state["campaign_epoch_requested"] == "route-no-graph-progress"

    # Resume recovery records an accepted exact gate, but the current-assignment
    # rule intentionally leaves its graph node proving until the queue rotates.
    assert runner._plan_state_resume_block(autonomy_state)
    assert plan_state.load_blueprint().node_by_id(helper.id).status == "proving"
    assert runner.campaign_epoch.campaign_snapshot()["no_progress_route_streak"] == 4

    autonomy_state["current_queue_assignment"] = {
        "target_symbol": parent.name,
        "active_file": str(active),
        "slice": "theorem unresolved_parent : True := by sorry",
    }
    assert runner._maybe_sync_plan_state(autonomy_state, None) is True

    assert plan_state.load_blueprint().node_by_id(helper.id).status == "proved"
    campaign = runner.campaign_epoch.campaign_snapshot()
    assert campaign["no_progress_route_streak"] == 4
    assert autonomy_state["orchestrator_routes_used"] == 4
    assert autonomy_state["campaign_epoch_requested"] == "route-no-graph-progress"
    restored = [kwargs for args, kwargs in events if args[0] == "plan-graph-resume-proof-restored"]
    assert len(restored) == 1
    assert restored[0]["node_ids"] == [helper.id]
    assert restored[0]["campaign_progress"] is False


def test_legacy_resume_reset_activity_correlation_is_exact():
    """Migration fallback requires the same node, run, and reset timestamp."""
    campaign = {
        "campaign_id": "campaign-one",
        "epoch": 7,
        "last_verified_graph_progress": {
            "node_ids": ["n-recovered"],
            "recorded_at": "2026-07-18T10:34:34+00:00",
        },
    }
    events = [
        {
            "type": "plan-graph-resume-gate-recovered",
            "timestamp": "2026-07-18T10:32:57+00:00",
            "run_id": "resume-run",
            "details": {"node_id": "n-recovered"},
        },
        {
            "type": "campaign-route-streak-reset",
            "timestamp": "2026-07-18T10:34:34+00:00",
            "run_id": "different-run",
            "details": {
                "campaign_id": "campaign-one",
                "epoch": 7,
                "node_ids": ["n-recovered"],
            },
        },
    ]

    assert runner.resume_graph_reconciliation.legacy_startup_reset_node_ids(campaign, events) == ()
    events[1]["run_id"] = "resume-run"
    assert runner.resume_graph_reconciliation.legacy_startup_reset_node_ids(campaign, events) == (
        "n-recovered",
    )


def test_resume_never_promotes_wrong_target_or_blocked_axiom_profile(
    plan_enabled, monkeypatch, tmp_path
):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma wrong_target : True := by\n  trivial\n\n"
        "lemma axiom_blocked : True := by\n  trivial\n\n"
        "theorem sibling_sorry : True := by\n  sorry\n",
        encoding="utf-8",
    )
    names = ("wrong_target", "axiom_blocked", "sibling_sorry")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=tuple(
                plan_state.GraphNode(
                    id=plan_state.node_id_for(name, str(active)),
                    name=name,
                    file=str(active),
                    statement="True",
                    status="stated",
                )
                for name in names
            )
        )
    )

    def exact_check(_file: str, target: str):
        checked_target = "some_other_declaration" if target == "wrong_target" else target
        return {
            "ok": True,
            "mode": "incremental_target",
            "target": checked_target,
            "incremental": {
                "success": True,
                "ok": True,
                "target": checked_target,
                "messages": [],
                "errors": 0,
                "sorry": 0,
            },
        }

    monkeypatch.setattr(runner, "_manager_incremental_check_queue_item", exact_check)
    batch_calls: list[tuple[str, ...]] = []

    def axiom_batch(targets, **_kwargs):
        batch_calls.append(tuple(targets))
        return {
            target: SimpleNamespace(
                axioms=["sorryAx"],
                inspection_succeeded=True,
                ok=False,
                note="depends on sorryAx",
            )
            for target in targets
        }

    monkeypatch.setattr(runner, "lean_axioms_many", axiom_batch)
    autonomy_state: dict[str, Any] = {}

    assert runner._plan_state_resume_block(autonomy_state)

    blueprint = plan_state.load_blueprint()
    assert all(
        blueprint.node_by_id(plan_state.node_id_for(name, str(active))).status == "stated"
        for name in names
    )
    assert not autonomy_state.get("theorem_outcomes")
    assert batch_calls == [("axiom_blocked",)]


def test_resume_gate_prioritizes_restored_assignment_dependencies(
    plan_enabled, monkeypatch, tmp_path
):
    """Downstream clean declarations must not tax startup before the active theorem."""
    _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "lemma dependency : True := by\n  trivial\n\n"
        "theorem active_target : True := by\n  sorry\n\n"
        "theorem downstream : True := by\n  exact active_target\n\n"
        "lemma unrelated : True := by\n  trivial\n",
        encoding="utf-8",
    )
    dependency = _graph_node("dependency", str(active), statement="True", status="stated")
    active_target = _graph_node("active_target", str(active), statement="True", status="proving")
    downstream = _graph_node("downstream", str(active), statement="True", status="stated")
    unrelated = _graph_node("unrelated", str(active), statement="True", status="stated")
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(dependency, active_target, downstream, unrelated),
            edges=(
                plan_state.GraphEdge(
                    source=active_target.id,
                    target=dependency.id,
                    kind="depends_on",
                ),
                plan_state.GraphEdge(
                    source=downstream.id,
                    target=active_target.id,
                    kind="depends_on",
                ),
            ),
        )
    )
    exact_calls: list[str] = []

    def exact_check(_file: str, target: str):
        exact_calls.append(target)
        return {
            "ok": True,
            "target": target,
            "incremental": {
                "success": True,
                "ok": True,
                "target": target,
                "messages": [],
                "errors": 0,
                "sorry": 0,
            },
        }

    monkeypatch.setattr(runner, "_manager_incremental_check_queue_item", exact_check)
    monkeypatch.setattr(
        runner,
        "lean_axioms_many",
        lambda targets, **_kwargs: {
            target: SimpleNamespace(axioms=["propext"], inspection_succeeded=True, ok=True, note="")
            for target in targets
        },
    )
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "active_file": str(active),
            "target_symbol": "active_target",
        }
    }

    assert runner._plan_state_resume_block(autonomy_state)

    assert exact_calls == ["dependency"]
    blueprint = plan_state.load_blueprint()
    assert blueprint.node_by_id(dependency.id).status == "proved"
    assert blueprint.node_by_id(downstream.id).status == "stated"
    assert blueprint.node_by_id(unrelated.id).status == "stated"


# ---------------------------------------------------------------------------
# Queue-drain outcome for the LAST theorem (the transition path never fires for
# it — nothing follows it — so its gate-backed 'solved' outcome is recorded on
# drain, then the ORDINARY gate-accept sync promotes it).
# ---------------------------------------------------------------------------


def _drained_verified_live_state(active) -> dict[str, Any]:
    # No current_queue_item => the queue has drained.
    return {
        "active_file": str(active),
        "goals": "no goals",
        "build_status": "ok",
        "last_verification": {
            "scope": "file",
            "ok": True,
            "errors": 0,
            "sorry": 0,
            "tool": "lean_verify",
        },
    }


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


def test_false_dependency_retires_stale_solved_outcome(plan_enabled, monkeypatch, tmp_path):
    events = _events(monkeypatch)
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem helper : True := by trivial\n" "theorem demo : True := by exact helper\n",
        encoding="utf-8",
    )
    helper_id = plan_state.node_id_for("helper", str(active))
    demo_id = plan_state.node_id_for("demo", str(active))
    plan_state.save_blueprint(
        plan_state.Blueprint(
            nodes=(
                plan_state.GraphNode(id=helper_id, name="helper", file=str(active), status="false"),
                plan_state.GraphNode(id=demo_id, name="demo", file=str(active), status="proved"),
            ),
            edges=(plan_state.GraphEdge(source=demo_id, target=helper_id, kind="depends_on"),),
        )
    )
    autonomy_state: dict[str, Any] = {
        "theorem_outcomes": {
            f"{active}::demo": {
                "target_symbol": "demo",
                "active_file": str(active),
                "status": "solved",
                "last_verification": _accepted_target_verification("demo"),
            }
        }
    }

    runner._maybe_sync_plan_state(autonomy_state, {"active_file": str(active)})

    assert plan_state.load_blueprint().node_by_id(demo_id).status == "conjectured"
    outcome = dict(autonomy_state["theorem_outcomes"])[f"{active}::demo"]
    assert outcome["status"] == "invalidated-by-dependency"
    assert any(args[0] == "plan-graph-reconcile" for args, _kwargs in events)


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
