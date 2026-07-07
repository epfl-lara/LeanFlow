"""Phase 3 §6 tests: the never-silent end-of-scope final report."""

from __future__ import annotations

import json
from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import final_report as fr


@pytest.fixture()
def state_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    (tmp_path / ".leanflow").mkdir()
    (tmp_path / ".leanflow" / "project.yaml").write_text("name: t\n", encoding="utf-8")
    return tmp_path / ".leanflow" / "workflow-state"


def _autonomy_state() -> dict[str, Any]:
    return {
        "current_queue_assignment": {"target_symbol": "hard", "active_file": "Demo.lean"},
        "theorem_outcomes": {
            "Demo.lean::demo": {"target_symbol": "demo", "status": "solved"},
            "Demo.lean::hard": {"target_symbol": "hard", "status": "blocked"},
        },
        "failed_attempts": [
            {
                "attempt": 1,
                "target_symbol": "hard",
                "active_file": "Demo.lean",
                "reason": "type mismatch",
            }
        ],
    }


def test_generate_final_report_sections_and_mirror(state_root):
    state_root.mkdir(parents=True)
    (state_root / "summary.json").write_text(
        json.dumps(
            {
                "dispatch_ledger": [
                    {"spec": {"job_id": "run.planner.np-001"}, "state": "running", "notes": ""}
                ],
                "negation_probes": [
                    {
                        "theorem": "hard",
                        "plausible": {"counterexample_text": "n := 7"},
                        "negation": {"verdict": "inconclusive"},
                    }
                ],
                "decision_packets": [{"packet_id": "bp-1", "decision": None}],
            }
        ),
        encoding="utf-8",
    )

    path = fr.generate_final_report(
        stop_reason="budget-breakpoint",
        autonomy_state=_autonomy_state(),
        live_state={},
        run_id="prove-t1",
    )

    text = path.read_text(encoding="utf-8")
    for heading in (
        "## Theorem ledger",
        "## What was tried",
        "## What was learned",
        "## Open jobs",
        "## Recommended next actions",
    ):
        assert heading in text
    assert "`demo` | solved" in text
    assert "`hard` | blocked" in text
    # Open jobs are LOUD (the never-silently-lost audit).
    assert "**OPEN** run.planner.np-001 [running]" in text
    assert "counterexample for `hard`: n := 7" in text
    assert "undecided decision packet" in text

    summary = json.loads((state_root / "summary.json").read_text(encoding="utf-8"))
    mirror = summary["final_report"]
    assert mirror["stop_reason"] == "budget-breakpoint"
    assert mirror["outcome_kind"] == "report"
    assert mirror["status"] == "documented"
    assert mirror["theorem_counts"] == {"proved": 1, "blocked": 1, "unresolved": 0}
    assert mirror["open_jobs"][0]["job_id"] == "run.planner.np-001"


def test_classify_disproved_requires_kernel_standard_probe(state_root):
    from leanflow_cli.workflows.queue_models import TheoremKey

    key = TheoremKey.make("hard", "Demo.lean").storage_key()
    summary = {
        "negation_probes": [
            {
                "key": key,
                "theorem": "hard",
                "negation": {"verdict": "negation_proved", "axioms_ok": True},
            }
        ]
    }
    outcome = fr.classify_scope_outcome(_autonomy_state(), {}, summary)
    assert outcome.kind == "disproved"

    tainted = {
        "negation_probes": [
            {"key": key, "negation": {"verdict": "negation_proved", "axioms_ok": False}}
        ]
    }
    assert fr.classify_scope_outcome(_autonomy_state(), {}, tainted).kind == "report"
    # Same theorem NAME in a different file never classifies this scope
    # disproved (exact key match required).
    other_key = TheoremKey.make("hard", "Other.lean").storage_key()
    other = {
        "negation_probes": [
            {"key": other_key, "negation": {"verdict": "negation_proved", "axioms_ok": True}}
        ]
    }
    assert fr.classify_scope_outcome(_autonomy_state(), {}, other).kind == "report"


def test_pause_is_not_a_scope_end(state_root, monkeypatch):
    monkeypatch.setattr(
        runner.final_report,
        "generate_final_report",
        lambda **kwargs: pytest.fail("paused must not generate a report"),
    )
    runner._maybe_generate_final_report("paused", {}, {})


def test_prose_cannot_break_report_structure(state_root):
    state = _autonomy_state()
    state["failed_attempts"][0]["reason"] = "evil | pipes\nand ## headings"

    path = fr.generate_final_report(
        stop_reason="stalled", autonomy_state=state, live_state={}, run_id="t"
    )

    text = path.read_text(encoding="utf-8")
    assert "evil / pipes and ## headings" in text
    assert "evil | pipes" not in text


def test_runner_hook_gating_and_idempotency(state_root, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        runner.final_report,
        "generate_final_report",
        lambda **kwargs: calls.append(kwargs["stop_reason"]) or (state_root / "r.md"),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    state: dict[str, Any] = {}

    # Verified and handoff exits are exempt.
    runner._maybe_generate_final_report("verified", state, {})
    runner._maybe_generate_final_report("formalization-prover-handoff-ready", state, {})
    assert calls == []

    runner._maybe_generate_final_report("stalled", state, {})
    assert calls == ["stalled"]
    assert state["final_report_written"] is True
    # Idempotent: ceiling + stall interplay writes once.
    runner._maybe_generate_final_report("blocked", state, {})
    assert calls == ["stalled"]

    # Opt-out flag.
    fresh: dict[str, Any] = {}
    monkeypatch.setenv("LEANFLOW_FINAL_REPORT", "0")
    runner._maybe_generate_final_report("stalled", fresh, {})
    assert calls == ["stalled"]


def test_generation_failure_never_crashes_the_stop(state_root, monkeypatch):
    monkeypatch.setattr(
        runner.final_report,
        "generate_final_report",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    state: dict[str, Any] = {}

    runner._maybe_generate_final_report("stalled", state, {})

    assert "final_report_written" not in state  # retryable on the next exit
