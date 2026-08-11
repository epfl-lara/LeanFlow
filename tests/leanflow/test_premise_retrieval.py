"""§4.6 tests: premise retrieval injected at queue assignment (flag-gated)."""

from __future__ import annotations

from typing import Any

import pytest

from leanflow_cli.native import native_runner as runner


@pytest.fixture()
def retrieval_enabled(monkeypatch):
    monkeypatch.setenv("LEANFLOW_PREMISE_RETRIEVAL", "1")
    monkeypatch.setattr(runner, "_record_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_project_root", lambda: "/tmp/project")


def _stub_suggest(monkeypatch, calls: list, candidates=None, raise_error=False):
    def fake(file_path, theorem_id, *, cwd=None, **kwargs):
        calls.append((file_path, theorem_id))
        if raise_error:
            raise RuntimeError("provider down")
        return {
            "success": True,
            "candidates": candidates
            or [
                {"name": "abs_sub_abs_le", "signature": "|a| - |b| <= |a - b|"},
                {"name": "abs_abs", "signature": "| |a| | = |a|"},
            ],
            "degraded_reasons": [],
        }

    monkeypatch.setattr(runner, "lean_lemma_suggest", fake)


def test_flag_off_never_calls_retrieval(monkeypatch):
    monkeypatch.delenv("LEANFLOW_PREMISE_RETRIEVAL", raising=False)
    calls: list = []
    _stub_suggest(monkeypatch, calls)
    autonomy_state: dict[str, Any] = {}

    hints = runner._inject_premise_hints(
        autonomy_state, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert hints == []
    assert calls == []
    assert "premise_hints" not in autonomy_state


def test_hints_computed_once_per_assignment(retrieval_enabled, monkeypatch):
    calls: list = []
    _stub_suggest(monkeypatch, calls)
    autonomy_state: dict[str, Any] = {}

    first = runner._inject_premise_hints(
        autonomy_state, target_symbol="demo", active_file="Demo/Main.lean"
    )
    second = runner._inject_premise_hints(
        autonomy_state, target_symbol="demo", active_file="Demo/Main.lean"
    )

    assert first == [
        "abs_sub_abs_le: |a| - |b| <= |a - b|",
        "abs_abs: | |a| | = |a|",
    ]
    assert second == first
    assert len(calls) == 1  # cached: rate-limited providers are hit once

    read_back = runner._premise_hints_for(
        autonomy_state, target_symbol="demo", active_file="Demo/Main.lean"
    )
    assert read_back == first


def test_assignment_retrieval_uses_fast_nonblocking_profile(retrieval_enabled, monkeypatch):
    captured: dict[str, Any] = {}

    def fake(file_path, theorem_id, *, cwd=None, **kwargs):
        captured.update(kwargs)
        return {"success": True, "candidates": [], "degraded_reasons": []}

    monkeypatch.setattr(runner, "lean_lemma_suggest", fake)

    runner._inject_premise_hints({}, target_symbol="demo", active_file="Demo/Main.lean")

    assert captured == {
        "max_candidates": 6,
        "max_queries": 2,
        "search_modes": ("regex",),
        "use_proof_context": False,
    }


def test_failure_caches_empty_and_never_raises(retrieval_enabled, monkeypatch):
    calls: list = []
    _stub_suggest(monkeypatch, calls, raise_error=True)
    autonomy_state: dict[str, Any] = {}

    assert (
        runner._inject_premise_hints(
            autonomy_state, target_symbol="demo", active_file="Demo/Main.lean"
        )
        == []
    )
    assert (
        runner._inject_premise_hints(
            autonomy_state, target_symbol="demo", active_file="Demo/Main.lean"
        )
        == []
    )
    assert len(calls) == 1


def test_queue_block_renders_premise_candidates(retrieval_enabled, monkeypatch):
    calls: list = []
    _stub_suggest(monkeypatch, calls)
    autonomy_state: dict[str, Any] = {}
    runner._inject_premise_hints(autonomy_state, target_symbol="demo", active_file="Demo/Main.lean")
    live_state = {
        "target_symbol": "demo",
        "active_file": "Demo/Main.lean",
        "active_file_label": "Demo/Main.lean",
        "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
    }

    text = runner._queue_assignment_block(live_state, autonomy_state)

    assert "Premise candidates (auto-retrieved at assignment; verify before use):" in text
    assert "- abs_sub_abs_le: |a| - |b| <= |a - b|" in text


def test_cached_hints_do_not_render_after_flag_disabled(retrieval_enabled, monkeypatch):
    calls: list = []
    _stub_suggest(monkeypatch, calls)
    autonomy_state: dict[str, Any] = {}
    runner._inject_premise_hints(autonomy_state, target_symbol="demo", active_file="Demo/Main.lean")

    monkeypatch.delenv("LEANFLOW_PREMISE_RETRIEVAL", raising=False)
    text = runner._queue_assignment_block(
        {
            "target_symbol": "demo",
            "active_file": "Demo/Main.lean",
            "active_file_label": "Demo/Main.lean",
            "current_queue_item": {"label": "demo", "reasons": ["contains sorry"]},
        },
        autonomy_state,
    )

    assert "Premise candidates" not in text


def test_research_mode_lifts_theorem_budget_default(monkeypatch):
    monkeypatch.delenv("LEANFLOW_THEOREM_BUDGET_STEPS", raising=False)
    monkeypatch.setenv("LEANFLOW_RESEARCH_MODE", "1")
    assert runner._theorem_budget_steps() == 0

    monkeypatch.setenv("LEANFLOW_THEOREM_BUDGET_STEPS", "250")
    assert runner._theorem_budget_steps() == 250

    monkeypatch.delenv("LEANFLOW_RESEARCH_MODE", raising=False)
    monkeypatch.delenv("LEANFLOW_THEOREM_BUDGET_STEPS", raising=False)
    assert runner._theorem_budget_steps() == 600
