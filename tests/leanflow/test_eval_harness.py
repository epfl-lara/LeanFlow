"""Phase E scorer tests: N1 compliance, drift detection, the P1 reconcile drill."""

from __future__ import annotations

import json

import pytest

from evals import harness
from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.plan_state import Blueprint, DeclTruth, GraphNode
from leanflow_cli.workflows.workflow_json_io import update_json_file


@pytest.fixture()
def state_root(monkeypatch, tmp_path):
    root = tmp_path / "plan-state"
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(root))
    return root


def _seed_compliant_run(root) -> None:
    bp = Blueprint(
        goal="prove demo",
        nodes=(
            GraphNode(id="n-a", name="demo", file="Demo.lean", status="proved"),
            GraphNode(id="n-b", name="hard", file="Demo.lean", status="blocked"),
        ),
    )
    plan_state.save_blueprint(bp)
    plan_state.record_decision_packet(
        {"packet_id": "bp-1", "scope": "theorem", "node_id": "n-b", "target_symbol": "hard"}
    )
    summary = plan_state.load_summary()
    summary["counters"] = plan_state.status_counters(plan_state.load_blueprint())
    plan_state.save_summary(summary)
    plan_state.write_final_report("documented", detail={"summary": "parked at frontier"})


def test_compliant_run_scores_clean(state_root):
    _seed_compliant_run(state_root)

    report = harness.score_terminal_artifacts(state_root)

    assert report["compliant"] is True
    assert report["violations"] == []
    assert report["final_report_status"] == "documented"
    assert report["verified_progress"] == 1
    assert report["decision_packets"] == 1
    assert report["journal_events"] > 0


def test_missing_final_report_is_an_n1_violation(state_root):
    plan_state.save_blueprint(Blueprint(goal="g"))

    report = harness.score_terminal_artifacts(state_root)

    assert report["compliant"] is False
    assert any("final_report" in violation for violation in report["violations"])


def test_counter_drift_and_dangling_packet_are_violations(state_root):
    _seed_compliant_run(state_root)
    summary = plan_state.load_summary()
    summary["counters"] = {"proved": 99}
    summary["decision_packets"].append({"packet_id": "bp-2", "node_id": "n-missing"})
    plan_state.save_summary(summary)

    report = harness.score_terminal_artifacts(state_root)

    assert not report["compliant"]
    assert any("diverge" in violation for violation in report["violations"])
    assert any("unknown node" in violation for violation in report["violations"])


def test_reconcile_drill_passes_when_verified_work_survives(state_root):
    bp = Blueprint(
        nodes=(
            GraphNode(id="n-a", name="clean", file="A.lean", status="proved"),
            GraphNode(id="n-b", name="regressed", file="A.lean", status="proved"),
        )
    )
    truth = {
        ("A.lean", "clean"): DeclTruth(present=True, has_sorry=False),
        ("A.lean", "regressed"): DeclTruth(present=True, has_sorry=True),
    }

    drill = harness.reconcile_drill(bp, truth)

    # The genuinely-regressed node downgrading is expected, not lost work.
    assert drill["ok"] is True
    assert drill["proved_after"] == 1
    assert [change["name"] for change in drill["changes"]] == ["regressed"]


def test_reconcile_drill_fails_on_lost_verified_work(state_root, monkeypatch):
    bp = Blueprint(nodes=(GraphNode(id="n-a", name="clean", file="A.lean", status="proved"),))
    truth = {("A.lean", "clean"): DeclTruth(present=True, has_sorry=False)}
    # Simulate a broken reconcile that SILENTLY drops clean proved work —
    # no change event emitted; the drill must judge the after graph itself.
    monkeypatch.setattr(
        harness,
        "reconcile",
        lambda blueprint, _truth: (
            blueprint.replace_node(
                GraphNode(id="n-a", name="clean", file="A.lean", status="stated")
            ),
            [],
        ),
    )

    drill = harness.reconcile_drill(bp, truth)

    assert drill["ok"] is False
    assert drill["lost_verified_work"] == ["n-a"]


def test_append_result_writes_jsonl(tmp_path):
    target = tmp_path / "results.jsonl"

    harness.append_result({"suite": "t1", "phase": "P1", "compliant": True}, path=target)
    harness.append_result({"suite": "t1", "phase": "P1", "compliant": True}, path=target)

    lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["suite"] == "t1"
    assert lines[0]["ts"]


def test_t1_fixture_inventory_lists_demo_projects():
    projects = harness.t1_fixture_projects()

    assert any(path.name == "ProveDemo" for path in projects)
    assert all(path.is_dir() for path in projects)


def test_frozen_t2_t3_and_adversarial_corpora_are_complete():
    assert harness.validate_corpus_manifest() == []
    assert len(harness.suite_cases("t2")) == 40
    assert len(harness.suite_cases("t3")) == 10
    assert len(harness.suite_cases("adversarial")) == 4
    assert any(case["declaration"] == "erdos_865.variants.k2" for case in harness.suite_cases("t3"))


def test_campaign_metrics_report_relentless_acceptance_data(state_root):
    plan_state.save_blueprint(
        Blueprint(
            goal="g",
            nodes=(GraphNode(id="n-a", name="helper", file="A.lean", status="proved"),),
        )
    )
    plan_state.append_journal_event(
        {"event": "orchestrator-route", "route": "decompose", "name": "hard"}
    )
    plan_state.append_journal_event(
        {"event": "orchestrator-route", "route": "negate", "name": "hard"}
    )
    plan_state.append_journal_event(
        {"event": "proof-attempt-rejected", "proof_shape": "simp", "name": "hard"}
    )

    def seed(summary):
        summary["campaign"] = {
            "campaign_id": "campaign-test",
            "status": "paused",
            "last_exit_code": 2,
            "last_exit_verified": False,
            "last_exit_reason": "headless early exit",
            "epoch_history": [{"epoch": 1}],
        }
        summary["campaign_metrics"] = {
            "rejected_turns": 3,
            "coach_messages": 3,
            "coach_fallbacks": 2,
        }
        summary["dispatch_ledger"] = [
            {
                "state": "done",
                "started_at": "now",
                "consumed": True,
                "spec": {"inputs": {"generation": 1}},
            },
            {
                "state": "running",
                "started_at": "later",
                "consumed": False,
                "spec": {"inputs": {"generation": 2}},
            },
        ]

    update_json_file(state_root / "summary.json", seed)

    report = harness.score_campaign_metrics(state_root)

    assert report["voluntary_give_up_termination"] is False
    assert report["unresolved_success_exit"] is False
    assert report["coach_coverage"] == 1.0
    assert report["route_diversity"] == 2
    assert report["proof_shape_diversity"] == 1
    assert report["jobs_launched"] == 2
    assert report["jobs_consumed"] == 1
    assert report["jobs_replaced"] == 1
    assert report["verified_graph_progress"] == 1
    assert report["epoch_rollovers"] == 1
    assert all(report["acceptance"].values())


def test_campaign_metrics_detect_surrender_and_false_success(state_root):
    plan_state.save_blueprint(Blueprint(goal="g"))

    def seed(summary):
        summary["campaign"] = {
            "last_exit_code": 0,
            "last_exit_verified": False,
            "last_exit_reason": "NOT SOLVED; deciding to halt further attempts",
        }

    update_json_file(state_root / "summary.json", seed)
    report = harness.score_campaign_metrics(state_root)

    assert report["unresolved_success_exit"] is True
    # Exit 0 is classified as false success rather than a non-success surrender.
    assert report["voluntary_give_up_termination"] is False


def test_campaign_metric_aggregation_reports_required_rates():
    aggregate = harness.aggregate_campaign_metrics(
        [
            {
                "rejected_turns": 2,
                "coach_messages": 2,
                "voluntary_give_up_termination": False,
                "unresolved_success_exit": False,
                "routes": ["decompose", "negate"],
                "proof_shapes": ["simp"],
                "jobs_launched": 2,
                "jobs_consumed": 1,
                "jobs_replaced": 1,
                "verified_graph_progress": 1,
                "epoch_rollovers": 1,
            },
            {
                "rejected_turns": 1,
                "coach_messages": 1,
                "voluntary_give_up_termination": False,
                "unresolved_success_exit": False,
                "routes": ["plan"],
                "proof_shapes": ["omega"],
            },
        ]
    )

    assert aggregate["voluntary_give_up_termination_rate"] == 0
    assert aggregate["unresolved_success_exit_rate"] == 0
    assert aggregate["coach_coverage"] == 1
    assert aggregate["route_diversity"] == 3
    assert aggregate["proof_shape_diversity"] == 2
