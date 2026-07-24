"""Tests for the goal->candidate-lemma retriever backend (lean_lemma_suggest).

Cover query derivation from a goal (conclusion head symbol + operator, namespaced short form,
hypothesis-type query) and the dedupe/rank pipeline, with the proof-context / inspect / search
backends mocked on ``lean_services`` (the module the retriever resolves them off lazily).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from leanflow_cli.lean import lean_lemma_suggest as lls
from leanflow_cli.lean import lean_proof_context_circuit as circuit
from leanflow_cli.lean import lean_services
from leanflow_cli.workflows import campaign_epoch

PROOF_CONTEXT_TOOL = "mcp_lean_proof_auto_get_proof_context"


def test_derive_queries_uses_head_symbol_operator_and_hypotheses():
    queries = lls.derive_queries(
        goal="⊢ List.length (l ++ m) ≤ n",
        hypotheses=["h : Nat.Prime p"],
        statement="theorem demo : List.length (l ++ m) ≤ n := by",
    )

    assert 2 <= len(queries) <= lls.MAX_DERIVED_QUERIES
    # Head symbol of the conclusion is the strongest probe and pairs with its operator.
    assert queries[0] == "List.length ≤"
    # The namespaced short form is offered so bare-name providers still match.
    assert "length" in queries
    # A hypothesis-type query surfaces the symbol the goal is proved *from*.
    assert any("Nat.Prime" in q for q in queries)


def test_derive_queries_falls_back_to_statement_when_goal_empty():
    queries = lls.derive_queries(
        goal="",
        hypotheses=[],
        statement="theorem foo : Continuous f := by",
    )

    assert queries
    assert any("Continuous" in q for q in queries)


def test_derive_queries_rejects_binder_only_erdos_goal_and_uses_statement_semantics():
    statement = (
        "private lemma erdos_242_residual_five_mod_five_eq_one "
        "(t : ℕ) (ht : t % 5 = 1) :\n"
        "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
        "      (4 / ((168 * t + 121 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z"
    )

    queries = lls.derive_queries(
        # This is the low-signal goal text returned by the live fallback path.
        goal="ht",
        hypotheses=["t : ℕ", "ht : t % 5 = 1"],
        statement=statement,
    )

    assert "ht" not in queries
    assert any("Nat" in query and "modulo" in query for query in queries)
    assert "rational reciprocal" in queries
    assert any("erdos_242_residual_five_mod_five_eq_one" in query for query in queries)


def test_rank_candidates_orders_by_goal_symbol_overlap():
    goal_symbols = ["List.length", "Nat"]
    raw_hits = [
        ("List.length", "Nat.succ_le : n ≤ Nat.succ n", {"provider": "leanfinder"}),
        (
            "List.length",
            "List.length_append : List.length (l ++ m) = List.length l + List.length m",
            {"provider": "leanexplore", "name": "List.length_append"},
        ),
    ]

    ranked = lls._rank_candidates(raw_hits, goal_symbols=goal_symbols)

    # The append-length lemma shares the highest-weighted head symbol, so it ranks first.
    assert ranked[0]["name"] == "List.length_append"
    assert "List.length" in ranked[0]["why_relevant"]
    assert ranked[0]["provider"] == "leanexplore"


def test_rank_candidates_dedupes_repeated_hits_across_queries():
    raw_hits = [
        ("q1", "List.length_append : ...", {"name": "List.length_append"}),
        ("q2", "List.length_append : ...", {"name": "List.length_append"}),
    ]

    ranked = lls._rank_candidates(raw_hits, goal_symbols=["List.length"])

    assert len(ranked) == 1
    assert ranked[0]["name"] == "List.length_append"


def test_lean_lemma_suggest_end_to_end_with_mocked_backends(monkeypatch):
    monkeypatch.setattr(
        lean_services,
        "lean_proof_context",
        lambda file_path, theorem_id, cwd=None: {
            "success": True,
            "theorem_statement": "theorem demo : List.length (l ++ m) ≤ n := by",
            "goals": "⊢ List.length (l ++ m) ≤ n",
            "hypotheses": [{"name": "h", "type": "n = 5"}],
            "degraded_reasons": [],
        },
    )

    captured_queries: list[tuple[str, str]] = []

    def _fake_search(query, *, mode, cwd=None, limit=10, file_path=""):
        captured_queries.append((query, mode))
        # Only the head-symbol query returns a relevant hit; others are empty.
        if query.startswith("List.length"):
            return SimpleNamespace(
                results=[
                    {
                        "provider": "leanexplore",
                        "name": "List.length_append",
                        "match": "List.length_append : List.length (l ++ m) = ...",
                    }
                ]
            )
        return SimpleNamespace(results=[])

    monkeypatch.setattr(lean_services, "lean_search", _fake_search)

    payload = lls.lean_lemma_suggest("Demo/Main.lean", "demo", cwd="/tmp/project")

    assert payload["success"] is True
    assert payload["queries"]
    assert payload["candidates"][0]["name"] == "List.length_append"
    assert payload["candidates"][0]["provider"] == "leanexplore"
    assert "List.length" in payload["candidates"][0]["why_relevant"]
    # Both semantic and type-pattern modes were exercised per query.
    assert {"semantic", "type-pattern"} <= {mode for _, mode in captured_queries}


def test_lean_lemma_suggest_does_not_search_erdos_local_binder_name(monkeypatch):
    statement = (
        "private lemma erdos_242_residual_five_mod_five_eq_one "
        "(t : ℕ) (ht : t % 5 = 1) :\n"
        "    ∃ x y z : ℕ, 1 ≤ x ∧ x < y ∧ y < z ∧\n"
        "      (4 / ((168 * t + 121 : ℕ) : ℚ)) = 1 / x + 1 / y + 1 / z"
    )
    monkeypatch.setattr(
        lean_services,
        "lean_proof_context",
        lambda file_path, theorem_id, cwd=None: {
            "success": True,
            "status": "local-fallback",
            "theorem_statement": statement,
            "goals": "ht",
            "hypotheses": ["t : ℕ", "ht : t % 5 = 1"],
        },
    )
    searched: list[str] = []

    def _fake_search(query, *, mode, cwd=None, limit=10, file_path=""):
        searched.append(query)
        if query == "ht":
            return SimpleNamespace(
                results=[
                    {
                        "name": "HasDerivWithinAt.cl",
                        "match": "HasDerivWithinAt.cl : HasDerivWithinAt f f' s x",
                    }
                ]
            )
        if query == "Nat modulo" and mode == "semantic":
            return SimpleNamespace(
                results=[
                    {
                        "name": "Nat.mod_add_div",
                        "match": "Nat.mod_add_div : m % k + k * (m / k) = m",
                    }
                ]
            )
        return SimpleNamespace(results=[])

    monkeypatch.setattr(lean_services, "lean_search", _fake_search)

    payload = lls.lean_lemma_suggest(
        "ErdosProblems/242.lean", "erdos_242_residual_five_mod_five_eq_one"
    )

    assert "ht" not in searched
    assert payload["candidates"][0]["name"] == "Nat.mod_add_div"
    assert "Nat" in payload["goal_symbols"]
    assert "ht" not in payload["goal_symbols"]


def test_lean_lemma_suggest_falls_back_to_inspect_goals(monkeypatch):
    monkeypatch.setattr(
        lean_services,
        "lean_proof_context",
        lambda file_path, theorem_id, cwd=None: {
            "success": True,
            "theorem_statement": "",
            "hypotheses": [],
            "degraded_reasons": ["proof context MCP unavailable"],
        },
    )
    monkeypatch.setattr(
        lean_services,
        "lean_inspect",
        lambda target, cwd=None, symbol=None: SimpleNamespace(goals="⊢ Continuous f"),
    )

    seen: list[str] = []

    def _fake_search(query, *, mode, cwd=None, limit=10, file_path=""):
        seen.append(query)
        return SimpleNamespace(results=[])

    monkeypatch.setattr(lean_services, "lean_search", _fake_search)

    payload = lls.lean_lemma_suggest("Demo/Main.lean", "demo")

    # The goal used for query derivation came from lean_inspect, not proof-context.
    assert any("Continuous" in q for q in payload["queries"])
    assert any("Continuous" in q for q in seen)
    assert payload["success"] is False  # no results, but queries were derived
    assert "no candidate lemmas found for the derived queries" in payload["degraded_reasons"]


def test_lean_lemma_suggest_honors_bounded_search_profile(monkeypatch):
    monkeypatch.setattr(
        lean_services,
        "lean_proof_context",
        lambda file_path, theorem_id, cwd=None: {
            "theorem_statement": "theorem demo : List.length (l ++ m) ≤ n := by",
            "goals": "⊢ List.length (l ++ m) ≤ n",
            "hypotheses": [{"name": "h", "type": "Nat.Prime p"}],
        },
    )
    calls: list[tuple[str, str]] = []

    def _fake_search(query, *, mode, cwd=None, limit=10, file_path=""):
        calls.append((query, mode))
        return SimpleNamespace(results=[])

    monkeypatch.setattr(lean_services, "lean_search", _fake_search)

    payload = lls.lean_lemma_suggest(
        "Demo/Main.lean",
        "demo",
        max_queries=1,
        search_modes=("regex",),
    )

    assert len(payload["queries"]) == 1
    assert payload["search_modes"] == ["regex"]
    assert calls == [(payload["queries"][0], "regex")]


def test_lean_lemma_suggest_can_skip_expensive_proof_context(monkeypatch, tmp_path):
    source = tmp_path / "Demo.lean"
    source.write_text("theorem demo : Nat.succ 0 = 1 := by sorry\n", encoding="utf-8")
    monkeypatch.setattr(
        lls,
        "_proof_context",
        lambda *_args, **_kwargs: pytest.fail("proof context must not run"),
    )
    monkeypatch.setattr(
        lls,
        "_inspect_goals",
        lambda *_args, **_kwargs: pytest.fail("goal inspection must not run"),
    )
    monkeypatch.setattr(
        lls,
        "_run_search",
        lambda query, **_kwargs: [
            {"name": "Nat.succ_eq_add_one", "match": f"{query} Nat.succ_eq_add_one"}
        ],
    )

    payload = lls.lean_lemma_suggest(
        str(source),
        "demo",
        max_queries=1,
        search_modes=("regex",),
        use_proof_context=False,
    )

    assert payload["used_proof_context"] is False
    assert payload["queries"]
    assert payload["candidates"]


def test_lean_lemma_suggest_uses_circuit_open_local_context_without_probe(monkeypatch, tmp_path):
    """A local-context suggestion must not reacquire Lean admission via inspect."""
    project = tmp_path / "DemoProject"
    project.mkdir()
    source = project / "Demo.lean"
    source.write_text(
        "theorem demo (n : Nat) : n + 0 = n := by\n  simpa\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LEANFLOW_PROJECT_ROOT", str(project))
    monkeypatch.setenv("LEANFLOW_WORKFLOW_RUN_ID", "lemma-suggest-circuit")
    monkeypatch.setattr(lean_services, "_DISABLED_MCP_TOOLS_BY_RUN", {})
    campaign_epoch.ensure_campaign({})
    assert circuit.record_timeout(
        PROOF_CONTEXT_TOOL,
        "MCP call failed: TimeoutError: declaration scan",
        cwd=project,
        file_path=str(source),
        theorem_id="demo",
        elapsed_s=121.0,
    )
    monkeypatch.setattr(
        lean_services,
        "probe_capabilities",
        lambda *_args, **_kwargs: pytest.fail(
            "circuit-open lemma suggestion must bypass capability probing"
        ),
    )
    monkeypatch.setattr(
        lls,
        "_inspect_goals",
        lambda *_args, **_kwargs: pytest.fail(
            "local declaration context must not fall through to lean_inspect"
        ),
    )
    monkeypatch.setattr(lls, "_run_search", lambda *_args, **_kwargs: [])

    payload = lls.lean_lemma_suggest(str(source), "demo", cwd=project)

    assert payload["used_proof_context"] is True
    assert payload["queries"]
    assert any("without capability probing" in reason for reason in payload["degraded_reasons"])


def test_lean_lemma_suggest_requires_file_and_theorem():
    payload = lls.lean_lemma_suggest("", "")

    assert payload["success"] is False
    assert payload["candidates"] == []
    assert "file_path and theorem_id are required" in payload["degraded_reasons"]
