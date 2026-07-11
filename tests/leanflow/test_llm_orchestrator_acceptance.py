"""Phase 6 acceptance (§4.4): the FULL LLM orchestrator path.

A rigged stall context with the LLM flag on: the floor proposes a route,
the (faked) LLM answers decompose WITH statements, and those statements
are stated end-to-end through the guarded door — shape check, name
binding, placement, guard refresh — with the queue picking them up via
the normal rescan. Kernel truth: the gate chain is never touched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import orchestrator_llm, plan_state

FILE_TEXT = """import Mathlib.Tactic

theorem goal : True := by sorry
"""


@pytest.fixture()
def rigged(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "ps"))
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    (tmp_path / "Demo.lean").write_text(FILE_TEXT, encoding="utf-8")
    return tmp_path


def test_llm_decompose_states_stubs_end_to_end(rigged, monkeypatch):
    decision = json.dumps(
        {
            "route": "decompose",
            "reason": "two lemmas make this goal mechanical",
            "statements_to_state": [
                {"name": "goal_left", "statement": "lemma goal_left : True := by sorry"},
                {"name": "wrong_claim", "statement": "lemma other_name : True := by sorry"},
                {"name": "bad_shape", "statement": "lemma bad_shape : True"},
            ],
        }
    )
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **kwargs: SimpleNamespace(response=decision, status="ok"),
    )
    placed_calls: list[dict] = []

    def fake_place(**kwargs):
        placed_calls.append(kwargs)
        from leanflow_cli.workflows.decomposer import DecomposeOutcome

        return DecomposeOutcome(ok=True, placed=("goal_left",), file=kwargs["active_file"])

    monkeypatch.setattr(runner.decomposer, "place_helpers", fake_place)
    guard_refreshes: list[Any] = []
    monkeypatch.setattr(
        runner.decomposer, "refresh_queue_edit_guard", lambda agent: guard_refreshes.append(agent)
    )
    events: list[tuple] = []
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: events.append((a, k)))

    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {"target_symbol": "goal", "active_file": "Demo.lean"},
        "continuation_stable_cycles": 4,
    }
    live_state = {"target_symbol": "goal", "active_file": "Demo.lean", "declaration_queue": []}

    # The consult: floor + LLM upgrade, then apply.
    route = runner._orchestrator_consult("stall", autonomy_state, live_state)
    assert route is not None and route.source == "llm" and route.route == "decompose"

    action = runner._orchestrator_apply_route(route, [], autonomy_state, live_state, agent=None)

    assert action == "continue"
    # Only the shape-valid, name-bound statement reached the guarded door.
    assert len(placed_calls) == 1
    assert placed_calls[0]["skeletons"] == ["lemma goal_left : True := by sorry"]
    assert placed_calls[0]["target_symbol"] == "goal"
    assert guard_refreshes  # the prover's guard snapshots were refreshed
    assert any(a[0] == "decomposer" and "LLM-decision stubs" in a[1] for a, _k in events)


def test_llm_decompose_without_statements_falls_to_mechanical(rigged, monkeypatch):
    """No statements in the decision => the Phase 4 mechanical arm owns it."""
    decision = json.dumps({"route": "decompose", "reason": "split it"})
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **kwargs: SimpleNamespace(response=decision, status="ok"),
    )
    mechanical: list[dict] = []
    monkeypatch.setattr(
        runner.decomposer,
        "run_decomposer",
        lambda **kwargs: mechanical.append(kwargs)
        or runner.decomposer.DecomposeOutcome(ok=False, reason="advisor unavailable"),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {"target_symbol": "goal", "active_file": "Demo.lean"}
    }
    live_state = {"target_symbol": "goal", "active_file": "Demo.lean", "declaration_queue": []}
    route = runner._orchestrator_consult("stall", autonomy_state, live_state)
    assert route is not None and route.route == "decompose"

    action = runner._orchestrator_apply_route(route, [], autonomy_state, live_state, agent=None)

    assert action == "continue"
    assert mechanical  # fell through to run_decomposer
    # And the graph gate chain was never touched: no proved nodes appeared.
    assert all(node.status != "proved" for node in plan_state.load_blueprint().nodes)


def test_llm_door_filters_goal_restatement_and_records_the_split(rigged, monkeypatch):
    """The LLM statement door is guarded exactly like the mechanical arm:
    a child that merely restates the parent goal is dropped (anti-sorry-
    offloading), and the survivor's stated node + split edges enter the
    graph. Regression for the review finding that a renamed copy of the
    goal could pass and that stated helpers never reached the blueprint."""
    decision = json.dumps(
        {
            "route": "decompose",
            "reason": "the primality fact is the reusable piece",
            "statements_to_state": [
                {"name": "prime_seven", "statement": "lemma prime_seven : Nat.Prime 7 := by sorry"},
                # A renamed copy of the whole goal — must be rejected.
                {
                    "name": "hard_again",
                    "statement": "lemma hard_again : Nat.Prime 7 ∧ 2 + 2 = 4 := by sorry",
                },
            ],
        }
    )
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **kwargs: SimpleNamespace(response=decision, status="ok"),
    )
    placed_calls: list[dict] = []

    def fake_place(**kwargs):
        placed_calls.append(kwargs)
        from leanflow_cli.workflows.decomposer import DecomposeOutcome

        return DecomposeOutcome(ok=True, placed=("prime_seven",), file=kwargs["active_file"])

    monkeypatch.setattr(runner.decomposer, "place_helpers", fake_place)
    monkeypatch.setattr(runner.decomposer, "refresh_queue_edit_guard", lambda agent: None)
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "hard",
            "active_file": "Demo.lean",
            "slice": "theorem hard : Nat.Prime 7 ∧ 2 + 2 = 4 := by sorry",
        },
        "continuation_stable_cycles": 4,
    }
    live_state = {"target_symbol": "hard", "active_file": "Demo.lean", "declaration_queue": []}

    route = runner._orchestrator_consult("stall", autonomy_state, live_state)
    assert route is not None and route.route == "decompose"
    action = runner._orchestrator_apply_route(route, [], autonomy_state, live_state, agent=None)

    assert action == "continue"
    # The goal-restatement never reached the door; only the genuine helper did.
    assert len(placed_calls) == 1
    assert placed_calls[0]["skeletons"] == ["lemma prime_seven : Nat.Prime 7 := by sorry"]
    # The survivor entered the graph as a STATED node (never proved — no gate).
    nodes = {node.name: node for node in plan_state.load_blueprint().nodes}
    assert "prime_seven" in nodes
    assert nodes["prime_seven"].status == "stated"
    assert all(node.status != "proved" for node in nodes.values())


def test_park_without_armed_packet_mints_one(rigged, monkeypatch):
    """park-with-packet invariant (N1 closed set): a park proposed without
    a budget breakpoint — no armed packet — still terminates carrying a
    freshly minted decision packet, and in research mode that packet names
    the next candidate route. Regression for parks that returned
    `stop:parked` with empty evidence."""
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    route = runner.orchestrator_floor.OrchestratorRoute(
        route="park",
        reason="frontier exhausted; documenting",
        target={"next_candidate_route": "plan"},
        source="llm",
    )
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "goal", "active_file": "Demo.lean"},
        "current_queue_assignment": {
            "target_symbol": "goal",
            "active_file": "Demo.lean",
            "slice": "theorem goal : True := by sorry",
        },
        # No budget_breakpoint => no armed packet_id: the park must mint one.
    }
    live_state = {"target_symbol": "goal", "active_file": "Demo.lean", "declaration_queue": []}

    action = runner._orchestrator_apply_route(route, [], autonomy_state, live_state, agent=None)

    assert action == "stop:parked"
    packets = plan_state.load_summary().get("decision_packets") or []
    assert len(packets) == 1
    minted = packets[0]
    assert minted["packet_id"].startswith("park-")
    assert minted["decision"] == "park"  # _decide_packet resolved the minted packet
    assert minted["next_candidate_route"] == "plan"  # research park names its successor


def test_park_survives_packet_persistence_failure(rigged, monkeypatch):
    """Fail-closed: if every packet write raises, the park still terminates
    (`stop:parked`) without crashing, and no decided packet is left behind
    to be cited as dangling evidence."""
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    def boom(*_a, **_k):
        raise RuntimeError("plan-state write failed")

    monkeypatch.setattr(runner.plan_state, "record_decision_packet", boom)

    route = runner.orchestrator_floor.OrchestratorRoute(
        route="park",
        reason="frontier exhausted",
        target={"next_candidate_route": "plan"},
        source="llm",
    )
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {"target_symbol": "goal", "active_file": "Demo.lean"},
        "current_queue_assignment": {
            "target_symbol": "goal",
            "active_file": "Demo.lean",
            "slice": "theorem goal : True := by sorry",
        },
    }
    live_state = {"target_symbol": "goal", "active_file": "Demo.lean", "declaration_queue": []}

    action = runner._orchestrator_apply_route(route, [], autonomy_state, live_state, agent=None)

    assert action == "stop:parked"  # a persistence failure never crashes the park
    packets = plan_state.load_summary().get("decision_packets") or []
    assert not any(p.get("decision") == "park" for p in packets)  # nothing dangling
