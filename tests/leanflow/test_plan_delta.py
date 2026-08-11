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


def test_delta_private_name_signature_conflict_does_not_reuse_proved_node(enabled):
    """A different private declaration must not alias kernel-proved graph truth."""
    proved = _node(
        "residue_split",
        status="proved",
        statement=(
            "private lemma residue_split (k : ℕ) (hmod : k % 7 = 1) : "
            "k % 35 = 1 ∨ k % 35 = 8 := by omega"
        ),
    )
    bp = plan_state.Blueprint(nodes=(proved,))

    merged, changes = plan_state.apply_delta(
        bp,
        {
            "nodes": [
                {
                    "name": "residue_split",
                    "file": "Demo.lean",
                    "statement": (
                        "private lemma residue_split (k : ℕ) (hk : 1 ≤ k) "
                        "(hmod : k % 7 = 1) : "
                        "k % 35 = 1 ∨ k % 35 = 8 := by sorry"
                    ),
                },
                {
                    "name": "residue_branch",
                    "file": "Demo.lean",
                    "statement": "private lemma residue_branch : True := by sorry",
                    "depends_on": ["residue_split"],
                },
            ]
        },
    )

    assert merged.node_by_id(proved.id) == proved
    branch_id = plan_state.node_id_for("residue_branch", "Demo.lean")
    assert merged.node_by_id(branch_id).status == "stated"
    assert not any(edge.source == branch_id and edge.target == proved.id for edge in merged.edges)
    conflict = [
        change for change in changes if change.get("event") == "plan-delta-node-signature-conflict"
    ]
    assert len(conflict) == 1
    assert conflict == [
        {
            "event": "plan-delta-node-signature-conflict",
            "node_id": proved.id,
            "name": "residue_split",
            "file": "Demo.lean",
            "existing_signature_sha256": conflict[0]["existing_signature_sha256"],
            "proposed_signature_sha256": conflict[0]["proposed_signature_sha256"],
        }
    ]
    assert conflict[0]["existing_signature_sha256"]
    assert conflict[0]["proposed_signature_sha256"]
    assert conflict[0]["existing_signature_sha256"] != conflict[0]["proposed_signature_sha256"]
    assert any(
        change.get("event") == "plan-delta-edge-skipped"
        and change.get("reason") == "declaration signature conflict"
        for change in changes
    )


def test_delta_same_private_signature_reuses_proved_node(enabled):
    """Proof-body and whitespace changes preserve exact declaration reuse."""
    proved = _node(
        "reflexive",
        status="proved",
        statement="private lemma reflexive (k : ℕ) : k = k := by rfl",
    )
    bp = plan_state.Blueprint(nodes=(proved,))

    merged, changes = plan_state.apply_delta(
        bp,
        {
            "nodes": [
                {
                    "name": "reflexive",
                    "file": "Demo.lean",
                    "statement": ("private lemma reflexive\n    (k : ℕ) : k = k := by\n  sorry"),
                    "notes": "same statement, new route",
                }
            ]
        },
    )

    reused = merged.node_by_id(proved.id)
    assert reused.status == "proved"
    assert reused.statement == proved.statement
    assert reused.notes == "same statement, new route"
    assert not any(
        change.get("event") == "plan-delta-node-signature-conflict" for change in changes
    )


def test_delta_cannot_fill_signatureless_proved_node_by_name(enabled):
    """Legacy graph truth without a statement cannot authenticate a new declaration."""
    proved = _node("legacy_private", status="proved")

    merged, changes = plan_state.apply_delta(
        plan_state.Blueprint(nodes=(proved,)),
        {
            "nodes": [
                {
                    "name": "legacy_private",
                    "file": "Demo.lean",
                    "statement": "private lemma legacy_private : False := by sorry",
                }
            ]
        },
    )

    assert merged.node_by_id(proved.id) == proved
    conflict = next(
        change for change in changes if change.get("event") == "plan-delta-node-signature-conflict"
    )
    assert conflict["existing_signature_sha256"] == ""
    assert conflict["proposed_signature_sha256"]


def test_delta_rejects_edges_incident_to_signatureless_kernel_node(enabled):
    """Name-only references cannot attach graph structure to legacy kernel truth."""
    legacy = _node("legacy_private", status="proved")

    merged, changes = plan_state.apply_delta(
        plan_state.Blueprint(nodes=(legacy,)),
        {
            "nodes": [
                {
                    "name": "fresh_branch",
                    "file": "Demo.lean",
                    "statement": "private lemma fresh_branch : True := by sorry",
                    "depends_on": ["legacy_private"],
                }
            ],
            "edges": [
                {
                    "source": {"name": "legacy_private", "file": "Demo.lean"},
                    "target": {"name": "fresh_branch", "file": "Demo.lean"},
                    "kind": "evidence",
                }
            ],
        },
    )

    assert merged.edges == ()
    skipped = [
        change
        for change in changes
        if change.get("event") == "plan-delta-edge-skipped"
        and change.get("reason") == "unauthenticated kernel declaration"
    ]
    assert len(skipped) == 2


def test_delta_migrates_equivalent_legacy_decomposer_statement_core(enabled):
    """A full declaration authenticates the exact core stored by old decomposers."""
    legacy = _node(
        "reflexive",
        status="proved",
        statement="(k : Nat) : k = k",
        generated_by="decomposer",
    )
    proposed = "private lemma reflexive\n    (k : Nat) : k = k := by\n  sorry"

    merged, changes = plan_state.apply_delta(
        plan_state.Blueprint(nodes=(legacy,)),
        {
            "nodes": [
                {
                    "name": "reflexive",
                    "file": "Demo.lean",
                    "statement": proposed,
                }
            ]
        },
    )

    migrated = merged.node_by_id(legacy.id)
    assert migrated.status == "proved"
    assert migrated.statement == "private lemma reflexive (k : Nat) : k = k"
    assert any(change.get("event") == "plan-delta-node-signature-migrated" for change in changes)
    assert not any(
        change.get("event") == "plan-delta-node-signature-conflict" for change in changes
    )


def test_delta_does_not_migrate_different_legacy_decomposer_statement_core(enabled):
    """Legacy provenance permits migration only after an exact core comparison."""
    legacy = _node(
        "reflexive",
        status="proved",
        statement="(k : Nat) : k = k",
        generated_by="decomposer",
    )

    merged, changes = plan_state.apply_delta(
        plan_state.Blueprint(nodes=(legacy,)),
        {
            "nodes": [
                {
                    "name": "reflexive",
                    "file": "Demo.lean",
                    "statement": "private lemma reflexive (k : Nat) : k + 0 = k := by sorry",
                }
            ]
        },
    )

    assert merged.node_by_id(legacy.id) == legacy
    assert any(change.get("event") == "plan-delta-node-signature-conflict" for change in changes)
    assert not any(
        change.get("event") == "plan-delta-node-signature-migrated" for change in changes
    )


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


def test_delta_journal_deferral(enabled):
    """journal=False defers the notebook writes to journal_delta_changes —
    the conflicted-save-then-retry path journals only the persisted set."""
    _bp, changes = plan_state.apply_delta(
        plan_state.Blueprint(), {"nodes": [{"name": "j", "file": "D.lean"}]}, journal=False
    )

    assert not plan_state.plan_state_paths().journal_jsonl.exists()
    plan_state.journal_delta_changes(changes, generated_by="planner")
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "node-created" in journal


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


def test_merge_planner_findings_resets_strategy_when_assignment_changes():
    summary = {
        "grounding_findings": ["historical factorization fact"],
        "strategy_notes": ["Step 1: split the old exceptional family"],
        "strategy_notes_scope": {
            "target_symbol": "old_family",
            "active_file": "Erdos242.lean",
        },
    }

    merged = plan_state.merge_planner_findings(
        summary,
        grounding=["new residue fact"],
        strategy=["Step 1: attack the current family"],
        target_symbol="current_family",
        active_file="Erdos242.lean",
    )

    assert merged["grounding_findings"] == [
        "historical factorization fact",
        "new residue fact",
    ]
    assert merged["strategy_notes"] == ["Step 1: attack the current family"]
    assert merged["strategy_notes_scope"] == {
        "target_symbol": "current_family",
        "active_file": "Erdos242.lean",
    }


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
