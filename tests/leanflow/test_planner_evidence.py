"""Tests for exact-target planner advisor evidence continuity."""

from __future__ import annotations

from leanflow_cli.workflows import plan_state, planner_evidence


def test_record_and_match_advisor_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PLAN_STATE", "1")
    monkeypatch.setenv("LEANFLOW_PLAN_STATE_DIR", str(tmp_path / "plan-state"))
    active = tmp_path / "Main.lean"
    active.write_text("theorem demo : True := by sorry\n", encoding="utf-8")

    assert planner_evidence.record_advisor_evidence(
        result_text='{"status":"answered"}',
        payload={
            "status": "answered",
            "answer": "Use subset sums; record the failed greedy branch.",
        },
        target_symbol="demo",
        active_file=str(active),
        target_declaration_sha256="abc",
    )

    matches = planner_evidence.matching_advisor_evidence(
        target_symbol="demo",
        active_file=str(active),
        target_declaration_sha256="abc",
    )
    assert matches == (
        {
            "source": "lean_reasoning_help",
            "text": "Use subset sums; record the failed greedy branch.",
        },
    )
    assert (
        planner_evidence.matching_advisor_evidence(
            target_symbol="demo",
            active_file=str(active),
            target_declaration_sha256="changed",
        )
        == ()
    )
    journal = plan_state.plan_state_paths().journal_jsonl.read_text(encoding="utf-8")
    assert "planner-advisor-evidence-persisted" in journal
