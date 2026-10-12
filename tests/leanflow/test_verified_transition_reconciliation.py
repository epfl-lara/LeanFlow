"""Verified queue-transition graph synchronization tests."""

from __future__ import annotations

import pytest

from leanflow_cli.workflows import verified_transition_reconciliation


def _transition(file: str) -> dict[str, str]:
    return {
        "previous_target": "scale_three_unit_fraction",
        "previous_file": file,
        "current_target": "main_target",
        "current_file": file,
    }


def _outcome(file: str, *, status: str = "solved") -> dict[str, object]:
    return {
        "target_symbol": "scale_three_unit_fraction",
        "active_file": file,
        "status": status,
        "last_verification": {
            "scope": "target:scale_three_unit_fraction",
            "target": "scale_three_unit_fraction",
            "ok": True,
            "errors": 0,
            "warnings": 1,
            "sorry": 0,
            "axiom_profile_checked": True,
            "axiom_profile_blockers": [],
        },
    }


def _live_state(file: str) -> dict[str, object]:
    return {
        "active_file": file,
        "active_file_label": file,
        "target_symbol": "main_target",
        "current_queue_item": {"label": "main_target", "reasons": ["contains sorry"]},
        "current_queue_item_slice": "theorem main_target : True := by\n  sorry",
        "declaration_queue_summary": "- main_target — contains sorry",
        "current_blocker": "main_target contains sorry",
    }


@pytest.mark.parametrize(
    ("accepted", "status", "current_target"),
    [
        (False, "solved", "main_target"),
        (True, "unverified", "main_target"),
        (True, "solved", "different_target"),
    ],
)
def test_verified_transition_sync_rejects_non_authoritative_boundaries(
    tmp_path, accepted, status, current_target
):
    active = str(tmp_path / "Main.lean")
    live_state = _live_state(active)
    live_state["current_queue_item"] = {
        "label": current_target,
        "reasons": ["contains sorry"],
    }

    request = verified_transition_reconciliation.verified_transition_sync(
        transition=_transition(active),
        outcome=_outcome(active, status=status),
        live_state=live_state,
        verification_accepted=accepted,
    )

    assert request is None


def test_verified_transition_sync_preserves_the_live_next_assignment(tmp_path):
    active = str(tmp_path / "Main.lean")

    request = verified_transition_reconciliation.verified_transition_sync(
        transition=_transition(active),
        outcome=_outcome(active),
        live_state=_live_state(active),
        verification_accepted=True,
    )

    assert request is not None
    assert request.completed_target == "scale_three_unit_fraction"
    assert request.assignment_mapping() == {
        "target_symbol": "main_target",
        "active_file": active,
        "slice": "theorem main_target : True := by\n  sorry",
    }
