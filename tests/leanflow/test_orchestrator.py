"""Phase 4 §4.1 tests: the pure deterministic orchestrator floor."""

from __future__ import annotations

from hashlib import sha256

import pytest

from leanflow_cli.workflows import campaign_epoch
from leanflow_cli.workflows.orchestrator import (
    HARD_RETRY_LIMIT,
    PROVER_ROUTE_REASON_MAX_CHARS,
    ROUTES,
    SEMANTIC_REFRESH_ROUTE,
    OrchestratorRoute,
    RouteContext,
    admit_semantically_distinct_route,
    bounded_requested_route_reason,
    build_route_context,
    evidence_supported_negate_request_from_text,
    generated_helper_negation_preflight_due,
    orchestrator_enabled,
    orchestrator_max_routes,
    orchestrator_route,
    requested_route_from_text,
    strategy_directive,
)
from leanflow_cli.workflows.plan_state import Blueprint, GraphEdge, GraphNode, node_id_for
from leanflow_cli.workflows.queue_manager import QueueItem, TheoremKey, TheoremQueueManager


def _ctx(**overrides) -> RouteContext:
    base = dict(
        trigger="scope-entry",
        target_symbol="demo",
        active_file="Demo/Main.lean",
        declaration_queue_total=2,
        attempt_count=0,
    )
    base.update(overrides)
    return RouteContext(**base)


def test_hard_retry_limit_mirrors_native_runner_constant():
    from leanflow_cli.native import native_runner as runner

    assert HARD_RETRY_LIMIT == runner.MANAGER_HARD_RETRY_LIMIT


def test_semantic_admission_rotates_reworded_repeat_to_distinct_family(tmp_path):
    """A renamed planning turn cannot consume another no-progress provider turn."""
    active_file = str(tmp_path / "Main.lean")
    ctx = _ctx(
        active_file=active_file,
        research_mode=True,
        semantic_route_history=(
            {
                "route": "plan",
                "target_symbol": "demo",
                "active_file": active_file,
                "reason": "first optimistic plan",
            },
        ),
    )

    admitted = admit_semantically_distinct_route(
        ctx,
        OrchestratorRoute(route="plan", reason="another optimistic plan, generation 99"),
    )

    assert admitted.route == "decompose"
    assert admitted.source == "deterministic-semantic-admission"
    assert "already attempted" in admitted.reason


def test_semantic_admission_refreshes_after_all_viable_families_are_spent(tmp_path):
    """Spent semantic families force a rollover action, never a parked scope."""
    active_file = str(tmp_path / "Main.lean")
    history = tuple(
        {
            "route": route,
            "target_symbol": "demo",
            "active_file": active_file,
        }
        for route in ("decompose", "negate", "plan")
    )
    ctx = _ctx(
        active_file=active_file,
        research_mode=True,
        semantic_route_history=history,
    )

    admitted = admit_semantically_distinct_route(
        ctx,
        OrchestratorRoute(route="plan", reason="try the same root planning pass"),
    )

    assert admitted.route == SEMANTIC_REFRESH_ROUTE
    assert admitted.route != "park"
    assert "refresh worker findings" in admitted.reason


def test_semantic_admission_allows_new_concrete_hypothesis_in_same_family(tmp_path):
    """A new mathematical target remains admissible inside a familiar route family."""
    active_file = str(tmp_path / "Main.lean")
    ctx = _ctx(
        active_file=active_file,
        semantic_route_history=(
            {
                "route": "plan",
                "target_symbol": "demo",
                "active_file": active_file,
                "target": {"target_hypothesis": "analyze residues modulo 41"},
            },
        ),
    )
    proposed = OrchestratorRoute(
        route="plan",
        reason="new concrete target",
        target={"target_hypothesis": "analyze residues modulo 43"},
    )

    assert admit_semantically_distinct_route(ctx, proposed).route == "plan"


def test_no_progress_semantic_ledger_does_not_forget_older_route_identity(tmp_path):
    """Long refresh campaigns retain the earliest spent identity until graph progress."""
    active_file = str(tmp_path / "Main.lean")
    oldest = {
        "route": "plan",
        "target_symbol": "demo",
        "active_file": active_file,
    }
    observational_refreshes = [
        {
            "route": "refresh-portfolio",
            "target_symbol": "demo",
            "active_file": active_file,
            "semantic_route_key": f"refresh-{index}",
        }
        for index in range(80)
    ]
    ctx = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": active_file,
            },
            campaign_epoch.SEMANTIC_ROUTE_HISTORY_STATE_KEY: [
                oldest,
                *observational_refreshes,
            ],
        },
    )

    admitted = admit_semantically_distinct_route(
        ctx,
        OrchestratorRoute(route="plan", reason="reword the oldest route"),
    )

    assert len(ctx.semantic_route_history) == 81
    assert admitted.route == "decompose"


def test_flags_default_off_and_bounded():
    assert orchestrator_enabled() is False
    assert orchestrator_max_routes() == 4


def test_row1_happy_path_is_passthrough():
    """Property: any live queue item below the hard-retry limit at a
    non-breakpoint trigger routes direct-prove."""
    for trigger in ("scope-entry", "event"):
        for attempts in range(HARD_RETRY_LIMIT):
            route = orchestrator_route(_ctx(trigger=trigger, attempt_count=attempts))
            assert route.route == "direct-prove"
            assert route.source == "deterministic"


def test_high_attempt_scope_entry_and_event_change_strategy():
    for trigger in ("scope-entry", "event"):
        route = orchestrator_route(
            _ctx(trigger=trigger, attempt_count=HARD_RETRY_LIMIT, research_mode=True)
        )
        assert route.route == "decompose"
        assert "repeated rejected attempts" in route.reason

    rotated = orchestrator_route(
        _ctx(
            trigger="event",
            attempt_count=HARD_RETRY_LIMIT,
            research_mode=True,
            current_epoch_routes=("decompose",),
        )
    )
    assert rotated.route == "negate"


def test_deferred_exact_verification_outranks_old_attempt_and_epoch_routes():
    """A newer proof candidate must reach the optimizer before stale persistence work."""
    route = orchestrator_route(
        _ctx(
            attempt_count=29,
            hard_retries=29,
            routes_used_this_scope=4,
            research_mode=True,
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose", "plan", "negate"),
            deferred_exact_verification=True,
        )
    )

    assert route.route == "direct-prove"
    assert "optimize and verify" in route.reason


def test_fresh_epoch_selects_persisted_distinct_non_direct_route():
    direct_epoch = orchestrator_route(
        _ctx(
            attempt_count=20,
            research_mode=True,
            epoch_refresh_required=True,
            previous_epoch_routes=("direct-prove",) * 4,
        )
    )
    assert direct_epoch.route == "decompose"
    assert "fresh epoch" in direct_epoch.reason

    used_decomposition = orchestrator_route(
        _ctx(
            attempt_count=20,
            research_mode=True,
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose",),
        )
    )
    assert used_decomposition.route == "negate"

    conclusive_probe = orchestrator_route(
        _ctx(
            attempt_count=20,
            research_mode=True,
            negation_status="inconclusive",
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose",),
        )
    )
    assert conclusive_probe.route == "plan"


def test_fresh_epoch_retries_inconclusive_negation_before_reusing_route_kind():
    """Live regression: epoch 22 must not label decompose as distinct here."""
    route = orchestrator_route(
        _ctx(
            attempt_count=20,
            research_mode=True,
            negation_status="inconclusive",
            negation_probe_budget_remaining=1,
            negation_refresh_evidence_key="probe-evidence-a",
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose", "plan", "plan", "plan"),
        )
    )

    assert route.route == "negate"
    assert route.route not in {"decompose", "plan"}


def test_fresh_epoch_skips_inconclusive_negation_when_exact_budget_is_spent():
    route = orchestrator_route(
        _ctx(
            attempt_count=20,
            research_mode=True,
            negation_status="inconclusive",
            negation_probe_budget_remaining=0,
            negation_refresh_evidence_key="probe-evidence-a",
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose", "plan", "plan", "decompose"),
        )
    )

    assert route.route == "plan"


def test_fresh_epoch_does_not_reopen_same_inconclusive_negation_twice():
    route = orchestrator_route(
        _ctx(
            attempt_count=20,
            research_mode=True,
            negation_status="inconclusive",
            negation_probe_budget_remaining=1,
            negation_refresh_evidence_key="probe-evidence-a",
            negation_refresh_retry_consumed=True,
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose", "plan", "plan", "plan"),
        )
    )

    assert route.route == "decompose"


def test_current_prover_route_request_precedes_epoch_refresh_route():
    """Honor the newest exact handoff before an older epoch portfolio choice."""
    requested = orchestrator_route(
        _ctx(
            trigger="event",
            requested_route="decompose",
            requested_route_reason=(
                "requested route: decompose; split the remaining residue classes"
            ),
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose",),
            research_mode=True,
        )
    )
    after_request = orchestrator_route(
        _ctx(
            trigger="event",
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose",),
            research_mode=True,
        )
    )

    assert requested.route == "decompose"
    assert requested.target["prover_requested_route"] == "decompose"
    assert after_request.route == "negate"
    assert "fresh epoch" in after_request.reason


def test_explicit_negate_request_falls_back_when_exact_budget_is_spent():
    route = orchestrator_route(
        _ctx(
            trigger="event",
            requested_route="negate",
            negation_status="inconclusive",
            negation_probe_budget_remaining=0,
            current_epoch_routes=("decompose",),
        )
    )

    assert route.route == "plan"
    assert "budget is exhausted" in route.reason


def test_fresh_epoch_diversity_never_overrides_fidelity_pause(monkeypatch):
    monkeypatch.setenv("LEANFLOW_HUMAN_REVIEW_ENABLED", "1")
    route = orchestrator_route(
        _ctx(
            attempt_count=20,
            research_mode=True,
            fidelity_suspect=True,
            target_node_found=True,
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose", "plan", "plan", "plan"),
        )
    )

    assert route.route == "ask-human"


@pytest.mark.parametrize(
    ("report", "expected_route", "expected_reason"),
    [
        ("Requested route: plan.", "plan", "Requested route: plan."),
        ("Requested next route = decompose", "decompose", "Requested next route = decompose"),
        (
            "Requested continuation route: `decompose`, beginning with the finite "
            "subset-sum spacing lemma.",
            "decompose",
            "Requested continuation route: `decompose`, beginning with the finite "
            "subset-sum spacing lemma.",
        ),
        (
            "Requested continuing route: `decompose`, beginning with the finite "
            "subset-sum spacing lemma.",
            "decompose",
            "Requested continuing route: `decompose`, beginning with the finite "
            "subset-sum spacing lemma.",
        ),
        (
            "Requested next route: `plan`, centered on the geometric " "board-construction lemma.",
            "plan",
            "Requested next route: `plan`, centered on the geometric " "board-construction lemma.",
        ),
        ("Route requested: `negate`", "negate", "Route requested: `negate`"),
        (
            "Blocked — requested route: plan/statement revision.",
            "plan",
            "Blocked — requested route: plan/statement revision.",
        ),
        (
            "- Requested route: plan — split residual cases.",
            "plan",
            "- Requested route: plan — split residual cases.",
        ),
        (
            "Stalled: **Requested route:** `NEGATE`\n"
            "Reason: the exact counterexample helper is kernel-verified.\n"
            "This unrelated footer must not become route evidence.",
            "negate",
            "Stalled: **Requested route:** `NEGATE`\n"
            "Reason: the exact counterexample helper is kernel-verified.",
        ),
    ],
)
def test_requested_route_marker_positive_matrix(report, expected_route, expected_reason):
    assert requested_route_from_text(report) == expected_route
    assert bounded_requested_route_reason(report, expected_route) == expected_reason


@pytest.mark.parametrize(
    "report",
    [
        "No requested route: plan.",
        "Do not use the requested route: plan.",
        "The requested route: negate is not valid.",
        "I quote the prior report: requested route: decompose.",
        "Example: requested route: plan.",
        "> Requested route: plan.",
        "```text\nRequested route: plan.\n```",
        "~~~\nRequested route: plan.\n~~~",
        "Requested route: plan or negate.",
        "Requested route: plan. I reject that request.",
        "Requested route: plan — this is not a request.",
        "Requested route: plan — hypothetical marker.",
        "Requested route: plan — copied from the prior report.",
        "Requested route: plan.\nRequested route: negate.",
        "Requested route: park.",
        "The proof is blocked, so route requested: negate.",
        "`Requested route: plan.`",
        "Requested route: plan.\nReason: this route request is invalid.",
    ],
)
def test_requested_route_marker_negative_matrix(report):
    assert requested_route_from_text(report) == ""
    assert bounded_requested_route_reason(report) == ""


def test_route_reason_is_bounded_without_copying_the_surrounding_report():
    report = "\n".join(
        (
            "Unrelated opening analysis.",
            "Requested route: negate; " + ("counterexample " * 500),
            "Unrelated footer.",
        )
    )

    bounded = bounded_requested_route_reason(report, "negate")

    assert len(bounded) == PROVER_ROUTE_REASON_MAX_CHARS
    assert bounded.startswith("Requested route: negate")
    assert "Unrelated opening" not in bounded
    assert "Unrelated footer" not in bounded


def test_route_reason_rejects_an_explicit_route_mismatch():
    assert bounded_requested_route_reason("Requested route: plan.", "negate") == ""


def test_explicit_prover_route_request_outranks_productive_passthrough():

    route = orchestrator_route(
        _ctx(
            requested_route="plan",
            requested_route_reason="requested route: plan; split the residual cases",
            attempt_count=0,
        )
    )

    assert route.route == "plan"
    assert "reported a blocker" in route.reason
    assert route.target["prover_request_reason"].endswith("split the residual cases")


def test_verified_evidence_accepts_affirmative_negated_obstruction_resolution():
    evidence = ({"node_id": "n-counterexample", "name": "demo_counterexample"},)
    report = (
        "Blocked: the assigned statement is false, so it cannot be repaired without "
        "changing its statement. Required resolution: correct or replace the false "
        "candidate statement (for example, route it as a negated obstruction), then "
        "assign the revised declaration."
    )

    assert evidence_supported_negate_request_from_text(report, evidence) == "negate"
    assert evidence_supported_negate_request_from_text(report, ()) == ""


@pytest.mark.parametrize(
    "report",
    [
        "Required resolution: do not route it as a negated obstruction.",
        "Required resolution: cannot route it as a negated obstruction.",
        "Required resolution: can't route this statement as a negated obstruction.",
        "Required resolution: never route this statement as a negated obstruction.",
        "Required resolution: the candidate needs a non-negated obstruction.",
        "Required resolution: route it as a negated obstruction is not valid.",
        "I am not requesting route negate; continue the direct proof.",
    ],
)
def test_verified_evidence_rejects_negated_negate_route_language(report):
    evidence = ({"node_id": "n-counterexample", "name": "demo_counterexample"},)

    assert evidence_supported_negate_request_from_text(report, evidence) == ""


def test_requested_route_cannot_bypass_spent_campaign_refresh():
    route = orchestrator_route(
        _ctx(
            requested_route="plan",
            attempt_count=0,
            routes_used_this_scope=4,
        )
    )

    assert route.route == SEMANTIC_REFRESH_ROUTE
    assert "route budget spent" in route.reason
    assert (
        route.target["campaign_rollover_reason"] == campaign_epoch.ROUTE_NO_PROGRESS_ROLLOVER_REASON
    )


def test_repeated_timeout_decomposition_outranks_spent_campaign_refresh():
    """New exact-timeout evidence must reach the splitter before another rollover."""
    ctx = _ctx(
        requested_route="decompose",
        requested_route_reason=(
            "Reason: repeated verification timeouts on a sorry-free declaration require "
            "structural recovery through cohesive top-level helpers"
        ),
        routes_used_this_scope=4,
        semantic_route_history=tuple(
            {
                "route": route,
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
            }
            for route in ("decompose", "negate", "plan")
        ),
    )

    proposed = orchestrator_route(ctx)
    admitted = admit_semantically_distinct_route(ctx, proposed)

    assert proposed.route == "decompose"
    assert proposed.source == "deterministic-timeout-recovery"
    assert proposed.target["timeout_decomposition_recovery"] is True
    assert admitted == proposed


def test_builder_accepts_only_current_assignment_route_request(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    assignment = {"target_symbol": "demo", "active_file": str(active)}

    current = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": assignment,
            "prover_requested_route": {**assignment, "route": "decompose"},
        },
    )
    stale = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": assignment,
            "prover_requested_route": {
                "target_symbol": "other",
                "active_file": str(active),
                "route": "plan",
            },
        },
    )

    assert current.requested_route == "decompose"
    assert stale.requested_route == ""


def test_verified_exact_target_counterexample_evidence_routes_negate(tmp_path, monkeypatch):
    """A proved evidence helper must outrank retries even after scratch budget."""
    monkeypatch.setenv("LEANFLOW_NEGATION_PROBE_BUDGET", "1")
    active = str(tmp_path / "Demo.lean")
    target_id = node_id_for("demo", active)
    helper_id = node_id_for("demo_counterexample", active)
    parent_id = node_id_for("parent_demo", active)
    declaration = "private lemma demo_counterexample : ¬ ((5 : Nat) < 5) := by\n" "  omega"
    blueprint = Blueprint(
        nodes=(
            GraphNode(
                id=target_id,
                name="demo",
                file=active,
                statement="theorem demo : ∀ n : Nat, n < 5 := by\n  sorry",
                status="proving",
            ),
            GraphNode(
                id=helper_id,
                name="demo_counterexample",
                file=active,
                statement=declaration,
                status="proved",
                generated_by="prover-edit",
            ),
            GraphNode(
                id=parent_id,
                name="parent_demo",
                file=active,
                status="proving",
            ),
        ),
        edges=(
            GraphEdge(source=helper_id, target=target_id, kind="evidence"),
            GraphEdge(source=target_id, target=parent_id, kind="split_of"),
        ),
    )
    finding = {
        "job_id": "campaign.ds-1",
        "target_symbol": "demo",
        "active_file": active,
        "deliverable": {
            "counterexample": {"n": 5},
            "checked_helper_status": "worker_checked_parent_recheck_required",
            "parent_recheck_required": True,
            "checked_helpers": [
                {
                    "anchor_target_symbol": "demo",
                    "active_file": active,
                    "declaration": declaration,
                    "declaration_sha256": sha256(declaration.encode()).hexdigest(),
                    "parent_recheck_required": True,
                    "worker_check": {
                        "tool": "lean_incremental_check",
                        "action": "check_helper",
                        "valid_without_sorry": True,
                        "has_errors": False,
                        "has_sorry": False,
                        "verification_scope": "helper_candidate",
                        "replacement_matches_target": False,
                        "replacement_declarations": ["demo_counterexample"],
                    },
                }
            ],
        },
    }

    ctx = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": active,
                "slice": "theorem demo : ∀ n : Nat, n < 5 := by\n  sorry",
            }
        },
        blueprint=blueprint,
        summary={
            "research_findings": [finding],
            "negation_probes": [
                {
                    "key": TheoremKey.make("demo", active).storage_key(),
                    "negation": {"verdict": "inconclusive"},
                }
            ],
        },
    )
    route = orchestrator_route(ctx)

    assert ctx.target_is_sublemma is True
    assert ctx.negation_probe_budget_remaining == 0
    assert [item["node_id"] for item in ctx.verified_counterexample_evidence] == [helper_id]
    assert route.route == "negate"
    assert route.target["verified_counterexample_evidence"] == [helper_id]
    assert route.target["source_negation_recovery_only"] is True
    assert route.target["target_symbol"] == "demo"


def test_negative_characterization_is_not_counterexample_evidence(tmp_path):
    """A theorem about `not target` does not prove the target false."""
    active = str(tmp_path / "Demo.lean")
    target_id = node_id_for("demo", active)
    helper_id = node_id_for("not_demo_iff_condition", active)
    blueprint = Blueprint(
        nodes=(
            GraphNode(
                id=target_id,
                name="demo",
                file=active,
                statement="theorem demo : True := by\n  sorry",
                status="proving",
            ),
            GraphNode(
                id=helper_id,
                name="not_demo_iff_condition",
                file=active,
                statement=(
                    "private lemma not_demo_iff_condition : " "(¬ True) ↔ False := by\n  simp"
                ),
                status="proved",
            ),
        ),
        edges=(GraphEdge(source=helper_id, target=target_id, kind="evidence"),),
    )

    ctx = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": active,
            }
        },
        blueprint=blueprint,
    )

    assert ctx.verified_counterexample_evidence == ()
    assert orchestrator_route(ctx).route == "direct-prove"


def test_negative_support_fact_is_not_counterexample_evidence(tmp_path):
    """A negative-looking support lemma does not itself refute the target."""
    active = str(tmp_path / "Demo.lean")
    target_id = node_id_for("demo", active)
    helper_id = node_id_for("support_avoids_multiple", active)
    blueprint = Blueprint(
        nodes=(
            GraphNode(
                id=target_id,
                name="demo",
                file=active,
                statement="theorem demo : {x : Nat | x = 1} = {1} := by\n  sorry",
                status="proving",
            ),
            GraphNode(
                id=helper_id,
                name="support_avoids_multiple",
                file=active,
                statement=(
                    "private lemma support_avoids_multiple : "
                    "∀ z : Int, (3 : Int) ≠ z * 2 := by\n  omega"
                ),
                status="proved",
            ),
        ),
        edges=(GraphEdge(source=helper_id, target=target_id, kind="evidence"),),
    )

    ctx = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": active,
            }
        },
        blueprint=blueprint,
    )

    assert ctx.verified_counterexample_evidence == ()
    assert orchestrator_route(ctx).route == "direct-prove"


def test_direct_target_negation_is_counterexample_evidence(tmp_path):
    """An exact proved negation remains authenticated without name heuristics."""
    active = str(tmp_path / "Demo.lean")
    target_id = node_id_for("demo", active)
    helper_id = node_id_for("impossible_case", active)
    blueprint = Blueprint(
        nodes=(
            GraphNode(
                id=target_id,
                name="demo",
                file=active,
                statement="theorem demo : True := by\n  sorry",
                status="proving",
            ),
            GraphNode(
                id=helper_id,
                name="impossible_case",
                file=active,
                statement="private lemma impossible_case : ¬ True := by\n  simp",
                status="proved",
            ),
        ),
        edges=(GraphEdge(source=helper_id, target=target_id, kind="evidence"),),
    )

    ctx = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": active,
            }
        },
        blueprint=blueprint,
    )

    assert [item["node_id"] for item in ctx.verified_counterexample_evidence] == [helper_id]


def test_named_exact_negation_with_arrow_form_routes_as_counterexample_evidence(tmp_path):
    """Route a checked negative helper even when binder syntax defeats text identity."""
    active = str(tmp_path / "Demo.lean")
    target_id = node_id_for("demo", active)
    helper_id = node_id_for("audit_exact_demo_negation", active)
    blueprint = Blueprint(
        nodes=(
            GraphNode(
                id=target_id,
                name="demo",
                file=active,
                statement=("theorem demo {a : Nat} (h : 0 < a) : a = a := by\n" "  sorry"),
                status="proving",
            ),
            GraphNode(
                id=helper_id,
                name="audit_exact_demo_negation",
                file=active,
                statement=(
                    "private lemma audit_exact_demo_negation : "
                    "¬ (∀ {a : Nat}, 0 < a → a = a) := by\n"
                    "  sorry"
                ),
                status="proved",
                generated_by="prover-edit",
            ),
        ),
        edges=(GraphEdge(source=helper_id, target=target_id, kind="evidence"),),
    )

    ctx = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": active,
            }
        },
        blueprint=blueprint,
    )

    assert [item["node_id"] for item in ctx.verified_counterexample_evidence] == [helper_id]
    assert orchestrator_route(ctx).route == "negate"


def test_explicit_negate_with_verified_evidence_survives_spent_scratch_budget():
    evidence = ({"node_id": "n-counterexample", "name": "demo_counterexample"},)

    route = orchestrator_route(
        _ctx(
            trigger="event",
            requested_route="negate",
            requested_route_reason="requested route: negate; checked helper exists",
            verified_counterexample_evidence=evidence,
            negation_status="inconclusive",
            negation_probe_budget_remaining=0,
        )
    )

    assert route.route == "negate"
    assert route.target["source_negation_recovery_only"] is True
    assert route.target["verified_counterexample_evidence"] == ["n-counterexample"]


@pytest.mark.parametrize("status", ["stated", "proving"])
def test_unverified_counterexample_helper_cannot_control_route(tmp_path, status):
    active = str(tmp_path / "Demo.lean")
    target_id = node_id_for("demo", active)
    helper_id = node_id_for("demo_counterexample", active)
    blueprint = Blueprint(
        nodes=(
            GraphNode(id=target_id, name="demo", file=active, status="proving"),
            GraphNode(
                id=helper_id,
                name="demo_counterexample",
                file=active,
                statement="lemma demo_counterexample : ¬ True := by simp",
                status=status,
            ),
        ),
        edges=(GraphEdge(source=helper_id, target=target_id, kind="evidence"),),
    )

    ctx = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": active,
            }
        },
        live_state={"blocker_summary": "Blocked: requested route negate"},
        blueprint=blueprint,
    )

    assert ctx.verified_counterexample_evidence == ()
    assert orchestrator_route(ctx).route == "direct-prove"


def test_prose_alone_cannot_upgrade_ordinary_verified_evidence_to_negate(tmp_path):
    active = str(tmp_path / "Demo.lean")
    target_id = node_id_for("demo", active)
    helper_id = node_id_for("support_evidence", active)
    blueprint = Blueprint(
        nodes=(
            GraphNode(id=target_id, name="demo", file=active, status="proving"),
            GraphNode(
                id=helper_id,
                name="support_evidence",
                file=active,
                statement="lemma support_evidence : True := by trivial",
                status="proved",
            ),
        ),
        edges=(GraphEdge(source=helper_id, target=target_id, kind="evidence"),),
    )

    ctx = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": active,
            }
        },
        live_state={"blocker_summary": "Blocked: requested route negate"},
        blueprint=blueprint,
    )

    assert ctx.verified_counterexample_evidence == ()
    assert (
        evidence_supported_negate_request_from_text(
            "Blocked: requested route negate",
            ctx.verified_counterexample_evidence,
        )
        == ""
    )
    assert orchestrator_route(ctx).route == "direct-prove"


def test_builder_hydrates_fresh_epoch_route_obligation(tmp_path):
    active = tmp_path / "Demo.lean"
    ctx = build_route_context(
        trigger="scope-entry",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            },
            "campaign_epoch_route_refresh": {
                "required": True,
                "previous_routes": ["direct-prove", "plan"],
            },
            "campaign_epoch_routes": [{"route": "decompose"}],
        },
    )

    assert ctx.epoch_refresh_required is True
    assert ctx.previous_epoch_routes == ("direct-prove", "plan")
    assert ctx.current_epoch_routes == ("decompose",)


def test_builder_includes_assigned_declaration_slice(tmp_path):
    ctx = build_route_context(
        trigger="scope-entry",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "sphere_goal",
                "active_file": str(tmp_path / "Main.lean"),
                "slice": "theorem sphere_goal : ∃ x : ℝ, x = x := by sorry",
            }
        },
    )

    assert "theorem sphere_goal" in ctx.target_statement


def test_builder_counts_deferred_route_as_unresolved_work(tmp_path):
    active = tmp_path / "Main.lean"
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="hard_demo"), active_file=str(active))
    mgr.record_outcome(status="deferred", note="direct route exhausted")

    ctx = build_route_context(trigger="event", mgr=mgr)

    assert ctx.unresolved_outcomes == 1


def test_row2_breakpoint_with_search_exhausted_decomposes():
    route = orchestrator_route(
        _ctx(trigger="budget-breakpoint", attempt_count=3, search_exhausted=True)
    )
    assert route.route == "decompose"
    assert route.target["target_symbol"] == "demo"


def test_row3_breakpoint_without_probe_verdict_negates():
    route = orchestrator_route(
        _ctx(
            trigger="budget-breakpoint",
            attempt_count=3,
            search_exhausted=False,
            negation_status="not-attempted",
        )
    )
    assert route.route == "negate"

    # A conclusive probe verdict removes the negate row.
    after_probe = orchestrator_route(
        _ctx(
            trigger="budget-breakpoint",
            attempt_count=3,
            search_exhausted=True,
            negation_status="inconclusive",
        )
    )
    assert after_probe.route == "decompose"


def test_generated_helper_gets_one_research_negation_preflight():
    ctx = _ctx(
        research_mode=True,
        target_generated_by="decomposer",
        negation_status="not-attempted",
        negation_probe_budget_remaining=1,
    )
    route = orchestrator_route(ctx)

    assert generated_helper_negation_preflight_due(ctx)
    assert route.route == "negate"
    assert route.target["generated_by"] == "decomposer"

    after_probe = orchestrator_route(
        _ctx(
            research_mode=True,
            target_generated_by="decomposer",
            negation_status="inconclusive",
            negation_probe_budget_remaining=1,
        )
    )
    assert after_probe.route == "direct-prove"


def test_generated_helper_preflight_outranks_stale_plan_and_epoch_routes():
    route = orchestrator_route(
        _ctx(
            research_mode=True,
            target_generated_by="planner",
            negation_status="not-attempted",
            negation_probe_budget_remaining=1,
            requested_route="plan",
            requested_route_reason="old interrupted worker request",
            epoch_refresh_required=True,
            previous_epoch_routes=("decompose", "plan"),
            routes_used_this_scope=99,
        )
    )

    assert route.route == "negate"
    assert route.target["generated_by"] == "planner"


@pytest.mark.parametrize(
    ("research_mode", "generated_by"),
    [(False, "decomposer"), (True, ""), (True, "queue-sync")],
)
def test_source_or_nonresearch_items_skip_generated_helper_preflight(
    research_mode,
    generated_by,
):
    route = orchestrator_route(
        _ctx(
            research_mode=research_mode,
            target_generated_by=generated_by,
            negation_status="not-attempted",
            negation_probe_budget_remaining=1,
        )
    )

    assert route.route == "direct-prove"


def test_row3_breakpoint_skips_negation_when_exact_probe_budget_is_spent():
    route = orchestrator_route(
        _ctx(
            trigger="budget-breakpoint",
            attempt_count=3,
            search_exhausted=False,
            negation_status="not-attempted",
            negation_probe_budget_remaining=0,
        )
    )

    assert route.route == "decompose"


def test_row4_false_sublemma_restates():
    route = orchestrator_route(
        _ctx(trigger="event", target_node_status="false", target_is_sublemma=True)
    )
    assert route.route == "re-state"

    revalidated_root = orchestrator_route(
        _ctx(trigger="event", negation_proved=True, target_is_sublemma=True)
    )
    assert revalidated_root.route == "escalate"


def test_row5_main_goal_negation_escalates_as_disproof():
    route = orchestrator_route(
        _ctx(
            trigger="event",
            negation_proved=True,
            target_node_found=True,
            target_is_sublemma=False,
        )
    )
    assert route.route == "escalate"
    assert "disproved" in route.reason


def test_revalidated_requested_root_does_not_depend_on_mutable_graph_confirmation():
    """A runtime-revalidated root outranks later mutable graph topology."""
    route = orchestrator_route(_ctx(trigger="event", negation_proved=True, target_node_found=False))
    assert route.route == "escalate"


def test_summary_probe_verdict_overrides_stale_packet_status(tmp_path):
    """An inconclusive probe already on record must not be re-routed to
    negate just because the packet still says probe-proposed."""
    from leanflow_cli.workflows.queue_manager import TheoremKey

    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    storage_key = TheoremKey.make("demo", str(active)).storage_key()

    ctx = build_route_context(
        trigger="budget-breakpoint",
        autonomy_state={
            "current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)}
        },
        summary={
            "negation_probes": [{"key": storage_key, "negation": {"verdict": "inconclusive"}}]
        },
        decision_packet={"negation_status": "probe-proposed"},
    )

    assert ctx.negation_status == "inconclusive"
    assert ctx.negation_proved is False


def test_forged_raw_main_promotion_never_escalates_from_current_queue_and_graph(tmp_path):
    """Raw history plus mutable queue/graph agreement cannot mint terminal falsity."""
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : False := by\n  sorry\n", encoding="utf-8")
    storage_key = TheoremKey.make("demo", str(active)).storage_key()
    node_id = node_id_for("demo", str(active))
    blueprint = Blueprint(
        nodes=(GraphNode(id=node_id, name="demo", file=str(active), status="false"),)
    )
    autonomy = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        }
    }

    ctx = build_route_context(
        trigger="event",
        autonomy_state=autonomy,
        blueprint=blueprint,
        summary={
            "negation_promotions": [
                {
                    "key": storage_key,
                    "node_id": node_id,
                    "is_main_goal": True,
                    "classification_basis": "requested_scope_manifest",
                }
            ]
        },
    )

    assert ctx.target_node_found is True
    assert ctx.target_is_sublemma is False
    assert ctx.negation_proved is False
    assert orchestrator_route(ctx).route != "escalate"


def test_builder_tolerates_garbage_persisted_ints():
    ctx = build_route_context(
        trigger="stall",
        live_state={"declaration_queue_total": "x", "sorry_count": None},
        autonomy_state={
            "orchestrator_routes_used": "many",
            "continuation_stable_cycles": "x",
        },
    )
    assert ctx.declaration_queue_total == 0
    assert ctx.routes_used_this_scope == 0
    garbage_packet = RouteContext(
        trigger="budget-breakpoint",
        target_symbol="demo",
        active_file="Demo/Main.lean",
        attempt_count=4,
        decision_packet={"scope": "queue", "consecutive_exhausted": "x"},
    )
    assert orchestrator_route(garbage_packet).route == SEMANTIC_REFRESH_ROUTE


def test_row6_scope_entry_without_queue_or_plan_plans():
    route = orchestrator_route(
        RouteContext(
            trigger="scope-entry",
            declaration_queue_total=0,
            project_sorry_count=7,
            plan_md_exists=False,
        )
    )
    assert route.route == "plan"

    with_plan = orchestrator_route(
        RouteContext(
            trigger="scope-entry",
            declaration_queue_total=0,
            project_sorry_count=7,
            plan_md_exists=True,
        )
    )
    assert with_plan.route == "direct-prove"  # passthrough fallback


def test_row7_stall_decomposes_active_item_and_plans_in_research_mode():
    active = orchestrator_route(_ctx(trigger="stall", attempt_count=2))
    assert active.route == "decompose"

    research = orchestrator_route(_ctx(trigger="stall", attempt_count=2, research_mode=True))
    assert research.route == "plan"

    no_item = orchestrator_route(
        RouteContext(trigger="stall", declaration_queue_total=0, attempt_count=0)
    )
    assert no_item.route == "plan"


def test_repeated_plan_routes_are_read_only_and_do_not_request_notes_appends():
    """Repeated plan decisions must not grow the user-owned Notes tail."""
    ctx = _ctx(trigger="stall", attempt_count=2, research_mode=True)
    route = orchestrator_route(ctx)

    directives = [strategy_directive(route, ctx) for _ in range(2)]

    assert directives[0] == directives[1]
    assert "read-only generated plan view" in directives[0]
    assert "do not edit managed plan.md" in directives[0]
    assert "append" not in directives[0]
    assert "Notes" not in directives[0]


def test_row8_prove_route_budget_and_queue_breakpoint_refresh():
    spent = orchestrator_route(_ctx(trigger="stall", attempt_count=4, routes_used_this_scope=4))
    assert spent.route == SEMANTIC_REFRESH_ROUTE

    queue_scope = orchestrator_route(
        _ctx(
            trigger="budget-breakpoint",
            attempt_count=4,
            decision_packet={"scope": "queue", "consecutive_exhausted": 3},
        )
    )
    assert queue_scope.route == SEMANTIC_REFRESH_ROUTE
    assert "consecutive" in queue_scope.reason

    custom_limit = orchestrator_route(
        _ctx(trigger="stall", attempt_count=4, routes_used_this_scope=2), max_routes=2
    )
    assert custom_limit.route == SEMANTIC_REFRESH_ROUTE

    non_prover = orchestrator_route(
        _ctx(
            workflow_kind="formalize",
            trigger="stall",
            attempt_count=4,
            routes_used_this_scope=4,
        )
    )
    assert non_prover.route == "park"


def test_breakpoint_fallthrough_changes_strategy():
    low_attempts = orchestrator_route(
        _ctx(trigger="retry-exhausted", attempt_count=1, search_exhausted=False)
    )
    assert low_attempts.route == "decompose"

    no_assignment = orchestrator_route(
        RouteContext(trigger="budget-breakpoint", declaration_queue_total=0)
    )
    assert no_assignment.route == "plan"


def test_every_emitted_route_is_in_the_vocabulary():
    contexts = [
        _ctx(),
        _ctx(trigger="stall", attempt_count=3),
        _ctx(trigger="budget-breakpoint", attempt_count=3, search_exhausted=True),
        _ctx(trigger="budget-breakpoint", attempt_count=3),
        _ctx(trigger="event", negation_proved=True),
        RouteContext(trigger="scope-entry"),
    ]
    for ctx in contexts:
        assert orchestrator_route(ctx).route in ROUTES


def test_build_route_context_is_total_on_empty_inputs():
    ctx = build_route_context(trigger="scope-entry")
    assert ctx.trigger == "scope-entry"
    assert ctx.has_queue_item() is False
    assert orchestrator_route(ctx).route in ROUTES

    weird = build_route_context(trigger="not-a-trigger")
    assert weird.trigger == "event"


def test_build_route_context_identifies_deferred_exact_verification():
    ctx = build_route_context(
        trigger="scope-entry",
        live_state={
            "active_file": "Demo.lean",
            "target_symbol": "demo",
            "proof_state_authority": "source_only_unverified",
            "defer_incremental_warmup": True,
            "sorry_count": 0,
        },
    )

    assert ctx.deferred_exact_verification is True
    assert orchestrator_route(ctx).route == "direct-prove"


def test_build_route_context_reads_queue_graph_and_negation(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label="demo", reasons=("contains sorry",)), active_file=str(active))
    mgr.record_attempt(cycle=1, proof_shape="direct", reason="type mismatch")
    mgr.record_attempt(cycle=2, proof_shape="direct2", reason="type mismatch")

    node_id = node_id_for("demo", str(active))
    blueprint = Blueprint(
        nodes=(
            GraphNode(
                id=node_id,
                name="demo",
                file=str(active),
                status="false",
                generated_by="decomposer",
            ),
            GraphNode(id="n-parent", name="main", file=str(active), status="stated"),
        ),
        edges=(GraphEdge(source=node_id, target="n-parent", kind="split_of"),),
    )
    storage_key = TheoremKey.make("demo", str(active)).storage_key()
    summary = {
        "negation_probes": [
            {
                "key": storage_key,
                "negation": {"verdict": "negation_proved", "axioms_ok": True},
            }
        ],
        "negation_promotions": [{"key": storage_key, "node_id": node_id, "is_main_goal": False}],
        "dispatch_ledger": [
            {
                "spec": {
                    "job_id": "campaign.ds-001",
                    "inputs": {"target_symbol": "demo", "active_file": str(active)},
                }
            }
        ],
        "research_findings": [
            {
                "job_id": "campaign.ds-001",
                "deliverable": {"summary": "use helper invariant"},
            }
        ],
    }

    ctx = build_route_context(
        trigger="event",
        live_state={"active_file": str(active), "search_exhausted": True},
        autonomy_state={
            "current_queue_assignment": {"target_symbol": "demo", "active_file": str(active)},
            "orchestrator_routes_used": 1,
            "continuation_stable_cycles": 2,
        },
        mgr=mgr,
        blueprint=blueprint,
        summary=summary,
        decision_packet={"negation_status": "probe-proposed"},
    )

    assert ctx.attempt_count == 2
    assert ctx.target_is_sublemma is True
    assert ctx.target_generated_by == "decomposer"
    assert ctx.negation_proved is False
    assert ctx.search_exhausted is True
    assert ctx.routes_used_this_scope == 1
    assert ctx.research_findings[0]["job_id"] == "campaign.ds-001"
    assert ctx.graph_frontier == ()
    assert "main" in ctx.graph_unrelated_frontier
    # Authenticated graph falsity for a sublemma re-routes without treating a
    # raw promotion row as terminal authority.
    assert orchestrator_route(ctx).route == "re-state"


def test_build_route_context_banks_newly_proved_dependency_coverage(tmp_path):
    active = tmp_path / "242.lean"
    active.write_text("-- fixture\n", encoding="utf-8")
    target = "residual_mod_seven_eq_five"
    helper = "residual_five_easy_mod_five"
    target_id = node_id_for(target, str(active))
    helper_id = node_id_for(helper, str(active))
    helper_statement = (
        "(t : ℕ) (hcase : t % 5 = 2 ∨ t % 5 = 3 ∨ t % 5 = 4) : "
        "∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧ "
        "(4 / ((168 * t + 121 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z"
    )
    blueprint = Blueprint(
        nodes=(
            GraphNode(id=target_id, name=target, file=str(active), status="proving"),
            GraphNode(
                id=helper_id,
                name=helper,
                file=str(active),
                statement=helper_statement,
                status="proved",
            ),
        ),
        edges=(
            GraphEdge(source=target_id, target=helper_id, kind="depends_on"),
            GraphEdge(source=helper_id, target=target_id, kind="split_of"),
        ),
    )
    mgr = TheoremQueueManager()
    mgr.assign(QueueItem(label=target), active_file=str(active))
    mgr.record_attempt(
        cycle=1,
        proof_shape="fixed-x factor pair with q = 3",
        reason="covered only one residue family",
    )

    ctx = build_route_context(
        trigger="scope-entry",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": target,
                "active_file": str(active),
            }
        },
        mgr=mgr,
        blueprint=blueprint,
    )

    assert ctx.verified_graph_facts == (
        {
            "node_id": helper_id,
            "name": helper,
            "statement": helper_statement,
            "notes": "",
            "relationship": "direct-dependency",
            "route_compatibility": "unverified-target-conclusion",
        },
    )
    assert ctx.failed_route_signatures == (
        "fixed-x factor pair with q = 3 | covered only one residue family",
    )


def test_builder_separates_target_dependencies_from_same_file_graph_nodes(tmp_path):
    """Regression: similarly named residue helpers for another denominator are not dependencies."""
    active = tmp_path / "242.lean"
    active.write_text("-- fixture\n", encoding="utf-8")
    target = "erdos_242_residual_mod_seven_eq_zero"
    target_id = node_id_for(target, str(active))
    exact_helper = "erdos_242_residual_zero_exact_helper"
    exact_helper_id = node_id_for(exact_helper, str(active))
    unrelated_helper = "erdos_242_residual_five_mod_five_eq_zero"
    unrelated_helper_id = node_id_for(unrelated_helper, str(active))
    unrelated_frontier = "erdos_242_residual_five_mod_five_eq_one"
    unrelated_frontier_id = node_id_for(unrelated_frontier, str(active))
    target_statement = (
        "private lemma erdos_242_residual_mod_seven_eq_zero (t : ℕ) : "
        "∃ x y z : ℕ, (4 / ((168 * t + 1 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z"
    )
    blueprint = Blueprint(
        nodes=(
            GraphNode(
                id=target_id,
                name=target,
                file=str(active),
                statement=target_statement,
                status="proving",
            ),
            GraphNode(
                id=exact_helper_id,
                name=exact_helper,
                file=str(active),
                statement=target_statement.replace(target, exact_helper),
                status="proved",
            ),
            GraphNode(
                id=unrelated_helper_id,
                name=unrelated_helper,
                file=str(active),
                statement=(
                    f"private lemma {unrelated_helper} (t : ℕ) : ∃ x y z : ℕ, "
                    "(4 / ((168 * t + 121 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z"
                ),
                status="proved",
            ),
            GraphNode(
                id=unrelated_frontier_id,
                name=unrelated_frontier,
                file=str(active),
                status="stated",
            ),
        ),
        edges=(GraphEdge(source=target_id, target=exact_helper_id, kind="depends_on"),),
    )

    ctx = build_route_context(
        trigger="event",
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": target,
                "active_file": str(active),
                "slice": target_statement,
            }
        },
        blueprint=blueprint,
    )

    assert ctx.graph_frontier == ()
    assert ctx.graph_unrelated_frontier == (unrelated_frontier,)
    facts = {str(fact["name"]): fact for fact in ctx.verified_graph_facts}
    assert facts[exact_helper]["route_compatibility"] == "exact-target-conclusion"
    assert facts[unrelated_helper]["relationship"] == "same-file-proved-unrelated"
    assert facts[unrelated_helper]["route_compatibility"] == "different-target-conclusion"


def test_scratch_negation_without_promotion_is_not_authoritative(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text("theorem demo : False := by\n  sorry\n", encoding="utf-8")
    node_id = node_id_for("demo", str(active))
    blueprint = Blueprint(
        nodes=(GraphNode(id=node_id, name="demo", file=str(active), status="proving"),)
    )
    storage_key = TheoremKey.make("demo", str(active)).storage_key()
    ctx = build_route_context(
        trigger="event",
        live_state={"active_file": str(active)},
        autonomy_state={
            "current_queue_assignment": {
                "target_symbol": "demo",
                "active_file": str(active),
            }
        },
        mgr=TheoremQueueManager(),
        blueprint=blueprint,
        summary={
            "negation_probes": [
                {
                    "key": storage_key,
                    "negation": {"verdict": "negation_proved", "axioms_ok": True},
                }
            ]
        },
    )

    assert ctx.negation_status == "negation_proved"
    assert ctx.negation_proved is False
    assert orchestrator_route(ctx).route != "escalate"


@pytest.mark.parametrize("route", ROUTES)
def test_route_dataclass_accepts_vocabulary(route):
    decision = OrchestratorRoute(route=route, reason="r")
    assert decision.route == route
