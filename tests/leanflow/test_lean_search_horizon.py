"""Characterize managed search visibility at the assigned declaration boundary."""

from __future__ import annotations

from leanflow_cli.lean import lean_search_horizon


def _payload(*results):
    return {
        "success": True,
        "query": "demo",
        "mode": "local",
        "attempted_providers": ["leanexplore-local"],
        "results": list(results),
        "degraded_reasons": [],
    }


def test_future_same_file_result_uses_current_disk_line_not_stale_provider_line(tmp_path):
    active = tmp_path / "FormalConjectures" / "ErdosProblems" / "242.lean"
    active.parent.mkdir(parents=True)
    active.write_text(
        "theorem assigned : True := by\n"
        "  sorry\n\n"
        "theorem future_result : True := by\n"
        "  trivial\n",
        encoding="utf-8",
    )
    result = {
        "provider": "leanexplore-local",
        "name": "Erdos242.future_result",
        "module": "FormalConjectures.ErdosProblems.«242»",
        "source_link": ("https://example.test/FormalConjectures/ErdosProblems/242.lean#L1-L2"),
    }

    projected = lean_search_horizon.partition_source_order_results(
        _payload(result),
        active_file=str(active),
        target_symbol="assigned",
        cwd=str(tmp_path),
    )

    assert projected["results"] == []
    assert projected["source_order_inaccessible_count"] == 1
    inaccessible = projected["source_order_inaccessible_results"][0]
    assert inaccessible["provider"] == "leanexplore-local"
    assert inaccessible["source_link"].endswith("#L1-L2")
    assert inaccessible["source_access"] == "future_same_file_unavailable"
    assert inaccessible["usable_in_assigned_proof"] is False
    assert inaccessible["assigned_source_line"] == 1
    assert inaccessible["current_source_line"] == 4
    assert "do not submit" in projected["source_order_guidance"].lower()


def test_prior_same_file_declaration_remains_usable(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem prior_result : True := by\n"
        "  trivial\n\n"
        "theorem assigned : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    result = {
        "provider": "leanexplore-local",
        "name": "Demo.prior_result",
        "module": "Demo",
    }

    projected = lean_search_horizon.partition_source_order_results(
        _payload(result),
        active_file=str(active),
        target_symbol="assigned",
        cwd=str(tmp_path),
    )

    assert projected["results"] == [result]
    assert "source_order_inaccessible_results" not in projected


def test_assigned_same_file_declaration_is_not_a_recursive_premise(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem prior_result : True := by\n"
        "  trivial\n\n"
        "theorem assigned : True := by\n"
        "  sorry\n",
        encoding="utf-8",
    )
    result = {
        "provider": "project-rg",
        "file": str(active),
        "line": 4,
        "preview": "theorem assigned : True := by",
    }

    projected = lean_search_horizon.partition_source_order_results(
        _payload(result),
        active_file=str(active),
        target_symbol="assigned",
        cwd=str(tmp_path),
    )

    assert projected["results"] == []
    inaccessible = projected["source_order_inaccessible_results"][0]
    assert inaccessible["source_access"] == "assigned_declaration_unavailable"
    assert inaccessible["current_source_line"] == 4
    assert "cannot use itself recursively" in inaccessible["reason"]


def test_imported_result_remains_usable(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem assigned : True := by\n"
        "  sorry\n\n"
        "theorem future_result : True := by\n"
        "  trivial\n",
        encoding="utf-8",
    )
    result = {
        "provider": "leanexplore-local",
        "name": "Nat.ModEq",
        "module": "Mathlib.Data.Nat.ModEq",
        "source_link": "https://example.test/Mathlib/Data/Nat/ModEq.lean#L35-L37",
    }

    projected = lean_search_horizon.partition_source_order_results(
        _payload(result),
        active_file=str(active),
        target_symbol="assigned",
        cwd=str(tmp_path),
    )

    assert projected["results"] == [result]


def test_ambiguous_local_short_name_fails_open(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem assigned : True := by\n"
        "  sorry\n\n"
        "namespace A\n"
        "theorem duplicate : True := by trivial\n"
        "end A\n\n"
        "namespace B\n"
        "theorem duplicate : True := by trivial\n"
        "end B\n",
        encoding="utf-8",
    )
    result = {
        "provider": "leanexplore-local",
        "name": "A.duplicate",
        "module": "Demo",
    }

    projected = lean_search_horizon.partition_source_order_results(
        _payload(result),
        active_file=str(active),
        target_symbol="assigned",
        cwd=str(tmp_path),
    )

    assert projected["results"] == [result]


def test_project_rg_declaration_headers_respect_source_order(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem prior_result : True := by\n"
        "  trivial\n\n"
        "theorem assigned : True := by\n"
        "  sorry\n\n"
        "theorem future_result : True := by\n"
        "  trivial\n",
        encoding="utf-8",
    )
    prior = {
        "provider": "project-rg",
        "file": str(active),
        "line": 1,
        "preview": "theorem prior_result : True := by",
    }
    future = {
        "provider": "project-rg",
        "file": str(active),
        "line": 7,
        "preview": "theorem future_result : True := by",
    }

    projected = lean_search_horizon.partition_source_order_results(
        _payload(prior, future),
        active_file=str(active),
        target_symbol="assigned",
        cwd=str(tmp_path),
    )

    assert projected["results"] == [prior]
    assert projected["source_order_inaccessible_results"][0]["line"] == 7
    assert projected["source_order_inaccessible_results"][0]["current_source_line"] == 7


def test_project_rg_body_match_without_name_fails_open(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "theorem assigned : True := by\n"
        "  sorry\n\n"
        "theorem future_result : True := by\n"
        "  exact True.intro\n",
        encoding="utf-8",
    )
    body_match = {
        "provider": "project-rg",
        "file": str(active),
        "line": 5,
        "preview": "exact True.intro",
    }

    projected = lean_search_horizon.partition_source_order_results(
        _payload(body_match),
        active_file=str(active),
        target_symbol="assigned",
        cwd=str(tmp_path),
    )

    assert projected["results"] == [body_match]
    assert "source_order_inaccessible_results" not in projected


def test_missing_horizon_context_leaves_payload_unchanged():
    payload = _payload({"provider": "leanexplore-local", "name": "Demo.future"})

    assert lean_search_horizon.partition_source_order_results(payload) == payload


def test_mcp_local_symbol_is_enriched_with_exact_private_declaration(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "namespace Demo\n\n"
        "private lemma prior_helper (n : Nat) : n = n := by\n"
        "  rfl\n\n"
        "theorem assigned : True := by\n"
        "  sorry\n\n"
        "private lemma future_helper : True := by\n"
        "  trivial\n\n"
        "end Demo\n",
        encoding="utf-8",
    )
    enriched = lean_search_horizon.enrich_local_source_results(
        _payload(
            {"provider": "mcp-local-search", "match": "Demo.prior_helper"},
            {"provider": "mcp-local-search", "match": "Demo.future_helper"},
        ),
        active_file=str(active),
        cwd=str(tmp_path),
    )

    assert enriched["results"][0]["name"] == "prior_helper"
    assert "private lemma prior_helper" in enriched["results"][0]["declaration"]
    assert enriched["results"][0]["local_source_enriched"] is True

    projected = lean_search_horizon.partition_source_order_results(
        enriched,
        active_file=str(active),
        target_symbol="assigned",
        cwd=str(tmp_path),
    )
    assert [result["name"] for result in projected["results"]] == ["prior_helper"]
    assert projected["source_order_inaccessible_results"][0]["name"] == "future_helper"


def test_mcp_local_symbol_with_unrelated_namespace_fails_open(tmp_path):
    active = tmp_path / "Demo.lean"
    active.write_text(
        "namespace Demo\nprivate lemma helper : True := by trivial\nend Demo\n",
        encoding="utf-8",
    )
    result = {"provider": "mcp-local-search", "match": "Imported.helper"}
    payload = _payload(result)

    assert (
        lean_search_horizon.enrich_local_source_results(
            payload,
            active_file=str(active),
            cwd=str(tmp_path),
        )
        == payload
    )
