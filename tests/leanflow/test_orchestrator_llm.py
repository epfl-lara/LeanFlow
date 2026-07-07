"""Phase 4 (6/6) tests: LLM routing layer plumbing (dark until Phase 6).

The floor stays authoritative in every failure mode: flag off, provider
unavailable, unparseable reply, out-of-vocabulary route, and — the
non-negotiable — any attempt to downgrade a working route to park/escalate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from leanflow_cli.workflows import orchestrator_llm as oll
from leanflow_cli.workflows.orchestrator import OrchestratorRoute, RouteContext


def _ctx(**overrides) -> RouteContext:
    defaults = dict(
        trigger="stall",
        target_symbol="demo",
        active_file="Demo.lean",
        declaration_queue_total=2,
    )
    defaults.update(overrides)
    return RouteContext(**defaults)


FLOOR = OrchestratorRoute(route="plan", reason="stall consult")


# ---------------------------------------------------------------------------
# Enable flag: dark by default
# ---------------------------------------------------------------------------


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", raising=False)
    assert oll.orchestrator_llm_enabled() is False
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", "0")
    assert oll.orchestrator_llm_enabled() is False
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", "1")
    assert oll.orchestrator_llm_enabled() is True


def test_flag_off_never_calls_provider(monkeypatch):
    monkeypatch.delenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", raising=False)

    def boom(**kwargs):
        raise AssertionError("provider must not be called when the flag is off")

    monkeypatch.setattr(oll, "run_model_verification_review", boom)
    assert oll.llm_route(_ctx(), FLOOR) == (None, "")


# ---------------------------------------------------------------------------
# Decision parser: fence-tolerant, strict vocabulary
# ---------------------------------------------------------------------------


def test_parse_fenced_json():
    text = 'Thinking...\n```json\n{"route": "decompose", "reason": "split it"}\n```\ndone'
    decision = oll.parse_llm_decision(text)
    assert decision is not None
    assert decision["route"] == "decompose"
    assert decision["reason"] == "split it"


def test_parse_bare_json_with_prose():
    decision = oll.parse_llm_decision('I decide: {"route": "negate", "reason": "smells false"} ok?')
    assert decision is not None and decision["route"] == "negate"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no json here",
        "{not valid json}",
        '["route", "plan"]',  # JSON but not an object
        '{"route": "give-up", "reason": "tired"}',  # out-of-vocabulary route
        '{"reason": "no route at all"}',
        # ask-human is a floor route but NOT in the §4.4 LLM vocabulary:
        # it exists only as the runtime's own fail-closed conversion.
        '{"route": "ask-human", "reason": "let a human decide"}',
    ],
)
def test_parse_rejects_garbage(text):
    assert oll.parse_llm_decision(text) is None


def test_parse_normalizes_payload():
    decision = oll.parse_llm_decision(
        '{"route": "decompose", "reason": "'
        + "r" * 600
        + '", "statements_to_state": [{"name": "h1"}, "not-a-mapping"],'
        ' "probes": [17, {"archetype": "negation"}]}'
    )
    assert decision is not None
    assert len(decision["reason"]) == 500
    assert decision["statements_to_state"] == [{"name": "h1"}]
    assert decision["probes"] == [{"archetype": "negation"}]


def test_parse_is_total_on_scalar_list_fields():
    # Valid JSON with the wrong shapes must degrade, never raise.
    decision = oll.parse_llm_decision(
        '{"route": "decompose", "reason": "ok", "statements_to_state": 5, "probes": "nope"}'
    )
    assert decision is not None
    assert decision["statements_to_state"] == []
    assert decision["probes"] == []


# ---------------------------------------------------------------------------
# llm_route: floor fallback notes + the upgrade-only rule
# ---------------------------------------------------------------------------


@pytest.fixture()
def llm_on(monkeypatch):
    monkeypatch.setenv("LEANFLOW_ORCHESTRATOR_LLM_ENABLED", "1")


def _fake_provider(monkeypatch, response: str):
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(response=response)

    monkeypatch.setattr(oll, "run_model_verification_review", fake)
    return calls


def test_refine_accepted_with_llm_source(llm_on, monkeypatch):
    calls = _fake_provider(
        monkeypatch,
        '{"route": "decompose", "reason": "two independent halves",'
        ' "target_node": "demo", "statements_to_state": [{"name": "demo_left"}]}',
    )
    route, note = oll.llm_route(_ctx(), FLOOR)
    assert note == ""
    assert route is not None
    assert route.route == "decompose"
    assert route.source == "llm"
    assert route.target["statements_to_state"] == [{"name": "demo_left"}]
    assert calls[0]["task"] == oll.ORCHESTRATION_TASK


def test_downgrade_to_park_rejected(llm_on, monkeypatch):
    _fake_provider(monkeypatch, '{"route": "park", "reason": "too hard"}')
    assert oll.llm_route(_ctx(), FLOOR) == (None, "llm-downgrade-rejected")

    _fake_provider(monkeypatch, '{"route": "escalate", "reason": "surely false"}')
    assert oll.llm_route(_ctx(), FLOOR) == (None, "llm-downgrade-rejected")


def test_protected_floor_is_llm_immutable(llm_on, monkeypatch):
    """park/escalate/ask-human floors skip the consult entirely — escalate
    encodes kernel-proved negation evidence, ask-human a fidelity integrity
    stop; no model answer may renegotiate either."""

    def boom(**kwargs):
        raise AssertionError("protected floor routes must never consult the LLM")

    monkeypatch.setattr(oll, "run_model_verification_review", boom)
    for protected in ("park", "escalate", "ask-human"):
        floor = OrchestratorRoute(route=protected, reason="evidence-backed stop")
        assert oll.llm_route(_ctx(), floor) == (None, "floor-protected")


def test_parse_failure_keeps_floor(llm_on, monkeypatch):
    _fake_provider(monkeypatch, "I could not decide, sorry.")
    assert oll.llm_route(_ctx(), FLOOR) == (None, "parse-failure")


def test_provider_exception_is_unavailable(llm_on, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(oll, "run_model_verification_review", boom)
    assert oll.llm_route(_ctx(), FLOOR) == (None, "unavailable")


def test_provider_failure_status_is_unavailable_not_parse_failure(llm_on, monkeypatch):
    """The provider layer swallows failures into result.status — a swallowed
    outage must report 'unavailable', never masquerade as 'parse-failure'."""
    for status in ("unavailable", "error", "no_answer"):
        monkeypatch.setattr(
            oll,
            "run_model_verification_review",
            lambda status=status, **kwargs: SimpleNamespace(response="", status=status),
        )
        assert oll.llm_route(_ctx(), FLOOR) == (None, "unavailable")

    # And an explicit ok status still parses normally.
    monkeypatch.setattr(
        oll,
        "run_model_verification_review",
        lambda **kwargs: SimpleNamespace(
            response='{"route": "plan", "reason": "replan"}', status="ok"
        ),
    )
    route, note = oll.llm_route(_ctx(), FLOOR)
    assert note == "" and route is not None and route.route == "plan"


# ---------------------------------------------------------------------------
# Prompt composition: research mode is context-rich
# ---------------------------------------------------------------------------


def test_prompt_includes_plan_md_only_in_research_mode():
    plan_text = "## Frontier\n- demo_left"
    _system, easy = oll.build_llm_prompt(_ctx(), FLOOR, plan_md_text=plan_text)
    assert plan_text not in easy
    _system, research = oll.build_llm_prompt(
        _ctx(research_mode=True), FLOOR, plan_md_text=plan_text
    )
    assert plan_text in research
    assert "Deterministic floor proposes: plan" in research
    assert "never choose park or escalate unless the floor already proposed it" in research
    assert "ask-human" not in research  # not in the §4.4 LLM vocabulary
