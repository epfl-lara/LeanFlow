"""Tests for plan_state — the Phase 1 living-plan artifacts (specs P1.1/P1.2/P1.6)."""

from __future__ import annotations

import json

import pytest

from leanflow_cli.workflows import plan_state
from leanflow_cli.workflows.plan_state import (
    Blueprint,
    DeclTruth,
    GraphEdge,
    GraphNode,
    PlanStateRevisionConflict,
)


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))
    return tmp_path / "plan-state"


def _demo_blueprint() -> Blueprint:
    main = GraphNode(id="n-main", name="main_thm", file="Demo.lean", status="stated")
    helper = GraphNode(id="n-helper", name="helper", file="Demo.lean", status="proved")
    child = GraphNode(id="n-child", name="child", file="Demo.lean", status="proving")
    return Blueprint(
        goal="prove main_thm",
        nodes=(main, helper, child),
        edges=(
            GraphEdge(source="n-main", target="n-helper", kind="depends_on"),
            GraphEdge(source="n-child", target="n-main", kind="split_of"),
        ),
    )


def test_everything_noops_when_flag_off(tmp_path, monkeypatch):
    state_dir = tmp_path / "plan-state"
    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(state_dir))

    assert plan_state.plan_state_enabled() is False
    assert plan_state.load_blueprint() == Blueprint()
    assert plan_state.load_summary() == {}
    plan_state.save_summary({"goal": "x"})
    plan_state.append_journal_event({"event": "x"})
    plan_state.write_final_report("documented")
    assert plan_state.artifact_context_block() == ""
    assert not state_dir.exists()


def test_blueprint_round_trip_and_revision_bump(enabled):
    bp = _demo_blueprint()

    saved = plan_state.save_blueprint(bp)
    assert saved.revision == 1
    loaded = plan_state.load_blueprint()

    assert loaded.goal == bp.goal
    assert [node.to_mapping() for node in loaded.nodes] == [node.to_mapping() for node in bp.nodes]
    assert [edge.to_mapping() for edge in loaded.edges] == [edge.to_mapping() for edge in bp.edges]
    assert loaded.revision == 1
    assert loaded.updated_at


def test_stale_revision_write_is_refused_loudly(enabled):
    first = plan_state.save_blueprint(_demo_blueprint())
    plan_state.save_blueprint(first)  # disk now at revision 2

    with pytest.raises(PlanStateRevisionConflict):
        plan_state.save_blueprint(first)  # still based on revision 1

    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    events = [json.loads(line) for line in journal.splitlines()]
    assert any(event["event"] == "plan-state-revision-conflict" for event in events)


def test_frontier_requires_proved_dependencies(enabled):
    bp = _demo_blueprint()
    assert [node.id for node in bp.frontier()] == ["n-main"]

    regressed = bp.replace_node(
        GraphNode(id="n-helper", name="helper", file="Demo.lean", status="stated")
    )
    assert [node.id for node in regressed.frontier()] == ["n-helper"]


def test_invalidate_false_subtree_poisons_split_ancestors_but_not_proved(enabled):
    bp = _demo_blueprint()

    poisoned = bp.invalidate_false_subtree("n-child")

    assert poisoned.node_by_id("n-child").status == "false"
    # n-main is the split_of parent: decomposition wrong -> back to conjectured.
    assert poisoned.node_by_id("n-main").status == "conjectured"
    # proved nodes are immutable kernel facts.
    assert poisoned.node_by_id("n-helper").status == "proved"


def test_set_node_status_enforces_kernel_truth_rules(enabled):
    bp = _demo_blueprint()

    with pytest.raises(ValueError, match="gate-accept"):
        plan_state.set_node_status(bp, "n-main", "proved")
    with pytest.raises(ValueError, match="negation promotion"):
        plan_state.set_node_status(bp, "n-main", "false")
    with pytest.raises(ValueError, match="immutable"):
        plan_state.set_node_status(bp, "n-helper", "stated")
    # via_gate only proves — it is not a downgrade licence.
    with pytest.raises(ValueError, match="immutable"):
        plan_state.set_node_status(bp, "n-helper", "stated", via_gate=True)

    gated = plan_state.set_node_status(bp, "n-main", "proved", via_gate=True)
    assert gated.node_by_id("n-main").status == "proved"


def test_upsert_node_for_assignment_get_or_create(enabled, monkeypatch, tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_NATIVE_RUNNER_OWNER", "run-42")

    bp, node = plan_state.upsert_node_for_assignment(
        Blueprint(), target_symbol="demo", active_file=str(active), statement="theorem demo"
    )
    assert node.status == "proving"
    assert node.owner == "run-42"
    assert node.id == plan_state.node_id_for("demo", str(active))

    again, same = plan_state.upsert_node_for_assignment(
        bp, target_symbol="demo", active_file=str(active), statement=""
    )
    assert len(again.nodes) == 1
    assert same.id == node.id
    assert same.statement == "theorem demo"

    proved = again.replace_node(
        plan_state.GraphNode(
            id=node.id, name="demo", file=str(active), statement="s", status="proved"
        )
    )
    _bp, kept = plan_state.upsert_node_for_assignment(
        proved, target_symbol="demo", active_file=str(active), statement="s"
    )
    assert kept.status == "proved"


def test_record_decision_packet_persists_and_cross_links(enabled):
    saved = plan_state.save_blueprint(_demo_blueprint())

    plan_state.record_decision_packet(
        {
            "packet_id": "bp-1",
            "scope": "theorem",
            "node_id": "n-main",
            "target_symbol": "main_thm",
        }
    )

    summary = plan_state.load_summary()
    assert summary["decision_packets"][0]["packet_id"] == "bp-1"
    reloaded = plan_state.load_blueprint()
    assert reloaded.node_by_id("n-main").decision_packets == ("bp-1",)
    assert reloaded.revision == saved.revision + 1

    # Idempotent by packet_id: a retry repairs, never duplicates.
    plan_state.record_decision_packet(
        {
            "packet_id": "bp-1",
            "scope": "theorem",
            "node_id": "n-main",
            "target_symbol": "main_thm",
            "decision": "park",
        }
    )
    summary = plan_state.load_summary()
    assert len(summary["decision_packets"]) == 1
    assert summary["decision_packets"][0]["decision"] == "park"
    assert plan_state.load_blueprint().node_by_id("n-main").decision_packets == ("bp-1",)
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    packet_events = [
        json.loads(line)
        for line in journal.splitlines()
        if json.loads(line).get("event") == "decision-packet"
    ]
    assert len(packet_events) == 1


def test_reconcile_downgrades_and_promotes_without_proving(enabled):
    bp = Blueprint(
        nodes=(
            GraphNode(id="n1", name="regressed", file="A.lean", status="proved"),
            GraphNode(id="n2", name="vanished", file="A.lean", status="proved"),
            GraphNode(id="n3", name="now_stated", file="A.lean", status="conjectured"),
            GraphNode(id="n4", name="still_clean", file="A.lean", status="stated"),
            GraphNode(id="n5", name="unscanned", file="B.lean", status="proved"),
        )
    )
    truth = {
        ("A.lean", "regressed"): DeclTruth(present=True, has_sorry=True),
        ("A.lean", "now_stated"): DeclTruth(present=True, has_sorry=True),
        ("A.lean", "still_clean"): DeclTruth(present=True, has_sorry=False),
    }

    updated, events = plan_state.reconcile(bp, truth)

    assert updated.node_by_id("n1").status == "stated"
    assert updated.node_by_id("n2").status == "conjectured"
    assert updated.node_by_id("n3").status == "stated"
    # Never promoted to proved; unscanned files untouched.
    assert updated.node_by_id("n4").status == "stated"
    assert updated.node_by_id("n5").status == "proved"
    assert {event["node_id"] for event in events} == {"n1", "n2", "n3"}
    assert all(event["event"] == "plan-graph-reconcile" for event in events)


def test_render_plan_md_sections_and_notes_preservation(enabled):
    bp = _demo_blueprint()
    summary = {
        "goal": "prove main_thm",
        "decision_packets": [
            {"packet_id": "bp-1", "scope": "theorem", "target_symbol": "main_thm"}
        ],
    }

    plan_state.save_plan_md(bp, summary)
    path = plan_state.plan_state_paths().plan_md
    first = path.read_text(encoding="utf-8")
    for heading in (
        "## Goal",
        "## Current state",
        "## Frontier",
        "## Grounding",
        "## Decision log",
        "## Dead ends & proven false",
        "## Final report",
        "## Notes",
    ):
        assert heading in first
    assert plan_state.PLAN_MD_GENERATED_MARKER in first
    assert "`main_thm`" in first

    edited = first.replace("[free-form notes below survive regeneration]", "KEEP THIS HUMAN NOTE")
    path.write_text(edited, encoding="utf-8")
    plan_state.save_plan_md(bp, summary)
    assert "KEEP THIS HUMAN NOTE" in path.read_text(encoding="utf-8")


def test_write_final_report_is_persisted_and_journaled(enabled):
    plan_state.save_blueprint(_demo_blueprint())

    plan_state.write_final_report("documented", detail={"summary": "parked at frontier"})

    summary = plan_state.load_summary()
    assert summary["final_report"]["status"] == "documented"
    assert "parked at frontier" in plan_state.plan_state_paths().plan_md.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        plan_state.write_final_report("gave-up")
    # "in-progress" is render-only, never a writable terminal state (N1).
    with pytest.raises(ValueError):
        plan_state.write_final_report("in-progress")
    # detail cannot smuggle a different status past the guard.
    plan_state.write_final_report("proved", detail={"status": "in-progress"})
    assert plan_state.load_summary()["final_report"]["status"] == "proved"


def test_artifact_blocks_are_stable_and_bounded(enabled):
    plan_state.save_blueprint(_demo_blueprint())

    paths_block = plan_state.artifact_paths_block()
    assert "blueprint.json" in paths_block
    assert paths_block == plan_state.artifact_paths_block()  # byte-stable

    digest = plan_state.frontier_digest_block()
    assert len(digest.splitlines()) <= 10
    assert "frontier: `main_thm`" in digest

    combined = plan_state.artifact_context_block()
    assert paths_block in combined
    assert "Dependency graph digest:" in combined


def test_node_id_is_stable_across_path_spellings(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    spelled = tmp_path / ".." / tmp_path.name / "Demo.lean"

    assert plan_state.node_id_for("demo", str(active)) == plan_state.node_id_for(
        "demo", str(spelled)
    )


def test_save_summary_never_regresses_foreign_keys(enabled):
    # Another writer's keys survive even a stale caller snapshot.
    stale = plan_state.load_summary()
    from leanflow_cli.workflows.workflow_json_io import update_json_file

    update_json_file(
        plan_state.plan_state_paths().summary_json,
        lambda summary: summary.update(
            {"manager_nudges": [{"mode": "dark"}], "dispatch_ledger": [{"state": "running"}]}
        ),
    )

    stale["goal"] = "merged later"
    stale["manager_nudges"] = []  # stale foreign copy must be ignored
    plan_state.save_summary(stale)

    current = plan_state.load_summary()
    assert current["goal"] == "merged later"
    assert current["manager_nudges"] == [{"mode": "dark"}]
    assert current["dispatch_ledger"] == [{"state": "running"}]
