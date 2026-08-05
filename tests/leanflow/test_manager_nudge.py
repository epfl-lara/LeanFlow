"""Persistence-coach tests: message-only output and rejected-turn coverage."""

from __future__ import annotations

import json
from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner
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


def test_kernel_verified_helpers_prioritizes_newest_graph_progress(monkeypatch):
    """A full helper bank must not strand newly verified nodes behind its cap."""
    active_file = "FormalConjectures/ErdosProblems/242.lean"
    target_symbol = "erdos_242_residual_five_mod_five_eq_one"
    target_id = runner.plan_state.node_id_for(target_symbol, active_file)
    nodes = [
        runner.plan_state.GraphNode(
            id=target_id,
            name=target_symbol,
            file=active_file,
            status="proving",
        ),
        *[
            runner.plan_state.GraphNode(
                id=f"helper-{index}",
                name=f"verified_helper_{index}",
                file=active_file,
                status="proved",
            )
            for index in range(8)
        ],
    ]
    monkeypatch.setattr(runner, "plan_state_enabled", lambda: True)
    monkeypatch.setattr(
        runner.plan_state,
        "load_blueprint",
        lambda: runner.plan_state.Blueprint(
            nodes=tuple(nodes),
            edges=tuple(
                runner.plan_state.GraphEdge(
                    source=f"helper-{index}",
                    target=target_id,
                    kind="split_of",
                )
                for index in range(8)
            ),
        ),
    )

    assert runner._kernel_verified_helpers(target_symbol, active_file) == [
        "verified_helper_7",
        "verified_helper_6",
        "verified_helper_5",
        "verified_helper_4",
        "verified_helper_3",
        "verified_helper_2",
    ]


def test_kernel_verified_helpers_excludes_evidence_only_graph_facts(tmp_path, monkeypatch):
    """Do not coach the prover toward circular or saturated finite helpers."""
    active = tmp_path / "Main.lean"
    active.write_text(
        "\n".join(
            [
                "private theorem demo (t : ℕ) : True := by sorry",
                "private theorem demo_mod_five (t : ℕ) (h : t % 5 = 1) : True := by trivial",
                "private theorem demo_mod_seven (t : ℕ) (h : t % 7 = 2) : True := by trivial",
                "private theorem demo_mod_eleven (t : ℕ) (h : t % 11 = 3) : True := by trivial",
                "private theorem demo_mod_thirteen (t : ℕ) (h : t % 13 = 4) : True := by trivial",
                "private theorem demo_mod_seventeen (t : ℕ) (h : t % 17 = 5) : True := by trivial",
                "private theorem demo_from_demo (h : ∀ t : ℕ, True) (t : ℕ) : True := h t",
                "private theorem useful_helper : True := by trivial",
                "",
            ]
        ),
        encoding="utf-8",
    )
    active_file = str(active)
    target_id = runner.plan_state.node_id_for("demo", active_file)
    helper_names = [
        "demo_mod_five",
        "demo_mod_seven",
        "demo_mod_eleven",
        "demo_mod_thirteen",
        "demo_mod_seventeen",
        "demo_from_demo",
    ]
    nodes = [
        runner.plan_state.GraphNode(
            id=target_id,
            name="demo",
            file=active_file,
            status="proving",
        ),
        *[
            runner.plan_state.GraphNode(
                id=runner.plan_state.node_id_for(name, active_file),
                name=name,
                file=active_file,
                status="proved",
            )
            for name in helper_names
        ],
        runner.plan_state.GraphNode(
            id="useful-helper",
            name="useful_helper",
            file=active_file,
            status="proved",
        ),
    ]
    edges = tuple(
        runner.plan_state.GraphEdge(
            source=runner.plan_state.node_id_for(name, active_file),
            target=target_id,
            kind="split_of",
        )
        for name in helper_names
    )
    monkeypatch.setattr(runner, "plan_state_enabled", lambda: True)
    monkeypatch.setattr(
        runner.plan_state,
        "load_blueprint",
        lambda: runner.plan_state.Blueprint(nodes=tuple(nodes), edges=edges),
    )

    assert runner._kernel_verified_helpers("demo", active_file) == [
        "demo_mod_thirteen",
        "demo_mod_eleven",
        "demo_mod_seven",
        "demo_mod_five",
    ]


def test_hook_acknowledges_recent_kernel_verified_graph_evidence_without_target_success(
    monkeypatch,
):
    """Regress the live coach calling a verified-evidence turn the starting line."""
    active_file = "FormalConjectures/ErdosProblems/242.lean"
    target_symbol = "erdos_242_residual_mod_seven_eq_one_normalized_of_mod_five_two"
    target_id = runner.plan_state.node_id_for(target_symbol, active_file)
    helper_name = "erdos_242_probe_q12"
    helper_id = runner.plan_state.node_id_for(helper_name, active_file)
    blueprint = runner.plan_state.Blueprint(
        nodes=(
            runner.plan_state.GraphNode(
                id=target_id,
                name=target_symbol,
                file=active_file,
                status="proving",
            ),
            runner.plan_state.GraphNode(
                id=helper_id,
                name=helper_name,
                file=active_file,
                status="proved",
            ),
        ),
        edges=(
            runner.plan_state.GraphEdge(
                source=helper_id,
                target=target_id,
                kind="evidence",
            ),
        ),
    )
    response = json.dumps(
        {
            "message": "You're right at the starting line; keep executing the assigned route.",
            "commitment": "continue_current_route",
        }
    )
    state = {
        **_hook_state(),
        "current_queue_assignment": {
            "target_symbol": target_symbol,
            "active_file": active_file,
            "slice": f"theorem {target_symbol} : True := by\n  sorry",
        },
    }
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    monkeypatch.setattr(runner, "plan_state_enabled", lambda: True)
    monkeypatch.setattr(runner.plan_state, "load_blueprint", lambda: blueprint)
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )
    monkeypatch.setattr(runner.manager_nudge, "record_nudge", lambda *args, **kwargs: None)

    guidance = runner._maybe_manager_nudge(
        state,
        {
            "ok": False,
            "feedback_kind": "sorry",
            "output": "assigned declaration still contains sorry",
        },
        target_symbol=target_symbol,
        active_file=active_file,
    )

    assert "starting line" not in guidance
    assert "Bank the kernel-verified progress" not in guidance
    assert "Retain the kernel-verified evidence" in guidance
    assert (
        "Progress acknowledged: erdos_242_probe_q12 "
        "(kernel-verified evidence only; assigned target remains unresolved)"
    ) in guidance


def test_hook_off_mode_uses_deterministic_fallback(monkeypatch):
    monkeypatch.delenv("LEANFLOW_MANAGER_LLM_MODE", raising=False)
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda *args, **kwargs: pytest.fail("model must not be called in off mode"),
    )
    monkeypatch.setattr(runner.manager_nudge, "record_nudge", lambda *args, **kwargs: None)

    guidance = runner._maybe_manager_nudge(
        _hook_state(), {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert "[PERSISTENCE COACH]" in guidance
    assert "evidence, not an ending" in guidance


def test_hook_reroute_signal_publishes_one_orchestrator_event_per_epoch(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "off")
    monkeypatch.setattr(runner.manager_nudge, "record_nudge", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    state = _hook_state()
    state["campaign_epoch"] = 7
    state["current_cycle"] = 4

    first = runner._maybe_manager_nudge(
        state,
        {"ok": False, "feedback_kind": "sorry"},
        target_symbol="demo",
        active_file="Demo/Main.lean",
    )
    produced = state["orchestrator_event_watermark"]
    state["current_cycle"] = 5
    state["_failed_attempt_provider_turn"] = {
        "campaign_id": "campaign",
        "epoch": 7,
        "nonce": 2,
    }
    second = runner._maybe_manager_nudge(
        state,
        {"ok": False, "feedback_kind": "sorry"},
        target_symbol="demo",
        active_file="Demo/Main.lean",
    )

    assert "next assigned route" in first
    assert "next assigned route" in second
    assert produced == 1
    assert state["orchestrator_event_watermark"] == produced
    assert state["orchestrator_event_acknowledged"] == 0


def test_hook_dark_mode_logs_model_but_applies_fallback(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "dark")
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    recorded: list[dict] = []
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda report, packet, **kwargs: manager_nudge.NudgeResult(
            message="Commit to the rewrite now.",
            progress_acknowledged=("the failed attempt narrowed the search",),
            commitment="continue_current_route",
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

    assert "[PERSISTENCE COACH]" in guidance
    assert "evidence, not an ending" in guidance
    assert recorded and recorded[0]["applied"] is False and recorded[0]["mode"] == "dark"


def test_hook_live_mode_appends_guidance_and_never_touches_verdict(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner.manager_nudge, "record_nudge", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda report, packet, **kwargs: manager_nudge.NudgeResult(
            message="The current evidence is useful; execute the assigned route now.",
            progress_acknowledged=("three failed simp shapes are now ruled out",),
            commitment="execute_assigned_route",
            raw_status="ok",
        ),
    )
    manager_check = {"ok": False, "feedback_kind": "error", "output": "error: unsolved goals"}
    state = _hook_state()

    guidance = runner._maybe_manager_nudge(
        state, manager_check, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert "[PERSISTENCE COACH]" in guidance
    assert "execute the assigned route" in guidance
    # The deterministic verdict is untouched by the coach.
    assert manager_check["ok"] is False
    assert "retry_exhausted" not in manager_check

    # Rate limit: the same managed turn and gate never call the LLM twice.
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


def test_hook_ungrounded_live_message_uses_positive_fallback(monkeypatch):
    """An invalid model message must not suppress rejected-turn coach coverage."""
    response = json.dumps(
        {
            "message": (
                "First attempt logged — the sorry shows the proof shape compiles. "
                "Keep executing the assigned route."
            ),
            "commitment": "continue_current_route",
        }
    )
    recorded: list[tuple[manager_nudge.NudgeResult | None, dict[str, Any]]] = []
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    monkeypatch.setattr(runner, "_kernel_verified_helpers", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        manager_nudge,
        "run_model_verification_review",
        lambda **kwargs: _FakeReview(response=response),
    )
    monkeypatch.setattr(
        runner.manager_nudge,
        "record_nudge",
        lambda result, report, **kwargs: recorded.append((result, kwargs)),
    )

    guidance = runner._maybe_manager_nudge(
        _hook_state(),
        {
            "ok": False,
            "feedback_kind": "sorry",
            "output": "warning: declaration uses 'sorry'",
        },
        target_symbol="demo",
        active_file="Demo/Main.lean",
    )

    assert "This rejection is evidence, not an ending" in guidance
    assert "shape compiles" not in guidance
    assert "Progress acknowledged:" not in guidance
    assert recorded[0][0] is not None and recorded[0][0].raw_status == "fallback"
    assert recorded[0][1]["fallback_used"] is True
    assert recorded[0][1]["applied"] is False


def test_sequential_no_edit_rejected_turns_each_receive_one_coach(monkeypatch):
    """Do not let an unchanged attempt count suppress a later rejected turn."""
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "off")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "coach-sequential-turns")
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda *args, **kwargs: pytest.fail("off mode must use the deterministic coach"),
    )
    coverage_keys: list[str] = []
    monkeypatch.setattr(
        runner.manager_nudge,
        "record_nudge",
        lambda *args, **kwargs: coverage_keys.append(kwargs["coverage_key"]),
    )
    state = _hook_state()
    state.update(
        {
            "campaign_id": "coach-sequential-turns",
            "campaign_epoch": 13,
            "current_cycle": 2,
            "_failed_attempt_provider_turn": {
                "campaign_id": "coach-sequential-turns",
                "epoch": 13,
                "nonce": 41,
            },
        }
    )
    manager_check = {
        "ok": False,
        "feedback_kind": "sorry",
        "output": "warning: declaration uses 'sorry'",
    }

    first = runner._maybe_manager_nudge(
        state, manager_check, target_symbol="demo", active_file="Demo/Main.lean"
    )
    duplicate = runner._maybe_manager_nudge(
        state, manager_check, target_symbol="demo", active_file="Demo/Main.lean"
    )

    # A checkpoint/restart may present the same completed turn again. Its
    # durable reservation and seen-set still coalesce that presentation.
    resumed = json.loads(json.dumps(state))
    resumed_duplicate = runner._maybe_manager_nudge(
        resumed, manager_check, target_symbol="demo", active_file="Demo/Main.lean"
    )

    # The next provider turn made no source edit, so the failed-attempt count
    # remains unchanged. Its new durable nonce must nevertheless get coverage.
    resumed["current_cycle"] = 3
    resumed["_failed_attempt_provider_turn"] = {
        "campaign_id": "coach-sequential-turns",
        "epoch": 13,
        "nonce": 42,
    }
    second = runner._maybe_manager_nudge(
        resumed, manager_check, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert "[PERSISTENCE COACH]" in first
    assert duplicate == ""
    assert resumed_duplicate == ""
    assert "[PERSISTENCE COACH]" in second
    assert len(coverage_keys) == 2
    assert "turn-41" in coverage_keys[0]
    assert "turn-42" in coverage_keys[1]
    assert coverage_keys[0] != coverage_keys[1]


def test_duplicate_gate_presentation_does_not_duplicate_coach(tmp_path, monkeypatch):
    active = tmp_path / "Main.lean"
    declaration = "theorem demo : True := by\n  sorry\n"
    active.write_text(declaration, encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "coach-dedupe-run")
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner.plan_state, "append_journal_event", lambda event: None)
    monkeypatch.setattr(runner, "_kernel_verified_helpers", lambda *args, **kwargs: [])
    calls: list[object] = []
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda report, packet, **kwargs: calls.append(report) or None,
    )
    monkeypatch.setattr(runner.manager_nudge, "record_nudge", lambda *args, **kwargs: None)
    state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": "theorem demo : True := by\n  exact True.intro",
        }
    }
    live_state = {
        "target_symbol": "demo",
        "active_file": str(active),
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "current_queue_item_slice": "Assigned declaration slice (1-2):\n" + declaration,
        "blocker_summary": "warning: declaration uses 'sorry'",
    }
    manager_check = {
        "ok": False,
        "feedback_kind": "sorry",
        "output": "warning: declaration uses 'sorry'",
    }

    assert runner._remember_failed_attempt(state, live_state, cycle_number=2)
    assert "[PERSISTENCE COACH]" in runner._maybe_manager_nudge(
        state, manager_check, target_symbol="demo", active_file=str(active)
    )
    assert not runner._remember_failed_attempt(state, live_state, cycle_number=2)
    assert (
        runner._maybe_manager_nudge(
            state,
            {"ok": False, "output": "full declaration still contains sorry"},
            target_symbol="demo",
            active_file=str(active),
        )
        == ""
    )

    assert len(calls) == 1
    assert len(state["failed_attempts"]) == 1


def test_two_rejected_check_target_candidates_in_one_turn_each_receive_coaching(
    tmp_path, monkeypatch
):
    """Coach each distinct temporary candidate without replaying one rejection."""
    active = tmp_path / "Main.lean"
    declaration = "theorem demo : True := by\n  sorry\n"
    active.write_text(declaration, encoding="utf-8")
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "off")
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner.plan_state, "append_journal_event", lambda event: None)
    coverage_keys: list[str] = []
    monkeypatch.setattr(
        runner.manager_nudge,
        "record_nudge",
        lambda *args, **kwargs: coverage_keys.append(kwargs["coverage_key"]),
    )
    state = {
        "campaign_id": "two-candidate-coach",
        "campaign_epoch": 1,
        "current_cycle": 7,
        "_failed_attempt_provider_turn": {
            "campaign_id": "two-candidate-coach",
            "epoch": 1,
            "nonce": 19,
        },
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": str(active),
            "slice": declaration,
        },
    }
    live_state = {
        "target_symbol": "demo",
        "active_file": str(active),
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        "current_queue_item_slice": declaration,
        "blocker_summary": "error: unsolved goals",
    }
    candidates = (
        "theorem demo : True := by\n  -- goal: ⊢ True\n  exact candidate_one",
        "theorem demo : True := by\n  -- goal: ⊢ True\n  exact candidate_two",
    )

    for candidate in candidates:
        manager_check = {
            "ok": False,
            "action": "check_target",
            "replacement_matches_target": True,
            "feedback_kind": "error",
            "output": "error: unsolved goals",
            "feedback_lean": candidate,
        }
        assert runner._remember_failed_attempt(
            state,
            live_state,
            cycle_number=7,
            reason="error: unsolved goals",
            candidate_evidence=manager_check,
        )
        first = runner._maybe_manager_nudge(
            state,
            manager_check,
            target_symbol="demo",
            active_file=str(active),
        )
        duplicate = runner._maybe_manager_nudge(
            state,
            {**manager_check, "output": "  ERROR:   unsolved goals  "},
            target_symbol="demo",
            active_file=str(active),
        )
        assert "[PERSISTENCE COACH]" in first
        assert duplicate == ""

    assert len(state["failed_attempts"]) == 2
    assert len(coverage_keys) == 2
    assert coverage_keys[0] != coverage_keys[1]
    assert all("turn-19" in key for key in coverage_keys)


def test_hook_quiet_rejection_still_calls_coach(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    calls: list[object] = []
    monkeypatch.setattr(
        runner.manager_nudge,
        "request_nudge",
        lambda report, packet, **kwargs: calls.append(report) or None,
    )
    monkeypatch.setattr(runner.manager_nudge, "record_nudge", lambda *args, **kwargs: None)
    state = {
        "current_queue_assignment": {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
            "slice": "s",
        }
    }

    guidance = runner._maybe_manager_nudge(
        state, {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )
    assert calls
    assert "[PERSISTENCE COACH]" in guidance


def test_hook_model_exception_emits_and_records_one_fallback(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    model_calls: list[object] = []
    recorded: list[manager_nudge.NudgeResult | None] = []

    def fail_model(report, packet, **kwargs):
        model_calls.append(report)
        raise RuntimeError("provider adapter failed")

    monkeypatch.setattr(runner.manager_nudge, "request_nudge", fail_model)
    monkeypatch.setattr(
        runner.manager_nudge,
        "record_nudge",
        lambda result, report, **kwargs: recorded.append(result),
    )
    state = _hook_state()

    first = runner._maybe_manager_nudge(
        state, {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )
    duplicate = runner._maybe_manager_nudge(
        state, {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert "[PERSISTENCE COACH]" in first
    assert "evidence, not an ending" in first
    assert duplicate == ""
    assert len(model_calls) == 1
    assert len(recorded) == 1
    assert recorded[0] is not None and recorded[0].raw_status == "fallback"


def test_hook_model_timeout_emits_and_records_one_fallback(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "live")
    calls: list[dict[str, Any]] = []
    recorded: list[tuple[manager_nudge.NudgeResult | None, dict[str, Any]]] = []

    def timeout_review(**kwargs):
        calls.append(kwargs)
        return _FakeReview(status="timeout")

    monkeypatch.setattr(manager_nudge, "run_model_verification_review", timeout_review)
    monkeypatch.setattr(
        runner.manager_nudge,
        "record_nudge",
        lambda result, report, **kwargs: recorded.append((result, kwargs)),
    )

    guidance = runner._maybe_manager_nudge(
        _hook_state(), {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert calls[0]["timeout_s"] == manager_nudge.MANAGER_NUDGE_TIMEOUT_DEFAULT_S
    assert "[PERSISTENCE COACH]" in guidance
    assert "evidence, not an ending" in guidance
    assert recorded[0][0] is not None and recorded[0][0].raw_status == "fallback"
    assert recorded[0][1]["fallback_used"] is True


def test_hook_state_enrichment_exception_still_coaches_once(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "off")
    monkeypatch.setattr(
        runner,
        "_queue_manager_from_state",
        lambda state: (_ for _ in ()).throw(OSError("checkpoint unavailable")),
    )
    recorded: list[manager_nudge.NudgeResult | None] = []
    monkeypatch.setattr(
        runner.manager_nudge,
        "record_nudge",
        lambda result, report, **kwargs: recorded.append(result),
    )
    state = _hook_state()

    first = runner._maybe_manager_nudge(
        state, {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )
    duplicate = runner._maybe_manager_nudge(
        state, {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert "[PERSISTENCE COACH]" in first
    assert duplicate == ""
    assert len(recorded) == 1
    assert recorded[0] is not None and recorded[0].raw_status == "fallback"


def test_hook_recording_exception_does_not_poison_or_duplicate_coach(monkeypatch):
    monkeypatch.setenv("LEANFLOW_MANAGER_LLM_MODE", "off")
    record_calls: list[object] = []

    def fail_record(result, report, **kwargs):
        record_calls.append(result)
        raise OSError("all persistence adapters unavailable")

    monkeypatch.setattr(runner.manager_nudge, "record_nudge", fail_record)
    state = _hook_state()

    first = runner._maybe_manager_nudge(
        state, {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )
    duplicate = runner._maybe_manager_nudge(
        state, {"ok": False}, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert "[PERSISTENCE COACH]" in first
    assert duplicate == ""
    assert len(record_calls) == 1


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
