"""The orchestrator chooses how a blocked obligation recovers; one global budget pays."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from leanflow_cli.workflows.prover import negation_job, recovery
from leanflow_cli.workflows.prover.config import ProverConfig
from leanflow_cli.workflows.prover.job_controller import job_budget
from leanflow_cli.workflows.prover.models import Node
from leanflow_cli.workflows.prover.recovery import parse_decision, parse_screen
from leanflow_cli.workflows.prover.runtime import ProverRuntime


class Verifier:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted

    def check(self, node: Node, file: Path, **kwargs: Any) -> dict[str, Any]:
        return {"accepted": self.accepted}

    def compile_module(self, relative: str) -> dict[str, Any]:
        return {"accepted": True}

    def final(self, files: list[Path]) -> dict[str, Any]:
        return {"accepted": self.accepted}


def session(**kwargs: Any) -> dict[str, Any]:
    return {"status": "candidate", "api_calls": 1, "final_response": json.dumps({"notes": "n"})}


def research_runtime(tmp_path: Path, **overrides: Any) -> ProverRuntime:
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    path = tmp_path / "Main.lean"
    path.write_text("import Std\n\ntheorem goal : True := by sorry\n")
    config = ProverConfig(mode="research", job_api_calls=3, **overrides)
    return ProverRuntime(
        root=tmp_path, targets=[path], config=config, session=session, verifier=Verifier()
    )


def scripted(actions: list[str]):
    """A stand-in orchestrator that answers with a fixed sequence of decisions."""
    calls: list[dict[str, Any]] = []
    queue = list(actions)

    def decide(runtime: ProverRuntime, node: Node, report: Any) -> dict[str, Any]:
        calls.append(dict(report))
        action = queue.pop(0)
        return {"action": action, "rationale": f"scripted {action}", "fallback": False}

    return decide, calls


# --- parsing -----------------------------------------------------------------


@pytest.mark.parametrize("action", ["retry", "negate", "decompose"])
def test_decision_accepts_each_action(action: str) -> None:
    decision = parse_decision(json.dumps({"action": action.upper(), "rationale": "why"}))
    assert decision == {"action": action, "rationale": "why", "fallback": False}


@pytest.mark.parametrize("text", ["", "not json", '{"action": "give_up"}', '{"rationale": "x"}'])
def test_unusable_decision_falls_back_to_decompose_and_says_so(text: str) -> None:
    decision = parse_decision(text)
    assert decision["action"] == "decompose"
    assert decision["fallback"] is True


def test_screen_reads_plausible_verdicts() -> None:
    found = parse_screen({"messages": [{"message": "Found a counter-example!\nn := 3"}]})
    assert found["found"] is True and "n := 3" in found["detail"]
    empty = parse_screen({"messages": [{"message": "Unable to find a counter-example"}]})
    assert empty["found"] is False
    broken = parse_screen({"messages": [{"message": "failed to synthesize Testable ..."}]})
    assert broken["found"] is None
    assert parse_screen({"success": False, "error": "timeout"})["found"] is None


# --- budgets -----------------------------------------------------------------


def test_negation_has_its_own_budget_defaulting_to_sixty() -> None:
    config = ProverConfig(model="m", job_api_calls=250, orchestrator_api_calls=100)
    assert config.negation_api_calls == 60
    assert job_budget(config, "prover") == 250
    assert job_budget(config, "negation") == 60
    assert job_budget(config, "orchestrator") == 100
    assert job_budget(config, "research") == 100
    env = ProverConfig.from_env(
        {"LEANFLOW_PROVER_MODEL": "m", "LEANFLOW_PROVER_NEGATION_API_CALLS": "40"}
    )
    assert env.negation_api_calls == 40
    with pytest.raises(ValueError, match="must be positive"):
        ProverConfig(model="m", negation_api_calls=0)


# --- the recovery loop -------------------------------------------------------


def test_retry_decision_spends_one_unit_and_reopens_the_node(tmp_path: Path, monkeypatch) -> None:
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    decide, calls = scripted(["retry"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)

    runtime._recover(node, {"notes": "nearly there", "progress": True})

    assert node.status == "retry"
    assert runtime.state["metrics"]["decompositions"] == 1
    assert calls[0]["progress"] is True
    assert runtime.state["recovery_decisions"][0]["action"] == "retry"
    assert "Recovery decision: retry" in node.notes


def test_unrefuted_negation_returns_to_the_orchestrator_and_spends_again(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    decide, calls = scripted(["negate", "retry"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)
    monkeypatch.setattr(
        recovery, "empirical_screen", lambda rt, n: {"found": False, "detail": "none"}
    )
    negations: list[dict[str, Any]] = []

    def negate(rt: ProverRuntime, n: Node, *, screen: Any = None) -> dict[str, Any]:
        negations.append({"screen": screen})
        return {"certified": False, "notes": "no witness"}

    monkeypatch.setattr(negation_job, "attempt_negation", negate)

    runtime._recover(node, {"notes": "stuck"})

    assert negations == [{"screen": {"found": False, "detail": "none"}}]
    # The second decision saw the failed refutation.
    assert calls[1]["negations_attempted"] == 1
    assert calls[1]["last_negation"]["notes"] == "no witness"
    assert runtime.state["metrics"]["decompositions"] == 2
    assert node.status == "retry"


def test_exhausted_global_budget_leaves_the_node_blocked_with_a_note(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = research_runtime(tmp_path, max_decompositions=1)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    decide, calls = scripted(["negate", "retry"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)
    monkeypatch.setattr(recovery, "empirical_screen", lambda rt, n: {"found": None, "detail": ""})
    monkeypatch.setattr(
        negation_job, "attempt_negation", lambda rt, n, *, screen=None: {"certified": False}
    )

    runtime._recover(node, {"notes": "stuck"})

    assert len(calls) == 1  # the second decision was never bought
    assert node.status == "blocked"
    assert "Campaign recovery budget exhausted (1/1)" in node.notes


def test_certified_negation_of_the_root_records_a_disproof_and_stops(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    decide, _ = scripted(["negate"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)
    monkeypatch.setattr(recovery, "empirical_screen", lambda rt, n: {"found": True, "detail": "x"})
    monkeypatch.setattr(
        negation_job,
        "attempt_negation",
        lambda rt, n, *, screen=None: {"certified": True, "evidence_path": "checks/neg.lean"},
    )

    runtime._recover(node, {"notes": "stuck"})

    assert node.status == "false"
    assert runtime.state["disproof"]["node_id"] == node.id
    assert runtime.stopping is True and runtime.cancelled.is_set()


def test_decompose_decision_replans_and_reopens(tmp_path: Path, monkeypatch) -> None:
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    decide, _ = scripted(["decompose"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)
    reasons: list[str] = []

    def replan(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        reasons.append(reason)
        return True

    monkeypatch.setattr(runtime, "_research_plan", replan)

    runtime._recover(node, {"notes": "too big"})

    assert node.status == "retry"
    assert "Recovery decision: decompose" in reasons[0]
    assert runtime.state["metrics"]["decompositions"] == 1


def test_rejected_replan_ends_the_round_with_the_node_blocked(tmp_path: Path, monkeypatch) -> None:
    """When planner and reviewer decline every proposal, stop spending on this branch."""
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    decide, calls = scripted(["decompose", "retry"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)
    monkeypatch.setattr(
        runtime, "_research_plan", lambda reason, *, affected=None, refinement=False: False
    )

    runtime._recover(node, {"notes": "too big"})

    assert len(calls) == 1  # no second decision was bought
    assert node.status == "blocked"
    assert "Replanning was rejected" in node.notes
    assert runtime.state["metrics"]["decompositions"] == 1


def test_standard_mode_blocks_without_ever_consulting_the_orchestrator(
    tmp_path: Path, monkeypatch
) -> None:
    """Standard mode has no orchestrator: a non-proving run blocks via the legacy path."""

    def never(*args: Any, **kwargs: Any):
        raise AssertionError("recovery_decision must not run in standard mode")

    monkeypatch.setattr(recovery, "recovery_decision", never)
    (tmp_path / "lakefile.toml").write_text('name = "Demo"\n[[lean_lib]]\nname = "Main"\n')
    path = tmp_path / "Main.lean"
    path.write_text("import Std\n\ntheorem goal : True := by sorry\n")

    def stuck(**kwargs: Any) -> dict[str, Any]:
        return {
            "status": "budget_exhausted",
            "api_calls": 1,
            "final_response": json.dumps({"notes": "no"}),
        }

    runtime = ProverRuntime(
        root=tmp_path,
        targets=[path],
        config=ProverConfig(mode="standard", job_api_calls=1),
        session=stuck,
        verifier=Verifier(),
    )
    state = runtime.run()
    assert state["status"] == "blocked"
    assert state["dag"]["nodes"][0]["status"] == "blocked"
    assert state["metrics"]["decompositions"] == 0


def test_certified_negation_of_a_NON_root_node_replans_without_stopping(
    tmp_path: Path, monkeypatch
) -> None:
    """A refuted helper is marked false and its branch replanned; the run continues."""
    runtime = research_runtime(tmp_path)
    root = runtime.dag.nodes[0]
    helper = Node("h", "helper", "theorem helper : True", "Main.lean", "Main", holes=[0])
    root.dependencies = ["h"]
    runtime.dag.nodes.append(helper)
    helper.status = "blocked"
    decide, _ = scripted(["negate"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)
    monkeypatch.setattr(recovery, "empirical_screen", lambda rt, n: {"found": True, "detail": "x"})
    monkeypatch.setattr(
        negation_job,
        "attempt_negation",
        lambda rt, n, *, screen=None: {"certified": True, "evidence_path": "checks/h.lean"},
    )
    replanned: list[Any] = []
    monkeypatch.setattr(
        runtime,
        "_research_plan",
        lambda reason, *, affected=None, refinement=False: replanned.append(affected) or True,
    )

    runtime._recover(helper, {"notes": "stuck"})

    assert helper.status == "false"
    assert runtime.stopping is False and not runtime.cancelled.is_set()
    assert replanned  # the affected branch was replanned
    assert "disproof" not in runtime.state


def test_garbage_orchestrator_reply_falls_back_to_decompose_and_dispatches_it(
    tmp_path: Path, monkeypatch
) -> None:
    """An unparseable decision must still dispatch -- through the real decision path."""
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    monkeypatch.setattr(
        runtime, "_run_role", lambda *a, **k: {"final_response": "sorry, no JSON here"}
    )
    replans: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_research_plan",
        lambda reason, *, affected=None, refinement=False: replans.append(reason) or True,
    )

    runtime._recover(node, {"notes": "stuck"})

    decision = runtime.state["recovery_decisions"][0]
    assert decision["action"] == "decompose"
    assert decision["fallback"] is True
    assert replans and node.status == "retry"


def _resume(runtime: ProverRuntime, **overrides: Any) -> ProverRuntime:
    """Reload a runtime from its persisted store, as a restart would."""
    cfg = runtime.config
    return ProverRuntime(
        root=runtime.root,
        targets=list(runtime.targets),
        config=cfg,
        session=session,
        verifier=Verifier(),
        run_id=runtime.run_id,
        resume=True,
        **overrides,
    )


def test_interrupted_decompose_replays_the_same_action_after_a_real_reload(
    tmp_path: Path, monkeypatch
) -> None:
    """P1: a decompose interrupted mid-replan is REPLAYED on resume, not re-decided.

    Exercises the real persistence: the decision is charged and recorded once,
    survives a fresh ProverRuntime restore, and the resumed run neither calls
    recovery_decision again nor charges a second unit.
    """
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    monkeypatch.setattr(
        recovery,
        "recovery_decision",
        lambda rt, n, rep: {"action": "decompose", "rationale": "split it", "fallback": False},
    )

    def replan_dies(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        # Real research_plan persists its checkpoint before doing work; emulate
        # that so the resume can tell "interrupted mid-plan" from "committed".
        runtime.state["planning_request"] = {
            "reason": reason,
            "affected": None,
            "refinement": refinement,
            "steps": {},
        }
        runtime._persist()
        raise recovery_runtime_error()

    monkeypatch.setattr(runtime, "_research_plan", replan_dies)
    with pytest.raises(Exception):
        runtime._recover(node, {"notes": "too big"})

    # The decide stage charged once, recorded once, and advanced to the plan
    # stage with the decision persisted.
    assert runtime.state["metrics"]["decompositions"] == 1
    assert len(runtime.state["recovery_decisions"]) == 1
    rec = runtime.state["recovery_in_flight"][node.id]
    assert rec["stage"] == "plan" and rec["decision"]["action"] == "decompose"
    assert rec["charged"] is True and rec["plan_started"] is True

    # Reload from disk and resume: the plan stage replays (planning_request is
    # present -> mid-plan), the decision is NOT remade, no second charge.
    resumed = _resume(runtime)
    resumed_node = resumed.dag.by_id()[node.id]
    assert node.id in resumed.state["recovery_in_flight"]

    def must_not_decide(*a: Any, **k: Any):
        raise AssertionError("recovery_decision must not run when replaying a persisted stage")

    monkeypatch.setattr(recovery, "recovery_decision", must_not_decide)
    reasons: list[str] = []

    def finish_plan(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        reasons.append(reason)
        resumed.state.pop("planning_request", None)  # a real resume consumes it
        return True

    monkeypatch.setattr(resumed, "_research_plan", finish_plan)
    resumed._recover(resumed_node)

    assert reasons and "decompose" in reasons[0]  # the persisted reason drove the replan
    assert resumed.state["metrics"]["decompositions"] == 1  # not recharged
    assert len(resumed.state["recovery_decisions"]) == 1  # not re-recorded
    assert node.id not in resumed.state["recovery_in_flight"]
    assert resumed_node.status == "retry"


def recovery_runtime_error() -> Exception:
    from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

    return InfrastructureFailure("provider outage", status="provider_error")


def test_promote_candidates_routes_rejections_through_recovery_and_survives_a_crash(
    tmp_path: Path, monkeypatch
) -> None:
    """P2/P3: rejected candidates are enqueued via the real _promote_candidates path.

    Both rejected nodes are persisted into recovery_in_flight before any recovery
    runs, so a crash during the first leaves the second recoverable on resume.
    """
    runtime = research_runtime(tmp_path)
    root = runtime.dag.nodes[0]
    # Two independent candidate nodes whose (empty) dependencies are satisfied.
    a = Node("a", "a", "theorem a : True", "Main.lean", "Main", holes=[0], status="candidate")
    b = Node("b", "b", "theorem b : True", "Main.lean", "Main", holes=[0], status="candidate")
    a.candidate = ["trivial"]
    b.candidate = ["trivial"]
    runtime.dag.nodes.extend([a, b])
    root.status = "proved"  # keep the promotion loop focused on a and b
    monkeypatch.setattr(runtime, "_accept", lambda node, cand, who: False)

    calls: list[str] = []

    def decide_first_dies(rt: ProverRuntime, n: Node, rep: Any) -> dict[str, Any]:
        calls.append(n.id)
        raise recovery_runtime_error()

    monkeypatch.setattr(recovery, "recovery_decision", decide_first_dies)
    with pytest.raises(Exception):
        runtime._promote_candidates()

    # Both were enqueued before any recovery ran; the second survives the crash.
    assert calls == ["a"]
    assert "a" in runtime.state["recovery_in_flight"]
    assert "b" in runtime.state["recovery_in_flight"]


def test_screen_reads_the_runner_output_field_not_messages() -> None:
    """The real runner returns diagnostics in `output` and leaves `messages` empty."""
    found = parse_screen(
        {"success": False, "output": "Found a counter-example!\nn := 3", "messages": []}
    )
    assert found["found"] is True and "n := 3" in found["detail"]
    empty = parse_screen(
        {"success": False, "output": "Unable to find a counter-example", "messages": []}
    )
    assert empty["found"] is False
    assert (
        parse_screen({"success": False, "output": "unsolved goals", "messages": []})["found"]
        is None
    )


def test_empirical_screen_never_raises_out_of_recovery(tmp_path: Path, monkeypatch) -> None:
    """P2: any screen failure -- bad runner shape, or a raising runner -- is inconclusive."""
    from leanflow_cli.lean import lean_ephemeral
    from leanflow_cli.workflows.prover import negation as negation_mod

    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]

    class FakeGoal:
        prop = "True"

    monkeypatch.setattr(
        recovery,
        "negation_context",
        lambda root, n: negation_mod.NegationContext(prefix="", suffix="", goal=FakeGoal()),
    )

    # A malformed runner result (messages=None) must not raise through parse_screen.
    monkeypatch.setattr(
        lean_ephemeral,
        "lean_ephemeral_source_check",
        lambda *a, **k: {"messages": None, "output": None},
    )
    assert recovery.empirical_screen(runtime, node)["found"] is None

    # A runner that raises an unexpected error is swallowed as inconclusive.
    def boom(*a: Any, **k: Any):
        raise TypeError("unexpected runner failure")

    monkeypatch.setattr(lean_ephemeral, "lean_ephemeral_source_check", boom)
    assert recovery.empirical_screen(runtime, node)["found"] is None


def test_negation_reuses_a_cached_proof_after_an_interrupted_verification(
    tmp_path: Path, monkeypatch
) -> None:
    """P2: a crash during verification re-verifies the cached proof, no new model job."""
    from leanflow_cli.workflows.prover import negation_job
    from leanflow_cli.workflows.prover.negation import NegationTask

    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]

    task = NegationTask(
        node=Node("neg", "neg", "theorem neg : False", node.file, node.module, holes=[0]),
        source="import Std\ntheorem neg : False := by sorry\n",
    )
    monkeypatch.setattr(negation_job, "prepare_negation", lambda root, n: task)

    model_jobs = {"n": 0}

    def model(**kwargs: Any) -> dict[str, Any]:
        model_jobs["n"] += 1
        return {
            "status": "completed",
            "api_calls": 1,
            "final_response": json.dumps({"proof": "exact h"}),
        }

    # Prime the cache as an interrupted verification would have left it.
    runtime.state.setdefault("negation_proofs", {})[node.id] = "exact h"
    monkeypatch.setattr(runtime, "session", model, raising=False)
    checked: list[str] = []
    monkeypatch.setattr(
        runtime.verifier, "check", lambda n, path, **k: checked.append(n.id) or {"accepted": False}
    )

    out = negation_job.attempt_negation(runtime, node)

    assert model_jobs["n"] == 0  # no new negation model job was bought
    assert checked == [task.node.id]  # it re-verified the cached proof
    assert out["certified"] is False
    # attempt_negation NO LONGER clears the cache -- the recovery state machine
    # does, atomically with recording the outcome and advancing the stage.
    assert runtime.state["negation_proofs"][node.id] == "exact h"


def test_negation_records_its_outcome_before_finishing_the_job(tmp_path: Path, monkeypatch) -> None:
    """P1: a completed negation job's outcome is cached atomically with finishing it.

    A finished negation job is not re-queued for resume, so if the proof were
    cached only after finish_job a crash in between would re-invoke the model
    from scratch. Assert the cache already holds the outcome when finish_job runs
    -- for a proof AND for a no-proof completion (empty marker, so resume does
    not re-run a completed model).
    """
    from leanflow_cli.workflows.prover import negation_job
    from leanflow_cli.workflows.prover.negation import NegationTask

    for final_response, expected in (
        (json.dumps({"proof": "exact h"}), "exact h"),
        (json.dumps({"notes": "no witness"}), ""),
    ):
        runtime = research_runtime(tmp_path)
        node = runtime.dag.nodes[0]
        task = NegationTask(
            node=Node("neg", "neg", "theorem neg : False", node.file, node.module, holes=[0]),
            source="import Std\ntheorem neg : False := by sorry\n",
        )
        monkeypatch.setattr(negation_job, "prepare_negation", lambda root, n: task)
        monkeypatch.setattr(
            runtime,
            "session",
            lambda **k: {"status": "completed", "api_calls": 1, "final_response": final_response},
            raising=False,
        )
        monkeypatch.setattr(
            runtime.verifier, "check", lambda n, path, **k: {"accepted": False}, raising=False
        )
        cached_at_finish: dict[str, Any] = {}
        real_finish = runtime._finish_job

        def spy_finish(job: dict[str, Any], result: dict[str, Any]) -> None:
            cached_at_finish["value"] = runtime.state.get("negation_proofs", {}).get(node.id)
            real_finish(job, result)

        monkeypatch.setattr(runtime, "_finish_job", spy_finish)

        negation_job.attempt_negation(runtime, node, screen=None)

        assert cached_at_finish["value"] == expected  # recorded before finishing


def test_interrupted_plan_seed_replays_instead_of_reading_a_stale_status(
    tmp_path: Path, monkeypatch
) -> None:
    """P1: a plan interrupted before research_plan commits its checkpoint replays.

    The plan stage seeds research_plan's planning_request together with
    plan_started in one persist. If a crash lands before research_plan commits
    its own checkpoint, resume still sees the seeded checkpoint and re-enters
    planning -- rather than misreading `planning_request is None` as completion
    and skipping the plan by trusting a stale proposal_status.
    """
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    monkeypatch.setattr(
        recovery,
        "recovery_decision",
        lambda rt, n, rep: {"action": "decompose", "rationale": "split", "fallback": False},
    )

    def plan_dies(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        raise recovery_runtime_error()  # crash before research_plan commits anything

    monkeypatch.setattr(runtime, "_research_plan", plan_dies)
    with pytest.raises(Exception):
        runtime._recover(node, {"notes": "too big"})

    # The seed is on disk even though research_plan never ran.
    rec = runtime.state["recovery_in_flight"][node.id]
    assert rec["stage"] == "plan" and rec["plan_started"] is True
    assert runtime.state["planning_request"] is not None
    # A stale accepted status from some earlier plan must not be mistaken for
    # this plan's completion.
    runtime.state["proposal_status"] = "accepted"
    runtime._persist()

    resumed = _resume(runtime)
    resumed_node = resumed.dag.by_id()[node.id]
    ran: list[str] = []

    def finish_plan(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        ran.append(reason)
        resumed.state.pop("planning_request", None)  # a real resume consumes it
        return True

    monkeypatch.setattr(resumed, "_research_plan", finish_plan)
    resumed._recover(resumed_node)

    assert ran  # planning re-entered on resume rather than being skipped
    assert node.id not in resumed.state["recovery_in_flight"]
    assert resumed_node.status == "retry"


def test_resume_during_recovery_planning_does_not_re_run_the_design_plan(
    tmp_path: Path, monkeypatch
) -> None:
    """A mid-recovery-planning resume must not spuriously re-run the initial design.

    A recovery decompose sets phase="planning", so resume_phase alone would
    re-trigger the top-level design plan. The durable design_plan_complete marker
    must suppress that; only the recovery's own replan should run.
    """
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    # Persist the exact on-disk state a crash mid-recovery-planning would leave.
    runtime.state["design_plan_complete"] = True
    runtime.state["phase"] = "planning"
    runtime.state["recovery_in_flight"] = {
        node.id: {
            "report": {"notes": "stuck"},
            "charged": True,
            "decision": {"action": "decompose", "rationale": "split", "fallback": False},
            "reason": "Repair only the branch for goal.",
        }
    }
    runtime.state["metrics"]["decompositions"] = 1
    runtime._persist()

    resumed = _resume(runtime)
    reasons: list[str] = []

    def record_plan(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        reasons.append(reason)
        rn = resumed.dag.by_id().get(node.id)
        if rn is not None:
            rn.status = "retry"
        return True

    monkeypatch.setattr(resumed, "_research_plan", record_plan)
    resumed.run()

    # The point of this test: the initial design plan must not run again on a
    # mid-recovery-planning resume. Only the recovery's own replan should appear.
    assert not any("Inspect available sources" in r for r in reasons), reasons
    assert any("Repair only the branch" in r for r in reasons)  # recovery replan did run


def test_certified_helper_refinement_replan_resumes_after_interruption(
    tmp_path: Path, monkeypatch
) -> None:
    """P1: a certified helper negation whose refinement replan was interrupted resumes.

    The negation certified (node is 'false'), the journal advanced to the 'plan'
    stage with refinement=True, and the replan was interrupted. The drain must
    re-enter the node via that persisted stage and finish the replan -- without
    re-charging or re-running the negation.
    """
    runtime = research_runtime(tmp_path)
    root = runtime.dag.nodes[0]
    helper = Node("h", "h", "theorem h : True", "Main.lean", "Main", holes=[0])
    root.dependencies = ["h"]
    runtime.dag.nodes.append(helper)
    helper.status = "blocked"
    decide, _ = scripted(["negate"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)
    monkeypatch.setattr(recovery, "empirical_screen", lambda rt, n: {"found": True, "detail": "x"})
    monkeypatch.setattr(
        negation_job,
        "attempt_negation",
        lambda rt, n, *, screen=None: {"certified": True, "evidence_path": "checks/h.lean"},
    )

    def replan_dies(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        # Real research_plan persists its checkpoint before doing work; emulate
        # that so the resume can tell "interrupted mid-plan" from "committed".
        runtime.state["planning_request"] = {
            "reason": reason,
            "affected": None,
            "refinement": refinement,
            "steps": {},
        }
        runtime._persist()
        raise recovery_runtime_error()

    monkeypatch.setattr(runtime, "_research_plan", replan_dies)
    with pytest.raises(Exception):
        runtime._recover(helper, {"notes": "stuck"})

    # Interrupted during the refinement replan: the negation certified (node is
    # 'false'), the journal is at the plan stage with refinement, and the plan
    # was started before the crash.
    assert helper.status == "false"
    rec = runtime.state["recovery_in_flight"]["h"]
    assert rec["stage"] == "plan" and rec["refinement"] is True
    assert rec["plan_started"] is True and rec["reason"]
    assert "h" not in runtime.state.get("negation_proofs", {})  # proof cache released at certify
    charged = runtime.state["metrics"]["decompositions"]

    # Resume as the drain does: re-enter every journalled node whatever its
    # status. The plan stage replays (planning_request present -> mid-plan) and
    # finishes without re-charging or re-running the negation.
    def negate_must_not_run(*a: Any, **k: Any):
        raise AssertionError("negation must not re-run when resuming a certified refinement")

    monkeypatch.setattr(negation_job, "attempt_negation", negate_must_not_run)
    reasons: list[str] = []

    def finish_plan(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        reasons.append(reason)
        runtime.state.pop("planning_request", None)  # a real resume consumes it
        return True

    monkeypatch.setattr(runtime, "_research_plan", finish_plan)
    for node_id in list(runtime.state["recovery_in_flight"]):
        n = runtime.dag.by_id().get(node_id)
        if n is not None:
            runtime._recover(n)

    assert reasons and "Repair only the branch" in reasons[0]
    assert runtime.state["metrics"]["decompositions"] == charged  # not recharged
    assert "h" not in runtime.state["recovery_in_flight"]  # dismissed after the replan


def test_resume_finishes_a_planning_checkpoint_whose_node_was_pruned(
    tmp_path: Path, monkeypatch
) -> None:
    """P2: a refinement that pruned the refuted helper must not strand its planning.

    On resume the pruned node is gone from the DAG, so the drain cannot re-enter
    it; the orphaned recovery planning checkpoint must still be resumed and the
    dead recovery_in_flight entry cleaned.
    """
    runtime = research_runtime(tmp_path)
    # 'h' is NOT in the DAG (pruned), but left a certified recovery entry and a
    # committed planning checkpoint behind, as an interrupted refinement would.
    runtime.state["design_plan_complete"] = True
    runtime.state["phase"] = "resume"
    runtime.state["resume_phase"] = "proving"
    runtime.state["recovery_in_flight"] = {
        "h": {
            "report": {},
            "charged": True,
            "decision": {"action": "negate"},
            "certified": True,
            "reason": "Repair only the branch for h.",
            "affected": [],
        }
    }
    runtime.state["planning_request"] = {
        "reason": "Repair only the branch for h.",
        "affected": [],
        "refinement": True,
        "steps": {},
    }
    runtime._persist()

    resumed = _resume(runtime)
    resumed_reasons: list[str] = []

    def record_plan(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        resumed_reasons.append(reason)
        resumed.state.pop("planning_request", None)  # a real resume consumes it
        return True

    monkeypatch.setattr(resumed, "_research_plan", record_plan)
    resumed.run()

    assert resumed_reasons  # the orphaned checkpoint was resumed
    assert "h" not in resumed.state.get("recovery_in_flight", {})  # dead entry cleaned
    assert not any("Inspect available sources" in r for r in resumed_reasons)  # not the design plan


def test_recovery_decision_replays_a_completed_turn_without_a_second_allocation(
    tmp_path: Path, monkeypatch
) -> None:
    """P1: a finished orchestrator decision is replayed, not re-bought, on resume.

    A completed role job is not re-queued for resume, so if a crash lands after
    the orchestrator turn finishes but before the decide stage records it, the
    cached reply in the journal must replay the SAME decision instead of buying
    a second orchestrator allocation.
    """
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    runtime._enqueue_recovery(node, {"notes": "stuck"})

    turns = {"n": 0}

    def session(**kwargs: Any) -> dict[str, Any]:
        turns["n"] += 1
        return {
            "status": "completed",
            "api_calls": 1,
            "final_response": json.dumps({"action": "retry", "rationale": "near miss"}),
        }

    monkeypatch.setattr(runtime, "session", session, raising=False)

    first = recovery.recovery_decision(runtime, node, {"notes": "stuck"})
    assert first["action"] == "retry" and turns["n"] == 1
    rec = runtime.state["recovery_in_flight"][node.id]
    assert isinstance(rec.get("decision_result"), str) and "retry" in rec["decision_result"]

    # A crash before the decide stage recorded the decision: re-entering
    # recovery_decision replays the cached turn -- no second orchestrator turn.
    second = recovery.recovery_decision(runtime, node, {"notes": "stuck"})
    assert second["action"] == "retry"
    assert turns["n"] == 1  # the completed turn was NOT re-invoked


def test_rejected_negation_proof_records_a_no_proof_marker(tmp_path: Path, monkeypatch) -> None:
    """P1: a completed attempt whose proof is rejected records "", not an empty cache.

    validate_hole_replacement rejects a replacement that smuggles in a command.
    Clearing the cache on rejection would let a crash re-buy a whole negation
    allocation to redo the finished attempt; the no-proof marker prevents that.
    """
    from leanflow_cli.workflows.prover import negation_job
    from leanflow_cli.workflows.prover.negation import NegationTask

    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    task = NegationTask(
        node=Node("neg", "neg", "theorem neg : False", node.file, node.module, holes=[0]),
        source="import Std\ntheorem neg : False := by sorry\n",
    )
    monkeypatch.setattr(negation_job, "prepare_negation", lambda root, n: task)
    turns = {"n": 0}

    def session(**kwargs: Any) -> dict[str, Any]:
        turns["n"] += 1
        # Nonempty and sorry-free (so "usable"), but validate rejects the smuggled
        # command, exercising the rejection branch of _certify_negation.
        return {
            "status": "completed",
            "api_calls": 1,
            "final_response": json.dumps({"proof": "exact h\ntheorem extra : True := trivial"}),
        }

    monkeypatch.setattr(runtime, "session", session, raising=False)

    out = negation_job.attempt_negation(runtime, node, screen=None)
    assert out["certified"] is False and turns["n"] == 1
    assert runtime.state["negation_proofs"][node.id] == ""  # marker, not a cleared cache

    # Resume before the stage advanced: the marker short-circuits, no re-invoke.
    again = negation_job.attempt_negation(runtime, node, screen=None)
    assert again["certified"] is False and turns["n"] == 1


def test_orphaned_checkpoint_does_not_hijack_another_nodes_recovery_plan(
    tmp_path: Path, monkeypatch
) -> None:
    """P2: a pruned node's stale checkpoint must not become another node's plan.

    planning_request is a single global slot. If node A is pruned mid-plan and
    leaves its checkpoint behind, node B's plan stage must NOT inherit it (which
    would run A's plan for B). The resume clears the orphan before draining, so
    B seeds and runs its OWN checkpoint.
    """
    runtime = research_runtime(tmp_path)
    live = runtime.dag.nodes[0]
    live.status = "blocked"
    runtime.state["design_plan_complete"] = True
    runtime.state["phase"] = "resume"
    runtime.state["resume_phase"] = "proving"
    # A is pruned (absent from the DAG) but left its checkpoint in the slot.
    runtime.state["planning_request"] = {
        "reason": "A's pruned branch",
        "affected": [],
        "refinement": True,
        "steps": {},
    }
    runtime.state["recovery_in_flight"] = {
        "A": {
            "stage": "plan",
            "plan_started": True,
            "refinement": True,
            "reason": "A's pruned branch",
            "affected": [],
            "charged": True,
            "decision": {"action": "decompose"},
        },
        live.id: {
            "stage": "plan",
            "plan_started": False,
            "refinement": True,
            "reason": "B's own branch",
            "affected": [],
            "charged": True,
            "decision": {"action": "decompose"},
        },
    }
    runtime._persist()

    resumed = _resume(runtime)
    plans: list[str] = []

    def record_plan(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        plans.append((resumed.state.get("planning_request") or {}).get("reason"))
        resumed.state.pop("planning_request", None)  # a real resume consumes it
        return True

    monkeypatch.setattr(resumed, "_research_plan", record_plan)
    resumed.run()

    # B ran a checkpoint carrying ITS OWN reason, not A's orphan.
    assert "B's own branch" in plans
    assert live.id not in resumed.state.get("recovery_in_flight", {})


def test_interrupted_negation_does_not_advance_the_recovery_stage(
    tmp_path: Path, monkeypatch
) -> None:
    """P1: a user interruption during NEGATE must not be read as "no refutation".

    finish_job only sets cancelled+stopping for an interrupted session (it does
    NOT raise), so without a cancellation check the negate stage would persist a
    spurious unrefuted advance (stage=decide, charged=False). Resume would then
    re-charge and re-decide, abandoning the still-resumable negation job. The
    stage must stay "negate" with the unit still charged.
    """
    from leanflow_cli.workflows.prover import negation_job
    from leanflow_cli.workflows.prover.negation import NegationTask

    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "blocked"
    decide, _ = scripted(["negate"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)
    monkeypatch.setattr(recovery, "empirical_screen", lambda rt, n: {"found": None, "detail": ""})
    task = NegationTask(
        node=Node("neg", "neg", "theorem neg : False", node.file, node.module, holes=[0]),
        source="import Std\ntheorem neg : False := by sorry\n",
    )
    monkeypatch.setattr(negation_job, "prepare_negation", lambda root, n: task)
    monkeypatch.setattr(
        runtime,
        "session",
        lambda **k: {"status": "interrupted", "api_calls": 1, "final_response": ""},
        raising=False,
    )

    with pytest.raises(Exception):
        runtime._recover(node, {"notes": "stuck"})

    rec = runtime.state["recovery_in_flight"][node.id]
    assert rec["stage"] == "negate"  # NOT advanced to decide
    assert rec["charged"] is True  # the unit stays charged
    assert runtime.state["metrics"]["decompositions"] == 1  # charged exactly once


def test_planning_call_does_not_replay_an_interrupted_step(tmp_path: Path, monkeypatch) -> None:
    """P1: a cancelled planning step's partial result must not be replayed as done.

    An interrupted (or source-conflicted) planning job left a partial result.json;
    replaying it as a completed planning report would corrupt the plan. The step
    must be re-run instead.
    """
    from leanflow_cli.workflows.prover.planning_controller import _planning_call

    runtime = research_runtime(tmp_path)
    ws = tmp_path / "jobws"
    ws.mkdir()
    (ws / "result.json").write_text(
        json.dumps({"status": "interrupted", "final_response": "STALE PARTIAL"})
    )
    runtime.state["jobs"].append(
        {"id": "j1", "status": "interrupted", "workspace": str(ws), "role": "orchestrator"}
    )
    checkpoint: dict[str, Any] = {
        "steps": {"outline": "j1"},
        "reason": "r",
        "refinement": False,
        "affected": None,
    }
    monkeypatch.setattr(
        runtime,
        "session",
        lambda **k: {"status": "completed", "api_calls": 1, "final_response": "FRESH"},
        raising=False,
    )

    out = _planning_call(runtime, checkpoint, "outline", "orchestrator", "prompt")

    assert out["final_response"] == "FRESH"  # re-ran; the interrupted partial was not replayed


def test_plan_completion_is_read_node_scoped_not_from_shared_status(
    tmp_path: Path, monkeypatch
) -> None:
    """P2: a completed plan's outcome is read from ITS journal, not proposal_status.

    B's plan completed (accepted) but crashed before dismissal. On resume A
    replans and is rejected, overwriting the shared proposal_status. B's
    'completed' branch must read the accepted outcome recorded on its own
    journal and reopen the node, not inherit A's rejection.
    """
    runtime = research_runtime(tmp_path)
    node_a = runtime.dag.nodes[0]
    node_a.status = "blocked"
    node_b = Node("b", "b", "theorem b : True", node_a.file, node_a.module, holes=[0])
    node_b.status = "blocked"
    runtime.dag.nodes.append(node_b)

    # B already completed its (accepted) plan -- outcome recorded on B's journal,
    # checkpoint popped -- but crashed before dismissal. A is still to replan.
    runtime.state["recovery_in_flight"] = {
        node_a.id: {
            "stage": "plan",
            "plan_started": False,
            "refinement": False,
            "affected": [node_a.id],
            "reason": "A repair",
            "charged": True,
            "decision": {"action": "decompose"},
        },
        node_b.id: {
            "stage": "plan",
            "plan_started": True,
            "refinement": False,
            "affected": [node_b.id],
            "reason": "B repair",
            "charged": True,
            "decision": {"action": "decompose"},
            "applied": True,
        },
    }
    runtime.state.pop("planning_request", None)

    def reject(reason: str, *, affected: Any = None, refinement: bool = False) -> bool:
        runtime.state.pop("planning_request", None)
        runtime.state["proposal_status"] = "rejected"  # A's rejection on the shared slot
        return False

    monkeypatch.setattr(runtime, "_research_plan", reject)
    runtime._recover(node_a)
    runtime._recover(node_b)

    assert node_a.status == "blocked"  # A really was rejected
    assert node_b.status == "retry"  # B read its OWN accepted outcome, not A's rejection
    assert node_b.id not in runtime.state["recovery_in_flight"]


def test_negation_infra_check_failure_preserves_proof_instead_of_advancing(
    tmp_path: Path, monkeypatch
) -> None:
    """P1: a verification that could not COMPLETE is not read as an unrefuted negation.

    If the independent check times out / is cancelled, the proof stays cached and
    an InfrastructureFailure propagates so resume re-verifies for free -- rather
    than the negate stage treating certified=False as "no refutation", dropping
    the proof and re-deciding.
    """
    from leanflow_cli.workflows.prover import negation_job
    from leanflow_cli.workflows.prover.negation import NegationTask
    from leanflow_cli.workflows.prover.runtime import InfrastructureFailure

    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    task = NegationTask(
        node=Node("neg", "neg", "theorem neg : False", node.file, node.module, holes=[0]),
        source="import Std\ntheorem neg : False := by sorry\n",
    )
    monkeypatch.setattr(negation_job, "prepare_negation", lambda root, n: task)
    runtime.state.setdefault("negation_proofs", {})[node.id] = "exact h"  # cached proof
    monkeypatch.setattr(
        runtime.verifier,
        "check",
        lambda n, path, **k: {"accepted": False, "error_code": "check_timeout"},
        raising=False,
    )

    with pytest.raises(InfrastructureFailure):
        negation_job.attempt_negation(runtime, node)

    assert runtime.state["negation_proofs"][node.id] == "exact h"  # preserved for re-verify


def test_committed_design_checkpoint_marks_completion_on_finish(tmp_path: Path) -> None:
    """P2: finishing a committed initial-design checkpoint sets design_plan_complete.

    The design marker is stamped atomically with popping the checkpoint, so an
    interrupted-but-committed design (marker unset, phase already 'proving') is
    finished on resume rather than stranded past every guard.
    """
    from leanflow_cli.workflows.prover import planning_controller

    runtime = research_runtime(tmp_path)
    runtime.state.pop("design_plan_complete", None)
    runtime.state["planning_request"] = {
        "reason": "design",
        "affected": None,
        "refinement": False,
        "steps": {},
        "design": True,
        "accepted_proposal": {"research_jobs": []},
    }

    assert planning_controller.research_plan(runtime, "resume the design") is True
    assert runtime.state.get("design_plan_complete") is True
    assert not runtime.state.get("planning_request")


def test_resume_finishes_a_committed_design_checkpoint(tmp_path: Path) -> None:
    """P2: resume must not skip a committed-but-unmarked initial design checkpoint."""
    runtime = research_runtime(tmp_path)
    runtime.state["phase"] = "resume"
    runtime.state["resume_phase"] = "proving"
    runtime.state.pop("design_plan_complete", None)  # marker never got set (the bug precondition)
    runtime.state["planning_request"] = {
        "reason": "design",
        "affected": None,
        "refinement": False,
        "steps": {},
        "design": True,
        "accepted_proposal": {"research_jobs": []},
    }
    runtime._persist()

    resumed = _resume(runtime)
    resumed.run()

    # If the committed design had been stranded, both the pre-drain finish (marker
    # unset) and the design guard (phase already "proving") would skip it and the
    # marker would stay False. Its being True proves the resume finished it.
    # (The full run then proceeds into proving/recovery, which may seed further
    # checkpoints -- not this test's concern.)
    assert resumed.state.get("design_plan_complete") is True


def test_retry_decision_marks_the_job_processed_so_resume_keeps_the_retry(
    tmp_path: Path, monkeypatch
) -> None:
    """P1: a persisted retry survives resume; the rejected candidate is not resurrected.

    The prover produced a candidate the gate rejected, recovery chose retry. If
    the triggering job were left unprocessed, _restore would resurrect that
    rejected candidate and overwrite the authorized 'retry'. Marking the job
    processed atomically with the retry keeps resume from clobbering it.
    """
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    node.status = "running"
    monkeypatch.setattr(
        runtime.verifier, "check", lambda n, path, **k: {"accepted": False}, raising=False
    )
    decide, _ = scripted(["retry"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)
    job, _ = runtime._new_job("prover", node=node)

    runtime._handle_result(
        job,
        {"status": "completed", "api_calls": 1, "final_response": json.dumps({"proof": "trivial"})},
    )

    assert node.status == "retry"
    assert job["result_processed"] is True  # marked atomically with the retry
    assert node.id not in runtime.state["recovery_in_flight"]  # dismissed after retry
    runtime._persist()

    # Resume: the rejected candidate must NOT be resurrected over the retry.
    resumed = _resume(runtime)
    assert resumed.dag.by_id()[node.id].status == "retry"


def test_promotion_rejection_marks_the_prover_job_so_resume_keeps_the_retry(
    tmp_path: Path, monkeypatch
) -> None:
    """P1: a candidate rejected during PROMOTION (not _handle_result) is not resurrected.

    A retained candidate is promoted, the gate rejects it, and recovery chooses
    retry. The originating prover job -- retained across an earlier resume, never
    seen by _handle_result -- must be marked processed as recovery opens, so the
    next resume cannot resurrect the rejected candidate over the 'retry'.
    """
    runtime = research_runtime(tmp_path)
    node = runtime.dag.nodes[0]
    job, _ = runtime._new_job("prover", node=node)
    runtime._finish_job(
        job,
        {"status": "completed", "api_calls": 1, "final_response": json.dumps({"proof": "trivial"})},
    )
    # As a resume's _retain_candidate would leave it: candidate on the node, the
    # originating job accounted but NOT yet result_processed.
    node.candidate = ["trivial"]
    node.status = "candidate"
    job.pop("result_processed", None)
    monkeypatch.setattr(
        runtime.verifier, "check", lambda n, path, **k: {"accepted": False}, raising=False
    )
    decide, _ = scripted(["retry"])
    monkeypatch.setattr(recovery, "recovery_decision", decide)

    runtime._promote_candidates()

    assert node.status == "retry"
    assert job["result_processed"] is True  # marked as recovery opened
    assert node.id not in runtime.state["recovery_in_flight"]
    runtime._persist()

    resumed = _resume(runtime)
    assert resumed.dag.by_id()[node.id].status == "retry"  # not resurrected as a candidate


@pytest.mark.parametrize("max_restarts", [0, 3])
def test_completed_failure_before_journal_opens_still_recovers_on_resume(
    tmp_path: Path, max_restarts: int
) -> None:
    """P1: a prover failure that crashed before recovery opened is recovered on resume.

    In research mode EVERY finished prover attempt with no candidate routes to
    the orchestrator (recovery), whatever the restart allowance. If the crash
    lands between _finish_job and opening the journal, _restore's legacy rule
    would either mark an over-restart node 'blocked' with no journal (stranded)
    or a within-allowance node 'retry' (a free prover allocation with no
    orchestrator decision). Resume must open recovery in both cases.
    """
    runtime = research_runtime(tmp_path, max_restarts=max_restarts)
    node = runtime.dag.nodes[0]
    node.status = "running"
    node.attempts = 1  # 'blocked' when max_restarts=0, 'retry' when max_restarts=3
    job, _ = runtime._new_job("prover", node=node)
    # The attempt finished with no usable candidate; _finish_job persisted the
    # terminal job, but the crash landed before _handle_result opened recovery
    # (result_processed never set, no journal).
    runtime._finish_job(
        job,
        {
            "status": "completed",
            "api_calls": 1,
            "final_response": json.dumps({"notes": "no proof"}),
        },
    )
    assert node.id not in runtime.state.get("recovery_in_flight", {})
    runtime._persist()

    resumed = _resume(runtime)

    # Recovery was opened for the node, so the drain will consult the orchestrator
    # instead of leaving it permanently blocked.
    assert node.id in resumed.state["recovery_in_flight"]
