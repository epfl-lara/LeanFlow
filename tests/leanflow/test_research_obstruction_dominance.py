"""Verified universal-obstruction research-portfolio policy tests."""

from __future__ import annotations

import pytest

from leanflow_cli.workflows import (
    dispatch_service,
    plan_state,
    research_obstruction_dominance,
    research_portfolio,
)

TARGET = """private lemma demo (t : ℕ) :
  let n := 840 * t + 361
  let q := n % 3
  q = 0 := by
  sorry"""
UNIVERSAL = """private lemma demo_impossible (t : ℕ) :
  ¬ (let n := 840 * t + 361
     let q := n % 3
     q = 0) := by
  omega"""


def _blueprint(
    active_file: str,
    *,
    helper_statement: str = UNIVERSAL,
    helper_status: str = "proved",
    evidence_edge: bool = True,
) -> plan_state.Blueprint:
    """Return one exact target plus a candidate obstruction helper."""
    target_id = plan_state.node_id_for("demo", active_file)
    helper_id = plan_state.node_id_for("demo_impossible", active_file)
    return plan_state.Blueprint(
        nodes=(
            plan_state.GraphNode(
                id=target_id,
                name="demo",
                file=active_file,
                statement=TARGET,
                status="conjectured",
            ),
            plan_state.GraphNode(
                id=helper_id,
                name="demo_impossible",
                file=active_file,
                statement=helper_statement,
                status=helper_status,
            ),
        ),
        edges=(
            ((plan_state.GraphEdge(source=helper_id, target=target_id, kind="evidence")),)
            if evidence_edge
            else ()
        ),
    )


def test_exact_parent_verified_universal_obstruction_dominates_finite_research(tmp_path):
    active_file = str(tmp_path / "Main.lean")
    evidence = research_obstruction_dominance.exact_target_universal_obstruction(
        _blueprint(active_file),
        target_symbol="demo",
        active_file=active_file,
    )

    assert evidence is not None
    assert evidence.name == "demo_impossible"
    assert research_obstruction_dominance.dominated_finite_instance_objective(
        archetype="empirical",
        route_key="history-refresh:old-routes",
        objective="Compute the next uncovered instance t = 11.",
    )
    assert not research_obstruction_dominance.dominated_finite_instance_objective(
        archetype="decomposition",
        route_key="alternate-decomposition",
        objective="Find a different proof shape after promotion fails.",
    )


@pytest.mark.parametrize(
    ("helper_statement", "helper_status", "evidence_edge"),
    [
        (
            "private lemma demo_impossible : ¬ (let n := 840 * 0 + 361; "
            "let q := n % 3; q = 0) := by omega",
            "proved",
            True,
        ),
        (
            "private lemma demo_impossible (t : ℕ) : ¬ ((840 * t + 361) % 5 = 0) := by omega",
            "proved",
            True,
        ),
        (UNIVERSAL, "conjectured", True),
        (UNIVERSAL, "proved", False),
    ],
)
def test_finite_unrelated_or_unverified_negative_helper_cannot_dominate(
    tmp_path,
    helper_statement,
    helper_status,
    evidence_edge,
):
    active_file = str(tmp_path / "Main.lean")

    assert (
        research_obstruction_dominance.exact_target_universal_obstruction(
            _blueprint(
                active_file,
                helper_statement=helper_statement,
                helper_status=helper_status,
                evidence_edge=evidence_edge,
            ),
            target_symbol="demo",
            active_file=active_file,
        )
        is None
    )


def test_dominance_rotates_capacity_to_negation_and_distinct_proof_shape():
    assert research_portfolio._desired_archetypes(
        0,
        1,
        universal_obstruction=True,
    ) == ["negation_probe"]
    assert research_portfolio._desired_archetypes(
        0,
        2,
        universal_obstruction=True,
    ) == ["negation_probe", "decomposition"]
    assert research_portfolio._desired_archetypes(
        4,
        3,
        universal_obstruction=True,
    ) == ["negation_probe", "decomposition", "deep_search"]


def test_job_spec_rejects_dominated_finite_objective(tmp_path, monkeypatch):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    active_file = str(tmp_path / "Main.lean")
    obstruction = research_obstruction_dominance.exact_target_universal_obstruction(
        _blueprint(active_file),
        target_symbol="demo",
        active_file=active_file,
    )
    assert obstruction is not None
    service = dispatch_service.DispatchService(root_job_id="campaign-demo")

    with pytest.raises(ValueError, match="dominated"):
        research_portfolio._job_spec(
            service,
            archetype="empirical",
            generation=1,
            target_symbol="demo",
            active_file=active_file,
            attempt_count=2,
            route_key="boundary-counterexample-probe",
            route_focus="probe boundary cases and assumptions for counterexamples",
            universal_obstruction=obstruction,
        )


def test_portfolio_retires_finite_worker_and_refills_non_dominated_lanes(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_DISPATCH_ENABLED", "1")
    active_file = str(tmp_path / "Main.lean")
    blueprint = _blueprint(active_file)
    monkeypatch.setattr(plan_state, "load_blueprint", lambda: blueprint)

    def fake_deploy_async(self, job_id):
        self._transition(job_id, "deployed")
        return self._transition(
            job_id,
            "running",
            started_at=dispatch_service._now_iso(),
        )

    monkeypatch.setattr(
        dispatch_service.DispatchService,
        "deploy_async",
        fake_deploy_async,
    )

    service = dispatch_service.DispatchService(root_job_id="campaign-demo", cap=2)
    finite = research_portfolio._job_spec(
        service,
        archetype="empirical",
        generation=1,
        target_symbol="demo",
        active_file=active_file,
        attempt_count=2,
        route_key="boundary-counterexample-probe",
        route_focus="probe boundary cases and assumptions for counterexamples",
    )
    service.propose(finite)
    service._transition(finite.job_id, "deployed")

    status = research_portfolio.maintain_portfolio(
        campaign_id="campaign-demo",
        target_symbol="demo",
        active_file=active_file,
        attempt_count=2,
        workers=2,
    )

    persisted = dispatch_service.DispatchService(root_job_id="campaign-demo", cap=2)
    assert persisted._entry(finite.job_id).state == "killed"
    assert status["universal_obstruction_dominance"]["retired_jobs"] == [finite.job_id]
    assert status["universal_obstruction_dominance"]["suppressed_archetypes"] == ["empirical"]
    launched = [persisted._entry(job_id) for job_id in status["launched"]]
    assert {entry.spec.archetype for entry in launched} == {
        "negation_probe",
        "decomposition",
    }
    by_archetype = {entry.spec.archetype: entry for entry in launched}
    negation = by_archetype["negation_probe"]
    decomposition = by_archetype["decomposition"]
    assert negation.spec.inputs["route_key"] == "universal-obstruction-promotion"
    assert "exact closed-negation bridge" in negation.spec.objective
    assert "do not test another finite instance" in negation.spec.objective
    assert decomposition.spec.inputs["route_key"] == "universal-obstruction-replan"
    assert "materially different proof shape" in decomposition.spec.objective
