"""Phase 5 (2/6) tests: planner graph-delta merge + summary prose merge.

apply_delta is the ONLY door a planner output has into the graph — these
tests pin its kernel-truth rules: planner nodes enter conjectured/stated
only, existing statuses are untouchable, statements never overwritten,
edges validated/deduped, and the whole merge is pure w.r.t. persistence.
"""

from __future__ import annotations

import pytest

from leanflow_cli.workflows import plan_state


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))


def _node(name: str, file: str = "Demo.lean", **kwargs) -> plan_state.GraphNode:
    return plan_state.GraphNode(
        id=plan_state.node_id_for(name, file), name=name, file=file, **kwargs
    )


# ---------------------------------------------------------------------------
# Node admission rules
# ---------------------------------------------------------------------------


def test_delta_creates_stated_and_conjectured_nodes(enabled):
    bp, changes = plan_state.apply_delta(
        plan_state.Blueprint(),
        {
            "goal": "prove the main bound",
            "nodes": [
                {"name": "helper_one", "file": "Demo.lean", "statement": "lemma h1 : True"},
                {"name": "idea_two", "file": "Demo.lean"},
            ],
        },
    )

    assert bp.goal == "prove the main bound"
    stated = bp.node_by_id(plan_state.node_id_for("helper_one", "Demo.lean"))
    conjectured = bp.node_by_id(plan_state.node_id_for("idea_two", "Demo.lean"))
    assert stated.status == "stated" and stated.generated_by == "planner"
    assert conjectured.status == "conjectured"
    assert any(c["event"] == "node-created" for c in changes)


def test_delta_status_is_derived_never_trusted(enabled):
    """Any status the delta claims is ignored: statement <=> stated."""
    bp, _ = plan_state.apply_delta(
        plan_state.Blueprint(),
        {
            "nodes": [
                {"name": "sneaky", "file": "Demo.lean", "statement": "s", "status": "proved"},
                {"name": "sneaky2", "file": "Demo.lean", "status": "false"},
                {"name": "sneaky3", "file": "Demo.lean", "status": "blocked"},
                # A statement-less 'stated' would enter the frontier with
                # nothing to prove; a stated-with-statement downgraded to
                # 'conjectured' would hide real work.
                {"name": "hollow", "file": "Demo.lean", "status": "stated"},
                {"name": "shy", "file": "Demo.lean", "statement": "s", "status": "conjectured"},
            ]
        },
    )

    statuses = {node.name: node.status for node in bp.nodes}
    assert statuses == {
        "sneaky": "stated",
        "sneaky2": "conjectured",
        "sneaky3": "conjectured",
        "hollow": "conjectured",
        "shy": "stated",
    }


def test_delta_cannot_touch_existing_status_or_statement(enabled):
    proved = _node("done", status="proved", statement="theorem done : True")
    bp = plan_state.Blueprint(nodes=(proved,))

    merged, _ = plan_state.apply_delta(
        bp,
        {
            "nodes": [
                {
                    "name": "done",
                    "file": "Demo.lean",
                    "statement": "theorem done : False",
                    "status": "conjectured",
                    "notes": "planner note",
                }
            ]
        },
    )

    node = merged.node_by_id(proved.id)
    assert node.status == "proved"
    assert node.statement == "theorem done : True"  # non-empty statement immutable
    assert node.notes == "planner note"  # blanks may be filled


def test_delta_truncates_runaway_node_lists(enabled):
    nodes = [{"name": f"n{i}", "file": "Demo.lean"} for i in range(40)]

    bp, changes = plan_state.apply_delta(plan_state.Blueprint(), {"nodes": nodes})

    assert len(bp.nodes) == plan_state.DELTA_MAX_NODES
    truncated = [c for c in changes if c["event"] == "plan-delta-truncated"]
    assert truncated and truncated[0]["dropped_nodes"] == 40 - plan_state.DELTA_MAX_NODES


def test_delta_goal_never_overwrites_existing(enabled):
    bp = plan_state.Blueprint(goal="original")
    merged, _ = plan_state.apply_delta(bp, {"goal": "usurper", "nodes": []})
    assert merged.goal == "original"


# ---------------------------------------------------------------------------
# Edge rules
# ---------------------------------------------------------------------------


def test_delta_builds_depends_on_and_split_of_edges(enabled):
    bp, _ = plan_state.apply_delta(
        plan_state.Blueprint(),
        {
            "nodes": [
                {"name": "parent", "file": "Demo.lean", "statement": "p"},
                {
                    "name": "child",
                    "file": "Demo.lean",
                    "statement": "c",
                    "depends_on": ["parent"],
                    "split_of": "parent",
                },
            ]
        },
    )

    kinds = {(e.source, e.target, e.kind) for e in bp.edges}
    child_id = plan_state.node_id_for("child", "Demo.lean")
    parent_id = plan_state.node_id_for("parent", "Demo.lean")
    assert (child_id, parent_id, "depends_on") in kinds
    assert (child_id, parent_id, "split_of") in kinds


def test_delta_drops_bad_edges_and_dedupes(enabled):
    existing_child = _node("a", statement="s", status="stated")
    existing_parent = _node("b", statement="s", status="stated")
    bp = plan_state.Blueprint(
        nodes=(existing_child, existing_parent),
        edges=(
            plan_state.GraphEdge(
                source=existing_child.id, target=existing_parent.id, kind="depends_on"
            ),
        ),
    )

    def ref(name: str) -> dict:
        return {"name": name, "file": "Demo.lean"}

    merged, changes = plan_state.apply_delta(
        bp,
        {
            "nodes": [],
            "edges": [
                {"source": ref("a"), "target": ref("b"), "kind": "depends_on"},  # duplicate
                {"source": ref("a"), "target": ref("a"), "kind": "depends_on"},  # self-edge
                {"source": ref("a"), "target": ref("ghost"), "kind": "depends_on"},  # unknown
                {"source": ref("a"), "target": ref("b"), "kind": "made-up-kind"},  # bad kind
                # Bare strings in the top-level edges list carry no file
                # context => unresolvable by design (use nodes[].depends_on).
                {"source": "a", "target": "b", "kind": "depends_on"},
            ],
        },
    )

    assert len(merged.edges) == 1  # nothing added
    skipped = [c for c in changes if c["event"] == "plan-delta-edge-skipped"]
    reasons = sorted(c["reason"] for c in skipped)
    assert reasons == ["unknown node", "unresolvable", "unresolvable"]


def test_delta_edges_resolve_cross_file_references(enabled):
    bp, _ = plan_state.apply_delta(
        plan_state.Blueprint(),
        {
            "nodes": [
                {"name": "here", "file": "A.lean", "statement": "s"},
                {
                    "name": "there",
                    "file": "B.lean",
                    "statement": "s",
                    "depends_on": [{"name": "here", "file": "A.lean"}],
                },
            ]
        },
    )

    edge = bp.edges[0]
    assert edge.source == plan_state.node_id_for("there", "B.lean")
    assert edge.target == plan_state.node_id_for("here", "A.lean")


# ---------------------------------------------------------------------------
# Purity + persistence interplay
# ---------------------------------------------------------------------------


def test_delta_is_pure_wrt_persistence(enabled):
    bp, _ = plan_state.apply_delta(
        plan_state.Blueprint(), {"nodes": [{"name": "x", "file": "Demo.lean"}]}
    )

    # Nothing written until the caller saves; then the revision machinery runs.
    assert plan_state.load_blueprint().nodes == ()
    saved = plan_state.save_blueprint(bp)
    assert saved.revision == 1
    assert plan_state.load_blueprint().node_by_id(plan_state.node_id_for("x", "Demo.lean"))


def test_delta_journals_events(enabled):
    plan_state.apply_delta(plan_state.Blueprint(), {"nodes": [{"name": "j", "file": "D.lean"}]})

    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "node-created" in journal and '"generated_by": "planner"' in journal


def test_delta_flag_off_never_writes_journal(monkeypatch, tmp_path):
    monkeypatch.delenv("LEANFLOW_PLAN_STATE", raising=False)
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "ps"))

    bp, _ = plan_state.apply_delta(
        plan_state.Blueprint(), {"nodes": [{"name": "x", "file": "D.lean"}]}
    )

    assert bp.nodes  # the pure merge still works
    assert not (tmp_path / "ps").exists()  # but nothing touches disk


# ---------------------------------------------------------------------------
# Summary prose merge + render
# ---------------------------------------------------------------------------


def test_merge_planner_findings_dedupes_and_caps():
    summary = {"grounding_findings": ["known fact"], "strategy_notes": []}

    merged = plan_state.merge_planner_findings(
        summary,
        grounding=["known fact", "new fact", "  new   fact "],
        strategy=[f"step {i}" for i in range(30)],
    )

    assert merged["grounding_findings"] == ["known fact", "new fact"]
    assert len(merged["strategy_notes"]) == 20  # capped, oldest kept
    assert merged["strategy_notes"][0] == "step 0"
    assert summary["strategy_notes"] == []  # input untouched (pure)


def test_render_plan_md_includes_strategy_section(enabled):
    text = plan_state.render_plan_md(
        plan_state.Blueprint(), {"strategy_notes": ["induct on n", "split the sum"]}
    )

    assert "## Strategy" in text
    assert "- induct on n" in text
    strategy_at = text.index("## Strategy")
    assert text.index("## Current state") < strategy_at < text.index("## Frontier")

    empty = plan_state.render_plan_md(plan_state.Blueprint(), {})
    assert "## Strategy" in empty and "- [none yet]" in empty


def test_save_plan_md_keeps_notes_tail_with_strategy(enabled):
    plan_state.save_plan_md(plan_state.Blueprint(), {})
    path = plan_state.plan_state_paths().plan_md
    content = path.read_text(encoding="utf-8")
    edited = content.replace("[free-form notes below survive regeneration]", "my precious notes")
    path.write_text(edited, encoding="utf-8")

    plan_state.save_plan_md(plan_state.Blueprint(), {"strategy_notes": ["try duality"]})

    final = path.read_text(encoding="utf-8")
    assert "- try duality" in final
    assert "my precious notes" in final


def test_prose_containing_notes_heading_cannot_hijack_tail(enabled):
    """'## Notes' inside rendered prose — mid-line, bare, or injected as a
    physical line via embedded newlines — must not become the tail anchor."""
    hostile = {
        "goal": "prove X\n## Notes\nhijacked goal",
        "strategy_notes": ["mention ## Notes mid-line", "line one\n## Notes\nline two"],
        "grounding_findings": ["## Notes"],
        "final_report": {"status": "documented", "summary": "done\n## Notes\ngotcha"},
    }
    plan_state.save_plan_md(plan_state.Blueprint(), hostile)
    path = plan_state.plan_state_paths().plan_md
    content = path.read_text(encoding="utf-8")
    content = content.replace("[free-form notes below survive regeneration]", "keep me")
    path.write_text(content, encoding="utf-8")

    plan_state.save_plan_md(plan_state.Blueprint(), hostile)

    final = path.read_text(encoding="utf-8")
    assert "keep me" in final
    # The generated body was regenerated, not swallowed into the tail:
    # every section heading appears exactly once.
    assert final.count("## Strategy") == 1
    assert final.count("## Goal") == 1
    # And the tail is anchored at the real heading, after all sections.
    assert final.index("## Final report") < final.index("\n## Notes")
