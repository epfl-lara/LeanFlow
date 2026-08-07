"""Test exact rollback of managed Lean edits with hard diagnostics."""

import hashlib

from leanflow_cli.native import managed_edit_rollback, native_runner


def test_hard_error_classifier_excludes_operational_timeouts():
    """Treat concrete diagnostics as invalid edits but preserve timeout-only work."""
    assert managed_edit_rollback.check_has_hard_errors(
        {"has_errors": True, "messages": [{"severity": "error", "message": "unknown id"}]},
        timed_out=native_runner._manager_check_timed_out,
    )
    assert not managed_edit_rollback.check_has_hard_errors(
        {"has_errors": False, "timed_out": True, "error": "wall-clock deadline"},
        timed_out=native_runner._manager_check_timed_out,
    )


def test_retained_edit_progress_requires_current_kernel_checked_after_image(tmp_path):
    """Accept unresolved clean elaboration, but reject rollback, timeout, and stale source."""
    target = tmp_path / "Demo.lean"
    before = "theorem demo : True := by\n  sorry\n"
    after = "theorem demo : True := by\n  have h : True := by trivial\n  sorry\n"
    before_sha256 = hashlib.sha256(before.encode("utf-8")).hexdigest()
    after_sha256 = hashlib.sha256(after.encode("utf-8")).hexdigest()
    target.write_text(after, encoding="utf-8")
    clean_partial = {
        "ok": False,
        "incremental": {
            "success": True,
            "ok": False,
            "has_errors": False,
            "has_sorry": True,
        },
    }

    assert managed_edit_rollback.retained_edit_confirms_kernel_progress(
        str(target),
        before_sha256=before_sha256,
        expected_after_sha256=after_sha256,
        manager_check=clean_partial,
        timed_out=native_runner._manager_check_timed_out,
    )
    assert not managed_edit_rollback.retained_edit_confirms_kernel_progress(
        str(target),
        before_sha256=before_sha256,
        expected_after_sha256=after_sha256,
        manager_check={**clean_partial, "failed_edit_restored": True},
        timed_out=native_runner._manager_check_timed_out,
    )
    assert not managed_edit_rollback.retained_edit_confirms_kernel_progress(
        str(target),
        before_sha256=before_sha256,
        expected_after_sha256=after_sha256,
        manager_check={"timed_out": True},
        timed_out=native_runner._manager_check_timed_out,
    )

    target.write_text(before, encoding="utf-8")
    assert not managed_edit_rollback.retained_edit_confirms_kernel_progress(
        str(target),
        before_sha256=before_sha256,
        expected_after_sha256=after_sha256,
        manager_check=clean_partial,
        timed_out=native_runner._manager_check_timed_out,
    )


def test_restore_failed_managed_edit_requires_exact_after_image(tmp_path):
    """Restore captured bytes without overwriting an intervening source revision."""
    target = tmp_path / "Demo.lean"
    before = "theorem demo : True := by\n  trivial\n"
    invalid = "theorem demo : True := by\n  exact missing\n"
    target.write_text(invalid, encoding="utf-8")
    after_sha256 = hashlib.sha256(invalid.encode("utf-8")).hexdigest()

    assert managed_edit_rollback.restore_exact_after_image(
        str(target),
        before_text=before,
        expected_after_sha256=after_sha256,
    )
    assert target.read_text(encoding="utf-8") == before

    target.write_text("-- concurrent edit\n", encoding="utf-8")
    assert not managed_edit_rollback.restore_exact_after_image(
        str(target),
        before_text=before,
        expected_after_sha256=after_sha256,
    )
    assert target.read_text(encoding="utf-8") == "-- concurrent edit\n"


def test_preview_candidate_source_and_match_exact_rejection():
    """Reconstruct a verified patch candidate and identify only its exact replay."""
    before = "theorem demo : True := by\n  sorry\n"
    patch = """*** Begin Patch
*** Update File: Demo.lean
@@
 theorem demo : True := by
-  sorry
+  exact True.intro
*** End Patch"""

    candidate = managed_edit_rollback.preview_candidate_source(
        "apply_verified_patch",
        {"patch": patch},
        before,
    )
    assert candidate == "theorem demo : True := by\n  exact True.intro\n"

    declaration = candidate.strip()
    candidate_hash = hashlib.sha256(declaration.encode()).hexdigest()
    matched = managed_edit_rollback.matching_rejected_candidate(
        [
            {"attempt": 1, "declaration_hash": "0" * 64},
            {"attempt": 2, "declaration_hash": candidate_hash},
        ],
        declaration,
    )
    assert matched is not None
    assert matched["attempt"] == 2
    assert (
        managed_edit_rollback.matching_rejected_candidate(
            [{"attempt": 1, "declaration_hash": "0" * 64}],
            declaration,
        )
        is None
    )


def test_rejected_candidate_identity_ignores_trace_state_instrumentation():
    """Treat diagnostic trace commands as non-semantic candidate presentation."""
    rejected = "theorem demo : True := by\n  exact?"
    replay = "theorem demo : True := by\n  trace_state\n  exact?"
    candidate_hash = hashlib.sha256(
        managed_edit_rollback.normalize_candidate_declaration(rejected).encode()
    ).hexdigest()

    matched = managed_edit_rollback.matching_rejected_candidate(
        [{"attempt": 7, "declaration_hash": candidate_hash}],
        replay,
    )

    assert matched is not None
    assert matched["attempt"] == 7


def test_rejected_candidate_identity_ignores_unsolved_goal_assertion():
    """Treat an unsolved-goal assertion as diagnostic candidate presentation."""
    plain = "theorem demo : True := by\n  exact True.intro"
    instrumented = (
        "theorem demo : True := by\n" "  all_goals fail_if_success done\n" "  exact True.intro"
    )

    assert managed_edit_rollback.normalize_candidate_declaration(
        instrumented
    ) == managed_edit_rollback.normalize_candidate_declaration(plain)


def test_transient_diagnostic_detection_accepts_comment_but_not_mentions():
    """Recognize standalone trace instrumentation without matching ordinary source text."""
    assert managed_edit_rollback.contains_transient_diagnostic(
        "by\n  trace_state -- temporary\n  sorry"
    )
    assert managed_edit_rollback.contains_transient_diagnostic(
        "by\n  all_goals fail_if_success done\n  sorry"
    )
    assert not managed_edit_rollback.contains_transient_diagnostic(
        'by\n  have label : String := "trace_state"\n  exact True.intro'
    )


def test_transient_diagnostic_detection_covers_command_level_probes():
    """Recognize command-level introspection that sits outside a declaration."""
    source = """theorem demo : True := by
  exact True.intro

#print prefix Demo
#check Demo.demo
run_cmd logInfo "diagnostic"
"""

    assert managed_edit_rollback.transient_diagnostic_markers(source) == (
        "#print prefix Demo",
        "#check Demo.demo",
        'run_cmd logInfo "diagnostic"',
    )


def test_introduced_transient_diagnostics_allows_cleanup_only():
    """Reject new diagnostics while allowing an edit that removes stale probes."""
    before = "theorem demo : True := by\n  trace_state\n  sorry\n#check demo\n"
    after = "theorem demo : True := by\n  exact True.intro\n#check other\n"

    assert managed_edit_rollback.introduced_transient_diagnostics(before, after) == (
        "#check other",
    )
    assert managed_edit_rollback.introduced_transient_diagnostics(after, "") == ()


def test_introduced_suggestion_tactics_cover_scoped_heartbeat_probe():
    """Detect suggestions outside the target and heartbeat-wrapped retries."""
    before = "theorem demo : True := by\n  exact?\n"
    after = (
        "private theorem helper : True := by\n"
        "  set_option maxHeartbeats 2000000 in exact?\n\n" + before
    )

    assert managed_edit_rollback.introduced_suggestion_tactics(before, after) == (
        "set_option maxHeartbeats 2000000 in exact?",
    )


def test_introduced_duplicate_declarations_reports_new_name_collision():
    before = "private lemma helper : True := by\n  trivial\n"
    after = before + "\nprivate lemma helper : True := by\n  trivial\n"

    assert managed_edit_rollback.introduced_duplicate_declarations(before, after) == ("helper",)


def test_introduced_duplicate_declarations_ignores_unrelated_edit():
    before = "private lemma helper : True := by\n  trivial\n"
    after = before + "\ntheorem result : True := by\n  trivial\n"

    assert managed_edit_rollback.introduced_duplicate_declarations(before, after) == ()
    assert managed_edit_rollback.introduced_suggestion_tactics(before, "") == ()
