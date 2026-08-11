"""Tests for non-authoritative source-negation candidate ordering."""

from collections.abc import Iterable
from typing import Protocol

from leanflow_cli.workflows import source_negation_candidates


class _Named(Protocol):
    name: str


def _names(ranked: Iterable[_Named]) -> tuple[str, ...]:
    """Return candidate names from one ranking result."""
    return tuple(candidate.name for candidate in ranked)


def test_eager_helper_promotion_requires_exact_counterexample_provenance(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
        }
    }

    assert (
        source_negation_candidates.eager_helper_promotion_provenance(
            state,
            target_symbol="demo",
            active_file=str(active),
            proof_declaration="ordinary_positive_helper",
        )
        == ""
    )
    assert (
        source_negation_candidates.eager_helper_promotion_provenance(
            state,
            target_symbol="demo",
            active_file=str(active),
            proof_declaration="unexpected_name",
            exact_counterexample_names=("unexpected_name",),
        )
        == "verified-counterexample-evidence"
    )

    state["campaign_inflight_route"] = {
        "route": "negate",
        "target_symbol": "demo",
        "active_file": str(active),
    }
    state["campaign_epoch_route_selection"] = dict(state["campaign_inflight_route"])
    state["prover_requested_route"] = dict(state["campaign_inflight_route"])
    state["orchestrator_current_route"] = "negate"
    assert not source_negation_candidates.eager_helper_promotion_provenance(
        state,
        target_symbol="demo",
        active_file=str(active),
        proof_declaration="ordinary_positive_helper",
    )


def test_eager_helper_promotion_rejects_stale_counterexample_scope(tmp_path) -> None:
    active = tmp_path / "Main.lean"
    state = {
        "current_queue_assignment": {
            "target_symbol": "other",
            "active_file": str(active),
        },
        "prover_requested_route": {
            "route": "negate",
            "target_symbol": "demo",
            "active_file": str(active),
        },
    }

    assert not source_negation_candidates.eager_helper_promotion_provenance(
        state,
        target_symbol="demo",
        active_file=str(active),
        proof_declaration="not_demo",
        exact_counterexample_names=("not_demo",),
    )


def test_exact_target_evidence_precedes_unrelated_negative_names() -> None:
    target = "erdos_242_mod_five_two_witness_candidate"
    unrelated = "erdos_242_exceptional_family_169_not_dvd_small_primes"
    synthetic = f"{target}_at_two_impossible"
    exact = f"{target}_counterexample"

    ranked = source_negation_candidates.rank_source_negation_candidates(
        (unrelated, synthetic, exact),
        target_symbol=target,
        exact_scope_evidence_names=(exact,),
    )

    assert _names(ranked) == (exact, synthetic, unrelated)
    assert ranked[0].rank_reason == "exact-target-graph-evidence"
    assert ranked[1].rank_reason == "target-derived-helper-name"
    assert ranked[2].rank_reason == "target-name-affinity"


def test_short_target_derived_helpers_precede_synthetic_specializations() -> None:
    ranked = source_negation_candidates.rank_source_negation_candidates(
        (
            "unrelated_not_obstruction",
            "demo_at_one_impossible",
            "demo_counterexample",
            "not_demo",
        ),
        target_symbol="demo",
    )

    assert _names(ranked) == (
        "demo_counterexample",
        "not_demo",
        "demo_at_one_impossible",
        "unrelated_not_obstruction",
    )
    assert all(candidate.target_derived_name for candidate in ranked[:3])


def test_unqualified_graph_evidence_matches_the_qualified_candidate() -> None:
    ranked = source_negation_candidates.rank_source_negation_candidates(
        ("Erdos.demo_counterexample", "Erdos.demo_impossible"),
        target_symbol="Erdos.demo",
        exact_scope_evidence_names=("demo_impossible",),
    )

    assert _names(ranked) == (
        "Erdos.demo_impossible",
        "Erdos.demo_counterexample",
    )
    assert ranked[0].exact_scope_evidence is True


def test_ranking_retains_every_potential_candidate_beyond_old_limit() -> None:
    candidates = tuple(f"helper_{index}_not_case" for index in range(24))

    ranked = source_negation_candidates.rank_source_negation_candidates(
        candidates,
        target_symbol="demo",
    )

    assert _names(ranked) == candidates
    assert len(ranked) == 24


def test_namespace_mismatch_cannot_claim_target_derived_affinity() -> None:
    ranked = source_negation_candidates.rank_source_negation_candidates(
        ("Other.demo_counterexample", "Erdos.unrelated_not_case"),
        target_symbol="Erdos.demo",
    )

    assert _names(ranked) == (
        "Other.demo_counterexample",
        "Erdos.unrelated_not_case",
    )
    assert ranked[0].target_derived_name is False
    assert ranked[0].rank_reason == "generic-negation-name"


def test_generic_tail_advances_in_bounded_batches_after_rejections(monkeypatch) -> None:
    monkeypatch.setattr(
        source_negation_candidates.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    state: dict[str, object] = {}
    revision = "a" * 64
    ranked = source_negation_candidates.rank_source_negation_candidates(
        tuple(f"helper_{index}_not_case" for index in range(10)),
        target_symbol="demo",
    )

    first = source_negation_candidates.select_candidate_batch(
        ranked,
        state=state,
        scope_key="Main.lean::demo",
        source_revision_sha256=revision,
        generic_limit=4,
    )
    assert _names(first.candidates) == tuple(f"helper_{index}_not_case" for index in range(4))
    assert first.deferred_generic_count == 6

    for candidate in first.candidates:
        source_negation_candidates.record_definitive_incompatibility(
            state,
            scope_key="Main.lean::demo",
            source_revision_sha256=revision,
            scheduled=candidate,
        )

    second = source_negation_candidates.select_candidate_batch(
        ranked,
        state=state,
        scope_key="Main.lean::demo",
        source_revision_sha256=revision,
        generic_limit=4,
    )
    assert _names(second.candidates) == tuple(f"helper_{index}_not_case" for index in range(4, 8))
    assert second.previously_rejected_count == 4
    assert second.deferred_generic_count == 2


def test_exact_candidates_are_not_charged_to_generic_batch_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        source_negation_candidates.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    ranked = source_negation_candidates.rank_source_negation_candidates(
        (
            "demo_counterexample",
            "not_demo",
            "unrelated_not_one",
            "unrelated_not_two",
        ),
        target_symbol="demo",
    )

    batch = source_negation_candidates.select_candidate_batch(
        ranked,
        state={},
        scope_key="Main.lean::demo",
        source_revision_sha256="b" * 64,
        generic_limit=1,
    )

    assert _names(batch.candidates) == (
        "demo_counterexample",
        "not_demo",
        "unrelated_not_one",
    )
    assert batch.deferred_generic_count == 1


def test_source_revision_change_reopens_previously_rejected_candidate(monkeypatch) -> None:
    monkeypatch.setattr(
        source_negation_candidates.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    state: dict[str, object] = {}
    ranked = source_negation_candidates.rank_source_negation_candidates(
        ("demo_counterexample",),
        target_symbol="demo",
    )
    initial = source_negation_candidates.select_candidate_batch(
        ranked,
        state=state,
        scope_key="Main.lean::demo",
        source_revision_sha256="c" * 64,
    )
    source_negation_candidates.record_definitive_incompatibility(
        state,
        scope_key="Main.lean::demo",
        source_revision_sha256="c" * 64,
        scheduled=initial.candidates[0],
    )

    unchanged = source_negation_candidates.select_candidate_batch(
        ranked,
        state=state,
        scope_key="Main.lean::demo",
        source_revision_sha256="c" * 64,
    )
    changed = source_negation_candidates.select_candidate_batch(
        ranked,
        state=state,
        scope_key="Main.lean::demo",
        source_revision_sha256="d" * 64,
    )

    assert unchanged.candidates == ()
    assert _names(changed.candidates) == ("demo_counterexample",)


def test_v3_contract_reopens_candidates_rejected_by_v2(monkeypatch) -> None:
    """A changed authoritative check contract invalidates every v2 cursor."""
    monkeypatch.setattr(
        source_negation_candidates.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    revision = "9" * 64
    state: dict[str, object] = {
        source_negation_candidates.PROCESS_STATE_KEY: {
            "schema_version": 2,
            "check_contract_version": "exact-source-harness-v2",
            "scope_key": "Main.lean::demo",
            "source_revision_sha256": revision,
            "exact_order_sha256": "a" * 64,
            "exact_cursor": 1,
            "generic_order_sha256": "b" * 64,
            "generic_cursor": 0,
        }
    }
    ranked = source_negation_candidates.rank_source_negation_candidates(
        ("demo_counterexample",),
        target_symbol="demo",
    )

    reopened = source_negation_candidates.select_candidate_batch(
        ranked,
        state=state,
        scope_key="Main.lean::demo",
        source_revision_sha256=revision,
    )

    assert _names(reopened.candidates) == ("demo_counterexample",)
    current = state[source_negation_candidates.PROCESS_STATE_KEY]
    assert isinstance(current, dict)
    assert current["schema_version"] == 3
    assert current["check_contract_version"] == "exact-source-harness-v4"
    assert current["exact_cursor"] == 0


def test_malformed_continuation_anchor_restarts_from_zero(monkeypatch) -> None:
    """Corrupt nonauthoritative rotation state can only cause safe rechecks."""
    monkeypatch.setattr(
        source_negation_candidates.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    state: dict[str, object] = {}
    revision = "8" * 64
    ranked = source_negation_candidates.rank_source_negation_candidates(
        (
            "demo_counterexample",
            "demo_impossible",
            "demo_refutation",
            "demo_obstruction",
        ),
        target_symbol="demo",
    )
    batch = source_negation_candidates.select_candidate_batch(
        ranked,
        state=state,
        scope_key="Main.lean::demo",
        source_revision_sha256=revision,
    )
    initial = source_negation_candidates.select_uncertain_continuation_window(
        batch,
        state=state,
        scope_key="Main.lean::demo",
        source_revision_sha256=revision,
        anchor=batch.candidates[0],
    )
    state[source_negation_candidates.CONTINUATION_STATE_KEY] = {
        "schema_version": source_negation_candidates.SCHEMA_VERSION,
        "check_contract_version": source_negation_candidates.CHECK_CONTRACT_VERSION,
        "scope_key": "Main.lean::demo",
        "source_revision_sha256": revision,
        "order_sha256": initial.order_sha256,
        "anchor_lane": "exact",
        "anchor_lane_index": "corrupt",
        "next_offset": 2,
    }

    restarted = source_negation_candidates.select_uncertain_continuation_window(
        batch,
        state=state,
        scope_key="Main.lean::demo",
        source_revision_sha256=revision,
        anchor=batch.candidates[0],
    )

    assert restarted.start_offset == 0
    assert _names(restarted.candidates) == (
        "demo_impossible",
        "demo_refutation",
    )


def test_rejection_cursor_survives_a_fresh_process_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))
    revision = "e" * 64
    ranked = source_negation_candidates.rank_source_negation_candidates(
        ("unrelated_not_one", "unrelated_not_two"),
        target_symbol="demo",
    )
    first_state: dict[str, object] = {}
    first = source_negation_candidates.select_candidate_batch(
        ranked,
        state=first_state,
        scope_key="Main.lean::demo",
        source_revision_sha256=revision,
        generic_limit=1,
    )
    source_negation_candidates.record_definitive_incompatibility(
        first_state,
        scope_key="Main.lean::demo",
        source_revision_sha256=revision,
        scheduled=first.candidates[0],
    )

    resumed = source_negation_candidates.select_candidate_batch(
        ranked,
        state={},
        scope_key="Main.lean::demo",
        source_revision_sha256=revision,
        generic_limit=1,
    )

    assert _names(resumed.candidates) == ("unrelated_not_two",)
    assert resumed.previously_rejected_count == 1


def test_monotonic_cursor_eventually_scans_more_than_old_signature_cap(monkeypatch) -> None:
    """A stable generic order cannot starve after more than 512 rejections."""
    monkeypatch.setattr(
        source_negation_candidates.plan_state,
        "plan_state_enabled",
        lambda: False,
    )
    state: dict[str, object] = {}
    revision = "f" * 64
    names = tuple(f"fallback_helper_{index}" for index in range(700))
    ranked = source_negation_candidates.rank_source_negation_candidates(
        names,
        target_symbol="demo",
    )
    observed: list[str] = []

    while True:
        batch = source_negation_candidates.select_candidate_batch(
            ranked,
            state=state,
            scope_key="Main.lean::demo",
            source_revision_sha256=revision,
            generic_limit=7,
        )
        if not batch.candidates:
            break
        for scheduled in batch.candidates:
            assert scheduled.name not in observed
            observed.append(scheduled.name)
            assert source_negation_candidates.record_definitive_incompatibility(
                state,
                scope_key="Main.lean::demo",
                source_revision_sha256=revision,
                scheduled=scheduled,
            )

    assert tuple(observed) == names
    record = state[source_negation_candidates.PROCESS_STATE_KEY]
    assert isinstance(record, dict)
    assert record["generic_cursor"] == 700
    assert "rejected_candidate_signatures" not in record
