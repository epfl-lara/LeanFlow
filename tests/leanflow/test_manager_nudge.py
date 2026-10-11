"""Persistence-coach tests: message-only output and rejected-turn coverage."""

from __future__ import annotations

import json
from typing import Any

import pytest

from leanflow_cli.workflows import manager_nudge, orchestrator_llm_circuit
from leanflow_cli.workflows.struggle_signals import (
    StruggleContext,
    StruggleReport,
    evaluate,
)


def _fired_report() -> StruggleReport:
    return evaluate(StruggleContext(attempt_count=3))


class _FakeReview:
    def __init__(
        self,
        status="ok",
        response="",
        *,
        provider="",
        model="",
        error="",
        timed_out=False,
    ):
        self.status = status
        self.response = response
        self.provider = provider
        self.model = model
        self.error = error
        self.timed_out = timed_out


def test_nudge_mode_matrix(monkeypatch):
    monkeypatch.delenv("LEANFLOW_MANAGER_LLM_MODE", raising=False)
    monkeypatch.delenv("LEANFLOW_MANAGER_LLM_ENABLED", raising=False)
    assert manager_nudge.nudge_mode() == "off"
    monkeypatch.setenv("LEANFLOW_NATIVE_WORKFLOW_KIND", "prove")
    assert manager_nudge.nudge_mode() == "live"
    monkeypatch.delenv("LEANFLOW_NATIVE_WORKFLOW_KIND", raising=False)
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "dark")
    assert manager_nudge.nudge_mode() == "dark"
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    assert manager_nudge.nudge_mode() == "live"
    monkeypatch.delenv("LEANFLOW_MANAGER_LLM_MODE", raising=False)
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_ENABLED", "1")
    assert manager_nudge.nudge_mode() == "live"


def test_manager_nudge_timeout_is_short_and_hard_bounded(monkeypatch):
    monkeypatch.delenv("LEANFLOW_MANAGER_NUDGE_TIMEOUT_S", raising=False)
    assert manager_nudge.manager_nudge_timeout_s() == manager_nudge.MANAGER_NUDGE_TIMEOUT_DEFAULT_S

    monkeypatch.setenv("LEANFLOW_MANAGER_NUDGE_TIMEOUT_S", "7")
    assert manager_nudge.manager_nudge_timeout_s() == 7

    monkeypatch.setenv("LEANFLOW_MANAGER_NUDGE_TIMEOUT_S", "999")
    assert manager_nudge.manager_nudge_timeout_s() == manager_nudge.MANAGER_NUDGE_TIMEOUT_MAX_S

    monkeypatch.setenv("LEANFLOW_MANAGER_NUDGE_TIMEOUT_S", "invalid")
    assert manager_nudge.manager_nudge_timeout_s() == manager_nudge.MANAGER_NUDGE_TIMEOUT_DEFAULT_S


def test_request_nudge_caps_explicit_timeout(monkeypatch):
    calls: list[dict[str, Any]] = []
    response = json.dumps(
        {
            "message": "Useful evidence gathered; keep executing the assigned route.",
            "commitment": "continue_current_route",
        }
    )

    def review(**kwargs):
        calls.append(kwargs)
        return _FakeReview(response=response)

    monkeypatch.setattr(manager_nudge, "run_model_verification_review", review)

    assert manager_nudge.request_nudge(_fired_report(), {}, timeout_s=45) is not None
    assert calls[0]["timeout_s"] == manager_nudge.MANAGER_NUDGE_TIMEOUT_MAX_S


def test_request_nudge_parses_strict_and_fenced_json(monkeypatch):
    payload = {
        "message": "The setback gave useful evidence; keep executing the assigned route.",
        "commitment": "continue_current_route",
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
        nudge = manager_nudge.request_nudge(
            _fired_report(), {"proved_helpers": ["helper h is kernel-verified"]}
        )
        assert nudge is not None
        assert nudge.commitment == "continue_current_route"
        assert nudge.progress_acknowledged == ("helper h is kernel-verified",)


def test_nudge_prompt_states_the_enforced_message_limit():
    system_prompt, _ = manager_nudge.build_nudge_prompt(_fired_report(), {})

    assert str(manager_nudge.MAX_COACH_MESSAGE_CHARS) in system_prompt
    assert "characters" in system_prompt


def test_nudge_prompt_keeps_long_helper_names_out_of_model_context():
    helper = "erdos_242_" + "very_long_kernel_verified_helper_name_" * 20

    system_prompt, user_prompt = manager_nudge.build_nudge_prompt(
        _fired_report(), {"proved_helpers": [helper]}
    )

    assert helper not in system_prompt
    assert helper not in user_prompt
    assert "Kernel-verified proof-support helper count already banked: 1" in user_prompt
    assert "Do not mention, count, name, or interpret those facts" in user_prompt


def test_nudge_prompt_does_not_request_unverified_progress_acknowledgement():
    system_prompt, user_prompt = manager_nudge.build_nudge_prompt(
        _fired_report(),
        {
            "attempts": [],
            "feedback_kind": "sorry",
            "gate_output": "warning: declaration uses 'sorry'",
            "proved_helpers": [],
        },
    )

    assert "acknowledge concrete progress" not in system_prompt.lower()
    assert "Acknowledge progress" not in user_prompt
    assert "No kernel-verified proof progress is available" in user_prompt
    assert "unchanged `sorry` only marks unresolved source" in user_prompt
    assert "not evidence that a new candidate" in user_prompt


@pytest.mark.parametrize(
    "response",
    [
        "total garbage",
        json.dumps({"message": "x", "progress_acknowledged": [], "commitment": "stop"}),
        json.dumps(
            {
                "message": "   ",
                "progress_acknowledged": [],
                "commitment": "continue_current_route",
            }
        ),
        json.dumps(
            {
                "message": "This is NOT SOLVED. I am deciding to halt further attempts.",
                "progress_acknowledged": [],
                "commitment": "continue_current_route",
            }
        ),
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


def test_research_nudge_reuses_shared_campaign_provider_failure_circuit(monkeypatch, tmp_path):
    """Regress repeated GLM connection waits from the live Erdős campaign."""
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    (tmp_path / ".leanflow" / "workflow-state").mkdir(parents=True)
    (tmp_path / ".leanflow" / "workflow-state" / "summary.json").write_text(
        json.dumps({"campaign": {"campaign_id": "erdos-242-campaign"}}),
        encoding="utf-8",
    )
    calls: list[bool] = []

    def failed(**_kwargs):
        calls.append(True)
        return _FakeReview(
            status="error",
            provider="custom",
            model="zai-org/GLM-5.2",
            error="Connection error.",
        )

    monkeypatch.setattr(manager_nudge, "run_model_verification_review", failed)

    assert manager_nudge.request_nudge(_fired_report(), {}) is None
    orchestrator_llm_circuit.record_provider_failure(
        "error",
        provider="custom",
        model="zai-org/GLM-5.2",
        error="Connection error.",
        task="orchestration",
    )
    assert manager_nudge.request_nudge(_fired_report(), {}) is None

    assert calls == [True]
    state = orchestrator_llm_circuit.circuit_snapshot()
    assert state["campaign_id"] == "erdos-242-campaign"
    assert state["consecutive_failures"] == 2
    assert state["open_until"]


def test_request_nudge_rejects_unverified_progress_claims(monkeypatch):
    response = json.dumps(
        {
            "message": "You built a proof scaffold and made real structural progress.",
            "progress_acknowledged": ["proof scaffold completed"],
            "commitment": "continue_current_route",
        }
    )
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )

    assert manager_nudge.request_nudge(_fired_report(), {"proved_helpers": []}) is None


def test_request_nudge_rejects_live_unchanged_sorry_progress_claim(monkeypatch):
    """Regress the ungrounded message observed in the live Erdős 242 campaign."""
    response = json.dumps(
        {
            "message": (
                "First attempt logged — the sorry tells us the shape compiles but the "
                "core obligation remains open. That's useful evidence about what the "
                "kernel still needs. Keep executing the assigned route."
            ),
            "commitment": "continue_current_route",
        }
    )
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )

    assert (
        manager_nudge.request_nudge(
            _fired_report(),
            {
                "attempts": [],
                "feedback_kind": "sorry",
                "gate_output": "warning: declaration uses 'sorry'",
                "proved_helpers": [],
            },
        )
        is None
    )


def test_verified_helper_does_not_license_model_proof_state_claim(monkeypatch):
    """Keep model prose grounded even when deterministic helper progress exists."""
    response = json.dumps(
        {
            "message": "The sorry shows the proof shape compiles; keep executing the assigned route.",
            "commitment": "continue_current_route",
        }
    )
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )

    assert (
        manager_nudge.request_nudge(
            _fired_report(),
            {"proved_helpers": ["kernel_verified_helper"]},
        )
        is None
    )


def test_request_nudge_preserves_generic_encouragement(monkeypatch):
    response = json.dumps(
        {
            "message": (
                "The recorded blocker is useful evidence; stay positive and keep "
                "executing the assigned route."
            ),
            "commitment": "continue_current_route",
        }
    )
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )

    result = manager_nudge.request_nudge(_fired_report(), {"proved_helpers": []})

    assert result is not None
    assert result.progress_acknowledged == ()


def test_request_nudge_rejects_live_erdos_strategy_selection(monkeypatch):
    """A positive coach still cannot prescribe the Erdős proof shape."""
    response = json.dumps(
        {
            "message": (
                "Strong foundation: five residual-class lemmas are banked. Keep planning "
                "the route: mirror the eq_two/eq_four proof shape for this residue class."
            ),
            "progress_acknowledged": ["erdos_242_residual_mod_seven_eq_two"],
            "commitment": "execute_assigned_route",
        }
    )
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )

    assert (
        manager_nudge.request_nudge(
            _fired_report(),
            {"proved_helpers": ["erdos_242_residual_mod_seven_eq_two"]},
        )
        is None
    )


@pytest.mark.parametrize(
    "message",
    [
        "Use omega on the next proof attempt.",
        "Launch a decomposition job and try a witness identity.",
        "Pivot to the negation route.",
    ],
)
def test_request_nudge_rejects_other_strategy_or_job_instructions(monkeypatch, message):
    response = json.dumps(
        {
            "message": message,
            "progress_acknowledged": [],
            "commitment": "continue_current_route",
        }
    )
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )

    assert manager_nudge.request_nudge(_fired_report(), {"proved_helpers": []}) is None


def test_request_nudge_uses_kernel_owned_progress_acknowledgement(monkeypatch):
    response = json.dumps(
        {
            "message": "Good evidence gathered; stay positive and keep executing the assigned route.",
            "progress_acknowledged": ["invented progress"],
            "commitment": "continue_current_route",
        }
    )
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )

    result = manager_nudge.request_nudge(
        _fired_report(),
        {"proved_helpers": ["helper_verified"]},
    )

    assert result is not None
    assert result.progress_acknowledged == ("helper_verified",)


def test_request_nudge_ignores_legacy_model_progress_field(monkeypatch):
    response = json.dumps(
        {
            "message": "Useful evidence gathered; keep executing the assigned route.",
            "progress_acknowledged": "malformed and invented",
            "commitment": "continue_current_route",
        }
    )
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )

    result = manager_nudge.request_nudge(
        _fired_report(),
        {"proved_helpers": ["kernel_verified_helper"]},
    )

    assert result is not None
    assert result.progress_acknowledged == ("kernel_verified_helper",)


@pytest.mark.parametrize(
    "text",
    [
        "NOT SOLVED. The blocker remains. Deciding to halt further attempts.",
        "Concrete blocker remains; I cannot proceed.",
        "I am unable to complete this proof and give up.",
    ],
)
def test_old_numina_surrender_phrases_are_protocol_violations(text):
    assert manager_nudge.contains_surrender_language(text)


def test_fallback_is_positive_and_message_only():
    fallback = manager_nudge.fallback_nudge(
        {"proved_helpers": ["h₁"], "assigned_route": "decompose"}
    )
    assert fallback.is_usable()
    assert fallback.commitment == "continue_current_route"
    assert "decompose" in fallback.message
    assert "h₁" in fallback.progress_acknowledged

    no_helpers = manager_nudge.fallback_nudge({"proved_helpers": [], "assigned_route": "plan"})
    assert "diagnostic evidence" in no_helpers.message
    assert "effort" in no_helpers.message
    assert "verified progress" not in no_helpers.message
    assert no_helpers.progress_acknowledged == ()


def test_reroute_fallback_waits_for_next_assignment_instead_of_exhausted_route():
    fallback = manager_nudge.fallback_nudge(
        {
            "proved_helpers": [],
            "assigned_route": "plan",
            "reroute_requested": True,
        }
    )

    assert fallback.commitment == "execute_assigned_route"
    assert "next assigned route" in fallback.message
    assert "assigned plan" not in fallback.message


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


def test_record_nudge_keeps_summary_when_activity_persistence_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(manager_nudge, "workflow_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        manager_nudge,
        "append_workflow_activity",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("activity unavailable")),
    )

    manager_nudge.record_nudge(None, _fired_report(), applied=False, mode="off")

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert len(summary["manager_nudges"]) == 1
    assert summary["campaign_metrics"]["coach_messages"] == 1


def test_record_nudge_uses_activity_when_summary_persistence_fails(monkeypatch):
    events: list[tuple] = []
    monkeypatch.setattr(
        manager_nudge,
        "update_json_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("summary unavailable")),
    )
    monkeypatch.setattr(
        manager_nudge,
        "append_workflow_activity",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    manager_nudge.record_nudge(None, _fired_report(), applied=False, mode="off")

    assert len(events) == 1
    assert events[0][0][0] == "manager-nudge"


# --- runner hook -----------------------------------------------------------
