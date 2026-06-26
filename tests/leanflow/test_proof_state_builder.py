"""Tests for the extracted proof_state_builder helpers + native_runner re-export (Phase 2).

``_build_live_proof_state`` itself stays in native_runner (it reaches lean_inspect /
probe_capabilities / route_workflow_step backend calls, the TheoremQueueManager, env readers,
and workflow-state mutation). Only its movable pure sub-helpers were extracted: the declaration
line-index lookups and the queue-horizon text shapers. The re-export-identity test pins every
moved name to the same object on ``native_runner`` so the many callers still living there (and
existing tests calling ``runner._diagnostics_for_queue_horizon`` / ``runner._declaration_line_index``)
keep resolving the extracted helpers without a back-import. The behavior tests exercise the
declaration-scoped diagnostic shaping, the queue-horizon summary, and the proof-status lines.
"""

from leanflow_cli import proof_state_builder as psb
from leanflow_cli.native import native_runner


def test_native_runner_reexports_are_identical():
    # Every re-exported name in native_runner must be the SAME object as in proof_state_builder,
    # so callers still living in native_runner resolve the extracted helpers without a back-import.
    for name in psb.__all__:
        assert getattr(native_runner, name) is getattr(psb, name), name


def test_find_declaration_entry_and_line_membership(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem alpha : True := by",
                "  trivial",
                "",
                "theorem beta : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )
    entry = psb._find_declaration_entry(str(active), "alpha")
    assert entry is not None and entry.get("name") == "alpha"
    # A line inside alpha's span is recognized; a line in beta is not.
    assert psb._line_in_declaration(entry, int(entry.get("line", 0) or 0)) is True
    assert psb._line_in_declaration(entry, 4) is False
    # Unknown / empty labels resolve to None.
    assert psb._find_declaration_entry(str(active), "gamma") is None
    assert psb._find_declaration_entry(str(active), "") is None
    # Missing files yield an empty index rather than raising.
    assert psb._declaration_line_index(str(tmp_path / "Missing.lean")) == []
    assert psb._declaration_line_index("") == []


def test_diagnostics_for_queue_horizon_scopes_to_assigned_declaration(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem assigned : True := by",
                "  exact bad",
                "",
                "theorem future : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )
    # A parseable diagnostic on line 2 (inside `assigned`) is surfaced.
    text = psb._diagnostics_for_queue_horizon(
        active_file=str(active),
        target_symbol="assigned",
        diagnostics="Main.lean:2:3: error: unknown identifier bad",
        declaration_scope="file",
        queue_needs_final_file_sweep=False,
    )
    assert "unknown identifier bad" in text

    # A parseable diagnostic that lands only in `future` (line 5) is hidden from the assigned view.
    hidden = psb._diagnostics_for_queue_horizon(
        active_file=str(active),
        target_symbol="assigned",
        diagnostics="Main.lean:5:3: error: something in future",
        declaration_scope="file",
        queue_needs_final_file_sweep=False,
    )
    assert "something in future" not in hidden
    assert "No diagnostics in the assigned declaration" in hidden

    # Non-file scope returns the raw diagnostics text unchanged.
    raw = psb._diagnostics_for_queue_horizon(
        active_file=str(active),
        target_symbol="assigned",
        diagnostics="anything",
        declaration_scope="project",
        queue_needs_final_file_sweep=False,
    )
    assert raw == "anything"


def test_queue_horizon_summary_and_proof_status_lines():
    # Non-file scope just echoes the supplied summary.
    assert (
        psb._queue_horizon_summary(
            declaration_scope="project",
            queue_needs_final_file_sweep=False,
            current_queue_item={"label": "x"},
            declaration_queue_summary="raw summary",
        )
        == "raw summary"
    )
    # File scope with an assigned item renders the assigned-declaration + pending lines.
    summary = psb._queue_horizon_summary(
        declaration_scope="file",
        queue_needs_final_file_sweep=False,
        current_queue_item={"label": "alpha", "reasons": ["contains sorry"]},
        declaration_queue_summary="ignored",
        declaration_queue_total=3,
    )
    assert "assigned declaration: alpha - contains sorry" in summary
    assert "2 pending" in summary

    # Final-file-sweep falls back to the project-wide proof status lines.
    lines = psb._proof_status_lines_for_queue_horizon(
        active_file="",
        target_symbol="",
        declaration_scope="file",
        queue_needs_final_file_sweep=True,
        sorry_count=2,
        project_sorry_count=5,
        project_sorry_files=["A.lean", "B.lean"],
    )
    assert lines[0] == "sorry count: 2"
    assert lines[1] == "project sorry count: 5"
    assert lines[2] == "project files with sorry: A.lean, B.lean"
