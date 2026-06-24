from __future__ import annotations

import json

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
    assert "prefer lean_search first" in description
    assert "theorem/proof lookup" in description


def test_web_search_uses_free_research_providers_and_no_firecrawl(monkeypatch):
    monkeypatch.setattr(web_tools, "Firecrawl", None)

    def fake_get(url, *, params=None, headers=None, timeout=None):
        if url == web_tools.ARXIV_API_URL:
            return FakeResponse(text=ARXIV_FEED)
        if url == web_tools.SEMANTIC_SCHOLAR_SEARCH_URL:
            return FakeResponse(status_code=429, payload={})
        if url == web_tools.CROSSREF_SEARCH_URL:
            return FakeResponse(payload={"message": {"items": []}})
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


def test_sourcegraph_code_query_keeps_identifiers_and_drops_filler():
    queries = web_tools._sourcegraph_queries("Nat.Prime theorem Lean code", limit=3)

    assert "Nat.Prime" in queries[0][2]
    assert "theorem" not in queries[0][2]
    assert queries[0][1] == "Lean code"


def test_code_only_query_skips_paper_providers():
    assert web_tools._web_search_provider_order("Nat.Prime dvd_mul Lean code") == (
        web_tools._search_sourcegraph_code,
    )


def test_formal_proof_title_query_keeps_paper_providers():
    assert web_tools._web_search_provider_order(
        "A Formal Proof of the Irrationality of zeta(3) in Lean 4"
    ) == (
        web_tools._search_arxiv,
        web_tools._search_semantic_scholar,
        web_tools._search_crossref,
        web_tools._search_sourcegraph_code,
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
