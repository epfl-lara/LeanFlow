"""Parent-owned semantic novelty tests for research portfolio refreshes."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Any

from leanflow_cli.workflows import (
    dispatch_service,
    research_findings,
    research_portfolio,
    research_route_context,
    research_semantic_identity,
)
from leanflow_cli.workflows.dispatch_models import JobBudget, JobSpec, LedgerEntry

_VALID_WORKER_CHECK = {
    "tool": "lean_incremental_check",
    "valid_without_sorry": True,
    "has_errors": False,
    "has_sorry": False,
    "replacement_matches_target": True,
}


def test_changed_active_file_downgrades_late_candidate_to_evidence(tmp_path):
    active = tmp_path / "Demo.lean"
    original = "theorem demo : True := by\n  sorry\n"
    active.write_text(original, encoding="utf-8")
    finding = {
        "active_file": str(active),
        "source_revision_sha256": sha256(original.encode()).hexdigest(),
        "semantic_novelty": {
            "version": research_route_context.SEMANTIC_NOVELTY_VERSION,
            "progress_anchor_eligible": True,
        },
    }
    active.write_text(
        "private lemma helper : True := by trivial\n\n" + original,
        encoding="utf-8",
    )

    assert research_findings.foreground_use_role(finding) == "evidence_only"
    assert research_findings.foreground_use_reason(finding) == "stale_active_file_revision"


def test_foreground_route_identity_ignores_operational_rewording_and_nonces(tmp_path):
    """Counters, job ids, and optimistic prose cannot make the same hypothesis novel."""
    first = research_semantic_identity.route_semantic_identity(
        route="plan",
        target_symbol="erdos_242",
        active_file=str(tmp_path / "242.lean"),
        reason="generation 1: keep going with route-set 1234567890abcdef",
        target={
            "prover_request_reason": (
                "Use modulus 41 from orchestrator.em-1 at 2026-07-20T10:00:00Z"
            )
        },
    )
    repeated = research_semantic_identity.route_semantic_identity(
        route="plan",
        target_symbol="erdos_242",
        active_file=str(tmp_path / "242.lean"),
        reason="generation 92: a renamed optimistic planning pass",
        target={
            "prover_request_reason": (
                "Check residues modulo 41 in orchestrator.em-99 at 2026-07-20T11:00:00Z"
            )
        },
    )

    assert repeated.key == first.key
    assert repeated.family == "plan"


def test_foreground_route_identity_retains_concrete_target_hypothesis(tmp_path):
    """A concrete mathematical target change is genuine route novelty."""
    modulus_41 = research_semantic_identity.route_semantic_identity(
        route="plan",
        target_symbol="erdos_242",
        active_file=str(tmp_path / "242.lean"),
        target={"target_hypothesis": "analyze residues modulo 41"},
    )
    modulus_43 = research_semantic_identity.route_semantic_identity(
        route="plan",
        target_symbol="erdos_242",
        active_file=str(tmp_path / "242.lean"),
        target={"target_hypothesis": "analyze residues modulo 43"},
    )
    reworded_41 = research_semantic_identity.route_semantic_identity(
        route="plan",
        target_symbol="erdos_242",
        active_file=str(tmp_path / "242.lean"),
        target={"target_hypothesis": "check congruence classes mod modulus 41"},
    )

    assert modulus_43.key != modulus_41.key
    assert reworded_41.key == modulus_41.key


def test_foreground_route_identity_retains_symbolic_target_hypothesis(tmp_path):
    """Changing a mathematical variable is novel without trusting cosmetic counters."""
    active_file = str(tmp_path / "242.lean")
    quotient_q = research_semantic_identity.route_semantic_identity(
        route="plan",
        target_symbol="erdos_242",
        active_file=active_file,
        target={
            "target_hypothesis": (
                "generation 7, epoch 2, route-set 4: prove q divides the numerator "
                "via orchestrator.ds-41"
            )
        },
    )
    reworded_q = research_semantic_identity.route_semantic_identity(
        route="plan",
        target_symbol="erdos_242",
        active_file=active_file,
        target={
            "target_hypothesis": (
                "generation 99, epoch 80, route-set 400: show the numerator is divisible "
                "by q via orchestrator.ds-900"
            )
        },
    )
    quotient_r = research_semantic_identity.route_semantic_identity(
        route="plan",
        target_symbol="erdos_242",
        active_file=active_file,
        target={"target_hypothesis": "prove r divides the numerator"},
    )

    assert reworded_q.key == quotient_q.key
    assert quotient_r.key != quotient_q.key


def test_foreground_route_identity_retains_symbolic_objective_fields(tmp_path):
    """Objective-like target fields carry concrete symbols, not route prose."""
    common = {
        "route": "decompose",
        "target_symbol": "erdos_242",
        "active_file": str(tmp_path / "242.lean"),
    }
    q_objective = research_semantic_identity.route_semantic_identity(
        **common,
        target={"mathematical_target": "derive the residual family for q"},
    )
    r_objective = research_semantic_identity.route_semantic_identity(
        **common,
        target={"mathematical_target": "derive the residual family for r"},
    )

    assert q_objective.key != r_objective.key


def _entry(
    job_id: str,
    *,
    archetype: str,
    deliverable: dict[str, Any],
    target_symbol: str = "erdos_242",
    active_file: str = "FormalConjectures/ErdosProblems/242.lean",
    route_key: str = "research-route",
) -> LedgerEntry:
    """Return one terminal assignment-scoped research entry."""
    spec = JobSpec(
        job_id=job_id,
        archetype=archetype,
        requester_role="orchestrator",
        objective=f"Research {target_symbol} through {route_key}.",
        budget=JobBudget(api_steps=10, wall_clock_s=60),
        deliverable=("experiment_result" if archetype == "empirical" else "findings_report"),
        inputs={
            "target_symbol": target_symbol,
            "active_file": active_file,
            "route_key": route_key,
            "route_signature": f"signature-{job_id}",
        },
        scope={"scratch_only": True},
        parent_job_id=job_id.rpartition(".")[0],
    )
    return LedgerEntry(
        spec=spec,
        state="done",
        result={"status": "done", "deliverable": deliverable},
    )


def _mixed_timeout_mathematical_deliverable() -> dict[str, Any]:
    """Return mathematical route evidence accompanied by one local tool timeout."""
    return {
        "completion_status": "incomplete_unverified",
        "local_mechanism": {
            "certificate": "erdos_242_factor_pair_certificate",
            "why_it_closes": "Reduce the target to one exact factor-pair identity.",
        },
        "partial_coverage_blueprint": {
            "immediate_branch": {
                "condition": "q = 5*t",
                "factorization": "168*q+25 = 5*(168*t+5)",
            },
            "remaining_work": "The other four residue classes remain unresolved.",
        },
        "research_tooling": {
            "decomposition": "Helper-decomposition request timed out.",
            "lean_checks": "No target replacement was claimed checked.",
        },
        "tested_construction_analysis": [
            {
                "choice": "r = 3",
                "modular_obstruction": (
                    "The evident factors cannot supply the required residue modulo 3."
                ),
            }
        ],
    }


def test_ten_witness_is_subsumed_by_prior_cross_archetype_congruence():
    """The live t=10 rediscovery cannot refresh a route after a checked class proof."""
    prior = _entry(
        "campaign.orchestrator.ds-288",
        archetype="deep_search",
        route_key="alternate-formulation",
        deliverable={
            "status": "candidate_verified",
            "checked_replacements": [
                {
                    "target_symbol": "erdos_242",
                    "replacement": (
                        "private lemma covers_ten (t : Nat) (h : t % 41 = 10) : True := by\n"
                        "  apply erdos_242_of_nonresidual_factor\n"
                        "  exact Nat.mod_add_div t 41"
                    ),
                    "worker_check": _VALID_WORKER_CHECK,
                }
            ],
        },
    )
    rediscovery = _entry(
        "campaign.orchestrator.em-294",
        archetype="empirical",
        route_key="boundary-counterexample-probe",
        deliverable={
            "concrete_countermodel": {
                "t": 10,
                "obstruction": "The fixed witness is already covered by the residue helper.",
            }
        },
    )

    novelty = research_route_context.classify_semantic_novelty(
        rediscovery,
        [prior, rediscovery],
    )

    assert "congruence:t%41=10" in research_route_context.semantic_evidence(prior).fingerprints
    assert novelty["classification"] == "subsumed"
    assert novelty["progress_anchor_eligible"] is False
    assert novelty["subsumed_fingerprints"] == ["witness:t=10"]
    assert novelty["subsumed_by_job_ids"] == [prior.spec.job_id]


def test_twenty_countermodel_is_not_selected_over_prior_empirical_witness():
    """The live t=20 cross-lane rediscovery is recorded but not used as a refresh anchor."""
    prior = _entry(
        "campaign.orchestrator.em-289",
        archetype="empirical",
        route_key="small-case-invariant",
        deliverable={
            "concrete_evidence": {
                "t": 20,
                "verified": True,
            }
        },
    )
    rediscovery = _entry(
        "campaign.orchestrator.ds-299",
        archetype="deep_search",
        route_key="informal-proof-blueprint",
        deliverable={
            "new_unresolved_dependency": {
                "concrete_countermodel": {
                    "t": 20,
                    "obstruction": "This proof shape reaches the known t=20 case only.",
                }
            }
        },
    )
    entries = [prior, rediscovery]

    novelty = research_route_context.classify_semantic_novelty(rediscovery, entries)
    anchor = research_portfolio._latest_unconsumed_anchor(
        entries,
        route_key="refresh-audit",
        semantic_entries=entries,
    )

    assert novelty["classification"] == "subsumed"
    assert novelty["progress_anchor_eligible"] is False
    assert "witness:t=20" in novelty["duplicate_fingerprints"]
    assert anchor is prior


def test_checked_helper_repetition_is_subsumed_and_cannot_request_another_helper():
    """The live em-297 to em-298 handoff cannot relaunch evidence-to-helper."""
    first_code = (
        "private lemma eleven_delta (t : Nat) (h : t = 11) : t = 11 := by\n" "  simpa using h"
    )
    repeated_code = (
        "private lemma eleven_delta_again (t : Nat) (h : t = 11) : t = 11 := by\n" "  simpa using h"
    )
    first = _entry(
        "campaign.orchestrator.em-297",
        archetype="empirical",
        route_key="boundary-counterexample-probe",
        deliverable={
            "status": "candidate_verified",
            "checked_proof_delta": {
                "candidate_statement": first_code,
                "worker_check": _VALID_WORKER_CHECK,
            },
        },
    )
    repeated = _entry(
        "campaign.orchestrator.em-298",
        archetype="empirical",
        route_key="helper-integration-audit",
        deliverable={
            "status": "candidate_verified",
            "checked_candidate_helper": {
                "candidate_statement": repeated_code,
                "worker_check": _VALID_WORKER_CHECK,
            },
            "new_unresolved_dependency": {
                "obstruction": "The checked helper still needs integration into the main theorem."
            },
        },
    )
    entries = [first, repeated]
    first_evidence = research_route_context.semantic_evidence(first)
    repeated_evidence = research_route_context.semantic_evidence(repeated)

    novelty = research_route_context.classify_semantic_novelty(repeated, entries)
    refresh_anchor = research_portfolio._latest_unconsumed_anchor(
        entries,
        route_key="refresh-audit",
        semantic_entries=entries,
    )
    helper_anchor = research_portfolio._latest_unconsumed_anchor(
        [first],
        route_key="evidence-to-helper",
        semantic_entries=[first],
    )

    assert first_evidence.has_checked_helper is True
    assert first_evidence.helper_statements == repeated_evidence.helper_statements
    assert first_evidence.mechanisms == repeated_evidence.mechanisms
    assert novelty["classification"] == "subsumed"
    assert novelty["progress_anchor_eligible"] is False
    assert refresh_anchor is None
    assert helper_anchor is None


def test_downgraded_statement_mismatch_does_not_seed_helper_synthesis():
    """Unchecked same-name code cannot masquerade as mathematical progress."""
    deliverable = dispatch_service.enforce_checked_replacement_contract(
        {
            "status": "candidate_verified",
            "checked_replacements": [
                {
                    "target_symbol": "erdos_242",
                    "replacement": "theorem erdos_242 : 1 = 1 := by\n  rfl",
                    "worker_check": {
                        **_VALID_WORKER_CHECK,
                        "replacement_matches_target": False,
                    },
                }
            ],
        },
        expected_target_symbol="erdos_242",
    )
    entry = _entry(
        "campaign.orchestrator.ds-mismatch",
        archetype="deep_search",
        deliverable=deliverable,
    )

    evidence = research_route_context.semantic_evidence(entry)
    helper_anchor = research_portfolio._latest_unconsumed_anchor(
        [entry],
        route_key="evidence-to-helper",
        semantic_entries=[entry],
    )

    assert deliverable["status"] == "incomplete_unverified"
    assert deliverable["checked_replacements"] == []
    assert evidence.has_checked_helper is False
    assert evidence.helper_statements == ()
    assert evidence.mechanisms == ()
    assert (
        research_route_context.classify_semantic_novelty(entry, [entry])["classification"]
        == "unclassified"
    )
    assert helper_anchor is None


def test_provider_timeout_prose_is_operational_not_mathematical_progress():
    """A successful transport envelope cannot turn provider downtime into an anchor."""
    timeout = _entry(
        "campaign.orchestrator.ds-timeout",
        archetype="deep_search",
        deliverable={
            "status": "provider_timeout",
            "summary": "Provider timed out after 75 seconds; retry later.",
        },
    )

    novelty = research_route_context.classify_semantic_novelty(timeout, [timeout])

    assert research_route_context.semantic_evidence(timeout).fingerprints == ()
    assert novelty["classification"] == "operational_error"
    assert research_route_context.semantic_result_is_operational_error(timeout) is True
    assert novelty["progress_anchor_eligible"] is False
    assert (
        research_route_context.semantic_knowledge(
            [timeout],
            target_symbol="erdos_242",
            active_file="FormalConjectures/ErdosProblems/242.lean",
        )["total"]
        == 0
    )
    assert novelty["progress_anchor_reason"] == "deliverable_status:provider_timeout"
    assert (
        research_portfolio._latest_unconsumed_anchor(
            [timeout],
            route_key="refresh-audit",
            semantic_entries=[timeout],
        )
        is None
    )


def test_operational_substance_check_preserves_primary_evidence():
    """A provider error cannot erase mathematical evidence already returned."""
    partial = _entry(
        "campaign.orchestrator.em-timeout-with-evidence",
        archetype="empirical",
        deliverable={
            "status": "provider_timeout",
            "concrete_evidence": {"t": 10, "verified": True},
            "summary": "The provider timed out after reporting a checked witness.",
        },
    )

    novelty = research_route_context.classify_semantic_novelty(partial, [partial])

    assert novelty["classification"] == "novel"
    assert research_route_context.semantic_result_is_operational_error(partial) is False


def test_local_tool_timeout_preserves_obstruction_and_proof_shape_evidence():
    """A failed nested advisor cannot erase the worker's mathematical report."""
    mixed = _entry(
        "campaign.orchestrator.ds-timeout-with-blueprint",
        archetype="deep_search",
        deliverable=_mixed_timeout_mathematical_deliverable(),
    )

    evidence = research_route_context.semantic_evidence(mixed)
    novelty = research_route_context.classify_semantic_novelty(mixed, [mixed])

    assert any(value.startswith("obstruction:") for value in evidence.fingerprints)
    assert any(value.startswith("proof-shape:") for value in evidence.fingerprints)
    assert research_route_context.semantic_result_is_operational_error(mixed) is False
    assert novelty["classification"] == "novel"
    assert novelty["progress_anchor_eligible"] is True


def test_nonempty_error_deliverable_cannot_seed_refresh_anchor():
    """Nonempty operational prose is diagnostic state, not route evidence."""
    error = _entry(
        "campaign.orchestrator.ds-error",
        archetype="deep_search",
        deliverable={
            "summary": "The worker returned a nonempty diagnostic.",
            "error": "Rate limited by the provider API.",
        },
    )

    novelty = research_route_context.classify_semantic_novelty(error, [error])
    route_key, _focus, anchor_job_id = research_portfolio._select_distinct_route(
        [error],
        archetype="deep_search",
        generation=99,
        target_symbol="erdos_242",
        active_file="FormalConjectures/ErdosProblems/242.lean",
    )

    assert novelty["classification"] == "operational_error"
    assert novelty["progress_anchor_eligible"] is False
    assert (
        research_portfolio._latest_unconsumed_anchor(
            [error],
            route_key="refresh-audit",
            semantic_entries=[error],
        )
        is None
    )
    assert route_key == "formal-library-grounding"
    assert anchor_job_id == ""


def test_semantic_fingerprints_are_stable_across_research_archetypes():
    """Container and declaration names cannot disguise the same checked result."""
    first = _entry(
        "campaign.orchestrator.ds-301",
        archetype="deep_search",
        deliverable={
            "status": "candidate_verified",
            "concrete_evidence": {"t": 37},
            "obstruction": {"reason": "the residual factor must be nonzero"},
            "checked_replacements": [
                {
                    "replacement": (
                        "private lemma deep_route (t : Nat) (h : t % 13 = 11) : True := by\n"
                        "  apply erdos_242_of_nonresidual_factor\n"
                        "  exact Nat.mod_add_div t 13"
                    ),
                    "worker_check": _VALID_WORKER_CHECK,
                }
            ],
        },
    )
    repeated = _entry(
        "campaign.orchestrator.em-302",
        archetype="empirical",
        deliverable={
            "status": "candidate_verified",
            "candidate_witness": {"t": 37},
            "obstruction": {"reason": "the residual factor must be nonzero"},
            "checked_candidate_helper": {
                "candidate_statement": (
                    "private lemma empirical_route (t : Nat) (h : t % 13 = 11) : True := by\n"
                    "  apply erdos_242_of_nonresidual_factor\n"
                    "  exact Nat.mod_add_div t 13"
                ),
                "worker_check": _VALID_WORKER_CHECK,
            },
        },
    )
    first_evidence = research_route_context.semantic_evidence(first)
    repeated_evidence = research_route_context.semantic_evidence(repeated)

    novelty = research_route_context.classify_semantic_novelty(
        repeated,
        [first, repeated],
    )

    assert first_evidence.witnesses == repeated_evidence.witnesses == (37,)
    assert first_evidence.congruences == repeated_evidence.congruences == ((13, 11),)
    assert first_evidence.helper_statements == repeated_evidence.helper_statements
    assert first_evidence.obstructions == repeated_evidence.obstructions
    assert first_evidence.mechanisms == repeated_evidence.mechanisms
    assert novelty["classification"] == "duplicate"
    assert novelty["progress_anchor_eligible"] is False


def test_full_history_semantic_index_survives_recent_route_window(monkeypatch, tmp_path):
    """A concrete fact remains visible after its source leaves the recent route window."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    target_symbol = "erdos_242"
    active_file = str(tmp_path / "FormalConjectures/ErdosProblems/242.lean")
    entries = [
        _entry(
            "campaign.orchestrator.em-001",
            archetype="empirical",
            target_symbol=target_symbol,
            active_file=active_file,
            deliverable={"concrete_evidence": {"t": 10}},
        )
    ]
    entries.extend(
        _entry(
            f"campaign.orchestrator.ds-{index:03d}",
            archetype="deep_search",
            target_symbol=target_symbol,
            active_file=active_file,
            route_key=f"route-{index}",
            deliverable={"summary": f"route {index} found no checked delta"},
        )
        for index in range(2, 7)
    )

    context = research_route_context.build_route_context(
        entries,
        target_symbol=target_symbol,
        active_file=active_file,
    )
    rendered = research_route_context.render_route_context(context)

    assert all(
        item["job_id"] != "campaign.orchestrator.em-001"
        for item in context["recent_research_routes"]
    )
    assert {item["fingerprint"] for item in context["semantic_knowledge"]} >= {"witness:t=10"}
    assert "witness:t=10" in rendered
    assert "duplicate or subsumed results are not new progress" in rendered


def test_refresh_chain_ignores_parent_job_route_hash_and_timestamp_churn():
    """The ds-290-style refresh chain cannot manufacture proof-shape novelty."""
    job_ids = [
        "campaign.orchestrator.ds-290",
        "campaign.orchestrator.ds-292",
        "campaign.orchestrator.ds-294",
        "campaign.orchestrator.ds-296",
        "campaign.orchestrator.ds-299",
        "campaign.orchestrator.ds-301",
    ]
    entries: list[LedgerEntry] = []
    for generation, job_id in enumerate(job_ids, start=156):
        parent_job_id = job_ids[max(0, len(entries) - 1)]
        route_hash = f"{generation:020x}"
        entries.append(
            _entry(
                job_id,
                archetype="deep_search",
                route_key=f"refresh-after:{parent_job_id}",
                deliverable={
                    "new_unresolved_dependency": {
                        "proof_shape": (
                            f"Audit parent job {parent_job_id} at route {route_hash}; "
                            "the dispatcher still lacks one exhaustive complement lemma."
                        ),
                        "obstruction": (
                            f"At 2026-07-16T02:{generation % 60:02d}:00Z, source "
                            f"{parent_job_id} and finding {route_hash} leave exactly the same "
                            "exhaustiveness dependency."
                        ),
                    }
                },
            )
        )

    classifications = [
        research_route_context.classify_semantic_novelty(entry, entries[: index + 1])
        for index, entry in enumerate(entries)
    ]
    identities = [research_route_context.semantic_evidence(entry).proof_shapes for entry in entries]
    anchor = research_portfolio._latest_unconsumed_anchor(
        entries,
        route_key="refresh-audit",
        semantic_entries=entries,
    )

    assert identities[0]
    assert len(set(identities)) == 1
    assert classifications[0]["classification"] == "novel"
    assert [item["classification"] for item in classifications[1:]] == ["duplicate"] * 5
    assert all(item["progress_anchor_eligible"] is False for item in classifications[1:])
    assert anchor is entries[0]


def test_checked_helper_is_a_concrete_delta_after_known_empirical_evidence():
    """Evidence-to-helper remains useful when it adds kernel-checked Lean structure."""
    empirical = _entry(
        "campaign.orchestrator.em-297",
        archetype="empirical",
        route_key="boundary-counterexample-probe",
        deliverable={"concrete_evidence": {"t": 20, "verified": True}},
    )
    helper = _entry(
        "campaign.orchestrator.em-298",
        archetype="empirical",
        route_key="evidence-to-helper",
        deliverable={
            "status": "candidate_verified",
            "concrete_evidence": {"t": 20, "verified": True},
            "checked_candidate_helper": {
                "candidate_statement": (
                    "private lemma twenty_delta (t : Nat) (h : t = 20) : t = 20 := by\n"
                    "  simpa using h"
                ),
                "worker_check": _VALID_WORKER_CHECK,
            },
        },
    )

    novelty = research_route_context.classify_semantic_novelty(
        helper,
        [empirical, helper],
    )

    assert novelty["classification"] == "novel"
    assert novelty["progress_anchor_eligible"] is True
    assert novelty["has_checked_helper"] is True
    assert any(
        fingerprint.startswith("helper-statement:") for fingerprint in novelty["novel_fingerprints"]
    )


def _checked_residue_helper(
    job_id: str,
    *,
    modulus: int,
    residue: int,
    target_closing: bool = False,
) -> LedgerEntry:
    """Return one checked residue helper using a stable factor mechanism."""
    declaration_name = "erdos_242" if target_closing else f"residue_{modulus}_{residue}"
    replacement = (
        f"private lemma {declaration_name} (t : Nat) (h : t % {modulus} = {residue}) : "
        "True := by\n"
        f"  have hdiv := Nat.mod_add_div t {modulus}\n"
        "  exact erdos_242_of_nonresidual_factor t hdiv"
    )
    if target_closing:
        checked: dict[str, Any] = {
            "checked_replacements": [
                {
                    "target_symbol": "erdos_242",
                    "replacement": replacement,
                    "worker_check": _VALID_WORKER_CHECK,
                }
            ]
        }
    else:
        checked = {
            "checked_helpers": [
                {
                    "anchor_target_symbol": "erdos_242",
                    "active_file": "FormalConjectures/ErdosProblems/242.lean",
                    "declaration": replacement,
                    "declaration_sha256": sha256(replacement.encode("utf-8")).hexdigest(),
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "action": "check_helper",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "verification_scope": "helper_candidate",
                        "replacement_matches_target": False,
                        "replacement_declarations": [declaration_name],
                    },
                    "parent_recheck_required": True,
                }
            ],
            "checked_helper_status": dispatch_service.CHECKED_HELPER_STATUS,
            "parent_recheck_required": True,
        }
    return _entry(
        job_id,
        archetype="deep_search",
        deliverable={"status": "candidate_verified", **checked},
    )


def _checked_custom_helper(
    job_id: str,
    declaration: str,
    *,
    active_file: str = "FormalConjectures/ErdosProblems/242.lean",
    archetype: str = "deep_search",
    status: str = "candidate_verified",
    target_symbol: str = "erdos_242",
    extra_deliverable: dict[str, Any] | None = None,
) -> LedgerEntry:
    """Return one independently checked helper with exact source preserved."""
    artifact = {
        "anchor_target_symbol": target_symbol,
        "active_file": active_file,
        "declaration": declaration,
        "declaration_sha256": sha256(declaration.encode("utf-8")).hexdigest(),
        "worker_check": {
            "tool": "lean_incremental_check",
            "action": "check_helper",
            "valid_without_sorry": True,
            "has_errors": False,
            "has_sorry": False,
            "verification_scope": "helper_candidate",
            "replacement_matches_target": False,
            "replacement_declarations": ["checked_helper"],
        },
        "parent_recheck_required": True,
    }
    return _entry(
        job_id,
        archetype=archetype,
        target_symbol=target_symbol,
        active_file=active_file,
        deliverable={
            **dict(extra_deliverable or {}),
            "status": status,
            "checked_helpers": [artifact],
            "checked_helper_status": dispatch_service.CHECKED_HELPER_STATUS,
            "parent_recheck_required": True,
        },
    )


def test_explicit_nonclosing_checked_corollary_is_evidence_only():
    """The live np-589 bounded wrapper cannot reset progress or seed a refresh."""
    target = "erdos_242_residual_mod_seven_eq_one"
    declaration = (
        "private lemma erdos_242_residual_mod_seven_eq_one_no_counterexample_bounded "
        ":\n    ¬ ∃ k : ℕ, 1 ≤ k ∧ k ≤ 84 ∧ k % 5 = 1 ∧\n"
        "      ¬ Witness k := by\n"
        "  rintro ⟨k, hk, -, hmod5, hcounter⟩\n"
        "  exact hcounter "
        "(erdos_242_residual_mod_seven_eq_one_of_mod_five_eq_one k hk hmod5)"
    )
    statuses = (
        "bounded_negation_checked_nonclosing",
        "bounded-negation-checked-non-closing",
    )

    for index, status in enumerate(statuses):
        entry = _checked_custom_helper(
            f"campaign.orchestrator.np-58{index}",
            declaration,
            archetype="negation_probe",
            status=status,
            target_symbol=target,
            extra_deliverable={
                "scope_limit": (
                    "This only excludes bounded counterexamples in an already solved branch "
                    "and does not close the target."
                )
            },
        )
        novelty = research_route_context.classify_semantic_novelty(entry, [entry])
        finding = research_findings.build_finding_record(
            entry,
            entry.result,
            entries=[entry],
        )

        assert research_route_context.semantic_evidence(entry).has_checked_helper is True
        assert novelty["classification"] == "nonclosing"
        assert novelty["progress_anchor_eligible"] is False
        assert novelty["progress_anchor_reason"] == "explicit_nonclosing_result"
        assert finding["semantic_novelty"]["classification"] == "nonclosing"
        assert research_findings.foreground_use_role(finding) == "evidence_only"
        assert research_findings.foreground_use_reason(finding) == "explicit_nonclosing_result"
        assert (
            research_portfolio._latest_unconsumed_anchor(
                [entry],
                route_key="refresh-audit",
                semantic_entries=[entry],
            )
            is None
        )


def test_live_fixed_or_bounded_instance_is_dedupe_evidence_not_progress():
    """The em-704/em-705 finite-result statuses cannot reset the portfolio."""
    declaration = (
        "private lemma research_fixed_two_over_seven :\n"
        "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
        "      (2 / 7 : ℚ) = 1 / x + 1 / y + 1 / z := by\n"
        "  refine ⟨4, 29, 812, by norm_num, by norm_num, by norm_num, ?_⟩\n"
        "  norm_num"
    )
    statuses = (
        "new_fixed_instance_checked_not_target_completion",
        "new_bounded_instance_evidence_not_proof_completion",
    )

    for index, status in enumerate(statuses):
        entry = _checked_custom_helper(
            f"campaign.orchestrator.em-70{index + 4}",
            declaration,
            archetype="empirical",
            status=status,
            target_symbol="erdos_242.variants.schinzel_generalization",
            extra_deliverable={
                "bounded_experiment": {
                    "bounds": {"m": [7, 7]},
                    "instance": {"a": 2, "n": 7, "x": 4, "y": 29, "z": 812},
                },
                "issues": [
                    "This supplies finite coverage only and does not advance an eventual proof."
                ],
            },
        )
        novelty = research_route_context.classify_semantic_novelty(entry, [entry])
        finding = research_findings.build_finding_record(
            entry,
            entry.result,
            entries=[entry],
        )
        cooldown = research_portfolio._semantic_lane_cooldown_record(
            entry,
            semantic_entries=[entry],
        )

        assert research_route_context.explicitly_declared_finite_evidence_result(
            entry.result["deliverable"]
        )
        assert novelty["classification"] == "finite_evidence_only"
        assert novelty["progress_anchor_eligible"] is False
        assert novelty["progress_anchor_reason"] == "declared_finite_evidence_only"
        assert research_findings.foreground_use_role(finding) == "evidence_only"
        assert (
            research_portfolio._latest_unconsumed_anchor(
                [entry],
                route_key="refresh-audit",
                semantic_entries=[entry],
            )
            is None
        )
        assert cooldown == {
            "job_id": entry.spec.job_id,
            "classification": "finite_evidence_only",
            "reason": "declared_finite_evidence_only",
        }


def test_partial_checked_general_reduction_remains_semantically_novel():
    """A generic partial label alone cannot suppress a new parametric reduction."""
    entry = _checked_custom_helper(
        "campaign.orchestrator.ds-general-reduction",
        (
            "private lemma erdos_242_uniform_descent (t : Nat) : True := by\n"
            "  induction t using Nat.strong_induction_on with\n"
            "  | h t ih => exact True.intro"
        ),
        status="partial_general_reduction_checked",
    )

    novelty = research_route_context.classify_semantic_novelty(entry, [entry])

    assert research_route_context.explicitly_nonclosing_result(entry.result["deliverable"])
    assert not research_route_context.explicitly_declared_nonclosing_result(
        entry.result["deliverable"]
    )
    assert novelty["classification"] == "novel"
    assert novelty["progress_anchor_eligible"] is True


def test_nonclosing_status_does_not_override_exact_target_replacement():
    """A contract-valid target proof remains actionable despite stale status prose."""
    entry = _checked_residue_helper(
        "campaign.orchestrator.ds-target-complete",
        modulus=97,
        residue=21,
        target_closing=True,
    )
    entry.result["deliverable"]["status"] = "research_checked_nonclosing"

    novelty = research_route_context.classify_semantic_novelty(entry, [entry])

    assert novelty["checked_target_replacement"] is True
    assert novelty["classification"] == "novel"
    assert novelty["progress_anchor_eligible"] is True


_AUDIT_TARGET = "erdos_242_residual_mod_seven_eq_one"
_AUDIT_ACTIVE_FILE = "FormalConjectures/ErdosProblems/242.lean"


def _audit_declaration(literal: int) -> str:
    """Return one exact live-shaped closed finite audit helper."""
    return (
        f"private lemma erdos_242_audit_k{literal} :\n"
        "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
        f"      (4 / ((24 * {literal} + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by\n"
        "  exact audit_certificate\n"
    )


def _checked_audit_entry(job_id: str, *literals: int) -> LedgerEntry:
    """Return one consumed-style empirical result with checked audit helpers."""
    checked_helpers = []
    for literal in literals:
        declaration = _audit_declaration(literal)
        checked_helpers.append(
            {
                "anchor_target_symbol": _AUDIT_TARGET,
                "active_file": _AUDIT_ACTIVE_FILE,
                "declaration": declaration,
                "declaration_sha256": sha256(declaration.encode("utf-8")).hexdigest(),
                "worker_check": {
                    "tool": "lean_incremental_check",
                    "action": "check_helper",
                    "valid_without_sorry": True,
                    "has_errors": False,
                    "has_sorry": False,
                    "verification_scope": "helper_candidate",
                    "replacement_matches_target": False,
                    "replacement_declarations": [f"erdos_242_audit_k{literal}"],
                },
                "parent_recheck_required": True,
            }
        )
    return _entry(
        job_id,
        archetype="empirical",
        target_symbol=_AUDIT_TARGET,
        active_file=_AUDIT_ACTIVE_FILE,
        route_key=f"history-refresh:{job_id.rsplit('-', 1)[-1]}",
        deliverable={
            "status": "new_finite_coverage_only",
            "checked_helpers": checked_helpers,
            "checked_helper_status": dispatch_service.CHECKED_HELPER_STATUS,
            "parent_recheck_required": True,
        },
    )


def test_new_modulus_with_repeated_mechanism_is_not_a_fresh_progress_anchor():
    """A residue number cannot disguise the same checked factor proof shape."""
    prior = _checked_residue_helper(
        "campaign.orchestrator.ds-401",
        modulus=41,
        residue=10,
    )
    repeated = _checked_residue_helper(
        "campaign.orchestrator.ds-402",
        modulus=83,
        residue=41,
    )
    entries = [prior, repeated]

    prior_evidence = research_route_context.semantic_evidence(prior)
    repeated_evidence = research_route_context.semantic_evidence(repeated)
    novelty = research_route_context.classify_semantic_novelty(repeated, entries)

    assert prior_evidence.mechanisms == repeated_evidence.mechanisms
    assert prior_evidence.congruences == ((41, 10),)
    assert repeated_evidence.congruences == ((83, 41),)
    assert novelty["classification"] == "mechanism_repeat"
    assert novelty["progress_anchor_eligible"] is False
    assert novelty["progress_anchor_reason"] == ("repeated_mechanism_without_material_coverage")
    assert novelty["repeated_mechanism_fingerprints"]
    assert novelty["repeated_mechanism_job_ids"] == [prior.spec.job_id]
    assert (
        research_portfolio._latest_unconsumed_anchor(
            entries,
            route_key="refresh-audit",
            semantic_entries=entries,
        )
        is None
    )


def test_repeated_mechanism_can_anchor_strictly_broader_verified_coverage():
    """A checked residue class that contains prior coverage remains real progress."""
    prior = _checked_residue_helper(
        "campaign.orchestrator.ds-403",
        modulus=12,
        residue=5,
    )
    broader = _checked_residue_helper(
        "campaign.orchestrator.ds-404",
        modulus=4,
        residue=1,
    )
    broader.result["deliverable"]["status"] = "new_finite_coverage_only"

    novelty = research_route_context.classify_semantic_novelty(
        broader,
        [prior, broader],
    )

    assert novelty["classification"] == "novel"
    assert novelty["progress_anchor_eligible"] is True
    assert novelty["materially_broader_coverage"] == [
        {
            "current": "congruence:t%4=1",
            "prior": "congruence:t%12=5",
            "prior_job_id": prior.spec.job_id,
        }
    ]


def test_repeated_mechanism_never_suppresses_checked_target_completion():
    """A contract-valid target replacement outranks mechanism repetition."""
    prior = _checked_residue_helper(
        "campaign.orchestrator.ds-405",
        modulus=41,
        residue=10,
    )
    closing = _checked_residue_helper(
        "campaign.orchestrator.ds-406",
        modulus=83,
        residue=41,
        target_closing=True,
    )

    novelty = research_route_context.classify_semantic_novelty(
        closing,
        [prior, closing],
    )

    assert novelty["classification"] == "novel"
    assert novelty["progress_anchor_eligible"] is True
    assert novelty["checked_target_replacement"] is True


def test_saturated_residue_family_rejects_syntactic_mechanism_evasion():
    """A ds-448-style one-residue proof is evidence after the finite sieve saturates."""
    prior = [
        _checked_residue_helper(
            f"campaign.orchestrator.ds-44{index}",
            modulus=modulus,
            residue=residue,
        )
        for index, (modulus, residue) in enumerate(
            ((41, 10), (43, 12), (83, 41), (87, 20)),
            start=1,
        )
    ]
    ds448 = _checked_custom_helper(
        "campaign.orchestrator.ds-448",
        (
            "private lemma erdos_242_residual_mod_forty_seven_eq_forty "
            "(t : Nat) (h47 : t % 47 = 40) : True := by\n"
            "  obtain \u27e8q, rfl\u27e9 : \u2203 q, t = 47 * q + 40 := by\n"
            "    refine \u27e8t / 47, ?_\u27e9\n"
            "    omega\n"
            "  ring_nf\n"
            "  exact True.intro"
        ),
    )
    entries = [*prior, ds448]

    novelty = research_route_context.classify_semantic_novelty(ds448, entries)
    evidence = research_route_context.semantic_evidence(ds448)

    assert evidence.has_checked_helper is True
    assert evidence.congruences == ((47, 40),)
    assert set(evidence.mechanisms).isdisjoint(
        {
            mechanism
            for entry in prior
            for mechanism in research_route_context.semantic_evidence(entry).mechanisms
        }
    )
    assert novelty["classification"] == "finite_branch_repeat"
    assert novelty["progress_anchor_eligible"] is False
    assert novelty["progress_anchor_reason"] == "saturated_finite_branch_family"
    assert novelty["finite_branch_family"] == "finite_or_single_congruence_coverage"
    assert novelty["finite_branch_prior_count"] == 4
    assert novelty["finite_branch_prior_job_ids"] == [entry.spec.job_id for entry in prior]


def test_live_finite_audit_sequence_counts_all_checked_declarations():
    """The em-575..581 sequence saturates after five distinct prior audits."""
    em575 = _checked_audit_entry("campaign.orchestrator.em-575", 358)
    em577 = _checked_audit_entry("campaign.orchestrator.em-577", 1002)
    em578 = _checked_audit_entry("campaign.orchestrator.em-578", 5006)
    em580 = _checked_audit_entry("campaign.orchestrator.em-580", 50008, 50009)
    em581 = _checked_audit_entry("campaign.orchestrator.em-581", 100003)
    entries = [em575, em577, em578, em580, em581]
    expected_prior_counts = [0, 1, 2, 3, 5]
    expected_current_counts = [1, 1, 1, 2, 1]

    classifications = [
        research_route_context.classify_semantic_novelty(entry, entries[: index + 1])
        for index, entry in enumerate(entries)
    ]

    assert [item["finite_branch_prior_count"] for item in classifications] == (
        expected_prior_counts
    )
    assert [item["finite_branch_current_count"] for item in classifications] == (
        expected_current_counts
    )
    assert classifications[-1]["classification"] == "finite_branch_repeat"
    assert classifications[-1]["progress_anchor_eligible"] is False
    assert classifications[-1]["finite_branch_prior_job_ids"] == [
        em575.spec.job_id,
        em577.spec.job_id,
        em578.spec.job_id,
        em580.spec.job_id,
    ]

    finding = research_findings.build_finding_record(
        em581,
        em581.result,
        entries=entries,
    )
    assert research_findings.foreground_use_role(finding) == "evidence_only"
    assert finding["deliverable"]["checked_helpers"][0]["declaration"] == (
        _audit_declaration(100003)
    )

    context = research_route_context.build_route_context(
        entries,
        target_symbol=_AUDIT_TARGET,
        active_file=_AUDIT_ACTIVE_FILE,
    )
    em581_route = next(
        record
        for record in context["recent_research_routes"]
        if record["job_id"] == em581.spec.job_id
    )
    assert em581_route["objective"].startswith("Evidence-only non-closing prior route")
    assert "partial_coverage_without_completion" in em581_route["result_excerpt"]
    assert "erdos_242_audit_k100003" not in research_route_context.render_route_context(context)


def test_exact_closed_target_case_is_immediate_evidence_without_prior_family():
    """A source-backed exact case cannot reset research progress on its own."""
    declaration = (
        f"private lemma {_AUDIT_TARGET}_case_k_eq_1 :\n"
        "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
        "      (4 / ((24 * 1 + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by\n"
        "  exact case_certificate\n"
    )
    entry = _checked_custom_helper(
        "campaign.orchestrator.ds-582",
        declaration,
        active_file=_AUDIT_ACTIVE_FILE,
    )
    entry = replace(
        entry,
        spec=replace(
            entry.spec,
            inputs={**entry.spec.inputs, "target_symbol": _AUDIT_TARGET},
        ),
    )

    novelty = research_route_context.classify_semantic_novelty(entry, [entry])
    finding = research_findings.build_finding_record(
        entry,
        entry.result,
        entries=[entry],
    )

    assert novelty["classification"] == "finite_branch_repeat"
    assert novelty["progress_anchor_eligible"] is False
    assert novelty["finite_branch_prior_count"] == 0
    assert novelty["finite_branch_current_count"] == 1
    assert novelty["finite_branch_current_fingerprints"] == [
        f"closed-target-case:{_AUDIT_TARGET}:k=1"
    ]
    assert research_findings.foreground_use_role(finding) == "evidence_only"


def test_saturated_audits_preserve_parametric_helper_and_exact_target_completion():
    """Finite-audit containment cannot suppress uniform or target-closing proofs."""
    prior = [
        _checked_audit_entry(f"campaign.orchestrator.em-{index}", literal)
        for index, literal in enumerate((358, 1002, 5006, 50008, 50009), start=590)
    ]
    parametric = _checked_custom_helper(
        "campaign.orchestrator.ds-596",
        (
            "private lemma erdos_242_audit_k358 (k : ℕ) :\n"
            "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
            "      (4 / ((24 * 358 + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by\n"
            "  exact parametric_audit_family k"
        ),
        active_file=_AUDIT_ACTIVE_FILE,
    )
    parametric = replace(
        parametric,
        spec=replace(
            parametric.spec,
            inputs={**parametric.spec.inputs, "target_symbol": _AUDIT_TARGET},
        ),
    )
    target_declaration = (
        f"private lemma {_AUDIT_TARGET} (k : ℕ) (hk : 1 ≤ k) "
        "(hmod : k % 7 = 1) :\n"
        "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
        "      (4 / ((24 * k + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z := by\n"
        "  exact uniform_target_completion k hk hmod"
    )
    closing = _entry(
        "campaign.orchestrator.ds-597",
        archetype="deep_search",
        target_symbol=_AUDIT_TARGET,
        active_file=_AUDIT_ACTIVE_FILE,
        deliverable={
            "status": "candidate_verified",
            "checked_replacements": [
                {
                    "target_symbol": _AUDIT_TARGET,
                    "replacement": target_declaration,
                    "worker_check": _VALID_WORKER_CHECK,
                }
            ],
        },
    )

    parametric_novelty = research_route_context.classify_semantic_novelty(
        parametric,
        [*prior, parametric],
    )
    closing_novelty = research_route_context.classify_semantic_novelty(
        closing,
        [*prior, closing],
    )

    assert parametric_novelty["finite_branch_current_count"] == 0
    assert parametric_novelty["progress_anchor_eligible"] is True
    assert closing_novelty["progress_anchor_eligible"] is True
    assert closing_novelty["checked_target_replacement"] is True


def test_singleton_then_scaled_residue_stays_in_saturated_finite_family():
    """The em-451/em-453 singleton-to-scaling chain cannot manufacture progress."""
    prior = [
        _checked_residue_helper(
            f"campaign.orchestrator.ds-45{index}",
            modulus=modulus,
            residue=residue,
        )
        for index, (modulus, residue) in enumerate(
            ((41, 10), (43, 12), (83, 41), (87, 20)),
            start=1,
        )
    ]
    em451 = _checked_custom_helper(
        "campaign.orchestrator.em-451",
        (
            "private lemma erdos_242_t_eq_twenty_one (t : Nat) (ht : t = 21) : True := by\n"
            "  subst t\n"
            "  norm_num"
        ),
    )
    em453 = _checked_custom_helper(
        "campaign.orchestrator.em-453",
        (
            "private lemma erdos_242_mod_3529_eq_twenty_one "
            "(t : Nat) (h : t % 3529 = 21) : True := by\n"
            "  have scaled := erdos_242_scale t 3529 21 h\n"
            "  simpa using scaled"
        ),
    )
    entries = [*prior, em451, em453]

    singleton_novelty = research_route_context.classify_semantic_novelty(
        em451,
        [*prior, em451],
    )
    scaled_novelty = research_route_context.classify_semantic_novelty(em453, entries)

    assert research_route_context.semantic_evidence(em451).witnesses == (21,)
    assert research_route_context.semantic_evidence(em453).congruences == ((3529, 21),)
    assert singleton_novelty["classification"] == "finite_branch_repeat"
    assert singleton_novelty["progress_anchor_eligible"] is False
    assert scaled_novelty["classification"] == "finite_branch_repeat"
    assert scaled_novelty["progress_anchor_eligible"] is False


def test_checked_branch_identity_outranks_supporting_witness_and_obstruction_metadata():
    """Exact ds-452/em-454 report extras cannot hide a one-congruence helper family."""
    prior = [
        _checked_residue_helper(
            f"campaign.orchestrator.ds-48{index}",
            modulus=modulus,
            residue=residue,
        )
        for index, (modulus, residue) in enumerate(
            ((41, 10), (43, 12), (83, 41), (87, 20)),
            start=1,
        )
    ]
    ds452 = _checked_custom_helper(
        "campaign.orchestrator.ds-452",
        (
            "private lemma erdos_242_mod_eleven_eq_three "
            "(t : Nat) (h : t % 11 = 3) : True := by\n"
            "  exact erdos_242_of_nonresidual_factor t (by omega)"
        ),
        extra_deliverable={
            "concrete_evidence": {"t": 25},
            "obstruction": "The remaining residue sieve is not exhaustive.",
        },
    )
    em454 = _checked_custom_helper(
        "campaign.orchestrator.em-454",
        (
            "private lemma erdos_242_mod_195_eq_sixteen "
            "(t : Nat) (h : t % 195 = 16) : True := by\n"
            "  have scaled := erdos_242_scale t 195 16 h\n"
            "  simpa using scaled"
        ),
        extra_deliverable={
            "surviving_witness": {"t": 16},
            "obstruction": "One exceptional family remains open.",
        },
    )

    for current in (ds452, em454):
        evidence = research_route_context.semantic_evidence(current)
        novelty = research_route_context.classify_semantic_novelty(
            current,
            [*prior, current],
        )

        assert len(evidence.witnesses) == 1
        assert len(evidence.congruences) == 1
        assert novelty["classification"] == "finite_branch_repeat"
        assert novelty["progress_anchor_eligible"] is False
        assert novelty["finite_branch_prior_count"] == 4


def test_closed_target_prefixed_singleton_is_in_saturated_finite_family():
    """A closed ``erdos_242_at_*`` base case cannot reset the saturated sieve."""
    prior = [
        _checked_residue_helper(
            f"campaign.orchestrator.ds-49{index}",
            modulus=modulus,
            residue=residue,
        )
        for index, (modulus, residue) in enumerate(
            ((41, 10), (43, 12), (83, 41), (87, 20)),
            start=1,
        )
    ]
    closed = _checked_custom_helper(
        "campaign.orchestrator.em-495",
        (
            "private lemma erdos_242_at_twenty_one : "
            "\u2203 x : Nat, x = 4 / (168 * 21 + 1) := by\n"
            "  exact \u27e80, by norm_num\u27e9"
        ),
    )

    novelty = research_route_context.classify_semantic_novelty(
        closed,
        [*prior, closed],
    )

    assert research_route_context.semantic_evidence(closed).witnesses == ()
    assert research_route_context.semantic_evidence(closed).congruences == ()
    assert novelty["classification"] == "finite_branch_repeat"
    assert novelty["progress_anchor_eligible"] is False


def test_saturated_finite_family_preserves_uniform_and_target_closing_helpers():
    """Family containment does not suppress uniform mathematics or exact completion."""
    prior = [
        _checked_residue_helper(
            f"campaign.orchestrator.ds-46{index}",
            modulus=modulus,
            residue=residue,
        )
        for index, (modulus, residue) in enumerate(
            ((41, 10), (43, 12), (83, 41), (87, 20)),
            start=1,
        )
    ]
    uniform = _checked_custom_helper(
        "campaign.orchestrator.ds-465",
        (
            "private lemma erdos_242_uniform_descent (t : Nat) : True := by\n"
            "  induction t using Nat.strong_induction_on with\n"
            "  | h t ih => exact True.intro"
        ),
    )
    closing = _checked_residue_helper(
        "campaign.orchestrator.ds-466",
        modulus=97,
        residue=21,
        target_closing=True,
    )

    uniform_novelty = research_route_context.classify_semantic_novelty(
        uniform,
        [*prior, uniform],
    )
    closing_novelty = research_route_context.classify_semantic_novelty(
        closing,
        [*prior, closing],
    )

    assert uniform_novelty["classification"] == "novel"
    assert uniform_novelty["progress_anchor_eligible"] is True
    assert closing_novelty["classification"] == "novel"
    assert closing_novelty["progress_anchor_eligible"] is True
    assert closing_novelty["checked_target_replacement"] is True


def test_saturated_finite_family_preserves_strictly_broader_congruence():
    """A class that strictly contains prior verified coverage remains progress."""
    prior = [
        _checked_residue_helper(
            f"campaign.orchestrator.ds-50{index}",
            modulus=modulus,
            residue=residue,
        )
        for index, (modulus, residue) in enumerate(
            ((10, 3), (7, 1), (11, 2), (13, 4)),
            start=1,
        )
    ]
    broader = _checked_residue_helper(
        "campaign.orchestrator.ds-505",
        modulus=5,
        residue=3,
    )

    novelty = research_route_context.classify_semantic_novelty(
        broader,
        [*prior, broader],
    )

    assert novelty["classification"] == "novel"
    assert novelty["progress_anchor_eligible"] is True
    assert {
        (item["current"], item["prior"], item["prior_job_id"])
        for item in novelty["materially_broader_coverage"]
    } >= {
        (
            "congruence:t%5=3",
            "congruence:t%10=3",
            prior[0].spec.job_id,
        )
    }


def test_checked_reverse_target_helper_is_evidence_only(tmp_path):
    """A checked target-to-exceptions implication cannot refresh its own target route."""
    active = tmp_path / "Main.lean"
    active.write_text(
        "theorem erdos_242 (k : Nat) (hk : 1 \u2264 k) (hmod : k % 7 = 0) : Witness k := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    reverse = _checked_custom_helper(
        "campaign.orchestrator.ds-450",
        (
            "private lemma exceptional_families_of_erdos_242\n"
            "    (hmain : \u2200 k : Nat, 1 \u2264 k \u2192 k % 7 = 0 \u2192 Witness k) :\n"
            "    \u2200 s : Nat, Exceptional s := by\n"
            "  exact deriveExceptional hmain"
        ),
        active_file=str(active),
    )

    novelty = research_route_context.classify_semantic_novelty(reverse, [reverse])

    assert research_route_context.semantic_evidence(reverse).has_checked_helper is True
    assert novelty["classification"] == "circular_helper"
    assert novelty["progress_anchor_eligible"] is False
    assert novelty["progress_anchor_reason"] == "helper_assumes_unresolved_target"
    assert novelty["circular_helper_obligation_hashes"]


def test_version_four_pending_finite_finding_is_reclassified_before_delivery():
    """Resume cannot replay an old actionable prompt after family policy changes."""
    prior = [
        _checked_residue_helper(
            f"campaign.orchestrator.ds-47{index}",
            modulus=modulus,
            residue=residue,
        )
        for index, (modulus, residue) in enumerate(
            ((41, 10), (43, 12), (83, 41), (87, 20)),
            start=1,
        )
    ]
    current = _checked_custom_helper(
        "campaign.orchestrator.ds-475",
        (
            "private lemma erdos_242_mod_101_eq_twenty_one "
            "(t : Nat) (h : t % 101 = 21) : True := by\n"
            "  have scaled := erdos_242_scale t 101 21 h\n"
            "  simpa using scaled"
        ),
    )
    entries = [*prior, current]
    stale = research_findings.build_finding_record(
        current,
        current.result,
        entries=entries,
    )
    stale["semantic_novelty"] = {
        "version": 4,
        "classification": "novel",
        "progress_anchor_eligible": True,
        "progress_anchor_reason": "new_mathematical_semantics",
    }
    summary = {
        "dispatch_ledger": [entry.to_mapping() for entry in entries],
        "research_findings": [stale],
    }

    selected = research_findings.relevant_findings(
        summary,
        target_symbol="erdos_242",
        active_file="FormalConjectures/ErdosProblems/242.lean",
        limit=None,
    )

    assert len(selected) == 1
    assert selected[0]["semantic_novelty"]["version"] == (
        research_route_context.SEMANTIC_NOVELTY_VERSION
    )
    assert selected[0]["semantic_novelty"]["classification"] == "finite_branch_repeat"
    assert research_findings.foreground_use_role(selected[0]) == "evidence_only"
    stale_prompt = "[LEANFLOW RESEARCH DELIVERY TOKEN: stale]\nold actionable branch"
    state = {
        "campaign_id": "campaign",
        research_findings.PENDING_FOREGROUND_KEY: [
            {
                "token": research_findings.DELIVERY_TOKEN_PREFIX + "stale",
                "target_symbol": "erdos_242",
                "markers": [research_findings.delivery_key(current.spec.job_id, "erdos_242")],
                "prompt": stale_prompt,
                "semantic_novelty_version": 4,
            }
        ],
    }
    assert (
        research_findings.attach_pending_foreground_prompts(
            state,
            target_symbol="erdos_242",
            user_message="continue",
            conversation_history=(),
        )
        == "continue"
    )
    assert research_findings.PENDING_FOREGROUND_KEY not in state


def test_version_five_audit_finding_is_reclassified_before_delivery():
    """Resume repairs an actionable v5 audit after plural finite coverage ships."""
    entries = [
        _checked_audit_entry("campaign.orchestrator.em-575", 358),
        _checked_audit_entry("campaign.orchestrator.em-577", 1002),
        _checked_audit_entry("campaign.orchestrator.em-578", 5006),
        _checked_audit_entry("campaign.orchestrator.em-580", 50008, 50009),
        _checked_audit_entry("campaign.orchestrator.em-581", 100003),
    ]
    current = entries[-1]
    stale = research_findings.build_finding_record(
        current,
        current.result,
        entries=entries,
    )
    stale["semantic_novelty"] = {
        "version": 5,
        "classification": "novel",
        "progress_anchor_eligible": True,
        "progress_anchor_reason": "new_mathematical_semantics",
    }
    summary = {
        "dispatch_ledger": [entry.to_mapping() for entry in entries],
        "research_findings": [stale],
    }

    selected = research_findings.relevant_findings(
        summary,
        target_symbol=_AUDIT_TARGET,
        active_file=_AUDIT_ACTIVE_FILE,
        limit=None,
    )

    assert len(selected) == 1
    assert selected[0]["semantic_novelty"]["version"] == (
        research_route_context.SEMANTIC_NOVELTY_VERSION
    )
    assert selected[0]["semantic_novelty"]["classification"] == "finite_branch_repeat"
    assert selected[0]["semantic_novelty"]["finite_branch_prior_count"] == 5
    assert research_findings.foreground_use_role(selected[0]) == "evidence_only"


def test_incomplete_report_preserves_only_canonical_checked_helpers():
    """Trusted helpers survive target-replacement downgrade without reviving other code."""
    entry = _entry(
        "campaign.orchestrator.ds-checked-helper",
        archetype="deep_search",
        deliverable={
            "status": "incomplete_unverified",
            "checked_helpers": [
                {
                    "declaration": "private lemma trusted : True := by exact True.intro",
                    "worker_check": {
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                    },
                }
            ],
            "checked_candidate_helper": {
                "declaration": "private lemma downgraded : True := by omega",
                "worker_check": {
                    "valid_without_sorry": True,
                    "has_errors": False,
                    "has_sorry": False,
                },
            },
        },
    )

    evidence = research_route_context.semantic_evidence(entry)

    assert evidence.has_checked_helper is True
    assert len(evidence.helper_statements) == 1
    assert len(evidence.mechanisms) == 1
    assert "exact" in evidence.mechanisms[0]
    assert "omega" not in evidence.mechanisms[0]


def test_malformed_proof_shape_cannot_seed_refresh_or_semantic_knowledge():
    """A non-text model shape fails closed even beside plausible obstruction prose."""
    malformed = _entry(
        "campaign.orchestrator.ds-malformed",
        archetype="deep_search",
        deliverable={
            "new_unresolved_dependency": {
                "proof_shape": 17,
                "obstruction": "The dispatcher still lacks exhaustive coverage.",
            }
        },
    )

    novelty = research_route_context.classify_semantic_novelty(malformed, [malformed])
    knowledge = research_route_context.semantic_knowledge(
        [malformed],
        target_symbol="erdos_242",
        active_file="FormalConjectures/ErdosProblems/242.lean",
    )

    assert novelty["classification"] == "malformed"
    assert novelty["progress_anchor_eligible"] is False
    assert novelty["progress_anchor_reason"] == "malformed_semantic_evidence"
    assert novelty["malformed"] is True
    assert knowledge["total"] == 0
    assert (
        research_portfolio._latest_unconsumed_anchor(
            [malformed],
            route_key="refresh-audit",
            semantic_entries=[malformed],
        )
        is None
    )


def test_em300_t11_local_branch_is_not_novel_after_em298_checked_helper():
    """Revalidating the same t=11 witnesses cannot launch an audit of em-300."""
    em298 = _entry(
        "campaign.orchestrator.em-298",
        archetype="empirical",
        route_key="evidence-to-helper",
        deliverable={
            "checked_candidate_helper": {
                "declaration": (
                    "private lemma eleven_helper (t : Nat) (ht : t = 11) : "
                    "Exists fun x : Nat => x = 465 := by\n"
                    "  subst t\n"
                    "  exact ⟨465, rfl⟩"
                ),
                "verification": _VALID_WORKER_CHECK,
            },
            "obstruction": "A uniform factor mechanism does not cover t = 11.",
        },
    )
    em300 = _entry(
        "campaign.orchestrator.em-300",
        archetype="empirical",
        route_key="refresh-after:campaign.orchestrator.em-298",
        deliverable={
            "checked_proof_delta": {
                "shape": (
                    "After the normal-form rewrite, define a local t = 11 continuation and "
                    "dispatch it before the existing branches."
                ),
                "verification": {
                    "has_errors": False,
                    "overall_valid_without_sorry": False,
                },
                "witnesses": [465, 78174, 521029710],
            },
            "isolated_unresolved_dependency": (
                "The checked local continuation revalidates t = 11 with witnesses "
                "465, 78174, and 521029710; the final general branch remains unresolved."
            ),
        },
    )
    entries = [em298, em300]

    novelty = research_route_context.classify_semantic_novelty(em300, entries)
    refresh_anchor = research_portfolio._latest_unconsumed_anchor(
        entries,
        route_key="refresh-audit",
        semantic_entries=entries,
    )

    assert "witness:t=11" in research_route_context.semantic_evidence(em298).fingerprints
    assert novelty["classification"] in {"duplicate", "subsumed"}
    assert novelty["progress_anchor_eligible"] is False
    assert "witness:t=11" in novelty["duplicate_fingerprints"]
    assert refresh_anchor is None


def test_plural_checked_helpers_and_proof_deltas_are_terminal_evidence():
    """Exact em-260/em-261 list shapes cannot recursively seed an integration audit."""
    em260 = _entry(
        "campaign.orchestrator.em-260",
        archetype="empirical",
        route_key="evidence-to-helper",
        deliverable={
            "checked_candidate_helpers": [
                {
                    "name": "guard_gap",
                    "statement": ("∃ t : ℕ, 1 ≤ t ∧ t = 421135 ∧ t % 5 = 0"),
                    "proof": "by exact ⟨421135, by norm_num, by norm_num, by norm_num⟩",
                    "verification": {
                        "result": "accepted",
                        "valid_without_sorry": True,
                    },
                },
                {
                    "name": "guard_gap_reachable",
                    "statement": ("∃ k t : ℕ, k = 2947945 ∧ t = 421135 ∧ k = 7 * t"),
                    "proof": "by exact ⟨2947945, 421135, by norm_num, by norm_num, by norm_num⟩",
                    "verification": {
                        "result": "accepted",
                        "valid_without_sorry": True,
                    },
                },
            ],
            "concrete_dependency": {"why": "At t = 421135 every existing residue guard fails."},
        },
    )
    em261 = _entry(
        "campaign.orchestrator.em-261",
        archetype="empirical",
        route_key="refresh-after:campaign.orchestrator.em-260",
        deliverable={
            "checked_proof_delta": [
                {
                    "name": "gap_witness_expansion",
                    "statement": (
                        "∃ x y z : ℕ, x = 17687673 ∧ y = 113764991823232 ∧ "
                        "z = 595678690418047247622754944"
                    ),
                    "proof": (
                        "by exact ⟨17687673, 113764991823232, "
                        "595678690418047247622754944, rfl, rfl, rfl⟩"
                    ),
                    "verification": {
                        "result": "accepted",
                        "valid_without_sorry": True,
                    },
                }
            ],
            "concrete_dependency_investigated": {
                "resolution": (
                    "The exhibited t = 421135 has a checked direct certificate; a uniform "
                    "construction remains unresolved."
                )
            },
        },
    )
    entries = [em260, em261]

    em260_evidence = research_route_context.semantic_evidence(em260)
    em261_evidence = research_route_context.semantic_evidence(em261)

    assert em260_evidence.has_checked_helper is True
    assert len(em260_evidence.helper_statements) == 2
    assert em261_evidence.has_checked_helper is True
    assert len(em261_evidence.helper_statements) == 1
    assert not research_portfolio._is_progress_route_evidence(
        em260,
        semantic_entries=entries,
    )
    assert not research_portfolio._is_progress_route_evidence(
        em261,
        semantic_entries=entries,
    )
