"""Test name-insensitive replay fencing for rejected Lean helper candidates."""

from leanflow_cli.native import managed_edit_rollback


def test_rejected_helper_fingerprint_ignores_only_declaration_name():
    first = "private lemma first_helper (n : Nat) : n = n := by\n  rfl"
    renamed = "private lemma renamed_helper (n : Nat) : n = n := by\n  rfl"
    changed = "private lemma renamed_helper (n : Nat) : n = n := by\n  exact rfl"

    assert managed_edit_rollback.helper_candidate_fingerprint(
        first
    ) == managed_edit_rollback.helper_candidate_fingerprint(renamed)
    assert managed_edit_rollback.helper_candidate_fingerprint(
        first
    ) != managed_edit_rollback.helper_candidate_fingerprint(changed)


def test_failed_check_helper_replay_is_blocked_at_same_source_revision():
    state: dict[str, object] = {}
    source_revision = "a" * 64
    failed = "private lemma failed_helper (n : Nat) : n + 1 = n := by\n  omega"

    remembered = managed_edit_rollback.remember_rejected_helper_check(
        state,
        args={"action": "check_helper", "replacement": failed},
        result={
            "ok": False,
            "has_errors": True,
            "messages": [{"severity": "error", "message": "unsolved goals"}],
        },
        target_symbol="result",
        active_file="Demo.lean",
        source_revision_sha256=source_revision,
    )

    before = "theorem result : True := by\n  sorry\n"
    after = (
        "private lemma same_failure_new_name (n : Nat) : n + 1 = n := by\n" "  omega\n\n" + before
    )
    replay = managed_edit_rollback.matching_new_rejected_helper(
        state,
        before_source=before,
        after_source=after,
        target_symbol="result",
        active_file="Demo.lean",
        source_revision_sha256=source_revision,
    )

    assert [record["name"] for record in remembered] == ["failed_helper"]
    assert replay is not None
    assert replay["name"] == "failed_helper"
    assert replay["replayed_name"] == "same_failure_new_name"


def test_helper_replay_record_requires_concrete_lean_error():
    for result in (
        {"ok": True, "has_errors": False},
        {"ok": False, "timed_out": True, "has_errors": False},
        {"ok": False, "lean_started": False, "has_errors": True},
        {"ok": False, "has_sorry": True, "has_errors": False},
    ):
        state: dict[str, object] = {}
        assert (
            managed_edit_rollback.remember_rejected_helper_check(
                state,
                args={
                    "action": "check_helper",
                    "replacement": "private lemma helper : True := by trivial",
                },
                result=result,
                target_symbol="result",
                active_file="Demo.lean",
                source_revision_sha256="b" * 64,
            )
            == []
        )
        assert managed_edit_rollback.REJECTED_HELPER_REPLAY_STATE_KEY not in state
