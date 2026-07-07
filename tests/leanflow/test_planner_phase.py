"""Phase 5 (3/6) tests: planner fan-out, synthesis, merge, stub seeding.

Everything model-shaped is faked: delegate_task returns canned lane
results, run_model_verification_review returns a canned synthesis, and
place_helpers is stubbed where file mechanics are not the point. The
assertions pin the N1 contract (no lane ever lost), the kernel-truth
merge path (apply_delta only), and the guarded stub door.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import plan_state, planner_phase
from leanflow_cli.workflows.orchestrator import OrchestratorRoute


@pytest.fixture()
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLANNER_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))


def _delegate_payload(*summaries: str, statuses: tuple[str, ...] = ()) -> str:
    results = []
    for index, summary in enumerate(summaries):
        status = statuses[index] if index < len(statuses) else "completed"
        results.append({"task_index": index, "status": status, "summary": summary, "api_calls": 1})
    return json.dumps({"results": results, "total_duration_seconds": 1.0})


_SYNTHESIS = json.dumps(
    {
        "grounding": ["the bound follows from AM-GM"],
        "strategy": ["state the helper", "close the goal"],
        "nodes": [
            {
                "name": "demo_helper",
                "file": "Demo.lean",
                "statement": "lemma demo_helper : True := by sorry",
                "split_of": "demo",
            },
            {"name": "demo", "file": "Demo.lean", "statement": "theorem demo : True"},
            {"name": "vague_idea", "file": "Demo.lean"},
        ],
    }
)


def _fake_synth(monkeypatch, response: str = _SYNTHESIS, status: str = "ok"):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(response=response, status=status)

    monkeypatch.setattr(planner_phase, "run_model_verification_review", fake)
    return calls


def _fake_delegate(monkeypatch, payload: str):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return payload

    monkeypatch.setattr(planner_phase, "delegate_task", fake)
    return calls


def _fake_place(monkeypatch, *, ok: bool = True):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        names = [planner_phase.decomposer._helper_name(s) or "?" for s in kwargs["skeletons"]]
        return planner_phase.decomposer.DecomposeOutcome(
            ok=ok, reason="" if ok else "rejected", placed=tuple(names) if ok else ()
        )

    monkeypatch.setattr(planner_phase.decomposer, "place_helpers", fake)
    return calls


# ---------------------------------------------------------------------------
# Flags + guards
# ---------------------------------------------------------------------------


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("LEANFLOW_PLANNER_ENABLED", raising=False)
    assert planner_phase.planner_enabled() is False
    monkeypatch.setenv("LEANFLOW_PLANNER_ENABLED", "1")
    assert planner_phase.planner_enabled() is True


def test_max_subagents_clamped(monkeypatch):
    monkeypatch.setenv("LEANFLOW_PLANNER_MAX_SUBAGENTS", "9")
    assert planner_phase.planner_max_subagents() == 3
    monkeypatch.setenv("LEANFLOW_PLANNER_MAX_SUBAGENTS", "0")
    assert planner_phase.planner_max_subagents() == 1
    monkeypatch.setenv("LEANFLOW_PLANNER_MAX_SUBAGENTS", "junk")
    assert planner_phase.planner_max_subagents() == 3


def test_requires_goal_and_agent(enabled, monkeypatch):
    assert "no goal" in planner_phase.run_planner_phase(goal="", agent=object()).reason
    assert "no parent agent" in planner_phase.run_planner_phase(goal="g", agent=None).reason


# ---------------------------------------------------------------------------
# Lane fan-out: N1 — no lane is ever lost
# ---------------------------------------------------------------------------


def test_happy_path_merges_graph_and_prose(enabled, monkeypatch):
    delegate_calls = _fake_delegate(
        monkeypatch,
        _delegate_payload(
            '{"findings": [{"claim": "known", "source": "arxiv"}]}',
            '```json\n{"candidates": [{"name": "Nat.le_succ"}]}\n```',
            '{"hypothesis": "h", "result": "supports"}',
        ),
    )
    _fake_synth(monkeypatch)
    place_calls = _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(
        goal="prove demo", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok
    assert [lane["status"] for lane in outcome.lanes] == ["completed"] * 3
    assert outcome.nodes_added == 3
    assert outcome.stubs_placed == ("demo_helper",)
    assert outcome.grounding_count == 1 and outcome.strategy_count == 2

    # Fan-out contract: one batch, isolated budget, bounded iterations.
    call = delegate_calls[0]
    assert len(call["tasks"]) == 3
    assert call["isolate_budget"] is True
    assert call["max_iterations"] == planner_phase.LANE_MAX_ITERATIONS

    # Only the shape-valid target-file stub went through the guarded door
    # (the non-stub 'theorem demo : True' and the statement-less node did not).
    assert [len(c["skeletons"]) for c in place_calls] == [1]

    # Graph merged through apply_delta: derived statuses, planner provenance.
    bp = plan_state.load_blueprint()
    helper = bp.node_by_id(plan_state.node_id_for("demo_helper", "Demo.lean"))
    assert helper.status == "stated" and helper.generated_by == "planner"
    vague = bp.node_by_id(plan_state.node_id_for("vague_idea", "Demo.lean"))
    assert vague.status == "conjectured"

    summary = plan_state.load_summary()
    assert summary["grounding_findings"] == ["the bound follows from AM-GM"]
    assert "## Strategy" in plan_state.plan_state_paths().plan_md.read_text(encoding="utf-8")


def test_lane_parse_failure_is_recorded_not_lost(enabled, monkeypatch):
    _fake_delegate(
        monkeypatch,
        _delegate_payload("utter prose, no json", '{"candidates": []}', "{}"),
    )
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert outcome.ok
    statuses = {lane["lane"]: lane["status"] for lane in outcome.lanes}
    assert statuses["web"] == "parse-failure"
    assert statuses["mathlib"] == "completed"
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-lanes" in journal and "parse-failure" in journal


def test_delegate_explosion_yields_error_lanes(enabled, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("threads on fire")

    monkeypatch.setattr(planner_phase, "delegate_task", boom)
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    # Synthesis still runs (over zero deliverables); lanes carry the error.
    assert outcome.ok
    assert all(lane["status"] == "error" for lane in outcome.lanes)


def test_failed_lane_status_preserved(enabled, monkeypatch):
    _fake_delegate(
        monkeypatch,
        _delegate_payload("{}", "irrelevant", "{}", statuses=("completed", "failed", "error")),
    )
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    statuses = [lane["status"] for lane in outcome.lanes]
    assert statuses == ["completed", "failed", "error"]


# ---------------------------------------------------------------------------
# Lane selection via probes
# ---------------------------------------------------------------------------


def test_lane_keys_select_and_alias(enabled, monkeypatch):
    calls = _fake_delegate(monkeypatch, _delegate_payload("{}"))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object(), lane_keys=["deep-search"])

    assert outcome.ok
    assert [lane["lane"] for lane in outcome.lanes] == ["web"]
    assert len(calls[0]["tasks"]) == 1


def test_non_research_probe_selection_falls_back_to_full_wave(enabled, monkeypatch):
    calls = _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object(), lane_keys=["negation"])

    assert outcome.ok
    assert len(calls[0]["tasks"]) == 3


# ---------------------------------------------------------------------------
# Synthesizer failure modes keep the floor authoritative
# ---------------------------------------------------------------------------


def test_synthesizer_unavailable_fails_soft(enabled, monkeypatch):
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response="", status="unavailable")

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert not outcome.ok and "unavailable" in outcome.reason
    assert plan_state.load_blueprint().nodes == ()  # nothing merged


def test_synthesizer_garbage_fails_soft_with_lanes_kept(enabled, monkeypatch):
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch, response="not json at all")

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert not outcome.ok and outcome.synthesis_status == "parse-failure"
    assert len(outcome.lanes) == 3  # N1: lane work still reported


def test_rejected_stub_placement_is_journaled(enabled, monkeypatch):
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch, ok=False)

    outcome = planner_phase.run_planner_phase(
        goal="g", target_symbol="demo", active_file="Demo.lean", agent=object()
    )

    assert outcome.ok and outcome.stubs_placed == ()
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-stubs-rejected" in journal


def test_delegate_toplevel_error_object_becomes_error_lanes(enabled, monkeypatch):
    """delegate_task guard failures arrive as {'error': ...}, not exceptions."""
    _fake_delegate(monkeypatch, json.dumps({"error": "Delegation depth limit reached."}))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert all(lane["status"] == "error" for lane in outcome.lanes)
    assert "Delegation depth limit" in outcome.lanes[0]["error"]


def test_post_fanout_exception_keeps_lanes_in_outcome(enabled, monkeypatch):
    """N1: lane work done before a late failure stays in the payload."""
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    monkeypatch.setattr(
        planner_phase.plan_state,
        "save_summary",
        lambda payload: (_ for _ in ()).throw(OSError("disk full")),
    )

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert not outcome.ok and "OSError" in outcome.reason
    assert len(outcome.lanes) == 3


def test_revision_conflict_retry_journals_final_changes_once(enabled, monkeypatch):
    """The notebook describes the graph that was PERSISTED — one change set,
    journaled only after the save wins."""
    _fake_delegate(monkeypatch, _delegate_payload("{}", "{}", "{}"))
    _fake_synth(monkeypatch)
    _fake_place(monkeypatch)
    real_save = plan_state.save_blueprint
    fails = {"left": 1}

    def flaky_save(bp):
        if fails["left"]:
            fails["left"] -= 1
            raise plan_state.PlanStateRevisionConflict("raced")
        return real_save(bp)

    monkeypatch.setattr(planner_phase.plan_state, "save_blueprint", flaky_save)

    outcome = planner_phase.run_planner_phase(goal="g", agent=object())

    assert outcome.ok
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    helper_creations = [
        line for line in journal.splitlines() if "node-created" in line and "demo_helper" in line
    ]
    assert len(helper_creations) == 1


def test_never_raises(enabled, monkeypatch):
    monkeypatch.setattr(
        planner_phase.plan_state,
        "load_blueprint",
        lambda: (_ for _ in ()).throw(RuntimeError("corrupt")),
    )
    outcome = planner_phase.run_planner_phase(goal="g", agent=object())
    assert not outcome.ok and "RuntimeError" in outcome.reason


# ---------------------------------------------------------------------------
# Runner wiring: mechanical-first with directive fallback
# ---------------------------------------------------------------------------


def _apply_plan_route(autonomy_state: dict[str, Any], history: list) -> str:
    return runner._orchestrator_apply_route(
        OrchestratorRoute(route="plan", reason="scope-entry planning"),
        history,
        autonomy_state,
        {},
        agent=None,
    )


def test_runner_plan_route_uses_planner_when_enabled(enabled, monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr(
        runner.planner_phase,
        "run_planner_phase",
        lambda **kwargs: planner_phase.PlannerOutcome(
            ok=True, reason="planner phase completed", nodes_added=2, stubs_placed=("h1",)
        ),
    )
    history: list[dict] = []

    action = _apply_plan_route(
        {"_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}},
        history,
    )

    assert action == "continue"
    assert history and "[LEANFLOW ORCHESTRATOR ROUTE: plan]" in history[-1]["content"]
    assert "planner phase ran" in history[-1]["content"]
    assert any(a[0] == "planner" for a, _k in events)


def test_runner_plan_route_falls_back_to_directive(enabled, monkeypatch):
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)
    monkeypatch.setattr(
        runner.planner_phase,
        "run_planner_phase",
        lambda **kwargs: planner_phase.PlannerOutcome(ok=False, reason="synth down"),
    )
    history: list[dict] = []

    action = _apply_plan_route(
        {"_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}},
        history,
    )

    assert action == "continue"
    assert history and "- directive:" in history[-1]["content"]


def test_runner_plan_route_flag_off_is_directive_only(monkeypatch, tmp_path):
    monkeypatch.delenv("LEANFLOW_PLANNER_ENABLED", raising=False)
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "ps"))
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    def explode(**kwargs):
        raise AssertionError("planner must not run when the flag is off")

    monkeypatch.setattr(runner.planner_phase, "run_planner_phase", explode)
    history: list[dict] = []

    action = _apply_plan_route(
        {"_orchestrator_last_ctx": {"target_symbol": "demo", "active_file": "Demo.lean"}},
        history,
    )

    assert action == "continue"
    assert history and "- directive:" in history[-1]["content"]
