"""Tests for authoritative checkpoint handoff status."""

from leanflow_cli.native.checkpoint_handoff import (
    checkpoint_success_state,
    extract_negative_evidence,
)


def test_signal_interruption_remains_in_progress_despite_concrete_blocker():
    assert (
        checkpoint_success_state(
            {"exit_code": 130, "interrupt_source": "signal"},
            verified=False,
            blocker_summary="target still contains sorry",
        )
        == "in-progress"
    )


def test_non_signal_blocker_and_verified_state_keep_existing_meanings():
    assert (
        checkpoint_success_state({}, verified=False, blocker_summary="target still contains sorry")
        == "blocked"
    )
    assert (
        checkpoint_success_state(
            {"exit_code": 130},
            verified=True,
            blocker_summary="stale blocker",
        )
        == "verified"
    )


def test_extract_negative_evidence_keeps_only_explicit_nested_dead_branches():
    summary = """## Workflow
- Negative evidence to preserve:
  - The predecessor map reaches the forbidden boundary value.
  - Repeating the same search query produced no new evidence.
- A later workflow fact that is not negative evidence.

## Next steps
1. Inspect the current source.
"""

    assert extract_negative_evidence(summary) == (
        "The predecessor map reaches the forbidden boundary value.",
        "Repeating the same search query produced no new evidence.",
    )


def test_extract_negative_evidence_does_not_promote_unlabeled_checkpoint_prose():
    assert (
        extract_negative_evidence(
            "## Lean findings\n- Use a claimed theorem.\n## Blockers\n- Current target has sorry."
        )
        == ()
    )
