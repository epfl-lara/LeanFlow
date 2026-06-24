"""Free academic / code web-search providers (arXiv, Semantic Scholar, Crossref, Sourcegraph).

Leaf module: the per-provider HTTP search functions, result normalization, and the
query-based provider-ordering router, plus their endpoint/timeout/stopword constants. Extracted
verbatim from tools/web_tools.py and re-exported there; imports only stdlib + requests (no
web_tools import), so there is no cycle. web_search_tool stays in web_tools and drives these
via the re-exported names (provider-tuple identity preserved for tests).
"""

import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urljoin

import requests

RESEARCH_SEARCH_USER_AGENT = "EPFLemma/0.3 free-research-search"
RESEARCH_SEARCH_TIMEOUT_SECONDS = 12
SOURCEGRAPH_SEARCH_TIMEOUT_SECONDS = 8
ARXIV_API_URL = "https://export.arxiv.org/api/query"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
CROSSREF_SEARCH_URL = "https://api.crossref.org/works"
SOURCEGRAPH_GRAPHQL_URL = "https://sourcegraph.com/.api/graphql"
CODE_SEARCH_STOPWORDS = {
    "lean",
    "coq",
    "rocq",
    "code",
    "theorem",
    "lemma",
    "proof",
    "example",
    "examples",
    "formalization",
    "formalisation",
    "mathlib",
}


def _normalize_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _truncate_text(value: Any, max_chars: int = 500) -> str:
    text = _normalize_whitespace(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _bounded_limit(limit: int, *, default: int = 5, maximum: int = 10) -> int:
    try:
        parsed = int(limit)
    except Exception:
        return default
    return max(1, min(maximum, parsed))


def _append_unique_result(
    results: list[dict[str, Any]], seen_urls: set[str], result: dict[str, Any]
) -> None:
    url = str(result.get("url") or "").strip()
    dedupe_key = url or f"{result.get('provider')}:{result.get('title')}:{result.get('snippet')}"
    if not dedupe_key or dedupe_key in seen_urls:
        return
    seen_urls.add(dedupe_key)
    result["position"] = len(results) + 1
    results.append(result)


def _research_headers() -> dict[str, str]:
    return {"User-Agent": RESEARCH_SEARCH_USER_AGENT}


def _arxiv_search_query(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_.+-]+", query)[:10]
    if not tokens:
        return f'all:"{query}"'
    return " AND ".join(f"all:{token}" for token in tokens)


def _search_arxiv(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    try:
        response = requests.get(
            ARXIV_API_URL,
            params={
                "search_query": _arxiv_search_query(query),
                "start": 0,
                "max_results": max(1, min(limit, 5)),
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
            headers=_research_headers(),
            timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
    except Exception as exc:
        return [], f"arXiv search unavailable: {exc}"

    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    results: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns)[:limit]:
        title = _normalize_whitespace(entry.findtext("atom:title", default="", namespaces=ns))
        url = _normalize_whitespace(entry.findtext("atom:id", default="", namespaces=ns))
        summary = _truncate_text(entry.findtext("atom:summary", default="", namespaces=ns))
        authors = [
            _normalize_whitespace(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
        ]
        authors = [author for author in authors if author]
        published = _normalize_whitespace(
            entry.findtext("atom:published", default="", namespaces=ns)
        )
        arxiv_id = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = link.attrib.get("href", "")
                break
        categories = [
            category.attrib.get("term", "")
            for category in entry.findall("atom:category", ns)
            if category.attrib.get("term")
        ]
        if title or url:
            results.append(
                {
                    "provider": "arxiv",
                    "kind": "paper",
                    "title": title,
                    "url": url,
                    "snippet": summary,
                    "authors": authors[:8],
                    "year": published[:4] if published else "",
                    "source": "arXiv",
                    "arxiv_id": arxiv_id,
                    "pdf_url": pdf_url,
                    "categories": categories[:6],
                }
            )
    return results, ""


def _search_semantic_scholar(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    try:
        response = requests.get(
            SEMANTIC_SCHOLAR_SEARCH_URL,
            params={
                "query": query,
                "limit": max(1, min(limit, 5)),
                "fields": "title,url,abstract,authors,year,venue,externalIds,citationCount,openAccessPdf",
            },
            headers=_research_headers(),
            timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            return [], "Semantic Scholar search throttled; retry later"
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], f"Semantic Scholar search unavailable: {exc}"

    results: list[dict[str, Any]] = []
    for item in payload.get("data", [])[:limit]:
        if not isinstance(item, dict):
            continue
        authors = [
            _normalize_whitespace(author.get("name"))
            for author in item.get("authors", [])
            if isinstance(author, dict) and author.get("name")
        ]
        external_ids = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        open_pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
        title = _normalize_whitespace(item.get("title"))
        url = _normalize_whitespace(item.get("url"))
        if title or url:
            results.append(
                {
                    "provider": "semantic-scholar",
                    "kind": "paper",
                    "title": title,
                    "url": url,
                    "snippet": _truncate_text(item.get("abstract")),
                    "authors": authors[:8],
                    "year": item.get("year") or "",
                    "source": _normalize_whitespace(item.get("venue")) or "Semantic Scholar",
                    "citation_count": item.get("citationCount"),
                    "external_ids": external_ids,
                    "pdf_url": _normalize_whitespace(open_pdf.get("url")) if open_pdf else "",
                }
            )
    return results, ""


def _crossref_year(item: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "created"):
        date_parts = (
            item.get(key, {}).get("date-parts") if isinstance(item.get(key), dict) else None
        )
        if date_parts and isinstance(date_parts, list) and date_parts[0]:
            return str(date_parts[0][0])
    return ""


def _crossref_authors(item: dict[str, Any]) -> list[str]:
    authors: list[str] = []
    for author in item.get("author", []) if isinstance(item.get("author"), list) else []:
        if not isinstance(author, dict):
            continue
        given = _normalize_whitespace(author.get("given"))
        family = _normalize_whitespace(author.get("family"))
        name = _normalize_whitespace(f"{given} {family}")
        if name:
            authors.append(name)
    return authors


def _search_crossref(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    try:
        response = requests.get(
            CROSSREF_SEARCH_URL,
            params={"query": query, "rows": max(1, min(limit, 5))},
            headers=_research_headers(),
            timeout=RESEARCH_SEARCH_TIMEOUT_SECONDS,
        )
        if response.status_code == 429:
            return [], "Crossref search throttled; retry later"
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return [], f"Crossref search unavailable: {exc}"

    items = payload.get("message", {}).get("items", [])
    results: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        title_values = item.get("title") if isinstance(item.get("title"), list) else []
        title = _normalize_whitespace(title_values[0] if title_values else "")
        url = _normalize_whitespace(
            item.get("URL") or item.get("DOI") and f"https://doi.org/{item.get('DOI')}"
        )
        container_values = (
            item.get("container-title") if isinstance(item.get("container-title"), list) else []
        )
        source = _normalize_whitespace(
            container_values[0] if container_values else item.get("publisher") or "Crossref"
        )
        abstract = re.sub(r"<[^>]+>", " ", str(item.get("abstract", "") or ""))
        snippet_parts = [
            part
            for part in (
                source,
                f"DOI: {item.get('DOI')}" if item.get("DOI") else "",
                _truncate_text(abstract, 350),
            )
            if part
        ]
        if title or url:
            results.append(
                {
                    "provider": "crossref",
                    "kind": "paper",
                    "title": title,
                    "url": url,
                    "snippet": _truncate_text(" - ".join(snippet_parts)),
                    "authors": _crossref_authors(item)[:8],
                    "year": _crossref_year(item),
                    "source": source,
                    "doi": _normalize_whitespace(item.get("DOI")),
                }
            )
    return results, ""


def _web_search_provider_order(query: str) -> tuple:
    lowered = query.lower()
    has_code_signal = any(token in lowered for token in (".lean", " coq", " rocq", ".v", " code"))
    has_identifier = bool(
        re.search(r"\b[A-Z][A-Za-z0-9]*\.[A-Za-z0-9_.]+|\b[a-zA-Z0-9]+_[a-zA-Z0-9_]+\b", query)
    )
    has_paper_signal = any(
        token in lowered
        for token in (
            "paper",
            "article",
            "arxiv",
            "doi",
            "citation",
            "formal proof",
            "formalization",
            "formalisation",
        )
    )
    if has_code_signal and not has_paper_signal:
        return (_search_sourcegraph_code,)
    if has_identifier and not has_paper_signal:
        return (_search_sourcegraph_code,)
    return (_search_arxiv, _search_semantic_scholar, _search_crossref, _search_sourcegraph_code)


def _sourcegraph_queries(query: str, limit: int) -> list[tuple[str, str, str]]:
    cleaned_query = _sourcegraph_code_terms(query)
    count = max(1, min(limit, 5))
    lowered = query.lower()
    wants_lean = "lean" in lowered or ".lean" in lowered
    wants_coq = "coq" in lowered or "rocq" in lowered or ".v" in lowered
    if wants_coq and not wants_lean:
        languages = [
            (
                "coq-rocq",
                "Coq/Rocq code",
                "repo:github.com/(rocq|coq|math-comp|rocq-community|coq-community) file:\\.v$",
            )
        ]
    elif wants_lean and not wants_coq:
        languages = [("lean", "Lean code", "file:\\.lean$")]
    else:
        languages = [
            ("lean", "Lean code", "file:\\.lean$"),
            ("coq-rocq", "Coq/Rocq code", "file:\\.v$"),
        ]
    queries: list[tuple[str, str, str]] = []
    for language, source, file_filter in languages:
        queries.append(
            (language, source, f"context:global {cleaned_query} {file_filter} count:{count}")
        )
        if " OR " not in cleaned_query and " " in cleaned_query:
            relaxed = " OR ".join(part for part in cleaned_query.split() if part)
            queries.append(
                (language, source, f"context:global ({relaxed}) {file_filter} count:{count}")
            )
    return queries


def _sourcegraph_code_terms(query: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_.+-]+", query)
    identifiers = [token for token in tokens if "." in token or "_" in token]
    if identifiers:
        return " ".join(identifiers[:3])
    terms = [
        token for token in tokens if len(token) > 2 and token.lower() not in CODE_SEARCH_STOPWORDS
    ]
    return " ".join(terms[:6]) or _normalize_whitespace(query)


def _search_sourcegraph_code(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    """Query Sourcegraph for code snippets in Lean/Coq repositories via GraphQL API. Generates language-specific queries (Lean and/or Coq/Rocq), fetches FileMatch results, and extracts repository, file path, code preview, and line numbers. Returns (results, error_msgs) where results is a list of code-match dicts and error_msgs is a semicolon-joined string of per-language failures."""
    graphql_query = """
query EPFLemmaCodeSearch($query: String!) {
  search(query: $query, version: V3) {
    results {
      results {
        __typename
        ... on FileMatch {
          repository { name url }
          file { path url }
          lineMatches { preview lineNumber }
        }
      }
    }
  }
}
"""
    results: list[dict[str, Any]] = []
    degraded: list[str] = []
    for language, source, sourcegraph_query in _sourcegraph_queries(query, limit):
        try:
            response = requests.post(
                SOURCEGRAPH_GRAPHQL_URL,
                json={"query": graphql_query, "variables": {"query": sourcegraph_query}},
                headers=_research_headers(),
                timeout=SOURCEGRAPH_SEARCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            degraded.append(f"Sourcegraph {language} search unavailable: {exc}")
            continue
        if payload.get("errors"):
            degraded.append(f"Sourcegraph {language} search returned errors")
            continue
        matches = payload.get("data", {}).get("search", {}).get("results", {}).get("results", [])
        for match in matches:
            if not isinstance(match, dict) or match.get("__typename") != "FileMatch":
                continue
            repo = match.get("repository") if isinstance(match.get("repository"), dict) else {}
            file_info = match.get("file") if isinstance(match.get("file"), dict) else {}
            repo_name = _normalize_whitespace(repo.get("name"))
            path = _normalize_whitespace(file_info.get("path"))
            file_url = urljoin(
                "https://sourcegraph.com", _normalize_whitespace(file_info.get("url"))
            )
            line_matches = (
                match.get("lineMatches") if isinstance(match.get("lineMatches"), list) else []
            )
            preview = ""
            line_number = None
            if line_matches:
                first_line = line_matches[0] if isinstance(line_matches[0], dict) else {}
                preview = _truncate_text(first_line.get("preview"), 300)
                line_number = first_line.get("lineNumber")
            url = f"{file_url}#L{line_number}" if line_number else file_url
            title = f"{repo_name}:{path}" if repo_name and path else path or repo_name
            if title or url:
                results.append(
                    {
                        "provider": "sourcegraph",
                        "kind": "code",
                        "title": title,
                        "url": url,
                        "snippet": preview,
                        "source": source,
                        "repository": repo_name,
                        "path": path,
                        "line": line_number,
                        "language": language,
                    }
                )
    return results, "; ".join(degraded)
