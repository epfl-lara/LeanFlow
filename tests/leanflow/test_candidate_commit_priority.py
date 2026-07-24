"""Characterize the exact-candidate foreground handoff predicate."""

from __future__ import annotations

import json

import pytest

from leanflow_cli.native import candidate_commit_priority


def _assignment(tmp_path):
    active = tmp_path / "Demo" / "Main.lean"
    active.parent.mkdir(parents=True)
    active.write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    return active, {"target_symbol": "demo", "active_file": str(active)}


def _clean_payload(active) -> dict[str, object]:
    return {
        "success": True,
        "ok": True,
        "action": "check_target",
        "file": str(active),
        "target": "demo",
        "valid_without_sorry": True,
        "has_errors": False,
        "has_sorry": False,
        "replacement_matches_target": True,
        "replacement_declarations": ["demo"],
        "verification_scope": "target_candidate",
        "axiom_profile_requested": True,
        "axiom_profile_checked": True,
        "axiom_profile_axioms": ["propext", "Classical.choice", "Quot.sound"],
        "axiom_profile_error": "",
    }


def _clean_helper_payload(active) -> dict[str, object]:
    return {
        "success": True,
        "ok": True,
        "action": "check_helper",
        "file": str(active),
        "target": "demo",
        "valid_without_sorry": True,
        "has_errors": False,
        "has_sorry": False,
        "timed_out": False,
        "replacement_matches_target": False,
        "replacement_declarations": ["checked_helper"],
        "verification_scope": "helper_candidate",
        "axiom_profile_requested": True,
        "axiom_profile_checked": True,
        "axiom_profile_axioms": ["propext", "Classical.choice", "Quot.sound"],
        "axiom_profile_blockers": [],
        "axiom_profile_error": "",
    }


def test_live_shape_without_profile_argument_requests_sixty_second_handoff(
    monkeypatch, tmp_path
) -> None:
    """Trust the wrapper's checked profile result when the live call omits its flag."""
    active, assignment = _assignment(tmp_path)
    monkeypatch.delenv("LEANFLOW_CANDIDATE_COMMIT_HANDOFF_S", raising=False)

    seconds = candidate_commit_priority.handoff_seconds(
        "lean_incremental_check",
        {
            "action": "check_target",
            "cwd": str(tmp_path),
            "file_path": str(active),
            "theorem_id": "demo",
            "replacement": "theorem demo : True := by trivial",
        },
        json.dumps(_clean_payload(active)),
        assignment=assignment,
        allowed_axioms={"propext", "Classical.choice", "Quot.sound"},
    )

    assert seconds == 60.0


@pytest.mark.parametrize(
    ("mutation", "argument_mutation"),
    [
        ({"ok": False}, {}),
        ({"replacement_matches_target": False}, {}),
        ({"valid_without_sorry": False, "has_sorry": True}, {}),
        ({"axiom_profile_checked": False}, {}),
        ({"axiom_profile_requested": False}, {}),
        ({"axiom_profile_axioms": ["sorryAx"]}, {}),
        ({"replacement_declarations": None}, {}),
        ({"target": "support"}, {}),
        ({}, {"replacement": ""}),
    ],
)
def test_unverified_or_nonassignment_candidate_never_requests_handoff(
    tmp_path,
    mutation,
    argument_mutation,
) -> None:
    """Keep ordinary failures, scratch checks, and unchecked profiles on normal grace."""
    active, assignment = _assignment(tmp_path)
    payload = _clean_payload(active)
    payload.update(mutation)
    arguments = {
        "action": "check_target",
        "cwd": str(tmp_path),
        "file_path": str(active),
        "theorem_id": "demo",
        "replacement": "theorem demo : True := by trivial",
        "include_axiom_profile": True,
    }
    arguments.update(argument_mutation)

    assert (
        candidate_commit_priority.handoff_seconds(
            "lean_incremental_check",
            arguments,
            json.dumps(payload),
            assignment=assignment,
            allowed_axioms={"propext", "Classical.choice", "Quot.sound"},
        )
        == 0.0
    )


def test_configured_candidate_handoff_is_bounded(monkeypatch, tmp_path) -> None:
    """Clamp expert configuration to the core's crash-recovery deadline cap."""
    active, assignment = _assignment(tmp_path)
    monkeypatch.setenv("LEANFLOW_CANDIDATE_COMMIT_HANDOFF_S", "9999")

    seconds = candidate_commit_priority.handoff_seconds(
        "lean_incremental_check",
        {
            "action": "check_target",
            "cwd": str(tmp_path),
            "file_path": str(active),
            "theorem_id": "demo",
            "replacement": "theorem demo : True := by trivial",
            "include_axiom_profile": True,
        },
        json.dumps(_clean_payload(active)),
        assignment=assignment,
        allowed_axioms={"propext", "Classical.choice", "Quot.sound"},
    )

    assert seconds == candidate_commit_priority.MAX_FOREGROUND_HANDOFF_LEASE_S


def test_nonfinite_candidate_handoff_configuration_uses_default(monkeypatch, tmp_path) -> None:
    """Do not turn a NaN configuration into the core's maximum lease."""
    active, assignment = _assignment(tmp_path)
    monkeypatch.setenv("LEANFLOW_CANDIDATE_COMMIT_HANDOFF_S", "nan")

    seconds = candidate_commit_priority.handoff_seconds(
        "lean_incremental_check",
        {
            "action": "check_target",
            "cwd": str(tmp_path),
            "file_path": str(active),
            "theorem_id": "demo",
            "replacement": "theorem demo : True := by trivial",
        },
        json.dumps(_clean_payload(active)),
        assignment=assignment,
        allowed_axioms={"propext", "Classical.choice", "Quot.sound"},
    )

    assert seconds == candidate_commit_priority.CANDIDATE_COMMIT_HANDOFF_DEFAULT_S


def test_clean_single_helper_requests_commit_handoff_without_durable_candidate(tmp_path) -> None:
    """Carry a model-authored clean helper from exact check to source commit."""
    active, assignment = _assignment(tmp_path)
    declaration = "private lemma checked_helper : True := by trivial"
    arguments = {
        "action": "check_helper",
        "cwd": str(tmp_path),
        "file_path": str(active),
        "theorem_id": "demo",
        "replacement": declaration,
    }
    pending = {
        "state": "ready_to_integrate",
        "target_symbol": "demo",
        "active_file": str(active),
        "helper_name": "checked_helper",
        "declaration": declaration,
    }

    accepted = candidate_commit_priority.handoff_seconds(
        "lean_incremental_check",
        arguments,
        json.dumps(_clean_helper_payload(active)),
        assignment=assignment,
        allowed_axioms={"propext", "Classical.choice", "Quot.sound"},
        pending_helper=pending,
    )
    wrong = candidate_commit_priority.handoff_seconds(
        "lean_incremental_check",
        {**arguments, "replacement": "private lemma scratch : True := by trivial"},
        json.dumps(_clean_helper_payload(active)),
        assignment=assignment,
        allowed_axioms={"propext", "Classical.choice", "Quot.sound"},
        pending_helper=pending,
    )
    model_authored = candidate_commit_priority.handoff_seconds(
        "lean_incremental_check",
        arguments,
        json.dumps(_clean_helper_payload(active)),
        assignment=assignment,
        allowed_axioms={"propext", "Classical.choice", "Quot.sound"},
    )

    assert accepted == candidate_commit_priority.CANDIDATE_COMMIT_HANDOFF_DEFAULT_S
    assert wrong == 0.0
    assert model_authored == candidate_commit_priority.CANDIDATE_COMMIT_HANDOFF_DEFAULT_S


@pytest.mark.parametrize(
    ("payload_mutation", "argument_mutation"),
    [
        ({"target": "other"}, {}),
        ({"file": "Other.lean"}, {}),
        ({"replacement_declarations": ["first", "second"]}, {}),
        ({"replacement_declarations": []}, {}),
        ({"replacement_declarations": None}, {}),
        ({"verification_scope": "target_candidate"}, {}),
        ({"replacement_matches_target": True}, {}),
        ({"valid_without_sorry": False, "has_sorry": True}, {}),
        ({"axiom_profile_checked": False}, {}),
        ({"axiom_profile_axioms": ["sorryAx"]}, {}),
        ({}, {"replacement": ""}),
        ({}, {"replacement": "private lemma other : True := by trivial"}),
        ({}, {"theorem_id": "other"}),
        ({}, {"file_path": "Other.lean"}),
    ],
)
def test_malformed_or_wrong_scope_helper_never_requests_commit_handoff(
    tmp_path,
    payload_mutation,
    argument_mutation,
) -> None:
    """Keep malformed, broad, and wrong-assignment helper checks fail closed."""
    active, assignment = _assignment(tmp_path)
    payload = _clean_helper_payload(active)
    payload.update(payload_mutation)
    arguments = {
        "action": "check_helper",
        "cwd": str(tmp_path),
        "file_path": str(active),
        "theorem_id": "demo",
        "replacement": "private lemma checked_helper : True := by trivial",
    }
    arguments.update(argument_mutation)

    assert (
        candidate_commit_priority.handoff_seconds(
            "lean_incremental_check",
            arguments,
            json.dumps(payload),
            assignment=assignment,
            allowed_axioms={"propext", "Classical.choice", "Quot.sound"},
        )
        == 0.0
    )


def test_non_json_helper_result_never_requests_commit_handoff(tmp_path) -> None:
    """Require the exact structured verifier result before extending priority."""
    active, assignment = _assignment(tmp_path)

    assert (
        candidate_commit_priority.handoff_seconds(
            "lean_incremental_check",
            {
                "action": "check_helper",
                "cwd": str(tmp_path),
                "file_path": str(active),
                "theorem_id": "demo",
                "replacement": "private lemma checked_helper : True := by trivial",
            },
            "helper accepted",
            assignment=assignment,
            allowed_axioms={"propext", "Classical.choice", "Quot.sound"},
        )
        == 0.0
    )
