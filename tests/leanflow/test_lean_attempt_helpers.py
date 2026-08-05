"""Tests for the extracted lean_attempt_helpers pure helpers + lean_services re-export (Phase 5).

These cover the side-effect-free leaf helpers moved out of lean_services: the ``lean_multi_attempt``
candidate normalizer / validator, the attempt-diagnostics summarizer, the Lean comment/string
stripper, and the diff-style path-prefix stripper (plus the ``MULTI_ATTEMPT_*`` bounds). The
re-export-identity test pins every moved name to the same object on ``lean_services`` so the callers
still living there (``_count_sorries`` / ``lean_inspect`` / ``lean_multi_attempt`` /
``_local_incremental_auto_probe`` / ``lean_auto_probe`` / ``_canonical_tool_file_path``) resolve the
extracted helpers without a back-import. The behavior tests exercise the trickiest helpers:
candidate normalization, the multi-validation reason set (count bounds, embedded ``sorry`` detection
through comment stripping, and the full-proof-block guard), and the attempt-diagnostics summarizer.
"""

from __future__ import annotations

from leanflow_cli.lean import lean_attempt_helpers as h
from leanflow_cli.lean import lean_services

_REEXPORTED = (
    "MULTI_ATTEMPT_MIN_CANDIDATES",
    "MULTI_ATTEMPT_MAX_CANDIDATES",
    "MULTI_ATTEMPT_MAX_LINES",
    "MULTI_ATTEMPT_MAX_CHARS",
    "_strip_diff_path_prefix",
    "_strip_comments_and_strings",
    "_summarize_attempt_diagnostics",
    "_normalize_multi_attempt_candidates",
    "_multi_attempt_validation_reasons",
)


def test_lean_services_reexports_are_identical():
    # Every re-exported name in lean_services must be the SAME object as in lean_attempt_helpers, so
    # the lean_services callers resolve the extracted helpers without a back-import.
    for name in _REEXPORTED:
        assert getattr(lean_services, name) is getattr(h, name), name


def test_normalize_multi_attempt_candidates_trims_and_drops_empty():
    assert h._normalize_multi_attempt_candidates(["  simp ", "", "  ", "ring"]) == ["simp", "ring"]
    assert h._normalize_multi_attempt_candidates(
        [" exact hfirst ", "linarith [hfirst]", "exact hfirst"]
    ) == ["exact hfirst", "linarith [hfirst]"]
    assert h._normalize_multi_attempt_candidates(None) == []


def test_multi_attempt_validation_flags_count_bounds():
    # Fewer than MIN candidates triggers the count-bound reason.
    reasons = h._multi_attempt_validation_reasons(["simp"])
    assert any("concrete tactic candidates" in r for r in reasons)
    # A valid pair of short tactics passes with no reasons.
    assert h._multi_attempt_validation_reasons(["simp", "ring"]) == []


def test_multi_attempt_validation_detects_sorry_through_comment_stripping():
    # `sorry` inside a real tactic is rejected; `sorry` only inside a comment is NOT, because the
    # validator strips comments before scanning.
    assert any(
        "must not contain `sorry`" in r
        for r in h._multi_attempt_validation_reasons(["exact foo", "sorry"])
    )
    assert h._multi_attempt_validation_reasons(["exact foo", "exact bar -- sorry"]) == []


def test_multi_attempt_validation_flags_full_proof_blocks():
    block = "theorem foo : True := by\n  trivial"
    assert any(
        "short local tactic candidates" in r
        for r in h._multi_attempt_validation_reasons(["simp", block])
    )


def test_summarize_attempt_diagnostics_picks_first_message_per_attempt():
    attempts = [
        {"diagnostics": [{"message": "  unknown   identifier  bar  "}]},
        {"diagnostics": "not-a-list"},
        {"diagnostics": [{"message": "type mismatch"}, {"message": "ignored"}]},
        {"diagnostics": [{"message": "third should be skipped"}]},
    ]
    # Collapses whitespace, takes one message per attempt, and stops after 2.
    assert h._summarize_attempt_diagnostics(attempts) == ["unknown identifier bar", "type mismatch"]


def test_strip_diff_path_prefix_strips_ab_prefixes():
    # The ``a//`` / ``b//`` diff sentinels have their leading 2 chars dropped.
    assert h._strip_diff_path_prefix("a//Foo/Bar.lean") == "/Foo/Bar.lean"
    assert h._strip_diff_path_prefix("b//Foo/Bar.lean") == "/Foo/Bar.lean"
    # Plain paths are only whitespace-trimmed, never altered.
    assert h._strip_diff_path_prefix("  Foo/Bar.lean  ") == "Foo/Bar.lean"
    assert h._strip_diff_path_prefix("") == ""
