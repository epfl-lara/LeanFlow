from __future__ import annotations

import json
import threading

import pytest

from tools.implementations import web_tools
from tools.registry import registry


class FakeResponse:
    def __init__(self, *, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


ARXIV_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>https://arxiv.org/abs/2401.00001</id>
    <title>Prime Number Theorem in Formal Mathematics</title>
    <summary>A paper about formalized number theory.</summary>
    <published>2024-01-01T00:00:00Z</published>
    <author><name>Ada Lovelace</name></author>
    <link title="pdf" type="application/pdf" href="https://arxiv.org/pdf/2401.00001"/>
    <category term="math.NT"/>
  </entry>
</feed>
"""


def test_web_search_is_exposed_without_firecrawl_config(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)

    definitions = registry.get_definitions({"web_search"}, quiet=True)

    assert [item["function"]["name"] for item in definitions] == ["web_search"]
    description = definitions[0]["function"]["description"]
    assert "prefer lean_search" in description.lower()
    assert "web_fetch" in description


def test_web_search_uses_free_research_providers_and_no_firecrawl(monkeypatch):
    monkeypatch.setattr(web_tools, "Firecrawl", None)

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    def fake_get(url, *, params=None, headers=None, timeout=None):
        if url == web_tools.ARXIV_API_URL:
            return FakeResponse(text=ARXIV_FEED)
        if url == web_tools.SEMANTIC_SCHOLAR_SEARCH_URL:
            return FakeResponse(status_code=429, payload={})
        if url == web_tools.CROSSREF_SEARCH_URL:
            return FakeResponse(payload={"message": {"items": []}})
        if url == web_tools.DUCKDUCKGO_HTML_URL:
            return FakeResponse(text="")  # general web returns nothing parseable here
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, *, json=None, headers=None, timeout=None):
        assert url == web_tools.SOURCEGRAPH_GRAPHQL_URL
        return FakeResponse(
            payload={
                "data": {
                    "search": {
                        "results": {
                            "results": [
                                {
                                    "__typename": "FileMatch",
                                    "repository": {
                                        "name": "github.com/leanprover-community/mathlib4",
                                        "url": "/r/github.com/leanprover-community/mathlib4",
                                    },
                                    "file": {
                                        "path": "Mathlib/Data/Nat/Prime/Basic.lean",
                                        "url": "/r/github.com/leanprover-community/mathlib4/-/blob/Mathlib/Data/Nat/Prime/Basic.lean",
                                    },
                                    "lineMatches": [
                                        {"preview": "theorem Nat.Prime.dvd_mul", "lineNumber": 123}
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        )

    monkeypatch.setattr(web_tools.requests, "get", fake_get)
    monkeypatch.setattr(web_tools.requests, "post", fake_post)

    result = json.loads(
        web_tools.web_search_tool("prime number theorem formalization Lean", limit=3)
    )

    assert result["success"] is True
    assert "Firecrawl" not in json.dumps(result)
    providers = {item["provider"] for item in result["data"]["web"]}
    assert providers == {"arxiv", "sourcegraph"}
    assert {item["kind"] for item in result["data"]["web"]} == {"paper", "code"}
    assert "Semantic Scholar search throttled" in result["degraded_reasons"][0]


def test_clean_room_skips_code_search_and_filters_repository_results(monkeypatch):
    calls: list[str] = []

    def fake_sourcegraph(_query, _limit):
        calls.append("sourcegraph")
        return (
            [
                {
                    "provider": "sourcegraph",
                    "kind": "code",
                    "title": "Existing solution",
                    "url": "https://sourcegraph.com/github.com/example/repo/-/blob/P6.lean",
                    "snippet": "theorem result",
                }
            ],
            "",
        )

    def fake_general(_query, _limit):
        calls.append("general")
        return (
            [
                {
                    "provider": "duckduckgo",
                    "kind": "web",
                    "title": "Repository result",
                    "url": "https://github.com/example/repo",
                    "snippet": "Lean source",
                },
                {
                    "provider": "duckduckgo",
                    "kind": "web",
                    "title": "Mathematical discussion",
                    "url": "https://artofproblemsolving.com/wiki/example",
                    "snippet": "Proof idea",
                },
            ],
            "",
        )

    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setattr(web_tools, "_search_sourcegraph_code", fake_sourcegraph)
    monkeypatch.setattr(
        web_tools,
        "_web_search_provider_order",
        lambda _query: (fake_sourcegraph, fake_general),
    )

    result = json.loads(web_tools.web_search_tool("IMO problem proof", limit=5))

    assert calls == ["general"]
    assert [item["title"] for item in result["data"]["web"]] == ["Mathematical discussion"]


def test_clean_room_blocks_active_problem_search_before_provider_call(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setenv(
        "LEANFLOW_CLEAN_ROOM_TASK_LABELS",
        "IMO 2026 Problem 6|IMO2026 P6",
    )
    monkeypatch.setattr(
        web_tools,
        "_web_search_provider_order",
        lambda _query: pytest.fail("blocked solution query reached a provider"),
    )

    result = json.loads(
        web_tools.web_search_tool(
            "IMO 2026 problem 6 smallest gcd sequence periodic",
            limit=3,
        )
    )

    assert result["success"] is False
    assert result["status"] == "clean_room_solution_research_denied"


def test_clean_room_filters_solution_result_from_general_query(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setenv(
        "LEANFLOW_CLEAN_ROOM_TASK_LABELS",
        "IMO 2026 Problem 6|IMO2026 P6",
    )

    def fake_general(_query, _limit):
        return (
            [
                {
                    "provider": "duckduckgo",
                    "kind": "web",
                    "title": "IMO 2026 Problem 6 solution",
                    "url": "https://example.org/olympiad/p6",
                    "snippet": "Official proof",
                },
                {
                    "provider": "duckduckgo",
                    "kind": "web",
                    "title": "Finite intersecting hypergraphs",
                    "url": "https://example.org/hypergraphs",
                    "snippet": "General theorem",
                },
            ],
            "",
        )

    monkeypatch.setattr(web_tools, "_web_search_provider_order", lambda _query: (fake_general,))

    result = json.loads(web_tools.web_search_tool("finite hypergraph kernel", limit=3))

    assert [item["title"] for item in result["data"]["web"]] == ["Finite intersecting hypergraphs"]


def test_sourcegraph_code_query_keeps_identifiers_and_drops_filler():
    queries = web_tools._sourcegraph_queries("Nat.Prime theorem Lean code", limit=3)

    assert "Nat.Prime" in queries[0][2]
    assert "theorem" not in queries[0][2]
    assert queries[0][1] == "Lean code"


def test_code_only_query_skips_paper_providers():
    # Code queries skip the paper providers but still include general web.
    assert web_tools._web_search_provider_order("Nat.Prime dvd_mul Lean code") == (
        web_tools._search_sourcegraph_code,
        web_tools._search_general_web,
    )


def test_formal_proof_title_query_keeps_paper_providers():
    assert web_tools._web_search_provider_order(
        "A Formal Proof of the Irrationality of zeta(3) in Lean 4"
    ) == (
        web_tools._search_arxiv,
        web_tools._search_semantic_scholar,
        web_tools._search_crossref,
        web_tools._search_sourcegraph_code,
        web_tools._search_general_web,
    )


def test_coq_query_searches_v_files_only():
    queries = web_tools._sourcegraph_queries("Coq prime number theorem proof code", limit=3)

    assert queries
    assert all("file:\\.v$" in query for _language, _source, query in queries)
    assert all("file:\\.lean$" not in query for _language, _source, query in queries)


def test_crossref_result_is_normalized(monkeypatch):
    def fake_get(url, *, params=None, headers=None, timeout=None):
        assert url == web_tools.CROSSREF_SEARCH_URL
        return FakeResponse(
            payload={
                "message": {
                    "items": [
                        {
                            "title": ["A Formal Proof"],
                            "URL": "https://doi.org/10.1000/example",
                            "DOI": "10.1000/example",
                            "container-title": ["Proceedings"],
                            "author": [{"given": "Ada", "family": "Lovelace"}],
                            "published-online": {"date-parts": [[2025, 1, 1]]},
                        }
                    ]
                }
            }
        )

    monkeypatch.setattr(web_tools.requests, "get", fake_get)

    results, error = web_tools._search_crossref("formal proof", limit=2)

    assert error == ""
    assert results[0]["provider"] == "crossref"
    assert results[0]["kind"] == "paper"
    assert results[0]["authors"] == ["Ada Lovelace"]
    assert results[0]["year"] == "2025"


def test_semantic_scholar_result_is_normalized(monkeypatch):
    def fake_get(url, *, params=None, headers=None, timeout=None):
        assert url == web_tools.SEMANTIC_SCHOLAR_SEARCH_URL
        return FakeResponse(
            payload={
                "data": [
                    {
                        "title": "A Formal Proof",
                        "url": "https://www.semanticscholar.org/paper/example",
                        "abstract": "A long enough abstract.",
                        "authors": [{"name": "Grace Hopper"}],
                        "year": 2025,
                        "venue": "ITP",
                        "citationCount": 7,
                        "externalIds": {"ArXiv": "2501.00001"},
                        "openAccessPdf": {"url": "https://example.test/paper.pdf"},
                    }
                ]
            }
        )

    monkeypatch.setattr(web_tools.requests, "get", fake_get)

    results, error = web_tools._search_semantic_scholar("formal proof", limit=2)

    assert error == ""
    assert results == [
        {
            "provider": "semantic-scholar",
            "kind": "paper",
            "title": "A Formal Proof",
            "url": "https://www.semanticscholar.org/paper/example",
            "snippet": "A long enough abstract.",
            "authors": ["Grace Hopper"],
            "year": 2025,
            "source": "ITP",
            "citation_count": 7,
            "external_ids": {"ArXiv": "2501.00001"},
            "pdf_url": "https://example.test/paper.pdf",
        }
    ]


def test_arxiv_query_is_field_aware_not_and_of_all_tokens():
    q = web_tools._arxiv_search_query("liquid tensor experiment condensed")
    # Phrase clause against title/abstract + token OR-disjunction; never AND-of-all-tokens.
    assert "ti:" in q and "abs:" in q
    assert " OR " in q
    assert " AND " not in q
    # A single-token query collapses to a plain all: clause.
    assert web_tools._arxiv_search_query("propext") == "all:propext"


def test_research_headers_attach_semantic_scholar_key(monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("S2_API_KEY", raising=False)
    assert "x-api-key" not in web_tools._research_headers()
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "sk-s2-test")
    assert web_tools._research_headers()["x-api-key"] == "sk-s2-test"


def test_web_search_schema_exposes_limit():
    props = web_tools.WEB_SEARCH_SCHEMA["parameters"]["properties"]
    assert "limit" in props
    assert props["limit"]["maximum"] == 10


def test_duckduckgo_html_is_parsed(monkeypatch):
    html_doc = (
        '<a rel="nofollow" class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fpage&rut=z">'
        "Example &amp; Title</a>"
        '<a class="result__snippet" href="x">A useful <b>snippet</b> here.</a>'
    )

    def fake_get(url, *, params=None, headers=None, timeout=None):
        assert "duckduckgo" in url
        return FakeResponse(text=html_doc)

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(web_tools.requests, "get", fake_get)

    results, error = web_tools._search_duckduckgo_html("example query", limit=5)
    assert error == ""
    assert results == [
        {
            "provider": "duckduckgo",
            "kind": "web",
            "title": "Example & Title",
            "url": "https://example.org/page",
            "snippet": "A useful snippet here.",
        }
    ]


def test_duckduckgo_antibot_challenge_is_reported(monkeypatch):
    html_doc = """
    <html><body>
      <form id="challenge-form" class="anomaly-modal">Bots use DuckDuckGo too.</form>
    </body></html>
    """

    def fake_get(url, *, params=None, headers=None, timeout=None):
        return FakeResponse(text=html_doc, status_code=202)

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(web_tools.requests, "get", fake_get)

    results, error = web_tools._search_duckduckgo_html("example query", limit=5)

    assert results == []
    assert error == "DuckDuckGo blocked this anonymous search with an anti-bot challenge"


def test_general_web_falls_back_to_bing_when_duckduckgo_is_blocked(monkeypatch):
    bing_html = """
    <ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://lean-lang.org/documentation/">Lean Documentation</a></h2>
        <div class="b_caption"><p>Official Lean 4 installation and reference documentation.</p></div>
      </li>
    </ol>
    """

    def fake_get(url, *, params=None, headers=None, timeout=None):
        if url == web_tools.DUCKDUCKGO_HTML_URL:
            return FakeResponse(
                text='<form class="anomaly-modal">Bots use DuckDuckGo too.</form>',
                status_code=202,
            )
        if url == web_tools.BING_SEARCH_URL:
            assert params["q"] == "Lean 4 installation documentation"
            return FakeResponse(text=bing_html)
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr(web_tools.requests, "get", fake_get)

    results, error = web_tools._search_general_web(
        "Lean 4 installation documentation",
        limit=3,
    )

    assert results == [
        {
            "provider": "bing",
            "kind": "web",
            "title": "Lean Documentation",
            "url": "https://lean-lang.org/documentation/",
            "snippet": "Official Lean 4 installation and reference documentation.",
        }
    ]
    assert "DuckDuckGo blocked" in error
    assert "Bing fallback succeeded" in error


def test_bing_rejects_generic_results_and_retries_with_entity_query(monkeypatch):
    generic_html = """
    <ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://www.cnn.com/">CNN</a></h2>
        <div class="b_caption"><p>Latest breaking news and headlines.</p></div>
      </li>
    </ol>
    """
    relevant_html = """
    <ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://lean-lang.org/lean4/doc/setup.html">Install Lean 4</a></h2>
        <div class="b_caption"><p>Set up the Lean theorem prover.</p></div>
      </li>
    </ol>
    """
    queries: list[str] = []

    def fake_get(url, *, params=None, headers=None, timeout=None):
        assert url == web_tools.BING_SEARCH_URL
        queries.append(params["q"])
        return FakeResponse(text=generic_html if len(queries) == 1 else relevant_html)

    monkeypatch.setattr(web_tools.requests, "get", fake_get)

    results, error = web_tools._search_bing_html(
        "latest Lean 4 installation documentation",
        limit=3,
    )

    assert error == ""
    assert queries == [
        "latest Lean 4 installation documentation",
        "Lean 4 installation",
    ]
    assert [item["title"] for item in results] == ["Install Lean 4"]


def test_bing_requires_more_than_a_broad_topic_match():
    from tools.implementations import web_research_providers as wp

    results = wp._filter_bing_results(
        [
            {
                "provider": "bing",
                "kind": "web",
                "title": "Welcome to Python.org",
                "url": "https://www.python.org/",
                "snippet": "Learn the Python programming language.",
            }
        ],
        "Python 3.12 typing documentation",
    )

    assert results == []


def test_github_repository_search_retries_without_version_noise(monkeypatch):
    from tools.implementations import web_research_providers as wp

    queries: list[str] = []

    def fake_get(url, *, params=None, headers=None, timeout=None):
        assert url == wp.GITHUB_REPOSITORY_SEARCH_URL
        queries.append(params["q"])
        if len(queries) == 1:
            return FakeResponse(payload={"items": []})
        return FakeResponse(
            payload={
                "items": [
                    {
                        "full_name": "python/typing",
                        "html_url": "https://github.com/python/typing",
                        "description": "Python static typing documentation.",
                        "clone_url": "https://github.com/python/typing.git",
                        "default_branch": "main",
                        "language": "Python",
                        "stargazers_count": 1700,
                        "updated_at": "2026-07-01T00:00:00Z",
                    }
                ]
            }
        )

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(wp.requests, "get", fake_get)

    results, error = wp._search_github_repositories(
        "Python 3.12 typing documentation",
        limit=3,
    )

    assert error == ""
    assert queries == ["Python 3.12 typing", "Python typing"]
    assert results[0]["provider"] == "github"
    assert results[0]["clone_url"] == "https://github.com/python/typing.git"

    cached_results, cached_error = wp._search_github_repositories(
        "Python 3.12 typing documentation",
        limit=3,
    )
    assert cached_error == ""
    assert cached_results == results
    assert queries == ["Python 3.12 typing", "Python typing"]


def test_github_repository_search_is_skipped_in_clean_room(monkeypatch):
    from tools.implementations import web_research_providers as wp

    monkeypatch.setenv("LEANFLOW_DISABLE_REPOSITORY_RESEARCH", "1")
    monkeypatch.setattr(
        wp.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("clean-room GitHub search reached network"),
    )

    results, error = wp._search_github_repositories("Lean 4", limit=3)

    assert results == []
    assert "clean-room policy" in error


def test_general_web_uses_github_after_anonymous_engines_fail(monkeypatch):
    from tools.implementations import web_research_providers as wp

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setattr(
        wp,
        "_search_duckduckgo_html",
        lambda _query, _limit: ([], "DuckDuckGo blocked"),
    )
    monkeypatch.setattr(
        wp,
        "_search_bing_html",
        lambda _query, _limit: ([], "Bing returned no relevant results"),
    )
    monkeypatch.setattr(
        wp,
        "_search_github_repositories",
        lambda _query, _limit: (
            [
                {
                    "provider": "github",
                    "kind": "repository",
                    "title": "leanprover/lean4",
                    "url": "https://github.com/leanprover/lean4",
                    "snippet": "Lean 4 programming language and theorem prover",
                }
            ],
            "",
        ),
    )

    results, error = wp._search_general_web("Lean 4 installation", limit=3)

    assert [item["provider"] for item in results] == ["github"]
    assert "GitHub repository fallback succeeded" in error


def test_duckduckgo_href_decode_preserves_encoded_values():
    from tools.implementations import web_research_providers as wp

    # parse_qs decodes uddg once; the target URL keeps its own encoded value (no double-decode).
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fex.org%2Fq%3Fa%3D1%252B2"
    assert wp._decode_duckduckgo_href(href) == "https://ex.org/q?a=1%2B2"


def test_general_web_prefers_tavily_when_keyed(monkeypatch):
    def fake_post(url, *, json=None, headers=None, timeout=None):
        assert json["query"] == "lean 4 install"
        return FakeResponse(
            payload={
                "results": [
                    {"title": "Install Lean", "url": "https://t.example/1", "content": "steps"}
                ]
            }
        )

    monkeypatch.setenv("TAVILY_API_KEY", "tv-key")
    monkeypatch.setattr(web_tools.requests, "post", fake_post)

    results, error = web_tools._search_general_web("lean 4 install", limit=3)
    assert error == ""
    assert results[0]["provider"] == "tavily"
    assert results[0]["kind"] == "web"
    assert results[0]["url"] == "https://t.example/1"


def test_general_web_uses_exa_when_keyed_without_tavily(monkeypatch):
    def fake_post(url, *, json=None, headers=None, timeout=None):
        assert url == web_tools.EXA_SEARCH_URL
        assert headers["x-api-key"] == "exa-key"
        assert json["query"] == "lean 4 latest documentation"
        assert json["contents"]["highlights"] is True
        return FakeResponse(
            payload={
                "results": [
                    {
                        "title": "Lean documentation",
                        "url": "https://lean-lang.org/documentation/",
                        "highlights": ["Official documentation and installation instructions."],
                        "publishedDate": "2026-07-01T00:00:00Z",
                        "author": "Lean FRO",
                    }
                ]
            }
        )

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("EXA_API_KEY", "exa-key")
    monkeypatch.setattr(web_tools.requests, "post", fake_post)

    results, error = web_tools._search_general_web("lean 4 latest documentation", limit=3)

    assert error == ""
    assert results == [
        {
            "provider": "exa",
            "kind": "web",
            "title": "Lean documentation",
            "url": "https://lean-lang.org/documentation/",
            "snippet": "Official documentation and installation instructions.",
            "source": "Exa",
            "author": "Lean FRO",
            "published": "2026-07-01T00:00:00Z",
        }
    ]


def test_provider_order_always_includes_general_web():
    from tools.implementations import web_research_providers as wp

    # Plain query -> general web leads.
    assert wp._search_general_web in wp._web_search_provider_order("how to install lean 4 mathlib")
    # Code query -> sourcegraph + general.
    code_order = wp._web_search_provider_order("Finset.sum_range code")
    assert wp._search_general_web in code_order and wp._search_sourcegraph_code in code_order
    # Paper query -> academic + general.
    paper_order = wp._web_search_provider_order("liquid tensor experiment paper")
    assert wp._search_general_web in paper_order and wp._search_arxiv in paper_order


def test_web_search_runs_independent_providers_concurrently(monkeypatch):
    rendezvous = threading.Barrier(2)

    def first_provider(_query, _limit):
        rendezvous.wait(timeout=1)
        return (
            [
                {
                    "provider": "first",
                    "kind": "web",
                    "title": "First source",
                    "url": "https://first.example/source",
                    "snippet": "relevant source",
                }
            ],
            "",
        )

    def second_provider(_query, _limit):
        rendezvous.wait(timeout=1)
        return (
            [
                {
                    "provider": "second",
                    "kind": "web",
                    "title": "Second source",
                    "url": "https://second.example/source",
                    "snippet": "relevant source",
                }
            ],
            "",
        )

    monkeypatch.setattr(
        web_tools,
        "_web_search_provider_order",
        lambda _query: (first_provider, second_provider),
    )

    result = json.loads(web_tools.web_search_tool("relevant source", limit=2))

    assert result["success"] is True
    assert [item["provider"] for item in result["data"]["web"]] == ["first", "second"]
    assert {item["status"] for item in result["provider_status"]} == {"ok"}


def test_web_search_isolates_provider_exceptions(monkeypatch):
    def broken_provider(_query, _limit):
        raise RuntimeError("provider exploded")

    def healthy_provider(_query, _limit):
        return (
            [
                {
                    "provider": "healthy",
                    "kind": "web",
                    "title": "Useful source",
                    "url": "https://healthy.example/source",
                    "snippet": "useful evidence",
                }
            ],
            "",
        )

    monkeypatch.setattr(
        web_tools,
        "_web_search_provider_order",
        lambda _query: (broken_provider, healthy_provider),
    )

    result = json.loads(web_tools.web_search_tool("useful evidence", limit=2))

    assert result["success"] is True
    assert [item["provider"] for item in result["data"]["web"]] == ["healthy"]
    assert any("provider exploded" in reason for reason in result["degraded_reasons"])
    assert any(item["status"] == "error" for item in result["provider_status"])


def test_empty_web_search_is_retryable_not_success(monkeypatch):
    def empty_provider(_query, _limit):
        return [], ""

    monkeypatch.setattr(
        web_tools,
        "_web_search_provider_order",
        lambda _query: (empty_provider,),
    )

    result = json.loads(web_tools.web_search_tool("rare research topic", limit=2))

    assert result["success"] is False
    assert result["status"] == "no_results"
    assert result["retryable"] is True
    assert "do not treat one empty search as exhausted research" in result["degraded"]


def test_web_search_deep_mode_accepts_alternate_queries_and_merges_provenance(monkeypatch):
    calls: list[str] = []

    def provider(query, _limit):
        calls.append(query)
        return (
            [
                {
                    "provider": "example",
                    "kind": "paper",
                    "title": "One formal proof",
                    "url": (
                        "https://arxiv.org/pdf/2501.00001.pdf"
                        if query == "primary formulation"
                        else "https://arxiv.org/abs/2501.00001v2"
                    ),
                    "snippet": f"matched {query}",
                    "arxiv_id": "2501.00001",
                }
            ],
            "",
        )

    monkeypatch.setattr(
        web_tools,
        "_web_search_provider_order",
        lambda _query: (provider,),
    )

    result = json.loads(
        web_tools.web_search_tool(
            "primary formulation",
            limit=5,
            search_depth="deep",
            alternate_queries=["equivalent formulation"],
        )
    )

    assert set(calls) == {"primary formulation", "equivalent formulation"}
    assert result["queries"] == ["primary formulation", "equivalent formulation"]
    assert result["search_depth"] == "deep"
    assert len(result["data"]["web"]) == 1
    assert result["data"]["web"][0]["matched_queries"] == [
        "primary formulation",
        "equivalent formulation",
    ]
    assert result["data"]["web"][0]["source_id"] == "web-1"


def test_web_search_blocks_clean_room_alternate_query_before_any_provider_call(monkeypatch):
    monkeypatch.setenv("LEANFLOW_DISABLE_SOLUTION_RESEARCH", "1")
    monkeypatch.setenv(
        "LEANFLOW_CLEAN_ROOM_TASK_LABELS",
        "IMO 2026 Problem 6|IMO2026 P6",
    )
    monkeypatch.setattr(
        web_tools,
        "_web_search_provider_order",
        lambda _query: pytest.fail("blocked alternate query reached provider routing"),
    )

    result = json.loads(
        web_tools.web_search_tool(
            "finite intersecting hypergraphs",
            search_depth="deep",
            alternate_queries=["IMO 2026 Problem 6 solution"],
        )
    )

    assert result["success"] is False
    assert result["status"] == "clean_room_solution_research_denied"


def test_documentation_query_uses_general_search_without_paper_fanout():
    from tools.implementations import web_research_providers as wp

    assert wp._web_search_provider_order("latest Lean 4 installation documentation") == (
        wp._search_general_web,
    )
