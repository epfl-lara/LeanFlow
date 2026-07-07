"""Phase 2.2 tests: the advisory LLM-manager (nudger) and its runner hook."""

from __future__ import annotations

import json
from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
from leanflow_cli.workflows import manager_nudge
from leanflow_cli.workflows.struggle_signals import (
    StruggleContext,
    StruggleReport,
    evaluate,
)


def _fired_report() -> StruggleReport:
    return evaluate(StruggleContext(attempt_count=3))


class _FakeReview:
    def __init__(self, status="ok", response=""):
        self.status = status
        self.response = response


def test_nudge_mode_matrix(monkeypatch):
    monkeypatch.delenv("LEANFLOW_MANAGER_LLM_MODE", raising=False)
    monkeypatch.delenv("LEANFLOW_MANAGER_LLM_ENABLED", raising=False)
    assert manager_nudge.nudge_mode() == "off"
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "dark")
    assert manager_nudge.nudge_mode() == "dark"
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    assert manager_nudge.nudge_mode() == "live"
    monkeypatch.delenv("LEANFLOW_MANAGER_LLM_MODE", raising=False)
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_ENABLED", "1")
    assert manager_nudge.nudge_mode() == "live"


def test_request_nudge_parses_strict_and_fenced_json(monkeypatch):
    payload = {
        "action": "replan",
        "message": "List the two sub-goals and prove the easier one first.",
        "rationale": "same shape failed twice",
        "confidence": 0.8,
        "report_note": "",
    }
    responses = [
        json.dumps(payload),
        f"Here you go:\n```json\n{json.dumps(payload)}\n```",
    ]
    for response in responses:
        monkeypatch.setattr(
            manager_nudge,
            "run_model_verification_review",
            lambda response=response, **kwargs: _FakeReview(response=response),
        )
        nudge = manager_nudge.request_nudge(_fired_report(), {})
        assert nudge is not None
        assert nudge.action == "replan"
        assert nudge.confidence == 0.8


@pytest.mark.parametrize(
    "response",
    [
        "total garbage",
        json.dumps({"action": "invent", "message": "x"}),
        json.dumps({"action": "replan", "message": "   "}),
        json.dumps({"action": "stop", "message": "give up"}),  # stop without report_note
    ],
)
def test_request_nudge_rejects_unusable_output(monkeypatch, response):
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )
    assert manager_nudge.request_nudge(_fired_report(), {}) is None


def test_request_nudge_fails_open(monkeypatch):
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    assert manager_nudge.request_nudge(_fired_report(), {}) is None
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(status="unavailable"),
    )
    assert manager_nudge.request_nudge(_fired_report(), {}) is None


def test_record_nudge_caps_log_and_emits_activity(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_nudge, "workflow_state_root", lambda: tmp_path)
    events: list[tuple] = []
    monkeypatch.setattr(
        manager_nudge,
        "append_workflow_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    report = _fired_report()

    for _ in range(manager_nudge.NUDGE_LOG_CAP + 5):
        manager_nudge.record_nudge(None, report, applied=False, mode="dark")

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert len(summary["manager_nudges"]) == manager_nudge.NUDGE_LOG_CAP
    assert summary["manager_nudges"][0]["mode"] == "dark"
    assert len(events) == manager_nudge.NUDGE_LOG_CAP + 5
    assert events[0][0][0] == "manager-nudge"


# --- runner hook -----------------------------------------------------------


def _hook_state() -> dict[str, Any]:
    return {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
            "slice": "theorem demo : True := by\n  sorry",
        },
        "failed_attempts": [
            {
                "attempt": index + 1,
                "cycle": index + 1,
                "target_symbol": "demo",
                "active_file": "Demo/Main.lean",
                "proof_shape": "simp",
                "reason": "type mismatch",
            }
            for index in range(3)
        ],
    }


def test_hook_off_mode_is_inert(monkeypatch):
    monkeypatch.delenv("LEANFLOW_MANAGER_LLM_MODE", raising=False)
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda *args, **kwargs: pytest.fail("nudge must not be called in off mode"),
    )

    guidance = runner._maybe_manager_nudge(
        _hook_state(), {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert guidance == ""


def test_hook_dark_mode_logs_but_returns_no_guidance(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "dark")
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    recorded: list[dict] = []
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda report, packet, **kwargs: manager_nudge.NudgeResult(
            action="continue",
            message="Commit to the rewrite now.",
            rationale="",
            confidence=0.6,
            raw_status="ok",
        ),
    )
    monkeypatch.setattr(
        runner.manager_nudge,
        "record_nudge",
        lambda result, report, **kwargs: recorded.append(kwargs),
    )

    guidance = runner._maybe_manager_nudge(
        _hook_state(), {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert guidance == ""
    assert recorded and recorded[0]["applied"] is False and recorded[0]["mode"] == "dark"


def test_hook_live_mode_appends_guidance_and_never_touches_verdict(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner.manager_nudge, "record_nudge", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda report, packet, **kwargs: manager_nudge.NudgeResult(
            action="stop",
            message="Park this and report.",
            rationale="",
            confidence=0.9,
            raw_status="ok",
            report_note="tried simp/ring/omega; suspect missing lemma",
        ),
    )
    manager_check = {"ok": False, "feedback_kind": "error", "output": "error: unsolved goals"}
    state = _hook_state()

    guidance = runner._maybe_manager_nudge(
        state, manager_check, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert "[MANAGER GUIDANCE — advisory]" in guidance
    assert "Park this and report." in guidance
    # The deterministic probe proposal rides along after >=2 failures.
    assert "feasibility probe" in guidance.lower()
    # The verdict is untouched even though the nudge said "stop".
    assert manager_check["ok"] is False
    assert "retry_exhausted" not in manager_check

    # Rate limit: same (theorem, attempt) never calls the LLM twice.
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda *args, **kwargs: pytest.fail("rate limit must skip the second call"),
    )
    assert (
        runner._maybe_manager_nudge(
            state, manager_check, target_symbol="demo", active_file="Demo/Main.lean"
        )
        == ""
    )


def test_hook_quiet_context_never_calls_llm(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda *args, **kwargs: pytest.fail("no struggle -> no LLM call"),
    )
    state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
            "slice": "s",
        }
    }

    assert (
        runner._maybe_manager_nudge(
            state, {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
        )
        == ""
    )


def test_hook_counts_repeated_failure_reasons_and_budget_pressure(monkeypatch):
    """Identical failure reasons and turn-budget pressure both reach the context."""
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "dark")
    monkeypatch.setenv("AGENT_MAX_TURNS", "10")
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    seen_reports: list = []
    monkeypatch.setattr(
        runner.manager_nudge, "request_nudge", lambda report, packet, **kwargs: None
    )
    monkeypatch.setattr(
        runner.manager_nudge,
        "record_nudge",
        lambda result, report, **kwargs: seen_reports.append(report),
    )

    runner._maybe_manager_nudge(
        _hook_state(),
        {"ok": False},
        target_symbol="demo",
        active_file="Demo/Main.lean",
        result={"api_calls": 8, "final_response": ""},
    )

    kinds = {signal.kind for signal in seen_reports[0].signals}
    # Three identical "type mismatch" reasons -> repeat signal (non-deduped).
    assert "repeat_error_signature" in kinds
    assert "budget_pressure" in kinds
