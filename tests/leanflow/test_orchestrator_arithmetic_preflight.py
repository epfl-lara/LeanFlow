"""Characterize deterministic arithmetic checks on advisory routes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import orchestrator_llm
from leanflow_cli.workflows.orchestrator import OrchestratorRoute, RouteContext
from leanflow_cli.workflows.orchestrator_arithmetic_preflight import (
    ARITHMETIC_PREFLIGHT_REJECTION_PREFIX,
    preflight_route_decision,
)

OBSERVED_ERDOS_242_RESPONSE = r"""```json
{
  "route": "decompose",
  "reason": "The target reduces to n = 168*t + 1 (t ≥ 1) with two branches already closed (t % 5 = 3 and t % 13 = 1). The remaining branch (t % 5 ≠ 3 ∧ t % 13 ≠ 1) is the Mordell residual class 169 mod 840 — the hardest case of the Erdős–Straus conjecture. Computational evidence confirms witnesses exist for all tested t, but no uniform parametric identity has been found. The key unexplored decomposition is a sub-helper that further splits the complement by t mod 7: when 7 | t, the denominator 168*t+1 = 7*(24*t+1) has a non-1-mod-24 factor, enabling erdos_242_of_nonresidual_factor. This covers 1/7 of the remaining cases and is provable with existing infrastructure. The residual after this split (t % 5 ≠ 3, t % 13 ≠ 1, t % 7 ≠ 0) is a strictly smaller family that can be attacked with further modular sub-helpers or direct witnesses.",
  "target_node": "erdos_242_residual_mod_seven_eq_zero",
  "statements_to_state": [
    {
      "name": "erdos_242_residual_mod_seven_eq_zero_of_mod_seven_eq_zero",
      "file": "/private/tmp/leanflow-open-erdos242/FormalConjectures/ErdosProblems/242.lean",
      "statement": "private lemma erdos_242_residual_mod_seven_eq_zero_of_mod_seven_eq_zero (t : ℕ) (ht : 1 ≤ t) (hmod : t % 7 = 0) : ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧ (4 / ((168 * t + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by\n  -- When 7 | t, write t = 7*s, so 168*t+1 = 1176*s+1 = 7*(168*s) + 1.\n  -- But 168*t+1 = 168*(7*s)+1 = 7*(24*(7*s)) + 1, which is NOT divisible by 7.\n  -- Instead use: 168*t+1 with t=7*s gives n = 1176*s+1. Need a different factor.\n  -- Actually: 168*t + 1 where t ≡ 0 mod 7: n = 168*7*s + 1 = 1176*s + 1.\n  -- Check if n has a small prime factor depending on s mod p.\n  sorry"
    }
  ],
  "probes": [
    {
      "archetype": "empirical",
      "objective": "For the complement family t ≥ 1, t % 5 ≠ 3, t % 13 ≠ 1, systematically search for which small primes p (7, 11, 17, 19, 23, 29, 31) divide 168*t+1 as a function of t mod p. For each such prime p, record the residue class r = t mod p that makes p | (168*t+1), and verify p % 24 ≠ 1 (so erdos_242_of_nonresidual_factor applies). The goal is to find a finite set of (prime, residue) pairs that covers all t in the complement, or at least covers a large fraction, reducing the residual family further. Also check: for t not covered by any such prime factor, what are the witness patterns (x, y, z) — do they follow a parametric formula in t?"
    }
  ]
}
```"""


def _decision(reason: str) -> dict[str, Any]:
    return {
        "route": "decompose",
        "reason": reason,
        "target_node": "erdos_242_residual_mod_seven_eq_zero",
        "statements_to_state": [],
        "probes": [],
    }


def test_rejects_observed_false_factor_transfer_identity() -> None:
    """Regression: k=7*t does not make 24*k+1 a multiple of seven."""
    reason = (
        "The target requires a witness for every k = 7*t (t ≥ 1), i.e., n = 168*t + 1. "
        "State a helper that (1) obtains t, (2) proves 24*k+1 = 7*(24*t+1) by ring, "
        "and (3) invokes the nonresidual-factor theorem."
    )

    report = preflight_route_decision(_decision(reason))

    assert report.accepted is False
    assert report.evidence() == (
        {
            "kind": "affine-identity",
            "claim": "24*k+1=7*(24*t+1)",
            "evidence": "normalized affine forms differ: 168*t+1 != 168*t+7",
        },
    )


def test_rejects_observed_false_divisibility_branch() -> None:
    """Regression: 168*t+1 is one modulo seven, including t=0 mod 7."""
    reason = (
        "For n = 168*t+1, either 7 | n (use the nonresidual factor route), "
        "i.e. when t ≡ 0 mod 7, or branch on t mod 5 / t mod 7."
    )

    report = preflight_route_decision(_decision(reason))

    assert report.accepted is False
    issue = report.evidence()[0]
    assert issue["kind"] == "affine-divisibility"
    assert issue["claim"] == "7|n"
    assert issue["evidence"] == ("after substitutions, 168*t+1 mod 7 is 1 at t=0")


def test_refutes_affine_divisibility_on_a_stated_residue_class() -> None:
    report = preflight_route_decision(
        _decision("When t ≡ 1 mod 3, claim 5 | (6*t+4) and close the branch.")
    )

    assert report.accepted is False
    # The representative t=1 happens to work, but the next value in the same
    # residue class refutes the claimed universal branch.
    assert "at t=4" in report.evidence()[0]["evidence"]


def test_accepts_valid_affine_controls_without_claiming_general_verification() -> None:
    report = preflight_route_decision(
        _decision(
            "Use the identity 24*(7*t)+1 = 168*t+1. When t ≡ 2 mod 5, "
            "5 | (3*t+4), and universally 7 | (168*t+7)."
        )
    )

    assert report.accepted is True
    assert report.evidence() == ()


def test_possible_divisibility_case_split_fails_open_without_a_residue_claim() -> None:
    report = preflight_route_decision(
        _decision("Split on the branch 5 | (6*t+4), then handle its complement separately.")
    )

    assert report.accepted is True


def test_affine_hypotheses_are_not_misclassified_as_asserted_identities() -> None:
    identity_hypothesis = preflight_route_decision(
        _decision("Under the assumption 2*t = 3*t, derive t = 0.")
    )
    divisibility_hypothesis = preflight_route_decision(
        _decision("Let n = 168*t+1. If 7 | n, apply the conditional factor lemma.")
    )

    assert identity_hypothesis.accepted is True
    assert divisibility_hypothesis.accepted is True


def test_ambiguous_multiple_residue_hypotheses_fail_open() -> None:
    report = preflight_route_decision(
        _decision("When t ≡ 0 mod 2 and t ≡ 1 mod 2, claim 7 | (168*t+1).")
    )

    assert report.accepted is True


def test_fails_open_on_nonlinear_and_explicitly_speculative_math() -> None:
    nonlinear = preflight_route_decision(_decision("Use x*x = n after deriving a square witness."))
    speculative = preflight_route_decision(
        _decision("Probe whether 7 | (168*t+1); do not treat it as established.")
    )

    assert nonlinear.accepted is True
    assert speculative.accepted is True


def test_modulo_hypothesis_is_not_misparsed_as_an_affine_identity() -> None:
    report = preflight_route_decision(
        {
            **_decision("State the exact residue helper."),
            "statements_to_state": [
                {
                    "name": "residue_helper",
                    "statement": "lemma residue_helper (t : ℕ) (h : t % 7 = 0) : True := by sorry",
                }
            ],
        }
    )

    assert report.accepted is True


def test_llm_route_rejects_false_arithmetic_and_keeps_floor(monkeypatch) -> None:
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", "1")
    response = json.dumps(
        _decision("For n = 168*t+1, either 7 | n, i.e. when t ≡ 0 mod 7, " "or use another branch.")
    )
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **_kwargs: SimpleNamespace(response=response, status="ok"),
    )
    floor = OrchestratorRoute(route="direct-prove", reason="deterministic passthrough")
    ctx = RouteContext(
        trigger="scope-entry",
        target_symbol="erdos_242_residual_mod_seven_eq_zero",
        active_file="FormalConjectures/ErdosProblems/242.lean",
    )

    route, note = orchestrator_llm.llm_route(ctx, floor)

    assert route is None
    assert note.startswith(ARITHMETIC_PREFLIGHT_REJECTION_PREFIX)
    assert "168*t+1 mod 7 is 1" in note


def test_llm_route_preflights_full_observed_reason_before_bounding(monkeypatch) -> None:
    """Replay the live route whose false middle claim vanished when bounded."""
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", "1")
    bounded = orchestrator_llm.parse_llm_decision(OBSERVED_ERDOS_242_RESPONSE)
    assert bounded is not None
    assert len(bounded["reason"]) == 500
    assert "168*t+1 = 7*(24*t+1)" not in bounded["reason"]
    # The proposed statement corrects the false rationale, so checking only
    # the bounded decision reproduces the live acceptance hole.
    assert preflight_route_decision(bounded).accepted is True
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **_kwargs: SimpleNamespace(
            response=OBSERVED_ERDOS_242_RESPONSE,
            status="ok",
        ),
    )
    floor = OrchestratorRoute(route="direct-prove", reason="deterministic passthrough")
    ctx = RouteContext(
        trigger="completed-job",
        target_symbol="erdos_242_residual_mod_seven_eq_zero",
        active_file="FormalConjectures/ErdosProblems/242.lean",
    )

    route, note = orchestrator_llm.llm_route(ctx, floor)

    assert route is None
    assert note.startswith(ARITHMETIC_PREFLIGHT_REJECTION_PREFIX)
    assert "168*t+1 != 168*t+7" in note


def test_native_consult_records_evidence_and_uses_deterministic_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "0")
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "0")
    response = json.dumps(
        _decision("Every k = 7*t gives n = 168*t+1; prove 24*k+1 = 7*(24*t+1) by ring.")
    )
    monkeypatch.setattr(
        orchestrator_llm,
        "run_model_verification_review",
        lambda **_kwargs: SimpleNamespace(response=response, status="ok"),
    )
    events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        runner, "_record_activity", lambda *args, **kwargs: events.append((args, kwargs))
    )
    autonomy_state: dict[str, Any] = {
        "current_queue_assignment": {
            "target_symbol": "erdos_242_residual_mod_seven_eq_zero",
            "active_file": "FormalConjectures/ErdosProblems/242.lean",
            "slice": "private lemma erdos_242_residual_mod_seven_eq_zero : True := by sorry",
        }
    }
    live_state = {
        "workflow_kind": "prove",
        "target_symbol": "erdos_242_residual_mod_seven_eq_zero",
        "active_file": "FormalConjectures/ErdosProblems/242.lean",
        "declaration_queue_total": 1,
    }

    route = runner._orchestrator_consult("scope-entry", autonomy_state, live_state)

    assert route is not None
    assert route.route == "direct-prove"
    assert route.source == "deterministic"
    signature = autonomy_state["failed_route_signatures"][-1]
    assert signature.startswith(ARITHMETIC_PREFLIGHT_REJECTION_PREFIX)
    route_events = [event for event in events if event[0][0] == "orchestrator-route"]
    assert route_events[-1][1]["llm_note"] == signature
