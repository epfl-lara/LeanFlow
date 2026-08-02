"""Tests for Lean source shaping during managed workflow recovery."""

from leanflow_cli.workflows import recovery_source


def test_strip_transient_diagnostics_removes_only_standalone_trace_state():
    declaration = (
        "theorem demo : True := by\n"
        "  trace_state -- temporary diagnostic\n"
        '  have label : String := "trace_state"\n'
        "  -- trace_state\n"
        "  sorry\n"
    )

    cleaned = recovery_source.strip_transient_diagnostics(declaration)

    assert "temporary diagnostic" not in cleaned
    assert 'have label : String := "trace_state"' in cleaned
    assert "  -- trace_state" in cleaned
    assert cleaned.endswith("  sorry\n")
