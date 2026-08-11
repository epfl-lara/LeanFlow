"""Tests for deterministic source-negation alias construction."""

from leanflow_cli.workflows import source_negation_harness


def test_canonical_harness_bridges_a_specialized_counterexample():
    harness = source_negation_harness.build_source_negation_harness(
        alias="leanflowNegationPromotion_test",
        negation_prop="∀ n : Nat, n < 5",
        candidate_name="not_bad_at_five",
    )

    assert harness is not None
    assert harness.proof_tactic == (
        "intro leanflowTarget\n"
        "apply not_bad_at_five\n"
        "first\n"
        "| exact leanflowTarget\n"
        "| apply leanflowTarget"
    )
    assert harness.declaration.endswith(
        "  | apply leanflowTarget\n#print axioms leanflowNegationPromotion_test"
    )


def test_revalidation_preserves_the_legacy_direct_exact_identity():
    harness = source_negation_harness.build_source_negation_harness(
        alias="leanflowNegationPromotion_test",
        negation_prop="False",
        candidate_name="not_false",
        recorded_proof_tactic="exact not_false",
    )

    assert harness is not None
    assert "  exact not_false" in harness.declaration


def test_revalidation_rejects_an_arbitrary_recorded_tactic():
    harness = source_negation_harness.build_source_negation_harness(
        alias="leanflowNegationPromotion_test",
        negation_prop="False",
        candidate_name="not_false",
        recorded_proof_tactic="aesop",
    )

    assert harness is None
