"""Tests for the extracted native_utils leaf helpers + native_runner re-export (Phase 2).

These cover the pure, closed-under-"calls" subset of shared text / JSON / format / normalize "glue"
helpers moved out of native_runner. The re-export-identity test pins every moved name to the same
object on ``native_runner`` so the many callers still living there resolve the extracted helpers
without a back-import. The behavior tests exercise the trickiest helpers: JSON-payload extraction
from fenced / noisy text, single-line collapsing + truncation, and message-text collection.
"""

from leanflow_cli.native import native_runner
from leanflow_cli.native import native_utils as nu


def test_native_runner_reexports_are_identical():
    # Every re-exported name in native_runner must be the SAME object as in native_utils, so callers
    # still living in native_runner resolve the extracted helpers without a back-import.
    for name in nu.__all__:
        assert getattr(native_runner, name) is getattr(nu, name), name


def test_extract_json_payload_from_fenced_and_noisy_text():
    # Clean JSON.
    assert nu._extract_json_payload('{"a": 1}') == {"a": 1}
    # JSON embedded in a fenced code block with chatter around it.
    fenced = 'Sure, here is the result:\n```json\n{"ok": true, "files": ["A.lean"]}\n```\nDone.'
    assert nu._extract_json_payload(fenced) == {"ok": True, "files": ["A.lean"]}
    # Leading/trailing prose around a bare object.
    assert nu._extract_json_payload('blah {"x": [1, 2]} trailing') == {"x": [1, 2]}
    # A top-level list still parses via the [ ] scan.
    assert nu._extract_json_payload("noise [1, 2, 3] noise") == [1, 2, 3]
    # Nothing JSON-like -> None; empty -> None.
    assert nu._extract_json_payload("no json here") is None
    assert nu._extract_json_payload("") is None
    assert nu._extract_json_payload(None) is None


def test_single_line_collapses_and_truncates():
    # Whitespace (including newlines/tabs) collapses to single spaces.
    assert nu._single_line("  hello\n\tworld   foo ") == "hello world foo"
    # None-ish input is safe.
    assert nu._single_line(None) == ""
    # Over-limit input is truncated with an ellipsis to exactly `limit` chars.
    out = nu._single_line("abcdefghij", limit=8)
    assert out == "abcde..."
    assert len(out) == 8
    # Exactly-at-limit input is returned untouched.
    assert nu._single_line("abcde", limit=5) == "abcde"


def test_collect_and_message_text():
    assert nu._message_text("plain") == "plain"
    assert nu._message_text(None) == ""
    assert nu._message_text(123) == "123"
    messages = [
        {"content": "first"},
        None,  # skipped entirely
        {"content": None},  # contributes an empty segment
        {"content": "third"},
    ]
    assert nu._collect_message_text(messages) == "first\n\n\n\nthird"


def test_collect_assistant_report_text_ignores_successful_tool_contracts_and_user_prompts():
    messages = [
        {
            "role": "user",
            "content": "A blocker is evidence for a new route, never permission to halt.",
        },
        {
            "role": "tool",
            "name": "skill_view",
            "content": (
                '{"success": true, "name": "lean-theorem-queue-worker", '
                '"content": "## Blocker Taxonomy\\n- failed proof attempts are route evidence"}'
            ),
        },
        {
            "role": "assistant",
            "content": "The remaining branch is stuck on a missing divisibility lemma.",
        },
    ]

    assert (
        nu._collect_assistant_report_text(messages)
        == "The remaining branch is stuck on a missing divisibility lemma."
    )
    assert nu._collect_assistant_report_text(messages[:2]) == ""


def test_bounded_verifier_response_truncates_long_text():
    short = "all good"
    assert nu._bounded_verifier_response(short) == short
    long_text = "x" * 200
    bounded = nu._bounded_verifier_response(long_text, limit=64)
    assert bounded.endswith("[truncated by LeanFlow verifier logging]")
    assert bounded.startswith("x")
    assert len(bounded) < len(long_text)


def test_diagnostic_counts_from_messages():
    messages = [
        {"severity": "error", "message": "boom"},
        {"severity": "warning", "message": "careful"},
        {"severity": "error", "message": "contains sorry here"},
        {"severity": "error", "message": "axiom guard depends on sorryAx"},
    ]
    errors, warnings, sorry_count = nu._diagnostic_counts_from_messages(messages=messages)
    assert (errors, warnings, sorry_count) == (3, 1, 1)
    # No input -> all zero.
    assert nu._diagnostic_counts_from_messages() == (0, 0, 0)


def test_normalize_blocker_summary_clears_resolved_markers():
    assert nu._normalize_blocker_summary("All blockers resolved") == ""
    assert nu._normalize_blocker_summary("No blocker declared") == ""
    assert nu._normalize_blocker_summary("  ") == ""
    assert (
        nu._normalize_blocker_summary("genuinely stuck on lemma foo")
        == "genuinely stuck on lemma foo"
    )
