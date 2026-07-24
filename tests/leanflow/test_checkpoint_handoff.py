"""Tests for authoritative checkpoint handoff status."""

from leanflow_cli.native.checkpoint_handoff import checkpoint_success_state


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
