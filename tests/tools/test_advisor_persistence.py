"""Tests for deterministic no-surrender normalization of advisor prose."""

import pytest

from tools.utilities.advisor_persistence import guard_reasoning_advice


def test_open_problem_evidence_is_preserved_without_fake_proof_claims():
    advice = guard_reasoning_advice(
        "This appears to be open in the current literature. "
        "Continuation route: launch a distinct literature and identity-search job."
    )

    assert advice.guard_applied is False
    assert "appears to be open" in advice.text
    assert "route-change evidence" in advice.text
    assert "proof success" in advice.text


def test_anti_surrender_instruction_is_not_misclassified():
    advice = guard_reasoning_advice(
        "Never report the unresolved theorem as mathematically blocked. "
        "Continuation route: refresh the proof portfolio."
    )

    assert advice.guard_applied is False
    assert "Never report" in advice.text


def test_terminal_only_advice_becomes_safe_route_change_handoff():
    advice = guard_reasoning_advice(
        "The correct outcome is to report this theorem as blocked and not to continue."
    )

    assert advice.guard_applied is True
    assert advice.rejected_fragment_count == 1
    assert "report this theorem as blocked" not in advice.text
    assert "supplied no safe, concrete strategy detail" in advice.text
    assert "portfolio refresh" in advice.text


def test_anti_surrender_prefix_does_not_hide_a_later_terminal_recommendation():
    advice = guard_reasoning_advice(
        "Never report the theorem as blocked, but further attempts are unwarranted."
    )

    assert advice.guard_applied is True
    assert "further attempts are unwarranted" not in advice.text


def test_bare_futility_recommendation_is_removed():
    advice = guard_reasoning_advice(
        "The computations are useful evidence. Further attempts are unwarranted."
    )

    assert advice.guard_applied is True
    assert "computations are useful evidence" in advice.text
    assert "Further attempts are unwarranted" not in advice.text


@pytest.mark.parametrize(
    "terminal_recommendation",
    [
        "Conclude that this theorem is blocked.",
        "Give up on this proof.",
        "Terminate the campaign.",
        "Do not spend any more effort on it.",
        "There is no point in continuing.",
    ],
)
def test_common_terminal_recommendations_are_removed(terminal_recommendation):
    advice = guard_reasoning_advice(
        f"The failed route is useful evidence. {terminal_recommendation}"
    )

    assert advice.guard_applied is True
    assert terminal_recommendation not in advice.text
    assert "failed route is useful evidence" in advice.text
