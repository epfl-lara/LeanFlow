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

ERDOS_PARENT = """private lemma erdos_242_residual_mod_seven_eq_one (k : ℕ) (hk : 1 ≤ k)
    (hmod : k % 7 = 1) :
    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧
      (4 / ((24 * k + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by
  sorry"""

ERDOS_CLOSED_SINGLETON = """private lemma erdos_242_residual_mod_seven_eq_one_case_k_eq_1 :
    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧
      (4 / ((24 * 1 + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by
  sorry"""

ERDOS_PARAMETERIZED_RESIDUE = """private lemma erdos_242_residual_denominator_positive
    (k : Nat) (hk : 1 ≤ k) (hmod : k % 7 = 1) :
    0 < 24 * k + 1 := by
  sorry"""


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
                {
                    "name": "invented_bound",
                    "statement": ("lemma invented_bound (n : ℕ) (hn : n ≥ 6) : True := by sorry"),
                },
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
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda _active_file, _target_symbol: {"ok": False},
    )
    events: list[tuple] = []
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: events.append((a, k)))

    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "goal",
            "active_file": "Demo.lean",
            "slice": "theorem goal : True ∧ True := by sorry",
        },
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


def test_decompose_suppressed_for_kernel_clean_assignment(rigged, monkeypatch):
    active = rigged / "Demo.lean"
    active.write_text("theorem goal : True := by\n  trivial\n", encoding="utf-8")
    route = runner.orchestrator_floor.OrchestratorRoute(
        route="decompose",
        reason="split the goal",
        target={
            "statements_to_state": [
                {"name": "goal_helper", "statement": "lemma goal_helper : True := by sorry"}
            ]
        },
        source="llm",
    )
    placed_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner.decomposer,
        "place_helpers",
        lambda **kwargs: placed_calls.append(kwargs),
    )
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda _active_file, target_symbol: {"ok": True, "target": target_symbol},
    )
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    history: list[dict[str, str]] = []
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {
            "target_symbol": "goal",
            "active_file": str(active),
        },
        "current_queue_assignment": {
            "target_symbol": "goal",
            "active_file": str(active),
        },
    }

    action = runner._orchestrator_apply_route(
        route,
        history,
        autonomy_state,
        {"target_symbol": "goal", "active_file": str(active)},
        agent=None,
    )

    assert action == "continue"
    assert placed_calls == []
    assert any(args[0] == "decomposer-clean-target-suppressed" for args, _kwargs in events)
    assert any("CLEAN-TARGET RECONCILIATION" in entry["content"] for entry in history)


def test_decompose_skips_repeated_timed_out_clean_target_check(rigged, monkeypatch):
    """Do not replay a timed-out parent check before structural decomposition."""
    active = rigged / "Demo.lean"
    route = runner.orchestrator_floor.OrchestratorRoute(
        route="decompose",
        reason="repeated verification timeouts require structural recovery",
        target={
            "statements_to_state": [
                {"name": "goal_helper", "statement": "lemma goal_helper : True := by sorry"}
            ]
        },
        source="llm",
    )
    placed_calls: list[dict[str, Any]] = []

    def fake_place(**kwargs):
        placed_calls.append(kwargs)
        from leanflow_cli.workflows.decomposer import DecomposeOutcome

        return DecomposeOutcome(ok=True, placed=("goal_helper",), file=kwargs["active_file"])

    monkeypatch.setattr(runner.decomposer, "place_helpers", fake_place)
    monkeypatch.setattr(runner.decomposer, "refresh_queue_edit_guard", lambda _agent: None)
    monkeypatch.setattr(
        runner,
        "_restored_assignment_verification_timeout_reason",
        lambda *_args, **_kwargs: "LeanProbe timed out after 300 seconds",
    )
    monkeypatch.setattr(
        runner,
        "_manager_incremental_check_queue_item",
        lambda *_args, **_kwargs: pytest.fail("timed-out parent check must not be replayed"),
    )
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    autonomy_state: dict[str, Any] = {
        "_orchestrator_last_ctx": {
            "target_symbol": "goal",
            "active_file": str(active),
        },
        "current_queue_assignment": {
            "target_symbol": "goal",
            "active_file": str(active),
            "slice": "theorem goal : True ∧ True := by sorry",
        },
    }

    action = runner._orchestrator_apply_route(
        route,
        [],
        autonomy_state,
        {"target_symbol": "goal", "active_file": str(active)},
        agent=None,
    )

    assert action == "continue"
    assert len(placed_calls) == 1
    assert any(args[0] == "decomposer-clean-target-check-backpressured" for args, _kwargs in events)


def test_llm_decompose_rejects_exact_erdos_singleton_before_placement(rigged, monkeypatch):
    """The live route-statements door cannot state one closed parent instance."""
    decision = json.dumps(
        {
            "route": "decompose",
            "reason": "try k = 1 first",
            "statements_to_state": [
                {
                    "name": "erdos_242_residual_mod_seven_eq_one_case_k_eq_1",
                    "statement": ERDOS_CLOSED_SINGLETON,
                }
            ],
        }
    )
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **kwargs: SimpleNamespace(response=decision, status="ok"),
    )
    placed_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner.decomposer,
        "place_helpers",
        lambda **kwargs: placed_calls.append(kwargs),
    )
    mechanical_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner.decomposer,
        "run_decomposer",
        lambda **kwargs: mechanical_calls.append(kwargs)
        or runner.decomposer.DecomposeOutcome(
            ok=False,
            reason="no ready, guarded helpers to insert",
        ),
    )
    journal: list[dict[str, Any]] = []
    monkeypatch.setattr(runner.plan_state, "append_journal_event", journal.append)
    activities: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        runner,
        "_record_activity",
        lambda *args, **kwargs: activities.append((args, kwargs)),
    )

    active_file = str((rigged / "Demo.lean").resolve())
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "erdos_242_residual_mod_seven_eq_one",
            "active_file": active_file,
            "slice": ERDOS_PARENT,
        },
        "continuation_stable_cycles": 4,
    }
    live_state = {
        "target_symbol": "erdos_242_residual_mod_seven_eq_one",
        "active_file": active_file,
        "declaration_queue": [],
    }

    route = runner._orchestrator_consult("stall", autonomy_state, live_state)
    assert route is not None and route.source == "llm" and route.route == "decompose"
    action = runner._orchestrator_apply_route(
        route,
        [],
        autonomy_state,
        live_state,
        agent=None,
    )

    assert action == "continue"
    assert placed_calls == []
    assert len(mechanical_calls) == 1
    rejected = [
        event
        for event in journal
        if event.get("event") == "decomposer-instantiated-parent-rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0]["instantiated_parameters"] == [{"name": "k", "literal": "1"}]
    assert any(
        args and args[0] == "decomposer-instantiated-parent-rejected"
        for args, _kwargs in activities
    )


def test_llm_decompose_allows_reusable_parameterized_erdos_residue(rigged, monkeypatch):
    """A distinct reusable residue subfamily still reaches guarded placement."""
    decision = json.dumps(
        {
            "route": "decompose",
            "reason": "split by the finer residue parameter",
            "statements_to_state": [
                {
                    "name": "erdos_242_residual_denominator_positive",
                    "statement": ERDOS_PARAMETERIZED_RESIDUE,
                }
            ],
        }
    )
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **kwargs: SimpleNamespace(response=decision, status="ok"),
    )
    placed_calls: list[dict[str, Any]] = []

    def fake_place(**kwargs):
        placed_calls.append(kwargs)
        return runner.decomposer.DecomposeOutcome(
            ok=True,
            placed=("erdos_242_residual_denominator_positive",),
            file=kwargs["active_file"],
        )

    monkeypatch.setattr(runner.decomposer, "place_helpers", fake_place)
    monkeypatch.setattr(runner.decomposer, "refresh_queue_edit_guard", lambda _agent: None)
    monkeypatch.setattr(runner, "_record_activity", lambda *_args, **_kwargs: None)

    active_file = str((rigged / "Demo.lean").resolve())
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "erdos_242_residual_mod_seven_eq_one",
            "active_file": active_file,
            "slice": ERDOS_PARENT,
        },
        "continuation_stable_cycles": 4,
    }
    live_state = {
        "target_symbol": "erdos_242_residual_mod_seven_eq_one",
        "active_file": active_file,
        "declaration_queue": [],
    }

    route = runner._orchestrator_consult("stall", autonomy_state, live_state)
    assert route is not None and route.route == "decompose"
    action = runner._orchestrator_apply_route(
        route,
        [],
        autonomy_state,
        live_state,
        agent=None,
    )

    assert action == "continue"
    assert len(placed_calls) == 1
    assert placed_calls[0]["skeletons"] == [ERDOS_PARAMETERIZED_RESIDUE]


def test_llm_decompose_without_statements_blocks_immediate_duplicate_advisor_call(
    rigged, monkeypatch
):
    """Mechanical fallback evidence replaces an identical foreground request."""
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
        or runner.decomposer.DecomposeOutcome(
            ok=False,
            reason="no ready, guarded helpers to insert",
            skipped=("candidate_one",),
            obstacle_summary="the terminal residue family remains uncovered",
            recommended_split="derive a factor-pair certificate for the first residual class",
            first_concrete_next_edit=(
                "prove and check the quotient-normalization helper with omega"
            ),
        ),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {"target_symbol": "goal", "active_file": "Demo.lean"}
    }
    live_state = {"target_symbol": "goal", "active_file": "Demo.lean", "declaration_queue": []}
    route = runner._orchestrator_consult("stall", autonomy_state, live_state)
    assert route is not None and route.route == "decompose"

    history: list[dict[str, Any]] = []
    action = runner._orchestrator_apply_route(
        route, history, autonomy_state, live_state, agent=None
    )

    assert action == "continue"
    assert len(mechanical) == 1
    directive = history[-1]["content"]
    assert "mechanical action already completed" in directive
    assert "terminal residue family remains uncovered" in directive
    assert "derive a factor-pair certificate" in directive
    assert "first checked edit" in directive
    assert "prove and check the quotient-normalization helper with omega" in directive
    assert "do not call `lean_decompose_helpers` again" in directive
    assert "Call `lean_decompose_helpers` now" not in directive

    agent = SimpleNamespace(_managed_autonomy_state=autonomy_state)
    duplicate_args = {"theorem_id": "goal", "file_path": "Demo.lean"}
    for _attempt in range(2):
        blocked = runner._managed_pre_tool_call(
            agent,
            "lean_decompose_helpers",
            duplicate_args,
        )
        assert blocked is not None
        payload = json.loads(blocked)
        assert payload["status"] == "duplicate_mechanical_decomposition_blocked"
        assert payload["obstacle_summary"] == "the terminal residue family remains uncovered"
    assert len(mechanical) == 1

    # A real source revision makes a later decomposition request distinct.
    (rigged / "Demo.lean").write_text(FILE_TEXT + "\n-- new proof evidence\n", encoding="utf-8")
    assert runner._managed_pre_tool_call(agent, "lean_decompose_helpers", duplicate_args) is None
    # And the graph gate chain was never touched: no proved nodes appeared.
    assert all(node.status != "proved" for node in plan_state.load_blueprint().nodes)


def test_llm_stub_exception_reconciles_source_before_fallback(rigged, monkeypatch):
    """An unexpected guarded-door crash cannot bypass a durable source pause."""
    decision = json.dumps(
        {
            "route": "decompose",
            "reason": "state one helper",
            "statements_to_state": [
                {
                    "name": "prime_seven",
                    "statement": "lemma prime_seven : Nat.Prime 7 := by sorry",
                }
            ],
        }
    )
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **_kwargs: SimpleNamespace(response=decision, status="ok"),
    )
    monkeypatch.setattr(
        runner.decomposer,
        "place_helpers",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("door crashed")),
    )
    monkeypatch.setattr(
        runner.decomposer,
        "run_decomposer",
        lambda **_kwargs: pytest.fail("source quarantine must stop before mechanical fallback"),
    )

    def reconcile(state):
        state["operational_pause"] = "paused_source_quarantine"
        return {"active": 1}

    monkeypatch.setattr(runner, "_reconcile_source_transaction_state", reconcile)
    monkeypatch.setattr(runner, "_record_activity", lambda *_args, **_kwargs: None)
    state = {
        "current_queue_assignment": {
            "target_symbol": "goal",
            "active_file": "Demo.lean",
            "slice": "theorem goal : True := by sorry",
        },
        "continuation_stable_cycles": 4,
    }
    live = {"target_symbol": "goal", "active_file": "Demo.lean"}
    route = runner._orchestrator_consult("stall", state, live)
    assert route is not None

    assert runner._orchestrator_apply_route(route, [], state, live, agent=None) == (
        "stop:source-quarantine"
    )


def test_mechanical_decomposer_exception_reconciles_source_before_fallback(rigged, monkeypatch):
    """The mechanical route also checks source transactions after a crash."""
    decision = json.dumps({"route": "decompose", "reason": "split it"})
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **_kwargs: SimpleNamespace(response=decision, status="ok"),
    )
    monkeypatch.setattr(
        runner.decomposer,
        "run_decomposer",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("mechanical crash")),
    )

    def reconcile(state):
        state["operational_pause"] = "paused_source_quarantine"
        return {"active": 1}

    monkeypatch.setattr(runner, "_reconcile_source_transaction_state", reconcile)
    monkeypatch.setattr(runner, "_record_activity", lambda *_args, **_kwargs: None)
    state = {"current_queue_assignment": {"target_symbol": "goal", "active_file": "Demo.lean"}}
    live = {"target_symbol": "goal", "active_file": "Demo.lean"}
    route = runner._orchestrator_consult("stall", state, live)
    assert route is not None

    assert runner._orchestrator_apply_route(route, [], state, live, agent=None) == (
        "stop:source-quarantine"
    )


def test_llm_stub_exception_pauses_infrastructure_when_source_is_clean(rigged, monkeypatch):
    decision = json.dumps(
        {
            "route": "decompose",
            "reason": "state one helper",
            "statements_to_state": [
                {
                    "name": "prime_seven",
                    "statement": "lemma prime_seven : Nat.Prime 7 := by sorry",
                }
            ],
        }
    )
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **_kwargs: SimpleNamespace(response=decision, status="ok"),
    )
    monkeypatch.setattr(
        runner.decomposer,
        "place_helpers",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("door crashed")),
    )
    monkeypatch.setattr(
        runner.decomposer,
        "run_decomposer",
        lambda **_kwargs: pytest.fail("unexpected exceptions must not fall through"),
    )
    monkeypatch.setattr(
        runner,
        "_reconcile_source_transaction_state",
        lambda _state: {"active": 0},
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner.campaign_epoch, "record_status", lambda *_args, **_kwargs: None)
    state = {
        "current_queue_assignment": {
            "target_symbol": "goal",
            "active_file": "Demo.lean",
            "slice": "theorem goal : True := by sorry",
        },
        "continuation_stable_cycles": 4,
    }
    live = {"target_symbol": "goal", "active_file": "Demo.lean"}
    route = runner._orchestrator_consult("stall", state, live)
    assert route is not None

    assert runner._orchestrator_apply_route(route, [], state, live, agent=None) == (
        "stop:infrastructure-pause"
    )
    assert state["operational_pause"] == "paused_infrastructure"
    assert "door crashed" in state["infrastructure_pause_reason"]


def test_mechanical_exception_pauses_infrastructure_when_source_is_clean(rigged, monkeypatch):
    decision = json.dumps({"route": "decompose", "reason": "split it"})
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **_kwargs: SimpleNamespace(response=decision, status="ok"),
    )
    monkeypatch.setattr(
        runner.decomposer,
        "run_decomposer",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("mechanical crash")),
    )
    monkeypatch.setattr(
        runner,
        "_reconcile_source_transaction_state",
        lambda _state: {"active": 0},
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner.campaign_epoch, "record_status", lambda *_args, **_kwargs: None)
    state = {"current_queue_assignment": {"target_symbol": "goal", "active_file": "Demo.lean"}}
    live = {"target_symbol": "goal", "active_file": "Demo.lean"}
    route = runner._orchestrator_consult("stall", state, live)
    assert route is not None

    assert runner._orchestrator_apply_route(route, [], state, live, agent=None) == (
        "stop:infrastructure-pause"
    )
    assert state["operational_pause"] == "paused_infrastructure"
    assert "mechanical crash" in state["infrastructure_pause_reason"]


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

    real_place = runner.decomposer.place_helpers

    def tracked_place(**kwargs):
        placed_calls.append(kwargs)
        return real_place(**kwargs)

    monkeypatch.setattr(runner.decomposer, "place_helpers", tracked_place)
    monkeypatch.setattr(
        "leanflow_cli.lean.lean_incremental.lean_incremental_check",
        lambda **_kwargs: {"success": True, "has_errors": False},
    )
    monkeypatch.setattr(runner.decomposer, "refresh_queue_edit_guard", lambda agent: None)
    monkeypatch.setattr(runner, "_record_activity", lambda *a, **k: None)

    active_file = str((rigged / "Demo.lean").resolve())
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "goal",
            "active_file": active_file,
            "slice": "theorem goal : Nat.Prime 7 ∧ 2 + 2 = 4 := by sorry",
        },
        "continuation_stable_cycles": 4,
    }
    live_state = {"target_symbol": "goal", "active_file": active_file, "declaration_queue": []}

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


def test_research_park_refreshes_campaign_instead_of_stopping(rigged, monkeypatch):
    """Route exhaustion in research mode requests a fresh campaign epoch."""
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

    history: list[dict[str, Any]] = []
    action = runner._orchestrator_apply_route(
        route, history, autonomy_state, live_state, agent=None
    )

    assert action == "continue"
    assert autonomy_state["campaign_epoch_requested"] == "route-portfolio-exhausted"
    assert "RELENTLESS ROUTE REFRESH" in history[-1]["content"]
    packets = plan_state.load_summary().get("decision_packets") or []
    assert not any(packet.get("decision") == "park" for packet in packets)


def test_research_park_does_not_depend_on_packet_persistence(rigged, monkeypatch):
    """A plan-state write failure cannot turn research route exhaustion into a stop."""
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

    assert action == "continue"
    assert autonomy_state["campaign_epoch_requested"] == "route-portfolio-exhausted"
    packets = plan_state.load_summary().get("decision_packets") or []
    assert not any(p.get("decision") == "park" for p in packets)  # nothing dangling
