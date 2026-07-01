"""Tests for the goal->candidate-lemma retriever backend (lean_lemma_suggest).

Cover query derivation from a goal (conclusion head symbol + operator, namespaced short form,
hypothesis-type query) and the dedupe/rank pipeline, with the proof-context / inspect / search
backends mocked on ``lean_services`` (the module the retriever resolves them off lazily).
"""

from __future__ import annotations

from types import SimpleNamespace

from leanflow_cli.lean import lean_lemma_suggest as lls
from leanflow_cli.lean import lean_services


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


def test_lean_lemma_suggest_requires_file_and_theorem():
    payload = lls.lean_lemma_suggest("", "")

    assert payload["success"] is False
    assert payload["candidates"] == []
    assert "file_path and theorem_id are required" in payload["degraded_reasons"]
