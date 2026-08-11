"""Tests for the extracted lean_diagnostics text parsers + lean_services re-export (Phase 5).

These cover the pure, closed-under-"calls" subset of Lean diagnostic / blocker / goal text parsers
moved out of lean_services. The re-export-identity test pins every moved name to the same object on
``lean_services`` so the many callers still living there (and external importers like native_runner,
native_utils, lean_tool, doctor) resolve the extracted parsers without a back-import. The behavior
tests exercise the trickiest helpers: JSON-payload diagnostic extraction, the human-readable
``file:line:col: severity:`` fallback, actionable-failure detection, blocker classification, and a
timing-bounded guard mirroring the existing catastrophic-backtracking regression test.
"""

from __future__ import annotations

import time

from leanflow_cli.lean import lean_diagnostics as ld
from leanflow_cli.lean import lean_services


def test_lean_services_reexports_are_identical():
    # Every re-exported name in lean_services must be the SAME object as in lean_diagnostics, so
    # the lean_services callers (and external importers) resolve the extracted parsers without a
    # back-import.
    for name in ld.__all__:
        assert getattr(lean_services, name) is getattr(ld, name), name


def test_diagnostic_items_parses_standard_lines():
    out = ld.diagnostic_items(
        "File.lean:12:7: error: unexpected token\nC:/proj/File.lean:3:0: warning: unused variable x"
    )
    assert out == [
        {"severity": "error", "message": "unexpected token", "line": 12},
        {"severity": "warning", "message": "unused variable x", "line": 3},
    ]


def test_diagnostic_items_parses_lean_error_codes():
    out = ld.diagnostic_items(
        "File.lean:12:7: error(lean.synthInstanceFailed): failed to synthesize Nonempty P"
    )
    assert out == [
        {
            "severity": "error",
            "message": "failed to synthesize Nonempty P",
            "line": 12,
        }
    ]


def test_diagnostic_items_parses_json_payload():
    # A JSON list of LSP-style diagnostics is normalized to severity/message/line records, with the
    # line read from a nested range.start.line.
    payload = (
        '[{"severity": "error", "message": "type mismatch", '
        '"range": {"start": {"line": 9}}}, '
        '{"level": "warning", "text": "unused", "line": 4}]'
    )
    out = ld.diagnostic_items(payload)
    assert out == [
        {"severity": "error", "message": "type mismatch", "line": 9},
        {"severity": "warning", "message": "unused", "line": 4},
    ]


def test_actionable_failure_detection():
    # error/warning diagnostics are actionable...
    assert ld.diagnostics_indicate_actionable_failure("F.lean:1:1: error: boom") is True
    # ...while a clean "no errors" run is not, even though the word "errors" appears.
    assert ld.diagnostics_indicate_actionable_failure("Build completed, no errors found.") is False
    # A bare "sorry" with no structured diagnostic still trips the textual failure patterns.
    assert ld.diagnostics_indicate_actionable_failure("declaration uses sorry") is True


def test_classify_blocker_kind():
    assert ld.classify_blocker_kind("") == "none"
    assert ld.classify_blocker_kind("failed to synthesize instance HMul") == "synth_instance"
    assert ld.classify_blocker_kind("unsolved goals\n⊢ p = q") == "open_goals"
    assert ld.classify_blocker_kind("some opaque build text") == "diagnostics"


def test_goal_envelopes_classify_only_current_nonempty_goals_as_open():
    cleared_payloads = (
        '{"goals": null, "goals_before": [], "goals_after": []}',
        '{"goals": [], "goals_before": ["⊢ stale"], "goals_after": ["⊢ historical"]}',
        "Lean goals unavailable.",
    )
    for payload in cleared_payloads:
        assert ld._goals_still_open(payload) is False
        assert ld.classify_blocker_kind("", goals=payload) == "none"

    open_payload = '{"goals": ["⊢ True"], "goals_before": [], "goals_after": []}'
    assert ld._goals_still_open(open_payload) is True
    assert ld.classify_blocker_kind("", goals=open_payload) == "open_goals"
    assert ld._goals_still_open("⊢ IsUnavailable x") is True
    assert (
        ld._goals_still_open(
            '{"line_context":"theorem unavailable_case : True :=",' '"goals":["⊢ True"]}'
        )
        is True
    )


def test_source_backed_sorry_outranks_empty_goal_envelope():
    assert (
        ld.classify_blocker_kind(
            "",
            goals='{"goals": null, "goals_before": [], "goals_after": []}',
            queue_reasons=("contains sorry",),
        )
        == "sorry"
    )
    assert (
        ld.classify_blocker_kind(
            "unsolved goals from a prior attempt",
            goals='{"goals": null, "goals_before": [], "goals_after": []}',
            queue_reasons=("contains sorry",),
        )
        == "sorry"
    )


def test_structured_error_outranks_sorry_and_warning_words_do_not_fabricate_errors():
    assert (
        ld.classify_blocker_kind(
            "",
            diagnostics='{"items":[{"severity":"error","message":"unknown tactic"}]}',
            queue_reasons=("contains sorry",),
        )
        == "diagnostics"
    )
    assert (
        ld.classify_blocker_kind(
            "",
            diagnostics=(
                '{"items":[{"severity":"warning",'
                '"message":"instance search is intentionally disabled"}]}'
            ),
        )
        == "warnings"
    )


def test_diagnostic_items_does_not_catastrophically_backtrack():
    # Regression guard mirroring the lean_services test: a long single line carrying many `:n:n:`
    # coordinates but NO error:/warning: token previously caused O(n^2) regex backtracking that
    # pinned the autonomous runner at ~100% CPU forever. The anchored pattern parses it in well
    # under a second.
    pathological = "1:" * 16000  # ~32 KB, no error/warning token -> no matches
    start = time.perf_counter()
    result = ld.diagnostic_items(pathological)
    elapsed = time.perf_counter() - start
    assert result == []
    assert elapsed < 1.0, f"diagnostic_items took {elapsed:.2f}s — regex backtracking regression"
