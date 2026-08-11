"""Tests for Lean source shaping during managed workflow recovery."""

from leanflow_cli.workflows import recovery_source


def test_strip_transient_diagnostics_removes_only_standalone_commands():
    declaration = (
        "theorem demo : True := by\n"
        "  trace_state -- temporary diagnostic\n"
        "  all_goals fail_if_success done -- temporary assertion\n"
        "  fail_if_success done\n"
        '  have label : String := "trace_state"\n'
        "  -- trace_state\n"
        "  sorry\n"
    )

    cleaned = recovery_source.strip_transient_diagnostics(declaration)

    assert "temporary diagnostic" not in cleaned
    assert "temporary assertion" not in cleaned
    assert "fail_if_success done" not in cleaned
    assert 'have label : String := "trace_state"' in cleaned
    assert "  -- trace_state" in cleaned
    assert cleaned.endswith("  sorry\n")
