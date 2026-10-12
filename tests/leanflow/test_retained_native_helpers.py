"""Exercise shared deterministic workflow helpers independently of retired engines."""

from __future__ import annotations

import json

from leanflow_cli.lean.lean_diagnostic_feedback import (
    _declaration_diagnostic_feedback_reason as _leaf_declaration_diagnostic_feedback_reason,
)
from leanflow_cli.lean.lean_diagnostic_feedback import (
    _declaration_prefix_text as _leaf_declaration_prefix_text,
)
from leanflow_cli.lean.lean_diagnostic_feedback import (
    _diagnostic_reason_for_entry as _leaf_diagnostic_reason_for_entry,
)
from leanflow_cli.lean.lean_diagnostic_feedback import (
    _diagnostics_indicate_failure as _leaf_diagnostics_indicate_failure,
)
from leanflow_cli.lean.lean_diagnostic_feedback import (
    _diagnostics_indicate_hard_failure as _leaf_diagnostics_indicate_hard_failure,
)
from leanflow_cli.lean.lean_diagnostic_feedback import _goals_still_open as _leaf_goals_still_open
from leanflow_cli.native import search_synthesis_admission as _leaf_search_synthesis_admission
from leanflow_cli.native.native_checkpoints import (
    _load_current_checkpoint as _leaf_load_current_checkpoint,
)
from leanflow_cli.native.native_checkpoints import _write_json_file as _leaf_write_json_file
from leanflow_cli.native.native_lean_files import (
    _checkpoint_active_files as _leaf_checkpoint_active_files,
)
from leanflow_cli.native.native_lean_files import (
    _count_project_sorries as _leaf_count_project_sorries,
)
from leanflow_cli.native.native_lean_files import (
    _extract_active_files as _leaf_extract_active_files,
)
from leanflow_cli.native.native_lean_files import _resolve_active_file as _leaf_resolve_active_file
from leanflow_cli.native.native_lean_files import (
    _resolve_target_symbol as _leaf_resolve_target_symbol,
)
from leanflow_cli.native.native_utils import (
    _extract_blocker_summary as _leaf_extract_blocker_summary,
)
from leanflow_cli.native.native_utils import (
    _normalize_blocker_summary as _leaf_normalize_blocker_summary,
)
from leanflow_cli.proof_state_builder import _declaration_line_index as _leaf_declaration_line_index
from leanflow_cli.proof_state_builder import (
    _diagnostics_for_queue_horizon as _leaf_diagnostics_for_queue_horizon,
)
from leanflow_cli.workflows.queue_item_predicates import (
    _current_queue_item as _leaf_current_queue_item,
)


def test_real_incremental_helper_is_not_classified_as_inspection():
    """Keep a substantive helper constructive even when it uses #check context."""
    args = {
        "action": "check_helper",
        "replacement": (
            "#check Nat.add_comm\n"
            "private lemma add_comm_helper (a b : Nat) : a + b = b + a := by\n"
            "  exact Nat.add_comm a b"
        ),
    }

    assert (
        _leaf_search_synthesis_admission.discovery_tool_name("lean_incremental_check", args) is None
    )


def test_declaration_diagnostic_feedback_reason_prefers_structured_items_over_text(tmp_path):
    """Pin the helper contract: a structured warning inside the assigned
    declaration's range wins over text-form parsing, so `lean_interact`
    output that text-parsers cannot locate still produces a cleanup reason."""

    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  have h : True := by",
                "    all_goals trivial",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    text_only = _leaf_declaration_diagnostic_feedback_reason(
        str(active),
        "demo",
        "warning: 'all_goals trivial' tactic does nothing",
    )
    structured = _leaf_declaration_diagnostic_feedback_reason(
        str(active),
        "demo",
        "warning: 'all_goals trivial' tactic does nothing",
        structured_items=[
            {
                "severity": "warning",
                "message": "'all_goals trivial' tactic does nothing",
                "line": 3,
                "column": 5,
            }
        ],
    )

    # Without structured items, the regex fallback cannot locate the warning
    # because there is no `<file>:<line>:<col>:` prefix.
    assert text_only == ""
    assert "warning near line 3" in structured
    assert "all_goals trivial" in structured


def test_declaration_diagnostic_feedback_reason_accepts_lean_interact_file_start(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem demo : True := by",
                "  have h : True := by",
                "    all_goals trivial",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )

    structured = _leaf_declaration_diagnostic_feedback_reason(
        str(active),
        "demo",
        "warning: this tactic is never executed",
        structured_items=[
            {
                "severity": "warning",
                "message": "this tactic is never executed",
                "start": {"line": 300, "column": 1},
                "file_start": {"line": 3, "column": 5},
            }
        ],
    )

    assert "warning near line 3" in structured
    assert "never executed" in structured


def test_declaration_cleanup_ignores_info_diagnostics(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "theorem demo : True := by\n  trivial\n#check True\n",
        encoding="utf-8",
    )

    reason = _leaf_declaration_diagnostic_feedback_reason(
        str(active),
        "demo",
        f"{active}:2:3: info: True : Prop",
    )

    assert reason == ""


def test_count_project_sorries_ignores_dependencies_and_build_dirs(tmp_path):
    project = tmp_path / "Demo"
    (project / ".lake" / "packages" / "mathlib").mkdir(parents=True)
    (project / "build" / "ir").mkdir(parents=True)
    (project / ".leanflow" / "runtime").mkdir(parents=True)
    (project / "Demo").mkdir(parents=True)
    (project / "Demo" / "Main.lean").write_text(
        "theorem t : True := by\n  sorry\n", encoding="utf-8"
    )
    (project / ".lake" / "packages" / "mathlib" / "Ignored.lean").write_text(
        "theorem x : True := by\n  sorry\n", encoding="utf-8"
    )
    (project / "build" / "ir" / "Ignored.lean").write_text(
        "theorem y : True := by\n  sorry\n", encoding="utf-8"
    )

    count, files = _leaf_count_project_sorries(str(project))

    assert count == 1
    assert files == ["Demo/Main.lean (1)"]


def test_declaration_ranges_do_not_include_next_declaration_doc_comment(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem first : True := by",
                "  trivial",
                "",
                "/-- Doc comment for the next theorem. -/",
                "theorem second : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )

    entries = _leaf_declaration_line_index(str(active))
    first = entries[0]

    assert first["name"] == "first"
    assert first["end_line"] == 2
    assert "Doc comment for the next theorem" not in first["text"]
    assert _leaf_diagnostic_reason_for_entry(first, [4]) == ""


def test_declaration_prefix_text_keeps_last_200_lines(tmp_path):
    active = tmp_path / "Main.lean"
    prefix = [f"-- prefix marker {i}" for i in range(1, 221)]
    active.write_text(
        "\n".join(prefix + ["theorem demo : True := by", "  sorry"]),
        encoding="utf-8",
    )

    text = _leaf_declaration_prefix_text(str(active), "demo")
    lines = text.splitlines()

    assert "Current file prefix ending at `demo` (23-222)" in text
    assert "-- prefix marker 22" not in lines
    assert "-- prefix marker 23" in lines
    assert "theorem demo : True := by" in text


def test_current_queue_item_prefers_diagnostic_blocker_before_later_sorry(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem broken : True := by",
                "  exact ?missing",
                "",
                "theorem later : True := by",
                "  sorry",
            ]
        ),
        encoding="utf-8",
    )
    queue = [
        {"label": "broken", "reasons": ["diagnostic near line 2"]},
        {"label": "later", "reasons": ["contains sorry"]},
    ]

    assert _leaf_current_queue_item(queue, str(active))["label"] == "broken"


def test_current_queue_item_does_not_select_clean_declaration(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem clean : True := by",
                "  trivial",
            ]
        ),
        encoding="utf-8",
    )
    queue = [{"label": "clean", "reasons": []}]

    assert _leaf_current_queue_item(queue, str(active)) is None


def test_diagnostics_for_queue_horizon_does_not_leak_unparseable_future_lines(tmp_path):
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "theorem assigned : True := by",
                "  trivial",
                "",
                "theorem future : True := by",
                "  exact bad",
            ]
        ),
        encoding="utf-8",
    )

    text = _leaf_diagnostics_for_queue_horizon(
        active_file=str(active),
        target_symbol="assigned",
        diagnostics="Lean said something failed around line 5: unknown identifier bad",
        declaration_scope="file",
        queue_needs_final_file_sweep=False,
    )

    assert "line 5" not in text
    assert "future queue items are hidden" not in text
    assert "could not be scoped reliably" in text


def test_diagnostics_indicate_failure_for_warnings():
    assert _leaf_diagnostics_indicate_failure("warning: declaration uses simp") is True


def test_diagnostics_indicate_failure_ignores_structured_info_messages():
    assert (
        _leaf_diagnostics_indicate_failure(
            '{"items":[{"severity":"info","message":"#check output","line":2,"column":1}]}'
        )
        is False
    )


def test_diagnostics_indicate_failure_keeps_structured_warnings_blocking():
    assert (
        _leaf_diagnostics_indicate_failure(
            '{"items":[{"severity":"warning","message":"style warning","line":6,"column":3}]}'
        )
        is True
    )


def test_diagnostics_indicate_hard_failure_ignores_warning_only_diagnostics():
    assert (
        _leaf_diagnostics_indicate_hard_failure(
            "Demo/Main.lean:2:3: warning: this tactic is never executed"
        )
        is False
    )
    assert (
        _leaf_diagnostics_indicate_hard_failure("Demo/Main.lean:2:3: error: unsolved goals") is True
    )


def test_normalize_blocker_summary_clears_resolved_text():
    assert _leaf_normalize_blocker_summary("None. All blockers resolved.") == ""
    assert _leaf_normalize_blocker_summary("type mismatch in `simpa`") == "type mismatch in `simpa`"


def test_extract_blocker_summary_does_not_fall_back_to_unrelated_trailing_line():
    text = "\n".join(
        [
            "## Blockers",
            "No blocker declared at this checkpoint.",
            "## Next steps",
            "- Exploring uniform continuity results building on these foundations",
        ]
    )

    assert _leaf_extract_blocker_summary(text) == ""


def test_extract_active_files_normalizes_project_relative_paths(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    target = project / "Demo" / "Main.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    files = _leaf_extract_active_files(f"a//{target}\n{target}\nDemo/Main.lean\nMain.lean\n")

    assert files == ["Demo/Main.lean", "Main.lean"]


def test_checkpoint_active_files_rejects_diff_header_fragments(monkeypatch, tmp_path):
    project = tmp_path / "Demo"
    target = project / "Demo" / "Main.lean"
    scratch = project / "Demo" / "Scratch.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem demo : True := by\n  trivial\n", encoding="utf-8")
    scratch.write_text("example : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))

    files = _leaf_checkpoint_active_files(
        {
            "active_file": str(target),
            "active_file_label": "Demo/Main.lean",
        },
        "\n".join(
            [
                "a/Demo/Main.lean",
                "b/Demo/Main.lean",
                "nDemo/Main.lean",
                str(target),
                "Demo/Scratch.lean",
            ]
        ),
    )

    assert files == ["Demo/Main.lean", "Demo/Scratch.lean"]


def test_goals_still_open_ignores_structured_history_fields_without_current_goals():
    payload = {
        "line_context": "theorem demo : True := by",
        "goals": None,
        "goals_before": [],
        "goals_after": ["⊢ True"],
    }

    assert _leaf_goals_still_open(json.dumps(payload)) is False


def test_resolve_active_file_prefers_configured_active_file(monkeypatch, tmp_path):
    project = tmp_path / "ProveDemo"
    target = project / "ProveDemo" / "RealTheorems-homework.lean"
    target.parent.mkdir(parents=True)
    target.write_text("theorem t : True := by\n  trivial\n", encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_ACTIVE_FILE", "ProveDemo/RealTheorems-homework.lean")

    resolved = _leaf_resolve_active_file([])

    assert resolved == str(target.resolve())


def test_resolve_target_symbol_does_not_drift_from_history(monkeypatch):
    monkeypatch.setenv(
        "LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove ./ProveDemo/RealTheorems-homework.lean"
    )

    symbol = _leaf_resolve_target_symbol(
        [
            {"role": "assistant", "content": "reading Basic.lean"},
            {"role": "tool", "content": 'def hello := "world"'},
        ]
    )

    assert symbol == ""


def test_current_checkpoint_loads_for_same_workflow_command(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_COMMAND", "/prove Main.lean")

    snapshot = project / ".leanflow" / "workflow-state" / "ckpt-current.json"
    _leaf_write_json_file(
        snapshot,
        {
            "version": 1,
            "checkpoint_id": "ckpt-current",
            "label": "verified proof milestone",
            "workflow_kind": "prove",
            "workflow_command": "/prove Main.lean",
            "project_root": str(project),
            "summary_text": "current run",
        },
    )
    _leaf_write_json_file(
        project / ".leanflow" / "workflow-state" / "current.json",
        {
            "version": 1,
            "checkpoint_id": "ckpt-current",
            "snapshot_path": str(snapshot),
        },
    )

    loaded = _leaf_load_current_checkpoint()
    assert loaded is not None
    assert loaded["checkpoint_id"] == "ckpt-current"
