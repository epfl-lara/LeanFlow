"""Tests for production naming of model-generated Lean helpers."""

from leanflow_cli.native import generated_helper_name_policy


def test_detects_new_scratch_style_helper_names() -> None:
    before = "theorem result : True := by\n  sorry\n"
    after = (
        "private lemma test_map : True := by\n"
        "  trivial\n\n"
        "private lemma scratch_bridge : True := by\n"
        "  trivial\n\n" + before
    )

    assert generated_helper_name_policy.nonproduction_generated_helpers(
        before,
        after,
        assigned_target="result",
    ) == ("test_map", "scratch_bridge")


def test_allows_semantic_helpers_and_existing_test_declarations() -> None:
    before = (
        "private lemma test_existing : True := by\n"
        "  trivial\n\n"
        "theorem result : True := by\n"
        "  sorry\n"
    )
    after = before.replace(
        "theorem result",
        "private lemma predecessor_pair_injective : True := by\n" "  trivial\n\n" "theorem result",
    )

    assert (
        generated_helper_name_policy.nonproduction_generated_helpers(
            before,
            after,
            assigned_target="result",
        )
        == ()
    )


def test_name_matching_uses_identifier_components() -> None:
    before = "theorem result : True := by\n  sorry\n"
    after = (
        "private lemma testimony_map : True := by\n"
        "  trivial\n\n"
        "private lemma attempt_bound : True := by\n"
        "  trivial\n\n" + before
    )

    assert (
        generated_helper_name_policy.nonproduction_generated_helpers(
            before,
            after,
            assigned_target="result",
        )
        == ()
    )
