"""Tests for the extracted lean_diagnostic_feedback parsers + native_runner re-export (Phase 2).

These cover the pure, closed-under-"calls" subset of diagnostic / goal text parsers moved out of
native_runner. The re-export-identity test pins every moved name to the same object on
``native_runner`` so the many callers still living there (``_declaration_work_queue``,
``_live_state_is_verified``, ``_build_live_proof_state`` ...) resolve the extracted helpers without
a back-import. The behavior tests exercise the trickiest helpers: hard-failure / queue-blocker
detection over noisy diagnostic text, structured + JSON goal-state parsing, and the per-declaration
diagnostic feedback reason that combines structured items with text fallbacks.
"""

from leanflow_cli.lean import lean_diagnostic_feedback as ldf
from leanflow_cli.native import native_runner

_REEXPORTED = (
    "_declaration_diagnostic_feedback_reason",
    "_declaration_name_safe_for_diagnostic_match",
    "_declaration_prefix_text",
    "_declaration_slice_text",
    "_diagnostic_reason_for_entry",
    "_diagnostics_indicate_failure",
    "_diagnostics_indicate_hard_failure",
    "_diagnostics_indicate_queue_blocker",
    "_goals_still_open",
    "_is_anonymous_declaration_label",
    "_nearest_declaration_name",
    "_queue_diagnostic_items",
    "_queue_diagnostic_line_numbers",
)


def test_native_runner_reexports_are_identical():
    # Every re-exported name in native_runner must be the SAME object as in
    # lean_diagnostic_feedback, so callers still living in native_runner resolve the
    # extracted helpers without a back-import.
    for name in _REEXPORTED:
        assert getattr(native_runner, name) is getattr(ldf, name), name


def test_diagnostics_indicate_hard_failure_distinguishes_cleared_from_error():
    # "no errors found" / "no errors" are stripped before pattern matching, so a clean
    # report is not a hard failure.
    assert ldf._diagnostics_indicate_hard_failure("Build completed with no errors found") is False
    assert ldf._diagnostics_indicate_hard_failure("no errors") is False
    assert ldf._diagnostics_indicate_hard_failure(
        "error: type mismatch\n  expected Nat",
    )
    assert ldf._diagnostics_indicate_hard_failure("") is False


def test_diagnostics_indicate_queue_blocker_text_fallback():
    assert ldf._diagnostics_indicate_queue_blocker("unsolved goals remain")
    # A pure "no errors" report clears the blocker patterns.
    assert ldf._diagnostics_indicate_queue_blocker("checked without errors") is False


def test_goals_still_open_handles_text_json_and_cleared():
    assert ldf._goals_still_open("⊢ True")
    assert ldf._goals_still_open("no goals") is False
    assert ldf._goals_still_open("goals accomplished") is False
    # Structured JSON payloads recurse through goals/goal/term_goal keys.
    assert ldf._goals_still_open('{"goals": ["⊢ p"]}')
    assert ldf._goals_still_open('{"goals": []}') is False
    assert ldf._goals_still_open("unavailable") is False


def test_declaration_name_safe_for_diagnostic_match():
    assert ldf._declaration_name_safe_for_diagnostic_match("my_lemma")
    # Short purely-alphabetic names are ambiguous and rejected.
    assert ldf._declaration_name_safe_for_diagnostic_match("ab") is False
    # Short names with a non-letter are specific enough.
    assert ldf._declaration_name_safe_for_diagnostic_match("h1")
    assert ldf._declaration_name_safe_for_diagnostic_match("[anonymous 3]") is False


def test_declaration_diagnostic_feedback_reason_prefers_structured_items(monkeypatch):
    # Pin a declaration entry so the helper has a line range to match against.
    monkeypatch.setattr(
        ldf,
        "_find_declaration_entry",
        lambda active_file, label: {"line": 10, "end_line": 12},
    )
    structured = [{"line": 11, "severity": "warning", "message": "unused variable x"}]
    reason = ldf._declaration_diagnostic_feedback_reason(
        "Demo/Main.lean",
        "foo",
        structured_items=structured,
    )
    assert reason == "warning near line 11: unused variable x"
    # Out-of-range diagnostics yield no reason.
    out_of_range = [{"line": 99, "severity": "error", "message": "boom"}]
    assert (
        ldf._declaration_diagnostic_feedback_reason(
            "Demo/Main.lean",
            "foo",
            structured_items=out_of_range,
        )
        == ""
    )


def test_declaration_diagnostic_feedback_reason_prioritizes_error_over_warning(monkeypatch):
    monkeypatch.setattr(
        ldf,
        "_find_declaration_entry",
        lambda active_file, label: {"line": 10, "end_line": 14},
    )

    reason = ldf._declaration_diagnostic_feedback_reason(
        "Demo/Main.lean",
        "foo",
        structured_items=[
            {"line": 11, "severity": "warning", "message": "deprecated tactic"},
            {"line": 13, "severity": "error", "message": "type mismatch"},
        ],
    )

    assert reason == "error near line 13: type mismatch"
