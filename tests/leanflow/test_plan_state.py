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
from leanflow_cli.workflows.workflow_json_io import update_json_file


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


def test_render_plan_keeps_deferred_theorem_visible():
    rendered = plan_state.render_plan_md(
        _demo_blueprint(),
        {
            "deferred_queue_items": [
                {
                    "target_symbol": "unique_index_exists",
                    "active_file": "IMO2026/P1.lean",
                    "reason": "direct route exhausted",
                    "return_condition": "verified graph progress",
                }
            ]
        },
    )

    assert "## Deferred queue items (still pending)" in rendered
    assert "`unique_index_exists`" in rendered
    assert "verified graph progress" in rendered


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


def test_update_node_effort_is_monotonic():
    bp = _demo_blueprint()

    raised = plan_state.update_node_effort(bp, "n-main", attempts=3, api_steps=19)
    unchanged = plan_state.update_node_effort(raised, "n-main", attempts=1, api_steps=7)

    assert raised.node_by_id("n-main").attempts == 3
    assert raised.node_by_id("n-main").api_steps == 19
    assert unchanged == raised


def test_stale_revision_write_is_refused_loudly(enabled):
    first = plan_state.save_blueprint(_demo_blueprint())
    plan_state.save_blueprint(first)  # disk now at revision 2

    with pytest.raises(PlanStateRevisionConflict):
        plan_state.save_blueprint(first)  # still based on revision 1

    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    events = [json.loads(line) for line in journal.splitlines()]
    assert any(event["event"] == "plan-state-revision-conflict" for event in events)


def test_blueprint_commit_guard_is_reentrant_across_existing_save_path(enabled):
    """An outer cross-artifact lease survives nested graph reconciliation writes."""
    with plan_state.blueprint_commit_guard():
        first = plan_state.save_blueprint(_demo_blueprint())
        with plan_state.blueprint_commit_guard():
            second = plan_state.save_blueprint(first)
        assert plan_state.load_blueprint().revision == 2

    assert first.revision == 1
    assert second.revision == 2


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


def test_false_dependency_reopens_a_proved_decomposition_ancestor(enabled):
    child = GraphNode(id="n-child", name="child", file="Demo.lean", status="proving")
    main = GraphNode(id="n-main", name="main", file="Demo.lean", status="proved")
    bp = Blueprint(
        nodes=(main, child),
        edges=(
            GraphEdge(source="n-child", target="n-main", kind="split_of"),
            GraphEdge(source="n-main", target="n-child", kind="depends_on"),
        ),
    )

    poisoned = bp.invalidate_false_subtree("n-child")

    assert poisoned.node_by_id("n-child").status == "false"
    assert poisoned.node_by_id("n-main").status == "conjectured"


def test_invalid_dependency_detection_is_transitive(enabled):
    leaf = GraphNode(id="n-leaf", name="leaf", file="Demo.lean", status="false")
    middle = GraphNode(id="n-middle", name="middle", file="Demo.lean", status="stated")
    main = GraphNode(id="n-main", name="main", file="Demo.lean", status="proved")
    bp = Blueprint(
        nodes=(main, middle, leaf),
        edges=(
            GraphEdge(source="n-main", target="n-middle", kind="depends_on"),
            GraphEdge(source="n-middle", target="n-leaf", kind="depends_on"),
        ),
    )

    assert bp.has_invalid_dependency("n-main") is True
    assert bp.has_invalid_dependency("n-middle") is True
    assert bp.has_invalid_dependency("n-leaf") is False


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
            GraphNode(
                id="n6",
                name="unchecked_planner_stub",
                file="A.lean",
                status="conjectured",
                notes="This helper needs separate verification.",
                generated_by="planner",
            ),
        )
    )
    truth = {
        ("A.lean", "regressed"): DeclTruth(present=True, has_sorry=True),
        ("A.lean", "now_stated"): DeclTruth(present=True, has_sorry=True),
        ("A.lean", "still_clean"): DeclTruth(present=True, has_sorry=False),
        ("A.lean", "unchecked_planner_stub"): DeclTruth(present=True, has_sorry=True),
    }

    updated, events = plan_state.reconcile(bp, truth)

    assert updated.node_by_id("n1").status == "stated"
    assert updated.node_by_id("n2").status == "conjectured"
    assert updated.node_by_id("n3").status == "stated"
    # Never promoted to proved; unscanned files untouched.
    assert updated.node_by_id("n4").status == "stated"
    assert updated.node_by_id("n5").status == "proved"
    assert updated.node_by_id("n6").status == "conjectured"
    assert {event["node_id"] for event in events} == {"n1", "n2", "n3"}
    assert all(event["event"] == "plan-graph-reconcile" for event in events)


def test_reconcile_refreshes_clean_proved_declaration_snapshot_and_source_revision(enabled):
    bp = Blueprint(
        nodes=(
            GraphNode(
                id="n1",
                name="helper",
                file="A.lean",
                statement="lemma helper : True := by sorry",
                status="proved",
                source_sha256="old-revision",
            ),
        )
    )
    current = "lemma helper : True := by\n  trivial"

    updated, events = plan_state.reconcile(
        bp,
        {
            ("A.lean", "helper"): DeclTruth(
                present=True,
                has_sorry=False,
                declaration_text=current,
                source_sha256="new-revision",
            )
        },
    )

    node = updated.node_by_id("n1")
    assert node is not None
    assert node.status == "proved"
    assert node.statement == current
    assert node.source_sha256 == "new-revision"
    assert events == []


def test_reconcile_retires_absent_generated_stub_from_frontier(enabled):
    target = GraphNode(
        id=plan_state.node_id_for("result", "A.lean"),
        name="result",
        file="A.lean",
        status="proving",
    )
    stale = GraphNode(
        id=plan_state.node_id_for("generated_helper", "A.lean"),
        name="generated_helper",
        file="A.lean",
        status="stated",
        generated_by="decomposer",
    )
    bp = Blueprint(
        nodes=(target, stale),
        edges=(
            GraphEdge(stale.id, target.id, "split_of"),
            GraphEdge(target.id, stale.id, "depends_on"),
        ),
    )

    updated, events = plan_state.reconcile(
        bp,
        {
            ("A.lean", "result"): DeclTruth(present=True, has_sorry=True),
            ("A.lean", "generated_helper"): DeclTruth(present=False, has_sorry=False),
        },
    )

    assert updated.node_by_id(stale.id).status == "conjectured"
    assert stale.id not in {node.id for node in updated.frontier()}
    assert events == [
        {
            "event": "plan-graph-reconcile",
            "node_id": stale.id,
            "name": "generated_helper",
            "file": "A.lean",
            "from": "stated",
            "to": "conjectured",
        }
    ]


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
        "## Exploration outcomes",
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


def test_render_plan_dead_ends_include_rejected_attempts(enabled):
    plan_state.append_journal_event(
        {
            "event": "proof-attempt-rejected",
            "name": "main_thm",
            "file": "Demo.lean",
            "proof_shape": "exact stale_route",
            "reason": "unknown constant stale_route",
        }
    )
    summary = {
        "queue_manager_state": {
            "current_queue_assignment": {
                "target_symbol": "main_thm",
                "active_file": "Demo.lean",
            }
        }
    }

    rendered = plan_state.render_plan_md(_demo_blueprint(), summary)
    dead = rendered.split("## Dead ends & proven false", 1)[1].split("## Final report", 1)[0]

    assert "[rejected_by_kernel attempt]" in dead
    assert "exact stale_route" in dead
    assert "unknown constant stale_route" in dead
    assert "- [none]" not in dead


def test_plan_render_surfaces_current_route_and_recent_route_decisions(enabled):
    summary = {
        "campaign": {
            "last_route_decision": {
                "route": "decompose",
                "target_symbol": "main_thm",
                "active_file": "Demo.lean",
                "decided_at": "2026-07-15T18:00:00+00:00",
            }
        }
    }
    for route, reason in (
        ("direct-prove", "start with the assigned theorem"),
        ("decompose", "split the remaining residue classes"),
    ):
        plan_state.append_journal_event(
            {
                "event": "orchestrator-route",
                "trigger": "stall",
                "route": route,
                "reason": reason,
                "source": "deterministic",
                "name": "main_thm",
            }
        )

    plan_state.save_plan_md(_demo_blueprint(), summary)

    rendered = plan_state.plan_state_paths().plan_md.read_text(encoding="utf-8")
    strategy = rendered.split("## Strategy", 1)[1].split("## Frontier", 1)[0]
    decisions = rendered.split("## Decision log", 1)[1].split("## Dead ends & proven false", 1)[0]
    assert "current orchestrator route: `decompose`" in strategy
    assert "[none yet]" not in strategy
    assert "route `direct-prove`" in decisions
    assert "route `decompose`" in decisions


def test_resume_omits_advisory_route_rationale_but_keeps_routing_metadata(enabled):
    """Unverified route prose must not become mathematical resume knowledge."""
    false_rationale = "decompose because 0 ∣ 2521 * 631 is true"
    plan_state.save_blueprint(_demo_blueprint())
    plan_state.save_queue_manager_state(
        {
            "current_queue_assignment": {
                "target_symbol": "main_thm",
                "active_file": "Demo.lean",
            }
        }
    )
    update_json_file(
        plan_state.plan_state_paths().summary_json,
        lambda summary: summary.update(
            {
                "campaign": {
                    "epoch": 9,
                    "no_progress_route_streak": 3,
                    "no_progress_route_limit": 4,
                    "last_route_decision": {
                        "route": "decompose",
                        "target_symbol": "main_thm",
                        "active_file": "Demo.lean",
                    },
                }
            }
        ),
    )
    plan_state.append_journal_event(
        {
            "event": "orchestrator-route",
            "trigger": "event",
            "route": "decompose",
            "reason": false_rationale,
            "source": "llm",
            "name": "main_thm",
            "file": "Demo.lean",
        }
    )
    plan_state.save_plan_md(plan_state.load_blueprint(), plan_state.load_summary())

    block = plan_state.resume_context_block()
    generated_plan = plan_state.read_generated_plan_prompt_view()

    assert false_rationale not in block
    assert false_rationale not in generated_plan
    assert "current orchestrator route: `decompose` for `main_thm`" in block
    assert "recent route decision: `decompose` for `main_thm`" in block
    assert "trigger=event" in block
    assert "source=llm" in block
    assert "campaign epoch: 9" in block
    assert "route streak: 3/4" in block
    assert "route rationales are omitted" in block
    assert plan_state.recent_orchestrator_routes()[-1]["reason"] == false_rationale


def test_plan_render_does_not_call_a_retired_assignment_route_current(enabled):
    summary = {
        "queue_manager_state": {
            "current_queue_assignment": {
                "target_symbol": "new_target",
                "active_file": "Demo.lean",
            }
        },
        "campaign": {
            "last_route_decision": {
                "route": "direct-prove",
                "target_symbol": "solved_target",
                "active_file": "Demo.lean",
            }
        },
    }
    recent = (
        {
            "event": "orchestrator-route",
            "route": "direct-prove",
            "name": "solved_target",
            "file": "Demo.lean",
            "trigger": "scope-entry",
        },
    )

    rendered = plan_state.render_plan_md(
        _demo_blueprint(),
        summary,
        recent_routes=recent,
    )

    strategy = rendered.split("## Strategy", 1)[1].split("## Frontier", 1)[0]
    decisions = rendered.split("## Decision log", 1)[1].split("## Dead ends & proven false", 1)[0]
    assert "current orchestrator route" not in strategy
    assert "current deterministic assignment: `new_target`" in strategy
    assert "[none yet]" not in strategy
    assert "route `direct-prove` for `solved_target`" in decisions


@pytest.mark.parametrize(
    "strategy_scope",
    [
        None,
        {"target_symbol": "old_target", "active_file": "Demo.lean"},
    ],
)
def test_plan_render_suppresses_unscoped_or_retired_assignment_strategy(enabled, strategy_scope):
    summary = {
        "queue_manager_state": {
            "current_queue_assignment": {
                "target_symbol": "current_target",
                "active_file": "Demo.lean",
            }
        },
        "campaign": {
            "last_route_decision": {
                "route": "plan",
                "target_symbol": "current_target",
                "active_file": "Demo.lean",
            }
        },
        "strategy_notes": ["Step 1: decompose the retired target"],
    }
    if strategy_scope is not None:
        summary["strategy_notes_scope"] = strategy_scope

    rendered = plan_state.render_plan_md(_demo_blueprint(), summary)
    strategy = rendered.split("## Strategy", 1)[1].split("## Frontier", 1)[0]

    assert "current orchestrator route: `plan` for `current_target`" in strategy
    assert "decompose the retired target" not in strategy


def test_plan_render_keeps_strategy_for_exact_current_assignment(enabled):
    summary = {
        "queue_manager_state": {
            "current_queue_assignment": {
                "target_symbol": "current_target",
                "active_file": "Demo.lean",
            }
        },
        "strategy_notes": ["Step 1: attack the current target"],
        "strategy_notes_scope": {
            "target_symbol": "current_target",
            "active_file": "Demo.lean",
        },
    }

    rendered = plan_state.render_plan_md(_demo_blueprint(), summary)

    assert "Step 1: attack the current target" in rendered


def test_plan_render_scopes_strategy_and_frontier_to_current_assignment(enabled):
    """Resume views never present unrelated campaign inventory as current work."""
    current = GraphNode(
        id=plan_state.node_id_for("current_target", "Demo.lean"),
        name="current_target",
        file="Demo.lean",
        status="proving",
    )
    dependency = GraphNode(
        id=plan_state.node_id_for("current_dependency", "Demo.lean"),
        name="current_dependency",
        file="Demo.lean",
        status="stated",
    )
    unrelated = GraphNode(
        id=plan_state.node_id_for("unrelated_frontier", "Demo.lean"),
        name="unrelated_frontier",
        file="Demo.lean",
        status="stated",
    )
    blueprint = Blueprint(
        nodes=(current, dependency, unrelated),
        edges=(GraphEdge(current.id, dependency.id, "depends_on"),),
    )
    summary = {
        "queue_manager_state": {
            "current_queue_assignment": {
                "target_symbol": "current_target",
                "active_file": "Demo.lean",
            }
        },
        "campaign": {
            "last_route_decision": {
                "route": "decompose",
                "target_symbol": "retired_target",
                "active_file": "Demo.lean",
            }
        },
        "strategy_notes": ["attack the retired target"],
        "strategy_notes_scope": {
            "target_symbol": "retired_target",
            "active_file": "Demo.lean",
        },
    }

    rendered = plan_state.render_plan_md(blueprint, summary)

    strategy = rendered.split("## Strategy", 1)[1].split("## Frontier", 1)[0]
    frontier = rendered.split("## Frontier", 1)[1].split("## Grounding", 1)[0]
    assert "current deterministic assignment: `current_target`" in strategy
    assert "retired_target" not in strategy
    assert "current assignment: `current_target`" in frontier
    assert "dependency frontier: `current_dependency`" in frontier
    assert "unrelated_frontier" not in frontier


def test_plan_regeneration_preserves_notes_bytes_and_restores_generated_authority(enabled):
    summary = {"strategy_notes": ["authoritative generated strategy"]}
    bp = _demo_blueprint()
    plan_state.save_plan_md(bp, summary)
    path = plan_state.plan_state_paths().plan_md
    current = path.read_text(encoding="utf-8")
    duplicated = current.replace(
        "## Strategy\n\n- authoritative generated strategy",
        "## Strategy\n\n- MODEL EDIT THAT MUST NOT SURVIVE",
    ).replace(
        "## Notes",
        "## Notes\n\n- newly appended agent note\n\n## Notes",
        1,
    )
    duplicated = duplicated.replace(
        "[free-form notes below survive regeneration]",
        "KEEP THIS HISTORICAL USER NOTE",
    )
    preserved_tail = duplicated[duplicated.index("## Notes") :]
    path.write_text(duplicated, encoding="utf-8")

    plan_state.save_plan_md(bp, summary)

    final = path.read_text(encoding="utf-8")
    assert final.endswith(preserved_tail)
    assert "newly appended agent note" in final
    assert "KEEP THIS HISTORICAL USER NOTE" in final
    assert "authoritative generated strategy" in final
    assert "MODEL EDIT THAT MUST NOT SURVIVE" not in final


def test_generated_plan_prompt_read_stops_before_user_notes_without_rewriting(enabled):
    """Keep historical Notes out of prompts and preserve their exact bytes."""
    path = plan_state.plan_state_paths().plan_md
    path.parent.mkdir(parents=True, exist_ok=True)
    before = (
        b"# Proving Plan\n\n## Strategy\n\n- generated attack order\n\n"
        b"## Notes\n\nKEEP THIS HISTORICAL USER NOTE\n" + (b"x" * 20_000)
    )
    path.write_bytes(before)

    first = plan_state.read_generated_plan_prompt_view(max_chars=1_000)
    second = plan_state.read_generated_plan_prompt_view(max_chars=1_000)

    assert first == second
    assert "generated attack order" in first
    assert "## Notes" not in first
    assert "KEEP THIS HISTORICAL USER NOTE" not in first
    assert len(first) <= 1_000
    assert path.read_bytes() == before


def test_generated_plan_prompt_default_uses_eight_thousand_character_projection(enabled):
    text = (
        "# Proving Plan\n\n## Goal\n\nprove current_target\n\n"
        "## Strategy\n\n- current route\n\n"
        "## Frontier\n\n- `current_target` (Demo.lean)\n\n"
        "## Grounding\n\n" + ("x" * 20_000) + "\n\n## Final report\n\n- status: in-progress\n\n"
        "## Notes\n\nHISTORICAL"
    )

    view = plan_state.generated_plan_prompt_view(text)

    assert plan_state.PLAN_PROMPT_VIEW_MAX_CHARS == 8_000
    assert len(view) == 8_000
    assert "prove current_target" in view
    assert "current route" in view
    assert "`current_target` (Demo.lean)" in view
    assert "## Final report" in view
    assert "returned_chars=8000" in view
    assert "HISTORICAL" not in view


def test_resume_context_privileges_current_inventory_over_historical_notes(enabled):
    stale_main = GraphNode(
        id="n-main",
        name="main_thm",
        file="Demo.lean",
        statement="theorem main_thm : OldShape := by sorry",
        status="blocked",
    )
    plan_state.save_blueprint(Blueprint(goal="prove main_thm", nodes=(stale_main,)))
    plan_state.save_queue_manager_state(
        {
            "current_queue_assignment": {
                "target_symbol": "current_helper",
                "active_file": "Demo.lean",
                "slice": "theorem current_helper : FreshShape := by sorry",
            }
        }
    )
    from leanflow_cli.workflows.workflow_json_io import update_json_file

    update_json_file(
        plan_state.plan_state_paths().summary_json,
        lambda summary: summary.update(
            {
                "campaign": {
                    "last_route_decision": {
                        "route": "plan",
                        "target_symbol": "current_helper",
                        "active_file": "Demo.lean",
                    }
                }
            }
        ),
    )
    plan_state.save_plan_md(plan_state.load_blueprint(), plan_state.load_summary())
    path = plan_state.plan_state_paths().plan_md
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[free-form notes below survive regeneration]",
            "STALE INVENTORY: only two sorries remain; use theorem OldShape",
        ),
        encoding="utf-8",
    )

    block = plan_state.resume_context_block()

    assert "current deterministic assignment: `current_helper` (Demo.lean)" in block
    assert "current orchestrator route: `plan`" in block
    assert "Notes are preserved historical context, not inventory or declaration truth" in block
    assert "current Lean source and queue assignment outrank stored graph statements" in block
    assert "STALE INVENTORY" not in block
    assert "FreshShape" not in block  # declaration bodies stay source-owned, not prompt-owned


def test_resume_context_preserves_only_exact_scope_checkpoint_negative_evidence(enabled):
    current_file = "Demo.lean"
    target = GraphNode(
        id=plan_state.node_id_for("current_helper", current_file),
        name="current_helper",
        file=current_file,
        statement="theorem current_helper : True := by sorry",
        status="proving",
    )
    plan_state.save_blueprint(Blueprint(goal="prove current_helper", nodes=(target,)))
    plan_state.save_queue_manager_state(
        {
            "current_queue_assignment": {
                "target_symbol": "current_helper",
                "active_file": current_file,
            }
        }
    )
    assert plan_state.record_checkpoint_advisory(
        checkpoint_id="old-scope",
        created_at="2026-08-05T00:00:00+00:00",
        target_symbol="retired_helper",
        active_file=current_file,
        negative_evidence=["Do not repeat the stale theorem route."],
    )
    assert plan_state.record_checkpoint_advisory(
        checkpoint_id="current-scope",
        created_at="2026-08-05T00:01:00+00:00",
        target_symbol="current_helper",
        active_file=current_file,
        negative_evidence=["The predecessor map reaches the forbidden boundary value."],
    )
    assert plan_state.record_checkpoint_advisory(
        checkpoint_id="current-scope-newer",
        created_at="2026-08-05T00:02:00+00:00",
        target_symbol="current_helper",
        active_file=current_file,
        negative_evidence=["Broad simplification was rejected by the kernel."],
    )
    assert not plan_state.record_checkpoint_advisory(
        checkpoint_id="current-scope-newer",
        created_at="2026-08-05T00:02:00+00:00",
        target_symbol="current_helper",
        active_file=current_file,
        negative_evidence=["Broad simplification was rejected by the kernel."],
    )

    block = plan_state.resume_context_block()
    plan_state.save_plan_md(plan_state.load_blueprint(), plan_state.load_summary())
    generated_plan = plan_state.read_generated_plan_prompt_view()

    assert "advisory dead-branch boundary" in block
    assert "prior negative evidence: The predecessor map reaches" in block
    assert "prior negative evidence: Broad simplification" in block
    assert "stale theorem route" not in block
    assert "## Advisory dead-branch record" in generated_plan
    assert "predecessor map reaches" in generated_plan
    assert "Broad simplification" in generated_plan
    assert "stale theorem route" not in generated_plan

    digest = plan_state.frontier_digest_block()
    assert "advisory route exclusion" in digest
    assert "Broad simplification" in digest
    assert len(digest.splitlines()) <= 10


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


def test_frontier_digest_exposes_only_the_current_assignment_route(enabled):
    plan_state.save_blueprint(_demo_blueprint())
    plan_state.save_queue_manager_state(
        {
            "current_queue_assignment": {
                "target_symbol": "new_target",
                "active_file": "Demo.lean",
            }
        }
    )
    summary = plan_state.load_summary()
    summary["campaign"] = {
        "last_route_decision": {
            "route": "direct-prove",
            "target_symbol": "solved_target",
            "active_file": "Demo.lean",
        }
    }
    plan_state.save_summary(summary)
    plan_state.append_journal_event(
        {
            "event": "orchestrator-route",
            "route": "direct-prove",
            "name": "solved_target",
            "file": "Demo.lean",
        }
    )

    stale_digest = plan_state.frontier_digest_block()

    assert "deterministic assignment: `new_target`" in stale_digest
    assert "current route" not in stale_digest

    summary = plan_state.load_summary()
    summary["campaign"] = {
        "last_route_decision": {
            "route": "plan",
            "target_symbol": "new_target",
            "active_file": "Demo.lean",
        }
    }
    plan_state.save_summary(summary)
    plan_state.append_journal_event(
        {
            "event": "orchestrator-route",
            "route": "plan",
            "name": "new_target",
            "file": "Demo.lean",
        }
    )

    fresh_digest = plan_state.frontier_digest_block()

    assert "current route: `plan` for `new_target`" in fresh_digest
    assert "solved_target" not in fresh_digest


def test_exploration_outcomes_are_typed_and_assignment_scoped(enabled):
    bp = _demo_blueprint()
    assignment = {"target_symbol": "main_thm", "active_file": "Demo.lean"}
    plan_state.save_blueprint(bp)
    plan_state.save_queue_manager_state({"current_queue_assignment": assignment})
    plan_state.append_journal_event(
        {
            "event": "proof-attempt-rejected",
            "name": "main_thm",
            "file": "Demo.lean",
            "proof_shape": "omega",
            "reason": "kernel type mismatch",
        }
    )
    plan_state.append_journal_event(
        {
            "event": "proof-attempt-rejected",
            "name": "other_thm",
            "file": "Other.lean",
            "proof_shape": "simp",
            "reason": "unrelated failure",
        }
    )
    plan_state.append_journal_event(
        {
            "event": "node-status",
            "node_id": "n-helper",
            "name": "helper",
            "from": "proving",
            "to": "proved",
            "via_gate": True,
            "why": "exact helper gate passed",
        }
    )

    outcomes = plan_state.recent_exploration_outcomes(bp, assignment)

    assert [outcome["type"] for outcome in outcomes] == [
        "rejected_by_kernel",
        "proved",
    ]
    assert [outcome["subject"] for outcome in outcomes] == ["main_thm", "helper"]
    rendered = plan_state.render_plan_md(bp, plan_state.load_summary())
    assert "[rejected_by_kernel] `main_thm`" in rendered
    assert "[proved] `helper`" in rendered
    assert "other_thm" not in rendered


def test_frontier_digest_includes_typed_dead_branch(enabled):
    plan_state.save_blueprint(_demo_blueprint())
    plan_state.save_queue_manager_state(
        {
            "current_queue_assignment": {
                "target_symbol": "main_thm",
                "active_file": "Demo.lean",
            }
        }
    )
    plan_state.append_journal_event(
        {
            "event": "proof-attempt-rejected",
            "name": "main_thm",
            "file": "Demo.lean",
            "proof_shape": "linarith",
            "reason": "declaration is hidden by source order",
        }
    )

    digest = plan_state.frontier_digest_block()

    assert "outcome [blocked_by_source_order] `main_thm`" in digest
    assert len(digest.splitlines()) <= 10


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
            {
                "manager_nudges": [{"mode": "dark"}],
                "dispatch_ledger": [{"state": "running"}],
                "research_finding_migration": {"version": 2, "records": {"ds-1": {}}},
                "research_delivery_backpressure": {
                    "active": True,
                    "scope": "active_delivery_target",
                },
                "research_portfolio_failure_backoff": {
                    "version": 2,
                    "scopes": {"scope-1": {"consecutive_failures": 2}},
                },
                "source_negation_candidate_scans": [
                    {
                        "schema_version": 3,
                        "check_contract_version": "exact-source-harness-v3",
                        "scope_key": "Demo.lean::demo",
                        "source_revision_sha256": "a" * 64,
                        "exact_order_sha256": "b" * 64,
                        "exact_cursor": 1,
                        "generic_order_sha256": "c" * 64,
                        "generic_cursor": 7,
                    }
                ],
                "pending_research_helper_candidate": {"candidate_id": "rhcp-current"},
                "resolved_research_helper_candidates": [{"candidate_id": "rhcp-resolved"}],
                "target_candidate_checkpoints": [{"candidate_id": "tcc-current"}],
            }
        ),
    )

    stale["goal"] = "merged later"
    stale["manager_nudges"] = []  # stale foreign copy must be ignored
    stale["research_finding_migration"] = {"version": 1}
    stale["research_delivery_backpressure"] = {"active": False}
    stale["research_portfolio_failure_backoff"] = {"version": 1, "scopes": {}}
    stale["source_negation_candidate_scans"] = []
    stale["pending_research_helper_candidate"] = {}
    stale["resolved_research_helper_candidates"] = []
    stale["target_candidate_checkpoints"] = []
    plan_state.save_summary(stale)

    current = plan_state.load_summary()
    assert current["goal"] == "merged later"
    assert current["manager_nudges"] == [{"mode": "dark"}]
    assert current["dispatch_ledger"] == [{"state": "running"}]
    assert current["research_finding_migration"] == {
        "version": 2,
        "records": {"ds-1": {}},
    }
    assert current["research_delivery_backpressure"] == {
        "active": True,
        "scope": "active_delivery_target",
    }
    assert current["research_portfolio_failure_backoff"] == {
        "version": 2,
        "scopes": {"scope-1": {"consecutive_failures": 2}},
    }
    assert current["source_negation_candidate_scans"] == [
        {
            "schema_version": 3,
            "check_contract_version": "exact-source-harness-v3",
            "scope_key": "Demo.lean::demo",
            "source_revision_sha256": "a" * 64,
            "exact_order_sha256": "b" * 64,
            "exact_cursor": 1,
            "generic_order_sha256": "c" * 64,
            "generic_cursor": 7,
        }
    ]
    assert current["pending_research_helper_candidate"] == {"candidate_id": "rhcp-current"}
    assert current["resolved_research_helper_candidates"] == [{"candidate_id": "rhcp-resolved"}]
    assert current["target_candidate_checkpoints"] == [{"candidate_id": "tcc-current"}]


def test_queue_manager_state_has_a_dedicated_non_regressing_writer(enabled):
    stale = plan_state.load_summary()
    state = {
        "failed_attempts": [
            {
                "target_symbol": "demo",
                "active_file": "Demo.lean",
                "attempt": 4,
                "cycle": 2,
                "proof_shape": "exact missing",
                "reason": "unknown identifier",
            }
        ]
    }

    plan_state.save_queue_manager_state(state)
    stale["queue_manager_state"] = {}
    stale["goal"] = "updated goal"
    plan_state.save_summary(stale)

    current = plan_state.load_summary()
    assert current["goal"] == "updated goal"
    assert current["queue_manager_state"] == state
    assert plan_state.load_queue_manager_state() == state


def test_queue_manager_state_rebuilds_old_campaign_attempts_from_journal(enabled):
    for index in range(12):
        plan_state.append_journal_event(
            {
                "event": "proof-attempt-rejected",
                "attempt": index + 1,
                "cycle": index,
                "name": "demo",
                "file": "Demo.lean",
                "proof_shape": f"shape {index + 1}",
                "reason": f"failure {index + 1}",
            }
        )

    restored = plan_state.load_queue_manager_state()["failed_attempts"]

    assert len(restored) == 10
    assert restored[0]["attempt"] == 3
    assert restored[-1]["attempt"] == 12
    assert restored[-1]["proof_shape"] == "shape 12"
